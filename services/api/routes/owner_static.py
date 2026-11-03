"""The owner app's web export at `/owner/` (experience.md S5, EX-I45).

`infra/Dockerfile` builds `apps/owner` with `expo export --platform web` and copies `dist/` to
`OWNER_DIST` (default `/app/owner_dist`). A path that names a file under it is served as that
file; any other path gets `index.html`, so the app's own routes (`/owner/auth`, the tabs)
load. Every response carries the CSP below: scripts and connections only from this origin;
inline styles allowed, because React Native Web injects its styles at runtime (EX-I45,
amended 2026-11-03).
A path that resolves outside `OWNER_DIST` is never served, and without an export the route
answers 404.
"""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

from services.common.config import get_settings

CSP = "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'"
INDEX = "index.html"


def dist_root() -> Path:
    return Path(get_settings().owner_dist)


def _file(root: Path, rel: str) -> Path | None:
    """The file `rel` names under `root`, or None (missing, a directory, or outside `root`)."""
    if not rel:
        return None
    root = root.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def _with_csp(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = CSP
    return response


async def owner(request: Request) -> Response:
    root = dist_root()
    path = _file(root, request.path_params.get("path", "")) or _file(root, INDEX)
    if path is None:
        return _with_csp(PlainTextResponse("Owner app not built", status_code=404))
    return _with_csp(FileResponse(path))


routes = [Route("/owner/{path:path}", owner, methods=["GET", "HEAD"])]
