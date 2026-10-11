"""Package artifact tests: the built wheel and sdist must be complete."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


@pytest.fixture(scope="module")
def artifacts():
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob("*"):
        old.unlink()
    try:
        import build  # noqa: F401
    except ModuleNotFoundError:
        pytest.skip("python build module not installed")
    r = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(DIST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    wheels = sorted(DIST.glob("*.whl"))
    sdists = sorted(DIST.glob("*.tar.gz"))
    assert wheels, "no wheel produced"
    assert sdists, "no sdist produced"
    return wheels[0], sdists[0]


def test_wheel_contains_core_package(artifacts):
    wheel, _ = artifacts
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
    for required in (
        "sacs/app.py",
        "sacs/cli.py",
        "sacs/db.py",
        "sacs/models.py",
        "sacs/workflow.py",
        "sacs/scheduler.py",
        "sacs/routers/__init__.py",
    ):
        assert any(n.endswith(required) for n in names), f"missing {required}"


def test_wheel_contains_migrations(artifacts):
    wheel, _ = artifacts
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
    assert any("migrations/versions" in n and n.endswith(".py") for n in names)
    assert any(n.endswith("alembic.ini") for n in names)


def test_wheel_excludes_web_and_tmp(artifacts):
    wheel, _ = artifacts
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
    assert not any(n.startswith("sacs/web") for n in names)
    assert not any(".tmp" in n for n in names)


def test_metadata_names_are_correct(artifacts):
    import sacs

    wheel, _ = artifacts
    with zipfile.ZipFile(wheel) as z:
        meta_name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = z.read(meta_name).decode()
        ep_name = next(n for n in z.namelist() if n.endswith("entry_points.txt"))
        ep = z.read(ep_name).decode()
    assert "Name: sacs" in metadata
    assert f"Version: {sacs.__version__}" in metadata
    assert "sacs = sacs.cli:main" in ep


def test_console_entry_is_argless():
    """Regression: ``sacs`` script calls main() with no arguments."""
    from sacs import cli

    assert callable(cli.main)
    import inspect

    sig = inspect.signature(cli.main)
    assert not sig.parameters or all(p.default is not inspect.Parameter.empty for p in sig.parameters.values())