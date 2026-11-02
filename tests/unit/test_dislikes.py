"""`services/common/dislikes.py`: the shape check and the match rule (agents §6, §9, file 14)."""

import pytest

from services.common import dislikes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" Mushrooms ", "mushrooms"),
        ("eggplant", "eggplant"),
        ("blue cheese", "blue cheese"),
        ("Blue   Cheese", "blue cheese"),
        ("bok-choy", "bok-choy"),
        ("five dollars", "five dollars"),
    ],
)
def test_check_value_accepts(value: str, expected: str) -> None:
    assert dislikes.check_value(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "5 dollars",
        "a",
        "",
        "   ",
        "very spicy red peppers",
        "x" * 41,
        "<b>okra</b>",
        "--",
        "okra; drop table",
    ],
)
def test_check_value_refuses(value: str) -> None:
    assert dislikes.check_value(value) is None


@pytest.mark.parametrize(
    ("item", "dislike", "expected"),
    [
        ("Mushroom risotto", "mushrooms", True),
        ("Tomatoes", "tomato", True),
        ("Blue cheese salad", "blue cheese", True),
        ("Cheese, blue", "blue cheese", False),
        ("Pineapple", "apple", False),
        ("Gas station snack", "gas", True),
        ("Okra stew", "", False),
    ],
)
def test_matches(item: str, dislike: str, expected: bool) -> None:
    assert dislikes.matches(item, dislike) is expected
