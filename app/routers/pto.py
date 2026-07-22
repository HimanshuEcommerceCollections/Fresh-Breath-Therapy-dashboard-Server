import uuid
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models.therapist import Therapist
from app.models.location import Location
from app.models.session import Session as SessionModel
from app.models.pto_transaction import PtoTransaction
from app.models.enums import SessionStatus, PtoTransactionType
from app.schemas.pto import PtoDashboardResponse, PtoStats, LocationPtoPoint, LeaderboardItem
from app.schemas.pto_transaction import PtoUsageCreate, PtoTransactionResponse
from app.models.user import User
from app.dependencies.auth import require_admin_or_coordinator, require_admin

router = APIRouter(prefix="/api/pto", tags=["pto"])


@router.get("", response_model=PtoDashboardResponse)
async def get_pto_dashboard(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    # Query 1: org-wide counts
    total_therapists = (await db.execute(select(func.count(Therapist.id)))).scalar_one()
    total_sessions = (await db.execute(select(func.count(SessionModel.id)))).scalar_one()

    # Query 2: org-wide PTO accrued/used, one grouped query
    pto_totals_rows = (await db.execute(
        select(PtoTransaction.type, func.coalesce(func.sum(PtoTransaction.hours), 0))
        .group_by(PtoTransaction.type)
    )).all()
    pto_totals = {row[0]: Decimal(str(row[1])) for row in pto_totals_rows}
    total_accrued = pto_totals.get(PtoTransactionType.ACCRUAL, Decimal("0"))
    total_used = pto_totals.get(PtoTransactionType.USAGE, Decimal("0"))
    total_balance = total_accrued - total_used

    # Query 3: therapist count per location, in one grouped query
    therapist_counts_rows = (await db.execute(
        select(Therapist.location_id, func.count(Therapist.id)).group_by(Therapist.location_id)
    )).all()
    therapist_counts_by_location = {row[0]: row[1] for row in therapist_counts_rows}

    # Query 4: completed session count per location, one JOIN+GROUP BY query
    session_counts_rows = (await db.execute(
        select(Therapist.location_id, func.count(SessionModel.id))
        .join(SessionModel, SessionModel.therapist_id == Therapist.id)
        .where(SessionModel.status == SessionStatus.COMPLETED)
        .group_by(Therapist.location_id)
    )).all()
    session_counts_by_location = {row[0]: row[1] for row in session_counts_rows}

    # Query 5: accrued PTO hours per location, one JOIN+GROUP BY query
    pto_by_location_rows = (await db.execute(
        select(Therapist.location_id, func.coalesce(func.sum(PtoTransaction.hours), 0))
        .join(PtoTransaction, PtoTransaction.therapist_id == Therapist.id)
        .where(PtoTransaction.type == PtoTransactionType.ACCRUAL)
        .group_by(Therapist.location_id)
    )).all()
    pto_by_location = {row[0]: Decimal(str(row[1])) for row in pto_by_location_rows}

    # Query 6: all locations
    locations = (await db.execute(select(Location))).scalars().all()

    by_location = [
        LocationPtoPoint(
            location_id=loc.id,
            location_name=loc.name,
            therapist_count=therapist_counts_by_location.get(loc.id, 0),
            session_count=session_counts_by_location.get(loc.id, 0),
            pto_hours=pto_by_location.get(loc.id, Decimal("0")),
        )
        for loc in locations
    ]

    # Query 7: all therapists with their location eagerly, one query
    therapists = (await db.execute(
        select(Therapist, Location.name)
        .join(Location, Therapist.location_id == Location.id)
    )).all()

    # Query 8: YTD completed sessions per therapist, one grouped query
    year_start = date(date.today().year, 1, 1)
    ytd_rows = (await db.execute(
        select(SessionModel.therapist_id, func.count(SessionModel.id))
        .where(SessionModel.status == SessionStatus.COMPLETED, SessionModel.date >= year_start)
        .group_by(SessionModel.therapist_id)
    )).all()
    ytd_by_therapist = {row[0]: row[1] for row in ytd_rows}

    # Query 9: accrued+used PTO per therapist, one grouped query
    pto_per_therapist_rows = (await db.execute(
        select(PtoTransaction.therapist_id, PtoTransaction.type, func.coalesce(func.sum(PtoTransaction.hours), 0))
        .group_by(PtoTransaction.therapist_id, PtoTransaction.type)
    )).all()
    pto_per_therapist: dict[uuid.UUID, dict] = {}
    for therapist_id, ptype, hours in pto_per_therapist_rows:
        pto_per_therapist.setdefault(therapist_id, {})[ptype] = Decimal(str(hours))

    leaderboard_raw = []
    for therapist, location_name in therapists:
        accrued = pto_per_therapist.get(therapist.id, {}).get(PtoTransactionType.ACCRUAL, Decimal("0"))
        used = pto_per_therapist.get(therapist.id, {}).get(PtoTransactionType.USAGE, Decimal("0"))
        balance = accrued - used

        weeks_active = max((date.today() - therapist.created_at.date()).days / 7, 1)
        avg_per_week = round(accrued / Decimal(str(weeks_active)), 1)

        leaderboard_raw.append({
            "therapist_id": therapist.id,
            "therapist_name": therapist.name,
            "credential": therapist.credential,
            "location_name": location_name,
            "ytd_sessions": ytd_by_therapist.get(therapist.id, 0),
            "pto_accrued": accrued,
            "pto_used": used,
            "balance": balance,
            "avg_per_week": avg_per_week,
        })

    leaderboard_raw.sort(key=lambda x: x["balance"], reverse=True)
    leaderboard = [LeaderboardItem(rank=i + 1, **item) for i, item in enumerate(leaderboard_raw)]

    return PtoDashboardResponse(
        stats=PtoStats(
            total_therapists=total_therapists,
            total_sessions=total_sessions,
            pto_accrued=total_accrued,
            pto_used=total_used,
            pto_balance=total_balance,
        ),
        by_location=by_location,
        leaderboard=leaderboard,
    )


@router.post("/usage", response_model=PtoTransactionResponse, status_code=201)
async def record_pto_usage(
    payload: PtoUsageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    therapist = await db.get(Therapist, payload.therapist_id)
    if therapist is None:
        raise HTTPException(status_code=400, detail="Therapist does not exist")

    balance_rows = (await db.execute(
        select(PtoTransaction.type, func.coalesce(func.sum(PtoTransaction.hours), 0))
        .where(PtoTransaction.therapist_id == payload.therapist_id)
        .group_by(PtoTransaction.type)
    )).all()
    balances = {row[0]: Decimal(str(row[1])) for row in balance_rows}
    accrued = balances.get(PtoTransactionType.ACCRUAL, Decimal("0"))
    used = balances.get(PtoTransactionType.USAGE, Decimal("0"))
    current_balance = accrued - used

    if payload.hours > current_balance:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot record {payload.hours}h usage — therapist only has {current_balance}h balance.",
        )

    transaction = PtoTransaction(
        id=uuid.uuid4(),
        therapist_id=payload.therapist_id,
        type=PtoTransactionType.USAGE,
        hours=payload.hours,
        date=payload.date,
        reason=payload.reason,
    )
    db.add(transaction)
    await db.commit()
    await db.refresh(transaction)
    return transaction