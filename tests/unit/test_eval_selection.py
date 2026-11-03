"""Case order, `--list` and the coverage prefixes (evals.md §5, §7; build step 6, #231)."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from evals import run
from evals import select as case_order
from evals.run import RunResult
from services.common import ids

T0 = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
HASHES = {"a": "ha", "b": "hb", "c": "hc", "d": "hd"}


def cases(*names: str) -> list[dict[str, Any]]:
    return [{"id": n, "category": "task"} for n in names]


def row(case_id: str, hours: int, digest: str | None = None) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "case_hash": digest or HASHES[case_id],
        "created_at": T0 + timedelta(hours=hours),
    }


def ids_of(ordered: list[dict[str, Any]]) -> list[str]:
    return [c["id"] for c in ordered]


def real(given: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[str]:
    return ids_of(case_order.order(given, rows, "real", hashes=HASHES))


def test_never_run_cases_first() -> None:
    assert real(cases("a", "b", "c"), [row("a", 1), row("c", 2)]) == ["b", "a", "c"]


def test_oldest_first() -> None:
    # The newest attempt of a case is the one that counts.
    rows = [row("a", 5), row("b", 1), row("c", 3), row("b", 4)]
    assert real(cases("c", "b", "a"), rows) == ["c", "b", "a"]


def test_ties_by_id() -> None:
    assert real(cases("d", "c", "b", "a"), [row("d", 1), row("b", 1)]) == ["a", "c", "b", "d"]


def test_changed_case_hash_counts_as_never_run() -> None:
    rows = [row("a", 1), row("b", 2), row("c", 3, digest="old-hc")]
    assert real(cases("a", "b", "c"), rows) == ["c", "a", "b"]
    # A case with no known hash counts as never run too.
    assert ids_of(case_order.order(cases("a", "b"), [row("a", 1)], "real")) == ["a", "b"]


def test_stub_mode_is_id_order() -> None:
    rows = [row("a", 9), row("b", 1)]
    assert ids_of(case_order.order(cases("c", "a", "b"), rows, "stub", hashes=HASHES)) == [
        "a",
        "b",
        "c",
    ]


def test_longest_prefix_wins() -> None:
    prefixes = {"task-weekly-order-": "architecture", "task-weekly-order-skips-pantry": "pantry"}
    assert case_order.source_design("task-weekly-order-completes", prefixes) == "architecture"
    assert case_order.source_design("task-weekly-order-skips-pantry-items", prefixes) == "pantry"
    assert case_order.source_design("task-other", prefixes) is None


def test_list_names_every_case_with_its_source_design(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(*a: Any, **k: Any) -> RunResult:
        raise AssertionError("--list runs nothing")

    monkeypatch.setattr(run, "run_case", never)
    monkeypatch.setattr(run, "write_results", never)
    assert run.main(["--list"]) == 0
    header, *lines = capsys.readouterr().out.splitlines()
    assert header.split("\t") == ["id", "category", "tags", "modes", "source"]
    thresholds = run.load_thresholds()
    repo = {c["id"]: c for c in run.load_cases(set(thresholds["categories"]))}
    listed = [line.split("\t") for line in lines]
    assert [fields[0] for fields in listed] == sorted(repo)
    designs = set(case_order.load_coverage()["prefixes"].values())
    for case_id, category, tags, modes, source in listed:
        assert source in designs, f"{case_id} has no source design in evals/coverage.yaml"
        assert category == repo[case_id]["category"]
        assert tags == (",".join(repo[case_id]["tags"]) or "-")
        assert modes.split(",") == run.model_modes(repo[case_id])


def test_list_with_an_invalid_case_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "task").mkdir()
    (tmp_path / "task" / "x.yaml").write_text("id: y\n")
    load = run.load_cases
    monkeypatch.setattr(run, "load_cases", lambda categories: load(categories, tmp_path))
    assert run.main(["--list"]) == 2
    assert capsys.readouterr().out == ""


def test_real_mode_runs_least_recently_run_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / "task").mkdir(exist_ok=True)
        (tmp_path / "task" / f"{name}.yaml").write_text(
            f"id: {name}\ncategory: task\ntags: [fast]\nrun: hello\n"
            "expect: [{check: outcome_recorded}]\n"
        )
    load = run.load_cases
    monkeypatch.setattr(run, "load_cases", lambda categories: load(categories, tmp_path))
    monkeypatch.setattr(run, "write_results", lambda summary, results, md: Path("r.json"))
    real_health = {"model_mode": "real", "model_name": "p", "model_business_name": "b"}
    monkeypatch.setattr(run, "agents_health", lambda: real_health)

    def history(dsn: str) -> list[dict[str, Any]]:
        return [
            {"case_id": "a", "case_hash": run.CASE_HASHES["a"], "created_at": T0},
            {"case_id": "c", "case_hash": "stale", "created_at": T0},
        ]

    started: list[tuple[str, int]] = []

    def track(c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"):
        started.append((c["id"], repeat))
        return RunResult(c["id"], c["category"], repeat, passed=True, mode=mode)

    monkeypatch.setattr(run, "last_real_attempts", history)
    monkeypatch.setattr(run, "run_case", track)
    assert run.main(["--all", "--mode", "real", "--repeats", "2"]) == 0
    # b and c (stale hash) never ran with their current hash; each case keeps its repeats.
    assert started == [("b", 1), ("b", 2), ("c", 1), ("c", 2), ("a", 1), ("a", 2)]
    started.clear()
    assert run.main(["--all", "--repeats", "1"]) == 0
    assert started == [("a", 1), ("b", 1), ("c", 1)]


@pytest.mark.db
def test_last_real_attempts_counts_started_real_attempts(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        eval_run_id = ids.new_eval_run_id()
        conn.execute(
            "INSERT INTO eval_runs (eval_run_id, trigger, git_sha, model_mode, models, selection,"
            " thresholds) VALUES (%s, 'nightly', 'abc', 'real', '{}', '{}', '{}')",
            (eval_run_id,),
        )
        rows = [
            ("a", 1, "h1", "real", "fail", "check_failed", T0),
            ("a", 2, "h1", "real", "pass", "none", T0 + timedelta(hours=1)),
            ("a", 3, "h0", "real", "pass", "none", T0 + timedelta(hours=2)),
            ("b", 1, "h1", "stub", "pass", "none", T0 + timedelta(hours=3)),
            ("c", 1, "h1", "real", "skipped", "budget_skipped", T0 + timedelta(hours=4)),
            ("c", 2, "h1", "real", "skipped", "infra", T0 + timedelta(hours=5)),
            ("d", 1, "h1", "real", "skipped", "model_unavailable", T0 + timedelta(hours=6)),
        ]
        for case_id, repeat, digest, mode, verdict, kind, at in rows:
            conn.execute(
                "INSERT INTO eval_results (eval_run_id, case_id, repeat, category, case_hash,"
                " model_mode, verdict, error_kind, seconds, checks, created_at)"
                " VALUES (%s, %s, %s, 'task', %s, %s, %s, %s, 1, '[]', %s)",
                (eval_run_id, case_id, repeat, digest, mode, verdict, kind, at),
            )
    got = {(r["case_id"], r["case_hash"]): r["created_at"] for r in run.last_real_attempts(pg)}
    assert got == {
        ("a", "h1"): T0 + timedelta(hours=1),
        ("a", "h0"): T0 + timedelta(hours=2),
        ("d", "h1"): T0 + timedelta(hours=6),
    }


def test_last_real_attempts_without_history_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*a: Any, **k: Any) -> Any:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(run.psycopg, "connect", refused)
    assert run.last_real_attempts(f"postgresql://nowhere/{uuid.uuid4().hex}") == []
