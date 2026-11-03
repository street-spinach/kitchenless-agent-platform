# The owner app's web export (experience EX-I45), from the committed lockfile.
FROM node:22-slim AS owner
WORKDIR /owner
COPY apps/owner/package.json apps/owner/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/owner ./
RUN npx expo export --platform web --output-dir dist

# One image for api, mcp, agents, migrate and testrunner.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY migrations ./migrations
# The OpenFGA model, applied by migrate (identity-authz I8, I41).
COPY authz ./authz
COPY services ./services
COPY evals ./evals
COPY tests ./tests
# Served at /owner/ by api (services/api/routes/owner_static.py, OWNER_DIST).
COPY --from=owner /owner/dist /app/owner_dist
