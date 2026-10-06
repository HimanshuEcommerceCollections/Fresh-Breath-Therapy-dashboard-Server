import logging
import uuid
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.role import Role
from app.models.user import User
from app.models.role_request import RoleRequest, RoleRequestStatus
from app.services.security import hash_password, verify_password

logger = logging.getLogger(__name__)


async def _ensure_roles(db: AsyncSession) -> dict[str, uuid.UUID]:
    result = await db.execute(select(Role))
    existing = {r.name: r.id for r in result.scalars().all()}

    for name, permissions in [("Admin", {"can_edit": True}), ("Coordinator", {"can_edit": False}), ("Therapist", {"can_edit": False})]:
        if name not in existing:
            role = Role(id=uuid.uuid4(), name=name, permissions=permissions)
            db.add(role)
            await db.flush()
            existing[name] = role.id

    return existing


def is_initial_admin_email(email: str | None) -> bool:
    configured = settings.INITIAL_ADMIN_EMAIL
    return bool(email and configured and email.strip().lower() == configured.strip().lower())


async def ensure_initial_admin(db: AsyncSession) -> User | None:
    """Make INITIAL_ADMIN_EMAIL an active Admin whose password is
    INITIAL_ADMIN_PASSWORD — whatever state the account is in, or if it has
    been deleted. Commits nothing; the caller does.

    Runs at boot AND on every sign-in by that address (password or Google), so
    the owner can always get in without a signup request or anyone's
    approval, even after being demoted or removed from the dashboard. It used
    to run only when the users table was empty, so once anyone else existed
    the env vars silently did nothing.
    """
    email = settings.INITIAL_ADMIN_EMAIL
    password = settings.INITIAL_ADMIN_PASSWORD
    if not email or not password:
        return None

    admin_role = (await db.execute(select(Role).where(Role.name == "Admin"))).scalar_one_or_none()
    if admin_role is None:
        admin_role_id = (await _ensure_roles(db))["Admin"]
    else:
        admin_role_id = admin_role.id

    user = (await db.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )).scalar_one_or_none()

    if user is None:
        user = User(
            id=uuid.uuid4(), name="Admin", email=email.strip(),
            password_hash=hash_password(password), role_id=admin_role_id,
        )
        db.add(user)
        # The address is not logged — see the note below.
        logger.info("Created the initial admin user from INITIAL_ADMIN_EMAIL.")
    else:
        user.role_id = admin_role_id
        user.is_active = True
        if not user.password_hash or not verify_password(password, user.password_hash):
            user.password_hash = hash_password(password)

    await db.flush()
    # A pending signup request for this address (left by an earlier Google
    # sign-in) would otherwise sit in the queue asking for approval of an
    # account that needs none.
    for req in (await db.execute(
        select(RoleRequest).where(
            RoleRequest.user_id == user.id, RoleRequest.status == RoleRequestStatus.PENDING
        )
    )).scalars().all():
        req.status = RoleRequestStatus.APPROVED
        req.requested_role_id = admin_role_id
        req.reviewed_at = datetime.now(timezone.utc)
    return user


async def ensure_auth_bootstrap(db: AsyncSession):
    """Runs exactly once per server process, at boot — NOT per request.
    Roles + the initial Admin only. Everything else is created through the API."""
    roles = await _ensure_roles(db)
    await ensure_initial_admin(db)
    await db.commit()