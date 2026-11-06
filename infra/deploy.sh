#!/usr/bin/env bash
# Deploy the image the `build` job pushed for SHA, verify it, and roll back to the last green
# image tag on failure (architecture §11 "Deploy changes"). The VM never builds images.
set -euo pipefail
SHA="$1"
APP_IMAGE_REPO="ghcr.io/street-spinach/kitchenless-app"
# The app root. Only tests set APP_DIR.
APP_DIR="${APP_DIR:-/opt/app}"

# Step 1: a freeze (before demo day, or a Keycloak upgrade) skips the deploy.
if [ -e "$APP_DIR/DEPLOY_FREEZE" ]; then
  echo "Deploy of $SHA skipped: $APP_DIR/DEPLOY_FREEZE exists."
  exit 0
fi

cd "$APP_DIR"
COMPOSE=(docker compose -f infra/docker-compose.yml --env-file infra/.env)
LAST_GREEN=$(cat .last_green 2>/dev/null || echo "")

# Step 2: log in to GHCR as the token's owner. The token is read from infra/.env and piped
# in, so it is never printed or put on a command line. No token, no deploy: nothing has
# changed yet, so there is nothing to roll back.
ghcr_token() {
  sed -n 's/^GHCR_READ_TOKEN=//p' infra/.env | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}
if ! ghcr_token | grep -q .; then
  echo "GHCR_READ_TOKEN is not set in infra/.env. Not deploying."
  exit 1
fi
ghcr_token | docker login ghcr.io -u madushankaf --password-stdin >/dev/null

# Step 3: shell values win over infra/.env in Compose interpolation. The checks run in stub
# mode (G7); step 6a gives `agents` back the mode infra/.env sets.
export APP_TAG="$SHA"
export MODEL_MODE=stub

# The MODEL_MODE line of infra/.env, without quotes (empty when missing).
env_model_mode() {
  sed -n 's/^MODEL_MODE=//p' infra/.env | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

# Caddy reads infra/Caddyfile through a single-file bind mount. `git checkout` replaces the
# file, the running container keeps the old one, and `up` only recreates Caddy when its
# service definition changes, so a Caddyfile-only change would never load (#174). Always
# recreate Caddy after `up`; `--wait` keeps its health check part of verification.
up_all() {
  "${COMPOSE[@]}" pull &&
    "${COMPOSE[@]}" up -d --no-build --remove-orphans --wait &&
    "${COMPOSE[@]}" up -d --no-build --force-recreate --no-deps --wait caddy
}

# Step 6: keep the 3 newest local app image tags, and always the one just deployed. A
# cleanup failure (for example an image still used by a stopped container) never fails a
# verified deploy.
prune_app_images() {
  docker image ls "$APP_IMAGE_REPO" --format '{{.Tag}}' |
    grep -v -x -e '<none>' -e "$SHA" |
    tail -n +3 |
    while read -r tag; do
      docker image rm "$APP_IMAGE_REPO:$tag" >/dev/null || true
    done || true
}

# A string field of a `/health` body, or `unknown`: health_field <name> <json>.
health_field() {
  local value
  value=$(printf '%s' "$2" | sed -nE "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/p")
  echo "${value:-unknown}"
}

# Print the modes `agents` reports on `/health`, not what infra/.env says (#445). Names only,
# never the whole body. Like the image cleanup, a failed read never fails a verified deploy.
report_active_modes() {
  local body
  body=$("${COMPOSE[@]}" exec -T agents python -c \
    "import sys,urllib.request; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8003/health', timeout=3).read().decode())" \
    2>/dev/null) || body=""
  echo "Active modes: model_mode=$(health_field model_mode "$body") token_source=$(health_field token_source "$body")"
}

echo "Deploying $SHA (last green: ${LAST_GREEN:-none})"
# Steps 4 and 5. `up --wait` is part of verification: an unhealthy service must roll back,
# not exit under set -e. `make seed` is an idempotent upsert; a seed failure rolls back (§9).
if up_all && make seed && make smoke && make scenario; then
  echo "$SHA" > .last_green
  prune_app_images
  echo "Deploy $SHA verified."
  # Step 6a: when infra/.env holds MODEL_MODE=real (demo week, evals §9), recreate `agents`
  # from it so the VM stays in real mode after a deploy. If that doesn't come up, put
  # `agents` back in stub mode (MODEL_MODE=stub is still exported here) and fail, so the
  # owner sees it. The image itself is verified, so `.last_green` stands and nothing rolls
  # back.
  if [ "$(env_model_mode)" = "real" ]; then
    if ! infra/eval-mode.sh restore; then
      echo "WARNING: agents did not come back in real mode; it is back in stub mode."
      "${COMPOSE[@]}" up -d --no-build --no-deps --force-recreate --wait agents || true
      exit 1
    fi
    echo "agents restored in real mode from infra/.env."
  fi
  report_active_modes
  exit 0
fi

# Step 7: roll back by image tag only. The checkout, the Keycloak image (KEYCLOAK_IMAGE,
# identity-authz I42) and everything else stay as they are.
echo "Verification failed for $SHA."
if [ -n "$LAST_GREEN" ] && [ "$LAST_GREEN" != "$SHA" ]; then
  echo "Rolling back to image tag $LAST_GREEN"
  export APP_TAG="$LAST_GREEN"
  if ! { "${COMPOSE[@]}" up -d --no-build --wait && make smoke; }; then
    echo "WARNING: rollback to $LAST_GREEN did not verify. Manual attention needed."
  fi
fi
exit 1
