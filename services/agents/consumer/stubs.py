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
    "SETUP_ANSWERS",
    "SETUP_CUJ1",
    "SETUP_CUJ2",
    "SHORTFALL_TEXT",
    "STUBS",
    "learn_dislike",
    "propose_setup",
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
    """The first matching rule wins: onboard, dislike, off topic, instant, dinner, weekly
    order, chat."""
    text = str(state.get("text", "")).lower()
    words = set(_WORD.findall(text))
    if _is_setup(text, words):
        return ModelResult(text="onboard")
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


def _is_setup(text: str, words: set[str]) -> bool:
    """Onboard: a stubbed setup message, a budget, or "add/remove <x> for <name>"."""
    key = " ".join(text.split())
    return (
        (key in SETUP_ANSWERS and key != SETUP_HI)
        or "budget" in words
        or _SETUP_CHANGE.match(key) is not None
    )


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


# Experience EX-I29: the setup answers for the EX-CUJ 1 and 2 messages and for "hi". Any
# other message gets the empty answer (§9 "Which reply").
SETUP_CUJ1 = (
    "Me, my husband Sam +1 555 0102, and Kavi who is 8. Kavi can't have peanuts or sesame. "
    "Sam is vegetarian. Budget 200 a week."
)
SETUP_CUJ2 = "Kavi is allergic to kiwi and nuts"
SETUP_HI = "hi"
EMPTY_SETUP: Mapping[str, Any] = {"people": [], "budget_minor": None, "unmatched": []}
_SETUP_CHANGE = re.compile(r"^(add|remove) .+ for \S+")


def _person(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "phone": None,
        "child": False,
        "add_allergies": [],
        "add_diets": [],
        "remove_allergies": [],
        "remove_diets": [],
        **fields,
    }


# Keys are lower-case with single spaces.
SETUP_ANSWERS: Mapping[str, Mapping[str, Any]] = {
    " ".join(SETUP_CUJ1.lower().split()): {
        "people": [
            _person("Sam", phone="+15550102", add_diets=["vegetarian"]),
            _person("Kavi", child=True, add_allergies=["peanuts", "sesame"]),
        ],
        "budget_minor": 20000,
        "unmatched": [],
    },
    " ".join(SETUP_CUJ2.lower().split()): {
        "people": [_person("Kavi", child=True, add_allergies=["tree_nuts", "peanuts"])],
        "budget_minor": None,
        "unmatched": ["kiwi"],
    },
    SETUP_HI: EMPTY_SETUP,
}


def propose_setup(state: Mapping[str, Any]) -> ModelResult:
    """The `SetupAnswer` JSON for a stubbed message, else the empty answer."""
    key = " ".join(str(state.get("text", "")).lower().split())
    return ModelResult(text=json.dumps(SETUP_ANSWERS.get(key, EMPTY_SETUP)))


STUBS: Mapping[str, Callable[[Mapping[str, Any]], ModelResult]] = {
    "understand_message": understand_message,
    "learn_dislike": learn_dislike,
    "reply_to_user": reply_to_user,
    "relay_shortfall": relay_shortfall,
    "propose_setup": propose_setup,
}
