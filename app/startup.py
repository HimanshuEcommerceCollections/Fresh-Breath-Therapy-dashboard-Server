import logging
import os
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.role import Role
from app.models.user import User
from app.services.security import hash_password

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


async def _ensure_first_admin(db: AsyncSession, admin_role_id: uuid.UUID):
    result = await db.execute(select(User))
    if result.first() is not None:
        return  # at least one user already exists — never touch this again

    email = os.getenv("INITIAL_ADMIN_EMAIL")
    password = os.getenv("INITIAL_ADMIN_PASSWORD")
    if not email or not password:
        logger.warning(
            "No users exist and INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD are not "
            "set. No one can log in yet."
        )
        return

    db.add(User(
        id=uuid.uuid4(),
        name="Admin",
        email=email,
        password_hash=hash_password(password),
        role_id=admin_role_id,
    ))
    # The address is not logged. It is staff rather than patient data, but it
    # is still an identifier and stdout is the least protected place it could
    # land; whoever set the env var already knows which account this is.
    logger.info("Created the initial admin user from INITIAL_ADMIN_EMAIL.")

async def ensure_auth_bootstrap(db: AsyncSession):
    """Runs exactly once per server process, at boot — NOT per request.
    Roles + first Admin only. Everything else is created through the API."""
    roles = await _ensure_roles(db)
    await _ensure_first_admin(db, roles["Admin"])
    await db.commit()