"""Platform administration router: tenant provisioning, status, health, audit."""

from __future__ import annotations

import platform as platform_mod
import time as time_mod
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session as OrmSession

from .. import audit
from ..db import get_db, get_engine
from ..deps import AuthContext, get_auth_context, require_platform
from ..events import Topic, enqueue
from ..models import (
    BellEvent,
    CompletionRequest,
    School,
    SchoolStatus,
    User,
    utcnow,
)
from ..rbac import Perm
from ..scheduler import scheduler_status
from ..security import generate_token, hash_token
from .. import __version__

router = APIRouter(prefix="/api/platform", tags=["platform"])


class CreateSchoolRequest(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    slug: str | None = Field(default=None, max_length=120)
    timezone: str = "UTC"
    head_email: EmailStr | None = None
    head_name: str | None = None


def _leaf_counts(db: OrmSession, school: School) -> dict:
    from ..models import (
        BellSchedule,
        Classroom,
        LessonSession,
        Membership,
        SchoolClass,
        Subject,
        TeacherAssignment,
        TimetableEntry,
    )

    locked = {"school_id": school.id}
    return {
        "members": db.query(func.count(Membership.id)).filter_by(**locked).scalar() or 0,
        "classes": db.query(func.count(SchoolClass.id)).filter_by(**locked).scalar() or 0,
        "subjects": db.query(func.count(Subject.id)).filter_by(**locked).scalar() or 0,
        "classrooms": db.query(func.count(Classroom.id)).filter_by(**locked).scalar() or 0,
        "assignments": db.query(func.count(TeacherAssignment.id)).filter_by(**locked).scalar() or 0,
        "timetable_entries": db.query(func.count(TimetableEntry.id)).filter_by(**locked).scalar() or 0,
        "lessons": db.query(func.count(LessonSession.id)).filter_by(**locked).scalar() or 0,
        "completion_requests": db.query(func.count(CompletionRequest.id)).filter_by(**locked).scalar() or 0,
        "bell_schedules": db.query(func.count(BellSchedule.id)).filter_by(**locked).scalar() or 0,
        "bell_events": db.query(func.count(BellEvent.id)).filter_by(**locked).scalar() or 0,
    }


def _serialize_school(db: OrmSession, school: School, with_counts: bool = False) -> dict:
    base = {
        "id": school.id,
        "name": school.name,
        "slug": school.slug,
        "status": school.status,
        "timezone": school.timezone,
        "is_demo": school.is_demo,
        "created_at": school.created_at.isoformat() + "Z",
        "verified_at": school.verified_at.isoformat() + "Z" if school.verified_at else None,
    }
    if with_counts:
        base["counts"] = _leaf_counts(db, school)
    return base


@router.get("/schools")
def list_schools(
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_platform(ctx, db, Perm.PLATFORM_SCHOOLS_MANAGE)
    schools = db.query(School).order_by(School.created_at).all()
    return [_serialize_school(db, s, with_counts=True) for s in schools]


@router.post("/schools", status_code=201)
def create_school(
    payload: CreateSchoolRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_platform(ctx, db, Perm.PLATFORM_SCHOOLS_MANAGE)
    slug = (payload.slug or "").strip() or payload.name.lower().replace(" ", "-")[:60]
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug).strip("-").lower()
    if db.query(School).filter(School.slug == slug).first():
        raise HTTPException(status_code=409, detail="A school with this slug already exists")

    school = School(
        name=payload.name.strip(),
        slug=slug or f"school-{utcnow().strftime('%Y%m%d%H%M%S')}",
        timezone=payload.timezone or "UTC",
        status=SchoolStatus.PROVISIONING,
    )
    db.add(school)
    db.flush()

    invite_token = None
    if payload.head_email:
        from ..models import Invitation

        invite_token = generate_token()
        db.add(
            Invitation(
                school_id=school.id,
                email=payload.head_email.lower().strip(),
                role="school_head",
                token_hash=hash_token(invite_token),
                invited_by=ctx.user.id,
                expires_at=utcnow() + timedelta(days=14),
            )
        )

    audit.record(
        db,
        school_id=school.id,
        actor_user_id=ctx.user.id,
        actor_role="platform_admin",
        action="school.created",
        summary=f"School '{school.name}' created and placed in provisioning",
        entity_type="school",
        entity_id=school.id,
    )
    enqueue(db, Topic.SCHOOLS, {"school_id": None, "audience": "platform", "data": {"action": "created", "school_id": school.id}})
    db.commit()

    result = _serialize_school(db, school)
    if invite_token:
        result["head_invitation_token"] = invite_token
    return result


@router.get("/schools/{school_id}")
def get_school(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_platform(ctx, db, Perm.PLATFORM_SCHOOLS_MANAGE)
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="School not found")
    return _serialize_school(db, school, with_counts=True)


class InviteHeadRequest(BaseModel):
    email: EmailStr
    expires_in_days: int = Field(default=14, ge=1, le=365)


@router.post("/schools/{school_id}/invitations", status_code=201)
def platform_invite_head(
    school_id: str,
    payload: InviteHeadRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    """Platform owner invites a Director (school head) to an existing school."""
    require_platform(ctx, db, Perm.PLATFORM_SCHOOLS_MANAGE)
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="School not found")
    from .invitations import create_invitation, invitation_response

    invitation, token = create_invitation(
        db,
        school=school,
        email=payload.email,
        role="school_head",
        invited_by=ctx.user.id,
        expires_in_days=payload.expires_in_days,
    )
    audit.record(
        db,
        school_id=school.id,
        actor_user_id=ctx.user.id,
        actor_role="platform_admin",
        action="invitation.created",
        summary=f"Invited {invitation.email} as school_head to '{school.name}'",
        entity_type="invitation",
        entity_id=invitation.id,
        meta={"role": "school_head"},
    )
    db.commit()
    return invitation_response(db, invitation, token)


@router.post("/schools/{school_id}/suspend")
def suspend_school(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_platform(ctx, db, Perm.PLATFORM_SCHOOLS_SUSPEND)
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="School not found")
    school.status = SchoolStatus.SUSPENDED
    audit.record(
        db, school_id=school.id, actor_user_id=ctx.user.id, actor_role="platform_admin",
        action="school.suspended", summary=f"School '{school.name}' suspended",
        entity_type="school", entity_id=school.id,
    )
    enqueue(db, Topic.SCHOOLS, {"school_id": None, "audience": "platform", "data": {"action": "suspended", "school_id": school.id}})
    db.commit()
    return _serialize_school(db, school)


@router.post("/schools/{school_id}/reactivate")
def reactivate_school(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_platform(ctx, db, Perm.PLATFORM_SCHOOLS_SUSPEND)
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="School not found")
    school.status = SchoolStatus.ACTIVE
    audit.record(
        db, school_id=school.id, actor_user_id=ctx.user.id, actor_role="platform_admin",
        action="school.reactivated", summary=f"School '{school.name}' reactivated",
        entity_type="school", entity_id=school.id,
    )
    enqueue(db, Topic.SCHOOLS, {"school_id": None, "audience": "platform", "data": {"action": "reactivated", "school_id": school.id}})
    db.commit()
    return _serialize_school(db, school)


@router.get("/health")
def health(
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_platform(ctx, db, Perm.PLATFORM_HEALTH_VIEW)
    db_status = "ok"
    try:
        db.execute(func.count(User.id).select())
    except Exception:  # noqa: BLE001
        db_status = "error"
    engine = get_engine()
    url = str(engine.url)
    if url.startswith("sqlite"):
        url = "sqlite:///<local>"
    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "version": __version__,
        "python": platform_mod.python_version(),
        "database": "ok" if db_status == "ok" else "error",
        "database_url": url,
        "scheduler": scheduler_status(),
        "uptime_seconds": round(time_mod.time() - _boot_time, 1),
    }


@router.get("/audit")
def platform_audit(
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_platform(ctx, db, Perm.PLATFORM_AUDIT_VIEW)
    return [
        {
            "id": e.id,
            "school_id": e.school_id,
            "actor_user_id": e.actor_user_id,
            "action": e.action,
            "summary": e.summary,
            "created_at": e.created_at.isoformat() + "Z",
        }
        for e in audit.list_events(db, platform=True, limit=200)
    ]


_boot_time = time_mod.time()