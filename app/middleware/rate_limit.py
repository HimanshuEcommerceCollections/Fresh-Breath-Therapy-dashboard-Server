"""Volumetric protection: per-IP request limits and a request body ceiling.

Audit item 1.9. Nothing limited attempts on any endpoint. The OTP attempt cap
protects one code from being ground down, but nothing stopped thousands of
PASSWORD guesses a minute, or someone burning the Gmail send quota by asking
for OTPs in a loop, or a single client saturating the process.

THREE LAYERS, AND THEY DO DIFFERENT JOBS. Worth being explicit, because they
are easy to confuse:

  * This middleware — per-IP request counting. Stops one source flooding.
  * The account lockout in routers/auth.py — counts a specific ACCOUNT's recent
    failed logins out of the audit log. Catches a distributed attempt that
    spreads across IPs, which per-IP counting cannot see.
  * Debouncing in the frontend — a UX nicety with no security value at all. It
    reduces load from cooperative clients and does nothing against anyone
    hostile, who simply does not run our JavaScript.

WHAT THIS IS NOT. The counters live in this process's memory, so with more than
one instance the effective limit multiplies by the instance count. That is a
real weakness and an accepted one: it still cuts a brute-force attempt by orders
of magnitude, it needs no Redis, and the account lockout is exact regardless of
how many instances are running. On AWS, WAF should take over the volumetric job
at the edge, where the traffic never reaches a container at all — but WAF cannot
count per ACCOUNT, so the other two layers stay useful.
"""
import time
from collections import defaultdict, deque

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Tight bucket: the endpoints where a wrong answer is worth retrying.
AUTH_PATHS = (
    "/api/auth/login",
    "/api/auth/signup",
    "/api/auth/resend-otp",
    "/api/auth/verify-login-otp",
    "/api/auth/verify-signup-otp",
)

AUTH_MAX_REQUESTS = 10
AUTH_WINDOW_SECONDS = 60

# Loose bucket for everything else. High enough that ordinary dashboard use —
# a page opening a dozen queries at once, infinite scroll — never notices, low
# enough to stop one client monopolising the process.
GENERAL_MAX_REQUESTS = 300
GENERAL_WINDOW_SECONDS = 60

# A body larger than this is refused before it is read. The avatar endpoint
# validated size AFTER await file.read(), so a 500 MB "image" was buffered into
# memory first and rejected second; the import ceiling is enforced the same way.
# This stops both at the door. Comfortably above the 20 MB import cap.
MAX_BODY_BYTES = 32 * 1024 * 1024

# Hard cap on tracked client keys, so the limiter cannot itself become the
# memory-exhaustion vector. Once full, new keys are unlimited rather than
# evicting an existing offender — refusing to forget the party currently being
# rate-limited matters more than tracking one more.
MAX_TRACKED_KEYS = 20_000


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)

        too_large = self._reject_oversized_body(headers)
        if too_large is not None:
            await too_large(scope, receive, send)
            return

        path = scope.get("path", "")
        bucket, limit, window = (
            ("auth", AUTH_MAX_REQUESTS, AUTH_WINDOW_SECONDS)
            if path in AUTH_PATHS
            else ("general", GENERAL_MAX_REQUESTS, GENERAL_WINDOW_SECONDS)
        )

        if self._is_over_limit(self._client_key(scope, headers), bucket, limit, window):
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down and try again."},
                headers={"Retry-After": str(window)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _reject_oversized_body(headers: Headers):
        raw_length = headers.get("content-length")
        if raw_length is None:
            return None
        try:
            declared = int(raw_length)
        except ValueError:
            return None
        if declared <= MAX_BODY_BYTES:
            return None
        return JSONResponse(
            status_code=413,
            content={
                "detail": f"Request body exceeds {MAX_BODY_BYTES // (1024 * 1024)} MB."
            },
        )

    @staticmethod
    def _client_key(scope: Scope, headers: Headers) -> str:
        """Best-effort client identity.

        Same caveat as the audit context's source_ip: behind a proxy the socket
        peer is the proxy, so the forwarded header is the only view of the
        caller — and it is client-settable, meaning a determined attacker can
        rotate it and get a fresh bucket. That is why the account lockout in
        auth.py exists and does not rely on this.
        """
        forwarded = headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _is_over_limit(self, key: str, bucket: str, limit: int, window: int) -> bool:
        now = time.monotonic()
        composite = (key, bucket)

        if composite not in self._hits and len(self._hits) >= MAX_TRACKED_KEYS:
            return False

        hits = self._hits[composite]
        cutoff = now - window
        while hits and hits[0] < cutoff:
            hits.popleft()

        if not hits and composite in self._hits and len(self._hits) > MAX_TRACKED_KEYS // 2:
            # Opportunistic cleanup: an emptied deque is dropped rather than
            # left as a permanent entry for a caller seen once an hour ago.
            del self._hits[composite]
            hits = self._hits[composite]

        if len(hits) >= limit:
            return True
        hits.append(now)
        return False
