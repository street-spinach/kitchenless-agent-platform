-- Idempotency keys and adapter modes (architecture item 12). Additive only.
CREATE TABLE idempotency_keys (
  key          text        PRIMARY KEY,
  run_id       uuid        NOT NULL REFERENCES runs,
  request_hash text        NOT NULL,
  result       jsonb       NULL,
  status       text        NOT NULL CHECK (status IN ('in_progress', 'done')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE adapter_modes (
  vendor     text        PRIMARY KEY,
  mode       text        NOT NULL CHECK (mode IN ('real', 'mock')),
  set_by     text        NOT NULL,
  set_at     timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);
