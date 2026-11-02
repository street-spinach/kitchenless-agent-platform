-- Identity tables (identity-authz item I11). Additive only.

-- No FK to agent_cards: 'system:api' has no card (identity-authz §3).
CREATE TABLE agent_clients (
  agent_id       text        PRIMARY KEY,
  client_id      text        UNIQUE NOT NULL,
  kc_client_uuid uuid        NULL,
  kind           text        NOT NULL CHECK (kind IN ('people', 'service')),
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_grants (
  grant_id           uuid        PRIMARY KEY,
  agent_id           text        NOT NULL REFERENCES agent_cards,
  agent_kind         text        NOT NULL CHECK (agent_kind IN ('consumer', 'household')),
  user_id            uuid        NOT NULL REFERENCES users,  -- the grant holder (sub)
  consented_by       uuid        NOT NULL REFERENCES users,  -- the guardian for a child
  household_id       text        NOT NULL REFERENCES households,
  scopes             text[]      NOT NULL,
  status             text        NOT NULL DEFAULT 'active'
                                   CHECK (status IN ('active', 'revoked', 'expired')),
  refresh_ciphertext bytea       NOT NULL,
  key_version        integer     NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  expires_at         timestamptz NOT NULL,
  revoked_at         timestamptz NULL,
  revoked_by         uuid        NULL REFERENCES users,
  revoke_reason      text        NULL
);

CREATE UNIQUE INDEX ON agent_grants (agent_id, user_id) WHERE status = 'active';
CREATE UNIQUE INDEX ON agent_grants (agent_id)
  WHERE status = 'active' AND agent_kind = 'household';
CREATE INDEX ON agent_grants (household_id, status);

CREATE TABLE consent_links (
  link_hash       bytea       PRIMARY KEY,
  purpose         text        NOT NULL CHECK (purpose IN ('consent', 'me')),
  target_user_id  uuid        NOT NULL REFERENCES users,
  sent_to_user_id uuid        NOT NULL REFERENCES users,
  household_id    text        NOT NULL REFERENCES households,
  agent_ids       text[]      NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL,
  used_at         timestamptz NULL
);

CREATE TABLE audit_events (
  event_id        bigserial   PRIMARY KEY,
  ts              timestamptz NOT NULL DEFAULT now(),
  kind            text        NOT NULL CHECK (kind IN ('user_onboarded',
                                'agent_client_created', 'link_issued', 'link_used',
                                'link_rejected', 'consent_granted', 'consent_revoked',
                                'grant_expired', 'band_changed', 'owner_rule_changed',
                                'approval_decided', 'fga_reconciled',
                                'keycloak_revoke_failed')),
  actor_user_id   uuid        NULL,
  subject_user_id uuid        NULL,
  agent_id        text        NULL,
  household_id    text        NULL,
  org_id          text        NULL,
  grant_id        uuid        NULL,
  run_id          uuid        NULL,
  detail          jsonb       NOT NULL DEFAULT '{}'
);

CREATE INDEX ON audit_events (household_id, ts DESC);

COMMENT ON TABLE audit_events IS 'Insert-only: rows are never updated (identity-authz I11).';

CREATE TABLE fga_models (
  model_id   text        PRIMARY KEY,
  store_id   text        NOT NULL,
  sha256     text        NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
