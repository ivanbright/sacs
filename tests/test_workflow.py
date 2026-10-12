"""Workflow state machine tests through the public API."""

from __future__ import annotations

from tests.conftest import auth_headers, school_a

REQ_PATH = "/api/schools/{sid}/requests"


def _submit(client, identities, session_id=None, note="done"):
    sid = school_a(identities).id
    if session_id is None:
        lessons = client.get(
            f"/api/schools/{sid}/timetable/lessons",
            headers=auth_headers(identities, "teacherA"),
        )
        session_id = lessons.json()[0]["id"]
    r = client.post(
        f"{REQ_PATH.format(sid=sid)}",
        headers=auth_headers(identities, "teacherA"),
        json={"lesson_session_id": session_id, "note": note},
    )
    return r


def _transition(client, identities, req_id, action, who, reason="ok"):
    sid = school_a(identities).id
    return client.post(
        f"{REQ_PATH.format(sid=sid)}/{req_id}/transition",
        headers=auth_headers(identities, who),
        json={"action": action, "reason": reason},
    )


def test_submit_forward_approve(client, identities):
    r = _submit(client, identities)
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["state"] == "submitted"
    req_id = req["id"]

    fwd = _transition(client, identities, req_id, "forward", "dosA")
    assert fwd.json()["state"] == "forwarded_to_head"

    apr = _transition(client, identities, req_id, "approve", "headA")
    assert apr.json()["state"] == "approved"

    detail = client.get(
        f"{REQ_PATH.format(sid=school_a(identities).id)}/{req_id}",
        headers=auth_headers(identities, "dosA"),
    ).json()
    assert [a["action"] for a in detail["actions"]] == ["submit", "forward", "approve"]


def test_dos_cannot_final_approve(client, identities):
    req_id = _submit(client, identities).json()["id"]
    r = _transition(client, identities, req_id, "approve", "dosA")
    assert r.status_code == 403


def test_head_cannot_submit_new_request(client, identities):
    sid = school_a(identities).id
    lessons = client.get(
        f"/api/schools/{sid}/timetable/lessons",
        headers=auth_headers(identities, "teacherA"),
    ).json()
    r = client.post(
        f"{REQ_PATH.format(sid=sid)}",
        headers=auth_headers(identities, "headA"),
        json={"lesson_session_id": lessons[0]["id"]},
    )
    assert r.status_code == 403


def test_invalid_transition_rejected(client, identities):
    req_id = _submit(client, identities).json()["id"]
    _transition(client, identities, req_id, "forward", "dosA")
    r = _transition(client, identities, req_id, "approve", "headA")
    assert r.json()["state"] == "approved"
    again = _transition(client, identities, req_id, "reject", "headA")
    assert again.status_code in (409, 422)


def test_duplicate_submit_rejected(client, identities):
    sid = school_a(identities).id
    lessons = client.get(
        f"/api/schools/{sid}/timetable/lessons",
        headers=auth_headers(identities, "teacherA"),
    ).json()
    first = _submit(client, identities, session_id=lessons[0]["id"])
    second = _submit(client, identities, session_id=lessons[0]["id"])
    assert first.status_code == 201
    assert second.status_code == 409


def test_teacher_sees_only_own_requests(client, identities):
    sid = school_a(identities).id
    mine = _submit(client, identities, note="mine").json()["id"]
    resp = client.get(f"{REQ_PATH.format(sid=sid)}", headers=auth_headers(identities, "teacherA"))
    assert resp.status_code == 200
    mine_again = resp.json()
    assert isinstance(mine_again, list)
    assert mine_again, "teacher must see their own requests"
    assert all(r.get("id") == mine for r in mine_again), "teacher must see only their own requests"
    r = client.get(
        f"{REQ_PATH.format(sid=sid)}/{mine}",
        headers=auth_headers(identities, "dosA"),
    )
    assert r.status_code == 200


def test_dos_returns_and_teacher_resubmits(client, identities):
    sid = school_a(identities).id
    req_id = _submit(client, identities).json()["id"]
    ret = _transition(client, identities, req_id, "return", "dosA", reason="please fix the note")
    assert ret.status_code == 200, ret.text
    assert ret.json()["state"] == "returned_for_correction"

    resub = _transition(client, identities, req_id, "submit", "teacherA")
    assert resub.status_code == 200, resub.text
    assert resub.json()["state"] == "submitted"

    detail = client.get(
        f"{REQ_PATH.format(sid=sid)}/{req_id}",
        headers=auth_headers(identities, "teacherA"),
    ).json()
    assert [a["action"] for a in detail["actions"]] == ["submit", "return", "submit"]


def test_bell_monitor_cannot_view_requests(client, identities):
    sid = school_a(identities).id
    r = client.get(f"{REQ_PATH.format(sid=sid)}", headers=auth_headers(identities, "bellA"))
    assert r.status_code == 403