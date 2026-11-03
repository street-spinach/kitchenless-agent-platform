"""The eval lock (evals.md §5, EG goal "one eval run per environment"; build step 5c)."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals import run
from evals.run import RunResult

pytestmark = pytest.mark.db

LOAD_CASES = run.load_cases

THRESHOLDS = {
    "categories": {"task": {"gate": True, "min_pass_rate": 0.9, "repeats": {"stub": 1, "real": 1}}},
    "budget": {"max_tokens_per_suite": 100, "warn_seconds_per_scenario": 120},
}


def _main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg: str) -> tuple[int, dict[str, Any]]:
    (tmp_path / "task").mkdir(exist_ok=True)
    data = {"id": "a", "category": "task", "tags": ["fast"], "run": "hello"}
    data["expect"] = [{"check": "outcome_recorded"}]
    (tmp_path / "task" / "a.yaml").write_text(yaml.safe_dump(data))
    load = LOAD_CASES
    monkeypatch.setenv("DATABASE_URL", pg)
    monkeypatch.setenv("GIT_SHA", "abc123")
    monkeypatch.setattr(run, "load_thresholds", lambda: THRESHOLDS)
    monkeypatch.setattr(run, "load_cases", lambda categories: load(categories, tmp_path))
    monkeypatch.setattr(run, "write_results", lambda summary, results, md: Path("r.json"))
    started: list[str] = []

    def attempt(
        c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"
    ) -> RunResult:
        started.append(c["id"])
        return RunResult(c["id"], c["category"], repeat, passed=True)

    monkeypatch.setattr(run, "run_case", attempt)
    lines: list[str] = []
    monkeypatch.setattr(
        run, "print", lambda *a, **k: lines.append(" ".join(map(str, a))), raising=False
    )
    code = run.main(["--all", "--trigger", "manual"])
    status = json.loads(lines[-1].removeprefix("EVAL_STATUS "))
    return code, {**status, "started": started}


def test_second_runner_waits_then_ends_infra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg: str
) -> None:
    holder = run.take_lock(pg, wait=0, poll=0.1)
    assert holder is not None
    try:
        # The wait limit is a parameter: 1 s here instead of 10 minutes.
        monkeypatch.setattr(run, "LOCK_WAIT_SECONDS", 1.0)
        monkeypatch.setattr(run, "LOCK_POLL_SECONDS", 0.2)
        polls: list[float] = []
        sleep = run.time.sleep

        class Time:
            monotonic = staticmethod(run.time.monotonic)

            @staticmethod
            def sleep(seconds: float) -> None:
                polls.append(seconds)
                sleep(seconds)

        monkeypatch.setattr(run, "time", Time)
        code, status = _main(tmp_path, monkeypatch, pg)
        assert (code, status["outcome"], status["detail"]) == (3, "infra", "lock_timeout")
        assert status["started"] == []
        assert len(polls) >= 3 and set(polls) == {0.2}  # it polled, then gave up
        with run.psycopg.connect(pg) as conn:
            assert conn.execute("SELECT count(*) FROM eval_runs").fetchone() == (0,)
    finally:
        run.release_lock(holder)

    # Released, the next runner takes the lock at once, runs, and releases it again.
    code, status = _main(tmp_path, monkeypatch, pg)
    assert (code, status["outcome"], status["started"]) == (0, "pass", ["a"])
    again = run.take_lock(pg, wait=0, poll=0.1)
    assert again is not None
    run.release_lock(again)
