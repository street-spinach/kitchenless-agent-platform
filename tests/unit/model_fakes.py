"""In-memory fakes for the model client tests (test_model_client.py, test_choose.py)."""

import json
import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from services.agents.common.model import ModelClient, ModelResult
from services.common.run_context import RunContext


def settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "model_mode": "stub",
        "model_base_url": "http://primary.invalid/v1",
        "model_name": "muse-spark",
        "model_api_key": "k1",
        "model_fallback_base_url": "http://fallback.invalid/v1",
        "model_fallback_name": "muse-spark-open",
        "model_fallback_api_key": "k2",
        "run_token_ceiling": None,
        "model_business_name": "Muse Glimmer",
        "model_business_timeout_seconds": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def ctx(agent_class: str = "grocery") -> RunContext:
    return RunContext(run_id=uuid.uuid4(), agent_id=f"{agent_class}:test", conversation_id=None)


class FakeTraces:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def write(self, run_id: Any, agent_id: str, kind: str, payload: dict, **cols: Any):
        self.rows.append({"run_id": run_id, "agent_id": agent_id, "kind": kind, **cols, **payload})
        return len(self.rows)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["kind"] == kind]


class FakeRunState:
    def __init__(self, overrides: Mapping[str, Any] | None = None, tokens: int = 0) -> None:
        self._overrides = dict(overrides or {})
        self.tokens = tokens

    async def overrides(self, ctx: RunContext) -> Mapping[str, Any]:
        return self._overrides

    async def token_total(self, ctx: RunContext) -> int:
        return self.tokens


class FakeClock:
    """A fake monotonic clock; `sleep` advances it instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScriptedTransport:
    """Answers each POST from a script: an exception to raise, or a response dict.

    Each call records `(base_url, body, timeout)`. With a `clock`, a `TimeoutError` entry
    advances the clock by the attempt's timeout, as a real timeout would.
    """

    def __init__(self, script: list[Any], clock: FakeClock | None = None) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.clock = clock

    async def post(self, base_url: str, api_key: Any, body: dict, timeout: float) -> dict:
        self.calls.append((base_url, body, timeout))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            if isinstance(step, TimeoutError) and self.clock is not None:
                self.clock.now += timeout
            raise step
        return step


def completion(content: Any, *, tokens_in: int = 10, tokens_out: int = 5) -> dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content)
    return {
        "model": "served-model",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


def stub_answer(pick: Any, rationale: str) -> ModelResult:
    return ModelResult(text=json.dumps({"pick": pick, "rationale": rationale}))


def client(
    *,
    mode: str = "stub",
    traces: FakeTraces | None = None,
    run_state: FakeRunState | None = None,
    transport: Any = None,
    stubs: Mapping[str, Any] | None = None,
    clock: FakeClock | None = None,
    **settings_overrides: Any,
) -> ModelClient:
    clock = clock or FakeClock()
    return ModelClient(
        traces or FakeTraces(),
        run_state or FakeRunState(),
        settings=settings(model_mode=mode, **settings_overrides),  # type: ignore[arg-type]
        transport=transport,
        stubs=lambda _cls: stubs or {},
        clock=clock,
        sleep=clock.sleep,
    )
