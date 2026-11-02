"""Settings read once from the environment (architecture item 2, agent-protocol P16).

Each field maps to the upper-cased environment variable of the same name. The services that
use a variable and its default come from the environment matrix in
docs/designs/architecture.md. A variable without a default is required only for the services
that list it; a missing one raises when the settings are built, at startup.

The identity variables follow the variable table in docs/designs/identity-authz.md §13: some
are required only under certain `TOKEN_SOURCE` values ("R live" means any source but
`mock`), an empty value counts as unset, and PEM values arrive as single-line base64.
"""

import base64
import binascii
import dataclasses
import functools
import os
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

APP = frozenset({"api", "agents", "mcp", "testrunner"})
SERVICES = APP | {"migrate"}

# `TOKEN_SOURCE` values (identity-authz I5). "Live" is every source but `mock`.
TOKEN_SOURCES = frozenset({"mock", "keycloak", "keycloak_azp", "token_service"})
LIVE = TOKEN_SOURCES - {"mock"}

_NO_DEFAULT = object()


class ConfigError(RuntimeError):
    """Raised at startup when the environment is incomplete or malformed."""


def _var(
    services: frozenset[str] | set[str],
    default: Any = _NO_DEFAULT,
    *,
    sources: frozenset[str] | None = None,
    blank_unset: bool = False,
    encoding: str | None = None,
) -> Any:
    """One setting.

    `sources`: required only under these `TOKEN_SOURCE` values (None: under any).
    `blank_unset`: an empty value counts as unset. `encoding`: "pem" decodes single-line
    base64 to PEM text; "base64" only checks the value is base64 and keeps it as given.
    """
    return field(
        metadata={
            "services": frozenset(services),
            "default": default,
            "sources": sources,
            "blank_unset": blank_unset,
            "encoding": encoding,
        }
    )


def _id(services: frozenset[str] | set[str], default: Any = _NO_DEFAULT, **kw: Any) -> Any:
    """An identity-authz §13 setting: an empty value counts as unset."""
    return _var(services, default, blank_unset=True, **kw)


@dataclass(frozen=True)
class Settings:
    # Every app service, and migrate.
    database_url: str | None = _var(SERVICES)
    service_name: str | None = _var(SERVICES)
    log_level: str | None = _var(SERVICES)

    # Timing (run overrides win; architecture item 36).
    approval_ttl_seconds: int = _var(APP, 14400)
    run_timeout_seconds: int = _var(APP, 300)
    quote_round_seconds: int = _var(APP, 30)

    # Adapter modes (architecture §5). MODE defaults to mock; the others are overrides.
    mode: str = _var({"api", "testrunner"}, "mock")
    mode_force_mock: bool = _var({"api", "testrunner"}, False)
    mode_kroger_catalog: str | None = _var({"api", "testrunner"}, None)
    mode_kroger_ordering: str | None = _var({"api", "testrunner"}, None)
    mode_square_retail: str | None = _var({"api", "testrunner"}, None)
    mode_square_restaurant: str | None = _var({"api", "testrunner"}, None)
    mode_doordash: str | None = _var({"api", "testrunner"}, None)

    # Model client (architecture §6).
    model_mode: str | None = _var({"agents", "testrunner"})
    model_base_url: str | None = _var({"agents", "testrunner"})
    model_name: str | None = _var({"agents", "testrunner"})
    model_api_key: str | None = _var({"agents", "testrunner"})
    model_fallback_base_url: str | None = _var({"agents", "testrunner"})
    model_fallback_name: str | None = _var({"agents", "testrunner"})
    model_fallback_api_key: str | None = _var({"agents", "testrunner"})
    run_token_ceiling: int | None = _var({"agents", "testrunner"})
    model_business_name: str = _var({"agents", "testrunner"}, "Muse Glimmer")
    model_business_timeout_seconds: int = _var({"agents", "testrunner"}, 5)

    # Retired by identity-authz §13 in the PR that removes its last reader
    # (services/identity/tokens.py, build step 5).
    mock_token_secret: str | None = _var(APP)

    # Identity (identity-authz §13 and I5).
    token_source: str | None = _id(APP)
    # Empty: derived from DOMAIN (see `from_env`).
    issuer_url: str | None = _id(APP, None)
    domain: str = _id(APP, "localhost")
    # Fixed test-only key committed in infra/.env.example (I-D26); required in every mode.
    mock_signing_key: str | None = _id(APP, encoding="pem")
    agent_signing_key: str | None = _id({"agents", "api"}, sources=LIVE, encoding="pem")
    # Base64 DER, as Keycloak stores it (services/identity/realm/README.md).
    agent_signing_cert: str | None = _id({"api"}, sources=LIVE, encoding="base64")
    grant_encryption_key: str | None = _id({"agents", "api"}, sources=LIVE)
    grant_key_version: int | None = _id({"agents", "api"}, sources=LIVE)
    token_service_key: str | None = _id(
        {"api"}, sources=frozenset({"token_service"}), encoding="pem"
    )
    kc_onboarding_secret: str | None = _id({"api"}, sources=LIVE)
    session_secret: str | None = _id({"api"})
    # Required on every app service and migrate: OpenFGA authentication is on (I16c).
    openfga_preshared_key: str | None = _id(SERVICES)

    neo4j_uri: str | None = _var({"mcp", "testrunner"})
    neo4j_user: str | None = _var({"mcp", "testrunner"})
    neo4j_password: str | None = _var({"mcp", "testrunner"})

    # migrate writes the model (authz/apply.py, I8). The store and model ids are read from
    # fga_models; OPENFGA_STORE_ID is retired (identity-authz §13).
    openfga_api_url: str | None = _var({"mcp", "migrate", "testrunner"})

    # Internal Keycloak URL; required only live (identity-authz §13).
    keycloak_url: str | None = _id({"agents", "api", "mcp"}, sources=LIVE)

    whatsapp_token: str | None = _var({"api"})
    whatsapp_app_secret: str | None = _var({"api"})
    whatsapp_phone_number_id: str | None = _var({"api"})
    whatsapp_verify_token: str | None = _var({"api"})

    doordash_developer_id: str | None = _var({"mcp"})
    doordash_key_id: str | None = _var({"mcp"})
    doordash_signing_secret: str | None = _var({"mcp"})
    doordash_webhook_path_secret: str | None = _var({"api"})

    square_retail_token: str | None = _var({"mcp"})
    square_restaurant_token: str | None = _var({"mcp"})
    square_env: str | None = _var({"mcp"})
    square_retail_location_id: str | None = _var({"mcp"})
    square_restaurant_location_id: str | None = _var({"mcp"})

    kroger_client_id: str | None = _var({"mcp", "testrunner"})
    kroger_client_secret: str | None = _var({"mcp", "testrunner"})
    kroger_location_id: str | None = _var({"mcp", "testrunner"})

    mock_courier_step_seconds: int = _var({"mcp"}, 20)
    # Where the mock courier posts `/internal/delivery-status` (architecture item 46).
    api_url: str = _var({"mcp"}, "http://api:8000")

    # Agent-protocol §13.
    offer_ttl_seconds: int = _var({"agents"}, 600)
    courier_quote_ttl_seconds: int = _var({"agents"}, 120)
    a2a_demo_timeout_seconds: int = _var({"agents"}, 900)
    a2a_send_wait_seconds: int = _var({"agents"}, 10)
    a2a_max_request_bytes: int = _var({"agents"}, 32768)
    a2a_rate_per_second: int = _var({"agents"}, 10)
    shortfall_reply_seconds: int = _var({"agents", "api", "mcp"}, 900)
    shortfall_max_lines: int = _var({"agents", "api", "mcp"}, 3)
    a2a_demo_token: str = _var({"agents", "testrunner"}, "change-me-local")
    agents_url: str = _var({"testrunner"}, "http://localhost:8003")

    seed_us1_phones: str | None = _var({"testrunner"})

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Build settings for the service named by `SERVICE_NAME`; raise if any is missing."""
        env = os.environ if environ is None else environ
        service = env.get("SERVICE_NAME")
        if service not in SERVICES:
            raise ConfigError(f"SERVICE_NAME must be one of {sorted(SERVICES)}, got {service!r}")
        token_source = env.get("TOKEN_SOURCE") or None
        if token_source is not None and token_source not in TOKEN_SOURCES:
            raise ConfigError(f"TOKEN_SOURCE must be one of {sorted(TOKEN_SOURCES)}")
        hints = typing.get_type_hints(cls)
        values: dict[str, Any] = {}
        missing: list[str] = []
        for f in dataclasses.fields(cls):
            name = f.name.upper()
            default = f.metadata["default"]
            raw = env.get(name)
            if raw == "" and f.metadata["blank_unset"]:
                raw = None
            if raw is None:
                if default is _NO_DEFAULT:
                    sources = f.metadata["sources"]
                    if service in f.metadata["services"] and (
                        sources is None or token_source in sources
                    ):
                        missing.append(name)
                    default = None
                values[f.name] = default
                continue
            values[f.name] = _parse(name, raw, hints[f.name], f.metadata["encoding"])
        if missing:
            # Names only: never echo values, some are secrets.
            raise ConfigError(f"missing required settings for {service}: {', '.join(missing)}")
        if values["issuer_url"] is None:
            path = "/tokens" if token_source == "token_service" else "/auth/realms/kitchenless"
            values["issuer_url"] = f"https://{values['domain']}{path}"
        return cls(**values)


def _parse(name: str, raw: str, hint: Any, encoding: str | None = None) -> Any:
    if encoding is not None:
        return _decode(name, raw, encoding)
    base = _base_type(hint)
    if base is bool:
        if raw.strip().lower() in {"1", "true", "yes"}:
            return True
        if raw.strip().lower() in {"", "0", "false", "no"}:
            return False
        raise ConfigError(f"{name} must be a boolean (1/0, true/false)")
    if base is int:
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{name} must be an integer") from None
    return raw


def _decode(name: str, raw: str, encoding: str) -> str:
    """Check single-line base64; for "pem", return the decoded PEM text."""
    try:
        data = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ConfigError(f"{name} must be single-line base64") from None
    if encoding == "base64":
        return raw.strip()
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        text = ""
    if not text.lstrip().startswith("-----BEGIN "):
        raise ConfigError(f"{name} must be a PEM value encoded as single-line base64")
    return text


def _base_type(hint: Any) -> Any:
    if isinstance(hint, types.UnionType):
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        return args[0]
    return hint


@functools.cache
def get_settings() -> Settings:
    """The process-wide settings, built once on first call. Call it at startup."""
    return Settings.from_env()
