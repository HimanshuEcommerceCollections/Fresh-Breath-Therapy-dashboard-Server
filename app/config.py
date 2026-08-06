from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str = "https://fresh-breath-therapy-dashboard-serv.vercel.app/api/auth/google/callback"
    FRONTEND_URL: str = "https://fresh-breath-therapy-dashboard-ui.vercel.app"
    ALLOWED_ORIGINS: str = "http://localhost:3000,https://fresh-breath-therapy-dashboard-ui.vercel.app"
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
    # Supabase's session-mode pooler caps this project at 15 concurrent
    # clients. SQLAlchemy's DEFAULT pool (5 + 10 overflow) can therefore
    # consume the entire quota from one process, leaving nothing for Alembic,
    # a maintenance script, or a second worker — and once the cap is hit every
    # request blocks forever waiting for a connection that can't arrive.
    # These leave deliberate headroom; raise only if the Supabase plan does.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 3
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
    # Optional fallback for inbound leads whose Location text matches no
    # location on record. Without it, such a lead is rejected (422) and the
    # automation surfaces the error instead of the lead landing unassigned.
    LEAD_WEBHOOK_DEFAULT_LOCATION_ID: str | None = None

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()