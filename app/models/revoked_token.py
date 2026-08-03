from datetime import datetime
from sqlalchemy import String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RevokedToken(Base):
    """Server-side logout: a token's jti lands here the moment the user logs
    out, so it's rejected by get_current_user even if someone else still has
    a copy of it — clearing the cookie alone only stops the browser that
    logged out from sending it, it doesn't stop the token itself from
    working elsewhere until it naturally expires.

    TODO(retention): rows only need to live until their own `expires_at` —
    same unaddressed cleanup gap as idempotency_keys (see that model's TODO).
    """

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
