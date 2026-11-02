"""`audit.record` (identity-authz §14 and I10)."""

import asyncio
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from services.identity import audit

_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "0010_identity.sql"


@pytest.mark.parametrize(
    "detail",
    [
        {"refresh_token": "x"},
        {"Client_Secret": "x"},
        {"magicLink": "x"},
        {"PASSWORD": "x"},
        {"outer": {"inner": {"AccessToken": "x"}}},
        {"items": [{"ok": 1}, {"nested": {"Link_URL": "x"}}]},
    ],
)
def test_detail_rejects_secret_keys(detail: dict[str, Any]) -> None:
    class NoConn:
        def execute(self, *a: Any) -> None:
            raise AssertionError("nothing may be written")

    with pytest.raises(ValueError, match="looks like a secret"):
        audit.record(NoConn(), "fga_reconciled", detail=detail)  # type: ignore[call-overload]


def test_detail_secret_values_are_not_echoed() -> None:
    with pytest.raises(ValueError) as exc:
        audit._params("fga_reconciled", *[None] * 7, {"token": "s3cr3t-value"})
    assert "s3cr3t-value" not in str(exc.value)


def test_unknown_kind_refused() -> None:
    with pytest.raises(ValueError, match="unknown audit kind"):
        audit._params("made_up", *[None] * 7, None)


def test_kinds_match_migration() -> None:
    sql = _MIGRATION.read_text()
    block = sql[sql.index("CREATE TABLE audit_events") :]
    check = block[block.index("CHECK (kind IN") : block.index("))")]
    assert set(re.findall(r"'([a-z_]+)'", check)) == audit.KINDS


@pytest.mark.db
@pytest.mark.parametrize("kind", sorted(audit.KINDS))
def test_grant_events_audited(pg: str, grant: Callable[..., uuid.UUID], kind: str) -> None:
    """Every I11 kind is recorded with its ids and detail. The writers named in §14 land
    with their build steps and extend this test there."""
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        grant_id = grant(conn)
        holder = conn.execute(
            "SELECT user_id FROM agent_grants WHERE grant_id = %s", (grant_id,)
        ).fetchone()[0]
        audit.record(
            conn,
            kind,
            actor_user_id=holder,
            subject_user_id=holder,
            agent_id="household:h-1",
            household_id="h-1",
            grant_id=grant_id,
            run_id=run_id,
            detail={"via": "test", "nested": {"n": 1}},
        )
        row = conn.execute(
            "SELECT kind, actor_user_id, subject_user_id, agent_id, household_id, org_id,"
            " grant_id, run_id, detail FROM audit_events"
        ).fetchone()
    assert row == (
        kind,
        holder,
        holder,
        "household:h-1",
        "h-1",
        None,
        grant_id,
        run_id,
        {"via": "test", "nested": {"n": 1}},
    )


@pytest.mark.db
def test_record_async_connection(pg: str) -> None:
    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            await audit.record(conn, "band_changed", household_id="h-1", detail={"to": 100})

    asyncio.run(go())
    with psycopg.connect(pg) as conn:
        rows = conn.execute("SELECT kind, household_id, detail FROM audit_events").fetchall()
    assert rows == [("band_changed", "h-1", {"to": 100})]
