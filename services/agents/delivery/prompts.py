"""The delivery agent's choice-point guidance (agents §12, prompt 9; AG12, AG-A11).

`choose()` puts the guidance before its unchanged contract text. The allowed set and the
rule default (the earliest window) are still computed in code by the handler.
"""

__all__ = ["GUIDANCE"]

# 9. `choose_window`.
GUIDANCE = {"choose_window": "Prefer the earliest window."}
