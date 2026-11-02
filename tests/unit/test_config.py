"""One test per required-value rule of `services.common.config` (architecture item 2)."""

import base64
import dataclasses

import pytest

from services.common import config
from services.common.config import ConfigError, Settings

_FIELDS = {f.name.upper(): f for f in dataclasses.fields(Settings)}
_REQUIRED = {
    name: f.metadata["services"]
    for name, f in _FIELDS.items()
    if f.metadata["default"] is config._NO_DEFAULT
}
# The TOKEN_SOURCE values under which each required value is required (None: any).
_SOURCES = {name: _FIELDS[name].metadata["sources"] for name in _REQUIRED}

_PEM = base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nplaceholder\n").decode()
_PLACEHOLDER = {
    "RUN_TOKEN_CEILING": "100000",
    "GRANT_KEY_VERSION": "1",
    "AGENT_SIGNING_CERT": base64.b64encode(b"der").decode(),
}


def _required(name: str, service: str, source: str) -> bool:
    sources = _SOURCES[name]
    return service in _REQUIRED[name] and (sources is None or source in sources)


def full_env(service: str, source: str = "mock") -> dict[str, str]:
    """Every value `service` requires under TOKEN_SOURCE `source`, set to a placeholder."""
    env = {}
    for name in _REQUIRED:
        if _required(name, service, source):
            pem = _FIELDS[name].metadata["encoding"] == "pem"
            env[name] = _PEM if pem else _PLACEHOLDER.get(name, "x")
    if "TOKEN_SOURCE" in env:
        env["TOKEN_SOURCE"] = source
    env["SERVICE_NAME"] = service
    return env


@pytest.mark.parametrize(
    ("service", "source"),
    [(s, t) for s in sorted(config.SERVICES) for t in sorted(config.TOKEN_SOURCES)],
)
def test_complete_env_builds(service: str, source: str) -> None:
    assert Settings.from_env(full_env(service, source)).service_name == service


@pytest.mark.parametrize(
    ("service", "name", "source"),
    [
        (s, n, t)
        for n, services in sorted(_REQUIRED.items())
        for s in sorted(services)
        for t in sorted(config.TOKEN_SOURCES)
        if _required(n, s, t)
    ],
)
def test_missing_required_value_raises(service: str, name: str, source: str) -> None:
    env = full_env(service, source)
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
    assert s.api_url == "http://api:8000"


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


def test_square_location_ids_read_for_mcp() -> None:
    env = {
        **full_env("mcp"),
        "SQUARE_RETAIL_LOCATION_ID": "L-RETAIL",
        "SQUARE_RESTAURANT_LOCATION_ID": "L-KITCHEN",
    }
    s = Settings.from_env(env)
    assert (s.square_retail_location_id, s.square_restaurant_location_id) == (
        "L-RETAIL",
        "L-KITCHEN",
    )
    assert config.Settings.__dataclass_fields__["square_retail_location_id"].metadata[
        "services"
    ] == {"mcp"}


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


def test_mcp_starts_live_without_grant_key() -> None:
    # GRANT_ENCRYPTION_KEY is R live on agents and api only (identity-authz §13).
    for source in sorted(config.LIVE):
        env = full_env("mcp", source)
        assert "GRANT_ENCRYPTION_KEY" not in env and "KEYCLOAK_URL" in env
        s = Settings.from_env(env)
        assert s.token_source == source and s.grant_encryption_key is None


def test_api_refuses_live_without_grant_key() -> None:
    for source in sorted(config.LIVE):
        env = full_env("api", source)
        env.pop("GRANT_ENCRYPTION_KEY")
        with pytest.raises(ConfigError, match="GRANT_ENCRYPTION_KEY"):
            Settings.from_env(env)
        # Compose passes unset values as empty strings: empty is missing too.
        with pytest.raises(ConfigError, match="GRANT_ENCRYPTION_KEY"):
            Settings.from_env({**env, "GRANT_ENCRYPTION_KEY": ""})
    # Under mock, api starts without it.
    assert Settings.from_env(full_env("api")).grant_encryption_key is None


def test_mock_key_required() -> None:
    # Required on agents, api, mcp and testrunner in every mode (I5, I-D26); not on migrate.
    for service in sorted(config.APP):
        for source in sorted(config.TOKEN_SOURCES):
            env = full_env(service, source)
            for value in (None, ""):
                if value is None:
                    env.pop("MOCK_SIGNING_KEY", None)
                else:
                    env["MOCK_SIGNING_KEY"] = value
                with pytest.raises(ConfigError, match="MOCK_SIGNING_KEY"):
                    Settings.from_env(env)
    assert "MOCK_SIGNING_KEY" not in full_env("migrate")
    assert Settings.from_env(full_env("migrate")).mock_signing_key is None


def test_env_example_mock_key_is_a_pem_rsa_key() -> None:
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    example = Path(__file__).resolve().parents[2] / "infra" / ".env.example"
    lines = example.read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("MOCK_SIGNING_KEY="))
    assert lines[i - 1] == "# TEST ONLY - never trusted for live households"
    env = {**full_env("mcp"), "MOCK_SIGNING_KEY": lines[i].split("=", 1)[1]}
    pem = Settings.from_env(env).mock_signing_key
    assert pem is not None
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)


def test_pem_values_decoded_from_base64() -> None:
    env = full_env("api", "token_service")
    s = Settings.from_env(env)
    assert s.token_service_key == "-----BEGIN PRIVATE KEY-----\nplaceholder\n"
    for bad in ("not base64!", base64.b64encode(b"not a pem").decode()):
        with pytest.raises(ConfigError, match="TOKEN_SERVICE_KEY") as err:
            Settings.from_env({**env, "TOKEN_SERVICE_KEY": bad})
        assert bad not in str(err.value)


def test_token_source_must_be_known() -> None:
    with pytest.raises(ConfigError, match="TOKEN_SOURCE"):
        Settings.from_env({**full_env("mcp"), "TOKEN_SOURCE": "none"})
    with pytest.raises(ConfigError, match="TOKEN_SOURCE"):
        Settings.from_env({**full_env("mcp"), "TOKEN_SOURCE": ""})


def test_keycloak_url_required_only_live() -> None:
    for service in ("agents", "api", "mcp"):
        assert "KEYCLOAK_URL" not in full_env(service, "mock")
        assert "KEYCLOAK_URL" in full_env(service, "keycloak")
    assert "KEYCLOAK_URL" not in full_env("testrunner", "keycloak")


def test_issuer_url_derived_from_domain() -> None:
    env = {**full_env("mcp"), "DOMAIN": "example.org"}
    assert Settings.from_env(env).issuer_url == "https://example.org/auth/realms/kitchenless"
    env = {**full_env("api", "token_service"), "DOMAIN": "example.org", "ISSUER_URL": ""}
    assert Settings.from_env(env).issuer_url == "https://example.org/tokens"
    env = {**full_env("mcp"), "ISSUER_URL": "https://issuer.test/x"}
    assert Settings.from_env(env).issuer_url == "https://issuer.test/x"


_TEMPLATE_SETTINGS = (
    "wa_tpl_approval_request",
    "wa_tpl_take_turns_offer",
    "wa_tpl_order_summary",
    "wa_tpl_consent_renewal",
    "wa_tpl_consent_revoked",
    "wa_tpl_reminder_delivery_today",
    "wa_tpl_reminder_pantry_summary",
)


def test_template_settings_default_empty() -> None:
    # Experience EX-I7: empty until a template is approved; never required.
    s = Settings.from_env(full_env("api"))
    for name in _TEMPLATE_SETTINGS:
        assert getattr(s, name) == "", name
        assert _FIELDS[name.upper()].metadata["services"] == {"api"}
    assert s.insights_sample is False
    env = {
        **full_env("api"),
        "WA_TPL_APPROVAL_REQUEST": "approval_request_v1",
        "INSIGHTS_SAMPLE": "1",
    }
    s = Settings.from_env(env)
    assert s.wa_tpl_approval_request == "approval_request_v1"
    assert s.insights_sample is True
