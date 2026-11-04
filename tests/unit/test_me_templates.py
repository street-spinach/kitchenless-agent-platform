"""The /me page templates and stylesheet (experience.md §8, EX-I35)."""

import re
from pathlib import Path
from typing import Any

import pytest
from starlette.templating import Jinja2Templates

TEMPLATES = Path("services/api/templates")
CSS = Path("services/api/static/me.css")
PAGES = ("household", "pantry", "history", "agents", "band", "error")

BASE: dict[str, Any] = {
    "person": {"name": "Maya"},
    "household": {"name": "The Rao household"},
    "tab": "household",
}
CONTEXTS: dict[str, dict[str, Any]] = {
    "household": {
        "household_wide": True,
        "people": [{"name": "Maya", "allergies": ["tree nuts"], "diets": ["vegetarian"]}],
        "order": {
            "summary": "Groceries from Store B, Thursday 6:00 pm-7:00 pm",
            "state": "Courier booked",
            "explanation": "Rice kept because you ran out.",
        },
        "spending": {"spent": "$84.20", "budget": "$150.00"},
        "take_turns": {"on": True, "can_switch": True, "csrf": "tok-tt"},
        "meals": [
            {
                "order_id": "6f0f0a5e-0000-4000-8000-000000000001",
                "title": "Dal and rice",
                "delivered": "Tue 5 Nov",
                "totals": {"up": 3, "down": 1},
                "mine": 1,
                "csrf": "tok-r1",
            }
        ],
    },
    "pantry": {
        "enabled": True,
        "can_correct": True,
        "items": [
            {"name": "rice", "state": "at_home", "checked": "2 days ago", "csrf": "tok-p1"},
            {"name": "eggs", "state": "unsure", "checked": None, "csrf": "tok-p2"},
            {"name": "milk", "state": "out", "checked": "today", "csrf": "tok-p3"},
        ],
    },
    "history": {
        "tab": "history",
        "spending": [
            {
                "week": "Week of 2 Nov",
                "total": "$84.20",
                "orders": [{"at": "Tue 4 Nov", "amount": "$84.20", "label": "Groceries"}],
            }
        ],
        "approvals": [{"at": "Tue 4 Nov 18:02", "amount": "$84.20", "status": "approved"}],
        "events": [{"at": "Tue 4 Nov 18:02", "text": "Approval decided"}],
    },
    "agents": {
        "tab": None,
        "agents": [
            {
                "agent_id": "consumer:us-1-a",
                "label": "Your agent",
                "until": "2026-12-03",
                "can_revoke": True,
                "csrf": "tok-a1",
            },
            {
                "agent_id": "household:us-1",
                "label": "Household agent",
                "until": None,
                "can_revoke": False,
                "csrf": "tok-a2",
            },
        ],
    },
    "band": {
        "tab": None,
        "band": {"amount": "$60.00", "per_order_minor": 6000},
        "currency": "USD",
        "can_set": True,
        "csrf": "tok-b",
    },
    "error": {
        "person": None,
        "household": None,
        "tab": None,
        "heading": "Session expired",
        "message": "This page timed out. Write *my page* in WhatsApp for a new link.",
    },
}


@pytest.fixture(scope="module")
def templates() -> Jinja2Templates:
    # Starlette's engine, as the /me routes use it.
    return Jinja2Templates(directory=str(TEMPLATES))


def _render(templates: Jinja2Templates, page: str, **extra: Any) -> str:
    context = {**BASE, **CONTEXTS[page], **extra}
    return templates.get_template(f"{page}.html").render(context)


def test_every_template_renders(templates: Jinja2Templates) -> None:
    # `screen.html` is the panel's presenter view (showcase §7), not a /me page: it has no
    # /me frame and is tested in test_panel_screen.py.
    assert sorted(p.stem for p in TEMPLATES.glob("*.html")) == sorted(("base", "screen", *PAGES))
    for page in PAGES:
        out = _render(templates, page)
        assert out.startswith("<!doctype html>"), page
        assert '<link rel="stylesheet" href="/me/static/me.css">' in out
        assert 'name="viewport"' in out
        # Three tabs, links on a sub-path of /me.
        tabs = re.findall(r'<a href="(/me/[a-z]+)"', out)
        assert tabs == ["/me/household", "/me/pantry", "/me/history"], page
        # Every form posts under /me and carries its CSRF field first.
        for form in re.findall(r"<form[^>]*>.*?</form>", out, re.S):
            assert re.search(r'method="post" action="/me/', form), (page, form)
            assert re.search(r'<input type="hidden" name="csrf" value="tok-[a-z0-9-]+">', form)
        # The only script is the switch's onchange.
        assert "<script" not in out
        handlers = re.findall(r"\son[a-z]+=", out)
        assert handlers == ([" onchange="] if page == "household" else []), page
        assert out.count('onchange="this.form.submit()"') == (1 if page == "household" else 0)

    household = _render(templates, "household")
    assert "Maya" in household and "The Rao household" in household
    assert '<a href="/me/household" aria-current="page">' in household
    assert "$84.20 of $150.00 this week" in household
    assert "Take turns is on." in household
    assert "👍 3 · 👎 1" in household
    assert 'action="/me/meals/6f0f0a5e-0000-4000-8000-000000000001/reaction"' in household

    # A non-approver sees the switch read-only, with no form; hidden totals do not show.
    member = _render(
        templates,
        "household",
        take_turns={"on": False, "can_switch": False, "csrf": "x"},
        meals=[{**CONTEXTS["household"]["meals"][0], "totals": None}],
        order=None,
    )
    assert "onchange" not in member
    assert 'action="/me/household/take-turns"' not in member
    assert '<input type="checkbox" disabled>' in member
    assert "Take turns is off." in member
    assert "👍 3" not in member
    assert "No order yet this week." in member

    pantry = _render(templates, "pantry")
    assert [h for h in re.findall(r"<h2[^>]*>([^<]+)</h2>", pantry)] == [
        "At home",
        "Not sure",
        "Out",
    ]
    assert 'action="/me/pantry/rice"' in pantry
    assert pantry.count('value="verify">Still have it') == 3
    assert pantry.count('value="out">Ran out') == 3
    # A child sees the states but no correction buttons.
    child = _render(templates, "pantry", can_correct=False)
    assert "Ran out" not in child and "<form" not in child
    off = _render(templates, "pantry", enabled=False)
    assert "Pantry tracking isn't on for your household yet." in off
    assert "rice" not in off

    band = _render(templates, "band", can_set=False, band=None)
    assert "No per-order band is set." in band and "<form" not in band

    error = _render(templates, "error")
    assert "<title>Session expired</title>" in error
    assert "This page timed out." in error


def test_autoescape_on(templates: Jinja2Templates) -> None:
    attack = '<script>alert("x")</script>'
    assert templates.env.autoescape is True or callable(templates.env.autoescape)
    for page in PAGES:
        out = _render(
            templates,
            page,
            person={"name": attack},
            household={"name": attack},
            message=attack,
            heading=attack,
        )
        assert attack not in out, page
        assert "&lt;script&gt;" in out, page

    household = _render(
        templates,
        "household",
        order={"summary": attack, "state": attack, "explanation": attack},
        meals=[{**CONTEXTS["household"]["meals"][0], "title": attack}],
    )
    assert "<script" not in household
    pantry = _render(
        templates,
        "pantry",
        items=[{"name": 'x"><script>', "state": "out", "checked": attack, "csrf": "t"}],
    )
    assert "<script" not in pantry
    # A quote cannot break out of an attribute.
    agents = _render(
        templates,
        "agents",
        agents=[
            {
                "agent_id": "a",
                "label": "b",
                "until": "c",
                "can_revoke": True,
                "csrf": '"><script>',
            }
        ],
    )
    assert "<script" not in agents
    assert 'value="&#34;&gt;&lt;script&gt;"' in agents


def test_stylesheet() -> None:
    css = CSS.read_text()
    assert "system-ui" in css
    assert "width=device-width" in (TEMPLATES / "base.html").read_text()
    assert "@import" not in css and "url(" not in css  # nothing fetched from elsewhere
