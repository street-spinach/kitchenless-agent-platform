"""The consent scenarios I-CUJ 1 and I-CUJ 10 against a local stack (identity-authz I43).

Run with `make up` and then `make scenario-identity`, with the stack's settings in the
environment (`DATABASE_URL` and `KEYCLOAK_URL` pointing at the published ports). The browser
steps go through Caddy at `https://localhost`, as `docs/spikes/keycloak/flow.py` does. Each
run registers new people through `services/identity/onboarding.py`.
"""

import asyncio
import html
import json
import re
import secrets
import urllib.parse as up
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import requests
import urllib3
from psycopg.rows import dict_row

from services.common import ids
from services.common.config import get_settings
from services.identity import fga, grants, links, onboarding

# Caddy's internal CA signs https://localhost (as in tests/smoke/test_health.py).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://localhost"
ADDRESS = {
    "line1": "300 Main Street",
    "city": "Cincinnati",
    "region": "OH",
    "postal_code": "45202",
    "country": "US",
}
SPEND_SCOPES = {"orders:place"}


def _dsn() -> str:
    dsn = get_settings().database_url
    assert dsn, "DATABASE_URL is not set"
    return dsn


def _run[T](fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=False) as conn:
            result = await fn(conn)
            await conn.commit()
            return result

    return asyncio.run(go())


def _rows(sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(_dsn()) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _new_phone() -> str:
    # The 555-01xx range is reserved for fiction; a random tail keeps each run's people new.
    return f"+1555{secrets.randbelow(10**7):07d}"


def _new_admin(name: str) -> tuple[uuid.UUID, str]:
    user_id = _run(
        lambda conn: onboarding.create_person(conn, _new_phone(), name, None, "admin", ADDRESS)
    )
    [row] = _rows("SELECT household_id FROM household_members WHERE user_id = %s", user_id)
    return user_id, str(row["household_id"])


def _keycloak_sub(user_id: uuid.UUID) -> str:
    [row] = _rows("SELECT keycloak_sub FROM users WHERE user_id = %s", user_id)
    return str(row["keycloak_sub"])


def _link(target: uuid.UUID, sent_to: uuid.UUID, household_id: str, agents: list[str]) -> str:
    url = _run(lambda conn: links.issue(conn, "consent", target, sent_to, household_id, agents))
    assert url.startswith(f"{BASE}/me/l/")
    return url


def _consent_form(page: requests.Response) -> tuple[str, dict[str, str]] | None:
    """Keycloak's consent screen: its form action and fields, with "Yes" chosen."""
    if 'name="accept"' not in page.text:
        return None
    form = re.search(r'<form[^>]+action="([^"]+)"', page.text)
    assert form, "consent page has no form"
    action = up.urljoin(page.url, html.unescape(form.group(1)))
    fields = dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', page.text))
    fields["accept"] = "Yes"
    return action, fields


def _consent(url: str) -> tuple[requests.Session, requests.Response, int]:
    """Tap the link, tap "Continue", then approve every consent screen. Returns the browser,
    the done page and how many consent screens were shown."""
    browser = requests.Session()
    browser.verify = False
    page = browser.get(url)
    assert page.status_code == 200 and "Continue" in page.text  # GET never uses the link
    resp = browser.post(url, allow_redirects=False)
    screens = 0
    for _ in range(30):
        if resp.is_redirect:
            nxt = up.urljoin(resp.url, resp.headers["Location"])
            resp = browser.get(nxt, allow_redirects=False)
            continue
        form = _consent_form(resp)
        if form is None:
            break
        screens += 1
        assert "password" not in resp.text.lower(), "a consent screen asked for a new sign-in"
        resp = browser.post(form[0], data=form[1], allow_redirects=False)
    assert up.urlparse(resp.url).path == "/me/consent/done", resp.text[:500]
    assert resp.status_code == 200
    return browser, resp, screens


def _sample_claims(done: requests.Response) -> dict[str, Any]:
    shown = re.search(r"<pre>(.*?)</pre>", done.text, re.S)
    assert shown, "the done page shows no sample token"
    return json.loads(html.unescape(shown.group(1)))


def _grants(user_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(
        "SELECT grant_id, agent_id, status, scopes, consented_by, refresh_ciphertext,"
        " expires_at - created_at AS lifetime FROM agent_grants WHERE user_id = %s"
        " ORDER BY agent_id",
        user_id,
    )


def _consent_audits(user_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(
        "SELECT actor_user_id, agent_id FROM audit_events"
        " WHERE kind = 'consent_granted' AND subject_user_id = %s ORDER BY agent_id",
        user_id,
    )


def _refused_again(url: str) -> None:
    again = requests.post(url, verify=False, allow_redirects=False)
    assert again.status_code == 410 and "This link has expired" in again.text


def test_admin_consents_two_agents() -> None:
    asha, hid = _new_admin("Asha")
    household = f"household:{hid}"
    consumer = ids.onboarded_consumer_agent_id(hid, 1)
    url = _link(asha, asha, hid, [household, consumer])

    _, done, screens = _consent(url)

    # Two consent screens, one sign-in (I-CUJ 1, steps 4 and 5).
    assert screens == 2
    rows = _grants(asha)
    assert [r["agent_id"] for r in rows] == sorted([household, consumer])
    for r in rows:
        assert r["status"] == "active" and r["consented_by"] == asha
        assert r["refresh_ciphertext"]
        assert r["lifetime"].days == grants.GRANT_DAYS
    assert [a["agent_id"] for a in _consent_audits(asha)] == sorted([household, consumer])
    # The done page: `sub` = Asha, `act.client_id` = the first agent (demo moment 1).
    claims = _sample_claims(done)
    assert claims["sub"] == _keycloak_sub(asha)
    assert claims["act"]["client_id"] == ids.keycloak_client_id(household)
    # A second use shows "This link has expired" and grants nothing more.
    _refused_again(url)
    assert len(_grants(asha)) == 2


def test_child_consent_by_guardian() -> None:
    asha, hid = _new_admin("Asha")
    mia = _run(lambda conn: onboarding.create_child(conn, asha, hid, "Mia"))
    agent = ids.onboarded_consumer_agent_id(hid, 2)
    # Mia has no phone; her link is sent to Asha, her guardian.
    assert _rows("SELECT 1 FROM channel_identities WHERE user_id = %s", mia) == []
    url = _link(mia, asha, hid, [agent])

    _, done, screens = _consent(url)

    assert screens == 1
    [grant] = _grants(mia)
    assert (grant["agent_id"], grant["status"], grant["consented_by"]) == (agent, "active", asha)
    # The link signed in as Mia: the grant's tokens carry `sub` = Mia (I-D15).
    claims = _sample_claims(done)
    assert claims["sub"] == _keycloak_sub(mia)
    assert claims["act"]["client_id"] == ids.keycloak_client_id(agent)
    # The audit row names Asha as the person who consented.
    [audit] = _consent_audits(mia)
    assert (audit["actor_user_id"], audit["agent_id"]) == (asha, agent)
    # `authorize_spend` is refused: no `can_spend` relation and no spending scope.
    client = f"agent:{ids.keycloak_client_id(agent)}"
    allowed, _ = fga.check(client, "can_spend", f"budget:{hid}")
    assert not allowed
    assert not SPEND_SCOPES & set(grant["scopes"])
    assert not SPEND_SCOPES & set(claims.get("scope", "").split())
    _refused_again(url)
