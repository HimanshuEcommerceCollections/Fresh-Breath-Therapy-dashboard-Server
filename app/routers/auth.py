import uuid
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
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
from app.services.otp_service import request_otp, verify_otp
from app.schemas.otp import OtpRequestResponse, VerifyOtpRequest, VerifyOtpResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=OtpRequestResponse)
@idempotent(OtpRequestResponse)
async def login(
    credentials: LoginRequest,
    request: Request,
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

    expires_at = await request_otp(db, user.id, user.email, purpose="login")
    return OtpRequestResponse(expires_at=expires_at)


@router.post("/verify-login-otp", response_model=VerifyOtpResponse)
@idempotent(VerifyOtpResponse)
async def verify_login_otp(
    payload: VerifyOtpRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid request")

    await verify_otp(db, user.id, purpose="login", code=payload.code)

    token = create_access_token(user.id)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=60 * 60,
    )
    return VerifyOtpResponse(detail="Login successful")


@router.post("/resend-otp", response_model=OtpRequestResponse)
@idempotent(OtpRequestResponse)
async def resend_otp(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == payload["email"]))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid request")

    expires_at = await request_otp(db, user.id, user.email, purpose=payload["purpose"])
    return OtpRequestResponse(expires_at=expires_at)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"detail": "Logged out"}


@router.post("/signup", response_model=OtpRequestResponse, status_code=status.HTTP_201_CREATED)
@idempotent(OtpRequestResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, request: Request, db: AsyncSession = Depends(get_db)):
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

    expires_at = await request_otp(db, new_user.id, new_user.email, purpose="signup")
    return OtpRequestResponse(expires_at=expires_at)


@router.post("/verify-signup-otp", response_model=VerifyOtpResponse)
@idempotent(VerifyOtpResponse)
async def verify_signup_otp(
    payload: VerifyOtpRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid request")

    await verify_otp(db, user.id, purpose="signup", code=payload.code)

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