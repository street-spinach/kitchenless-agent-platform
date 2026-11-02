"""The `choose_offer` explanation and the order summary (agents §11, §15; issue #202).

One Spark call writes the sentence under `EXPLAIN_TIMEOUT_SECONDS`. A timeout, a model
failure or a sentence that fails `check_rationale` gives the template. Sellers are named by
their vendor label (adapters §6).
"""

import asyncio
from typing import Any

import pytest

from services.agents.common.model import ModelResult, check_rationale
from services.agents.household import graph as hg
from services.agents.household import stubs
from services.common.consumer import RunFinish
from services.identity import tokens
from tests.unit import model_fakes as mf
from tests.unit.test_household_graph import LABELS, NOW, SECRET, World

BEST = "grocery:square-us"
RUNNER_UP = "grocery:mock-us"


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)


class SlowModel:
    """The stub model client, except that `choose_offer` never answers in time."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.mode = inner.mode

    async def complete(self, ctx: Any, messages: Any, tools: Any, node: str, **kw: Any) -> Any:
        if node == "choose_offer":
            await asyncio.sleep(5)
        return await self.inner.complete(ctx, messages, tools, node, **kw)


class ExplainWorld(World):
    """`World` with a chosen `choose_offer` answer, model overrides or a slow model."""

    def __init__(
        self,
        *,
        text: str | None = None,
        model_overrides: dict[str, Any] | None = None,
        slow: bool = False,
        labels: dict[str, str] | None = None,
        **kw: Any,
    ) -> None:
        self.text = text
        self.model_overrides = dict(model_overrides or {})
        self.slow = slow
        self.labels = dict(labels or LABELS)
        self.facts_seen: list[dict[str, Any]] = []
        super().__init__(**kw)

    def build(self) -> hg.HouseholdGraph:
        mapping = dict(self.stubs.mapping())

        def choose_offer(state: Any) -> ModelResult:
            self.facts_seen.append(dict(state["facts"]))
            if self.text is not None:
                return ModelResult(text=self.text)
            return stubs.choose_offer(state)

        mapping["choose_offer"] = choose_offer
        model: Any = mf.client(
            traces=self.traces,
            stubs=mapping,
            run_state=mf.FakeRunState(self.model_overrides),
        )
        if self.slow:
            model = SlowModel(model)
        return hg.HouseholdGraph(
            model=model,
            traces=self.traces,
            checkpointer=self.checkpointer,
            load_run=self.load_run,
            load_members=self.load_members,
            find_sellers=self.find_sellers,
            call_tool=self.household,
            issue_token=_issue,
            clock=lambda: NOW,
            sleep=self.sleep,
            load_whatsapp_members=self.load_whatsapp_members,
            label_seller=self.label_seller,
        )

    async def label_seller(self, run_id: Any, agent_id: str) -> str:
        assert run_id == self.run_id
        return self.labels[agent_id]

    def delivered(self) -> Any:
        self.order_update("order_confirm", "placed", BEST)
        self.order_update("order_status", "in_delivery", "system:api")
        return self.order_update("order_status", "delivered", "system:api")


def _issue(sub: str, act: str, scopes: Any) -> str:
    return tokens.MockIssuer(secret=SECRET).issue(sub, act, list(scopes))


def _explanations(w: World) -> list[dict[str, Any]]:
    return [
        d
        for d in w.traces.of("decision")
        if d["node"] == "choose_offer" and str(d["choice"]).startswith("explanation_")
    ]


FACTS = {
    "seller": "Store A",
    "total": "$80.00",
    "items_covered": 5,
    "items_requested": 5,
    "substitutions": 0,
    "runner_up": None,
    "runner_up_total": None,
    "remaining": "$100.00",
    "counter_from": None,
    "counter_to": None,
}


def test_model_sentence_with_formatted_amounts_passes() -> None:
    text = "Store A had all 5 items for $80.00"
    assert check_rationale(text, [FACTS]) == text


def test_model_sentence_passes_through_the_graph() -> None:
    w = ExplainWorld()
    w.accepted()  # square-us at 2100, under budget
    [facts] = w.facts_seen
    assert facts == {
        "seller": "Square Grocery (simulated)",
        "total": "$21.00",
        "items_covered": 1,
        "items_requested": 4,
        "substitutions": 0,
        "runner_up": "Hillcrest Market (simulated)",
        "runner_up_total": "$24.00",
        "remaining": "$150.00",
        "counter_from": None,
        "counter_to": None,
    }
    expected = "Square Grocery (simulated) had 1 of 4 items for $21.00."
    [row] = _explanations(w)
    assert (row["choice"], row["explanation"], row["facts"]) == (
        "explanation_model",
        expected,
        facts,
    )
    assert w.saved()["explanation"] == expected
    done = w.delivered()
    [summary] = done.messages
    text = summary.body["params"]["text"]
    assert text.startswith(f"Your order {w.order_id[:8]} was delivered: 1 items, 2100 USD")
    # The explanation, then the pantry skip line: the fake pantry has rice (pantry §6).
    assert text.endswith(" " + expected + " Skipped, already at home: rice.")
    assert "\n" not in text


def test_choice_decision_is_traced() -> None:
    w = ExplainWorld()
    w.accepted()
    choices = [d["choice"] for d in w.traces.of("decision") if d["node"] == "choose_offer"]
    # The model client's own row ("text"), then the code's choice and the explanation.
    assert "chosen" in choices and "explanation_model" in choices


def test_model_sentence_with_wrong_number_uses_template() -> None:
    w = ExplainWorld(text="Square Grocery (simulated) had all 7 items for $21.00.")
    w.accepted()
    [row] = _explanations(w)
    assert row["choice"] == "explanation_template"
    assert row["explanation"] == "Chose Square Grocery (simulated): 1 of 4 items for $21.00."


def test_model_failure_uses_template() -> None:
    w = ExplainWorld(model_overrides={"STUB_FAIL_NODE": "choose_offer"})
    accept = w.accepted()
    assert accept.body["total_minor"] == 2100  # the order goes ahead
    [row] = _explanations(w)
    assert row["choice"] == "explanation_template"
    done = w.delivered()
    assert done.finish == RunFinish("completed")
    [summary] = done.messages
    assert summary.body["params"]["text"].endswith(
        "Chose Square Grocery (simulated): 1 of 4 items for $21.00. Skipped, already at home: rice."
    )


def test_explanation_timeout_uses_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hg, "EXPLAIN_TIMEOUT_SECONDS", 0.01)
    w = ExplainWorld(slow=True)
    accept = w.accepted()
    assert accept.type == "accept"
    [row] = _explanations(w)
    assert row["choice"] == "explanation_template"
    assert w.facts_seen == []  # the model never answered


def test_counter_template_names_the_counter() -> None:
    w = ExplainWorld(model_overrides={"STUB_FAIL_NODE": "choose_offer"}, remaining_minor=2000)
    w.to_quoting()
    w.offer(RUNNER_UP, 3000)
    w.offer(BEST, 2500)
    w.round_closed()  # counter at 2000
    [accept] = w.offer(BEST, 2000, round=1).messages
    assert accept.body["total_minor"] == 2000
    [row] = _explanations(w)
    assert row["facts"]["counter_from"] == "$25.00"
    assert row["facts"]["counter_to"] == "$20.00"
    assert row["explanation"] == (
        "Chose Square Grocery (simulated): 1 of 4 items for $20.00, after a counter from $25.00."
    )


def test_seller_named_with_sandbox_label() -> None:
    labels = {**LABELS, BEST: "Square Grocery (sandbox)"}
    w = ExplainWorld(model_overrides={"STUB_FAIL_NODE": "choose_offer"}, labels=labels)
    w.accepted()
    [row] = _explanations(w)
    assert row["facts"]["seller"] == "Square Grocery (sandbox)"
    assert row["explanation"].startswith("Chose Square Grocery (sandbox): ")


def test_template_passes_check_rationale() -> None:
    facts = {**FACTS, "counter_from": "$90.00", "counter_to": "$80.00"}
    text = hg.explanation_template(facts)
    assert text == "Chose Store A: 5 of 5 items for $80.00, after a counter from $90.00."
    assert check_rationale(text, [facts]) == text
