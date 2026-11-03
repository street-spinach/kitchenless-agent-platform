"""Pantry vocabulary, matching, the estimate and the skip decision (pantry design §2, §4, §5).

Pure: the only I/O is `load_vocabulary` reading the YAML. `as_of` is always passed in.

*   `match(vocab, text)` lower-cases, trims and collapses spaces, then looks up the exact name
    or alias. No stemming, no fuzzy match, no model. No hit is `None` (unmatched).
*   `estimate(latest, window_days, as_of)` turns an item's latest event into presence, trust
    and a confidence that reaches `THETA` exactly when the window ends. No rule reads the
    confidence; it is written into decision rows.
*   `decide(vocab, estimates, list_name)` is the §5 step 2 rule: unmatched keeps, matched and
    trusted skips, matched and not trusted keeps with a reason from `KEPT_REASONS`.
"""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "KEPT_REASONS",
    "KINDS",
    "THETA",
    "VOCABULARY_PATH",
    "Estimate",
    "Vocabulary",
    "decide",
    "estimate",
    "is_enabled",
    "load_vocabulary",
    "match",
    "normalize",
]

KINDS = ("add", "verify", "out")
KEPT_REASONS = ("absent", "stale", "out", "pantry_unavailable", "pantry_off")
THETA = 0.7
VOCABULARY_PATH = Path(__file__).resolve().parents[1] / "seed" / "data" / "pantry_items.yaml"

_PRESENT_KINDS = ("add", "verify")


def is_enabled(env: str) -> bool:
    """Whether pantry estimates are on for a household with this `env` (PT-OD4, EX-A17)."""
    return env == "eval"


def normalize(text: str) -> str:
    """Lower-case, trim and collapse runs of whitespace to one space."""
    return " ".join(text.lower().split())


@dataclass(frozen=True)
class Vocabulary:
    """The pantry items: canonical names, the alias map, windows and the file's sha256."""

    names: tuple[str, ...]
    aliases: Mapping[str, str]  # normalized alias -> name
    window_days: Mapping[str, int]  # name -> window in days
    version: str

    @classmethod
    def from_entries(cls, entries: Iterable[Mapping[str, Any]], version: str) -> "Vocabulary":
        """Build from `[{name, aliases, window_days}]`. Raises `ValueError` on a bad entry, a
        duplicate name or alias, or an alias equal to another item's name."""
        names: list[str] = []
        aliases: dict[str, str] = {}
        windows: dict[str, int] = {}
        seen: set[str] = set()
        for entry in entries:
            raw = entry.get("name")
            if not isinstance(raw, str) or not normalize(raw):
                raise ValueError(f"pantry item without a name: {entry!r}")
            name = normalize(raw)
            if raw != name:
                raise ValueError(f"pantry item name {raw!r} is not normalized")
            window = entry.get("window_days")
            if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
                raise ValueError(f"{name}: window_days must be a positive integer")
            item_aliases = entry.get("aliases") or []
            if not isinstance(item_aliases, list) or not all(
                isinstance(a, str) for a in item_aliases
            ):
                raise ValueError(f"{name}: aliases must be a list of strings")
            for term in [name, *(normalize(a) for a in item_aliases)]:
                if not term or term in seen:
                    raise ValueError(f"duplicate pantry name or alias {term!r}")
                seen.add(term)
            names.append(name)
            windows[name] = window
            for alias in item_aliases:
                aliases[normalize(alias)] = name
        return cls(names=tuple(names), aliases=aliases, window_days=windows, version=version)


def load_vocabulary(path: Path = VOCABULARY_PATH) -> Vocabulary:
    """Read the vocabulary YAML. `version` is the sha256 of the file's bytes."""
    raw = path.read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict) or not isinstance(data.get("pantry_items"), list):
        raise ValueError(f"{path.name}: expected a top-level pantry_items list")
    return Vocabulary.from_entries(data["pantry_items"], hashlib.sha256(raw).hexdigest())


def match(vocab: Vocabulary, text: str) -> str | None:
    """The vocabulary name for `text` (an exact name or alias after `normalize`), or `None`."""
    key = normalize(text)
    if key in vocab.window_days:
        return key
    return vocab.aliases.get(key)


@dataclass(frozen=True)
class Estimate:
    pantry_item: str
    latest_event_id: int
    kind: str
    age_hours: float
    window_hours: float
    present: bool
    trusted: bool
    confidence: float


def estimate(latest: Mapping[str, Any], window_days: int, as_of: datetime) -> Estimate:
    """The estimate for an item from its latest event (`pantry_item`, `event_id`, `kind`,
    `ts`). `age_hours` is rounded to 2 places; the rules use the exact age."""
    age = (as_of - latest["ts"]).total_seconds() / 3600
    window = float(window_days * 24)
    present = latest["kind"] in _PRESENT_KINDS
    confidence = max(0.0, 1 - (1 - THETA) * age / window) if present else 0.0
    return Estimate(
        pantry_item=latest["pantry_item"],
        latest_event_id=latest["event_id"],
        kind=latest["kind"],
        age_hours=round(age, 2),
        window_hours=window,
        present=present,
        trusted=present and age < window,
        confidence=confidence,
    )


def _facts(
    list_name: str,
    pantry_item: str | None,
    est: Mapping[str, Any] | None,
    as_of: str | None,
    vocab: Vocabulary | None,
    reason: str | None,
) -> dict[str, Any]:
    est = est or {}
    return {
        "list_name": list_name,
        "pantry_item": pantry_item,
        "latest_event_id": est.get("latest_event_id"),
        "kind": est.get("kind"),
        "age_hours": est.get("age_hours"),
        "window_hours": est.get("window_hours"),
        "confidence": est.get("confidence"),
        "as_of": as_of,
        "vocabulary_version": vocab.version if vocab is not None else None,
        "reason": reason,
    }


def decide(
    vocab: Vocabulary | None,
    estimates: Sequence[Mapping[str, Any]] | None,
    list_name: str,
    *,
    as_of: str | None = None,
    enabled: bool = True,
) -> tuple[str, str | None, dict[str, Any]]:
    """`(choice, reason, facts)` for one list item (§5).

    `estimates` is `get_pantry`'s list, used as given (`present`, `trusted`); `None` means the
    call failed (`pantry_unavailable`). `enabled=False` is `pantry_off`. A `None` vocabulary
    (load failed) is `pantry_unavailable`. `choice` is `pantry_skipped`, `pantry_kept` or
    `pantry_unmatched`; `reason` is set only on `pantry_kept`.
    """
    matched = match(vocab, list_name) if vocab is not None else None
    if estimates is None or vocab is None or not enabled:
        # §5 order: a failed call, then `enabled`, then the vocabulary load.
        off = "pantry_off" if estimates is not None and not enabled else "pantry_unavailable"
        return "pantry_kept", off, _facts(list_name, matched, None, as_of, vocab, off)

    if matched is None:
        return "pantry_unmatched", None, _facts(list_name, None, None, as_of, vocab, None)
    est = next((e for e in estimates if e.get("pantry_item") == matched), None)
    if est is None:
        reason = "absent"
    elif est.get("trusted") is True and est.get("present") is True:
        return "pantry_skipped", None, _facts(list_name, matched, est, as_of, vocab, None)
    elif est.get("present") is True:
        reason = "stale"
    else:
        reason = "out"
    return "pantry_kept", reason, _facts(list_name, matched, est, as_of, vocab, reason)
