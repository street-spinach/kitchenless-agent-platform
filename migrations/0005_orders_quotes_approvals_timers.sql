-- Orders, quotes, approvals, timers, pantry events and shortfalls (architecture item 11,
-- amended by A4; agent-protocol P9). Additive only.
CREATE TABLE orders (
  order_id             uuid        PRIMARY KEY,
  run_id               uuid        NOT NULL REFERENCES runs,
  household_id         text        NOT NULL REFERENCES households,
  kind                 text        NOT NULL CHECK (kind IN ('grocery', 'dinner')),
  seller_agent_id      text        NULL REFERENCES agent_cards,
  delivery_agent_id    text        NULL REFERENCES agent_cards,
  state                text        NOT NULL DEFAULT 'draft' CHECK (state IN
                                     ('draft', 'quoting', 'negotiating', 'awaiting_approval',
                                      'authorized', 'placed', 'courier_booked', 'in_delivery',
                                      'delivered', 'cancelled', 'expired', 'failed')),
  total_minor          bigint      NULL,
  currency             char(3)     NOT NULL,
  items_hash           text        NULL,
  address_ref          uuid        NOT NULL REFERENCES household_addresses,
  external_order_id    text        NULL,
  external_delivery_id text        NULL,
  delivery_window      tstzrange   NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON orders (household_id, state, created_at);

CREATE TABLE order_items (
  order_id        uuid        NOT NULL REFERENCES orders,
  line            integer     NOT NULL,
  sku             text        NOT NULL,
  name            text        NOT NULL,
  qty             numeric     NOT NULL,
  unit_minor      bigint      NOT NULL,
  substituted_for text        NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (order_id, line)
);

CREATE TABLE quotes (
  quote_id        uuid        PRIMARY KEY,
  run_id          uuid        NOT NULL REFERENCES runs,
  order_id        uuid        NOT NULL REFERENCES orders,
  conversation_id uuid        NOT NULL,
  seller_agent_id text        NOT NULL REFERENCES agent_cards,
  buyer_agent_id  text        NOT NULL REFERENCES agent_cards,
  items           jsonb       NOT NULL,
  total_minor     bigint      NOT NULL,
  currency        char(3)     NOT NULL,
  round           integer     NOT NULL DEFAULT 0,
  expires_at      timestamptz NOT NULL,
  status          text        NOT NULL DEFAULT 'open' CHECK (status IN
                                ('open', 'countered', 'accepted', 'rejected', 'expired')),
  -- Allergen and diet codes from the quote_request, used for shortfall substitutes.
  constraints     jsonb       NOT NULL DEFAULT '{}',
  closed_by       text        NULL CHECK (closed_by IN ('buyer', 'seller')),
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approvals (
  approval_id  uuid        PRIMARY KEY,
  run_id       uuid        NOT NULL REFERENCES runs,
  household_id text        NOT NULL REFERENCES households,
  order_id     uuid        NOT NULL REFERENCES orders,
  amount_minor bigint      NOT NULL,
  currency     char(3)     NOT NULL,
  status       text        NOT NULL DEFAULT 'pending' CHECK (status IN
                             ('pending', 'approved', 'rejected', 'expired')),
  requested_at timestamptz NOT NULL DEFAULT now(),
  decided_at   timestamptz NULL,
  decided_by   uuid        NULL REFERENCES users
);

CREATE TABLE timers (
  timer_id   bigserial   PRIMARY KEY,
  kind       text        NOT NULL CHECK (kind IN ('approval_expiry', 'run_timeout',
                                                  'quote_round', 'shortfall_expiry')),
  run_id     uuid        NOT NULL REFERENCES runs,
  due_at     timestamptz NOT NULL,
  ref        jsonb       NOT NULL DEFAULT '{}',
  status     text        NOT NULL DEFAULT 'pending' CHECK (status IN
                           ('pending', 'done', 'cancelled')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON timers (status, due_at);

-- Provisional (architecture §13). The pantry design extends it.
CREATE TABLE pantry_events (
  event_id     bigserial   PRIMARY KEY,
  household_id text        NOT NULL REFERENCES households,
  item         text        NOT NULL,
  delta        numeric     NOT NULL,
  unit         text        NOT NULL,
  source       text        NOT NULL,
  order_id     uuid        NULL REFERENCES orders,
  ts           timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ON pantry_events (order_id, item) WHERE order_id IS NOT NULL;

-- Post-booking shortfall (agent-protocol P9). picks maps line -> sku or 'drop'.
CREATE TABLE shortfalls (
  shortfall_id    uuid        PRIMARY KEY,
  run_id          uuid        NOT NULL REFERENCES runs,
  order_id        uuid        NOT NULL UNIQUE REFERENCES orders,
  seller_agent_id text        NOT NULL REFERENCES agent_cards,
  lines           jsonb       NOT NULL,
  picks           jsonb       NOT NULL DEFAULT '{}',
  status          text        NOT NULL DEFAULT 'pending' CHECK (status IN
                                ('pending', 'closed', 'applied')),
  due_at          timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  closed_at       timestamptz NULL
);
