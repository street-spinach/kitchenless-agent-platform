-- Trace additions (architecture item 15, §10). Additive only: nullable columns and indexes.
ALTER TABLE agent_traces ADD COLUMN conversation_id uuid;
ALTER TABLE agent_traces ADD COLUMN message_id uuid;
ALTER TABLE agent_traces ADD COLUMN model text;
ALTER TABLE agent_traces ADD COLUMN latency_ms integer;

CREATE INDEX ON agent_traces (ts);
CREATE INDEX ON agent_traces (agent_id, ts);

COMMENT ON COLUMN agent_traces.kind IS
  'message | tool_call | tool_result | decision | approval | outcome;'
  ' new kinds (architecture §10): run_start | error | data_share';
