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

app = typer.Typer(add_completion=False, no_args_is_help=True)
demo_app = typer.Typer(no_args_is_help=False)
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
    """Five authenticated identities over one shared service."""

    def __init__(self, base: str, env: dict):
        self.base = base
        self.tokens: dict[str, str] = {}
        self.env = env
        self.lesson_list: list[dict] = []
        self.request_index: dict[str, int] = {}
        self.request_list: list[dict] = []

    def auth(self, role: str, email: str, password: str = "SacsDemo123!", school_slug: str | None = None) -> None:
        sid = None
        if school_slug:
            s = next((x for x in self.env["schools"] if x["school"].slug == school_slug), None)
            sid = s["school"].id if s else None
        self.tokens[role] = _login(self.base, email, password, sid)

    def _h(self, role: str, path: str, method="GET", **kwargs):
        headers = {"Authorization": f"Bearer {self.tokens[role]}"}
        fn = getattr(api, method.lower())
        resp = fn(f"{self.base}{path}", headers=headers, **kwargs)
        return resp

    def j(self, role, path, **kwargs):
        r = self._h(role, path, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.json().get('detail', r.text)}")
        return r.json()


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

    session = DemoSession(runtime["url"], env)
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
            _dashboard(console, s)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Dashboard error: {exc}[/red]")
        try:
            cmd = input("[bold cyan]sacs>[/bold cyan] ").strip()
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


def _dashboard_layout(s: DemoSession) -> Layout:
    platform = s.j("platform", "/api/platform/schools")
    health = s.j("platform", "/api/health")
    school_id = s.env["schools"][0]["school"].id
    school_overview = s.j("head", f"/api/schools/{school_id}/overview")
    requests = s.j("dos", f"/api/schools/{school_id}/requests")
    teacher_reqs = s.j("teacher", f"/api/schools/{school_id}/requests")
    bell = s.j("bell", f"/api/schools/{school_id}/bell/events")

    layout = Layout(name="root")
    layout.split(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
    )
    layout["header"].update(Text(f"SACS workspaces - one shared system - {s.base}", style="bold cyan"))

    top = Layout()
    mid = Layout()
    btm = Layout()
    layout["body"].split_column(top, Layout(size=1), mid, Layout(size=1), btm)
    top.split_row(Layout(name="platform"), Layout(name="tenants"))
    mid.split_row(Layout(name="head"), Layout(name="dos"))
    btm.split_row(Layout(name="teacher"), Layout(name="bell"))

    layout["platform"].update(_platform_panel(health, platform))
    layout["tenants"].update(_schools_panel(platform))
    layout["dos"].update(_queue_panel("DOS - Incoming requests", requests, "dos"))
    layout["teacher"].update(_queue_panel("Teacher - My requests", teacher_reqs, "teacher"))
    layout["head"].update(_head_panel(school_overview, requests))
    layout["bell"].update(_bell_panel(bell))
    return layout


def _platform_panel(health: dict, _schools) -> Panel:
    table = Table(show_header=False, box=None)
    table.add_column("")
    table.add_column("")
    table.add_row("Scheduler", health["scheduler"]["clock"])
    table.add_row("DB", health["database"])
    table.add_row("Version", f"sacs {health['version']}")
    return Panel(table, title="[bold]1. Platform Owner[/bold]", border_style="cyan")


def _schools_panel(schools: list[dict]) -> Panel:
    table = Table(box=box.SIMPLE)
    table.add_column("School")
    table.add_column("Status")
    for school in schools:
        table.add_row(school["name"], f"[{ 'yellow' if school['status']=='suspended' else 'green' }]{school['status']}[/]")
    return Panel(table, title="[bold]Tenants[/bold]")


def _queue_panel(title: str, requests: list[dict], kind: str) -> Panel:
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


def _head_panel(overview: dict, requests: list[dict]) -> Panel:
    pending = [r for r in requests if r["state"] == "forwarded_to_head"]
    text = Text()
    text.append(f"Pending requests for final decision: {len(pending)}", style="yellow")
    text.append(f"\nLessons today: {overview.get('lessons_today', 0)}")
    text.append(f"\nTimetable entries: {overview.get('timetable_entries', 0)}")
    return Panel(text, title="[bold]2. School Head[/bold]", border_style="magenta")


def _bell_panel(bell: dict) -> Panel:
    table = Table(box=box.SIMPLE)
    table.add_column("Event")
    table.add_column("Scheduled")
    table.add_column("Outcome")
    for e in bell["events"][:8]:
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
    return f"[{' ' + colors.get(state, 'white')}]{state}[/{colors.get(state, 'white')}]"


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #

def _handle(console: Console, s: DemoSession, verb: str, args: list[str]) -> None:
    sid = s.env["schools"][0]["school"].id
    if verb == "help":
        _help(console)
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
    else:
        console.print(f"Unknown command: {verb}  (try [bold]help[/bold])")


def _help(console: Console) -> None:
    table = Table(box=box.SIMPLE_HEAVY, title="SACS demo commands")
    table.add_column("Command")
    table.add_column("Meaning")
    commands = [
        ("lessons", "List the teacher's assigned published lessons"),
        ("submit <n> [note]", "Submit a lesson-completion request (as Teacher)"),
        ("requests", "List all requests for the school (DOS/Head view)"),
        ("review <n>", "DOS takes a request under review"),
        ("forward <n> [reason]", "DOS forwards a reviewed request to the head"),
        ("return <n> <reason>", "DOS returns a request for correction"),
        ("approve <n> [reason]", "School Head final approval"),
        ("reject <n> <reason>", "School Head final rejection"),
        ("bell", "Show scheduler status and latest events"),
        ("bell tick", "Force the scheduler to evaluate now"),
        ("bell advance <seconds>", "Advance the test clock and fire the next event"),
        ("bell pause | resume", "Pause / resume the school bell schedule"),
        ("school create <name>", "Platform Owner provisions a new tenant"),
        ("school suspend <slug> | reactivate <slug>", "Platform Owner tenant control"),
        ("watch [seconds]", "Live auto-refreshing dashboard"),
        ("accounts", "Show demo identities"),
        ("quit", "Stop the demonstration"),
    ]
    for c, m in commands:
        table.add_row(f"[bold]{c}[/bold]", m)
    console.print(table)


def _accounts(console: Console, s: DemoSession) -> None:
    _welcome(console, {"url": s.base}, s.env["schools"][0])


def _lessons(console: Console, s: DemoSession) -> None:
    sid = s.env["schools"][0]["school"].id
    lessons = s.j("teacher", f"/api/schools/{sid}/timetable/lessons?date_from=" + _today())
    s.lesson_list = lessons
    table = Table(title="Assigned lessons (Teacher workspace)", box=box.SIMPLE_HEAVY)
    table.add_column("n")
    table.add_column("Date")
    table.add_column("Time")
    table.add_column("Subject")
    table.add_column("Class")
    table.add_column("Room")
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
    r = s._h("teacher", f"/api/schools/{s.env['schools'][0]['school'].id}/requests", method="POST", json={"lesson_session_id": lesson["id"], "note": note})
    console.print(f"Submitted request -> [bold]{r.json()['state']}[/bold] (id {r.json()['id'][:8]})")


def _requests(console: Console, s: DemoSession) -> None:
    sid = s.env["schools"][0]["school"].id
    reqs = s.j("dos", f"/api/schools/{sid}/requests")
    s.request_list = reqs
    table = Table(title="Requests (school view)", box=box.SIMPLE_HEAVY)
    table.add_column("n")
    table.add_column("id")
    table.add_column("Subject/Date")
    table.add_column("State")
    table.add_column("Updated")
    for i, r in enumerate(reqs, 1):
        lesson = r.get("lesson") or {}
        table.add_row(str(i), r["id"][:8], f"{lesson.get('date')} {lesson.get('start', '')[:16]}", _state(r["state"]), r["updated_at"][:19])
    console.print(table)


def _acts(console: Console, s: DemoSession, verb: str, args: list[str]) -> None:
    if not s.request_list:
        _requests(console, s)
    idx = int(args[0]) if args else 1
    req = s.request_list[idx - 1]
    body = {"action": verb}
    if verb in ("approve", "forward"):
        body["reason"] = " ".join(args[1:]) or f"Action: {verb}"
    if verb in ("return", "reject"):
        if len(args) < 2:
            raise RuntimeError(f"`{verb}` requires a reason")
        body["reason"] = " ".join(args[1:])
    role = {"approve": "head", "reject": "head", "review": "dos", "forward": "dos", "return": "dos"}[verb]
    r = s._h(role, f"/api/schools/{s.env['schools'][0]['school'].id}/requests/{req['id']}/transition", method="POST", json=body)
    resp = r.json() if r.status_code == 200 else {"detail": r.json().get("detail")}
    console.print(f"[bold]{verb}[/bold] {req['id'][:8]} -> {resp.get('state', resp.get('detail'))}")


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
    action = args[0].lower() if args else "list"
    if action == "create":
        name = " ".join(args[1:])
        if not name:
            raise RuntimeError("school create <name>")
        r = s._h("platform", "/api/platform/schools", method="POST", json={"name": name, "timezone": "UTC"})
        payload = r.json()
        if r.status_code == 201 and payload.get("status") == "provisioning":
            console.print(f"[yellow]Provisioned {name} (slug {payload['slug']}). Awaiting school-head invitation acceptance.[/yellow]")
        else:
            console.print(payload)
    elif action in ("suspend", "reactivate"):
        slug = args[1] if len(args) > 1 else ""
        schools = s.j("platform", "/api/platform/schools")
        target = next((x for x in schools if x["slug"] == slug), None)
        if target is None:
            raise RuntimeError(f"Unknown school slug: {slug}")
        r = s._h("platform", f"/api/platform/schools/{target['id']}/{action}", method="POST")
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