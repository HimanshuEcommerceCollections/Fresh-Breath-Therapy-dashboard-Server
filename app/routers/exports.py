"""CSV / PDF export endpoints.

Every report export calls the SAME handler that backs the on-screen chart
(reports.py) rather than re-querying, so a downloaded file always matches
what the user is looking at. The routers' Depends(...) defaults are just
Python defaults, so calling them directly with an explicit db/current_user
bypasses dependency injection cleanly.
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.enums import (
    PaymentMethod, PaymentStatus, CONTACT_STATUS_LABELS,
    COLLECTED_STATUSES, OUTSTANDING_STATUSES,
)
from app.models.payment import Payment
from app.models.session import Session as SessionModel
from app.models.user import User
from app.dependencies.auth import require_admin_or_coordinator
from app.services.audit_service import record_export
from app.routers import reports as reports_router
from app.routers.payments import _payment_query
from app.services.export_service import (
    MEDIA_TYPES, filename_for, to_csv, to_pdf,
)

router = APIRouter(prefix="/api/exports", tags=["exports"])

RANGE_LABELS = {
    "last_30_days": "Last 30 days",
    "last_3_months": "Last 3 months",
    "last_6_months": "Last 6 months",
    "last_12_months": "Last 12 months",
}

STATUS_LABELS = {
    PaymentStatus.PAID: "Paid",
    PaymentStatus.PENDING: "Pending",
    PaymentStatus.CANCELLED: "Cancelled",
}

METHOD_LABELS = {
    PaymentMethod.COPAY: "Copay",
    PaymentMethod.SELF_PAY: "Self-Pay",
    PaymentMethod.INSURANCE: "Insurance",
}

# Same guard as the status labels in models/enums.py: a new method or status
# must not silently render as its raw enum value in an export.
assert set(STATUS_LABELS) == set(PaymentStatus)
assert set(METHOD_LABELS) == set(PaymentMethod)

# Keyed by the raw enum VALUE, since that is what the export rows carry.
# Derived from the single label map in models/enums.py rather than restated —
# a second copy is how a renamed status ends up rendering as "closed_inactive"
# in the CSV while the dashboard shows it correctly.
LEAD_STATUS_LABELS = {s.value: label for s, label in CONTACT_STATUS_LABELS.items()}


def _num(v) -> float:
    """Raw number for CSV. Thousands separators would force quoting and turn
    the cell into text a spreadsheet can't sum — formatting is a PDF concern."""
    return round(float(v), 2)


def _fmt_for_pdf(rows: list[list]) -> list[list]:
    """Money/number columns get separators only in the rendered PDF."""
    return [
        [f"{v:,.2f}" if isinstance(v, float) else v for v in row]
        for row in rows
    ]


async def _report_table(name: str, range_: str, location_id, db, user):
    """-> (title, headers, rows) for one report, via the live report handler."""
    if name == "sales":
        data = await reports_router.sales_report(
            range=range_, location_id=location_id, db=db, current_user=user)
        return ("Sales Report", ["Month", "Revenue (USD)"],
                [[p.month, _num(p.total)] for p in data])

    if name == "clients":
        data = await reports_router.clients_by_status_report(
            range=range_, location_id=location_id, db=db, current_user=user)
        # Named "clients" but built from Lead.status — labelled honestly here.
        return ("Lead Distribution by Status", ["Status", "Leads"],
                [[LEAD_STATUS_LABELS.get(p.status, p.status), p.count] for p in data])

    if name == "team":
        data = await reports_router.team_performance_report(
            range=range_, location_id=location_id, db=db, current_user=user)
        return ("Team Performance", ["Therapist", "Sessions"],
                [[p.therapist_name, p.sessions] for p in data])

    if name == "conversion":
        rep = await reports_router.conversion_report(
            range=range_, location_id=location_id, db=db, current_user=user)
        rows = [[LEAD_STATUS_LABELS.get(s.status, s.status), s.count, f"{s.percent}%"]
                for s in rep.stages]
        rows.append(["TOTAL", rep.total_leads, f"{rep.overall_rate}% converted"])
        return ("Lead Conversion", ["Stage", "Leads", "Share"], rows)

    if name == "utilization":
        data = await reports_router.utilization_report(
            location_id=location_id, db=db, current_user=user)
        return ("Therapist Utilization", ["Therapist", "Completed sessions / week"],
                [[p.therapist_name, p.utilization] for p in data])

    if name == "revenue":
        data = await reports_router.revenue_by_therapist_report(
            range=range_, location_id=location_id, db=db, current_user=user)
        return ("Revenue by Therapist", ["Therapist", "Revenue (USD)"],
                [[p.therapist_name, _num(p.revenue)] for p in data])

    if name == "retention":
        data = await reports_router.retention_by_location_report(db=db, current_user=user)
        return ("Retention by Location", ["Location", "Avg months retained"],
                [[p.location_name, p.retention_months] for p in data])

    raise HTTPException(status_code=404, detail=f"Unknown report '{name}'")


@router.get("/reports/{name}")
async def export_report(
    name: str,
    format: str = Query("csv", pattern="^(csv|pdf)$"),
    range: str = "last_6_months",
    location_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    title, headers, rows = await _report_table(name, range, location_id, db, current_user)

    if format == "csv":
        body = to_csv(headers, rows)
    else:
        bits = [RANGE_LABELS.get(range, range)]
        if location_id:
            bits.append("filtered by location")
        body = to_pdf(title, " · ".join(bits), headers, _fmt_for_pdf(rows))

    # Item 3.6 asks specifically for row count AND filter criteria, so that
    # "148 rows" is always explainable after the fact.
    await record_export(
        db, "report",
        count=len(rows),
        criteria={"report": name, "format": format, "range": range,
                  "location_id": str(location_id) if location_id else None},
    )
    fname = filename_for(f"fbt-{name}-report", format)
    return Response(
        content=body,
        media_type=MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/payments")
async def export_payments(
    format: str = Query("csv", pattern="^(csv|pdf)$"),
    payment_status: PaymentStatus | None = None,
    method: PaymentMethod | None = None,
    client_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    """The Payments table as it stands, all rows — an export that stopped at
    the first scrolled-in page would quietly under-report.

    One row per payment now, not per invoice: with packages gone there is no
    Due/Paid/Balance triple to report, only what a session cost and whether it
    has been settled.
    """
    query = _payment_query()
    if payment_status:
        query = query.where(Payment.status == payment_status)
    if method:
        query = query.where(Payment.method == method)
    if client_id:
        query = query.where(
            Payment.session_id.in_(
                select(SessionModel.id).where(SessionModel.client_id == client_id)
            )
        )

    result = await db.execute(query.order_by(Payment.date.desc()))
    payments = result.scalars().all()

    # "Patient", not "Client": a payment can be for a lead's consultation.
    headers = ["Patient", "Type", "Amount (USD)", "Method", "Status", "Date"]
    rows = [[
        p.session.subject.name if p.session and p.session.subject else "",
        p.session.subject_kind.title() if p.session else "",
        _num(p.amount),
        METHOD_LABELS.get(p.method, p.method.value),
        STATUS_LABELS.get(p.status, p.status.value),
        p.date.isoformat() if p.date else "",
    ] for p in payments]

    if format == "csv":
        body = to_csv(headers, rows)
    else:
        sub = STATUS_LABELS.get(payment_status, "All payments") if payment_status else "All payments"
        # Totalled by what each row IS, not by the filter — a filtered export
        # still reports its own collected/outstanding split correctly, and a
        # cancelled row lands in neither.
        collected = sum(float(p.amount) for p in payments if p.status in COLLECTED_STATUSES)
        outstanding = sum(float(p.amount) for p in payments if p.status in OUTSTANDING_STATUSES)
        body = to_pdf(
            "Payments",
            f"{sub} · {len(payments)} payment(s) · collected {collected:,.2f} · "
            f"outstanding {outstanding:,.2f}",
            headers, _fmt_for_pdf(rows),
        )

    # Deliberately unbounded (see the docstring), so this is the single
    # largest PHI extraction the API offers and the ids are worth keeping.
    await record_export(
        db, "payment",
        count=len(payments),
        entity_ids=[p.id for p in payments],
        criteria={"format": format,
                  "payment_status": payment_status.value if payment_status else None,
                  "method": method.value if method else None,
                  "client_id": str(client_id) if client_id else None},
    )
    fname = filename_for("fbt-payments", format)
    return Response(
        content=body,
        media_type=MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
