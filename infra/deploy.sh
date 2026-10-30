#!/usr/bin/env bash
# Deploy the checked-out SHA, verify it, and roll back to the last green SHA on failure.
set -euo pipefail
SHA="$1"
cd /opt/app
COMPOSE=(docker compose -f infra/docker-compose.yml --env-file infra/.env)
LAST_GREEN=$(cat .last_green 2>/dev/null || echo "")

echo "Deploying $SHA (last green: ${LAST_GREEN:-none})"
# `up --wait` is part of verification: an unhealthy service must roll back, not exit under set -e.
if "${COMPOSE[@]}" up -d --build --remove-orphans --wait && make smoke && make scenario; then
  echo "$SHA" > .last_green
  docker image prune -f >/dev/null
  echo "Deploy $SHA verified."
  exit 0
fi

echo "Verification failed for $SHA."
if [ -n "$LAST_GREEN" ] && [ "$LAST_GREEN" != "$SHA" ]; then
  echo "Rolling back to $LAST_GREEN"
  git checkout --quiet --force "$LAST_GREEN"
  if ! { "${COMPOSE[@]}" up -d --build --remove-orphans --wait && make smoke; }; then
    echo "WARNING: rollback to $LAST_GREEN did not verify. Manual attention needed."
  fi
fi
exit 1
