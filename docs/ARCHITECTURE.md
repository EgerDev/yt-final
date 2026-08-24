# yt_final architecture

`Final/` contains 107 Python files: 35 application modules and 72 `test_*`
modules. This document maps each of the 35 to a layer and says which file holds
which major class, so a reviewer does not have to open all of them to find out
where a concern lives.

Thirty-one of the 35 carry a module docstring — the four that do not are the
entry-point shims — and that docstring is the authority for what the module
does. This map is an index over them, not a replacement: read the docstring
before changing the module.

## What is supported, and what is internal

The **CLI is the supported interface**. Three spellings are equivalent:

```bash
yt-final download URL         # installed console script (Final.cli:main)
python -m Final download URL  # installed package
python Final/yt_final.py download URL  # direct script, from the portable ZIP
```

`CONTRIBUTING.md` commits to preserving that CLI and direct-script behavior for
1.0.0. Nothing else in the tree carries that promise:

- **`/api/*` is internal and unversioned.** There is no `/api/v1`. Those routes
  exist to serve `Final/webui/`, which ships in the same wheel and is upgraded
  in lockstep, so a route can be added, renamed, or removed in any release
  without a deprecation window. Do not build an external client against it.
- **The Python modules are not a public API.** `Final/__init__.py` exports
  `__version__` and nothing else; the wheel's only entry point is
  `Final.cli:main`. Imports such as `from Final.jobstore import DownloadJournal`
  work, but no signature, class, or module name below `Final.cli` is covered by
  a compatibility guarantee.

## Where the weight is

Line counts at 1.0.0. Fourteen modules are over 500 lines; the other 21 are
small enough to read in a sitting.

| Module | Lines |
| --- | --- |
| `Final/yt_final.py` | 9495 |
| `Final/webserver.py` | 3768 |
| `Final/_cdp_client.py` | 2911 |
| `Final/ffmpeg_updater.py` | 1786 |
| `Final/optional_tools.py` | 1421 |
| `Final/jobstore.py` | 1142 |
| `Final/capability.py` | 872 |
| `Final/watch_filters.py` | 778 |
| `Final/cookies_auth.py` | 702 |
| `Final/watchstore.py` | 657 |
| `Final/notifications.py` | 611 |
| `Final/jobspec.py` | 585 |
| `Final/secureio.py` | 557 |
| `Final/state_validation.py` | 504 |

The web UI is not Python and is not in that table: `Final/webui/app.js` is 5827
lines, which makes it the second-largest file in the wheel after `yt_final.py`.

Two of the four heaviest modules — `_cdp_client.py` and `ffmpeg_updater.py` —
are not download logic at all. They are the browser-cookie and managed-installer
layers, and their size is almost entirely platform trust checks.

## Major classes

`CapabilityDetector` is the one major class that does **not** live in
`yt_final.py`. It moved out into `capability.py` when the FFmpeg/GPU probing was
split from the engine, and `Final/_syntax_check.py` exists specifically to
assert that the split stayed split — that the detector is defined in one module
and the engine imports it rather than redefining it. The remaining engine
classes are still in `yt_final.py`.

| Class | Module |
| --- | --- |
| `CapabilityDetector`, `FFmpegResolution` | `Final/capability.py` |
| `FinalYouTubeGPUEngine`, `YouTubeDownloader`, `FFmpegProcessor` | `Final/yt_final.py` |
| `YTConfig`, `BandwidthMonitor`, `UpdateChecker` | `Final/yt_final.py` |
| `JobSpec`, `DownloadEngineDecision` | `Final/jobspec.py` |
| `DownloadJournal`, `HistoryLedger` | `Final/jobstore.py` |
| `WatchStore` | `Final/watchstore.py` |
| `ServerState`, `Handler` | `Final/webserver.py` |
| `SessionStore` | `Final/websessions.py` |
| `CookieManager`, `CookieState` | `Final/cookies_auth.py` |
| `CookieFileProvider`, `HeadlessProfileProvider`, `BrowserExtractProvider` | `Final/cookie_providers.py` |
| `ManagedFFmpegUpdater`, `FFmpegRelease` | `Final/ffmpeg_updater.py` |
| `ToolArtifact` | `Final/tool_manifest.py` |
| `NotificationDispatcher`, `DeadlineBoundSender` | `Final/notifications.py` |
| `SensitiveDataFilter` | `Final/redaction.py` |

## Module map

### Entry points and version identity

| Module | Role |
| --- | --- |
| `cli.py` | The wheel's console-script target. Puts the package directory on `sys.path`, then imports `yt_final` and calls its `main()`, so the installed command and the direct script run the same code. |
| `__main__.py` | Makes `python -m Final` call the same `cli.main()`. |
| `__init__.py` | Re-exports `__version__`. Deliberately nothing else. |
| `_version.py` | The single `__version__` string. The build reads it through `[tool.setuptools.dynamic]`, and the release verifier cross-checks every embedded copy. |

### Engine and download path

| Module | Role |
| --- | --- |
| `yt_final.py` | The engine and the whole argument parser. Android API 14 extraction with layered client fallbacks, parallel downloads, retry planning, the profile table, and the download-then-finalize orchestration. `download_then_process` remains as a back-compat adapter over `FinalYouTubeGPUEngine.run(spec)`. |
| `jobspec.py` | `JobSpec`, the typed job contract. One declarative description of "download these URLs and turn them into edit-ready media this way", shared by the CLI, the batch manifest, the durable queue, and the HTTP server, so a new knob is a field plus a consumer rather than a wider method signature. stdlib only, so it stays cheap on the CLI fast path. |
| `url_identity.py` | Canonical download identities. Strict YouTube URL parsing, video-ID extraction, duplicate collapsing across spellings of the same video, and `output_identity_lock()`, which serializes one video's output lifecycle per output directory inside the process so two workers cannot race on the same file. |
| `watch.py` | Channel subscribe and auto-archive over YouTube's public RSS feed, which needs no API key. Resolves a channel URL, `@handle`, or ID to a channel ID, fetches the latest uploads, and keeps a seen-ID archive so only new videos are acted on. |
| `watch_filters.py` | Validated, time-bounded title matching for those watches. User patterns are restricted to a documented subset of `re` grammar and run on the `regex` engine so each match has a hard per-operation time budget — a watch pattern is user input that runs on a timer. |
| `desync_proxy.py` | A local DPI-desync proxy speaking HTTP CONNECT and SOCKS5, used to get out from under ISP throttling without a VPN, a remote server, or root. Runs standalone (`python Final/desync_proxy.py`), and the web server can also supervise it as a child process and route downloads through it. |

### Media and encoding

Encoding itself is not a separate module: `FFmpegProcessor` and the profile
table live in `yt_final.py`. What is factored out is the sidecar generation,
because both formats are exact third-party schemas.

| Module | Role |
| --- | --- |
| `markers.py` | Chapter-to-NLE timeline-marker export: CMX3600 EDL with DaVinci Resolve `* LOC:` lines, Premiere's marker-import CSV, and FCPXML chapter markers. The chapters are already in the downloader's `last_info`, so this costs no extra network. stdlib only. |
| `nfo.py` | Kodi/Jellyfin/Plex `.nfo` sidecars from a yt-dlp info dict, using the widely read Kodi `<movie>` schema, so a media server shows the real title and metadata instead of the filename. stdlib only. |

### Capability detection

| Module | Role |
| --- | --- |
| `capability.py` | `CapabilityDetector`. Finds FFmpeg, enumerates encoders, and decides which hardware encoders (NVENC, AMF, QSV, including AV1) actually work by running a tiny real null-encode probe rather than trusting the encoder list. `resolve_ffmpeg()` and `get_detector()` are what the engine imports. |
| `dependency_contract.py` | Parses `Final/requirements.txt` into a contract: canonical distribution name to its single declared `>=` floor, the installed version, and whether the floor is met. Ambiguous requirement text is an error, not a guess. |
| `speedtest.py` | The Speed Test decision engine — pure logic, no sockets. Takes a measurement vector gathered by `/api/speedtest` and returns a verdict plus ranked recommendations. A failed probe is `None` and never `0.0`, so a transient fetch error degrades to "couldn't test" instead of masquerading as an extreme throttle. |

### Web server, session, and auth

| Module | Role |
| --- | --- |
| `webserver.py` | The whole `serve` tier: a stdlib `ThreadingHTTPServer` on loopback, the `/api/*` handlers, and one background worker that drains the `DownloadJournal` under single-writer discipline. Imported lazily from the `serve` subcommand only, so the CLI fast path never pays for it. It binds only a normally resolved loopback address, rejects foreign `Host` headers, requires an exact `Origin` match on mutations, and unlocks with a per-launch random password. |
| `websessions.py` | `SessionStore`: thread-safe in-memory sessions for that server. Only a SHA-256 digest of the token is retained, sessions expire after 12 hours, and a restart invalidates all of them because the store is in memory. Also retires the obsolete permanent-token file without ever reading it. |
| `notifications.py` | Bounded, tracked delivery for the job-completion webhook a `JobSpec` names in `notify_url`. `DeadlineBoundSender` gives every send a deadline and `NotificationDispatcher` tracks every outstanding future, so shutdown cannot leak a hanging request, and credential-bearing headers are stripped from anything that reaches a log. |

The browser half of this tier is `Final/webui/` (`index.html`, `app.js`,
`style.css`) — no build step, no framework, no bundler.

### Cookies and browser integration

| Module | Role |
| --- | --- |
| `cookies_auth.py` | `CookieManager`, the lazy single-identity cookie store. Its read-check-validate-adopt-save sequence is serialized per resolved store path so that N parallel download workers, the per-request managers built by `/api/probe` and `/api/auth`, and the server's worker loop converge on one validation walk instead of each re-running it. |
| `cookie_providers.py` | The tier chain behind that manager, in order: `CookieFileProvider` (an imported Netscape `cookies.txt`), `HeadlessProfileProvider` (the managed native-browser profile from `auth login`), `BrowserExtractProvider` (best-effort extraction from a live browser). Providers are best-effort by contract and never raise up. |
| `_cdp_client.py` | A minimal bounded DevTools-Protocol client used to read decrypted cookies out of a real Chromium profile. It snapshots the profile into a temporary `--user-data-dir` so Chrome will open it without fighting the running browser's `SingletonLock`; if no real profile can be snapshotted it navigates nowhere and returns `None` rather than a misleading success seeded with throwaway consent cookies. Every HTTP, WebSocket, and profile-size limit is a named constant, and the size of this module is the platform ownership and ACL checking around that snapshot. |

### Durable state and local files

Most of this writes under `~/.yt_final/` — the queue, the watch store, the
config, the cookie store. The operational log `final_yt.log` is the exception:
it goes to the OS app-data directory instead, or wherever `YT_FINAL_LOG` points.
It used to be opened as a relative path at import time, which scattered a log
file into whatever directory the shell happened to be in. The
shared discipline is: atomic replace, owner-only from the first byte, validate
before trusting, and never rewrite a document just because it parsed.

| Module | Role |
| --- | --- |
| `jobstore.py` | `DownloadJournal`, the durable job queue: one JSON file, written temp-then-`os.replace`, guarded by a single-writer pidfile lock so two `queue run` processes cannot corrupt it. It is what `batch` and `serve` both read and write, and it replaced an in-memory history that did not survive a reboot. Also holds `HistoryLedger`. |
| `watchstore.py` | `WatchStore`, the persistent channel watches behind the web UI's auto-detection panel. Each watch's `auto_queue` flag decides whether a new upload is enqueued or parked as pending for the user. Atomic JSON writes plus an in-process mutex. |
| `state_validation.py` | Strict, non-destructive validation for both stores. The stores deliberately preserve unknown fields for forward and backward compatibility, so this validates only the versioned schema existing readers consume, and never normalizes or rewrites a valid document. |
| `secureio.py` | The primitive under all of it: durable owner-only file replacement for credential data. On Windows a restrictive DACL naming only the current user; on macOS mode `0600` enforced through the creating descriptor. Files do not inherit their parent directory's permissions. |
| `settings.py` | Opt-in network settings — the default proxy. Config load and save that does not clobber the `[network]` table, proxy validation, and the per-job proxy-resolution rule. Pure helpers with no server or engine imports. `https://` proxies are rejected on purpose: the managed yt-dlp lacks the backend they need and they would fail at download time. |
| `redaction.py` | `SensitiveDataFilter`, the logging filter that keeps credential URLs, cookies, webhook secrets, proxy details, and home-directory paths out of `final_yt.log` — which is itself created owner-only at mode `0600`. The filter covers traceback frames through `exc_text`, not just labelled log lines, because on Windows a home directory contains the operator's username and an unlabelled absolute path in a traceback leaked it verbatim. |

### Supply chain and installers

This is the layer with no user-visible feature and the strictest rules. The
premise is that the application downloads and then executes third-party
binaries, so every one of those bytes must be named in advance, verified before
it is probed or run, and installed under a lock.

| Module | Role |
| --- | --- |
| `tool_manifest.py` | The immutable manifest. `ARTIFACTS` is a module-level tuple of frozen `ToolArtifact` records — name, version, platform, architecture, exact URL, archive SHA-256, and the SHA-256 of the extracted binary itself. Nothing outside that tuple can be installed, and no entry is a mutable `latest` URL. A digest mismatch raises `ChecksumError`; a host with no approved artifact raises `UnsupportedManagedInstall` rather than falling back to something unreviewed. |
| `executable_trust.py` | Explicit-PATH-only executable discovery. `explicit_path_executable()` skips empty and relative `PATH` entries because both can authorize the current directory, and it deliberately does not use `shutil.which`, whose Windows behavior prepends the current directory. Browsers and OS helpers come only from fixed vendor install roots (`installed_browser_executable`, `system_helper_executable`), with symlink, junction, and reparse-point candidates rejected. |
| `installer_lock.py` | Process-wide and OS-wide serialization for installer mutations, plus `validated_private_root()`. A second concurrent install fails fast with `INSTALLER_BUSY_CODE` instead of interleaving two writers into one install tree. |
| `ffmpeg_updater.py` | Managed FFmpeg fetch and install for the one approved Windows build. `HTTPSOnlyRedirectHandler` refuses every redirect and the response's final URL must equal the approved URL; then verify, stage, and atomically activate under the installer lock. The activation swap happens inside `ffmpeg_launch_gate()` with a `busy()` re-check, because activation replaces a binary another thread may be about to launch. |
| `optional_tools.py` | The same shape for the certified Node and Deno runtimes that solve YouTube's `nsig` challenge, plus the discovery half (finding an existing runtime) and the manual-only guidance for tools that have no certified artifact — notably the bgutil PO-token helper server, which stays manual until an immutable server artifact passes the same review. |
| `managed_env.py` | The fail-closed Python-dependency policy. The application can report the packages visible to its own interpreter, and cannot install or activate app-writable Python code in 1.0.0. Python dependency changes ship only as a reviewed application release. This module is 32 lines because the answer is "no". |

### Release and verification tooling

| Module | Role |
| --- | --- |
| `_syntax_check.py` | The structural gate on the engine/detector split described above. `CONTRIBUTING.md` runs it as `python Final/_syntax_check.py`; it resolves paths from its own location so that documented invocation works from any directory, and it exits nonzero on failure rather than printing `False` and exiting `0`. |
| `runtime_verify.py` | Empirical runtime verification that actually runs FFmpeg, rather than asserting against a mock. |

Neither ships in the wheel or the portable ZIP: `setup.py` lists both in
`RELEASE_EXCLUDED_MODULES`, and `release/portable-files.txt` omits them. Both
are still in the source distribution, because `MANIFEST.in` ships every
`Final/*.py`, tests included.

The rest of the release machinery is outside `Final/`:

| Script | Role |
| --- | --- |
| `scripts/build_portable.py` | Builds the allowlisted, byte-reproducible portable ZIP from `release/portable-files.txt`. |
| `scripts/write_checksums.py` | Writes the stable `SHA256SUMS` manifest. |
| `scripts/verify_release.py` | Fail-closed verification of a complete artifact set: reviewed member boundaries, recomputed digests, wheel `RECORD`, and every embedded `_version.py`. It holds one of the three hand-maintained `Final/*.py` inventories. |
| `scripts/make_public_tree.py` | Produces the published source tree as a pure subtraction of this repository. Every file present in both trees is byte-identical; nothing is rewritten on the way out, which is what stops the two trees from diverging. |

`docs/RELEASING.md` is the procedure those scripts belong to.

## Cross-cutting invariants

Four rules show up in more than one layer. Breaking one of them in a single
module usually breaks something two layers away.

- **Import cheapness on the CLI fast path.** `jobspec.py` and the other
  contract modules are stdlib-only, and `webserver.py` is imported lazily from
  the `serve` branch. A new top-level import in the wrong module is a
  measurable startup regression for every `download` invocation.
- **Single writer per durable file.** The journal and the watch store each take
  an OS-level file lock — `msvcrt.locking` on Windows, `fcntl.flock` elsewhere —
  around their mutations, on top of an in-process mutex, because the file lock
  only guards across processes. Adding a mutation path that skips either half is
  the defect class these stores were built to eliminate.
- **Verify before execute.** Nothing downloaded is probed, extracted, or run
  before its SHA-256 matches a `tool_manifest.py` entry.
- **Fail closed and say so.** An unmeasured probe is `None`, not `0.0`; an
  unsupported host raises rather than falling back; a provider that cannot
  produce cookies returns nothing rather than an empty success.

## Type checking is not uniform

`pyproject.toml` runs mypy over nine `Final` modules — `websessions.py`,
`secureio.py`, `redaction.py`, `state_validation.py`, `watch_filters.py`,
`url_identity.py`, `notifications.py`, `installer_lock.py`,
`tool_manifest.py` — plus `scripts/*.py`. Those are the modules where a type
error is a security or durability bug. The engine and the server are not in
that list, so do not read an absent annotation there as a checked one.

Ruff is configured narrowly too (`E9`, `F63`, `F7`, `F82`): syntax errors and
undefined names, not style. `python -m pytest` is the authoritative gate. See
`CONTRIBUTING.md` for the full list.

## Suggested reading order

For a reviewer starting cold:

1. `jobspec.py` — the object everything else moves around.
2. `capability.py` — what the machine can actually do.
3. `yt_final.py`, starting at `FinalYouTubeGPUEngine.run()`.
4. `jobstore.py` — how intent survives a reboot.
5. `webserver.py` — the second front end over the same journal.
6. `tool_manifest.py` and `executable_trust.py` — the trust boundary, and the
   two files that explain why the installer modules are as large as they are.
