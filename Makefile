COMPOSE := docker compose -f infra/docker-compose.yml --env-file infra/.env

.PHONY: up down

# Remaining targets (section 8) arrive in Phase 0 task 4.
up:
	$(COMPOSE) up -d --build --remove-orphans --wait

down:
	$(COMPOSE) down
