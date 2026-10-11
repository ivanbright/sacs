# Changelog

All notable changes to SACS are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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