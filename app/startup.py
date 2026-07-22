import uuid
import os
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.role import Role
from app.models.user import User
from app.services.security import hash_password


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
        print("WARNING: no users exist and INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD not set. No one can log in yet.")
        return

    db.add(User(
        id=uuid.uuid4(),
        name="Admin",
        email=email,
        password_hash=hash_password(password),
        role_id=admin_role_id,
    ))
    print(f"Created initial admin user: {email}")

async def ensure_auth_bootstrap(db: AsyncSession):
    """Runs exactly once per server process, at boot — NOT per request.
    Roles + first Admin only. Everything else is created through the API."""
    roles = await _ensure_roles(db)
    await _ensure_first_admin(db, roles["Admin"])
    await db.commit()