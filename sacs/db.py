"""Database engine, sessions, and migration execution.

The local demonstration uses SQLite; deployed environments use PostgreSQL. The
schema is always created through Alembic migrations - never by an implicit
``create_all`` at import time.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def make_engine(url: str, echo: bool = False) -> Engine:
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_fk(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings().database_url)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _sessionmaker


def configure_database(url: str, *, run_migrations: bool = True, echo: bool = False) -> Engine:
    """Point the process at a database and (by default) upgrade it to head."""
    global _engine, _sessionmaker
    _engine = make_engine(url, echo=echo)
    _sessionmaker = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    if run_migrations:
        upgrade_database(_engine)
    return _engine


def reset_database_state() -> None:
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #

def _alembic_config(url: str):
    from alembic.config import Config

    migrations_dir = Path(__file__).parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.set_main_option("prepend_sys_path", ".")
    return cfg


def upgrade_database(engine: Engine) -> None:
    """Run Alembic migrations up to head against ``engine``."""
    from alembic import command

    cfg = _alembic_config(str(engine.url))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def stamp_database(engine: Engine, revision: str = "head") -> None:
    from alembic import command

    cfg = _alembic_config(str(engine.url))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.stamp(cfg, revision)


def create_all_for_tests(engine: Engine) -> None:
    """Create the schema directly from metadata (test bootstrap only)."""
    Base.metadata.create_all(engine)
