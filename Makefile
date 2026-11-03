COMPOSE := docker compose -f infra/docker-compose.yml --env-file infra/.env
CONTRACT_PATH := tests/contract$(if $(SERVICE),/$(SERVICE))

# pytest exits 5 when it collects no tests. The contract and scenario suites stay empty until the
# adapter and scenario designs land, so only those two treat "no tests yet" as a pass.
define allow_empty
s=$$?; if [ $$s -eq 5 ]; then echo "No $(1) tests yet."; exit 0; fi; exit $$s
endef

.PHONY: up down lint test-unit test-contract fga-test smoke scenario eval-fast eval-safety eval-full seed seed-local app-deps app-lint app-test

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

scenario:
	$(COMPOSE) run --rm testrunner pytest tests/scenario -q; $(call allow_empty,scenario)

# evals/run.py defaults ORCHESTRATOR_URL to http://localhost:8000, where api serves
# /hello/run (architecture item 73).
eval-fast:
	uv run python -m evals.run --tag fast

# The PR gate (architecture item 63, amended 2026-11-03): the fast safety cases only.
eval-safety:
	uv run python -m evals.run --tag fast --category safety

eval-full:
	$(COMPOSE) run --rm testrunner python -m evals.run --all

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
