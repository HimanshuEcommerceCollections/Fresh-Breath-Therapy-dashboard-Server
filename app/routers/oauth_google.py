import logging
import secrets
import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.config import settings
from app.models.user import User
from app.models.role_request import RoleRequest, RoleRequestStatus
from app.services.jwt_service import create_access_token
from app.services.auth_cookie import set_auth_cookie, set_login_ticket_cookie
from app.services.otp_service import OTP_TTL_MINUTES, request_otp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/google", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


@router.get("/login")
async def google_login():
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())

    redirect = RedirectResponse(f"{GOOGLE_AUTH_URL}?{query}")
    redirect.set_cookie(
        key="oauth_state", value=state, httponly=True,
        # secure=True: this cookie IS the CSRF protection for the Google login
        # flow, so it must never travel over plaintext. It was False, which let
        # a browser send it unencrypted.
        samesite="lax", secure=True, max_age=600,
    )
    return redirect


def _login_error_redirect(reason: str) -> RedirectResponse:
    """Any failure past this point sends the browser back to the login page
    instead of surfacing a raw JSON error — this is a full-page redirect
    flow, not an XHR call the SPA can catch and render itself."""
    redirect = RedirectResponse(f"{settings.FRONTEND_URL}/login?status=google_error&reason={reason}")
    redirect.delete_cookie("oauth_state")
    return redirect


@router.get("/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    stored_state = request.cookies.get("oauth_state")
    if not code or not state or state != stored_state:
        return _login_error_redirect("invalid_state")

    try:
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            })
            if token_resp.status_code != 200:
                return _login_error_redirect("token_exchange_failed")
            token_data = token_resp.json()

            userinfo_resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            if userinfo_resp.status_code != 200:
                return _login_error_redirect("profile_fetch_failed")
            profile = userinfo_resp.json()

        email = profile.get("email")
        email_verified = profile.get("email_verified", False)
        name = profile.get("name", email)

        if not email or not email_verified:
            return _login_error_redirect("email_not_verified")

        # Hosted-domain check. Without it any Google account can complete this
        # flow and create a pending signup request, so rejecting strangers is
        # manual work that arrives by surprise.
        #
        # `hd` is the authoritative claim but is only present for Workspace
        # accounts — a personal gmail.com login has none — so the address's own
        # domain is the fallback. Skipped entirely when unconfigured, which
        # keeps the current behaviour rather than locking anyone out on deploy.
        allowed_domains = settings.allowed_google_domains
        if allowed_domains:
            hosted_domain = (profile.get("hd") or "").strip().lower()
            email_domain = email.rsplit("@", 1)[-1].lower()
            if hosted_domain not in allowed_domains and email_domain not in allowed_domains:
                logger.warning("Google sign-in refused: domain not allowed")
                return _login_error_redirect("domain_not_allowed")

        result = await db.execute(
            select(User).options(selectinload(User.role)).where(User.email == email)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(id=uuid.uuid4(), name=name, email=email, password_hash=None, role_id=None)
            db.add(user)
            await db.flush()
            db.add(RoleRequest(
                id=uuid.uuid4(), user_id=user.id, requested_role_id=None,
                status=RoleRequestStatus.PENDING,
            ))
            await db.commit()
            redirect = RedirectResponse(f"{settings.FRONTEND_URL}/login?status=pending_approval")
            redirect.delete_cookie("oauth_state")
            return redirect

        if user.role_id is None:
            redirect = RedirectResponse(f"{settings.FRONTEND_URL}/login?status=pending_approval")
            redirect.delete_cookie("oauth_state")
            return redirect

        if not user.is_active:
            redirect = RedirectResponse(f"{settings.FRONTEND_URL}/login?status=inactive")
            redirect.delete_cookie("oauth_state")
            return redirect

        if settings.EMAIL_SERVICE:
            # Mirror the password-login flow: don't set the session cookie yet —
            # send an OTP and hand off to the SPA's /verify-otp page (full-page
            # redirect, so params carry what the login form would otherwise pass
            # via JSON) to finish the same verify-login-otp step password users go
            # through.
            _, ticket = await request_otp(db, user.id, user.email, purpose="login")
            # No email and no expiry in the URL. This is a full-page redirect,
            # so anything here is recorded in platform access logs and browser
            # history (audit item 4.4); the OTP page asks
            # GET /api/auth/pending-login for a masked address instead.
            redirect = RedirectResponse(f"{settings.FRONTEND_URL}/verify-otp?flow=login")
            # Google asserting this identity is what earns the ticket here, in
            # place of the password check /login does. It rides as a cookie
            # rather than a query param for the obvious reason: this is a
            # full-page redirect, and a ticket in the URL would be recorded in
            # platform access logs and browser history.
            set_login_ticket_cookie(redirect, ticket, OTP_TTL_MINUTES * 60)
            redirect.delete_cookie("oauth_state")
            return redirect

        token = create_access_token(user.id)
        # Canonical post-login route is "/" (the frontend's home page — there is
        # no /dashboard route), matching the OTP/password login flow's redirect.
        redirect = RedirectResponse(settings.FRONTEND_URL)
        set_auth_cookie(redirect, token)
        redirect.delete_cookie("oauth_state")
        return redirect
    except HTTPException:
        # e.g. request_otp's OTP-cooldown (429) or SMTP-send-failed (502) —
        # still a real backend error, just surfaced via redirect+query param
        # instead of a raw JSON body, since this is a full-page navigation.
        logger.exception("Google OAuth callback failed with an HTTPException")
        return _login_error_redirect("otp_send_failed")
    except Exception:
        logger.exception("Google OAuth callback failed unexpectedly")
        return _login_error_redirect("unexpected_error")