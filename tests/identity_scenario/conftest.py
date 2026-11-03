"""The identity scenario runs from the host against `make up` (identity-authz I43), so it
uses the stack's own settings, not the unit tests' mock signing key."""

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def identity_settings() -> Iterator[None]:
    """Overrides `tests/conftest.py`'s fixture of the same name: no settings are patched."""
    yield


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Never against a deployed realm: it creates people and grants (I43).
    if os.environ.get("DOMAIN") != "localhost":
        raise pytest.UsageError("tests/identity_scenario runs only with DOMAIN=localhost")
