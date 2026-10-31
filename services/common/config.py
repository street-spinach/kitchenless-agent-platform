"""Settings read once from the environment (architecture item 2, agent-protocol P16).

Each field maps to the upper-cased environment variable of the same name. The services that
use a variable and its default come from the environment matrix in
docs/designs/architecture.md. A variable without a default is required only for the services
that list it; a missing one raises when the settings are built, at startup.
"""

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

_NO_DEFAULT = object()


class ConfigError(RuntimeError):
    """Raised at startup when the environment is incomplete or malformed."""


def _var(services: frozenset[str] | set[str], default: Any = _NO_DEFAULT) -> Any:
    return field(metadata={"services": frozenset(services), "default": default})


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

    mock_token_secret: str | None = _var(APP)

    neo4j_uri: str | None = _var({"mcp", "testrunner"})
    neo4j_user: str | None = _var({"mcp", "testrunner"})
    neo4j_password: str | None = _var({"mcp", "testrunner"})

    openfga_api_url: str | None = _var({"mcp", "testrunner"})
    openfga_store_id: str | None = _var({"mcp", "testrunner"})

    keycloak_url: str | None = _var({"api", "mcp"})

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

    kroger_client_id: str | None = _var({"mcp", "testrunner"})
    kroger_client_secret: str | None = _var({"mcp", "testrunner"})
    kroger_location_id: str | None = _var({"mcp", "testrunner"})

    mock_courier_step_seconds: int = _var({"mcp"}, 20)

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
        hints = typing.get_type_hints(cls)
        values: dict[str, Any] = {}
        missing: list[str] = []
        for f in dataclasses.fields(cls):
            name = f.name.upper()
            default = f.metadata["default"]
            raw = env.get(name)
            if raw is None:
                if default is _NO_DEFAULT:
                    if service in f.metadata["services"]:
                        missing.append(name)
                    default = None
                values[f.name] = default
                continue
            values[f.name] = _parse(name, raw, hints[f.name])
        if missing:
            # Names only: never echo values, some are secrets.
            raise ConfigError(f"missing required settings for {service}: {', '.join(missing)}")
        return cls(**values)


def _parse(name: str, raw: str, hint: Any) -> Any:
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


def _base_type(hint: Any) -> Any:
    if isinstance(hint, types.UnionType):
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        return args[0]
    return hint


@functools.cache
def get_settings() -> Settings:
    """The process-wide settings, built once on first call. Call it at startup."""
    return Settings.from_env()
