-- Agents (agents design §9, §10; AG-A13). Additive only: one CHECK, one partial unique
-- index and one new table. Uses nothing from 0010 or 0011.

-- Learned rows can only be dislikes. Every existing row has source = 'seed'.
ALTER TABLE member_constraints ADD CONSTRAINT member_constraints_learned_dislike_only
  CHECK (source <> 'learned' OR kind = 'dislike');

-- One learned row per user and value.
CREATE UNIQUE INDEX member_constraints_learned_uniq ON member_constraints (user_id, value)
  WHERE source = 'learned';

-- Take turns (§10): one row per household.
CREATE TABLE household_rules (
  household_id       text        PRIMARY KEY REFERENCES households,
  take_turns         boolean     NOT NULL DEFAULT false,
  turn_index         integer     NOT NULL DEFAULT 0,
  last_turn_order_id uuid        NULL,
  set_by             uuid        NULL REFERENCES users,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
