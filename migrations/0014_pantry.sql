-- Pantry (pantry design §3, PT-A5). Additive only: new columns and one index, no new table.

-- pantry_item: the vocabulary name; NULL marks a legacy or unmapped row, never matched.
-- kind: add, verify or out, accepted in code (pantry.KINDS). It has no constraint so a later
-- source needs no migration to add a kind. Old inserts without kind still write 'add'.
-- run_id: the run that wrote the event, so triage can join it to its traces.
ALTER TABLE pantry_events ADD COLUMN pantry_item text NULL;
ALTER TABLE pantry_events ADD COLUMN kind text NOT NULL DEFAULT 'add';
ALTER TABLE pantry_events ADD COLUMN run_id uuid NULL REFERENCES runs;

-- Latest event per household and pantry item decides presence.
CREATE INDEX pantry_events_item_latest
  ON pantry_events (household_id, pantry_item, event_id DESC);

-- The vocabulary item a catalog SKU maps to.
ALTER TABLE catalog_items ADD COLUMN pantry_item text NULL;
