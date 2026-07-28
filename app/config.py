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
    GOOGLE_REDIRECT_URI: str = "https://fresh-breath-therapy-dashboard-server.onrender.com/api/auth/google/callback"
    FRONTEND_URL: str = "https://fresh-breath-therapy-dashboard-ui.vercel.app"
    ALLOWED_ORIGINS: str = "http://localhost:3000,https://fresh-breath-therapy-dashboard-ui.vercel.app"
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str | None = None
    RESEND_API_KEY: str | None = None
    RESEND_FROM_EMAIL: str | None = None
    EMAIL_SERVICE: bool = False  # suspended for now — see app/services/email_service.py

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()