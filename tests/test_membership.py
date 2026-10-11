"""Membership, staff, and platform lifecycle tests."""

from __future__ import annotations

from sqlalchemy import select

from tests.conftest import PW, auth_headers, school_a


def test_platform_invites_and_head_accepts(client, identities):
    r = client.post(
        "/api/platform/schools",
        headers=auth_headers(identities, "platform"),
        json={
            "name": "Third Campus",
            "slug": "third-campus",
            "timezone": "Asia/Tokyo",
            "head_email": "head3@demo.sacs",
            "head_name": "Third Head",
        },
    )
    assert r.status_code == 201, r.text
    third = r.json()
    assert third["status"] == "provisioning"
    inv = third["head_invitation_token"]

    r = client.post(
        "/api/invitations/accept-token",
        json={"token": inv, "password": PW, "display_name": "Third Head"},
    )
    assert r.status_code == 200, r.text

    assert client.post(
        "/api/invitations/accept-token",
        json={"token": inv, "password": PW, "display_name": "Again"},
    ).status_code == 409

    login = client.post(
        "/api/auth/login",
        json={"email": "head3@demo.sacs", "password": PW, "school_id": third["id"]},
    )
    assert login.status_code == 200


def test_unknown_invitation_token(client, identities):
    r = client.post(
        "/api/invitations/accept-token",
        json={"token": "definitely-not-real", "password": PW, "display_name": "X"},
    )
    assert r.status_code == 404


def test_staff_create_reactivate_cycle(client, identities):
    sid = school_a(identities).id
    created = client.post(
        f"/api/schools/{sid}/staff",
        headers=auth_headers(identities, "headA"),
        json={"email": "cycling@demo.sacs", "role": "teacher", "display_name": "Cycle"},
    )
    assert created.status_code == 201, created.text

    membership_id = created.json()["id"]
    deact = client.patch(
        f"/api/schools/{sid}/staff/{membership_id}",
        headers=auth_headers(identities, "headA"),
        json={"status": "deactivated"},
    )
    assert deact.status_code == 200
    assert deact.json()["status"] == "deactivated"

    react = client.patch(
        f"/api/schools/{sid}/staff/{membership_id}",
        headers=auth_headers(identities, "headA"),
        json={"status": "active"},
    )
    assert react.status_code == 200
    assert react.json()["status"] == "active"


def test_deactivated_staff_cannot_login(client, identities):
    from sacs.models import Membership

    sid = school_a(identities).id
    created = client.post(
        f"/api/schools/{sid}/staff",
        headers=auth_headers(identities, "headA"),
        json={"email": "temp@demo.sacs", "role": "teacher", "display_name": "Temp"},
    ).json()
    client.patch(
        f"/api/schools/{sid}/staff/{created['id']}",
        headers=auth_headers(identities, "headA"),
        json={"status": "deactivated"},
    )

    login = client.post(
        "/api/auth/login",
        json={"email": "temp@demo.sacs", "password": "WrongPass123!", "school_id": sid},
    )
    assert login.status_code == 401


def test_suspend_and_reactivate_via_platform(client, identities):
    from sacs.db import get_sessionmaker
    from sacs.models import School

    sid = school_a(identities).id
    r = client.post(f"/api/platform/schools/{sid}/suspend", headers=auth_headers(identities, "platform"))
    assert r.status_code == 200, r.text

    db = get_sessionmaker()()
    school = db.get(School, sid)
    assert school.status == "suspended"
    db.close()

    r2 = client.post(f"/api/platform/schools/{sid}/reactivate", headers=auth_headers(identities, "platform"))
    assert r2.status_code == 200

    db = get_sessionmaker()()
    assert db.get(School, sid).status == "active"
    db.close()


def test_suspended_school_full_flow(client, identities):
    """Suspension by platform revokes every live membership's access."""
    from sacs.db import get_sessionmaker
    from sacs.models import Membership, School

    sid = school_a(identities).id
    client.post(f"/api/platform/schools/{sid}/suspend", headers=auth_headers(identities, "platform"))

    for who in ("teacherA", "headA", "dosA", "bellA"):
        r = client.get(f"/api/schools/{sid}/profile", headers=auth_headers(identities, who))
        assert r.status_code == 403, (who, r.text)

    db = get_sessionmaker()()
    school = db.get(School, sid)
    assert school.status == "suspended"
    assert db.execute(select(Membership.id).where(Membership.school_id == sid)).scalars().all()  # rows kept
    db.close()