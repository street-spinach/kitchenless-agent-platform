"""`services/common/codes.py` (experience.md §9 step 3, EX-I25)."""

import re
from pathlib import Path

from services.common import codes
from services.common.protocol.schemas import AllergenCode, DietCode

A, D = AllergenCode, DietCode


def _check_values(kind: str) -> set[str]:
    sql = Path("migrations/0002_core_entities.sql").read_text()
    found = re.search(rf"kind = '{kind}' AND value IN \(([^)]*)\)", sql)
    assert found, kind
    return set(re.findall(r"'([a-z_]+)'", found.group(1)))


def test_enums_match_migration() -> None:
    """The enums are exactly the 0002 `member_constraints_code_check` values, and every alias
    names only codes from them."""
    assert {c.value for c in AllergenCode} == _check_values("allergy")
    assert {c.value for c in DietCode} == _check_values("diet")
    for word, named in codes.ALIASES.items():
        assert named, word
        assert all(isinstance(c, AllergenCode | DietCode) for c in named), word
        assert word == word.lower(), word
    # Every code can be found by its own name.
    for code in [*AllergenCode, *DietCode]:
        assert code in codes.ALIASES[code.value]


def test_labels_cover_every_code() -> None:
    """Showcase §4: a plain lower-case label for every code, never the raw code."""
    assert set(codes.LABELS) == {*AllergenCode, *DietCode}
    for code, label in codes.LABELS.items():
        assert label == label.lower() and label.strip() == label, code
        assert "_" not in label, code
    assert codes.LABELS[A.PEANUTS] == "peanuts"
    assert codes.LABELS[A.TREE_NUTS] == "tree nuts"
    assert codes.LABELS[D.HIGH_PROTEIN] == "high protein"
    # The labels are looked up by the plain strings stored in message bodies.
    assert codes.LABELS["tree_nuts"] == "tree nuts"  # type: ignore[index]


def test_cross_check_finds_missed_alias() -> None:
    msg = "Kavi is allergic to nuts and dairy, Mira eats no prawns and Ravi is gluten-free."
    assert codes.cross_check(msg, set()) == ["nuts", "dairy", "prawns", "gluten-free"]
    # "nuts" names both nut codes: one chosen is not enough.
    assert codes.cross_check(msg, {A.TREE_NUTS, A.MILK, A.CRUSTACEANS, A.GLUTEN}) == ["nuts"]
    assert codes.cross_check(msg, {A.TREE_NUTS, A.PEANUTS, A.MILK, A.CRUSTACEANS, A.GLUTEN}) == []
    # Longest phrase wins, whole words only.
    assert codes.cross_check("tree nuts only", {A.TREE_NUTS}) == []
    assert codes.cross_check("Peanuts!", {A.PEANUTS}) == []
    assert codes.cross_check("PEANUTS and peanuts", set()) == ["peanuts"]
    assert codes.cross_check("doughnuts and eggplant", set()) == []
    assert codes.cross_check("she's vegan, high protein", {D.VEGAN}) == ["high protein"]
    # Plain strings compare equal to the codes.
    assert codes.cross_check("dairy", {"milk"}) == []


def test_cross_check_never_adds_codes() -> None:
    chosen = {A.SESAME}
    before = set(chosen)
    out = codes.cross_check("nuts, dairy, sesame, soy", chosen)
    assert chosen == before
    assert out == ["nuts", "dairy", "soy"]
    assert all(isinstance(word, str) for word in out)
    assert not any(isinstance(word, AllergenCode | DietCode) for word in out)
