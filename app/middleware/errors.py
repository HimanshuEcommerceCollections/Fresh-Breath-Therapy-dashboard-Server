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
from fastapi.exception_handlers import (
    http_exception_handler as fastapi_http_exception_handler,
)
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.middleware.cache import CACHE_CONTROL
from app.services.audit_context import AuditContext  # noqa: F401  (type context)
from app.services.audit_service import record_denied
from app.services.auth_cookie import ACCESS_TOKEN_COOKIE

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


# ── denied attempts (audit item 3.5) ──────────────────────────────────────
#
# A refusal is often the more interesting signal than a success: it is what a
# probe, a stale link or someone reaching past their caseload looks like.
#
# The two statuses are treated differently on purpose, to keep the log
# readable:
#
#   403 is ALWAYS recorded. It means an authenticated user was refused, which
#       is never routine and always worth a line.
#   401 is recorded ONLY when the request carried a session cookie. The
#       frontend probes /api/auth/me on every page load and takes a 401 as
#       "not signed in", so logging bare 401s would bury the log in ordinary
#       traffic. A 401 WITH a cookie is different — an expired, revoked or
#       forged token — and that is the one worth seeing.

_RESOURCE_FROM_PREFIX = {
    "clients": "client",
    "leads": "lead",
    "sessions": "session",
    "payments": "payment",
    "follow-ups": "follow_up",
    "client-messages": "client_message",
    "therapists": "therapist",
    "imports": "import_batch",
    "exports": "export",
    "reports": "report",
    "dashboard": "dashboard",
    "pto": "pto",
    "notifications": "notification",
    "audit-logs": "audit_log",
    "auth": "user",
    "settings": "settings",
    "uploads": "upload",
    "internal": "internal",
}


def _entity_type_for_path(path: str) -> str:
    """Coarse resource name from a URL path, for grouping denied attempts.

    Intentionally coarse: this is for "someone was refused on clients", not for
    identifying a record. The specific id is not recorded, because on a denial
    we have not established that the caller was entitled to learn it exists.
    """
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "api" and len(parts) > 1:
        return _RESOURCE_FROM_PREFIX.get(parts[1], parts[1])
    return "route"


async def http_exception_audit_handler(request: Request, exc: StarletteHTTPException):
    """Record 401/403 as denied attempts, then answer exactly as before.

    Delegates to FastAPI's own handler so the response body and headers are
    unchanged — this adds an audit record and alters nothing the client sees.
    """
    should_record = exc.status_code == 403 or (
        exc.status_code == 401 and ACCESS_TOKEN_COOKIE in request.cookies
    )
    if should_record:
        # Read the context off the scope, not the ContextVar: exception
        # handlers run above AuditContextMiddleware, whose context has already
        # unwound by the time we get here.
        context = getattr(request.state, "audit_context", None)
        await record_denied(
            _entity_type_for_path(request.url.path),
            exc.status_code,
            context=context,
        )
    return await fastapi_http_exception_handler(request, exc)
