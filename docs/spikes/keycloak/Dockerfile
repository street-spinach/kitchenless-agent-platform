# syntax=docker/dockerfile:1.7
# Spike image only (identity-authz I14). Not part of the deployed stack.
FROM quay.io/keycloak/keycloak:26.8.1@sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332
ADD --chown=keycloak:keycloak --checksum=sha256:e9e7c3df0c0b36fd72ed5dfbcd305d281b4a18ffd88cdff7685103194e9072e1 \
    https://repo1.maven.org/maven2/io/phasetwo/keycloak/keycloak-magic-link/0.94/keycloak-magic-link-0.94.jar \
    /opt/keycloak/providers/keycloak-magic-link-0.94.jar
RUN /opt/keycloak/bin/kc.sh build --db=postgres --health-enabled=true --http-relative-path=/auth \
    --features=token-exchange-delegation,parameterized-scopes
COPY realm-spike.json /opt/keycloak/data/import/realm-spike.json
