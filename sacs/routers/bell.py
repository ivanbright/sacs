"""Bell scheduling router: schedules, events, monitor status, test clock."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

from .. import audio
from ..config import get_settings
from ..db import get_db, get_sessionmaker
from ..deps import (
    AuthContext,
    get_auth_context,
    require_membership,
)
from ..events import Topic, enqueue, dispatch_pending
from ..models import BellEvent, BellOutcome, BellSchedule, Membership, School, utcnow
from ..rbac import Perm
from .. import scheduler

router = APIRouter(prefix="/api/schools/{school_id}/bell", tags=["bell"])
test_router = APIRouter(prefix="/api/test", tags=["test"])


def _validate_period(p: "PeriodModel") -> None:
    try:
        start = datetime.strptime(p.start, "%H:%M")
        end = datetime.strptime(p.end, "%H:%M")
    except ValueError:
        raise HTTPException(status_code=422, detail="Period times must be HH:MM")
    if end <= start:
        raise HTTPException(status_code=422, detail="Period end must be after start")


def _validate_days(days: list[int] | None) -> list[int] | None:
    if days is None:
        return None
    if not days:
        raise HTTPException(status_code=422, detail="days must not be empty when provided")
    if any((not isinstance(d, int)) or d < 0 or d > 6 for d in days):
        raise HTTPException(status_code=422, detail="days must be integers 0..6")
    return sorted(set(days))


class PeriodModel(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    start: str
    end: str


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    timezone: str = "UTC"
    periods: list[PeriodModel] = Field(min_length=1)
    days: list[int] | None = None


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
    for p in payload.periods:
        _validate_period(p)
    schedule = BellSchedule(
        school_id=school_id,
        name=payload.name.strip(),
        timezone=payload.timezone,
        periods=[p.model_dump() for p in payload.periods],
        days=_validate_days(payload.days),
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
    for p in payload.periods:
        _validate_period(p)
    schedule.name = payload.name.strip()
    schedule.timezone = payload.timezone
    schedule.periods = [p.model_dump() for p in payload.periods]
    if payload.days is not None:
        schedule.days = _validate_days(payload.days)
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


@router.get("/schedules/{schedule_id}")
def get_schedule(
    school_id: str,
    schedule_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_VIEW)
    schedule = db.get(BellSchedule, schedule_id)
    if schedule is None or schedule.school_id != school_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _serialize(schedule)


@router.delete("/schedules/{schedule_id}")
def delete_schedule(
    school_id: str,
    schedule_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    schedule = db.get(BellSchedule, schedule_id)
    if schedule is None or schedule.school_id != school_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.query(BellEvent).filter(BellEvent.schedule_id == schedule_id).delete()
    db.delete(schedule)
    enqueue(db, Topic.BELL, {"school_id": school_id, "audience": {"roles": ["school_head", "dos", "bell_monitor"]}, "data": {"action": "schedule_deleted"}})
    db.commit()
    return {"id": schedule_id, "deleted": True}


@router.post("/schedules/{schedule_id}/periods", status_code=201)
def add_period(
    school_id: str,
    schedule_id: str,
    payload: PeriodModel,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    schedule = db.get(BellSchedule, schedule_id)
    if schedule is None or schedule.school_id != school_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    _validate_period(payload)
    periods = list(schedule.periods or [])
    periods.append(payload.model_dump())
    schedule.periods = periods
    schedule.updated_at = utcnow()
    db.commit()
    return _serialize(schedule)


@router.delete("/schedules/{schedule_id}/periods/{index}")
def remove_period(
    school_id: str,
    schedule_id: str,
    index: int,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    schedule = db.get(BellSchedule, schedule_id)
    if schedule is None or schedule.school_id != school_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    periods = list(schedule.periods or [])
    if index < 0 or index >= len(periods):
        raise HTTPException(status_code=404, detail="Period index out of range")
    periods.pop(index)
    schedule.periods = periods
    schedule.updated_at = utcnow()
    db.commit()
    return _serialize(schedule)


@router.post("/schedules/{schedule_id}/activate")
def activate_schedule(
    school_id: str,
    schedule_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    schedule = db.get(BellSchedule, schedule_id)
    if schedule is None or schedule.school_id != school_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    schedule.is_active = True
    schedule.updated_at = utcnow()
    db.commit()
    return _serialize(schedule)


@router.post("/schedules/{schedule_id}/deactivate")
def deactivate_schedule(
    school_id: str,
    schedule_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    schedule = db.get(BellSchedule, schedule_id)
    if schedule is None or schedule.school_id != school_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    schedule.is_active = False
    schedule.updated_at = utcnow()
    db.commit()
    return _serialize(schedule)


class RingRequest(BaseModel):
    label: str = Field(default="Manual ring", max_length=120)


@router.post("/ring")
def ring_now(
    school_id: str,
    payload: RingRequest | None = None,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    """Ring the bell immediately (manual) and record it as a fired event."""
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    settings = get_settings()
    label = (payload.label if payload else "Manual ring") or "Manual ring"
    now = utcnow()
    played = False
    if settings.bell_audio:
        played = audio.ring()
    schedule = (
        db.query(BellSchedule)
        .filter_by(school_id=school_id)
        .order_by(BellSchedule.created_at)
        .first()
    )
    if schedule is None:
        raise HTTPException(status_code=409, detail="No bell schedule exists for this school")
    event = BellEvent(
        school_id=school_id,
        schedule_id=schedule.id,
        occurrence_key=f"manual:{uuid4()}",
        label=label,
        event_type="manual",
        scheduled_at=now,
        outcome=BellOutcome.FIRED if played else BellOutcome.SKIPPED,
        detail="Manual ring via API" if played else "Audio unavailable or disabled",
        fired_at=now if played else None,
    )
    db.add(event)
    enqueue(db, Topic.BELL, {"school_id": school_id, "audience": {"roles": ["school_head", "dos", "bell_monitor"]}, "data": {"action": "manual_ring", "label": label, "played": played}})
    db.commit()
    return {"ok": True, "played": played, "audio_available": audio.is_available(), "event_id": event.id}


@router.post("/test-sound")
def test_sound(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    """Play the configured bell sound so an operator can confirm audio works."""
    require_membership(ctx, db, school_id, Perm.BELL_MANAGE)
    settings = get_settings()
    played = False
    if settings.bell_audio:
        played = audio.ring()
    return {"audio_available": audio.is_available(), "enabled": settings.bell_audio, "played": played}


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
        "days": s.days,
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