"""Shared FastAPI dependencies: session resolve, platform and school guards."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, WebSocket
from sqlalchemy.orm import Session as OrmSession

from .db import get_db
from .models import Membership, School, Session as AuthSession, User, utcnow
from .rbac import Perm, has_permission
from .security import resolve_session


def extract_token(request: Request | WebSocket) -> str:
    auth = None
    if isinstance(request, WebSocket):
        auth = request.query_params.get("token")
    else:
        header = request.headers.get("Authorization")
        if header and header.lower().startswith("bearer "):
            auth = header[7:]
        if not auth:
            auth = request.query_params.get("token")
    return (auth or "").strip()


@dataclass
class AuthContext:
    session: AuthSession
    user: User

    @property
    def user_id(self) -> str:
        return self.user.id


def get_auth_context(
    request: Request,
    db: OrmSession = Depends(get_db),
) -> AuthContext:
    token = extract_token(request)
    auth_session = resolve_session(db, token)
    if auth_session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    user = db.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account unavailable")
    return AuthContext(session=auth_session, user=user)


def get_ws_auth_context(db: OrmSession, websocket: WebSocket) -> AuthContext:
    token = extract_token(websocket)
    auth_session = resolve_session(db, token)
    if auth_session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    user = db.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account unavailable")
    return AuthContext(session=auth_session, user=user)


def require_platform(ctx: AuthContext, db: OrmSession, permission: str) -> None:
    if not ctx.user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrator role required")
    if not has_permission(ctx.user, None, permission):
        raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")


def active_membership(db: OrmSession, user: User, school_id: str) -> Membership:
    membership = (
        db.query(Membership)
        .filter(Membership.user_id == user.id, Membership.school_id == school_id)
        .first()
    )
    return membership


def require_membership(
    ctx: AuthContext, db: OrmSession, school_id: str, permission: str
) -> Membership:
    """Server-side tenant + role guard for every school-scoped operation."""
    membership = active_membership(db, ctx.user, school_id)
    if membership is None:
        raise HTTPException(status_code=403, detail="No active membership in this school")
    if not has_permission(ctx.user, membership, permission):
        raise HTTPException(
            status_code=403,
            detail=f"Role '{membership.role}' lacks permission '{permission}'",
        )
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="School not found")
    if school.status == "suspended":
        raise HTTPException(status_code=403, detail="School is suspended")
    return membership


def require_school(ctx: AuthContext, db: OrmSession, school_id: str, permission: str = Perm.SCHOOL_VIEW) -> Membership:
    return require_membership(ctx, db, school_id, permission)