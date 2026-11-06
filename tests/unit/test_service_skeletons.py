import pytest
from starlette.testclient import TestClient

from services.agents import host
from services.api import main as api
from services.mcp import app as mcp
from tests.unit import model_fakes


def test_api_health() -> None:
    resp = TestClient(api.create_app()).get("/health")
    assert (resp.status_code, resp.json()) == (200, {"status": "ok", "service": "api"})


def test_mcp_health() -> None:
    resp = TestClient(mcp.create_app()).get("/health")
    assert (resp.status_code, resp.json()) == (200, {"status": "ok", "service": "mcp"})


@pytest.mark.parametrize("reachable", [True, False])
def test_agents_health_reports_postgres(reachable: bool) -> None:
    async def check_db() -> bool:
        return reachable

    async def no_backlog() -> None:
        return None

    app = host.create_app(check_db, settings=model_fakes.settings(), get_backlog=no_backlog)
    resp = TestClient(app).get("/health")
    assert (resp.status_code, resp.json()) == (
        200,
        {
            "status": "ok",
            "service": "agents",
            "postgres": reachable,
            "cards": 0,
            "backlog": None,
            "model_mode": "stub",
            "model_name": "muse-spark",
            "model_business_name": "Muse Glimmer",
            "token_source": "mock",
        },
    )


def test_agents_postgres_unreachable_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = model_fakes.settings(database_url=None)
    monkeypatch.setattr(host, "get_settings", lambda: settings)
    resp = TestClient(host.create_app()).get("/health")
    assert resp.json()["postgres"] is False


def test_service_ports() -> None:
    assert (api.PORT, mcp.PORT, host.PORT) == (8000, 8002, 8003)


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/app", "/app/orders/1"])
def test_reserved_routes_501(method: str, path: str) -> None:
    resp = TestClient(api.create_app()).request(method, path)
    assert resp.status_code == 501
    assert resp.json() == {"owner": "experience design"}


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/me", "/me/profile"])
def test_me_no_longer_reserved(method: str, path: str) -> None:
    # identity-authz I-A14: `me.py` serves `/me/*`, so nothing there answers 501.
    resp = TestClient(api.create_app()).request(method, path, follow_redirects=False)
    assert resp.status_code != 501


def test_unreserved_prefix_is_404() -> None:
    assert TestClient(api.create_app()).get("/application").status_code == 404
