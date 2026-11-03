"""infra/Caddyfile routes (experience EX-I45)."""

import re
from pathlib import Path

CADDYFILE = Path(__file__).resolve().parents[2] / "infra" / "Caddyfile"


def _handle_block(text: str, matcher: str) -> str:
    m = re.search(rf"^\s*handle {re.escape(matcher)} \{{(.*?)^\s*\}}", text, re.M | re.S)
    assert m, f"no handle {matcher} block"
    return m.group(1)


def test_owner_route() -> None:
    text = CADDYFILE.read_text()
    assert re.search(r"^\s*redir /owner /owner/\s*$", text, re.M)
    assert _handle_block(text, "/owner/*").strip() == "reverse_proxy api:8000"
    # Before the catch-all 404, so it is reachable.
    assert text.index("handle /owner/*") < text.index('respond "Not found" 404')
