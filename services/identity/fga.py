"""OpenFGA checks and structural tuples (identity-authz §7 and I9, architecture item 54).

*   `check` returns `(allowed, model_id)`. A timeout (2 s) or a connection error raises
    `AuthzUnavailable`: an outage is not a denial (I-G9, I-A3).
*   `write` and `delete` change tuples; `sync_member`, `sync_agent` and `reconcile` derive
    the structural tuples of §7 from Postgres. The model holds structure, not consent
    (I-D19).
*   The store and model ids come from `fga_models` (written by `authz/apply.py`), never from
    configuration. Every request carries the preshared key.

Agent object ids are the agent's Keycloak client id (`ids.keycloak_client_id`): the first
`:` of the `agent_id` is written as `.`, for example `agent:household.us-1`.
"""

import functools
from collections.abc import Iterable
from typing import Any

import httpx
import psycopg

from services.common import ids
from services.common.config import get_settings
from services.identity import audit
from services.identity.errors import AuthzUnavailable
from services.safety.scope import unscoped

__all__ = [
    "AuthzUnavailable",
    "Client",
    "FgaError",
    "Tuple",
    "card_tuples",
    "check",
    "delete",
    "ensure",
    "expected_tuples",
    "member_tuple",
    "reconcile",
    "sync_agent",
    "sync_member",
    "write",
]

TIMEOUT_MILLISEC = 2000
# OpenFGA's default limit on tuples per Write request.
WRITE_CHUNK = 100
READ_PAGE_SIZE = 100

Tuple = tuple[str, str, str]  # (user, relation, object)

# The (object type, relation) pairs that `reconcile` owns (§7 table). `org owner` has no
# source row in Postgres until the owner seed lands (I27), so reconcile leaves it alone.
MANAGED = frozenset(
    {
        ("household", "admin"),
        ("household", "member"),
        ("household", "child"),
        ("household", "acts_for"),
        ("household", "can_deliver_for"),
        ("user", "acts_for"),
        ("budget", "can_spend"),
        ("org", "member_of"),
    }
)


class FgaError(RuntimeError):
    """OpenFGA refused a request (a 4xx other than a timeout). Not an outage."""


class Client:
    """A minimal OpenFGA HTTP API client. Tests replace it with `tests/conftest.py`'s fake."""

    def __init__(self, api_url: str, preshared_key: str | None) -> None:
        headers = {"Authorization": f"Bearer {preshared_key}"} if preshared_key else {}
        # No retries, so one call never takes much longer than the timeout.
        self._http = httpx.Client(
            base_url=api_url.rstrip("/"), headers=headers, timeout=TIMEOUT_MILLISEC / 1000
        )

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._send("POST", path, body)

    def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, json=body)
        except httpx.TransportError as e:  # includes every timeout
            raise AuthzUnavailable(type(e).__name__) from None
        if response.status_code >= 500:
            raise AuthzUnavailable(f"openfga status {response.status_code}")
        if response.status_code >= 400:
            raise FgaError(f"openfga status {response.status_code}: {response.text[:200]}")
        return response.json()

    def create_store(self, name: str) -> str:
        return self._post("/stores", {"name": name})["id"]

    def write_model(self, store_id: str, model: dict[str, Any]) -> str:
        out = self._post(f"/stores/{store_id}/authorization-models", model)
        return out["authorization_model_id"]

    def read_model(self, store_id: str, model_id: str) -> dict[str, Any]:
        out = self._send("GET", f"/stores/{store_id}/authorization-models/{model_id}")
        return out["authorization_model"]

    def check(self, store_id: str, model_id: str, user: str, relation: str, object: str) -> bool:
        out = self._post(
            f"/stores/{store_id}/check",
            {
                "tuple_key": {"user": user, "relation": relation, "object": object},
                "authorization_model_id": model_id,
            },
        )
        return out.get("allowed") is True

    def read(self, store_id: str, key: Tuple | None = None) -> list[Tuple]:
        """Every tuple in the store, or those matching `key`."""
        found: list[Tuple] = []
        token = ""
        while True:
            body: dict[str, Any] = {"page_size": READ_PAGE_SIZE, "continuation_token": token}
            if key is not None:
                body["tuple_key"] = {"user": key[0], "relation": key[1], "object": key[2]}
            out = self._post(f"/stores/{store_id}/read", body)
            for t in out.get("tuples") or []:
                k = t["key"]
                found.append((k["user"], k["relation"], k["object"]))
            token = out.get("continuation_token") or ""
            if not token:
                return found

    def write(
        self, store_id: str, model_id: str, writes: list[Tuple], deletes: list[Tuple]
    ) -> None:
        body: dict[str, Any] = {"authorization_model_id": model_id}
        if writes:
            body["writes"] = {"tuple_keys": [_key(t) for t in writes]}
        if deletes:
            body["deletes"] = {"tuple_keys": [_key(t) for t in deletes]}
        self._post(f"/stores/{store_id}/write", body)


def _key(t: Tuple) -> dict[str, str]:
    return {"user": t[0], "relation": t[1], "object": t[2]}


@functools.cache
def _client() -> Client:
    settings = get_settings()
    if not settings.openfga_api_url:
        raise RuntimeError("OPENFGA_API_URL is required")
    return Client(settings.openfga_api_url, settings.openfga_preshared_key)


def model_ids(conn: psycopg.Connection) -> tuple[str, str]:
    """`(store_id, model_id)` of the latest applied model. None applied fails closed."""
    row = conn.execute(
        "SELECT store_id, model_id FROM fga_models ORDER BY applied_at DESC, model_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise AuthzUnavailable("no OpenFGA model applied (fga_models is empty)")
    return row[0], row[1]


@functools.cache
def _ids() -> tuple[str, str]:
    """`model_ids` for `check`, read once per process (`migrate` runs before every service)."""
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required")
    with psycopg.connect(settings.database_url) as conn:
        return model_ids(conn)


def check(user: str, relation: str, object: str) -> tuple[bool, str]:  # design signature
    """`(allowed, model_id)` for `user relation object`. Raises `AuthzUnavailable` on a
    timeout or connection error, never returns it as a denial."""
    store_id, model_id = _ids()
    return _client().check(store_id, model_id, user, relation, object), model_id


def write(conn: psycopg.Connection, tuples: Iterable[Tuple]) -> None:
    _change(conn, writes=list(tuples), deletes=[])


def delete(conn: psycopg.Connection, tuples: Iterable[Tuple]) -> None:
    _change(conn, writes=[], deletes=list(tuples))


def _change(conn: psycopg.Connection, writes: list[Tuple], deletes: list[Tuple]) -> None:
    store_id, model_id = model_ids(conn)
    client = _client()
    for i in range(0, len(writes), WRITE_CHUNK):
        client.write(store_id, model_id, writes[i : i + WRITE_CHUNK], [])
    for i in range(0, len(deletes), WRITE_CHUNK):
        client.write(store_id, model_id, [], deletes[i : i + WRITE_CHUNK])


def agent_object(agent_id: str) -> str:
    return f"agent:{ids.keycloak_client_id(agent_id)}"


def _member_tuples(rows: Iterable[tuple[Any, ...]]) -> set[Tuple]:
    return {(f"user:{user_id}", role, f"household:{hid}") for hid, user_id, role in rows}


def member_tuple(household_id: str, user_id: Any, role: str) -> Tuple:
    """The §7 tuple of one `household_members` row."""
    return (f"user:{user_id}", role, f"household:{household_id}")


def card_tuples(agent_id: str, cls: str, owner_ref: str) -> set[Tuple]:
    """The §7 tuples of one people or business card, without the delivery coverage tuples
    (those need the households of the card's zones)."""
    kind, _, ref = owner_ref.partition(":")
    if not ref:
        return set()
    agent = agent_object(agent_id)
    if cls == "consumer" and kind == "user":
        return {(agent, "acts_for", f"user:{ref}")}
    if cls == "household" and kind == "household":
        return {(agent, "acts_for", f"household:{ref}"), (agent, "can_spend", f"budget:{ref}")}
    if kind == "org":
        return {(agent, "member_of", f"org:{ref}")}
    return set()


def ensure(tuples: Iterable[Tuple]) -> None:
    """Write those of `tuples` that are missing, in the store `check` uses. Sync: an async
    caller runs it in a thread. Raises `AuthzUnavailable` on an outage."""
    store_id, model_id = _ids()
    missing = _missing(store_id, tuples)
    client = _client()
    for i in range(0, len(missing), WRITE_CHUNK):
        client.write(store_id, model_id, missing[i : i + WRITE_CHUNK], [])


def _agent_tuples(conn: psycopg.Connection, where: str, params: tuple[Any, ...]) -> set[Tuple]:
    out: set[Tuple] = set()
    cards = (
        unscoped(conn, "fga.reconcile")
        .execute(
            "SELECT agent_id, class, owner_ref, region, zone_ids FROM agent_cards"
            f" WHERE enabled AND {where}",
            params,
        )
        .fetchall()
    )
    for agent_id, cls, owner_ref, region, zone_ids in cards:
        if not owner_ref.partition(":")[2]:
            continue
        agent = agent_object(agent_id)
        out |= card_tuples(agent_id, cls, owner_ref)
        if cls == "delivery" and zone_ids:
            covered = (
                unscoped(conn, "fga.reconcile")
                .execute(
                    "SELECT household_id FROM households WHERE region = %s AND zone_id = ANY(%s)",
                    (region, list(zone_ids)),
                )
                .fetchall()
            )
            out.update((agent, "can_deliver_for", f"household:{h}") for (h,) in covered)
    return out


def expected_tuples(conn: psycopg.Connection) -> set[Tuple]:
    """The structural tuples of §7, derived from Postgres."""
    members = (
        unscoped(conn, "fga.reconcile")
        .execute("SELECT household_id, user_id, role FROM household_members")
        .fetchall()
    )
    return _member_tuples(members) | _agent_tuples(conn, "true", ())


def _missing(store_id: str, expected: Iterable[Tuple]) -> list[Tuple]:
    client = _client()
    return sorted(t for t in expected if not client.read(store_id, t))


def sync_member(conn: psycopg.Connection, household_id: str, user_id: Any) -> None:
    """Write the member's tuple for their `household_members` role, if missing."""
    rows = (
        unscoped(conn, "fga.reconcile")
        .execute(
            "SELECT household_id, user_id, role FROM household_members"
            " WHERE household_id = %s AND user_id = %s",
            (household_id, user_id),
        )
        .fetchall()
    )
    store_id, _ = model_ids(conn)
    write(conn, _missing(store_id, _member_tuples(rows)))


def sync_agent(conn: psycopg.Connection, agent_id: str) -> None:
    """Write the agent's structural tuples from its enabled card, if missing."""
    store_id, _ = model_ids(conn)
    write(conn, _missing(store_id, _agent_tuples(conn, "agent_id = %s", (agent_id,))))


def _object_type(t: Tuple) -> str:
    return t[2].partition(":")[0]


def reconcile(conn: psycopg.Connection) -> int:
    """Make OpenFGA's structural tuples match Postgres; audit and count each change."""
    store_id, model_id = model_ids(conn)
    expected = expected_tuples(conn)
    actual = {t for t in _client().read(store_id) if (_object_type(t), t[1]) in MANAGED}
    writes = sorted(expected - actual)
    deletes = sorted(actual - expected)
    if not writes and not deletes:
        return 0
    # The audit rows commit only if OpenFGA accepted the change.
    with conn.transaction():
        for action, changed in (("write", writes), ("delete", deletes)):
            for user, relation, obj in changed:
                obj_type, _, obj_id = obj.partition(":")
                audit.record(
                    conn,
                    "fga_reconciled",
                    household_id=obj_id if obj_type in ("household", "budget") else None,
                    org_id=obj_id if obj_type == "org" else None,
                    detail={"action": action, "user": user, "relation": relation, "object": obj},
                )
        _change(conn, writes, deletes)
    return len(writes) + len(deletes)
