"""`GET /internal/sim/outbox?phone=` reads an unencoded E.164 `+` (issue #40).

A raw `+` in a query string decodes to a space, so `?phone=+1555...` used to match no rows.
"""

import pytest
from starlette.datastructures import QueryParams

from services.api.routes.internal import _phone_param


@pytest.mark.parametrize("query", ["phone=+15550100202", "phone=%2B15550100202"])
def test_phone_param_keeps_the_plus(query: str) -> None:
    assert _phone_param(QueryParams(query).get("phone")) == "+15550100202"


@pytest.mark.parametrize("raw", [None, "", "15550100202"])
def test_phone_param_leaves_other_values(raw: str | None) -> None:
    assert _phone_param(raw) == raw
