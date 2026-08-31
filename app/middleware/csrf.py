"""CSRF protection for cookie-authenticated state-changing requests.

WHY THIS EXISTS, GIVEN THERE IS ALREADY A CORS ALLOWLIST.

CORS is not a request filter. Its job is to stop the *calling page* from
reading a response it should not see; it says nothing about whether the
request is allowed to happen. For most cross-site requests the browser sends
them, lets the server run them, and then merely withholds the reply from the
attacker's JavaScript. For a write endpoint the attacker does not want the
reply — the side effect IS the payload, and a blind write is still a write.

The one case where CORS does prevent arrival is the preflight, and browsers
only preflight requests they consider non-"simple". A request is simple when
it is GET/HEAD/POST, carries no unusual headers, and its Content-Type is one
of `application/x-www-form-urlencoded`, `multipart/form-data`, `text/plain`
— or is absent entirely. So the protection does not fall where intuition puts
it. PUT, PATCH and DELETE are always preflighted and therefore were already
safe here. Plain POST was not, and two concrete holes existed:

  1. NO Content-Type AT ALL. FastAPI parses a body as JSON when the header is
     missing (`if not content_type_value: json_body = await request.json()`),
     and a fetch() with a typeless Blob body sends no Content-Type, so the
     request stays simple, skips the preflight, and executes. Measured
     against this API before this middleware: POST /api/clients with a
     foreign Origin, a valid session cookie and no Content-Type returned
     201 Created.

  2. MULTIPART FORMS, which need no JavaScript at all — multipart/form-data
     is safelisted, so a cross-site auto-submitting <form> reaches
     POST /api/uploads/avatar and POST /api/imports with cookies attached.

THE ASYMMETRY THIS RELIES ON. `Origin` is a forbidden header name: page
JavaScript cannot set, spoof or strip it, the browser stamps it from the real
page, and it is sent on every unsafe-method request. A script (curl, Postman)
CAN send any Origin it likes — but a script does not have the victim's cookie.
Credential access and header control never coexist, which is exactly what
makes checking Origin sufficient.

WHAT THIS DOES NOT DO. It is not session security. Someone who has genuinely
stolen a token and replays it from a script can set Origin to an allowed value
and pass. That is token theft, not CSRF, and it is answered by the 30-minute
expiry and the jti revocation list instead.

SCOPE. Both checks apply only to unsafe methods AND only when the request
carries one of our auth cookies. CSRF requires an ambient credential, so a
request without one is not a CSRF vector — which is also why the lead webhook
and the cron trigger, which authenticate with a shared-secret header, are
deliberately untouched.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.services.audit_service import record_denied
from app.services.auth_cookie import ACCESS_TOKEN_COOKIE, LOGIN_TICKET_COOKIE

# GET/HEAD/OPTIONS are excluded. OPTIONS in particular must fall through so
# CORSMiddleware can answer preflights.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Anything that makes the browser attach credentials on its own. The login
# ticket counts: it is a half-finished authentication, and letting another
# site drive the OTP step with it would be the same class of bug.
AMBIENT_CREDENTIAL_COOKIES = (ACCESS_TOKEN_COOKIE, LOGIN_TICKET_COOKIE)


class CsrfProtectionMiddleware(BaseHTTPMiddleware):
    """Two independent locks on cookie-authenticated writes.

    Add this BEFORE CORSMiddleware in main.py. Starlette runs the most
    recently added middleware outermost, so adding CORS last leaves it
    wrapping this one — which means a rejection here still comes back with
    CORS headers on it and the browser reports a clean 403 instead of an
    opaque network error.
    """

    def __init__(self, app, allowed_origins: list[str]):
        super().__init__(app)
        # The SAME list CORS uses. One allowlist, so the two can never drift
        # into disagreeing about which origins are ours.
        self.allowed_origins = frozenset(allowed_origins)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in UNSAFE_METHODS and self._has_ambient_credential(request):
            rejection = (
                self._reject_untyped_body(request)
                or self._reject_foreign_origin(request)
            )
            if rejection is not None:
                # A cross-site write attempt against a live session is exactly
                # the kind of denied attempt audit item 3.5 wants recorded, and
                # it cannot reach the HTTPException handler because middleware
                # returns a response rather than raising.
                #
                # NOTE this is one insert per rejection, so a determined flood
                # writes rows. Rate limiting on these routes (audit item 1.9)
                # is the answer to that, not dropping the record.
                await record_denied("csrf", rejection.status_code)
                return rejection
        return await call_next(request)

    def _has_ambient_credential(self, request: Request) -> bool:
        return any(name in request.cookies for name in AMBIENT_CREDENTIAL_COOKIES)

    # ── lock 1: the body must declare what it is ──────────────────────────
    #
    # Closes hole (1) above at its source, and is self-enforcing: the attacker
    # needs the body read as JSON, but the only way to ask for that is a
    # Content-Type header, and the header's mere presence makes the request
    # non-simple and forces the preflight that stops it. Omit it -> 415 here.
    # Send it -> the browser never lets the request leave. There is no third
    # option.
    #
    # Kept as a separate lock rather than folded into the Origin check because
    # it still holds if the allowlist is ever loosened by mistake.
    def _reject_untyped_body(self, request: Request) -> Response | None:
        if request.headers.get("content-type"):
            return None
        if not self._has_body(request):
            # Bodyless POSTs are legitimate here — /api/auth/logout,
            # /api/notifications/mark-all-read, /api/follow-ups/{id}/complete,
            # /api/imports/{id}/rollback — and axios omits Content-Type when
            # there is no data to send. Rejecting these would break real
            # traffic; the Origin check below is what covers them.
            return None
        return JSONResponse(
            status_code=415,
            content={"detail": "Content-Type header is required for a request with a body."},
        )

    @staticmethod
    def _has_body(request: Request) -> bool:
        if request.headers.get("transfer-encoding", "").lower() == "chunked":
            return True
        raw_length = request.headers.get("content-length")
        if raw_length is None:
            return False
        try:
            return int(raw_length) > 0
        except ValueError:
            # Unparseable Content-Length: treat as a body rather than waving
            # it through, so a malformed header cannot be used to skip this.
            return True

    # ── lock 2: the browser must vouch for where this came from ───────────
    #
    # The primary control, and the only one that can cover a bodyless POST or
    # a legitimately-typed multipart form. A missing Origin is a rejection,
    # not an exemption: browsers always send it on unsafe methods, so its
    # absence on a cookie-bearing write is not something to accommodate.
    # `Origin: null` (sandboxed iframe, some redirect chains) is likewise not
    # in the allowlist and fails closed.
    def _reject_foreign_origin(self, request: Request) -> Response | None:
        if request.headers.get("origin") in self.allowed_origins:
            return None
        return JSONResponse(
            status_code=403,
            content={"detail": "Cross-site request rejected."},
        )
