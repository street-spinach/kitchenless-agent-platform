"""The real WhatsApp webhook and its signature check (architecture items 34, 39; issue #73)."""

import contextlib
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from services.api import main as api
from services.api.ingest import Content
from services.api.routes import whatsapp
from services.api.whatsapp import signature
from services.common.config import Settings
from tests.unit import api_world as w

SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
PATH = "/webhooks/whatsapp"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)
    env = {
        "SERVICE_NAME": "migrate",
        "DATABASE_URL": "x",
        "LOG_LEVEL": "info",
        "WHATSAPP_APP_SECRET": SECRET,
        "WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN,
    }
    settings = Settings.from_env(env)
    monkeypatch.setattr(whatsapp, "get_settings", lambda: settings)


@contextlib.asynccontextmanager
async def _no_db() -> AsyncIterator[Any]:
    raise AssertionError("the webhook must not touch the database here")
    yield  # pragma: no cover


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(*messages: dict[str, Any], statuses: list[dict[str, Any]] | None = None) -> bytes:
    value: dict[str, Any] = {"messaging_product": "whatsapp", "messages": list(messages)}
    if statuses is not None:
        value["statuses"] = statuses
    body = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba", "changes": [{"field": "messages", "value": value}]}],
    }
    return json.dumps(body).encode()


def _text(external_id: str, phone: str, text: str) -> dict[str, Any]:
    return {
        "from": phone.removeprefix("+"),
        "id": external_id,
        "type": "text",
        "text": {"body": text},
    }


# --- signature --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=" + "0" * 64,
        _sign(b"another body"),
        _sign(_payload(), secret="wrong-secret"),
        _sign(_payload()).removeprefix("sha256="),
    ],
)
def test_bad_signature_401(header: str | None) -> None:
    body = _payload(_text("wamid.1", w.REAL_PHONE, "do this week's shop"))
    headers = {} if header is None else {whatsapp.SIGNATURE_HEADER: header}
    resp = TestClient(api.create_app(_no_db)).post(PATH, content=body, headers=headers)
    assert resp.status_code == 401


def test_missing_app_secret_refuses_every_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"SERVICE_NAME": "migrate", "DATABASE_URL": "x", "LOG_LEVEL": "info"}
    monkeypatch.setattr(whatsapp, "get_settings", lambda: Settings.from_env(env))
    body = _payload()
    resp = TestClient(api.create_app(_no_db)).post(
        PATH, content=body, headers={whatsapp.SIGNATURE_HEADER: _sign(body, secret="")}
    )
    assert resp.status_code == 401


def test_signature_verify() -> None:
    body = b'{"a": 1}'
    assert signature.verify(body, _sign(body), SECRET)
    assert signature.verify(body, _sign(body).upper().replace("SHA256=", "sha256="), SECRET)
    assert not signature.verify(body, _sign(body), "")
    assert not signature.verify(body, "", SECRET)
    assert not signature.verify(body + b" ", _sign(body), SECRET)


# --- verify challenge -------------------------------------------------------------------


def test_verify_challenge_echoes_challenge() -> None:
    params = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "42"}
    resp = TestClient(api.create_app(_no_db)).get(PATH, params=params)
    assert (resp.status_code, resp.text) == (200, "42")


@pytest.mark.parametrize(
    "params",
    [
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"},
        {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "42"},
        {"hub.mode": "subscribe", "hub.challenge": "42"},
        {},
    ],
)
def test_verify_challenge_refused(params: dict[str, str]) -> None:
    resp = TestClient(api.create_app(_no_db)).get(PATH, params=params)
    assert resp.status_code == 403


# --- message parsing --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"type": "text", "text": {"body": "hi"}}, Content("text", text="hi")),
        (
            {"type": "interactive", "interactive": {"button_reply": {"id": "p1", "title": "OK"}}},
            Content("button", button="p1"),
        ),
        (
            {"type": "button", "button": {"payload": "p2", "text": "OK"}},
            Content("button", button="p2"),
        ),
        ({"type": "image", "image": {"id": "m1"}}, Content("media")),
        ({"type": "audio", "audio": {"id": "m2"}}, Content("media")),
        ({"type": "reaction", "reaction": {"emoji": "x"}}, None),
        ({"type": "text"}, None),
    ],
)
def test_content_of(message: dict[str, Any], expected: Content | None) -> None:
    assert whatsapp.content_of(message) == expected


# --- ingest (db) ------------------------------------------------------------------------


@pytest.mark.db
def test_signed_message_is_ingested_and_statuses_ignored(pg: str) -> None:
    w.seed(pg)
    body = _payload(
        _text("wamid.1", w.REAL_PHONE, "do this week's shop"),
        statuses=[{"id": "wamid.0", "status": "delivered", "recipient_id": "1"}],
    )
    client = TestClient(api.create_app(w.connect_to(pg)))
    headers = {whatsapp.SIGNATURE_HEADER: _sign(body)}
    assert client.post(PATH, content=body, headers=headers).status_code == 200
    assert client.post(PATH, content=body, headers=headers).status_code == 200  # redelivery

    [inbound] = w.query(pg, "SELECT * FROM inbound_messages")
    assert (inbound["channel"], inbound["external_id"]) == ("whatsapp", "wamid.1")
    [run] = w.query(pg, "SELECT run_id, household_id, trigger FROM runs")
    assert run["household_id"] == w.LIVE_HOUSEHOLD_ID
    assert run["trigger"]["text"] == "do this week's shop"
    assert inbound["run_id"] == run["run_id"]


@pytest.mark.db
def test_status_only_callback_writes_nothing(pg: str) -> None:
    w.seed(pg)
    body = _payload(statuses=[{"id": "wamid.0", "status": "read", "recipient_id": "1"}])
    resp = TestClient(api.create_app(w.connect_to(pg))).post(
        PATH, content=body, headers={whatsapp.SIGNATURE_HEADER: _sign(body)}
    )
    assert resp.status_code == 200
    assert w.query(pg, "SELECT * FROM inbound_messages") == []
    assert w.query(pg, "SELECT * FROM runs") == []
