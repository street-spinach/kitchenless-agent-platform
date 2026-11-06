"""infra/deploy.sh gives `agents` back the env file's model mode after a verified deploy
(architecture §11 step 6a, evals §9 "demo week").

The deploy runs in a temporary APP_DIR with fake `docker` and `make` on PATH and a fake
`infra/eval-mode.sh`; each logs its calls with the MODEL_MODE it sees.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra/deploy.sh"

SHEBANG = "#!/usr/bin/env bash\n"
LOG_CALL = r"""cat >/dev/null 2>&1 </dev/null
printf '%s %s|MODEL_MODE=%s\n' "$(basename "$0")" "$*" "${MODEL_MODE-<unset>}" >> "$STATE/calls.log"
"""
FAKE = SHEBANG + LOG_CALL
FAKE_MAKE = FAKE + 'case "$1" in scenario) exit "${FAKE_SCENARIO_RC:-0}";; esac\n'
FAKE_EVAL_MODE = FAKE + 'touch "$STATE/restored"\nexit "${FAKE_RESTORE_RC:-0}"\n'
# `compose exec agents` answers `/health` and isn't logged: it changes nothing. The mode is
# stub until eval-mode.sh has restored, so the deploy log shows when the read happened.
FAKE_DOCKER = (
    SHEBANG
    + r"""case " $* " in
  *" exec "*)
    [ "${FAKE_HEALTH_RC:-0}" = 0 ] || exit "$FAKE_HEALTH_RC"
    mode=stub
    [ -e "$STATE/restored" ] && mode=real
    printf '{"status":"ok","model_mode": "%s","token_source":"%s"}' \
      "$mode" "${FAKE_TOKEN_SOURCE:-mock}"
    exit 0
    ;;
esac
"""
    + LOG_CALL
)


def _script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    (tmp_path / "bin").mkdir()
    (tmp_path / "infra").mkdir()
    _script(tmp_path / "bin/docker", FAKE_DOCKER)
    _script(tmp_path / "bin/make", FAKE_MAKE)
    _script(tmp_path / "infra/eval-mode.sh", FAKE_EVAL_MODE)
    return tmp_path


def deploy(app: Path, env_mode: str | None, **env: str) -> tuple[int, list[str]]:
    lines = ["GHCR_READ_TOKEN=token"]
    if env_mode is not None:
        lines.append(f"MODEL_MODE={env_mode}")
    (app / "infra" / ".env").write_text("\n".join(lines) + "\n")
    environ = {k: v for k, v in os.environ.items() if k != "MODEL_MODE"}
    environ.update(
        {"APP_DIR": str(app), "STATE": str(app), "PATH": f"{app / 'bin'}:{environ['PATH']}"}
    )
    environ.update(env)
    done = subprocess.run(
        ["bash", str(SCRIPT), "abc123"], env=environ, capture_output=True, text=True, timeout=30
    )
    (app / "deploy.log").write_text(done.stdout)
    log = app / "calls.log"
    return done.returncode, log.read_text().splitlines() if log.exists() else []


def active_modes(app: Path) -> list[str]:
    lines = (app / "deploy.log").read_text().splitlines()
    return [line for line in lines if line.startswith("Active modes:")]


def test_real_env_restored_after_verified_deploy(app: Path) -> None:
    code, calls = deploy(app, '"real"')
    assert code == 0
    assert (app / ".last_green").read_text().strip() == "abc123"
    # Every check ran in stub mode (G7); the restore came after the last check.
    checks = [c for c in calls if c.startswith("make ")]
    assert [c.split("|")[0] for c in checks] == ["make seed", "make smoke", "make scenario"]
    assert all(c.endswith("MODEL_MODE=stub") for c in checks)
    assert all(c.endswith("MODEL_MODE=stub") for c in calls if " up " in c)
    assert calls[-1].startswith("eval-mode.sh restore|")
    assert calls.index(checks[-1]) < len(calls) - 1


@pytest.mark.parametrize("env_mode", ["stub", None])
def test_stub_env_not_restored(app: Path, env_mode: str | None) -> None:
    code, calls = deploy(app, env_mode)
    assert code == 0
    assert not any(c.startswith("eval-mode.sh") for c in calls)


def test_failed_restore_back_to_stub(app: Path) -> None:
    code, calls = deploy(app, "real", FAKE_RESTORE_RC="1")
    assert code == 1
    # The image was verified: it stays the last green one, and agents is recreated in stub.
    assert (app / ".last_green").read_text().strip() == "abc123"
    assert calls[-2].startswith("eval-mode.sh restore|")
    assert " up " in calls[-1] and "agents" in calls[-1]
    assert calls[-1].endswith("MODEL_MODE=stub")
    assert active_modes(app) == []


def test_verified_deploy_logs_active_modes(app: Path) -> None:
    """#445: the line comes from `/health` after the restore, so it shows real, not stub."""
    code, _ = deploy(app, "real", FAKE_TOKEN_SOURCE="keycloak")
    assert code == 0
    assert active_modes(app) == ["Active modes: model_mode=real token_source=keycloak"]


def test_stub_deploy_logs_active_modes(app: Path) -> None:
    code, _ = deploy(app, "stub")
    assert code == 0
    assert active_modes(app) == ["Active modes: model_mode=stub token_source=mock"]


def test_unreadable_health_does_not_fail_verified_deploy(app: Path) -> None:
    code, _ = deploy(app, "stub", FAKE_HEALTH_RC="1")
    assert code == 0
    assert (app / ".last_green").read_text().strip() == "abc123"
    assert active_modes(app) == ["Active modes: model_mode=unknown token_source=unknown"]


FAKE_COMPOSE = r"""#!/usr/bin/env bash
case "$1" in
  exec) echo "$FAKE_MODE" ;;
  run) echo "suite ran"; exit "${FAKE_SUITE_RC:-0}" ;;
esac
"""


@pytest.mark.parametrize(
    ("mode", "suite_rc", "ran", "ok"),
    [
        ("real", "0", False, True),
        ("stub", "0", True, True),
        ("", "0", True, True),
        ("stub", "1", True, False),
    ],
)
def test_eval_full_skips_when_agents_real(
    tmp_path: Path, mode: str, suite_rc: str, ran: bool, ok: bool
) -> None:
    """The nightly's `make eval-full` never runs the stub suite on real-mode `agents`."""
    _script(tmp_path / "compose", FAKE_COMPOSE)
    environ = {**os.environ, "FAKE_MODE": mode, "FAKE_SUITE_RC": suite_rc}
    done = subprocess.run(
        ["make", "-s", "eval-full", f"COMPOSE={tmp_path / 'compose'}"],
        cwd=REPO,
        env=environ,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ("suite ran" in done.stdout) is ran
    assert ("agents_real" in done.stdout) is not ran
    assert (done.returncode == 0) is ok


def test_failed_verification_stays_stub(app: Path) -> None:
    code, calls = deploy(app, "real", FAKE_SCENARIO_RC="1")
    assert code == 1
    assert not (app / ".last_green").exists()
    assert not any(c.startswith("eval-mode.sh") for c in calls)
    assert active_modes(app) == []
