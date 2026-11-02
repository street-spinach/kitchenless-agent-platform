"""The `/mcp/restaurant` MCP server (architecture §5 and item 43).

`services/mcp/app.py` mounts `server`. Its tools are the seller tools in
`services/mcp/seller.py`: `search_items`, `quote`, and the *guarded* `place_order` and
`amend_order`. Mock cards use `MockRestaurantAdapter`; Square cards resolve `square_restaurant`
for every tool (`vendors.card_vendor_key`).
"""

from mcp.server.mcpserver import MCPServer

from services.mcp import seller
from services.mcp.restaurant.adapters.mock_restaurant import MockRestaurantAdapter

__all__ = ["amend_order", "place_order", "quote", "search_items", "server", "tools"]

AREA = "restaurant"

server = MCPServer(name=AREA)

tools = seller.SellerTools(
    area=AREA,
    mock_adapter=MockRestaurantAdapter,
)
_tools = tools.build()
search_items = _tools["search_items"]
quote = _tools["quote"]
place_order = _tools["place_order"]
amend_order = _tools["amend_order"]

seller.register(server, _tools, AREA)
