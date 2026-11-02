"""Stub decisions for the consumer graph (architecture item 28).

`MODEL_MODE=stub` calls these instead of a model. Each takes the node's state and returns a
fixed `ModelResult`.
"""

import re
from collections.abc import Callable, Mapping
from typing import Any

from services.agents.common.model import ModelResult

__all__ = [
    "CHAT_REPLY",
    "FOOD_WORDS",
    "SHORTFALL_TEXT",
    "STUBS",
    "relay_shortfall",
    "reply_to_user",
    "understand_message",
]

# A text that mentions any of these (and not "dinner") is a weekly order.
FOOD_WORDS = frozenset(
    {
        "food",
        "foods",
        "grocery",
        "groceries",
        "shop",
        "shopping",
        "order",
        "weekly",
        "eat",
        "meal",
        "meals",
        "lunch",
        "breakfast",
        "milk",
        "bread",
        "eggs",
        "fruit",
        "vegetables",
        "veg",
        "rice",
        "pasta",
        "cheese",
        "chicken",
        "fish",
        "snacks",
    }
)

CHAT_REPLY = "I can order your weekly groceries or a dinner. Just tell me what you need."

_WORD = re.compile(r"[a-z]+")


def understand_message(state: Mapping[str, Any]) -> ModelResult:
    """A text with "dinner" is a dinner; any other food word is a weekly order; else chat."""
    words = set(_WORD.findall(str(state.get("text", "")).lower()))
    if "dinner" in words:
        return ModelResult(text="dinner")
    if words & FOOD_WORDS:
        return ModelResult(text="weekly_order")
    return ModelResult(text="chat")


def reply_to_user(state: Mapping[str, Any]) -> ModelResult:
    return ModelResult(text=CHAT_REPLY)


SHORTFALL_TEXT = "The store is out of an item in your order. Pick a substitute or skip it."


def relay_shortfall(state: Mapping[str, Any]) -> ModelResult:
    """Fixed text (agent-protocol P22); the buttons are built in code."""
    return ModelResult(text=SHORTFALL_TEXT)


STUBS: Mapping[str, Callable[[Mapping[str, Any]], ModelResult]] = {
    "understand_message": understand_message,
    "reply_to_user": reply_to_user,
    "relay_shortfall": relay_shortfall,
}
