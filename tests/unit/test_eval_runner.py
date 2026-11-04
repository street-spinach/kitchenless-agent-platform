import asyncio
import json
import urllib.error
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from psycopg.rows import dict_row

from evals import run
from evals import select as case_order
from evals.run import CaseError, RunResult
from services.agents.common.trace import read_trace
from services.common import ids, outbox
from tests.unit.protocol.conftest import SAMPLES

THRESHOLDS = {
    "categories": {
        "safety": {"gate": True, "min_pass_rate": 1.0, "repeats": {"stub": 3, "real": 5}},
        "task": {"gate": True, "min_pass_rate": 0.9, "repeats": {"stub": 2, "real": 4}},
        "explanation": {"gate": False, "min_pass_rate": 0.7, "repeats": {"stub": 1, "real": 1}},
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


def test_budget_exhausted_is_reported_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaces `test_token_budget_fails_suite`: the runner no longer starts an attempt it
    can't afford, so a suite can't go over budget; the attempts it didn't start make the
    outcome `budget_exhausted`, exit 0 (evals.md §5, §7)."""
    skipped = RunResult(
        "c", "task", 2, passed=False, verdict="skipped", error_kind="budget_skipped"
    )
    summary = run.summarize([result("task", True, tokens=60), skipped], THRESHOLDS)
    assert (summary["outcome"], summary["outcome_detail"], summary["exit_code"]) == (
        "budget_exhausted",
        "budget_skipped",
        0,
    )
    assert summary["categories"]["task"]["pass_rate"] == 1.0
    # Through `main`: the second attempt would need 60 tokens with 40 left, so it never starts.
    write_case(tmp_path, "task", case(id="a"))
    monkeypatch.setenv("RUN_TOKEN_CEILING", "60")
    started: list[int] = []

    def spend(c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"):
        started.append(repeat)
        return RunResult(c["id"], c["category"], repeat, passed=True, tokens=60)

    status = main_status(tmp_path, monkeypatch, ["--all"], run_case=spend)
    assert started == [1]
    assert (status["outcome"], status["detail"], status["exit_code"]) == (
        "budget_exhausted",
        "budget_skipped",
        0,
    )
    assert status["failed_cases"] == []


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


def test_repeats_default_to_thresholds_and_override_applies_to_every_case() -> None:
    assert run.parse_args(["--tag", "fast"]).repeats is None
    assert run.repeats_for(case(category="safety"), THRESHOLDS, None) == 3
    assert run.repeats_for(case(category="task"), THRESHOLDS, None) == 2
    assert run.parse_args(["--tag", "fast", "--repeats", "1"]).repeats == 1
    assert run.repeats_for(case(category="safety"), THRESHOLDS, 1) == 1
    with pytest.raises(SystemExit):
        run.parse_args(["--tag", "fast", "--repeats", "0"])


def test_main_runs_each_case_repeats_times(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [case(id="s1", category="safety"), case(id="t1", category="task")]
    monkeypatch.setattr(run, "load_thresholds", lambda: THRESHOLDS)
    monkeypatch.setattr(run, "load_cases", lambda categories: cases)
    monkeypatch.setattr(run, "write_results", lambda summary, results, md: Path("r.json"))
    calls: list[tuple[str, int]] = []

    def fake_run(
        c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"
    ) -> RunResult:
        calls.append((c["id"], repeat))
        return RunResult(c["id"], c["category"], repeat, passed=True)

    monkeypatch.setattr(run, "run_case", fake_run)
    assert run.main(["--tag", "fast"]) == 0
    assert calls == [("s1", 1), ("s1", 2), ("s1", 3), ("t1", 1), ("t1", 2)]
    calls.clear()
    assert run.main(["--tag", "fast", "--repeats", "1"]) == 0
    assert calls == [("s1", 1), ("t1", 1)]


# --- the case format and validation (evals.md §4, build step 5a) ----------------------------


def validate(data: dict[str, Any]) -> dict[str, Any]:
    return run.validate_case(data, f"cases/{data['category']}/{data['id']}.yaml", CATEGORIES)


def _passing_run(
    c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"
) -> RunResult:
    return RunResult(c["id"], c["category"], repeat, passed=True)


class FakeHistory:
    """Stands in for `run.History` in tests with no database."""

    def __init__(self, dsn: str, eval_run_id: str) -> None:
        self.errors = 0
        self.attempts: list[RunResult] = []

    def last_green_sha(self, mode: str, trigger: str) -> str | None:
        return None

    def start(self, run_info: dict[str, Any], selection: list[str], thresholds: Any) -> None:
        pass

    def attempt(self, r: RunResult, case_hash: str) -> None:
        self.attempts.append(r)

    def finish(self, summary: dict[str, Any]) -> None:
        pass


def main_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    *,
    run_case: Any = _passing_run,
    db: bool = False,
    written: dict[str, Any] | None = None,
) -> int:
    """`run.main` over the cases written under `tmp_path`, with no attempt run for real.
    Unless `db`, the eval lock and the history are faked (they need Postgres). `written`
    receives the results JSON v1."""
    load = LOAD_CASES

    def write(summary: dict[str, Any], results: list[RunResult], md: str) -> Path:
        if written is not None:
            written.update(run.results_json(summary, results))
        return Path("r.json")

    monkeypatch.setattr(run, "load_thresholds", lambda: THRESHOLDS)
    monkeypatch.setattr(run, "load_cases", lambda categories: load(categories, tmp_path))
    monkeypatch.setattr(run, "write_results", write)
    monkeypatch.setattr(run, "run_case", run_case)
    if not db:
        monkeypatch.setattr(run, "take_lock", lambda dsn, **kw: object())
        monkeypatch.setattr(run, "release_lock", lambda conn: None)
        monkeypatch.setattr(run, "History", FakeHistory)
        monkeypatch.setattr(run, "last_real_attempts", lambda dsn: [])
    return run.main(argv)


def main_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], **kw: Any
) -> dict[str, Any]:
    """`main_on`, returning its `EVAL_STATUS` line (printed last) plus `json`, the results
    JSON v1 it would have written."""
    lines: list[str] = []
    monkeypatch.setattr(
        run, "print", lambda *a, **k: lines.append(" ".join(map(str, a))), raising=False
    )
    written: dict[str, Any] = {}
    code = main_on(tmp_path, monkeypatch, argv, written=written, **kw)
    status = json.loads(lines[-1].removeprefix("EVAL_STATUS "))
    assert status["exit_code"] == code
    return {**status, "json": written}


LOAD_CASES = run.load_cases


def weekly(**overrides: Any) -> dict[str, Any]:
    return case(run="weekly_grocery_order", **overrides)


def test_stub_override_requires_stub_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overrides = {"STUB_FAIL_NODE": "respond_to_counter"}
    for modes in (None, ["stub", "real"], ["real"]):
        data = (
            weekly(overrides=overrides)
            if modes is None
            else weekly(overrides=overrides, model_modes=modes)
        )
        with pytest.raises(CaseError, match="need model_modes: \\[stub\\]"):
            validate(data)
    assert validate(weekly(overrides=overrides, model_modes=["stub"]))
    # A non-stub override is fine on a real-capable case.
    assert validate(weekly(overrides={"APPROVAL_TTL_SECONDS": 5}))
    write_case(tmp_path, "task", weekly(overrides=overrides))
    assert main_on(tmp_path, monkeypatch, ["--all"]) == 2


def test_unknown_setup_key() -> None:
    with pytest.raises(CaseError, match="unknown setup key 'made_up'"):
        validate(weekly(setup={"made_up": 1}))
    with pytest.raises(CaseError, match="must be int"):
        validate(weekly(setup={"budget_minor": "1000"}))
    with pytest.raises(CaseError, match="must be int"):
        validate(weekly(setup={"budget_minor": True}))
    with pytest.raises(CaseError, match="setup must be a mapping"):
        validate(weekly(setup=["budget_minor"]))
    assert validate(weekly(setup={"budget_minor": 1000, "take_turns": True}))


def test_unknown_check_arg() -> None:
    with pytest.raises(CaseError, match="unknown args \\['colour'\\] for check run_status"):
        validate(weekly(expect=[{"check": "run_status", "args": {"colour": "red"}}]))
    # `db` is supplied by the runner, never by the case.
    with pytest.raises(CaseError, match="unknown args \\['db'\\]"):
        validate(weekly(expect=[{"check": "order_placed_once", "args": {"db": {}}}]))
    args = {"status": "error", "reason": "no_offers"}
    assert validate(weekly(expect=[{"check": "run_status", "args": args}]))


def test_input_only_for_member_message() -> None:
    with pytest.raises(CaseError, match="input is only for member_message"):
        validate(weekly(input={"member": "us-1-a-eval", "text": "hi"}))
    with pytest.raises(CaseError, match="member_message needs input"):
        validate(case(run="member_message"))
    with pytest.raises(CaseError, match="member_message needs input"):
        validate(case(run="member_message", input={"text": "hi"}))
    with pytest.raises(CaseError, match="households is only for"):
        validate(weekly(households=["us-1-eval", "us-2-eval"]))
    with pytest.raises(CaseError, match="two different household ids"):
        validate(case(run="weekly_grocery_order_pair", households=["us-1-eval", "us-1-eval"]))
    assert validate(case(run="weekly_grocery_order_pair", households=["us-2-eval", "us-1-eval"]))
    # `hello` keeps its body as `input` (the two existing hello cases).
    assert validate(case(input={"text": "eval", "on_behalf_of": "eval-user"}))


def test_stub_capable_safety_must_be_fast() -> None:
    for modes in (None, ["stub"], ["stub", "real"]):
        data = case(category="safety", tags=[])
        if modes is not None:
            data["model_modes"] = modes
        with pytest.raises(CaseError, match="must carry the fast tag"):
            validate(data)
    assert validate(case(category="safety", tags=[], model_modes=["real"]))


def test_unknown_fields_and_bad_model_modes_are_rejected() -> None:
    with pytest.raises(CaseError, match="unknown fields \\['nightly_only'\\]"):
        validate(case(nightly_only=True))
    for modes in ([], ["stub", "stub"], ["fake"], "stub"):
        with pytest.raises(CaseError, match="model_modes must be"):
            validate(case(model_modes=modes))
    with pytest.raises(CaseError, match="does not match its file name"):
        run.validate_case(case(), "cases/task/other.yaml", CATEGORIES)
    with pytest.raises(CaseError, match="expect entries take only"):
        validate(case(expect=[{"check": "outcome_recorded", "arg": {}}]))


def test_mode_filters_by_model_modes_and_picks_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path, "task", case(id="both"))
    write_case(tmp_path, "task", case(id="stub-only", model_modes=["stub"]))
    write_case(tmp_path, "task", case(id="real-only", model_modes=["real"]))
    cases = run.load_cases(CATEGORIES, tmp_path)
    assert [c["id"] for c in run.select(cases, all_cases=True, mode="stub")] == [
        "both",
        "stub-only",
    ]
    assert [c["id"] for c in run.select(cases, tag="fast", mode="real")] == ["both", "real-only"]
    assert run.parse_args(["--all"]).mode == "stub"
    assert run.repeats_for(case(category="safety"), THRESHOLDS, None, "real") == 5
    calls: list[tuple[str, int]] = []
    load = LOAD_CASES
    monkeypatch.setattr(run, "load_thresholds", lambda: THRESHOLDS)
    monkeypatch.setattr(run, "load_cases", lambda categories: load(categories, tmp_path))
    monkeypatch.setattr(run, "write_results", lambda summary, results, md: Path("r.json"))

    def fake_run(
        c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"
    ) -> RunResult:
        assert mode == "real"
        calls.append((c["id"], repeat))
        return RunResult(c["id"], c["category"], repeat, passed=True)

    monkeypatch.setattr(run, "run_case", fake_run)
    # Real mode first confirms the mode on `agents` /health (build step 5c).
    monkeypatch.setattr(run, "agents_health", lambda: {"model_mode": "real"})
    assert run.main(["--all", "--mode", "real"]) == 0
    assert calls == [(i, r) for i in ("both", "real-only") for r in (1, 2, 3, 4)]


def test_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_case(tmp_path, "task", case(id="a"))
    write_case(tmp_path, "task", case(id="b", tags=[]))
    write_case(tmp_path, "task", case(id="c"))
    listed = tmp_path / "list.yaml"
    listed.write_text(yaml.safe_dump(["c", "b"]))
    args = run.parse_args(["--cases-file", str(listed), "--trigger", "manual", "--mode", "real"])
    assert (args.cases_file, args.trigger, args.mode) == (listed, "manual", "real")
    assert run.parse_args(["--tag", "fast"]).trigger is None
    for bad in (["--all", "--mode", "fake"], ["--all", "--trigger", "weekly"]):
        with pytest.raises(SystemExit):
            run.parse_args(bad)
    ids_ = run.load_case_ids(listed, {"a", "b", "c"})
    cases = run.load_cases(CATEGORIES, tmp_path)
    assert [c["id"] for c in run.select(cases, case_ids=ids_)] == ["b", "c"]
    assert main_on(tmp_path, monkeypatch, ["--cases-file", str(listed)]) == 0
    # An id that is not a case, or a file that is not a list, is refused (exit 2).
    listed.write_text(yaml.safe_dump(["c", "zzz"]))
    assert main_on(tmp_path, monkeypatch, ["--cases-file", str(listed)]) == 2
    listed.write_text("c: 1\n")
    assert main_on(tmp_path, monkeypatch, ["--cases-file", str(listed)]) == 2
    assert main_on(tmp_path, monkeypatch, ["--cases-file", str(tmp_path / "missing.yaml")]) == 2


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


def test_run_case_sends_setup_to_reset(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces `test_run_case_refuses_setup_without_control_panel`: `setup` is supported
    (evals.md §4), so `run_case` sends it with the case's household to `POST /panel/reset`
    before it starts the run."""
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: outcome_trace())
    data = weekly(
        household="us-2-eval",
        setup={"budget_minor": 1000},
        expect=[{"check": "run_status", "args": {"status": "completed"}}],
    )
    res = run.run_case(validate(data), 1, "dsn", timeout=1)
    assert res.passed, res
    reset = {"env": "eval", "household": "us-2-eval", "setup": {"budget_minor": 1000}}
    assert [c[0] for c in api.calls] == ["/panel/reset", "/runs"]
    assert api.calls[0][1] == reset
    assert api.calls[1][1]["household_id"] == "us-2-eval"


def test_pair_reset_uses_the_first_household(api: FakeApi) -> None:
    run.reset_and_seed_mocks(
        case(run="weekly_grocery_order_pair", households=["us-2-eval", "us-1-eval"])
    )
    run.reset_and_seed_mocks(case(run="weekly_grocery_order_pair"))
    assert [b["household"] for _, b in api.calls] == ["us-2-eval", "us-1-eval"]


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


class FakeA2A:
    """Records `a2a_rpc` calls and answers as the endpoint does for one task."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.states = iter(["TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED"])
        offer = {"round": 0, "lines": [{"line": 0}], "total_minor": 1000, "currency": "USD"}
        self.task = {
            "id": str(uuid.uuid4()),
            "contextId": str(uuid.uuid4()),
            "status": {"state": "TASK_STATE_SUBMITTED"},
            "artifacts": [{"parts": [{"data": {"type": "offer", "body": offer}}]}],
        }

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "SendMessage":
            data = params["message"]["parts"][0]["data"]
            if data["type"] == "accept":
                return {"error": {"code": -32004, "message": "no", "data": []}}
            return {"result": {"task": self.task}}
        if method == "GetTask":
            return {"result": {**self.task, "status": {"state": next(self.states)}}}
        return {"result": self.task}

    def sent(self, kind: str) -> list[dict[str, Any]]:
        return [
            p["message"]
            for m, p in self.calls
            if m == "SendMessage" and p["message"]["parts"][0]["data"]["type"] == kind
        ]


A2A_STEPS = [
    {"op": "send", "type": "quote_request", "message": "request"},
    {"op": "send", "type": "quote_request", "message": "request"},
    {"op": "get"},
    {"op": "send", "type": "counter_offer", "total_percent": 96},
    {"op": "send", "type": "accept", "error": -32004},
    {"op": "cancel"},
]


def a2a_case(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return case(run="a2a_demo", a2a_steps=steps)


def test_a2a_steps_drive_client(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeA2A()
    monkeypatch.setattr(run, "a2a_rpc", fake)
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: [])
    monkeypatch.setattr(run.time, "sleep", lambda s: None)

    run_id = run.start_a2a(a2a_case(A2A_STEPS))
    first = fake.calls[0][1]["message"]
    message_id = ids.a2a_message_id("external:a2a-demo", first["messageId"])
    assert run_id == ids.a2a_run_id(message_id)
    assert [m for m, _ in fake.calls] == ["SendMessage"]

    [step] = run.run_steps(a2a_case(A2A_STEPS), "dsn", run_id)
    run.drive("dsn", run_id, [step], deadline=run.time.monotonic() + 5)

    assert [m for m, _ in fake.calls] == [
        "SendMessage",
        "SendMessage",
        "GetTask",  # still WORKING, so it repeats
        "GetTask",
        "SendMessage",
        "SendMessage",
        "CancelTask",
    ]
    # A named message keeps its id; the client sends no ids of the order or the run.
    assert [m["messageId"] for m in fake.sent("quote_request")] == [first["messageId"]] * 2
    assert "taskId" not in first
    assert first["parts"][0]["data"]["body"] == run.A2A_REQUEST
    [counter] = fake.sent("counter_offer")
    assert (counter["taskId"], counter["contextId"]) == (fake.task["id"], fake.task["contextId"])
    body = counter["parts"][0]["data"]["body"]
    assert (body["round"], body["total_minor"], body["currency"]) == (0, 960, "USD")
    assert fake.calls[-1][1] == {"id": fake.task["id"]}
    assert run_id not in run.A2A_CLIENTS


def test_a2a_message_ids_differ_between_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeA2A()
    monkeypatch.setattr(run, "a2a_rpc", fake)
    steps = [{"op": "send", "type": "quote_request", "message": "request"}]
    assert run.start_a2a(a2a_case(steps)) != run.start_a2a(a2a_case(steps))


@pytest.mark.parametrize(
    "steps",
    [
        # A step whose refusal does not come fails the attempt, as an unrefused accept would.
        [
            {"op": "send", "type": "quote_request"},
            {"op": "send", "type": "reject", "error": -32004},
        ],
        # An error nobody expected fails it too.
        [{"op": "send", "type": "quote_request"}, {"op": "send", "type": "accept"}],
    ],
)
def test_a2a_unexpected_answer_is_a_runner_error(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch, steps: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(run, "a2a_rpc", FakeA2A())
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: [])
    res = run.run_case(a2a_case(steps), 1, "dsn", timeout=5)
    assert not res.passed
    assert [c["check"] for c in res.checks] == [run.RUNNER_ERROR_CHECK]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"run": "a2a_demo"}, "needs a non-empty a2a_steps"),
        ({"run": "a2a_demo", "a2a_steps": [{"op": "get"}]}, "must open with"),
        ({"run": "a2a_demo", "a2a_steps": [{"op": "send"}]}, "bad a2a step"),
        ({"run": "a2a_demo", "a2a_steps": [{"op": "wait"}]}, "bad a2a step"),
        ({"run": "a2a_demo", "a2a_steps": [{"op": "get", "type": "accept"}]}, "bad a2a step"),
        (
            {
                "run": "a2a_demo",
                "a2a_steps": [{"op": "send", "type": "quote_request"}],
                "overrides": {"STUB_FAIL_NODE": "x"},
                "model_modes": ["stub"],
            },
            "takes no overrides",
        ),
        ({"a2a_steps": [{"op": "send", "type": "quote_request"}]}, "only for a2a_demo"),
    ],
)
def test_bad_a2a_steps_are_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(CaseError, match=message):
        run.validate_case(case(**overrides), "cases/task/c1.yaml", CATEGORIES)


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


# --- verdicts, outcome and standard output (evals.md §5, build step 5b) ---------------------


def _refused(path: str, body: dict[str, Any]) -> dict[str, Any]:
    raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))


def _http_error(code: int) -> Any:
    def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "/runs":
            raise urllib.error.HTTPError(path, code, "error", {}, None)  # type: ignore[arg-type]
        return {}

    return post


def test_connection_refused_is_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "post_json", _refused)
    res = run.run_case(weekly(), 1, "dsn", timeout=1)
    assert (res.verdict, res.error_kind, res.passed) == ("skipped", "infra", False)
    assert res.run_id is None
    # A 5xx from `api` before the run started is infra too; a 4xx from `/runs` is not.
    monkeypatch.setattr(run, "post_json", _http_error(503))
    assert run.run_case(weekly(), 1, "dsn", timeout=1).error_kind == "infra"
    monkeypatch.setattr(run, "post_json", _http_error(422))
    refused = run.run_case(weekly(), 1, "dsn", timeout=1)
    assert (refused.verdict, refused.error_kind) == ("fail", "check_failed")
    # More than half the attempts infra: the eval run ends infra (exit 3).
    monkeypatch.setattr(run, "post_json", _refused)
    summary = run.summarize([run.run_case(weekly(), 1, "dsn", 1)] * 2, THRESHOLDS)
    assert summary["categories"]["task"]["pass_rate"] is None
    assert (summary["outcome"], summary["exit_code"]) == ("infra", 3)


def test_real_model_unavailable_completion_only_is_skipped(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: outcome_trace("model_unavailable"))
    data = weekly(expect=[{"check": "run_status", "args": {"status": "error"}}])
    real = run.run_case(data, 1, "dsn", timeout=1, mode="real")
    assert real.checks[0]["invariant"] is False and not real.checks[0]["passed"]
    assert (real.verdict, real.error_kind) == ("skipped", "model_unavailable")
    # In stub mode the same attempt is a failure.
    stub = run.run_case(data, 1, "dsn", timeout=1, mode="stub")
    assert (stub.verdict, stub.error_kind) == ("fail", "check_failed")
    summary = run.summarize([real] * 3, THRESHOLDS, "real")
    assert (summary["outcome"], summary["exit_code"]) == ("model_unavailable", 3)


def _failing(invariant: bool | None) -> Any:
    def check(trace: Any, case: Any) -> tuple[bool, str]:
        return False, "peanuts ordered"

    if invariant is not None:
        check.invariant = invariant  # type: ignore[attr-defined]
    return check


def test_allergen_and_token_budget_is_fail(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: outcome_trace("token_budget"))
    monkeypatch.setitem(run.CHECKS, "no_allergen_in_order", _failing(True))
    data = weekly(
        category="safety",
        expect=[
            {"check": "no_allergen_in_order"},
            {"check": "run_status", "args": {"status": "error"}},
        ],
    )
    res = run.run_case(data, 1, "dsn", timeout=1, mode="real")
    assert (res.verdict, res.error_kind) == ("fail", "check_failed")
    summary = run.summarize([res], THRESHOLDS, "real")
    assert (summary["outcome"], summary["outcome_detail"], summary["exit_code"]) == (
        "regression",
        "safety",
        1,
    )


def test_undeclared_check_is_invariant(api: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: outcome_trace("model_unavailable"))
    monkeypatch.setitem(run.CHECKS, "undeclared", _failing(None))
    monkeypatch.setitem(run.CHECKS, "completion", _failing(False))
    [undeclared] = run.run_checks([], case(expect=[{"check": "undeclared"}]))
    assert undeclared["invariant"] is True
    data = weekly(expect=[{"check": "undeclared"}, {"check": "completion"}])
    res = run.run_case(data, 1, "dsn", timeout=1, mode="real")
    assert (res.verdict, res.error_kind) == ("fail", "check_failed")
    only_completion = run.run_case(
        weekly(expect=[{"check": "completion"}]), 1, "dsn", timeout=1, mode="real"
    )
    assert only_completion.verdict == "skipped"


def test_all_skipped_category_has_no_rate() -> None:
    skipped = RunResult("s", "safety", 1, passed=False, verdict="skipped", error_kind="infra")
    summary = run.summarize([skipped, result("task", True), result("task", True)], THRESHOLDS)
    safety = summary["categories"]["safety"]
    assert (safety["pass"], safety["fail"], safety["skipped"]) == (0, 0, 1)
    assert safety["pass_rate"] is None and safety["ok"] is None
    assert summary["gate_failures"] == []
    assert summary["categories"]["task"]["pass_rate"] == 1.0
    assert (summary["outcome"], summary["exit_code"]) == ("pass", 0)
    assert summary["error_kind_counts"] == {"infra": 1, "none": 2}
    assert "n/a" in run.to_markdown(summary, [skipped])


def _stdout_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.rstrip("\n").split("\n")


def test_status_line_is_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path, "task", case(id="a"))
    monkeypatch.setenv("GIT_SHA", "abc123")
    assert main_on(tmp_path, monkeypatch, ["--all", "--trigger", "manual"]) == 0
    last = _stdout_lines(capsys)[-1]
    assert last.startswith("EVAL_STATUS ")
    status = json.loads(last.removeprefix("EVAL_STATUS "))
    assert list(status) == [
        "outcome",
        "detail",
        "exit_code",
        "eval_run_id",
        "mode",
        "trigger",
        "git_sha",
        "last_green_sha",
        "failed_cases",
        "real_safety_failed",
    ]
    assert status["outcome"] == "pass" and status["exit_code"] == 0
    assert (status["mode"], status["trigger"], status["git_sha"]) == ("stub", "manual", "abc123")
    assert status["failed_cases"] == [] and status["real_safety_failed"] is False
    assert uuid.UUID(status["eval_run_id"])
    # Invalid cases end with the status line too (exit 2).
    write_case(tmp_path, "task", case(id="b", expect=[]))
    assert main_on(tmp_path, monkeypatch, ["--all"]) == 2
    last = _stdout_lines(capsys)[-1]
    assert json.loads(last.removeprefix("EVAL_STATUS "))["outcome"] == "invalid_cases"


def test_summary_between_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_case(tmp_path, "safety", case(id="s1", category="safety"))
    write_case(tmp_path, "task", case(id="t1"))
    monkeypatch.setattr(run, "load_thresholds", lambda: THRESHOLDS)
    monkeypatch.setattr(run, "load_cases", lambda categories: LOAD_CASES(categories, tmp_path))
    written: dict[str, Any] = {}

    def write(summary: dict[str, Any], results: list[RunResult], md: str) -> Path:
        written["json"] = run.results_json(summary, results)
        return Path("r.json")

    monkeypatch.setattr(run, "write_results", write)
    monkeypatch.setattr(
        run,
        "run_case",
        lambda c, repeat, dsn, timeout, mode="stub": RunResult(
            c["id"], c["category"], repeat, passed=c["id"] != "s1"
        ),
    )
    assert run.main(["--all", "--repeats", "1"]) == 1
    lines = _stdout_lines(capsys)
    begin, end = lines.index(run.SUMMARY_BEGIN), lines.index(run.SUMMARY_END)
    assert begin < end == len(lines) - 2
    summary = "\n".join(lines[begin + 1 : end])
    assert summary.startswith("# Eval results: regression (safety)")
    assert "`s1` repeat 1" in summary
    status = json.loads(lines[-1].removeprefix("EVAL_STATUS "))
    assert (status["outcome"], status["exit_code"], status["failed_cases"]) == (
        "regression",
        1,
        ["s1"],
    )
    payload = written["json"]
    assert payload["schema"] == 1
    assert payload["selection"] == ["s1", "t1"]
    assert (payload["outcome"], payload["outcome_detail"], payload["exit_code"]) == (
        "regression",
        "safety",
        1,
    )
    assert payload["categories"]["safety"] == {
        "pass": 0,
        "fail": 1,
        "skipped": 0,
        "pass_rate": 0.0,
        "min_pass_rate": 1.0,
        "gate": True,
        "ok": False,
    }
    assert set(payload["attempts"][0]) == {
        "case_id",
        "repeat",
        "agent_run_ids",
        "verdict",
        "error_kind",
        "reason",
        "seconds",
        "tokens",
        "checks",
        "models_used",
    }


def test_timeout_runs_checks_over_partial_trace(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real-mode timeout after a model error is skipped only when no invariant failed."""
    model_error = {
        "seq": 1,
        "kind": "error",
        "payload": {"where": "model:choose_offer", "reason": "timeout", "attempt": 1},
        "tokens_in": None,
        "tokens_out": None,
        "model": "m-1",
    }
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: [model_error])
    completion = weekly(expect=[{"check": "run_status", "args": {"status": "completed"}}])
    res = run.run_case(completion, 1, "dsn", timeout=0, mode="real")
    assert (res.verdict, res.error_kind, res.models_used) == ("skipped", "timeout", ["m-1"])
    assert run.run_case(completion, 1, "dsn", timeout=0, mode="stub").verdict == "fail"
    monkeypatch.setitem(run.CHECKS, "no_allergen_in_order", _failing(True))
    allergen = weekly(expect=[{"check": "no_allergen_in_order"}])
    res = run.run_case(allergen, 1, "dsn", timeout=0, mode="real")
    assert (res.verdict, res.error_kind) == ("fail", "timeout")


# --- budget, deadline, history and the eval-run checks (evals.md §5, §7, build step 5c) ------


def _budget(**kw: Any) -> run.Budget:
    base = {"max_tokens": 100, "run_ceiling": 60, "timeout": 4.0, "deadline": 10.0, "started": 0.0}
    return run.Budget(**{**base, **kw})


def test_attempt_not_started_below_run_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = _budget()
    assert budget.affords(40, now=0.0)  # 60 tokens left: exactly one run's ceiling
    assert not budget.affords(41, now=0.0)
    write_case(tmp_path, "task", case(id="a"))
    write_case(tmp_path, "task", case(id="b"))
    monkeypatch.setenv("RUN_TOKEN_CEILING", "60")
    started: list[str] = []

    def spend(c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"):
        started.append(c["id"])
        return RunResult(c["id"], c["category"], repeat, passed=True, tokens=41)

    status = main_status(tmp_path, monkeypatch, ["--all", "--repeats", "1"], run_case=spend)
    assert started == ["a"]
    attempts = status["json"]["attempts"]
    assert [(a["case_id"], a["verdict"], a["error_kind"]) for a in attempts] == [
        ("a", "pass", "none"),
        ("b", "skipped", "budget_skipped"),
    ]
    assert attempts[1]["agent_run_ids"] == [] and attempts[1]["tokens"] == 0
    assert status["json"]["tokens"] <= THRESHOLDS["budget"]["max_tokens_per_suite"]
    assert status["outcome"] == "budget_exhausted"
    # Unset, the ceiling is the whole suite budget: only an untouched budget starts a run.
    monkeypatch.delenv("RUN_TOKEN_CEILING")
    assert run.run_token_ceiling(100) == 100


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_deadline_marks_rest_budget_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = _budget(started=0.0)
    assert budget.affords(0, now=6.0)  # 4 s left: exactly one attempt's timeout
    assert not budget.affords(0, now=6.1)
    for name in ("a", "b", "c"):
        write_case(tmp_path, "task", case(id=name))
    monkeypatch.setenv("EVAL_DEADLINE_SECONDS", "10")
    monkeypatch.setenv("EVAL_TIMEOUT_SECONDS", "4")
    clock = Clock()
    monkeypatch.setattr(run, "time", clock)
    started: list[str] = []

    def slow(c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"):
        assert timeout == 4.0
        started.append(c["id"])
        clock.now += 5  # each attempt takes 5 s
        return RunResult(c["id"], c["category"], repeat, passed=True)

    status = main_status(tmp_path, monkeypatch, ["--all", "--repeats", "1"], run_case=slow)
    # a starts with 10 s left, b with 5 s; c would have 0 s, below one attempt's 4 s.
    assert started == ["a", "b"]
    assert [a["error_kind"] for a in status["json"]["attempts"]] == [
        "none",
        "none",
        "budget_skipped",
    ]
    assert (status["outcome"], status["exit_code"]) == ("budget_exhausted", 0)


class FailingConnect:
    """`psycopg.connect` for the history: every call raises `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.error


def _history_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connect: FailingConnect
) -> dict[str, Any]:
    write_case(tmp_path, "safety", case(id="s1", category="safety"))
    write_case(tmp_path, "task", case(id="t1"))
    monkeypatch.setenv("GIT_SHA", "abc123")
    monkeypatch.setattr(run.psycopg, "connect", connect)
    monkeypatch.setattr(run, "take_lock", lambda dsn, **kw: object())
    monkeypatch.setattr(run, "release_lock", lambda conn: None)

    def mixed(c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"):
        return RunResult(c["id"], c["category"], repeat, passed=c["id"] != "s1")

    return main_status(
        tmp_path,
        monkeypatch,
        ["--all", "--repeats", "1", "--trigger", "nightly"],
        run_case=mixed,
        db=True,
    )


def test_history_failure_keeps_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connect = FailingConnect(psycopg.OperationalError("connection refused"))
    status = _history_main(tmp_path, monkeypatch, connect)
    assert [a["verdict"] for a in status["json"]["attempts"]] == ["fail", "pass"]
    assert (status["outcome"], status["detail"], status["exit_code"]) == ("regression", "safety", 1)
    # The last-green read, the eval_runs row, two attempts and the finish all failed.
    assert connect.calls == 5
    assert status["json"]["history_write_errors"] == 5
    assert status["last_green_sha"] is None


def test_runs_without_history_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connect = FailingConnect(psycopg.errors.UndefinedTable('relation "eval_runs" does not exist'))
    status = _history_main(tmp_path, monkeypatch, connect)
    assert [a["verdict"] for a in status["json"]["attempts"]] == ["fail", "pass"]
    assert status["outcome"] == "regression"
    assert connect.calls == 1  # logged once, nothing more written
    assert status["json"]["history_write_errors"] == 0


def test_no_trigger_takes_no_lock_and_writes_no_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path, "task", case(id="a"))
    monkeypatch.delenv("GIT_SHA", raising=False)
    connect = FailingConnect(AssertionError("no database access without --trigger"))
    monkeypatch.setattr(run.psycopg, "connect", connect)
    status = main_status(tmp_path, monkeypatch, ["--all"], db=True)
    assert (status["outcome"], status["trigger"], status["git_sha"]) == ("pass", None, None)
    assert connect.calls == 0


def test_trigger_without_git_sha_is_infra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_case(tmp_path, "task", case(id="a"))
    locked: list[str] = []
    monkeypatch.setattr(run, "take_lock", lambda dsn, **kw: locked.append(dsn))

    def never(*a: Any, **k: Any) -> RunResult:
        raise AssertionError("no attempt may start")

    for unset in (None, "", "  "):
        if unset is None:
            monkeypatch.delenv("GIT_SHA", raising=False)
        else:
            monkeypatch.setenv("GIT_SHA", unset)

        status = main_status(
            tmp_path, monkeypatch, ["--all", "--trigger", "nightly"], run_case=never, db=True
        )
        assert (status["outcome"], status["detail"], status["exit_code"]) == (
            "infra",
            "no_git_sha",
            3,
        )
        assert status["json"]["attempts"] == [] and locked == []
    # The PR check passes no trigger and needs no GIT_SHA.
    assert main_on(tmp_path, monkeypatch, ["--all"]) == 0


def test_real_mode_health_mismatch_is_infra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path, "task", case(id="a"))
    started: list[str] = []

    def track(c: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"):
        started.append(mode)
        return RunResult(c["id"], c["category"], repeat, passed=True, mode=mode)

    def unreachable() -> dict[str, Any]:
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    for health in ({"model_mode": "stub"}, {"status": "ok"}, unreachable):
        monkeypatch.setattr(
            run, "agents_health", health if callable(health) else (lambda h=health: h)
        )
        status = main_status(tmp_path, monkeypatch, ["--all", "--mode", "real"], run_case=track)
        assert (status["outcome"], status["detail"], status["exit_code"]) == (
            "infra",
            "mode_mismatch",
            3,
        )
        assert started == []
    real = {"model_mode": "real", "model_name": "people-1", "model_business_name": "biz-1"}
    monkeypatch.setattr(run, "agents_health", lambda: real)
    status = main_status(
        tmp_path, monkeypatch, ["--all", "--mode", "real", "--repeats", "1"], run_case=track
    )
    assert status["outcome"] == "pass" and started == ["real"]
    assert status["json"]["models"] == {"people": "people-1", "business": "biz-1"}
    # Stub mode never reads /health.
    monkeypatch.setattr(run, "agents_health", unreachable)
    assert main_on(tmp_path, monkeypatch, ["--all"]) == 0


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


# --- coverage (evals.md EG9, build step 6) --------------------------------------------------

# evals.md EG9: every agents goal a model run can show. AG13 and AG14 are unit tests only.
AGENTS_GOALS = {f"AG{n}" for n in range(1, 13)} | {"AG15"}


def test_agents_goals_covered() -> None:
    coverage = case_order.load_coverage()
    goals = coverage["goals"]["agents"]
    assert set(goals) >= AGENTS_GOALS
    categories = set(run.load_thresholds()["categories"])
    on_disk = {p.stem: p for p in run.CASES_DIR.glob("*/*.yaml")}
    for goal in sorted(AGENTS_GOALS):
        mapped = goals[goal]
        assert isinstance(mapped, list) and mapped, f"{goal} maps to no case"
        for case_id in mapped:
            assert isinstance(case_id, str) and case_id
            assert case_order.source_design(case_id, coverage["prefixes"]) == "agents", case_id
            if case_id in on_disk:
                path = on_disk[case_id]
                run.validate_case(yaml.safe_load(path.read_text()), str(path), categories)
