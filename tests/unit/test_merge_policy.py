"""scripts/ci/merge_policy.sh against throwaway git repos."""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ci/merge_policy.sh"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, env=os.environ | GIT_ENV
    )


def write(repo: Path, path: str, text: str = "x\n") -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, ".github/critical-paths.txt", (REPO / ".github/critical-paths.txt").read_text())
    write(tmp_path, "services/safety/allergens.py", "ALLERGENS = ['tree_nuts']\n" * 20)
    write(tmp_path, "services/agents/consumer/agent.py")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def policy(repo: Path, labels: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=os.environ | {"LABELS": labels},
    )


def commit(repo: Path) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")


def test_non_critical_change_passes(repo: Path) -> None:
    write(repo, "services/agents/consumer/agent.py", "changed\n")
    commit(repo)
    result = policy(repo)
    assert result.returncode == 0, result.stdout
    assert "No critical paths touched" in result.stdout


@pytest.mark.parametrize(
    "path",
    ["services/safety/allergens.py", "evals/thresholds.yaml", "CLAUDE.md", ".github/x.yml"],
)
def test_critical_change_without_label_fails(repo: Path, path: str) -> None:
    write(repo, path, "changed\n")
    commit(repo)
    result = policy(repo)
    assert result.returncode == 1
    assert path in result.stdout


def test_critical_change_with_label_passes(repo: Path) -> None:
    write(repo, "services/safety/allergens.py", "changed\n")
    commit(repo)
    assert policy(repo, labels="ready,approved-by-human").returncode == 0


@pytest.mark.parametrize("labels", ["not-approved-by-human", "approved-by-human-later", "ready"])
def test_lookalike_labels_do_not_pass(repo: Path, labels: str) -> None:
    write(repo, "services/safety/allergens.py", "changed\n")
    commit(repo)
    assert policy(repo, labels=labels).returncode == 1


def test_deleting_critical_file_fails(repo: Path) -> None:
    git(repo, "rm", "-q", "services/safety/allergens.py")
    commit(repo)
    assert policy(repo).returncode == 1


def test_moving_file_out_of_critical_path_fails(repo: Path) -> None:
    # Regression: with rename detection, git diff --name-only only lists the new path.
    (repo / "services/agents/common").mkdir(parents=True)
    git(repo, "mv", "services/safety/allergens.py", "services/agents/common/allergens.py")
    commit(repo)
    result = policy(repo)
    assert result.returncode == 1
    assert "services/safety/allergens.py" in result.stdout


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
