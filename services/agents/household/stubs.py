"""Stub decisions for the household graph (architecture item 29; agents §11, file 25).

`MODEL_MODE=stub` calls these instead of a model. Each takes the node's state (the same
data the prompt is built from) and returns a fixed `ModelResult`:

*   `build_list`: a fixed list for the order's kind.
*   `choose_offer`: the explanation sentence, built from `facts` so that it passes
    `check_rationale`: "{seller} had {items_covered} of {items_requested} items for
    {total}." The run override `STUB_FAIL_NODE=choose_offer` makes the model client fail
    instead, so the graph uses its template sentence.

`negotiate` has no stub: the counter is decided in code (agents §7).

`accept_delay_seconds` is not a model step: it honors the run override
`STUB_ACCEPT_DELAY_ROUNDS: n` (agent-protocol P21). Before each of the run's first `n`
accepts, the graph waits `OFFER_TTL_SECONDS + 1` seconds in stub mode, so those offers
expire.

`STUB_NEGOTIATE_HOLD_SECONDS: n` is not a model step either: in stub mode the graph waits `n`
seconds just after the order enters `negotiating`, which otherwise lasts milliseconds, so the
eval runner's `revoke` step can act in that state (identity-authz I35).
"""

import json
from collections.abc import Callable, Mapping
from typing import Any

from services.agents.common.model import ModelResult

__all__ = [
    "LISTS",
    "STUBS",
    "accept_delay_seconds",
    "build_list",
    "choose_offer",
]

LISTS: Mapping[str, list[dict[str, Any]]] = {
    "grocery": [
        {"item": "milk", "qty": 1, "unit": "each"},
        {"item": "eggs", "qty": 1, "unit": "each"},
        {"item": "bread", "qty": 1, "unit": "each"},
        {"item": "bananas", "qty": 1, "unit": "each"},
        {"item": "rice", "qty": 1, "unit": "each"},
    ],
    # `MOCK-DIN-CHICKEN-RICE` by its menu name: a seller's search must find it (evals §12).
    "dinner": [{"item": "Grilled chicken and rice bowl", "qty": 1, "unit": "each"}],
}


def build_list(state: Mapping[str, Any]) -> ModelResult:
    return ModelResult(text=json.dumps({"items": LISTS.get(str(state.get("kind")), [])}))


def choose_offer(state: Mapping[str, Any]) -> ModelResult:
    facts = state.get("facts") or {}
    text = (
        f"{facts.get('seller')} had {facts.get('items_covered')} of "
        f"{facts.get('items_requested')} items for {facts.get('total')}."
    )
    return ModelResult(text=text)


def accept_delay_seconds(state: Mapping[str, Any]) -> int:
    """Seconds to wait before this accept: `OFFER_TTL_SECONDS + 1` for the first `n`."""
    rounds = int(state.get("accept_delay_rounds") or 0)
    if int(state.get("accepts") or 0) >= rounds:
        return 0
    return int(state.get("offer_ttl_seconds") or 0) + 1


STUBS: Mapping[str, Callable[[Mapping[str, Any]], ModelResult]] = {
    "build_list": build_list,
    "choose_offer": choose_offer,
}
