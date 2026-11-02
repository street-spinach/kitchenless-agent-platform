"""Seller adapter `OPERATIONS` and the `unsupported` refusal (adapters §8, file 18)."""

import importlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import pytest

from services.common import ids, modes, orders
from services.mcp import seller
from services.mcp.grocery import server as grocery
from services.mcp.meta import tool_errors
from services.mcp.restaurant import server as restaurant
from tests.unit.test_seller_tools import (
    COURIER,
    ITEMS,
    SELLER,
    _auth,  # noqa: F401  (autouse fixture: token secret and OpenFGA)
    _authorized_order,
    _ctx,
    _query,
    _seed,
    _with_pool,
)

GUARDED = ("place_order", "amend_order")
_SELLER_AREAS = ("grocery", "restaurant")


def _seller_adapter_classes() -> list[type[Any]]:
    """The mock adapter of each seller server, and every built class in `modes._ADAPTERS`
    under a seller area module."""
    classes: list[type[Any]] = [grocery.tools.mock_adapter, restaurant.tools.mock_adapter]
    for path in modes._ADAPTERS.values():
        module, _, name = path.partition(":")
        if not any(module.startswith(f"services.mcp.{area}.") for area in _SELLER_AREAS):
            continue
        try:
            loaded = importlib.import_module(module)
        except ModuleNotFoundError as e:
            # A registered class whose module is not built yet (adapters build step 6).
            if e.name != module:
                raise
            continue
        cls = getattr(loaded, name)
        if cls not in classes:
            classes.append(cls)
    return classes


def test_operations_match_methods() -> None:
    classes = _seller_adapter_classes()
    assert grocery.tools.mock_adapter in classes and restaurant.tools.mock_adapter in classes
    for cls in classes:
        assert isinstance(cls.OPERATIONS, frozenset), cls
        assert cls.OPERATIONS <= set(GUARDED), cls
        for tool in GUARDED:
            defined = callable(getattr(cls, tool, None))
            assert (tool in cls.OPERATIONS) == defined, (cls, tool)


def test_mocks_support_place_and_amend() -> None:
    for cls in (grocery.tools.mock_adapter, restaurant.tools.mock_adapter):
        assert frozenset({"place_order", "amend_order"}) == cls.OPERATIONS


class _NoAmendAdapter:
    """A seller adapter that places orders but cannot amend them."""

    OPERATIONS: ClassVar[frozenset[str]] = frozenset({"place_order"})
    calls: ClassVar[list[str]] = []

    def __init__(self, card: Any, env: str) -> None:
        pass

    async def search_items(self, query: str, filters: Mapping[str, Any]) -> dict[str, Any]:
        raise AssertionError("not called")

    async def quote(self, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        raise AssertionError("not called")

    async def place_order(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("place_order")
        raise AssertionError("not called")


class _NoOrderAdapter:
    """A seller adapter with no guarded tools at all."""

    OPERATIONS: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, card: Any, env: str) -> None:
        pass

    async def search_items(self, query: str, filters: Mapping[str, Any]) -> dict[str, Any]:
        raise AssertionError("not called")

    async def quote(self, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        raise AssertionError("not called")


def test_fake_adapters_match_their_operations() -> None:
    for cls in (_NoAmendAdapter, _NoOrderAdapter):
        for tool in GUARDED:
            assert (tool in cls.OPERATIONS) == callable(getattr(cls, tool, None))


@pytest.mark.db
@pytest.mark.parametrize(
    ("tool", "adapter"),
    [("amend_order", _NoAmendAdapter), ("place_order", _NoOrderAdapter)],
)
def test_unsupported_refused_before_claim(
    pg: str, monkeypatch: pytest.MonkeyPatch, tool: str, adapter: type[Any]
) -> None:
    run_id = _seed(pg)
    order_id = _authorized_order(pg, run_id, SELLER)
    tools = seller.SellerTools(area="grocery", mock_adapter=adapter)
    ctx = _ctx(run_id)

    async def go() -> Any:
        async with tool_errors(ctx, "grocery", tool) as err:
            if tool == "amend_order":
                await tools.amend_order(
                    ctx, order_id=order_id, shortfall_id=uuid.uuid4(), picks={1: "drop"}
                )
            else:
                await tools.place_order(
                    ctx,
                    order_id=order_id,
                    quote_id=ids.quote_id(order_id),
                    items_hash=orders.items_hash(ITEMS),
                    delivery_agent_id=COURIER,
                )
            raise AssertionError("not refused")
        return err.result

    result = _with_pool(pg, monkeypatch, go)

    assert result is not None and result.is_error
    assert result.structured_content == {
        "kind": "refused",
        "reason": seller.UNSUPPORTED,
        "retryable": False,
    }
    assert _NoAmendAdapter.calls == []
    assert _query(pg, "SELECT key FROM idempotency_keys") == []
    rows = _query(
        pg, "SELECT kind, payload FROM agent_traces WHERE run_id = %s ORDER BY seq", run_id
    )
    assert [r["kind"] for r in rows] == ["tool_result"]
    assert rows[0]["payload"] == {
        "server": "grocery",
        "tool": tool,
        "result": None,
        "refused": True,
        "reason": seller.UNSUPPORTED,
    }
    [order] = _query(pg, "SELECT state FROM orders WHERE order_id = %s", order_id)
    assert order["state"] == "authorized"
