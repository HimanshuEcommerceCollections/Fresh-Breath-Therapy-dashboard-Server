"""Baseline security response headers.

Each of these defends something different, and two of them matter far more on
the frontend than here — see fbtdashboard/next.config.ts for that half.

X-Content-Type-Options: nosniff
    The one with a concrete exposure in THIS service. /api/exports/payments
    returns text/csv built from client-controlled data (exports.py:168 reads
    e.client.name, plus therapist and location names). Without nosniff a
    browser is free to disregard the declared text/csv and sniff the body as
    HTML — so markup stored in a client's name field, arriving through the
    public lead webhook, could execute as script in our own origin.
    Content-Disposition: attachment discourages rendering; this forbids the
    sniff.

Referrer-Policy: no-referrer
    Stops the full URL leaking in the Referer header. Relevant because the
    OTP handoff carries a staff email in the query string
    (/verify-otp?email=...). Small live surface today, free to close.

X-Frame-Options: DENY
    Defence in depth only, HERE. Nobody usefully frames a JSON response; the
    clickjacking target is the dashboard UI, so the load-bearing copy of this
    header is the frontend's. Worth knowing it is the COMPLEMENT to
    CsrfProtectionMiddleware rather than a duplicate: CSRF protection rejects
    requests from foreign origins, while clickjacking makes the victim click
    inside OUR origin — the request then carries a legitimate Origin and
    passes every check we have. Only refusing to be framed stops that.

Content-Security-Policy: default-src 'none'; ...
    Near-meaningless for JSON bodies, but correct and free. Skipped on the
    documentation routes: FastAPI's Swagger UI pulls its bundle from
    cdn.jsdelivr.net and runs an inline bootstrap script, so a strict policy
    would break /docs. Those routes only exist when ENVIRONMENT=development
    (see main.py), so in production nothing matches this exemption.

Strict-Transport-Security
    Technically audit item 8.1 rather than 8.3, but it is a response header
    and belongs with its siblings. Gated on ENVIRONMENT so it is never SENT
    outside production, rather than relying on RFC 6797 section 8.1 (which
    requires browsers to ignore it over insecure transport). Two independent
    reasons it cannot affect local HTTP development.

    No `preload` — submission to the browser preload list is effectively a
    one-way door. No `includeSubDomains` — nothing is served under these
    Vercel hostnames, so it would add scope without adding protection. Note
    HSTS is slow to reverse: you unwind it by serving max-age=0 for as long
    as the original max-age, and only for visitors who come back.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Exempt from CSP only. nosniff / DENY / Referrer-Policy are harmless here.
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

# 'none' across the board: this service returns JSON, CSV and PDF bodies and
# has no legitimate reason to load a script, frame anything, be framed, or be
# the target of a form submission.
API_CSP = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

HSTS_VALUE = "max-age=31536000"  # one year, no preload, no includeSubDomains


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp the baseline headers on every response.

    Registered last in main.py so it is outermost and therefore also covers
    responses produced by the middleware beneath it — a CSRF rejection and a
    CORS preflight get the same treatment as a route's own response.
    """

    def __init__(self, app, hsts_enabled: bool):
        super().__init__(app)
        self.hsts_enabled = hsts_enabled

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"

        if request.url.path not in DOCS_PATHS:
            response.headers["Content-Security-Policy"] = API_CSP

        if self.hsts_enabled:
            response.headers["Strict-Transport-Security"] = HSTS_VALUE

        return response
