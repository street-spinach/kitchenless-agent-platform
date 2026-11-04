"""infra/eval-nightly.sh with a fake `docker` and fake `/health` (evals §10, file 26b).

The app root is a temporary directory holding the real infra/eval-mode.sh. The fake `docker`
logs each call with the APP_TAG it sees; on `up` it writes the `/health` body the recreated
`agents` would serve, and on `run` it prints the runner's status line.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra/eval-nightly.sh"
SHA = "abc123"

FAKE_DOCKER = r"""#!/usr/bin/env bash
set -eu
printf '%s|APP_TAG=%s\n' "$*" "${APP_TAG-<unset>}" >> "$STATE/docker.log"
case " $* " in
  *" up "*)
    if [ -n "${FAKE_NEVER_HEALTHY:-}" ] && [ -n "${MODEL_MODE:-}" ]; then
      rm -f "$STATE/health.json"
      exit 0
    fi
    printf '{"model_mode":"%s","model_business_name":"%s"}' "${MODEL_MODE:-stub}" \
      "${MODEL_BUSINESS_NAME:-meta-models/muse-spark-open}" > "$STATE/health.json"
    ;;
  *" run "*)
    echo 'EVAL_STATUS {"outcome": "pass", "detail": null}'
    exit "${FAKE_RUN_RC:-0}"
    ;;
esac
"""
FAKE_GIT = f"#!/usr/bin/env bash\necho {SHA}\n"

COMPOSE = "compose -f infra/docker-compose.yml --env-file infra/.env"
RUN = f"{COMPOSE} run --rm -e GIT_SHA={SHA} testrunner python -m evals.run --all"
UP = f"{COMPOSE} up -d --no-deps --force-recreate agents"


def _script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    (tmp_path / "bin").mkdir()
    (tmp_path / "infra").mkdir()
    shutil.copy(REPO / "infra/eval-mode.sh", tmp_path / "infra/eval-mode.sh")
    _script(tmp_path / "bin/docker", FAKE_DOCKER)
    _script(tmp_path / "bin/git", FAKE_GIT)
    return tmp_path


def serving(app: Path, mode: str) -> None:
    body = {"model_mode": mode, "model_business_name": "meta-models/muse-spark-open"}
    (app / "health.json").write_text(json.dumps(body))


def nightly(
    app: Path, *args: str, enabled: str | None = "0", **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    lines = ["OTHER=1"] + ([] if enabled is None else [f"REAL_EVALS_ENABLED={enabled}"])
    (app / "infra/.env").write_text("\n".join(lines) + "\n")
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("MODEL_", "APP_"))}
    environ |= {
        "APP_DIR": str(app),
        "DOCKER": str(app / "bin/docker"),
        "HEALTH_CMD": f"cat {app / 'health.json'}",
        "PATH": f"{app / 'bin'}:{environ['PATH']}",
        "STATE": str(app),
        "EVAL_MODE_POLL_SECONDS": "1",
        "EVAL_MODE_WAIT_SECONDS": "2",
    } | env
    done = subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=environ, timeout=30
    )
    log = app / "docker.log"
    calls = [c.split("|")[0] for c in log.read_text().splitlines()] if log.exists() else []
    return done, calls


def status_of(done: subprocess.CompletedProcess[str]) -> dict:
    last = done.stdout.strip().splitlines()[-1]
    assert last.startswith("EVAL_STATUS ")
    return json.loads(last.removeprefix("EVAL_STATUS "))


def test_freeze_skips_the_night(app: Path) -> None:
    serving(app, "stub")
    (app / "DEPLOY_FREEZE").touch()
    done, calls = nightly(app, enabled="1")
    status = status_of(done)
    assert (status["outcome"], status["detail"]) == ("skipped", "freeze")
    assert status["git_sha"] == SHA
    assert done.returncode == 0
    assert calls == []


@pytest.mark.parametrize("enabled", ["0", None, "yes", "true"])
def test_real_evals_off_and_agents_real_is_skipped(app: Path, enabled: str | None) -> None:
    serving(app, "real")
    done, calls = nightly(app, enabled=enabled)
    status = status_of(done)
    assert (status["outcome"], status["detail"]) == ("skipped", "agents_real")
    assert done.returncode == 0
    assert calls == []


def test_real_evals_off_runs_every_stub_case(app: Path) -> None:
    serving(app, "stub")
    done, calls = nightly(app)
    assert calls == [f"{RUN} --mode stub --trigger nightly"]
    assert status_of(done)["outcome"] == "pass"


def test_real_evals_off_and_no_health_runs_nothing(app: Path) -> None:
    """Without `/health` nothing shows `agents` is in stub mode, so tokens could be spent."""
    done, calls = nightly(app)
    status = status_of(done)
    assert (status["outcome"], status["detail"]) == ("infra", "health_unavailable")
    assert calls == []


def test_business_model_with_real_evals_off_is_infra(app: Path) -> None:
    serving(app, "stub")
    done, calls = nightly(app, "Muse Glimmer")
    status = status_of(done)
    assert (status["outcome"], status["detail"]) == ("infra", "real_evals_disabled")
    assert status["trigger"] == "d4"
    assert calls == []


def test_real_evals_on_switches_runs_and_restores(app: Path) -> None:
    serving(app, "stub")
    done, calls = nightly(app, enabled="1")
    assert calls == [UP, f"{RUN} --mode real --trigger nightly", UP]
    assert status_of(done)["outcome"] == "pass"
    assert json.loads((app / "health.json").read_text())["model_mode"] == "stub"


def test_quoted_enabled_value_counts(app: Path) -> None:
    serving(app, "stub")
    _, calls = nightly(app, enabled='"1"')
    assert f"{RUN} --mode real --trigger nightly" in calls


def test_business_model_is_a_d4_run(app: Path) -> None:
    serving(app, "stub")
    _, calls = nightly(app, "Muse Glimmer", enabled="1")
    assert calls == [UP, f"{RUN} --mode real --trigger d4 --cases-file evals/d4.yaml", UP]


def test_already_real_recreates_nothing_and_leaves_agents_alone(app: Path) -> None:
    serving(app, "real")
    _, calls = nightly(app, enabled="1")
    assert calls == [f"{RUN} --mode real --trigger nightly"]


def test_switch_failure_is_infra_and_restores(app: Path) -> None:
    serving(app, "stub")
    done, calls = nightly(app, enabled="1", FAKE_NEVER_HEALTHY="1")
    status = status_of(done)
    assert (status["outcome"], status["detail"]) == ("infra", "mode_switch_failed")
    assert not any(" run " in c for c in calls)
    assert calls == [UP, UP]  # the switch, then the trap's restore


def test_runner_failure_keeps_its_exit_code_and_restores(app: Path) -> None:
    serving(app, "stub")
    done, calls = nightly(app, enabled="1", FAKE_RUN_RC="1")
    assert done.returncode == 1
    assert calls[-1] == UP


def test_stub_runner_failure_keeps_its_exit_code(app: Path) -> None:
    serving(app, "stub")
    done, _ = nightly(app, FAKE_RUN_RC="3")
    assert done.returncode == 3


def test_more_than_one_argument_runs_nothing(app: Path) -> None:
    serving(app, "stub")
    done, calls = nightly(app, "Muse", "Glimmer", enabled="1")
    assert status_of(done)["detail"] == "bad_arguments"
    assert calls == []


def test_uses_the_image_of_the_last_verified_deploy(app: Path) -> None:
    """APP_TAG is only exported inside deploy.sh; `.env`'s value isn't an image on the VM."""
    serving(app, "stub")
    (app / ".last_green").write_text("deadbee\n")
    nightly(app, enabled="1")
    tags = {c.split("|")[1] for c in (app / "docker.log").read_text().splitlines()}
    assert tags == {"APP_TAG=deadbee"}


def test_makefile_target_passes_the_business_model() -> None:
    done = subprocess.run(
        ["make", "-n", "eval-nightly", "BUSINESS_MODEL=meta-models/x"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.stdout.strip() == "infra/eval-nightly.sh meta-models/x"


def test_makefile_target_output_is_the_same_under_an_outer_make(tmp_path: Path) -> None:
    """`make test-unit` is an outer make: its children must not print directory banners."""
    outer = tmp_path / "outer.mk"
    outer.write_text("outer:\n\t@make -n eval-nightly BUSINESS_MODEL=meta-models/x\n")
    done = subprocess.run(
        ["make", "-f", "Makefile", "-f", str(outer), "outer"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.stdout.strip() == "infra/eval-nightly.sh meta-models/x"


def test_testrunner_gets_the_runner_settings() -> None:
    services = yaml.safe_load((REPO / "infra/docker-compose.yml").read_text())["services"]
    env = services["testrunner"]["environment"]
    assert env["GIT_SHA"] == "${GIT_SHA:-}"
    assert env["EVAL_DEADLINE_SECONDS"] == "${EVAL_DEADLINE_SECONDS:-3000}"
    assert env["RUN_TOKEN_CEILING"] == "${RUN_TOKEN_CEILING}"


def test_env_example_keeps_real_evals_off() -> None:
    lines = (REPO / "infra/.env.example").read_text().splitlines()
    assert "REAL_EVALS_ENABLED=0" in lines
    assert "EVAL_DEADLINE_SECONDS=3000" in lines


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
