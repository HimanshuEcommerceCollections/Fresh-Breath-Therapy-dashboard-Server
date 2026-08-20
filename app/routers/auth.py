import uuid
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models.user import User
from app.models.role import Role
from app.models.therapist import Therapist
from app.schemas.auth import LoginRequest
from app.schemas.user import UserCreate, UserResponse
from app.services.security import verify_password, hash_password
from app.services.jwt_service import create_access_token
from app.dependencies.auth import get_current_user, require_admin
from app.dependencies.idempotency import idempotent
from datetime import datetime, timezone
from app.models.role_request import RoleRequest, RoleRequestStatus
from app.schemas.role_request import SignupRequest, ApproveRoleRequest, RoleRequestResponse
from app.services.otp_service import (
    OTP_TTL_MINUTES, request_otp, resolve_ticket, verify_otp,
)
from app.schemas.otp import (
    OtpRequestResponse, ResendOtpRequest, VerifyOtpRequest, VerifyOtpResponse,
)
from app.config import settings
from app.services.auth_cookie import (
    ACCESS_TOKEN_COOKIE, LOGIN_TICKET_COOKIE, clear_auth_cookie,
    clear_login_ticket_cookie, set_auth_cookie, set_login_ticket_cookie,
)
from app.services.token_revocation_service import revoke_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_access_token_cookie(response: Response, user_id: uuid.UUID):
    set_auth_cookie(response, create_access_token(user_id))


def _issue_login_ticket(response: Response, ticket: str) -> None:
    set_login_ticket_cookie(response, ticket, OTP_TTL_MINUTES * 60)


def _assert_may_hold_session(user: User) -> None:
    """The gates /login enforces, re-checked at the moment the session is minted.

    Between the password step and the OTP step an admin may have deactivated
    the account or revoked its role. get_current_user would reject the token
    afterwards anyway, but issuing a cookie we know is already dead is a
    confusing state to hand a user, and the check costs nothing here.
    """
    if user.role_id is None:
        raise HTTPException(status_code=403, detail="Account pending admin approval")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is inactive")


def _assert_ticket_matches_email(user: User, claimed_email: str) -> None:
    """Cross-check only — NEVER a lookup.

    The ticket already decided which account this is. This just refuses a
    request whose body disagrees with it, which in practice means a stale tab
    submitting one account's code against another's ticket.
    """
    if user.email.lower() != claimed_email.lower():
        raise HTTPException(
            status_code=401,
            detail="This login session is no longer valid. Please sign in again.",
        )


@router.post("/login", response_model=OtpRequestResponse)
@idempotent(OtpRequestResponse)
async def login(
    credentials: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.email == credentials.email)
    )
    user = result.scalar_one_or_none()

    if user is None or user.password_hash is None:
        raise HTTPException(status_code=401, detail="Please sign in with Google for this account")

    if not verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if user.role_id is None:
        raise HTTPException(status_code=403, detail="Account pending admin approval")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is inactive")

    if not settings.EMAIL_SERVICE:
        # No second factor configured, so the password IS the whole check and
        # this is the only place a session can be minted. No ticket is issued,
        # which is precisely why /verify-login-otp cannot mint one either.
        _set_access_token_cookie(response, user.id)
        return OtpRequestResponse(detail="Login successful", otp_required=False)

    # The password verified above is what earns the ticket. Everything the OTP
    # step is allowed to do flows from this line.
    expires_at, ticket = await request_otp(db, user.id, user.email, purpose="login")
    _issue_login_ticket(response, ticket)
    return OtpRequestResponse(expires_at=expires_at)


@router.post("/verify-login-otp", response_model=VerifyOtpResponse)
@idempotent(VerifyOtpResponse)
async def verify_login_otp(
    payload: VerifyOtpRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    # The user comes from the TICKET, never from payload.email.
    #
    # This is the fix for a complete authentication bypass. This endpoint used
    # to SELECT the user by the email in its own request body and then mint
    # that user's session cookie — gated only by `if settings.EMAIL_SERVICE`,
    # whose default is False. POST an admin's address with any six digits and
    # you were an admin: the password is checked in /login, an endpoint the
    # attacker simply never called.
    #
    # There is no `if EMAIL_SERVICE` here any more, deliberately. With the flag
    # off, /login issues no ticket, so resolve_ticket finds nothing and this
    # 401s — the bypass is gone because the code path is gone, not because a
    # second flag now guards it. verify_otp also enforces the attempt cap, so
    # the six digits can no longer be brute-forced.
    ticket = request.cookies.get(LOGIN_TICKET_COOKIE)
    otp = await verify_otp(db, ticket, purpose="login", code=payload.code)
    user = otp.user  # loaded with .role by resolve_ticket

    _assert_ticket_matches_email(user, payload.email)
    _assert_may_hold_session(user)

    # Spend the ticket before handing out the session: one login attempt, one
    # session, and no reusable half-authenticated credential left in the
    # browser afterwards.
    clear_login_ticket_cookie(response)
    _set_access_token_cookie(response, user.id)
    # The user rides back with the verification so the client can populate its
    # session cache and route straight to the dashboard. An account with no
    # role yet is still pending approval, so there is nothing useful to send.
    return VerifyOtpResponse(
        detail="Login successful",
        user=UserResponse.model_validate(user) if user.role_id else None,
    )


@router.post("/resend-otp", response_model=OtpRequestResponse)
@idempotent(OtpRequestResponse)
async def resend_otp(
    payload: ResendOtpRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Re-send the code for a login attempt already in progress.

    Also ticket-bound, and for the same reason. This endpoint used to take an
    email and mint a fresh login OTP for it with no credential of any kind —
    which meant an attacker could summon a code for any account on demand and
    then grind the six digits at /verify-login-otp. Deriving the account from
    the ticket instead means only whoever passed the password step can ask for
    another code, and only for their own account.

    `payload` was an untyped dict read with payload["email"] / ["purpose"];
    a malformed body was a KeyError and a 500. ResendOtpRequest makes it a 422.
    """
    if not settings.EMAIL_SERVICE:
        raise HTTPException(status_code=400, detail="OTP verification is currently disabled")

    otp = await resolve_ticket(db, request.cookies.get(LOGIN_TICKET_COOKIE), payload.purpose)
    user = otp.user

    if payload.email is not None:
        _assert_ticket_matches_email(user, payload.email)

    # A login OTP must never go out to an account /login itself would refuse
    # to authenticate. Signup-purpose OTPs are exempt: at signup time role_id
    # is *always* still None (that's the normal, pre-approval state being
    # verified), so this check would otherwise block the one case resend-otp
    # legitimately exists for.
    if payload.purpose == "login":
        _assert_may_hold_session(user)

    # request_otp rotates ticket_hash, so the cookie MUST be replaced — the one
    # the browser is holding stops resolving the moment this commits.
    expires_at, ticket = await request_otp(db, user.id, user.email, purpose=payload.purpose)
    _issue_login_ticket(response, ticket)
    return OtpRequestResponse(expires_at=expires_at)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    # Revoke the token server-side, not just clear the cookie client-side —
    # otherwise a copy of the token captured elsewhere (another device,
    # intercepted, etc.) would keep working until it naturally expires.
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    if token:
        await revoke_token(db, token)
    clear_auth_cookie(response)
    return {"detail": "Logged out"}


@router.post("/signup", response_model=OtpRequestResponse, status_code=status.HTTP_201_CREATED)
@idempotent(OtpRequestResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    new_user = User(
        id=uuid.uuid4(),
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role_id=None,  # no access until an admin approves and assigns a role
    )
    db.add(new_user)
    await db.flush()

    db.add(RoleRequest(
        id=uuid.uuid4(),
        user_id=new_user.id,
        requested_role_id=None,
        status=RoleRequestStatus.PENDING,
    ))
    await db.commit()

    if not settings.EMAIL_SERVICE:
        return OtpRequestResponse(
            detail="Signup complete. Awaiting admin approval.", otp_required=False
        )

    # Same ticket mechanism as login, so both flows share one code path rather
    # than diverging. Verifying a signup mints no session, so this is not a
    # takeover surface — but it does stop resend-otp being an anonymous
    # "email a code to this address" oracle for the signup purpose too.
    expires_at, ticket = await request_otp(db, new_user.id, new_user.email, purpose="signup")
    _issue_login_ticket(response, ticket)
    return OtpRequestResponse(expires_at=expires_at)


@router.post("/verify-signup-otp", response_model=VerifyOtpResponse)
@idempotent(VerifyOtpResponse)
async def verify_signup_otp(
    payload: VerifyOtpRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    # Ticket-bound like the login flow, and with the `if EMAIL_SERVICE` gate
    # likewise removed: with the flag off /signup issues no ticket, so there is
    # nothing here to verify and this 401s instead of silently reporting
    # success for an email it never checked.
    ticket = request.cookies.get(LOGIN_TICKET_COOKIE)
    otp = await verify_otp(db, ticket, purpose="signup", code=payload.code)
    _assert_ticket_matches_email(otp.user, payload.email)

    # No session is minted here — the account is still pending admin approval —
    # but the spent ticket still gets cleared out of the browser.
    clear_login_ticket_cookie(response)
    return VerifyOtpResponse(detail="Email verified. Awaiting admin approval.")


@router.get("/role-requests", response_model=list[RoleRequestResponse])
async def list_role_requests(
    status_filter: RoleRequestStatus | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    query = select(RoleRequest).options(
        selectinload(RoleRequest.user), selectinload(RoleRequest.requested_role)
    )
    if status_filter:
        query = query.where(RoleRequest.status == status_filter)
    result = await db.execute(query.order_by(RoleRequest.created_at))
    return result.scalars().all()


@router.post("/role-requests/{request_id}/approve", response_model=RoleRequestResponse)
@idempotent(RoleRequestResponse)
async def approve_role_request(
    request_id: uuid.UUID,
    payload: ApproveRoleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    req = await db.get(RoleRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != RoleRequestStatus.PENDING:
        raise HTTPException(status_code=400, detail="Request already reviewed")

    role = await db.get(Role, payload.role_id)
    if role is None:
        raise HTTPException(status_code=400, detail="Role does not exist")

    target_user = await db.get(User, req.user_id)

    # Email-based Therapist linking runs for every approval: REQUIRED for the
    # Therapist role (approval blocked without a linkable record), best-effort
    # for Admin/Coordinator (linked if possible, never blocks approval).
    result = await db.execute(
        select(Therapist).where(Therapist.email == target_user.email)
    )
    therapist = result.scalar_one_or_none()

    if role.name == "Therapist":
        if therapist is None:
            raise HTTPException(
                status_code=400,
                detail="No therapist record found with this email. Please create a therapist record with this email before approving this request.",
            )
        if therapist.user_id is not None and therapist.user_id != target_user.id:
            raise HTTPException(
                status_code=400,
                detail="This therapist record is already linked to another user account.",
            )
        therapist.user_id = target_user.id
        therapist.ever_linked = True
    elif therapist is not None and therapist.user_id is None:
        therapist.user_id = target_user.id
        therapist.ever_linked = True

    target_user.role_id = role.id

    req.requested_role_id = role.id
    req.status = RoleRequestStatus.APPROVED
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.now(timezone.utc)

    await db.commit()
    result = await db.execute(
        select(RoleRequest).options(selectinload(RoleRequest.user), selectinload(RoleRequest.requested_role))
        .where(RoleRequest.id == request_id)
    )
    return result.scalar_one()


@router.delete("/role-requests/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def reject_and_delete_user(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Rejecting a signup means the account was fraudulent/unwanted —
    delete the user entirely, not just the request."""
    req = await db.get(RoleRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != RoleRequestStatus.PENDING:
        raise HTTPException(status_code=400, detail="Request already reviewed")

    target_user = await db.get(User, req.user_id)

    await db.delete(req)
    if target_user is not None:
        await db.delete(target_user)
    await db.commit()