"""The household stub's fixed lists against the seed menus (evals §12 case 13).

A stub dinner item is sent to restaurants as a search query, and every word of the query
must match, so a name that isn't on a menu ends the run `no_offers`.
"""

from pathlib import Path

import yaml

from services.agents.household import stubs
from services.common import dislikes

MENU = Path(__file__).resolve().parents[2] / "services/seed/data/menu_mock.yaml"


def _dish() -> dict[str, str]:
    menus = yaml.safe_load(MENU.read_text())["menus"]
    return {i["sku"]: i["name"] for i in menus["restaurant:mock-us"]}


def test_dinner_stub_lists_the_chicken_rice_dish() -> None:
    [line] = stubs.LISTS["dinner"]
    assert line["item"] == _dish()["MOCK-DIN-CHICKEN-RICE"]


def test_dinner_stub_item_matches_the_case_dislike() -> None:
    [line] = stubs.LISTS["dinner"]
    assert dislikes.matches(line["item"], "rice")
