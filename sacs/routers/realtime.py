"""Authenticated WebSocket endpoint for real-time event delivery."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session as OrmSession

from ..db import get_sessionmaker
from ..deps import get_ws_auth_context
from ..events import Connection, manager
from ..models import Membership
from ..security import resolve_session

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    db = get_sessionmaker()()
    try:
        ctx = get_ws_auth_context(db, websocket)
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401)
        db.close()
        return

    role = None
    is_platform = ctx.user.is_platform_admin
    school_id = ctx.session.school_id
    if school_id is not None:
        membership = (
            db.query(Membership)
            .filter(
                Membership.user_id == ctx.user.id,
                Membership.school_id == school_id,
                Membership.status == "active",
            )
            .first()
        )
        role = membership.role if membership else None

    if school_id is not None and role is None:
        # Not an active school member: no tenant-scoped events for this socket.
        await websocket.close(code=4403)
        db.close()
        return

    conn = Connection(
        ws=websocket,
        session_id=ctx.session.id,
        user_id=ctx.user.id,
        display_name=ctx.user.display_name,
        school_id=school_id,
        role=role,
        is_platform=is_platform,
    )
    await manager.connect(conn)
    try:
        await websocket.send_json(
            {
                "type": "hello",
                "school_id": school_id,
                "role": role,
                "platform": is_platform,
            }
        )
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        await manager.disconnect(conn)
        db.close()