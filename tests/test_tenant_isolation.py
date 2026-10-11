"""Tenant isolation tests: school A data must never be visible to school B."""

from __future__ import annotations

from tests.conftest import auth_headers, school_b


def test_school_b_teacher_cannot_see_a_requests(client, identities):
    sid = identities["_a"]["school"].id
    r = client.get(
        f"/api/schools/{sid}/requests",
        headers=auth_headers(identities, "teacherB"),
    )
    assert r.status_code == 403


def test_school_b_cannot_submit_on_a(client, identities):
    sid = identities["_a"]["school"].id
    lessons = client.get(
        f"/api/schools/{sid}/timetable/lessons",
        headers=auth_headers(identities, "teacherA"),
    ).json()
    r = client.post(
        f"/api/schools/{sid}/requests",
        headers=auth_headers(identities, "teacherB"),
        json={"lesson_session_id": lessons[0]["id"]},
    )
    assert r.status_code == 403


def test_school_b_cannot_read_a_timetable(client, identities):
    sid = identities["_a"]["school"].id
    r = client.get(
        f"/api/schools/{sid}/timetable/lessons",
        headers=auth_headers(identities, "teacherB"),
    )
    assert r.status_code == 403


def test_school_b_cannot_manage_a_staff(client, identities):
    sid = identities["_a"]["school"].id
    r = client.post(
        f"/api/schools/{sid}/staff",
        headers=auth_headers(identities, "headB"),
        json={"email": "x@demo.sacs", "role": "teacher", "display_name": "X"},
    )
    assert r.status_code == 403


def test_school_b_cannot_edit_a_bell_schedule(client, identities):
    sid = identities["_a"]["school"].id
    r = client.post(
        f"/api/schools/{sid}/bell/schedules",
        headers=auth_headers(identities, "headB"),
        json={
            "name": "x",
            "timezone": "UTC",
            "periods": [{"label": "P", "start": "08:00", "end": "08:05"}],
        },
    )
    assert r.status_code == 403


def test_suspended_school_blocked(client, identities):
    from sacs.models import School
    from sacs.db import get_sessionmaker

    sa = identities["_a"]["school"]
    db = get_sessionmaker()()
    school = db.get(School, sa.id)
    school.status = "suspended"
    db.commit()
    db.close()

    sid = school_a(identities).id
    r = client.get(
        f"/api/schools/{sid}/requests",
        headers=auth_headers(identities, "teacherA"),
    )
    assert r.status_code == 403


def school_a(identities):
    return identities["_a"]["school"]