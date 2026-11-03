"""The real WhatsApp webhook (architecture item 34; issue #73).

*   `GET /webhooks/whatsapp`: Meta's verify challenge. `hub.mode=subscribe` with
    `hub.verify_token` equal to `WHATSAPP_VERIFY_TOKEN` echoes `hub.challenge`; anything
    else gets 403.
*   `POST /webhooks/whatsapp`: verifies `X-Hub-Signature-256` with `WHATSAPP_APP_SECRET`
    (401 on failure, before the body is parsed), then `ingest`s each message and passes
    each `value.statuses[]` list to `status.record` (experience §5, EX-I16). Both only
    write rows, so the webhook acknowledges at once.

WhatsApp sends the sender's number as digits without `+`; it becomes E.164 by adding `+`.
"""

import hmac
import json
import logging
from collections.abc import Iterator
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from services.api.ingest import Content, ingest
from services.api.routes.internal import SIM_CHANNEL
from services.api.whatsapp import signature, status
from services.common.config import get_settings

__all__ = ["CHANNEL", "MEDIA_TYPES", "SIGNATURE_HEADER", "content_of", "routes"]

log = logging.getLogger(__name__)

# Real and simulated messages share one `inbound_messages` channel (see internal.py).
CHANNEL = SIM_CHANNEL
SIGNATURE_HEADER = "X-Hub-Signature-256"
# Message types that get the "not supported yet" reply (architecture R9, item 33 step 5).
MEDIA_TYPES = frozenset({"image", "audio", "video", "document", "sticker"})


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def content_of(message: dict[str, Any]) -> Content | None:
    """The `Content` of one webhook message, or `None` for a type `ingest` does not take."""
    kind = message.get("type")
    if kind == "text":
        text = (message.get("text") or {}).get("body")
        return Content("text", text=text) if isinstance(text, str) else None
    if kind == "interactive":
        reply = (message.get("interactive") or {}).get("button_reply") or {}
        payload = _str(reply.get("id"))
        return Content("button", button=payload) if payload else None
    if kind == "button":
        payload = _str((message.get("button") or {}).get("payload"))
        return Content("button", button=payload) if payload else None
    if kind in MEDIA_TYPES:
        return Content("media")
    return None


def _values(payload: object) -> Iterator[dict[str, Any]]:
    """Every `entry[].changes[].value` in a webhook payload. Malformed parts are skipped."""
    if not isinstance(payload, dict):
        return
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = change.get("value") if isinstance(change, dict) else None
            if isinstance(value, dict):
                yield value


def _messages(payload: object) -> Iterator[dict[str, Any]]:
    """Every message in a webhook payload. Malformed parts are skipped."""
    for value in _values(payload):
        for message in value.get("messages") or []:
            if isinstance(message, dict):
                yield message


def _statuses(payload: object) -> list[object]:
    """Every `value.statuses[]` entry in a webhook payload."""
    found: list[object] = []
    for value in _values(payload):
        statuses = value.get("statuses")
        if isinstance(statuses, list):
            found.extend(statuses)
    return found


async def verify_challenge(request: Request) -> Response:
    params = request.query_params
    expected = get_settings().whatsapp_verify_token or ""
    token = params.get("hub.verify_token") or ""
    challenge = params.get("hub.challenge")
    if (
        params.get("hub.mode") != "subscribe"
        or not expected
        or not hmac.compare_digest(token.encode(), expected.encode())
        or challenge is None
    ):
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge)


async def receive(request: Request) -> JSONResponse:
    raw = await request.body()
    secret = get_settings().whatsapp_app_secret or ""
    if not signature.verify(raw, request.headers.get(SIGNATURE_HEADER, ""), secret):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "the body must be JSON"}, status_code=400)

    async with request.app.state.connect() as conn:
        for message in _messages(payload):
            external_id, sender = _str(message.get("id")), _str(message.get("from"))
            content = content_of(message)
            if external_id is None or sender is None or content is None:
                log.info("whatsapp webhook: skipped a %r message", message.get("type"))
                continue
            phone = sender if sender.startswith("+") else "+" + sender
            await ingest(conn, CHANNEL, external_id, phone, content)
        await status.record(conn, _statuses(payload))
    return JSONResponse({"status": "ok"})


routes = [
    Route("/webhooks/whatsapp", verify_challenge, methods=["GET"]),
    Route("/webhooks/whatsapp", receive, methods=["POST"]),
]
