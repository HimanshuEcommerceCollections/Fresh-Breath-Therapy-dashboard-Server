import asyncio
import uuid
from app.database import AsyncSessionLocal
from app.models.role import Role
from app.models.user import User
from app.services.security import hash_password


async def seed():
    async with AsyncSessionLocal() as db:
        admin_role = Role(id=uuid.uuid4(), name="Admin", permissions={"can_edit": True})
        viewer_role = Role(id=uuid.uuid4(), name="Viewer", permissions={"can_edit": False})
        db.add_all([admin_role, viewer_role])
        await db.flush()

        diane = User(
            id=uuid.uuid4(),
            name="Diane",
            email="diane@freshbreaththerapy.com",
            password_hash=hash_password("ChangeMe123!"),
            role_id=admin_role.id,
        )
        kaylee = User(
            id=uuid.uuid4(),
            name="Kaylee",
            email="kaylee@freshbreaththerapy.com",
            password_hash=hash_password("ChangeMe123!"),
            role_id=viewer_role.id,
        )
        db.add_all([diane, kaylee])

        await db.commit()
        print("Seeded roles (Admin, Viewer) and users (Diane, Kaylee).")
        print(f"Admin role_id: {admin_role.id}")
        print(f"Viewer role_id: {viewer_role.id}")


if __name__ == "__main__":
    asyncio.run(seed())