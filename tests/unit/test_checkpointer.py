"""LangGraph checkpointer schema setup (architecture items 16 and 17, Q3 on #12, issue #53)."""

import psycopg
import pytest

from migrations import apply, checkpointer

TABLES = {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}


@pytest.mark.db
def test_checkpointer_setup_idempotent(empty_schema_dsn: str) -> None:
    checkpointer.main(empty_schema_dsn)
    checkpointer.main(empty_schema_dsn)

    with psycopg.connect(empty_schema_dsn) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
        versions = conn.execute("SELECT count(*) FROM checkpoint_migrations").fetchone()
        valid = conn.execute(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relnamespace = current_schema()::regnamespace AND i.indisvalid"
        ).fetchall()
        assert checkpointer.invalid_indexes(conn) == []
    assert TABLES <= {r[0] for r in rows}
    assert versions == (10,)
    assert set(checkpointer.INDEXES) <= {r[0] for r in valid}


def test_exits_non_zero_on_invalid_index(monkeypatch: pytest.MonkeyPatch) -> None:
    class Conn:
        def __enter__(self) -> "Conn":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, *args: object) -> None:
            return None

    class Saver:
        def __init__(self, conn: object) -> None:
            pass

        def setup(self) -> None:
            pass

    kwargs: dict = {}
    monkeypatch.setattr(checkpointer.psycopg, "connect", lambda *a, **k: kwargs.update(k) or Conn())
    monkeypatch.setattr(checkpointer, "PostgresSaver", Saver)
    monkeypatch.setattr(checkpointer, "invalid_indexes", lambda conn: ["checkpoints_thread_id_idx"])

    with pytest.raises(SystemExit) as exc:
        checkpointer.main("postgresql://unused")
    assert exc.value.code not in (0, None)
    assert kwargs == {"autocommit": True}


def test_apply_main_runs_checkpointer_after_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(apply, "apply", lambda dsn: calls.append("sql") or [])
    monkeypatch.setattr(checkpointer, "main", lambda: calls.append("checkpointer"))

    apply.main()

    assert calls == ["sql", "checkpointer"]
