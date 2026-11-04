"""Grant and outage failures in the agent host (identity-authz I28, §11, §12; issue #126).

A household agent's `GrantError` ends the run with its reason. A consumer agent's skips that
step, and the household stops waiting for that member. A `DependencyUnavailable` is an
ordinary step failure, and after the last attempt its reason replaces `poison_message`.
"""

import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.agents import host
from services.common import consumer as c
from services.common import runs
from services.common.directory import AgentCard
from services.identity import grants
from services.identity.errors import (
    GrantError,
    GrantExpired,
    GrantMissing,
    GrantRevoked,
    IdentityUnavailable,
)
from tests.unit import identity_world as iw
from tests.unit.test_consumer import GROCERY, HOUSEHOLD, World, step_once

pytestmark = pytest.mark.db

CONSUMER = "consumer:us-1.2"
WHATSAPP = "channel:whatsapp"


class IdentityRun(World):
    """`World` with a member, their consumer agent, the WhatsApp channel and the household
    agent's grant held by an admin."""

    def __init__(self, pg: str) -> None:
        super().__init__(pg)
        self.admin = uuid.uuid4()
        self.member = uuid.uuid4()
        with psycopg.connect(pg) as conn:
            for user, role in ((self.admin, "admin"), (self.member, "member")):
                conn.execute(
                    "INSERT INTO users (user_id, display_name, keycloak_sub)"
                    " VALUES (%s, 'Person', %s)",
                    (user, str(uuid.uuid4())),
                )
                conn.execute(
                    "INSERT INTO household_members (household_id, user_id, role)"
                    " VALUES ('us-1', %s, %s)",
                    (user, role),
                )
            for agent_id, cls, owner, process in (
                (CONSUMER, "consumer", f"user:{self.member}", "agents"),
                (WHATSAPP, "channel", "system", "api"),
            ):
                conn.execute(
                    "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                    " VALUES (%s, %s, %s, 'us', %s) ON CONFLICT DO NOTHING",
                    (agent_id, cls, owner, process),
                )
            conn.execute(
                "UPDATE runs SET trigger = %s WHERE run_id = %s",
                (Jsonb({"source": "test", "user_id": str(self.admin)}), self.run_id),
            )
            self.grant_id = iw.add_grant(conn, HOUSEHOLD, iw.Person(self.admin, "unused"), "us-1")

    def request(self) -> uuid.UUID:
        return self.insert(
            type="constraints_request",
            to_agent=CONSUMER,
            body={"order_id": str(self.order_id), "user_id": str(self.member)},
        )

    def answer(self) -> uuid.UUID:
        return self.insert(
            type="constraints",
            from_agent=CONSUMER,
            to_agent=HOUSEHOLD,
            body={"order_id": str(self.order_id), "allergens": [], "diets": [], "dislikes": []},
        )

    def run(self) -> dict[str, Any]:
        return self.query("SELECT status, reason FROM runs WHERE run_id = %s", (self.run_id,))[0]

    def order_state(self) -> str:
        rows = self.query("SELECT state FROM orders WHERE order_id = %s", (self.order_id,))
        return rows[0]["state"]

    def templates(self) -> list[str]:
        rows = self.query(
            "SELECT body->>'template' AS t FROM agent_messages"
            " WHERE run_id = %s AND type = 'whatsapp_out'",
            (self.run_id,),
        )
        return [r["t"] for r in rows]

    def decisions(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT agent_id, payload FROM agent_traces WHERE run_id = %s AND kind = 'decision'",
            (self.run_id,),
        )
        return rows


def _card(agent_id: str, cls: str) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        agent_class=cls,
        owner_ref="user:x",
        region="us",
        zone_ids=("us-oh-45202",),
        currency="USD",
        host_process="agents",
        vendor=None,
    )


def _dispatch(error: Callable[[], Exception]) -> host.Dispatcher:
    """A dispatcher whose every handler raises `error()`."""

    async def fail(msg: c.Inbound, card: AgentCard) -> c.StepResult:
        raise error()

    cards = [
        _card(CONSUMER, "consumer"),
        _card(HOUSEHOLD, "household"),
        _card(GROCERY, "grocery"),
    ]
    return host.Dispatcher(cards, {"consumer": fail, "household": fail, "grocery": fail})


@pytest.mark.parametrize("error", [GrantMissing, GrantRevoked, GrantExpired])
def test_consumer_grant_missing_skips_step(pg: str, error: type[GrantError]) -> None:
    w = IdentityRun(pg)
    mid = w.request()
    step_once(pg, _dispatch(error), finish_run=runs.finish)
    assert w.message(mid)["status"] == "processed"
    [reply] = w.query("SELECT * FROM agent_messages WHERE type = 'constraints'")
    assert (reply["from_agent"], reply["to_agent"]) == (CONSUMER, HOUSEHOLD)
    assert reply["causation_id"] == mid
    # Nothing of the member's is sent.
    assert reply["body"] == {
        "order_id": str(w.order_id),
        "allergens": [],
        "diets": [],
        "dislikes": [],
    }
    [note] = w.decisions()
    assert note["agent_id"] == CONSUMER
    assert note["payload"]["explanation"] == "member without consent: preferences skipped"


def test_consumer_revoke_does_not_end_household_run(pg: str) -> None:
    w = IdentityRun(pg)
    w.request()
    step_once(pg, _dispatch(GrantRevoked), finish_run=runs.finish)
    assert w.run() == {"status": "running", "reason": None}
    assert w.templates() == []


def test_consumer_grant_error_in_own_chat_ends_run(pg: str) -> None:
    """Not a household run's step: there is nothing to continue, so it is not retried."""
    w = IdentityRun(pg)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE runs SET trigger = %s WHERE run_id = %s",
            (Jsonb({"source": "test", "user_id": str(w.member)}), w.run_id),
        )
    mid = w.insert(
        type="user_message",
        from_agent="channel:whatsapp",
        to_agent=CONSUMER,
        body={"inbound_id": str(uuid.uuid4())},
    )
    step_once(pg, _dispatch(GrantRevoked), finish_run=runs.finish)
    assert w.message(mid)["status"] == "processed"
    assert w.run() == {"status": "error", "reason": "grant_revoked"}


def test_household_grant_revoked_ends_run(pg: str) -> None:
    w = IdentityRun(pg)
    mid = w.answer()
    step_once(pg, _dispatch(GrantRevoked), finish_run=runs.finish)
    assert w.message(mid)["status"] == "processed"
    assert w.run() == {"status": "error", "reason": "grant_revoked"}
    assert w.order_state() == "failed"
    # The grant here is still `active`, as when Keycloak's refusal is the first sign.
    assert "retry_prompt" not in w.templates()


def test_person_revoke_sends_no_second_text(pg: str) -> None:
    """`after_revoke`'s notice already carries the Renew button."""
    w = IdentityRun(pg)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE agent_grants SET status = 'revoked', revoke_reason = 'user'"
            " WHERE grant_id = %s",
            (w.grant_id,),
        )
    w.answer()
    step_once(pg, _dispatch(GrantRevoked), finish_run=runs.finish)
    assert w.run() == {"status": "error", "reason": "grant_revoked"}
    assert w.templates() == []


def test_keycloak_refused_grant_gets_renew_text(pg: str) -> None:
    """`mark_refused` sends no notice, so the run's end must tell the holder."""
    w = IdentityRun(pg)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE agent_grants SET status = 'revoked', revoke_reason = %s WHERE grant_id = %s",
            (grants.KEYCLOAK_REFUSED, w.grant_id),
        )
    w.answer()
    step_once(pg, _dispatch(GrantRevoked), finish_run=runs.finish)
    assert w.templates() == ["consent_renewal"]


def test_business_agent_grant_error_stays_a_step_failure(pg: str) -> None:
    w = IdentityRun(pg)
    mid = w.insert()  # `accept` to the seller
    step_once(pg, _dispatch(GrantRevoked), finish_run=runs.finish)
    assert w.message(mid)["status"] == "pending"
    assert w.run()["status"] == "running"


def test_grant_expires_during_approval_ends_run(pg: str) -> None:
    """I-CUJ 6: the wait outlives the grant; the resumed step's token is refused."""
    w = IdentityRun(pg)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE agent_grants SET expires_at = now() - interval '1 minute' WHERE grant_id = %s",
            (w.grant_id,),
        )
        conn.execute("UPDATE runs SET status = 'awaiting_approval' WHERE run_id = %s", (w.run_id,))

    async def resume(msg: c.Inbound, card: AgentCard) -> c.StepResult:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            try:
                await grants.load_active(conn, HOUSEHOLD)
            finally:
                await conn.commit()  # as `mcp_client._token`: keeps the expiry mark
        return c.StepResult()

    w.answer()
    dispatch = host.Dispatcher([_card(HOUSEHOLD, "household")], {"household": resume})
    step_once(pg, dispatch, finish_run=runs.finish)
    assert w.run() == {"status": "error", "reason": "grant_expired"}
    [grant] = w.query("SELECT status FROM agent_grants WHERE grant_id = %s", (w.grant_id,))
    assert grant["status"] == "expired"
    [renew] = w.query("SELECT body FROM agent_messages WHERE type = 'whatsapp_out'")
    assert renew["body"]["template"] == "consent_renewal"
    assert renew["body"]["user_id"] == str(w.admin)
    assert renew["body"]["ref"] == {"kind": "grant", "id": str(w.grant_id)}


def test_identity_outage_30s_absorbed_by_retries(pg: str) -> None:
    """I-CUJ 8: 2 + 4 + 8 + 16 s of backoff outlast 30 s; the fifth attempt succeeds."""
    w = IdentityRun(pg)
    mid = w.answer()
    seen: list[int] = []

    async def handle(msg: c.Inbound) -> c.StepResult:
        seen.append(msg.attempts)
        if msg.attempts < c.MAX_ATTEMPTS:
            raise IdentityUnavailable()
        return c.StepResult()

    for _ in range(c.MAX_ATTEMPTS - 1):
        step_once(pg, handle, finish_run=runs.finish)
        assert w.message(mid)["last_error"] == "identity_unavailable"
        w.make_due(mid)
    step_once(pg, handle, finish_run=runs.finish)
    assert seen == [1, 2, 3, 4, 5]
    assert w.message(mid)["status"] == "processed"
    assert w.run()["status"] == "running"


def test_last_attempt_reason_is_identity_unavailable(pg: str) -> None:
    w = IdentityRun(pg)
    mid = w.answer()

    async def handle(msg: c.Inbound) -> c.StepResult:
        raise IdentityUnavailable()

    for _ in range(c.MAX_ATTEMPTS):
        step_once(pg, handle, finish_run=runs.finish)
        w.make_due(mid)
    assert w.message(mid)["status"] == "failed"
    assert w.message(mid)["last_error"] == "identity_unavailable"
    assert w.run() == {"status": "error", "reason": "identity_unavailable"}
    # The retry text, not the renew text (I-CUJ 8).
    assert w.templates() == ["retry_prompt"]
