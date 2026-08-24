import re
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # Which deployment this process is. Defaults to the LOCKED-DOWN value on
    # purpose, which is the opposite of how these flags usually read.
    #
    # A permissive default means every environment that forgets to set the
    # variable is exposed, and the mistake is invisible until someone finds
    # it. Defaulting to "production" inverts the failure mode: forget it on a
    # new deploy and you are safe, forget it locally and you merely lose
    # /docs. Local development opts IN via ENVIRONMENT=development in .env.
    #
    # Anything other than exactly "development" is treated as production —
    # a typo must not silently unlock things.
    ENVIRONMENT: str = "production"
    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str = "https://fresh-breath-therapy-dashboard-serv.vercel.app/api/auth/google/callback"
    FRONTEND_URL: str = "https://fresh-breath-therapy-dashboard-ui.vercel.app"
    # Exact origins, never a wildcard: allow_credentials=True is set in
    # main.py, and the CORS spec forbids "*" alongside credentials — the
    # browser rejects the response rather than the server. So every port the
    # frontend might run on has to be listed. 3001 is what Next.js falls back
    # to when 3000 is already taken.
    ALLOWED_ORIGINS: str = (
        "http://localhost:3000,http://localhost:3001,"
        "https://fresh-breath-therapy-dashboard-ui.vercel.app"
    )
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str | None = None
    EMAIL_SERVICE: bool = False  # flip to true once SMTP is confirmed working end-to-end
    # Shared secret for the /api/internal/notification-scan route — required
    # so the AsyncIOScheduler-based scan (Render-only, see scheduler_service.py)
    # can also be triggered externally (e.g. Vercel Cron) if this ever runs
    # somewhere the in-process scheduler can't. No default: unset means the
    # route refuses every request rather than running unauthenticated.
    CRON_SECRET: str | None = None
    # ── audit log retention ───────────────────────────────────────────────
    # HIPAA 164.316(b)(2) requires documentation to be kept six years, and
    # audit records are treated as required documentation. Configurable rather
    # than a constant because it is a legal/business call, not a code one —
    # state law or an insurer may require longer, and the Settings screen
    # currently claims seven.
    AUDIT_RETENTION_DAYS: int = 6 * 365
    # The purge is the only path that deletes audit rows. On by default and a
    # no-op for six years; leaving the MECHANISM unbuilt is how this codebase
    # ended up with three tables carrying TODO(retention) and nowhere to hang a
    # policy.
    AUDIT_PURGE_ENABLED: bool = True

    # ── pooling ───────────────────────────────────────────────────────────
    # Supabase exposes the same database three ways, and which one you use
    # decides how many clients you get:
    #
    #   :5432 session mode      — 15 clients for this project, one per
    #                             connection for its whole life. This is what
    #                             the app used, and it is why a deployed
    #                             instance plus a local dev server plus one
    #                             script could exhaust the quota and make
    #                             every query hang waiting for a slot.
    #   :6543 transaction mode  — a connection is borrowed per transaction and
    #                             returned immediately, so the same quota
    #                             stretches vastly further. The right fit for
    #                             serverless, where instances are many and
    #                             each is idle most of the time.
    #   direct                  — needed for DDL; transaction mode cannot run
    #                             migrations.
    #
    # The app runs on transaction mode; Alembic keeps the session/direct URL.
    DB_TRANSACTION_POOLER: bool = True
    DB_TRANSACTION_POOLER_PORT: int = 6543
    DB_SESSION_POOLER_PORT: int = 5432
    # Set explicitly if the migration endpoint isn't just DATABASE_URL on the
    # session port (e.g. a true direct db.<ref>.supabase.co host).
    MIGRATION_DATABASE_URL: str | None = None

    # Transaction mode multiplexes connections, so a big per-instance pool
    # buys nothing and starves everyone else. max_overflow=0 keeps the ceiling
    # honest: this instance will never hold more than DB_POOL_SIZE.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 0
    DB_POOL_TIMEOUT: int = 15  # fail loudly instead of hanging indefinitely
    DB_POOL_RECYCLE: int = 1800
    DB_ECHO: bool = False  # SQL logging is very noisy; opt in per environment
    # Costs one extra round trip per checkout (~1 RTT). Worth it in front of a
    # pooler that silently drops idle connections; turn off if the DB is
    # co-located and latency matters more than reconnect resilience.
    DB_POOL_PRE_PING: bool = True
    # Shared secret the website's lead automation must present in the
    # X-Webhook-Secret header. No default: unset means the webhook rejects
    # every request rather than accepting unauthenticated leads from anyone
    # who guesses the URL.
    LEAD_WEBHOOK_SECRET: str | None = None

    # Commit batching. The importer tries a whole group in one savepoint and
    # narrows only where it fails: 200, then 25, then bisection down to the
    # single offending row. Set to [1] to reproduce the original per-row
    # behaviour exactly — the differential test uses that to prove the batched
    # path produces identical output.
    IMPORT_CHUNK_TIER_SIZES: list[int] = [200, 25]

    # How long one import may spend writing before it is judged failed,
    # its claim released and the entity handed to the next queued batch.
    # Generous: a 5,000-row import measures around 3 minutes, so this is
    # a stuck-run backstop rather than a normal-operation limit.
    IMPORT_MAX_RUNTIME_SECONDS: int = 600

    class Config:
        env_file = ".env"
        extra = "ignore"

    # ── resolved connection URLs ──────────────────────────────────────────

    @staticmethod
    def _with_port(url: str, port: int) -> str:
        """Swap the port in a postgres URL, leaving credentials untouched."""
        return re.sub(r"(?<=:)\d+(?=/[^/]*$)", str(port), url, count=1)

    @property
    def is_development(self) -> bool:
        """True only for an explicit, exact ENVIRONMENT=development.

        Case-insensitive and whitespace-tolerant so a stray space in a
        dashboard env var doesn't flip a deployment into dev mode, but
        deliberately not fuzzy beyond that: "dev", "local" and "staging" are
        all production as far as this is concerned.
        """
        return self.ENVIRONMENT.strip().lower() == "development"

    @property
    def is_supabase_pooler(self) -> bool:
        return "pooler.supabase.com" in self.DATABASE_URL

    @property
    def app_database_url(self) -> str:
        """What the application connects with.

        Rewritten to the transaction pooler rather than requiring the port to
        be right in .env, because getting it wrong is invisible until the
        pooler saturates under load and every request starts hanging.
        """
        if self.DB_TRANSACTION_POOLER and self.is_supabase_pooler:
            return self._with_port(self.DATABASE_URL, self.DB_TRANSACTION_POOLER_PORT)
        return self.DATABASE_URL

    @property
    def migration_database_url(self) -> str:
        """What Alembic connects with — never transaction mode.

        Transaction pooling hands back a different backend per transaction and
        does not support the session state DDL relies on, so migrations must
        use the session pooler or a direct connection.
        """
        if self.MIGRATION_DATABASE_URL:
            return self.MIGRATION_DATABASE_URL
        if self.is_supabase_pooler:
            return self._with_port(self.DATABASE_URL, self.DB_SESSION_POOLER_PORT)
        return self.DATABASE_URL

    @property
    def db_pool_mode(self) -> str:
        if not self.is_supabase_pooler:
            return "direct"
        port = self.app_database_url.rsplit(":", 1)[-1].split("/")[0]
        return "transaction" if port == str(self.DB_TRANSACTION_POOLER_PORT) else "session"

    @property
    def db_endpoint(self) -> str:
        """host:port, with credentials stripped — safe to log."""
        match = re.search(r"@([^/]+)", self.app_database_url)
        return match.group(1) if match else "unknown"


settings = Settings()