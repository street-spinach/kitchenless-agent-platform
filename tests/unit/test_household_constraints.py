"""Household constraint merging and the AG5 dislike rule (agents §6, §12 "Item names").

The graph runs with the stub model; the stub's list is replaced per test, so the dislike
rule sees known item names.
"""

import asyncio
import json
from typing import Any

import pytest

from services.agents.household import graph as hg
from services.agents.household import stubs
from services.common import ids
from services.common.consumer import StepResult
from tests.unit.test_household_graph import HOUSEHOLD, MEMBERS, STARTER, World

A = "consumer:us-1-a"  # the starter, STARTER
B = "consumer:us-1-b"
B_USER = MEMBERS[B]
LIST = [
    {"item": "Mushroom risotto", "qty": 1, "unit": "each"},
    {"item": "Tomato soup", "qty": 1, "unit": "each"},
    {"item": "Green salad", "qty": 1, "unit": "each"},
]


class DinnerWorld(World):
    """A household dinner: restaurants instead of grocers."""

    async def find_sellers(self, cls: str, region: str, zone: str, currency: str) -> list[str]:
        assert cls == "restaurant"
        return ["restaurant:mock-us"]

    def start(self) -> StepResult:
        msg = self.inbound("start_dinner", {"kind": "dinner"}, from_agent=A)
        return self.step(msg)


def _list(monkeypatch: pytest.MonkeyPatch, items: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(stubs, "LISTS", {"grocery": items, "dinner": items})


def _answer(
    w: World,
    agent: str,
    *,
    allergens: list[str] | None = None,
    diets: list[str] | None = None,
    dislikes: list[str] | None = None,
) -> StepResult:
    body = {
        "order_id": w.order_id,
        "allergens": allergens or [],
        "diets": diets or [],
        "dislikes": dislikes or [],
    }
    return w.step(w.inbound("constraints", body, from_agent=agent))


def _set_scope(w: World, scope: str) -> None:
    # `start_order` stores `scope` from `StartOrder` in build step 9; set it directly here.
    config = {"configurable": {"thread_id": ids.thread_id(HOUSEHOLD, w.run_id)}}
    asyncio.run(w.agent.graph.aupdate_state(config, {"scope": scope}, as_node="send"))


def _quote_request(result: StepResult) -> dict[str, Any]:
    requests = [m.body for m in result.messages if m.type == "quote_request"]
    assert requests
    return requests[0]


def _choices(w: World, *choices: str) -> list[dict[str, Any]]:
    return [
        r for r in w.traces.of("decision") if r["node"] == "build_list" and r["choice"] in choices
    ]


def _items(result: StepResult) -> list[str]:
    return [i["item"] for i in _quote_request(result)["items"]]


def test_start_order_snapshots_household_rules() -> None:
    w = World()
    w.household.rules = {"take_turns": True, "turn_index": 3, "approver_user_id": B_USER}
    w.start()
    [call] = w.household.of("get_household_rules")
    assert call["args"] == {}
    # Agent code passes no token; the scopes are `tokens.TOOL_SCOPES` (identity-authz I6).
    assert "token" not in call and call["ctx"].agent_id == HOUSEHOLD
    saved = w.saved()
    assert (saved["take_turns"], saved["turn_index"], saved["approver_user_id"]) == (
        True,
        3,
        B_USER,
    )


def test_record_constraints_keeps_dislikes() -> None:
    w = World()
    w.start()
    _answer(w, A, dislikes=["mushrooms"])
    assert w.saved()["constraints"][A]["dislikes"] == ["mushrooms"]


def test_allergen_and_diet_unions_in_quote_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, LIST)
    w = World()
    w.start()
    _answer(w, A, allergens=["peanuts"], diets=["vegetarian"], dislikes=["mushrooms"])
    result = _answer(w, B, allergens=["sesame", "milk"], diets=["halal"], dislikes=["tomatoes"])
    constraints = _quote_request(result)["constraints"]
    assert constraints == {
        "allergens": ["milk", "peanuts", "sesame"],
        "diets": ["halal", "vegetarian"],
    }


def test_dislikes_reach_prompt_without_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, LIST)
    w = World()
    w.start()
    _answer(w, A, dislikes=["mushrooms"])
    result = _answer(w, B, dislikes=["tomatoes"])
    [(_, seen)] = [s for s in w.stubs.seen if s[0] == "build_list"]
    prompt = json.dumps(seen["messages"])
    assert "mushrooms, tomatoes" in prompt
    for name in [A, B, STARTER, B_USER]:
        assert name not in prompt
    # Dislikes are never sent to sellers.
    request = json.dumps(_quote_request(result))
    assert "dislike" not in request
    assert "tomatoes" not in request and "mushrooms" not in request


def test_solo_requester_dislike_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, LIST)
    w = DinnerWorld()
    w.start()
    _set_scope(w, "solo")
    _answer(w, A, dislikes=["mushrooms"])
    result = _answer(w, B, dislikes=["tomatoes"])
    assert _items(result) == ["Tomato soup", "Green salad"]
    saved = w.saved()
    assert saved["dropped_items"] == ["Mushroom risotto"]
    assert saved["unavoidable_dislikes"] == ["tomatoes"]
    assert saved["turn_holder"] is None
    [system] = [s["messages"][0]["content"] for n, s in w.stubs.seen if n == "build_list"]
    assert "for 1 people" in system


def test_turn_holder_dislike_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, LIST)
    w = DinnerWorld()
    # Sorted by user id the members are STARTER, B_USER; index 3 mod 2 picks B_USER.
    w.household.rules = {"take_turns": True, "turn_index": 3, "approver_user_id": B_USER}
    w.start()
    _answer(w, A, dislikes=["mushrooms"])
    result = _answer(w, B, dislikes=["tomatoes"])
    assert _items(result) == ["Mushroom risotto", "Green salad"]
    saved = w.saved()
    assert saved["turn_holder"] == B_USER
    assert saved["dropped_items"] == ["Tomato soup"]
    assert saved["unavoidable_dislikes"] == ["mushrooms"]
    [dropped] = _choices(w, "dislike_dropped")
    assert dropped["explanation"] == "item=Tomato soup"


def test_turn_holder_only_among_members_with_dislikes(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, LIST)
    w = DinnerWorld()
    w.household.rules = {"take_turns": True, "turn_index": 0, "approver_user_id": B_USER}
    w.start()
    _answer(w, A)
    _answer(w, B, dislikes=["tomatoes"])
    assert w.saved()["turn_holder"] == B_USER
    assert w.saved()["dropped_items"] == ["Tomato soup"]


def test_take_turns_does_not_apply_to_weekly_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, LIST)
    w = World()
    w.household.rules = {"take_turns": True, "turn_index": 1, "approver_user_id": B_USER}
    w.start()
    _answer(w, A, dislikes=["mushrooms"])
    result = _answer(w, B, dislikes=["tomatoes"])
    assert _items(result) == ["Mushroom risotto", "Tomato soup", "Green salad"]
    assert w.saved()["turn_holder"] is None


def test_all_members_dislike_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, LIST)
    w = World()
    w.start()
    _answer(w, A, dislikes=["tomato"])
    result = _answer(w, B, dislikes=["tomatoes", "mushrooms"])
    assert _items(result) == ["Mushroom risotto", "Green salad"]
    saved = w.saved()
    assert saved["dropped_items"] == ["Tomato soup"]
    assert saved["unavoidable_dislikes"] == ["mushrooms"]


def test_other_dislike_kept_and_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, LIST)
    w = World()
    w.start()
    _answer(w, A)
    result = _answer(w, B, dislikes=["tomatoes"])
    assert _items(result) == ["Mushroom risotto", "Tomato soup", "Green salad"]
    saved = w.saved()
    assert saved["dropped_items"] == []
    assert saved["unavoidable_dislikes"] == ["tomatoes"]
    [kept] = _choices(w, "dislike_kept")
    assert kept["explanation"] == "item=Tomato soup dislikes=tomatoes"
    # Decision rows never name the member.
    for row in _choices(w, "dislike_kept", "dislike_dropped"):
        assert B not in json.dumps(row, default=str) and B_USER not in row["explanation"]


def test_last_item_never_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, [{"item": "Tomato soup", "qty": 1, "unit": "each"}])
    w = World()
    w.start()
    _answer(w, A, dislikes=["tomatoes"])
    result = _answer(w, B, dislikes=["tomatoes"])
    assert _items(result) == ["Tomato soup"]
    saved = w.saved()
    assert saved["dropped_items"] == []
    assert saved["unavoidable_dislikes"] == ["tomatoes"]


def test_bad_item_name_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(
        monkeypatch,
        [
            {"item": "x" * 61, "qty": 1, "unit": "each"},
            {"item": "cake <script>", "qty": 1, "unit": "each"},
            {"item": "x" * 60, "qty": 1, "unit": "each"},
            {"item": "Ben & Jerry's", "qty": 1, "unit": "each"},
        ],
    )
    w = World()
    w.start()
    _answer(w, A)
    result = _answer(w, B)
    assert _items(result) == ["x" * 60, "Ben & Jerry's"]
    assert len(_choices(w, "item_rejected")) == 2


def test_only_bad_item_names_is_invalid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _list(monkeypatch, [{"item": "  ", "qty": 1, "unit": "each"}])
    w = World()
    w.start()
    _answer(w, A)
    with pytest.raises(hg.InvalidOutput):
        _answer(w, B)


def test_item_name_rules() -> None:
    assert hg.item_name_ok("Green salad")
    assert not hg.item_name_ok("")
    assert not hg.item_name_ok("tea\x07")
    assert not hg.item_name_ok("{kind}")
    assert hg.item_name_ok("  " + "y" * 60 + "  ")  # length counts after trimming


def test_old_checkpoint_entry_without_dislikes_reads_empty() -> None:
    owners_constraints = {A: {"allergens": [], "diets": []}, B: {"dislikes": ["tomatoes"]}}
    assert hg.turn_holder(owners_constraints, MEMBERS, 0) == B_USER
    outcome = hg.apply_dislike_rule(LIST, {STARTER: [], B_USER: ["tomatoes"]}, frozenset())
    assert outcome.dropped == [] and outcome.kept == [("Tomato soup", ["tomatoes"])]
