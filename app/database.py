from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

# Pool sizing matters here: Supabase's session-mode pooler caps this project
# at 15 concurrent clients, and SQLAlchemy's default pool (5 + 10 overflow)
# can claim all 15 from a single process. When that happens the app doesn't
# error — it silently hangs, because every request waits on a connection that
# will never be released. pool_timeout turns that hang into a fast, visible
# failure; the size/overflow defaults in config.py leave headroom for Alembic
# and one-off scripts.
#
# pool_pre_ping is required in front of a pooler: it recycles connections the
# pooler has already dropped, instead of surfacing them as random
# "connection was closed" errors mid-request.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

class Base(DeclarativeBase):
    pass

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
