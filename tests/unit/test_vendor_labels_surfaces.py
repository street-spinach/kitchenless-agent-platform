"""Vendor labels on the surfaces that name a run's seller or courier (adapters §6, file 33;
issue #169).

The run view (`GET /runs/{run_id}`) is the surface on main that names them. The labels come
from the run's pinned `runs.modes`, never from the current modes.
"""

import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.common import ids
from tests.unit import api_world as w

db = pytest.mark.db

SELLER = "grocery:square-us"
COURIER = "delivery:doordash-us"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)


@pytest.fixture
def world(pg: str) -> str:
    w.seed(pg)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (ids.seed_address_ref(w.HOUSEHOLD_ID), w.HOUSEHOLD_ID),
        )
        for org_id, name in (
            ("square-grocery-us", "Square Grocery"),
            ("doordash-us", "DoorDash Drive (simulated)"),
        ):
            conn.execute(
                "INSERT INTO organizations (org_id, name, region) VALUES (%s, %s, 'us')",
                (org_id, name),
            )
        for agent_id, cls, owner, vendor in (
            (SELLER, "grocery", "org:square-grocery-us", "square"),
            (COURIER, "delivery", "org:doordash-us", "doordash"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process,"
                " vendor) VALUES (%s, %s, %s, 'us', 'agents', %s)",
                (agent_id, cls, owner, vendor),
            )
    return pg


def _run(pg: str, run_modes: dict[str, str]) -> uuid.UUID:
    """A weekly run pinned to `run_modes`, with an order at SELLER booked with COURIER."""
    run_id, order_id = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, %s)",
            (run_id, w.HOUSEHOLD_ID, Jsonb({"user_id": str(w.ADMIN)}), Jsonb(run_modes)),
        )
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, seller_agent_id,"
            " delivery_agent_id, total_minor, currency, address_ref)"
            " VALUES (%s, %s, %s, 'grocery', 'courier_booked', %s, %s, 1207, 'USD', %s)",
            (
                order_id,
                run_id,
                w.HOUSEHOLD_ID,
                SELLER,
                COURIER,
                ids.seed_address_ref(w.HOUSEHOLD_ID),
            ),
        )
    return run_id


def _view(pg: str, run_id: uuid.UUID) -> dict:
    resp = w.client(pg).get(f"/runs/{run_id}")
    assert resp.status_code == 200
    return resp.json()


@db
def test_run_view_real_square_is_sandbox(world: str) -> None:
    run_id = _run(world, {"square_retail": "real", "doordash": "mock"})
    got = _view(world, run_id)
    assert got["seller"] == "Square Grocery (sandbox)"
    assert got["courier"] == "DoorDash Drive (simulated)"


@db
def test_run_view_mock_square_is_simulated(world: str) -> None:
    run_id = _run(world, {"square_retail": "mock", "doordash": "mock"})
    got = _view(world, run_id)
    assert got["seller"] == "Square Grocery (simulated)"
    assert got["courier"] == "DoorDash Drive (simulated)"


@db
def test_run_view_uses_the_runs_pinned_modes(world: str) -> None:
    """A later panel switch to `real` does not relabel a run pinned to `mock`."""
    run_id = _run(world, {"square_retail": "mock"})
    with psycopg.connect(world) as conn:
        conn.execute(
            "INSERT INTO adapter_modes (vendor, mode, set_by)"
            " VALUES ('square_retail', 'real', 'panel')"
        )
    assert _view(world, run_id)["seller"] == "Square Grocery (simulated)"


@db
def test_run_view_without_an_order_names_no_one(world: str) -> None:
    run_id = uuid.uuid4()
    with psycopg.connect(world) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', '{}', '{}')",
            (run_id, w.HOUSEHOLD_ID),
        )
    got = _view(world, run_id)
    assert (got["seller"], got["courier"]) == (None, None)
