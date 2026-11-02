"""Spike flow (identity-authz I14): magic link -> me-pages, consent -> offline token,
refresh, delegation exchange. Local throwaway stack only; prints claims, never tokens."""

import base64
import json
import re
import sys
import urllib.parse as up

import requests

KC = "http://localhost:8180/auth/realms/kitchenless"
TOKEN = f"{KC}/protocol/openid-connect/token"
AGENT, AGENT_SECRET = "household.us-1", "spike-household-secret"
results = {}


def claims(jwt):
    p = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def show(label, c, keys=("iss", "sub", "azp", "aud", "scope", "act", "may_act", "typ")):
    print(f"--- {label}")
    for k in keys:
        if k in c:
            print(f"    {k}: {json.dumps(c[k])}")
    if "exp" in c and "iat" in c:
        print(f"    lifetime_s: {c['exp'] - c['iat']}")


def follow(s, url, stop_prefix):
    """Follow redirects by hand until one points at stop_prefix.

    Returns (final_url, last_response).
    """
    for _ in range(15):
        r = s.get(url, allow_redirects=False)
        loc = r.headers.get("Location")
        if not loc:
            return None, r
        loc = up.urljoin(url, loc)
        if loc.startswith(stop_prefix):
            return loc, r
        url = loc
    sys.exit("too many redirects")


def code_of(url):
    return up.parse_qs(up.urlparse(url).query)["code"][0]


class Browser(requests.Session):
    """Browsers treat http://localhost as a secure context and send Secure cookies there;
    requests does not, so clear the flag after every response (spike client only)."""

    def send(self, *a, **k):
        r = super().send(*a, **k)
        for c in self.cookies:
            c.secure = False
        return r


s = Browser()

# 1. Admin token for svc.onboarding (realm-management manage-users only).
adm = requests.post(
    TOKEN,
    data={
        "grant_type": "client_credentials",
        "client_id": "svc.onboarding",
        "client_secret": "spike-onb-secret",
    },
).json()["access_token"]

# 2. Q1: magic link for me-pages, by username, single use, 120 s.
ml = requests.post(
    f"{KC}/magic-link",
    headers={"Authorization": f"Bearer {adm}"},
    json={
        "username": "us-1.1",
        "client_id": "me-pages",
        "redirect_uri": "http://localhost:8180/me/callback",
        "expiration_seconds": 120,
        "reusable": False,
        "state": "signed-state-1",
        "scope": "openid",
    },
)
print("magic-link status", ml.status_code, "keys", sorted(ml.json()))
link = ml.json()["link"]
cb, _ = follow(s, link, "http://localhost:8180/me/callback")
results["Q1 magic link reaches me-pages callback with code"] = bool(cb and "code=" in cb)
print("me callback state:", up.parse_qs(up.urlparse(cb).query).get("state"))
me = requests.post(
    TOKEN,
    data={
        "grant_type": "authorization_code",
        "code": code_of(cb),
        "redirect_uri": "http://localhost:8180/me/callback",
        "client_id": "me-pages",
        "client_secret": "spike-me-secret",
    },
).json()
show("me-pages id_token", claims(me["id_token"]), ("sub", "aud", "azp", "preferred_username"))
results["Q1 me-pages sub is the target user"] = (
    claims(me["id_token"])["sub"] == "11111111-1111-4111-8111-111111111111"
)

# Reusing the link must fail.
r2 = requests.get(link, allow_redirects=False)
results["Q1 magic link single use (second open not redirected to callback)"] = (
    "me/callback" not in (r2.headers.get("Location") or "")
)

# 3. Same browser session: consent for the agent client, delegation scope + offline_access.
scope = f"openid offline_access delegation:client:{AGENT}"
auth = f"{KC}/protocol/openid-connect/auth?" + up.urlencode(
    {
        "client_id": AGENT,
        "response_type": "code",
        "scope": scope,
        "prompt": "consent",
        "redirect_uri": "http://localhost:8180/agent/callback",
        "state": "signed-state-2",
    }
)
cb2, r = follow(s, auth, "http://localhost:8180/agent/callback")
if cb2 is None:
    html = r.text
    if "password" in html.lower() and "username" in html.lower():
        print("!! got a login page, not consent: session not reused")
    form = re.search(r'<form[^>]+action="([^"]+)"', html)
    print("consent page lists:", sorted(set(re.findall(r"<li>\s*<span>([^<]+)</span>", html)))[:10])
    action = up.urljoin("http://localhost:8180", form.group(1).replace("&amp;", "&"))
    fields = dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html))
    fields["accept"] = "Yes"
    r = s.post(action, data=fields, allow_redirects=False)
    cb2 = up.urljoin(action, r.headers["Location"])
results["Q1 consent on the same session, no second login"] = (
    cb2.startswith("http://localhost:8180/agent/callback") and "code=" in cb2
)
tok = requests.post(
    TOKEN,
    data={
        "grant_type": "authorization_code",
        "code": code_of(cb2),
        "redirect_uri": "http://localhost:8180/agent/callback",
        "client_id": AGENT,
        "client_secret": AGENT_SECRET,
    },
).json()
print("agent code exchange keys:", sorted(tok), tok.get("error"), tok.get("error_description"))
show("agent access token after consent", claims(tok["access_token"]))
show("agent refresh token", claims(tok["refresh_token"]), ("typ", "scope"))
results["offline token issued"] = claims(tok["refresh_token"]).get("typ") == "Offline"
offline = tok["refresh_token"]

# 4. Kill the browser sessions (person gone), keep the offline session.
requests.post(
    "http://localhost:8180/auth/admin/realms/kitchenless/users/11111111-1111-4111-8111-111111111111/logout",
    headers={"Authorization": f"Bearer {adm}"},
)


# 5. Q2: refresh the offline token, then delegation exchange.
def refresh():
    return requests.post(
        TOKEN,
        data={
            "grant_type": "refresh_token",
            "refresh_token": offline,
            "client_id": AGENT,
            "client_secret": AGENT_SECRET,
        },
    ).json()


rt = refresh()
if "access_token" not in rt:
    print("refresh failed:", rt)
    sys.exit(1)
sub_c = claims(rt["access_token"])
show("refreshed access token (subject)", sub_c)
results["Q2 refreshed offline token still carries may_act"] = "may_act" in sub_c

actor = requests.post(
    TOKEN,
    data={"grant_type": "client_credentials", "client_id": AGENT, "client_secret": AGENT_SECRET},
).json()["access_token"]


def exchange(subject):
    return requests.post(
        TOKEN,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": AGENT,
            "client_secret": AGENT_SECRET,
            "subject_token": subject,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "actor_token": actor,
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
        },
    )


ex = exchange(rt["access_token"])
print("exchange status", ex.status_code, "keys", sorted(ex.json()))
if ex.status_code == 200:
    d = claims(ex.json()["access_token"])
    show("delegated token", d)
    act = d.get("act", {})
    aud = d.get("aud")
    aud = aud if isinstance(aud, list) else [aud]
    results["Q2.1 sub = user, act names the agent"] = (
        d.get("sub") == "11111111-1111-4111-8111-111111111111" and act.get("client_id") == AGENT
    )
    results["Q2.2 aud contains kitchenless-mcp"] = "kitchenless-mcp" in aud
    results["Q2.3 no refresh token"] = "refresh_token" not in ex.json()
    results["_lifetime"] = d["exp"] - d["iat"]
else:
    print("exchange error:", ex.json())

print("\n=== RESULTS")
for k, v in results.items():
    print(f"{'PASS' if v is True else ('FAIL' if v is False else v)}  {k}")
