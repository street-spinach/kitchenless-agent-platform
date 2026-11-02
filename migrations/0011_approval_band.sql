-- Approval band (identity-authz item I12). Additive only: one nullable column, one
-- column with a default.
ALTER TABLE approval_rules ADD COLUMN per_order_minor bigint NULL
  CHECK (per_order_minor >= 0);

-- Which limits sent this approval to a person: 'band', 'budget'.
ALTER TABLE approvals ADD COLUMN limits text[] NOT NULL DEFAULT '{}';
