"""CLI smoke tests executed through the real entrypoint in a subprocess."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*args, cwd=ROOT, env_extra=None):
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(ROOT))
    env["SACS_HOME"] = tempfile.mkdtemp()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "sacs.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_version_subcommand():
    r = _run("version")
    assert r.returncode == 0, r.stderr
    assert "0.1.0" in r.stdout


def test_help_lists_demo():
    r = _run("--help")
    assert r.returncode == 0
    assert "demo" in r.stdout


def test_demo_group_help():
    r = _run("demo", "--help")
    assert r.returncode == 0
    assert "reset" in r.stdout
    assert "stop" in r.stdout


def test_check_reports_ok_without_server():
    r = _run("check")
    assert r.returncode == 0, r.stderr
    assert "ready" in r.stdout.lower() or "ok" in r.stdout.lower()