"""The `/me` pages (identity-authz §6, §9, §11 and I24). Keycloak is faked at the
`keycloak_admin` functions the routes call."""

import base64
import os
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import psycopg
import pytest
from starlette.testclient import TestClient

from services.api import main as api
from services.api.routes import me
from services.identity import grants, keycloak_admin, links
from tests.unit import api_world as aw
from tests.unit import identity_world as w

db = pytest.mark.db

HOUSEHOLD = "h-me"
DOMAIN = "kitchenless.test"
ISSUER = f"https://{DOMAIN}/auth/realms/kitchenless"
HOUSEHOLD_AGENT = f"household:{HOUSEHOLD}"
ADMIN_AGENT = f"consumer:{HOUSEHOLD}-a"
MEMBER_AGENT = f"consumer:{HOUSEHOLD}-m"
CHILD_AGENT = f"consumer:{HOUSEHOLD}-c"


@dataclass(frozen=True)
class _Settings:
    domain: str = DOMAIN
    issuer_url: str = ISSUER
    session_secret: str = "test-session-secret"
    grant_encryption_key: str = base64.b64encode(os.urandom(32)).decode()
    grant_key_version: int = 1
    token_source: str = "mock"


@dataclass
class FakeKeycloak:
    """What `routes/me.py` and `grants.after_revoke` call in Keycloak."""

    id_sub: str | None = None  # the `sub` of the next me-pages ID token
    magic: list[dict[str, Any]] = field(default_factory=list)
    redeemed: list[tuple[str, str]] = field(default_factory=list)
    revoked: list[tuple[str, str]] = field(default_factory=list)
    revoke_fails: bool = False

    async def username(self, keycloak_user_id: str) -> str:
        return f"user-{keycloak_user_id}"

    async def magic_link(self, username: str, redirect_uri: str, state: str) -> str:
        self.magic.append({"username": username, "redirect_uri": redirect_uri, "state": state})
        return f"https://kc.test/magic/{len(self.magic)}"

    async def redeem_code(self, client_id: str, code: str, redirect_uri: str) -> dict[str, Any]:
        assert redirect_uri == f"https://{DOMAIN}/me/callback"
        self.redeemed.append((client_id, code))
        if client_id == "me-pages":
            claims = {"sub": self.id_sub, "aud": "me-pages", "iss": ISSUER}
            return {"id_token": jwt.encode(claims, "x" * 32, algorithm="HS256")}
        return {"access_token": "at", "refresh_token": f"offline-{client_id}-{code}"}

    async def revoke_consent(self, keycloak_user_id: str, client_id: str) -> None:
        if self.revoke_fails:
            raise keycloak_admin.KeycloakError("boom")
        self.revoked.append((keycloak_user_id, client_id))


@pytest.fixture
def kc(monkeypatch: pytest.MonkeyPatch) -> FakeKeycloak:
    return make_kc(monkeypatch)


def make_kc(monkeypatch: pytest.MonkeyPatch) -> FakeKeycloak:
    """The fake Keycloak and test settings (shared with `test_me_household.py`)."""
    settings = _Settings()
    for module in (me, links, grants):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    aw.patch_settings(monkeypatch)
    me._spent_states.clear()
    fake = FakeKeycloak()
    for name in ("username", "magic_link", "redeem_code", "revoke_consent"):
        monkeypatch.setattr(keycloak_admin, name, getattr(fake, name))
    return fake


@dataclass
class World:
    pg: str
    admin: w.Person
    member: w.Person
    child: w.Person
    other_admin: w.Person


@pytest.fixture
def world(pg: str, kc: FakeKeycloak) -> World:
    return make_world(pg, kc)


def make_world(pg: str, kc: FakeKeycloak) -> World:
    """The `/me` test household (shared with `test_me_household.py`)."""
    with psycopg.connect(pg) as conn:
        w.add_household(conn, HOUSEHOLD, env="eval")
        admin = w.add_person(conn, HOUSEHOLD, "admin")
        member = w.add_person(conn, HOUSEHOLD, "member")
        child = w.add_person(conn, HOUSEHOLD, "child")
        other_admin = w.add_person(conn, "h-other", "admin")
        conn.execute(
            "INSERT INTO approval_rules (household_id, approver_user_id) VALUES (%s, %s)",
            (HOUSEHOLD, admin.user_id),
        )
        w.add_agent(conn, HOUSEHOLD_AGENT, HOUSEHOLD_AGENT)
        for agent, person in (
            (ADMIN_AGENT, admin),
            (MEMBER_AGENT, member),
            (CHILD_AGENT, child),
        ):
            w.add_agent(conn, agent, f"user:{person.user_id}")
        # The WhatsApp channel's card: the revoke notice is written to it (EX-I19).
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
            " VALUES ('channel:whatsapp', 'channel', 'system', 'us', 'api')"
        )
    kc.id_sub = admin.keycloak_sub
    return World(pg, admin, member, child, other_admin)


def _client(pg: str) -> TestClient:
    # https, so the Secure session cookie is sent back.
    return TestClient(api.create_app(aw.connect_to(pg)), base_url="https://testserver")


def _issue(world: World, agents: list[str], purpose: str = "consent") -> str:
    url = aw.run(
        world.pg,
        lambda conn: links.issue(
            conn, purpose, world.admin.user_id, world.admin.user_id, HOUSEHOLD, agents
        ),
    )
    return urlsplit(url).path  # /me/l/<token>


def _query(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def _grants(pg: str) -> list[dict[str, Any]]:
    return aw.query(pg, "SELECT * FROM agent_grants ORDER BY created_at")


def _sign_in(world: World, kc: FakeKeycloak, client: TestClient, agents: list[str]) -> Any:
    """Tap the link, POST it, and come back from the magic link. Returns the response."""
    path = _issue(world, agents)
    assert client.post(path, follow_redirects=False).status_code == 303
    state = kc.magic[-1]["state"]
    return client.get("/me/callback", params={"code": "c0", "state": state}, follow_redirects=False)


def _session_id(person: w.Person) -> str:
    return f"session-{person.user_id}"


def _session_cookie(person: w.Person, household_id: str = HOUSEHOLD) -> dict[str, str]:
    value = me._sign(
        "me-session", {"u": str(person.user_id), "h": household_id, "i": _session_id(person)}
    )
    return {me.SESSION_COOKIE: value}


def _csrf(person: w.Person, form: str, household_id: str = HOUSEHOLD) -> str:
    """The CSRF token the page gives this person for `form` (experience §8)."""
    return me.csrf_token(me.Session(person.user_id, household_id, _session_id(person)), form)


@db
def test_link_get_and_head_do_not_consume(world: World, kc: FakeKeycloak) -> None:
    path = _issue(world, [HOUSEHOLD_AGENT])
    client = _client(world.pg)
    for _ in range(2):
        page = client.get(path)
        assert page.status_code == 200 and "method='post'" in page.text
        assert client.head(path).status_code == 200
    [row] = aw.query(world.pg, "SELECT used_at FROM consent_links")
    assert row["used_at"] is None
    assert kc.magic == []
    # The POST uses it, once.
    resp = client.post(path, follow_redirects=False)
    assert (resp.status_code, resp.headers["location"]) == (303, "https://kc.test/magic/1")
    assert kc.magic[0]["username"] == f"user-{world.admin.keycloak_sub}"
    assert kc.magic[0]["redirect_uri"] == f"https://{DOMAIN}/me/callback"
    assert client.post(path, follow_redirects=False).status_code == 410
    assert len(kc.magic) == 1
    kinds = [
        r["kind"] for r in aw.query(world.pg, "SELECT kind FROM audit_events ORDER BY event_id")
    ]
    assert kinds == ["link_issued", "link_used", "link_rejected"]


@db
def test_link_next_redirects(world: World, kc: FakeKeycloak) -> None:
    """A `me` link with `next` opens that page after sign-in; an edited `next` outside the
    closed set opens `/me/agents` (experience EX-A15, amended for #299)."""
    for sent, expect in (
        ("pantry", "/me/pantry"),
        ("history", "/me/history"),
        (None, "/me/agents"),
        ("evil", "/me/agents"),
    ):
        url = aw.run(
            world.pg,
            lambda conn: links.issue(
                conn, "me", world.admin.user_id, world.admin.user_id, HOUSEHOLD, []
            ),
        )
        path = urlsplit(url).path
        if sent is not None:
            path = f"{path}?next={sent}"
        client = _client(world.pg)
        page = client.get(path)
        form_next = "" if sent in (None, "evil") else f"?next={sent}"
        assert f"action='{urlsplit(url).path}{form_next}'" in page.text
        assert client.post(path, follow_redirects=False).status_code == 303
        resp = client.get(
            "/me/callback",
            params={"code": "c0", "state": kc.magic[-1]["state"]},
            follow_redirects=False,
        )
        assert (resp.status_code, resp.headers["location"]) == (302, expect), sent
        assert me.SESSION_COOKIE in resp.cookies


@db
def test_consent_happy_path_two_agents(world: World, kc: FakeKeycloak) -> None:
    client = _client(world.pg)
    resp = _sign_in(world, kc, client, [HOUSEHOLD_AGENT, ADMIN_AGENT])
    assert resp.status_code == 302
    assert me.SESSION_COOKIE in resp.cookies
    set_cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=lax" in set_cookie
    first = resp.headers["location"]
    assert first.startswith(f"{ISSUER}/protocol/openid-connect/auth?")
    assert _query(first)["client_id"] == "household.h-me"

    resp = client.get(
        "/me/callback",
        params={"code": "c1", "state": _query(first)["state"]},
        follow_redirects=False,
    )
    second = resp.headers["location"]
    assert _query(second)["client_id"] == "consumer.h-me-a"
    resp = client.get(
        "/me/callback",
        params={"code": "c2", "state": _query(second)["state"]},
        follow_redirects=False,
    )
    assert resp.status_code == 302 and resp.headers["location"].startswith("/me/consent/done?")

    rows = _grants(world.pg)
    assert [(g["agent_id"], g["status"]) for g in rows] == [
        (HOUSEHOLD_AGENT, "active"),
        (ADMIN_AGENT, "active"),
    ]
    for row in rows:
        assert (row["user_id"], row["consented_by"], row["household_id"]) == (
            world.admin.user_id,
            world.admin.user_id,
            HOUSEHOLD,
        )
        assert row["scopes"] == list(grants.CLASS_SCOPES[row["agent_kind"]])
        # The offline token is stored encrypted only.
        assert b"offline-" not in bytes(row["refresh_ciphertext"])
    assert kc.redeemed == [
        ("me-pages", "c0"),
        ("household.h-me", "c1"),
        ("consumer.h-me-a", "c2"),
    ]

    done = client.get(resp.headers["location"])
    assert done.status_code == 200
    # Plain words, no token or agent id (#436).
    assert "Your household agent and your personal agent can now act for you." in done.text
    assert world.admin.keycloak_sub not in done.text and "household.h-me" not in done.text
    assert "<pre>" not in done.text and "token" not in done.text.lower()
    assert "Skipped" not in done.text
    agents = client.get("/me/agents")
    assert agents.status_code == 200
    assert agents.text.count("Revoke") == 2
    # Plain names, not agent ids (#432); the ids stay only in the revoke form URLs.
    assert "Your household agent" in agents.text and "Your personal agent" in agents.text
    assert f"<strong>{ADMIN_AGENT}</strong>" not in agents.text


@db
def test_consent_second_agent_needs_no_login(world: World, kc: FakeKeycloak) -> None:
    client = _client(world.pg)
    resp = _sign_in(world, kc, client, [HOUSEHOLD_AGENT, ADMIN_AGENT])
    for code, client_id in (("c1", "household.h-me"), ("c2", "consumer.h-me-a")):
        query = _query(resp.headers["location"])
        assert query["client_id"] == client_id
        assert query["prompt"] == "consent"
        assert query["redirect_uri"] == f"https://{DOMAIN}/me/callback"
        assert query["scope"].split()[:3] == [
            "openid",
            "offline_access",
            f"delegation:client:{client_id}",
        ]
        resp = client.get(
            "/me/callback", params={"code": code, "state": query["state"]}, follow_redirects=False
        )
    # One magic link for the whole chain: the Keycloak session signs in every agent.
    assert len(kc.magic) == 1
    assert resp.headers["location"].startswith("/me/consent/done?")


@db
def test_callback_rejects_replayed_state(world: World, kc: FakeKeycloak) -> None:
    client = _client(world.pg)
    path = _issue(world, [HOUSEHOLD_AGENT, ADMIN_AGENT])
    client.post(path, follow_redirects=False)
    state = kc.magic[-1]["state"]
    first = client.get(
        "/me/callback", params={"code": "c0", "state": state}, follow_redirects=False
    )
    assert first.status_code == 302
    replay = client.get(
        "/me/callback", params={"code": "c9", "state": state}, follow_redirects=False
    )
    assert replay.status_code == 400
    agent_state = _query(first.headers["location"])["state"]
    ok = client.get(
        "/me/callback", params={"code": "c1", "state": agent_state}, follow_redirects=False
    )
    assert ok.status_code == 302
    again = client.get(
        "/me/callback", params={"code": "c1", "state": agent_state}, follow_redirects=False
    )
    assert again.status_code == 400
    # A forged or tampered state is refused too.
    bad = client.get("/me/callback", params={"code": "c1", "state": agent_state[:-2] + "xx"})
    assert bad.status_code == 400
    assert len(_grants(world.pg)) == 1


@db
def test_callback_rejects_other_person(world: World, kc: FakeKeycloak) -> None:
    kc.id_sub = world.member.keycloak_sub  # Keycloak signed in someone else
    client = _client(world.pg)
    resp = _sign_in(world, kc, client, [HOUSEHOLD_AGENT])
    assert resp.status_code == 403
    assert me.SESSION_COOKIE not in resp.cookies
    assert client.get("/me/agents").status_code == 401
    assert _grants(world.pg) == []
    # An agent callback without the target's session is refused as well.
    state = me._sign(
        "me-state",
        {
            "n": "n-1",
            "p": "consent",
            "t": str(world.admin.user_id),
            "s": str(world.admin.user_id),
            "h": HOUSEHOLD,
            "a": [HOUSEHOLD_AGENT],
            "i": 0,
            "g": [],
            "k": [],
        },
    )
    other = _client(world.pg)
    other.cookies.update(_session_cookie(world.member))
    resp = other.get("/me/callback", params={"code": "c1", "state": state}, follow_redirects=False)
    assert resp.status_code == 403
    assert _grants(world.pg) == []


@db
def test_decline_skips_agent(world: World, kc: FakeKeycloak) -> None:
    client = _client(world.pg)
    resp = _sign_in(world, kc, client, [HOUSEHOLD_AGENT, ADMIN_AGENT])
    state = _query(resp.headers["location"])["state"]
    resp = client.get(
        "/me/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    second = _query(resp.headers["location"])
    assert second["client_id"] == "consumer.h-me-a"
    resp = client.get(
        "/me/callback", params={"code": "c2", "state": second["state"]}, follow_redirects=False
    )
    assert [g["agent_id"] for g in _grants(world.pg)] == [ADMIN_AGENT]
    done = client.get(resp.headers["location"])
    assert done.status_code == 200
    assert "Skipped: Your household agent." in done.text
    assert HOUSEHOLD_AGENT not in done.text
    assert ("household.h-me", "c1") not in kc.redeemed


@db
def test_revoke_permissions(world: World, kc: FakeKeycloak) -> None:
    with psycopg.connect(world.pg) as conn:
        household = w.add_grant(conn, HOUSEHOLD_AGENT, world.admin, HOUSEHOLD)
        member = w.add_grant(conn, MEMBER_AGENT, world.member, HOUSEHOLD)
        child = w.add_grant(
            conn, CHILD_AGENT, world.child, HOUSEHOLD, consented_by=world.admin.user_id
        )

    def revoke(person: w.Person, agent_id: str, household_id: str = HOUSEHOLD) -> int:
        client = _client(world.pg)
        client.cookies.update(_session_cookie(person, household_id))
        return client.post(
            f"/me/agents/{agent_id}/revoke",
            data={"csrf": _csrf(person, "revoke", household_id)},
            follow_redirects=False,
        ).status_code

    def status(grant_id: uuid.UUID) -> tuple[str, Any, Any]:
        [row] = aw.query(world.pg, "SELECT * FROM agent_grants WHERE grant_id = %s", grant_id)
        return row["status"], row["revoked_by"], row["revoke_reason"]

    # Signed out, or a member acting on someone else's agent: refused.
    assert _client(world.pg).post(f"/me/agents/{MEMBER_AGENT}/revoke").status_code == 401
    assert revoke(world.member, HOUSEHOLD_AGENT) == 403
    assert revoke(world.member, CHILD_AGENT) == 403
    assert revoke(world.child, MEMBER_AGENT) == 403
    # Another household's admin sees nothing of this household.
    assert revoke(world.other_admin, HOUSEHOLD_AGENT, "h-other") == 404
    assert {status(g)[0] for g in (household, member, child)} == {"active"}
    assert kc.revoked == []

    # The grant holder.
    assert revoke(world.member, MEMBER_AGENT) == 303
    assert status(member) == ("revoked", world.member.user_id, "user")
    assert kc.revoked == [(world.member.keycloak_sub, "consumer.h-me-m")]
    assert revoke(world.member, MEMBER_AGENT) == 404  # nothing active left
    # A guardian, for a child's agent.
    assert revoke(world.admin, CHILD_AGENT) == 303
    assert status(child) == ("revoked", world.admin.user_id, "user")
    assert kc.revoked[-1] == (world.child.keycloak_sub, "consumer.h-me-c")
    # Any admin, for the household agent; Keycloak's failure is audited, not fatal.
    kc.revoke_fails = True
    assert revoke(world.admin, HOUSEHOLD_AGENT) == 303
    assert status(household) == ("revoked", world.admin.user_id, "user")
    events = aw.query(world.pg, "SELECT kind, grant_id, detail FROM audit_events ORDER BY event_id")
    assert [(e["kind"], e["grant_id"]) for e in events] == [
        ("consent_revoked", member),
        ("consent_revoked", child),
        ("consent_revoked", household),
        ("keycloak_revoke_failed", household),
    ]
    assert events[-1]["detail"] == {"error": "KeycloakError"}


@db
def test_band_admin_only(world: World, kc: FakeKeycloak) -> None:
    admin, member = _client(world.pg), _client(world.pg)
    admin.cookies.update(_session_cookie(world.admin))
    member.cookies.update(_session_cookie(world.member))
    admin.headers[me.CSRF_HEADER] = _csrf(world.admin, "band")
    member.headers[me.CSRF_HEADER] = _csrf(world.member, "band")

    assert _client(world.pg).get("/me/household/band").status_code == 401
    assert member.get("/me/household/band").json() == {
        "household_id": HOUSEHOLD,
        "per_order_minor": None,
    }
    assert member.post("/me/household/band", json={"per_order_minor": 10000}).status_code == 403
    for bad in ({"per_order_minor": -1}, {"per_order_minor": "100"}, {"per_order_minor": True}, {}):
        assert admin.post("/me/household/band", json=bad).status_code == 400
    resp = admin.post("/me/household/band", json={"per_order_minor": 10000})
    assert resp.status_code == 200 and resp.json()["per_order_minor"] == 10000
    assert member.get("/me/household/band").json()["per_order_minor"] == 10000
    assert admin.post("/me/household/band", json={"per_order_minor": None}).status_code == 200
    events = aw.query(
        world.pg,
        "SELECT kind, actor_user_id, household_id, detail FROM audit_events ORDER BY event_id",
    )
    assert events == [
        {
            "kind": "band_changed",
            "actor_user_id": world.admin.user_id,
            "household_id": HOUSEHOLD,
            "detail": {"from": None, "to": 10000},
        },
        {
            "kind": "band_changed",
            "actor_user_id": world.admin.user_id,
            "household_id": HOUSEHOLD,
            "detail": {"from": 10000, "to": None},
        },
    ]
    # The history page shows the person's own audit rows.
    history = admin.get("/me/history")
    # In plain words (#432): no internal kind names.
    assert history.status_code == 200
    assert history.text.count("The approval limit changed") == 2
    assert "band_changed" not in history.text
    assert member.get("/me/history").text.count("The approval limit changed") == 0


def test_event_text_is_plain() -> None:
    """#432: every audit kind has plain words, and agent names are never ids."""
    from services.api.routes import me
    from services.identity import audit

    assert set(me._EVENT_TEXT) == set(audit.KINDS)
    granted = me._event_text("consent_granted", "consumer")
    assert granted == "You let your personal agent act for you"
    expired = me._event_text("grant_expired", "household")
    assert expired == "Your household agent's permission ran out"
    assert me._event_text("consent_revoked", None) == "An agent can no longer act for you"
    assert me._event_text("something_new", None) == "Account activity"
    for kind in audit.KINDS:
        text = me._event_text(kind, "consumer")
        assert "_" not in text and ":" not in text
