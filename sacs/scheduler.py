"""Deterministic clock and software bell scheduler.

The scheduler evaluates each school's active bell schedule in the school's own
time zone, records every event (``fired`` / ``missed`` / ``skipped``) in the
database with a unique ``occurrence_key`` so restarts and reconnects can never
duplicate execution, and streams outcomes to the monitor workspaces.

A controllable clock lets automated tests advance time without waiting for a
real school period to elapse (``scheduler.set_clock(lambda: virtual_now)``).
"""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .events import Topic, enqueue
from .models import BellEvent, BellOutcome, BellSchedule, School, utcnow

logger = logging.getLogger("sacs.scheduler")

_clock_fn = None
_last_tick: dict | None = None


def set_clock(fn) -> None:
    """Install a deterministic clock (returning tz-aware datetimes)."""
    global _clock_fn
    _clock_fn = fn


def clear_clock() -> None:
    global _clock_fn
    _clock_fn = None


def now() -> datetime:
    if _clock_fn is not None:
        value = _clock_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone_utc())
        return value.astimezone(timezone_utc())
    return datetime.now(timezone_utc())


def timezone_utc():
    return ZoneInfo("UTC")


def _combine(day, hhmm: str, tz) -> datetime:
    try:
        hour, minute = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 0, 0
    return datetime.combine(day, dtime(hour, minute), tzinfo=tz)


def occurrences_for_day(schedule: BellSchedule, day):
    """Yield (occurrence_key, label, event_type, due_local, token) tuples."""
    try:
        tz = ZoneInfo(schedule.timezone)
    except Exception:  # noqa: BLE001 - invalid timezone falls back to UTC
        tz = ZoneInfo("UTC")
    days = getattr(schedule, "days", None)
    if days is not None and day.weekday() not in days:
        return
    for index, period in enumerate(schedule.periods or []):
        start = period.get("start") or "00:00"
        end = period.get("end") or "23:59"
        label = period.get("label") or f"Period {index + 1}"
        key_prefix = f"{day.isoformat()}:{index}"
        yield (f"{key_prefix}:start", f"{label} start", "start", _combine(day, start, tz))
        yield (f"{key_prefix}:end", f"{label} end", "end", _combine(day, end, tz))


def evaluate_schedule(db: OrmSession, schedule: BellSchedule, reference: datetime) -> dict:
    """Record every due-but-unrecorded occurrence for a schedule.

    Returns counts by outcome. Never claims success for an execution that did
    not happen: a due event older than the grace window is recorded as
    ``missed``, and a paused schedule records ``skipped`` events.
    """
    settings = get_settings()
    grace = timedelta(seconds=settings.bell_grace_seconds)
    counts = {outcome: 0 for outcome in (BellOutcome.FIRED, BellOutcome.MISSED, BellOutcome.SKIPPED, BellOutcome.FAILED)}
    tz_name = schedule.timezone
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    local_now = reference.astimezone(tz)
    for day in (local_now.date() - timedelta(days=1), local_now.date(), local_now.date() + timedelta(days=1)):
        for key, label, event_type, due_local in occurrences_for_day(schedule, day):
            due_utc = due_local.astimezone(ZoneInfo("UTC"))
            if due_utc > reference:
                continue
            existing = (
                db.execute(
                    select(BellEvent.id).where(
                        BellEvent.schedule_id == schedule.id,
                        BellEvent.occurrence_key == key,
                    )
                ).scalar_one_or_none()
            )
            if existing is not None:
                continue
            if schedule.paused:
                outcome, detail = BellOutcome.SKIPPED, "Schedule paused"
            elif reference - due_utc <= grace:
                outcome, detail = BellOutcome.FIRED, "Executed within grace window"
            else:
                outcome, detail = BellOutcome.MISSED, "Execution window elapsed while offline"
            event = BellEvent(
                school_id=schedule.school_id,
                schedule_id=schedule.id,
                occurrence_key=key,
                label=label,
                event_type=event_type,
                scheduled_at=due_utc,
                outcome=outcome,
                detail=detail,
                fired_at=reference if outcome == BellOutcome.FIRED else None,
            )
            db.add(event)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                return counts
            counts[outcome] += 1
            enqueue(
                db,
                Topic.BELL,
                {
                    "school_id": schedule.school_id,
                    "audience": {"roles": ["bell_monitor", "school_head", "dos"]},
                    "data": {
                        "event_type": "bell",
                        "schedule_id": schedule.id,
                        "occurrence_key": key,
                        "label": label,
                        "outcome": outcome,
                        "scheduled_at": due_utc.isoformat() + "Z",
                        "fired_at": event.fired_at.isoformat() + "Z" if event.fired_at else None,
                        "detail": detail,
                    },
                },
            )
    return counts


def run_tick(session_factory) -> dict:
    """Evaluate all active schedules exactly once against the current time."""
    global _last_tick
    settings = get_settings()
    reference = now()
    totals = {o: 0 for o in (BellOutcome.FIRED, BellOutcome.MISSED, BellOutcome.SKIPPED, BellOutcome.FAILED)}
    db = session_factory()
    try:
        schedules = db.execute(
            select(BellSchedule).where(BellSchedule.is_active.is_(True))
        ).scalars().all()
        for schedule in schedules:
            counts = evaluate_schedule(db, schedule, reference)
            for outcome, value in counts.items():
                totals[outcome] += value
        db.commit()
        _last_tick = {"at": reference.isoformat() + "Z", **totals}
        if totals[BellOutcome.FIRED] and settings.bell_audio:
            from . import audio

            if not audio.ring(background=True):
                logger.warning("bell fired but audio could not be started")
        return dict(_last_tick)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("bell tick failed")
        return {"at": reference.isoformat() + "Z", "error": "tick failed"}
    finally:
        db.close()


def scheduler_status() -> dict:
    global _last_tick
    base = dict(_last_tick or {"at": None, "fired": 0, "missed": 0, "skipped": 0})
    base["running"] = True
    base["clock"] = "controlled" if _clock_fn else "wall"
    return base


async def bell_loop(session_factory, interval: float = 1.0) -> None:
    """Background task that keeps the bell scheduler running after startup."""
    while True:
        try:
            run_tick(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("bell_loop error")
        await asyncio.sleep(interval)