# The `kitchenless` realm

`kitchenless.json` is the Keycloak realm import file (identity-authz §4, I19). Keycloak loads
it with `--import-realm` on its **first start against an empty `keycloak` database** (I-D16).
After that, changes to the file are **not** applied automatically: the owner runs the
re-apply runbook below. A PR that changes this file says so in its description, and the
owner runs the steps after merge.

**Re-consent the live cast the day before a demo.**

## What the file holds

*   Token settings: access token 300 s; offline session idle and max 30 days (max limited);
    refresh-token revocation off.
*   `VERIFY_PROFILE` disabled, so a person with no email is not stopped on a profile page.
*   User and admin events on, kept 30 days.
*   FGAP v2 (`adminPermissionsEnabled`), with the client policy `agents-may-delegate`
    listing every people-agent client and the scope permission `agents-delegate-users`
    (resource type `Users`, scope `delegate`). Without it Keycloak silently drops the
    delegation scope. Onboarding adds every new agent client to the policy (I22).
*   The clients of §3:
    *   one per live consumer and household agent (`consumer.us-1-a`, `household.us-1`, …),
        and the disabled templates `template.consumer` and `template.household` that
        onboarding copies. Each has `consentRequired`, a service account (for the actor
        token), `standard.token.exchange.enabled`, the `kitchenless-mcp` audience mapper,
        its §6 class scopes as default scopes, and `offline_access` and `delegation:client`
        as optional scopes;
    *   one service-account client per business agent card (`grocery.square-us`, …; I-D23)
        and `svc.api`, each with the `kitchenless-mcp` audience mapper;
    *   `svc.onboarding` (client secret, `realm-management` `manage-users` and
        `manage-clients`), `me-pages`, `owner-app` (public, PKCE S256, `owner-app`
        audience) and `kitchenless-mcp` (names the audience only).
    *   Every agent client, `svc.api` and `me-pages` use `private_key_jwt` against the
        certificate in `jwt.credential.certificate`.
*   The client scopes: the §6 class scopes, plus copies of the Keycloak 26.8.1 built-in
    scopes the realm uses (`basic`, `acr`, `web-origins`, `roles`, `profile`, `email`,
    `service_account`, `delegation:client`). **A realm import that lists `clientScopes`
    gets no built-in scopes except `offline_access`**, so a scope removed from the list
    is gone from the realm (without `basic`, tokens have no `sub`).
*   The seeded users with fixed ids, each with `realmRoles: ["default-roles-kitchenless"]`
    (users created by an import get no default roles, and without `offline_access`
    consent fails):
    *   US-1's members, usernames `${SEED_US1_PHONE_1}` to `_3` (they mirror
        `SEED_US1_PHONES`);
    *   US-2's members and the eval copies, usernames `sim.<household_id>.<n>`;
    *   one owner per organization, username `owner.<org_id>`, password
        `${OWNER_DEMO_PASSWORD}`.

    The ids come from `keycloak_user_id` in `services/seed/data/households.yaml` and
    `services/seed/data/agents.yaml` (`owners`), the source of truth;
    `tests/unit/test_realm_file.py` checks they match. The seed matches realm users on
    these ids, never on username.

**Placeholders.** Secrets and per-host values appear only as `${VAR}`: `AGENT_SIGNING_CERT`,
`KC_ONBOARDING_SECRET`, `OWNER_DEMO_PASSWORD`, `DOMAIN` and `SEED_US1_PHONE_1` to `_3`,
all set on the `keycloak` service. Never use `${env.VAR}`: Keycloak leaves it as literal
text, and a literal one in a redirect URI stops Keycloak from starting (spike #111).

**New people do not go in the file.** `onboarding.py` creates them through the Admin API,
copying the template clients (§5).

## Re-apply the file (owner, on the VM)

`--override true` replaces the realm with the file. Everything not in the file is lost:
people and agent clients created by onboarding, and every consent and offline session (so
every grant needs a new consent). Plan for it.

1.  `touch /opt/app/DEPLOY_FREEZE`.
2.  Dump the `keycloak` database: `pg_dump -Fc` of `keycloak`, copied off the VM.
3.  Stop Keycloak and import in a **one-off container**, never inside the running one
    (a second JVM there shares the 640 MB limit and can get Keycloak OOM-killed; spike Q4):

    ```sh
    docker compose stop keycloak
    docker compose run --rm keycloak import --optimized \
      --file /opt/keycloak/data/import/kitchenless.json --override true
    ```

4.  `docker compose up -d --no-build keycloak`.
5.  Run smoke.
6.  `rm /opt/app/DEPLOY_FREEZE`.

Recovery is a restore of the dump from step 2.

## Upgrade Keycloak (identity-authz §15, I-D3)

The upgrade is roll-forward only: it migrates the `keycloak` database one way.
`KEYCLOAK_IMAGE` must be set in the VM's `infra/.env` before the PR that bumps the image
merges, because that merge deploys. On the VM:

1.  `touch /opt/app/DEPLOY_FREEZE`.
2.  `pg_dump -Fc` of the `keycloak` database.
3.  Set `KEYCLOAK_IMAGE` to the new tag (`26.8.1-<sha8>`; the image runs
    `start --optimized` with a 384 MB heap and a 640 MB container limit).
4.  `docker compose pull keycloak && docker compose up -d --no-build keycloak`. The realm
    is imported on first start against an empty `keycloak` database only.
5.  Run smoke.
6.  Measure G6 (peak memory).
7.  `rm /opt/app/DEPLOY_FREEZE`.

Recovery is a restore from the dump, never a downgrade. The Keycloak image has its own tag,
separate from `APP_TAG`, so the deploy's rollback by app tag never changes it.

## Rotate the signing certificate

`agents` and `api` sign client assertions with `AGENT_SIGNING_KEY`; Keycloak checks them
against the certificate stored on each client. A client holds one certificate, so rotation
is a cutover.

1.  Make the new key and certificate, each as single-line base64:

    ```sh
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 > agent.key
    openssl req -x509 -key agent.key -days 365 -subj /CN=agents -outform DER \
      | base64 | tr -d '\n'                      # the new AGENT_SIGNING_CERT (base64 DER)
    base64 < agent.key | tr -d '\n'              # the new AGENT_SIGNING_KEY
    ```

2.  `touch /opt/app/DEPLOY_FREEZE`.
3.  Through the Admin API (with `kcadm.sh` in its own container, never inside the
    Keycloak container), set `attributes."jwt.credential.certificate"` to the new value on
    **every client that has one**: every consumer and household agent client (including
    ones onboarding created), both templates, every business agent client, `svc.api` and
    `me-pages`. For example:

    ```sh
    kcadm.sh update clients/<client uuid> -r kitchenless \
      -s 'attributes."jwt.credential.certificate"=<new cert>'
    ```

4.  Set `AGENT_SIGNING_KEY` and `AGENT_SIGNING_CERT` in the VM's `infra/.env`
    (`AGENT_SIGNING_CERT` is also what a future import of this file uses).
5.  `docker compose up -d --no-build agents api keycloak`.
6.  Run smoke.
7.  `rm /opt/app/DEPLOY_FREEZE`, then delete the local key files.
