"""Compose environment wiring (experience EX-I15)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

from services.common.config import Settings

_INFRA = Path(__file__).resolve().parents[2] / "infra"

# The eight api settings of experience EX-I7.
_TEMPLATE_VARS = {
    "WA_TPL_APPROVAL_REQUEST",
    "WA_TPL_TAKE_TURNS_OFFER",
    "WA_TPL_ORDER_SUMMARY",
    "WA_TPL_CONSENT_RENEWAL",
    "WA_TPL_CONSENT_REVOKED",
    "WA_TPL_REMINDER_DELIVERY_TODAY",
    "WA_TPL_REMINDER_PANTRY_SUMMARY",
    "INSIGHTS_SAMPLE",
}


def _env_example() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in (_INFRA / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            entries[name] = value
    return entries


def test_api_gets_template_vars() -> None:
    settings = {
        f.name.upper()
        for f in dataclasses.fields(Settings)
        if f.name.startswith("wa_tpl_") or f.name == "insights_sample"
    }
    assert settings == _TEMPLATE_VARS

    services = yaml.safe_load((_INFRA / "docker-compose.yml").read_text())["services"]
    api_env = services["api"]["environment"]
    for name in sorted(_TEMPLATE_VARS):
        assert api_env.get(name) == f"${{{name}:-}}", name

    # Only api reads them (EX-I7: `_var({"api"}, ...)`).
    for service, svc in services.items():
        if service != "api":
            env = svc.get("environment") or {}
            assert not _TEMPLATE_VARS & set(env), service

    example = _env_example()
    for name in sorted(_TEMPLATE_VARS):
        assert example.get(name) == "", name
