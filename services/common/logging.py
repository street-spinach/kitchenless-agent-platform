"""JSON-lines logging to stdout (architecture item 7).

`configure(service_name)` installs the handler once per process. `log_context(run_id=...,
agent_id=...)` sets a context variable whose values appear on every log line inside it.
"""

import contextlib
import contextvars
import datetime
import json
import logging
import os
import sys
from collections.abc import Iterator
from typing import Any

_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "log_context", default=None
)


@contextlib.contextmanager
def log_context(*, run_id: Any = None, agent_id: str | None = None) -> Iterator[None]:
    """Attach `run_id` and `agent_id` to every log line written inside the block."""
    current = dict(_context.get() or {})
    if run_id is not None:
        current["run_id"] = str(run_id)
    if agent_id is not None:
        current["agent_id"] = agent_id
    token = _context.set(current)
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.datetime.fromtimestamp(record.created, datetime.UTC).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "msg": record.getMessage(),
            **(_context.get() or {}),
        }
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure(service_name: str, level: str | None = None) -> None:
    """Send every log record to stdout as one JSON object per line."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel((level or os.environ.get("LOG_LEVEL") or "INFO").upper())
