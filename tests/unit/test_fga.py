"""OpenFGA check (architecture §5 step 2, item 54): a timeout counts as a refusal."""

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from services.identity import fga

STORE_ID = "01HVMMBCMGZNT3SED4Z17ECXCA"


class _Settings:
    def __init__(self, url: str) -> None:
        self.openfga_api_url = url
        self.openfga_store_id = STORE_ID


@pytest.fixture
def use_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def _use(url: str) -> None:
        fga._client.cache_clear()
        monkeypatch.setattr(fga, "get_settings", lambda: _Settings(url))

    yield _use
    fga._client.cache_clear()


@pytest.fixture
def fake_openfga() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Allows exactly one tuple: agent:household:h-1 acts_for household:h-1."""
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "body": body})
            key = body["tuple_key"]
            allowed = (key["user"], key["relation"], key["object"]) == (
                "agent:household:h-1",
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


def test_allowed_relation(use_url: Any, fake_openfga: tuple[str, list[dict[str, Any]]]) -> None:
    url, seen = fake_openfga
    use_url(url)
    assert fga.check("agent:household:h-1", "acts_for", "household:h-1") is True
    assert seen[0]["path"] == f"/stores/{STORE_ID}/check"


def test_missing_relation_is_refused(
    use_url: Any, fake_openfga: tuple[str, list[dict[str, Any]]]
) -> None:
    url, _ = fake_openfga
    use_url(url)
    assert fga.check("agent:grocery:mock-us", "acts_for", "household:h-1") is False


def test_timeout_is_a_refusal(use_url: Any) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    held: list[socket.socket] = []
    threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True).start()
    use_url(f"http://127.0.0.1:{listener.getsockname()[1]}")
    started = time.monotonic()
    try:
        assert fga.check("agent:household:h-1", "acts_for", "household:h-1") is False
        elapsed = time.monotonic() - started
    finally:
        for conn in held:
            conn.close()
        listener.close()
    assert fga.TIMEOUT_MILLISEC / 1000 - 0.1 <= elapsed < fga.TIMEOUT_MILLISEC / 1000 + 1
