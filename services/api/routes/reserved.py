"""Reserved `/app/*` routes (architecture item 38).

They hold the paths for the owner app and return 501 until identity-authz's `app.py`
lands (I32). `/me/*` is no longer reserved: `me.py` serves it (identity-authz I-A14).
"""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

OWNER = "experience design"
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


async def not_implemented(request: Request) -> JSONResponse:
    return JSONResponse({"owner": OWNER}, status_code=501)


routes = [Route(path, not_implemented, methods=METHODS) for path in ("/app", "/app/{rest:path}")]
