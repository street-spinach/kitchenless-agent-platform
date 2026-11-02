"""Stub decisions for the household graph (architecture item 29).

`MODEL_MODE=stub` calls these instead of a model. Each takes the node's state (the same
rationale-free data the prompt is built from) and returns a fixed `ModelResult`:

*   `build_list`: a fixed list for the order's kind.
*   `negotiate`: if the cheapest valid offer is over the remaining weekly budget and no
    counter has been sent, one `counter_offer` at 90% of its total; otherwise choose.
*   `choose_offer`: the cheapest valid offer, with a fixed explanation.

`accept_delay_seconds` is not a model step: it honors the run override
`STUB_ACCEPT_DELAY_ROUNDS: n` (agent-protocol P21). Before each of the run's first `n`
accepts, the graph waits `OFFER_TTL_SECONDS + 1` seconds in stub mode, so those offers
expire.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any

from services.agents.common.model import ModelResult

__all__ = [
    "CHOOSE_EXPLANATION",
    "LISTS",
    "STUBS",
    "accept_delay_seconds",
    "build_list",
    "choose_offer",
    "negotiate",
]

LISTS: Mapping[str, list[dict[str, Any]]] = {
    "grocery": [
        {"item": "milk", "qty": 1, "unit": "each"},
        {"item": "eggs", "qty": 1, "unit": "each"},
        {"item": "bread", "qty": 1, "unit": "each"},
        {"item": "bananas", "qty": 1, "unit": "each"},
        {"item": "rice", "qty": 1, "unit": "each"},
    ],
    "dinner": [{"item": "dinner for the household", "qty": 1, "unit": "each"}],
}
CHOOSE_EXPLANATION = "The cheapest valid offer."
COUNTER_PCT = 90


def _cheapest(offers: Any) -> Mapping[str, Any] | None:
    valid = [o for o in offers or [] if isinstance(o, Mapping)]
    return min(valid, key=lambda o: (o["total_minor"], o["seller"]), default=None)


def build_list(state: Mapping[str, Any]) -> ModelResult:
    return ModelResult(text=json.dumps({"items": LISTS.get(str(state.get("kind")), [])}))


def negotiate(state: Mapping[str, Any]) -> ModelResult:
    best = _cheapest(state.get("offers"))
    remaining = state.get("remaining_minor")
    if (
        best is not None
        and state.get("counters", 0) == 0
        and isinstance(remaining, int)
        and best["total_minor"] > remaining
    ):
        total = best["total_minor"] * COUNTER_PCT // 100
        answer = {"action": "counter", "quote_id": best["quote_id"], "total_minor": total}
    else:
        answer = {"action": "choose"}
    return ModelResult(text=json.dumps(answer))


def choose_offer(state: Mapping[str, Any]) -> ModelResult:
    best = _cheapest(state.get("offers"))
    quote_id = None if best is None else best["quote_id"]
    return ModelResult(text=json.dumps({"quote_id": quote_id, "explanation": CHOOSE_EXPLANATION}))


def accept_delay_seconds(state: Mapping[str, Any]) -> int:
    """Seconds to wait before this accept: `OFFER_TTL_SECONDS + 1` for the first `n`."""
    rounds = int(state.get("accept_delay_rounds") or 0)
    if int(state.get("accepts") or 0) >= rounds:
        return 0
    return int(state.get("offer_ttl_seconds") or 0) + 1


STUBS: Mapping[str, Callable[[Mapping[str, Any]], ModelResult]] = {
    "build_list": build_list,
    "negotiate": negotiate,
    "choose_offer": choose_offer,
}
