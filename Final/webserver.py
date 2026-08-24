#!/usr/bin/env python3
"""
Local web UI + REST API for yt_final (full-stack tier C4).

Deliberately dependency-free: stdlib http.server.ThreadingHTTPServer bound to 127.0.0.1,
a static web UI (server/webui/*) that is literally a JobSpec editor, and a single background
worker that drains the DownloadJournal (single-writer discipline). This tier imports lazily
from the `serve` subcommand only, so the CLI fast-path never pays for it.

Security posture (this is why the UI tier is "late"):
- binds only a normally resolved loopback address and rejects foreign Host headers.
- web UI unlocks with a per-launch RANDOM password (printed on startup + written to webui.txt;
  override with the YT_FINAL_WEBUI_PASSWORD env var); successful login returns a session token used
  as X-Token on authenticated API requests (CSRF-safe custom header).
- an exact Origin check rejects cross-site mutations while origin-less authenticated clients work.
"""

import ipaddress
import os
import re
import socket
import sys
import json
import time
import secrets
import uuid
import logging
import threading
import subprocess
import http.server
import concurrent.futures
from pathlib import Path

try:
    from .websessions import SessionStore, retire_legacy_token_file
except ImportError:
    from websessions import SessionStore, retire_legacy_token_file

try:
    from .secureio import atomic_replace_private
except ImportError:
    from secureio import atomic_replace_private

try:
    from .executable_trust import system_helper_executable
except ImportError:
    from executable_trust import system_helper_executable

try:
    from .installer_lock import (
        INSTALLER_BUSY_CODE,
        INSTALLER_BUSY_MESSAGE,
        InstallerBusyError,
        installer_guard,
    )
except ImportError:
    from installer_lock import (
        INSTALLER_BUSY_CODE,
        INSTALLER_BUSY_MESSAGE,
        InstallerBusyError,
        installer_guard,
    )

logger = logging.getLogger(__name__)


def _loopback_ip(value: str):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    return address if address.is_loopback else None


def _resolve_loopback_bind_host(host: str) -> str | None:
    """Resolve a bind name once and return a canonical loopback literal."""
    if not isinstance(host, str):
        return None
    candidate = host.strip()
    if not candidate or "%" in candidate:
        return None
    literal = _loopback_ip(candidate)
    if literal is not None:
        return literal.compressed
    try:
        records = socket.getaddrinfo(candidate, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    resolved = []
    for family, _socktype, _protocol, _canonname, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6) or not sockaddr:
            continue
        address = _loopback_ip(str(sockaddr[0]).split("%", 1)[0])
        if address is None:
            return None
        resolved.append(address)
    if not resolved:
        return None
    # Prefer IPv4 for resolved names because the stock default and launcher have always used it.
    selected = next((address for address in resolved if address.version == 4), resolved[0])
    return selected.compressed


def is_loopback_bind_host(host: str) -> bool:
    """Return whether host is a literal or exclusively resolved loopback address."""
    return _resolve_loopback_bind_host(host) is not None


def _normalize_request_loopback_host(host: str) -> str | None:
    """Canonicalize request-host spellings accepted by the loopback listener."""
    if not isinstance(host, str) or not host:
        return None
    literal = _loopback_ip(host)
    if literal is not None:
        return literal.compressed
    normalized = host.lower().removesuffix(".")
    return "localhost" if normalized == "localhost" else None


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


class _IPv6ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _pick_directory(initialdir=None):
    """Pop a NATIVE OS folder-select dialog on the server machine (which, for the localhost
    default, IS the user's machine) and return the chosen absolute path. Runs in a throwaway
    subprocess so the GUI toolkit never tangles with the HTTP server threads. Returns '' if the
    user cancels, or None if no picker is available (e.g. headless / Tk missing)."""
    script = (
        "import sys\n"
        "try: sys.stdout.reconfigure(encoding='utf-8')\n"
        "except Exception: pass\n"
        "import tkinter, tkinter.filedialog as fd\n"
        "r = tkinter.Tk(); r.withdraw(); r.attributes('-topmost', True); r.update()\n"
        "init = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None\n"
        "d = fd.askdirectory(title='Select download folder', mustexist=False, initialdir=init)\n"
        "r.destroy(); sys.stdout.write(d or '')\n"
    )
    try:
        res = subprocess.run([sys.executable, '-c', script, initialdir or ''],
                             capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=60)
        if res.returncode != 0:
            return None          # crashed -> "no picker available", not a silent phantom cancel
        return (res.stdout or '').strip()
    except Exception:
        return None


def _pick_file(initialdir=None, title='Select cookies.txt', filetypes=None):
    """Native OS open-file dialog on the server machine (same model as _pick_directory).

    Returns absolute path string, '' if cancelled, or None if picker unavailable.
    Default filter prefers cookies / txt; caller can pass filetypes for other uses.
    """
    # filetypes serialized as "Label|*.ext;*.ext2|Label2|*.*" pairs for the subprocess argv.
    ft = filetypes or [
        ('Cookies / text', '*.txt'),
        ('All files', '*.*'),
    ]
    flat = []
    for label, patterns in ft:
        flat.append(str(label))
        flat.append(str(patterns))
    script = (
        "import sys\n"
        "try: sys.stdout.reconfigure(encoding='utf-8')\n"
        "except Exception: pass\n"
        "import tkinter, tkinter.filedialog as fd\n"
        "r = tkinter.Tk(); r.withdraw()\n"
        "try:\n"
        "    r.attributes('-topmost', True)\n"
        "except Exception:\n"
        "    pass\n"
        "r.update()\n"
        "init = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None\n"
        "title = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else 'Select file'\n"
        "args = sys.argv[3:]\n"
        "pairs = list(zip(args[0::2], args[1::2])) if args else [('All files', '*.*')]\n"
        "f = fd.askopenfilename(title=title, initialdir=init, filetypes=pairs)\n"
        "r.destroy(); sys.stdout.write(f or '')\n"
    )
    try:
        res = subprocess.run(
            [sys.executable, '-c', script, initialdir or '', title, *flat],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if res.returncode != 0:
            return None          # crashed -> "no picker available", not a silent phantom cancel
        return (res.stdout or '').strip()
    except Exception:
        return None


def save_uploaded_cookiefile(content: str) -> dict:
    """Write uploaded cookies.txt for a one-job override path (not the Auth store).

    Returns {ok, path?, error?, youtube_cookies?}. Quality-checked like Auth import so
    users don't point jobs at random text files.
    """
    try:
        ok, err, yt_n = _cookie_import_quality(content if isinstance(content, str) else "")
        if not ok:
            return {'ok': False, 'error': err}
        try:
            import cookies_auth
            base = cookies_auth.auth_config_dir() / 'overrides'
        except Exception:
            base = Path.home() / '.yt_final' / 'overrides'
        base.mkdir(parents=True, exist_ok=True)
        # FI4-05: a UNIQUE, private file per upload. The old fixed name aliased every upload to one
        # path, so a second upload silently substituted its cookies into an earlier job (cross-job
        # credential swap), and Path.write_text created it with the ambient umask (0644). Create it
        # O_EXCL|0600 (owner-only) so uploads never collide and never leak to other local accounts.
        data = (content if content.endswith('\n') else content + '\n').encode('utf-8')
        dest = base / f'job_{secrets.token_hex(8)}_cookies.txt'
        fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.close(fd)
            fd = -1
            try:
                atomic_replace_private(dest, data)
            except Exception:
                try:
                    dest.unlink()
                except FileNotFoundError:
                    pass
                raise
        finally:
            if fd >= 0:
                os.close(fd)
        return {
            'ok': True,
            'path': str(dest.resolve()),
            'youtube_cookies': yt_n,
            'message': f'Saved override ({yt_n} YouTube/Google cookie row(s)).',
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _engine_notify_helpers():
    """Lazy import engine helpers (avoid import cost on CLI paths that never serve)."""
    try:
        from yt_final import FinalYouTubeGPUEngine
    except ImportError:
        from Final.yt_final import FinalYouTubeGPUEngine
    return FinalYouTubeGPUEngine


_MAX_PUBLIC_URLS = 500   # per-job URL ceiling on the PUBLIC path only (generous; stops abuse, not batches)


def _web_jobspec_from_payload(payload, *, confine_root=None):
    """Build a JobSpec for the browser surface: strip shell hooks; allow SSRF-checked notify.

    Raises ValueError with a user-facing message if notify_url is present but blocked.
    """
    try:
        from jobspec import JobSpec
    except ImportError:
        from Final.jobspec import JobSpec
    data = dict(payload) if isinstance(payload, dict) else {}
    data.pop('on_complete', None)
    nurl = (data.get('notify_url') or '').strip() or None
    ntmpl = data.get('notify_template') or 'auto'
    if isinstance(ntmpl, str):
        ntmpl = ntmpl.strip().lower() or 'auto'
    else:
        ntmpl = 'auto'
    Eng = _engine_notify_helpers()
    if nurl:
        err = Eng.validate_notify_url(nurl)
        if err:
            raise ValueError(err)
        if ntmpl in ('', 'auto'):
            ntmpl = Eng.guess_notify_template(nurl)
        if ntmpl not in ('discord', 'slack', 'ntfy', 'raw'):
            ntmpl = 'raw'
        data['notify_url'] = nurl
        data['notify_template'] = ntmpl
    else:
        data['notify_url'] = None
        data['notify_template'] = 'raw'
    # Validate / normalize clip range early (fail before queueing a full-video surprise).
    sec = (data.get('section') or '').strip() if isinstance(data.get('section'), str) else data.get('section')
    if sec:
        try:
            from yt_final import YouTubeDownloader
        except ImportError:
            from Final.yt_final import YouTubeDownloader
        rng = YouTubeDownloader._parse_section(sec)
        if not rng:
            raise ValueError(
                f"Invalid clip range '{sec}'. Use START-END like 1:30-3:00 or 90-180 "
                f"(start must be before end)."
            )
        data['section'] = YouTubeDownloader._normalize_section_text(sec) or sec
    else:
        data['section'] = None

    # Passwordless-proxy rule applies to per-job overrides too ('direct'/blank exempt).
    # A truthy non-string proxy (JSON bool/number/list/object) would AttributeError on .strip();
    # the /api/jobs and /api/jobs/rerun handlers only catch (ValueError, PathPolicyError), so it
    # would escape as a 500 / connection reset. Reject it here as a clean 400, mirroring the
    # validate_proxy failure path below. Falsy non-strings (false/0/[]/null) keep coalescing to
    # "" (the "no proxy" cases) — no behavior change there.
    _pxy = data.get("proxy") or ""
    if not isinstance(_pxy, str):
        raise ValueError("proxy must be a string")
    _pxy = _pxy.strip()
    if _pxy and _pxy.lower() != "direct":
        import settings as _settings
        _ok, _err = _settings.validate_proxy(_pxy)
        if not _ok:
            raise ValueError(_err)

    spec = JobSpec.from_dict(data)
    spec.on_complete = None
    # Path confinement is PUBLIC-server-only and is applied HERE, not in the handlers, because this
    # builder is the one chokepoint both routes pass through — and rerun passes the already-MERGED
    # spec, so a handler-level guard would have to be duplicated (and was, in fact, simply missing).
    if confine_root:
        bad = _spec_path_violation(spec, confine_root)
        if bad:
            extra = ' (or the uploaded-cookies folder)' if bad in ('cookiefile', 'cookies_from_browser') else ''
            raise PathPolicyError(
                f'{bad} must be within the server download directory{extra} when exposed publicly')
        # PUBLIC-only URL cap: an untrusted LAN client must not queue an unbounded URL list (huge
        # journal row + a job that runs for hours). Applied ONLY on the confined/public path, never in
        # JobSpec/engine, so the trusted local CLI/web console (which legitimately builds large batches
        # or expands a playlist) is untouched. The public path uses noplaylist=True, so each accepted
        # URL is a single video — a count cap here bounds real work without breaking playlist ingestion.
        if len(spec.urls or []) > _MAX_PUBLIC_URLS:
            raise ValueError(
                f'too many URLs in one job ({len(spec.urls)}); the public server accepts at most '
                f'{_MAX_PUBLIC_URLS} per job. Split the batch.')
    return spec


def _notify_test(url, template=None) -> dict:
    """POST a sample payload to url (SSRF-checked). Never raises."""
    nurl = ""
    try:
        Eng = _engine_notify_helpers()
        nurl = (url or '').strip()
        if not nurl:
            return {'ok': False, 'error': 'URL required'}
        ntmpl = template or 'auto'
        if isinstance(ntmpl, str):
            ntmpl = ntmpl.strip().lower() or 'auto'
        else:
            ntmpl = 'auto'
        if ntmpl in ('', 'auto'):
            ntmpl = Eng.guess_notify_template(nurl)
        if ntmpl not in ('discord', 'slack', 'ntfy', 'raw'):
            ntmpl = 'raw'
        payload = {
            'event': 'job_complete',
            'ok': True,
            'status': 'ok',
            'title': 'yt_final test ping',
            'count': 1,
            'files': [],
            'output': '',
            'error': '',
            'elapsed': 0,
            'urls': ['https://www.youtube.com/watch?v=dQw4w9WgXcQ'],
            'test': True,
        }
        ok, fire_err = Eng._fire_webhook_with_deadline(
            nurl,
            payload,
            ntmpl,
        )
        if not ok:
            return {
                'ok': False,
                'template': ntmpl,
                'error': sanitize_notification_error(
                    fire_err or 'webhook POST failed',
                    nurl,
                ),
            }
        return {
            'ok': True,
            'template': ntmpl,
            'message': (
                f'Test {ntmpl} POST sent to '
                f'{safe_notification_origin(nurl)}'
            ),
        }
    except Exception as e:
        return {
            'ok': False,
            'error': sanitize_notification_error(
                f"{type(e).__name__}: {e}",
                nurl or url,
            ),
        }


def resolve_open_target(path):
    """Normalize a user/job path for 'reveal in file manager'.

    Returns (target: Path|None, mode: 'file'|'dir'|None, error: str|None).
    Pure path logic (no subprocess) so unit tests don't pop Explorer windows.
    - Existing file  -> (file, 'file', None)  # select the file in its folder
    - Existing dir   -> (dir, 'dir', None)
    - Missing file but parent dir exists -> (parent, 'dir', None)
    - Empty / missing everything -> error

    Also tries a case-insensitive basename match in the parent (Windows path quirks /
    Unicode lookalikes in titles) before giving up on a missing file.
    """
    raw = (path or '').strip().strip('"').strip("'")
    if not raw:
        return None, None, 'path required'
    try:
        p = Path(raw).expanduser()
        # resolve(strict=False) so we can still fall back to parent of a deleted file
        p = p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as e:
        return None, None, f'invalid path: {e}'
    try:
        if p.is_file():
            return p, 'file', None
        if p.is_dir():
            return p, 'dir', None
        # Basename match in parent (handles slight title/path drift vs on-disk name)
        parent = p.parent
        if parent.is_dir():
            want = p.name.casefold()
            try:
                for child in parent.iterdir():
                    if child.name.casefold() == want and child.is_file():
                        return child.resolve(), 'file', None
            except OSError:
                pass
            return parent, 'dir', None
    except OSError as e:
        return None, None, str(e)
    return None, None, f'path does not exist: {p}'


def reveal_in_file_manager(path):
    """Open the OS file manager at `path` (file → select it; dir → open folder).

    Runs on the server host (localhost UI = user's machine). Never raises.
    Returns {ok, path, mode} or {ok: False, error}.
    """
    target, mode, err = resolve_open_target(path)
    if err or target is None:
        return {'ok': False, 'error': err or 'path not found'}
    try:
        target_s = str(target)
        helper = system_helper_executable(
            'file-manager',
            platform=sys.platform,
        )
        if helper is None:
            return {
                'ok': False,
                'error': 'trusted file manager helper unavailable',
            }
        helper_s = str(helper)
        popen_kw = {
            'stdout': subprocess.DEVNULL,
            'stderr': subprocess.DEVNULL,
        }
        if sys.platform.startswith('win'):
            popen_kw['creationflags'] = getattr(
                subprocess,
                'CREATE_NO_WINDOW',
                0x08000000,
            )
            command = (
                [helper_s, f'/select,{target_s}']
                if mode == 'file'
                else [helper_s, target_s]
            )
        elif sys.platform == 'darwin':
            command = (
                [helper_s, '-R', target_s]
                if mode == 'file'
                else [helper_s, target_s]
            )
        else:
            # Linux: xdg-open only opens dirs/files with handlers; always open the containing folder
            folder = str(target) if mode == 'dir' else str(target.parent)
            command = [helper_s, folder]
        subprocess.Popen(command, **popen_kw)
        return {'ok': True, 'path': str(target), 'mode': mode}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def open_folder_from_payload(payload, journal=None):
    """Resolve path from the POST body. Accepts `path` and/or `job_id` (+ optional index).

    job_id looks up output_paths from the live journal so the UI still works if the
    client path is stale/wrong encoding — common after title sanitization.
    """
    payload = payload or {}
    path = payload.get('path')
    if not path and journal is not None and payload.get('job_id'):
        job = journal.get(payload.get('job_id'))
        outs = (job or {}).get('output_paths') or []
        try:
            idx = int(payload.get('index') or 0)
        except (TypeError, ValueError):
            idx = 0
        if outs:
            path = outs[min(max(idx, 0), len(outs) - 1)]
    if not path:
        return {'ok': False, 'error': 'path or job_id required'}
    result = reveal_in_file_manager(path)
    # If the client path failed but we have a job_id, retry every output_path.
    if (not result.get('ok')) and journal is not None and payload.get('job_id'):
        job = journal.get(payload.get('job_id'))
        for alt in (job or {}).get('output_paths') or []:
            if alt == path:
                continue
            result = reveal_in_file_manager(alt)
            if result.get('ok'):
                break
        # Last resort: open the job's configured output_dir from the durable spec
        if not result.get('ok') and job:
            od = (job.get('spec') or {}).get('output_dir')
            if od:
                result = reveal_in_file_manager(od)
    return result
# Dual-import the engine + spec + journal whether run as `Final.webserver` or `webserver`.
try:
    from .yt_final import FinalYouTubeGPUEngine, FFmpegProcessor, CapabilityDetector, JobSpec
    from .ffmpeg_updater import ManagedFFmpegUpdater, PUBLIC_FAILURE_MESSAGES
    from .capability import invalidate_capability_cache
    from .jobstore import DownloadJournal, STATUS_QUEUED
    from .notifications import (
        persisted_notification,
        safe_notification_origin,
        sanitize_notification_error,
    )
    from .state_validation import validate_request_id
    from .watchstore import WatchStore
    from .watch import resolve_channel_id, fetch_feed, match_filters, probe_duration
    from .watch_filters import (
        WatchPatternError,
        title_matches,
        validate_title_pattern,
    )
except ImportError:
    from yt_final import FinalYouTubeGPUEngine, FFmpegProcessor, CapabilityDetector, JobSpec
    from ffmpeg_updater import ManagedFFmpegUpdater, PUBLIC_FAILURE_MESSAGES
    from capability import invalidate_capability_cache
    from jobstore import DownloadJournal, STATUS_QUEUED
    from notifications import (
        persisted_notification,
        safe_notification_origin,
        sanitize_notification_error,
    )
    from state_validation import validate_request_id
    from watchstore import WatchStore
    from watch import resolve_channel_id, fetch_feed, match_filters, probe_duration
    from watch_filters import (
        WatchPatternError,
        title_matches,
        validate_title_pattern,
    )

def find_existing_download(video_id, output_dir):
    """Has this video id already been downloaded? Joins the history ledger + the output folder.

    Returns {found, when(ts), quality, codec, size, path} — path/size only when a real file exists.
    """
    result = {"found": False}
    if not video_id:
        return result
    tag = f"[{video_id}]"
    # 1) History ledger — authoritative (carries ts + the spec's quality/codec).
    try:
        from jobstore import HistoryLedger
        for rec in reversed(HistoryLedger().read(limit=500)):   # newest first
            out = rec.get("output") or ""
            spec = rec.get("spec") or {}
            urls = spec.get("urls") or []
            if tag in out or any(video_id in str(u) for u in urls):
                size = None
                have_file = bool(out) and os.path.exists(out)
                if have_file:
                    try:
                        size = os.path.getsize(out)
                    except OSError:
                        have_file = False
                result = {"found": True, "when": rec.get("ts"),
                          "quality": spec.get("quality"), "codec": spec.get("codec"),
                          "size": size, "path": out if have_file else None}
                break
    except Exception:
        pass
    # 2) Scan the output folder (+ one level of uploader/date subfolders) for *[id]* — catches
    #    files not in history and resolves a real, revealable path/size.
    try:
        base = Path(output_dir or "./downloads")
        if base.exists():
            pat = f"*[[]{video_id}[]]*"                          # literal [id] (glob-escaped brackets)
            files = [m for m in base.glob(pat) if m.is_file()]
            for sub in base.glob("*/"):
                files += [m for m in sub.glob(pat) if m.is_file()]
            files = [f for f in files
                     if not (f.name.endswith((".part", ".ytdl", ".temp")) or ".part-Frag" in f.name)]
            if files:
                newest = max(files, key=lambda p: p.stat().st_mtime)
                st = newest.stat()
                if not result["found"]:
                    result = {"found": True, "when": st.st_mtime, "quality": None,
                              "codec": None, "size": st.st_size, "path": str(newest)}
                elif not result.get("path"):
                    result["path"] = str(newest)
                    result["size"] = st.st_size
    except Exception:
        pass
    return result


def _quit_hint_text() -> str:
    """Platform-accurate 'fully quit the browser' phrasing for user-facing messages.
    Mirrors cookie_providers._quit_hint(); macOS keeps its Cmd+Q guidance."""
    try:
        import cookie_providers as _cp
        return _cp._quit_hint()
    except Exception:
        return "Cmd+Q" if sys.platform == 'darwin' else "close every window"


def auth_status_payload() -> dict:
    """Pure helper: build the default cookie/session auth manager and return its status dict.
    Never raises — a broken/misconfigured auth setup degrades to a disabled-looking status
    instead of taking down /api/auth (and this function, being import-light and socket-free,
    is directly unit-testable)."""
    import cookies_auth
    try:
        return cookies_auth.build_default_manager().status()
    except Exception as e:
        return {"enabled": False, "error": str(e), "tiers_available": []}


def _proxy_targets_local(proxy) -> bool:
    """SSRF guard for public servers: True if `proxy`'s host resolves to any private / loopback /
    link-local / reserved / unspecified address (v4 OR v6, including literals like [::1]). Fails
    CLOSED — an unparseable/unresolvable host counts as local (blocked), since we can't prove it is
    safe. (The prior gethostbyname check was IPv4-only, so socks5://[::1] slipped through.)"""
    import ipaddress
    import socket as _sock
    import urllib.parse
    try:
        host = urllib.parse.urlparse(proxy or '').hostname
        if not host:
            return True
        try:
            addrs = [ipaddress.ip_address(host)]                          # numeric literal (v4/v6)
        except ValueError:
            addrs = [ipaddress.ip_address(ai[4][0]) for ai in _sock.getaddrinfo(host, None)]
        if not addrs:
            return True
        return any(a.is_private or a.is_loopback or a.is_link_local
                   or a.is_reserved or a.is_unspecified for a in addrs)
    except Exception:
        return True                                                      # fail closed


def _output_dir_escapes(output_dir, base) -> bool:
    """True if `output_dir` resolves OUTSIDE `base` (path-traversal guard for public servers). Fails
    CLOSED — an unresolvable path counts as escaping."""
    try:
        Path(output_dir).resolve().relative_to(Path(base).resolve())
        return False
    except Exception:
        return True


class PathPolicyError(ValueError):
    """A client-supplied path field points outside the roots a PUBLIC server may touch."""


def _spec_path_violation(spec, root):
    """Name of the first path-bearing field landing outside its allowed root(s), else None.

    Why this exists: only output_dir was ever checked, and only on create — so output_template,
    download_archive, cookiefile and a (browser, profile_path) cookies_from_browser were unchecked
    escapes, and the rerun route skipped even that one guard. cookiefile is the worst of them:
    yt-dlp's close() -> save_cookies() TRUNCATES and rewrites whatever path it names, making an
    unchecked value an arbitrary-file-OVERWRITE primitive, not merely a write-outside-root.

    CRITICAL: each field is resolved with the SAME base and SAME expansion its CONSUMER uses, or the
    guard validates a different location than the one actually opened. In particular a RELATIVE
    download_archive/cookiefile is NOT root-relative: the engine passes it verbatim to yt-dlp, which
    opens it relative to the process CWD (there is no chdir), so it must be validated CWD-relative —
    validating it against `root` (as the first version did) let "archive.txt" pass while yt-dlp
    actually wrote it beside the process, outside the root. And output_dir is expanduser-only in the
    engine (Path(...).expanduser()), NOT expandvars, so "$HOME/x" must be treated as the literal it
    stays. Fails CLOSED on non-string/unresolvable. outtmpl placeholders like %(title)s are inert
    literal text to pathlib, so legitimate templates resolve inside the root and pass."""
    root = Path(root).resolve()
    try:
        import cookies_auth
        cookie_roots = (root, (cookies_auth.auth_config_dir() / 'overrides').resolve())
    except Exception:
        cookie_roots = (root,)

    def _ok(value, base, roots, expand_vars=True):
        # base=None -> resolve the value AS THE CONSUMER DOES: absolute stays, relative is CWD-relative
        # (pathlib .resolve() uses os.getcwd(), exactly where yt-dlp/the engine will open it).
        try:
            if not isinstance(value, str):
                return False
            joined = value if (base is None or os.path.isabs(value)) else str(Path(base) / value)
            s = os.path.expanduser(joined)
            if expand_vars:                       # yt-dlp's expand_path does $VAR; the engine does not
                s = os.path.expandvars(s)
            p = Path(s).resolve()
        except Exception:
            return False
        for r in roots:
            try:
                p.relative_to(r)
                return True
            except (ValueError, OSError):
                continue
        return False

    # output_dir: engine does Path(v).expanduser() then mkdir -> CWD-relative, expanduser only.
    if spec.output_dir is not None and not _ok(spec.output_dir, None, (root,), expand_vars=False):
        return 'output_dir'
    # output_template: yt-dlp joins the outtmpl onto the (already-confined) output dir.
    tmpl_base = spec.output_dir or root
    if spec.output_template is not None and not _ok(spec.output_template, tmpl_base, (root,)):
        return 'output_template'
    # download_archive / cookiefile: handed VERBATIM to yt-dlp, opened CWD-relative (base=None).
    if spec.download_archive is not None and not _ok(spec.download_archive, None, (root,)):
        return 'download_archive'
    if spec.cookiefile is not None and not _ok(spec.cookiefile, None, cookie_roots):
        return 'cookiefile'
    # A bare browser NAME carries no path; only the (browser, profile_path) form does. yt-dlp opens
    # the profile path CWD-relative too.
    cfb = getattr(spec, 'cookies_from_browser', None)
    if cfb is not None and not isinstance(cfb, str):
        try:
            parts = list(cfb)
        except TypeError:
            return 'cookies_from_browser'
        if len(parts) > 1 and parts[1] and not _ok(str(parts[1]), None, cookie_roots):
            return 'cookies_from_browser'
    return None


def network_settings_payload(state) -> dict:
    return {"network": {
        "enabled": bool(state.default_proxy_enabled),
        "proxy": state.default_proxy or "",
        "proxy_set": bool(state.default_proxy),
        "desync": bool(getattr(state, "desync_enabled", False)),
        "desync_mode": getattr(state, "desync_mode", "oob"),
        "doh": bool(getattr(state, "doh_enabled", False)),
        "desync_running": state.desync_running() if hasattr(state, "desync_running") else False,
        "desync_health": state.desync_health() if hasattr(state, "desync_health") else "off",
    }}


def _timed_proxy_fetch(proxy, cap_bytes=10_000_000, hard_s=20.0):
    """~10MB neutral fetch through `proxy` (None=direct). Returns MiB/s or 0.0. Bounded by a socket
    timeout AND a hard wall-clock cap: the socket timeout is a per-recv IDLE bound, so a TRICKLE proxy
    (bytes dribbling in under 15s each) would otherwise stretch the transfer arbitrarily and pin the
    request-handler thread. The progress hook aborts at cap_bytes or hard_s; nopart so the abort can't
    orphan a .part file."""
    import time, tempfile, os, shutil
    try:
        from yt_dlp.utils import DownloadCancelled as _Stop
    except Exception:                          # pragma: no cover - very old yt-dlp
        class _Stop(Exception): pass
    url = "https://speed.cloudflare.com/__down?bytes=10000000"
    d = tempfile.mkdtemp(prefix="ytf_pt_")
    tmp = os.path.join(d, "probe.bin")
    st = {"t0": None}
    def _hook(dd):
        if dd.get("status") != "downloading":
            return
        now = time.monotonic()
        if st["t0"] is None:
            st["t0"] = now
        if (dd.get("downloaded_bytes") or 0) >= cap_bytes or (now - st["t0"]) >= hard_s:
            raise _Stop()
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "noprogress": True, "outtmpl": tmp,
                "overwrites": True, "nopart": True, "socket_timeout": 15,
                "retries": 0, "fragment_retries": 0, "progress_hooks": [_hook]}
        if proxy:
            opts["proxy"] = proxy
        t0 = time.time()
        with yt_dlp.YoutubeDL(opts) as y:
            try:
                y.download([url])
            except _Stop:
                pass
        el = max(time.time() - t0, 1e-3)
        sz = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        return sz / el / 1048576
    except Exception:
        return 0.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- Speed Test measurement primitives (used only by POST /api/speedtest) ---
# A neutral, always-available Creative-Commons test video (Blender's Big Buck Bunny) and a public
# Google Cloud Storage object on a DIFFERENT SNI than *.googlevideo.com. The GCS object is the
# same-owner/different-hostname control that isolates SNI-targeted shaping from a slow link.
SPEEDTEST_VIDEO_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
SPEEDTEST_CEILING_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
# Same-owner (Google), different-SNI-than-*.googlevideo.com controls that ISOLATE SNI-targeted
# shaping from a slow link. This is a LIST, not one object: the previous single GCS clip
# (gtv-videos-bucket) went private (now 403s), which silently forced the verdict onto the Cloudflare
# ceiling — a different CDN whose fat single-flow throughput mislabels a healthy fast link as
# "throttled". Each candidate is a large, public, Range-honoring Google-hosted asset on a distinct
# host; the fetcher tries them in order and returns None only if all fail (verified live 2026-07-20).
SPEEDTEST_GCS_URLS = (
    "https://storage.googleapis.com/kubernetes-release/release/v1.30.0/bin/linux/amd64/kubelet",
    "https://dl.google.com/go/go1.22.0.linux-amd64.tar.gz",
)


def _median(xs):
    """Median of the measured (positive) values, or None if none were measured."""
    vals = sorted(v for v in xs if isinstance(v, (int, float)) and v > 0)
    if not vals:
        return None
    n, mid = len(vals), len(vals) // 2
    return vals[mid] if n % 2 else round((vals[mid - 1] + vals[mid]) / 2, 2)


def _confidence(*groups):
    """'high' only if every decisive group has >=2 samples that agree within ~50%; else 'low'.
    A noisy/single-sample A/B on a jittery 5G line should hedge rather than assert."""
    for xs in groups:
        vals = [v for v in xs if isinstance(v, (int, float)) and v > 0]
        if len(vals) < 2:
            return "low"
        if (max(vals) - min(vals)) > 0.5 * (sum(vals) / len(vals)):
            return "low"
    return "high"


def _timed_media_fetch(url, proxy=None, cap_bytes=48 * 1024 * 1024, timeout=15, headers=None,
                       window_s=5.0, warmup_bytes=768 * 1024):
    """Stream `url` through `proxy` (None=direct) via yt-dlp and return steady-state MiB/s measured
    AFTER a warmup prefix (so TCP slow-start is excluded), or None if it never moved a usable chunk.

    None — never 0.0 — means "couldn't measure": the decision engine must degrade to "couldn't test"
    rather than read a probe failure as an extreme throttle. Bounded by BOTH a post-warmup time
    window and a byte cap (uniform across legs so the A/B compares like with like). Uses nopart + a
    private temp dir removed in finally, so the intentional cancel never leaks a .part file.
    Using yt-dlp keeps direct / local-desync-CONNECT / user-SOCKS all on the real download transport."""
    import time as _t, tempfile, os as _os, shutil, yt_dlp
    try:
        from yt_dlp.utils import DownloadCancelled as _Stop
    except Exception:                          # pragma: no cover - very old yt-dlp
        class _Stop(Exception): pass
    d = tempfile.mkdtemp(prefix="ytf_st_")
    tmp = _os.path.join(d, "probe.bin")
    st = {"t_start": None, "t_warm": None, "b_warm": 0, "t_last": None, "bytes": 0}
    def _hook(dd):
        if dd.get("status") != "downloading":
            return
        b = dd.get("downloaded_bytes") or 0
        now = _t.monotonic()
        if st["t_start"] is None and b > 0:
            st["t_start"] = now
        if st["t_warm"] is None and b >= warmup_bytes:
            st["t_warm"], st["b_warm"] = now, b        # steady-state window opens here
        st["bytes"], st["t_last"] = b, now
        if b >= cap_bytes:
            raise _Stop()
        if st["t_warm"] is not None and (now - st["t_warm"]) >= window_s:
            raise _Stop()
        if st["t_start"] is not None and (now - st["t_start"]) >= window_s + 8:  # hard cap incl. warmup
            raise _Stop()
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "outtmpl": tmp,
            "overwrites": True, "nopart": True, "progress_hooks": [_hook],
            "socket_timeout": timeout, "retries": 0, "fragment_retries": 0}
    if headers:
        opts["http_headers"] = headers
    if proxy:
        opts["proxy"] = proxy
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
    except _Stop:
        pass
    except Exception:
        pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # Prefer the post-warmup steady-state window; but if that window was too short/small (a stall, a
    # truncated window on a very slow line, or a single progress callback), fall back to the whole
    # transfer rather than DISCARDING a real measurement and flipping the verdict to "couldn't test".
    moved = el = None
    if st["t_warm"] is not None and st["t_last"] and st["t_last"] > st["t_warm"]:
        w_moved, w_el = st["bytes"] - st["b_warm"], st["t_last"] - st["t_warm"]
        if w_moved >= 512 * 1024 and w_el >= 0.2:
            moved, el = w_moved, w_el
    if moved is None and st["t_start"] is not None and st["t_last"]:
        moved, el = st["bytes"], st["t_last"] - st["t_start"]   # whole transfer (incl. warmup bytes)
    # Require a real amount over a real duration — the duration floor also rejects the single-callback
    # case whose ~0 elapsed would otherwise yield a garbage hundreds-of-MiB/s rate.
    if not moved or not el or moved < 256 * 1024 or el < 0.2:
        return None
    return round(moved / el / 1048576, 2)


def _speedtest_extract_media(state, cookiefile, proxy=None):
    """Extract ONE real *.googlevideo.com media URL (+ its request headers) for a mid-quality
    video-only format, using the app's cookies/PO-token path. Returns {url,headers,id} or None.
    Extracting once (then fetching that same URL direct vs. bypass) makes the A/B purely about the
    network path and avoids hammering YouTube's extractor / bot-check with repeat extractions.

    `proxy` MUST be threaded through when the fetch will use a proxy: googlevideo signed URLs embed
    the extractor's egress IP (ip=/ipbits, covered by the signature), so a URL extracted at the home
    IP returns 403 if replayed through a proxy's different IP. Extracting THROUGH the proxy yields a
    URL locked to the proxy's IP, which the proxy fetch can then actually download. The local desync
    bypass keeps the same public IP, so it reuses the home-IP URL and does NOT need this."""
    from pathlib import Path as _P
    try:
        from yt_final import YouTubeDownloader, get_detector
    except ImportError:
        from Final.yt_final import YouTubeDownloader, get_detector
    import yt_dlp
    out = _P(state.output_dir or "./downloads")
    det = None
    try:
        if state._engine is not None:
            det = state.engine().detector
    except Exception:
        det = None
    if det is None:
        det = get_detector()
    dl = YouTubeDownloader(det, out_dir=str(out),
                           pot_base_url=getattr(state, "pot_base_url", None),
                           pot_mode=getattr(state, "pot_mode", "auto"))
    opts = dict(dl.get_optimized_ydl_opts(quality="720", audio_only=False,
                                          cookies_from_browser=None, cookiefile=cookiefile,
                                          codec="any", section=None))
    opts.update(quiet=True, no_warnings=True, skip_download=True, progress_hooks=[])
    opts.pop("download_archive", None)
    if proxy:
        opts["proxy"] = proxy               # extract through the proxy so the signed URL is IP-locked to it
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(SPEEDTEST_VIDEO_URL, download=False)
    if info and info.get("_type") == "playlist":
        info = (info.get("entries") or [None])[0] or {}
    fmts = info.get("formats") or []
    vfmts = [f for f in fmts if f.get("url") and f.get("vcodec") not in (None, "none")]
    if not fmts or not vfmts:
        # only storyboards / nothing playable -> nsig/SABR broke every direct-URL client.
        return None
    # Degraded = the known-good canary video no longer offers a DIRECT https DASH video stream — only
    # SABR/HLS fallbacks remain, i.e. nsig/SABR is throttling. A fixed video-itag set is the WRONG
    # check: 140/251 audio are near-universal so it never fires (reports OK on a throttled video), and
    # it false-positives on live/HLS. Presence of a direct https video stream is the real signal.
    direct = any((f.get("protocol") or "").startswith("https") for f in vfmts)
    # closest to 720p, then highest bitrate — a stable single-connection googlevideo stream
    vfmts.sort(key=lambda f: (abs((f.get("height") or 0) - 720), -(f.get("tbr") or 0)))
    fmt = vfmts[0]
    return {"url": fmt["url"], "id": info.get("id"),
            "headers": fmt.get("http_headers") or info.get("http_headers") or {},
            "degraded": not direct,
            "degraded_reason": None if direct else "SABR/HLS only — no direct DASH (throttled)"}


def _speedtest_egress():
    """Best-effort egress identity so the tab can tell 'your ISP' from 'a VPN/proxy exit'.
    Reveals only THIS machine's own outbound IP/ASN (never any user data). Returns {} on failure."""
    import urllib.request
    try:
        req = urllib.request.Request("https://speed.cloudflare.com/meta",
                                     headers={"User-Agent": "yt_final-speedtest"})
        with urllib.request.urlopen(req, timeout=5) as r:
            meta = json.loads(r.read(65536).decode("utf-8", "replace"))
        return {"org": meta.get("asOrganization") or "", "asn": meta.get("asn"),
                "city": meta.get("city") or "", "country": meta.get("country") or ""}
    except Exception:
        return {}


def _speedtest_engine_caps(state):
    """{js_runtime, po_provider, ffmpeg} — the tool-side gates that pin or unlock YouTube speed."""
    caps = {"js_runtime": None, "po_provider": None, "ffmpeg": None}
    try:
        try:
            from yt_final import UpdateChecker
        except ImportError:
            from Final.yt_final import UpdateChecker
        node = (UpdateChecker._resolve_js_runtime("node") or {}).get("version")
        deno = (UpdateChecker._resolve_js_runtime("deno") or {}).get("version")
        caps["js_runtime"] = bool(node or deno)
    except Exception:
        pass
    # PO-token provider: quick TCP knock on its port (default bgutil 127.0.0.1:4416)
    try:
        import socket as _s
        from urllib.parse import urlparse as _up
        base = getattr(state, "pot_base_url", None) or "http://127.0.0.1:4416"
        u = _up(base)
        host, port = (u.hostname or "127.0.0.1"), (u.port or 4416)
        with _s.create_connection((host, port), timeout=0.4):
            caps["po_provider"] = "running"
    except Exception:
        caps["po_provider"] = "down"
    try:
        import shutil as _sh
        caps["ffmpeg"] = bool(_sh.which("ffmpeg"))
    except Exception:
        pass
    return caps


_DESYNC_SWEEP = ("oob", "frag", "frag_oob", "tlsrec")

# The mode auto-sweep tries up to 4 bypass modes, each a same-IP googlevideo media fetch. Ranking a
# mode ("does it clear the shaping?") needs only a short steady-state sample, so cap the EXPLORATORY
# sweep legs well below the full 48 MiB budget to cut the Speed Test's own contribution to the per-IP
# token bucket. The direct baseline and the final confidence round keep full budget for an accurate
# verdict. 16 MiB still yields a valid post-warmup window (>=512 KiB, >=0.2s) for ranking.
_SWEEP_DISCOVERY_CAP = 16 * 1024 * 1024


def run_speedtest(state, user_proxy=None):
    """Orchestrate the bounded network probes and hand the measurement vector to the pure decision
    engine (speedtest.analyze). NEVER persists any config: it temp-restarts the desync child to sweep
    modes, then restores it to the saved desync_enabled/mode via ensure_desync() in a finally.
    `user_proxy` (already validated) is an optional VPN/proxy the user asked to test too."""
    import speedtest as _decide
    m = {"engine": _speedtest_engine_caps(state), "egress": _speedtest_egress(),
         "youtube_direct_mibs": None, "youtube_bypass_mibs": None, "youtube_proxy_mibs": None,
         "proxy_label": None, "proxy_url": None, "bypass_mode": None,
         "desync_enabled": bool(state.desync_enabled), "confidence": "high"}
    m["ceiling_mibs"] = _timed_media_fetch(SPEEDTEST_CEILING_URL)
    # Try each same-owner Google control until one moves real bytes; None only if all are down.
    m["google_nonvideo_mibs"] = None
    for _ctrl in SPEEDTEST_GCS_URLS:
        _r = _timed_media_fetch(_ctrl)
        if _r:
            m["google_nonvideo_mibs"] = _r
            break
    # Use the SAME control-selection the decision engine uses (trust rule), so the sweep's
    # throttled_ish gate can never disagree with the final verdict about whether the line is throttled.
    control = _decide.pick_control(m["google_nonvideo_mibs"], m["ceiling_mibs"]) or 0.0

    cookiefile = None
    try:
        import cookies_auth
        mgr = cookies_auth.build_default_manager()
        cookiefile = mgr.get_cookiefile() if mgr else None
    except Exception:
        cookiefile = None
    media = None
    try:
        media = _speedtest_extract_media(state, cookiefile)   # home-IP URL (for direct + bypass)
    except Exception as e:
        logger.info(f"[speedtest] extraction failed: {e}")
        media = None
    m["extraction_ok"] = bool(media)

    if media:
        murl, mhdr = media["url"], media.get("headers")
        bypass_url = f"http://127.0.0.1:{state.desync_port}"
        directs, bypasses = [], []
        try:
            d0 = _timed_media_fetch(murl, proxy=None, headers=mhdr)
            directs.append(d0)
            throttled_ish = not (d0 and control and d0 >= _decide._CLEAN_RATIO * control)

            # --- mode auto-sweep: find the desync mode that best clears the shaping here ---
            # Try the user's SAVED mode first so we always measure what they currently get (and can
            # early-exit without flapping when it already works); then the other modes.
            import settings as _st
            saved_mode = _st.normalize_desync_mode(getattr(state, "desync_mode", "oob"))
            sweep_order = [saved_mode] + [x for x in _DESYNC_SWEEP if x != saved_mode]
            by_mode, best_mode, best_bypass, cur_mode = {}, None, None, None
            for mode in sweep_order:
                if not state.restart_desync(mode):
                    cur_mode = None            # restart failed: the child is NOT on `mode` (it's down)
                    continue
                cur_mode = mode
                # Exploratory ranking only -> reduced byte budget (the confidence round below re-measures
                # the winner at full budget). Cuts up to 4x48 MiB of same-IP bucket load per click.
                b = _timed_media_fetch(murl, proxy=bypass_url, headers=mhdr, cap_bytes=_SWEEP_DISCOVERY_CAP)
                if b:
                    by_mode[mode] = b
                if b and (best_bypass is None or b > best_bypass):
                    best_bypass, best_mode = b, mode
                # stop early once a mode clearly clears it, or if the line isn't throttled at all
                if not throttled_ish:
                    break
                if b and d0 and b > _decide._BYPASS_WIN * d0:
                    break
            if best_bypass:
                bypasses.append(best_bypass)

            # --- confidence: one more interleaved direct/bypass round on the winning mode ---
            if best_mode:
                if cur_mode != best_mode or not state.desync_running():
                    state.restart_desync(best_mode)
                directs.append(_timed_media_fetch(murl, proxy=None, headers=mhdr))
                if state.desync_running():
                    bypasses.append(_timed_media_fetch(murl, proxy=bypass_url, headers=mhdr))

            # --- adaptive re-sampling: if the direct/bypass samples STILL disagree (a jittery
            #     googlevideo edge — bursts then stalls), take a few MORE bounded rounds to steady the
            #     median. Fires ONLY on low confidence and is HARD-CAPPED at 4 direct probes on a
            #     reduced byte budget, so a jittery line can't balloon same-IP load — over-sampling a
            #     shaped IP can itself depress the rate, so we resample sparingly, then let the
            #     variability note (analyze) carry the honest spread rather than chasing a stable point. ---
            _MAX_DIRECT = 4
            while (best_mode and len(directs) < _MAX_DIRECT
                   and _confidence(directs, bypasses) == "low"):
                if not state.desync_running():
                    state.restart_desync(best_mode)
                directs.append(_timed_media_fetch(murl, proxy=None, headers=mhdr,
                                                  cap_bytes=_SWEEP_DISCOVERY_CAP))
                if state.desync_running():
                    bypasses.append(_timed_media_fetch(murl, proxy=bypass_url, headers=mhdr,
                                                       cap_bytes=_SWEEP_DISCOVERY_CAP))

            m["youtube_direct_mibs"] = _median(directs)
            # Direct-flow sample spread (min..max) so the UI can show a range, not a lone median:
            # googlevideo pacing bursts+stalls, so back-to-back direct probes legitimately disagree.
            _pos_d = [v for v in directs if isinstance(v, (int, float)) and v > 0]
            if _pos_d:
                m["youtube_direct_lo"], m["youtube_direct_hi"] = min(_pos_d), max(_pos_d)
            m["youtube_bypass_mibs"] = _median(bypasses)
            m["bypass_mode"] = best_mode
            m["desync_mode"] = saved_mode                 # the mode the user currently runs
            m["saved_bypass_mibs"] = by_mode.get(saved_mode)  # what that mode yields (for bypass_active)
            m["confidence"] = _confidence(directs, bypasses) if best_mode else "low"

            # --- user proxy/VPN leg: RE-EXTRACT through the proxy (googlevideo URLs are IP-locked) ---
            if user_proxy:
                m["proxy_label"] = m["proxy_url"] = user_proxy
                pmedia = None
                try:
                    pmedia = _speedtest_extract_media(state, cookiefile, proxy=user_proxy)
                except Exception as e:
                    logger.info(f"[speedtest] proxy extraction failed: {e}")
                if pmedia:
                    m["youtube_proxy_mibs"] = _timed_media_fetch(
                        pmedia["url"], proxy=user_proxy, headers=pmedia.get("headers"))
        finally:
            # Restore the child to the SAVED config+mode. ensure_desync()/start_desync() are
            # idempotent (early-return if a child is already running), so after the sweep left the
            # child on some other mode they would NOT switch it back — restart explicitly on the
            # saved mode when enabled, else stop it.
            if state.desync_enabled:
                state.restart_desync(state.desync_mode)
            else:
                state.stop_desync()

    m["desync_health"] = state.desync_health()
    result = _decide.analyze(m)
    result["measurements"] = m
    return result


def updates_report_payload(check_pypi=True, pot_base_url=None, force=False) -> dict:
    """Toolchain + pip version report for the Updates tab. Never raises.

    force=False reuses a short-TTL scan cache (fast tab reopen). force=True re-probes tools.
    """
    try:
        try:
            from yt_final import UpdateChecker
        except ImportError:
            from Final.yt_final import UpdateChecker
        report = UpdateChecker.scan(
            check_pypi=bool(check_pypi),
            pot_base_url=pot_base_url,
            include_optional_tools=True,
            force=bool(force),
        )
        # Prefer original scan time when serving a cached report
        if report.get('cached') and report.get('cache_age_s') is not None:
            report['checked_at'] = time.time() - float(report['cache_age_s'])
        else:
            report['checked_at'] = time.time()
        report['ok'] = True
        return report
    except Exception as e:
        return {
            'ok': False,
            'error': str(e),
            'pip': [],
            'info': [],
            'warnings': [str(e)],
            'summary': {'outdated_count': 0, 'outdated': [], 'missing_pip': [], 'warning_count': 1, 'ok': False},
            'checked_at': time.time(),
        }


def invalidate_update_scan_cache():
    """Drop only the short-lived report cache; validated release metadata remains reusable."""
    try:
        from yt_final import UpdateChecker
    except ImportError:
        from Final.yt_final import UpdateChecker
    UpdateChecker.invalidate_cache()


def seed_update_ffmpeg_release_cache(version):
    try:
        from yt_final import UpdateChecker
    except ImportError:
        from Final.yt_final import UpdateChecker
    UpdateChecker.record_ffmpeg_release(version)


def apply_ffmpeg_update(pot_base_url=None) -> tuple[int, dict]:
    """Install from fixed trusted endpoints and return an HTTP status/payload pair."""
    try:
        result = ManagedFFmpegUpdater().install(busy=FFmpegProcessor.ffmpeg_busy)
    except InstallerBusyError:
        return 409, {
            'ok': False,
            'code': INSTALLER_BUSY_CODE,
            'error': INSTALLER_BUSY_MESSAGE,
        }
    except Exception:
        logger.exception('Managed FFmpeg updater raised unexpectedly')
        result = {'ok': False, 'status': 'failed'}

    status = result.get('status')
    if status == 'busy':
        return 409, {
            'ok': False,
            'code': INSTALLER_BUSY_CODE,
            'error': INSTALLER_BUSY_MESSAGE,
        }
    if status == 'unsupported':
        return 400, {
            'ok': False,
            'code': 'unsupported',
            'error': (
                'Managed FFmpeg is certified only for Windows x86_64; '
                'install FFmpeg manually (Homebrew is recommended on macOS).'
            ),
        }
    if result.get('ok'):
        try:
            invalidate_update_scan_cache()
            seed_update_ffmpeg_release_cache(result.get('version'))
        except Exception:
            logger.exception('FFmpeg post-install update cache invalidation failed')
        for invalidator in (invalidate_capability_cache,):
            try:
                invalidator()
            except Exception:
                logger.exception('FFmpeg post-install cache invalidation failed')
        version = result.get('version')
        public_result = {
            'status': 'installed',
            'version': version,
            'checksum_verified': bool(result.get('checksum_verified')),
            'message': f'Managed FFmpeg {version} installed' if version else 'Managed FFmpeg installed',
        }
        report = updates_report_payload(
            check_pypi=True, pot_base_url=pot_base_url, force=True)
        return 200, {'ok': True, 'result': public_result, 'report': report}

    errors = {
        'busy': 'FFmpeg is busy',
        'lock_conflict': 'FFmpeg update lock conflict',
        'failed': 'Managed FFmpeg update failed',
    }
    failure_code = result.get('code') or status or 'failed'
    if failure_code in PUBLIC_FAILURE_MESSAGES:
        errors['failed'] = PUBLIC_FAILURE_MESSAGES[failure_code]
    if status in ('busy', 'lock_conflict'):
        http_status = 409
    elif failure_code == 'network':
        http_status = 502
    elif failure_code in ('checksum', 'archive', 'probe'):
        http_status = 422
    else:
        http_status = 500
    report = updates_report_payload(check_pypi=True, pot_base_url=pot_base_url, force=True)
    return http_status, {
        'ok': False,
        'code': failure_code,
        'error': errors.get(status, errors['failed']),
        'report': report,
    }


_PACKAGE_SELECTION_UNSET = object()
PER_PACKAGE_DISABLED_CODE = 'per-package-disabled'
PER_PACKAGE_DISABLED_MESSAGE = (
    'Per-package updates are disabled; install a future reviewed yt_final release'
)


def apply_pip_updates(
        packages=_PACKAGE_SELECTION_UNSET, mode='auto', pot_base_url=None) -> dict:
    """Return the 1.0.0 Python-update freeze before any side-effect boundary."""
    del pot_base_url
    if packages is not _PACKAGE_SELECTION_UNSET:
        raise ValueError(PER_PACKAGE_DISABLED_MESSAGE)
    normalized_mode = (mode or 'auto').strip().lower()
    if normalized_mode not in ('auto', 'install', 'update', 'all'):
        normalized_mode = 'auto'
    try:
        try:
            from managed_env import (
                CERTIFIED_RUNTIME_UNAVAILABLE_CODE,
                CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE,
                require_certified_python_runtime_activation,
            )
            from tool_manifest import UnsupportedManagedInstall
        except ImportError:
            from Final.managed_env import (  # type: ignore
                CERTIFIED_RUNTIME_UNAVAILABLE_CODE,
                CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE,
                require_certified_python_runtime_activation,
            )
            from Final.tool_manifest import UnsupportedManagedInstall  # type: ignore
        require_certified_python_runtime_activation()
    except UnsupportedManagedInstall:
        pass
    return {
        'ok': False,
        'updated': [],
        'mode': 'certified-release',
        'requested_mode': normalized_mode,
        'code': CERTIFIED_RUNTIME_UNAVAILABLE_CODE,
        'error': CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE,
        'log': '',
    }


_MAX_COOKIE_BYTES = 1_000_000  # ~1 MB — cookies.txt is a few KB; this is a safety cap

# How long shutdown waits for the queue worker to unwind an in-flight job (so _run_ffmpeg's finally
# reaps the ffmpeg child). The engine polls the cancel probe every 0.3s and the probe self-throttles
# to 0.5s, so a healthy job aborts in ~1s; this is the ceiling before we give up and exit anyway.
WORKER_SHUTDOWN_JOIN_S = 12.0
NOTIFICATION_SHUTDOWN_TIMEOUT_S = 5.0


def _join_worker_and_shutdown_notifications(state, worker) -> bool:
    """Unwind queue work, then fence only an engine that already exists."""
    try:
        if worker.is_alive():
            worker.join(timeout=WORKER_SHUTDOWN_JOIN_S)
    except Exception:
        logger.exception("[shutdown] queue worker join failed")
    cached_engine = getattr(state, '_engine', None)
    if cached_engine is None:
        return True
    try:
        complete = bool(cached_engine.shutdown_notifications(
            timeout=NOTIFICATION_SHUTDOWN_TIMEOUT_S,
        ))
    except Exception:
        logger.exception("[shutdown] notification dispatcher shutdown failed")
        return False
    if not complete:
        logger.warning(
            "[shutdown] notification delivery exceeded %.1fs budget",
            NOTIFICATION_SHUTDOWN_TIMEOUT_S,
        )
    return complete


def _ensure_auth_enabled_in_config() -> None:
    """Turn on [auth].enabled in ~/.yt_final/config.toml after a successful cookie import.

    CookieManager.get_cookiefile() short-circuits when enabled is false (the default), so
    importing cookies without enabling auth is a silent no-op for downloads. Importing is an
    explicit user action that implies they want auth used — flip the flag (create or patch
    config.toml) without clobbering other tables/keys.
    """
    import cookies_auth
    base = cookies_auth.auth_config_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / "config.toml"
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        text = ""
    # Table-aware check: only [auth].enabled counts. A file-wide regex would be fooled by
    # OTHER tables' `enabled = true` (e.g. [network].enabled), silently skipping auth-enable.
    try:
        import tomllib
        data = tomllib.loads(text) if text.strip() else {}
        auth = data.get("auth", {})
        if isinstance(auth, dict) and auth.get("enabled") is True:
            return
    except Exception:
        pass  # unparseable — fall through to a best-effort scoped text edit
    if not text.strip():
        path.write_text("[auth]\nenabled = true\n", encoding="utf-8")
        return
    m = re.search(r"(?m)^\s*\[auth\]\s*$", text)
    if m:
        # Edit ONLY inside the [auth] table body: from after its header to the next table header/EOF.
        start = m.end()
        nxt = re.search(r"(?m)^\s*\[", text[start:])
        end = start + nxt.start() if nxt else len(text)
        body = text[start:end]
        if re.search(r"(?m)^\s*enabled\s*=", body):
            body = re.sub(r"(?m)^\s*enabled\s*=\s*.*$", "enabled = true", body, count=1)
        else:
            body = "\nenabled = true" + body
        path.write_text(text[:start] + body + text[end:], encoding="utf-8")
        return
    # No [auth] table — append one.
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "\n[auth]\nenabled = true\n", encoding="utf-8")


def _cookie_import_quality(text: str) -> tuple[bool, str, int]:
    """Lightweight Netscape cookies.txt check before writing.

    Returns (ok, error_or_empty, youtubeish_line_count).
    """
    text = text if isinstance(text, str) else ""
    if not text.strip():
        return False, "empty cookie content", 0
    if len(text.encode("utf-8", "ignore")) > _MAX_COOKIE_BYTES:
        return False, "cookie file too large", 0
    yt_lines = 0
    tab_lines = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Netscape rows are tab-separated: domain flag path secure expiry name value
        if "\t" in line:
            tab_lines += 1
            low = line.lower()
            if "youtube.com" in low or "google.com" in low or "youtu.be" in low:
                yt_lines += 1
    if tab_lines == 0:
        return (
            False,
            "Not a Netscape cookies.txt (expected tab-separated rows). "
            "Export with a “Get cookies.txt” browser extension.",
            0,
        )
    if yt_lines == 0:
        return (
            False,
            "No YouTube/Google cookies found in the file. Log into YouTube in the browser, then re-export.",
            0,
        )
    return True, "", yt_lines


def auth_from_browser(browser: str = "auto") -> dict:
    """Pull YouTube cookies from an installed browser into the auth store.

    Uses Firefox (yt-dlp cookie jar) or Chromium-family (CDP snapshot). Never raises.
    """
    try:
        import cookies_auth
        import cookie_providers as cp

        name = (browser or "auto").strip().lower() or "auto"
        if name not in ("auto", "firefox", "chrome", "edge", "brave"):
            return {"ok": False, "error": "browser must be auto, firefox, chrome, edge, or brave"}

        prov = cp.BrowserExtractProvider(browser=name)
        detected = prov.list_detected()
        if not detected:
            return {
                "ok": False,
                "error": (
                    "No supported browser profile found. "
                    "Install Chrome/Firefox/Brave, log into YouTube there, "
                    "then try again — or import a cookies.txt export."
                ),
                "browsers_detected": [],
            }

        path = prov.acquire()
        if not path:
            return {
                "ok": False,
                "error": (
                    "Could not read browser cookies. Fully quit the browser "
                    "(not just the window) and retry, or export cookies.txt manually."
                ),
                "browsers_detected": detected,
            }

        active = cookies_auth.replace_active_cookiefile(source=Path(path))
        try:
            _ensure_auth_enabled_in_config()
        except Exception:
            pass

        # Count YT-ish rows for the UI message
        yt_n = 0
        try:
            for line in Path(active).read_text(encoding="utf-8", errors="ignore").splitlines():
                low = line.lower()
                if "\t" in line and ("youtube.com" in low or "google.com" in low):
                    yt_n += 1
        except Exception:
            yt_n = 0

        return {
            "ok": True,
            "path": str(active),
            "source": "browser",
            "browsers_detected": detected,
            "youtube_cookies": yt_n,
            "message": (
                f"Imported from browser ({yt_n} YouTube/Google cookie row(s)). "
                "Click Test to verify the session."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def auth_login_session() -> dict:
    """Open real Chrome/Edge/Brave for human YouTube login, then export cookies.

    Does NOT use Playwright (Google blocks automation). Blocks until the login
    browser process exits, then CDP-exports the managed profile into active.txt.
    """
    try:
        import cookies_auth
        import cookie_providers as cp

        launch = cp.native_browser_login()
        if not launch.get("ok"):
            return {
                "ok": False,
                "error": launch.get("error") or "Login browser failed to open",
                "code": launch.get("code") or "launch_failed",
            }

        browser = launch.get("browser") or "chrome"
        profile_dir = launch.get("profile_dir")
        path = cp._export_managed_profile(profile_dir, browser_name=browser)
        if not path:
            # Distinguish "you did not sign in" from "we could not READ the profile you signed
            # into". They were reported identically, so a profile refused for its directory
            # permissions told the user to go and sign in again -- the one thing that could not
            # help, and the one thing they had already done correctly.
            reason = cp.last_export_failure()
            if reason:
                return {
                    "ok": False,
                    "error": (
                        "You signed in, but the login profile could not be read back: "
                        f"{reason}. This is a problem with the profile directory, not with your "
                        "sign-in — see docs/TROUBLESHOOTING.md. Importing a cookies.txt works "
                        "regardless."
                    ),
                    "code": "profile_unreadable",
                }
            return {
                "ok": False,
                "error": (
                    "Browser closed, but no YouTube login cookies were found. "
                    f"Sign into YouTube fully in that window, then fully quit it ({_quit_hint_text()}) "
                    "and try Login again — or use Import from browser."
                ),
                "code": "not_signed_in",
            }

        active = cookies_auth.replace_active_cookiefile(source=Path(path))
        try:
            _ensure_auth_enabled_in_config()
        except Exception:
            pass

        yt_n = 0
        try:
            for line in Path(active).read_text(encoding="utf-8", errors="ignore").splitlines():
                low = line.lower()
                if "\t" in line and ("youtube.com" in low or "google.com" in low):
                    yt_n += 1
        except Exception:
            pass

        message = (
            f"Login complete via {browser} ({yt_n} cookie row(s) saved). "
            "Click Test to verify."
        )
        # A snapshot that could not be deleted is recorded even when the read SUCCEEDS, and until
        # now nothing read it on this path: the reason was written to a module global that only the
        # failure branch above ever looked at. The leftover directory holds a copy of the user's
        # YouTube session, so telling them about it is the entire reason it is recorded.
        leftover = cp.last_export_failure()
        if leftover and "could not delete the temporary profile snapshot" in leftover:
            message += f" Note: {leftover}"

        return {
            "ok": True,
            "path": str(active),
            "source": "profile",
            "browser": browser,
            "youtube_cookies": yt_n,
            "leftover_snapshot": leftover if (
                leftover and "could not delete the temporary profile snapshot" in leftover
            ) else None,
            "message": message,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def auth_import_cookies(content: str) -> dict:
    """Write an uploaded cookies.txt to the auth store's active.txt. Never raises."""
    try:
        import cookies_auth
        ok, err, yt_n = _cookie_import_quality(content if isinstance(content, str) else "")
        if not ok:
            return {"ok": False, "error": err}
        text = content if isinstance(content, str) else ""
        # newline='': an uploaded cookies.txt already carries CRLF (every Windows browser extension
        # emits it), and default translation turned each '\n' into '\r\n' — i.e. '\r\r\n' doubled
        # carriage returns in the stored jar. Store the upload byte-for-byte instead.
        dest = cookies_auth.replace_active_cookiefile(data=text.encode("utf-8"))
        # Without enabled=true, get_cookiefile() ignores the file — enable after explicit import.
        try:
            _ensure_auth_enabled_in_config()
        except Exception:
            pass
        return {
            "ok": True,
            "path": str(dest),
            "youtube_cookies": yt_n,
            "message": (
                f"Imported {yt_n} YouTube/Google cookie row(s). "
                "Click Test to verify the session against YouTube."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


WEBUI_DIR = Path(__file__).resolve().parent / "webui"
_CONTENT_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
                  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon'}


# Simple web-UI password (non-coder friendly). Override with env YT_FINAL_WEBUI_PASSWORD.
import secrets
DEFAULT_WEBUI_PASSWORD = secrets.token_urlsafe(12)


def webui_password() -> str:
    return (os.environ.get('YT_FINAL_WEBUI_PASSWORD') or DEFAULT_WEBUI_PASSWORD).strip() or DEFAULT_WEBUI_PASSWORD



#: Failed-login throttling. The DEFAULT password is secrets.token_urlsafe(12) -- about 96 bits --
#: and no amount of guessing touches that. The risk appears only when an operator sets
#: YT_FINAL_WEBUI_PASSWORD to something memorable AND reaches the server through the SSH/WireGuard
#: tunnel the README blesses. Publishing the project also publishes the exact request shape, so the
#: cost of an online guessing attempt drops to writing a for-loop.
#:
#: Deliberately modest: a small delay per miss, then a short lock after a run of them. This is a
#: local single-user application, and locking its owner out of their own UI would be a worse bug
#: than the one being prevented -- so the lock is measured in seconds and any success clears it.
_LOGIN_MISS_DELAY_S = 0.25
_LOGIN_FAILURE_LIMIT = 10
_LOGIN_LOCK_SECONDS = 30.0
#: Below this, a hand-chosen password is weak enough to be worth saying so out loud.
_WEAK_PASSWORD_LENGTH = 16


class LoginThrottle:
    """Per-process record of consecutive failed unlock attempts."""

    def __init__(self):
        self._lock = threading.Lock()
        self._failures = 0
        self._locked_until = 0.0

    def retry_after(self) -> float:
        """Seconds the caller must wait, or 0.0 when a guess is allowed now."""
        with self._lock:
            return max(0.0, self._locked_until - time.monotonic())

    def record_failure(self) -> float:
        """Count a miss. Returns the lock duration if this one triggered a lock."""
        with self._lock:
            self._failures += 1
            if self._failures < _LOGIN_FAILURE_LIMIT:
                return 0.0
            self._locked_until = time.monotonic() + _LOGIN_LOCK_SECONDS
            self._failures = 0
            return _LOGIN_LOCK_SECONDS

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._locked_until = 0.0


def warn_if_webui_password_is_weak() -> str | None:
    """Return a warning when the operator-supplied password is short, else None.

    Deliberately a warning and not a refusal: rejecting it would lock someone out of their own
    application on upgrade, which is a worse outcome than the weak password it would prevent.
    """
    override = (os.environ.get('YT_FINAL_WEBUI_PASSWORD') or '').strip()
    if not override or len(override) >= _WEAK_PASSWORD_LENGTH:
        return None
    return (
        f"YT_FINAL_WEBUI_PASSWORD is {len(override)} characters. The generated default is ~16 "
        "random characters; a short one is guessable if you ever expose this server through a "
        "tunnel. Failed attempts are slowed and briefly locked, which does not make a weak "
        "password safe."
    )


def check_webui_password(password) -> bool:
    """Compare the UI unlock password (length-safe)."""
    got = str(password or '').encode('utf-8')
    want = webui_password().encode('utf-8')
    if len(got) != len(want):
        secrets.compare_digest(want, want)  # keep work roughly similar
        return False
    return secrets.compare_digest(got, want)


class ServerState:
    def __init__(self, output_dir, queue_file=None, token=None, pot_base_url=None, pot_mode='auto',
                 allow_public=False, sessions: SessionStore | None = None):
        self.output_dir = output_dir
        self.login_throttle = LoginThrottle()
        self.journal = DownloadJournal(queue_file)
        self.sessions = sessions if sessions is not None else SessionStore()
        self.pot_base_url = pot_base_url
        self.pot_mode = pot_mode
        self.allow_public = allow_public
        import settings as _settings
        _net = _settings.load_network_settings()
        self.default_proxy_enabled = _net["enabled"]
        self.default_proxy = _net["proxy"]
        # Local DPI-desync proxy (anti-throttle): opt-in; when on, the server runs
        # desync_proxy.py as a child and routes downloads through it. See start_desync().
        self.desync_enabled = _net.get("desync", False)
        self.desync_mode = _net.get("desync_mode", "oob")  # oob | frag | frag_oob | tlsrec (mode auto-sweep)
        self.doh_enabled = _net.get("doh", False)          # opt-in DNS-over-HTTPS (1.1.1.1) in the desync child
        self.desync_port = int(os.getenv("YT_FINAL_DESYNC_PORT", "8799"))
        self.desync_proc = None
        self._desync_lock = threading.Lock()  # serialize check-then-act on the child process
        self._speedtest_lock = threading.Lock()  # only one Speed Test run at a time (temp-starts the child)
        self.stop = threading.Event()
        self._engine = None
        self._engine_lock = threading.Lock()
        # background engine warm-up coordination (so /api/gpu never blocks a request thread on the
        # ~5s GPU probe). _engine_ready is set once warm-up finishes (success OR failure).
        self._engine_ready = threading.Event()
        self._warm_started = threading.Event()
        self._has_lock = self.journal.acquire_run_lock()
        # live per-job download progress (in memory; not persisted — too write-heavy for the journal)
        self.progress = {}
        # Per-job map of concurrent sub-downloads keyed by the worker thread id that reports them.
        # A parallel job (download_parallel) runs one download() per URL on its own pool thread, and
        # each thread's yt-dlp progress hook fires the SAME shared progress_sink. Keying the raw
        # per-URL events by thread id (instead of only by job id) stops URL A's numbers from
        # clobbering URL B's, and lets set_progress aggregate them into one coherent job-level view.
        self._progress_subs = {}
        # Monotonic generation bumped on every progress mutation. Folds into the /api/jobs change
        # token so a poll can 304 while a job is running only when its live progress is unchanged.
        self._progress_gen = 0
        self._progress_lock = threading.Lock()
        # channel watches (auto-detect new uploads via RSS)
        self.watches = WatchStore()
        self.watch_interval = int(os.getenv("YT_FINAL_WATCH_INTERVAL", "900"))  # seconds (15 min)
        # bounded IO pool for blocking network calls (resolve / fetch_feed / poll) so the
        # request thread and the poll cycle never serialize on ~15s-each network round-trips.
        self._io_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='watch-io')
        # Per-watch in-flight guard for poll_watch(): a poll already running for a given watch id
        # makes a concurrent poll of the same watch a fast no-op, so repeated /api/watches/poll
        # requests (many tabs, rapid clicks, a 25s-timed-out request whose future keeps running)
        # can't stack duplicate ~15s RSS-fetch+probe work on the 4-worker IO pool.
        self._polls_inflight = set()
        self._polls_lock = threading.Lock()

    def set_network_settings(self, enabled, proxy, desync=None, desync_mode=None, doh=None):
        """Update the network settings in-memory AND persist to config.toml.

        `desync` (opt-in local DPI-desync proxy), `desync_mode`, and `doh` (DNS-over-HTTPS in the
        desync child) are left unchanged when None.
        """
        import settings as _settings
        proxy = (proxy or "").strip()
        enabled = bool(enabled) and bool(proxy)  # can't enable remote proxy with no url
        if desync is not None:
            self.desync_enabled = bool(desync)
        if desync_mode is not None:
            self.desync_mode = _settings.normalize_desync_mode(desync_mode)
        if doh is not None:
            self.doh_enabled = bool(doh)
        _settings.save_network_settings(enabled, proxy, config_dir=None,
                                        desync=self.desync_enabled, desync_mode=self.desync_mode,
                                        doh=self.doh_enabled)
        self.default_proxy_enabled = enabled
        self.default_proxy = proxy
        self.ensure_desync()

    # --- local DPI-desync proxy lifecycle (anti-throttle, no VPN) ---

    def _desync_script(self):
        return str(Path(__file__).resolve().parent / "desync_proxy.py")

    def desync_running(self):
        return bool(self.desync_proc and self.desync_proc.poll() is None)

    def start_desync(self, mode=None):
        """Launch desync_proxy.py as a child on 127.0.0.1:desync_port (idempotent, thread-safe).
        `mode` (whitelisted) overrides the saved desync_mode — used by the Speed Test mode sweep."""
        import settings as _settings
        mode = _settings.normalize_desync_mode(mode or self.desync_mode)
        with self._desync_lock:
            if self.desync_running():
                return
            try:
                kwargs = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)} if os.name == 'nt' else {}
                self.desync_proc = subprocess.Popen(
                    [sys.executable, self._desync_script(),
                     "--port", str(self.desync_port), "--mode", mode]
                    + (["--doh"] if self.doh_enabled else []),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
                # Wait (briefly) until the child is actually listening, so health/status is accurate
                # the instant this returns (and a stale-port bind failure surfaces immediately).
                import socket as _socket
                deadline = time.monotonic() + 2.0
                listening = False
                while time.monotonic() < deadline:
                    if self.desync_proc.poll() is not None:
                        break                              # child exited (e.g. port already in use)
                    try:
                        with _socket.create_connection(('127.0.0.1', self.desync_port), timeout=0.2):
                            listening = True
                            break
                    except OSError:
                        time.sleep(0.1)
                if not listening:
                    # Exited or never bound within the deadline — do NOT leave a dead/half-open child
                    # that desync_running()/effective_default_proxy would treat as usable.
                    logger.warning(f"[desync] proxy did not come up on 127.0.0.1:{self.desync_port} (mode {mode})")
                    try:
                        if self.desync_proc.poll() is None:
                            self.desync_proc.terminate()
                            self.desync_proc.wait(timeout=2)
                    except Exception:
                        pass
                    self.desync_proc = None
                else:
                    logger.info(f"[desync] local anti-throttle proxy started on 127.0.0.1:{self.desync_port} (mode {mode})")
            except Exception as e:
                logger.warning(f"[desync] failed to start: {e}")
                self.desync_proc = None

    def restart_desync(self, mode):
        """Stop any running child and (re)start it on a specific mode. Returns True if now listening.
        Used by the Speed Test mode sweep to A/B oob vs frag vs frag_oob."""
        self.stop_desync()
        self.start_desync(mode=mode)
        return self.desync_running()

    def stop_desync(self):
        # Reap the child INSIDE the lock so a concurrent start_desync can't Popen a new child (and
        # rebind the same port) before the old one has actually exited and freed it.
        with self._desync_lock:
            p, self.desync_proc = self.desync_proc, None
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=3)      # reap so the port is freed before any restart
                except Exception:
                    # terminate didn't reap in time — force-kill AND reap it. Without the second
                    # wait() the killed child can linger as a zombie still holding the port, and a
                    # restart_desync() (which start_desync()s immediately) could see that orphan
                    # listener satisfy its readiness probe and report a wrong-mode proxy as "up".
                    try:
                        p.kill()
                        p.wait(timeout=3)
                    except Exception:
                        pass
                    if p.poll() is None:
                        logger.warning("[desync] child survived terminate+kill; port may still be held")

    def ensure_desync(self):
        """Start or stop the child to match self.desync_enabled."""
        if self.desync_enabled:
            self.start_desync()
        else:
            self.stop_desync()

    def desync_health(self):
        """'off' | 'running' | 'unreachable' — is the local bypass actually accepting connections?"""
        if not self.desync_enabled:
            return 'off'
        if not self.desync_running():
            return 'unreachable'
        import socket as _socket
        try:
            with _socket.create_connection(('127.0.0.1', self.desync_port), timeout=0.5):
                return 'running'
        except OSError:
            return 'unreachable'

    def effective_default_proxy(self):
        """(enabled, proxy_url) to inject into jobs. Local desync takes precedence, and self-heals:
        if the bypass is enabled but its child died, restart it; if it still won't run, fall back to
        the remote proxy (or direct) rather than silently routing into a dead port."""
        if self.desync_enabled:
            # A Speed Test owns the child while it runs (mode sweep) — don't self-heal/flap it from a
            # concurrent enqueue mid-measurement; just report its current liveness.
            if not self.desync_running() and not self._speedtest_lock.locked():
                self.start_desync()                       # self-heal a dead/never-started child
            if self.desync_running():
                return True, f"http://127.0.0.1:{self.desync_port}"
            logger.warning("[desync] bypass enabled but proxy not running — falling back to remote/direct")
        if self.default_proxy_enabled and self.default_proxy:
            return True, self.default_proxy
        return False, ""

    def _enqueue_from_watch(self, w, v):
        """auto_enqueue callback: add a detected video to the download journal.
        Builds the JobSpec via from_dict so the FULL persisted defaults subset (profile/loudnorm/
        write_nfo/export_markers/organize_by/no_shorts/...) flows in, and old 3-key records still work."""
        d = dict(w.get('defaults') or {})
        d['urls'] = [v['url']]
        # Idempotency: if the watches.json 'seen' write fails after enqueue, the next poll would
        # re-classify this video as new and download it twice. add_if_url_absent does the "already
        # queued?" check and the append under ONE journal lock, so two concurrent polls for the same
        # video can't both slip past a separate list() check and each enqueue it (the old race).
        spec = JobSpec.from_dict(d)
        # Parity with /api/jobs, which BAKES resolve_job_proxy(..., *effective_default_proxy())
        # into every stored spec: without this, watch-enqueued jobs carried no proxy at all and ran
        # DIRECT even with the desync bypass or a privacy proxy enabled — worker_loop and the
        # engine never consult network settings at run time. d.get('proxy') keeps a per-watch
        # override (including the 'direct' sentinel) authoritative over the server default.
        try:
            import settings as _settings
        except ImportError:
            from Final import settings as _settings  # type: ignore
        _eff_enabled, _eff_proxy = self.effective_default_proxy()
        spec.proxy = _settings.resolve_job_proxy(d.get('proxy'), _eff_enabled, _eff_proxy)
        self.journal.add_if_url_absent(spec, v['url'],
                                       label=f"[watch] {v.get('title') or v['id']}")
        return True

    def _poll_watch_body(self, wid, w):
        """Fetch a watch's RSS and reconcile (auto-enqueue or stash as pending). Returns (new, mode).
        Pure poll work — the CALLER owns the in-flight bookkeeping (poll_watch / _poll_and_release)."""
        try:
            videos = self._fetch_one(w)   # _fetch_one applies the watch's filters in one place
            return self.watches.apply_poll(wid, videos, auto_enqueue_fn=self._enqueue_from_watch)
        except Exception as e:
            try:
                return self.watches.apply_poll(wid, [], error=e)
            except Exception as e2:
                # _read() now re-raises a persistent store error, so the RECOVERY path can raise
                # the very same error. Never let it escape — the caller treats this as a poll result.
                logger.warning(f'[Watch] could not record poll error for {wid}: {e2}')
                return (0, None)

    def poll_watch(self, wid):
        """Synchronous guarded poll (direct callers + the execute-time backstop). A concurrent poll
        of the SAME watch is a no-op returning (0, None). For the HTTP routes use _submit_poll, which
        dedups at SUBMIT time so duplicate futures never even reach the pool's queue."""
        w = self.watches.get(wid)
        if not w:
            return (0, None)
        with self._polls_lock:
            if wid in self._polls_inflight:
                return (0, None)          # a poll for this watch is already running -> skip
            self._polls_inflight.add(wid)
        try:
            return self._poll_watch_body(wid, w)
        finally:
            with self._polls_lock:
                self._polls_inflight.discard(wid)

    def _poll_and_release(self, wid):
        """Pool worker for _submit_poll: run the poll body, then release the in-flight slot the SUBMIT
        already claimed. Deliberately does NOT re-add wid (that would make the submitted future no-op
        against its own guard)."""
        try:
            w = self.watches.get(wid)
            return self._poll_watch_body(wid, w) if w else (0, None)
        finally:
            with self._polls_lock:
                self._polls_inflight.discard(wid)

    def _submit_poll(self, wid):
        """Submit a poll to the IO pool, deduped at SUBMIT time. If a poll for wid is already in flight
        (running OR merely queued), return None instead of stacking another future on the bounded
        4-worker pool. This bounds outstanding poll futures to at most one per watch no matter how many
        clients hammer /api/watches/poll — the execute-time guard alone still let duplicate futures
        pile up in the pool's unbounded work queue behind the workers, and a 25s-timed-out request's
        future kept its slot. Returns the Future, or None when a poll for wid is already outstanding."""
        with self._polls_lock:
            if wid in self._polls_inflight:
                return None
            self._polls_inflight.add(wid)
        try:
            return self._io_pool.submit(self._poll_and_release, wid)
        except BaseException:
            with self._polls_lock:
                self._polls_inflight.discard(wid)
            raise

    def watch_poller_loop(self):
        if not self._has_lock:
            return
        self.stop.wait(5)   # let the server come up first
        from concurrent.futures import ThreadPoolExecutor, as_completed
        while not self.stop.is_set():
            # The guard wraps the WHOLE cycle including watches.list(): _read() now re-raises a
            # persistent OSError rather than silently returning an empty document (which used to
            # erase every watch), so the READ can raise too — not just the write. Anything escaping
            # here would permanently kill this daemon thread and silently stop all channel polling.
            try:
                watches = self.watches.list()
                if watches:
                    results = {}
                    with ThreadPoolExecutor(max_workers=min(len(watches), 8)) as ex:
                        futs = {ex.submit(self._fetch_one, w): w['id'] for w in watches}
                        for fut in as_completed(futs):
                            wid = futs[fut]
                            try:
                                results[wid] = (fut.result(), None)
                            except Exception as e:
                                results[wid] = ([], e)
                    # batch_apply_poll() writes watches.json (atomic replace with retries) and can
                    # raise PermissionError / OSError just like the journal write can — a transient
                    # failure must be logged and retried next cycle, never escape.
                    if results:
                        self.watches.batch_apply_poll(results, auto_enqueue_fn=self._enqueue_from_watch)
            except Exception as e:
                logger.warning(f'[Watch] poll cycle error: {e}')
            self.stop.wait(self.watch_interval)

    def _fetch_one(self, w):
        try:
            from watch import fetch_feed, match_filters, probe_duration
        except ImportError:
            from .watch import fetch_feed, match_filters, probe_duration
        flt = w.get('filters') or {}
        # Validate legacy persisted values before any network call. Invalid syntax/length raises a
        # typed error that the poll caller records on this watch without affecting its siblings.
        checked_title = validate_title_pattern(flt.get('filter_title'))
        _title, videos = fetch_feed(w['channel_id'])
        if not flt:
            return videos
        remaining_filters = dict(flt)
        remaining_filters.pop('filter_title', None)
        # Pre-filter on the free RSS fields (title/date) first; only probe duration for the
        # survivors, and only when a duration bound is actually set (keeps polling cheap).
        need_dur = (
            remaining_filters.get('min_duration') is not None
            or remaining_filters.get('max_duration') is not None
        )
        title_date = {'after': remaining_filters.get('after')}
        out = []
        for v in videos:
            # Keep pattern errors visible to the poller: direct match_filters callers retain their
            # historical invalid-pattern=>reject contract, while stored watch errors are recorded.
            if checked_title is not None and not title_matches(
                    checked_title, v.get('title') or ''):
                continue
            if not match_filters(v, title_date, None):
                continue
            dur = probe_duration(v['url']) if need_dur else None
            if match_filters(v, remaining_filters, dur):
                out.append(v)
        return out

    def set_progress(self, job_id, info):
        with self._progress_lock:
            # Each concurrent URL of a parallel job downloads on its own worker thread and reports
            # through the same sink; bucket the raw event by reporting thread so per-URL updates
            # accumulate independently instead of overwriting one shared slot.
            tid = threading.get_ident()
            subs = self._progress_subs.setdefault(job_id, {})
            sub = subs.get(tid) or {}
            sub.update(info)
            subs[tid] = sub
            self.progress[job_id] = self._aggregate_progress(subs, sub)
            self._progress_gen += 1

    @staticmethod
    def _aggregate_progress(subs, latest):
        """Fold the per-thread sub-download events into one job-level progress dict.

        Single active download (the common case, incl. every non-parallel job): pass the relevant
        slot through unchanged, so behavior is byte-for-byte identical to the pre-aggregation server.
        Two or more concurrently downloading URLs: sum their bytes/speed so the job shows real
        combined progress instead of flickering between unrelated files.
        """
        vals = list(subs.values())
        active = [s for s in vals if s.get('status') == 'downloading']
        if len(active) >= 2:
            downloaded = sum(s.get('downloaded') or 0 for s in active)
            total = sum(s.get('total') or 0 for s in active)
            speed = sum(s.get('speed') or 0 for s in active) or None
            etas = [s.get('eta') for s in active if s.get('eta') is not None]
            out = {
                'status': 'downloading',
                'downloaded': downloaded,
                'total': total,
                'percent': min(100.0, round(downloaded / total * 100, 1)) if total else None,
                'speed': speed,
                'eta': max(etas) if etas else None,
                'active_downloads': len(active),
            }
        else:
            # 0 or 1 active: show the single downloading slot when there is one, otherwise the
            # most recently updated slot (the event that just arrived, e.g. 'starting'/'finishing').
            out = dict(active[0]) if len(active) == 1 else dict(latest)
        # Keep engine identity on the job-level view (hybrid policy ≠ actual fetch path).
        if not out.get('engine'):
            for s in reversed(vals):
                if s.get('engine'):
                    out['engine'] = s['engine']
                    if s.get('engine_reason'):
                        out['engine_reason'] = s['engine_reason']
                    break
        elif not out.get('engine_reason'):
            for s in reversed(vals):
                if s.get('engine_reason'):
                    out['engine_reason'] = s['engine_reason']
                    break
        # Preserve the earliest 'started' timestamp across slots so the UI's elapsed timer survives
        # even when the download runs on a different thread than the initial 'starting' event.
        if not out.get('started'):
            for s in vals:
                if s.get('started'):
                    out['started'] = s['started']
                    break
        out['updated'] = time.time()
        return out

    def get_progress(self, job_id):
        with self._progress_lock:
            return dict(self.progress[job_id]) if job_id in self.progress else None

    def progress_generation(self):
        with self._progress_lock:
            return self._progress_gen

    def clear_progress(self, job_id):
        with self._progress_lock:
            self.progress.pop(job_id, None)
            self._progress_subs.pop(job_id, None)
            self._progress_gen += 1

    def _result_meta_for_job(self, eng, job_id):
        """Build durable post-run stats for the job card.

        Prefer downloader speed samples + actual fetch engine; fall back to the last
        live progress snapshot so a finished remux still records engine/speed.
        """
        meta = {}
        try:
            rm = eng.downloader.result_meta()
            if isinstance(rm, dict):
                meta.update(rm)
        except Exception:
            pass
        p = self.get_progress(job_id) or {}
        if not meta.get('engine') and p.get('engine'):
            meta['engine'] = p.get('engine')
        if not meta.get('engine_reason') and p.get('engine_reason'):
            meta['engine_reason'] = p.get('engine_reason')
        # If samples were empty (rare hook gaps), keep last observed speed as both avg/peak.
        spd = p.get('speed')
        if spd and isinstance(spd, (int, float)) and spd > 0:
            if not meta.get('peak_speed'):
                meta['peak_speed'] = float(spd)
            if not meta.get('avg_speed'):
                meta['avg_speed'] = float(spd)
        # Policy fallback so the card never shows a blank engine after a real download.
        if not meta.get('engine'):
            eng_name = getattr(eng.downloader, '_last_engine', None)
            if eng_name:
                meta['engine'] = eng_name
            reason = getattr(eng.downloader, '_last_engine_reason', None)
            if reason and not meta.get('engine_reason'):
                meta['engine_reason'] = reason
        return meta or None

    def engine(self):
        """Lazy, thread-safe engine init (the GPU probe is ~5s — don't block server startup)."""
        if self._engine is None:
            with self._engine_lock:
                if self._engine is None:
                    self._engine = FinalYouTubeGPUEngine(
                        self.output_dir, pot_base_url=self.pot_base_url, pot_mode=self.pot_mode)
        return self._engine

    def invalidate_engine(self):
        """Make the next job rebuild against freshly invalidated FFmpeg capabilities."""
        with self._engine_lock:
            self._engine = None
        self._engine_ready.clear()
        self._warm_started.clear()

    def _warm_engine(self):
        """Build the engine off the request thread, then flag readiness. The Event is always set
        (try/finally) so a failed init can't leave /api/gpu spinning on 202 forever."""
        try:
            self.engine()
        except Exception as e:
            logger.warning(f"[engine] warm-up failed: {e}")   # /api/gpu will surface 503
        finally:
            self._engine_ready.set()

    def start_warm_engine(self):
        """Kick off engine warm-up exactly once (idempotent; safe to call from any request)."""
        if not self._warm_started.is_set():
            self._warm_started.set()
            threading.Thread(target=self._warm_engine, daemon=True).start()

    def job_cancel_probe(self, jid):
        """The journal's per-job cancel probe, OR'd with this server's stop event.

        Why the OR: children are spawned with CREATE_NO_WINDOW, which DETACHES them from the console
        (verified on Windows with GetConsoleProcessList — the parent pid is absent from the child's
        console process list), so a console Ctrl+C cannot reach a running ffmpeg. The only thing that
        reaps it is _run_ffmpeg's `finally`, and that requires the worker thread to actually UNWIND.
        Folding `stop` into the probe means the engine's existing 0.3s cancel poll aborts the encode
        (and the download) on shutdown, so the finally runs and the child dies with us instead of
        being orphaned when the interpreter tears the daemon thread down. Exceptions from the inner
        probe keep their 'not cancelled' meaning (see yt_final._cancel_pending)."""
        inner = self.journal.cancel_probe(jid)
        return lambda: self.stop.is_set() or bool(inner())

    def worker_loop(self):
        if not self._has_lock:
            return  # another runner owns the journal; serve read-only
        while not self.stop.is_set():
            # Supervise the WHOLE iteration. The inner try/except handles a failing download, but
            # its own except-branch set_status() (and claim_next / clear_progress) can themselves
            # raise under the same disk-full / journal-locked / cache-corruption conditions. Without
            # this outer guard such a second failure would escape and permanently kill the only
            # worker thread — the HTTP server keeps answering while NO job ever runs again, with no
            # user-visible log. Catch, log, back off, and keep draining the queue.
            try:
                job = self.journal.claim_next()   # atomic find-queued + mark-running (+attempt cap)
                if not job:
                    self.stop.wait(1.0)
                    continue
                jid = job['id']
                # claim_next() has ALREADY made this row durably 'running', so every statement that
                # can raise between here and a terminal write must sit inside the try below —
                # otherwise an engine build that blows up falls to the outer guard (log + backoff)
                # and leaves the card spinning in 'running' forever. eng is bound first so the
                # except/finally can still run when self.engine() is what failed.
                eng = None
                try:
                    eng = self.engine()
                    self.set_progress(jid, {'status': 'starting', 'started': time.time(), 'percent': None})

                    # Route live progress + cooperative cancel: when /api/cancel sets
                    # cancel_requested on a running job, the next progress tick aborts download.
                    def _progress_for_job(info, _j=jid):
                        # cancel_requested OR card dismissed (removed from journal) → abort download.
                        # One journal snapshot instead of two (is_cancel_requested + get both read the
                        # journal): same abort semantics from a single consistent read, half the stats.
                        job = self.journal.get(_j)
                        if job is None or job.get('cancel_requested'):
                            try:
                                from yt_final import DownloadCancelled
                            except ImportError:
                                from Final.yt_final import DownloadCancelled
                            raise DownloadCancelled('YT_FINAL_CANCEL')
                        self.set_progress(_j, info)

                    eng.downloader.progress_sink = _progress_for_job
                    # Both runners build the cancel channel the same way, so CLI and web cannot drift.
                    # job_cancel_probe (not the bare journal probe) so a server SHUTDOWN also aborts
                    # the in-flight download/encode — see its docstring for why that matters on Windows.
                    eng.downloader.cancel_check = self.job_cancel_probe(jid)
                    notify_url = (job.get('spec') or {}).get('notify_url')

                    def _persist_notification(result, _jid=jid):
                        try:
                            self.journal.update(
                                _jid,
                                notification=persisted_notification(result),
                            )
                        except Exception:
                            logger.exception(
                                '[Notify] could not persist result for queue job %s',
                                _jid,
                            )

                    outs = eng.run(
                        JobSpec.from_dict(job['spec']),
                        on_notification=_persist_notification if notify_url else None,
                    )
                    # Snapshot BEFORE clear_progress: merge downloader samples + last live progress.
                    meta = self._result_meta_for_job(eng, jid)
                    # Propose the NATURAL outcome only: set_status resolves a pending cancel inside
                    # its own lock, so the old read-then-write window a late cancel could fall into
                    # (is_cancel_requested here, set_status there) no longer exists.
                    if outs:
                        self.journal.set_status(
                            jid, 'done', output_paths=outs, result=meta)
                    else:
                        self.journal.set_status(jid, 'failed', error=(
                            getattr(eng.downloader, 'last_error', None) or
                            'No file produced - YouTube bot-check / throttle, or no matching format. '
                            'Add cookies (Advanced > cookies) or let the IP cool down, then retry.'),
                            error_kind=getattr(eng.downloader, 'last_error_kind', None),
                            result=meta)
                except Exception as e:
                    is_cancel = (
                        type(e).__name__ == 'DownloadCancelled'
                        or 'YT_FINAL_CANCEL' in str(e)
                        or self.journal.is_cancel_requested(jid)
                    )
                    # eng is None when self.engine() itself raised — _result_meta_for_job reads
                    # eng.downloader, so calling it here would raise AttributeError and re-strand
                    # the very row this handler exists to settle.
                    dl = getattr(eng, 'downloader', None)
                    meta = self._result_meta_for_job(eng, jid) if eng is not None else None
                    if is_cancel:
                        self.journal.set_status(jid, 'cancelled', result=meta)
                    else:
                        self.journal.set_status(jid, 'failed', error=e,
                            error_kind=getattr(dl, 'last_error_kind', None),
                            result=meta)
                    if eng is None:
                        # A failed engine build is worker-WIDE, not job-specific: without this pause
                        # one bad GPU/ffmpeg probe drains the whole queue into failed cards in
                        # milliseconds. Costing one job per transient beats losing the queue.
                        self.stop.wait(2.0)
                finally:
                    dl = getattr(eng, 'downloader', None)
                    if dl is not None:
                        dl.progress_sink = None
                    self.clear_progress(jid)
                    # Closing invariant: if every terminal write above failed (disk full, journal
                    # locked), the row is still 'running' and nothing in-process would ever look at
                    # it again. Best-effort — its own failure must never escape this iteration.
                    try:
                        self.journal.settle_claimed(jid)
                    except Exception:
                        logger.exception('[worker] settle_claimed failed for %s', jid)
            except Exception:
                logger.exception('[worker] job iteration failed; continuing after backoff')
                self.stop.wait(1.0)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ytfinal/1.0"
    # StreamRequestHandler applies this via socket.settimeout, so a client that lies about
    # Content-Length (or stalls mid-body) makes rfile.read() raise instead of hanging the
    # request thread forever — the connection is dropped rather than blocked indefinitely.
    timeout = 30

    # ----- helpers ----- #
    @property
    def state(self):
        return self.server.state

    def parse_request(self):
        """Reject an invalid loopback authority before dispatching any HTTP method."""
        if not super().parse_request():
            return False
        if self._host_allowed():
            return True
        self._send(403, {'error': 'forbidden: invalid Host'})
        return False

    def _send(self, code, body=b'', ctype='application/json; charset=utf-8', etag=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if etag:
            self.send_header('ETag', etag)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _jobs_change_token(self):
        """Cheap change token for GET /api/jobs: queue.json mtime + the in-memory progress counter.

        Folds the two things a poll can observe change: persisted job state (any add / status write
        bumps queue.json's mtime via the atomic os.replace) and live download progress (not
        persisted — the counter bumps on every set_progress/clear_progress). Two stat/int reads, no
        full job scan, so an unchanged poll can be answered from cache / with a 304 instead of
        re-serializing the entire (unbounded) job list every 1.2s.
        """
        try:
            mtime = os.stat(self.state.journal.path).st_mtime_ns
        except OSError:
            mtime = 0
        try:
            gen = self.state.progress_generation()
        except Exception:
            gen = 0
        return f'W/"{mtime}-{gen}"'

    def _confine_root(self):
        """Root that client-supplied path fields must stay inside, or None for 'no confinement'.

        Only a PUBLIC server confines. On the default localhost bind the operator IS the client and
        legitimately saves to ~/Movies or an external drive via this server's own native folder
        picker, so enforcing containment there would break stock local flows for zero security gain
        — which is why the original output_dir guard was gated the same way."""
        # UNREACHABLE BY CONSTRUCTION since --allow-public was removed: serve() is the only non-test
        # constructor, it hardcodes allow_public=False and raises if asked for True. So this returns
        # None always, and the confinement it gates — plus the same branch at the local-proxy guards
        # below — is dead code. Kept rather than deleted on purpose: stripping ~150 lines out of live
        # do_POST and _web_jobspec_from_payload paths to change reachable behaviour by exactly nothing
        # is the riskier edit. The tests that set state.allow_public = True are forcing an impossible
        # state; read them as documentation of the retired LAN model, not as live coverage.
        return self.state.output_dir if self.state.allow_public else None

    def _session_token(self) -> str:
        """Return the header credential only; query-string credentials are never accepted."""
        token = self.headers.get('X-Token') or ''
        return token.strip() if isinstance(token, str) else ''

    def _session_expiry(self) -> float | None:
        token = self._session_token()
        return self.state.sessions.expiry_for(token) if token else None

    def _request_authority(self) -> tuple[str, int] | None:
        from urllib.parse import urlsplit

        get_all = getattr(self.headers, "get_all", None)
        if callable(get_all):
            values = get_all("Host") or []
            if len(values) != 1:
                return None
            raw_host = values[0]
        else:
            raw_host = self.headers.get("Host")
        if not isinstance(raw_host, str) or not raw_host or raw_host != raw_host.strip():
            return None
        try:
            parsed = urlsplit(f"//{raw_host}")
            port = parsed.port
        except ValueError:
            return None
        if (
            not parsed.hostname
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        normalized_host = _normalize_request_loopback_host(parsed.hostname)
        if normalized_host is None:
            return None
        try:
            listener_port = int(self.server.server_address[1])
        except (AttributeError, IndexError, TypeError, ValueError):
            return None
        if port != listener_port:
            return None
        return normalized_host, listener_port

    def _host_allowed(self) -> bool:
        return self._request_authority() is not None

    def _origin_allowed(self, *, require_origin=False):
        authority = self._request_authority()
        if authority is None:
            return False
        get_all = getattr(self.headers, "get_all", None)
        if callable(get_all):
            values = get_all("Origin") or []
            if len(values) > 1:
                return False
            origin = values[0] if values else None
        else:
            origin = self.headers.get("Origin")
        if origin is None:
            # Only callers that already proved a header session may opt into origin-less native/CLI
            # compatibility. Login has no credential yet, so it requires an exact browser Origin.
            return not require_origin
        normalized_host, listener_port = authority
        expected = f"http://{_url_host(normalized_host)}:{listener_port}"
        return origin == expected

    def _authorized(self):
        """Compatibility helper for route-local checks after the central POST gate."""
        return self._session_expiry() is not None and self._origin_allowed()

    def _token_ok(self):
        """Compatibility alias for header-only session validation."""
        return self._session_expiry() is not None

    def log_message(self, *a):
        pass  # quiet; the engine already logs to final_yt.log

    # ----- routing ----- #
    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            return self._serve_static('index.html')
        if path.startswith('/static/'):
            return self._serve_static(path[len('/static/'):])
        if path in ('/app.js', '/style.css'):
            return self._serve_static(path.lstrip('/'))
        # Static assets and minimal health are the only pre-auth GET/HEAD surface. Gate every other
        # API path before route selection so unsupported sensitive paths do not leak their existence.
        session_expiry = None
        if path.startswith('/api/') and path != '/api/health':
            session_expiry = self._session_expiry()
            if session_expiry is None:
                return self._send(401, {'error': 'unauthorized'})
        if path == '/api/session':
            return self._send(200, {'ok': True, 'expires_at': session_expiry})
        if path == '/api/health':
            # Host OS is first-class so the UI (header + Updates tabs) never guesses.
            try:
                from ffmpeg_updater import host_os_label, managed_locations_by_os
            except ImportError:
                from Final.ffmpeg_updater import host_os_label, managed_locations_by_os  # type: ignore
            # Cheap LOCAL check (no network/probe): is yt-dlp's pooled transport active in this runtime?
            # Surfaced under Advanced -> Performance so a silently-unpooled (slow) runtime is visible.
            try:
                from yt_final import _pooled_transport_available
            except ImportError:
                from Final.yt_final import _pooled_transport_available  # type: ignore
            # Liveness (ok/has_lock) stays open so the login page can probe before auth. The
            # non-liveness fields disclose the absolute output_dir (OS username + layout), host OS,
            # and toolchain locations. Return them only to an authenticated session.
            resp = {'ok': True, 'has_lock': self.state._has_lock}
            if self._session_expiry() is not None:
                resp.update({
                    'output_dir': str(Path(self.state.output_dir).resolve()),
                    'host_os': host_os_label(),
                    'host_platform': sys.platform,
                    'managed_locations': managed_locations_by_os(),
                    'pooled_transport': bool(_pooled_transport_available()),
                })
            return self._send(200, resp)
        if path == '/api/profiles':
            profs = FFmpegProcessor._load_profiles()
            # video_codec is STATIC profile metadata (no probe) so the UI can cross-reference the
            # /api/gpu working_encoders and grey out HW profiles that will not run here. The central
            # API gate authenticates this metadata endpoint like the other non-health reads.
            return self._send(200, [{'id': k, 'name': v['name'], 'video_codec': v.get('video_codec', '')}
                                    for k, v in profs.items()])
        if path == '/api/gpu':
            # token-gated: this lazily builds the engine (a ~5s GPU probe) — don't let it be an
            # unauthenticated DoS / process-spawn trigger.
            # Non-blocking: if the engine is already built, answer now; otherwise kick off the
            # background warm-up (idempotent) and return 202 so we never stall the request thread
            # on the GPU probe. The web UI tolerates a missing gpu field, so 202 is safe.
            if self.state._engine is not None:
                det = self.state.engine().detector
                return self._send(200, {'gpu_name': det.gpu_info.get('gpu_name'),
                                        'working_encoders': det.gpu_info.get('working_encoders', [])})
            self.state.start_warm_engine()
            # If warm-up has FINISHED but the engine is still None, init failed — don't spin on 202 forever.
            if self.state._engine_ready.is_set():
                return self._send(503, {'error': 'engine initialisation failed (check ffmpeg/GPU)'})
            return self._send(202, {'status': 'initialising'})
        if path == '/api/jobs':
            # Conditional GET: the UI polls this every ~1.2s forever and terminal jobs accumulate,
            # so the response is re-serialized+resent unchanged most of the time. A client that sends
            # If-None-Match gets a 0-byte 304 when nothing changed. (Older clients that don't send
            # the header simply ignore the ETag and keep getting 200s — no behavior change.)
            etag = self._jobs_change_token()
            inm = self.headers.get('If-None-Match')
            if inm and inm == etag:
                self.send_response(304)
                self.send_header('ETag', etag)
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
            jobs = self.state.journal.list()
            for j in jobs:
                if j.get('status') == 'running':
                    p = self.state.get_progress(j['id'])
                    if p:
                        j['progress'] = p
            return self._send(200, {'jobs': jobs, 'counts': self.state.journal.counts(),
                                    'paused': self.state.journal.is_paused()}, etag=etag)
        if path.startswith('/api/jobs/'):
            # journal.get() returns the LIVE cached dict; a concurrent worker_loop set_status()
            # mutates that same object field-by-field, so serialize a shallow copy to avoid a torn
            # snapshot (e.g. status:'done' with stale output_paths) — same guard list() already uses.
            job = self.state.journal.get(path.rsplit('/', 1)[-1])
            job = dict(job) if job else None
            return self._send(200 if job else 404, job or {'error': 'not found'})
        if path == '/api/watches':
            return self._send(200, {'watches': self.state.watches.list(),
                                    'interval': self.state.watch_interval})
        if path == '/api/auth':
            # Mirrors `auth status` CLI; the public-mode read gate above protects this response.
            return self._send(200, auth_status_payload())
        if path == '/api/settings':
            return self._send(200, network_settings_payload(self.state))
        if path == '/api/updates':
            # Toolchain version scan (yt-dlp, ffmpeg, aria2, turbo, PO helper, …).
            # Query ?pypi=0 skips network PyPI checks (faster offline).
            # Query ?force=1 bypasses the short scan cache (default reuses ~90s).
            from urllib.parse import parse_qs, urlparse as _up
            qs = parse_qs(_up(self.path).query)
            check_pypi = (qs.get('pypi') or ['1'])[0] not in ('0', 'false', 'no')
            force = (qs.get('force') or ['0'])[0] in ('1', 'true', 'yes')
            return self._send(200, updates_report_payload(
                check_pypi=check_pypi,
                pot_base_url=getattr(self.state, 'pot_base_url', None),
                force=force))
        if path == '/api/history':
            # Persistent ledger of completed jobs; public-mode access is gated above.
            try:
                from .jobstore import HistoryLedger
            except ImportError:
                from jobstore import HistoryLedger
            return self._send(200, {'history': HistoryLedger().read(limit=200)})
        return self._send(404, {'error': 'not found'})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        _MAX_BODY = 1024 * 1024  # allow cookies.txt uploads; still a safe DoS guard on a localhost/token-gated server
        # Authenticate protected routes using headers before inspecting Content-Length or reading a
        # byte of the body. Logout deliberately accepts a previously revoked header token so retries
        # are idempotent; revoke() never creates or revives a session.
        if path != '/api/login':
            token = self._session_token()
            if path == '/api/logout':
                if not token:
                    return self._send(401, {'error': 'unauthorized'})
                if not self._origin_allowed():
                    return self._send(403, {'error': 'forbidden: cross-origin'})
                if not self.state.sessions.revoke(token):
                    return self._send(401, {'error': 'unauthorized'})
                return self._send(200, {'ok': True})
            if self._session_expiry() is None:
                return self._send(401, {'error': 'unauthorized'})
            if not self._origin_allowed():
                return self._send(403, {'error': 'forbidden: cross-origin'})
        elif not self._origin_allowed(require_origin=True):
            return self._send(403, {'error': 'forbidden: cross-origin'})
        # Parse Content-Length defensively: a malformed header must not crash the request thread,
        # and a negative/absurd value must not turn rfile.read() into an unbounded read.
        raw_len = self.headers.get('Content-Length')
        try:
            length = int(raw_len) if raw_len else 0
        except ValueError:
            return self._send(400, {'error': 'invalid Content-Length'})
        if length < 0:
            return self._send(400, {'error': 'invalid Content-Length'})
        if length > _MAX_BODY:
            return self._send(413, {'error': f'request body too large (max {_MAX_BODY} bytes)'})
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw or b'{}')
        except json.JSONDecodeError:
            return self._send(400, {'error': 'invalid JSON'})
        # Every route below assumes an object body (payload.get/.pop). A non-object JSON value
        # (list/str/number/null) would crash the handler with AttributeError — reject it once here.
        if not isinstance(payload, dict):
            return self._send(400, {'error': 'JSON body must be an object'})

        # Password login — no prior session required. Returns the API session token.
        if path == '/api/login':
            throttle = self.state.login_throttle
            waiting = throttle.retry_after()
            if waiting > 0:
                # 429 rather than 401: the guess was not evaluated at all, and saying so keeps an
                # attacker from reading the lock as "wrong password" and continuing.
                return self._send(429, {
                    'ok': False,
                    'error': 'too many failed attempts; try again shortly',
                    'retry_after': round(waiting, 1),
                })
            if not check_webui_password((payload or {}).get('password')):
                locked_for = throttle.record_failure()
                # Slow every miss. Trivial for the one person typing their own password; it is the
                # difference between thousands of guesses a second and a handful.
                time.sleep(_LOGIN_MISS_DELAY_S)
                if locked_for:
                    logger.warning(
                        "[auth] %d consecutive failed UI logins; locking logins for %.0fs",
                        _LOGIN_FAILURE_LIMIT, locked_for)
                    return self._send(429, {
                        'ok': False,
                        'error': 'too many failed attempts; try again shortly',
                        'retry_after': round(locked_for, 1),
                    })
                return self._send(401, {'ok': False, 'error': 'wrong password'})
            throttle.record_success()
            token, expiry = self.state.sessions.create()
            return self._send(200, {'ok': True, 'token': token, 'expires_at': expiry})

        if path == '/api/settings':
            # Merge-aware: absent keys keep their current value, so a {desync:…}-only toggle
            # never disturbs the saved remote proxy (and vice-versa).
            pl = payload or {}
            proxy = (pl.get('proxy') or '').strip() if 'proxy' in pl else (self.state.default_proxy or '')
            enabled = bool(pl.get('enabled')) if 'enabled' in pl else bool(self.state.default_proxy_enabled)
            desync = bool(pl.get('desync')) if 'desync' in pl else None
            desync_mode = pl.get('desync_mode') if 'desync_mode' in pl else None
            doh = bool(pl.get('doh')) if 'doh' in pl else None
            import settings as _settings
            if desync_mode is not None and str(desync_mode).strip().lower() not in _settings.DESYNC_MODES:
                return self._send(400, {'error': f'invalid desync_mode (use one of {", ".join(_settings.DESYNC_MODES)})'})
            if proxy:
                ok, err = _settings.validate_proxy(proxy)
                if not ok:
                    return self._send(400, {'error': err})
                # Same public-mode SSRF gate as test-proxy/speedtest: a saved remote proxy that
                # targets loopback/private/link-local would let an authenticated LAN client aim
                # every subsequent job (and engine traffic) at internal hosts. Desync still injects
                # its own 127.0.0.1 child via effective_default_proxy — that path is not user-supplied.
                if self.state.allow_public and _proxy_targets_local(proxy):
                    return self._send(403, {'error': 'local proxies forbidden on public server'})
            elif enabled:
                return self._send(400, {'error': 'enable a proxy first (proxy is empty)'})
            self.state.set_network_settings(enabled, proxy, desync, desync_mode, doh)
            return self._send(200, network_settings_payload(self.state))
        if path == '/api/settings/test-proxy':
            proxy = (payload.get('proxy') or '').strip() or self.state.default_proxy
            if not proxy:
                # No proxy to test — don't fetch direct twice and fabricate an "ok" proxy result.
                return self._send(400, {'ok': False, 'error': 'no proxy to test — enter one first'})
            import settings as _settings
            ok, err = _settings.validate_proxy(proxy)
            if not ok:
                return self._send(400, {'error': err})
            if self.state.allow_public and _proxy_targets_local(proxy):
                return self._send(403, {'error': 'local proxies forbidden on public server'})
            pm = _timed_proxy_fetch(proxy or None)
            dm = _timed_proxy_fetch(None)
            return self._send(200, {'ok': pm > 0, 'proxy_mibs': round(pm, 2),
                                    'direct_mibs': round(dm, 2), 'error': '' if pm > 0 else 'proxy unreachable'})
        if path == '/api/canary':
            if not self._authorized():
                return self._send(403, {'error': 'unauthorized'})
            try:
                # Same guard auth_status_payload and run_speedtest put around this exact pair: a
                # broken/misconfigured auth setup can raise here, and the canary must answer with
                # its own {'ok': False, ...} contract, not a dropped connection mid-request.
                import cookies_auth
                mgr = cookies_auth.build_default_manager()
                cookiefile = mgr.get_cookiefile() if mgr else None
            except Exception as e:
                return self._send(200, {'ok': False, 'error': f'auth setup failed: {e}'})
            try:
                media = _speedtest_extract_media(self.state, cookiefile)
            except Exception as e:
                return self._send(200, {'ok': False, 'error': str(e)})
            
            if not media:
                return self._send(200, {'ok': False, 'error': 'No formats returned.'})
            if media.get('degraded'):
                return self._send(200, {'ok': False, 'error': f"Degraded: {media.get('degraded_reason')}"})
            return self._send(200, {'ok': True, 'degraded': False})

        if path == '/api/speedtest':
            # Diagnose why YouTube is slow: run bounded probes and return a plain-language verdict +
            # ranked, one-click actions. Heavy (several network fetches + a mode sweep that flaps the
            # desync child), so guard it: one run at a time; PAUSE the queue so no queued job is
            # claimed mid-test (the sweep would tear the bypass out from under it); and refuse if a
            # download is already actively running (can't pause an in-flight transfer, and it would
            # contend for bandwidth and skew the measurement).
            user_proxy = (payload.get('proxy') or '').strip() or None
            if user_proxy:
                import settings as _settings
                ok, err = _settings.validate_proxy(user_proxy)
                if not ok:
                    return self._send(400, {'error': err})
                if self.state.allow_public and _proxy_targets_local(user_proxy):
                    return self._send(403, {'error': 'local proxies forbidden on public server'})
            if not self.state._speedtest_lock.acquire(blocking=False):
                return self._send(409, {'error': 'a speed test is already running'})
            prev_paused, paused_by_us = False, False
            try:
                try:
                    prev_paused = bool(self.state.journal.is_paused())
                    self.state.journal.set_paused(True)         # block NEW claims for the test window
                    paused_by_us = True
                except Exception as e:
                    # If we can't guarantee the queue is paused, DON'T run unpaused (a queued job could
                    # be claimed mid-sweep and have its bypass flapped). Abort and let the user retry.
                    logger.warning(f"[speedtest] could not pause the queue for the test — try again")
                    return self._send(503, {'error': 'could not pause the queue for the test — try again'})
                if any(j.get('status') == 'running' for j in self.state.journal.list()):
                    return self._send(409, {'error': 'a download is running — let it finish, then re-test'})
                return self._send(200, run_speedtest(self.state, user_proxy=user_proxy))
            except Exception as e:
                logger.warning(f"[speedtest] run failed: {e}")
                return self._send(200, {'error': str(e), 'verdict': {
                    'headline': 'The speed test hit an error — see logs.', 'state': 'error',
                    'recommended': None}, 'bars': [], 'recommendations': []})
            finally:
                if paused_by_us:
                    try:
                        # Only revert if OUR pause is still in effect. If the user hit resume (or pause)
                        # during the ~1-min test, respect their newer choice instead of clobbering it.
                        if self.state.journal.is_paused():
                            self.state.journal.set_paused(prev_paused)
                    except Exception as e:
                        logger.warning(f"[speedtest] could not restore pause state: {e}")
                self.state._speedtest_lock.release()

        if path == '/api/jobs':
            # SECURITY: never let a web client supply a shell hook — _run_on_complete runs it on the
            # host. Notify webhooks are allowed only after SSRF validation (public http/https).
            try:
                spec = _web_jobspec_from_payload(payload, confine_root=self._confine_root())
            except PathPolicyError as e:
                return self._send(403, {'error': str(e)})
            except ValueError as e:
                return self._send(400, {'error': str(e)})

            import settings as _settings
            # Public-mode SSRF: block USER-supplied local/private proxies only. Server-injected
            # desync (http://127.0.0.1:desync_port from effective_default_proxy) is not a client
            # target and must keep working when the user leaves proxy blank.
            _user_proxy = (payload.get('proxy') or '').strip() if isinstance(payload, dict) else ''
            # 'direct' (any case) is a documented per-job SENTINEL meaning "no proxy for this job"
            # (settings.resolve_job_proxy -> None), not a URL: it must be exempt from validate_proxy
            # exactly like the _web_jobspec_from_payload chokepoint, and it is not an SSRF vector.
            # Without this exemption a per-job "direct" override 400s.
            if _user_proxy and _user_proxy.lower() != 'direct':
                _ok, _err = _settings.validate_proxy(_user_proxy)
                if not _ok:
                    return self._send(400, {'error': _err})
                if self.state.allow_public and _proxy_targets_local(_user_proxy):
                    return self._send(403, {'error': 'local proxies forbidden on public server'})
            _eff_enabled, _eff_proxy = self.state.effective_default_proxy()
            spec.proxy = _settings.resolve_job_proxy(
                payload.get('proxy'), _eff_enabled, _eff_proxy)
            if not spec.urls:
                return self._send(400, {'error': 'spec needs at least one url'})
            rec = self.state.journal.add(spec)
            # Tell the caller about accepted-but-not-yet-honored knobs instead of silently no-oping.
            warns = spec.config_warnings()
            if warns:
                rec = dict(rec, warnings=warns)
            return self._send(201, rec)
        if path == '/api/jobs/rerun':
            if 'request_id' in payload:
                try:
                    request_id = validate_request_id(payload['request_id'])
                except ValueError as error:
                    return self._send(400, {'error': str(error)})
            else:
                request_id = str(uuid.uuid4())

            # A syntactically valid duplicate is authoritative before any source or tweak
            # dereference. The add() transaction repeats this check after validation to close the
            # miss-then-concurrent-commit race.
            existing = self.state.journal.get_by_request_id(request_id)
            if existing is not None:
                return self._send(201, existing)

            def build_rerun_spec():
                # All loser-payload evaluation belongs inside add_idempotent's transaction.
                # If a writer committed after the fast lookup miss, its row is returned before
                # touching source id, tweaks, paths, proxy targets, or spec warnings.
                src_id = payload.get('id')
                tweaks = payload.get('tweaks') or {}
                if not isinstance(tweaks, dict):
                    raise ValueError('tweaks must be an object')
                tweaks = dict(tweaks)
                tweaks.pop('on_complete', None)
                src = self.state.journal.get(src_id)
                if not src:
                    raise KeyError(src_id)
                base = JobSpec.from_dict(src['spec']).merged(**tweaks)
                # Rerun passes the MERGED spec through the same chokepoint, so tweaks that replace a
                # path field are confined too — previously rerun skipped even the output_dir guard.
                spec = _web_jobspec_from_payload(base.to_dict(), confine_root=self._confine_root())
                # Public-mode SSRF: rerun tweaks can inject a fresh local/private proxy that the
                # create path already blocks. Exempt only a configured value the server injected
                # when the client did not name a proxy.
                _p = (spec.proxy or '').strip()
                _client_named_proxy = 'proxy' in tweaks
                _server_defaults = set()
                if self.state.desync_enabled:
                    _server_defaults.add(f'http://127.0.0.1:{self.state.desync_port}')
                if self.state.default_proxy_enabled and self.state.default_proxy:
                    _server_defaults.add(str(self.state.default_proxy).strip())
                _server_injected = (not _client_named_proxy) and _p in _server_defaults
                if (self.state.allow_public and _p and _p.lower() != 'direct'
                        and not _server_injected and _proxy_targets_local(_p)):
                    raise PathPolicyError('local proxies forbidden on public server')
                if not spec.urls:
                    raise ValueError('spec has no urls')
                return spec

            try:
                rec = self.state.journal.add_idempotent(
                    request_id,
                    build_rerun_spec,
                )
            except KeyError:
                return self._send(404, {'error': 'unknown job id'})
            except PathPolicyError as e:
                return self._send(403, {'error': str(e)})
            except ValueError as e:
                return self._send(400, {'error': str(e)})
            return self._send(201, rec)
        if path == '/api/notify-test':
            # Fire a sample webhook (same SSRF rules as real job notify).
            return self._send(200, _notify_test((payload or {}).get('url'), (payload or {}).get('template')))
        if path == '/api/check-folder':
            # Preflight the Save-to-folder box BEFORE queueing. Without this, an unusable path (an
            # unmounted E:\, a typo, a read-only dir) fell through to the engine, which logged a
            # warning and quietly saved to the previous folder — the card still said 'done' and the
            # editor went looking in the wrong place. Read-only probe: it may CREATE the directory
            # (the engine would too, via mkdir(parents=True)) but writes nothing else.
            raw = ((payload or {}).get('path') or '').strip()
            if not raw:
                return self._send(200, {'ok': True, 'path': '', 'note': 'default folder'})
            try:
                p = Path(raw).expanduser()
            except Exception as e:
                return self._send(200, {'ok': False, 'error': f'unusable path ({e})'})
            if p.exists() and not p.is_dir():
                return self._send(200, {'ok': False, 'path': str(p), 'exists': True,
                                        'writable': False,
                                        'error': 'that path is a file, not a folder'})
            # A CHECK must not mutate the filesystem: creating the target would leave a stray folder
            # behind on every typo (this box is free text). For a path that does not exist yet, walk
            # up to the nearest existing ancestor and test THAT — which is exactly what decides
            # whether the engine's later mkdir(parents=True) will succeed.
            existed = p.is_dir()
            probe_dir = p
            while not probe_dir.exists():
                parent = probe_dir.parent
                if parent == probe_dir:            # walked off the top (e.g. a missing drive root)
                    break
                probe_dir = parent
            if not probe_dir.is_dir():
                return self._send(200, {'ok': False, 'path': str(p), 'exists': False,
                                        'writable': False,
                                        'error': 'drive or folder does not exist'})
            # Prove writability rather than trusting mode bits: on Windows an ACL or a disconnected
            # network/removable drive can allow stat but refuse a create.
            probe = probe_dir / '.yt_final_write_probe'
            try:
                with open(probe, 'w', encoding='utf-8') as fh:
                    fh.write('')
            except OSError as e:
                return self._send(200, {'ok': False, 'path': str(p), 'exists': existed,
                                        'writable': False,
                                        'error': f'not writable — {e.strerror or e}'})
            finally:
                try:
                    probe.unlink()
                except OSError:
                    pass
            return self._send(200, {'ok': True, 'path': str(p), 'exists': existed, 'writable': True,
                                    'will_create': (not existed)})
        if path == '/api/pick-folder':
            # opens a native folder dialog on the server desktop; returns the absolute path chosen
            chosen = _pick_directory((payload or {}).get('current'))
            if chosen is None:
                return self._send(200, {'path': None, 'error': 'no folder picker available on the server'})
            return self._send(200, {'path': chosen})
        if path == '/api/pick-file':
            # Native open-file dialog (cookies.txt override, etc.). Same host as the UI.
            cur = (payload or {}).get('current') or ''
            try:
                init = str(Path(cur).expanduser().parent) if cur and Path(cur).expanduser().parent.is_dir() else (
                    str(Path(cur).expanduser()) if cur and Path(cur).expanduser().is_dir() else None
                )
            except Exception:
                init = None
            title = (payload or {}).get('title') or 'Select cookies.txt'
            chosen = _pick_file(initialdir=init, title=title)
            if chosen is None:
                return self._send(200, {'path': None, 'error': 'no file picker available on the server'})
            # Empty string = user cancelled — not an error
            return self._send(200, {'path': chosen or None, 'cancelled': not bool(chosen)})
        if path == '/api/upload-cookiefile':
            # Browser file pick → write override cookies.txt → return path for cookiefile field
            r = save_uploaded_cookiefile((payload or {}).get('content', ''))
            code = 200 if r.get('ok') else 400
            return self._send(code, r)
        if path in ('/api/probe', '/api/probe-formats'):
            # Detect max resolution / available heights without downloading.
            url = ((payload or {}).get('url') or '').strip()
            if not url:
                return self._send(400, {'ok': False, 'error': 'url required'})
            # Optional per-job cookies from the compose form
            cookiefile = (payload or {}).get('cookiefile') or None
            cookies_from_browser = (payload or {}).get('cookies_from_browser') or None
            # Probe skipped the path policy that create/rerun enforce, yet the downloader constructor
            # mkdir()s output_dir and yt-dlp may touch cookiefile — so a public client could create a
            # dir or truncate a file outside the root. Confine the same fields here.
            _confine = self._confine_root()
            if _confine:
                from types import SimpleNamespace as _NS
                _bad = _spec_path_violation(_NS(
                    output_dir=(payload or {}).get('output_dir'), output_template=None,
                    download_archive=None, cookiefile=cookiefile,
                    cookies_from_browser=cookies_from_browser), _confine)
                if _bad:
                    return self._send(403, {'ok': False, 'error': (
                        f'{_bad} must be within the server download directory when exposed publicly')})
            try:
                # Light extract-only probe (no full GPU encode path).
                from pathlib import Path as _P
                try:
                    from yt_final import YouTubeDownloader, get_detector
                except ImportError:
                    from Final.yt_final import YouTubeDownloader, get_detector
                out = _P((payload or {}).get('output_dir') or self.state.output_dir or './downloads')
                # Prefer warmed engine detector when available (avoids re-probe cost)
                det = None
                try:
                    if self.state._engine is not None:
                        det = self.state.engine().detector
                except Exception:
                    det = None
                if det is None:
                    # Use the process-wide singleton, NOT a fresh CapabilityDetector(): before the
                    # engine warm-up finishes, a bare constructor here reruns the full blocking
                    # encoder probe on every probe request and races concurrent probes over the same
                    # GPU encoder sessions (and the caps_cache.json write). get_detector() builds it
                    # at most once per process and every concurrent caller shares that result.
                    det = get_detector()
                dl = YouTubeDownloader(
                    det, out_dir=str(out),
                    pot_base_url=getattr(self.state, 'pot_base_url', None),
                    pot_mode=getattr(self.state, 'pot_mode', 'auto'),
                )
                # Prefer auto auth cookies when form didn't supply any and job wants Auth.
                use_auth = True
                if isinstance(payload, dict) and 'use_auth' in payload:
                    v = payload.get('use_auth')
                    use_auth = v not in (False, 0, '0', 'false', 'False', 'no', 'off')
                if use_auth and not cookiefile and not cookies_from_browser:
                    try:
                        mgr = dl._get_cookie_manager()
                        if mgr:
                            cookiefile = mgr.get_cookiefile() or cookiefile
                    except Exception:
                        pass
                summary = dl.probe_url(
                    url,
                    cookies_from_browser=cookies_from_browser or None,
                    cookiefile=cookiefile or None,
                )
                # "Already got this?" — join the probed id against history + the output folder.
                try:
                    summary['existing'] = find_existing_download(summary.get('id'), str(out))
                except Exception:
                    pass
                return self._send(200, summary)
            except Exception as e:
                return self._send(200, {'ok': False, 'error': str(e), 'url': url})
        if path in ('/api/open-folder', '/api/reveal'):

            # Reveal a job output (or any path) in the host OS file manager. Token-gated POST.
            # Accepts {path} and/or {job_id, index?} — job_id is the reliable path when the
            # client string is stale. Alias /api/reveal kept for older UI builds.
            return self._send(200, open_folder_from_payload(payload, journal=self.state.journal))
        if path == '/api/watches':
            try:
                filter_title = validate_title_pattern(payload.get('filter_title'))
            except WatchPatternError as e:
                return self._send(400, {'error': str(e)})
            url = (payload.get('url') or payload.get('channel') or '').strip()
            if not url:
                return self._send(400, {'error': 'provide a channel url / @handle / id'})
            # resolve is a ~15s blocking network call — run it on the bounded IO pool with a hard
            # timeout so a slow/hung resolve can't tie up the request thread indefinitely.
            try:
                cid = self.state._io_pool.submit(resolve_channel_id, url).result(timeout=25)
            except Exception:
                cid = None
            if not cid:
                return self._send(400, {'error': 'could not resolve a YouTube channel id from that input'})
            defaults = {'quality': payload.get('quality') or '1080p',
                        'encode_mode': payload.get('encode_mode') or 'copy',
                        'profile': payload.get('profile') or 'auto_h264',
                        'no_shorts': bool(payload.get('no_shorts', True)),
                        'loudnorm': payload.get('loudnorm'),
                        'write_nfo': bool(payload.get('write_nfo', False)),
                        'export_markers': payload.get('export_markers') or None,
                        'organize_by': payload.get('organize_by') or None}
            # per-watch upload filters (None/empty -> no-op; min/max coerced to int when present)
            def _int_or_none(x):
                try:
                    return int(x) if x not in (None, '') else None
                except (TypeError, ValueError):
                    return None
            filters = {'filter_title': filter_title,
                       'after': payload.get('after') or None,
                       'min_duration': _int_or_none(payload.get('min_duration')),
                       'max_duration': _int_or_none(payload.get('max_duration'))}
            # one fetch to get the channel title (label); bounded by the IO pool + timeout
            label = None
            try:
                label, _vids = self.state._io_pool.submit(fetch_feed, cid).result(timeout=15)
            except Exception:
                pass
            w = self.state.watches.add(cid, label=label or url, url=url,
                                       auto_queue=bool(payload.get('auto_queue')),
                                       defaults=defaults, filters=filters)
            self.state._submit_poll(w['id'])   # fire-and-forget first detection (submit-time deduped)
            return self._send(201, self.state.watches.get(w['id']) or w)
        if path == '/api/watches/poll':
            # poll_watch() does blocking RSS fetch + per-video duration probes. Route it through the
            # bounded IO pool with a hard timeout so a slow/hung feed can't tie up the request thread
            # indefinitely (mirrors the /api/watches POST resolve/fetch handling above).
            wid = payload.get('id')
            if wid:
                fut = self.state._submit_poll(wid)
                if fut is None:
                    # A poll for this watch is already outstanding; return current state rather than
                    # stack a duplicate future on the pool.
                    return self._send(200, {'new': 0, 'mode': None,
                                            'watch': self.state.watches.get(wid)})
                try:
                    n, mode = fut.result(timeout=25)
                except concurrent.futures.TimeoutError:
                    # The poll keeps running in the pool; report a bounded, non-blocking answer.
                    return self._send(200, {'new': 0, 'mode': None, 'timeout': True,
                                            'watch': self.state.watches.get(wid)})
                return self._send(200, {'new': n, 'mode': mode, 'watch': self.state.watches.get(wid)})
            # 'poll all': submit every watch concurrently on the IO pool with an overall deadline
            # instead of blocking the request thread serially, one watch at a time. _submit_poll
            # dedups at submit time, so a repeated 'poll all' can't stack a second full batch of
            # futures; watches already being polled are skipped (None).
            watches = self.state.watches.list()
            futs = [f for f in (self.state._submit_poll(w['id']) for w in watches) if f is not None]
            try:
                for fut in concurrent.futures.as_completed(futs, timeout=25):
                    try:
                        fut.result()
                    except Exception:
                        pass
            except concurrent.futures.TimeoutError:
                pass  # stragglers keep running in the pool; return current state without blocking further
            return self._send(200, {'watches': self.state.watches.list()})
        if path == '/api/watches/queue':
            wid = payload.get('id')
            w = self.state.watches.get(wid) or {}
            req = payload.get('video_ids', 'all')
            pending = w.get('pending', [])
            if req == 'all' or req is True:
                candidates = list(pending)
            else:
                ids = set(req or [])
                candidates = [v for v in pending if v.get('id') in ids]
            # Enqueue FIRST; only remove-from-pending/mark-seen the ones that actually enqueued,
            # so a mid-batch failure leaves the rest recoverable instead of silently lost.
            queued_ids = []
            for v in candidates:
                try:
                    self.state._enqueue_from_watch(w, v)
                    queued_ids.append(v['id'])
                except Exception as e:
                    logger.warning(f"[watch] enqueue failed for {v.get('id')}: {e}")
            if queued_ids:
                self.state.watches.take_pending(wid, queued_ids)
            return self._send(200, {'queued': len(queued_ids), 'watch': self.state.watches.get(wid)})
        if path == '/api/watches/dismiss':
            wid = payload.get('id')
            taken = self.state.watches.take_pending(wid, payload.get('video_ids', 'all'))
            return self._send(200, {'dismissed': len(taken), 'watch': self.state.watches.get(wid)})
        if path == '/api/watches/update':
            wid = payload.get('id')
            checked_filter_title = None
            has_filter_title = (
                isinstance(payload.get('filters'), dict)
                and 'filter_title' in payload['filters']
            )
            if has_filter_title:
                try:
                    checked_filter_title = validate_title_pattern(
                        payload['filters']['filter_title'])
                except WatchPatternError as e:
                    return self._send(400, {'error': str(e)})
            fields = {}
            if 'auto_queue' in payload:
                fields['auto_queue'] = bool(payload['auto_queue'])
            # allow editing the persisted defaults/filters after creation (merge whitelisted keys)
            if isinstance(payload.get('defaults'), dict):
                cur = self.state.watches.get(wid) or {}
                merged = dict(cur.get('defaults') or {})
                for k in ('quality', 'encode_mode', 'profile', 'no_shorts', 'loudnorm',
                          'write_nfo', 'export_markers', 'organize_by'):
                    if k in payload['defaults']:
                        merged[k] = payload['defaults'][k]
                fields['defaults'] = merged
            if isinstance(payload.get('filters'), dict):
                cur = self.state.watches.get(wid) or {}
                merged = dict(cur.get('filters') or {})
                for k in ('filter_title', 'after', 'min_duration', 'max_duration'):
                    if k in payload['filters']:
                        merged[k] = (
                            checked_filter_title
                            if k == 'filter_title' and has_filter_title
                            else payload['filters'][k]
                        )
                fields['filters'] = merged
            w = self.state.watches.update(wid, **fields) if fields else self.state.watches.get(wid)
            return self._send(200 if w else 404, w or {'error': 'not found'})
        if path == '/api/watches/remove':
            ok = self.state.watches.remove(payload.get('id'))
            return self._send(200, {'removed': ok})
        if path == '/api/retry-failed':
            return self._send(200, {'reset': self.state.journal.retry_failed()})
        if path == '/api/clear':
            return self._send(200, {'removed': self.state.journal.clear(only_finished=True)})
        if path == '/api/pause':
            return self._send(200, {'paused': self.state.journal.set_paused(True)})
        if path == '/api/resume':
            return self._send(200, {'paused': self.state.journal.set_paused(False)})
        if path == '/api/cancel':
            job = self.state.journal.cancel(payload.get('id'))
            if job:
                # running → cancel_requested (worker aborts); queued/failed → cancelled
                return self._send(200, job)
            return self._send(409, {'error': 'cannot cancel (already finished or missing)'})
        if path == '/api/remove':
            # Dismiss card: drop job from the pipeline list (any status). Running jobs abort
            # on the next progress tick because the journal record is gone.
            jid = payload.get('id')
            job = self.state.journal.remove(jid)
            if job:
                try:
                    self.state.clear_progress(jid)
                except Exception:
                    pass
                return self._send(200, {'removed': True, 'id': jid, 'was_status': job.get('status')})
            return self._send(404, {'error': 'not found'})
        if path == '/api/priority':
            job = self.state.journal.set_priority(payload.get('id'), payload.get('priority', 100))
            return self._send(200 if job else 404, job or {'error': 'not found'})
        if path == '/api/auth':
            import cookies_auth
            action = payload.get('action')
            ok = False
            message = None
            error = None
            if action == 'test':
                # Validate existing file only — never launch browser extractors on Test.
                mgr = cookies_auth.build_default_manager()
                ok, message, _path = mgr.validate_existing_file()
                if not ok:
                    error = message
            elif action == 'refresh':
                mgr = cookies_auth.build_default_manager()
                path_got = mgr.refresh_cookiefile()
                ok = bool(path_got)
                message = (
                    f'Refreshed cookies from provider → {path_got}'
                    if ok else
                    'Could not refresh cookies (no file / browser extract failed). Import cookies.txt.'
                )
                if not ok:
                    error = message
            elif action == 'clear':
                # same files the CLI `auth clear` removes
                store = cookies_auth.cookie_store_dir()
                try:
                    cookies_auth.clear_cookie_store(store)
                    ok = True
                    message = 'Cleared local cookie store (auth config left unchanged).'
                except OSError as exc:
                    ok = False
                    error = str(exc)
                    message = 'Could not clear local cookie store.'
            elif action == 'import':
                r = auth_import_cookies((payload or {}).get('content', ''))
                return self._send(200, {
                    'ok': r.get('ok', False),
                    'error': r.get('error'),
                    'message': r.get('message'),
                    'path': r.get('path'),
                    'youtube_cookies': r.get('youtube_cookies'),
                    'status': auth_status_payload(),
                })
            elif action == 'from_browser':
                r = auth_from_browser((payload or {}).get('browser') or 'auto')
                return self._send(200, {
                    'ok': r.get('ok', False),
                    'error': r.get('error'),
                    'message': r.get('message'),
                    'path': r.get('path'),
                    'browsers_detected': r.get('browsers_detected'),
                    'youtube_cookies': r.get('youtube_cookies'),
                    'status': auth_status_payload(),
                })
            elif action == 'login':
                # Blocks until the headed native-browser (real Chrome/Edge/Brave via CDP, NOT
                # Playwright) login window closes — client should show a long "waiting…" state
                # (local-only tool).
                r = auth_login_session()
                return self._send(200, {
                    'ok': r.get('ok', False),
                    'error': r.get('error'),
                    'message': r.get('message'),
                    'code': r.get('code'),
                    'path': r.get('path'),
                    'status': auth_status_payload(),
                })
            else:
                return self._send(400, {
                    'error': 'action must be one of: refresh, test, clear, import, from_browser, login',
                })
            return self._send(200, {
                'ok': ok,
                'message': message,
                'error': error,
                'status': auth_status_payload(),
            })
        if path == '/api/updates/tools':
            # Only Node/Deno have certified managed installers. Other allow-listed names
            # return stable manual or whole-lock guidance without mutation/report I/O.
            # Auth already enforced by do_POST → _authorized().
            tool = (payload or {}).get('tool')
            try:
                from optional_tools import install_optional_tool
            except ImportError:
                from Final.optional_tools import install_optional_tool  # type: ignore
            allowed = {
                'aria2c', 'aria2', 'N_m3u8DL-RE', 'n_m3u8dl-re', 'turbo',
                'deno', 'node', 'nodejs', 'playwright',
                'bgutil-server', 'bgutil_server', 'bgutil-plugin',
                'bgutil-ytdlp-pot-provider', 'bgutil',
            }
            if not tool or str(tool) not in allowed:
                return self._send(400, {
                    'ok': False,
                    'error': (
                        'tool must be aria2c, N_m3u8DL-RE, deno, node, playwright, '
                        'bgutil-plugin, or bgutil-server'
                    ),
                })
            try:
                # Certified runtime installers own the Task 6 guard. Manual-only
                # dispatch must return before any guard or filesystem access.
                result = install_optional_tool(str(tool))
            except InstallerBusyError:
                return self._send(409, {
                    'ok': False,
                    'code': INSTALLER_BUSY_CODE,
                    'error': INSTALLER_BUSY_MESSAGE,
                })
            if result.get('code') == INSTALLER_BUSY_CODE:
                return self._send(409, {
                    'ok': False,
                    'code': INSTALLER_BUSY_CODE,
                    'error': INSTALLER_BUSY_MESSAGE,
                })
            if not result.get('ok'):
                return self._send(422, {
                    'ok': False,
                    'error': (
                        result.get('error')
                        or result.get('message')
                        or 'install failed'
                    ),
                    'code': result.get('code') or 'failed',
                    'result': result,
                })
            try:
                from yt_final import UpdateChecker
                UpdateChecker.invalidate_cache()
            except Exception:
                pass
            # Next download must re-probe PO provider (was cached as missing).
            try:
                eng = self.state._engine
                if eng is not None and getattr(eng, 'downloader', None) is not None:
                    eng.downloader._pot_status_cache = None
            except Exception:
                pass
            report = updates_report_payload(
                check_pypi=True,
                pot_base_url=getattr(self.state, 'pot_base_url', None),
                force=True,
            )
            if result.get('ok'):
                return self._send(200, {
                    'ok': True,
                    'result': result,
                    'report': report,
                })
        if path == '/api/updates/ffmpeg':
            # The updater owns fixed HTTPS endpoints and its managed destination. Never accept
            # client-selected URLs, paths, or options that could redirect this host mutation.
            if payload:
                fields = ', '.join(sorted(str(key) for key in payload))[:300]
                report = updates_report_payload(
                    check_pypi=True,
                    pot_base_url=getattr(self.state, 'pot_base_url', None),
                    force=True,
                )
                return self._send(
                    400,
                    {'ok': False,
                     'error': f'request body must be an empty JSON object; forbidden fields: {fields}',
                     'report': report},
                )
            code, response = apply_ffmpeg_update(
                pot_base_url=getattr(self.state, 'pot_base_url', None))
            if code == 200:
                self.state.invalidate_engine()
            return self._send(code, response)
        if path == '/api/updates':
            # action=scan | install | update | all
            # Python mutation labels are retained only as compatibility aliases.
            # In 1.0.0 they return one stable reviewed-release requirement.
            # A packages key is always rejected, including null/empty values.
            # scan: force=true (default for explicit check now) re-probes; force=false uses cache.
            action = (payload or {}).get('action') or 'scan'
            pot = getattr(self.state, 'pot_base_url', None)
            if 'packages' in (payload or {}):
                return self._send(400, {
                    'ok': False,
                    'code': PER_PACKAGE_DISABLED_CODE,
                    'error': PER_PACKAGE_DISABLED_MESSAGE,
                })
            if action == 'scan':
                # Default force=True for POST scan so "check now" is fresh; pass force:false for quiet.
                force = True if 'force' not in (payload or {}) else bool((payload or {}).get('force'))
                # pypi=0/false skips network latest checks (fast tab open); default True.
                pypi_raw = (payload or {}).get('pypi', True)
                if isinstance(pypi_raw, str):
                    check_pypi = pypi_raw.strip().lower() not in ('0', 'false', 'no')
                else:
                    check_pypi = bool(pypi_raw)
                return self._send(200, updates_report_payload(
                    check_pypi=check_pypi, pot_base_url=pot, force=force))
            if action in ('install', 'update', 'all', 'auto'):
                try:
                    r = apply_pip_updates(mode=action, pot_base_url=pot)
                except InstallerBusyError:
                    return self._send(409, {
                        'ok': False,
                        'code': INSTALLER_BUSY_CODE,
                        'error': INSTALLER_BUSY_MESSAGE,
                    })
                status = (
                    409
                    if r.get('code') == 'certified-runtime-unavailable'
                    else (200 if r.get('ok') else 400)
                )
                return self._send(status, r)
            return self._send(400, {'error': 'action must be scan, install, update, or all'})
        return self._send(404, {'error': 'not found'})

    def _serve_static(self, rel):
        # prevent path traversal
        target = (WEBUI_DIR / rel).resolve()
        try:
            target.relative_to(WEBUI_DIR.resolve())
        except ValueError:
            return self._send(403, {'error': 'forbidden'})
        if not target.is_file():
            return self._send(404, {'error': 'not found'})
        ctype = _CONTENT_TYPES.get(target.suffix.lower(), 'application/octet-stream')
        st = target.stat()
        etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
        # Conditional GET: if the client's cached copy is still current, skip the body entirely.
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            return
        is_head = self.command == 'HEAD'
        # For HEAD, never read the file body — emit headers only (Content-Length still reflects size).
        body = b'' if is_head else target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(st.st_size))
        self.send_header('ETag', etag)
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        if not is_head:
            self.wfile.write(body)


def _write_secret_file(path, text):
    """Create/overwrite a secret file OWNER-ONLY, from its first byte.

    This used to be os.open(O_CREAT|O_TRUNC, 0o600). Correct on POSIX — it beats write_text()+chmod,
    which leaves the file at the ambient umask for the window between creation and the chmod. But on
    Windows the mode argument is ADVISORY: the file inherits whatever DACL its parent carries, so
    ~/.yt_final/webui.txt — the cleartext Web UI password — was readable by any process running as
    another local user, as SYSTEM, or as an administrator, including the backup/sync/AV agents that
    index a user profile. SECURITY.md meanwhile promised that secret-bearing files, naming this one,
    are "created owner-only from their first byte where the platform supports it", and Windows is
    the primary supported platform. The repository already had the right primitive and used it for
    cookies.

    secureio.atomic_replace_private() writes a temp file that is owner-only FROM CREATION (a real
    DACL on Windows; 0600 enforced through the descriptor on POSIX), fsyncs it, re-asserts privacy
    on the published path, and replaces atomically — so a concurrent reader sees either the old
    secret or the new one, never a torn or empty file. It also sidesteps a trap in the obvious
    alternative: routing this through open_private_file() would NOT truncate, because the Windows
    path maps O_CREAT to OPEN_ALWAYS, which would leave the tail of a longer previous password
    behind a shorter new one.
    """
    atomic_replace_private(Path(path), text.encode('utf-8'))


def _ensure_stdout_encoding_safe():
    """Make this process's stdout tolerate the banner's non-ASCII glyphs (U+26A0 '⚠').

    On an interactive Windows console this is a no-op (WriteConsoleW handles Unicode), but with
    stdout redirected to a pipe/file under a non-UTF-8 locale (e.g. cp1252) a bare print('⚠…')
    raises UnicodeEncodeError — and, being unwrapped in serve(), that aborts startup before
    serve_forever(). Reconfigure to UTF-8 with errors='replace' so the glyph is emitted (or, on a
    truly byte-limited stream, downgraded) instead of raising. Best-effort: a stream that can't be
    reconfigured is left as-is."""
    stream = getattr(sys, "stdout", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # Can't switch encoding (rare) — at least stop strict errors from raising.
        try:
            reconfigure(errors="replace")
        except Exception:
            pass


def _endpoint_already_serving(bind_host, port, timeout=0.35):
    """Is something already listening on this endpoint?

    SO_REUSEADDR is inherited deliberately (see the note in serve()), and the alternative --
    SO_EXCLUSIVEADDRUSE on Windows -- was rejected for a good reason: it also refuses a bind while
    any socket at the endpoint sits in TIME_WAIT, and this server is the active closer on every
    request, so `serve` became unrestartable for ~120s after a single page load.

    The cost of that correct decision is that a SECOND `serve` binds the same port with no error at
    all: both sockets are accepted and every request goes to the first. The user then sees a window
    saying "ready" with a password that does not work, because the password is per-process.
    Double-clicking the launcher twice -- the obvious thing after closing the browser tab -- lands
    exactly there.

    So detect it the honest way, by connecting. Advisory only: a race between probe and bind is
    possible, and the run lock stays the authoritative check.
    """
    import socket as _socket

    try:
        family = _socket.AF_INET6 if ipaddress.ip_address(bind_host).version == 6 else _socket.AF_INET
    except ValueError:
        family = _socket.AF_INET
    probe = _socket.socket(family, _socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        return probe.connect_ex((bind_host, int(port))) == 0
    except OSError:
        return False
    finally:
        probe.close()

def _print_ready_banner(state, simple_url):
    _ensure_stdout_encoding_safe()
    print()
    print("=" * 70)
    if state._has_lock:
        print("  yt_final is ready")
    else:
        # Do not claim "ready" when another copy owns the queue. On Windows a second `serve` binds
        # the same port without error and every request goes to the FIRST process, so this window's
        # password opens nothing -- announcing readiness and printing that password first is how a
        # user ends up locked out of a UI that says it is working.
        print("  yt_final started, but ANOTHER COPY IS ALREADY RUNNING")
    print()
    print(f"  Open:      {simple_url}")
    _weak = warn_if_webui_password_is_weak()
    if _weak:
        print()
        print(f"  !  {_weak}")
        print()
    try:
        # Absolute, because output_dir defaults to a RELATIVE "./downloads" and therefore lands
        # wherever the launcher was started from. Printing "downloads" told the user nothing about
        # which folder that actually is.
        print(f"  Saving to: {Path(state.output_dir).resolve()}")
    except Exception:
        pass
    if state._has_lock:
        print(f"  Password:  {webui_password()}")
    else:
        print("  Password:  use the password shown by the copy that is already running")
        print("             (this window's password will not open the UI)")
    try:
        from yt_final import _pooled_transport_available as _ptavail
    except ImportError:
        from Final.yt_final import _pooled_transport_available as _ptavail  # type: ignore
    if not _ptavail():
        print()
        print("  ⚠  SLOW TRANSPORT: this runtime is on the UNPOOLED urllib handler (yt-dlp's requests")
        print("     transport is absent). Install a future reviewed yt_final release with requests +")
        print("     urllib3 to enable pooled connections — ~+33-67% download throughput on fast links.")
    print()
    print("  Your browser should open by itself.")
    print("  Leave this window open while you use the app.")
    print("  Close it (or press Ctrl+C) when you're done.")
    if not state._has_lock:
        _holder = getattr(state.journal, 'lock_holder_pid', None)
        print()
        # Baseline sentence on POSIX; the pid is only actionable next to the Windows
        # stale-lock hint below, so splice it in there and nowhere else.
        if sys.platform.startswith('win') and _holder is not None:
            print(f"  NOTE: another copy is already running the queue (pid {_holder}); "
                  "new jobs may not start here.")
        else:
            print("  NOTE: another copy is already running the queue; new jobs may not start here.")
        # On Windows the NORMAL exit (closing the console / End task) skips lock release, and
        # recycled PIDs can make a dead lock look alive — so make this self-service instead of
        # a dead end the user has to guess at.
        if sys.platform.startswith('win'):
            print("  If you just closed a previous window and nothing else is running,")
            print(f"  that lock is stale — delete it and restart:  {state.journal.lock_path}")
    print("=" * 70)
    print()


def serve(host='127.0.0.1', port=8765, token=None, output_dir='./downloads',
          queue_file=None, pot_base_url=None, pot_mode='auto', allow_public=False,
          open_browser=True):
    if token:
        raise ValueError(
            "token is no longer supported; start the loopback server and sign in with its password"
        )
    if allow_public:
        raise ValueError(
            "allow_public is no longer supported; the web server is loopback-only "
            "(use an operator-managed SSH or WireGuard tunnel for remote access)"
        )
    bind_host = _resolve_loopback_bind_host(host)
    if bind_host is None:
        raise ValueError(f"refusing to bind {host!r}: the web server is loopback-only")
    try:
        import cookies_auth
        home = cookies_auth.auth_config_dir()
    except Exception:
        home = Path.home() / '.yt_final'
    retire_legacy_token_file(home / 'web_token')
    state = ServerState(output_dir, queue_file=queue_file,
                        pot_base_url=pot_base_url, pot_mode=pot_mode, allow_public=False)
    # NOTE: deliberately plain ThreadingHTTPServer with the inherited allow_reuse_address=1.
    # An earlier revision set SO_EXCLUSIVEADDRUSE on Windows to stop a second instance binding the
    # same port. That was a bad trade: on Windows that option also refuses a bind while ANY socket
    # at the endpoint sits in TIME_WAIT, and this server is the ACTIVE CLOSER on every request
    # (BaseHTTPRequestHandler defaults to HTTP/1.0, so each response closes), so a single page load
    # leaves TIME_WAIT entries and `serve` became unrestartable for TcpTimedWaitDelay (~120s).
    # A second copy is already detected out-of-band by the run lock — see state._has_lock and the
    # notice printed below — which is the right layer for it.
    server_type = (
        _IPv6ThreadingHTTPServer
        if ipaddress.ip_address(bind_host).version == 6
        else http.server.ThreadingHTTPServer
    )
    if _endpoint_already_serving(bind_host, port):
        raise SystemExit(
            f"yt_final is already serving on {_url_host(bind_host)}:{port}. "
            "Open that window's URL and password, or start this one elsewhere: "
            f"yt-final serve --port {int(port) + 1}")
    try:
        httpd = server_type((bind_host, port), Handler)
    except OSError as exc:
        # macOS/Linux refuse the duplicate bind outright (EADDRINUSE) where Windows
        # accepts it. Same user situation, so give the same answer, not a traceback.
        raise SystemExit(
            f"could not bind {_url_host(bind_host)}:{port} ({exc.strerror or exc}). "
            "Another program may be using that port. Try: "
            f"yt-final serve --port {int(port) + 1}") from exc
    httpd.state = state
    worker = threading.Thread(target=state.worker_loop, daemon=True)
    worker.start()
    threading.Thread(target=state.watch_poller_loop, daemon=True).start()
    state.ensure_desync()  # start the local anti-throttle proxy if it was left enabled
    import atexit
    atexit.register(state.stop_desync)  # backstop so an abrupt exit still reaps the child
    # Warm the engine in the background so the first /api/gpu (and the first job) don't pay the
    # ~5s GPU probe inline. Idempotent with the on-demand kick in the /api/gpu handler.
    state.start_warm_engine()

    # Simple bookmarkable URL — password unlocks the UI (no token in the address bar).
    listener_port = int(httpd.server_address[1])
    simple_url = f"http://{_url_host(bind_host)}:{listener_port}/"
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / 'webui.url').write_text(
            f"[InternetShortcut]\nURL={simple_url}\n", encoding='utf-8')
        # webui.txt holds the per-launch UI password in plaintext - create owner-only, never
        # at the ambient umask. Same helper as the session token.
        #
        # Only written when THIS process holds the run lock. The password is per-process, so a
        # second copy overwriting this file replaces a working recovery note with one whose
        # password opens nothing - and webui/index.html points users straight at it. Losing the
        # note is worse than not refreshing it.
        if state._has_lock:
            _write_secret_file(home / 'webui.txt',
                               "yt_final web app\n"
                               f"Open: {simple_url}\n"
                               f"Password: {webui_password()}\n")
    except OSError:
        pass

    _print_ready_banner(state, simple_url)

    if open_browser:
        def _open():
            time.sleep(0.4)  # let the listener bind
            try:
                import webbrowser
                webbrowser.open(simple_url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down — you can close this window.")
    finally:
        state.stop.set()
        # Join the queue worker so an in-flight job UNWINDS instead of being torn down as a daemon
        # thread at interpreter exit. That unwinding is what runs _run_ffmpeg's finally, which is the
        # only thing that force-kills and reaps the ffmpeg child — on Windows the child is detached
        # from the console (CREATE_NO_WINDOW), so Ctrl+C cannot reach it and skipping this leaks a
        # stray ffmpeg.exe. stop is already set above, and it is folded into the job's cancel probe
        # (job_cancel_probe), so the engine's 0.3s poll aborts the encode almost immediately. Bounded:
        # a wedged child must never make the app unclosable — after the timeout we exit anyway,
        # which is no worse than the old behaviour.
        _join_worker_and_shutdown_notifications(state, worker)
        try:
            state._io_pool.shutdown(wait=False)
        except Exception:
            pass
        try:
            state.stop_desync()   # don't orphan the local anti-throttle proxy on :8799
        except Exception:
            pass
        state.journal.release_run_lock()
        httpd.shutdown()
