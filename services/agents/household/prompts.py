"""The household agent's prompts (agents §12, prompts 5 and 6)."""

__all__ = ["BUILD_LIST_PROMPT", "EXPLAIN_PROMPT", "NO_CODES"]

# 5. Build list (moved from graph.py and rewritten). Filled by `build_list`.
BUILD_LIST_PROMPT = (
    "Plan a {kind} order for {people} people. Answer with JSON "
    '{{"items": [{{"item", "qty", "unit"}}]}}. Item names: at most 60 characters. Use these '
    "names for the items you choose, where they fit: {pantry_names}. Every item must fit "
    "these diets: {diets}. Never include these allergens: {allergens}. Avoid these dislikes "
    "where you can: {dislikes}."
)

# 6. Explain. `{facts}` is the facts object as JSON (agents §11).
EXPLAIN_PROMPT = (
    "Write one sentence of at most 200 characters telling the household why this offer was "
    "chosen, and about the counter if there was one. Use only these facts, and copy amounts "
    "exactly as written: {facts}"
)

# What the build-list prompt says when there are no codes of a kind.
NO_CODES = "none"
