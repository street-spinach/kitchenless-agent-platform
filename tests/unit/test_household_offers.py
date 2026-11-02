"""`compare_offers`, the household's offer ranking (agents §7, file 26; issue #202)."""

from typing import Any

from services.agents.household.graph import compare_offers


def _offer(seller: str, total: int, *, covered: int, lines: int = 5, subs: int = 0) -> dict:
    out: list[dict[str, Any]] = []
    for n in range(1, lines + 1):
        has = n <= covered
        out.append(
            {
                "line": n,
                "requested_item": f"item {n}",
                "offered_sku": f"SKU-{n}" if has else None,
                "offered_name": f"Item {n}" if has else None,
                "qty": 1,
                "unit_minor": 100,
                "substituted_for": f"ORIG-{n}" if has and n <= subs else None,
            }
        )
    return {"seller": seller, "quote_id": f"q-{seller}", "total_minor": total, "lines": out}


def _sellers(offers: list[dict]) -> list[str]:
    return [o["seller"] for o in compare_offers(offers)]


def test_full_coverage_beats_cheaper_partial() -> None:
    partial = _offer("grocery:a", 1000, covered=1)
    full = _offer("grocery:b", 3000, covered=5)
    assert _sellers([partial, full]) == ["grocery:b", "grocery:a"]


def test_equal_coverage_lower_total_wins() -> None:
    assert _sellers(
        [_offer("grocery:a", 3000, covered=5), _offer("grocery:b", 2500, covered=5)]
    ) == ["grocery:b", "grocery:a"]


def test_equal_coverage_and_total_fewer_substitutions_win() -> None:
    two = _offer("grocery:a", 2500, covered=5, subs=2)
    one = _offer("grocery:b", 2500, covered=5, subs=1)
    assert _sellers([two, one]) == ["grocery:b", "grocery:a"]


def test_every_key_equal_seller_id_decides() -> None:
    assert _sellers(
        [_offer("grocery:b", 2500, covered=5), _offer("grocery:a", 2500, covered=5)]
    ) == ["grocery:a", "grocery:b"]


def test_compare_offers_is_pure() -> None:
    offers = [_offer("grocery:b", 3000, covered=5), _offer("grocery:a", 2500, covered=5)]
    before = [dict(o) for o in offers]
    compare_offers(offers)
    assert offers == before
    assert compare_offers([]) == []
