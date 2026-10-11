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
    Invitation,
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
from .invitations import create_invitation, invitation_response

router = APIRouter(prefix="/api/schools", tags=["schools"])


class RoleLabel:
    pass


ROLE_NAMES = Role.LABELS


def _user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "display_name": u.display_name, "is_demo": u.is_demo}


# --------------------------------------------------------------------------- #
# Invitation lifecycle lives in ``routers.invitations``. School-scoped
# invitation *creation* (Director -> Teacher) is defined in the staff section.
# --------------------------------------------------------------------------- #


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


class InvitationCreateRequest(BaseModel):
    email: EmailStr
    role: str
    expires_in_days: int = Field(default=14, ge=1, le=365)


@router.post("/{school_id}/invitations", status_code=201)
def create_school_invitation(
    school_id: str,
    payload: InvitationCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    """Invite a person to this school. Directors may invite teachers and
    directors of studies; the platform owner invites school heads."""
    membership = require_membership(ctx, db, school_id, Perm.STAFF_MANAGE)
    if payload.role not in (Role.TEACHER, Role.DOS):
        raise HTTPException(
            status_code=422,
            detail="A Director may invite 'teacher' or 'dos' memberships",
        )
    school = db.get(School, school_id)
    invitation, token = create_invitation(
        db,
        school=school,
        email=payload.email,
        role=payload.role,
        invited_by=ctx.user.id,
        expires_in_days=payload.expires_in_days,
    )
    audit.record(
        db,
        school_id=school_id,
        actor_user_id=ctx.user.id,
        actor_role=membership.role,
        action="invitation.created",
        summary=f"Invited {invitation.email} as {payload.role}",
        entity_type="invitation",
        entity_id=invitation.id,
        meta={"role": payload.role},
    )
    invitee = db.query(User).filter(User.email == invitation.email).first()
    if invitee is not None:
        notify(
            db,
            school_id=school_id,
            user_id=invitee.id,
            type="invitation.created",
            title=f"Invitation to {school.name}",
            body=f"You were invited as {Role.LABELS.get(payload.role, payload.role)}.",
        )
    enqueue(
        db,
        Topic.STAFF,
        {"school_id": school_id, "audience": {"roles": ["school_head", "dos"]}, "data": {"action": "invited"}},
    )
    db.commit()
    return invitation_response(db, invitation, token)


@router.post("/{school_id}/invitations/{invitation_id}/revoke")
def revoke_school_invitation(
    school_id: str,
    invitation_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    membership = require_membership(ctx, db, school_id, Perm.STAFF_MANAGE)
    invitation = db.get(Invitation, invitation_id)
    if invitation is None or invitation.school_id != school_id:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invitation.status != "pending":
        raise HTTPException(status_code=409, detail=f"Invitation is already {invitation.status}")
    invitation.status = "revoked"
    audit.record(
        db,
        school_id=school_id,
        actor_user_id=ctx.user.id,
        actor_role=membership.role,
        action="invitation.revoked",
        summary=f"Revoked invitation for {invitation.email}",
        entity_type="invitation",
        entity_id=invitation.id,
    )
    db.commit()
    return {"id": invitation.id, "status": invitation.status}


@router.get("/{school_id}/invitations")
def list_school_invitations(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.STAFF_MANAGE)
    rows = (
        db.query(Invitation)
        .filter(Invitation.school_id == school_id)
        .order_by(Invitation.created_at.desc())
        .all()
    )
    return [
        {
            "id": i.id,
            "email": i.email,
            "role": i.role,
            "role_label": ROLE_NAMES.get(i.role, i.role),
            "status": i.status,
            "created_at": i.created_at.isoformat() + "Z",
            "expires_at": i.expires_at.isoformat() + "Z" if i.expires_at else None,
        }
        for i in rows
    ]


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