"""Runtime configuration.

Configuration is resolved from environment variables so the same codebase
runs the SQLite-backed local demonstration and a deployed PostgreSQL service.

Environment variables
---------------------
SACS_HOME              Directory for the local demo state (default ``~/.sacs``).
SACS_DATABASE_URL      SQLAlchemy database URL. Defaults to SQLite in SACS_HOME.
SACS_ENV               ``demo`` | ``dev`` | ``prod`` (default ``demo``).
SACS_HOST              Bind host (default ``127.0.0.1``).
SACS_PORT              Bind port (default ``0`` -> auto-select a free port).
SACS_CORS_ORIGINS      Comma separated allowed origins (deployed environments).
SACS_ENABLE_TEST_CLOCK Enable the deterministic test clock + scheduler tick API.
SACS_SESSION_TTL_HOURS Session lifetime in hours (default ``12``).
SACS_BELL_GRACE_SECONDS Grace window for late bell execution (default ``90``).
SACS_BELL_AUDIO        Play the bell sound when a bell fires (default ``true``).
SACS_BELL_SOUND        Optional path to a custom WAV bell sound.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_home() -> Path:
    return Path(os.environ.get("SACS_HOME") or (Path.home() / ".sacs")).expanduser()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Settings:
    home: Path = field(default_factory=_default_home)
    env: str = field(default_factory=lambda: os.environ.get("SACS_ENV", "demo"))
    host: str = field(default_factory=lambda: os.environ.get("SACS_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("SACS_PORT", "0")))
    database_url: str = field(default_factory=lambda: os.environ.get("SACS_DATABASE_URL", ""))
    cors_origins: list[str] = field(
        default_factory=lambda: _split_csv(os.environ.get("SACS_CORS_ORIGINS"))
    )
    enable_test_clock: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("SACS_ENABLE_TEST_CLOCK"), True)
    )
    session_ttl_hours: int = field(
        default_factory=lambda: int(os.environ.get("SACS_SESSION_TTL_HOURS", "12"))
    )
    bell_grace_seconds: int = field(
        default_factory=lambda: int(os.environ.get("SACS_BELL_GRACE_SECONDS", "90"))
    )
    bell_audio: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("SACS_BELL_AUDIO"), True)
    )
    bell_sound: str = field(default_factory=lambda: os.environ.get("SACS_BELL_SOUND", ""))

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser()
        if not self.database_url:
            self.database_url = f"sqlite:///{(self.home / 'sacs.db').as_posix()}"
        if self.env == "prod" and self.enable_test_clock:
            # Never expose the deterministic clock in production by default.
            self.enable_test_clock = _as_bool(os.environ.get("SACS_ENABLE_TEST_CLOCK"), False)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        return self.home

    @property
    def pid_file(self) -> Path:
        return self.home / "demo.pid"

    @property
    def runtime_file(self) -> Path:
        return self.home / "demo.runtime.json"

    @property
    def log_file(self) -> Path:
        return self.home / "demo.log"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def override_settings(**kwargs) -> Settings:
    """Replace the settings singleton (used by tests and the CLI)."""
    global _settings
    _settings = Settings(**kwargs)
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
