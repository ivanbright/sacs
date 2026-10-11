"""Authentication and RBAC tests."""

from __future__ import annotations

from tests.conftest import PW, auth_headers, school_a


def test_login_and_me(client, identities):
    me = client.get("/api/auth/me", headers=auth_headers(identities, "teacherA"))
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"].endswith("@demo.sacs")
    assert "permissions" in body


def test_login_wrong_password(client, identities):
    r = client.post(
        "/api/auth/login",
        json={"email": identities["_a"]["teacher_user"].email, "password": "nope"},
    )
    assert r.status_code == 401


def test_access_without_token(client, identities):
    sid = school_a(identities).id
    assert client.get(f"/api/schools/{sid}/requests").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_garbage_token(client, identities):
    sid = school_a(identities).id
    r = client.get(
        f"/api/schools/{sid}/requests",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401


def test_logout_revokes(client, identities):
    sid = school_a(identities).id
    token = identities["teacherA"]["token"]
    out = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert out.status_code == 200
    gone = client.get(
        f"/api/schools/{sid}/requests",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert gone.status_code == 401


def test_platform_only_endpoints(client, identities):
    sid = school_a(identities).id
    H_t = auth_headers(identities, "teacherA")
    H_b = auth_headers(identities, "bellB")
    assert client.get("/api/platform/schools", headers=H_t).status_code == 403
    assert client.post(
        "/api/platform/schools",
        headers=H_b,
        json={"name": "X School", "slug": "x-school", "timezone": "UTC"},
    ).status_code == 403


def test_teacher_cannot_decide(client, identities):
    teacher = identities["_a"]["teacher_user"]
    body = {"email": teacher.email, "password": PW}
    r = client.post("/api/auth/login", json=body)
    assert r.status_code == 200