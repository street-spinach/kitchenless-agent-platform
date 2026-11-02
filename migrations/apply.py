"""Apply pending SQL migrations in filename order. Migrations are additive only (CLAUDE.md)."""

import os
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent
# Arbitrary constant so concurrent runners serialize instead of racing.
LOCK_ID = 7_205_001


def pending(available: list[str], applied: set[str]) -> list[str]:
    return sorted(name for name in available if name not in applied)


def apply(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    available = [p.name for p in migrations_dir.glob("*.sql")]
    done: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}
        for name in pending(available, applied):
            with conn.transaction():
                conn.execute((migrations_dir / name).read_text())
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (name,))
            done.append(name)
    return done


def main() -> None:
    from migrations import checkpointer  # imported here: checkpointer imports LOCK_ID from us

    done = apply(os.environ["DATABASE_URL"])
    print(f"Applied {len(done)} migration(s): {', '.join(done) or 'none pending'}")
    checkpointer.main()


if __name__ == "__main__":
    main()
