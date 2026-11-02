"""Stub choices for the grocery and restaurant handlers (architecture items 22, 30).

`MODEL_MODE=stub` calls these instead of a model, as `stub(inputs, default, *,
out_of_bounds)`. Normally each returns the rule default with a fixed rationale. With
`STUB_OUT_OF_BOUNDS: <point>` it returns the bad pick architecture §6 names for evals:

*   `rank_substitutes`: the seeded tree-nut SKU for every missing item, and a rationale
    with an invented price;
*   `respond_to_counter`: `agree`, whatever the floor total.

The handler's own allowed-set check turns both into the rule default.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any

from services.agents.common.model import ModelResult

__all__ = ["RATIONALES", "STUBS", "UNSAFE_SKU", "rank_substitutes", "respond_to_counter"]

# The seeded tree-nut item of `grocery:mock-us` (services/seed/data/catalog_mock.yaml).
UNSAFE_SKU = "MOCK-MILK-ALMOND"
RATIONALES: Mapping[str, str] = {
    "rank_substitutes": "Substitutes are the closest matches we have in stock.",
    "respond_to_counter": "We can meet you on this order.",
}


def _answer(pick: Any, rationale: str) -> ModelResult:
    return ModelResult(text=json.dumps({"pick": pick, "rationale": rationale}))


def _missing_lines(inputs: Mapping[str, Any]) -> list[str]:
    allowed = inputs.get("allowed")
    per_item = allowed.get("per_item") if isinstance(allowed, Mapping) else None
    return sorted(per_item) if isinstance(per_item, Mapping) else []


def rank_substitutes(
    inputs: Mapping[str, Any], default: Any, *, out_of_bounds: bool = False
) -> ModelResult:
    if out_of_bounds:
        lines = _missing_lines(inputs) or ["0"]
        return _answer({line: [UNSAFE_SKU] for line in lines}, "Try our almond milk at 9.99.")
    return _answer(default, RATIONALES["rank_substitutes"])


def respond_to_counter(
    inputs: Mapping[str, Any], default: Any, *, out_of_bounds: bool = False
) -> ModelResult:
    if out_of_bounds:
        return _answer({"action": "agree"}, RATIONALES["respond_to_counter"])
    return _answer(default, RATIONALES["respond_to_counter"])


STUBS: Mapping[str, Callable[..., ModelResult]] = {
    "rank_substitutes": rank_substitutes,
    "respond_to_counter": respond_to_counter,
}
