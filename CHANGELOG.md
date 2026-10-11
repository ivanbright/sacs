# Changelog

All notable changes to SACS are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] - 2026-10-11

### Added
- **Invitation lifecycle (Admin -> Director -> Teacher)**. Persisted, single-use,
  expiring invitations tie an email to a school and a role and are accepted by the
  named recipient. New endpoints: `GET /api/invitations/mine`,
  `POST /api/invitations/accept` (signed-in recipient) and
  `POST /api/invitations/accept-token` (anonymous, token + new password);
  `POST/GET /api/schools/{id}/invitations` and `.../{invitation_id}/revoke` for
  Directors; `POST /api/platform/schools/{id}/invitations` for the platform owner.
  Only the SHA-256 hash of a token is stored.
- **CLI: explicit identity switching and contextual prompt**. `session list`,
  `session switch <email>`, and a prompt (`sacs[role:email/school]>`) that always
  makes the acting identity visible. New commands: `whoami`, `invitations`,
  `invite <email> <role>`, `accept <id>`, and `school create <name> [head_email]` /
  `school invite <slug> <email>`.
- **CLI: in-product help**. `help`, `help <command>`, and `examples` describe the
  workflows and show a runnable Admin -> Director -> Teacher example.
- **Directors review requests**. `school_head` now holds `request.review`; a submitted
  request is visible to both `dos` and `school_head` and can be reviewed/forwarded or
  finally decided by the Director.
- **Pending sessions**. A user who has not yet accepted any invitation can sign in
  without a school context; the session can list and accept invitations but holds no
  school permissions until a membership exists.
- **Seeded candidate identities** (`newdirector@demo.sacs`, `newteacher@demo.sacs`)
  so the invitation lifecycle can be demonstrated end to end.
- **Tests**: invitation lifecycle, teacher own-scope workflow, and CLI resilience
  (dashboard survives a failing backend; command dispatch and help).

### Changed
- `sacs demo` dashboard renders each panel defensively: a single failing tenant,
  role view, or endpoint shows a friendly "no access or unavailable" message instead
  of blanking the whole dashboard, and the header names the acting identity.

### Fixed
- **Demo dashboard crashed on startup (three linked defects)**. The teacher workspace
  triggered `403 Role 'teacher' lacks permission 'request.view.school'`; after that was
  fixed the platform panel raised `KeyError: 'scheduler'`, and rendering a request row then
  raised a Rich `MarkupError` from an opening tag with a stray leading space. All three are
  corrected, so `sacs demo` now renders its live dashboard.
- **Teachers could not view their own completion requests**: the request list/detail
  endpoints required `request.view.school`, which the `teacher` role does not hold.
  Teachers now hold `request.view.own` and are scoped to their own requests in list and
  detail; `school_head`/`dos` keep `request.view.school` and `bell_monitor` remains denied.
- **Dashboard read the wrong health endpoint**: the platform panel now reads
  `/api/platform/health` (which reports scheduler and database state) instead of the public
  `/api/health`.
- **Inline prompt markup** no longer leaks Rich markup (`[bold cyan]sacs>`) into the input
  line, and the state label no longer emits a malformed closing tag.

## [0.1.1] - 2026-10-11

### Fixed
- **Console script crash on launch**: the `sacs` entry point called the Typer callback
  directly with no arguments; a dedicated no-argument `main()` dispatcher is now the
  console-script target (`sacs`, `sacs --help`, `sacs demo`, … now start correctly).

## [0.1.0] - 2026-10-11

### Added
- **Terminal-first product**: `sacs demo` runs the full platform locally — migrate, seed,
  serve, authenticate five identities, and drive the product from a live Rich dashboard.
  No browser frontend.
- **Multi-tenant model**: two seeded demonstration schools
  (`northstar-academy`, `riverside-institute`), per-school memberships, roles
  (`platform_admin`, `school_head`, `dos`, `teacher`, `bell_monitor`), opaque session
  tokens, PBKDF2 passwords, and server-side tenant + role checks on every endpoint.
- **Lesson-completion workflow**: a strictly enforced state machine
  (`submitted → under_dos_review → forwarded_to_head → approved/rejected`,
  plus `returned_for_correction`), with full action history and audit trail.
- **Bell scheduler**: per-school schedules evaluated in the school time zone, a
  deterministic test clock, and `fired`/`missed`/`skipped` outcomes de-duplicated by a
  unique `(schedule, occurrence)` constraint so restarts can never double-ring.
- **Real-time events**: transactional outbox + authenticated WebSocket delivery with
  server-side audience filtering; pending outbox is re-drained on restart.
- **Alembic-only schema management**: initial baseline migration creates all 19 tables.
- **Platform administration**: tenant provisioning, invitations, suspend/reactivate, health,
  audit, and scheduler status.
- **CLI commands**: `sacs demo`, `sacs demo reset`, `sacs demo stop`, `sacs status`,
  `sacs serve`, `sacs check`, `sacs version`.
- **Test suite**: workflow, auth/RBAC, tenant isolation, bell determinism/dedup, outbox,
  WebSocket auth, CLI subprocess, and package-artifact tests.