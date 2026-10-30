#!/usr/bin/env bash
# Runs once on an empty data volume: databases for Keycloak and OpenFGA, pgvector for the app.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE DATABASE keycloak OWNER "$POSTGRES_USER";
CREATE DATABASE openfga OWNER "$POSTGRES_USER";
CREATE EXTENSION IF NOT EXISTS vector;
SQL
