-- Agent directory (architecture item 9, amended by A3; agent-protocol P7). Additive only.
CREATE TABLE agent_cards (
  agent_id     text        PRIMARY KEY,
  class        text        NOT NULL CHECK (class IN ('consumer', 'household', 'grocery',
                                                     'restaurant', 'delivery', 'channel',
                                                     'external')),
  owner_ref    text        NOT NULL,  -- user:<id> | household:<id> | org:<id> | system
  region       text        NOT NULL,
  zone_ids     text[]      NOT NULL DEFAULT '{}',
  currency     char(3)     NULL,
  host_process text        NOT NULL CHECK (host_process IN ('agents', 'api', 'external')),
  vendor       text        NULL,
  config       jsonb       NOT NULL DEFAULT '{}',
  enabled      boolean     NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON agent_cards USING gin (zone_ids);

-- Price lists, margin floors, substitution policy, capacity, hours, coverage and floors.
CREATE TABLE business_rules (
  agent_id   text        NOT NULL REFERENCES agent_cards,
  kind       text        NOT NULL,
  value      jsonb       NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, kind)
);
