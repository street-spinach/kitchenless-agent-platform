"""Trace writer: one agent_traces row per agent step (ENGINEERING_PROCESS.md section 12.3).

Several agents can write to the same run, so seq is allocated in Postgres under a per-run
advisory lock rather than counted in process.
"""

import uuid
from types import TracebackType
from typing import Any, Self

import psycopg
from psycopg.types.json import Jsonb

KINDS = frozenset({"message", "tool_call", "tool_result", "decision", "approval", "outcome"})

_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))"
_INSERT_SQL = """
INSERT INTO agent_traces
  (run_id, seq, agent_id, on_behalf_of, kind, payload, tokens_in, tokens_out)
SELECT %(run_id)s, COALESCE(MAX(seq), 0) + 1, %(agent_id)s, %(on_behalf_of)s, %(kind)s,
       %(payload)s, %(tokens_in)s, %(tokens_out)s
FROM agent_traces WHERE run_id = %(run_id)s
RETURNING seq
"""


def validate(kind: str, payload: Any) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown trace kind {kind!r}; expected one of {sorted(KINDS)}")
    if not isinstance(payload, dict):
        raise TypeError("trace payload must be a dict")


class TraceWriter:
    def __init__(
        self,
        dsn: str,
        agent_id: str,
        run_id: uuid.UUID | None = None,
        on_behalf_of: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.run_id = run_id or uuid.uuid4()
        self.on_behalf_of = on_behalf_of
        self._conn = psycopg.connect(dsn)

    def write(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> int:
        """Write one step and return its seq within the run."""
        validate(kind, payload)
        with self._conn.transaction():
            self._conn.execute(_LOCK_SQL, (str(self.run_id),))
            row = self._conn.execute(
                _INSERT_SQL,
                {
                    "run_id": self.run_id,
                    "agent_id": self.agent_id,
                    "on_behalf_of": self.on_behalf_of,
                    "kind": kind,
                    "payload": Jsonb(payload),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                },
            ).fetchone()
        assert row is not None
        return row[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def read_trace(dsn: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Return a run's rows in seq order, as dicts keyed by column name."""
    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT * FROM agent_traces WHERE run_id = %s ORDER BY seq", (run_id,))
        return cur.fetchall()
