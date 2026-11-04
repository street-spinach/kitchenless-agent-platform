"""The brand tokens (showcase.md §2, SG1): `brand.css` is the source; the owner app's
`theme.ts` (`export const brand`) and the Keycloak login theme CSS hold copies that must
match it exactly. A copy that doesn't exist yet is skipped with the step that adds it."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BRAND_CSS = ROOT / "services/api/static/brand.css"
LOGO = ROOT / "services/api/static/brand/spinach-icon.png"
OWNER_THEME = ROOT / "apps/owner/src/ui/theme.ts"
KEYCLOAK_THEME = ROOT / "services/identity/realm/theme/kitchenless/login"
KEYCLOAK_CSS = KEYCLOAK_THEME / "resources/css/kitchenless.css"

# showcase.md §2, in table order.
EXPECTED = {
    "green-900": "#123710",
    "green-700": "#1e6b1c",
    "green-600": "#268923",
    "green-500": "#2a9727",
    "green-50": "#eaf5e9",
    "lime": "#8ecc06",
    "orange": "#db5f00",
    "ink": "#262b25",
    "ink-soft": "#323831",
    "muted": "#5a6559",
    "muted-2": "#7b847a",
    "line": "rgba(38, 43, 37, 0.08)",
    "bg": "#f8fbf7",
    "bg-2": "#efefee",
    "white": "#ffffff",
    "danger": "#e03030",
    "radius-card": "24px",
    "radius-control": "12px",
    "shadow": "0 10px 30px rgba(38, 43, 37, 0.06)",
    "shadow-lift": "0 22px 70px rgba(18, 55, 16, 0.14)",
    "gradient": "linear-gradient(91deg, #1e6b1c, #2a9727)",
    "font": 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
}

_CSS_TOKEN = re.compile(r"--brand-([a-z0-9-]+)\s*:\s*([^;]+);")


def css_tokens(text: str) -> dict[str, str]:
    return {name: " ".join(value.split()) for name, value in _CSS_TOKEN.findall(text)}


def camel(name: str) -> str:
    """`green-900` → `green900`, `ink-soft` → `inkSoft`, `muted-2` → `muted2`."""
    head, *rest = name.split("-")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def ts_brand(text: str) -> dict[str, str] | None:
    """The `export const brand = { key: "value", ... }` object of theme.ts, or None."""
    block = re.search(r"export const brand\s*=\s*\{(.*?)\}\s*as const", text, re.S)
    if block is None:
        return None
    pairs = re.findall(r"(\w+)\s*:\s*(['\"])(.*?)\2\s*,", block.group(1), re.S)
    return {key: value for key, _, value in pairs}


def test_brand_css_holds_every_token() -> None:
    assert css_tokens(BRAND_CSS.read_text()) == EXPECTED


def test_logo_is_a_png() -> None:
    assert LOGO.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_owner_theme_matches() -> None:
    brand = ts_brand(OWNER_THEME.read_text())
    if brand is None:
        pytest.skip("theme.ts has no `brand` export yet (showcase SC3)")
    assert brand == {camel(name): value for name, value in EXPECTED.items()}


def test_keycloak_theme_matches() -> None:
    if not KEYCLOAK_CSS.exists():
        pytest.skip("no Keycloak login theme yet (showcase SC4)")
    assert css_tokens(KEYCLOAK_CSS.read_text()) == EXPECTED


@pytest.mark.parametrize(
    ("name", "expected"),
    [("green-900", "green900"), ("ink-soft", "inkSoft"), ("muted-2", "muted2"), ("bg", "bg")],
)
def test_camel(name: str, expected: str) -> None:
    assert camel(name) == expected


def test_ts_brand_parses() -> None:
    text = 'export const brand = {\n  green900: "#123710",\n  font: \'a, "b"\',\n} as const;'
    assert ts_brand(text) == {"green900": "#123710", "font": 'a, "b"'}
