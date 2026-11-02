"""`authz/apply.py` writes the OpenFGA model during migrate (identity-authz I8)."""

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from authz import apply as authz_apply


def _model() -> dict[str, Any]:
    return json.loads(authz_apply.MODEL_PATH.read_text())


def _write(tmp_path: Path, model: dict[str, Any]) -> Path:
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model))
    return path


def _rows(conn: psycopg.Connection) -> list[tuple[str, str, str]]:
    return conn.execute("SELECT model_id, store_id, sha256 FROM fga_models").fetchall()


@pytest.mark.db
def test_apply_twice_is_noop(pg: str, fake_openfga: Any) -> None:
    with psycopg.connect(pg) as conn:
        model_id = authz_apply.apply(conn, fake_openfga)
        assert authz_apply.apply(conn, fake_openfga) is None
        rows = _rows(conn)
    assert model_id is not None
    assert [r[0] for r in rows] == [model_id]
    assert fake_openfga.calls.count("create_store") == 1
    assert fake_openfga.calls.count("write_model") == 1


@pytest.mark.db
def test_apply_refuses_removed_relation(pg: str, fake_openfga: Any, tmp_path: Path) -> None:
    smaller = _model()
    household = next(t for t in smaller["type_definitions"] if t["type"] == "household")
    del household["relations"]["can_deliver_for"]
    del household["metadata"]["relations"]["can_deliver_for"]
    with psycopg.connect(pg) as conn:
        first = authz_apply.apply(conn, fake_openfga)
        with pytest.raises(authz_apply.RemovedRelations, match="household#can_deliver_for"):
            authz_apply.apply(conn, fake_openfga, _write(tmp_path, smaller))
        rows = _rows(conn)
    assert [r[0] for r in rows] == [first]
    assert fake_openfga.calls.count("write_model") == 1


@pytest.mark.db
def test_apply_refuses_removed_type(pg: str, fake_openfga: Any, tmp_path: Path) -> None:
    smaller = _model()
    smaller["type_definitions"] = [t for t in smaller["type_definitions"] if t["type"] != "org"]
    with psycopg.connect(pg) as conn:
        authz_apply.apply(conn, fake_openfga)
        with pytest.raises(authz_apply.RemovedRelations, match="org"):
            authz_apply.apply(conn, fake_openfga, _write(tmp_path, smaller))


@pytest.mark.db
def test_apply_adds_relation(pg: str, fake_openfga: Any, tmp_path: Path) -> None:
    bigger = _model()
    agent = next(t for t in bigger["type_definitions"] if t["type"] == "agent")
    agent["relations"] = {"blocked": {"this": {}}}
    agent["metadata"] = {
        "relations": {"blocked": {"directly_related_user_types": [{"type": "user"}]}}
    }
    with psycopg.connect(pg) as conn:
        first = authz_apply.apply(conn, fake_openfga)
        second = authz_apply.apply(conn, fake_openfga, _write(tmp_path, bigger))
        rows = _rows(conn)
    assert second is not None and second != first
    assert {r[0] for r in rows} == {first, second}
    assert len({r[1] for r in rows}) == 1  # the same store


@pytest.mark.db
def test_migrate_fails_when_relation_removed(
    pg: str, fake_openfga: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smaller = _model()
    smaller["type_definitions"] = [t for t in smaller["type_definitions"] if t["type"] != "budget"]
    with psycopg.connect(pg) as conn:
        authz_apply.apply(conn, fake_openfga)
    monkeypatch.setenv("DATABASE_URL", pg)
    monkeypatch.setattr(authz_apply, "MODEL_PATH", _write(tmp_path, smaller))
    with pytest.raises(SystemExit) as exc:
        authz_apply.main()
    assert exc.value.code == 1


def test_committed_json_has_the_dsl_relations() -> None:
    """A cheap local check; `make fga-test` compares with a fresh `fga model transform`."""
    dsl: set[str] = set()
    current = ""
    for line in authz_apply.MODEL_PATH.with_suffix(".fga").read_text().splitlines():
        words = line.split()
        if words[:1] == ["type"]:
            current = words[1]
            dsl.add(current)
        elif words[:1] == ["define"]:
            dsl.add(f"{current}#{words[1].rstrip(':')}")
    assert authz_apply.relations(_model()) == dsl
