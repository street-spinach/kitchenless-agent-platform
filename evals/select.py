"""Case order and the coverage map (evals.md §5 `--list`, §7 "Order", EG9; build step 6).

`order(cases, last_real_attempts, mode)` is a pure function of the cases and the history:

*   stub mode: id order;
*   real mode: by the time of each case's last real attempt with the same `case_hash`, oldest
    first; cases never run with their current `case_hash` first; ties by id.

`last_real_attempts` holds history rows `{case_id, case_hash, created_at}` (`eval_results`
where `model_mode = 'real'`); `hashes` maps each case id to its current `case_hash`. A case
with no hash, or whose hash has no row, counts as never run.

`evals/coverage.yaml` holds the static map of case id prefixes to source designs (the longest
matching prefix wins) and, under `goals`, each design's goal-to-case map (evals.md §12).
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

COVERAGE_PATH = Path(__file__).parent / "coverage.yaml"


def order(
    cases: list[dict[str, Any]],
    last_real_attempts: Iterable[Mapping[str, Any]],
    mode: str,
    *,
    hashes: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The cases in the order they run (evals.md §7). Each case keeps all its repeats
    together; the runner runs them in this order."""
    by_id = sorted(cases, key=lambda c: c["id"])
    if mode != "real":
        return by_id
    last: dict[tuple[str, str], datetime] = {}
    for row in last_real_attempts:
        key = (row["case_id"], row["case_hash"])
        if key not in last or row["created_at"] > last[key]:
            last[key] = row["created_at"]
    known = hashes or {}

    def when(case: dict[str, Any]) -> datetime | None:
        digest = known.get(case["id"])
        return None if digest is None else last.get((case["id"], digest))

    timed = [(when(c), c) for c in by_id]
    never = [c for t, c in timed if t is None]
    ran = [(t, c) for t, c in timed if t is not None]
    # `sorted` is stable, so cases last run at the same time stay in id order.
    return never + [c for _, c in sorted(ran, key=lambda tc: tc[0])]


def load_coverage(path: Path = COVERAGE_PATH) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not (
        isinstance(data, dict)
        and isinstance(data.get("prefixes"), dict)
        and isinstance(data.get("goals"), dict)
    ):
        raise ValueError(f"{path}: coverage needs the mappings prefixes and goals")
    return data


def source_design(case_id: str, prefixes: Mapping[str, str]) -> str | None:
    """The source design of `case_id`: the design of its longest matching prefix, or None."""
    matches = [p for p in prefixes if case_id.startswith(p)]
    return prefixes[max(matches, key=len)] if matches else None
