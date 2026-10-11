"""Bell scheduling router: schedules, events, monitor status, test clock."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

from ..config import get_settings
from ..db import get_db, get_sessionmaker
from ..deps import (
    AuthContext,
    get_auth_context,
    require_membership,
)
from ..events import Topic, enqueue, dispatch_pending
from ..models import BellEvent, BellSchedule, Membership, School, utcnow
from ..rbac import Perm
from .. import scheduler

router = APIRouter(prefix="/api/schools/{school_id}/bell", tags=["bell"])
test_router = APIRouter(prefix="/api/test", tags=["test"])


class PeriodModel(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    start: str
    end: str


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    timezone: str = "UTC"
    periods: list[PeriodModel] = Field(min_length=1)


@router.get("/schedules")
def list_schedules(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.BELL_VIEW)
    rows = db.query(BellSchedule).filter_by(school_id=school_id).order_by(BellSchedule.created_at).all()
    return [_serialize(s) for s in rows]


@router.post("/schedules", status_code=201)
def create_schedule(
    school_id: str,
    payload: ScheduleCreate,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    schedule = BellSchedule(
        school_id=school_id,
        name=payload.name.strip(),
        timezone=payload.timezone,
        periods=[p.model_dump() for p in payload.periods],
    )
    db.add(schedule)
    enqueue(db, Topic.BELL, {"school_id": school_id, "audience": {"roles": ["school_head", "dos", "bell_monitor"]}, "data": {"action": "schedule_created"}})
    db.commit()
    return _serialize(schedule)


@router.put("/schedules/{schedule_id}")
def update_schedule(
    school_id: str,
    schedule_id: str,
    payload: ScheduleCreate,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    schedule = db.get(BellSchedule, schedule_id)
    if schedule is None or schedule.school_id != school_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    schedule.name = payload.name.strip()
    schedule.timezone = payload.timezone
    schedule.periods = [p.model_dump() for p in payload.periods]
    schedule.updated_at = utcnow()
    enqueue(db, Topic.BELL, {"school_id": school_id, "audience": {"roles": ["school_head", "dos", "bell_monitor"]}, "data": {"action": "schedule_updated"}})
    db.commit()
    return _serialize(schedule)


@router.post("/schedules/{schedule_id}/pause")
def pause_schedule(
    school_id: str,
    schedule_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    schedule = db.get(BellSchedule, schedule_id)
    if schedule is None or schedule.school_id != school_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    schedule.paused = True
    enqueue(db, Topic.BELL, {"school_id": school_id, "audience": {"roles": ["school_head", "dos", "bell_monitor"]}, "data": {"action": "schedule_paused"}})
    db.commit()
    return _serialize(schedule)


@router.post("/schedules/{schedule_id}/resume")
def resume_schedule(
    school_id: str,
    schedule_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    schedule = db.get(BellSchedule, schedule_id)
    if schedule is None or schedule.school_id != school_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    schedule.paused = False
    enqueue(db, Topic.BELL, {"school_id": school_id, "audience": {"roles": ["school_head", "dos", "bell_monitor"]}, "data": {"action": "schedule_resumed"}})
    db.commit()
    return _serialize(schedule)


@router.get("/events")
def events(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
    limit: int = 50,
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_VIEW)
    rows = (
        db.query(BellEvent)
        .filter_by(school_id=school_id)
        .order_by(BellEvent.scheduled_at.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return {
        "scheduler": scheduler.scheduler_status(),
        "school_id": school_id,
        "events": [
            {
                "id": e.id,
                "schedule_id": e.schedule_id,
                "occurrence_key": e.occurrence_key,
                "label": e.label,
                "event_type": e.event_type,
                "scheduled_at": e.scheduled_at.isoformat() + "Z",
                "fired_at": e.fired_at.isoformat() + "Z" if e.fired_at else None,
                "outcome": e.outcome,
                "detail": e.detail,
            }
            for e in rows
        ],
    }


def _serialize(s: BellSchedule) -> dict:
    return {
        "id": s.id,
        "school_id": s.school_id,
        "name": s.name,
        "timezone": s.timezone,
        "is_active": s.is_active,
        "paused": s.paused,
        "periods": s.periods,
        "updated_at": s.updated_at.isoformat() + "Z",
    }


# --------------------------------------------------------------------------- #
# Deterministic test clock (disabled in production unless explicitly enabled)
# --------------------------------------------------------------------------- #

class ClockSet(BaseModel):
    now_utc: str


@test_router.post("/clock/set")
def clock_set(payload: ClockSet, db: OrmSession = Depends(get_db)) -> dict:
    settings = get_settings()
    if not settings.enable_test_clock:
        raise HTTPException(status_code=403, detail="Test clock is disabled")
    try:
        parsed = datetime.fromisoformat(payload.now_utc.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="now_utc must be ISO 8601")
    scheduler.set_clock(lambda: parsed.astimezone(timezone.utc))
    return {"ok": True, "now": parsed.isoformat()}


@test_router.post("/clock/clear")
def clock_clear(db: OrmSession = Depends(get_db)) -> dict:
    settings = get_settings()
    if not settings.enable_test_clock:
        raise HTTPException(status_code=403, detail="Test clock is disabled")
    scheduler.clear_clock()
    return {"ok": True}


@test_router.post("/bell/tick")
def bell_tick(db: OrmSession = Depends(get_db)) -> dict:
    settings = get_settings()
    if not settings.enable_test_clock:
        raise HTTPException(status_code=403, detail="Test clock is disabled")
    return scheduler.run_tick(get_sessionmaker())