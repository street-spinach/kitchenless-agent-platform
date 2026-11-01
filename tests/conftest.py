"""Shared fixtures. `pg` gives a db test its own empty schema (architecture item 68)."""

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="db test: DATABASE_URL is not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def empty_schema_dsn() -> Iterator[str]:
    """A DSN whose search_path is a fresh, empty schema, dropped afterwards."""
    dsn = os.environ["DATABASE_URL"]
    schema = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield make_conninfo(dsn, options=f"-c search_path={schema}")
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def pg(empty_schema_dsn: str) -> str:
    """A DSN for a fresh schema with every migration applied."""
    from migrations.apply import apply

    apply(empty_schema_dsn)
    return empty_schema_dsn
