"""Write `authz/model.json` to OpenFGA during `migrate` (identity-authz §7 and I8).

*   Creates the store if `fga_models` has none.
*   An unchanged file (same sha256 as the latest `fga_models` row) does nothing.
*   Compares the new model's types and relations with the previous model's. If any was
    removed, it writes nothing and exits non-zero, which fails `migrate` and the deploy: the
    model is additive only.
*   Records `(store_id, model_id, sha256)` in `fga_models`.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Protocol

import psycopg

MODEL_PATH = Path(__file__).parent / "model.json"
STORE_NAME = "kitchenless"
# Serializes concurrent appliers (migrations/apply.py uses its own lock id).
LOCK_ID = 7_205_002


class ModelClient(Protocol):
    def create_store(self, name: str) -> str: ...
    def write_model(self, store_id: str, model: dict[str, Any]) -> str: ...
    def read_model(self, store_id: str, model_id: str) -> dict[str, Any]: ...


class RemovedRelations(RuntimeError):
    """The new model drops a type or relation of the previous one."""


def relations(model: dict[str, Any]) -> set[str]:
    """`type` and `type#relation` names of a model, in OpenFGA's JSON form."""
    out: set[str] = set()
    for td in model.get("type_definitions") or []:
        out.add(td["type"])
        out.update(f"{td['type']}#{rel}" for rel in td.get("relations") or {})
    return out


def apply(
    conn: psycopg.Connection, client: ModelClient, model_path: Path = MODEL_PATH
) -> str | None:
    """Apply the model; return the new model id, or None when the file is unchanged."""
    raw = model_path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    model = json.loads(raw)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        latest = conn.execute(
            "SELECT store_id, model_id, sha256 FROM fga_models"
            " ORDER BY applied_at DESC, model_id DESC LIMIT 1"
        ).fetchone()
        if latest is not None and latest[2] == sha256:
            return None
        if latest is None:
            store_id = client.create_store(STORE_NAME)
        else:
            store_id = latest[0]
            removed = relations(client.read_model(store_id, latest[1])) - relations(model)
            if removed:
                raise RemovedRelations(
                    f"model removes {', '.join(sorted(removed))}; the model is additive only"
                )
        model_id = client.write_model(store_id, model)
        conn.execute(
            "INSERT INTO fga_models (model_id, store_id, sha256) VALUES (%s, %s, %s)",
            (model_id, store_id, sha256),
        )
    return model_id


def main() -> None:
    from services.identity import fga

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        try:
            model_id = apply(conn, fga._client(), MODEL_PATH)
        except RemovedRelations as e:
            print(f"authz.apply refused: {e}", file=sys.stderr)
            raise SystemExit(1) from None
    print(f"OpenFGA model: {model_id or 'unchanged'}")


if __name__ == "__main__":
    main()
