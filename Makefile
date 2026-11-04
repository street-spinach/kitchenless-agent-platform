COMPOSE := docker compose -f infra/docker-compose.yml --env-file infra/.env
CONTRACT_PATH := tests/contract$(if $(SERVICE),/$(SERVICE))

# pytest exits 5 when it collects no tests. The contract and scenario suites stay empty until the
# adapter and scenario designs land, so only those two treat "no tests yet" as a pass.
define allow_empty
s=$$?; if [ $$s -eq 5 ]; then echo "No $(1) tests yet."; exit 0; fi; exit $$s
endef

.PHONY: up down lint test-unit test-contract fga-test smoke a2a-smoke scenario scenario-identity eval-fast eval-safety eval-full seed seed-local app-deps app-lint app-test

up:
	$(COMPOSE) up -d --build --remove-orphans --wait

down:
	$(COMPOSE) down

lint:
	uv run ruff check .
	uv run ruff format --check .

test-unit:
	uv run pytest tests/unit -q

test-contract:
	MODE=$${MODE:-mock} REAL=$(REAL) uv run pytest $(CONTRACT_PATH) -q; $(call allow_empty,contract)

# The OpenFGA model tests, and authz/model.json against a fresh transform (identity-authz
# I7). Needs the pinned `fga` CLI (.github/workflows/ci.yml installs it).
fga-test:
	fga model test --tests authz/model.fga.yaml
	fga model transform --file authz/model.fga | uv run python -m authz.check_transform

smoke:
	$(COMPOSE) run --rm testrunner pytest tests/smoke -q

# The A2A endpoint alone (agent-protocol P44).
a2a-smoke:
	$(COMPOSE) run --rm testrunner pytest tests/smoke/test_a2a.py -q

scenario:
	$(COMPOSE) run --rm testrunner pytest tests/scenario -q; $(call allow_empty,scenario)

# identity-authz I43: from the host against `make up` (TOKEN_SOURCE=mock, DOMAIN=localhost),
# with the stack's settings in the environment. Outside tests/scenario, so `make scenario`
# and the deploy never run it, and it never creates grants in the live realm.
scenario-identity:
	uv run pytest tests/identity_scenario -q

# evals/run.py defaults ORCHESTRATOR_URL to http://localhost:8000, where api serves
# /hello/run (architecture item 73).
eval-fast:
	uv run python -m evals.run --tag fast

# The PR gate (architecture item 63, amended 2026-11-03): the fast safety cases, once each.
# Nightly `eval-full` runs every case with thresholds.yaml's repeats.
eval-safety:
	uv run python -m evals.run --tag fast --category safety --repeats 1

# Until eval-nightly.sh (#232) replaces it: when `agents` serves the real model (demo week,
# evals §9), the stub suite is skipped, as evals §10 step 3 does with real evals off, so the
# nightly never spends model tokens.
AGENTS_MODEL_MODE = $(COMPOSE) exec -T agents python -c "import json,urllib.request; \
	print(json.load(urllib.request.urlopen('http://127.0.0.1:8003/health', timeout=3)) \
	.get('model_mode', ''))"

eval-full:
	@if [ "$$($(AGENTS_MODEL_MODE) 2>/dev/null)" = "real" ]; then \
		echo 'EVAL_STATUS {"outcome": "skipped", "detail": "agents_real"}'; \
	else \
		$(COMPOSE) run --rm testrunner python -m evals.run --all; \
	fi

# SEED_ARGS, for example --env eval (architecture items 55 and 73).
seed:
	$(COMPOSE) run --rm testrunner python -m services.seed $(SEED_ARGS)

seed-local:
	uv run python -m services.seed $(SEED_ARGS)

# The owner app (experience EX-I24). CI runs these in node:22 (the owner-app job).
OWNER_APP := apps/owner

app-deps:
	cd $(OWNER_APP) && npm ci --no-audit --no-fund

app-lint: app-deps
	cd $(OWNER_APP) && npx eslint . && npx tsc --noEmit

app-test: app-deps
	cd $(OWNER_APP) && npx jest --ci
