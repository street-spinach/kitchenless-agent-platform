# syntax=docker/dockerfile:1.7
# Keycloak 26.8.1 with the magic-link provider and the realm file (identity-authz I15).
# Build context: the repository root. CI pushes it as
# ghcr.io/street-spinach/kitchenless-keycloak:26.8.1-<sha8> (I42); the VM picks it with
# KEYCLOAK_IMAGE (§15). The upgrade from 26.0 is one way: never downgrade.
FROM quay.io/keycloak/keycloak:26.8.1@sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332

# io.phasetwo.keycloak:keycloak-magic-link:0.94, built for Keycloak 26.8.0, no runtime
# dependencies. Pinned by checksum.
ADD --chown=keycloak:keycloak \
    --checksum=sha256:e9e7c3df0c0b36fd72ed5dfbcd305d281b4a18ffd88cdff7685103194e9072e1 \
    https://repo1.maven.org/maven2/io/phasetwo/keycloak/keycloak-magic-link/0.94/keycloak-magic-link-0.94.jar \
    /opt/keycloak/providers/keycloak-magic-link-0.94.jar

# Build options are fixed here because the service runs `start --optimized`. Never
# --features=preview: it also enables legacy token exchange.
RUN /opt/keycloak/bin/kc.sh build \
    --db=postgres \
    --health-enabled=true \
    --http-relative-path=/auth \
    --features=token-exchange-delegation,parameterized-scopes

# Imported on start with --import-realm (skipped when the realm already exists).
COPY --chown=keycloak:keycloak services/identity/realm/kitchenless.json /opt/keycloak/data/import/
