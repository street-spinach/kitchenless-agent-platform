# Spike: Keycloak 26.8 delegation, magic link and memory (identity-authz I14)

*   **Issue:** #111 (identity-authz build step 1)
*   **Date:** 2026-11-01
*   **Where:** a scratch Compose project in [`keycloak/`](keycloak/), run locally on Colima
    (Docker 29.5.2, 4 CPUs, aarch64). Not part of the deployed stack.
*   **Outcome:** §12a row **"Q2 passes"**. Live uses `TOKEN_SOURCE=keycloak`.
    `TokenServiceSource` and the `token_service` parts of I25 are not built. Q1, Q3 and Q4
    pass; Q4's margin is thin (below).

## How to rerun

```
cd docs/spikes/keycloak
docker compose up -d --build
./kc.sh config credentials --server http://keycloak:8080/auth --realm master \
  --user spike-admin --password spike-only
./setup.sh                      # clients, roles, mapper, VERIFY_PROFILE off, FGAP v2 delegate permission
uv run --with requests python flow.py
docker compose down -v          # when done
```

All values in the spike files are throwaway and local only.

## Answers

### Q3. Which 26.8.N

*   Newest patch on 2026-11-01: **26.8.1**. `quay.io/keycloak/keycloak:26.8.1` →
    `sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332` (the
    multi-arch index; the same digest as tag `26.8.1-2`).
*   Java: OpenJDK 21.0.13.

### Preview features

*   Build option: `--features=token-exchange-delegation,parameterized-scopes`. Both are
    needed; the log shows `Preview features enabled: parameterized-scopes:v1,
    token-exchange-delegation:v1`.
*   Standard token exchange (v2) and admin fine-grained permissions (FGAP v2) are on by
    default. FGAP v2 must still be enabled per realm (`adminPermissionsEnabled: true`).
*   Do **not** use `--features=preview`: it also turns on legacy `token-exchange:v1`.

### Q1. Magic link to one confidential client, then consent on the same session — passes

*   Extension: `io.phasetwo.keycloak:keycloak-magic-link:0.94` from Maven Central, built
    against Keycloak 26.8.0, SHA-256
    `e9e7c3df0c0b36fd72ed5dfbcd305d281b4a18ffd88cdff7685103194e9072e1`. No runtime
    dependencies. **License: Elastic License 2.0** (source-available, not OSI open source).
    It allows our use; it forbids offering the extension itself as a hosted service.
*   Call: `POST /auth/realms/kitchenless/magic-link` with a bearer token holding
    `realm-management` `manage-users` (no separate magic-link role). Body used:

    ```json
    {"username": "us-1.1", "client_id": "me-pages",
     "redirect_uri": "http://localhost:8180/me/callback",
     "expiration_seconds": 120, "reusable": false,
     "state": "<api's signed state>", "scope": "openid"}
    ```

    Response keys: `user_id`, `link`, `sent`. `username` works for users with no email.
    `reusable` defaults to **true**, so `false` must be sent. Without `scope: openid` the
    code returns no ID token.
*   Opening the link signs the user in and redirects to `me-pages`'s redirect URI with
    `code` and our `state` in the query. The ID token's `sub` is the target user.
*   A second open of the same link does not reach the callback.
*   On the same browser session, an authorization request for the agent client with
    `prompt=consent` shows Keycloak's consent screen with no second login. It lists
    "Allow household.us-1 to act on your behalf?", "Offline Access", and the profile
    scopes. Accepting returns a code.
*   Keycloak's cookies are `Secure; SameSite=None`. Browsers accept them on `https` and on
    `http://localhost`; the spike's Python client needed a workaround, not the design.

### Q2. Delegation with an offline token — passes (all four exit tests)

The flow, as run:

1.  Authorization request for the agent client: `scope=openid offline_access
    delegation:client:household.us-1`, `prompt=consent`. The agent client has
    `consentRequired: true`.
2.  The code exchange returns an access token carrying
    `may_act: {"sub": <agent service-account user id>, "client_id": "household.us-1"}`,
    and a refresh token with `typ: "Offline"` (30 days).
3.  The person's browser sessions are logged out (Admin API `users/{id}/logout`). The
    offline session survives.
4.  Refreshing the offline token with the agent client returns an access token that
    **still carries `may_act`**. This is the question the design rested on.
5.  The agent gets its own `client_credentials` token (the actor token), then calls the
    token endpoint as itself:

    ```
    grant_type=urn:ietf:params:oauth:grant-type:token-exchange
    subject_token=<refreshed access token>
    subject_token_type=urn:ietf:params:oauth:token-type:access_token
    actor_token=<agent's client_credentials token>
    actor_token_type=urn:ietf:params:oauth:token-type:access_token
    ```

    No `audience` parameter: the audience comes from the agent client's mappers.

The delegated token:

| Claim | Value |
|---|---|
| `sub` | the person (`11111111-…`) |
| `act` | `{"sub": <agent's service-account user id>, "client_id": "household.us-1"}` |
| `azp` | `household.us-1` |
| `aud` | `"kitchenless-mcp"` (a string, not a list) |
| `scope` | `profile email` |
| refresh token | none (`refresh_token` absent) |
| lifetime | the agent client's access-token lifespan: 300 s by default; 120 s after setting the client attribute `access.token.lifespan=120` |

*   Exit test 1: `sub` = person and the agent is named in `act` — **but in
    `act.client_id`**. `act.sub` is the service-account user id, not the client id.
*   Exit tests 2–4: pass as above.

**Revoke.** `DELETE /admin/realms/kitchenless/users/{id}/consents/household.us-1` (204)
deletes the offline session. The next refresh fails with `400 invalid_grant` "Offline user
session not found", which is the path `grants.mark_refused` handles.

**Client authentication.** `private_key_jwt` (`clientAuthenticatorType: client-jwt`, the
certificate in `jwt.credential.certificate` as base64 DER) accepts an RS256 assertion with
**no `kid`**, and `aud` = the realm issuer URL (the token endpoint URL also works).

### Q4. Memory with a 384 MB heap and a 640 MB limit — passes, thin margin

| Moment | Container memory (cgroup `memory.current` / `memory.peak`) |
|---|---|
| Idle after start | 501 MiB |
| After five full flows (magic link, consent, offline refresh, exchange) | 552 MiB, peak 554 MiB |

*   About 86 MiB of headroom under 640 MiB. Measured on aarch64; G6 re-measures on the
    x86_64 VM after the upgrade.
*   **Any second JVM inside the Keycloak container counts against the same limit.**
    Running `kcadm.sh` inside the container got Keycloak OOM-killed during setup. The
    spike runs `kcadm` in a separate container (`compose.yml` service `kcadm`). The
    realm re-apply runbook must do the same for `kc.sh import`.

## Realm setup the import file must carry

Found while running the flow; each is needed for it to work:

1.  **Placeholder syntax is `${VAR}`, not `${env.VAR}`.** `${env.VAR}` is left as literal
    text everywhere (usernames, secrets, certificate attributes, redirect URIs); a literal
    placeholder in a redirect URI stops Keycloak from starting. `${VAR}` is replaced in all
    four places.
2.  **`:` is not allowed in usernames** (`error-username-invalid-character`). E.164 phone
    numbers (`+15135550101`) and dotted forms (`us-1.1`, `child.hh-abc.1`) are accepted.
3.  **Seeded users need `realmRoles: ["default-roles-kitchenless"]`.** Users created by the
    import get no default roles, and without `offline_access` the code exchange fails with
    "Offline tokens not allowed for the user or client". Users created through the Admin
    API get the default roles.
4.  **The `VERIFY_PROFILE` required action must be disabled** (or email made optional).
    Otherwise a user with no email stops on a profile page after the magic link.
5.  **Each agent client:** confidential, `consentRequired: true`, service account enabled
    (for the actor token), attribute `standard.token.exchange.enabled: "true"`, an
    audience mapper adding `kitchenless-mcp`, and the delegation scopes as optional scopes
    (Keycloak adds `delegation:client` and `delegation:user` to every realm as optional
    scopes when the feature is on).
6.  **FGAP v2:** `adminPermissionsEnabled: true`; a client policy listing the agent
    clients; and a scope permission on resource type `Users` with scope `delegate` and
    that policy. Without it Keycloak silently drops the delegation scope. Every new agent
    client must be added to the policy.

## What changes in identity-authz (applied by the amendment in this PR)

*   §12a: outcome "Q2 passes"; `TOKEN_SOURCE=keycloak` for live.
*   I3, I15, I19, I22: the values above.
*   §8 step 1 and I2: the agent comes from `act.client_id`.
*   §4 and I19: `${VAR}` placeholders, no `:` in usernames, default roles, `VERIFY_PROFILE`
    off, the FGAP v2 policy and permission.
*   §5 and I22: onboarding adds each new agent client to the delegate policy.
*   I22: `magic_link` sends `username`, `reusable: false`, `state` and `scope`.
*   The realm README runbook: run `kc.sh import` in a one-off container, never inside the
    running Keycloak container.
*   I-D1: the extension is source-available (ELv2), not open source.
