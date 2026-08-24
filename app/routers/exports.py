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
from app.models.enrollment import Enrollment
from app.models.enums import PaymentStatus
from app.models.user import User
from app.dependencies.auth import require_admin_or_coordinator
from app.services.audit_service import record_export
from app.routers import reports as reports_router
from app.routers.enrollments import _enrollment_query, _payment_status_filter
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
    PaymentStatus.PARTIALLY_PAID: "Partially Paid",
    PaymentStatus.PENDING: "Pending",
    PaymentStatus.OVERDUE: "Overdue",
}

LEAD_STATUS_LABELS = {
    "new_lead": "New Lead",
    "contacted": "Contacted",
    "consultation_scheduled": "Consultation Scheduled",
    "consultation_completed": "Consultation Completed",
    "therapy_session_booked": "Therapy Session Booked",
    "ongoing_therapy": "Ongoing Therapy",
    "completed_program": "Completed Program",
    "inactive_client": "Inactive Client",
}


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
    client_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    """The Payments table as it stands, all rows — an export that stopped at
    the first scrolled-in page would quietly under-report."""
    query = _enrollment_query()
    if payment_status:
        query = _payment_status_filter(query, payment_status)
    if client_id:
        query = query.where(Enrollment.client_id == client_id)

    result = await db.execute(query.order_by(Enrollment.created_at.desc()))
    invoices = result.scalars().all()

    headers = ["Client", "Package", "Due (USD)", "Paid (USD)", "Balance (USD)",
               "Status", "Started"]
    rows = [[
        e.client.name if e.client else "",
        e.package.name if e.package else "",
        _num(e.package_price_snapshot),
        _num(e.total_paid),
        _num(e.amount_due),
        STATUS_LABELS.get(e.payment_status, e.payment_status.value),
        e.started_at.date().isoformat() if e.started_at else "",
    ] for e in invoices]

    if format == "csv":
        body = to_csv(headers, rows)
    else:
        sub = STATUS_LABELS.get(payment_status, "All invoices") if payment_status else "All invoices"
        total_due = sum(float(e.amount_due) for e in invoices)
        total_paid = sum(float(e.total_paid) for e in invoices)
        body = to_pdf(
            "Payments",
            f"{sub} · {len(invoices)} invoice(s) · collected {total_paid:,.2f} · "
            f"outstanding {total_due:,.2f}",
            headers, _fmt_for_pdf(rows),
        )

    # Deliberately unbounded (see the docstring), so this is the single
    # largest PHI extraction the API offers and the ids are worth keeping.
    await record_export(
        db, "enrollment",
        count=len(invoices),
        entity_ids=[e.id for e in invoices],
        criteria={"format": format,
                  "payment_status": payment_status.value if payment_status else None,
                  "client_id": str(client_id) if client_id else None},
    )
    fname = filename_for("fbt-payments", format)
    return Response(
        content=body,
        media_type=MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
