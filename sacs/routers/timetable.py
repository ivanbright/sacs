"""Timetable router: entries, publication, and lesson-session generation."""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session as OrmSession

from ..db import get_db
from ..deps import AuthContext, get_auth_context, require_membership
from ..events import Topic, enqueue
from ..models import (
    Classroom,
    LessonSession,
    Membership,
    School,
    SchoolClass,
    Subject,
    TimetableEntry,
    TimetableStatus,
    User,
    utcnow,
)
from ..rbac import Perm

router = APIRouter(prefix="/api/schools/{school_id}/timetable", tags=["timetable"])


class EntryCreate(BaseModel):
    class_id: str
    subject_id: str
    classroom_id: str | None = None
    teacher_membership_id: str
    day_of_week: int = Field(ge=0, le=6)
    start_time: str
    end_time: str
    academic_term: str | None = None


def _serialize(db: OrmSession, e: TimetableEntry) -> dict:
    return {
        "id": e.id,
        "class_id": e.class_id,
        "subject_id": e.subject_id,
        "classroom_id": e.classroom_id,
        "teacher_membership_id": e.teacher_membership_id,
        "day_of_week": e.day_of_week,
        "start_time": e.start_time,
        "end_time": e.end_time,
        "status": e.status,
        "academic_term": e.academic_term,
    }


def _validate_entry(db: OrmSession, school_id: str, p: EntryCreate) -> None:
    if not (0 <= p.day_of_week <= 6):
        raise HTTPException(status_code=422, detail="day_of_week must be 0-6")
    try:
        s = datetime.strptime(p.start_time, "%H:%M")
        e = datetime.strptime(p.end_time, "%H:%M")
    except ValueError:
        raise HTTPException(status_code=422, detail="Times must be HH:MM")
    if e <= s:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")
    teacher = db.get(Membership, p.teacher_membership_id)
    if teacher is None or teacher.school_id != school_id or teacher.role != "teacher":
        raise HTTPException(status_code=422, detail="Teacher membership not found in this school")
    for attr, model in (("class_id", SchoolClass), ("subject_id", Subject)):
        row = db.get(model, getattr(p, attr))
        if row is None or row.school_id != school_id:
            raise HTTPException(status_code=422, detail=f"Unknown {attr} in this school")
    if p.classroom_id:
        row = db.get(Classroom, p.classroom_id)
        if row is None or row.school_id != school_id:
            raise HTTPException(status_code=422, detail="Unknown classroom_id in this school")


@router.get("")
def list_timetable(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.TIMETABLE_VIEW)
    rows = (
        db.query(TimetableEntry)
        .filter_by(school_id=school_id)
        .order_by(TimetableEntry.day_of_week, TimetableEntry.start_time)
        .all()
    )
    return [_serialize(db, e) for e in rows]


@router.post("", status_code=201)
def create_entry(
    school_id: str,
    payload: EntryCreate,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.TIMETABLE_MANAGE)
    _validate_entry(db, school_id, payload)
    row = TimetableEntry(
        school_id=school_id,
        class_id=payload.class_id,
        subject_id=payload.subject_id,
        classroom_id=payload.classroom_id,
        teacher_membership_id=payload.teacher_membership_id,
        day_of_week=payload.day_of_week,
        start_time=payload.start_time,
        end_time=payload.end_time,
        academic_term=payload.academic_term,
        status=TimetableStatus.DRAFT,
    )
    db.add(row)
    enqueue(db, Topic.TIMETABLE, {"school_id": school_id, "audience": {"roles": ["dos", "school_head"]}, "data": {"action": "entry_created", "id": row.id}})
    db.commit()
    return _serialize(db, row)


@router.post("/publish")
def publish(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    need = require_membership(ctx, db, school_id, Perm.TIMETABLE_PUBLISH)
    school = db.get(School, school_id)
    try:
        tz = ZoneInfo(school.timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")

    entries = (
        db.query(TimetableEntry)
        .filter(TimetableEntry.school_id == school_id, TimetableEntry.status != TimetableStatus.PUBLISHED)
        .all()
    )
    for e in entries:
        e.status = TimetableStatus.PUBLISHED

    today = utcnow().astimezone(tz).date()
    created = 0
    for e in entries:
        for offset in range(0, 7):
            day = today + timedelta(days=offset)
            if day.weekday() != e.day_of_week:
                continue
            start_dt = datetime.combine(day, dtime(*map(int, e.start_time.split(":"))), tzinfo=tz)
            end_dt = datetime.combine(day, dtime(*map(int, e.end_time.split(":"))), tzinfo=tz)
            existing = (
                db.query(LessonSession)
                .filter(
                    LessonSession.timetable_entry_id == e.id,
                    LessonSession.session_date == day.isoformat(),
                )
                .first()
            )
            if existing:
                continue
            db.add(
                LessonSession(
                    school_id=school_id,
                    timetable_entry_id=e.id,
                    session_date=day.isoformat(),
                    start_at=start_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
                    end_at=end_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
                )
            )
            created += 1
    enqueue(db, Topic.TIMETABLE, {"school_id": school_id, "audience": {"roles": ["school_head", "dos", "teacher", "bell_monitor"]}, "data": {"action": "published", "entries": len(entries), "sessions": created}})
    db.commit()

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {"published_entries": len(entries), "sessions_created": created, "publisher": need.role}


@router.get("/lessons")
def list_lessons(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    membership = require_membership(ctx, db, school_id, Perm.TIMETABLE_VIEW)
    q = db.query(LessonSession).filter(LessonSession.school_id == school_id)
    if date_from:
        q = q.filter(LessonSession.session_date >= date_from)
    if date_to:
        q = q.filter(LessonSession.session_date <= date_to)

    # Teachers only see their own assigned lessons.
    if membership.role == "teacher":
        assignment_ids = [
            a.id
            for a in db.query(TimetableEntry).filter(
                TimetableEntry.school_id == school_id,
                TimetableEntry.teacher_membership_id == membership.id,
                TimetableEntry.status == TimetableStatus.PUBLISHED,
            )
        ]
        q = q.filter(LessonSession.timetable_entry_id.in_(assignment_ids))
    if membership.role != "teacher":  # DOS / head see the whole timetable
        pass

    rows = q.order_by(LessonSession.session_date, LessonSession.start_at).all()
    entries = {e.id: e for e in db.query(TimetableEntry).filter_by(school_id=school_id).all()}
    classes = {c.id: c for c in db.query(SchoolClass).filter_by(school_id=school_id).all()}
    subjects = {s.id: s for s in db.query(Subject).filter_by(school_id=school_id).all()}
    classrooms = {c.id: c for c in db.query(Classroom).filter_by(school_id=school_id).all()}
    teachers = {m.user_id: m for m in db.query(Membership).filter(Membership.school_id == school_id).all() if m.role == "teacher"}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(teachers.keys())).all()}

    out = []
    for ls in rows:
        e = entries.get(ls.timetable_entry_id)
        if e is None:
            continue
        teacher_m = teachers.get(e.teacher_membership_id)
        out.append(
            {
                "id": ls.id,
                "date": ls.session_date,
                "start": ls.start_at.isoformat() + "Z",
                "end": ls.end_at.isoformat() + "Z",
                "class": classes[e.class_id].name if e.class_id in classes else None,
                "subject": subjects[e.subject_id].name if e.subject_id in subjects else None,
                "classroom": classrooms[e.classroom_id].name if e.classroom_id and e.classroom_id in classrooms else None,
                "teacher": users[teacher_m.user_id].display_name if teacher_m and teacher_m.user_id in users else None,
            }
        )
    return out