"""Learned dislikes: the shape check and the match rule (agents design §6, §9).

*   `check_value(value)` normalizes a dislike a member stated, or returns `None` when it
    is not a short food name. `record_dislike` runs it again, so a wrong agent fails
    closed.
*   `matches(item, dislike)` says whether a list item contains the dislike's words, in
    order and next to each other, after removing one trailing `es` or `s` from each word
    longer than three letters. A miss is soft: never a safety failure.
"""

import re

__all__ = ["MAX_CHARS", "MAX_WORDS", "MIN_CHARS", "check_value", "matches"]

MIN_CHARS = 2
MAX_CHARS = 40
MAX_WORDS = 3

# Letters, hyphens and apostrophes; each word holds at least one letter.
_WORD = re.compile(r"[a-z'-]*[a-z][a-z'-]*")
_MATCH_WORD = re.compile(r"[a-z]+")


def check_value(value: str) -> str | None:
    """The lower-cased, trimmed, space-collapsed value, or `None` if its shape is wrong."""
    if not isinstance(value, str):
        return None
    words = value.lower().split()
    normalized = " ".join(words)
    if not MIN_CHARS <= len(normalized) <= MAX_CHARS or not 1 <= len(words) <= MAX_WORDS:
        return None
    if not all(_WORD.fullmatch(w) for w in words):
        return None
    return normalized


def _stem(word: str) -> str:
    if len(word) > 3:
        if word.endswith("es"):
            return word[:-2]
        if word.endswith("s"):
            return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return [_stem(w) for w in _MATCH_WORD.findall(text.lower())]


def matches(item: str, dislike: str) -> bool:
    """Whether the dislike's words appear, in order and adjacent, among the item's words."""
    item_words, dislike_words = _words(item), _words(dislike)
    n = len(dislike_words)
    if n == 0:
        return False
    return any(item_words[i : i + n] == dislike_words for i in range(len(item_words) - n + 1))
