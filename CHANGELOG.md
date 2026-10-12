# Changelog

All notable changes to SACS are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.5] - 2026-10-12

### Added
- **Platform admin member control.** New endpoints `GET /api/platform/schools/{id}/members`
  and `PATCH /api/platform/schools/{id}/members/{membership_id}` let the Administrator list
  every member of any school and deactivate/reactivate them. The **Manage Schools** menu gains
  *School members*, *Deactivate member* and *Reactivate member*.

### Fixed
- **Suspend/Reactivate school listed every school.** *Reactivate school* now only lists
  `suspended` schools and *Suspend school* only lists `active` ones, so you can no longer
  pick an active school and get a no-op "is now active" message.

## [0.2.4] - 2026-10-12

### Added
- **Director of Studies role end to end.** A dedicated "Director of Studies" sign-in option
  and a role-specific menu (Review Lesson Requests, Timetable, Classes, Subjects, Classrooms,
  School Bell) matching the `dos` permissions (`request.review`, `timetable.manage`,
  `academics.manage`, `bell.view`).
- **Create a DOS.** From **Teachers and Staff**, a Director can now *Invite Director of
  Studies* (invitation token) or *Add Director of Studies* (temporary password, forced change
  at first log in). Also added *Add teacher* for parity with the existing invite flow.

## [0.2.3] - 2026-10-12

### Fixed
- **Accepting an invitation with a token failed with "Invalid or expired session".** The
  terminal signed the recipient in automatically after acceptance (using the password they
  just chose), instead of calling an authenticated endpoint while still logged out. This
  also fixed the same failure when accepting from the "no school membership" menu.

## [0.2.2] - 2026-10-12

### Removed
- **`sacs demo` command removed entirely.** The product entry point is now `sacs start`
  (interactive terminal app). `sacs serve`, `sacs status`, `sacs check` and
  `sacs version` remain; the live Rich dashboard, identity-switching REPL commands
  (`session list`/`session switch`/`whoami`/etc.) and `demo reset`/`demo stop` are gone.

### Added
- **First-login password policy.** Administrators who create staff directly issue a
  temporary password; the new member must change it at first sign-in
  (`POST /api/auth/change-password`). A `must_change_password` flag is set when an
  account is created with an admin-chosen password and cleared once the owner changes it.
- **Change password from the terminal** (`Account → Change password`).
- **Staff deactivation/reactivation** from the Director flow
  (`Teachers and Staff → Deactivate member / Reactivate member`), backed by
  `PATCH /api/schools/{id}/staff/{membership_id}`.
- **Accept an invitation with a token** directly from the sign-in screen, so a brand-new
  school head or teacher creates their account and password without logging in first.

## [0.2.1] - 2026-10-12

### Added
- Bell chimes three times per ring (configurable via `SACS_BELL_RINGS`, default `3`).
  Audio synthesis writes multiple chimes separated by a short gap.

### Fixed
- Windows `winsound` playback: use implicit synchronous flag (no `SND_SYNC`
  attribute) so bell audio no longer fails with `AttributeError` on some
  Windows builds.

## [0.2.0] - 2026-10-11

### Added
- **Interactive terminal application** (`sacs start`). A single numbered-menu
  experience for the three product roles - Platform Administrator, Director
  (School Head) and Teacher - built on a menu *stack* so `0` always returns to the
  immediate parent. `0. Logout` from a main menu, `0. Exit` from login; EOF and
  Ctrl+C are treated as "back". Black background, green primary text, dark-green
  secondary text, red errors, monospaced; automatic plain-text fallback when colour
  is unsupported. No web/browser frontend.
- **In-process API client** (`sacs.api_client`). The terminal drives the *same*
  FastAPI app through Starlette's ASGI test client, so every action passes through
  the real authentication/authorization/validation code.
- **Terminal presentation layer** (`sacs.ui`): console, colours, menus, navigation
  and form helpers.
- **Persistent notification inbox** (`/api/notifications`): list, unread count,
  open, mark one/all read - scoped strictly to the signed-in user. Invitations now
  notify the inviter (and Platform Administrators for Director invitations) when
  accepted.
- **School bell scheduling**: per-schedule weekdays (`days`), period add/remove,
  activate/deactivate, delete, manual `ring`, and `test-sound`, plus best-effort
  audio synthesis/playback (`sacs.audio`) with a terminal-bell fallback.
- **Timetable drafting**: entries are created as drafts; teachers never see drafts;
  teacher/class/classroom time-slot conflicts are rejected on create and re-checked
  before publishing. Publishing promotes drafts and materialises lesson sessions.
- **Director timetable draft entry** flow in the terminal app.

### Changed
- Version bumped to 0.2.0. Package description corrected to reflect the
  terminal-only product.

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