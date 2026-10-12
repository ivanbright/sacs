# SACS — Smart Academic Coordination System

A real-time, multi-tenant school coordination platform that runs entirely from the terminal.
SACS coordinates **schools** (tenants), **staff**, **timetables**, **lesson sessions**, a
**lesson-completion approval workflow**, and a deterministic **software bell scheduler**,
streaming every event to authenticated workspaces over WebSockets.

There is no browser frontend: the whole product is exercised through the `sacs` CLI, which
talks to one shared FastAPI backend exactly like a real client would.

---

## Quick start

```bash
pip install sacs          # then:
sacs start                # migrate, seed if empty, open the interactive terminal app
sacs serve                # start the service in the foreground (no interactive app)
sacs status               # show whether a local service is running
sacs check                # verify dependencies and running environment
sacs version
```

`sacs start` is the product entry point. It opens the interactive, numbered-menu terminal
application for the three product roles (Platform Administrator, Director, Teacher). It
talks to the shared backend through an in-process client, so every action is authorized by
the server. `0` always returns to the immediate parent menu; the main menu uses
`0. Logout` and login uses `0. Exit`.

On first run the database is migrated to the latest schema (Alembic) and, when empty, seeded
with two schools so there is something to sign in to. Existing data persists between restarts.

### Sign-in identities

The seed ships these sample identities. All use the password **`SacsDemo123!`**.

| Role           | Email                          | What they can do                                   |
| -------------- | ------------------------------ | -------------------------------------------------- |
| Platform admin | `platform@demo.sacs`           | create/suspend/reactivate schools, health, audit   |
| School head    | `head.northstar@demo.sacs`     | final approve/reject, staff & academics, bells     |
| Director of studies | `dos.northstar@demo.sacs` | review, forward, return requests; timetable        |
| Teacher        | `teacher.northstar@demo.sacs`  | submit completion requests for own lessons         |
| Bell monitor   | `bell.northstar@demo.sacs`     | monitor bell events in real time                   |

(Pair for `riverside-institute`: `head.riverside@demo.sacs`, `dos.riverside@demo.sacs`,
`teacher.riverside@demo.sacs`, `bell.riverside@demo.sacs`.)

---

## Identities and invitations

The product supports three primary roles — **Admin** (platform owner), **Director**
(`school_head`, the final academic authority) and **Teacher** — wired together by a persisted
invitation lifecycle. The seeded demo also ships two internal demo identities (Director of
Studies and bell monitor) used by the pre-existing workflow and to demonstrate read-only
access; they are not part of the invitation flow.

* The **Admin** provisions a school and invites its **Director** from **Manage Schools →
  Register new school** (with a Director email) or **Manage Schools → Invite Director**.
  A school stays in *provisioning* until its Director accepts.
* A **Director** invites **Teachers** from **Teachers and Staff → Invite teacher**.
* The recipient uses **Accept invitation with token** on the sign-in screen, pastes the token,
  and chooses their own password and display name — the membership is created server-side.
  Invitations are single-use, expire after 14 days, can be revoked, and store only the
  SHA-256 hash of their token.

Staff created directly by an administrator receive a temporary password and are required to
change it the first time they sign in. Any signed-in user can change their own password from
**Account → Change password**.

---

## The lesson-completion workflow

A teacher submits a completion request for one of their published lessons. The request then
moves through a strictly enforced state machine — invalid transitions are rejected
server-side:

```
submitted ──review──> under_dos_review
submitted ──forward─> forwarded_to_head
submitted ──return──> returned_for_correction
under_dos_review ──forward─> forwarded_to_head
under_dos_review ──return──> returned_for_correction
forwarded_to_head ──approve─> approved
forwarded_to_head ──reject──> rejected
returned_for_correction ──submit─> submitted
```

Every transition records an audit action and the requester-facing history. A secondary-level
request may not be final-approved: `request.decide` (head) and `request.review` (DOS) are
deliberately disjoint.

## Roles and permissions

| Permission             | school_head | dos | teacher | bell_monitor |
| ---------------------- | :-: | :-: | :-: | :-: |
| `school.view`          | ✓ | ✓ | ✓ | ✓ |
| `school.settings.manage` | ✓ |   |   |   |
| `staff.manage`         | ✓ |   |   |   |
| `academics.manage`     | ✓ | ✓ |   |   |
| `timetable.manage`     | ✓ | ✓ |   |   |
| `timetable.publish`    | ✓ |   |   |   |
| `request.submit`       |   |   | ✓ |   |
| `request.review`       |   | ✓ |   |   |
| `request.decide`       | ✓ |   |   |   |
| `bell.view`            | ✓ | ✓ | ✓ | ✓ |
| `bell.manage`          | ✓ |   |   |   |
| `audit.view.school`    | ✓ |   |   |   |

Platform permissions (`platform.schools.manage`, `platform.schools.suspend`,
`platform.health.view`, `platform.audit.view`) are exclusive to platform administrators and
never grant academic approval authority.

## Bell scheduler

Each school runs `BellSchedule`s with per-period times evaluated in the school's own time
zone (`Europe/London`, `America/New_York`, …). A background loop ticks once per second; each
tick records every due-but-unrecorded occurrence with outcome:

- **fired** — executed inside the grace window (`SACS_BELL_GRACE_SECONDS`, default 90);
- **missed** — the window elapsed while the scheduler was offline;
- **skipped** — the schedule was paused;
- occurrences are keyed `schedule_id + day + period + (start|end)` with a unique constraint,
  so restarts and overlapping ticks can never double-fire the school bell.

### Deterministic test clock

In non-production environments the clock can be driven for testing and demonstrations:

```
POST /api/test/clock/set   { "now_utc": "2026-03-02T10:00:30+00:00" }
POST /api/test/bell/tick
POST /api/test/clock/clear
```

The test clock is hard-disabled in production unless explicitly enabled.

## Real-time events

Every state change is written to a transactional **outbox** in the same database commit as
the change itself, then drained and streamed to the exact recipients allowed server-side by
the event audience rule (`platform` | school roles | specific users). This guarantees a
persisted change never silently loses its notification, and gives tests a synchronous
delivery path.

## Architecture

- **API**: FastAPI + Pydantic v2, packaged with uvicorn.
- **Persistence**: SQLAlchemy 2.0 (typed `Mapped` models); schema only ever created through
  Alembic migrations — no implicit `create_all`.
- **Auth**: opaque hashed session tokens, PBKDF2 password hashing, per-school memberships.
- **Tenant isolation**: every school-scoped endpoint resolves the caller's membership and
  re-checks the requested `school_id` server-side; a suspended school blocks all its members.
- **Time handling**: all timestamps are UTC; per-school time zones applied only for bell
  scheduling and local display (`tzdata` included on Windows).

## Configuration

Everything is configurable through environment variables (see `sacs/config.py`):

| Variable                  | Default                                   |
| ------------------------- | ----------------------------------------- |
| `SACS_HOME`               | `~/.sacs` (demo database lives here)      |
| `SACS_DATABASE_URL`       | `sqlite:///<SACS_HOME>/sacs.db`           |
| `SACS_ENV`                | `demo` (`demo`/`dev`/`prod`)              |
| `SACS_HOST` / `SACS_PORT` | `127.0.0.1` / `0` (auto-pick a free port) |
| `SACS_ENABLE_TEST_CLOCK`  | `true` (`false` forced in `prod`)         |
| `SACS_SESSION_TTL_HOURS`  | `12`                                       |
| `SACS_BELL_GRACE_SECONDS` | `90`                                       |

Deployments can point `SACS_DATABASE_URL` at PostgreSQL (see the `postgres` extra) and run
`uvicorn sacs.app:create_app --factory`.

## Development

```bash
pip install -e ".[dev]"
pytest                      # full test suite
pytest tests/test_packaging.py   # build wheel/sdist and inspect artifacts
python -m build --no-isolation --outdir dist
```

## Repository layout

```
sacs/
  cli.py          Typer CLI (`start`, `serve`, `status`, `check`, `version`)
  app_menu.py     interactive terminal application (numbered-menu, all roles)
  ui.py           terminal presentation layer (colours, menus, navigation)
  api_client.py   in-process API client used by the terminal app
  audio.py        bell sound synthesis + best-effort playback
  app.py          FastAPI application and lifespan tasks
  models.py       SQLAlchemy models (19 tables)
  db.py           engines, sessions, Alembic wiring
  security.py     password + session token handling
  rbac.py         permission catalog for roles and platform
  workflow.py     the completion-request state machine
  scheduler.py    deterministic clock + bell loop and outcomes
  events.py       connection manager, transactional outbox, dispatcher
  seed.py         idempotent sample seed (two schools, five identities)
  migrations/     Alembic baseline migration
  routers/        platform, schools, academics, timetable, requests, invitations, bell, notifications, realtime, auth
tests/            unit + API + isolation + scheduler + terminal + packaging tests
```