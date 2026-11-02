#!/usr/bin/env bash
# Configures the spike realm after import. Throwaway secrets, local only.
set -euo pipefail
cd "$(dirname "$0")"
k() { ./kc.sh "$@"; }
R=(-r kitchenless)
k config credentials --server http://keycloak:8080/auth --realm master --user spike-admin --password spike-only >/dev/null
k update authentication/required-actions/VERIFY_PROFILE "${R[@]}" -s enabled=false
k update realms/kitchenless -s adminPermissionsEnabled=true
k create clients "${R[@]}" -s clientId=kitchenless-mcp -s enabled=true -s publicClient=false -s standardFlowEnabled=false >/dev/null
k create clients "${R[@]}" -s clientId=household.us-1 -s enabled=true -s publicClient=false -s consentRequired=true \
  -s standardFlowEnabled=true -s serviceAccountsEnabled=true -s directAccessGrantsEnabled=false \
  -s secret=spike-household-secret -s 'redirectUris=["http://localhost:8180/agent/callback"]' \
  -s 'attributes."standard.token.exchange.enabled"=true' >/dev/null
k create clients "${R[@]}" -s clientId=me-pages -s enabled=true -s publicClient=false -s standardFlowEnabled=true \
  -s secret=spike-me-secret -s 'redirectUris=["http://localhost:8180/me/callback"]' >/dev/null
k create clients "${R[@]}" -s clientId=svc.onboarding -s enabled=true -s publicClient=false -s standardFlowEnabled=false \
  -s serviceAccountsEnabled=true -s secret=spike-onb-secret >/dev/null
k add-roles "${R[@]}" --uusername service-account-svc.onboarding --cclientid realm-management --rolename manage-users
H=$(k get clients "${R[@]}" -q clientId=household.us-1 --fields id --format csv --noquotes)
k create "clients/$H/protocol-mappers/models" "${R[@]}" -s name=aud-mcp -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper -s 'config."included.client.audience"=kitchenless-mcp' \
  -s 'config."access.token.claim"=true' >/dev/null
AP=$(k get clients "${R[@]}" -q clientId=admin-permissions --fields id --format csv --noquotes)
k create "clients/$AP/authz/resource-server/policy/client" "${R[@]}" -s name=agents-may-delegate \
  -s "clients=[\"$H\"]" -s logic=POSITIVE >/dev/null
P=$(k get "clients/$AP/authz/resource-server/policy" "${R[@]}" -q name=agents-may-delegate --fields id --format csv --noquotes)
k create "clients/$AP/authz/resource-server/permission/scope" "${R[@]}" -s name=agents-delegate-users \
  -s resourceType=Users -s 'scopes=["delegate"]' -s "policies=[\"$P\"]" >/dev/null
echo "household.us-1 uuid: $H; delegate policy $P"
