"""`SquareRestaurantAdapter`, the real restaurant adapter on the Square sandbox (adapters §7).

The `square_restaurant` vendor key's `real` class (`services/common/modes.py`). Items come
from `menu_items`; orders go to the location `SQUARE_RESTAURANT_LOCATION_ID` with
`SQUARE_RESTAURANT_TOKEN`. The behaviour is in `services/mcp/square_seller.py`.
"""

from typing import ClassVar

from services.mcp.square_seller import SquareSeller

__all__ = ["SquareRestaurantAdapter"]


class SquareRestaurantAdapter(SquareSeller):
    table: ClassVar[str] = "menu_items"
    token_setting: ClassVar[str] = "square_restaurant_token"
    location_setting: ClassVar[str] = "square_restaurant_location_id"
