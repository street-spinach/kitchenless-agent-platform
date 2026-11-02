"""`X-Hub-Signature-256` check for the WhatsApp webhook (architecture item 39, NFR webhooks).

Meta signs each webhook body with the app secret: the header is `sha256=<hex HMAC-SHA256>`.
"""

import hashlib
import hmac

__all__ = ["PREFIX", "verify"]

PREFIX = "sha256="


def verify(body: bytes, header: str, secret: str) -> bool:
    """True only if `header` is the HMAC-SHA256 of `body` under `secret`.

    An empty secret or header, or a header without the `sha256=` prefix, is refused
    (fails closed).
    """
    if not secret or not header or not header.startswith(PREFIX):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len(PREFIX) :].strip().lower())
