"""infra/keycloak-sync.sh against a fake kcadm.sh (showcase.md §3.3, SC-I8).

The script runs in a temporary APP_DIR holding a fake `infra/.env` and the real realm file,
with a fake `docker` on PATH. The fake plays `docker compose exec -T keycloak kcadm.sh` over a
JSON realm in `$STATE/kc.json` (the realm as first imported with DOMAIN=localhost, before the
theme), reads its stdin as exec does, pages `get clients` at 100 unless told otherwise, and
logs every change it makes.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra/keycloak-sync.sh"
REALM_FILE = REPO / "services/identity/realm/kitchenless.json"

ADMIN_USER = "kc-admin-7f3e"
ADMIN_PASSWORD = "s3cret-Pa55-9d1c"
OTHER_SECRET = "pg-secret-41ab"
DOMAIN = "demo.example.test"
EXTRA_CONSUMERS = 150

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys

state_path = os.path.join(os.environ["STATE"], "kc.json")
stdin = sys.stdin.read()  # exec attaches stdin
args = sys.argv[1:]
prefix = ["compose", "-f", "infra/docker-compose.yml", "--env-file", "infra/.env",
          "exec", "-T", "keycloak", "/opt/keycloak/bin/kcadm.sh"]
if args[:len(prefix)] != prefix:
    sys.exit("fake docker: unexpected call " + " ".join(args[:9]))
args = args[len(prefix):]
with open(state_path) as f:
    st = json.load(f)


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def save():
    with open(state_path, "w") as f:
        json.dump(st, f)


def change(what):
    st["changes"].append(what)
    save()


def body():
    if opt("-f") != "-":
        sys.exit("fake kcadm: expected -f -")
    return json.loads(stdin)


cmd, path = args[0], args[1]
if cmd == "config":
    sys.stderr.write(f"Logging into {opt('--server')} as user {opt('--user')} of realm master\n")
    if opt("--user") != os.environ["WANT_USER"] or opt("--password") != os.environ["WANT_PASSWORD"]:
        sys.stderr.write("Invalid user credentials\n")
        sys.exit(1)
    st["logged_in"] = True
    save()
    sys.exit(0)
if not st.get("logged_in"):
    sys.exit("fake kcadm: not logged in")
if path != "realms/kitchenless" and opt("-r") != "kitchenless":
    sys.exit("fake kcadm: wrong realm")

if (cmd, path) == ("get", "realms/kitchenless"):
    print(json.dumps({"loginTheme": st["loginTheme"]}))
elif (cmd, path) == ("update", "realms/kitchenless"):
    st.update(body())
    change("realm")
elif (cmd, path) == ("get", "client-scopes"):
    print(json.dumps(st["scopes"]))
elif (cmd, path) == ("create", "client-scopes"):
    rep = body()
    if any(s["name"] == rep["name"] for s in st["scopes"]):
        sys.exit("fake kcadm: Conflict")
    rep["id"] = "scope-new-" + rep["name"]
    st["scopes"].append(rep)
    sys.stderr.write(f"Created new client-scope with id '{rep['id']}'\n")
    change("create " + rep["name"])
elif cmd == "update" and path.startswith("client-scopes/"):
    rep = body()
    scope = next(s for s in st["scopes"] if s["id"] == path.split("/", 1)[1])
    scope.setdefault("attributes", {}).update(rep.get("attributes") or {})
    change("scope " + scope["name"])
elif (cmd, path) == ("get", "clients"):
    offset, limit = int(opt("--offset", "0")), int(opt("--limit", "100"))
    fields = opt("--fields")
    page = st["clients"][offset:offset + limit]
    if fields:
        page = [{k: c[k] for k in fields.split(",") if k in c} for c in page]
    print(json.dumps(page))
elif cmd == "update" and path.startswith("clients/"):
    rep = body()
    client = next(c for c in st["clients"] if c["id"] == path.split("/", 1)[1])
    if rep.get("clientId", client["clientId"]) != client["clientId"]:
        sys.exit("fake kcadm: clientId changed")
    for field in ("redirectUris", "webOrigins"):
        if field in rep:
            client[field] = rep[field]
    change("client " + client["clientId"])
else:
    sys.exit("fake kcadm: unexpected " + " ".join(args))
"""


def _localhost(uris: list[str]) -> list[str]:
    return [u.replace("${DOMAIN}", "localhost") for u in uris]


def _initial_realm() -> dict:
    """The realm as imported once with DOMAIN=localhost, before SC4's theme and texts."""
    realm = json.loads(REALM_FILE.read_text())
    scopes = []
    for i, scope in enumerate(realm["clientScopes"]):
        if scope["name"] == "approvals:request":
            continue  # added to the realm file after the import
        attrs = dict(scope.get("attributes") or {})
        if ":" in scope["name"]:
            attrs.pop("consent.screen.text", None)
        attrs["live.only"] = "kept"
        scopes.append({"id": f"scope-{i}", "name": scope["name"], "attributes": attrs})
    clients = []
    for i, client in enumerate(realm["clients"]):
        clients.append(
            {
                "id": f"client-{i}",
                "clientId": client["clientId"],
                "redirectUris": _localhost(client.get("redirectUris") or []),
                "webOrigins": _localhost(client.get("webOrigins") or []),
            }
        )
    # A URI added by hand stays.
    clients[0]["redirectUris"].append("http://127.0.0.1:9000/me/callback")
    # Onboarding copies the templates: more than one page of clients.
    for n in range(EXTRA_CONSUMERS):
        clients.append(
            {
                "id": f"copy-{n}",
                "clientId": f"consumer.us-9-{n:03d}",
                "redirectUris": ["https://localhost/me/callback"],
                "webOrigins": [],
            }
        )
    return {"loginTheme": "keycloak", "scopes": scopes, "clients": clients, "changes": []}


@pytest.fixture
def app(tmp_path: Path) -> Path:
    (tmp_path / "bin").mkdir()
    (tmp_path / "infra").mkdir()
    realm_dir = tmp_path / "services/identity/realm"
    realm_dir.mkdir(parents=True)
    shutil.copy(REALM_FILE, realm_dir / "kitchenless.json")
    docker = tmp_path / "bin/docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    (tmp_path / "kc.json").write_text(json.dumps(_initial_realm()))
    write_env(tmp_path, DOMAIN)
    return tmp_path


def write_env(app: Path, domain: str) -> None:
    (app / "infra" / ".env").write_text(
        f"POSTGRES_PASSWORD={OTHER_SECRET}\n"
        f"KEYCLOAK_ADMIN={ADMIN_USER}\n"
        f'KEYCLOAK_ADMIN_PASSWORD="{ADMIN_PASSWORD}"\n'
        f"DOMAIN={domain}\n"
    )


def sync(app: Path, password: str = ADMIN_PASSWORD) -> subprocess.CompletedProcess[str]:
    environ = {k: v for k, v in os.environ.items() if k not in ("DOMAIN", "KEYCLOAK_ADMIN")}
    environ.update(
        {
            "APP_DIR": str(app),
            "STATE": str(app),
            "PATH": f"{app / 'bin'}:{environ['PATH']}",
            "WANT_USER": ADMIN_USER,
            "WANT_PASSWORD": password,
        }
    )
    return subprocess.run(
        ["bash", str(SCRIPT)], env=environ, capture_output=True, text=True, timeout=60
    )


def realm_state(app: Path) -> dict:
    return json.loads((app / "kc.json").read_text())


def client(state: dict, client_id: str) -> dict:
    return next(c for c in state["clients"] if c["clientId"] == client_id)


def test_applies_each_setting(app: Path) -> None:
    done = sync(app)
    assert done.returncode == 0, done.stderr
    state = realm_state(app)
    want = json.loads(REALM_FILE.read_text())

    assert state["loginTheme"] == "kitchenless"
    assert "realm kitchenless: loginTheme set to kitchenless" in done.stdout

    # Every scope has the realm file's attributes; the missing one is created.
    live = {s["name"]: s for s in state["scopes"]}
    for scope in want["clientScopes"]:
        attrs = live[scope["name"]]["attributes"]
        assert {k: attrs.get(k) for k in scope["attributes"]} == scope["attributes"]
    assert live["approvals:request"]["attributes"]["consent.screen.text"] == (
        "Ask your approver before spending above the band"
    )
    assert "scope approvals:request: created" in done.stdout
    assert "scope pantry:read: updated consent.screen.text" in done.stdout
    assert live["pantry:read"]["attributes"]["live.only"] == "kept"

    # Every client with a ${DOMAIN} URI in the realm file, and every template copy, gets the
    # domain's URIs; nothing is removed.
    callback = f"https://{DOMAIN}/me/callback"
    for c in want["clients"]:
        got = client(state, c["clientId"])
        for field in ("redirectUris", "webOrigins"):
            expected = [u.replace("${DOMAIN}", DOMAIN) for u in c.get(field) or []]
            assert set(_localhost(c.get(field) or [])) <= set(got[field])
            assert set(expected) <= set(got[field])
    assert client(state, "household.us-1")["redirectUris"] == [
        "https://localhost/me/callback",
        "http://127.0.0.1:9000/me/callback",
        callback,
    ]
    assert client(state, "owner-app")["webOrigins"] == ["https://localhost", f"https://{DOMAIN}"]
    assert client(state, "grocery.kroger-us")["redirectUris"] == []
    assert "client grocery.kroger-us" not in done.stdout
    assert "client owner-app: added 1 redirect URI, 1 web origin" in done.stdout


def test_more_than_one_page_of_clients(app: Path) -> None:
    done = sync(app)
    assert done.returncode == 0, done.stderr
    state = realm_state(app)
    assert len(state["clients"]) > 100
    copies = [c for c in state["clients"] if c["clientId"].startswith("consumer.us-9-")]
    assert len(copies) == EXTRA_CONSUMERS
    for c in copies:
        assert c["redirectUris"] == [
            "https://localhost/me/callback",
            f"https://{DOMAIN}/me/callback",
        ]
    assert f"client consumer.us-9-{EXTRA_CONSUMERS - 1:03d}: added 1 redirect URI" in done.stdout


def test_second_run_changes_nothing(app: Path) -> None:
    assert sync(app).returncode == 0
    first = realm_state(app)
    done = sync(app)
    assert done.returncode == 0, done.stderr
    second = realm_state(app)
    assert second["changes"] == first["changes"]
    assert second == first
    lines = done.stdout.splitlines()
    assert lines and all(line.endswith("no change") for line in lines)
    # One line per scope and per synced client, plus the theme.
    want = json.loads(REALM_FILE.read_text())
    assert sum(line.startswith("scope ") for line in lines) == len(want["clientScopes"])
    assert "client consumer.us-9-000: no change" in lines


def test_prints_no_secret(app: Path) -> None:
    done = sync(app)
    assert done.returncode == 0, done.stderr
    out = done.stdout + done.stderr
    for secret in (ADMIN_USER, ADMIN_PASSWORD, OTHER_SECRET):
        assert secret not in out


def test_failed_login_prints_no_secret(app: Path) -> None:
    done = sync(app, password="something-else")
    assert done.returncode == 1
    assert "login to master failed" in done.stderr
    out = done.stdout + done.stderr
    for secret in (ADMIN_USER, ADMIN_PASSWORD, OTHER_SECRET):
        assert secret not in out
    assert realm_state(app)["changes"] == []


@pytest.mark.parametrize("domain", ["localhost", '"localhost"', ""])
def test_refuses_localhost_or_empty_domain(app: Path, domain: str) -> None:
    write_env(app, domain)
    done = sync(app)
    assert done.returncode == 1
    assert "Not syncing" in done.stderr
    state = realm_state(app)
    assert state["changes"] == []
    assert not state.get("logged_in")
