"""The interactive terminal application: in-process API client and menu driving."""

from __future__ import annotations

import io

from sacs.api_client import ApiClient, ApiError
from sacs.app_menu import TerminalApp
from sacs.seed import PLATFORM_EMAIL
from sacs.ui import NAV_EXIT, Console, Style

PASSWORD = "SacsDemo123!"


def run_app(inputs):
    """Drive the terminal app with a scripted list of inputs; return (result, output)."""
    stream = io.StringIO()
    queue = list(inputs)

    def input_fn(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    console = Console(stream=stream, style=Style(False), input_fn=input_fn)
    client = ApiClient()
    app = TerminalApp(console=console, client=client)
    try:
        from sacs.ui import Navigator

        result = Navigator(console).run(app.login_menu)
    finally:
        client.close()
    return result, stream.getvalue()


def test_api_client_login_me_logout(env):
    with ApiClient() as api:
        data = api.login(PLATFORM_EMAIL, PASSWORD)
        assert data["token"]
        assert api.token == data["token"]
        me = api.me()
        assert me["is_platform"] is True
        api.logout()
        assert api.token is None


def test_api_client_bad_login_raises(env):
    with ApiClient() as api:
        try:
            api.login(PLATFORM_EMAIL, "wrong-password")
        except ApiError as exc:
            assert exc.status_code == 401
        else:  # pragma: no cover
            raise AssertionError("expected ApiError")


def test_platform_login_and_browse_schools(env):
    result, out = run_app(["2", PLATFORM_EMAIL, PASSWORD, "1", "1", "0", "0", "0"])
    assert result == NAV_EXIT
    assert "Platform Administrator" in out
    assert "Manage Schools" in out
    assert "Schools" in out


def test_unauthenticated_login_failure_is_reported(env):
    result, out = run_app(["2", PLATFORM_EMAIL, "nope", "0", "0"])
    assert result == NAV_EXIT
    assert "Invalid credentials" in out


def test_first_login_forces_password_change(env):
    school = env["schools"][0]
    head_email = school["head_user"].email
    sid = school["school"].id
    with ApiClient() as api:
        api.login(head_email, PASSWORD, sid)
        created = api.post(
            f"/api/schools/{sid}/staff",
            json={"email": "mustchange@demo.sacs", "role": "teacher", "display_name": "Must Change"},
        )
        temp = created["generated_password"]

    # 3 Teacher -> email -> temp password -> forced change (current/new/confirm)
    # -> Teacher menu -> logout -> exit
    result, out = run_app(
        ["3", "mustchange@demo.sacs", temp, temp, "BrandNew123!", "BrandNew123!", "0", "0"]
    )
    assert result == NAV_EXIT
    assert "Password changed" in out


def test_accept_token_then_login_lands_in_director_menu(env):
    from sacs.seed import PLATFORM_EMAIL

    with ApiClient() as api:
        api.login(PLATFORM_EMAIL, PASSWORD)
        school = api.post(
            "/api/platform/schools",
            json={
                "name": "Director Campus",
                "slug": "director-campus",
                "timezone": "UTC",
                "head_email": "head.director@demo.sacs",
                "head_name": "Director Head",
            },
        )
        token = school["head_invitation_token"]

    # 1 Accept invitation with token -> token / password / name
    # -> auto-signed-in Director menu -> logout -> exit
    result, out = run_app(["1", token, "DirectorPass123!", "Director Head", "0", "0"])
    assert result == NAV_EXIT
    assert "Invitation accepted" in out
    assert "Director" in out


def test_teacher_can_submit_a_lesson(env):
    teacher_email = env["schools"][0]["teacher_user"].email
    # 3 Teacher -> email/password -> 3 Submit -> 1 first lesson -> blank note -> back/out
    result, out = run_app(["3", teacher_email, PASSWORD, "3", "1", "", "0", "0", "0"])
    assert result == NAV_EXIT
    assert "Teacher" in out
    assert "Submitted" in out
