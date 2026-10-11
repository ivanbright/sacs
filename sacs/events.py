"""Real-time delivery and the transactional outbox.

Every interesting state change is recorded in the ``outbox`` table *in the same
database transaction* as the change itself. After commit, a dispatcher drains
the outbox and streams each event over authenticated WebSockets to exactly the
recipients allowed server-side for that event. This guarantees that a persisted
change never silently loses its notification, and gives tests a synchronous
delivery path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .models import Outbox, Notification, utcnow

logger = logging.getLogger("sacs.events")


class Topic:
    SCHOOLS = "platform.schools"
    HEALTH = "platform.health"
    STAFF = "school.staff"
    ACADEMICS = "school.academics"
    TIMETABLE = "school.timetable"
    REQUESTS = "school.requests"
    NOTIFICATIONS = "school.notifications"
    BELL = "school.bell"


# --------------------------------------------------------------------------- #
# Connection manager
# --------------------------------------------------------------------------- #

@dataclass
class Connection:
    ws: object
    session_id: str
    user_id: str
    display_name: str
    school_id: str | None
    role: str | None
    is_platform: bool

    def audience_matches(self, payload: dict) -> bool:
        audience = payload.get("audience", "all")
        if audience is None or audience == "all":
            return True
        if isinstance(audience, str):
            if audience == "platform":
                return self.is_platform
            if audience == "school":
                return self.school_id is not None
            return audience == self.role
        if isinstance(audience, dict):
            if self.user_id in (audience.get("users") or []):
                return True
            if self.role in (audience.get("roles") or []):
                return True
        return False


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: list[Connection] = []
        self._lock = asyncio.Lock()

    async def connect(self, conn: Connection) -> None:
        async with self._lock:
            self._connections.append(conn)

    async def disconnect(self, conn: Connection) -> None:
        async with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)

    def _candidates(self, payload: dict) -> list[Connection]:
        if payload.get("audience") == "platform":
            return [c for c in self._connections if c.is_platform]
        school_id = payload.get("school_id")
        if school_id is None:
            return []
        return [c for c in self._connections if c.school_id == school_id]

    async def deliver(self, topic: str, payload: dict) -> int:
        sent = 0
        for conn in self._candidates(payload):
            if not conn.audience_matches(payload):
                continue
            message = {
                "type": topic,
                "school_id": payload.get("school_id"),
                "ts": utcnow().isoformat() + "Z",
                "payload": payload.get("data", payload),
            }
            try:
                await conn.ws.send_json(message)
                sent += 1
            except Exception:  # noqa: BLE001 - dead socket must not break others
                logger.warning("real-time delivery failed to user=%s", conn.user_id)
        return sent


manager = ConnectionManager()


# --------------------------------------------------------------------------- #
# Outbox
# --------------------------------------------------------------------------- #

def enqueue(db: OrmSession, topic: str, payload: dict) -> str:
    """Insert an outbox record in the *current* transaction."""
    entry = Outbox(topic=topic, school_id=payload.get("school_id"), payload=payload)
    db.add(entry)
    db.flush()
    return entry.id


async def _deliver_pending(pending: list[Outbox]) -> int:
    delivered = 0
    for entry in pending:
        try:
            delivered += await manager.deliver(entry.topic, entry.payload)
        except Exception:  # noqa: BLE001
            logger.exception("outbox delivery failed topic=%s", entry.topic)
    return delivered


def _run_in_new_loop(coro) -> int:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def dispatch_pending(db: OrmSession, entries: list[Outbox] | None = None) -> int:
    """Deliver pending outbox records and mark them processed.

    When called from a sync route handler (no running loop) a short-lived event
    loop is created; inside the async scheduler it uses ``create_task``.
    """
    if entries is None:
        entries = (
            db.execute(
                select(Outbox).where(Outbox.processed_at.is_(None)).order_by(Outbox.created_at)
            )
            .scalars()
            .all()
        )
    if not entries:
        return 0

    running = None
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    now = utcnow()
    for entry in entries:
        entry.processed_at = now
    db.commit()

    if running is not None:
        running.create_task(_deliver_pending(entries))
        return len(entries)
    return _run_in_new_loop(_deliver_pending(entries))


async def background_dispatcher(session_factory) -> None:
    """Periodically drain the outbox so recovery happens even if a commit
    happened without a live dispatcher (e.g. process restart)."""
    while True:
        try:
            session = session_factory()
            try:
                pending = (
                    session.execute(
                        select(Outbox).where(Outbox.processed_at.is_(None)).order_by(Outbox.created_at)
                    )
                    .scalars()
                    .all()
                )
                if pending:
                    await _deliver_pending(pending)
                    now = utcnow()
                    for entry in pending:
                        entry.processed_at = now
                    session.commit()
            finally:
                session.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("background dispatcher error")
        await asyncio.sleep(1.0)


# --------------------------------------------------------------------------- #
# Notifications (created in the same transaction as the change they describe)
# --------------------------------------------------------------------------- #

def notify(
    db: OrmSession,
    *,
    school_id: str | None,
    user_id: str | None = None,
    audience_role: str | None = None,
    type: str,
    title: str,
    body: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> None:
    db.add(
        Notification(
            school_id=school_id,
            user_id=user_id,
            audience_role=audience_role,
            type=type,
            title=title,
            body=body,
            entity_type=entity_type,
            entity_id=entity_id,
        )
    )