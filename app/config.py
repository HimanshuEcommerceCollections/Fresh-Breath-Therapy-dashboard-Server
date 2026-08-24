import pathlib
import re
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    # This doubles as the IDLE window. The token is re-issued while the user is
    # active (see dependencies/auth.py), so it only actually expires after this
    # long with no requests at all — which is what "automatic logoff after 30
    # minutes idle" means to the person using it.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # Hard ceiling regardless of activity, so a session cannot slide forever on
    # a machine somebody left open. A shift is under 12 hours; anyone still
    # working past it signs in again.
    SESSION_ABSOLUTE_HOURS: int = 12
    # Do not mint a new token on every single request — that would be a
    # Set-Cookie on every response for no benefit. Re-issue only once the
    # current one is this old, so a busy user gets a handful per hour.
    TOKEN_REISSUE_AFTER_MINUTES: int = 5
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
    # Comma-separated Google Workspace domains permitted to sign in, e.g.
    # "freshbreaththerapy.com". Empty means ANY Google account on earth can
    # complete the OAuth flow and land in the pending-approval queue for someone
    # to reject by hand. Set this.
    ALLOWED_GOOGLE_DOMAINS: str | None = None
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
    # NO DEFAULT, deliberately. This single flag decides whether a second
    # factor exists at all, and its old default of False was half of a full
    # authentication bypass (see the ticket-binding work in otp_service.py).
    # There is no safe value to guess: False silently disables 2FA, True breaks
    # every login if SMTP is not actually working. So the operator has to say,
    # and the app refuses to start until they do.
    EMAIL_SERVICE: bool
    # Shared secret for the /api/internal/notification-scan route — required
    # so the AsyncIOScheduler-based scan (Render-only, see scheduler_service.py)
    # can also be triggered externally (e.g. Vercel Cron) if this ever runs
    # somewhere the in-process scheduler can't. No default: unset means the
    # route refuses every request rather than running unauthenticated.
    CRON_SECRET: str | None = None
    # Used once, to create the very first admin when the users table is empty.
    # Declared here rather than read with os.getenv so that every environment
    # variable this app consumes is visible in one place — which is also what
    # makes extra="forbid" viable.
    INITIAL_ADMIN_EMAIL: str | None = None
    INITIAL_ADMIN_PASSWORD: str | None = None
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

    # ── retention for the other PHI-bearing tables (item 5.7) ─────────────
    # How long after a batch settles the spreadsheet's raw contents are kept.
    # Long enough to investigate a bad import, short enough that years of other
    # people's spreadsheets do not sit in the database. The row's verdict and
    # the id it produced are kept regardless — only the PHI columns are nulled.
    IMPORT_ROW_RETENTION_DAYS: int = 30
    # Stored API responses exist to make a retried request safe, which is
    # answered within seconds. Stripe keeps theirs 24 hours; anything longer is
    # a copy of a client record for no reason.
    IDEMPOTENCY_KEY_RETENTION_HOURS: int = 24

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
        # FORBID, not ignore. With "ignore" a misspelled variable was silently
        # discarded: set SECRET_KEYY on the host and the app booted happily
        # using a different key, or none. Security settings that fail quietly
        # are worse than ones that fail loudly, so an unrecognised key in .env
        # now stops startup instead of being swallowed.
        #
        # Note this only polices the .env FILE. Real environment variables are
        # matched by field name, so the platform's own vars (PORT, RENDER_*)
        # are never collected and cannot trip this.
        extra = "forbid"

    # ── resolved connection URLs ──────────────────────────────────────────

    @staticmethod
    def _with_port(url: str, port: int) -> str:
        """Swap the port in a postgres URL, leaving credentials untouched."""
        return re.sub(r"(?<=:)\d+(?=/[^/]*$)", str(port), url, count=1)

    @property
    def allowed_google_domains(self) -> set[str]:
        raw = self.ALLOWED_GOOGLE_DOMAINS or ""
        return {d.strip().lower() for d in raw.split(",") if d.strip()}

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


def _assert_no_unknown_env_file_keys(path: str = ".env") -> None:
    """Refuse to start if .env contains a key no setting will ever read.

    This exists because pydantic-settings' extra="forbid" does NOT cover dotenv
    keys here. Its DotEnvSettingsSource only raises for a key that does not
    start with env_prefix, and our prefix is the empty string — so every key
    "starts with" it and the check never fires. extra="forbid" is kept above
    because it still governs values passed to the constructor directly, but it
    is not what catches a typo in the file.

    Which matters, because the failure it prevents is silent: set SECRET_KEYY
    on a host and the old behaviour was to discard it and boot with a different
    key, or none, with nothing in the logs. A security setting that fails
    quietly is worse than one that fails loudly.

    Only the FILE is checked. Real environment variables are matched by field
    name, so a platform's own vars (PORT, RENDER_*, PYTHON_VERSION) are never
    collected and must not be treated as errors.
    """
    env_path = pathlib.Path(path)
    if not env_path.is_file():
        return

    known = set(Settings.model_fields)
    unknown = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key and key not in known:
            unknown.append(key)

    if unknown:
        raise RuntimeError(
            f"{env_path} defines {len(unknown)} key(s) that no setting reads: "
            f"{', '.join(sorted(unknown))}. Either a typo or a leftover — fix or "
            f"remove it. Refusing to start rather than ignoring it silently."
        )


settings = Settings()
_assert_no_unknown_env_file_keys()