"""A correlation id for every request.

Two consumers, and they need it in two different places.

1. AUDIT LOGGING (audit item 3.2) requires a request/correlation id on every
   record. Audit writes happen deep inside SQLAlchemy event listeners and
   service functions that have no access to the Request object, and threading
   an id through every signature to reach them is exactly the kind of
   discipline that decays. A ContextVar solves it: set once at the edge,
   readable anywhere in the same task.

2. ERROR RESPONSES (audit item 4.2) must give the caller something traceable
   and nothing else. Previously an unhandled exception produced Starlette's
   bare "Internal Server Error" — no leak, but also nothing anyone could
   correlate to a log line.

WHY THIS IS PURE ASGI MIDDLEWARE AND NOT BaseHTTPMiddleware.

BaseHTTPMiddleware runs the downstream app in a separate anyio task. A
ContextVar set before call_next does propagate into it (a child task inherits
a copy of the context), but the relationship is one-way and easy to get subtly
wrong. Setting the ContextVar in the same coroutine that awaits the downstream
app removes the question entirely — the value is unambiguously visible to
every route handler, service and event listener beneath it.

WHY THE ID IS ALSO WRITTEN TO scope["state"].

Starlette's ServerErrorMiddleware sits ABOVE all user middleware. When an
unhandled exception propagates up to it, this middleware's `finally` has
already reset the ContextVar, so the 500 handler cannot read it there. The
scope dict survives, so the handler reads request.state.request_id instead.
Both are set, deliberately, for these two different readers.
"""
import logging
import re
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"

# An inbound id is echoed into responses and written to log lines, so it is
# untrusted input. A newline in a log line is log injection — a forged entry
# indistinguishable from a real one, which in an audited system is precisely
# the thing not to allow. Conservative allowlist plus a length cap; anything
# that does not match is discarded and replaced rather than cleaned up, since
# a caller sending a malformed id has no legitimate expectation about it.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """The current request's id, or None outside a request.

    None is a legitimate answer, not an error: the APScheduler jobs in
    scheduler_service.py run outside any request and will need to record
    something else as their origin.
    """
    return _request_id.get()


def new_request_id() -> str:
    return uuid.uuid4().hex


def _resolve_request_id(scope: Scope) -> str:
    """Honour a valid upstream id so traces join up across tiers; else mint one."""
    inbound = Headers(scope=scope).get(REQUEST_ID_HEADER)
    if inbound and _SAFE_REQUEST_ID.match(inbound):
        return inbound
    return new_request_id()


class RequestIdMiddleware:
    """Assign an id, expose it, and echo it back.

    Register this LAST in main.py so it is outermost: every response, including
    ones produced by the middleware beneath it (a CSRF rejection, a CORS
    preflight), comes back carrying an id the caller can quote.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope)
        # request.state is backed by this dict; the 500 handler reads it here
        # because by then the ContextVar below has been reset.
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _request_id.reset(token)
