"""Invitation lifecycle.

An invitation is a small, persisted, single-use record that ties an email
address to a school and a role. It is created by an authorized actor (the
platform administrator for a school head, or a school Director for staff) and
accepted by the named recipient.

Two acceptance paths exist:

* ``POST /api/invitations/accept`` (authenticated) - the recipient is already
  signed in; the invitation must be addressed to the signed-in user's email.
  This is the path exercised by the local demonstration.
* ``POST /api/invitations/accept-token`` (anonymous, token-based) - for a
  recipient who does not yet have an account; they supply the invitation token
  plus the password and display name for their new account.

Only the SHA-256 hash of an invitation token is ever stored. Tokens are returned
once to the creator for copy/paste and are never written to audit logs.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

from .. import audit
from ..db import get_db
from ..deps import AuthContext, get_auth_context
from ..events import Topic, enqueue, notify
from ..models import (
    Invitation,
    Membership,
    MembershipStatus,
    Role,
    School,
    SchoolStatus,
    User,
    utcnow,
)
from ..security import generate_token, hash_password, hash_token

router = APIRouter(prefix="/api/invitations", tags=["invitations"])

INVITATION_TTL_DAYS = 14


def _invite_out(db: OrmSession, inv: Invitation) -> dict:
    school = db.get(School, inv.school_id)
    return {
        "id": inv.id,
        "email": inv.email,
        "role": inv.role,
        "role_label": Role.LABELS.get(inv.role, inv.role),
        "school_id": inv.school_id,
        "school_name": school.name if school else None,
        "school_slug": school.slug if school else None,
        "status": inv.status,
        "created_at": inv.created_at.isoformat() + "Z",
        "expires_at": inv.expires_at.isoformat() + "Z" if inv.expires_at else None,
        "accepted_at": inv.accepted_at.isoformat() + "Z" if inv.accepted_at else None,
    }


@router.get("/mine")
def my_invitations(
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    """Every invitation addressed to the authenticated identity's email."""
    rows = (
        db.query(Invitation)
        .filter(Invitation.email == ctx.user.email.lower().strip())
        .order_by(Invitation.created_at.desc())
        .all()
    )
    return [_invite_out(db, r) for r in rows]


class AcceptByBody(BaseModel):
    invitation_id: str
    display_name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8)


@router.post("/accept")
def accept_invitation(
    payload: AcceptByBody,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    """Accept an invitation as the authenticated recipient (matched by email)."""
    invitation = db.get(Invitation, payload.invitation_id)
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invitation.email.lower().strip() != ctx.user.email.lower().strip():
        raise HTTPException(status_code=403, detail="This invitation is for a different recipient")
    return _activate(db, invitation, user=ctx.user)


class TokenAcceptBody(BaseModel):
    token: str = Field(min_length=8)
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1, max_length=255)


@router.post("/accept-token")
def accept_invitation_token(
    payload: TokenAcceptBody,
    db: OrmSession = Depends(get_db),
) -> dict:
    """Accept an invitation with a copy-pasted token when no account exists yet."""
    invitation = (
        db.query(Invitation).filter(Invitation.token_hash == hash_token(payload.token)).first()
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="Unknown invitation token")

    existing = db.query(User).filter(User.email == invitation.email).first()
    if existing is None:
        user = User(
            email=invitation.email,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name.strip(),
            is_demo=False,
        )
        db.add(user)
        db.flush()
    else:
        user = existing
        if not user.is_active:
            user.is_active = True
    return _activate(db, invitation, user=user)


def _activate(db: OrmSession, invitation: Invitation, *, user: User) -> dict:
    if invitation.status == "accepted":
        raise HTTPException(status_code=409, detail="Invitation already accepted")
    if invitation.status == "revoked":
        raise HTTPException(status_code=410, detail="Invitation was revoked")
    if invitation.expires_at is not None and invitation.expires_at < utcnow():
        raise HTTPException(status_code=410, detail="Invitation expired")

    school = db.get(School, invitation.school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="School not found")
    if school.status == SchoolStatus.SUSPENDED:
        raise HTTPException(status_code=403, detail="School is suspended")

    existing = (
        db.query(Membership)
        .filter(Membership.user_id == user.id, Membership.school_id == school.id)
        .first()
    )
    if existing is not None and existing.status == MembershipStatus.ACTIVE:
        raise HTTPException(status_code=409, detail="Already a member of this school")
    if existing is not None:
        existing.role = invitation.role
        existing.status = MembershipStatus.ACTIVE
        existing.deactivated_at = None
        membership = existing
    else:
        membership = Membership(
            user_id=user.id,
            school_id=school.id,
            role=invitation.role,
            status=MembershipStatus.ACTIVE,
            activated_at=utcnow(),
        )
        db.add(membership)

    if not user.is_active:
        user.is_active = True
    invitation.status = "accepted"
    invitation.accepted_at = utcnow()
    if school.status == SchoolStatus.PROVISIONING:
        school.status = SchoolStatus.ACTIVE
        school.verified_at = utcnow()

    # Notify the person who issued the invitation, and -- when a Director joins
    # -- every Platform Administrator. This closes the onboarding loop the
    # specification calls for without granting any approval authority.
    if invitation.invited_by:
        notify(
            db,
            school_id=school.id,
            user_id=invitation.invited_by,
            type="invitation.accepted",
            title="Invitation accepted",
            body=f"{user.display_name} accepted the invitation to join '{school.name}'.",
            entity_type="invitation",
            entity_id=invitation.id,
        )
    if invitation.role == Role.SCHOOL_HEAD:
        for admin in db.query(User).filter(User.is_platform_admin.is_(True)).all():
            notify(
                db,
                school_id=school.id,
                user_id=admin.id,
                type="school.director_joined",
                title="Director joined a school",
                body=f"{user.display_name} accepted the Director invitation for '{school.name}'.",
                entity_type="invitation",
                entity_id=invitation.id,
            )

    audit.record(
        db,
        school_id=school.id,
        actor_user_id=user.id,
        actor_role=invitation.role,
        action="invitation.accepted",
        summary=f"{user.display_name} joined '{school.name}' as {invitation.role}",
        entity_type="membership",
        entity_id=membership.id,
        meta={"invitation_id": invitation.id, "role": invitation.role},
    )
    notify(
        db,
        school_id=school.id,
        user_id=user.id,
        type="invitation.accepted",
        title="Welcome to " + school.name,
        body=f"You joined as {Role.LABELS.get(invitation.role, invitation.role)}.",
    )
    enqueue(
        db,
        Topic.STAFF,
        {
            "school_id": school.id,
            "audience": {"roles": ["school_head", "dos"]},
            "data": {"action": "accepted", "user_id": user.id, "role": invitation.role},
        },
    )
    db.commit()
    return {
        "ok": True,
        "school_id": school.id,
        "school_slug": school.slug,
        "user_id": user.id,
        "membership_id": membership.id,
        "role": membership.role,
        "next": f"session switch {user.email}",
    }


# --------------------------------------------------------------------------- #
# Creation helpers shared by the school and platform routers
# --------------------------------------------------------------------------- #

def create_invitation(
    db: OrmSession,
    *,
    school: School,
    email: str,
    role: str,
    invited_by: str | None,
    expires_in_days: int = INVITATION_TTL_DAYS,
) -> tuple[Invitation, str]:
    """Create a pending invitation and return ``(invitation, raw_token)``."""
    email = email.lower().strip()
    member = (
        db.query(Membership)
        .join(User, Membership.user_id == User.id)
        .filter(Membership.school_id == school.id, User.email == email)
        .first()
    )
    if member is not None and member.status == MembershipStatus.ACTIVE:
        raise HTTPException(status_code=409, detail="That email already has an active membership")

    token = generate_token()
    invitation = Invitation(
        school_id=school.id,
        email=email,
        role=role,
        token_hash=hash_token(token),
        invited_by=invited_by,
        expires_at=utcnow() + timedelta(days=expires_in_days),
    )
    db.add(invitation)
    db.flush()
    return invitation, token


def invitation_response(db: OrmSession, invitation: Invitation, token: str) -> dict:
    data = _invite_out(db, invitation)
    data["invitation_token"] = token
    data["accept_uri"] = f"sacs://invitations/{invitation.id}"
    return data
