"""OpenFGA relation checks for guarded tools (architecture §5 step 2, item 54).

Until the identity-authz design defines the model, the relations in §5 are seeded as static
tuples (§13). A check that times out (2 s) counts as a refusal.
"""

import functools

import urllib3
from openfga_sdk import ClientConfiguration
from openfga_sdk.client.models import ClientCheckRequest
from openfga_sdk.configuration import RetryParams
from openfga_sdk.sync import OpenFgaClient

from services.common.config import get_settings

TIMEOUT_MILLISEC = 2000


@functools.cache
def _client() -> OpenFgaClient:
    settings = get_settings()
    if not settings.openfga_api_url or not settings.openfga_store_id:
        raise RuntimeError("OPENFGA_API_URL and OPENFGA_STORE_ID are required")
    return OpenFgaClient(
        ClientConfiguration(
            api_url=settings.openfga_api_url,
            store_id=settings.openfga_store_id,
            timeout_millisec=TIMEOUT_MILLISEC,
            # No retries, so one check never takes longer than the timeout.
            retry_params=RetryParams(max_retry=0),
        )
    )


def check(user: str, relation: str, object: str) -> bool:  # `object` is the design signature
    """True only if OpenFGA allows `user relation object`. A timeout returns False."""
    try:
        response = _client().check(ClientCheckRequest(user=user, relation=relation, object=object))
    except urllib3.exceptions.TimeoutError:
        return False
    return response.allowed is True
