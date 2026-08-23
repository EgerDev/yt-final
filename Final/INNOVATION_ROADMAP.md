# Historical Innovation Roadmap

This file preserves high-level product research that informed `yt_final 1.0.0`.
It is not a release contract, support statement, installer manifest, or promise
that a listed idea will ship. The active behavior and supported boundaries are
defined by the root `README.md`, this directory's `README.md`, `SECURITY.md`,
the committed tests, and the release workflows.

## What 1.0.0 completed

- A direct-script CLI, installed `yt-final` command, and `python -m Final`
  compatibility entry point.
- Fast single-extraction downloads with quality/codec selection, bounded
  parallelism, retry classification, cancellation, and output verification.
- Edit-oriented FFmpeg profiles, hardware capability probes, marker export,
  subtitles, chapters, loudness normalization, thumbnails, and metadata.
- Durable queue, history, channel watch, batch manifests, and a local Web UI.
- Loopback-only password login with revocable 12-hour sessions and no
  query-string credentials.
- Owner-only local secrets/state, redacted logs, validated persistent schemas,
  idempotent reruns, tracked notifications, and serialized installers.
- Immutable, checksum-verified automated tool artifacts with fail-closed
  unsupported-platform behavior.
- Reproducible portable ZIP, wheel, source distribution, checksum manifest,
  hash-locked environments, and Windows/macOS CI contracts.
- Keyboard and live-region accessibility, stale-request cancellation, truthful
  busy/error states, and responsive layout hardening.

## Explicit 1.0.0 boundaries

- Windows x86-64 and macOS Intel/Apple Silicon are Tier 1.
- Linux is unsupported.
- Managed FFmpeg is Windows-only; macOS uses manual or Homebrew FFmpeg.
- The Web UI is a local application. It has no LAN/public binding,
  multi-user authorization, built-in TLS, or reverse-proxy trust.
- Optional PO-token services and arbitrary dependency upgrades remain manual
  until their source, license, artifacts, and complete dependency closure pass a
  separate review.
- Internal Python modules are not a stable public API; the CLI is the supported
  interface.

## Deferred ideas

The following are possible subjects for a separately designed later release,
not incomplete 1.0.0 work:

- a breaking `src/` package migration and normalized internal imports;
- an explicitly supported Linux tier with native platform evidence;
- signed update metadata and additional verified artifact providers;
- richer multi-user or remote-service deployment; and
- broader static typing of the legacy engine.

Any future proposal must begin with a concrete user need and threat/support
model, receive its own Q&A and design review, add failing tests before behavior
changes, and preserve the 1.0.0 compatibility contract unless the release is
explicitly breaking.
