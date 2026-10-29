import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from services.agents.hello import main as hello
from services.mocks import panel


@pytest.fixture(params=[(hello.Handler, "hello"), (panel.Handler, "mocks")], ids=["hello", "mocks"])
def server(request: pytest.FixtureRequest) -> Iterator[tuple[str, str]]:
    handler, name = request.param
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", name
    httpd.shutdown()
    httpd.server_close()


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_health_reports_ok(server: tuple[str, str]) -> None:
    base, name = server
    assert get(f"{base}/health") == (200, {"status": "ok", "service": name})


def test_unknown_path_is_404(server: tuple[str, str]) -> None:
    base, _ = server
    status, _ = get(f"{base}/nope")
    assert status == 404


def test_hello_reports_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "mock")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), hello.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert get(f"http://127.0.0.1:{httpd.server_port}/hello") == (
            200,
            {"message": "hello", "mode": "mock"},
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
