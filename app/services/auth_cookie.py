from fastapi import Response

ACCESS_TOKEN_COOKIE = "access_token"
ACCESS_TOKEN_COOKIE_MAX_AGE = 60 * 60

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
        max_age=ACCESS_TOKEN_COOKIE_MAX_AGE,
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        ACCESS_TOKEN_COOKIE,
        httponly=True,
        samesite="none",
        secure=True,
    )
