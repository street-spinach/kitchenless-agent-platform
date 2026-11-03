"""Meta templates for sends outside the WhatsApp window (experience §5).

The map: each text key that may be sent as a Meta template, its submitted Meta name, and
the setting that holds its approved name (`WA_TPL_<KEY>`; empty means not approved). The
parameter builders and the template send come with EX-I13.

Also here: the holdable kinds (§5 "Holdable kinds"), which text keys each `ref.kind` may
hold, and the model-written keys, which are never held and never sent as a template.
"""

from dataclasses import dataclass

from services.common.config import Settings

__all__ = [
    "HOLDABLE",
    "MODEL_KEYS",
    "REMINDER_KEYS",
    "TEMPLATES",
    "MetaTemplate",
    "holdable",
    "meta_name",
]


@dataclass(frozen=True)
class MetaTemplate:
    """One row of the §5 map."""

    key: str  # the text key in `sender.TEXTS`
    name: str  # the name submitted to Meta (runbook)
    setting: str  # the `Settings` field holding the approved name


TEMPLATES: dict[str, MetaTemplate] = {
    t.key: t
    for t in (
        MetaTemplate("approval_request", "approval_needed_v1", "wa_tpl_approval_request"),
        MetaTemplate("take_turns_offer", "take_turns_offer_v1", "wa_tpl_take_turns_offer"),
        MetaTemplate("order_summary", "order_delivered_v1", "wa_tpl_order_summary"),
        MetaTemplate("consent_renewal", "consent_renewal_v1", "wa_tpl_consent_renewal"),
        MetaTemplate("consent_revoked", "consent_revoked_v1", "wa_tpl_consent_revoked"),
        MetaTemplate(
            "reminder_delivery_today", "delivery_today_v1", "wa_tpl_reminder_delivery_today"
        ),
        MetaTemplate(
            "reminder_pantry_summary", "pantry_summary_v1", "wa_tpl_reminder_pantry_summary"
        ),
    )
}

# `ref.kind` → the text keys it may hold (§5). Checked together, so a key sent with the
# wrong kind is not held (fails closed).
HOLDABLE: dict[str, frozenset[str]] = {
    "approval": frozenset({"approval_request"}),
    "order": frozenset(
        {"order_summary", "nothing_needed", "take_turns_offer", "take_turns_offer_off"}
    ),
    "grant": frozenset({"consent_renewal", "consent_revoked"}),
}

# Model-written text (§5 step 6): never held, never a Meta template.
MODEL_KEYS: frozenset[str] = frozenset({"chat_reply", "shortfall_prompt", "shortfall_result"})

# Reminders skipped outside the window get reason `no_template` (§5 step 6).
REMINDER_KEYS: frozenset[str] = frozenset({"reminder_delivery_today", "reminder_pantry_summary"})


def meta_name(key: str, settings: Settings) -> str | None:
    """The approved Meta template name for `key`, or `None` if it has none (not in the map,
    model-written, or its setting is empty)."""
    template = TEMPLATES.get(key)
    if template is None or key in MODEL_KEYS:
        return None
    return getattr(settings, template.setting) or None


def holdable(key: str, ref_kind: str | None) -> bool:
    """Whether a message with text key `key` and `ref.kind` may be held."""
    if ref_kind is None or key in MODEL_KEYS:
        return False
    return key in HOLDABLE.get(ref_kind, frozenset())
