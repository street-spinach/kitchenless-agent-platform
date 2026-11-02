"""OpenFGA check over HTTP (identity-authz I9, architecture item 54 as amended by I-A3).

The timeout test is `test_timeout_raises_authz_unavailable` in test_fga_sync.py: a timeout
now raises `AuthzUnavailable` instead of counting as a refusal.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from services.identity import fga

STORE_ID = "01HVMMBCMGZNT3SED4Z17ECXCA"
MODEL_ID = "01HVMMBCMGZNT3SED4Z17ECXCB"
KEY = "unit-test-preshared-key"


class _Settings:
    def __init__(self, url: str) -> None:
        self.openfga_api_url = url
        self.openfga_preshared_key = KEY


@pytest.fixture
def use_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def _use(url: str) -> None:
        fga._client.cache_clear()
        monkeypatch.setattr(fga, "get_settings", lambda: _Settings(url))
        monkeypatch.setattr(fga, "_ids", lambda: (STORE_ID, MODEL_ID))

    yield _use
    fga._client.cache_clear()


@pytest.fixture
def openfga_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Allows exactly one tuple: agent:household.h-1 acts_for household:h-1."""
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(
                {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
            )
            key = body["tuple_key"]
            allowed = (key["user"], key["relation"], key["object"]) == (
                "agent:household.h-1",
                "acts_for",
                "household:h-1",
            )
            out = json.dumps({"allowed": allowed, "resolution": ""}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


def test_allowed_relation(use_url: Any, openfga_server: tuple[str, list[dict[str, Any]]]) -> None:
    url, seen = openfga_server
    use_url(url)
    assert fga.check("agent:household.h-1", "acts_for", "household:h-1") == (True, MODEL_ID)
    assert seen[0]["path"] == f"/stores/{STORE_ID}/check"
    assert seen[0]["body"]["authorization_model_id"] == MODEL_ID


def test_missing_relation_is_refused(
    use_url: Any, openfga_server: tuple[str, list[dict[str, Any]]]
) -> None:
    url, _ = openfga_server
    use_url(url)
    assert fga.check("agent:grocery.mock-us", "acts_for", "household:h-1") == (False, MODEL_ID)


def test_check_sends_preshared_key(
    use_url: Any, openfga_server: tuple[str, list[dict[str, Any]]]
) -> None:
    url, seen = openfga_server
    use_url(url)
    fga.check("agent:household.h-1", "acts_for", "household:h-1")
    assert seen[0]["auth"] == f"Bearer {KEY}"
