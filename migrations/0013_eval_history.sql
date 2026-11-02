-- Eval history (evals design §6; architecture "Migrations", E-A14). Additive only: two new
-- tables. Neither has a foreign key to runs or agent_traces, so the eval reset (which
-- deletes eval runs) never touches them.

-- One row per eval run. outcome NULL means the eval run died before it finished.
CREATE TABLE eval_runs (
  eval_run_id       uuid        PRIMARY KEY,
  trigger           text        NOT NULL
                    CHECK (trigger IN ('nightly', 'manual', 'd4', 'rehearsal')),
  git_sha           text        NOT NULL,
  model_mode        text        NOT NULL CHECK (model_mode IN ('stub', 'real')),
  models            jsonb       NOT NULL,
  selection         text[]      NOT NULL,
  thresholds        jsonb       NOT NULL,
  tokens            bigint      NOT NULL DEFAULT 0,
  outcome           text        NULL,
  outcome_detail    text        NULL,
  reason_counts     jsonb       NOT NULL DEFAULT '{}',
  error_kind_counts jsonb       NOT NULL DEFAULT '{}',
  started_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz NULL
);

-- One row per attempt. agent_run_ids has no foreign key (two for a pair case).
CREATE TABLE eval_results (
  eval_run_id   uuid        NOT NULL REFERENCES eval_runs ON DELETE CASCADE,
  case_id       text        NOT NULL,
  repeat        integer     NOT NULL,
  category      text        NOT NULL,
  case_hash     text        NOT NULL,
  model_mode    text        NOT NULL,
  agent_run_ids uuid[]      NOT NULL DEFAULT '{}',
  verdict       text        NOT NULL,
  error_kind    text        NOT NULL,
  reason        text        NULL,
  seconds       numeric     NOT NULL,
  tokens        integer     NOT NULL DEFAULT 0,
  models_used   text[]      NOT NULL DEFAULT '{}',
  checks        jsonb       NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (eval_run_id, case_id, repeat)
);

-- "Last attempt per case" and ordering.
CREATE INDEX eval_results_case_mode_created_idx
  ON eval_results (case_id, model_mode, created_at DESC);
