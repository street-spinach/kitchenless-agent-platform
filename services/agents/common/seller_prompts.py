"""The seller agents' choice-point guidance (agents §12, prompts 7 and 8; AG12, AG-A11).

`choose()` puts the guidance before its unchanged contract text (the answer format and
the allowed set). The allowed sets and rule defaults are still computed in code by
`SellerHandler`; guidance never changes them.
"""

__all__ = ["GUIDANCE", "NOUN", "guidance"]

# 7. `rank_substitutes` and 8. `respond_to_counter`. `{noun}` is the area's word for a line.
GUIDANCE = {
    "rank_substitutes": (
        "For each missing {noun}, rank the substitute candidates by closeness in kind and "
        "size, then by price. The shopper's constraints are in the inputs."
    ),
    "respond_to_counter": (
        "A buyer has countered your offer. Prefer agreeing or countering within your "
        "business rules over rejecting, when the rules allow it."
    ),
}
NOUN = {"grocery": "item", "restaurant": "dish"}


def guidance(point: str, area: str) -> str:
    """The guidance for `point`, formatted with `area`'s noun. Raises `KeyError` if unknown."""
    return GUIDANCE[point].format(noun=NOUN[area])
