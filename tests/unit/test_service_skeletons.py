import pytest
from starlette.testclient import TestClient

from services.agents import host
from services.api import main as api
from services.mcp import app as mcp


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

    resp = TestClient(host.create_app(check_db)).get("/health")
    assert (resp.status_code, resp.json()) == (
        200,
        {"status": "ok", "service": "agents", "postgres": reachable},
    )


def test_agents_postgres_unreachable_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host, "get_settings", lambda: type("S", (), {"database_url": None})())
    resp = TestClient(host.create_app()).get("/health")
    assert resp.json()["postgres"] is False


def test_service_ports() -> None:
    assert (api.PORT, mcp.PORT, host.PORT) == (8000, 8002, 8003)


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/app", "/app/orders/1", "/me", "/me/profile"])
def test_reserved_routes_501(method: str, path: str) -> None:
    resp = TestClient(api.create_app()).request(method, path)
    assert resp.status_code == 501
    assert resp.json() == {"owner": "experience design"}


def test_unreserved_prefix_is_404() -> None:
    assert TestClient(api.create_app()).get("/application").status_code == 404
