"""Install the audit context for each request.

Runs INSIDE RequestIdMiddleware (registered before it, so it sits beneath it),
which matters only for ordering clarity — the request id is read lazily at
write time rather than copied in here.

Source IP is best-effort and advisory. Behind Vercel the socket peer is a
proxy, so X-Forwarded-For is the only way to see the caller; it is also
client-settable, so a determined party can put anything in it. That is
acceptable for a forensic record and unacceptable for an authorisation
decision, which is why nothing in this codebase makes a decision based on it.
The socket address is preferred when no forwarding header is present.
"""
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.audit_context import (
    AuditContext, install_context, reset_context, truncate_user_agent,
)


def _client_ip(scope: Scope, headers: Headers) -> str | None:
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        # Leftmost is the original client; the rest are proxies that appended.
        return forwarded.split(",")[0].strip() or None
    real_ip = headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip() or None
    client = scope.get("client")
    return client[0] if client else None


class AuditContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        ctx = AuditContext(
            source_ip=_client_ip(scope, headers),
            user_agent=truncate_user_agent(headers.get("user-agent")),
            route=f"{scope.get('method', '')} {scope.get('path', '')}".strip(),
        )
        # Also on the scope, so the exception handlers — which run above this
        # middleware, after its context has unwound — can still see it.
        scope.setdefault("state", {})["audit_context"] = ctx

        token = install_context(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_context(token)
