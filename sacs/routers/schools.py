"""School workspace router: profile, staff, invitations, overview, audit."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session as OrmSession

from .. import audit
from ..db import get_db
from ..deps import AuthContext, get_auth_context, require_membership
from ..events import Topic, enqueue, notify
from ..models import (
    CompletionRequest,
    Membership,
    MembershipStatus,
    Notification,
    RequestState,
    Role,
    School,
    SchoolStatus,
    User,
    utcnow,
)
from ..rbac import Perm
from ..security import create_session, generate_token, hash_password, hash_token

router = APIRouter(prefix="/api/schools", tags=["schools"])


class RoleLabel:
    pass


ROLE_NAMES = Role.LABELS


def _user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "display_name": u.display_name, "is_demo": u.is_demo}


# --------------------------------------------------------------------------- #
# Invitation acceptance (defined secure process for establishing a school head)
# --------------------------------------------------------------------------- #

accept_router = APIRouter(tags=["invitations"])


class InvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=8)
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1, max_length=255)


@accept_router.post("/api/invitations/accept")
def accept_invitation(
    payload: InvitationAcceptRequest,
    db: OrmSession = Depends(get_db),
) -> dict:
    from ..models import Invitation

    invitation = (
        db.query(Invitation).filter(Invitation.token_hash == hash_token(payload.token)).first()
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="Unknown invitation token")
    if invitation.status == "accepted":
        raise HTTPException(status_code=409, detail="Invitation already used")
    if invitation.expires_at is not None and invitation.expires_at < utcnow():
        raise HTTPException(status_code=410, detail="Invitation expired")

    school = db.get(School, invitation.school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="School not found")

    existing = db.query(User).filter(User.email == invitation.email).first()
    if existing is not None and existing.is_active:
        user = existing
    elif existing is not None and not existing.is_active:
        existing.is_active = True
        user = existing
    else:
        user = User(
            email=invitation.email,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name.strip(),
            is_demo=school.is_demo,
        )
        db.add(user)
        db.flush()

    membership = (
        db.query(Membership)
        .filter(Membership.user_id == user.id, Membership.school_id == school.id)
        .first()
    )
    if membership is None:
        membership = Membership(
            user_id=user.id, school_id=school.id, role=invitation.role, status=MembershipStatus.ACTIVE
        )
        db.add(membership)
    else:
        membership.role = invitation.role
        membership.status = MembershipStatus.ACTIVE

    invitation.status = "accepted"
    invitation.accepted_at = utcnow()

    if school.status == SchoolStatus.PROVISIONING:
        school.status = SchoolStatus.ACTIVE
        school.verified_at = utcnow()

    audit.record(
        db, school_id=school.id, actor_user_id=user.id, actor_role=invitation.role,
        action="membership.activated", summary=f"Invitation accepted; '{user.display_name}' joined as {invitation.role}",
        entity_type="membership", entity_id=membership.id,
        meta={"invitation_id": invitation.id},
    )
    db.commit()
    return {"ok": True, "school_id": school.id, "user_id": user.id, "membership_id": membership.id}


# --------------------------------------------------------------------------- #
# Staff management
# --------------------------------------------------------------------------- #

class StaffCreateRequest(BaseModel):
    email: EmailStr
    role: str
    display_name: str = Field(min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=8)


@router.get("/{school_id}/staff")
def list_staff(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.STAFF_MANAGE)
    members = (
        db.query(Membership)
        .filter(Membership.school_id == school_id)
        .order_by(Membership.created_at)
        .all()
    )
    users = {u.id: u for u in db.query(User).filter(User.id.in_([m.user_id for m in members])).all()}
    return [
        {
            "id": m.id,
            "user": _user_out(users[m.user_id]),
            "role": m.role,
            "role_label": ROLE_NAMES.get(m.role, m.role),
            "status": m.status,
            "created_at": m.created_at.isoformat() + "Z",
        }
        for m in members
    ]


@router.post("/{school_id}/staff", status_code=201)
def create_staff(
    school_id: str,
    payload: StaffCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    membership = require_membership(ctx, db, school_id, Perm.STAFF_MANAGE)
    if payload.role not in Role.ALL:
        raise HTTPException(status_code=422, detail=f"Unknown role: {payload.role}")
    email = payload.email.lower().strip()

    generated_password = None
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        generated_password = payload.password or generate_token()
        user = User(
            email=email,
            password_hash=hash_password(generated_password),
            display_name=payload.display_name.strip(),
            is_demo=getattr(ctx.user, "is_demo", False),
        )
        db.add(user)
        db.flush()
    else:
        user.is_active = True
        user.display_name = payload.display_name.strip()
        if payload.password:
            user.password_hash = hash_password(payload.password)

    existing = (
        db.query(Membership)
        .filter(Membership.user_id == user.id, Membership.school_id == school_id)
        .first()
    )
    if existing is not None and existing.status != MembershipStatus.DEACTIVATED:
        raise HTTPException(status_code=409, detail="User already has a membership here")
    if existing is not None:
        existing.status = MembershipStatus.ACTIVE
        existing.role = payload.role
        existing.deactivated_at = None
        member = existing
    else:
        member = Membership(
            user_id=user.id, school_id=school_id, role=payload.role, status=MembershipStatus.ACTIVE
        )
        db.add(member)

    audit.record(
        db, school_id=school_id, actor_user_id=ctx.user.id, actor_role=membership.role,
        action="staff.created", summary=f"Added {user.display_name} as {payload.role}",
        entity_type="membership", entity_id=member.id,
    )
    notify(
        db, school_id=school_id, user_id=user.id, type="staff.invited",
        title="You were added to the school", body=f"Role: {payload.role}",
    )
    enqueue(db, Topic.STAFF, {"school_id": school_id, "audience": {"roles": ["school_head", "dos"]}, "data": {"action": "created", "user_id": user.id}})
    db.commit()
    result = {"id": member.id, "role": payload.role, "status": member.status, "user": _user_out(user)}
    if generated_password is not None:
        result["generated_password"] = generated_password
    return result


class StaffUpdateRequest(BaseModel):
    role: str | None = None
    status: str | None = None


@router.patch("/{school_id}/staff/{membership_id}")
def update_staff(
    school_id: str,
    membership_id: str,
    payload: StaffUpdateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    membership = require_membership(ctx, db, school_id, Perm.STAFF_MANAGE)
    target = db.get(Membership, membership_id)
    if target is None or target.school_id != school_id:
        raise HTTPException(status_code=404, detail="Membership not found")
    if target.user_id == ctx.user.id:
        raise HTTPException(status_code=422, detail="Cannot change your own membership here")
    if payload.role is not None:
        if payload.role not in Role.ALL:
            raise HTTPException(status_code=422, detail="Unknown role")
        target.role = payload.role
    if payload.status is not None:
        if payload.status == MembershipStatus.DEACTIVATED:
            target.status = MembershipStatus.DEACTIVATED
            target.deactivated_at = utcnow()
        elif payload.status == MembershipStatus.ACTIVE:
            target.status = MembershipStatus.ACTIVE
            target.deactivated_at = None
        else:
            raise HTTPException(status_code=422, detail="Invalid membership status")
    audit.record(
        db, school_id=school_id, actor_user_id=ctx.user.id, actor_role=membership.role,
        action="staff.updated", summary=f"Membership {target.id[:8]} updated (role={target.role}, status={target.status})",
        entity_type="membership", entity_id=target.id,
    )
    enqueue(db, Topic.STAFF, {"school_id": school_id, "audience": {"roles": ["school_head", "dos"]}, "data": {"action": "updated"}})
    db.commit()
    return {"id": target.id, "role": target.role, "status": target.status}


# --------------------------------------------------------------------------- #
# School profile / settings / overview
# --------------------------------------------------------------------------- #

@router.get("/{school_id}/profile")
def school_profile(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.SCHOOL_VIEW)
    school = db.get(School, school_id)
    return {
        "id": school.id,
        "name": school.name,
        "slug": school.slug,
        "status": school.status,
        "timezone": school.timezone,
        "is_demo": school.is_demo,
        "created_at": school.created_at.isoformat() + "Z",
    }


class SettingsUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    timezone: str | None = None


@router.put("/{school_id}/settings")
def update_settings(
    school_id: str,
    payload: SettingsUpdate,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    membership = require_membership(ctx, db, school_id, Perm.SCHOOL_SETTINGS_MANAGE)
    school = db.get(School, school_id)
    if payload.name:
        school.name = payload.name.strip()
    if payload.timezone:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(payload.timezone)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=422, detail="Invalid timezone")
        school.timezone = payload.timezone
    audit.record(
        db, school_id=school_id, actor_user_id=ctx.user.id, actor_role=membership.role,
        action="school.settings_updated", summary=f"Settings updated by {membership.role}",
        entity_type="school", entity_id=school.id,
    )
    db.commit()
    return {"id": school.id, "name": school.name, "timezone": school.timezone, "status": school.status}


@router.get("/{school_id}/overview")
def school_overview(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.SCHOOL_VIEW)
    from ..models import BellEvent, LessonSession, TeacherAssignment, TimetableEntry

    pending = (
        db.query(func.count(CompletionRequest.id))
        .filter(
            CompletionRequest.school_id == school_id,
            CompletionRequest.state.in_(
                [RequestState.SUBMITTED, RequestState.UNDER_DOS_REVIEW, RequestState.FORWARDED_TO_HEAD]
            ),
        )
        .scalar()
        or 0
    )
    today = utcnow().date().isoformat()
    lessons_today = (
        db.query(func.count(LessonSession.id))
        .filter(LessonSession.school_id == school_id, LessonSession.session_date == today)
        .scalar()
        or 0
    )
    return {
        "school_id": school_id,
        "pending_requests": pending,
        "lessons_today": lessons_today,
        "timetable_entries": db.query(func.count(TimetableEntry.id)).filter_by(school_id=school_id).scalar() or 0,
        "assignments": db.query(func.count(TeacherAssignment.id)).filter_by(school_id=school_id).scalar() or 0,
        "recent_bell_events": db.query(func.count(BellEvent.id)).filter_by(school_id=school_id).scalar() or 0,
    }


@router.get("/{school_id}/audit")
def school_audit(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.AUDIT_VIEW_SCHOOL)
    return [
        {
            "id": e.id,
            "actor_user_id": e.actor_user_id,
            "action": e.action,
            "summary": e.summary,
            "created_at": e.created_at.isoformat() + "Z",
        }
        for e in audit.list_events(db, school_id=school_id, limit=200)
    ]


@router.get("/{school_id}/notifications")
def notifications(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.SCHOOL_VIEW)
    rows = (
        db.query(Notification)
        .filter(
            Notification.user_id == ctx.user.id,
            (Notification.school_id == school_id) | (Notification.school_id.is_(None)),
        )
        .order_by(Notification.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": n.id,
            "type": n.type,
            "title": n.title,
            "body": n.body,
            "read": n.read,
            "created_at": n.created_at.isoformat() + "Z",
        }
        for n in rows
    ]