from fastapi import Response
from app.config import settings

ACCESS_TOKEN_COOKIE = "access_token"

# Single source of truth for the access_token cookie's attributes. Every
# login path (OTP verify, Google OAuth callback, any future one) MUST call
# this instead of its own response.set_cookie(...) — samesite="none" +
# secure=True is required because the frontend and backend are on different
# domains (cross-site cookie). A path that sets these independently and gets
# it wrong (e.g. samesite="lax") silently breaks auth for that path only.


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=token,
        httponly=True,
        samesite="none",
        secure=True,
        # Derived from the SAME setting that determines the JWT's own
        # expiry (jwt_service.create_access_token), not a separate literal —
        # a cookie that outlives its token just means the browser keeps
        # sending a dead token until the user notices; a cookie that expires
        # SOONER than the token forces a needless re-login even though the
        # token would still have been valid. Previously hardcoded to 3600s
        # regardless of this setting, so a shorter ACCESS_TOKEN_EXPIRE_MINUTES
        # (e.g. set differently in a deployed environment) still left the
        # cookie looking present for a full hour after the token inside it
        # had already expired — every request "logged out" until that cookie
        # aged out too.
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        ACCESS_TOKEN_COOKIE,
        httponly=True,
        samesite="none",
        secure=True,
    )


# ── login ticket ──────────────────────────────────────────────────────────
#
# The half-finished-login credential: proof that /login (or the Google
# callback) already verified who this is, carried across to the OTP step. See
# otp_service.py for the bypass this closes.
#
# A cookie rather than a response field, for two reasons that are both
# structural rather than stylistic:
#
#   * the OTP page is reached by a client-side navigation away from the login
#     page, which destroys the login form's React state — so a ticket the
#     client had to hold itself would need the URL (landing it in access logs,
#     forever) or Web Storage (readable by any injected script). httpOnly is
#     neither.
#   * the Google callback is a 302 with no body to put a ticket in.
#
# Scoped to /api/auth: the OTP endpoints are the only things that ever read
# it, so it is not sent on the hundred-odd data requests that follow login.

LOGIN_TICKET_COOKIE = "login_ticket"
LOGIN_TICKET_PATH = "/api/auth"


def set_login_ticket_cookie(response: Response, ticket: str, max_age_seconds: int) -> None:
    response.set_cookie(
        key=LOGIN_TICKET_COOKIE,
        value=ticket,
        httponly=True,
        samesite="none",  # same cross-site constraint as the session cookie
        secure=True,
        path=LOGIN_TICKET_PATH,
        # Matches the OTP's own TTL. A ticket outliving its code would keep a
        # dead login attempt addressable for no reason.
        max_age=max_age_seconds,
    )


def clear_login_ticket_cookie(response: Response) -> None:
    # `path` MUST match what set_login_ticket_cookie used — a delete_cookie on
    # a different path is silently ignored by the browser and the stale cookie
    # keeps being sent.
    response.delete_cookie(
        LOGIN_TICKET_COOKIE,
        path=LOGIN_TICKET_PATH,
        httponly=True,
        samesite="none",
        secure=True,
    )
