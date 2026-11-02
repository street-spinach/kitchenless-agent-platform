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
