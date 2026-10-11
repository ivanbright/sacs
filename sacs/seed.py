"""Deterministic demonstration seed data.

Everything created here is flagged ``is_demo``. Passwords are fixed demo
credentials described in the README - they are *never* reused in deployed
environments. Running this function twice on the same database is idempotent.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session as OrmSession

from .models import (
    BellSchedule,
    Classroom,
    Invitation,
    LessonSession,
    Membership,
    MembershipStatus,
    Role,
    School,
    SchoolClass,
    Subject,
    TeacherAssignment,
    TimetableEntry,
    TimetableStatus,
    User,
    utcnow,
)
from .security import hash_password

DEMO_PASSWORD = "SacsDemo123!"
PLATFORM_EMAIL = "platform@demo.sacs"

SCHOOLS = [
    {
        "name": "Northstar Academy",
        "slug": "northstar-academy",
        "timezone": "Europe/London",
        "head": ("head.northstar@demo.sacs", "Amara Okonkwo"),
        "dos": ("dos.northstar@demo.sacs", "Priya Raman"),
        "teacher": ("teacher.northstar@demo.sacs", "Jonas Weber"),
        "bell": ("bell.northstar@demo.sacs", "Bell Monitor"),
        "classes": ["Year 7", "Year 8"],
        "subjects": [("Mathematics", "MTH"), ("Physics", "PHY"), ("English", "ENG")],
        "classrooms": ["Hall A", "Lab 1"],
    },
    {
        "name": "Riverside Institute",
        "slug": "riverside-institute",
        "timezone": "America/New_York",
        "head": ("head.riverside@demo.sacs", "Marcus Chen"),
        "dos": ("dos.riverside@demo.sacs", "Elena Petrova"),
        "teacher": ("teacher.riverside@demo.sacs", "Sofia Marin"),
        "bell": ("bell.riverside@demo.sacs", "Riverside Bell"),
        "classes": ["Grade 9"],
        "subjects": [("Chemistry", "CHM"), ("History", "HST")],
        "classrooms": ["Room 201"],
    },
]


def period_blocks() -> list[dict]:
    """A standard day: 8 periods with five-minute gaps."""
    times = [
        ("08:00", "08:45", "Period 1"),
        ("08:50", "09:35", "Period 2"),
        ("09:45", "10:30", "Period 3"),
        ("10:35", "11:20", "Period 4"),
        ("11:30", "12:15", "Period 5"),
        ("13:00", "13:45", "Period 6"),
        ("13:50", "14:35", "Period 7"),
        ("14:45", "15:30", "Period 8"),
    ]
    return [{"label": label, "start": start, "end": end} for start, end, label in times]


def seed_platform_admin(db: OrmSession) -> User:
    admin = db.query(User).filter(User.email == PLATFORM_EMAIL).first()
    if admin is None:
        admin = User(
            email=PLATFORM_EMAIL,
            password_hash=hash_password(DEMO_PASSWORD),
            display_name="Platform Owner (Demo)",
            is_platform_admin=True,
            is_demo=True,
        )
        db.add(admin)
        db.flush()
    return admin


def _ensure_school(db: OrmSession, spec: dict) -> School:
    school = db.query(School).filter(School.slug == spec["slug"]).first()
    if school is None:
        school = School(
            name=spec["name"],
            slug=spec["slug"],
            timezone=spec["timezone"],
            status="active",
            is_demo=True,
            verified_at=utcnow(),
        )
        db.add(school)
        db.flush()
    return school


def _ensure_user(db: OrmSession, email: str, name: str) -> User:
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(DEMO_PASSWORD),
            display_name=name,
            is_demo=True,
        )
        db.add(user)
        db.flush()
    return user


def _ensure_membership(db: OrmSession, school: School, user: User, role: str) -> Membership:
    member = (
        db.query(Membership)
        .filter(Membership.user_id == user.id, Membership.school_id == school.id)
        .first()
    )
    if member is None:
        member = Membership(
            user_id=user.id, school_id=school.id, role=role, status=MembershipStatus.ACTIVE
        )
        db.add(member)
        db.flush()
    return member


def seed_school(db: OrmSession, spec: dict) -> dict:
    school = _ensure_school(db, spec)

    head_u = _ensure_user(db, spec["head"][0], spec["head"][1])
    dos_u = _ensure_user(db, spec["dos"][0], spec["dos"][1])
    teacher_u = _ensure_user(db, spec["teacher"][0], spec["teacher"][1])
    bell_u = _ensure_user(db, spec["bell"][0], spec["bell"][1])
    head_m = _ensure_membership(db, school, head_u, Role.SCHOOL_HEAD)
    dos_m = _ensure_membership(db, school, dos_u, Role.DOS)
    teacher_m = _ensure_membership(db, school, teacher_u, Role.TEACHER)
    bell_m = _ensure_membership(db, school, bell_u, Role.BELL_MONITOR)

    classes = {}
    for name in spec["classes"]:
        c = db.query(SchoolClass).filter_by(school_id=school.id, name=name).first()
        if c is None:
            c = SchoolClass(school_id=school.id, name=name)
            db.add(c)
            db.flush()
        classes[name] = c

    subjects = {}
    for name, code in spec["subjects"]:
        s = db.query(Subject).filter_by(school_id=school.id, name=name).first()
        if s is None:
            s = Subject(school_id=school.id, name=name, code=code)
            db.add(s)
            db.flush()
        subjects[name] = s

    classrooms = {}
    for name in spec["classrooms"]:
        c = db.query(Classroom).filter_by(school_id=school.id, name=name).first()
        if c is None:
            c = Classroom(school_id=school.id, name=name, capacity=30)
            db.add(c)
            db.flush()
        classrooms[name] = c

    # Teacher assignment: first subject of first class.
    if db.query(TeacherAssignment).filter_by(school_id=school.id).first() is None:
        first_class = list(classes.values())[0]
        first_subject = list(subjects.values())[0]
        first_room = list(classrooms.values())[0]
        db.add(
            TeacherAssignment(
                school_id=school.id,
                teacher_membership_id=teacher_m.id,
                class_id=first_class.id,
                subject_id=first_subject.id,
                classroom_id=first_room.id,
            )
        )

    seed_timetable(db, school, teacher_m, list(classes.values()), list(subjects.values()), list(classrooms.values()))
    seed_bell_schedule(db, school)
    return {
        "school": school,
        "head_user": head_u,
        "dos_user": dos_u,
        "teacher_user": teacher_u,
        "bell_user": bell_u,
        "head_membership": head_m,
        "dos_membership": dos_m,
        "teacher_membership": teacher_m,
        "bell_membership": bell_m,
        "classes": classes,
        "subjects": subjects,
        "classrooms": classrooms,
    }


def seed_timetable(db, school: School, teacher_m, classes, subjects, classrooms) -> None:
    if db.query(TimetableEntry).filter_by(school_id=school.id).first() is not None:
        return
    # Monday (0) through Friday (4), cycling subject/room by weekday.
    for weekday in range(5):
        subject = subjects[weekday % len(subjects)]
        room = classrooms[weekday % len(classrooms)]
        db.add(
            TimetableEntry(
                school_id=school.id,
                class_id=classes[0].id,
                subject_id=subject.id,
                classroom_id=room.id,
                teacher_membership_id=teacher_m.id,
                day_of_week=weekday,
                start_time="09:00",
                end_time="09:50",
                status=TimetableStatus.PUBLISHED,
                academic_term="Autumn 2026",
            )
        )
    db.flush()
    _generate_sessions(db, school, days=14)


def _generate_sessions(db, school: School, days: int) -> None:
    entries = db.query(TimetableEntry).filter_by(school_id=school.id).all()
    try:
        tz = ZoneInfo(school.timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    today = utcnow().astimezone(tz).date()
    for entry in entries:
        for offset in range(days):
            day = today + timedelta(days=offset)
            if day.weekday() != entry.day_of_week:
                continue
            date_key = day.isoformat()
            existing = (
                db.query(LessonSession)
                .filter(
                    LessonSession.timetable_entry_id == entry.id,
                    LessonSession.session_date == date_key,
                )
                .first()
            )
            if existing:
                continue
            start_dt = datetime.combine(day, dtime(*map(int, entry.start_time.split(":"))), tzinfo=tz)
            end_dt = datetime.combine(day, dtime(*map(int, entry.end_time.split(":"))), tzinfo=tz)
            db.add(
                LessonSession(
                    school_id=school.id,
                    timetable_entry_id=entry.id,
                    session_date=date_key,
                    start_at=start_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
                    end_at=end_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
                )
            )


def seed_bell_schedule(db, school: School) -> None:
    if db.query(BellSchedule).filter_by(school_id=school.id).first() is not None:
        return
    # Remove periods already passed today so the monitor shows a "today" schedule,
    # and add a near-future demonstration block based on wall clock.
    periods = period_blocks()
    now_local = utcnow().astimezone(ZoneInfo(school.timezone))
    current_mins = now_local.hour * 60 + now_local.minute
    future_periods = []
    for p in periods:
        h, m = map(int, p["start"].split(":"))
        if h * 60 + m >= current_mins - 5:
            future_periods.append(p)
    if not future_periods:
        future_periods = periods[:3]
    db.add(
        BellSchedule(
            school_id=school.id,
            name="Standard Day",
            timezone=school.timezone,
            is_active=True,
            paused=False,
            periods=future_periods,
        )
    )


def seed_all(db: OrmSession) -> dict:
    """Idempotently create the full demonstration dataset."""
    seed_platform_admin(db)
    result = {"platform": {"email": PLATFORM_EMAIL}, "schools": []}
    for spec in SCHOOLS:
        result["schools"].append(seed_school(db, spec))
    db.commit()
    return result