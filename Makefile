COMPOSE := docker compose -f infra/docker-compose.yml --env-file infra/.env
CONTRACT_PATH := tests/contract$(if $(SERVICE),/$(SERVICE))

# pytest exits 5 when it collects no tests. The contract and scenario suites stay empty until the
# adapter and scenario designs land, so only those two treat "no tests yet" as a pass.
define allow_empty
s=$$?; if [ $$s -eq 5 ]; then echo "No $(1) tests yet."; exit 0; fi; exit $$s
endef

.PHONY: up down lint test-unit test-contract smoke scenario eval-fast eval-full seed seed-local

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

smoke:
	$(COMPOSE) run --rm testrunner pytest tests/smoke -q

scenario:
	$(COMPOSE) run --rm testrunner pytest tests/scenario -q; $(call allow_empty,scenario)

# Until build step 6, /hello/run lives in the hello container, published on host port 8004
# because api owns 8000 (architecture items 56 and 73). Step 6 removes this override.
eval-fast: ORCHESTRATOR_URL ?= http://localhost:8004
eval-fast:
	ORCHESTRATOR_URL=$(ORCHESTRATOR_URL) uv run python -m evals.run --tag fast

eval-full:
	$(COMPOSE) run --rm testrunner python -m evals.run --all

# SEED_ARGS, for example --env eval (architecture items 55 and 73).
seed:
	$(COMPOSE) run --rm testrunner python -m services.seed $(SEED_ARGS)

seed-local:
	uv run python -m services.seed $(SEED_ARGS)
