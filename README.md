# yt_final 1.0.0

`yt_final` turns YouTube URLs into edit-ready media for Premiere, Resolve, Avid,
and other NLE workflows. It combines `yt-dlp`, FFmpeg processing, hardware
encoder detection, durable queues, marker and metadata export, and a local Web UI.

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](LICENSE)

Third-party software and assets retain their own terms. See
[Final/THIRD_PARTY_NOTICES](Final/THIRD_PARTY_NOTICES).

## Supported release

The 1.0.0 Tier 1 contract is:

- Python 3.11 through Python 3.14;
- Windows x86-64; and
- macOS Intel and Apple Silicon.

Linux is unsupported for 1.0.0.

### External tools

Two tools live outside Python. Neither is installed by `pip`, and the second one
is the single most common cause of slow downloads.

| Tool | Needed for | Windows x86-64 | macOS |
| --- | --- | --- | --- |
| **FFmpeg** + `ffprobe` | all encoding, remuxing, and merging video with audio | managed install from the Web UI **Updates** tab, or put it on `PATH` yourself | `brew install ffmpeg`, or put it on `PATH` yourself |
| **Node** or **Deno** | solving YouTube's `nsig` challenge | managed install (Node 24.18.0 / Deno 2.9.4) | managed install on Apple Silicon; **Intel Macs must install manually**, e.g. `brew install node` |

Without a JavaScript runtime every download is throttled to roughly 1 MiB/s.
Nothing fails — it just gets slow, which is why it is worth checking before you
blame the network.

Managed FFmpeg is Windows x86-64 only: no redistributable macOS build has passed
the 1.0.0 license and provenance gate.

Check what your machine actually has:

```bash
yt-final diagnose
```

## Install

`yt_final` is not on PyPI, and 1.0.0 has no published download yet, so install
from a checkout:

```powershell
git clone https://github.com/EgerDev/yt-final.git
cd yt-final
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\yt-final.exe diagnose
```

```bash
git clone https://github.com/EgerDev/yt-final.git
cd yt-final
python3.11 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/yt-final diagnose
```

That builds and installs the wheel from source. It resolves dependencies
normally rather than from the certified hash-locked set — fine for trying the
tool, and the section below is what you use once a verified release exists.

### Installing a verified release

*No 1.0.0 artifacts have been published yet; this describes the process for when
they are.* A release contains a wheel, a source distribution, a portable ZIP,
and `SHA256SUMS`. Verify all four before installing:

```powershell
# From a source checkout, validates names, checksums, versions, and members.
python scripts/verify_release.py path\to\release-artifacts
```

You can also compare each platform's SHA-256 command with `SHA256SUMS`:

```powershell
# Windows PowerShell
Get-FileHash -Algorithm SHA256 .\yt_final-1.0.0-*
```

```bash
# macOS
shasum -a 256 yt_final-1.0.0-*
```

For a clean wheel installation, create a Python 3.11+ virtual environment,
install the certified dependency lock, then install the wheel without resolving
different dependencies.

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r Final\approved-python.lock
.\.venv\Scripts\python.exe -m pip install --no-index --no-deps .\yt_final-1.0.0-py3-none-any.whl
.\.venv\Scripts\yt-final.exe --version
```

macOS:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r Final/approved-python.lock
.venv/bin/python -m pip install --no-index --no-deps ./yt_final-1.0.0-py3-none-any.whl
.venv/bin/yt-final --version
```

The portable ZIP keeps the established direct-script workflow:

```bash
python Final/yt_final.py --version
python Final/yt_final.py download "https://youtu.be/..."
```

An installed wheel additionally supports:

```bash
yt-final --help
yt-final list-profiles
python -m Final --help
```

See [docs/RELEASING.md](docs/RELEASING.md) for the protected build and draft
release procedure.

## Quick start

```bash
# Fast, edit-friendly default
yt-final download "https://youtu.be/..."

# ProRes, 24-bit PCM, loudness normalization, and marker export
yt-final download URL \
  --encode-mode reencode \
  --profile prores_hq \
  --audio-codec pcm_s24le \
  --loudnorm -14 \
  --export-markers all

# Local Web UI
yt-final serve
```

### Where things are written

| what | where |
| --- | --- |
| downloads | `./downloads` **relative to where you run it** — set it explicitly with `--output-dir`, and `serve` prints the absolute path on startup |
| log | `%LOCALAPPDATA%\yt_final\final_yt.log` (Windows) or `~/Library/Application Support/yt_final/` (macOS); override with `YT_FINAL_LOG` |
| cookies, queue, history, settings | `~/.yt_final/` |

The download directory is the one path that follows your shell, so pass
`--output-dir` if you care where files land:

```bash
yt-final --output-dir "D:/Footage/incoming" download "https://youtu.be/..."
```

Core capabilities include:

- hardware encoder probing with safe CPU fallback;
- H.264/H.265/AV1, ProRes, and DNxHR profiles;
- subtitles, thumbnails, SponsorBlock chapters, `.nfo`, and NLE markers;
- collision-safe batch and parallel downloads;
- persistent queue, history, watch feeds, retry, and cancellation; and
- explicit, user-controlled update reporting and certified installers.

The complete command and workflow reference is in
[Final/README.md](Final/README.md).

## Why not just use yt-dlp?

`yt_final` calls yt-dlp to extract and download, so it is not a replacement for it. yt-dlp already
merges with FFmpeg, embeds subtitles, writes chapters, and marks SponsorBlock segments; if that is
what you need, use it directly. What this project adds is the part after the bytes arrive, aimed at
getting footage into an edit:

- **Verified hardware encoding.** Every hardware encoder is proven with a real null-encode probe
  before it is used, because an FFmpeg build routinely advertises an encoder the machine cannot
  actually run. A failed or unusable encoder degrades down a checked chain — AV1 NVENC to HEVC
  NVENC to H.264, and ultimately to libx264 — instead of failing the job.
- **Editing intermediates.** ProRes and DNxHR/DNxHD profiles, and `--proxy-pair` for a master plus a
  half-res H.264 proxy, so the result opens in Premiere, Resolve, or Avid without a transcode of
  your own.
- **NLE marker export.** `--export-markers edl|csv|fcpxml|all` turns YouTube chapters into timeline
  markers; `--sponsorblock-markers` does the same for SponsorBlock categories.
- **A durable queue.** `queue` persists jobs to a JSON journal written by atomic rename under a
  single-writer lock, so a reboot or a crashed run resumes rather than restarts.
- **Watch feeds.** `watch` polls a channel's RSS feed and downloads new uploads, or queues them with `--enqueue`. No API key.
- **A local console.** `serve` runs a loopback-only web UI over the same engine and queue.

Where yt-dlp wins is **site coverage**: it ships over 1,700 extractors, while `yt_final` accepts
YouTube URLs and rejects everything else. It also tracks extractor breakage far faster than a
downstream project can, which is exactly why extraction here is yt-dlp rather than something
homegrown.

## Local Web UI security

`yt-final serve` is loopback-only. It rejects non-loopback binds, foreign `Host`
headers, query-string credentials, and cross-origin browser mutations.

The startup password is exchanged for a unique revocable session:

- the credential is held in versioned browser local storage and sent only in
  `X-Token`;
- only its SHA-256 digest is stored by the server;
- sessions expire after 12 hours;
- logout revokes only the current session; and
- every server restart invalidates every session.

Only static assets, same-origin login, and minimal liveness health are public.
All other `/api/*` reads and mutations require a valid session. The service does
not support LAN/public binding, built-in TLS, or reverse-proxy trust. An operator
may create an SSH or WireGuard tunnel to the loopback listener, but that tunnel
is outside the application's security boundary.

## Authentication and privacy

Cookie-based YouTube authentication is optional. Use a dedicated account:
YouTube can rate-limit or suspend accounts whose cookies are used by download
tools.

`yt_final` never types your Google password. You either import a Netscape
`cookies.txt`, complete a native-browser login yourself, or explicitly request a
best-effort browser-cookie extraction. Cookie files, the browser profile,
history, queue state, detailed job data, and operational logs remain local and
are created owner-only where the platform supports it.

The application has no analytics service. It makes outbound requests required
for downloads and explicit features you configure, such as version checks,
webhooks, proxies, or optional authentication. Normal logs redact credential
URLs, cookies, proxy details, and unnecessary local paths.

## Updates and supply-chain limits

The app reports available versions but does not silently install arbitrary
latest packages. Automated installers accept only immutable manifest entries,
verify SHA-256 before probing or execution, stage changes, and atomically
activate them under a cross-process installer lock.

The bgutil PO-token integration has two halves, and only one of them is
installed for you:

- the **provider plugin** (`bgutil-ytdlp-pot-provider`) is a declared runtime
  dependency, so it is present in the certified lock and in the wheel's
  `Requires-Dist`; the running application never upgrades or replaces it, and
  the in-app Python dependency updater is disabled for 1.0.0; and
- the **helper server** is manual. It is not downloaded, started, or managed
  here, and it stays manual until an immutable server artifact passes the same
  checksum-verified review the other automated installers use. Keep the server
  version aligned with the plugin.

The plugin declares GNU GPLv3 metadata. Shipping it is intentional product
behavior, not a license-compatibility certification: a distributor must
complete the legal and transitive-dependency review described in
[requirements/README.md](requirements/README.md) before publishing a build.

## Repository layout

Most of the tests are not in `tests/`, which makes the coverage look far thinner than it is:

| Path | Contents |
| --- | --- |
| `Final/` | the product: 107 Python modules, of which **72 are `test_*.py`** — the pytest suites sit beside the code they cover |
| `Final/webui/` | the static console served by `yt-final serve` |
| `Final/examples/` | a sample config and batch manifest |
| `tests/browser/` | the only tests that are not pytest: one Playwright spec (six cases) plus its fail-closed reporter, run by `npm run test:browser` |
| `scripts/` | release and test tooling — portable build, checksums, `verify_release.py`, published-tree generation, browser-test runners |
| `requirements/` | hash-locked build, development, and release dependency sets |
| `docs/` | `CLI.md`, `CONFIGURATION.md`, `TROUBLESHOOTING.md`, `ARCHITECTURE.md`, and `RELEASING.md` |
| `release/` | the file list for the portable ZIP |

`python -m pytest` from the repository root is the authoritative suite: it collects everything under
`Final/`, more than 2,700 tests. The Playwright spec runs separately, through npm.

## Development

Install the exact development lock and run the authoritative root suite:

```bash
python -m pip install --require-hashes -r requirements/dev.lock
python -m pytest
python -m ruff check Final scripts
npm ci
npm run lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the complete gate list and
[SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Something not working?

Run `yt-final diagnose` first — it checks FFmpeg, the JavaScript runtime, the
PO-token provider, and your hardware encoders, and most problems are one of
those lines. [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) is keyed to
symptoms: bot checks, 403s, downloads capped near 1 MiB/s, missing hardware
encoders, and Web UI login trouble.

## Legal and intended use

`yt_final` is a local download and media-processing tool. Use it for content you
own, content you have permission to download, or content whose licence allows
it. The terms of the services you access may prohibit downloading, and those
terms are between you and the service — you are responsible for complying with
them and with the copyright law that applies where you are.

The project does not circumvent DRM, bot detection, or access controls, and
changes that would add such capability are out of scope
([CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)). Cookie-based authentication exists
so the tool can act as *you*, on an account you already have — not to bypass a
restriction that applies to you.

The Unlicense below dedicates this project's own code to the public domain. That
is a statement about the code, not a warranty about what you do with it.

## Trademarks

YouTube and Google are trademarks of Google LLC. This project is not affiliated
with, endorsed by, or sponsored by Google LLC or YouTube. Other names are used
descriptively and belong to their owners.

## License

The project is released under the [Unlicense](LICENSE). Third-party license and
source-offer information is recorded in
[Final/THIRD_PARTY_NOTICES](Final/THIRD_PARTY_NOTICES).

Contributions are covered by [CONTRIBUTING.md](CONTRIBUTING.md#licensing-of-contributions).
