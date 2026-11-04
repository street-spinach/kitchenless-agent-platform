#!/usr/bin/env bash
# Apply the realm file's theme, scope and URI settings to an existing `kitchenless` realm
# (showcase.md §3.3 "Deploying the theme", SC-I8). The realm import is skipped when the realm
# already exists, so this is the operator step after a realm-file change.
#
#   cd /opt/app && infra/keycloak-sync.sh
#
# It applies, through kcadm.sh inside the keycloak container:
#   - the realm's loginTheme;
#   - every client scope in services/identity/realm/kitchenless.json: a missing scope is
#     created from the file; an existing one gets the file's attributes (consent texts);
#   - https://<DOMAIN> redirect URIs and web origins: for every client whose realm-file
#     redirectUris/webOrigins contain ${DOMAIN}, and for every client copied from a template
#     (consumer.*, household.*), which gets its template's. URIs are only ever added.
# Idempotent: a second run prints only "no change" lines. It never prints the admin
# credentials and never removes a URI. Needs python3 on the host (no jq).
#
# Test points: APP_DIR (default /opt/app) and DOCKER (default `docker`).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/app}"
DOCKER="${DOCKER:-docker}"
REALM=kitchenless
REALM_FILE=services/identity/realm/kitchenless.json
ENV_FILE=infra/.env

cd "$APP_DIR"

# A value from infra/.env, without surrounding quotes (empty when missing).
env_value() {
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

DOMAIN="$(env_value DOMAIN)"
if [ -z "$DOMAIN" ] || [ "$DOMAIN" = "localhost" ]; then
  echo "DOMAIN in $ENV_FILE is empty or localhost. Not syncing." >&2
  exit 1
fi

# kcadm.sh in the keycloak container. Callers give stdin explicitly (a file or /dev/null):
# exec reads stdin, so an unredirected call inside a `while read` loop would eat the loop's.
kc() {
  "$DOCKER" compose -f infra/docker-compose.yml --env-file "$ENV_FILE" exec -T keycloak \
    /opt/keycloak/bin/kcadm.sh "$@"
}

# Log in. The credentials go only to kcadm; its output names the user, so it is dropped.
KC_USER="$(env_value KEYCLOAK_ADMIN)"
KC_PASS="$(env_value KEYCLOAK_ADMIN_PASSWORD)"
if [ -z "$KC_USER" ] || [ -z "$KC_PASS" ]; then
  unset KC_USER KC_PASS
  echo "KEYCLOAK_ADMIN or KEYCLOAK_ADMIN_PASSWORD is not set in $ENV_FILE. Not syncing." >&2
  exit 1
fi
if ! kc config credentials --server http://localhost:8080/auth --realm master \
  --user "$KC_USER" --password "$KC_PASS" </dev/null >/dev/null 2>&1; then
  unset KC_USER KC_PASS
  echo "kcadm login to master failed. Not syncing." >&2
  exit 1
fi
unset KC_USER KC_PASS

WORK="$(mktemp -d)"
trap 'rm -r -f "$WORK"' EXIT

kc get "realms/$REALM" --fields loginTheme </dev/null >"$WORK/realm.json"
kc get client-scopes -r "$REALM" </dev/null >"$WORK/scopes.json"
# One request for every client: the default page would stop at 100.
kc get clients -r "$REALM" --offset 0 --limit 5000 --fields id,clientId,redirectUris,webOrigins \
  </dev/null >"$WORK/clients.json"

# Plan the changes. Each plan line is tab-separated: action, target, body file, message.
# A body file holds the JSON to send; `-` means none.
DOMAIN="$DOMAIN" python3 - "$REALM_FILE" "$WORK" >"$WORK/plan.tsv" <<'PY'
import json
import os
import sys

realm_file, work = sys.argv[1], sys.argv[2]
domain = os.environ["DOMAIN"]
with open(realm_file) as f:
    want = json.load(f)


def load(name):
    with open(os.path.join(work, name)) as f:
        return json.load(f)


n = 0


def body(obj):
    global n
    n += 1
    path = os.path.join(work, f"body{n}.json")
    with open(path, "w") as f:
        json.dump(obj, f)
    return path


def plan(action, target, path, message):
    print("\t".join([action, target, path, message]))


# loginTheme.
theme = want["loginTheme"]
if load("realm.json").get("loginTheme") == theme:
    plan("note", "-", "-", f"realm {want['realm']}: loginTheme no change")
else:
    plan("realm", "-", body({"loginTheme": theme}), f"realm {want['realm']}: loginTheme set to {theme}")

# Client scopes.
live_scopes = {s["name"]: s for s in load("scopes.json")}
for scope in want.get("clientScopes", []):
    name = scope["name"]
    live = live_scopes.get(name)
    if live is None:
        rep = {k: v for k, v in scope.items() if k != "id"}
        plan("create-scope", "-", body(rep), f"scope {name}: created")
        continue
    attrs = dict(live.get("attributes") or {})
    changed = sorted(k for k, v in (scope.get("attributes") or {}).items() if attrs.get(k) != v)
    if not changed:
        plan("note", "-", "-", f"scope {name}: no change")
        continue
    attrs.update(scope["attributes"])
    rep = {"id": live["id"], "name": live["name"], "attributes": attrs}
    if live.get("protocol"):
        rep["protocol"] = live["protocol"]
    plan("update-scope", live["id"], body(rep), f"scope {name}: updated {', '.join(changed)}")

# Redirect URIs and web origins.
def domain_uris(client):
    return {
        field: [u.replace("${DOMAIN}", domain) for u in client.get(field) or [] if "${DOMAIN}" in u]
        for field in ("redirectUris", "webOrigins")
    }


file_clients = {c["clientId"]: c for c in want.get("clients", [])}
templates = {
    prefix: domain_uris(file_clients[f"template.{prefix}"])
    for prefix in ("consumer", "household")
    if f"template.{prefix}" in file_clients
}
targets = {}
for client_id, client in file_clients.items():
    uris = domain_uris(client)
    if uris["redirectUris"] or uris["webOrigins"]:
        targets[client_id] = uris
live_clients = {c["clientId"]: c for c in load("clients.json")}
for client_id in live_clients:
    prefix = client_id.split(".", 1)[0]
    if "." in client_id and prefix in templates and client_id not in targets:
        targets[client_id] = templates[prefix]

for client_id in sorted(targets):
    live = live_clients.get(client_id)
    if live is None:
        plan("note", "-", "-", f"client {client_id}: not in the realm, skipped")
        continue
    rep = {"id": live["id"], "clientId": client_id}
    added = []
    for field, label in (("redirectUris", "redirect URI"), ("webOrigins", "web origin")):
        have = list(live.get(field) or [])
        new = [u for u in targets[client_id][field] if u not in have]
        if new:
            rep[field] = have + new
            added.append(f"{len(new)} {label}{'' if len(new) == 1 else 's'}")
    if added:
        plan("update-client", live["id"], body(rep), f"client {client_id}: added {', '.join(added)}")
    else:
        plan("note", "-", "-", f"client {client_id}: no change")
PY

# Apply the plan. The loop reads fd 3, and every kcadm call has its own stdin.
while IFS=$'\t' read -r action target file message <&3; do
  case "$action" in
    note) ;;
    realm) kc update "realms/$REALM" -f - <"$file" >/dev/null ;;
    create-scope) kc create client-scopes -r "$REALM" -f - <"$file" >/dev/null ;;
    update-scope) kc update "client-scopes/$target" -r "$REALM" -f - <"$file" >/dev/null ;;
    update-client) kc update "clients/$target" -r "$REALM" -f - <"$file" >/dev/null ;;
    *)
      echo "unknown plan action: $action" >&2
      exit 1
      ;;
  esac
  echo "$message"
done 3<"$WORK/plan.tsv"
