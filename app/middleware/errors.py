"""The global handler for anything that escapes a route.

Before this, an unhandled exception hit Starlette's ServerErrorMiddleware and
returned a bare `Internal Server Error`. With debug off that leaks nothing —
but it also gives the caller nothing to quote and nothing to correlate against
a log line, which is the other half of audit item 4.2.

The rule this enforces: the response body carries a request id and NOTHING
else. Never str(exc). Exception strings in this codebase can contain row data
and SQL fragments — a failed insert renders the parameters it was given, which
for these tables means client names, emails and phone numbers. The traceback
goes to the server log, where the id ties it back to what the user saw.
"""
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.middleware.cache import CACHE_CONTROL

logger = logging.getLogger(__name__)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)

    # exc_info rather than interpolating the exception: the message stays a
    # fixed string, so nothing PHI-bearing lands in the log MESSAGE, while the
    # traceback still records what actually happened for whoever investigates.
    logger.error(
        "Unhandled exception on %s %s (request_id=%s)",
        request.method,
        request.url.path,
        request_id,
        exc_info=exc,
    )

    # Headers set explicitly rather than inherited. ServerErrorMiddleware sits
    # ABOVE all user middleware, so a response built here does not travel back
    # out through NoStoreCacheMiddleware or SecurityHeadersMiddleware — their
    # send wrappers were skipped when the exception propagated past them. Only
    # the directives that matter on an error body are repeated here.
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An unexpected error occurred. Quote the request id when reporting this.",
            "request_id": request_id,
        },
        headers={
            "Cache-Control": CACHE_CONTROL,
            "X-Content-Type-Options": "nosniff",
            "X-Request-ID": request_id or "",
        },
    )
