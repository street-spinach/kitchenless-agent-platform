"""Eval history tables survive the eval reset and need no `runs` row (evals design §6, E-A8;
issue #226)."""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from psycopg.rows import dict_row

from evals import run
from evals.run import RunResult
from services.common import ids
from services.mocks import control
from services.seed import __main__ as seed_mod

pytestmark = pytest.mark.db

LOAD_CASES = run.load_cases

US1_PHONES = ["+15550100201", "+15550100202", "+15550100203"]


@pytest.fixture(autouse=True)
def _no_openfga(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset's post-commit `fga.reconcile()` (identity-authz I27) needs OpenFGA;
    `test_panel_reset.py` tests that it is called."""

    async def reconcile() -> int:
        return 0

    monkeypatch.setattr(control, "reconcile_fga", reconcile)


def _write_history(conn: psycopg.Connection, agent_run_ids: list[uuid.UUID]) -> uuid.UUID:
    eval_run_id = ids.new_eval_run_id()
    conn.execute(
        "INSERT INTO eval_runs (eval_run_id, trigger, git_sha, model_mode, models, selection,"
        " thresholds) VALUES (%s, 'manual', 'abc123', 'stub', '{}', '{case-1}', '{}')",
        (eval_run_id,),
    )
    conn.execute(
        "INSERT INTO eval_results (eval_run_id, case_id, repeat, category, case_hash,"
        " model_mode, agent_run_ids, verdict, error_kind, seconds, checks)"
        " VALUES (%s, 'case-1', 0, 'safety', 'h', 'stub', %s, 'pass', 'none', 2.5, '{}')",
        (eval_run_id, agent_run_ids),
    )
    return eval_run_id


def _counts(conn: psycopg.Connection, eval_run_id: uuid.UUID) -> tuple[int, int]:
    runs = conn.execute(
        "SELECT count(*) FROM eval_runs WHERE eval_run_id = %s", (eval_run_id,)
    ).fetchone()
    results = conn.execute(
        "SELECT count(*) FROM eval_results WHERE eval_run_id = %s", (eval_run_id,)
    ).fetchone()
    assert runs is not None and results is not None
    return runs[0], results[0]


def test_reset_keeps_history(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        seed_mod.seed(conn, "all", us1_phones=US1_PHONES)
        agent_run_id = ids.new_run_id()
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, 'us-1-eval', 'weekly', '{}', '{}')",
            (agent_run_id,),
        )
        eval_run_id = _write_history(conn, [agent_run_id])

    async def go() -> dict[str, int]:
        async with await psycopg.AsyncConnection.connect(pg) as aconn:
            return await control.reset(aconn, "eval")

    deleted = asyncio.run(go())

    assert deleted["runs"] == 1
    assert "eval_runs" not in deleted and "eval_results" not in deleted
    with psycopg.connect(pg) as conn:
        assert _counts(conn, eval_run_id) == (1, 1)
        row = conn.execute("SELECT agent_run_ids FROM eval_results").fetchone()
    assert row == ([agent_run_id],)


def test_history_rows_need_no_runs_row(pg: str) -> None:
    missing = [ids.new_run_id(), ids.new_run_id()]
    with psycopg.connect(pg) as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone() == (0,)
        eval_run_id = _write_history(conn, missing)
        assert _counts(conn, eval_run_id) == (1, 1)
        fks = conn.execute(
            "SELECT tc.table_name, ccu.table_name FROM information_schema.table_constraints tc"
            " JOIN information_schema.constraint_column_usage ccu"
            " ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema"
            " WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = current_schema()"
            " AND tc.table_name IN ('eval_runs', 'eval_results')"
        ).fetchall()
    assert fks == [("eval_results", "eval_runs")]


# --- the runner's history writes (evals.md §5; build step 5c) -------------------------------

THRESHOLDS = {
    "categories": {
        "safety": {"gate": True, "min_pass_rate": 1.0, "repeats": {"stub": 1, "real": 1}},
        "task": {"gate": True, "min_pass_rate": 0.9, "repeats": {"stub": 2, "real": 1}},
    },
    "budget": {"max_tokens_per_suite": 1000, "warn_seconds_per_scenario": 120},
}


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pg: str,
    argv: list[str],
    *,
    git_sha: str = "sha-new",
    passed: bool = True,
) -> dict[str, Any]:
    """`evals.run.main` against `pg`, with every attempt faked; returns its `EVAL_STATUS`."""
    for cid, category in (("s1", "safety"), ("t1", "task")):
        (tmp_path / category).mkdir(exist_ok=True)
        data = {"id": cid, "category": category, "tags": ["fast"], "run": "hello"}
        data["expect"] = [{"check": "outcome_recorded"}]
        (tmp_path / category / f"{cid}.yaml").write_text(yaml.safe_dump(data))
    load = LOAD_CASES
    monkeypatch.setenv("DATABASE_URL", pg)
    monkeypatch.setenv("GIT_SHA", git_sha)
    monkeypatch.setenv("RUN_TOKEN_CEILING", "100")
    monkeypatch.setattr(run, "load_thresholds", lambda: THRESHOLDS)
    monkeypatch.setattr(run, "load_cases", lambda categories: load(categories, tmp_path))
    monkeypatch.setattr(run, "write_results", lambda summary, results, md: Path("r.json"))
    monkeypatch.setattr(run, "agents_health", lambda: {"model_mode": "real", "model_name": "m"})
    run_id = ids.new_run_id()

    def attempt(
        c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"
    ) -> RunResult:
        res = RunResult(c["id"], c["category"], repeat, passed=passed, mode=mode)
        res.run_id, res.tokens, res.seconds, res.models_used = str(run_id), 7, 1.5, ["m"]
        res.checks = [{"check": "outcome_recorded", "args": {}, "passed": passed, "detail": "x"}]
        return res

    monkeypatch.setattr(run, "run_case", attempt)
    lines: list[str] = []
    monkeypatch.setattr(
        run, "print", lambda *a, **k: lines.append(" ".join(map(str, a))), raising=False
    )
    code = run.main(argv)
    status = json.loads(lines[-1].removeprefix("EVAL_STATUS "))
    assert status["exit_code"] == code
    return {**status, "agent_run_id": run_id}


def test_no_trigger_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg: str
) -> None:
    status = _run_main(tmp_path, monkeypatch, pg, ["--all"])
    assert (status["outcome"], status["trigger"], status["last_green_sha"]) == ("pass", None, None)
    with psycopg.connect(pg) as conn:
        assert conn.execute("SELECT count(*) FROM eval_runs").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM eval_results").fetchone() == (0,)

    # With --trigger, the same run writes its row, one row per attempt and the outcome.
    status = _run_main(tmp_path, monkeypatch, pg, ["--all", "--trigger", "manual"])
    with psycopg.connect(pg, row_factory=dict_row) as conn:
        [row] = conn.execute("SELECT * FROM eval_runs").fetchall()
        results = conn.execute("SELECT * FROM eval_results ORDER BY case_id, repeat").fetchall()
    assert str(row["eval_run_id"]) == status["eval_run_id"]
    assert (row["trigger"], row["git_sha"], row["model_mode"]) == ("manual", "sha-new", "stub")
    assert (row["outcome"], row["outcome_detail"], row["tokens"]) == ("pass", None, 21)
    assert row["selection"] == ["s1", "t1"] and row["finished_at"] is not None
    assert row["thresholds"] == THRESHOLDS
    assert row["error_kind_counts"] == {"none": 3}
    assert [(r["case_id"], r["repeat"], r["verdict"]) for r in results] == [
        ("s1", 1, "pass"),
        ("t1", 1, "pass"),
        ("t1", 2, "pass"),
    ]
    first = results[0]
    assert first["agent_run_ids"] == [status["agent_run_id"]]
    assert (first["category"], first["model_mode"], first["error_kind"]) == (
        "safety",
        "stub",
        "none",
    )
    s1_file = (tmp_path / "safety" / "s1.yaml").read_bytes()
    assert first["case_hash"] == hashlib.sha256(s1_file).hexdigest()
    assert first["checks"][0]["invariant"] is True and first["models_used"] == ["m"]


def test_last_green_sha_same_mode_and_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg: str
) -> None:
    rows = [
        # (git_sha, trigger, model_mode, outcome, minutes ago)
        ("sha-old-green", "nightly", "real", "pass", 50),
        ("sha-green", "nightly", "real", "pass", 40),
        ("sha-stub", "nightly", "stub", "pass", 30),
        ("sha-manual", "manual", "real", "pass", 20),
        ("sha-red", "nightly", "real", "regression", 10),
        ("sha-died", "nightly", "real", None, 5),
    ]
    with psycopg.connect(pg) as conn:
        for sha, trigger, mode, result, ago in rows:
            conn.execute(
                "INSERT INTO eval_runs (eval_run_id, trigger, git_sha, model_mode, models,"
                " selection, thresholds, outcome, started_at) VALUES"
                " (%s, %s, %s, %s, '{}', '{}', '{}', %s, now() - make_interval(mins => %s))",
                (ids.new_eval_run_id(), trigger, sha, mode, result, ago),
            )
    argv = ["--all", "--mode", "real", "--trigger", "nightly"]
    status = _run_main(tmp_path, monkeypatch, pg, argv, git_sha="sha-a", passed=False)
    assert status["outcome"] == "regression"
    assert status["last_green_sha"] == "sha-green"
    # A green run becomes the next run's last green; a red one never does.
    status = _run_main(tmp_path, monkeypatch, pg, argv, git_sha="sha-b")
    assert (status["outcome"], status["last_green_sha"]) == ("pass", "sha-green")
    status = _run_main(tmp_path, monkeypatch, pg, argv, git_sha="sha-c")
    assert status["last_green_sha"] == "sha-b"
    stub = _run_main(tmp_path, monkeypatch, pg, ["--all", "--trigger", "nightly"])
    assert stub["last_green_sha"] == "sha-stub"
