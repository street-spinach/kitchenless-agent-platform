# Agentic food network: rules for Claude

## What this is
A network of AI agents (consumer, household, grocery, restaurant, delivery) that plan,
negotiate and order food on behalf of people. See docs/designs/SITEMAP.md.

## Source of truth
- Approved designs in docs/designs/ define what to build. Follow them exactly.
- If a design is ambiguous or wrong, stop and say so. Don't guess, and don't redesign.

## Commands
- make up / make down: run the stack locally (Docker Compose)
- make lint, make test-unit, make test-contract SERVICE=<name>
- make eval-fast: run before opening a PR if you touched services/agents/
- MODE=mock is the default. Never switch tests to real integrations.
- MODEL_MODE=stub is the default for tests. Never switch tests to real.

## Conventions
- Python 3.12, uv, ruff, pytest. Type hints on public functions.
- Agents call business systems only through the domain MCP servers in services/mcp/.
  Never call vendor APIs directly from agent code.
- Every tool that spends money, commits to an order or shares personal data must:
  validate the caller token (sub and act), check OpenFGA, check limits in
  services/safety/, call the adapter, and write a trace event.
- Log agent steps through the trace writer. Evals depend on it.
- Migrations are additive only (add tables and columns; never drop or rename).
- Migrations are SQL only. migrations/checkpointer.py is the only exception.
- Shared identifiers come from services/common/ids.py, never ad hoc.
- Idempotency keys are built only from order_id, the action, the acting agent id and an
  optional scope id, always through ids.idempotency_key.
- Business agents use the model only through model.choose(), with an allowed set and a
  rule default computed in code.

## Safety rules (never break these)
- Allergies, diets, budgets and approval rules live in Postgres tables and are always
  enforced in code, never left to the model.
- Business agents receive intent and constraints, never raw consumer profiles.
- Never read, print or edit .env files or secrets. Use .env.example for new variables.

## Tests and evals
- Never weaken, skip or delete tests or eval cases to make checks pass.
- Never edit evals/thresholds.yaml unless the issue explicitly asks.
- Every bug fix adds a test or eval case that would have caught it.

## Git and PRs
- Work on the issue branch. Never commit or push to main. Never force push.
- PR body starts with "Closes #<issue>".
- Never merge PRs and never add labels to PRs.

## Critical paths
Changes under these paths need human review and won't auto-merge:
.github/, .claude/, CLAUDE.md, infra/, migrations/, authz/, services/identity/,
services/safety/, docs/designs/, evals/cases/safety/, evals/thresholds.yaml, scripts/.
Keep critical changes in their own small PRs when you can.
