"""Placeholder agent service so the stack has something to deploy, health-check and trace."""

import json
import os
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from services.agents.common.trace import TraceWriter

AGENT_ID = "hello"


def real_writer(on_behalf_of: str | None) -> TraceWriter:
    return TraceWriter(os.environ["DATABASE_URL"], AGENT_ID, on_behalf_of=on_behalf_of)


def run(writer: Any, text: str) -> None:
    """One hello run: receive a message, decide on a reply, record the outcome."""
    writer.write("message", {"direction": "in", "text": text})
    writer.write("decision", {"reply": f"hello, {text}"})
    writer.write("outcome", {"status": "ok"})


class Handler(BaseHTTPRequestHandler):
    writer_factory: Callable[[str | None], Any] = staticmethod(real_writer)

    def do_GET(self) -> None:
        if self.path == "/health":
            # Rollback drill (#6): deliberately unhealthy. Revert after the drill.
            self._send(503, {"status": "drill", "service": "hello"})
        elif self.path.startswith("/hello"):
            self._send(200, {"message": "hello", "mode": os.environ.get("MODE", "mock")})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/hello/run":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": "invalid JSON body"})
            return
        try:
            writer = self.writer_factory(body.get("on_behalf_of"))
            try:
                run(writer, str(body.get("text", "world")))
            finally:
                writer.close()
        except Exception as err:
            self._send(500, {"error": f"run failed: {type(err).__name__}"})
            return
        self._send(200, {"run_id": str(writer.run_id)})

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
