"""Notification inbox: business events create notifications and users can read them."""

from __future__ import annotations

from tests.conftest import auth_headers, school_a


def _submit_as_teacher(client, identities):
    sid = school_a(identities).id
    lessons = client.get(
        f"/api/schools/{sid}/timetable/lessons", headers=auth_headers(identities, "teacherA")
    ).json()
    r = client.post(
        f"/api/schools/{sid}/requests",
        headers=auth_headers(identities, "teacherA"),
        json={"lesson_session_id": lessons[0]["id"]},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_request_submission_notifies_reviewers(client, identities):
    _submit_as_teacher(client, identities)
    inbox = client.get("/api/notifications", headers=auth_headers(identities, "headA")).json()
    assert any(n["type"] == "request.submitted" for n in inbox)
    unread = client.get(
        "/api/notifications/unread-count", headers=auth_headers(identities, "headA")
    ).json()["count"]
    assert unread >= 1


def test_mark_one_and_mark_all(client, identities):
    _submit_as_teacher(client, identities)
    inbox = client.get("/api/notifications", headers=auth_headers(identities, "headA")).json()
    nid = inbox[0]["id"]

    one = client.post(f"/api/notifications/{nid}/read", headers=auth_headers(identities, "headA"))
    assert one.status_code == 200 and one.json()["read"] is True

    allr = client.post("/api/notifications/read-all", headers=auth_headers(identities, "headA"))
    assert allr.status_code == 200
    assert (
        client.get(
            "/api/notifications/unread-count", headers=auth_headers(identities, "headA")
        ).json()["count"]
        == 0
    )


def test_cannot_read_another_users_notification(client, identities):
    _submit_as_teacher(client, identities)
    nid = client.get("/api/notifications", headers=auth_headers(identities, "headA")).json()[0]["id"]
    assert (
        client.post(
            f"/api/notifications/{nid}/read", headers=auth_headers(identities, "teacherA")
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/notifications/{nid}", headers=auth_headers(identities, "teacherA")
        ).status_code
        == 404
    )


def test_invitation_acceptance_notifies_inviter(client, identities, factory):
    from sacs.models import Notification

    sid = school_a(identities).id
    invite = client.post(
        f"/api/schools/{sid}/invitations",
        headers=auth_headers(identities, "headA"),
        json={"email": "newteacher@demo.sacs", "role": "teacher"},
    )
    assert invite.status_code == 201, invite.text

    login = client.post(
        "/api/auth/login",
        json={"email": "newteacher@demo.sacs", "password": "SacsDemo123!"},
    )
    assert login.status_code == 200, login.text
    tok = login.json()["token"]
    acc = client.post(
        "/api/invitations/accept",
        headers={"Authorization": f"Bearer {tok}"},
        json={"invitation_id": invite.json()["id"]},
    )
    assert acc.status_code == 200, acc.text

    db = factory()
    try:
        head_id = identities["headA"]["user"]["id"]
        rows = db.query(Notification).filter(Notification.user_id == head_id).all()
        assert any(n.type == "invitation.accepted" for n in rows)
    finally:
        db.close()
