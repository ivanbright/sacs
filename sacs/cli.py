"""SACS command-line interface.

``sacs demo`` runs the whole platform locally: it migrates the database, seeds
deterministic demonstration data, starts the shared SACS service, opens five
independently authorized identities, and provides a live multi-workspace
dashboard plus the full academic coordination workflow in the terminal.

Workspaces are first-class CLI views backed by the same server; there is no
separate browser frontend, and no role can mutate another role's tenant data
because every CLI action is performed through the authenticated API with
server-side authorization.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time as time_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import typer
from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import get_settings, override_settings
from .db import configure_database, get_sessionmaker

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=(
        "SACS - Smart Academic Coordination System.\n\n"
        "Run the whole multi-tenant school platform locally from your terminal:\n\n"
        "[bold]sacs demo[/bold]   start the server, seed data, and open the live dashboard\n\n"
        "Inside the demo type [bold]help[/bold] for commands, [bold]examples[/bold] for a "
        "runnable Admin -> Director -> Teacher walkthrough, and [bold]whoami[/bold] to see the "
        "identity you are acting as. Other commands: [bold]check[/bold], [bold]serve[/bold], "
        "[bold]status[/bold], [bold]demo reset[/bold], [bold]demo stop[/bold], [bold]version[/bold]."
    ),
)
demo_app = typer.Typer(no_args_is_help=False, help="Run or manage the local SACS demonstration.")
app.add_typer(demo_app, name="demo", help="Run or manage the local SACS demonstration.")
api = httpx.Client(timeout=10.0)


def _console(ctx: typer.Context) -> Console:
    return ctx.obj if hasattr(ctx, "obj") and ctx.obj else Console()


def main() -> None:
    """Console-script entry point: dispatch the Typer application."""
    app()


@app.callback()
def cli(
    ctx: typer.Context,
    no_color: bool = typer.Option(False, "--no-color", help="Disable ANSI colors."),
) -> None:
    ctx.obj = Console(no_color=no_color)


@app.command("version")
def version_cmd(ctx: typer.Context) -> None:
    """Show the installed SACS version."""
    console = _console(ctx)
    console.print(f"sacs [bold]{__version__}[/bold] (Smart Academic Coordination System)")


@app.command("check")
def check(ctx: typer.Context) -> None:
    """Verify the local runtime can run the demonstration."""
    import sys

    import rich  # noqa: F401

    console = _console(ctx)
    table = Table(title="SACS runtime check", box=box.ROUNDED)
    table.add_column("Requirement")
    table.add_column("Status")
    good = True
    checks = []

    if sys.version_info >= (3, 11):
        checks.append(("Python >= 3.11", f"ok ({sys.version.split()[0]})", "green"))
    else:
        checks.append(("Python >= 3.11", f"too old ({sys.version})", "red"))
        good = False

    for name, module in (
        ("FastAPI", "fastapi"),
        ("SQLAlchemy", "sqlalchemy"),
        ("Alembic", "alembic"),
        ("Typer", "typer"),
        ("Rich", "rich"),
        ("httpx", "httpx"),
        ("uvicorn", "uvicorn"),
    ):
        try:
            __import__(module)
            checks.append((f"dependency: {name}", "ok", "green"))
        except Exception:  # noqa: BLE001
            checks.append((f"dependency: {name}", "missing", "red"))
            good = False

    settings = get_settings()
    try:
        settings.ensure_home()
        checks.append((f"data dir writable ({settings.home})", "ok", "green"))
    except Exception as exc:  # noqa: BLE001
        checks.append((f"data dir writable ({settings.home})", str(exc), "red"))
        good = False

    from sacs import db as db_mod

    try:
        db_mod.get_engine().connect().close()
        checks.append(("database engine", "ok", "green"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("database engine", str(exc), "red"))
        good = False

    for label, status, color in checks:
        table.add_row(label, f"[{color}]{status}[/{color}]")
    console.print(table)
    console.print(
        "\n[green]Ready for `sacs demo`.[/green]" if good else "\n[red]Resolve the failures above before running the demo.[/red]"
    )
    raise typer.Exit(code=0 if good else 1)


@app.command("serve")
def serve(ctx: typer.Context) -> None:
    """Start the SACS service in the foreground (no seeding or dashboard)."""
    console = _console(ctx)
    settings = get_settings()
    settings.ensure_home()
    _init_db(console)
    runtime = _start_server(console)
    console.print(f"SACS service listening on [bold]{runtime['url']}[/bold]  (Ctrl+C to stop)")
    _wait_health(console, runtime["url"], timeout=20)
    try:
        while True:
            time_mod.sleep(3600)
    except KeyboardInterrupt:
        _stop_server(runtime)
        console.print("Stopped.")


# --------------------------------------------------------------------------- #
# `sacs demo` group
# --------------------------------------------------------------------------- #

@demo_app.callback(invoke_without_command=True)
def demo(ctx: typer.Context) -> None:
    console = _console(ctx)
    if ctx.invoked_subcommand is not None:
        return
    run_demo(console)


@demo_app.command("reset")
def demo_reset(ctx: typer.Context, force: bool = typer.Option(False, "--force", help="Skip the confirmation prompt.")) -> None:
    """Destroy ONLY the local demonstration database (requires confirmation)."""
    console = _console(ctx)
    settings = get_settings()
    if not force:
        confirmed = typer.confirm(
            "This destroys ONLY the local demonstration database. Deployed school data "
            "is never touched. Continue?",
            default=False,
        )
        if not confirmed:
            console.print("Aborted - nothing was deleted.")
            raise typer.Exit(0)
    db_file = settings.home / "sacs.db"
    removed = []
    if db_file.exists():
        db_file.unlink()
        removed.append(str(db_file))
    for extra in (settings.runtime_file, settings.pid_file):
        if extra.exists():
            extra.unlink()
            removed.append(str(extra))
    if removed:
        console.print(f"[red]Reset complete.[/red] Removed:\n" + "\n".join(f"  - {p}" for p in removed))
    else:
        console.print("No demonstration database found; nothing to reset.")


@demo_app.command("stop")
def demo_stop(ctx: typer.Context) -> None:
    """Stop a demonstration running in another terminal."""
    console = _console(ctx)
    settings = get_settings()
    if not settings.runtime_file.exists():
        console.print("The demonstration does not appear to be running.")
        raise typer.Exit(0)
    pid = json.loads(settings.runtime_file.read_text()).get("pid")
    if pid and int(pid) != os.getpid():
        os.kill(int(pid), signal.SIGTERM)
        console.print(f"Sent stop signal to demonstration process [bold]{pid}[/bold].")
    else:
        console.print("Stop this process by pressing Ctrl+C in its terminal.")


# --------------------------------------------------------------------------- #
# Demo bootstrap
# --------------------------------------------------------------------------- #

def _init_db(console: Console) -> None:
    settings = get_settings()
    settings.ensure_home()
    configure_database(settings.database_url)


def _bind_socket(host: str, port: int) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port if port else 0))
    sock.listen(1024)
    return sock


def _start_server(console: Console) -> dict:
    import uvicorn

    from .app import create_app

    settings = get_settings()
    host = settings.host
    sock = _bind_socket(host, settings.port)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app=create_app(), host=host, port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    return {
        "server": server,
        "thread": thread,
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "pid": os.getpid(),
    }


def _stop_server(runtime: dict) -> None:
    runtime["server"].should_exit = True
    runtime["thread"].join(timeout=10)


def _wait_health(console: Console, url: str, timeout: float = 25.0) -> None:
    deadline = time_mod.time() + timeout
    while time_mod.time() < deadline:
        try:
            if api.get(f"{url}/api/health", timeout=1.5).status_code == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time_mod.sleep(0.3)
    raise typer.Exit("The SACS service did not become healthy in time. Check `sacs status` and the demo log.")


def _login(base: str, email: str, password: str, school_id: str | None = None) -> str:
    resp = api.post(
        f"{base}/api/auth/login",
        json={"email": email, "password": password, "school_id": school_id},
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _write_runtime(runtime: dict, extra: dict) -> None:
    settings = get_settings()
    payload = {k: str(v) for k, v in runtime.items() if k not in ("server", "thread")}
    payload.update(extra)
    settings.runtime_file.write_text(json.dumps(payload, indent=2))
    settings.pid_file.write_text(str(runtime["pid"]))
    settings.log_file.write_text("SACS demo started.\n")


# --------------------------------------------------------------------------- #
# Interactive demo session
# --------------------------------------------------------------------------- #

class DemoSession:
    """Authenticated identities over one shared service.

    Each identity holds its own server session token. The *current* identity is
    explicit and never changes silently; workflow commands act as the current
    identity and the server rejects them when that identity lacks permission.
    """

    def __init__(self, base: str, env: dict, console: Console):
        self.base = base
        self.env = env
        self.console = console
        self.tokens: dict[str, str] = {}
        self.details: dict[str, dict] = {}
        self.current: str = "platform"
        self.lesson_list: list[dict] = []
        self.request_list: list[dict] = []

    def auth(
        self, key: str, email: str, password: str = "SacsDemo123!", school_slug: str | None = None
    ) -> dict:
        sid = None
        if school_slug:
            s = next((x for x in self.env["schools"] if x["school"].slug == school_slug), None)
            sid = s["school"].id if s else None
        return self.login(key, email, password, sid)

    def login(
        self, key: str, email: str, password: str = "SacsDemo123!", school_id: str | None = None
    ) -> dict:
        resp = api.post(
            f"{self.base}/api/auth/login",
            json={"email": email, "password": password, "school_id": school_id},
        )
        resp.raise_for_status()
        data = resp.json()
        self.tokens[key] = data["token"]
        self.details[key] = data
        return data

    def switch(self, email: str, school_id: str | None = None) -> dict:
        email = email.strip().lower()
        data = self.login(email, email, school_id=school_id)
        self.current = email
        return data

    def _h(self, key: str, path: str, method="GET", **kwargs):
        headers = {"Authorization": f"Bearer {self.tokens[key]}"}
        return api.request(method, f"{self.base}{path}", headers=headers, **kwargs)

    def j(self, key, path, **kwargs):
        r = self._h(key, path, **kwargs)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:  # noqa: BLE001
                detail = r.text
            raise RuntimeError(f"{r.status_code} {detail}")
        return r.json()

    # -- act as the current identity -----------------------------------------
    def cur(self, path, **kwargs):
        return self.j(self.current, path, **kwargs)

    def cur_h(self, path, method="GET", **kwargs):
        return self._h(self.current, path, method=method, **kwargs)

    def whoami(self) -> dict:
        return self.details.get(self.current, {})

    def _slug(self, school_id: str | None) -> str:
        for s in self.env.get("schools", []):
            if s["school"].id == school_id:
                return s["school"].slug
        return "?"

    def prompt(self) -> str:
        email = self.current
        d = self.details.get(email) or {}
        mem = d.get("membership")
        if self.current == "platform" or d.get("is_platform"):
            inner = "admin"
        elif mem:
            inner = f"{mem.get('role')}:{email}/{self._slug(mem.get('school_id'))}"
        else:
            inner = f"{email}:no-school"
        return f"sacs[{inner}]> "



def run_demo(console: Console) -> None:
    from . import seed as seed_mod

    settings = get_settings()
    console.rule("[bold cyan]SACS Demo[/bold cyan]")
    with console.status("[cyan]Initialising database and migrations…"):
        _init_db(console)
        factory = get_sessionmaker()
        db = factory()
        env = seed_mod.seed_all(db)
        db.close()

    runtime = _start_server(console)
    _wait_health(console, runtime["url"])
    db = factory()
    school_a = next(s for s in env["schools"] if s["school"].slug == "northstar-academy")

    session = DemoSession(runtime["url"], env, console)
    session.auth("platform", seed_mod.PLATFORM_EMAIL)
    session.auth("head", school_a["head_user"].email, school_slug=school_a["school"].slug)
    session.auth("dos", school_a["dos_user"].email, school_slug=school_a["school"].slug)
    session.auth("teacher", school_a["teacher_user"].email, school_slug=school_a["school"].slug)
    session.auth("bell", school_a["bell_user"].email, school_slug=school_a["school"].slug)
    db.close()

    _write_runtime(
        runtime,
        {
            "emails": {
                "platform": seed_mod.PLATFORM_EMAIL,
                "head": school_a["head_user"].email,
                "dos": school_a["dos_user"].email,
                "teacher": school_a["teacher_user"].email,
                "bell": school_a["bell_user"].email,
            },
            "password": seed_mod.DEMO_PASSWORD,
            "school_id": school_a["school"].id,
        },
    )

    _welcome(console, runtime, school_a)
    try:
        _loop(console, session)
    except KeyboardInterrupt:
        console.print("\n[dimi]Shutting down…[/dimi]")
    finally:
        _set_clock_back(session)
        _stop_server(runtime)
        settings.pid_file.unlink(missing_ok=True)
        console.print("[green]Demonstration stopped cleanly. `sacs demo` reuses the existing database.[/green]")


def _set_clock_back(session: DemoSession) -> None:
    try:
        session._h("platform", "/api/test/clock/clear", method="POST")
    except Exception:  # noqa: BLE001
        pass


def _welcome(console: Console, runtime: dict, school_a) -> None:
    settings = get_settings()
    table = Table(title="Demo identities (all five share one running system)", box=box.SIMPLE_HEAVY)
    table.add_column("Workspace")
    table.add_column("Email")
    table.add_column("Role")
    rows = [
        ("Platform Owner", "platform@demo.sacs", "Platform administration"),
        ("School Head", school_a["head_user"].email, "Final academic authority"),
        ("Director of Studies", school_a["dos_user"].email, "Coordination & review"),
        ("Teacher", school_a["teacher_user"].email, "Submits completion requests"),
        ("Bell & Schedule Monitor", school_a["bell_user"].email, "Read-only scheduler view"),
    ]
    for name, email, role in rows:
        table.add_row(f"[bold]{name}[/bold]", email, role)
    console.print(
        Panel(
            Group(
                Text(f"Service URL : {runtime['url']}"),
                Text(f"Demo DB     : {settings.home / 'sacs.db'}"),
                Text("Demo password (all): SacsDemo123!  (local demonstration identity only)"),
                table,
            ),
            title="[bold]Welcome to SACS[/bold]",
            border_style="cyan",
        )
    )
    console.print(
        "Type [bold]help[/bold] for commands, [bold]watch[/bold] for the live monitor, or [bold]quit[/bold] to stop.\n"
    )


def _loop(console: Console, s: DemoSession) -> None:
    while True:
        try:
            if s.console.is_terminal:
                _dashboard(console, s)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Dashboard error: {exc}[/red]")
        try:
            cmd = input(s.prompt()).strip()
        except EOFError:
            break
        if not cmd:
            continue
        parts = cmd.split()
        verb = parts[0].lower()
        if verb in ("q", "quit", "exit"):
            break
        try:
            _handle(console, s, verb, parts[1:])
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Error:[/red] {exc}")


# --------------------------------------------------------------------------- #
# Live dashboard
# --------------------------------------------------------------------------- #

def _dashboard(console: Console, s: DemoSession) -> None:
    console.print(_dashboard_layout(s), new_line_start=False)


def _is_error(data) -> bool:
    return isinstance(data, dict) and "__error__" in data


def _err_text(data) -> str:
    return data.get("__error__", "unavailable") if isinstance(data, dict) else "unavailable"


def _error_panel(title: str, data) -> Panel:
    return Panel(
        Text(f"No access or unavailable:\n{_err_text(data)}", style="yellow"),
        title=f"[bold]{title}[/bold]",
        border_style="yellow",
    )


def _fetch(s: DemoSession, key: str, path: str):
    try:
        return s.j(key, path)
    except Exception as exc:  # noqa: BLE001 - a single failing panel must not blank the dashboard
        return {"__error__": str(exc)}


def _dashboard_layout(s: DemoSession) -> Layout:
    school_id = s.env["schools"][0]["school"].id
    platform = _fetch(s, "platform", "/api/platform/schools")
    health = _fetch(s, "platform", "/api/platform/health")
    school_overview = _fetch(s, "head", f"/api/schools/{school_id}/overview")
    requests = _fetch(s, "dos", f"/api/schools/{school_id}/requests")
    teacher_reqs = _fetch(s, "teacher", f"/api/schools/{school_id}/requests")
    bell = _fetch(s, "bell", f"/api/schools/{school_id}/bell/events")

    layout = Layout(name="root")
    layout.split(Layout(name="header", size=3), Layout(name="body", ratio=1))
    layout["header"].update(
        Text(
            f"SACS - one shared system - acting as {s.current} - {s.base}",
            style="bold cyan",
        )
    )

    top = Layout()
    mid = Layout()
    btm = Layout()
    layout["body"].split_column(top, Layout(size=1), mid, Layout(size=1), btm)
    top.split_row(Layout(name="platform"), Layout(name="tenants"))
    mid.split_row(Layout(name="head"), Layout(name="dos"))
    btm.split_row(Layout(name="teacher"), Layout(name="bell"))

    layout["platform"].update(_platform_panel(health))
    layout["tenants"].update(_schools_panel(platform))
    layout["dos"].update(_queue_panel("Review queue (Director)", requests))
    layout["teacher"].update(_queue_panel("Teacher - My requests", teacher_reqs))
    layout["head"].update(_head_panel(school_overview, requests))
    layout["bell"].update(_bell_panel(bell))
    return layout


def _platform_panel(health) -> Panel:
    if _is_error(health):
        return _error_panel("1. Platform Owner", health)
    table = Table(show_header=False, box=None)
    table.add_column("")
    table.add_column("")
    table.add_row("Scheduler", (health.get("scheduler") or {}).get("clock", "?"))
    table.add_row("DB", health.get("database", "?"))
    table.add_row("Version", f"sacs {health.get('version', '?')}")
    return Panel(table, title="[bold]1. Platform Owner[/bold]", border_style="cyan")


def _schools_panel(schools) -> Panel:
    if _is_error(schools) or not isinstance(schools, list):
        return _error_panel("Tenants", schools)
    table = Table(box=box.SIMPLE)
    table.add_column("School")
    table.add_column("Status")
    for school in schools:
        color = "yellow" if school["status"] == "suspended" else "green"
        table.add_row(school["name"], f"[{color}]{school['status']}[/]")
    return Panel(table, title="[bold]Tenants[/bold]")


def _queue_panel(title: str, requests) -> Panel:
    if _is_error(requests) or not isinstance(requests, list):
        return _error_panel(title, requests)
    table = Table(box=box.SIMPLE, show_lines=False)
    table.add_column("#")
    table.add_column("Subject")
    table.add_column("Date")
    table.add_column("State")
    for i, r in enumerate(requests[:8], 1):
        lesson = r.get("lesson") or {}
        table.add_row(
            str(i),
            lesson.get("date") or "-",
            (lesson.get("start") or "-")[:16],
            _state(r["state"]),
        )
    return Panel(table, title=f"[bold]{title}[/bold]")


def _head_panel(overview, requests) -> Panel:
    if _is_error(overview):
        return _error_panel("2. Director", overview)
    pending = 0
    if isinstance(requests, list):
        pending = len([r for r in requests if r["state"] == "forwarded_to_head"])
    text = Text()
    text.append(f"Awaiting final decision: {pending}", style="yellow")
    text.append(f"\nLessons today: {overview.get('lessons_today', 0)}")
    text.append(f"\nTimetable entries: {overview.get('timetable_entries', 0)}")
    return Panel(text, title="[bold]2. Director[/bold]", border_style="magenta")


def _bell_panel(bell) -> Panel:
    if _is_error(bell):
        return _error_panel("5. Bell & Schedule Monitor", bell)
    table = Table(box=box.SIMPLE)
    table.add_column("Event")
    table.add_column("Scheduled")
    table.add_column("Outcome")
    for e in bell.get("events", [])[:8]:
        color = {"fired": "green", "missed": "red", "skipped": "yellow"}.get(e["outcome"], "white")
        table.add_row(e["label"], (e["scheduled_at"] or "?")[:19], f"[{color}]{e['outcome']}[/{color}]")
    next_event = _next_bell(bell)
    return Panel(
        Group(table, Text(f"\nNext scheduled event: {next_event}", style="bold yellow")),
        title="[bold]5. Bell & Schedule Monitor[/bold]",
        border_style="green",
    )


def _next_bell(bell: dict) -> str:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    candidates = []
    for e in bell["events"]:
        if e["outcome"] == "fired":
            continue
        try:
            scheduled = datetime.fromisoformat(e["scheduled_at"].replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        if scheduled > now:
            candidates.append((scheduled, e["label"]))
    if not candidates:
        return "none (use `bell advance` to fire the next one)"
    candidates.sort()
    delta = candidates[0][0] - now
    seconds = int(delta.total_seconds())
    return f"{candidates[0][1]} in {seconds}s"


def _state(state: str) -> str:
    colors = {
        "submitted": "magenta",
        "under_dos_review": "cyan",
        "forwarded_to_head": "yellow",
        "approved": "green",
        "rejected": "red",
        "returned_for_correction": "orange1",
    }
    color = colors.get(state, "white")
    return f"[{color}]{state}[/{color}]"


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #

def _handle(console: Console, s: DemoSession, verb: str, args: list[str]) -> None:
    if verb == "help":
        _help(console, s, args)
    elif verb == "examples":
        _examples(console)
    elif verb in ("whoami", "status", "context"):
        _whoami(console, s)
    elif verb in ("session", "switch", "su"):
        _session_cmd(console, s, args)
    elif verb == "invite":
        _invite(console, s, args)
    elif verb in ("invitations", "inbox"):
        _invitations(console, s)
    elif verb == "accept":
        _accept(console, s, args)
    elif verb == "watch":
        _watch(console, s, int(args[0]) if args else 10)
    elif verb == "accounts":
        _accounts(console, s)
    elif verb == "lessons":
        _lessons(console, s)
    elif verb == "requests":
        _requests(console, s)
    elif verb == "submit":
        _submit(console, s, args)
    elif verb in ("review", "forward", "return", "approve", "reject"):
        _acts(console, s, verb, args)
    elif verb == "bell":
        _bell_cmd(console, s, args)
    elif verb == "school":
        _school_cmd(console, s, args)
    elif verb == "home":
        _session_cmd(console, s, ["switch", "platform"])
    else:
        console.print(
            f"Unknown command: [bold]{verb}[/bold]. "
            "Try [bold]help[/bold], [bold]examples[/bold] or [bold]whoami[/bold]."
        )


def _whoami(console: Console, s: DemoSession) -> None:
    d = s.whoami()
    if not d:
        console.print("No active identity. Use [bold]session switch <email>[/bold].")
        return
    user = d.get("user", {})
    mem = d.get("membership")
    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column(""); table.add_column("")
    table.add_row("Email", user.get("email", "?"))
    table.add_row("Name", user.get("display_name", "?"))
    table.add_row("Kind", "Platform administrator" if d.get("is_platform") else "School member")
    if mem:
        table.add_row("School", f"{mem.get('school_name')}")
        table.add_row("Role", mem.get("role"))
        table.add_row("Membership", mem.get("status"))
    else:
        table.add_row("School", "- (accept an invitation to join one)")
    console.print(Panel(table, title="[bold]Current identity[/bold]", border_style="cyan"))


def _session_cmd(console: Console, s: DemoSession, args: list[str]) -> None:
    action = (args[0].lower() if args else "list")
    if action in ("list", "ls"):
        table = Table(title="Demo identities", box=box.SIMPLE_HEAVY)
        table.add_column("Email"); table.add_column("Role"); table.add_column("School")
        for key, d in s.details.items():
            u = d.get("user", {})
            mem = d.get("membership")
            table.add_row(
                ("[bold]" + u.get("email", key) + "[/bold]") if key == s.current else u.get("email", key),
                "admin" if d.get("is_platform") else (mem or {}).get("role", "no school"),
                (mem or {}).get("school_name", "-"),
            )
        console.print(table)
        console.print("Switch with [bold]session switch <email>[/bold] (demo only, password is shared).")
        return
    if action in ("switch", "su"):
        if len(args) < 2:
            raise RuntimeError("session switch <email>  (see `session list`)")
        email = args[1]
        if email == "platform":
            key = next((k for k, d in s.details.items() if d.get("is_platform")), None)
            if key is None:
                raise RuntimeError("No platform identity is signed in.")
            s.current = key
            _whoami(console, s)
            return
        d = s.switch(email)
        console.print(
            f"[green]Now acting as[/green] {email}"
            + ("" if d.get("membership") else "  [yellow](no school membership yet - check `invitations`)[/yellow]")
        )
        return
    raise RuntimeError("Usage: session list | session switch <email>")


def _invitations(console: Console, s: DemoSession) -> None:
    rows = s.cur_h("/api/invitations/mine")
    if rows.status_code >= 400:
        raise RuntimeError(f"{rows.status_code} {rows.json().get('detail')}")
    items = rows.json()
    if not items:
        console.print("No invitations addressed to " + s.current + ".")
        return
    table = Table(title=f"Invitations for {s.current}", box=box.SIMPLE_HEAVY)
    table.add_column("id")
    table.add_column("School")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Expires")
    for i in items:
        table.add_row(i["id"][:8], i.get("school_name", "?"), i.get("role", "?"), i["status"], (i.get("expires_at") or "-")[:19])
    console.print(table)
    pending = [i for i in items if i["status"] == "pending"]
    if pending:
        console.print(f"Accept with [bold]accept {pending[0]['id']}[/bold].")


def _accept(console: Console, s: DemoSession, args: list[str]) -> None:
    target = args[0] if args else None
    if target is None:
        rows = s.cur_h("/api/invitations/mine").json()
        pending = [i for i in rows if i["status"] == "pending"]
        if not pending:
            raise RuntimeError("You have no pending invitations.")
        target = pending[0]["id"]
    # allow a short (8-char) reference
    rows = s.cur_h("/api/invitations/mine").json()
    if len(target) < 36:
        matches = [i for i in rows if i["id"].startswith(target)]
        if len(matches) == 1:
            target = matches[0]["id"]
        elif not matches:
            raise RuntimeError(f"No invitation matches '{target}'. Use `invitations` to list them.")
    r = s.cur_h("/api/invitations/accept", method="POST", json={"invitation_id": target})
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.json().get('detail', r.text)}")
    payload = r.json()
    console.print(f"[green]Invitation accepted.[/green] Joined {payload.get('school_id')} as {payload.get('role')}.")
    # refresh the identity so the new membership context is active
    s.switch(s.current)
    _whoami(console, s)


def _invite(console: Console, s: DemoSession, args: list[str]) -> None:
    if s.current == "platform":
        raise RuntimeError(
            "As platform owner use `school create <name>` (optionally inviting a head), "
            "or `school` to list tenants."
        )
    d = s.whoami()
    mem = d.get("membership")
    if not mem:
        raise RuntimeError("You are not a member of a school yet. Accept an invitation first.")
    email = args[0] if args else input("Recipient email: ").strip()
    role = args[1] if len(args) > 1 else (input("Role [teacher/dos] (default teacher): ").strip() or "teacher")
    sid = mem["school_id"]
    r = s.cur_h(f"/api/schools/{sid}/invitations", method="POST", json={"email": email, "role": role})
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.json().get('detail', r.text)}")
    inv = r.json()
    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column(""); table.add_column("")
    table.add_row("Invitation", inv["id"][:8])
    table.add_row("Recipient", inv["email"])
    table.add_row("Role", inv.get("role_label", inv["role"]))
    table.add_row("School", inv.get("school_name", ""))
    table.add_row("Expires", (inv.get("expires_at") or "")[:19])
    table.add_row("Reference", inv.get("accept_uri", ""))
    console.print(Panel(table, title="[bold]Invitation created[/bold]", border_style="green"))
    console.print(
        "Next: the recipient runs [bold]session switch " + inv["email"] + "[/bold], then "
        "[bold]invitations[/bold] and [bold]accept " + inv["id"][:8] + "[/bold]."
    )


def _examples(console: Console) -> None:
    examples = (
        "[bold]Explore[/bold]\n"
        "  help                      contextual help\n"
        "  whoami                    show the active identity and school\n"
        "  session list              list demo identities\n"
        "  session switch teacher.northstar@demo.sacs\n\n"
        "[bold]Admin -> Director -> Teacher invitation lifecycle[/bold]\n"
        "  session switch platform\n"
        "  school create \"Beta Academy\" newdirector@demo.sacs\n"
        "  session switch newdirector@demo.sacs\n"
        "  invitations ; accept <id>\n"
        "  invite newteacher@demo.sacs teacher\n"
        "  session switch newteacher@demo.sacs\n"
        "  invitations ; accept <id>\n\n"
        "[bold]Lesson completion workflow[/bold]\n"
        "  session switch teacher.northstar@demo.sacs\n"
        "  lessons ; submit 1 \"taught as planned\"\n"
        "  session switch head.northstar@demo.sacs\n"
        "  requests ; review 1 ; forward 1 ; approve 1\n\n"
        "[bold]Bell scheduler[/bold]\n"
        "  bell ; bell advance 600 ; bell"
    )
    console.print(Panel(examples, title="[bold]SACS examples[/bold]", border_style="cyan", padding=(1, 2)))


def _help(console: Console, s: DemoSession | None = None, args: list[str] | None = None) -> None:
    if args:
        _help_command(console, args[0])
        return
    console.print(
        Panel(
            Text.from_markup(
                "SACS coordinates schools through the terminal.\n"
                "You are always one explicit identity; commands act as that identity and the\n"
                "server enforces every permission. Type [bold]whoami[/bold] to see where you are."
            ),
            title="[bold]What is SACS?[/bold]",
            border_style="cyan",
        )
    )
    groups = {
        "Getting started & navigation": [
            ("help [command]", "Show this help, or details for one command"),
            ("examples", "Runnable end-to-end examples"),
            ("whoami", "Show the active identity, school and role"),
            ("session list", "List demo identities"),
            ("session switch <email>", "Act as another demo identity (demo only)"),
            ("watch [seconds]", "Live auto-refreshing dashboard"),
        ],
        "Schools & platform administration": [
            ("school", "List tenants"),
            ("school create <name> [head_email]", "Platform: provision a new school (optionally invite a Director)"),
            ("school invite <slug> <email>", "Platform: invite a Director to an existing school"),
            ("school suspend|reactivate <slug>", "Platform: change a school's lifecycle state"),
        ],
        "Invitations & memberships": [
            ("invite <email> <role>", "Director: invite a teacher/dos"),
            ("invitations", "List invitations addressed to you"),
            ("accept <id>", "Accept your invitation"),
        ],
        "Lessons & completion requests": [
            ("lessons", "Teacher: list assigned published lessons"),
            ("submit <n> [note]", "Teacher: submit a completion request"),
            ("requests", "List requests visible to you"),
            ("review|forward|return <n> [reason]", "Director: move a request"),
            ("approve|reject <n> [reason]", "Director: final decision"),
        ],
        "Bell scheduling": [
            ("bell", "Show scheduler status and latest events"),
            ("bell tick", "Force the scheduler to evaluate now"),
            ("bell advance <seconds>", "Advance the test clock and fire the next event"),
            ("bell pause|resume", "Pause/resume the school bell schedule"),
        ],
        "Demo state & troubleshooting": [
            ("accounts", "Show demo identities and credentials"),
            ("quit", "Stop the demonstration"),
        ],
    }
    for title, rows in groups.items():
        table = Table(title=title, box=box.SIMPLE, show_header=False)
        table.add_column("Command"); table.add_column("Meaning")
        for c, m in rows:
            table.add_row(f"[bold]{c}[/bold]", m)
        console.print(table)
    console.print("Add [bold]--help[/bold] to any top-level command for syntax. Try [bold]examples[/bold].")


def _help_command(console: Console, name: str) -> None:
    docs = {
        "session": ("session switch <email>", "Demo-only identity switching. Changes which identity subsequent commands act as; never silently."),
        "invite": ("invite <email> <role>", "Director invites a person to the current school. Role is teacher (or dos). Creates a persisted invitation the recipient accepts themselves."),
        "accept": ("accept <id>", "Accept an invitation addressed to your email. Establishes the membership server-side."),
        "school": ("school create <name> [head_email] | school invite <slug> <email> | school suspend|reactivate <slug>", "Platform tenant management. Creating a school places it in provisioning until an invited Director accepts, which activates it."),
        "submit": ("submit <n> [note]", "Teacher submits a completion request for the nth lesson shown by `lessons`."),
        "requests": ("requests", "Lists requests you may see: your own as a teacher, the school queue as a director."),
    }
    if name not in docs:
        console.print(f"No detailed help for '{name}'. Try [bold]help[/bold].")
        return
    syntax, meaning = docs[name]
    console.print(Panel(meaning, title=f"[bold]{syntax}[/bold]", border_style="cyan"))


def _accounts(console: Console, s: DemoSession) -> None:
    _welcome(console, {"url": s.base}, s.env["schools"][0])


def _lessons(console: Console, s: DemoSession) -> None:
    sid = s.env["schools"][0]["school"].id
    r = s.cur_h(f"/api/schools/{sid}/timetable/lessons?date_from=" + _today())
    if r.status_code >= 400:
        raise RuntimeError(
            f"{r.status_code} {r.json().get('detail')}  -  switch to a teacher, e.g. "
            "`session switch teacher.northstar@demo.sacs`"
        )
    lessons = r.json()
    s.lesson_list = lessons
    table = Table(title=f"Assigned lessons ({s.current})", box=box.SIMPLE_HEAVY)
    table.add_column("n"); table.add_column("Date"); table.add_column("Time")
    table.add_column("Subject"); table.add_column("Class"); table.add_column("Room")
    for i, lesson in enumerate(lessons, 1):
        table.add_row(str(i), lesson["date"], lesson["start"][11:19], lesson["subject"], lesson["class"], lesson["classroom"] or "-")
    console.print(table)


def _submit(console: Console, s: DemoSession, args: list[str]) -> None:
    if not s.lesson_list:
        _lessons(console, s)
    if not s.lesson_list:
        raise RuntimeError("No published lessons found")
    idx = int(args[0]) if args else 1
    note = " ".join(args[1:]) or "Lesson completed as scheduled"
    lesson = s.lesson_list[idx - 1]
    r = s.cur_h(f"/api/schools/{s.env['schools'][0]['school'].id}/requests", method="POST", json={"lesson_session_id": lesson["id"], "note": note})
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.json().get('detail', r.text)}")
    body = r.json()
    console.print(f"Submitted request -> [bold]{body['state']}[/bold] (id {body['id'][:8]})")


def _requests(console: Console, s: DemoSession) -> None:
    sid = s.env["schools"][0]["school"].id
    r = s.cur_h(f"/api/schools/{sid}/requests")
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.json().get('detail')}")
    reqs = r.json()
    s.request_list = reqs
    table = Table(title=f"Requests visible to {s.current}", box=box.SIMPLE_HEAVY)
    table.add_column("n"); table.add_column("id"); table.add_column("Subject/Date")
    table.add_column("State"); table.add_column("Updated")
    for i, req in enumerate(reqs, 1):
        lesson = req.get("lesson") or {}
        table.add_row(str(i), req["id"][:8], f"{lesson.get('date')} {lesson.get('start', '')[:16]}", _state(req["state"]), req["updated_at"][:19])
    console.print(table)


def _acts(console: Console, s: DemoSession, verb: str, args: list[str]) -> None:
    if not s.request_list:
        _requests(console, s)
    if not s.request_list:
        raise RuntimeError("No requests to act on.")
    idx = int(args[0]) if args else 1
    req = s.request_list[idx - 1]
    body = {"action": verb}
    if verb in ("approve", "forward"):
        body["reason"] = " ".join(args[1:]) or f"Action: {verb}"
    if verb in ("return", "reject"):
        if len(args) < 2:
            raise RuntimeError(f"`{verb}` requires a reason")
        body["reason"] = " ".join(args[1:])
    r = s.cur_h(f"/api/schools/{s.env['schools'][0]['school'].id}/requests/{req['id']}/transition", method="POST", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.json().get('detail', r.text)}")
    console.print(f"[bold]{verb}[/bold] {req['id'][:8]} -> {r.json()['state']}")


def _bell_cmd(console: Console, s: DemoSession, args: list[str]) -> None:
    action = args[0].lower() if args else "status"
    sid = s.env["schools"][0]["school"].id
    if action == "tick":
        r = s.j("platform", "/api/test/bell/tick", method="POST")
        console.print(f"Bell evaluation: {r}")
    elif action == "advance":
        seconds = int(args[1]) if len(args) > 1 else 120
        data = s.j("bell", f"/api/schools/{sid}/bell/events")
        last = data["scheduler"].get("at")
        if last:
            current = datetime.fromisoformat(last.replace("Z", "+00:00"))
        else:
            current = datetime.now(timezone.utc)
        target = current + timedelta(seconds=seconds)
        r = s._h("platform", "/api/test/clock/set", method="POST", json={"now_utc": target.isoformat()})
        console.print(f"Test clock advanced to {target.isoformat()} -> {r.json()}")
    elif action in ("pause", "resume"):
        schedules = s.j("head", f"/api/schools/{sid}/bell/schedules")
        if not schedules:
            raise RuntimeError("No bell schedule")
        r = s._h("head", f"/api/schools/{sid}/bell/schedules/{schedules[0]['id']}/{action}", method="POST")
        console.print(f"Schedule {action}d: {r.json()['name']}")
    else:
        events = s.j("bell", f"/api/schools/{sid}/bell/events")
        console.print(_bell_panel(events))


def _school_cmd(console: Console, s: DemoSession, args: list[str]) -> None:
    if s.current != "platform" and not s.whoami().get("is_platform"):
        raise RuntimeError("Tenant administration requires the platform owner. Try `session switch platform`.")
    action = args[0].lower() if args else "list"
    if action == "create":
        name = args[1] if len(args) > 1 else input("New school name: ").strip()
        if not name:
            raise RuntimeError("school create <name> [head_email]")
        head_email = args[2] if len(args) > 2 else None
        body = {"name": name, "timezone": "UTC"}
        if head_email:
            body["head_email"] = head_email
        r = s.cur_h("/api/platform/schools", method="POST", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.json().get('detail', r.text)}")
        payload = r.json()
        console.print(
            f"[green]Provisioned[/green] {payload['name']} (slug [bold]{payload['slug']}[/bold], "
            f"status {payload['status']})."
        )
        if payload.get("head_invitation_token"):
            console.print(
                "Head invited. The Director accepts by signing in and running "
                f"[bold]invitations[/bold] then [bold]accept <id>[/bold]."
            )
        else:
            console.print("Invite a Director with [bold]school invite <slug> <email>[/bold].")
    elif action == "invite":
        if len(args) < 3:
            raise RuntimeError("school invite <slug> <email>")
        slug, email = args[1], args[2]
        schools = s.j("platform", "/api/platform/schools")
        target = next((x for x in schools if x["slug"] == slug), None)
        if target is None:
            raise RuntimeError(f"Unknown school slug: {slug}")
        r = s.cur_h(f"/api/platform/schools/{target['id']}/invitations", method="POST", json={"email": email})
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.json().get('detail', r.text)}")
        inv = r.json()
        console.print(
            f"[green]Invited[/green] {inv['email']} as {inv.get('role_label', inv['role'])} to "
            f"{inv.get('school_name', slug)} (invitation {inv['id'][:8]})."
        )
    elif action in ("suspend", "reactivate"):
        slug = args[1] if len(args) > 1 else ""
        schools = s.j("platform", "/api/platform/schools")
        target = next((x for x in schools if x["slug"] == slug), None)
        if target is None:
            raise RuntimeError(f"Unknown school slug: {slug}")
        r = s.cur_h(f"/api/platform/schools/{target['id']}/{action}", method="POST")
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.json().get('detail', r.text)}")
        console.print(f"{action} -> {r.json()['status']}")
    else:
        schools = s.j("platform", "/api/platform/schools")
        console.print(_schools_panel(schools))


def _watch(console: Console, s: DemoSession, seconds: int) -> None:
    console.print(f"Live monitor for {seconds}s (Ctrl+C to stop early)…")
    from rich.live import Live

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        deadline = time_mod.time() + seconds
        while time_mod.time() < deadline:
            live.update(_dashboard_layout(s))
            time_mod.sleep(0.5)


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


# --------------------------------------------------------------------------- #
# `sacs status`
# --------------------------------------------------------------------------- #

@app.command("status")
def status_cmd(ctx: typer.Context) -> None:
    """Report whether the local SACS demonstration is running."""
    console = _console(ctx)
    settings = get_settings()
    if not settings.runtime_file.exists():
        console.print("The SACS demonstration is not running (no runtime file).")
        raise typer.Exit(0)
    runtime = json.loads(settings.runtime_file.read_text())
    pid = runtime.get("pid")
    alive = _pid_alive(int(pid))
    table = Table(box=box.SIMPLE_HEAVY, title="SACS status")
    table.add_column("")
    table.add_column("")
    table.add_row("PID", str(pid))
    table.add_row("URL", runtime.get("url", "?"))
    table.add_row("Process", "running" if alive else "not running")
    console.print(table)
    if alive:
        try:
            r = api.get(f"{runtime['url']}/api/health", timeout=3)
            console.print(f"Health: [green]{r.json()}[/green]" if r.status_code == 200 else f"Health: [red]HTTP {r.status_code}[/red]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"Health: [red]unreachable ({exc})[/red]")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _noop_db_upgrade():  # pragma: no cover
    pass


if __name__ == "__main__":
    main()