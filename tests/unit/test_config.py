"""One test per required-value rule of `services.common.config` (architecture item 2)."""

import dataclasses

import pytest

from services.common import config
from services.common.config import ConfigError, Settings

_REQUIRED = {
    f.name.upper(): f.metadata["services"]
    for f in dataclasses.fields(Settings)
    if f.metadata["default"] is config._NO_DEFAULT
}


def full_env(service: str) -> dict[str, str]:
    """Every value `service` requires, set to a placeholder."""
    env = {name: "x" for name, services in _REQUIRED.items() if service in services}
    env["SERVICE_NAME"] = service
    if "RUN_TOKEN_CEILING" in env:
        env["RUN_TOKEN_CEILING"] = "100000"
    return env


@pytest.mark.parametrize("service", sorted(config.SERVICES))
def test_complete_env_builds(service: str) -> None:
    assert Settings.from_env(full_env(service)).service_name == service


@pytest.mark.parametrize(
    ("service", "name"),
    [(s, n) for n, services in sorted(_REQUIRED.items()) for s in sorted(services)],
)
def test_missing_required_value_raises(service: str, name: str) -> None:
    env = full_env(service)
    env.pop(name)
    with pytest.raises(ConfigError, match=name):
        Settings.from_env(env)


def test_value_required_only_for_services_that_list_it() -> None:
    # WHATSAPP_TOKEN is listed for api only; agents, mcp and migrate start without it.
    for service in ("agents", "mcp", "migrate"):
        assert "WHATSAPP_TOKEN" not in full_env(service)
        assert Settings.from_env(full_env(service)).whatsapp_token is None
    # DATABASE_URL is listed for every service.
    assert all(s in _REQUIRED["DATABASE_URL"] for s in config.SERVICES)


def test_missing_or_unknown_service_name_raises() -> None:
    with pytest.raises(ConfigError, match="SERVICE_NAME"):
        Settings.from_env({})
    with pytest.raises(ConfigError, match="SERVICE_NAME"):
        Settings.from_env({**full_env("api"), "SERVICE_NAME": "hello"})


def test_error_names_missing_values_without_echoing_any() -> None:
    env = full_env("api")
    env.pop("WHATSAPP_TOKEN")
    env["WHATSAPP_APP_SECRET"] = "s3cret-value"
    with pytest.raises(ConfigError) as err:
        Settings.from_env(env)
    assert "s3cret-value" not in str(err.value)


def test_defaulted_values_are_never_required() -> None:
    s = Settings.from_env(full_env("agents"))
    assert (s.approval_ttl_seconds, s.run_timeout_seconds, s.quote_round_seconds) == (
        14400,
        300,
        30,
    )
    assert s.mode == "mock" and s.mode_force_mock is False
    assert s.model_business_timeout_seconds == 5
    assert s.mock_courier_step_seconds == 20


def test_protocol_settings_have_their_defaults() -> None:
    s = Settings.from_env(full_env("agents"))
    assert s.offer_ttl_seconds == 600
    assert s.courier_quote_ttl_seconds == 120
    assert s.shortfall_reply_seconds == 900
    assert s.shortfall_max_lines == 3
    assert s.a2a_demo_timeout_seconds == 900
    assert s.a2a_send_wait_seconds == 10
    assert s.a2a_max_request_bytes == 32768
    assert s.a2a_rate_per_second == 10
    assert s.a2a_demo_token == "change-me-local"
    assert s.agents_url == "http://localhost:8003"


def test_env_overrides_default_and_bad_number_raises() -> None:
    env = {**full_env("agents"), "OFFER_TTL_SECONDS": "60", "MODE_FORCE_MOCK": "1"}
    s = Settings.from_env(env)
    assert s.offer_ttl_seconds == 60 and s.mode_force_mock is True
    with pytest.raises(ConfigError, match="OFFER_TTL_SECONDS"):
        Settings.from_env({**env, "OFFER_TTL_SECONDS": "ten"})


def test_settings_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    config.get_settings.cache_clear()
    for name, value in full_env("api").items():
        monkeypatch.setenv(name, value)
    try:
        first = config.get_settings()
        monkeypatch.setenv("APPROVAL_TTL_SECONDS", "1")
        assert config.get_settings() is first
        with pytest.raises(dataclasses.FrozenInstanceError):
            first.mode = "real"  # type: ignore[misc]
    finally:
        config.get_settings.cache_clear()
