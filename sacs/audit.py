"""Audit event recording.

Audit events are written inside the same transaction as the change they
describe, so a committed business action always carries an auditable record.
School-scoped records carry a ``school_id``; platform-level records do not.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .models import AuditEvent, utcnow


def record(
    db: OrmSession,
    *,
    school_id: str | None,
    actor_user_id: str | None,
    actor_role: str | None,
    action: str,
    summary: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    meta: dict | None = None,
) -> str:
    event = AuditEvent(
        school_id=school_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        action=action,
        summary=summary,
        entity_type=entity_type,
        entity_id=entity_id,
        meta=meta or {},
    )
    db.add(event)
    db.flush()
    return event.id


def list_events(
    db: OrmSession,
    *,
    school_id: str | None = None,
    platform: bool = False,
    limit: int = 100,
) -> list[AuditEvent]:
    stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc())
    if platform:
        stmt = stmt.where(AuditEvent.school_id.is_(None))
    elif school_id is not None:
        stmt = stmt.where(AuditEvent.school_id == school_id)
    return list(db.execute(stmt.limit(limit)).scalars().all() or [])