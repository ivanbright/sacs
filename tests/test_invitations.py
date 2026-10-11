"""End-to-end invitation lifecycle: Admin -> Director -> Teacher.

These tests exercise the *real* persisted lifecycle across identities, not the
CLI. They require that invited identities can authenticate before they hold any
membership (a "pending" session) and that acceptance is bound to the recipient.
"""

from __future__ import annotations

from tests.conftest import PW, auth_headers

CANDIDATE_DIRECTOR = "newdirector@demo.sacs"
CANDIDATE_TEACHER = "newteacher@demo.sacs"


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login(client, email, school_id=None):
    r = client.post("/api/auth/login", json={"email": email, "password": PW, "school_id": school_id})
    assert r.status_code == 200, r.text
    return r.json()


def _create_school(client, identities, name, slug):
    r = client.post(
        "/api/platform/schools",
        headers=auth_headers(identities, "platform"),
        json={"name": name, "slug": slug, "timezone": "UTC"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _pending_id(client, token, email) -> str:
    rows = client.get("/api/invitations/mine", headers=_h(token)).json()
    matches = [i for i in rows if i["email"] == email and i["status"] == "pending"]
    assert matches, f"no pending invitation for {email}: {rows}"
    return matches[0]["id"]


def test_full_admin_director_teacher_lifecycle(client, identities):
    # 1. Admin creates a school (no head yet) -> provisioning.
    school = _create_school(client, identities, "Beta Academy", "beta-academy")
    sid = school["id"]
    assert school["status"] == "provisioning"

    # 2. Admin invites a Director.
    inv = client.post(
        f"/api/platform/schools/{sid}/invitations",
        headers=auth_headers(identities, "platform"),
        json={"email": CANDIDATE_DIRECTOR},
    )
    assert inv.status_code == 201, inv.text
    invitation = inv.json()
    assert invitation["role"] == "school_head"
    assert invitation["status"] == "pending"
    assert invitation["id"] and invitation["invitation_token"]

    # 3. The invited Director can sign in *before* any membership exists.
    director = _login(client, CANDIDATE_DIRECTOR)
    assert director["membership"] is None
    inv_id = _pending_id(client, director["token"], CANDIDATE_DIRECTOR)
    assert invitation["id"] == _pending_id(client, director["token"], CANDIDATE_DIRECTOR)

    # 4. The Director accepts, establishing a real membership in one transaction.
    acc = client.post(
        "/api/invitations/accept",
        headers=_h(director["token"]),
        json={"invitation_id": inv_id},
    )
    assert acc.status_code == 200, acc.text
    assert acc.json()["role"] == "school_head"

    # 5. School is now active; the Director logs in with school context.
    sid_beta = acc.json()["school_id"]
    director = _login(client, CANDIDATE_DIRECTOR, sid_beta)
    assert director["membership"]["role"] == "school_head"

    # 6. The Director invites a Teacher.
    inv = client.post(
        f"/api/schools/{sid_beta}/invitations",
        headers=_h(director["token"]),
        json={"email": CANDIDATE_TEACHER, "role": "teacher"},
    )
    assert inv.status_code == 201, inv.text

    # 7. The Teacher signs in (pending), inspects, and accepts.
    teacher = _login(client, CANDIDATE_TEACHER)
    tid = _pending_id(client, teacher["token"], CANDIDATE_TEACHER)
    acc = client.post(
        "/api/invitations/accept",
        headers=_h(teacher["token"]),
        json={"invitation_id": tid},
    )
    assert acc.status_code == 200, acc.text

    # 8. Both sender and recipient independently verify the membership.
    teacher = _login(client, CANDIDATE_TEACHER, sid_beta)
    assert teacher["membership"]["role"] == "teacher"
    staff = client.get(f"/api/schools/{sid_beta}/staff", headers=_h(director["token"])).json()
    emails = {m["user"]["email"] for m in staff}
    assert {CANDIDATE_DIRECTOR, CANDIDATE_TEACHER} <= emails


def test_accept_requires_matching_recipient(client, identities):
    school = _create_school(client, identities, "Gamma", "gamma")
    sid = school["id"]
    inv = client.post(
        f"/api/platform/schools/{sid}/invitations",
        headers=auth_headers(identities, "platform"),
        json={"email": CANDIDATE_DIRECTOR},
    ).json()
    r = client.post(
        "/api/invitations/accept",
        headers=auth_headers(identities, "platform"),
        json={"invitation_id": inv["id"]},
    )
    assert r.status_code == 403, r.text


def test_duplicate_and_repeated_acceptance(client, identities):
    school = _create_school(client, identities, "Delta", "delta")
    sid = school["id"]
    created = client.post(
        f"/api/platform/schools/{sid}/invitations",
        headers=auth_headers(identities, "platform"),
        json={"email": CANDIDATE_DIRECTOR},
    ).json()

    director = _login(client, CANDIDATE_DIRECTOR)
    first = client.post(
        "/api/invitations/accept",
        headers=_h(director["token"]),
        json={"invitation_id": created["id"]},
    )
    assert first.status_code == 200, first.text

    # Inviting the now-active member again is rejected.
    again = client.post(
        f"/api/platform/schools/{sid}/invitations",
        headers=auth_headers(identities, "platform"),
        json={"email": CANDIDATE_DIRECTOR},
    )
    assert again.status_code == 409

    # Accepting the same invitation twice is rejected.
    director = _login(client, CANDIDATE_DIRECTOR, sid)
    twice = client.post(
        "/api/invitations/accept",
        headers=_h(director["token"]),
        json={"invitation_id": created["id"]},
    )
    assert twice.status_code == 409


def test_director_revokes_invitation(client, identities):
    school = _create_school(client, identities, "Epsilon", "epsilon")
    sid = school["id"]
    # Establish a Director, then invite and revoke a teacher invitation.
    inv = client.post(
        f"/api/platform/schools/{sid}/invitations",
        headers=auth_headers(identities, "platform"),
        json={"email": CANDIDATE_DIRECTOR},
    ).json()
    director = _login(client, CANDIDATE_DIRECTOR)
    client.post("/api/invitations/accept", headers=_h(director["token"]), json={"invitation_id": inv["id"]})
    director = _login(client, CANDIDATE_DIRECTOR, sid)

    invite = client.post(
        f"/api/schools/{sid}/invitations",
        headers=_h(director["token"]),
        json={"email": CANDIDATE_TEACHER, "role": "teacher"},
    ).json()
    revoke = client.post(
        f"/api/schools/{sid}/invitations/{invite['id']}/revoke",
        headers=_h(director["token"]),
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["status"] == "revoked"


def test_unknown_invitation_is_404(client, identities):
    r = client.post(
        "/api/invitations/accept",
        headers=auth_headers(identities, "platform"),
        json={"invitation_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert r.status_code == 404
