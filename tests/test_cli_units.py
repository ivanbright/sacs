"""Unit tests for the terminal demo's pure, server-independent logic.

These cover the parts of ``sacs demo`` that do not require a running HTTP
service: the contextual prompt, the robust dashboard (one failing panel must
not blank the whole view), command dispatch, help, and identity listing.
The subprocess smoke tests live in ``test_cli.py``.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from sacs.cli import (
    DemoSession,
    _dashboard_layout,
    _examples,
    _handle,
    _help,
    _session_cmd,
    _whoami,
)


def make_console() -> Console:
    return Console(file=StringIO(), force_terminal=False, width=100)


class FakeSchool:
    def __init__(self, sid: str, slug: str) -> None:
        self.id = sid
        self.slug = slug


class FakeSession(DemoSession):
    """A DemoSession whose HTTP layer always fails, to prove resilience."""

    def __init__(self, fail: bool = True):
        self.base = "http://localhost:0"
        self.console = make_console()
        self.tokens = {"platform": "x", "head": "x", "dos": "x", "teacher": "x", "bell": "x"}
        self.details = {
            "platform": {
                "user": {"email": "platform@demo.sacs", "display_name": "Platform"},
                "membership": None,
                "is_platform": True,
            }
        }
        self.current = "platform"
        self.lesson_list = []
        self.request_list = []
        self.env = {"schools": [{"school": FakeSchool("school-1", "northstar-academy")}]}
        self._fail = fail

    def j(self, key, path, **kwargs):
        if self._fail:
            raise RuntimeError("connection refused")
        return super().j(key, path, **kwargs)

    def cur_h(self, path, method="GET", **kwargs):
        raise RuntimeError("connection refused")


def test_prompt_reflects_identity_and_school():
    s = FakeSession()
    assert s.prompt() == "sacs[admin]> "
    s.current = "teacher@demo.sacs"
    s.details["teacher@demo.sacs"] = {
        "user": {"email": "teacher@demo.sacs"},
        "membership": {"role": "teacher", "school_id": "school-1"},
    }
    assert s.prompt() == "sacs[teacher:teacher@demo.sacs/northstar-academy]> "


def test_prompt_for_user_without_school():
    s = FakeSession()
    s.current = "new@demo.sacs"
    s.details["new@demo.sacs"] = {"user": {"email": "new@demo.sacs"}, "membership": None}
    assert s.prompt() == "sacs[new@demo.sacs:no-school]> "


def test_dashboard_survives_total_backend_failure():
    s = FakeSession(fail=True)
    layout = _dashboard_layout(s)
    assert layout is not None
    assert layout["dos"] is not None


def test_dashboard_shows_friendly_message_per_panel():
    console = make_console()
    s = FakeSession(fail=True)
    console.print(_dashboard_layout(s))
    rendered = console.file.getvalue()
    assert "No access or unavailable" in rendered
    assert "connection refused" in rendered


def test_unknown_command_suggests_help():
    console = make_console()
    s = FakeSession()
    _handle(console, s, "frobnicate", [])
    out = console.file.getvalue()
    assert "Unknown command" in out
    assert "help" in out


def test_help_lists_navigation_and_invitations():
    console = make_console()
    _help(console, None, [])
    out = console.file.getvalue()
    assert "session switch" in out
    assert "invite" in out
    assert "accept" in out


def test_help_for_single_command():
    console = make_console()
    _help(console, None, ["session"])
    assert "identity switching" in console.file.getvalue()


def test_examples_show_full_lifecycle():
    console = make_console()
    _examples(console)
    out = console.file.getvalue()
    assert "Admin" in out and "Director" in out and "Teacher" in out


def test_session_list_marks_current_identity():
    console = make_console()
    s = FakeSession()
    s.details["teacher@demo.sacs"] = {
        "user": {"email": "teacher@demo.sacs"},
        "membership": {"role": "teacher", "school_name": "Northstar"},
    }
    s.current = "teacher@demo.sacs"
    _session_cmd(console, s, [])
    out = console.file.getvalue()
    assert "teacher@demo.sacs" in out
    assert "platform@demo.sacs" in out


def test_session_switch_requires_email():
    console = make_console()
    s = FakeSession()
    try:
        _session_cmd(console, s, ["switch"])
    except RuntimeError as exc:
        assert "session switch" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a usage error")


def test_whoami_reports_missing_identity():
    console = make_console()
    s = FakeSession()
    s.current = "ghost@demo.sacs"
    _whoami(console, s)
    assert "No active identity" in console.file.getvalue()
