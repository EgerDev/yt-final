# Changelog

All notable changes will be documented here.

## [Unreleased]

### Fixed

- "Login with Chrome" no longer reports a failure after a sign-in that worked.
  To read a profile without fighting the running browser the app copies it to a
  temporary folder, and on Windows that copy often cannot be deleted afterwards:
  Chrome creates `Cache` and `Shared Dictionary` inside it with permissions
  naming only an internal Chrome identity, so the owning account has no entry.
  A failed cleanup discarded a complete, valid cookie set. It now keeps the
  cookies and reports where the leftover folder is, since that folder holds a
  copy of your session. A browser that could not be terminated still fails
  closed and returns nothing.
- A login profile the app could not read back is now reported as exactly that,
  with the underlying reason, instead of "no cookies were found". The old
  wording sent people to sign in again, which is the one thing that cannot fix a
  directory-permissions problem.
- "Test cookies" now answers in about two seconds instead of nearly two minutes.
  The check let `yt-dlp` request a PO token once per client, and a provider that
  is installed but failing is retried every time; the check only needs to know
  whether the stored cookies unlock an age-gated video, which one client answers
  without a token. Measured 111.6s to 1.8s for the same verdict.
- A "Sign in to confirm you're not a bot" challenge is no longer reported as a
  failed login. It is a rate limit on the connection, not a signed-out session,
  and reporting it as one sent people to re-authenticate when that could not
  help. A genuine age gate still reports a signed-out session.
- The password field is no longer cleared while it is being typed into. A
  session check that resolved after the login screen appeared reset the field,
  so the next attempt submitted an empty password and the screen never
  unlocked.
- `pip install .` works again. `pyproject.toml` declared both a license
  expression and the superseded license classifier, and current `setuptools`
  rejects that combination outright, so every install and every packaging build
  stopped before it began.

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
