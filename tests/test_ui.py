"""Terminal presentation layer tests: colours, numbered menus, back navigation."""

from __future__ import annotations

import io

from sacs.ui import (
    NAV_BACK,
    NAV_EXIT,
    NAV_LOGOUT,
    Console,
    Menu,
    Navigator,
    Style,
)


def make_console(lines):
    it = iter(lines)

    def input_fn(_prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    stream = io.StringIO()
    return Console(stream=stream, style=Style(False), input_fn=input_fn), stream


def test_style_falls_back_when_not_a_tty():
    stream = io.StringIO()  # not a tty
    style = Style.detect(stream)
    assert style.enabled is False
    assert style.primary("x") == "x"
    assert style.error("boom") == "boom"


def test_style_wraps_when_enabled():
    style = Style(True)
    assert "\x1b[92m" in style.primary("x")
    assert "\x1b[91m" in style.error("x")
    assert style.primary("x").endswith("\x1b[0m")


def test_menu_numbers_options_and_invalid_input_reprompts():
    console, stream = make_console(["zzz", "9", "0"])
    menu = Menu("Root")
    menu.add("Only", lambda: None)
    result = Navigator(console).run(menu)
    out = stream.getvalue()
    assert "1. Only" in out
    assert "0. Back" in out
    assert out.count("Error:") >= 2  # "zzz" then "9"
    assert result == NAV_EXIT


def test_back_returns_to_immediate_parent():
    seen: list[str] = []

    def root():
        m = Menu("ROOT")
        m.add("Child", child)
        return m

    def child():
        m = Menu("CHILD")
        m.add("Leaf", leaf)
        return m

    def leaf():
        m = Menu("LEAF")
        m.add("Touch", lambda: seen.append("touch"))
        return m

    # root -> child -> leaf -> touch -> back(leaf->child) -> open leaf ->
    # touch -> back(leaf->child) -> back(child->root) -> 0 exits
    console, stream = make_console(["1", "1", "1", "0", "1", "1", "0", "0", "0"])
    result = Navigator(console).run(root)
    assert seen == ["touch", "touch"]
    titles = [ln for ln in stream.getvalue().splitlines() if ln in {"ROOT", "CHILD", "LEAF"}]
    # ROOT -> CHILD -> LEAF -> (re-render) LEAF -> back lands in CHILD (not ROOT)
    assert titles[:5] == ["ROOT", "CHILD", "LEAF", "LEAF", "CHILD"]
    assert result == NAV_EXIT


def test_handler_can_logout():
    def root():
        m = Menu("ROOT", back_label="Logout")
        m.add("Quit now", lambda: NAV_LOGOUT)
        return m

    console, _ = make_console(["1"])
    assert Navigator(console).run(root) == NAV_LOGOUT


def test_eof_is_treated_as_back_and_exits_at_root():
    console, _ = make_console([])  # immediate EOF

    def root():
        m = Menu("ROOT")
        m.add("x", lambda: None)
        return m

    assert Navigator(console).run(root) == NAV_EXIT


def test_handler_returning_menu_descends():
    reached = []

    def root():
        m = Menu("ROOT")
        m.add("Go", lambda: sub())
        return m

    def sub():
        m = Menu("SUB")
        m.add("Hit", lambda: reached.append(1))
        return m

    console, _ = make_console(["1", "1", "0", "0"])
    Navigator(console).run(root)
    assert reached == [1]
