import uuid
from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models.user import User
from app.models.role import Role
from app.schemas.auth import LoginRequest, LoginResponse
from app.schemas.user import UserCreate, UserResponse
from app.services.security import verify_password, hash_password
from app.services.jwt_service import create_access_token
from app.dependencies.auth import get_current_user, require_admin
from datetime import datetime, timezone
from app.models.role_request import RoleRequest, RoleRequestStatus
from app.schemas.role_request import SignupRequest, ApproveRoleRequest, SignupResponse, RoleRequestResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])

@router.post("/login", response_model=LoginResponse)
async def login(
    credentials: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.email == credentials.email)
    )
    user = result.scalar_one_or_none()

    if user is None or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    token = create_access_token(user.id)

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  
        max_age=60 * 60,
    )

    return LoginResponse()

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"detail": "Logged out"}


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
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

    return SignupResponse()


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
async def approve_role_request(
    request_id: uuid.UUID,
    payload: ApproveRoleRequest,
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

    await db.delete(req)       # delete the request row first — it FKs to the user
    if target_user is not None:
        await db.delete(target_user)  # then the user itself
    await db.commit()