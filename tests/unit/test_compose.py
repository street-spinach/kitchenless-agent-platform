"""Compose environment wiring (experience EX-I15)."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "https://inference.baseten.co/v1"),  # unset: the #392 default
        ("", ""),  # explicitly empty: the primary host
        ("http://own.invalid/v1", "http://own.invalid/v1"),
    ],
)
def test_business_host_default_applies_only_when_unset(value: str | None, expected: str) -> None:
    """#444. Compose and the shell read `${VAR-default}` the same way, so `sh` expands it."""
    services = yaml.safe_load((_INFRA / "docker-compose.yml").read_text())["services"]
    template = services["agents"]["environment"]["MODEL_BUSINESS_BASE_URL"]
    env = {k: v for k, v in os.environ.items() if k != "MODEL_BUSINESS_BASE_URL"}
    if value is not None:
        env["MODEL_BUSINESS_BASE_URL"] = value
    result = subprocess.run(
        ["sh", "-c", f'printf %s "{template}"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == expected


def test_agents_and_env_example_get_self_hosted_flag() -> None:
    services = yaml.safe_load((_INFRA / "docker-compose.yml").read_text())["services"]
    flag = services["agents"]["environment"]["MODEL_BUSINESS_SELF_HOSTED"]
    assert flag == "${MODEL_BUSINESS_SELF_HOSTED:-false}"
    assert _env_example()["MODEL_BUSINESS_SELF_HOSTED"] == "false"


def test_api_reaches_openfga() -> None:
    """api writes tuples (onboarding, and `/panel/reset` reconciling after an eval reset,
    identity-authz §7 and I27), so it needs the OpenFGA URL and preshared key. Without the
    URL every eval reset failed with `OPENFGA_API_URL is required`."""
    services = yaml.safe_load((_INFRA / "docker-compose.yml").read_text())["services"]
    api_env = services["api"]["environment"]
    assert api_env.get("OPENFGA_API_URL") == "${OPENFGA_API_URL:-http://openfga:8080}"
    assert "OPENFGA_PRESHARED_KEY" in api_env
