"""Eval history tables survive the eval reset and need no `runs` row (evals design §6, E-A8;
issue #226)."""

import asyncio
import uuid

import psycopg
import pytest

from services.common import ids
from services.mocks import control
from services.seed import __main__ as seed_mod

pytestmark = pytest.mark.db

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
