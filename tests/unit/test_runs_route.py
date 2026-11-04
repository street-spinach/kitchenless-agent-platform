"""`POST /runs` and `GET /runs/{run_id}` (architecture item 36, agent-protocol A21, P32;
issue #40)."""

import uuid

import pytest
from starlette.testclient import TestClient

from services.api import main as api
from services.api.routes.runs import ALLOWED_OVERRIDES
from services.common import ids
from tests.unit import api_world as w

db = pytest.mark.db

P32_KEYS = {
    "OFFER_TTL_SECONDS": 5,
    "SHORTFALL_REPLY_SECONDS": 5,
    "STUB_ACCEPT_DELAY_ROUNDS": 1,
    "FAIL_FIRST_ATTEMPT": "household",
    "CRASH_BEFORE_FINISH": True,
    "STUB_SHORTFALL_UNSAFE_SUBSTITUTE": True,
    "STUB_NEGOTIATE_HOLD_SECONDS": 3,
}


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)


@pytest.fixture
def world(pg: str) -> str:
    w.seed(pg)
    return pg


def _post(pg: str, **body: object):
    body.setdefault("scenario", "weekly_grocery_order")
    body.setdefault("household_id", w.HOUSEHOLD_ID)
    body.setdefault("input", "Weekly shop please")
    return w.client(pg).post("/runs", json=body)


@db
def test_post_runs_stores_allowed_overrides_including_p32(world: str) -> None:
    overrides = {"APPROVAL_TTL_SECONDS": 10, "STUB_FAIL_NODE": "build_list", **P32_KEYS}
    assert set(overrides) == ALLOWED_OVERRIDES - {"STUB_OUT_OF_BOUNDS", "STUB_DUPLICATE_MESSAGE"}
    resp = _post(world, overrides=overrides)
    assert resp.status_code == 200
    run_id = uuid.UUID(resp.json()["run_id"])

    [run] = w.query(world, "SELECT * FROM runs WHERE run_id = %s", run_id)
    assert (run["kind"], run["household_id"]) == ("weekly", w.HOUSEHOLD_ID)
    assert run["trigger"]["overrides"] == overrides
    # Simulated from the household's first member: the admin.
    assert run["trigger"]["user_id"] == str(w.ADMIN)
    assert run["trigger"]["inbound_id"] == str(ids.inbound_id("runs", str(run_id)))
    [msg] = w.query(world, "SELECT * FROM agent_messages WHERE run_id = %s", run_id)
    assert (msg["type"], msg["to_agent"]) == ("user_message", w.ADMIN_CONSUMER)


@db
def test_post_runs_refuses_unknown_override(world: str) -> None:
    resp = _post(world, overrides={"APPROVAL_TTL_SECONDS": 10, "MODE": "real"})
    assert resp.status_code == 400
    assert "MODE" in resp.json()["error"]
    assert w.query(world, "SELECT * FROM runs") == []
    assert w.query(world, "SELECT * FROM inbound_messages") == []


@db
def test_post_runs_refuses_live_household(world: str) -> None:
    resp = _post(world, household_id=w.LIVE_HOUSEHOLD_ID)
    assert resp.status_code == 403
    assert w.query(world, "SELECT * FROM runs") == []


@db
def test_post_runs_dinner_scenario_and_get_run(world: str) -> None:
    resp = _post(world, scenario="busy_night_dinner", input="Dinner tonight")
    run_id = resp.json()["run_id"]
    [run] = w.query(world, "SELECT kind FROM runs WHERE run_id = %s", uuid.UUID(run_id))
    assert run["kind"] == "dinner"

    got = w.client(world).get(f"/runs/{run_id}")
    assert got.json() == {
        "run_id": run_id,
        "status": "running",
        "reason": None,
        "summary": None,
        "seller": None,
        "courier": None,
    }
    assert w.client(world).get(f"/runs/{uuid.uuid4()}").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"scenario": "unknown", "household_id": "h", "input": "hi"},
        {"scenario": "weekly_grocery_order", "household_id": "h", "input": ""},
        {
            "scenario": "weekly_grocery_order",
            "household_id": "h",
            "input": "hi",
            "overrides": {"MODE": "real"},
        },
        {
            "scenario": "weekly_grocery_order",
            "household_id": "h",
            "input": "hi",
            "overrides": {"APPROVAL_TTL_SECONDS": {"nested": 1}},
        },
    ],
)
def test_post_runs_validates_before_touching_the_database(body: dict) -> None:
    def no_db() -> None:
        raise AssertionError("the database must not be used")

    resp = TestClient(api.create_app(no_db)).post("/runs", json=body)  # type: ignore[arg-type]
    assert resp.status_code == 400
