"""Authentication endpoints: login, logout, current identity."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session as OrmSession

from ..db import get_db
from ..deps import AuthContext, get_auth_context
from ..models import Membership, School, User, utcnow
from ..rbac import effective_permissions, membership_permissions
from ..security import create_session, revoke_session, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)
    school_id: str | None = None


class LoginResponse(BaseModel):
    token: str
    user: dict
    membership: dict | None
    permissions: list[str]
    is_platform: bool
    session: dict


def _serialize_membership(db: OrmSession, m: Membership) -> dict:
    school = db.get(School, m.school_id)
    return {
        "id": m.id,
        "school_id": m.school_id,
        "school_name": school.name if school else None,
        "role": m.role,
        "status": m.status,
    }


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: OrmSession = Depends(get_db)) -> LoginResponse:
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    membership = None
    if user.is_platform_admin and not payload.school_id:
        session, token = create_session(db, user=user, label="platform")
    else:
        if not payload.school_id:
            candidates = (
                db.query(Membership)
                .filter(Membership.user_id == user.id, Membership.status == "active")
                .all()
            )
            if len(candidates) == 1:
                payload.school_id = candidates[0].school_id
            else:
                raise HTTPException(
                    status_code=400,
                    detail="school_id is required when the account spans schools",
                )
        membership = (
            db.query(Membership)
            .filter(
                Membership.user_id == user.id,
                Membership.school_id == payload.school_id,
                Membership.status == "active",
            )
            .first()
        )
        if membership is None:
            raise HTTPException(status_code=403, detail="No active membership in this school")
        session, token = create_session(db, user=user, school_id=membership.school_id, label="school")

    db.commit()
    perms = effective_permissions(user, membership)
    return LoginResponse(
        token=token,
        user={"id": user.id, "display_name": user.display_name, "email": user.email, "is_demo": user.is_demo},
        membership=_serialize_membership(db, membership) if membership else None,
        permissions=sorted(perms),
        is_platform=user.is_platform_admin,
        session={
            "id": session.id,
            "school_id": session.school_id,
            "expires_at": session.expires_at.isoformat() + "Z",
        },
    )


@router.post("/logout")
def logout(
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    revoke_session(db, ctx.session)
    db.commit()
    return {"ok": True}


class MeContext(BaseModel):
    user: dict
    membership: dict | None
    permissions: list[str]
    is_platform: bool
    school: dict | None
    session_expires_at: str | None


@router.get("/me", response_model=MeContext)
def me(
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> MeContext:
    membership = None
    school = None
    if ctx.session.school_id:
        membership = (
            db.query(Membership)
            .filter(Membership.user_id == ctx.user.id, Membership.school_id == ctx.session.school_id)
            .first()
        )
        school = db.get(School, ctx.session.school_id)
    perms = effective_permissions(ctx.user, membership)
    return MeContext(
        user={
            "id": ctx.user.id,
            "display_name": ctx.user.display_name,
            "email": ctx.user.email,
            "is_demo": ctx.user.is_demo,
        },
        membership={
            "id": membership.id,
            "school_id": membership.school_id,
            "role": membership.role,
            "status": membership.status,
            "school_name": school.name if school else None,
            "school_status": school.status if school else None,
        }
        if membership
        else None,
        permissions=sorted(perms),
        is_platform=ctx.user.is_platform_admin,
        school={
            "id": school.id,
            "name": school.name,
            "slug": school.slug,
            "status": school.status,
            "timezone": school.timezone,
        }
        if school
        else None,
        session_expires_at=ctx.session.expires_at.isoformat() + "Z",
    )