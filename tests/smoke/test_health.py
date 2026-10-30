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
        "http://openfga:8080/healthz",
        "http://keycloak:9000/auth/health/ready",
        "http://neo4j:7474",
    ],
)
def test_service_health(url: str) -> None:
    assert http_ok(url)


@pytest.mark.parametrize("host,port", [("postgres", 5432), ("redis", 6379)])
def test_tcp_reachable(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=5):
        pass


def test_redis_ping() -> None:
    with socket.create_connection(("redis", 6379), timeout=5) as sock:
        sock.sendall(b"PING\r\n")
        assert sock.recv(16) == b"+PONG\r\n"


def test_caddy_serves_health_over_https() -> None:
    # Connect to Caddy inside the network but present the public domain via SNI and Host.
    # Locally DOMAIN=localhost uses Caddy's internal CA, so certificate checks are skipped there.
    ctx = ssl.create_default_context()
    if DOMAIN == "localhost":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("caddy", 443), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname=DOMAIN) as tls:
            tls.sendall(
                f"GET /health HTTP/1.1\r\nHost: {DOMAIN}\r\nConnection: close\r\n\r\n".encode()
            )
            status_line = tls.recv(64).split(b"\r\n", 1)[0]
    assert status_line.startswith(b"HTTP/1.1 200")
