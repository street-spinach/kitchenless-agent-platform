"""Live health checks. Run inside the Compose network via `make smoke` (testrunner)."""

import os
import socket
import ssl
import urllib.request

import pytest

DOMAIN = os.environ.get("DOMAIN", "localhost")


def http_ok(url: str) -> bool:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status == 200


@pytest.mark.parametrize(
    "url",
    [
        "http://hello:8000/health",
        "http://mocks:8001/health",
        "http://api:8000/health",
        "http://mcp:8002/health",
        "http://agents:8003/health",
        "http://openfga:8080/healthz",
        "http://keycloak:9000/auth/health/ready",
        "http://neo4j:7474",
    ],
)
def test_service_health(url: str) -> None:
    assert http_ok(url)


@pytest.mark.parametrize("host,port", [("postgres", 5432)])
def test_tcp_reachable(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=5):
        pass


def caddy_status_line(method: str, path: str) -> bytes:
    """Send one request to Caddy over HTTPS and return the response's status line."""
    # Connect to Caddy inside the network but present the public domain via SNI and Host.
    # Locally DOMAIN=localhost uses Caddy's internal CA, so certificate checks are skipped there.
    ctx = ssl.create_default_context()
    if DOMAIN == "localhost":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("caddy", 443), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname=DOMAIN) as tls:
            tls.sendall(
                f"{method} {path} HTTP/1.1\r\nHost: {DOMAIN}\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )
            return tls.recv(64).split(b"\r\n", 1)[0]


def test_caddy_serves_health_over_https() -> None:
    assert caddy_status_line("GET", "/health").startswith(b"HTTP/1.1 200")


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/internal/sim/whatsapp"),
        ("POST", "/runs"),
        ("GET", "/runs/any"),
        ("POST", "/mcp"),
        ("GET", "/mcp/grocery"),
        ("POST", "/a2a/grocery-us-1"),
        ("GET", "/a2a/grocery-us-1/.well-known/agent-card.json"),
    ],
)
def test_internal_routes_not_public(method: str, path: str) -> None:
    # These live on api, mcp and agents but must only be reachable inside the Compose network.
    assert caddy_status_line(method, path).startswith(b"HTTP/1.1 404")
