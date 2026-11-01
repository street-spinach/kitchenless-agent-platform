"""The model client, `people` profile (architecture §6, item 22; issue #52)."""

import asyncio

import httpx
import pytest

from services.agents.common.model import (
    ModelHTTPError,
    ModelResult,
    ModelUnavailable,
    TokenBudgetExceeded,
)
from tests.unit.model_fakes import (
    FakeClock,
    FakeRunState,
    FakeTraces,
    ScriptedTransport,
    client,
    completion,
    ctx,
)

MESSAGES = [{"role": "user", "content": "plan the week"}]


def test_two_primary_attempts_then_fallback() -> None:
    clock = FakeClock()
    traces = FakeTraces()
    transport = ScriptedTransport(
        [TimeoutError(), ModelHTTPError(503), completion("ok")], clock=clock
    )
    c = client(mode="real", traces=traces, transport=transport, clock=clock)

    result = asyncio.run(c.complete(ctx("household"), MESSAGES, None, "choose_offer"))

    assert result.text == "ok"
    urls = [url for url, _, _ in transport.calls]
    assert urls == ["http://primary.invalid/v1"] * 2 + ["http://fallback.invalid/v1"]
    models = [body["model"] for _, body, _ in transport.calls]
    assert models == ["muse-spark", "muse-spark", "muse-spark-open"]
    errors = traces.of("error")
    assert [(e["reason"], e["attempt"]) for e in errors] == [("timeout", 1), ("http_503", 2)]
    (decision,) = traces.of("decision")
    assert decision["node"] == "choose_offer"
    assert (decision["tokens_in"], decision["tokens_out"]) == (10, 5)
    assert decision["model"] == "served-model"
    assert decision["latency_ms"] is not None


def test_step_capped_at_60s() -> None:
    clock = FakeClock()
    traces = FakeTraces()
    transport = ScriptedTransport([TimeoutError(), TimeoutError(), TimeoutError()], clock=clock)
    c = client(mode="real", traces=traces, transport=transport, clock=clock)

    with pytest.raises(ModelUnavailable):
        asyncio.run(c.complete(ctx("household"), MESSAGES, None, "choose_offer"))

    timeouts = [t for _, _, t in transport.calls]
    # 20 s, 2 s pause, 20 s, then the fallback gets only what is left of 60 s.
    assert timeouts == [20.0, 20.0, 18.0]
    assert clock.now <= 60.0
    assert len(transport.calls) == 3
    assert len(traces.of("error")) == 3
    assert traces.of("decision") == []


def test_token_ceiling_stops_run() -> None:
    traces = FakeTraces()
    transport = ScriptedTransport([])
    c = client(
        mode="real",
        traces=traces,
        run_state=FakeRunState(tokens=1000),
        transport=transport,
        run_token_ceiling=1000,
    )

    with pytest.raises(TokenBudgetExceeded):
        asyncio.run(c.complete(ctx("household"), MESSAGES, None, "choose_offer"))

    assert transport.calls == []
    (error,) = traces.of("error")
    assert error["reason"] == "token_budget"


def test_stub_mode_never_calls_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("stub mode touched the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    seen = []

    def stub(state: dict) -> ModelResult:
        seen.append(state)
        return ModelResult(text="cheapest offer")

    traces = FakeTraces()
    c = client(mode="stub", traces=traces, stubs={"choose_offer": stub})

    result = asyncio.run(
        c.complete(ctx("household"), MESSAGES, None, "choose_offer", state={"quotes": {}})
    )

    assert result.text == "cheapest offer"
    assert seen == [{"quotes": {}}]
    (decision,) = traces.of("decision")
    assert decision["model"] == "stub"


def test_stub_fail_node_raises() -> None:
    traces = FakeTraces()
    called = []
    c = client(
        mode="stub",
        traces=traces,
        run_state=FakeRunState({"STUB_FAIL_NODE": "choose_offer"}),
        stubs={"choose_offer": lambda state: called.append(state) or ModelResult(text="x")},
    )

    with pytest.raises(ModelUnavailable):
        asyncio.run(c.complete(ctx("household"), MESSAGES, None, "choose_offer"))

    assert called == []
    (error,) = traces.of("error")
    assert error["reason"] == "model_unavailable"
    # Another node in the same run is unaffected.
    c2 = client(
        mode="stub",
        run_state=FakeRunState({"STUB_FAIL_NODE": "choose_offer"}),
        stubs={"build_list": lambda state: ModelResult(text="list")},
    )
    assert asyncio.run(c2.complete(ctx("household"), MESSAGES, None, "build_list")).text == "list"
