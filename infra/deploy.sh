#!/usr/bin/env bash
# Deploy the checked-out SHA, verify it, and roll back to the last green SHA on failure.
set -euo pipefail
SHA="$1"
cd /opt/app
COMPOSE=(docker compose -f infra/docker-compose.yml --env-file infra/.env)
LAST_GREEN=$(cat .last_green 2>/dev/null || echo "")

# Caddy reads infra/Caddyfile through a single-file bind mount. `git checkout` replaces the
# file, the running container keeps the old one, and `up` only recreates Caddy when its
# service definition changes, so a Caddyfile-only change would never load (#174). Always
# recreate Caddy after `up`; `--wait` keeps its health check part of verification.
up_all() {
  "${COMPOSE[@]}" up -d --build --remove-orphans --wait &&
    "${COMPOSE[@]}" up -d --force-recreate --no-deps --wait caddy
}

echo "Deploying $SHA (last green: ${LAST_GREEN:-none})"
# `up --wait` is part of verification: an unhealthy service must roll back, not exit under set -e.
# `make seed` is an idempotent upsert (architecture §11 step 5); a seed failure rolls back (§9).
if up_all && make seed && make smoke && make scenario; then
  echo "$SHA" > .last_green
  docker image prune -f >/dev/null
  echo "Deploy $SHA verified."
  exit 0
fi

echo "Verification failed for $SHA."
if [ -n "$LAST_GREEN" ] && [ "$LAST_GREEN" != "$SHA" ]; then
  echo "Rolling back to $LAST_GREEN"
  git checkout --quiet --force "$LAST_GREEN"
  if ! { up_all && make smoke; }; then
    echo "WARNING: rollback to $LAST_GREEN did not verify. Manual attention needed."
  fi
fi
exit 1
