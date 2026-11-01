"""The `/mcp/knowledge` MCP server (architecture §5 and item 43).

`services/mcp/app.py` mounts `server`. Its tools are unguarded (they spend no money, commit
no order and share no personal data), so each is wrapped with `@traced` and runs inside
`meta.tool_errors()`. Tool names and arguments are provisional (§13).

*   `check_allergens(skus, allergen_codes)`: which SKUs carry which of the codes.
*   `find_substitutes(sku, constraints)`: up to 3 same-category products free of
    `constraints["allergen_codes"]` and fitting every `constraints["diet_codes"]`.
*   `explain_path(sku, allergen_code)`: the graph path from the product to the allergen.

If Neo4j is down the adapter raises `AdapterFailure`, which reaches the client as an
`unavailable` error, so callers fail closed (§9).
"""

import dataclasses
import json
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent

from services.common.config import get_settings
from services.mcp.knowledge.adapters.neo4j_knowledge import Knowledge, Neo4jKnowledge
from services.mcp.meta import CallContext, tool_error, tool_errors
from services.mcp.traced import Tool, traced
from services.safety.guard import GuardRefused

__all__ = ["check_allergens", "explain_path", "find_substitutes", "get_knowledge", "server"]

AREA = "knowledge"

server = MCPServer(name=AREA)

_knowledge: Knowledge | None = None


def get_knowledge() -> Knowledge:
    """The process-wide Neo4j adapter, connected on first use."""
    global _knowledge
    if _knowledge is None:
        s = get_settings()
        if not (s.neo4j_uri and s.neo4j_user and s.neo4j_password):
            raise RuntimeError("NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD are required")
        _knowledge = Neo4jKnowledge.connect(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    return _knowledge


@traced(AREA)
async def check_allergens(
    ctx: CallContext, *, skus: list[str], allergen_codes: list[str]
) -> dict[str, Any]:
    return dataclasses.asdict(await get_knowledge().check_allergens(skus, allergen_codes))


@traced(AREA)
async def find_substitutes(
    ctx: CallContext, *, sku: str, constraints: dict[str, list[str]]
) -> dict[str, Any]:
    subs = await get_knowledge().find_substitutes(sku, constraints)
    return {"sku": sku, "substitutes": [dataclasses.asdict(s) for s in subs]}


@traced(AREA)
async def explain_path(ctx: CallContext, *, sku: str, allergen_code: str) -> dict[str, Any]:
    return dataclasses.asdict(await get_knowledge().explain_path(sku, allergen_code))


async def _serve(mcp_ctx: Context, tool: Tool[dict[str, Any]], **args: Any) -> CallToolResult:
    try:
        ctx = await CallContext.from_meta(mcp_ctx.request_context.meta)
    except GuardRefused as e:
        return tool_error("refused", e.reason)
    async with tool_errors(ctx, AREA, tool.__name__) as err:
        body = await tool(ctx, **args)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(body))],
            structured_content=body,
        )
    assert err.result is not None
    return err.result


@server.tool(name="check_allergens")
async def _check_allergens_tool(
    skus: list[str], allergen_codes: list[str], ctx: Context
) -> CallToolResult:
    return await _serve(ctx, check_allergens, skus=skus, allergen_codes=allergen_codes)


@server.tool(name="find_substitutes")
async def _find_substitutes_tool(
    sku: str, constraints: dict[str, list[str]], ctx: Context
) -> CallToolResult:
    return await _serve(ctx, find_substitutes, sku=sku, constraints=constraints)


@server.tool(name="explain_path")
async def _explain_path_tool(sku: str, allergen_code: str, ctx: Context) -> CallToolResult:
    return await _serve(ctx, explain_path, sku=sku, allergen_code=allergen_code)
