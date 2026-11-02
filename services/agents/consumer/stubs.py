"""Stub decisions for the consumer graph (architecture item 28).

`MODEL_MODE=stub` calls these instead of a model. Each takes the node's state and returns a
fixed `ModelResult`.
"""

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from services.agents.common.model import ModelResult

__all__ = [
    "CHAT_REPLY",
    "FOOD_WORDS",
    "SHORTFALL_TEXT",
    "STUBS",
    "learn_dislike",
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

_WORD = re.compile(r"[a-z']+")

# Classify rules 1–3 (agents file 21).
DISLIKE_PHRASES = ("don't like", "dislike", "fine with", "allergic", "intolerant")
OFF_TOPIC_WORDS = frozenset({"flight", "weather", "news", "hotel", "taxi"})
NOT_INSTANT_WORDS = frozenset({"weekly", "groceries", "grocery", "shop", "dinner"})
# Extract: allergy wording answers `none`; these words are dropped from the value.
ALLERGY_PHRASES = ("allergic", "intolerant")
EXTRACT_FILLER = frozenset({"now", "anymore", "a", "the", "some", "any", "please"})
EXTRACT_MAX_WORDS = 3


def understand_message(state: Mapping[str, Any]) -> ModelResult:
    """The first matching rule wins: dislike, off topic, instant, dinner, weekly order, chat."""
    text = str(state.get("text", "")).lower()
    words = set(_WORD.findall(text))
    if any(p in text for p in DISLIKE_PHRASES):
        return ModelResult(text="dislike")
    if words & OFF_TOPIC_WORDS:
        return ModelResult(text="off_topic")
    if "now" in words and not words & NOT_INSTANT_WORDS:
        return ModelResult(text="instant")
    if "dinner" in words:
        return ModelResult(text="dinner")
    if words & FOOD_WORDS:
        return ModelResult(text="weekly_order")
    return ModelResult(text="chat")


def learn_dislike(state: Mapping[str, Any]) -> ModelResult:
    """`{action, value}`: the words after the last "like" or "with", fillers dropped."""
    text = str(state.get("text", "")).lower()
    if any(p in text for p in ALLERGY_PHRASES):
        return ModelResult(text=json.dumps({"action": "none"}))
    action = "remove" if "fine with" in text else "add"
    words = _WORD.findall(text)
    anchors = [i for i, w in enumerate(words) if w in ("like", "with")]
    tail = words[anchors[-1] + 1 :] if anchors else []
    value = [w for w in tail if w not in EXTRACT_FILLER][:EXTRACT_MAX_WORDS]
    return ModelResult(text=json.dumps({"action": action, "value": " ".join(value)}))


def reply_to_user(state: Mapping[str, Any]) -> ModelResult:
    return ModelResult(text=CHAT_REPLY)


SHORTFALL_TEXT = "The store is out of an item in your order. Pick a substitute or skip it."


def relay_shortfall(state: Mapping[str, Any]) -> ModelResult:
    """Fixed text (agent-protocol P22); the buttons are built in code."""
    return ModelResult(text=SHORTFALL_TEXT)


STUBS: Mapping[str, Callable[[Mapping[str, Any]], ModelResult]] = {
    "understand_message": understand_message,
    "learn_dislike": learn_dislike,
    "reply_to_user": reply_to_user,
    "relay_shortfall": relay_shortfall,
}
