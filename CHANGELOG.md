# Changelog

All notable changes will be documented here.

## [Unreleased]

No unreleased user-facing changes.

## [1.0.0] - 2026-07-29

### Added

- Supported `yt-final` and `python -m Final` entry points while preserving the
  established direct-script workflow.
- Hash-locked build, development, runtime, and release dependency sets.
- Reproducible wheel, source distribution, portable ZIP, and `SHA256SUMS`
  generation with explicit content boundaries.
- Revocable 12-hour Web UI sessions, logout, restoration, and restart
  invalidation on a loopback-only server.
- Owner-only secret/state writers, log redaction, schema-validated durable
  state, bounded watch filters, idempotent reruns, tracked notifications, and a
  cross-process installer lock.
- Immutable, checksum-verified supply-chain manifests for approved automated
  installers.
- Keyboard-accessible tabs, live-region feedback, stale-request cancellation,
  and responsive frontend corrections.

### Changed

- The three hand-maintained source-distribution inventories are now
  cross-checked against each other and against `Final/` on every test run,
  rather than only inside the artifact-building release test.
- Added `CODE_OF_CONDUCT.md`, issue templates, and a pull request template.
- Established `Final/` as the supported 1.0.0 product boundary.
- Made update installation fail closed: scans may report newer software, but
  automated mutation accepts only the certified release allowlist.
- Limited managed FFmpeg to the reviewed Windows x86-64 artifact. macOS Intel
  and Apple Silicon use manual or Homebrew FFmpeg.
- Defined Windows and macOS as Tier 1 and Linux as unsupported for 1.0.0.

### Fixed

- Cancelling or timing out a job no longer terminated the application itself on
  macOS. The FFmpeg child was started without its own process group, so the
  cleanup signal reached every process in the group, including `yt_final`.
- Burned-in subtitle and drawtext filters no longer break on an apostrophe.
  FFmpeg parses a filter value twice, so a backslash cannot escape a quote
  inside quotes; the value is now closed, escaped, and reopened.
- Jobs enqueued from a watched channel now use the configured default proxy,
  matching jobs created through the API.
- Output verification no longer rejects a valid video-only result when a single
  explicit format was requested.
- Command-line transfer speed is no longer misreported when no progress sink is
  attached.
- Cookie files are written atomically, so a download reading the same file
  cannot observe a torn or empty file, and a failed write no longer destroys
  existing cookies.
- Watch state mutations take the same cross-process lock the job store uses.
- Web UI: polling resumes after a hidden tab, strict-verify selection is
  applied, overlapping polls cannot clobber each other, the paused banner
  survives an unchanged response, and sub-480p probe chips no longer widen to
  the best available quality.
- Prevented state corruption, duplicate output work, duplicate reruns,
  abandoned notification threads, concurrent installer mutation, stale update
  rendering, session races, and sensitive-log disclosure.
- Hardened browser-profile extraction, subprocess cleanup, Windows server
  lifecycle handling, packaging path validation, and deterministic
  distribution creation.

### Security

- An `on_complete` command arriving from a batch manifest or `config.toml`
  is ignored unless `--allow-hooks` is passed, and `--dry-run` now shows any
  hook that is in effect. Supplying one on the command line is unchanged.
- The Web UI password file is created owner-only on Windows through a
  restrictive DACL instead of an advisory mode bit, so it no longer inherits
  read access for `SYSTEM`, `Administrators`, and other local accounts.
- Removed public/LAN server mode and legacy URL/static-token authentication.
- Authenticated every non-public API route and enforced loopback `Host` and
  browser-origin policy.
- Replaced mutable or hashless automated download paths with immutable
  supply-chain verification.
