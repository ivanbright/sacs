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


def test_staff_first_login_must_change_password(client, identities):
    sid = school_a(identities).id
    created = client.post(
        f"/api/schools/{sid}/staff",
        headers=auth_headers(identities, "headA"),
        json={"email": "firstlogin@demo.sacs", "role": "teacher", "display_name": "First"},
    ).json()
    temp = created["generated_password"]

    login = client.post(
        "/api/auth/login",
        json={"email": "firstlogin@demo.sacs", "password": temp, "school_id": sid},
    )
    assert login.status_code == 200
    body = login.json()
    assert body["user"]["must_change_password"] is True
    H = {"Authorization": f"Bearer {body['token']}"}

    bad = client.post(
        "/api/auth/change-password",
        headers=H,
        json={"current_password": "not-the-password", "new_password": "BrandNew123!"},
    )
    assert bad.status_code == 403

    ok = client.post(
        "/api/auth/change-password",
        headers=H,
        json={"current_password": temp, "new_password": "BrandNew123!"},
    )
    assert ok.status_code == 200

    me = client.get("/api/auth/me", headers=H).json()
    assert me["user"]["must_change_password"] is False

    old = client.post(
        "/api/auth/login",
        json={"email": "firstlogin@demo.sacs", "password": temp, "school_id": sid},
    )
    assert old.status_code == 401
    new = client.post(
        "/api/auth/login",
        json={"email": "firstlogin@demo.sacs", "password": "BrandNew123!", "school_id": sid},
    )
    assert new.status_code == 200
    assert new.json()["user"]["must_change_password"] is False


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


def test_platform_lists_and_toggles_members(client, identities):
    from sacs.db import get_sessionmaker
    from sacs.models import Membership

    sid = school_a(identities).id
    r = client.get(f"/api/platform/schools/{sid}/members", headers=auth_headers(identities, "platform"))
    assert r.status_code == 200, r.text
    members = r.json()
    assert {m["role"] for m in members} >= {"school_head", "dos", "teacher", "bell_monitor"}
    assert all(m["user"]["email"] for m in members)

    victim = next(m for m in members if m["role"] == "teacher")
    deact = client.patch(
        f"/api/platform/schools/{sid}/members/{victim['id']}",
        headers=auth_headers(identities, "platform"),
        json={"status": "deactivated"},
    )
    assert deact.status_code == 200
    assert deact.json()["status"] == "deactivated"

    db = get_sessionmaker()()
    assert db.get(Membership, victim["id"]).status == "deactivated"
    db.close()

    react = client.patch(
        f"/api/platform/schools/{sid}/members/{victim['id']}",
        headers=auth_headers(identities, "platform"),
        json={"status": "active"},
    )
    assert react.status_code == 200
    assert react.json()["status"] == "active"

    bad = client.patch(
        f"/api/platform/schools/{sid}/members/{victim['id']}",
        headers=auth_headers(identities, "platform"),
        json={"status": "bogus"},
    )
    assert bad.status_code == 422


def test_platform_suspend_and_reactivate_via_platform(client, identities):
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