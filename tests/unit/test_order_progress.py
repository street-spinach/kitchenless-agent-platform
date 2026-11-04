"""`order_progress` (showcase §5, SC-A1, SC-A6): once per order when the first `quote_request`
goes out, to the adult who started the run, inside the window only, never held."""

import asyncio
from typing import Any

import pytest

from services.agents.household import graph as hg
from services.api.whatsapp import sender as s
from services.api.whatsapp import templates
from services.common.config import Settings
from services.common.consumer import StepResult
from tests.unit import api_world as w
from tests.unit.test_config import full_env
from tests.unit.test_household_graph import MEMBERS, SELLERS, STARTER, DinnerWorld, World
from tests.unit.test_whatsapp_window import _decisions, _last_inbound, _out, _send, _sends

WEEKLY = {"amount_minor": 20000, "currency": "USD"}
PROGRESS = {
    "template": "order_progress",
    "params": {
        "sellers": "3 stores",
        "items": "18 items",
        "checks": "peanuts and sesame, vegetarian",
        "budget": "$200.00",
        "period": "weekly",
    },
}


def _world(cls: type[World] = World, **kwargs: Any) -> World:
    world = cls(adults=dict(MEMBERS), **kwargs)
    world.household.rules["weekly"] = WEEKLY
    return world


def _progress(result: StepResult) -> list[Any]:
    return [
        m
        for m in result.messages
        if m.type == "whatsapp_out" and m.body["template"] == "order_progress"
    ]


def _quotes_out(world: World) -> StepResult:
    world.start()
    world.constraints("consumer:us-1-a")
    return world.constraints("consumer:us-1-b")


# Wording ---------------------------------------------------------------------------------


def test_wording() -> None:
    assert s.graph_payload("+15550100202", PROGRESS)["text"]["body"] == (  # type: ignore[index]
        "On it. Asking 3 stores for 18 items. Every line is checked for peanuts and sesame, "
        "vegetarian, and your $200.00 weekly budget."
    )


def test_sent_with_the_first_quote_requests() -> None:
    world = _world()
    result = _quotes_out(world)
    [progress] = _progress(result)
    assert progress.to_agent == "channel:whatsapp"
    assert progress.body["user_id"] == STARTER
    assert progress.body["buttons"] == []
    assert progress.body["params"] == {
        "sellers": "2 stores",
        "items": "4 items",  # the stub's list, less the rice at home
        "checks": "your rules",
        "budget": "$200.00",
        "period": "weekly",
    }
    # After the quotes, so the quote ids and conversations are what they were.
    assert [m.type for m in result.messages] == ["quote_request"] * len(SELLERS) + ["whatsapp_out"]


def test_checks_are_words_for_the_union_of_codes() -> None:
    world = _world()
    world.start()
    world.constraints("consumer:us-1-a", ["sesame"])
    result = world.constraints("consumer:us-1-b", ["peanuts"])
    assert _progress(result)[0].body["params"]["checks"] == "peanuts and sesame"


@pytest.mark.parametrize(
    ("allergens", "diets", "text"),
    [
        ([], [], "your rules"),
        (["peanuts"], [], "peanuts"),
        ([], ["vegetarian"], "vegetarian"),
        (["peanuts", "sesame"], ["vegetarian"], "peanuts and sesame, vegetarian"),
        (["eggs", "peanuts", "sesame"], [], "eggs, peanuts and sesame"),
    ],
)
def test_check_words(allergens: list[str], diets: list[str], text: str) -> None:
    assert hg._check_words(allergens, diets) == text


def test_dinner_wording() -> None:
    world = _world(DinnerWorld)
    [progress] = _progress(_quotes_out(world))
    params = progress.body["params"]
    assert (params["sellers"], params["items"]) == ("1 restaurant", "1 dinner")


# Once, and only when quotes go out -------------------------------------------------------


def test_once_per_order_not_again_on_a_requote() -> None:
    world = _world()
    accept = world.accepted()
    result = world.reject(accept.to_agent, accept.body["quote_id"], "expired")
    assert [m.type for m in result.messages] == ["quote_request"] * len(SELLERS)


def test_no_sellers_no_progress() -> None:
    assert _progress(_quotes_out(_world(sellers=[]))) == []


def test_no_budget_no_progress() -> None:
    world = _world()
    world.household.rules["weekly"] = None
    assert _progress(_quotes_out(world)) == []


# Adults only -----------------------------------------------------------------------------


def test_not_to_a_child() -> None:
    adults = {a: u for a, u in MEMBERS.items() if u != STARTER}
    world = World(adults=adults)
    world.household.rules["weekly"] = WEEKLY
    result = _quotes_out(world)
    assert _progress(result) == []
    assert [m.type for m in result.messages] == ["quote_request"] * len(SELLERS)


def test_without_the_adults_loader_nobody_gets_it() -> None:
    world = World()  # fails closed
    world.household.rules["weekly"] = WEEKLY
    assert _progress(_quotes_out(world)) == []


@pytest.mark.db
def test_adults_query_leaves_out_children(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)
    w.seed(pg)
    w.query(
        pg,
        "UPDATE household_members SET role = 'child' WHERE user_id = %s RETURNING 1",
        w.MEMBER,
    )
    rows = w.query(pg, hg._ADULTS_SQL, w.HOUSEHOLD_ID)
    assert [r["user_id"] for r in rows] == [str(w.ADMIN)]


# No new work (showcase SG6) --------------------------------------------------------------


def test_no_new_tool_call_model_call_or_trace_row() -> None:
    def work(world: World) -> tuple[list[str], list[str], list[str]]:
        _quotes_out(world)
        return (
            [c["tool"] for c in world.household.calls],
            [name for name, _ in world.stubs.seen],
            [str(r.get("choice")) for r in world.traces.of("decision")],
        )

    assert work(_world()) == work(World())


# The window ------------------------------------------------------------------------------


@pytest.fixture
def world(pg: str, monkeypatch: pytest.MonkeyPatch) -> str:
    w.patch_settings(monkeypatch)
    w.seed(pg)
    return pg


@pytest.mark.db
def test_sent_inside_the_window(world: str) -> None:
    _send(world, _out(world, PROGRESS))
    [out] = w.query(world, "SELECT * FROM sim_outbound")
    assert out["body"]["graph"]["text"]["body"].startswith("On it. Asking 3 stores")
    assert out["body"]["buttons"] == []
    [send] = _sends(world)
    assert send["outcome"] == s.SENT


@pytest.mark.db
def test_dropped_outside_the_window(world: str) -> None:
    # Every template configured, and an order `ref`: still not held, still no template.
    settings = Settings.from_env(
        {**full_env("api"), **{t.setting.upper(): t.name for t in templates.TEMPLATES.values()}}
    )
    assert templates.meta_name("order_progress", settings) is None
    assert not any(
        templates.holdable("order_progress", kind) for kind in ("approval", "order", "grant")
    )
    _last_inbound(world, w.MEMBER_PHONE, "30 hours")
    msg = _out(world, {**PROGRESS, "ref": {"kind": "order", "id": "o"}})
    asyncio.run(s.Sender(w.connect_to(world), settings=settings)(msg))
    assert w.query(world, "SELECT * FROM sim_outbound") == []
    assert w.query(world, "SELECT * FROM held_messages") == []
    [send] = _sends(world)
    assert (send["outcome"], send["reason"]) == (s.SKIPPED, s.OUTSIDE_WINDOW)
    [decision] = _decisions(world, msg)
    assert (decision["choice"], decision["reason"]) == (s.SKIPPED, s.OUTSIDE_WINDOW)
