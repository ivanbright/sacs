"""Bell scheduler tests using the deterministic test clock.

Counting is done per schedule because the seeded demonstration schedules also
evaluate against the controlled clock; isolating by ``schedule_id`` keeps the
assertions deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from tests.conftest import auth_headers, school_a


def _create_schedule(client, identities, name="Test periods", start="08:00", end="08:45"):
    sid = school_a(identities).id
    r = client.post(
        f"/api/schools/{sid}/bell/schedules",
        headers=auth_headers(identities, "headA"),
        json={
            "name": name,
            "timezone": "UTC",
            "periods": [{"label": "P1", "start": start, "end": end}],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _events_for_schedule_id(client, identities, schedule_id, asof=None):
    sid = school_a(identities).id
    r = client.get(f"/api/schools/{sid}/bell/events", headers=auth_headers(identities, "headA"))
    assert r.status_code == 200, r.text
    rows = [e for e in r.json()["events"] if e["schedule_id"] == schedule_id]
    if asof:
        rows = [e for e in rows if e["occurrence_key"].startswith(asof)]
    return rows


def _tick(client, at_utc):
    r = client.post("/api/test/clock/set", json={"now_utc": at_utc.isoformat()})
    assert r.status_code == 200, r.text
    r = client.post("/api/test/bell/tick")
    assert r.status_code == 200, r.text
    return r.json()


def test_fires_within_grace(client, identities):
    schedule = _create_schedule(client, identities, start="10:00", end="10:45")
    reference = datetime(2026, 3, 2, 10, 0, 30, tzinfo=timezone.utc)
    _tick(client, reference)
    events = _events_for_schedule_id(client, identities, schedule["id"], asof="2026-03-02")
    assert [e["outcome"] for e in events] == ["fired"]


def test_misses_after_grace(client, identities):
    schedule = _create_schedule(client, identities, name="Old", start="10:00", end="10:45")
    reference = datetime(2026, 3, 2, 13, 30, tzinfo=timezone.utc)
    _tick(client, reference)
    events = _events_for_schedule_id(client, identities, schedule["id"], asof="2026-03-02")
    assert [e["outcome"] for e in events] == ["missed", "missed"]


def test_no_duplicates_on_second_tick(client, identities):
    schedule = _create_schedule(client, identities, start="10:00", end="10:45")
    reference = datetime(2026, 3, 2, 10, 0, 30, tzinfo=timezone.utc)
    _tick(client, reference)
    _tick(client, reference)
    events = _events_for_schedule_id(client, identities, schedule["id"], asof="2026-03-02")
    assert len(events) == 1
    assert all(e["outcome"] == "fired" for e in events)


def test_paused_schedule_skipped(client, identities):
    schedule = _create_schedule(client, identities, start="10:00", end="10:45")
    sid = school_a(identities).id
    r = client.post(
        f"/api/schools/{sid}/bell/schedules/{schedule['id']}/pause",
        headers=auth_headers(identities, "headA"),
    )
    assert r.status_code == 200
    _tick(client, datetime(2026, 3, 2, 10, 0, 30, tzinfo=timezone.utc))
    events = _events_for_schedule_id(client, identities, schedule["id"], asof="2026-03-02")
    assert [e["outcome"] for e in events] == ["skipped"]  # only the start period is due at 10:00:30 UTC


def test_unique_occurrence_keys(client, identities, factory):
    from sacs.models import BellEvent

    schedule = _create_schedule(client, identities, start="10:00", end="10:45")
    _tick(client, datetime(2026, 3, 2, 10, 0, 30, tzinfo=timezone.utc))
    _tick(client, datetime(2026, 3, 2, 10, 1, 0, tzinfo=timezone.utc))
    _tick(client, datetime(2026, 3, 2, 22, 0, 0, tzinfo=timezone.utc))

    events = _events_for_schedule_id(client, identities, schedule["id"], asof="2026-03-02")
    assert len(events) == 2  # start fired, end missed once the evening tick passes it
    assert {e["outcome"] for e in events} == {"fired", "missed"}

    db = factory()
    pairs = list(db.execute(select(BellEvent.schedule_id, BellEvent.occurrence_key)).all())
    db.close()
    unique = {(s, k) for s, k in pairs}
    assert len(pairs) == len(unique)


def test_test_clock_disabled_in_prod(client, identities):
    from sacs.config import override_settings

    override_settings(env="prod", home="", database_url="", enable_test_clock=False)
    r = client.post("/api/test/clock/set", json={"now_utc": "2026-03-02T10:10:00Z"})
    assert r.status_code == 403