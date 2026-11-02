"""The `/mcp/grocery` MCP server (architecture §5 and item 43).

`services/mcp/app.py` mounts `server`. Its tools are the seller tools in
`services/mcp/seller.py`: `search_items`, `quote` and `place_order` (*guarded*). Mock
cards use `MockStoreAdapter`; Square cards resolve `square_retail`, and Kroger cards
resolve `kroger_catalog` for search and pricing and `kroger_ordering` (always mock) for
orders.
"""

from mcp.server.mcpserver import MCPServer

from services.mcp import seller
from services.mcp.grocery.adapters.mock_store import MockStoreAdapter

__all__ = ["place_order", "quote", "search_items", "server", "tools"]

AREA = "grocery"

server = MCPServer(name=AREA)

tools = seller.SellerTools(
    area=AREA,
    mock_adapter=MockStoreAdapter,
    vendor_keys={
        "square": dict.fromkeys(("search_items", "quote", "place_order"), "square_retail"),
        "kroger": {
            "search_items": "kroger_catalog",
            "quote": "kroger_catalog",
            "place_order": "kroger_ordering",
        },
    },
)
_tools = tools.build()
search_items = _tools["search_items"]
quote = _tools["quote"]
place_order = _tools["place_order"]

seller.register(server, _tools, AREA)
