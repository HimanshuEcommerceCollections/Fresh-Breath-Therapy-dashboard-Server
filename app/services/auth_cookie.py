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
