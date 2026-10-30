-- Agent trace table (ENGINEERING_PROCESS.md section 12.3). Evals read traces, not model text.
CREATE TABLE agent_traces (
  run_id       uuid        NOT NULL,
  seq          integer     NOT NULL,
  ts           timestamptz NOT NULL DEFAULT now(),
  agent_id     text        NOT NULL,
  on_behalf_of text,
  kind         text        NOT NULL,  -- message | tool_call | tool_result | decision | approval | outcome
  payload      jsonb       NOT NULL,
  tokens_in    integer,
  tokens_out   integer,
  PRIMARY KEY (run_id, seq)
);
