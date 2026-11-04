"""The D4 report (evals.md §11, EG8). Pure: no I/O, no clock.

For `--trigger d4` the runner adds `report(attempts)` to its Markdown summary. The owner
compares the reports of the two runs, one per business model. Nothing here picks a model.

`attempts` carry `category`, `verdict` and `decisions`, the rows `decision_rows` keeps from
the attempt's trace.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from evals import outcome
from evals.checks import BUSINESS_AGENT_CLASSES
from services.agents.common.model import TEMPLATES

# A point with no rows still prints, with sample 0 (evals.md, file 44).
CHOICE_POINTS = tuple(sorted(TEMPLATES))
REASONS = ("out_of_bounds", "model_invalid_output", "timeout")
RATE_CATEGORIES = (outcome.SAFETY, "decision")
NO_VALUE = "n/a"


def decision_rows(trace: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The trace's `model.choose` rows from business agents. Their code decisions (`_decision`)
    and the household's rows carry no `source`, so they are not choice points."""
    return [
        row
        for row in trace
        if row.get("kind") == "decision"
        and str(row.get("agent_id")).split(":", 1)[0] in BUSINESS_AGENT_CLASSES
        and "source" in (row.get("payload") or {})
    ]


def _share(count: int, sample: int) -> str:
    return f"{count / sample:.0%}" if sample else NO_VALUE


def _percentile(values: Sequence[int], percent: int) -> str:
    """Nearest rank. Rows with no model answer have no latency, so there may be no values."""
    if not values:
        return NO_VALUE
    return str(sorted(values)[math.ceil(len(values) * percent / 100) - 1])


def _tokens(row: Mapping[str, Any]) -> int | None:
    """`tokens_in + tokens_out`, or None when no model answer was recorded."""
    if row.get("tokens_in") is None and row.get("tokens_out") is None:
        return None
    return (row.get("tokens_in") or 0) + (row.get("tokens_out") or 0)


def _point_line(node: str, rows: Sequence[Mapping[str, Any]]) -> str:
    sample = len(rows)
    payloads = [row["payload"] for row in rows]
    latencies = [row["latency_ms"] for row in rows if row.get("latency_ms") is not None]
    tokens = [t for t in map(_tokens, rows) if t is not None]
    cells = [
        node,
        str(sample),
        _share(sum(p.get("source") == "model" for p in payloads), sample),
        *(_share(sum(p.get("reason") == r for p in payloads), sample) for r in REASONS),
        _percentile(latencies, 50),
        _percentile(latencies, 95),
        f"{sum(tokens) / len(tokens):.1f}" if tokens else NO_VALUE,
    ]
    return "| " + " | ".join(cells) + " |"


def _pass_rate(attempts: Sequence[Mapping[str, Any]], category: str) -> str:
    """Pass rates count only `pass` and `fail` (evals.md §5)."""
    verdicts = [a["verdict"] for a in attempts if a["category"] == category]
    passed, failed = verdicts.count(outcome.PASS), verdicts.count(outcome.FAIL)
    return _share(passed, passed + failed)


def report(attempts: Iterable[Mapping[str, Any]]) -> str:
    """The Markdown report of evals.md §11, per business choice point."""
    attempts = list(attempts)
    rows = [row for a in attempts for row in a.get("decisions") or ()]
    by_node: dict[str, list[Mapping[str, Any]]] = {node: [] for node in CHOICE_POINTS}
    for row in rows:
        by_node.setdefault(str(row["payload"].get("node")), []).append(row)
    models = sorted({row["model"] for row in rows if row.get("model")})
    lines = [
        "## D4 report",
        "",
        f"Business model: {', '.join(models) or NO_VALUE}",
        "",
        "| Choice point | Sample | Model used | Out of bounds | Invalid output | Timeout "
        "| p50 ms | p95 ms | Mean tokens |",
        "|---|---|---|---|---|---|---|---|---|",
        *(_point_line(node, by_node[node]) for node in sorted(by_node)),
        "",
        *(
            f"{name.capitalize()} pass rate: {_pass_rate(attempts, name)}"
            for name in RATE_CATEGORIES
        ),
    ]
    return "\n".join(lines) + "\n"
