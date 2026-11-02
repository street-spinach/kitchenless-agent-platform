"""`GET /panel/evals`, the eval results page (evals design §14, EG7, E-A7; issue #233)."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.api.routes import panel
from services.common import ids
from tests.unit import api_world as w

pytestmark = pytest.mark.db

NOW = datetime.now(UTC)
MODELS = {"people": "people-model-1", "business": "business-model-1"}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    w.patch_settings(monkeypatch)
    # Three real-capable case files (the default modes and [real]) and one stub-only.
    cases = {
        "safety/safe-a.yaml": "id: safe-a\n",
        "safety/safe-b.yaml": "id: safe-b\nmodel_modes: [stub, real]\n",
        "task/task-a.yaml": "id: task-a\nmodel_modes: [real]\n",
        "task/stub-only.yaml": "id: stub-only\nmodel_modes: [stub]\n",
    }
    for name, text in cases.items():
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(text)
    monkeypatch.setattr(panel, "EVAL_CASES_DIR", tmp_path)


def _eval_run(
    conn: psycopg.Connection,
    *,
    mode: str,
    sha: str,
    started: datetime,
    trigger: str = "nightly",
    models: dict[str, str] | None = None,
) -> uuid.UUID:
    eval_run_id = ids.new_eval_run_id()
    conn.execute(
        "INSERT INTO eval_runs (eval_run_id, trigger, git_sha, model_mode, models, selection,"
        " thresholds, started_at) VALUES (%s, %s, %s, %s, %s, '{}', '{}', %s)",
        (eval_run_id, trigger, sha, mode, Jsonb(models or MODELS), started),
    )
    return eval_run_id


def _result(
    conn: psycopg.Connection,
    eval_run_id: uuid.UUID,
    case_id: str,
    *,
    mode: str,
    verdict: str,
    created: datetime,
    category: str = "safety",
    repeat: int = 0,
    agent_run_ids: list[uuid.UUID] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO eval_results (eval_run_id, case_id, repeat, category, case_hash,"
        " model_mode, agent_run_ids, verdict, error_kind, seconds, checks, created_at)"
        " VALUES (%s, %s, %s, %s, 'h', %s, %s, %s, 'none', 1.0, '{}', %s)",
        (
            eval_run_id,
            case_id,
            repeat,
            category,
            mode,
            agent_run_ids or [],
            verdict,
            created,
        ),
    )


def _get(pg: str) -> str:
    response = w.client(pg).get("/panel/evals")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<script" not in response.text
    return response.text


def _section(page: str, start: str, end: str | None) -> str:
    tail = page.split(start, 1)[1]
    return tail if end is None else tail.split(end, 1)[0]


def _headline(page: str) -> str:
    return _section(page, "Real model, last 7 days", panel.STUB_HEADING)


def _cases(page: str) -> str:
    return _section(page, "<h2>Cases</h2>", None)


def _case_row(page: str, case_id: str) -> str:
    match = re.search(rf"<tr><td>{re.escape(case_id)}</td>.*?</tr>", _cases(page))
    assert match is not None, case_id
    return match.group(0)


def test_no_rows(pg: str) -> None:
    page = _get(pg)
    assert panel.NO_EVAL_RUNS in page
    assert "<table" not in page


def test_no_history_tables(empty_schema_dsn: str) -> None:
    page = _get(empty_schema_dsn)
    assert panel.NO_EVAL_RUNS in page


def test_eval_run_without_results_is_no_rows(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        _eval_run(conn, mode="real", sha="aaa", started=NOW)
    assert panel.NO_EVAL_RUNS in _get(pg)


def test_real_headline_excludes_stub_rows(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        real = _eval_run(conn, mode="real", sha="realsha", started=NOW - timedelta(hours=1))
        _result(conn, real, "safe-a", mode="real", verdict="pass", created=NOW)
        stub = _eval_run(conn, mode="stub", sha="stubsha", started=NOW - timedelta(hours=2))
        _result(conn, stub, "safe-a", mode="stub", verdict="fail", created=NOW)
        _result(conn, stub, "safe-b", mode="stub", verdict="fail", created=NOW)
    page = _get(pg)
    headline = _headline(page)
    assert "1 of 3 real-capable cases run in 7 days" in headline
    assert "Safety pass rate: 1 of 1 (100%)" in headline
    assert "stubsha" not in headline and "safe-b" not in headline
    assert "people people-model-1; business business-model-1" in headline
    assert panel.NO_EVAL_RUNS not in page
    # The stub nightly sits in its own labelled section.
    stub_section = _section(page, panel.STUB_HEADING, "<h2>Cases</h2>")
    assert "stubsha" in stub_section and "safe-b" in stub_section
    assert "realsha" not in stub_section


def test_headline_uses_latest_real_attempt_in_7_days(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        old = _eval_run(conn, mode="real", sha="oldsha", started=NOW - timedelta(days=9))
        _result(conn, old, "task-a", mode="real", verdict="pass", created=NOW - timedelta(days=9))
        first = _eval_run(conn, mode="real", sha="s1", started=NOW - timedelta(days=2))
        _result(conn, first, "safe-a", mode="real", verdict="pass", created=NOW - timedelta(2))
        second = _eval_run(conn, mode="real", sha="s2", started=NOW - timedelta(hours=1))
        _result(conn, second, "safe-a", mode="real", verdict="fail", created=NOW)
        # A skipped safety attempt counts in neither side of the pass rate (evals.md §5).
        _result(conn, second, "safe-b", mode="real", verdict="skipped", created=NOW)
    headline = _headline(_get(pg))
    assert "2 of 3 real-capable cases run in 7 days" in headline
    assert "Safety pass rate: 0 of 1 (0%)" in headline
    assert "oldsha" not in headline and "task-a" not in headline


def test_oldest_and_newest_sha_shown(pg: str) -> None:
    oldest_at, newest_at = NOW - timedelta(days=5), NOW - timedelta(hours=3)
    with psycopg.connect(pg) as conn:
        early = _eval_run(conn, mode="real", sha="sha-oldest", started=oldest_at)
        _result(conn, early, "safe-a", mode="real", verdict="pass", created=oldest_at)
        middle = _eval_run(conn, mode="real", sha="sha-middle", started=NOW - timedelta(days=3))
        _result(conn, middle, "safe-b", mode="real", verdict="pass", created=NOW - timedelta(3))
        late = _eval_run(conn, mode="real", sha="sha-newest", started=newest_at)
        _result(conn, late, "task-a", mode="real", verdict="pass", created=newest_at)
    headline = _headline(_get(pg))
    assert f"Oldest: sha-oldest {oldest_at.strftime('%Y-%m-%d %H:%M')} UTC" in headline
    assert f"Newest: sha-newest {newest_at.strftime('%Y-%m-%d %H:%M')} UTC" in headline


def test_stale_marker_after_48_hours(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        real = _eval_run(conn, mode="real", sha="a", started=NOW - timedelta(hours=50))
        created = NOW - timedelta(hours=49)
        _result(conn, real, "safe-a", mode="real", verdict="pass", created=created)
    assert "<strong>stale</strong>" in _headline(_get(pg))


def test_no_stale_marker_within_48_hours(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        real = _eval_run(conn, mode="real", sha="a", started=NOW - timedelta(hours=47))
        _result(conn, real, "safe-a", mode="real", verdict="pass", created=NOW)
    assert "stale" not in _headline(_get(pg))


def test_stale_marker_with_no_real_eval_run(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        stub = _eval_run(conn, mode="stub", sha="a", started=NOW)
        _result(conn, stub, "safe-a", mode="stub", verdict="pass", created=NOW)
    headline = _headline(_get(pg))
    assert "<strong>stale</strong>" in headline
    assert "0 of 3 real-capable cases run in 7 days" in headline


def test_last_green_picks_latest_all_pass_real_nightly(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        green_old = _eval_run(conn, mode="real", sha="green-old", started=NOW - timedelta(4))
        _result(conn, green_old, "safe-a", mode="real", verdict="pass", created=NOW - timedelta(4))
        green = _eval_run(conn, mode="real", sha="green-new", started=NOW - timedelta(3))
        for repeat in range(3):
            _result(
                conn, green, "safe-a", mode="real", verdict="pass", repeat=repeat,
                created=NOW - timedelta(3),
            )  # fmt: skip
        # Newer, but one repeat failed: not green.
        mixed = _eval_run(conn, mode="real", sha="mixed", started=NOW - timedelta(2))
        _result(conn, mixed, "safe-a", mode="real", verdict="pass", created=NOW - timedelta(2))
        _result(
            conn, mixed, "safe-a", mode="real", verdict="fail", repeat=1,
            created=NOW - timedelta(2),
        )  # fmt: skip
        # Newer and all pass, but not a nightly.
        manual = _eval_run(
            conn, mode="real", sha="manual", trigger="manual", started=NOW - timedelta(1)
        )
        _result(conn, manual, "safe-a", mode="real", verdict="pass", created=NOW - timedelta(1))
        # Newer and all pass, but a stub nightly.
        stub = _eval_run(conn, mode="stub", sha="stub-green", started=NOW)
        _result(conn, stub, "safe-a", mode="stub", verdict="pass", created=NOW)
        # Never green.
        _result(conn, mixed, "safe-b", mode="real", verdict="fail", created=NOW - timedelta(2))
    page = _get(pg)
    row = _case_row(page, "safe-a")
    green_cell = row.rsplit("<td>", 1)[1]
    assert green_cell.startswith(f"green-new {(NOW - timedelta(3)).strftime('%Y-%m-%d')}")
    assert _case_row(page, "safe-b").endswith("<td>never</td></tr>")


def test_case_table_shows_modes_and_last_verdict_per_mode(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        real = _eval_run(conn, mode="real", sha="realsha", started=NOW)
        _result(conn, real, "task-a", mode="real", verdict="fail", category="task", created=NOW)
        stub = _eval_run(conn, mode="stub", sha="stubsha", started=NOW)
        _result(conn, stub, "safe-a", mode="stub", verdict="pass", created=NOW)
    page = _get(pg)
    task = _case_row(page, "task-a")
    assert "<td>task</td><td>real</td><td>never</td><td>fail realsha" in task
    safe = _case_row(page, "safe-a")
    assert "<td>safety</td><td>stub, real</td><td>pass stubsha" in safe
    assert safe.count("<td>never</td>") == 2  # no real attempt and no green nightly


def test_links_use_runs_live(pg: str) -> None:
    agent_run_ids = [ids.new_run_id(), ids.new_run_id()]
    with psycopg.connect(pg) as conn:
        real = _eval_run(conn, mode="real", sha="a", started=NOW)
        _result(
            conn, real, "safe-a", mode="real", verdict="pass", created=NOW,
            agent_run_ids=agent_run_ids,
        )  # fmt: skip
        stub = _eval_run(conn, mode="stub", sha="b", started=NOW)
        _result(conn, stub, "safe-a", mode="stub", verdict="pass", created=NOW)
    page = _get(pg)
    link = f'<a href="/panel/runs/live?run_id={agent_run_ids[0]}">trace</a>'
    assert link in _headline(page)
    assert link in _case_row(page, "safe-a")
    assert str(agent_run_ids[1]) not in page
    # An attempt with no agent run (for example budget_skipped) has no link.
    assert page.count("/panel/runs/live?run_id=") == 2


def test_page_escapes_html(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        real = _eval_run(
            conn, mode="real", sha="<b>x</b>", started=NOW, models={"people": "<i>p</i>"}
        )
        _result(conn, real, "safe-a", mode="real", verdict="pass", created=NOW)
    page = _get(pg)
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
    assert "<i>p</i>" not in page
