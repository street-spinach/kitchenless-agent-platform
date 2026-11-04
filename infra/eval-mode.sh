#!/usr/bin/env bash
# Switch `agents` to real model mode for an eval run, or restore `.env`'s mode (evals §9, EG6).
#
#   infra/eval-mode.sh real [business-model-name]
#   infra/eval-mode.sh restore
#
# Run from the app root (/opt/app). `real` prints `already_real` on stdout and recreates
# nothing when `/health` already reports real mode with the requested business model.
# `restore` recreates `agents` with no shell overrides, so `.env` applies again; it never
# forces stub. Exit 1 when `/health` doesn't confirm the mode within the wait limit.
#
# Test points: DOCKER (default `docker`) and HEALTH_CMD (default: fetch
# http://127.0.0.1:8003/health through `docker compose exec agents`); the wait limit and poll
# interval are EVAL_MODE_WAIT_SECONDS (90) and EVAL_MODE_POLL_SECONDS (2).
set -euo pipefail

DOCKER="${DOCKER:-docker}"
WAIT_SECONDS="${EVAL_MODE_WAIT_SECONDS:-90}"
POLL_SECONDS="${EVAL_MODE_POLL_SECONDS:-2}"
ENV_FILE=infra/.env
COMPOSE=("$DOCKER" compose -f infra/docker-compose.yml --env-file "$ENV_FILE")
# The compose file's default when `.env` has no MODEL_BUSINESS_NAME line.
DEFAULT_BUSINESS_NAME="meta-models/muse-spark-open"

usage() {
  echo "usage: infra/eval-mode.sh real [business-model-name] | restore" >&2
  exit 2
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

# The string value of a top-level JSON field (empty when absent): field <name> <json>.
field() {
  printf '%s' "$2" | sed -nE "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/p"
}

# True when `/health` answers with model_mode real and the given business model.
is_real() {
  local body
  body=$(health 2>/dev/null) || return 1
  [ "$(field model_mode "$body")" = "real" ] && [ "$(field model_business_name "$body")" = "$1" ]
}

# `.env`'s MODEL_BUSINESS_NAME, or the compose default when the line is missing.
env_business_name() {
  local line value
  line=$(grep '^MODEL_BUSINESS_NAME=' "$ENV_FILE" 2>/dev/null | tail -n 1 || true)
  value="${line#MODEL_BUSINESS_NAME=}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "${value:-$DEFAULT_BUSINESS_NAME}"
}

recreate() {
  "${COMPOSE[@]}" up -d --no-deps --force-recreate agents
}

# wait_for <description> <command...>: poll until the command succeeds or the limit passes.
wait_for() {
  local what="$1" waited=0
  shift
  until "$@"; do
    if [ "$waited" -ge "$WAIT_SECONDS" ]; then
      echo "eval-mode: /health did not confirm $what within ${WAIT_SECONDS}s" >&2
      return 1
    fi
    sleep "$POLL_SECONDS"
    waited=$((waited + POLL_SECONDS))
  done
}

health_ok() {
  health >/dev/null 2>&1
}

case "${1:-}" in
  real)
    [ "$#" -le 2 ] || usage
    business="${2:-}"
    [ -n "$business" ] || business=$(env_business_name)
    if is_real "$business"; then
      echo "already_real"
      exit 0
    fi
    # Shell variables override `.env` interpolation in the compose file.
    MODEL_MODE=real MODEL_BUSINESS_NAME="$business" recreate >&2
    wait_for "model_mode real, model_business_name $business" is_real "$business"
    echo "eval-mode: agents is in real mode with $business" >&2
    ;;
  restore)
    [ "$#" -eq 1 ] || usage
    # No overrides: `.env` decides the mode, also when it holds MODEL_MODE=real.
    unset MODEL_MODE MODEL_BUSINESS_NAME
    recreate >&2
    wait_for "a 200 answer" health_ok
    echo "eval-mode: agents restored from $ENV_FILE" >&2
    ;;
  *)
    usage
    ;;
esac
