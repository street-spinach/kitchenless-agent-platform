"""Reserved `/app/*` and `/me/*` routes (architecture item 38).

They hold the paths for the experience design and return 501 until it lands.
"""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

OWNER = "experience design"
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


async def not_implemented(request: Request) -> JSONResponse:
    return JSONResponse({"owner": OWNER}, status_code=501)


routes = [
    Route(path, not_implemented, methods=METHODS)
    for path in ("/app", "/app/{rest:path}", "/me", "/me/{rest:path}")
]
