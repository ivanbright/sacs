"""WebSocket authentication and outbox draining."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from tests.conftest import auth_headers, school_a


def test_ws_hello_and_ping(client, identities):
    token = identities["teacherA"]["token"]
    with client.websocket_connect(f"/ws?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["role"] == "teacher"
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}


def test_ws_rejects_bad_token(client, identities):
    """Server closes a socket whose token does not resolve to a session."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=bogus-token") as ws:
            ws.send_text("ping")
            ws.receive_json()


def test_ws_platform_joins_platform_workspace(client, identities):
    """Platform may connect, but only as the platform workspace, never as a tenant."""
    token = identities["platform"]["token"]
    with client.websocket_connect(f"/ws?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["platform"] is True
        assert hello["role"] is None
        assert hello["school_id"] is None


def test_outbox_drain(client, identities, factory):
    from sacs.models import Outbox

    sid = school_a(identities).id
    client.post(
        f"/api/schools/{sid}/bell/schedules",
        headers=auth_headers(identities, "headA"),
        json={
            "name": "Outbox",
            "timezone": "UTC",
            "periods": [{"label": "P", "start": "09:00", "end": "09:45"}],
        },
    )

    db = factory()
    pending_id = db.execute(
        select(Outbox.id).where(Outbox.processed_at.is_(None)).order_by(Outbox.created_at.desc())
    ).scalars().first()
    assert pending_id is not None

    from sacs import events

    events.dispatch_pending(db)

    remaining = db.execute(
        select(func.count(Outbox.id)).where(Outbox.processed_at.is_(None))
    ).scalar()
    db.close()
    assert remaining == 0