import logging
import uuid

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

logger = logging.getLogger(__name__)

# ── connecting ────────────────────────────────────────────────────────────
#
# The app talks to Supabase's TRANSACTION pooler (:6543), not the session one
# (:5432). Session mode holds a real backend for the life of each client
# connection and caps this project at 15 of them — a deployed instance, a
# local dev server and a single script could exhaust the quota between them,
# after which nothing errors, everything just hangs waiting for a slot that
# never frees. Transaction mode borrows a backend per transaction and returns
# it immediately, so the same quota goes much further. Alembic keeps the
# session URL; transaction mode cannot run migrations.
#
# Two consequences of transaction pooling that must be configured for:
#
#   * PREPARED STATEMENTS CANNOT BE CACHED. Each transaction may land on a
#     different backend, so a statement prepared on one is absent on the next
#     and you get intermittent `prepared statement "__asyncpg_stmt_3__" does
#     not exist` — the kind of failure that only shows up under load. Both
#     caches below must be zero: statement_cache_size is asyncpg's own,
#     prepared_statement_cache_size is SQLAlchemy's asyncpg dialect.
#
#   * A LARGE POOL BUYS NOTHING. Connections are multiplexed on the far side,
#     so an oversized local pool only reserves capacity other instances need.
#     max_overflow=0 makes the ceiling exactly DB_POOL_SIZE.
#
# pool_pre_ping stays: a pooler still drops idle connections, and without it
# those surface as random "connection was closed" errors mid-request.
_url = settings.app_database_url
_connect_args: dict = {}

if settings.db_pool_mode == "transaction":
    # THREE separate settings, and all three are needed. Missing any one of
    # them produces `prepared statement "__asyncpg_stmt_8__" already exists`
    # (or "does not exist") intermittently, once traffic is high enough that
    # two transactions land on different backends.
    #
    #  1. prepared_statement_cache_size — SQLAlchemy's own cache. It is a
    #     DIALECT parameter, read from the URL query string; passing it in
    #     connect_args is silently ignored, which is how you end up believing
    #     it is off when it is not.
    _url += ("&" if "?" in _url else "?") + "prepared_statement_cache_size=0"
    _connect_args = {
        #  2. asyncpg's own statement cache, disabled at the driver.
        "statement_cache_size": 0,
        #  3. Unique names anyway. Even uncached, asyncpg names each prepared
        #     statement, and the pooler can route two transactions using the
        #     same generated name to the same backend. A UUID per statement
        #     makes a collision impossible rather than unlikely.
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
    }

engine = create_async_engine(
    _url,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    connect_args=_connect_args,
)


def log_connection_mode() -> None:
    """Record how this process is connected, at startup.

    A saturated pooler presents as unexplained slowness and hanging requests;
    knowing from the logs which mode and pool size an instance actually
    resolved turns that from guesswork into a one-line answer.
    """
    logger.info(
        "database: mode=%s endpoint=%s pool=%d+%d prepared_statements=%s",
        settings.db_pool_mode,
        settings.db_endpoint,
        settings.DB_POOL_SIZE,
        settings.DB_MAX_OVERFLOW,
        "off" if settings.db_pool_mode == "transaction" else "on",
    )
    if settings.db_pool_mode == "session":
        logger.warning(
            "database: on the SESSION pooler (:%d) — this project is capped at "
            "15 clients across every instance. Set DB_TRANSACTION_POOLER=true "
            "unless migrations are the reason.",
            settings.DB_SESSION_POOLER_PORT,
        )


AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
