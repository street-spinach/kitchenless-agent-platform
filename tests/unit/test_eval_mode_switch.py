"""infra/eval-mode.sh with fake DOCKER and HEALTH_CMD (evals §9, EG6, file 23).

The fake `docker` logs each call with the MODEL_MODE and MODEL_BUSINESS_NAME it sees and, on
`up`, writes the `/health` body the recreated `agents` would serve. The fake HEALTH_CMD
prints that body, or fails as a host that doesn't answer would.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra/eval-mode.sh"

FAKE_DOCKER = r"""#!/usr/bin/env bash
set -eu
printf '%s|MODEL_MODE=%s|MODEL_BUSINESS_NAME=%s\n' "$*" "${MODEL_MODE-<unset>}" \
  "${MODEL_BUSINESS_NAME-<unset>}" >> "$STATE/docker.log"
case " $* " in
  *" up "*)
    if [ -n "${FAKE_NEVER_HEALTHY:-}" ]; then rm -f "$STATE/health.json"; exit 0; fi
    mode="${MODEL_MODE:-$ENV_MODE}"
    name="${MODEL_BUSINESS_NAME:-$ENV_BUSINESS}"
    printf '{"status":"ok","model_mode":"%s","model_business_name":"%s"}' "$mode" "$name" \
      > "$STATE/health.json"
    ;;
esac
"""


@pytest.fixture
def state(tmp_path: Path) -> Path:
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    return tmp_path


def serving(state: Path, mode: str, business: str = "Muse Glimmer") -> None:
    body = {"status": "ok", "model_mode": mode, "model_name": "m", "model_business_name": business}
    (state / "health.json").write_text(json.dumps(body))


def run(state: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {
        "DOCKER": str(state / "docker"),
        "HEALTH_CMD": f"cat {state / 'health.json'}",
        "STATE": str(state),
        # What `.env` interpolation gives the fake when no shell override is set.
        "ENV_MODE": "stub",
        "ENV_BUSINESS": "Muse Glimmer",
        "EVAL_MODE_POLL_SECONDS": "1",
        "EVAL_MODE_WAIT_SECONDS": "3",
    }
    environ = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_")}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=state,  # no infra/.env here: the business name falls back to the compose default
        capture_output=True,
        text=True,
        env=environ | base | env,
        timeout=30,
    )


def docker_calls(state: Path) -> list[str]:
    log = state / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


UP = (
    "compose -f infra/docker-compose.yml --env-file infra/.env"
    " up -d --no-deps --force-recreate agents"
)


def test_real_recreates_agents_and_succeeds_on_match(state: Path) -> None:
    serving(state, "stub")
    result = run(state, "real", "Muse Spark Lite")
    assert result.returncode == 0, result.stderr
    assert "already_real" not in result.stdout
    assert docker_calls(state) == [f"{UP}|MODEL_MODE=real|MODEL_BUSINESS_NAME=Muse Spark Lite"]


def test_real_defaults_business_name(state: Path) -> None:
    serving(state, "stub")
    result = run(state, "real")
    assert result.returncode == 0, result.stderr
    assert docker_calls(state) == [f"{UP}|MODEL_MODE=real|MODEL_BUSINESS_NAME=Muse Glimmer"]


def test_real_exits_1_when_health_never_confirms(state: Path) -> None:
    serving(state, "stub")
    result = run(state, "real", "Muse Spark Lite", FAKE_NEVER_HEALTHY="1")
    assert result.returncode == 1
    assert "did not confirm" in result.stderr
    assert len(docker_calls(state)) == 1


def test_real_exits_1_when_mode_stays_stub(state: Path) -> None:
    """A recreated `agents` that still reports stub (e.g. the override was lost) fails."""
    (state / "docker").write_text(
        FAKE_DOCKER.replace('mode="${MODEL_MODE:-$ENV_MODE}"', 'mode="stub"')
    )
    serving(state, "stub")
    result = run(state, "real", "Muse Spark Lite")
    assert result.returncode == 1
    assert len(docker_calls(state)) == 1


def test_real_prints_already_real_and_recreates_nothing(state: Path) -> None:
    serving(state, "real", "Muse Spark Lite")
    result = run(state, "real", "Muse Spark Lite")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "already_real"
    assert docker_calls(state) == []


def test_real_with_another_business_model_recreates(state: Path) -> None:
    serving(state, "real", "Muse Glimmer")
    result = run(state, "real", "Muse Spark Lite")
    assert result.returncode == 0, result.stderr
    assert "already_real" not in result.stdout
    assert docker_calls(state) == [f"{UP}|MODEL_MODE=real|MODEL_BUSINESS_NAME=Muse Spark Lite"]


def test_restore_recreates_with_no_model_mode_in_environment(state: Path) -> None:
    serving(state, "real", "Muse Spark Lite")
    # Even when the caller's shell still holds the real-mode overrides.
    result = run(state, "restore", MODEL_MODE="real", MODEL_BUSINESS_NAME="Muse Spark Lite")
    assert result.returncode == 0, result.stderr
    assert docker_calls(state) == [f"{UP}|MODEL_MODE=<unset>|MODEL_BUSINESS_NAME=<unset>"]
    assert json.loads((state / "health.json").read_text())["model_mode"] == "stub"


def test_restore_never_forces_stub(state: Path) -> None:
    """On demo week `.env` holds MODEL_MODE=real; restore leaves `agents` in real mode."""
    serving(state, "real")
    result = run(state, "restore", ENV_MODE="real")
    assert result.returncode == 0, result.stderr
    assert json.loads((state / "health.json").read_text())["model_mode"] == "real"


def test_restore_exits_1_when_health_never_answers(state: Path) -> None:
    serving(state, "real")
    result = run(state, "restore", FAKE_NEVER_HEALTHY="1")
    assert result.returncode == 1
    assert len(docker_calls(state)) == 1
