"""SACS command-line interface.

``sacs start`` opens the interactive terminal app through which an
Administrator, Director (school head), or Teacher signs in and works. The
command migrates the database, optionally seeds demonstration data when the
database is empty, and starts the shared SACS service in-process.

``sacs serve`` runs the same service in the foreground without the interactive
app, and ``sacs status`` reports whether a service is already running.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time as time_mod

import httpx
import typer
from rich import box
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import get_settings
from .db import configure_database, get_sessionmaker

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=(
        "SACS - Smart Academic Coordination System.\n\n"
        "Run the whole multi-tenant school platform locally from your terminal:\n\n"
        "[bold]sacs start[/bold]   start the interactive terminal app (Administrator / Director / Teacher)\n\n"
        "Other commands: [bold]check[/bold], [bold]serve[/bold], "
        "[bold]status[/bold], [bold]version[/bold]."
    ),
)
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
    """Verify the local runtime can run the SACS service."""
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
        "\n[green]Ready for `sacs start`.[/green]"
        if good
        else "\n[red]Resolve the failures above before running SACS.[/red]"
    )
    raise typer.Exit(code=0 if good else 1)


@app.command("serve")
def serve(ctx: typer.Context) -> None:
    """Start the SACS service in the foreground (no seeding or interactive app)."""
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


@app.command("start")
def start(
    ctx: typer.Context,
    seed: bool = typer.Option(
        True, "--seed/--no-seed", help="Seed demonstration data when the database is empty."
    ),
) -> None:
    """Open the interactive terminal app (Administrator / Director / Teacher)."""
    from . import seed as seed_mod
    from .app_menu import run as run_app
    from .models import School
    from .ui import Console as UiConsole

    console = _console(ctx)
    _init_db(console)
    if seed:
        db = get_sessionmaker()()
        try:
            if db.query(School).count() == 0:
                seed_mod.seed_all(db)
                console.print("[green]Seeded demonstration data.[/green]")
        finally:
            db.close()
    run_app(UiConsole())


# --------------------------------------------------------------------------- #
# Service bootstrap
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
    raise typer.Exit("The SACS service did not become healthy in time. Check `sacs status`.")


@app.command("status")
def status_cmd(ctx: typer.Context) -> None:
    """Report whether a local SACS service is running."""
    console = _console(ctx)
    settings = get_settings()
    if not settings.runtime_file.exists():
        console.print("The SACS service is not running (no runtime file).")
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
            console.print(
                f"Health: [green]{r.json()}[/green]"
                if r.status_code == 200
                else f"Health: [red]HTTP {r.status_code}[/red]"
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"Health: [red]unreachable ({exc})[/red]")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    main()
