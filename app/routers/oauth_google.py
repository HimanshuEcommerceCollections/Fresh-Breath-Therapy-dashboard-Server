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
        samesite="lax", secure=False, max_age=600,
    )
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
        raise HTTPException(status_code=400, detail="Invalid OAuth state or missing code")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to exchange Google auth code")
        token_data = token_resp.json()

        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch Google profile")
        profile = userinfo_resp.json()

    email = profile.get("email")
    email_verified = profile.get("email_verified", False)
    name = profile.get("name", email)

    if not email or not email_verified:
        raise HTTPException(status_code=400, detail="Google account email not verified")

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

    token = create_access_token(user.id)
    redirect = RedirectResponse(f"{settings.FRONTEND_URL}/dashboard")
    redirect.set_cookie(
        key="access_token", value=token, httponly=True,
        samesite="lax", secure=False, max_age=60 * 60,
    )
    redirect.delete_cookie("oauth_state")
    return redirect