#!/usr/bin/env bash
# kcadm in the tools container (see compose.yml), talking to keycloak:8080.
exec docker compose -f "$(dirname "$0")/compose.yml" run --rm -T kcadm "$@" --config /cfg/kcadm.config
