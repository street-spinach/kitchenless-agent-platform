#!/usr/bin/env bash
# One night's eval run on the VM (evals §10): `make eval-nightly [BUSINESS_MODEL=<name>]`.
#
#   infra/eval-nightly.sh [business-model]
#
# Runs on the host as the VM user, as one script so its `trap` covers every step. Every path
# that doesn't reach the runner prints its own EVAL_STATUS line; the workflow reads that line,
# never the exit code. A business model means a D4 run (evals §11).
#
# Test points: APP_DIR, plus DOCKER and HEALTH_CMD as in infra/eval-mode.sh.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/app}"
DOCKER="${DOCKER:-docker}"
ENV_FILE=infra/.env
EXIT_INFRA=3 # the runner's exit code for outcome `infra` (evals §5)

cd "$APP_DIR"
COMPOSE=("$DOCKER" compose -f infra/docker-compose.yml --env-file "$ENV_FILE")

business="${1:-}"
trigger=nightly
[ -z "$business" ] || trigger=d4
sha=$(git rev-parse HEAD 2>/dev/null || true)
sha_json=null
[ -z "$sha" ] || sha_json="\"$sha\""

# The image the last verified deploy left running. APP_TAG is only exported inside deploy.sh,
# so without this `run` and `up` would use `.env`'s tag; the VM never builds images.
if [ -s .last_green ]; then
  APP_TAG=$(cat .last_green)
  export APP_TAG
fi

# status <outcome> <detail> <exit code> <mode as JSON>: the runner's status line (evals §5).
status() {
  printf 'EVAL_STATUS {"outcome": "%s", "detail": "%s", "exit_code": %s, "eval_run_id": null, "mode": %s, "trigger": "%s", "git_sha": %s, "last_green_sha": null, "failed_cases": [], "real_safety_failed": false}\n' \
    "$1" "$2" "$3" "$4" "$trigger" "$sha_json"
}

# Print the `/health` JSON body; non-zero when it doesn't answer 200.
health() {
  if [ -n "${HEALTH_CMD:-}" ]; then
    bash -c "$HEALTH_CMD"
  else
    "${COMPOSE[@]}" exec -T agents python -c \
      "import sys,urllib.request; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8003/health', timeout=3).read().decode())"
  fi
}

# The `model_mode` `/health` reports; empty when it doesn't answer or doesn't say.
agents_mode() {
  health 2>/dev/null | sed -nE 's/.*"model_mode"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' || true
}

# `.env`'s REAL_EVALS_ENABLED, without quotes; empty when the line is missing.
real_evals_enabled() {
  local line value
  line=$(grep '^REAL_EVALS_ENABLED=' "$ENV_FILE" 2>/dev/null | tail -n 1 || true)
  value="${line#REAL_EVALS_ENABLED=}"
  value="${value%\"}"
  printf '%s' "${value#\"}"
}

runner() {
  "${COMPOSE[@]}" run --rm -e GIT_SHA="$sha" testrunner python -m evals.run "$@"
}

if [ "$#" -gt 1 ]; then
  status infra bad_arguments "$EXIT_INFRA" null
  exit "$EXIT_INFRA"
fi

# Step 1: a freeze skips the night, as it skips a deploy.
if [ -e "$APP_DIR/DEPLOY_FREEZE" ]; then
  status skipped freeze 0 null
  exit 0
fi

# Step 2. Anything but 1 means off, so a typo never spends tokens.
enabled=$(real_evals_enabled)
if [ "$enabled" != "1" ] && [ -n "$business" ]; then
  status infra real_evals_disabled "$EXIT_INFRA" null
  exit "$EXIT_INFRA"
fi

# Step 3: real evals off. The stub suite only runs on `agents` known to be in stub mode.
if [ "$enabled" != "1" ]; then
  case "$(agents_mode)" in
    real)
      status skipped agents_real 0 '"stub"'
      exit 0
      ;;
    stub)
      runner --all --mode stub --trigger nightly
      exit 0
      ;;
    *)
      status infra health_unavailable "$EXIT_INFRA" '"stub"'
      exit "$EXIT_INFRA"
      ;;
  esac
fi

# Step 4: real evals on. A failed switch still restores (evals §15), so the trap goes first.
switched=0
out=$(infra/eval-mode.sh real ${business:+"$business"}) || switched=$?
if [ "$out" != "already_real" ]; then
  trap 'infra/eval-mode.sh restore' EXIT
fi
if [ "$switched" -ne 0 ]; then
  status infra mode_switch_failed "$EXIT_INFRA" '"real"'
  exit "$EXIT_INFRA"
fi

if [ -n "$business" ]; then
  runner --all --mode real --trigger d4 --cases-file evals/d4.yaml
else
  runner --all --mode real --trigger nightly
fi
