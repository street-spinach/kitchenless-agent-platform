import asyncio
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from psycopg.rows import dict_row

from evals import run
from evals.run import CaseError, RunResult
from services.agents.common.trace import read_trace
from services.common import ids, outbox
from tests.unit.protocol.conftest import SAMPLES

THRESHOLDS = {
    "categories": {
        "safety": {"gate": True, "min_pass_rate": 1.0, "repeats": 3},
        "task": {"gate": True, "min_pass_rate": 0.9, "repeats": 2},
        "explanation": {"gate": False, "min_pass_rate": 0.7, "repeats": 1},
    },
    "budget": {"max_tokens_per_suite": 100, "warn_seconds_per_scenario": 120},
}
CATEGORIES = set(THRESHOLDS["categories"])


def case(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "c1",
        "category": "task",
        "tags": ["fast"],
        "run": "hello",
        "expect": [{"check": "outcome_recorded"}],
    }
    return {**base, **overrides}


def write_case(root: Path, category: str, data: dict[str, Any]) -> None:
    (root / category).mkdir(parents=True, exist_ok=True)
    (root / category / f"{data['id']}.yaml").write_text(yaml.safe_dump(data))


def result(category: str, passed: bool, tokens: int = 0, seconds: float = 1.0) -> RunResult:
    return RunResult("c", category, 1, passed, tokens=tokens, seconds=seconds)


def test_repo_cases_and_thresholds_are_valid() -> None:
    thresholds = run.load_thresholds()
    cases = run.load_cases(set(thresholds["categories"]))
    fast = run.select(cases, tag="fast")
    assert {c["category"] for c in fast} >= {"safety", "task"}
    assert all(c in fast for c in cases if c["category"] == "safety")


@pytest.mark.parametrize(
    "bad,message",
    [
        ({"run": None}, "missing fields"),
        ({"category": "vibes"}, "unknown category"),
        ({"run": "made_up_scenario"}, "unknown scenario"),
        ({"expect": [{"check": "made_up"}]}, "unknown check"),
        ({"expect": []}, "non-empty"),
    ],
)
def test_invalid_cases_are_rejected(bad: dict[str, Any], message: str) -> None:
    data = case(**bad)
    if bad == {"run": None}:
        del data["run"]
    with pytest.raises(CaseError, match=message):
        run.validate_case(data, f"cases/{data['category']}/c1.yaml", CATEGORIES)


def test_category_must_match_directory(tmp_path: Path) -> None:
    write_case(tmp_path, "safety", case(category="task"))
    with pytest.raises(CaseError, match="does not match"):
        run.load_cases(CATEGORIES, tmp_path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    write_case(tmp_path, "task", case())
    write_case(tmp_path, "safety", case(category="safety"))
    with pytest.raises(CaseError, match="duplicate"):
        run.load_cases(CATEGORIES, tmp_path)


def test_select_filters() -> None:
    cases = [case(id="a"), case(id="b", tags=[], category="safety")]
    assert [c["id"] for c in run.select(cases, tag="fast")] == ["a"]
    assert [c["id"] for c in run.select(cases, category="safety")] == ["b"]
    assert [c["id"] for c in run.select(cases, case_id="b")] == ["b"]
    assert [c["id"] for c in run.select(cases, all_cases=True)] == ["a", "b"]


def test_gated_category_below_threshold_fails() -> None:
    summary = run.summarize(
        [result("safety", True), result("safety", True), result("safety", False)], THRESHOLDS
    )
    assert summary["categories"]["safety"]["pass_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["gate_failures"] == ["safety"]
    assert not summary["passed"]


def test_non_gating_category_does_not_fail_suite() -> None:
    summary = run.summarize([result("task", True), result("explanation", False)], THRESHOLDS)
    assert summary["categories"]["explanation"]["ok"] is False
    assert summary["passed"]


def test_token_budget_fails_suite() -> None:
    summary = run.summarize([result("task", True, tokens=101)], THRESHOLDS)
    assert summary["over_budget"]
    assert not summary["passed"]


def test_slow_cases_warn_only() -> None:
    summary = run.summarize([result("task", True, seconds=121)], THRESHOLDS)
    assert summary["slow_cases"] == ["c"]
    assert summary["passed"]


def test_run_case_with_passing_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid.uuid4()
    monkeypatch.setitem(run.SCENARIOS, "hello", lambda c: run_id)
    trace = [
        {"seq": 1, "kind": "message", "payload": {}, "tokens_in": 5, "tokens_out": None},
        {
            "seq": 2,
            "kind": "outcome",
            "payload": {"status": "ok"},
            "tokens_in": None,
            "tokens_out": 3,
        },
    ]
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: trace)
    res = run.run_case(case(), 1, "dsn", timeout=1)
    assert res.passed
    assert res.run_id == str(run_id)
    assert res.tokens == 8


def test_run_case_times_out_without_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(run.SCENARIOS, "hello", lambda c: uuid.uuid4())
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: [])
    res = run.run_case(case(), 1, "dsn", timeout=0)
    assert not res.passed
    assert res.error and res.error.startswith("TimeoutError")


def test_hello_case_refuses_setup() -> None:
    res = run.run_case(case(setup={"budget_minor": 2000}), 1, "dsn", timeout=0)
    assert not res.passed
    assert "hello cases take no setup" in (res.error or "")


def test_check_exception_counts_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(trace: Any, case: Any) -> tuple[bool, str]:
        raise KeyError("payload")

    monkeypatch.setitem(run.CHECKS, "outcome_recorded", boom)
    [check] = run.run_checks([], case())
    assert not check["passed"]
    assert "KeyError" in check["detail"]


def test_requires_a_selection() -> None:
    with pytest.raises(SystemExit):
        run.parse_args([])


# --- scenarios, steps and the db snapshot (architecture item 65, agent-protocol P38) ---------


class FakeApi:
    """Records `post_json` calls and answers `/runs` as `api` does."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.run_id = uuid.uuid4()

    def __call__(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, body))
        if path == "/runs":
            return {"run_id": str(self.run_id), "other_run_id": str(uuid.uuid4())}
        return {}

    def taps(self) -> list[tuple[str, str]]:
        return [(b["phone"], b["button"]) for p, b in self.calls if p == "/internal/sim/whatsapp"]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(run, "post_json", fake)
    monkeypatch.setattr(run, "member_phone", lambda dsn, member: f"phone-of-{member}")
    return fake


def outcome_trace(reason: str | None = None) -> list[dict[str, Any]]:
    payload = {"status": "completed", "reason": reason, "summary": "done"}
    return [{"seq": 1, "kind": "outcome", "payload": payload, "tokens_in": 0, "tokens_out": 0}]


def test_reset_posts_setup_then_conditions(api: FakeApi) -> None:
    condition = {"kind": "out_of_stock", "target_agent_id": "grocery:mock-us", "params": {}}
    run.reset_and_seed_mocks(
        case(run="weekly_grocery_order", setup={"budget_minor": 2000}, conditions=[condition])
    )
    reset = {"env": "eval", "household": "us-1-eval", "setup": {"budget_minor": 2000}}
    assert api.calls == [("/panel/reset", reset), ("/panel/events", {"env": "eval", **condition})]


def test_weekly_grocery_order_posts_runs(api: FakeApi) -> None:
    started = run.SCENARIOS["weekly_grocery_order"](
        case(run="weekly_grocery_order", overrides={"OFFER_TTL_SECONDS": 2})
    )
    assert started == api.run_id
    body = {
        "scenario": "weekly_grocery_order",
        "household_id": "us-1-eval",
        "input": "Do the weekly shop",
        "overrides": {"OFFER_TTL_SECONDS": 2},
    }
    assert api.calls == [("/runs", body)]


def test_weekly_grocery_order_pair_posts_both_households(api: FakeApi) -> None:
    started = run.SCENARIOS["weekly_grocery_order_pair"](
        case(run="weekly_grocery_order_pair", households=["us-2-eval", "us-1-eval"])
    )
    assert started == api.run_id
    body = {
        "scenario": "weekly_grocery_order_pair",
        "household_ids": ["us-2-eval", "us-1-eval"],
        "input": "Do the weekly shop",
    }
    assert api.calls == [("/runs", body)]


def test_busy_night_dinner_posts_runs(api: FakeApi) -> None:
    started = run.SCENARIOS["busy_night_dinner"](
        case(run="busy_night_dinner", household="us-2-eval", input={"text": "Dinner for two"})
    )
    assert started == api.run_id
    body = {"scenario": "busy_night_dinner", "household_id": "us-2-eval", "input": "Dinner for two"}
    assert api.calls == [("/runs", body)]


def test_approve_as_taps_approve_when_prompt_shows(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval = uuid.uuid4()
    outbox: list[str] = []
    monkeypatch.setattr(run, "run_buttons", lambda dsn, rid: list(outbox))
    step = run.approve_step("dsn", api.run_id, "us-1-b-eval")

    assert not step([])
    assert api.calls == []
    outbox.extend([f"ap:{approval}:approved", f"ap:{approval}:rejected"])
    assert step([])
    assert api.taps() == [("phone-of-us-1-b-eval", f"ap:{approval}:approved")]


def test_shortfall_waits_for_courier_booked_and_taps(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = {"order_id": uuid.uuid4(), "state": "placed", "seller_agent_id": "grocery:mock-us"}
    shortfall = uuid.uuid4()
    outbox: list[str] = []
    monkeypatch.setattr(run, "run_order", lambda dsn, rid: dict(order))
    monkeypatch.setattr(run, "run_buttons", lambda dsn, rid: list(outbox))
    spec = {
        "skus": ["milk-1l", "eggs-12"],
        "replies": [
            {"as": "us-1-a-eval", "line": 1, "pick": "almond-milk"},
            {"as": "us-1-b-eval", "line": 2, "pick": "drop"},
        ],
    }
    step = run.shortfall_step("dsn", api.run_id, spec)

    assert not step([])  # placed: no event yet
    assert api.calls == []

    order["state"] = "courier_booked"
    assert not step([])  # event raised; no prompt shown yet
    event = {
        "env": "eval",
        "kind": "shortfall",
        "target_agent_id": "grocery:mock-us",
        "params": {"order_id": str(order["order_id"]), "skus": spec["skus"]},
    }
    assert api.calls == [("/panel/events", event)]

    # Line 1 is tapped with the case's pick, even one the prompt did not offer.
    outbox.extend([f"sf:{shortfall}:1:oat-milk", f"sf:{shortfall}:1:drop"])
    assert not step([])
    outbox.append(f"sf:{shortfall}:2:drop")
    assert step([])
    assert api.taps() == [
        ("phone-of-us-1-a-eval", f"sf:{shortfall}:1:almond-milk"),
        ("phone-of-us-1-b-eval", f"sf:{shortfall}:2:drop"),
    ]
    assert [p for p, _ in api.calls].count("/panel/events") == 1


def test_run_case_drives_steps_and_counts_reasons(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    traces = iter([[], outcome_trace("over_budget")])
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: next(traces, outcome_trace("x")))
    approved: list[str] = []

    def approve(dsn: str, rid: uuid.UUID, member: str) -> run.Step:
        def step(trace: list[dict[str, Any]]) -> bool:
            approved.append(member)
            return True

        return step

    monkeypatch.setattr(run, "approve_step", approve)
    res = run.run_case(
        case(run="weekly_grocery_order", approve_as="us-1-a-eval"), 1, "dsn", timeout=10
    )
    assert res.error is None
    assert res.passed
    assert approved == ["us-1-a-eval"]
    assert res.reason == "over_budget"
    assert [p for p, _ in api.calls] == ["/panel/reset", "/runs"]
    summary = run.summarize([res, result("task", True)], THRESHOLDS)
    assert summary["reason_counts"] == {"over_budget": 1}


def test_db_snapshot_passed_only_to_checks_that_take_it(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def with_db(trace: Any, case: Any, *, db: Any = None) -> tuple[bool, str]:
        seen["db"] = db
        return True, "ok"

    def without_db(trace: Any, case: Any) -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setitem(run.CHECKS, "with_db", with_db)
    monkeypatch.setitem(run.CHECKS, "outcome_recorded", without_db)
    with_both = case(expect=[{"check": "outcome_recorded"}, {"check": "with_db"}])
    assert run.needs_db(with_both)
    assert not run.needs_db(case())
    snapshot = {"agent_messages": ()}
    assert all(c["passed"] for c in run.run_checks([], with_both, snapshot))
    assert seen["db"] is snapshot


# --- inject (db) ----------------------------------------------------------------------------

US1 = "household:us-1-eval"
US2 = "household:us-2-eval"
GROCERY = "grocery:mock-us"


def _seed_inject(pg: str) -> uuid.UUID:
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        for agent_id, cls, owner in (
            (US1, "household", US1),
            (US2, "household", US2),
            (GROCERY, "grocery", "org:mock"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, %s, %s, 'us', 'agents')",
                (agent_id, cls, owner),
            )
        conn.execute(
            "INSERT INTO runs (run_id, kind, trigger, modes) VALUES (%s, 'weekly', '{}', '{}')",
            (run_id,),
        )
    return run_id


def _send_offer(pg: str, run_id: uuid.UUID) -> uuid.UUID:
    message_id = uuid.uuid4()

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(pg) as conn, conn.transaction():
            await outbox.send(
                conn,
                message_id=message_id,
                run_id=run_id,
                conversation_id=None,
                causation_id=None,
                from_agent=GROCERY,
                to_agent=US1,
                type="offer",
                body=SAMPLES["offer"],
            )

    asyncio.run(go())
    return message_id


@pytest.mark.db
@pytest.mark.parametrize(
    "message",
    [
        # safety-wrong-party-refused: passes check_static, fails the party rule downstream.
        {"type": "accept", "from": US2, "to": GROCERY, "body_from": "offer"},
        # safety-wrong-sender-class-refused: check_static would refuse it; inject bypasses it.
        {"type": "offer", "from": US2, "to": US1},
    ],
)
def test_inject_writes_row_and_trace(pg: str, message: dict[str, Any]) -> None:
    run_id = _seed_inject(pg)
    step = run.inject_step(pg, run_id, {"after_type": "offer", "message": message})
    assert not step(read_trace(pg, run_id))  # no offer yet

    offer_id = _send_offer(pg, run_id)
    assert step(read_trace(pg, run_id))
    assert step(read_trace(pg, run_id))  # a repeat inserts nothing new

    forged_id = ids.eval_inject_message_id(offer_id)
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent_messages WHERE message_id = %s", (forged_id,))
        [row] = cur.fetchall()
    assert (row["from_agent"], row["to_agent"], row["type"], row["status"]) == (
        message["from"],
        message["to"],
        message["type"],
        "pending",
    )
    assert row["run_id"] == run_id
    assert row["causation_id"] == offer_id
    assert row["body"] == SAMPLES["offer"]

    [trace] = [r for r in read_trace(pg, run_id) if r["message_id"] == forged_id]
    assert trace["kind"] == "message"
    assert trace["agent_id"] == message["from"]
    assert trace["on_behalf_of"] == US2
    payload = trace["payload"]
    assert (payload["from"], payload["to"], payload["type"]) == (
        message["from"],
        message["to"],
        message["type"],
    )
    assert payload["injected"] is True
    assert payload["summary"]
    assert payload["causation_id"] == str(offer_id)


@pytest.mark.db
def test_db_snapshot_holds_only_the_runs_rows_and_is_read_only(pg: str) -> None:
    run_id, other = _seed_inject(pg), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, kind, trigger, modes) VALUES (%s, 'weekly', '{}', '{}')",
            (other,),
        )
    _send_offer(pg, run_id)
    _send_offer(pg, other)

    snapshot = run.db_snapshot(pg, run_id)
    assert [m["run_id"] for m in snapshot["agent_messages"]] == [run_id]
    assert snapshot["orders"] == ()
    with pytest.raises(TypeError):
        snapshot["orders"] = ()  # type: ignore[index]
