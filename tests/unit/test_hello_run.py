import json
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from services.agents.hello import main as hello


class FakeWriter:
    instances: list["FakeWriter"] = []

    def __init__(self, on_behalf_of: str | None) -> None:
        self.run_id = uuid.uuid4()
        self.on_behalf_of = on_behalf_of
        self.rows: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        FakeWriter.instances.append(self)

    def write(self, kind: str, payload: dict[str, Any], **_: Any) -> int:
        self.rows.append((kind, payload))
        return len(self.rows)

    def close(self) -> None:
        self.closed = True


class FakeHandler(hello.Handler):
    writer_factory = FakeWriter


class BrokenHandler(hello.Handler):
    @staticmethod
    def writer_factory(on_behalf_of: str | None) -> Any:
        raise ConnectionError("db down")


def serve(handler: type) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def base() -> Iterator[str]:
    FakeWriter.instances.clear()
    yield from serve(FakeHandler)


def post(url: str, body: bytes) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_run_writes_message_decision_outcome(base: str) -> None:
    status, body = post(f"{base}/hello/run", b'{"text": "ana", "on_behalf_of": "user-1"}')
    assert status == 200
    [writer] = FakeWriter.instances
    assert body == {"run_id": str(writer.run_id)}
    assert [kind for kind, _ in writer.rows] == ["message", "decision", "outcome"]
    assert writer.rows[0][1] == {"direction": "in", "text": "ana"}
    assert writer.on_behalf_of == "user-1"
    assert writer.closed


def test_run_rejects_invalid_json(base: str) -> None:
    assert post(f"{base}/hello/run", b"{not json")[0] == 400
    assert FakeWriter.instances == []


def test_unknown_post_path_is_404(base: str) -> None:
    assert post(f"{base}/hello/other", b"{}")[0] == 404


def test_run_reports_500_when_traces_cannot_be_written() -> None:
    for url in serve(BrokenHandler):
        status, body = post(f"{url}/hello/run", b"{}")
    assert status == 500
    assert body == {"error": "run failed: ConnectionError"}
