"""Shared pytest fixtures: an isolated database, seed data, and API clients."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# The bell scheduler must never emit real audio during the test run.
os.environ.setdefault("SACS_BELL_AUDIO", "0")

from sacs import seed as seed_mod
from sacs.app import create_app
from sacs.config import override_settings, reset_settings
from sacs.db import configure_database, get_sessionmaker, reset_database_state

PW = seed_mod.DEMO_PASSWORD


@pytest.fixture()
def env(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    override_settings(
        home=tmp_path,
        env="test",
        database_url=url,
        enable_test_clock=True,
        session_ttl_hours=1,
    )
    configure_database(url, run_migrations=True)
    factory = get_sessionmaker()
    db = factory()
    env = seed_mod.seed_all(db)
    db.close()
    env["db_url"] = url
    try:
        yield env
    finally:
        from sacs import scheduler

        scheduler.clear_clock()
        reset_database_state()
        reset_settings()


@pytest.fixture()
def factory(env):
    return get_sessionmaker()


@pytest.fixture()
def client(env):
    app = create_app()
    return TestClient(app)


@pytest.fixture()
def identities(client, env):
    out = {}

    def login(email, school_id=None):
        r = client.post(
            "/api/auth/login",
            json={"email": email, "password": PW, "school_id": school_id},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def H(token):
        return {"Authorization": f"Bearer {token}"}

    out["platform"] = login(seed_mod.PLATFORM_EMAIL)

    a = env["schools"][0]
    b = env["schools"][1]
    for label, key in (
        ("head", "head_user"),
        ("dos", "dos_user"),
        ("teacher", "teacher_user"),
        ("bell", "bell_user"),
    ):
        out[f"headA" if label == "head" else f"{label}A"] = login(a[key].email, a["school"].id)
        out[f"{label}B"] = login(b[key].email, b["school"].id)

    out["_a"] = a
    out["_b"] = b
    out["_H"] = H
    out["_login"] = login
    return out


def auth_headers(identities, key: str) -> dict:
    return identities["_H"](identities[key]["token"])


def school_a(identities):
    return identities["_a"]["school"]


def school_b(identities):
    return identities["_b"]["school"]


@pytest.fixture()
def bell_rules(factory):
    from sacs import scheduler

    return scheduler