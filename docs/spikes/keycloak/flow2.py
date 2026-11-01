"""Spike follow-ups: delegated-token lifetime, consent revoke vs the offline token.
Reuses flow.py's helpers by running it first to get a fresh offline token."""
import runpy, requests, json
g = runpy.run_path("flow.py")          # full flow again; leaves `offline`, `exchange`, `claims`, `adm`
KC, TOKEN, AGENT, SEC = g["KC"], g["TOKEN"], g["AGENT"], g["AGENT_SECRET"]
claims, offline, adm = g["claims"], g["offline"], g["adm"]
ADMIN = "http://localhost:8180/auth/admin/realms/kitchenless"
print("\n=== FOLLOW-UPS")
# Lifetime: per-client access token lifespan applies to the delegated token?
print("lifetime set via client attribute access.token.lifespan=120 (see run notes)")
rt = requests.post(TOKEN, data={"grant_type": "refresh_token", "refresh_token": offline,
                                "client_id": AGENT, "client_secret": SEC}).json()
ex = g["exchange"](rt["access_token"]).json()
d = claims(ex["access_token"]); print("delegated lifetime_s:", d["exp"] - d["iat"], "expires_in:", ex["expires_in"])
# Revoke the person's consent for the agent client in Keycloak, then refresh again.
H = "11111111-1111-4111-8111-111111111111"
r = requests.delete(f"{ADMIN}/users/{H}/consents/{AGENT}", headers={"Authorization": f"Bearer {adm}"})
print("consent revoke status:", r.status_code)
rt2 = requests.post(TOKEN, data={"grant_type": "refresh_token", "refresh_token": offline,
                                 "client_id": AGENT, "client_secret": SEC})
print("refresh after revoke:", rt2.status_code, rt2.json().get("error"), rt2.json().get("error_description"))
