"""Terminal presentation layer.

A single, consistent rendering and input layer used by the whole application:

* black background where the terminal allows it, green primary text, dark-green
  secondary text, red errors, monospaced by default;
* plain numbered menus with no boxes, borders, separators, ASCII art, or
  animations;
* automatic fallback to uncoloured text when colour is unsupported (redirected
  output, ``NO_COLOR``, dumb terminals, Windows console without VT).

Navigation is modelled as a *stack* of menu factories. ``0`` always returns to
the immediate parent (never a hard-coded main menu), which is what makes deeply
nested detail views behave predictably. Input is validated in place: bad input
re-prompts the same menu, and EOF / Ctrl+C are treated as "go back" rather than
corrupting state.
"""

from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #

_RESET = "\x1b[0m"
_PRIMARY = "\x1b[92m"  # bright green
_SECONDARY = "\x1b[32m"  # dark green
_ERROR = "\x1b[91m"  # red
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"


def _enable_windows_ansi() -> None:
    """Best-effort enable of VT processing on the Windows console."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # noqa: BLE001 - colour is never worth crashing over
        pass


@dataclass
class Style:
    enabled: bool = False

    @classmethod
    def detect(cls, stream) -> "Style":
        if os.environ.get("NO_COLOR"):
            return cls(False)
        if os.environ.get("TERM") == "dumb":
            return cls(False)
        if not bool(getattr(stream, "isatty", lambda: False)()):
            return cls(False)
        _enable_windows_ansi()
        return cls(True)

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"{code}{text}{_RESET}"

    def primary(self, text: str) -> str:
        return self._wrap(_PRIMARY, text)

    def secondary(self, text: str) -> str:
        return self._wrap(_SECONDARY, text)

    def error(self, text: str) -> str:
        return self._wrap(_ERROR, text)

    def bold(self, text: str) -> str:
        return self._wrap(_BOLD, text)

    def dim(self, text: str) -> str:
        return self._wrap(_DIM, text)


# --------------------------------------------------------------------------- #
# Navigation sentinels
# --------------------------------------------------------------------------- #

NAV_BACK = "__back__"
NAV_LOGOUT = "__logout__"
NAV_EXIT = "__exit__"


@dataclass
class Menu:
    """A numbered screen. Items are ``(label, handler)`` pairs.

    A handler may return:

    * ``None``          -> re-render the current menu;
    * a ``Menu``        -> descend into it;
    * a callable        -> a menu factory to descend into;
    * ``NAV_BACK``      -> return to the parent;
    * ``NAV_LOGOUT`` / ``NAV_EXIT`` -> unwind the whole stack.
    """

    title: str
    items: list[tuple[str, Callable[[], object]]] = field(default_factory=list)
    subtitle: Optional[Callable[[], str]] = None
    back_label: str = "Back"
    back: object = NAV_BACK

    def add(self, label: str, handler: Callable[[], object]) -> "Menu":
        self.items.append((label, handler))
        return self


class Console:
    """All user-visible output and input goes through here."""

    def __init__(self, *, stream=None, style: Optional[Style] = None, input_fn=None):
        self.stream = stream if stream is not None else sys.stdout
        self.style = style if style is not None else Style.detect(self.stream)
        self._input_fn = input_fn if input_fn is not None else input

    # -- output ----------------------------------------------------------- #
    def write(self, text: str = "") -> None:
        print(text, file=self.stream)

    def say(self, text: str = "") -> None:
        self.write(self.style.primary(text))

    def info(self, text: str = "") -> None:
        self.write(self.style.secondary(text))

    def error(self, text: str) -> None:
        self.write(self.style.error(f"Error: {text}"))

    def title(self, text: str) -> None:
        self.write(self.style.bold(self.style.primary(text)))

    def blank(self) -> None:
        self.write("")

    # -- input ------------------------------------------------------------ #
    def read(self, prompt: str = "") -> Optional[str]:
        """Read a line. Returns ``None`` on EOF or Ctrl+C (a cancel signal)."""
        try:
            return self._input_fn(prompt)
        except EOFError:
            self.blank()
            return None
        except KeyboardInterrupt:
            self.blank()
            return None

    def ask(self, prompt: str) -> Optional[str]:
        value = self.read(prompt)
        if value is None:
            return None
        return value.strip()

    def confirm(self, prompt: str, *, default: bool = False) -> Optional[bool]:
        suffix = " [Y/n]: " if default else " [y/N]: "
        raw = self.read(f"{prompt}{suffix}")
        if raw is None:
            return None
        value = raw.strip().lower()
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        self.error("Please answer y or n")
        return self.confirm(prompt)

    def pause(self, prompt: str = "Press Enter to continue") -> None:
        self.read(f"{prompt}... ")


class Navigator:
    """Drives the menu stack and dispatches handlers."""

    def __init__(self, console: Console):
        self.console = console

    def run(self, root: Callable[[], Menu] | Menu) -> str:
        stack: list[Callable[[], Menu]] = []
        stack.append(_as_factory(root))
        while stack:
            try:
                menu = stack[-1]()
                choice = self._read_choice(menu)
                if choice == 0:
                    outcome = menu.back() if callable(menu.back) else menu.back
                    if outcome in (NAV_LOGOUT, NAV_EXIT):
                        return str(outcome)
                    if callable(outcome):
                        stack.append(outcome)
                        continue
                    if len(stack) == 1:
                        return NAV_EXIT
                    stack.pop()
                    continue
                handler = menu.items[choice - 1][1]
                outcome = handler()
            except Cancelled:
                if len(stack) > 1:
                    stack.pop()
                    continue
                return NAV_EXIT
            resolved = self._apply(outcome, stack)
            if resolved is not None:
                return resolved
        return NAV_EXIT

    def _apply(self, outcome: object, stack: list) -> Optional[str]:
        if outcome is None:
            return None
        if isinstance(outcome, Menu):
            stack.append(lambda m=outcome: m)
            return None
        if callable(outcome):
            stack.append(outcome)
            return None
        if outcome == NAV_BACK:
            if len(stack) > 1:
                stack.pop()
            else:
                return NAV_EXIT
            return None
        if outcome in (NAV_LOGOUT, NAV_EXIT):
            return str(outcome)
        return None

    def _read_choice(self, menu: Menu) -> int:
        while True:
            self.console.blank()
            self.console.title(menu.title)
            if menu.subtitle is not None:
                text = menu.subtitle()
                if text:
                    self.console.info(text)
            for index, (label, _) in enumerate(menu.items, start=1):
                self.console.say(f"{index}. {label}")
            self.console.say(f"0. {menu.back_label}")
            raw = self.console.read("Select: ")
            if raw is None:
                return 0
            raw = raw.strip()
            if not raw:
                self.console.error("Please enter a number.")
                continue
            try:
                value = int(raw)
            except ValueError:
                self.console.error("Please enter a number.")
                continue
            if value == 0:
                return 0
            if 1 <= value <= len(menu.items):
                return value
            self.console.error(f"Choose between 0 and {len(menu.items)}.")


def _as_factory(menu: Menu | Callable[[], Menu]) -> Callable[[], Menu]:
    if isinstance(menu, Menu):
        return lambda m=menu: m
    return menu


# --------------------------------------------------------------------------- #
# Small helpers for form-style input
# --------------------------------------------------------------------------- #

class Cancelled(Exception):
    """Raised by form helpers when the user cancels (blank/EOF/Ctrl+C)."""


def ask_required(console: Console, label: str) -> str:
    value = console.read(f"{label}: ")
    if value is None:
        raise Cancelled()
    value = value.strip()
    if not value:
        raise Cancelled()
    return value


def ask_optional(console: Console, label: str) -> Optional[str]:
    value = console.read(f"{label} (blank to skip): ")
    if value is None:
        raise Cancelled()
    value = value.strip()
    return value or None


def ask_int(console: Console, label: str, *, minimum: int, maximum: int) -> int:
    while True:
        value = console.read(f"{label}: ")
        if value is None:
            raise Cancelled()
        value = value.strip()
        try:
            number = int(value)
        except ValueError:
            console.error("Please enter a whole number.")
            continue
        if minimum <= number <= maximum:
            return number
        console.error(f"Enter a number between {minimum} and {maximum}.")
