"""Bounded choices for business agents: `choose()` (architecture §4, §6, item 22; issue #52)."""

import asyncio
from typing import Any

from services.agents.common.model import (
    TEMPLATES,
    Actions,
    Choice,
    ModelResult,
    OneOf,
    PerItem,
    Range,
    Subset,
    check_rationale,
)
from tests.unit.model_fakes import (
    FakeRunState,
    FakeTraces,
    ScriptedTransport,
    client,
    completion,
    ctx,
    stub_answer,
)

FLOOR = 1800
OFFER = 2400
COUNTER = Actions({"reject": None, "agree": None, "counter": Range(FLOOR, OFFER)})
COUNTER_INPUTS = {"buyer_total_minor": 1500, "floor_total_minor": FLOOR, "offer_minor": OFFER}
COUNTER_DEFAULT = {"action": "counter", "arg": FLOOR}


def _choose(c: Any, point: str, allowed: Any, default: Any, inputs: Any) -> Choice:
    return asyncio.run(c.choose(ctx(), point, allowed, default, inputs))


def test_out_of_bounds_pick_replaced_by_default() -> None:
    traces = FakeTraces()
    transport = ScriptedTransport([completion({"pick": "w9", "rationale": "Fastest."})])
    c = client(mode="real", traces=traces, transport=transport)

    choice = _choose(c, "choose_window", OneOf(["w1", "w2"]), "w1", {"windows": ["w1", "w2"]})

    assert choice == Choice("w1", TEMPLATES["choose_window"], "rule_default", "out_of_bounds")
    (row,) = traces.of("decision")
    assert row["model_pick"] == "w9"


def test_range_pick_inside_bounds_kept() -> None:
    answer = {"pick": {"action": "counter", "arg": 2000}, "rationale": "We can meet you at 2000."}
    inputs = {**COUNTER_INPUTS, "proposed_minor": 2000}
    c = client(mode="real", transport=ScriptedTransport([completion(answer)]))

    choice = _choose(c, "respond_to_counter", COUNTER, COUNTER_DEFAULT, inputs)

    assert choice == Choice(answer["pick"], "We can meet you at 2000.", "model", None)
    # Below the floor or above the offer is out of bounds.
    assert not COUNTER.check({"action": "counter", "arg": FLOOR - 1})
    assert not COUNTER.check({"action": "counter", "arg": OFFER + 1})
    assert not COUNTER.check({"action": "counter", "arg": True})
    assert COUNTER.check({"action": "reject"})
    assert not COUNTER.check({"action": "accept"})


def test_timeout_uses_default_without_raising() -> None:
    traces = FakeTraces()
    transport = ScriptedTransport([TimeoutError()])
    c = client(mode="real", traces=traces, transport=transport)

    choice = _choose(c, "respond_to_counter", COUNTER, COUNTER_DEFAULT, COUNTER_INPUTS)

    assert choice.source == "rule_default"
    assert choice.reason == "timeout"
    assert choice.value == COUNTER_DEFAULT
    # One attempt, the business timeout, the business model, at most 300 output tokens.
    ((_, body, timeout),) = transport.calls
    assert timeout == 5.0
    assert body["model"] == "Muse Glimmer"
    assert body["max_tokens"] == 300
    assert len(traces.of("decision")) == 1


def test_invalid_json_uses_default() -> None:
    for content in ["not json", {"rationale": "no pick"}, ["w1"], {"pick": "w1", "rationale": 7}]:
        c = client(mode="real", transport=ScriptedTransport([completion(content)]))
        choice = _choose(c, "choose_window", OneOf(["w1", "w2"]), "w2", {})
        assert (choice.value, choice.source, choice.reason) == (
            "w2",
            "rule_default",
            "model_invalid_output",
        ), content


def test_token_budget_skips_model_for_business() -> None:
    traces = FakeTraces()
    transport = ScriptedTransport([])
    c = client(
        mode="real",
        traces=traces,
        transport=transport,
        run_state=FakeRunState(tokens=500),
        run_token_ceiling=500,
    )

    choice = _choose(c, "choose_window", OneOf(["w1"]), "w1", {})

    assert transport.calls == []
    assert (choice.source, choice.reason) == ("rule_default", "token_budget")
    (row,) = traces.of("decision")
    assert row["reason"] == "token_budget"


def test_decision_row_records_allowed_pick_and_source() -> None:
    traces = FakeTraces()
    c = client(
        mode="real",
        traces=traces,
        transport=ScriptedTransport([completion({"pick": "w2", "rationale": "Later is fine."})]),
    )

    _choose(c, "choose_window", OneOf(["w1", "w2"]), "w1", {"inputs": "never logged"})

    (row,) = traces.of("decision")
    assert row["node"] == "choose_window"
    assert row["choice"] == "w2"
    assert row["explanation"] == "Later is fine."
    assert row["allowed"] == {"one_of": ["w1", "w2"]}
    assert row["model_pick"] == "w2"
    assert row["source"] == "model"
    assert row["reason"] is None
    assert (row["tokens_in"], row["tokens_out"], row["model"]) == (10, 5, "served-model")
    assert "never logged" not in repr(row)


def test_stub_out_of_bounds_override() -> None:
    def stub(inputs: dict, default: Any, *, out_of_bounds: bool) -> ModelResult:
        if out_of_bounds:
            return stub_answer({"action": "agree"}, "Deal at 1500, price 9.99.")
        return stub_answer(default, "Rules.")

    allowed = Actions({"reject": None, "counter": Range(FLOOR, OFFER)})
    traces = FakeTraces()
    c = client(
        mode="stub",
        traces=traces,
        run_state=FakeRunState({"STUB_OUT_OF_BOUNDS": "respond_to_counter"}),
        stubs={"respond_to_counter": stub},
    )

    choice = _choose(c, "respond_to_counter", allowed, COUNTER_DEFAULT, COUNTER_INPUTS)

    assert (choice.value, choice.source, choice.reason) == (
        COUNTER_DEFAULT,
        "rule_default",
        "out_of_bounds",
    )
    (row,) = traces.of("decision")
    assert row["model_pick"] == {"action": "agree"}
    assert row["model"] == "stub"

    # Without the override the stub's answer is the rule default, recorded as the model's.
    c2 = client(mode="stub", stubs={"respond_to_counter": stub})
    choice = _choose(c2, "respond_to_counter", allowed, COUNTER_DEFAULT, COUNTER_INPUTS)
    assert (choice.value, choice.source, choice.reason) == (COUNTER_DEFAULT, "model", None)

    # STUB_FAIL_NODE on the choice point gives model_unavailable.
    c3 = client(
        mode="stub",
        run_state=FakeRunState({"STUB_FAIL_NODE": "respond_to_counter"}),
        stubs={"respond_to_counter": stub},
    )
    choice = _choose(c3, "respond_to_counter", allowed, COUNTER_DEFAULT, COUNTER_INPUTS)
    assert (choice.source, choice.reason) == ("rule_default", "model_unavailable")


def test_subset_checker_rejects_non_members_and_duplicates() -> None:
    subset = Subset(["a", "b", "c", "d"], max_len=3)
    assert subset.check(["b", "a"])
    assert subset.check([])
    assert not subset.check(["a", "x"])
    assert not subset.check(["a", "a"])
    assert not subset.check(["a", "b", "c", "d"])
    assert not subset.check("a")
    assert not subset.check([["a"]])

    per_item = PerItem({"sku1": Subset(["s1", "s2"], 3), "sku2": Subset(["s3"], 3)})
    assert per_item.check({"sku1": ["s2", "s1"], "sku2": []})
    assert not per_item.check({"sku1": ["s3"], "sku2": []})
    assert not per_item.check({"sku1": ["s1"]})
    assert PerItem({}).check({})

    c = client(
        mode="real",
        transport=ScriptedTransport([completion({"pick": {"sku1": ["s1", "s1"]}})]),
    )
    choice = _choose(c, "rank_substitutes", PerItem({"sku1": Subset(["s1", "s2"], 3)}), {}, {})
    assert (choice.source, choice.reason) == ("rule_default", "out_of_bounds")


def test_rationale_with_invented_number_replaced_by_template() -> None:
    assert check_rationale("Only 9.99 today.", {"price_minor": 1299}) is None
    assert check_rationale("Total 1299 as listed.", {"price_minor": 1299}) == (
        "Total 1299 as listed."
    )
    assert check_rationale("x" * 201, {}) is None

    c = client(
        mode="real",
        transport=ScriptedTransport(
            [completion({"pick": "w1", "rationale": "Cheaper at 9.99 than anyone."})]
        ),
    )
    choice = _choose(c, "choose_window", OneOf(["w1"]), "w1", {"fee_minor": 599})
    # The pick is valid, so it is kept; only the rationale falls back to the template.
    assert choice == Choice("w1", TEMPLATES["choose_window"], "model", None)


def test_rationale_urls_stripped() -> None:
    text = "See https://evil.example/x?y=1 and www.spam.example now.\x07‮"
    assert check_rationale(text, {}) == "See and now."
    assert check_rationale("https://only.example/link", {}) is None
