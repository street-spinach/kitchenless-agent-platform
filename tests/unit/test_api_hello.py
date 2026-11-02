"""`POST /hello/run` in `api` and `hello.start_run` (architecture items 31 and 32; issue #75)."""

import uuid
from typing import Any

import pytest
from starlette.testclient import TestClient

from services.agents.hello import main as hello
from services.api import main as api
from tests.unit.test_hello_run import FakeWriter


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    seen: list[tuple[str, str | None]] = []
    run_id = uuid.UUID("44444444-4444-4444-8444-444444444444")

    def fake_start_run(text: str, on_behalf_of: str | None = None) -> uuid.UUID:
        seen.append((text, on_behalf_of))
        return run_id

    monkeypatch.setattr(hello, "start_run", fake_start_run)
    return seen


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.create_app())


def test_start_run_writes_three_rows_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWriter.instances.clear()
    monkeypatch.setattr(hello, "real_writer", FakeWriter)

    run_id = hello.start_run("ana", on_behalf_of="user-1")

    [writer] = FakeWriter.instances
    assert run_id == writer.run_id
    assert [kind for kind, _ in writer.rows] == ["message", "decision", "outcome"]
    assert writer.rows[0][1] == {"direction": "in", "text": "ana"}
    assert writer.rows[2][1] == {"status": "ok"}
    assert writer.on_behalf_of == "user-1"
    assert writer.closed


def test_start_run_closes_the_writer_when_a_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingWriter(FakeWriter):
        def write(self, kind: str, payload: dict[str, Any], **_: Any) -> int:
            raise ConnectionError("db down")

    FakeWriter.instances.clear()
    monkeypatch.setattr(hello, "real_writer", FailingWriter)

    with pytest.raises(ConnectionError):
        hello.start_run("ana")
    [writer] = FakeWriter.instances
    assert writer.closed


def test_hello_run_returns_the_run_id(client: TestClient, calls: list) -> None:
    resp = client.post("/hello/run", json={"text": "smoke", "on_behalf_of": "smoke-test"})

    assert resp.status_code == 200
    assert resp.json() == {"run_id": "44444444-4444-4444-8444-444444444444"}
    assert calls == [("smoke", "smoke-test")]


def test_hello_run_defaults_with_an_empty_body(client: TestClient, calls: list) -> None:
    assert client.post("/hello/run").status_code == 200
    assert calls == [("world", None)]


@pytest.mark.parametrize(
    "body",
    [b"{not json", b"[1, 2]", b'{"on_behalf_of": 7}'],
    ids=["invalid-json", "not-an-object", "non-string-on-behalf-of"],
)
def test_hello_run_rejects_bad_bodies(client: TestClient, calls: list, body: bytes) -> None:
    resp = client.post("/hello/run", content=body)

    assert resp.status_code == 400
    assert calls == []


def test_hello_run_reports_a_failed_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(text: str, on_behalf_of: str | None = None) -> uuid.UUID:
        raise ConnectionError("db down")

    monkeypatch.setattr(hello, "start_run", broken)

    resp = client.post("/hello/run", json={"text": "x"})

    assert resp.status_code == 500
    assert resp.json() == {"error": "run failed: ConnectionError"}


def test_hello_run_is_post_only(client: TestClient, calls: list) -> None:
    assert client.get("/hello/run").status_code == 405
    assert calls == []
