"""Shared fixtures. `pg` gives a db test its own empty schema (architecture item 68)."""

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="db test: DATABASE_URL is not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def empty_schema_dsn() -> Iterator[str]:
    """A DSN whose search_path is a fresh, empty schema, dropped afterwards."""
    dsn = os.environ["DATABASE_URL"]
    schema = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield make_conninfo(dsn, options=f"-c search_path={schema}")
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def pg(empty_schema_dsn: str) -> str:
    """A DSN for a fresh schema with every migration applied."""
    from migrations.apply import apply

    apply(empty_schema_dsn)
    return empty_schema_dsn


# --- Identity fixtures (identity-authz I39, build step 4) ---


@pytest.fixture(scope="session")
def mock_signing_key() -> str:
    """A fresh RSA private key as PEM text, standing in for `MOCK_SIGNING_KEY` (§13)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@dataclass
class IdentitySettings:
    """The settings `services.identity.tokens` reads (identity-authz §13), mock by default."""

    mock_signing_key: str | None
    token_source: str | None = "mock"
    domain: str = "localhost"
    issuer_url: str | None = "https://localhost/auth/realms/kitchenless"
    keycloak_url: str | None = None


@pytest.fixture(autouse=True)
def identity_settings(
    monkeypatch: pytest.MonkeyPatch, mock_signing_key: str
) -> Iterator[IdentitySettings]:
    """Every test validates and signs tokens under `TOKEN_SOURCE=mock` with a fresh test key
    (CLAUDE.md: tests use mock). Tests change the returned object to try other modes."""
    from services.identity import token_source, tokens

    settings = IdentitySettings(mock_signing_key=mock_signing_key)
    monkeypatch.setattr(tokens, "get_settings", lambda: settings)
    token_source._SOURCES.clear()
    tokens._jwks_by_url.clear()
    yield settings
    token_source._SOURCES.clear()
    tokens._jwks_by_url.clear()


Tuple = tuple[str, str, str]


class FakeOpenFga:
    """An in-memory stand-in for `services.identity.fga.Client`.

    `check` answers from direct tuples only (no rewrites); `authz/model.fga.yaml` tests the
    model itself. Set `fail` to an exception to make every call raise it.
    """

    def __init__(self) -> None:
        self.models: dict[str, dict[str, dict[str, Any]]] = {}  # store -> model id -> model
        self.tuples: dict[str, set[Tuple]] = {}
        self.calls: list[str] = []
        self.fail: Exception | None = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail is not None:
            raise self.fail

    def create_store(self, name: str) -> str:
        self._call("create_store")
        store_id = f"store-{len(self.models) + 1}"
        self.models[store_id] = {}
        self.tuples[store_id] = set()
        return store_id

    def write_model(self, store_id: str, model: dict[str, Any]) -> str:
        self._call("write_model")
        model_id = f"model-{sum(len(m) for m in self.models.values()) + 1}"
        self.models[store_id][model_id] = model
        return model_id

    def read_model(self, store_id: str, model_id: str) -> dict[str, Any]:
        self._call("read_model")
        return self.models[store_id][model_id]

    def check(self, store_id: str, model_id: str, user: str, relation: str, object: str) -> bool:
        self._call("check")
        assert model_id in self.models[store_id]
        return (user, relation, object) in self.tuples[store_id]

    def read(self, store_id: str, key: Tuple | None = None) -> list[Tuple]:
        self._call("read")
        found = self.tuples[store_id]
        return sorted(found if key is None else found & {key})

    def write(
        self, store_id: str, model_id: str, writes: list[Tuple], deletes: list[Tuple]
    ) -> None:
        self._call("write")
        assert model_id in self.models[store_id]
        stored = self.tuples[store_id]
        # OpenFGA refuses the whole request on a duplicate write or a missing delete.
        if any(t in stored for t in writes) or any(t not in stored for t in deletes):
            raise AssertionError("OpenFGA would refuse this write")
        stored.update(writes)
        stored.difference_update(deletes)


@pytest.fixture
def fake_openfga(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOpenFga]:
    """Replace the OpenFGA client of `services.identity.fga` with a `FakeOpenFga`."""
    from services.identity import fga

    fake = FakeOpenFga()
    fga._ids.cache_clear()
    monkeypatch.setattr(fga, "_client", lambda: fake)
    yield fake
    fga._ids.cache_clear()


@pytest.fixture
def grant() -> Callable[..., uuid.UUID]:
    """Insert an `agent_grants` row (and the user, household and card it needs) on a sync
    connection; returns the `grant_id`. Needs a migrated database (`pg`)."""

    def make(
        conn: psycopg.Connection,
        *,
        agent_id: str = "household:h-1",
        agent_kind: str = "household",
        household_id: str = "h-1",
        user_id: uuid.UUID | None = None,
        consented_by: uuid.UUID | None = None,
        status: str = "active",
        scopes: tuple[str, ...] = ("orders:place",),
    ) -> uuid.UUID:
        user_id = user_id or uuid.uuid4()
        conn.execute(
            "INSERT INTO users (user_id, display_name) VALUES (%s, 'Test person')"
            " ON CONFLICT DO NOTHING",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, 'Test household', 'us', 'z-1', 'America/New_York', 'USD')"
            " ON CONFLICT DO NOTHING",
            (household_id,),
        )
        owner_ref = f"household:{household_id}" if agent_kind == "household" else f"user:{user_id}"
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
            " VALUES (%s, %s, %s, 'us', 'agents') ON CONFLICT DO NOTHING",
            (agent_id, agent_kind, owner_ref),
        )
        grant_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO agent_grants (grant_id, agent_id, agent_kind, user_id, consented_by,"
            " household_id, scopes, status, refresh_ciphertext, key_version, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, now() + interval '30 days')",
            (
                grant_id,
                agent_id,
                agent_kind,
                user_id,
                consented_by or user_id,
                household_id,
                list(scopes),
                status,
                b"\x00",
            ),
        )
        return grant_id

    return make
