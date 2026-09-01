"""What the security controls are ACTUALLY set to.

Finding F-24. The Settings screen showed four toggles — "Require MFA for
Admins", "HIPAA audit logging", "Auto-logout after 30 min idle", "7-year data
retention" — all displaying as ON, backed by a hardcoded array in the frontend.
Nothing read them and nothing enforced them.

That is worse than showing nothing. An auditor who reads that screen and then
reads the code does not find a to-do list, they find a claim that is not true,
and the conversation changes.

So this endpoint reports the REAL configuration, computed from settings at
request time, and the screen renders it READ-ONLY. Two consequences that are
both deliberate:

  * A control cannot be misreported. The value shown is the value in force,
    because it is the same object the middleware reads.
  * A control cannot be switched OFF from a web page. These are deployment
    decisions — an audit trail an Admin could disable from the UI is not an
    audit trail — so there is no PATCH here and there should never be one.

Where something is not implemented it says so plainly rather than reading as
enabled. MFA-per-role is the honest example: the OTP second factor exists and
covers every role when configured, but it is not separately REQUIRED for Admin,
so it reports false.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.config import settings
from app.dependencies.auth import require_admin_or_coordinator
from app.models.user import User

router = APIRouter(prefix="/api/settings/security", tags=["settings"])


class SecurityControl(BaseModel):
    key: str
    label: str
    # Tri-state on purpose. False means "off"; None means "enforced outside the
    # application" — a redirect at the load balancer, for instance — which is
    # different from off and must not be drawn the same way.
    enabled: bool | None
    detail: str


class SecuritySettingsResponse(BaseModel):
    controls: list[SecurityControl]


@router.get("", response_model=SecuritySettingsResponse)
async def get_security_settings(
    current_user: User = Depends(require_admin_or_coordinator()),
):
    retention_years = round(settings.AUDIT_RETENTION_DAYS / 365, 1)
    retention_label = (
        f"{int(retention_years)} years" if retention_years.is_integer()
        else f"{retention_years} years"
    )

    session_hours = settings.SESSION_ABSOLUTE_HOURS
    session_label = f"{session_hours} hour" + ("" if session_hours == 1 else "s")
    # True once the idle window is widened to meet the absolute cap: nothing
    # ends a session on inactivity any more, so the copy must not claim it does.
    idle_never_fires = (
        settings.ACCESS_TOKEN_EXPIRE_MINUTES >= session_hours * 60
    )

    return SecuritySettingsResponse(controls=[
        SecurityControl(
            key="audit_logging",
            label="HIPAA audit logging",
            enabled=True,
            detail=(
                "Every read, write, export and denied attempt is recorded with the "
                "acting user, the record touched, the source address and a request id."
            ),
        ),
        SecurityControl(
            key="audit_retention",
            label="Audit log retention",
            enabled=True,
            detail=(
                f"Audit records are kept for {retention_label} "
                f"({settings.AUDIT_RETENTION_DAYS} days), then purged by a daily job "
                "that records what it removed."
            ),
        ),
        SecurityControl(
            key="idle_logout",
            label="Automatic logoff",
            # Honest about which of the two windows is actually doing the work.
            # Once the idle window is widened to the absolute cap, nothing ends
            # a session on inactivity any more, and saying "ends after N minutes
            # with no activity" would overstate the control.
            enabled=True,
            detail=(
                f"Every session ends {session_label} after sign-in, and the user "
                "signs in again."
                + (
                    "" if idle_never_fires
                    else f" A session left idle for "
                         f"{settings.ACCESS_TOKEN_EXPIRE_MINUTES} minutes ends sooner."
                )
            ),
        ),
        SecurityControl(
            key="two_factor",
            label="Two-step verification at sign-in",
            enabled=settings.EMAIL_SERVICE,
            detail=(
                "A one-time code is emailed on every sign-in, single-use, expiring in "
                "5 minutes, locked after 5 wrong attempts."
                if settings.EMAIL_SERVICE else
                "Currently disabled: sign-in requires only a password."
            ),
        ),
        SecurityControl(
            key="mfa_required_for_admin",
            label="MFA specifically required for Admins",
            # Honest false. Held as a separate decision — the OTP step covers
            # every role when enabled but is not independently mandatory for
            # Admin, so nothing would stop it being turned off for them too.
            enabled=False,
            detail=(
                "Not implemented. The one-time code above applies to all roles when "
                "enabled, but it is not separately enforced for Admin accounts."
            ),
        ),
        SecurityControl(
            key="data_retention",
            label="PHI retention outside the audit log",
            enabled=True,
            detail=(
                f"Imported spreadsheet contents are redacted "
                f"{settings.IMPORT_ROW_RETENTION_DAYS} days after an import settles; "
                f"stored API responses are deleted after "
                f"{settings.IDEMPOTENCY_KEY_RETENTION_HOURS} hours."
            ),
        ),
        SecurityControl(
            key="brute_force_protection",
            label="Brute-force protection",
            enabled=True,
            detail=(
                f"An account is rate limited after {settings.MAX_FAILED_LOGINS} failed "
                f"sign-ins within {settings.FAILED_LOGIN_WINDOW_MINUTES} minutes, "
                "counted per account rather than per address."
            ),
        ),
        SecurityControl(
            key="transport_security",
            label="Strict transport security",
            # None, not False: outside production the header is withheld on
            # purpose, which is not the same as the control being absent.
            enabled=None if settings.is_development else True,
            detail=(
                "Withheld in development so it is never sent over plaintext HTTP."
                if settings.is_development else
                "HSTS is sent on every response; browsers refuse plaintext for a year."
            ),
        ),
    ])
