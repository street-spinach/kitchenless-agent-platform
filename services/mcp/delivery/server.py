"""The `/mcp/delivery` MCP server (architecture §5 and item 43).

`services/mcp/app.py` mounts `server`. Its tools are added by the item 43 issues.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="delivery")
