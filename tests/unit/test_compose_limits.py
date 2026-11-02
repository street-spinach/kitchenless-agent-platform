"""Compose memory limits and database tuning (architecture §11, item 56)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.yml"

# architecture.md §11, "Deployment and memory budget (4 GB e2-medium)".
_SECTION_11 = {
    "caddy": "64m",
    "postgres": "384m",
    "keycloak": "640m",
    "openfga": "128m",
    "neo4j": "896m",
    "api": "256m",
    "agents": "384m",
    "mcp": "320m",
    "testrunner": "256m",
}

# One-shot services outside the §11 table (PR #175 decisions).
_ONE_SHOT = {
    "migrate": "256m",
    "openfga-migrate": "128m",
}


def _services() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE.read_text())["services"]


def test_every_service_has_a_mem_limit() -> None:
    services = _services()
    missing = [name for name, svc in services.items() if not svc.get("mem_limit")]
    assert missing == []


def test_every_service_is_budgeted() -> None:
    assert set(_services()) == set(_SECTION_11) | set(_ONE_SHOT)


def test_mem_limits_match_section_11() -> None:
    services = _services()
    for name, limit in {**_SECTION_11, **_ONE_SHOT}.items():
        assert services[name]["mem_limit"] == limit, name


def test_long_running_caps_total_3072_mb() -> None:
    long_running = {k: v for k, v in _SECTION_11.items() if k != "testrunner"}
    assert sum(int(v.removesuffix("m")) for v in long_running.values()) == 3072


def test_postgres_tuning() -> None:
    command = _services()["postgres"]["command"]
    assert command == "postgres -c shared_buffers=128MB -c max_connections=60"


def test_openfga_max_open_conns() -> None:
    env = _services()["openfga"]["environment"]
    assert env["OPENFGA_DATASTORE_MAX_OPEN_CONNS"] == "10"


def test_neo4j_heap_and_page_cache() -> None:
    env = _services()["neo4j"]["environment"]
    assert env["NEO4J_server_memory_heap_initial__size"] == "512m"
    assert env["NEO4J_server_memory_heap_max__size"] == "512m"
    assert env["NEO4J_server_memory_pagecache_size"] == "192m"
