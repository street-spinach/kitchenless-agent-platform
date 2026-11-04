"""Allergy and diet codes and the words people use for them (experience.md §9 step 3, EX-I25).

`cross_check(message, chosen)` finds alias words in an onboarding message whose codes the
model did not choose for anyone, so the restate can say "I also saw '{word}' but didn't
save it." Code only reports words; it never adds a code.
"""

import re
from collections.abc import Mapping

from services.common.protocol.schemas import AllergenCode, DietCode

__all__ = ["ALIASES", "LABELS", "Code", "cross_check"]

type Code = AllergenCode | DietCode

A, D = AllergenCode, DietCode

# Lower-case words and phrases → the codes they name. Each code's own name is included;
# "nuts" names both nut codes, so it is reported unless both were chosen.
ALIASES: Mapping[str, frozenset[Code]] = {
    word: frozenset(codes)
    for word, codes in {
        # Allergens (the EU 14).
        "celery": {A.CELERY},
        "celeriac": {A.CELERY},
        "gluten": {A.GLUTEN},
        "gluten-free": {A.GLUTEN},
        "gluten free": {A.GLUTEN},
        "coeliac": {A.GLUTEN},
        "celiac": {A.GLUTEN},
        "wheat": {A.GLUTEN},
        "crustaceans": {A.CRUSTACEANS},
        "crustacean": {A.CRUSTACEANS},
        "shellfish": {A.CRUSTACEANS, A.MOLLUSCS},
        "prawns": {A.CRUSTACEANS},
        "prawn": {A.CRUSTACEANS},
        "shrimp": {A.CRUSTACEANS},
        "crab": {A.CRUSTACEANS},
        "lobster": {A.CRUSTACEANS},
        "eggs": {A.EGGS},
        "egg": {A.EGGS},
        "fish": {A.FISH},
        "lupin": {A.LUPIN},
        "milk": {A.MILK},
        "dairy": {A.MILK},
        "lactose": {A.MILK},
        "molluscs": {A.MOLLUSCS},
        "mollusks": {A.MOLLUSCS},
        "mussels": {A.MOLLUSCS},
        "oysters": {A.MOLLUSCS},
        "squid": {A.MOLLUSCS},
        "mustard": {A.MUSTARD},
        "nuts": {A.TREE_NUTS, A.PEANUTS},
        "nut": {A.TREE_NUTS, A.PEANUTS},
        "nut-free": {A.TREE_NUTS, A.PEANUTS},
        "tree nuts": {A.TREE_NUTS},
        "tree nut": {A.TREE_NUTS},
        "tree_nuts": {A.TREE_NUTS},
        "almonds": {A.TREE_NUTS},
        "cashews": {A.TREE_NUTS},
        "walnuts": {A.TREE_NUTS},
        "peanuts": {A.PEANUTS},
        "peanut": {A.PEANUTS},
        "sesame": {A.SESAME},
        "soy": {A.SOY},
        "soya": {A.SOY},
        "sulphites": {A.SULPHITES},
        "sulfites": {A.SULPHITES},
        # Diets.
        "vegetarian": {D.VEGETARIAN},
        "veggie": {D.VEGETARIAN},
        "vegan": {D.VEGAN},
        "pescatarian": {D.PESCATARIAN},
        "pescetarian": {D.PESCATARIAN},
        "halal": {D.HALAL},
        "kosher": {D.KOSHER},
        "keto": {D.KETO},
        "high protein": {D.HIGH_PROTEIN},
        "high-protein": {D.HIGH_PROTEIN},
        "high_protein": {D.HIGH_PROTEIN},
    }.items()
}

# The plain word shown to people for each code (showcase.md §4).
LABELS: Mapping[Code, str] = {
    A.CELERY: "celery",
    A.GLUTEN: "gluten",
    A.CRUSTACEANS: "crustaceans",
    A.EGGS: "eggs",
    A.FISH: "fish",
    A.LUPIN: "lupin",
    A.MILK: "milk",
    A.MOLLUSCS: "molluscs",
    A.MUSTARD: "mustard",
    A.TREE_NUTS: "tree nuts",
    A.PEANUTS: "peanuts",
    A.SESAME: "sesame",
    A.SOY: "soy",
    A.SULPHITES: "sulphites",
    D.VEGETARIAN: "vegetarian",
    D.VEGAN: "vegan",
    D.PESCATARIAN: "pescatarian",
    D.HALAL: "halal",
    D.KOSHER: "kosher",
    D.KETO: "keto",
    D.HIGH_PROTEIN: "high protein",
}

# Longest phrase first, whole words only, so "tree nuts" is one match, not "nuts" too, and
# "peanuts" never matches "nuts".
_PATTERN = re.compile(
    r"(?<![\w-])("
    + "|".join(re.escape(w) for w in sorted(ALIASES, key=len, reverse=True))
    + r")(?![\w-])",
    re.IGNORECASE,
)


def cross_check(message: str, chosen: set[Code] | frozenset[Code]) -> list[str]:
    """The alias words in `message` whose codes were not all chosen, in the order they first
    appear, each once (lower-case). Never adds a code."""
    missed: list[str] = []
    for match in _PATTERN.finditer(message):
        word = match.group(1).lower()
        if word not in missed and not ALIASES[word] <= chosen:
            missed.append(word)
    return missed
