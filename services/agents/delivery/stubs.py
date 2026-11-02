"""Stub choices for the delivery handler (architecture items 22, 30, §6).

`MODEL_MODE=stub` calls these instead of a model, as `stub(inputs, default, *,
out_of_bounds)`. Normally `choose_window` returns the rule default (the earliest allowed
window) with a fixed rationale. With `STUB_OUT_OF_BOUNDS: choose_window` it returns the bad
pick architecture §6 names for evals: a window ending 2 hours after the requested window.
The handler's own allowed-set check turns it into the rule default.
"""

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from services.agents.common.model import ModelResult

__all__ = ["RATIONALES", "STUBS", "choose_window"]

RATIONALES: Mapping[str, str] = {
    "choose_window": "This is the earliest slot our couriers have free.",
}


def _answer(pick: Any, rationale: str) -> ModelResult:
    return ModelResult(text=json.dumps({"pick": pick, "rationale": rationale}))


def _late_window(inputs: Mapping[str, Any]) -> dict[str, str]:
    message = inputs.get("message")
    body = message.get("body") if isinstance(message, Mapping) else None
    requested = body.get("window") if isinstance(body, Mapping) else None
    start = datetime.fromisoformat(str(requested["start"]))  # type: ignore[index]
    end = datetime.fromisoformat(str(requested["end"]))  # type: ignore[index]
    return {"start": start.isoformat(), "end": (end + timedelta(hours=2)).isoformat()}


def choose_window(
    inputs: Mapping[str, Any], default: Any, *, out_of_bounds: bool = False
) -> ModelResult:
    if out_of_bounds:
        return _answer(_late_window(inputs), RATIONALES["choose_window"])
    return _answer(default, RATIONALES["choose_window"])


STUBS: Mapping[str, Callable[..., ModelResult]] = {"choose_window": choose_window}
