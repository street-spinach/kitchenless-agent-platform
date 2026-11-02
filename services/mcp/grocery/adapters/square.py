"""`SquareRetailAdapter`, the real grocery adapter on the Square sandbox (adapters §7).

The `square_retail` vendor key's `real` class (`services/common/modes.py`). Items come
from `catalog_items`; orders go to the location `SQUARE_RETAIL_LOCATION_ID` with
`SQUARE_RETAIL_TOKEN`. The behaviour is in `services/mcp/square_seller.py`.
"""

from typing import ClassVar

from services.mcp.square_seller import SquareSeller

__all__ = ["SquareRetailAdapter"]


class SquareRetailAdapter(SquareSeller):
    table: ClassVar[str] = "catalog_items"
    token_setting: ClassVar[str] = "square_retail_token"
    location_setting: ClassVar[str] = "square_retail_location_id"
