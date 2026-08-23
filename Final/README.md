# yt_final 1.0.0 Product Guide

Consolidated YouTube downloader (yt-dlp) + GPU/CPU encoder (FFmpeg) with robust extraction,
parallel downloads, Premiere/Resolve/Avid-friendly profiles and verified hardware-encoder detection.

Third-party assets retain their own terms; see [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

The supported 1.0.0 runtime is Python 3.11 through 3.14 on Windows x86-64
and macOS Intel/Apple Silicon. Linux is unsupported for 1.0.0. The server is
loopback-only; it does not provide LAN/public binding or reverse-proxy trust.

- `yt_final.py` — downloader, encoder, orchestrator, CLI + interactive menu.
- `capability.py` — FFmpeg + GPU/NVENC/AMF/QSV detection (real null-encode probe, incl. AV1).
- `test_*.py` — the pytest suites (`python -m pytest` from the repository root).

## Quick start

```bash
cd Final

# Fast, edit-ready default (1080p H.264, copy/remux)
python yt_final.py download "https://youtu.be/..."

# Pick codec / resolution explicitly
python yt_final.py download URL --quality 4k --codec av1 --encode-mode copy
python yt_final.py download URL --quality best --codec h264

# Clip just a section (nearest-keyframe cut; great for quick grabs)
python yt_final.py download URL --section 0:00-2:00 --encode-mode none

# GPU re-encode to a profile (see list-profiles)
python yt_final.py download URL --encode-mode reencode --profile nvenc_av1_quality

# Parallel multi-video
python yt_final.py download URL1 URL2 --parallel --workers 4

# Encode/remux existing files or folders (collision-safe output names)
python yt_final.py encode ./downloads/clip.mp4 --mode reencode --profile prores_hq
python yt_final.py encode ./raw/ --mode copy

# Hardware + profiles
python yt_final.py gpu-info
python yt_final.py list-profiles
python yt_final.py troubleshoot

# Updates — scan and decide (never auto-applied)
python yt_final.py check-updates        # report only: installed vs reported versions
python yt_final.py update               # fails closed in 1.0.0; install a future reviewed app release
```

Run `python yt_final.py` with no args for the interactive menu.

## How the fast path works

**The default is a smart 1080p H.264 selection**, not `best`/`4k`, because the dominant cost is
download SIZE: a 2-hour video is ~1.5–2 GB at 1080p H.264 against ~5 GB of 4K VP9 at `best`/`4k`
— roughly **3× less data** to move at the same network speed. It also stays a true stream-copy for
editing, with no surprise VP9→MP4 re-encode. Use `--quality best/4k` and `--codec av1|vp9` when you
really do want more.

**Throughput levers (2025–2026 reality):**
- yt-dlp **2026.06.09 disabled external downloaders (aria2c/curl/wget) for DASH/HLS**
  (CVE-2026-50574), so byte fetching stays on yt-dlp's **native downloader with concurrent
  fragments** — `-N` defaults to 32 for common high-quality selections and 16 for lower ones, which
  parallelises the formats that are genuinely fragmented. aria2c, if present, is reserved for the
  rare single progressive `http` URL only.
- YouTube throttles every stream via the `n` (nsig) JS challenge. The tool **auto-detects a JS runtime
  (Deno/Node/Bun)** and enables yt-dlp's EJS solver so nsig is solved and full formats are returned.
  `yt-dlp-ejs` is installed only as part of the complete approved hash-locked Python set. On
  supported hosts, the Updates view can install the exact Node or Deno release in the immutable
  manifest; individual package upgrades are intentionally unsupported.
- Some YouTube clients need a **GVS PO token** before they will serve media, so extraction leads
  with the token-free direct-URL clients (`android_vr`, `web_safari`, `default` — the
  `CLIENTS_NO_POT` set) and negotiates the rest afterwards. Adding a provider widens that set to
  `CLIENTS_WITH_POT`, which also includes `mweb`. To unlock
  the fast fragmented web formats and exceed the per-session throttle, use **Premium** cookies
  (`--cookies ~/cookies.txt`) or configure a provenance-reviewed PO-token provider manually. Its
  Python plugin changes only when you install a future reviewed yt_final release.
- **One extraction per download.** The resolved format is reused for the transfer instead of being
  probed again, which is worth ~10–20 s and one fewer chance to trip the bot gate. A retry
  re-extracts a fresh signed URL, which is also how a stale URL recovers.

Measured throughput, the client and format shape to expect, and what to check when a download lands
on the slow HLS path are in *How it hits ~20–30 MiB/s on 1080p (the fast path)* below.

## Staying current (scan, you decide)

The tool never auto-applies updates. Reporting and installation are deliberately separate:

- On launch it may print a throttled, cached notice about a newer published Python package. This is
  report-only and never authorizes that version for installation. Disable it with
  `--no-update-check`.
- `check-updates` reports installed Python packages and optional
  FFmpeg/Node/Deno/provider status. A reported newer version is informational until a reviewed
  application release updates the lock or immutable manifest.
- Python dependency mutation is disabled in 1.0.0. `update` returns
  `certified-runtime-unavailable` before prompts, filesystem changes, network access, or process
  launch. Install a future reviewed yt_final release to change Python packages.
- Managed Node and Deno installs use exact direct URLs and SHA-256 values from the immutable
  manifest on supported hosts. Managed FFmpeg uses that contract only on Windows x86_64.
  Unsupported platform/architecture combinations fail closed with manual guidance.

Nothing is ever installed or changed without your explicit go-ahead.

On **Windows x86_64**, the web UI's **Updates** action can install the pinned Gyan FFmpeg 8.1.2
essentials GPLv3 build into `%LOCALAPPDATA%\yt_final\ffmpeg`. It never changes `PATH` or replaces
system FFmpeg. Before activation, the updater verifies the exact archive SHA-256 and proves both
`ffmpeg.exe` and `ffprobe.exe` came from those authenticated archive bytes. It rejects an update
while FFmpeg is in use, stages the release, and activates it atomically with rollback.

Managed FFmpeg download is disabled on **macOS** because the previously considered provider limits
its files to educational use. Use a manual or Homebrew FFmpeg installation instead. The app can
use that explicit system installation but does not download or replace it.

### Launching the web UI

- **Windows:** double-click `Start yt_final.bat` (or run `python yt_final.py serve`).
- **macOS:** double-click `Start yt_final.command` (or run `python3 yt_final.py serve` from this directory).

The startup password creates a unique revocable browser session. Sessions have
a 12-hour absolute lifetime, logout revokes only the presented session, and a
server restart invalidates every session. The browser sends the credential only in `X-Token`;
query-string credentials are rejected. Static files, same-origin login, and
minimal health are public, while every other `/api/*` route requires a valid
session.

## Download options (new)

| Flag | Meaning |
|------|---------|
| `--quality` | `1080p` (default) `\| best \| 4k \| 1440p \| 720p \| 480p \| audio` |
| `--codec` | `h264` (default, smallest/edit-ready) `\| av1 \| vp9 \| any` |
| `--section START-END` | Download only a time range, e.g. `0:00-2:00`, `90-210`, `1:00:00-1:02:00` |
| `--encode-mode` | `copy` (remux, default) `\| reencode \| none` |
| `--profile` | encoder profile for `reencode` (see below) |
| `--cookies ~/cookies.txt` | cookie file, kept outside the repo (more reliable than browser on Windows) |
| `--cookies-from-browser` | `chrome/edge/firefox` (close the browser first) |
| `--slow-requests` | 2s pacing between extraction requests (use to cool down a soft-rate-limited IP) |
| `--source-address IP` | bind downloads to a specific local NIC/IP |

## How it hits ~20–30 MiB/s on 1080p (the fast path)

Measured 2026-06-23: a **2-hour 1080p video downloaded at 30 MiB/s average** (583 MiB video stream in 19s, peaks ~100 MiB/s). What makes that work:

- **Client = `android_vr` / `mweb` → direct HTTPS DASH (itag 137+140).** These are the only clients that still return *direct* googlevideo URLs in 2026; `web`/`web_safari` are SABR-forced (HLS-only, ~3–8 MiB/s) and `android`/`ios`/`tv` are SABR/403/DRM. `android_vr` needs no token; `mweb` uses the bgutil token. The tool leads with these.
- **Native downloader, `concurrent_fragment_downloads=32`, `http_chunk_size=10 MiB`** — 32 parallel range requests saturate the CDN. (yt-dlp's per-URL throttle is beaten by spreading across many parallel requests.)
- **aria2c is NOT registered on the default path.** Critical: yt-dlp routes direct `https` DASH components through its HTTP downloader, so globally wiring aria2c would replace native `-N 32` and cap speed. Hybrid applies aria2 only after extraction proves the selection is one progressive file containing both video and audio.
- nsig solved via Node + `yt-dlp-ejs`; PO token via bgutil (`--pot-mode auto`).

If you ever see `[Quality] format_id:` with `hls-...` instead of a numeric `137+140`, you fell back to the slow HLS path (token/geo issue) — check the bgutil server and `--pot-mode`.

### Download-engine policies

Use `--download-engine auto|builtin|hybrid|aria2|turbo` (also available under **Advanced → Speed** in the web UI):

- `auto` / `builtin` — native yt-dlp parallel fragments. This remains the default.
- `hybrid` — resolves the requested format once, preserves its quality, then chooses the safe byte-fetch path: built-in for DASH components/manifests, aria2c for a progressive combined audio+video file, or N_m3u8DL-RE for a proven HLS playlist. Missing, incompatible, or failed optional tools fall back to built-in without a second extraction.
- `aria2` — prefers aria2c only for a progressive combined-media HTTP(S) file; every other stream shape uses built-in.
- `turbo` — prefers N_m3u8DL-RE only for a proven HLS playlist; DASH remains built-in so Turbo cannot silently change the representation or requested quality.

Normal high-quality YouTube selections are separate video+audio DASH streams, so Hybrid intentionally uses built-in yt-dlp for them. It never lowers quality merely to activate aria2.

## Maximum throughput: PO-token provider (bgutil)

Without a PO token, several web clients return 403 while tokenless direct-URL clients remain
available. A **bgutil PO-token provider** can mint GVS tokens for additional fast formats.
Default `auto` mode probes only the configured loopback HTTP endpoint; it never discovers or
executes an app-data/home script.

The running app never changes the Python provider plugin in 1.0.0. The optional provider server
also remains manual because no server source, container digest, and transitive dependency lock are
approved in the application manifest.

If you configure that server yourself, follow its upstream documentation but select a specific
reviewed release or commit, verify its published digest, use a locked dependency graph, and bind
the service only to `127.0.0.1:4416`. Do not use a floating container tag, a branch head, or an
unlocked lifecycle-script install. Script mode likewise requires a locally reviewed, pinned build.
Then select `--pot-mode http` or explicitly opt into `--pot-mode script`.

`--pot-mode auto` uses the configured HTTP server only if it answers on `:4416`; otherwise it uses
the tokenless path. `--pot-mode lazy` starts tokenless and can escalate only to that reachable HTTP
provider. `--pot-mode off` forces tokenless operation. No default mode authorizes script execution.

> **Security note on the HTTP helper.** The app does not install, start, rewrite, or confine this
> server. Operate it separately, bind it only to loopback, and use an OS/container policy pinned to
> reviewed bytes. The helper executes caller-supplied JavaScript by design. Explicit script mode
> opens no port but still executes manually supplied third-party code.
Note: YouTube may still SABR-throttle even with a valid token, and per-IP rate limits apply — the
provider removes the 403 wall and the per-connection cap, but is not a guarantee against server-side throttling.

## Speed test & ISP throttle bypass (web UI)

Some ISPs — especially carrier / 5G home internet — inspect the plaintext TLS hostname (SNI) of
`*.googlevideo.com` and shape only YouTube to ~1 MiB/s while the rest of the line runs full speed.
The **Speed test** tab diagnoses this and tells you the one thing to fix.

**What it does.** Press *Run speed test*. It measures your raw link (Cloudflare), a non-YouTube
Google server (the same-owner control that isolates SNI shaping), and a real YouTube video fetched
two ways — straight and through the local bypass — then gives a plain verdict, a comparison chart,
and one-click actions. It never changes anything until you press a button on a result, and it pauses
the download queue for the ~1 minute it runs.

**Verdicts you may see:**
- *Throttling — bypass fixes it* → one-click **Enable Bypass** (see below).
- *Bypass is on, leave it on* → already handled (may offer a faster mode).
- *Try a proxy/VPN* → local tricks didn't clear it; test a remote proxy or VPN exit.
- *Slow link* / *Clean* → the link itself is slow, or nothing's wrong.

**The local Bypass (no VPN, no account, nothing to install).** Opt-in, default **off**. When on, the
app runs a tiny local proxy (`desync_proxy.py`) that fragments/obscures the video hostname in the TLS
handshake so the ISP can't classify the flow, restoring full speed. It has four modes — `oob`,
`frag`, `frag_oob`, `tlsrec` — and the Speed test **auto-sweeps** them to pick the one that clears *your* ISP,
persisted as `[network] desync_mode` in `config.toml`. Toggle it any time in **Settings → Bypass ISP
throttling**.

**Remote proxy (optional).** Under *Also test a proxy or VPN*, pick a scheme (SOCKS5/5h/4, HTTP) and
paste `IP:PORT` — **passwordless only**. The test measures YouTube through it (re-extracting so the
signed URL matches the proxy's IP) and can save it as your download proxy. Or turn on a system VPN
and re-run: the tab detects the VPN exit and the run-history line shows the before/after.

## Authenticated downloads (optional)

Enable authenticated cookie-based downloads to bypass age-gating, watch-history gating, and soft rate-limit throttling.

### Setup

Create or edit `~/.yt_final/config.toml`:

```toml
[auth]
enabled = true
```

Run any auth command to initialize the cookie store and see the mandatory safety notice:

```bash
python yt_final.py auth status
```

You'll see a one-time warning (shown only once per install):

```
[auth] Authenticated downloads enabled.
  • Use a BURNER Google account — YouTube may rate-limit or suspend accounts
    whose cookies are used with downloaders. This is against YouTube's Terms.
  • Cookies are stored LOCALLY in plaintext (chmod 600), not uploaded to yt_final
    or analytics, and sent only with your configured authenticated requests.
  • You performed the login yourself; yt_final never types your password.
```

### Authentication methods (tier chain)

The tool tries these methods in order:

1. **Tier 1: cookies.txt file** (fastest, no dependencies)
   - Netscape-format file, e.g. from browser extension or VPS curl
   - Override via `cookiefile` key in `[auth]` config or `YT_FINAL_COOKIES` env var
   - No interactive login needed

2. **Tier 2: Managed native-browser profile** (single interactive login, persisted)
   - One-time login via `python yt_final.py auth login` (opens a browser, you log in, then closes)
   - Profile saved to `~/.yt_final/cookies/profile/`
   - Requires an installed **Chrome, Edge, or Brave** (the login opens your real browser; Google blocks automated ones). Playwright is not required.
   - Reused on subsequent runs until invalidated

3. **Tier 3: Auto-extract from browser** (slow, reads live browser cookies)
   - Firefox: extracts directly and works out of the box (no login profile of its own)
   - Chrome/Edge/Brave: **experimental/best-effort**. Windows app-bound cookie
     encryption often prevents the throwaway debugging profile from returning
     any cookies, in which case this tier silently yields nothing and the
     manager falls through. If auto-extract doesn't work for your Chromium
     browser, use the managed profile (`auth login`) or import a `cookies.txt`
     (`auth import`) instead.
   - No extra setup

Reorder via `order = ["file", "profile", "browser"]` in config.

### CLI commands

```bash
# Status: check which tier is available and active
python yt_final.py auth status

# Tier 2 setup: one-time interactive login
python yt_final.py auth login

# Import a cookies.txt file (Tier 1)
python yt_final.py auth import ~/cookies.txt

# Test the current cookies (probe age-gated video)
python yt_final.py auth test

# Clear all stored cookies and login state
python yt_final.py auth clear
```

### Using authenticated downloads in your scripts

Once `enabled = true` and at least one tier is set up, `download` just works:

```bash
python yt_final.py download URL  # automatically uses cookies if available
```

Or use the `--cookies` flag to override. Keep the file outside the working
directory: the engine auto-discovers `./cookies.txt`, and a cookie file is a
live login session, not a config file.

```bash
python yt_final.py download URL --cookies /path/to/custom/cookies.txt
```

### Headless use on a supported host

If a supported Windows or macOS host has no interactive browser session, export
a Netscape-format `cookies.txt` on a trusted machine, transfer it through your
own secure channel, then import it:

```bash
python yt_final.py auth import /secure/path/cookies.txt
python yt_final.py download URL
```

The transfer mechanism, remote host, and its access controls are operator-owned.
This procedure does not make Linux/VPS deployments a supported 1.0.0 target.

### Safety & privacy

- **Burner account required**: YouTube ToS prohibits cookie reuse with downloaders. Accounts caught are rate-limited or suspended. Use a dedicated account.
- **Local plaintext storage**: Cookies live in `~/.yt_final/cookies/` as plaintext with mode 600 (readable by you only). They are not uploaded to yt_final or third-party analytics; cookie values are sent only as part of the authenticated YouTube/Google requests you configure.
- **No credential automation**: yt_final never types your password. Tier 2 (managed profile) opens a browser *you* control; you log in yourself, then it saves your persistent cookies.
- **Tier 2 uses your installed browser**: The one-time managed login opens a native Chrome, Edge, or Brave window that you control; Playwright is not required. Tier 1 (cookies.txt) also has no extra dependencies.

### Top-level flags (must come BEFORE the subcommand)

These attach to the parser itself, not to `download`, so `download URL --pot-mode http` exits 2.
Write `yt-final --pot-mode http download URL`.

| flag | meaning |
| --- | --- |
| `--pot-mode` | PO-token backend: `auto` (default, detect) `\| http \| script \| off \| lazy` |
| `--pot-server URL` | bgutil HTTP provider base URL (default `http://127.0.0.1:4416`) |
| `--output-dir DIR` | where downloads are written |
| `--allow-hooks` | honor an `on_complete` command read from a manifest or config file |
| `--no-update-check` | skip the launch "newer version available" notice |
| `--config FILE` / `--preset NAME` | reusable TOML presets |

## Encoder profiles

Detection-gated with graceful HW→SW fallback (e.g. `av1_nvenc → hevc_nvenc → libx264`).
Profiles whose encoder isn't usable on your machine are skipped automatically.

- **Default**: `auto_h264` — resolves at run time to the fastest working H.264 encoder on
  *your* machine (NVENC, AMF, QSV, VideoToolbox, or libx264). This is what you get when you
  pass no `--profile`; name a specific profile only when you want to override that choice.
- **NVENC**: `premiere_h264_fast`, `premiere_h264_high`, `nvenc_h264_quality`,
  `premiere_h265_4k`, `nvenc_h265_quality` (10-bit), `nvenc_av1_fast`, `nvenc_av1_quality` (Ada/RTX 40+),
  `premiere_h264_fast_cuda` (expert: keeps frames on the CUDA surface).
- **Apple VideoToolbox** (macOS): `vt_h264`, `vt_h264_fast`, `vt_hevc`, `vt_prores_hq`.
- **AMD AMF**: `amf_h264`, `amf_hevc`, `amf_av1`.
- **Intel QSV**: `qsv_h264`, `qsv_hevc`, `qsv_av1`.
- **CPU**: `premiere_h264_cpu` (libx264), `cpu_h265` (libx265 10-bit), `cpu_av1` (libaom),
  `cpu_av1_svt` (SVT-AV1, if your FFmpeg has it).
- **Editing intermediates** (`.mov`): `prores_proxy`, `prores_hq`, `dnxhr_hq`, `dnxhr_hqx`.
- **Remux**: `copy_fast` (mp4), `copy_mkv`.

## 2026 innovation update — URL → edit-ready, in one command

A research swarm vetted 163 ideas down to 26; the result turns the downloader into a
**YouTube→NLE ingest engine**. Everything below is built and tested. See
[`INNOVATION_ROADMAP.md`](INNOVATION_ROADMAP.md) for the full decision record.

**Edit-ready by default.** A normal download now also writes YouTube **chapters as container
chapter markers** and tags **title / uploader / date / source-url / description** into the file, so
clips land in the Premiere/Resolve Media Browser named and dated instead of as opaque `title [id].mp4`.
Disable with `--no-chapters` / `--no-metadata`.

```bash
# Lossless audio pull for editorial (no lossy->lossy; "best" remuxes the native stream)
python yt_final.py download URL --quality audio --audio-format flac
python yt_final.py download URL --quality audio --audio-format best

# Override a video profile's audio (e.g. 24-bit PCM for Avid), normalize loudness to -14 LUFS
python yt_final.py download URL --encode-mode reencode --profile prores_hq \
    --audio-codec pcm_s24le --loudnorm -14

# Subtitles (sidecar | embed | burn) + SponsorBlock chapters + thumbnail cover art
python yt_final.py download URL --sub-langs en,es --sub-mode embed --sponsorblock mark --embed-thumbnail

# Channel/playlist sync that skips what you already have
python yt_final.py download "https://youtube.com/@channel" --download-archive seen.txt
```

**New flags on `download`** (all optional, defaults preserve the old fast path):

| Flag | Meaning |
|------|---------|
| `--audio-format` | audio-only target `mp3\|aac\|opus\|flac\|wav\|m4a\|alac\|best` (`best`=remux, no transcode) |
| `--audio-quality` | audio bitrate / VBR level for the audio extractor |
| `--audio-codec` / `--audio-bitrate` | override a **video** profile's audio (`copy\|aac\|pcm_s24le\|flac\|libopus`); Opus auto-switches to MKV |
| `--no-chapters` / `--no-metadata` | turn off the (default-on) chapter markers / metadata tags |
| `--embed-thumbnail` | embed the thumbnail as cover art |
| `--sub-langs en,es` `--sub-mode sidecar\|embed\|burn` `--sub-auto` | subtitles |
| `--sponsorblock mark\|remove` | SponsorBlock segments as chapters, or cut them |
| `--loudnorm -14` | EBU R128 two-pass loudness normalization |
| `--no-hdr` | don't carry HDR10/HLG color metadata through a re-encode (default: carry it) |
| `--download-archive FILE` | skip already-downloaded ids (section-aware) |
| `--write-sidecar` | write the full `info.json` (MAM provenance) |
| `--organize-by uploader\|date` / `--output-template T` | foldering / naming |
| `--proxy URL` / `--concurrent-fragments N` / `--on-complete "cmd {}"` | network + post hook |
| `--config FILE` / `--preset NAME` | reusable TOML presets (top-level flags) |
| `--allow-hooks` | honor an `on_complete` command coming from a manifest/config file (top-level flag) |

> **`on_complete` runs a program.** Typing `--on-complete` yourself is fine. Reading it out
> of a *file* is not honored unless you also pass `--allow-hooks`, because manifests and
> `config.toml` get shared — pasted into issues, copied from a gist — and `batch --queue`
> persists the hook into the queue for the worker to run later. Ignored hooks are logged,
> and `--dry-run` shows any hook that *is* in effect.

**New subcommands**

```bash
# Declarative batch manifest (the dependency-free "full-stack" surface)
python yt_final.py batch jobs.json            # array of job objects, or {defaults:{...}, jobs:[...]}
python yt_final.py batch jobs.json --dry-run  # print the plan, download nothing
python yt_final.py batch jobs.json --queue    # enqueue instead of running inline

# Persistent durable queue (survives reboots; atomic JSON journal + single-writer lock)
python yt_final.py queue add URL1 URL2 --quality 4k --encode-mode reencode --profile nvenc_av1_quality
python yt_final.py queue add --json '{"urls":["URL"],"loudnorm":-16,"sub_langs":["en"]}'
python yt_final.py queue list
python yt_final.py queue run            # drain the queue
python yt_final.py queue retry-failed

# Local web UI + REST API over the engine + queue (password session, loopback-only)
python yt_final.py serve                # prints a password-login URL (no token in the URL)

# Standalone loudness normalize / format audit
python yt_final.py normalize ./clip.mp4 --lufs -14
python yt_final.py list-formats URL

# Subscribe to a channel via its RSS feed (no API key) and auto-grab new uploads
python yt_final.py watch "https://www.youtube.com/@mkbhd" --enqueue --no-shorts   # one pass -> queue
python yt_final.py watch UCBJycsmduvYEL83R_U4JriQ --interval 1800                 # poll every 30 min
```

**Editorial & self-hosted extras**

```bash
# 🎯 Export YouTube chapters as NLE timeline markers (no other tool does this)
python yt_final.py download URL --export-markers edl     # DaVinci Resolve (* LOC: markers)
python yt_final.py download URL --export-markers all     # edl + Premiere csv + fcpxml

python yt_final.py download URL --proxy-pair             # master + half-res H.264 editing proxy
python yt_final.py download URL --write-nfo              # Kodi/Jellyfin/Plex .nfo sidecar
python yt_final.py download URL --no-shorts              # skip Shorts on channel/playlist pulls
python yt_final.py download URL --notify-url https://ntfy.sh/mytopic   # webhook on completion
```

**New encoder profiles**: `prores_lt`, `prores_4444` (alpha/VFX), `dnxhd_hq_1080p25` (Avid DNxHD 185),
`cpu_av1_svt_filmgrain` (15–25 % bitrate win on grain), `nvenc_av1_vbr` (capped-size AV1).

**Web UI** (`serve`): an "ingest console" dashboard — compose a job (URL, quality, codec, encode
mode, audio/metadata/subs/SponsorBlock/loudnorm), watch a live pipeline with color-coded status, and
retry/clear. Backed by `jobspec.py` (the typed contract), `jobstore.py` (durable queue) and
`webserver.py` (stdlib HTTP). No FastAPI, no build step, no new mandatory dependencies.

## Architecture

- `CapabilityDetector` — FFmpeg + GPU detection; real null-encode probe per HW encoder; `encoder_usable()`.
- `YouTubeDownloader` — speed-tuned yt-dlp opts (native `-N`, nsig runtime, codec/section selection),
  single-extraction download with a retry planner, collision-safe `_unique_path()`.
- `FFmpegProcessor` — profiles, per-family command builder, `output_ext()`, availability-aware
  `encode_with_fallback()`.
- `FinalYouTubeGPUEngine` — orchestrates download → finalize (`run(spec)`): encode/remux + metadata
  + chapters + subtitles + loudnorm + HDR + thumbnail. `download_then_process` is a back-compat adapter.
- `jobspec.py` (`JobSpec`) — the typed job contract shared by CLI, manifest, queue and server.
- `jobstore.py` (`DownloadJournal`) — durable JSON queue: atomic write-temp-rename, single-writer pidfile lock.
- `webserver.py` + `webui/` — stdlib HTTP API and the static "ingest console" web UI (the `serve` tier).

Requires Python 3.11+, the versions permitted by `requirements.txt`, a recent
working FFmpeg/ffprobe pair, and — for full speed — a certified Node or Deno
runtime with `yt-dlp-ejs`. Optional: NVIDIA/AMD/Intel GPU, aria2c, and a manually
operated PO-token provider.
