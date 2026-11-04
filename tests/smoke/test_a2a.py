"""The internal A2A endpoint on the live stack (agent-protocol §11, P40). Run inside the Compose
network via `make a2a-smoke` (testrunner)."""

import json
import urllib.request

import pytest

from tests.smoke.test_health import caddy_status_line

AGENT = "grocery:mock-us"
CARD_PATH = f"/a2a/{AGENT}/.well-known/agent-card.json"


def test_card_served_on_docker_network() -> None:
    with urllib.request.urlopen(f"http://agents:8003{CARD_PATH}", timeout=5) as resp:
        assert resp.status == 200
        card = json.loads(resp.read())
    assert card["name"] == AGENT
    assert card["supportedInterfaces"][0]["url"] == f"http://agents:8003/a2a/{AGENT}"


@pytest.mark.parametrize("method,path", [("POST", f"/a2a/{AGENT}"), ("GET", CARD_PATH)])
def test_a2a_not_reachable_through_caddy(method: str, path: str) -> None:
    assert caddy_status_line(method, path).startswith(b"HTTP/1.1 404")
