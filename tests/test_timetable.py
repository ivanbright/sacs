"""Timetable drafting, conflict detection, and publishing visibility rules."""

from __future__ import annotations

from tests.conftest import auth_headers, school_a


def _refs(client, sid, head):
    classes = client.get(
        f"/api/schools/{sid}/academics/classes", headers=head
    ).json()
    subjects = client.get(
        f"/api/schools/{sid}/academics/subjects", headers=head
    ).json()
    classrooms = client.get(
        f"/api/schools/{sid}/academics/classrooms", headers=head
    ).json()
    staff = client.get(f"/api/schools/{sid}/staff", headers=head).json()
    teacher = next(m for m in staff if m["role"] == "teacher")
    return classes[0]["id"], subjects[0]["id"], classrooms[0]["id"], teacher["id"]


def _make_entry(client, sid, head, refs, *, day, start, end):
    class_id, subject_id, classroom_id, teacher_id = refs
    return client.post(
        f"/api/schools/{sid}/timetable",
        headers=head,
        json={
            "class_id": class_id,
            "subject_id": subject_id,
            "classroom_id": classroom_id,
            "teacher_membership_id": teacher_id,
            "day_of_week": day,
            "start_time": start,
            "end_time": end,
        },
    )


def test_new_entry_is_a_draft_and_conflicts_are_rejected(client, identities):
    sid = school_a(identities).id
    head = auth_headers(identities, "headA")
    refs = _refs(client, sid, head)

    first = _make_entry(client, sid, head, refs, day=2, start="11:00", end="11:50")
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "draft"

    clash = _make_entry(client, sid, head, refs, day=2, start="11:30", end="12:20")
    assert clash.status_code == 409, clash.text
    assert "conflict" in str(clash.json()["detail"]).lower()

    other_day = _make_entry(client, sid, head, refs, day=3, start="11:00", end="11:50")
    assert other_day.status_code == 201, other_day.text


def test_teachers_do_not_see_draft_entries(client, identities):
    sid = school_a(identities).id
    head = auth_headers(identities, "headA")
    teacher = auth_headers(identities, "teacherA")
    refs = _refs(client, sid, head)

    created = _make_entry(client, sid, head, refs, day=1, start="13:00", end="13:50")
    assert created.status_code == 201, created.text

    head_view = client.get(f"/api/schools/{sid}/timetable", headers=head).json()
    assert any(e["status"] == "draft" for e in head_view)

    teacher_view = client.get(f"/api/schools/{sid}/timetable", headers=teacher).json()
    assert all(e["status"] == "published" for e in teacher_view)


def test_publish_promotes_drafts_and_creates_sessions(client, identities):
    sid = school_a(identities).id
    head = auth_headers(identities, "headA")
    refs = _refs(client, sid, head)

    created = _make_entry(client, sid, head, refs, day=4, start="14:00", end="14:50")
    assert created.status_code == 201, created.text

    published = client.post(f"/api/schools/{sid}/timetable/publish", headers=head)
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["published_entries"] >= 1
    assert body["sessions_created"] >= 1

    teacher_view = client.get(
        f"/api/schools/{sid}/timetable",
        headers=auth_headers(identities, "teacherA"),
    ).json()
    assert teacher_view and all(e["status"] == "published" for e in teacher_view)
