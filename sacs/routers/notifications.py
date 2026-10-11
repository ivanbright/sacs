"""Persistent notification inbox.

Notifications are written by the business layer (``events.notify``) in the same
transaction as the change they describe. This router exposes the recipient's
own inbox: list, unread count, open one, mark one/all read. A user may only see
and mutate notifications addressed to them; there is no ``?user_id=`` parameter
so the inbox cannot be used to read another account's notifications.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as OrmSession

from ..db import get_db
from ..deps import AuthContext, get_auth_context
from ..models import Notification

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _serialize(n: Notification) -> dict:
    return {
        "id": n.id,
        "school_id": n.school_id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "entity_type": n.entity_type,
        "entity_id": n.entity_id,
        "read": n.read,
        "created_at": n.created_at.isoformat() + "Z",
    }


def _mine(db: OrmSession, ctx: AuthContext):
    return db.query(Notification).filter(Notification.user_id == ctx.user.id)


@router.get("")
def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    q = _mine(db, ctx)
    if unread_only:
        q = q.filter(Notification.read.is_(False))
    rows = q.order_by(Notification.created_at.desc()).limit(min(max(limit, 1), 200)).all()
    return [_serialize(n) for n in rows]


@router.get("/unread-count")
def unread_count(
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    count = _mine(db, ctx).filter(Notification.read.is_(False)).count()
    return {"count": count}


@router.get("/{notification_id}")
def notification_detail(
    notification_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    note = db.get(Notification, notification_id)
    if note is None or note.user_id != ctx.user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    return _serialize(note)


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    note = db.get(Notification, notification_id)
    if note is None or note.user_id != ctx.user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not note.read:
        note.read = True
        db.commit()
    return _serialize(note)


@router.post("/read-all")
def mark_all_read(
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    updated = (
        _mine(db, ctx)
        .filter(Notification.read.is_(False))
        .update({Notification.read: True}, synchronize_session=False)
    )
    db.commit()
    return {"updated": updated}
