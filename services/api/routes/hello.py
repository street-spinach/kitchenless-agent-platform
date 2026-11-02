"""`POST /hello/run` in `api` (architecture items 31 and 32, build step 6).

It runs the Phase 0 hello flow through `services.agents.hello.main.start_run`, which writes
three trace rows, and returns the run id. The smoke trace test and the `hello` eval scenario
call it. The response shapes match the retired `hello` container.
"""

from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.agents.hello import main as hello

DEFAULT_TEXT = "world"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def hello_run(request: Request) -> JSONResponse:
    try:
        raw = await request.body()
        body: Any = await request.json() if raw else {}
    except ValueError:
        return _error(400, "invalid JSON body")
    if not isinstance(body, dict):
        return _error(400, "invalid JSON body")
    on_behalf_of = body.get("on_behalf_of")
    if on_behalf_of is not None and not isinstance(on_behalf_of, str):
        return _error(400, "on_behalf_of must be a string")
    text = str(body.get("text", DEFAULT_TEXT))
    try:
        # start_run uses the synchronous trace writer, so keep it off the event loop.
        run_id = await run_in_threadpool(hello.start_run, text, on_behalf_of)
    except Exception as err:
        return _error(500, f"run failed: {type(err).__name__}")
    return JSONResponse({"run_id": str(run_id)})


routes = [Route("/hello/run", hello_run, methods=["POST"])]
