"""The owner rule raises the seller floors (identity-authz §10, I33, I-A11, I-CUJ 7).

An item's floor is the higher of its seeded floor (95 % of list here) and
`list × (1 − max_discount_pct / 100)`, rounded up to the cent.
"""

from typing import Any

import pytest

from services.agents.common.seller import OWNER_FLOOR_NODE, OWNER_RULE, floor_total
from tests.unit import seller_fakes as sf
from tests.unit.test_handlers import MILK, World

LIST = sf.CATALOG[MILK][2]  # 429
SEEDED = sf.FLOORS[MILK]  # ceil(429 × 0.95) = 408
OWNER_4 = -(-LIST * 96 // 100)  # ceil(429 × 0.96) = 412


def _lines(qty: int = 10) -> list[dict[str, Any]]:
    return [{"offered_sku": MILK, "qty": qty, "unit_minor": LIST}]


def _world(pct: Any, answers: dict[str, Any] | None = None) -> World:
    w = World(answers=answers)
    rules = {**sf.RULES, OWNER_RULE: pct}

    async def owner_rules(agent_id: str) -> dict[str, Any]:
        assert agent_id == sf.GROCERY
        return dict(rules)

    w.store.rules = owner_rules  # type: ignore[method-assign]
    return w


def _decisions(w: World, node: str) -> list[dict[str, Any]]:
    return [r for r in w.traces.of("decision") if r["node"] == node]


def test_owner_rule_raises_floor() -> None:
    assert SEEDED < OWNER_4
    assert floor_total(_lines(), sf.RULES) == 10 * SEEDED
    # 4 %: the owner's 96 % floor is above the seeded 95 % floor, so it binds.
    assert floor_total(_lines(), {**sf.RULES, OWNER_RULE: 4}) == 10 * OWNER_4
    # 10 %: a 90 % owner floor is below the seeded one, which still binds.
    assert floor_total(_lines(), {**sf.RULES, OWNER_RULE: 10}) == 10 * SEEDED
    # 0 %: the floor is the list price. An item with no seeded floor is at list price.
    assert floor_total(_lines(), {**sf.RULES, OWNER_RULE: 0}) == 10 * LIST
    assert floor_total(_lines(), {OWNER_RULE: 4}) == 10 * LIST


@pytest.mark.parametrize("bad", [-1, 101, "4", 4.5, True, None, {"pct": 4}])
def test_damaged_owner_rule_fails_closed(bad: Any) -> None:
    # A rule that is not an integer 0-100 counts as 0 %: the floor is the list price.
    assert floor_total(_lines(), {**sf.RULES, OWNER_RULE: bad}) == 10 * LIST


def test_counter_below_owner_floor_refused() -> None:
    """I-CUJ 7: a counter needing a 5 % discount is at the seeded floor but below the
    owner's 96 % floor; the handler does not agree and the `decision` row names the rule."""
    w = _world(4, answers={"respond_to_counter": {"action": "agree"}})
    q = w.quote()
    buyer = 10 * SEEDED  # 5 % off, rounded to the seeded floor
    result = w.counter(q, buyer)
    [offer] = result.messages
    # The model's "agree" is outside the allowed set: code counters at the owner floor.
    assert offer.type == "offer"
    assert offer.body["total_minor"] == 10 * OWNER_4
    assert w.store.quotes[q.quote_id].total_minor == 10 * OWNER_4
    [choice] = _decisions(w, "respond_to_counter")
    assert "agree" not in choice["allowed"]["actions"]
    assert choice["allowed"]["actions"]["counter"] == {"range": [10 * OWNER_4, q.total_minor]}
    [owner] = _decisions(w, OWNER_FLOOR_NODE)
    assert owner["rule"] == OWNER_RULE and owner[OWNER_RULE] == 4
    assert (owner["floor_minor"], owner["seeded_floor_minor"]) == (10 * OWNER_4, 10 * SEEDED)
    assert owner["buyer_total_minor"] == buyer
    assert owner["choice"] == "below_floor"
    assert OWNER_RULE in owner["explanation"]
    assert owner["on_behalf_of"] == sf.CARD.owner_ref


def test_same_counter_agreed_without_owner_rule() -> None:
    w = World(answers={"respond_to_counter": {"action": "agree"}})
    q = w.quote()
    result = w.counter(q, 10 * SEEDED)
    [offer] = result.messages
    assert offer.body["total_minor"] == 10 * SEEDED
    assert _decisions(w, OWNER_FLOOR_NODE) == []


def test_no_owner_decision_when_seeded_floor_binds() -> None:
    w = _world(10)
    q = w.quote()
    w.counter(q, 10 * SEEDED - 1)
    assert _decisions(w, OWNER_FLOOR_NODE) == []


def test_no_owner_decision_at_or_above_owner_floor() -> None:
    w = _world(4)
    q = w.quote()
    result = w.counter(q, 10 * OWNER_4)
    assert result.messages[0].body["total_minor"] == 10 * OWNER_4
    assert _decisions(w, OWNER_FLOOR_NODE) == []
