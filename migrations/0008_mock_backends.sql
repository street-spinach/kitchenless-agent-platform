-- Mock vendor backends (architecture item 14, amended by A5; agent-protocol P10).
-- Additive only.
CREATE TABLE mock_stock (
  env        text        NOT NULL CHECK (env IN ('live', 'eval')),
  agent_id   text        NOT NULL,
  sku        text        NOT NULL,
  qty        integer     NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (env, agent_id, sku)
);

CREATE TABLE mock_orders (
  external_order_id text        PRIMARY KEY,
  env               text        NOT NULL,
  agent_id          text        NOT NULL,
  idempotency_key   text        NOT NULL UNIQUE,
  body              jsonb       NOT NULL,
  state             text        NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- "window" is a reserved word in Postgres, so the column name is quoted.
CREATE TABLE mock_deliveries (
  external_delivery_id text        PRIMARY KEY,
  env                  text        NOT NULL,
  agent_id             text        NOT NULL,
  idempotency_key      text        NOT NULL UNIQUE,
  zone_id              text        NOT NULL,
  "window"             tstzrange   NOT NULL,
  batch_id             uuid        NULL,
  state                text        NOT NULL CHECK (state IN
                                     ('booked', 'picked_up', 'delivered', 'failed')),
  next_state_at        timestamptz NULL,
  created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE mock_events (
  id              bigserial   PRIMARY KEY,
  env             text        NOT NULL,
  kind            text        NOT NULL CHECK (kind IN
                                ('out_of_stock', 'kitchen_overloaded', 'courier_delayed',
                                 'adapter_down', 'shortfall')),
  target_agent_id text        NOT NULL,
  params          jsonb       NOT NULL DEFAULT '{}',
  active          boolean     NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sim_outbound (
  id         bigserial   PRIMARY KEY,
  phone_e164 text        NOT NULL,
  run_id     uuid        NULL,
  body       jsonb       NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON sim_outbound (phone_e164, id);
