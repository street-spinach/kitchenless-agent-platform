"""The model client and bounded choices (architecture §6, item 22).

This module is the only place that calls a model. It has two profiles:

* `people` (consumer and household graphs): `ModelClient.complete`. In `real` mode it makes
  up to 2 attempts on the primary model (20 s each, 2 s apart, on a timeout, 429 or 5xx) and
  then 1 on the fallback model, within 60 s per step. It raises `ModelUnavailable`,
  `TokenBudgetExceeded` or `InvalidOutput`.
* `business` (grocery, restaurant and delivery handlers): `ModelClient.choose`. One attempt,
  `MODEL_BUSINESS_TIMEOUT_SECONDS`, `max_tokens=300`, then a JSON-schema check, the
  allowed-set check and the rationale check. It never raises; any failure gives the rule
  default computed by the caller.

`MODEL_MODE=stub` never touches the network: it calls `services/agents/<class>/stubs.py`,
whose `STUBS` maps a graph node or choice point name to a function. The run's overrides
(`STUB_FAIL_NODE`, `STUB_OUT_OF_BOUNDS`, stored in `runs.trigger.overrides`) and token total
are loaded by `ctx.run_id`.
"""

import asyncio
import importlib
import json
import logging
import math
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

import httpx

from services.common.config import Settings, get_settings
from services.common.ids import parse_agent_id
from services.common.run_context import RunContext

__all__ = [
    "Actions",
    "Allowed",
    "Choice",
    "InvalidOutput",
    "ModelClient",
    "ModelHTTPError",
    "ModelResult",
    "ModelUnavailable",
    "OneOf",
    "PerItem",
    "Range",
    "Subset",
    "TEMPLATES",
    "TokenBudgetExceeded",
    "ToolCall",
    "check_rationale",
]

log = logging.getLogger(__name__)

Profile = Literal["people", "business"]

# `people` profile timing (§6).
ATTEMPT_TIMEOUT_SECONDS = 20.0
PRIMARY_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 2.0
STEP_CAP_SECONDS = 60.0
# `business` profile output cap (§6).
BUSINESS_MAX_TOKENS = 300
RATIONALE_MAX_CHARS = 200

STUB_MODEL = "stub"

# Template rationales, used when the model's rationale fails the checks or the rule default
# is used. They contain no numbers, so they always pass `check_rationale`.
TEMPLATES: Mapping[str, str] = {
    "rank_substitutes": "Substitutes follow the store's ranking: same category, then price.",
    "respond_to_counter": "Our answer follows the store's pricing rules.",
    "choose_window": "The earliest window our courier capacity allows.",
}
_DEFAULT_TEMPLATE = "Chosen by the business rules."


class ModelUnavailable(RuntimeError):
    """The model could not answer (all attempts failed, or `STUB_FAIL_NODE`)."""


class TokenBudgetExceeded(RuntimeError):
    """The run's token total reached `RUN_TOKEN_CEILING` before a `people` call."""


class InvalidOutput(RuntimeError):
    """The model's answer could not be parsed (for example, tool arguments that aren't JSON)."""


class ModelHTTPError(RuntimeError):
    """The model API answered with a non-2xx status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"model API returned HTTP {status}")
        self.status = status


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResult:
    """One model answer. Stubs return it too, with `model="stub"`."""

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = STUB_MODEL
    latency_ms: int = 0


@dataclass(frozen=True)
class Choice:
    """A bounded choice: `source` is `model` or `rule_default`; `reason` is `None` or a code."""

    value: Any
    rationale: str
    source: Literal["model", "rule_default"]
    reason: str | None


# Allowed sets (item 22). Each has one checker and a JSON description for the trace row.


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return False
    return not (isinstance(value, float) and not math.isfinite(value))


def _hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


class Allowed(Protocol):
    def check(self, pick: Any) -> bool: ...

    def describe(self) -> Any: ...


@dataclass(frozen=True)
class OneOf:
    """The pick is one of `values`."""

    values: tuple[Any, ...]

    def __init__(self, values: Sequence[Any]) -> None:
        object.__setattr__(self, "values", tuple(values))

    def check(self, pick: Any) -> bool:
        return any(pick == v and type(pick) is type(v) for v in self.values)

    def describe(self) -> Any:
        return {"one_of": list(self.values)}


@dataclass(frozen=True)
class Range:
    """The pick is a number with `low <= pick <= high`."""

    low: int | float | Decimal
    high: int | float | Decimal

    def check(self, pick: Any) -> bool:
        return _is_number(pick) and self.low <= pick <= self.high

    def describe(self) -> Any:
        return {"range": [_jsonable(self.low), _jsonable(self.high)]}


@dataclass(frozen=True)
class Subset:
    """The pick is an ordered list of distinct members of `values`, at most `max_len` long."""

    values: tuple[Any, ...]
    max_len: int

    def __init__(self, values: Sequence[Any], max_len: int) -> None:
        object.__setattr__(self, "values", tuple(values))
        object.__setattr__(self, "max_len", max_len)

    def check(self, pick: Any) -> bool:
        if not isinstance(pick, list) or len(pick) > self.max_len:
            return False
        if not all(_hashable(p) and p in self.values for p in pick):
            return False
        return len(set(pick)) == len(pick)

    def describe(self) -> Any:
        return {"subset": list(self.values), "max_len": self.max_len}


@dataclass(frozen=True)
class Actions:
    """The pick is `{"action": <name>, "arg": <value>}`; `arg` passes that action's checker.

    An action mapped to `None` takes no argument (`arg` is absent or `null`).
    """

    actions: Mapping[str, Allowed | None]

    def check(self, pick: Any) -> bool:
        if not isinstance(pick, dict) or not set(pick) <= {"action", "arg"}:
            return False
        action = pick.get("action")
        if not isinstance(action, str) or action not in self.actions:
            return False
        checker = self.actions[action]
        if checker is None:
            return pick.get("arg") is None
        return "arg" in pick and checker.check(pick["arg"])

    def describe(self) -> Any:
        return {
            "actions": {
                name: (None if c is None else c.describe()) for name, c in self.actions.items()
            }
        }


@dataclass(frozen=True)
class PerItem:
    """One `Subset` pick per missing item (`{sku: [..]}`), checked one by one."""

    items: Mapping[str, Subset]

    def check(self, pick: Any) -> bool:
        if not isinstance(pick, dict) or set(pick) != set(self.items):
            return False
        return all(self.items[sku].check(sub) for sku, sub in pick.items())

    def describe(self) -> Any:
        return {"per_item": {sku: s.describe() for sku, s in self.items.items()}}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


# Rationale check (§4 "Rationale rules").

_URL_RE = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _to_decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


def _numbers_in(value: Any) -> Iterator[Decimal]:
    if isinstance(value, bool) or value is None:
        return
    if _is_number(value):
        yield Decimal(str(value))
    elif isinstance(value, str):
        for token in _NUMBER_RE.findall(value):
            if (d := _to_decimal(token)) is not None:
                yield d
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield from _numbers_in(k)
            yield from _numbers_in(v)
    elif isinstance(value, list | tuple | set | frozenset):
        for v in value:
            yield from _numbers_in(v)


def check_rationale(text: Any, payload_numbers: Any) -> str | None:
    """The cleaned rationale, or `None` if it fails the rules.

    Strips URLs and control characters, then requires at most 200 characters and every number
    token to equal a number in `payload_numbers` (any JSON-like value; numbers inside its
    strings count too).
    """
    if not isinstance(text, str):
        return None
    cleaned = _URL_RE.sub("", text)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.category(ch).startswith("C"))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or len(cleaned) > RATIONALE_MAX_CHARS:
        return None
    allowed = set(_numbers_in(payload_numbers))
    for token in _NUMBER_RE.findall(cleaned):
        d = _to_decimal(token)
        if d is None or d not in allowed:
            return None
    return cleaned


# Collaborators. Each has a Postgres or HTTP default and can be replaced in tests.


class RunState(Protocol):
    """Loads a run's overrides and token total by `run_id`."""

    async def overrides(self, ctx: RunContext) -> Mapping[str, Any]: ...

    async def token_total(self, ctx: RunContext) -> int: ...


class TraceSink(Protocol):
    """The subset of `services.common.trace.TraceStore` the client uses."""

    async def write(
        self,
        run_id: Any,
        agent_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        on_behalf_of: str | None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        conversation_id: Any = None,
        model: str | None = None,
        latency_ms: int | None = None,
    ) -> int: ...


class Transport(Protocol):
    """POSTs one OpenAI-compatible chat completion; raises `TimeoutError` or `ModelHTTPError`."""

    async def post(
        self, base_url: str, api_key: str | None, body: dict[str, Any], timeout: float
    ) -> dict[str, Any]: ...


class PgRunState:
    """Reads `runs.trigger.overrides` and the sum of the run's trace token columns."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def overrides(self, ctx: RunContext) -> Mapping[str, Any]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT trigger->'overrides' FROM runs WHERE run_id = %s", (ctx.run_id,)
            )
            row = await cur.fetchone()
        value = row[0] if row else None
        return value if isinstance(value, dict) else {}

    async def token_total(self, ctx: RunContext) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0)"
                " FROM agent_traces WHERE run_id = %s",
                (ctx.run_id,),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0


class HttpxTransport:
    """The OpenAI-compatible `POST <base_url>/chat/completions`."""

    async def post(
        self, base_url: str, api_key: str | None, body: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        url = base_url.rstrip("/") + "/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await asyncio.wait_for(client.post(url, json=body, headers=headers), timeout)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise TimeoutError("model call timed out") from exc
        if resp.status_code >= 300:
            raise ModelHTTPError(resp.status_code)
        data = resp.json()
        if not isinstance(data, dict):
            raise InvalidOutput("response is not a JSON object")
        return data


StubFn = Callable[..., ModelResult]


def _load_stubs(agent_class: str) -> Mapping[str, StubFn]:
    module = importlib.import_module(f"services.agents.{agent_class}.stubs")
    return getattr(module, "STUBS", {})


def _parse_completion(data: dict[str, Any], model: str, latency_ms: int) -> ModelResult:
    try:
        message = data["choices"][0]["message"]
        calls = []
        for call in message.get("tool_calls") or []:
            fn = call["function"]
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("tool arguments are not an object")
            calls.append(ToolCall(name=fn["name"], arguments=args))
        text = message.get("content")
        if text is not None and not isinstance(text, str):
            raise ValueError("content is not a string")
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise InvalidOutput(f"unparseable model answer: {type(exc).__name__}") from None
    usage = data.get("usage") or {}
    return ModelResult(
        text=text,
        tool_calls=tuple(calls),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        model=str(data.get("model") or model),
        latency_ms=latency_ms,
    )


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ModelHTTPError):
        return f"http_{exc.status}"
    return "model_unavailable"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, ModelHTTPError):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, TimeoutError | httpx.TransportError)


class ModelClient:
    """The one model client. One instance per process; every call names its run by `ctx`."""

    def __init__(
        self,
        traces: TraceSink,
        run_state: RunState,
        *,
        settings: Settings | None = None,
        transport: Transport | None = None,
        stubs: Callable[[str], Mapping[str, StubFn]] = _load_stubs,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._traces = traces
        self._runs = run_state
        self._settings = settings or get_settings()
        self._transport = transport or HttpxTransport()
        self._stubs = stubs
        self._clock = clock
        self._sleep = sleep
        mode = self._settings.model_mode or "stub"
        if mode not in {"real", "stub"}:
            raise ValueError(f"MODEL_MODE must be 'real' or 'stub', got {mode!r}")
        self.mode = mode

    # Shared helpers.

    async def _trace(self, ctx: RunContext, kind: str, payload: dict[str, Any], **cols: Any):
        await self._traces.write(
            ctx.run_id,
            ctx.agent_id,
            kind,
            payload,
            on_behalf_of=None,
            conversation_id=ctx.conversation_id,
            **cols,
        )

    async def _over_ceiling(self, ctx: RunContext) -> bool:
        ceiling = self._settings.run_token_ceiling
        if ceiling is None:
            return False
        return await self._runs.token_total(ctx) >= ceiling

    def _stub(self, ctx: RunContext, name: str) -> StubFn | None:
        agent_class, _ = parse_agent_id(ctx.agent_id)
        return self._stubs(agent_class).get(name)

    # `people` profile.

    async def complete(
        self,
        ctx: RunContext,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        node: str,
        *,
        profile: Profile = "people",
        state: Mapping[str, Any] | None = None,
    ) -> ModelResult:
        """One model step for graph node `node`. Stub mode passes `state` to the node's stub."""
        if profile != "people":
            raise ValueError("complete() serves the people profile; business agents use choose()")
        if await self._over_ceiling(ctx):
            await self._trace(
                ctx, "error", {"where": f"model:{node}", "reason": "token_budget", "attempt": 0}
            )
            raise TokenBudgetExceeded(node)
        if self.mode == "stub":
            result = await self._complete_stub(ctx, node, messages, tools, state)
        else:
            result = await self._complete_real(ctx, messages, tools, node)
        await self._trace(
            ctx,
            "decision",
            {
                "node": node,
                "choice": [c.name for c in result.tool_calls] or "text",
                "explanation": "",
            },
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            model=result.model,
            latency_ms=result.latency_ms,
        )
        return result

    async def _complete_stub(
        self,
        ctx: RunContext,
        node: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        state: Mapping[str, Any] | None,
    ) -> ModelResult:
        overrides = await self._runs.overrides(ctx)
        if overrides.get("STUB_FAIL_NODE") == node:
            await self._trace(
                ctx,
                "error",
                {"where": f"model:{node}", "reason": "model_unavailable", "attempt": 1},
                model=STUB_MODEL,
            )
            raise ModelUnavailable(node)
        stub = self._stub(ctx, node)
        if stub is None:
            raise ModelUnavailable(f"no stub for node {node!r}")
        return stub(state if state is not None else {"messages": messages, "tools": tools})

    async def _complete_real(
        self,
        ctx: RunContext,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        node: str,
    ) -> ModelResult:
        s = self._settings
        targets: list[tuple[str | None, str | None, str | None]] = [
            (s.model_base_url, s.model_name, s.model_api_key)
        ] * PRIMARY_ATTEMPTS
        fallback = (s.model_fallback_base_url, s.model_fallback_name, s.model_fallback_api_key)
        deadline = self._clock() + STEP_CAP_SECONDS
        attempt = 0
        on_primary = True
        while targets:
            base_url, model, api_key = targets.pop(0)
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            attempt += 1
            body: dict[str, Any] = {"model": model, "messages": list(messages)}
            if tools:
                body["tools"] = list(tools)
            started = self._clock()
            try:
                if not base_url or not model:
                    raise ModelUnavailable("model not configured")
                timeout = min(ATTEMPT_TIMEOUT_SECONDS, remaining)
                data = await asyncio.wait_for(
                    self._transport.post(base_url, api_key, body, timeout), timeout
                )
                latency_ms = int((self._clock() - started) * 1000)
                return _parse_completion(data, model, latency_ms)
            except InvalidOutput:
                await self._trace(
                    ctx,
                    "error",
                    {
                        "where": f"model:{node}",
                        "reason": "model_invalid_output",
                        "attempt": attempt,
                    },
                    model=model,
                )
                raise
            except Exception as exc:
                await self._trace(
                    ctx,
                    "error",
                    {"where": f"model:{node}", "reason": _failure_reason(exc), "attempt": attempt},
                    model=model,
                    latency_ms=int((self._clock() - started) * 1000),
                )
                if on_primary and targets and _retryable(exc):
                    if deadline - self._clock() <= RETRY_DELAY_SECONDS:
                        break
                    await self._sleep(RETRY_DELAY_SECONDS)
                    continue
            if on_primary:
                # Primary attempts are used up (or not retryable): one try on the fallback.
                on_primary = False
                targets = [fallback]
        raise ModelUnavailable(node)

    # `business` profile.

    async def choose(
        self,
        ctx: RunContext,
        point: str,
        allowed: Allowed,
        default: Any,
        inputs: Mapping[str, Any],
    ) -> Choice:
        """A bounded choice at `point`. Never raises; writes one `decision` row."""
        template = TEMPLATES.get(point, _DEFAULT_TEMPLATE)
        choice = Choice(default, template, "rule_default", "model_unavailable")
        model_pick: Any = None
        cols: dict[str, Any] = {}
        try:
            choice, model_pick, cols = await self._choose(
                ctx, point, allowed, default, inputs, template
            )
        except Exception:
            log.exception("choose(%s) failed; using the rule default", point)
        try:
            await self._trace(
                ctx,
                "decision",
                {
                    "node": point,
                    "choice": _jsonable(choice.value),
                    "explanation": choice.rationale,
                    "allowed": allowed.describe(),
                    "model_pick": model_pick,
                    "source": choice.source,
                    "reason": choice.reason,
                },
                **cols,
            )
        except Exception:
            log.exception("choose(%s): decision trace write failed", point)
        return choice

    async def _choose(
        self,
        ctx: RunContext,
        point: str,
        allowed: Allowed,
        default: Any,
        inputs: Mapping[str, Any],
        template: str,
    ) -> tuple[Choice, Any, dict[str, Any]]:
        def fallback(reason: str) -> Choice:
            return Choice(default, template, "rule_default", reason)

        if await self._over_ceiling(ctx):
            return fallback("token_budget"), None, {}
        if self.mode == "stub":
            overrides = await self._runs.overrides(ctx)
            if overrides.get("STUB_FAIL_NODE") == point:
                return fallback("model_unavailable"), None, {"model": STUB_MODEL}
            stub = self._stub(ctx, point)
            if stub is None:
                result = ModelResult(text=json.dumps({"pick": default, "rationale": template}))
            else:
                out_of_bounds = overrides.get("STUB_OUT_OF_BOUNDS") == point
                result = stub(inputs, default, out_of_bounds=out_of_bounds)
        else:
            try:
                result = await self._choose_real(point, allowed, inputs)
            except TimeoutError:
                return fallback("timeout"), None, {"model": self._settings.model_business_name}
            except InvalidOutput:
                return (
                    fallback("model_invalid_output"),
                    None,
                    {"model": self._settings.model_business_name},
                )
            except Exception:
                return (
                    fallback("model_unavailable"),
                    None,
                    {"model": self._settings.model_business_name},
                )
        cols = {
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "model": result.model,
            "latency_ms": result.latency_ms,
        }
        try:
            answer = json.loads(result.text or "")
        except (TypeError, ValueError):
            return fallback("model_invalid_output"), None, cols
        if not isinstance(answer, dict) or "pick" not in answer:
            return fallback("model_invalid_output"), None, cols
        pick = answer["pick"]
        rationale = answer.get("rationale")
        if rationale is not None and not isinstance(rationale, str):
            return fallback("model_invalid_output"), pick, cols
        if not allowed.check(pick):
            return fallback("out_of_bounds"), pick, cols
        text = check_rationale(rationale, [inputs, pick]) or template
        return Choice(pick, text, "model", None), pick, cols

    async def _choose_real(
        self, point: str, allowed: Allowed, inputs: Mapping[str, Any]
    ) -> ModelResult:
        s = self._settings
        if not s.model_base_url:
            raise ModelUnavailable("model not configured")
        system = (
            f"You make the '{point}' choice for a business. Answer with one JSON object "
            '{"pick": <your pick>, "rationale": <at most 200 characters>}. The pick must '
            f"satisfy: {json.dumps(allowed.describe(), default=str)}"
        )
        body = {
            "model": s.model_business_name,
            "max_tokens": BUSINESS_MAX_TOKENS,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(inputs, default=str)},
            ],
        }
        timeout = float(s.model_business_timeout_seconds)
        started = self._clock()
        data = await asyncio.wait_for(
            self._transport.post(s.model_base_url, s.model_api_key, body, timeout), timeout
        )
        latency_ms = int((self._clock() - started) * 1000)
        return _parse_completion(data, s.model_business_name, latency_ms)
