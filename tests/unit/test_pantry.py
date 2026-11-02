"""Pantry vocabulary, matching, the estimate and the skip rule (pantry design §2, §4, §5, P3)."""

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.common import pantry

AS_OF = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FACT_KEYS = {
    "list_name",
    "pantry_item",
    "latest_event_id",
    "kind",
    "age_hours",
    "window_hours",
    "confidence",
    "as_of",
    "vocabulary_version",
    "reason",
}


@pytest.fixture(scope="module")
def vocab() -> pantry.Vocabulary:
    return pantry.load_vocabulary()


def _event(kind: str = "add", age_hours: float = 0, event_id: int = 1, item: str = "eggs"):
    return {
        "pantry_item": item,
        "event_id": event_id,
        "kind": kind,
        "ts": AS_OF - timedelta(hours=age_hours),
    }


# --- Vocabulary and matching -----------------------------------------------------------------


def test_constants() -> None:
    assert pantry.KINDS == ("add", "verify", "out")
    assert pantry.KEPT_REASONS == ("absent", "stale", "out", "pantry_unavailable", "pantry_off")
    assert pantry.THETA == 0.7
    assert pantry.VOCABULARY_PATH.name == "pantry_items.yaml"
    assert pantry.VOCABULARY_PATH.parent.name == "data"


def test_vocabulary_launch_list(vocab: pantry.Vocabulary) -> None:
    assert dict(vocab.window_days) == {
        "milk": 3,
        "almond milk": 7,
        "eggs": 7,
        "bread": 3,
        "bananas": 4,
        "apples": 10,
        "chicken breast": 2,
        "rice": 14,
        "pasta": 14,
        "granola": 14,
        "almonds": 14,
    }
    assert vocab.version == hashlib.sha256(pantry.VOCABULARY_PATH.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("milk", "milk"),
        ("Whole Milk", "milk"),
        ("  2%   milk ", "milk"),
        ("reduced fat milk", "milk"),
        ("egg", "eggs"),
        ("LARGE EGGS", "eggs"),
        ("sandwich\tbread", "bread"),
        ("banana", "bananas"),
        ("apple", "apples"),
        ("chicken", "chicken breast"),
        ("white rice", "rice"),
        ("penne", "pasta"),
        ("granola", "granola"),
        ("almond", "almonds"),
        ("Almond Milk", "almond milk"),
    ],
)
def test_match_names_aliases_case_and_spaces(
    vocab: pantry.Vocabulary, text: str, expected: str
) -> None:
    assert pantry.match(vocab, text) == expected


def test_almond_milk_never_matches_milk(vocab: pantry.Vocabulary) -> None:
    assert pantry.match(vocab, "almond milk") == "almond milk"
    assert "almond milk" not in vocab.aliases
    assert all(alias != "almond milk" for alias, name in vocab.aliases.items() if name == "milk")


@pytest.mark.parametrize("text", ["oat milk", "milks", "eggs and bread", "", "   ", "chocolate"])
def test_unknown_name_is_none(vocab: pantry.Vocabulary, text: str) -> None:
    assert pantry.match(vocab, text) is None


def test_normalize() -> None:
    assert pantry.normalize("  Whole \t  MILK\n") == "whole milk"


@pytest.mark.parametrize(
    "entries",
    [
        [{"name": "milk", "aliases": ["moo"], "window_days": 3},
         {"name": "cream", "aliases": ["moo"], "window_days": 3}],
        [{"name": "milk", "aliases": [], "window_days": 3},
         {"name": "cream", "aliases": ["Milk"], "window_days": 3}],
        [{"name": "milk", "aliases": [], "window_days": 3},
         {"name": "milk", "aliases": [], "window_days": 4}],
        [{"name": "milk", "aliases": ["milk"], "window_days": 3}],
        [{"name": "milk", "aliases": [], "window_days": 0}],
        [{"name": "Milk", "aliases": [], "window_days": 3}],
    ],
)  # fmt: skip
def test_vocabulary_rejects_bad_entries(entries: list[dict]) -> None:
    with pytest.raises(ValueError):
        pantry.Vocabulary.from_entries(entries, version="x")


def test_load_vocabulary_rejects_wrong_shape(tmp_path: Path) -> None:
    path = tmp_path / "pantry_items.yaml"
    path.write_text("items: []\n")
    with pytest.raises(ValueError):
        pantry.load_vocabulary(path)


# --- The estimate ----------------------------------------------------------------------------


def test_estimate_age_zero() -> None:
    est = pantry.estimate(_event(age_hours=0, event_id=9), 7, AS_OF)
    assert est == pantry.Estimate(
        pantry_item="eggs",
        latest_event_id=9,
        kind="add",
        age_hours=0.0,
        window_hours=168.0,
        present=True,
        trusted=True,
        confidence=1.0,
    )


def test_estimate_just_inside_window() -> None:
    est = pantry.estimate(_event(age_hours=167.99), 7, AS_OF)
    assert est.present and est.trusted
    assert pantry.THETA < est.confidence < 1


def test_estimate_at_window_edge_is_theta_and_not_trusted() -> None:
    est = pantry.estimate(_event(age_hours=168), 7, AS_OF)
    assert est.present
    assert not est.trusted
    assert est.confidence == pantry.THETA


def test_estimate_past_window() -> None:
    est = pantry.estimate(_event(age_hours=200), 7, AS_OF)
    assert est.present and not est.trusted
    assert 0 <= est.confidence < pantry.THETA
    # Far past the window, confidence floors at 0.
    assert pantry.estimate(_event(age_hours=24 * 365), 7, AS_OF).confidence == 0


def test_estimate_out_is_absent_with_zero_confidence() -> None:
    est = pantry.estimate(_event(kind="out", age_hours=1), 7, AS_OF)
    assert (est.present, est.trusted, est.confidence) == (False, False, 0.0)


def test_estimate_verify_is_present() -> None:
    est = pantry.estimate(_event(kind="verify", age_hours=1), 7, AS_OF)
    assert est.present and est.trusted and est.kind == "verify"


def test_estimate_age_hours_rounded() -> None:
    est = pantry.estimate(_event(age_hours=1 / 3), 7, AS_OF)
    assert est.age_hours == 0.33


def test_latest_event_wins() -> None:
    # The latest event per item is the highest event_id (P4's query); the estimate reports
    # that event's id and kind, so a later `out` after an `add` means not present.
    events = [_event("add", 1, event_id=3), _event("out", 2, event_id=7)]
    latest = max(events, key=lambda e: e["event_id"])
    est = pantry.estimate(latest, 7, AS_OF)
    assert (est.latest_event_id, est.kind, est.present) == (7, "out", False)
    estimates = [asdict(est)]
    assert pantry.decide(pantry.load_vocabulary(), estimates, "eggs")[:2] == ("pantry_kept", "out")


# --- decide ----------------------------------------------------------------------------------


def _estimates(*events: dict) -> list[dict]:
    vocab = pantry.load_vocabulary()
    return [asdict(pantry.estimate(e, vocab.window_days[e["pantry_item"]], AS_OF)) for e in events]


def test_decide_skipped(vocab: pantry.Vocabulary) -> None:
    ests = _estimates(_event("add", 24, event_id=5, item="rice"))
    choice, reason, facts = pantry.decide(vocab, ests, "White Rice", as_of="2026-11-03T12:00:00Z")
    assert (choice, reason) == ("pantry_skipped", None)
    assert set(facts) == FACT_KEYS
    assert facts == {
        "list_name": "White Rice",
        "pantry_item": "rice",
        "latest_event_id": 5,
        "kind": "add",
        "age_hours": 24.0,
        "window_hours": 336.0,
        "confidence": ests[0]["confidence"],
        "as_of": "2026-11-03T12:00:00Z",
        "vocabulary_version": vocab.version,
        "reason": None,
    }


def test_decide_unmatched(vocab: pantry.Vocabulary) -> None:
    choice, reason, facts = pantry.decide(vocab, _estimates(_event()), "oat milk")
    assert (choice, reason) == ("pantry_unmatched", None)
    assert facts["pantry_item"] is None and facts["latest_event_id"] is None
    assert facts["vocabulary_version"] == vocab.version


def test_decide_kept_absent(vocab: pantry.Vocabulary) -> None:
    choice, reason, facts = pantry.decide(vocab, _estimates(_event(item="rice")), "eggs")
    assert (choice, reason) == ("pantry_kept", "absent")
    assert facts["pantry_item"] == "eggs" and facts["reason"] == "absent"
    assert facts["latest_event_id"] is None and facts["confidence"] is None


def test_decide_kept_stale(vocab: pantry.Vocabulary) -> None:
    ests = _estimates(_event("add", 7 * 24 + 1, item="eggs"))
    choice, reason, facts = pantry.decide(vocab, ests, "egg")
    assert (choice, reason) == ("pantry_kept", "stale")
    assert facts["kind"] == "add" and facts["reason"] == "stale"


def test_decide_kept_out(vocab: pantry.Vocabulary) -> None:
    ests = _estimates(_event("out", 1, item="milk"))
    assert pantry.decide(vocab, ests, "whole milk")[:2] == ("pantry_kept", "out")


def test_decide_uses_estimates_as_given(vocab: pantry.Vocabulary) -> None:
    # A fresh event marked untrusted by get_pantry stays kept: decide recomputes nothing.
    est = {**_estimates(_event("add", 0))[0], "trusted": False}
    assert pantry.decide(vocab, [est], "eggs")[:2] == ("pantry_kept", "stale")


def test_decide_almond_milk_not_skipped_by_milk(vocab: pantry.Vocabulary) -> None:
    ests = _estimates(_event("add", 1, item="milk"))
    assert pantry.decide(vocab, ests, "almond milk")[:2] == ("pantry_kept", "absent")


def test_decide_pantry_off(vocab: pantry.Vocabulary) -> None:
    ests = _estimates(_event("add", 1, item="rice"))
    choice, reason, facts = pantry.decide(vocab, ests, "rice", enabled=False)
    assert (choice, reason) == ("pantry_kept", "pantry_off")
    assert facts["pantry_item"] == "rice" and facts["reason"] == "pantry_off"
    assert all(facts[k] is None for k in ("latest_event_id", "kind", "age_hours", "confidence"))


def test_decide_pantry_unavailable_call_failed(vocab: pantry.Vocabulary) -> None:
    choice, reason, facts = pantry.decide(vocab, None, "rice", enabled=False)
    assert (choice, reason) == ("pantry_kept", "pantry_unavailable")
    assert facts["pantry_item"] == "rice" and facts["vocabulary_version"] == vocab.version


def test_decide_pantry_unavailable_vocabulary_failed() -> None:
    ests = _estimates(_event("add", 1, item="rice"))
    choice, reason, facts = pantry.decide(None, ests, "rice")
    assert (choice, reason) == ("pantry_kept", "pantry_unavailable")
    assert facts["pantry_item"] is None and facts["vocabulary_version"] is None


def test_every_kept_reason_is_reachable(vocab: pantry.Vocabulary) -> None:
    ests = _estimates(_event("add", 200, item="eggs"), _event("out", 1, item="milk"))
    reasons = {
        pantry.decide(vocab, ests, "bread")[1],
        pantry.decide(vocab, ests, "eggs")[1],
        pantry.decide(vocab, ests, "milk")[1],
        pantry.decide(None, ests, "milk")[1],
        pantry.decide(vocab, ests, "milk", enabled=False)[1],
    }
    assert reasons == set(pantry.KEPT_REASONS)
