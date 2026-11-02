"""Create the LangGraph checkpointer tables with the pinned PostgresSaver.setup().

The only exception to SQL-only migrations (CLAUDE.md, architecture items 16 and 71). setup()
in langgraph-checkpoint-postgres 3.2.0 runs CREATE INDEX CONCURRENTLY, so it needs an
autocommit connection with no pipeline and must never run inside a transaction (Q3, #12).
"""

import os

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver

from migrations.apply import LOCK_ID

# Built CONCURRENTLY by setup(); an interrupted build can leave one INVALID.
INDEXES = (
    "checkpoints_thread_id_idx",
    "checkpoint_blobs_thread_id_idx",
    "checkpoint_writes_thread_id_idx",
)


def invalid_indexes(conn: psycopg.Connection) -> list[str]:
    """Names in INDEXES that are missing or not valid in the current search_path."""
    bad = []
    for name in INDEXES:
        row = conn.execute(
            "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(%s)",
            (name,),
        ).fetchone()
        if row is None or not row[0]:
            bad.append(name)
    return bad


def main(dsn: str | None = None) -> None:
    dsn = dsn or os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        # setup() has no lock of its own; share apply.py's so concurrent runs serialize.
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        PostgresSaver(conn).setup()
        bad = invalid_indexes(conn)
    if bad:
        raise SystemExit(f"Checkpointer setup left invalid indexes: {', '.join(bad)}")
    print("Checkpointer schema is up to date.")


if __name__ == "__main__":
    main()
