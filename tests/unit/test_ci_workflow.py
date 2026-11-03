"""The PR workflow holds no model key and runs the eval job as architecture item 63 says.

evals.md EG1 and file 6: parses .github/workflows/ci.yml.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
ARCHITECTURE = ROOT / "docs" / "designs" / "architecture.md"

# Architecture item 63's eight paths, in its order.
ITEM_63_PATHS = [
    "services/agents/",
    "services/mcp/",
    "services/safety/",
    "services/common/",
    "services/api/",
    "services/seed/",
    "migrations/",
    "evals/",
]
ITEM_63_REGEX = "^(" + "|".join(ITEM_63_PATHS) + ")"

SECRET_REF = re.compile(r"secrets\.([A-Za-z0-9_]+)")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(CI_YML.read_text())


def _eval_job() -> dict[str, Any]:
    return _workflow()["jobs"]["evals"]


def _run_lines(job: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for step in job.get("steps", []):
        lines.extend(line.strip() for line in str(step.get("run", "")).splitlines())
    return [line for line in lines if line]


def test_no_job_references_a_model_key() -> None:
    for name, job in _workflow()["jobs"].items():
        text = yaml.safe_dump({"env": job.get("env"), "steps": job.get("steps")})
        assert "MUSE_API_KEY" not in text, name
        assert "MODEL_API_KEY" not in text, name
        for secret in SECRET_REF.findall(text):
            assert "KEY" not in secret.upper() or secret == "GITHUB_TOKEN", (name, secret)


def test_no_model_key_anywhere_in_the_file() -> None:
    text = CI_YML.read_text()
    assert "MUSE_API_KEY" not in text
    assert "MODEL_API_KEY" not in text


def test_eval_job_sets_model_mode_stub() -> None:
    assert _eval_job()["env"]["MODEL_MODE"] == "stub"


def test_eval_job_regex_is_architecture_item_63() -> None:
    changed = next(s for s in _eval_job()["steps"] if s.get("id") == "changed")
    found = re.findall(r"grep -qE '([^']+)'", changed["run"])
    assert found == [ITEM_63_REGEX]
    # The design states the same filter, so the test fails if either side drifts.
    assert f"`'{ITEM_63_REGEX}'`" in ARCHITECTURE.read_text()


def test_eval_job_seeds_the_eval_households_before_eval_fast() -> None:
    lines = _run_lines(_eval_job())
    seed = lines.index('make seed SEED_ARGS="--env eval"')
    up = next(i for i, line in enumerate(lines) if "up -d --wait" in line)
    fast = lines.index("make eval-fast")
    assert up < seed < fast


def test_eval_results_upload_and_summary_run_always() -> None:
    steps = _eval_job()["steps"]
    upload = next(s for s in steps if str(s.get("uses", "")) == "actions/upload-artifact@v4")
    assert upload["with"]["name"] == "eval-results"
    assert upload["with"]["path"].rstrip("/") == "evals/results"
    assert "always()" in upload["if"]
    summary = next(s for s in steps if "GITHUB_STEP_SUMMARY" in str(s.get("run", "")))
    assert "always()" in summary["if"]
    assert "evals/results/*.md" in summary["run"]


def test_owner_app_job() -> None:
    """experience EX-I24: the owner-app job runs app-lint and app-test in node:22."""
    job = _workflow()["jobs"]["owner-app"]
    assert job["container"] == "node:22"
    steps = job["steps"]
    assert steps[0]["uses"] == "actions/checkout@v4"
    assert _run_lines(job) == ["make app-lint app-test"]
    assert not SECRET_REF.search(yaml.safe_dump(job))
