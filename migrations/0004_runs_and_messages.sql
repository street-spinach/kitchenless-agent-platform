-- Runs and messages (architecture item 10, amended by A2; agent-protocol P8). Additive only.
-- trigger: {source, user_id, inbound_id, retry_of?, overrides?}; a2a_demo runs have no user_id.
CREATE TABLE runs (
  run_id       uuid        PRIMARY KEY,
  household_id text        NULL REFERENCES households,
  kind         text        NOT NULL CHECK (kind IN ('weekly', 'dinner', 'chat', 'hello',
                                                    'a2a_demo')),
  status       text        NOT NULL DEFAULT 'running' CHECK (status IN
                             ('running', 'awaiting_approval', 'completed', 'cancelled', 'error',
                              'timeout')),
  reason       text        NULL,
  trigger      jsonb       NOT NULL,
  modes        jsonb       NOT NULL,
  started_at   timestamptz NOT NULL DEFAULT now(),
  ended_at     timestamptz NULL
);

CREATE INDEX ON runs ((trigger->>'user_id'), started_at DESC);

CREATE TABLE agent_messages (
  message_id      uuid        PRIMARY KEY,
  run_id          uuid        NOT NULL REFERENCES runs,
  conversation_id uuid        NULL,
  causation_id    uuid        NULL,
  from_agent      text        NOT NULL,
  to_agent        text        NOT NULL,
  to_host         text        NOT NULL,
  type            text        NOT NULL,
  body            jsonb       NOT NULL,
  status          text        NOT NULL DEFAULT 'pending' CHECK (status IN
                                ('pending', 'processing', 'processed', 'failed')),
  attempts        integer     NOT NULL DEFAULT 0,
  available_at    timestamptz NOT NULL DEFAULT now(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  claimed_at      timestamptz NULL,
  processed_at    timestamptz NULL,
  last_error      text        NULL
);

-- The claim query (architecture §4) also filters available_at <= now().
CREATE INDEX ON agent_messages (to_host, status, available_at);
-- GetTask and the live view (agent-protocol P8).
CREATE INDEX ON agent_messages (conversation_id, created_at);

-- Deduplicates webhook deliveries from WhatsApp and DoorDash.
CREATE TABLE inbound_messages (
  channel     text        NOT NULL,
  external_id text        NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  run_id      uuid        NULL,
  PRIMARY KEY (channel, external_id)
);
