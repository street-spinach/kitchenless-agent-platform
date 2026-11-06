"""scripts/ci/report_deploy.sh with a fake `gh` that records every call."""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ci/report_deploy.sh"
SHA = "a" * 40
EARLIER = "b" * 40
GREEN = "c" * 40
API = "repos/o/r"

FAKE_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_DIR/calls"
case "$*" in
  "api --paginate repos/o/r/compare/"*)
    [ -e "$GH_DIR/compare-fails" ] && exit 1
    cat "$GH_DIR/range" ;;
  "api repos/o/r/compare/"*)
    [ -e "$GH_DIR/compare-fails" ] && exit 1
    wc -l < "$GH_DIR/range" | tr -d ' ' ;;
  "api repos/o/r/commits/"*)
    sha=${2#repos/o/r/commits/}
    cat "$GH_DIR/pr-${sha%/pulls}" 2>/dev/null || true ;;
  "pr view "*) echo "Closes #$((${3} + 1000))" ;;
esac
"""


class World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.gh = root / "gh-state"
        self.gh.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "gh"
        fake.write_text(FAKE_GH)
        fake.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.merged(SHA, 1)

    def merged(self, sha: str, pr: int) -> None:
        """`sha` came from PR `pr`, which closes issue 1000 + `pr`."""
        (self.gh / f"pr-{sha}").write_text(f"{pr}\n")

    def since_green(self, *shas: str) -> None:
        (self.gh / "range").write_text("".join(f"{sha}\n" for sha in shas))

    def log(self, text: str) -> None:
        (self.root / "deploy.log").write_text(text)

    def report(self, status: str) -> list[str]:
        result = subprocess.run(
            ["bash", str(SCRIPT), status],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=os.environ
            | {
                "PATH": self.path,
                "GH_DIR": str(self.gh),
                "GITHUB_REPOSITORY": "o/r",
                "GITHUB_SERVER_URL": "https://github.test",
                "GITHUB_RUN_ID": "7",
                "SHA": SHA,
            },
        )
        assert result.returncode == 0, result.stderr
        calls = self.gh / "calls"
        return calls.read_text().splitlines() if calls.exists() else []


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def failed_log(last_green: str = GREEN) -> str:
    return (
        f"Deploying {SHA} (last green: {last_green})\n"
        "1 failed\n"
        f"Verification failed for {SHA}.\n"
        f"Rolling back to image tag {last_green}\n"
    )


def green_log(last_green: str = GREEN) -> str:
    return f"Deploying {SHA} (last green: {last_green})\nDeploy {SHA} verified.\n"


def requeued(calls: list[str]) -> bool:
    return any(c.startswith("issue reopen") or "--add-label ready" in c for c in calls)


def test_no_log_says_the_vm_was_not_reached_and_does_not_requeue(world: World) -> None:
    calls = world.report("failure")
    assert not requeued(calls)
    assert not any("--add-label" in c for c in calls)
    assert any(c.startswith("pr comment 1 ") and "didn't reach the VM" in c for c in calls)
    assert any(c.startswith("issue comment 1001 ") and "didn't reach the VM" in c for c in calls)
    assert not any("rolled back." in c and "failed verification" in c for c in calls)


@pytest.mark.parametrize(
    "log",
    [
        "",
        "ssh: connect to host 203.0.113.9 port 22: Connection timed out\n",
        "fatal: unable to access 'https://github.com/o/r/': 503\n",
        "GHCR_READ_TOKEN is not set in infra/.env. Not deploying.\n",
    ],
)
def test_log_without_deploy_sh_starting_does_not_requeue(world: World, log: str) -> None:
    """`tee` writes deploy.log even when SSH or `git fetch` fails (#129)."""
    world.log(log)
    calls = world.report("failure")
    assert not requeued(calls)
    assert any("didn't reach the VM" in c for c in calls)


def test_vm_host_in_an_ssh_error_is_not_posted(world: World) -> None:
    world.log("ssh: connect to host 203.0.113.9 port 22: Connection timed out\n")
    assert not any("203.0.113.9" in c for c in world.report("failure"))


def test_real_failure_of_the_only_new_commit_requeues(world: World) -> None:
    world.log(failed_log())
    world.since_green(SHA)
    calls = world.report("failure")
    assert "issue reopen 1001" in calls
    assert "issue edit 1001 --add-label ready,deploy-failed" in calls
    assert any(c.startswith("pr comment 1 ") and "1 failed" in c for c in calls)
    assert any(c.startswith("issue comment 1001 ") and "was rolled back." in c for c in calls)


def test_failure_with_earlier_commits_since_green_is_not_requeued(world: World) -> None:
    world.log(failed_log())
    world.since_green(EARLIER, SHA)
    calls = world.report("failure")
    assert not requeued(calls)
    assert "issue edit 1001 --add-label deploy-failed" in calls
    assert any("may be inherited" in c for c in calls)


@pytest.mark.parametrize("last_green", ["none", SHA])
def test_failure_with_no_new_commit_to_blame_is_not_requeued(world: World, last_green: str) -> None:
    world.log(failed_log(last_green))
    world.since_green()
    assert not requeued(world.report("failure"))


def test_failure_is_not_requeued_when_the_range_cannot_be_read(world: World) -> None:
    world.log(failed_log())
    (world.gh / "compare-fails").touch()
    assert not requeued(world.report("failure"))


@pytest.mark.parametrize(
    "log",
    [
        # SSH dropped mid-deploy.
        f"Deploying {SHA} (last green: {GREEN})\nConnection to 203.0.113.9 closed.\n",
        # Verified, then `agents` didn't return to real mode, or the tag push failed.
        green_log(),
    ],
)
def test_failure_outside_verification_is_not_requeued(world: World, log: str) -> None:
    world.log(log)
    calls = world.report("failure")
    assert not requeued(calls)
    assert any("not at verification" in c for c in calls)
    assert not any("203.0.113.9" in c for c in calls)


def test_green_clears_deploy_failed_from_every_issue_it_contains(world: World) -> None:
    world.log(green_log())
    world.merged(EARLIER, 2)
    world.since_green(EARLIER, SHA)
    calls = world.report("success")
    assert "issue edit 1001 --remove-label deploy-failed" in calls
    assert "issue edit 1002 --remove-label deploy-failed" in calls
    assert not requeued(calls)
    assert any(c.startswith("pr comment 1 ") and "Deployed and verified" in c for c in calls)


def test_green_clears_its_own_issue_when_there_is_no_earlier_green(world: World) -> None:
    world.log(green_log("none"))
    assert "issue edit 1001 --remove-label deploy-failed" in world.report("success")


def test_green_still_clears_its_own_issue_when_the_range_cannot_be_read(world: World) -> None:
    world.log(green_log())
    (world.gh / "compare-fails").touch()
    assert "issue edit 1001 --remove-label deploy-failed" in world.report("success")


def test_green_direct_push_still_clears_the_issues_it_contains(world: World) -> None:
    world.log(green_log())
    (world.gh / f"pr-{SHA}").unlink()
    world.merged(EARLIER, 2)
    world.since_green(EARLIER, SHA)
    calls = world.report("success")
    assert "issue edit 1002 --remove-label deploy-failed" in calls
    assert not any(c.startswith("pr comment") for c in calls)


def test_frozen_deploy_verified_nothing_and_clears_nothing(world: World) -> None:
    world.log(f"Deploy of {SHA} skipped: /opt/app/DEPLOY_FREEZE exists.\n")
    assert not any("--remove-label" in c for c in world.report("success"))
