#!/usr/bin/env python3
"""
Final YouTube + GPU Tool (Merged Best-of-3 from 2025 collection)
- Robust Android API 14 extraction + layered fallbacks (from efficient_gpu_2.4)
- Parallel downloads, psutil, BandwidthMonitor, speed opts (from youtube_downloader4)
- Premiere profiles, verified 3-method CUDA/NVENC testing, direct FFmpeg control + fallbacks (from video_encoder)

Fixes applied:
- Correct postprocessor_args dict wrapper (never bare list)
- Fixed CUDA pix_fmt / surface handling (safe by default)
- Decoupled yt-dlp download from final encode/remux for reliability
- Fast "copy" mode vs quality re-encode
- Cleaner structure (CapabilityDetector, FFmpegProcessor, etc.)
"""

import re
import time
import subprocess
import logging
import os
from pathlib import Path
from urllib.parse import urlparse
import shutil
import threading
from collections import deque
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse

try:
    from ._version import __version__
except ImportError:
    from _version import __version__

try:
    from .dependency_contract import (
        declared_runtime_floors,
        installed_distribution_version,
        satisfies_floor,
    )
except ImportError:
    from dependency_contract import (
        declared_runtime_floors,
        installed_distribution_version,
        satisfies_floor,
    )

try:
    from .installer_lock import (
        INSTALLER_BUSY_MESSAGE,
        InstallerBusyError,
        installer_guard,
    )
except ImportError:
    from installer_lock import (
        INSTALLER_BUSY_MESSAGE,
        InstallerBusyError,
        installer_guard,
    )

try:
    from .ffmpeg_updater import (
        cache_dir as _app_cache_dir,
        fetch_release as fetch_ffmpeg_release,
        ffmpeg_launch_gate,
        host_os_label,
        managed_ffmpeg_path,
        managed_install_supported,
        managed_locations_by_os,
        managed_root_display,
    )
except ImportError:
    from ffmpeg_updater import (
        cache_dir as _app_cache_dir,
        fetch_release as fetch_ffmpeg_release,
        ffmpeg_launch_gate,
        host_os_label,
        managed_ffmpeg_path,
        managed_install_supported,
        managed_locations_by_os,
        managed_root_display,
    )

try:
    from .capability import resolve_ffmpeg
except ImportError:
    from capability import resolve_ffmpeg

try:
    from .executable_trust import explicit_path_executable
except ImportError:
    from executable_trust import explicit_path_executable

try:
    from .redaction import SensitiveDataFilter
    from .secureio import open_private_file
except ImportError:
    from redaction import SensitiveDataFilter
    from secureio import open_private_file

try:
    from .url_identity import (
        dedupe_download_urls,
        normalize_youtube_url,
        output_identity_lock,
    )
except ImportError:
    from url_identity import (
        dedupe_download_urls,
        normalize_youtube_url,
        output_identity_lock,
    )

try:
    from .notifications import (
        DeadlineBoundSender,
        NotificationDispatcher,
        persisted_notification,
        safe_notification_origin,
        sanitize_notification_error,
    )
except ImportError:
    from notifications import (
        DeadlineBoundSender,
        NotificationDispatcher,
        persisted_notification,
        safe_notification_origin,
        sanitize_notification_error,
    )

_ENGINE_UNSET = object()
NOTIFICATION_DELIVERY_TIMEOUT_S = 4.0

def win_no_window_kw():
    """CREATE_NO_WINDOW on Windows, {} everywhere else — provably a no-op on macOS/Linux.

    Every console child (ffmpeg, ffprobe, GPU probes) otherwise flashes a console window when the
    parent has no console of its own (pythonw / a shortcut / a service). Under "Start yt_final.bat"
    the parent owns a console and children inherit it, so this is hardening, not a live fix.

    Deliberately NOT applied to: the detached bgutil helper (CREATE_NO_WINDOW is mutually exclusive
    with DETACHED_PROCESS -> ERROR_INVALID_PARAMETER), the interactive sign-in browser (must be
    visible), and the desync child (needs an OS-enforced job-object reap first, or the flag turns a
    console-group child into an unreapable orphan).
    """
    import subprocess as _sp
    import sys as _s
    if _s.platform.startswith('win'):
        return {'creationflags': getattr(_sp, 'CREATE_NO_WINDOW', 0)}
    return {}


def _ffmpeg_child_group_kw():
    """start_new_session on POSIX so an ffmpeg child leads its OWN process group/session.

    _kill_process_tree's POSIX branch is killpg(getpgid(child), SIGKILL) and its contract requires
    the child to be a group/session leader. _run_ffmpeg used to spawn ffmpeg into the app's own
    group, so getpgid(child) returned the APP's pgid and every cancel/timeout kill SIGKILLed the
    whole process (web server included) on macOS/Linux. Windows stays {} deliberately: taskkill /T
    walks the tree by pid without needing a group, and CREATE_NEW_PROCESS_GROUP would change
    Ctrl+C delivery to the child under "Start yt_final.bat".
    """
    if _sys.platform.startswith('win'):
        return {}
    return {'start_new_session': True}


# Windows reserved DEVICE names. Illegal as a whole path segment even with an extension, so an
# `organize_by=uploader` channel literally named "CON"/"NUL"/"AUX" yields an uncreatable folder.
_WIN_RESERVED_NAMES = frozenset(
    ['con', 'prn', 'aux', 'nul']
    + [f'com{i}' for i in range(1, 10)]
    + [f'lpt{i}' for i in range(1, 10)]
)


def guard_reserved_uploader_dir(ydl, info):
    """organize_by='uploader' emits %(uploader)s as a bare directory segment. yt-dlp sanitizes
    illegal CHARACTERS but not reserved DEVICE names, so a channel named e.g. "CON" produces a
    folder Windows refuses to create. Rewrite just that segment with a trailing underscore.

    win32-only and only when the uploader actually collides, so macOS is provably untouched."""
    if not _sys.platform.startswith('win'):
        return
    try:
        tmpl = (ydl.params or {}).get('outtmpl')
        tmpl_s = tmpl.get('default') if isinstance(tmpl, dict) else tmpl
        if not tmpl_s or '%(uploader)s' not in tmpl_s:
            return
        uploader = (info or {}).get('uploader') or (info or {}).get('channel') or ''
        # Reserved-ness is decided by the stem before the first '.', so "NUL.stuff" also collides.
        if uploader.split('.')[0].strip().lower() not in _WIN_RESERVED_NAMES:
            return
        # Underscore the reserved STEM (before the first dot), not the tail of the whole
        # string — 'NUL.official' must become 'NUL_.official', not 'NUL.official_'.
        stem, dot, rest = uploader.partition('.')
        safe_seg = (stem.rstrip(' .') + '_' + dot + rest) if dot else (uploader.rstrip(' .') + '_')
        new_tmpl = tmpl_s.replace('%(uploader)s', safe_seg.replace('%', '%%'))
        if isinstance(tmpl, dict):
            tmpl = dict(tmpl); tmpl['default'] = new_tmpl
            ydl.params['outtmpl'] = tmpl
        else:
            ydl.params['outtmpl'] = new_tmpl
        logger.warning(f"[organize] uploader '{uploader}' is a reserved Windows device name; "
                       f"using folder '{safe_seg}' instead")
    except Exception:
        pass      # never let output organisation break a download


# Setup logging (replaces heavy prints)
# Make StreamHandler safe for Windows consoles (cp1252 etc.)
import sys as _sys

def _resolve_log_path():
    """Where final_yt.log goes.

    This used to be a bare Path("final_yt.log") -- relative to the CURRENT DIRECTORY, opened at
    IMPORT time, before argparse ever ran. Three consequences, all of them wrong:

      * `yt-final --version` dropped a log file wherever the shell happened to be, and `serve`
        scattered one per launch directory.
      * With a non-writable CWD the IMPORT died with a bare PermissionError pointing at a
        logging line, before any command had run. An elevated cmd.exe starts in the Windows
        system directory, so "run as administrator" alone was enough to make the tool refuse
        to start, with an error naming nothing the user had done.
      * The log is the one place a user is told to look for a traceback, and it was never in the
        same place twice.

    Every other piece of durable state already lives under the app data directory; the log now
    joins it. YT_FINAL_LOG overrides, which is also how the tests pin it somewhere temporary.
    """
    override = os.environ.get("YT_FINAL_LOG")
    if override:
        return Path(override).expanduser()
    try:
        try:
            from ffmpeg_updater import app_data_dir
        except ImportError:
            from Final.ffmpeg_updater import app_data_dir  # type: ignore
        return Path(app_data_dir()) / "final_yt.log"
    except Exception:
        # Never let log placement stop the program from starting.
        return Path.home() / ".yt_final" / "final_yt.log"


_log_path = _resolve_log_path()
_log_flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
#: Set when the log file could not be opened; the program then runs with console logging only
#: rather than refusing to start. Surfaced once by _warn_if_file_logging_unavailable().
_file_logging_error = None
try:
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_fd = open_private_file(_log_path, _log_flags, 0o600)
    os.close(_log_fd)
except OSError as _log_exc:
    _file_logging_error = _log_exc
    _log_path = None
_log_formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s')
_root_logger = logging.getLogger()
_PRODUCT_LOGGER_BASENAMES = frozenset({
    "yt_final",
    "webserver",
    "optional_tools",
    "managed_env",
    "ffmpeg_updater",
    "capability",
    "markers",
    "nfo",
})
_PRODUCT_LOGGER_NAMES = frozenset(
    {
        logger_name
        for basename in _PRODUCT_LOGGER_BASENAMES
        for logger_name in (basename, f"Final.{basename}")
    }
    | {__name__}
)


class _ProductRecordsOnlyFilter(SensitiveDataFilter):
    def filter(self, record):
        if record.name not in _PRODUCT_LOGGER_NAMES:
            return False
        return super().filter(record)


class _SuppressProductRecords(logging.Filter):
    def filter(self, record):
        return record.name not in _PRODUCT_LOGGER_NAMES


def _has_filter_marker(target, marker: str) -> bool:
    return any(
        getattr(item, marker, False) for item in target.filters
    )


def _warn_if_file_logging_unavailable():
    """Say once, at command start, that this run is not writing a log file.

    Degrading beats aborting the import, but degrading SILENTLY is its own trap: the log is the
    one place the user is told to look for a traceback.
    """
    if _file_logging_error is None:
        return
    logger.warning(
        "[log] file logging is off for this run (%s). Console output only. "
        "Set YT_FINAL_LOG to a writable path to restore it.",
        _file_logging_error,
    )


def _same_log_file(handler) -> bool:
    if _log_path is None:          # file logging unavailable; nothing can match it
        return False
    return (
        isinstance(handler, logging.FileHandler)
        and os.path.normcase(os.path.abspath(handler.baseFilename))
        == os.path.normcase(os.path.abspath(_log_path))
    )


def _same_stdout(handler) -> bool:
    return (
        isinstance(handler, logging.StreamHandler)
        and getattr(handler, "stream", None) is _sys.stdout
    )


_sensitive_filter = SensitiveDataFilter()
_sensitive_filter._yt_final_sensitive_filter = True
for _product_logger_name in _PRODUCT_LOGGER_NAMES:
    _product_logger = logging.getLogger(_product_logger_name)
    if not _has_filter_marker(_product_logger, "_yt_final_sensitive_filter"):
        _product_logger.addFilter(_sensitive_filter)
    _product_logger.setLevel(logging.INFO)
    _product_logger.propagate = True
logger = logging.getLogger(__name__)

_external_root_handlers = list(_root_logger.handlers)
_suppression_filter = _SuppressProductRecords()
_suppression_filter._yt_final_product_suppression = True
for _handler in _external_root_handlers:
    if (
            getattr(_handler, "_yt_final_managed_handler", None) is None
            and (_same_stdout(_handler) or _same_log_file(_handler))
            and not _has_filter_marker(
                _handler, "_yt_final_product_suppression")):
        _handler.addFilter(_suppression_filter)

_stream_handler = next(
    (
        handler
        for handler in _root_logger.handlers
        if getattr(handler, "_yt_final_managed_handler", None) == "stream"
    ),
    None,
)
if _stream_handler is None:
    _stream_handler = logging.StreamHandler(_sys.stdout)
    _stream_handler._yt_final_managed_handler = "stream"
    _stream_handler.setFormatter(_log_formatter)
    _product_stream_filter = _ProductRecordsOnlyFilter()
    _product_stream_filter._yt_final_sensitive_filter = True
    _stream_handler.addFilter(_product_stream_filter)
    _root_logger.addHandler(_stream_handler)

_file_handler = next(
    (
        handler
        for handler in _root_logger.handlers
        if getattr(handler, "_yt_final_managed_handler", None) == "file"
    ),
    None,
)
if _file_handler is None and _log_path is not None:
    _file_handler = logging.FileHandler(_log_path, encoding='utf-8')
    _file_handler._yt_final_managed_handler = "file"
    _file_handler.setFormatter(_log_formatter)
    _product_file_filter = _ProductRecordsOnlyFilter()
    _product_file_filter._yt_final_sensitive_filter = True
    _file_handler.addFilter(_product_file_filter)
    _root_logger.addHandler(_file_handler)

try:
    # Python 3.7+
    _stream_handler.stream.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
if _sys.platform.startswith('win'):
    # The JSON stdout guard re-points matching stream handlers at sys.stderr, which was never
    # reconfigured. When stderr is REDIRECTED (the normal way a caller consumes --json) Windows
    # falls back to the ANSI code page, and every '→'/'⚠️' log record raises inside the handler.
    # Win-only so macOS keeps its default errors='backslashreplace'. pythonw can hand us a
    # None/shim stderr, hence the reconfigure guard.
    try:
        _sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


@contextmanager
def _json_stdout_guard(enabled=True):
    """Keep machine-readable stdout isolated without changing host logging policy."""
    if not enabled:
        yield
        return

    redirected = []
    guard_error_active = False
    try:
        for handler in list(_root_logger.handlers):
            if (
                    isinstance(handler, logging.StreamHandler)
                    and (
                        handler is _stream_handler
                        or getattr(handler, "stream", None) is _sys.stdout
                    )):
                original_stream = handler.stream
                redirected.append((handler, original_stream))
                # setStream() flushes a host-owned stream and is overridable. Either can raise
                # after mutating state, so swap the public stream under the handler's emit lock.
                handler.acquire()
                try:
                    handler.stream = _sys.stderr
                finally:
                    handler.release()
        yield
    except BaseException:
        guard_error_active = True
        raise
    finally:
        restore_error = None
        for handler, original_stream in reversed(redirected):
            try:
                handler.acquire()
                try:
                    handler.stream = original_stream
                finally:
                    handler.release()
            except BaseException as exc:
                # One hostile/broken host handler must not strand the remaining handlers or mask
                # an exception already escaping the JSON command.
                if restore_error is None:
                    restore_error = exc
        if restore_error is not None and not guard_error_active:
            raise restore_error


class DownloadCancelled(Exception):
    """Raised from progress_sink / download path when the journal requests cancel mid-run."""


class _PostDownloadFailure(Exception):
    """Carry a processing exception past the download retry planner unchanged."""

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


class _OutputIdentityChanged(RuntimeError):
    """Fail closed if one URL resolves to different output identities across retries."""


class _PostDownloadContractError(RuntimeError):
    """The downloader did not honor the synchronous exact-once finalization hook."""


class _SynchronousPostDownload:
    """Audit one downloader call's private post-download callback contract."""

    _UNSET = object()

    def __init__(self, callback):
        self._callback = callback
        self._owner_thread = threading.get_ident()
        self._guard = threading.Lock()
        self._open = True
        self._calls = 0
        self._callback_result = self._UNSET
        self._callback_failure = None
        self._violation = None

    @property
    def callback_failure(self):
        with self._guard:
            return self._callback_failure

    def __call__(self, paths):
        with self._guard:
            if threading.get_ident() != self._owner_thread or not self._open:
                error = _PostDownloadContractError(
                    "post-download callback must run synchronously before downloader returns"
                )
                self._violation = self._violation or error
                raise error
            if self._calls:
                error = _PostDownloadContractError(
                    "post-download callback must run exactly once"
                )
                self._violation = self._violation or error
                raise error
            self._calls = 1

        try:
            result = self._callback(paths)
        except BaseException as exc:
            with self._guard:
                self._callback_failure = exc
            raise
        with self._guard:
            self._callback_result = result
        return result

    def abort(self) -> None:
        with self._guard:
            self._open = False

    def finish(self, result):
        with self._guard:
            self._open = False
            violation = self._violation
            calls = self._calls
            callback_result = self._callback_result
            callback_failure = self._callback_failure

        if violation is not None:
            raise violation
        if callback_failure is not None:
            raise callback_failure
        if result and calls != 1:
            raise _PostDownloadContractError(
                "nonempty downloader result requires exactly one synchronous "
                "post-download callback"
            )
        if calls == 1 and callback_result is self._UNSET:
            raise _PostDownloadContractError(
                "post-download callback must complete synchronously before downloader returns"
            )
        if result and callback_result != result:
            raise _PostDownloadContractError(
                "downloader must return the synchronous post-download callback result"
            )
        return result


def _call_with_post_download_contract(call, callback, *args, **kwargs):
    """Invoke one downloader method and fail closed on a broken hook contract."""
    if callback is None:
        return call(*args, **kwargs)
    contract = _SynchronousPostDownload(callback)
    kwargs["_post_download"] = contract
    try:
        result = call(*args, **kwargs)
    except BaseException:
        contract.abort()
        raise
    return contract.finish(result)


def _windows_system_executable(filename):
    """Resolve one fixed Windows system executable without PATH/CWD search."""
    if (
        not _sys.platform.startswith('win')
        or not isinstance(filename, str)
        or Path(filename).name != filename
        or filename in {'', '.', '..'}
    ):
        return None
    try:
        import ctypes
        import stat

        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(32768)
        length = int(kernel32.GetSystemDirectoryW(buffer, len(buffer)))
        if length <= 0 or length >= len(buffer):
            return None
        system_directory = Path(buffer.value).resolve(strict=True)
        candidate = (system_directory / filename).resolve(strict=True)
        metadata = candidate.lstat()
        if (
            candidate.parent != system_directory
            or not candidate.is_absolute()
            or not stat.S_ISREG(metadata.st_mode)
            or candidate.is_symlink()
            or bool(
                getattr(metadata, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
            )
        ):
            return None
        return candidate
    except (AttributeError, OSError, RuntimeError, ValueError):
        return None


def _kill_process_tree(process):
    """Kill a child WE spawned together with its DESCENDANTS. Best-effort; never raises.

    A plain process.kill() only reaps the direct child. The turbo downloader (N_m3u8DL-RE) spawns
    ffmpeg for the merge/mux phase — we hand it --ffmpeg-binary-path — so killing just the parent
    orphans the muxer: it keeps writing into the job temp dir the caller is about to rmtree (on
    Windows those unlinks then fail and the .turbo_tmp dir survives with multi-GB partials that
    nothing ever sweeps), and it holds the inherited stdout/stderr pipes open, pinning the reader
    threads and their fds. Requires the child to be its own group/session leader — see the Popen
    kwargs in _run_turbo_cancellable. Mirrors optional_tools._terminate_bgutil_child.
    """
    import signal
    try:
        if process.poll() is not None:
            return                                   # already exited; nothing to kill
    except Exception:
        return
    if _sys.platform.startswith('win'):
        try:
            # Check the exit code: this used to `return` unconditionally, so a FAILING taskkill
            # (nonzero rc — e.g. access denied on an elevated child) silently skipped the documented
            # last-resort process.kill() below. Mirrors _terminate_bgutil_child in optional_tools.
            taskkill = _windows_system_executable('taskkill.exe')
            if taskkill is not None:
                _r = subprocess.run(
                    [str(taskkill), '/F', '/T', '/PID', str(process.pid)],
                    capture_output=True,
                    timeout=5,
                )
                if getattr(_r, 'returncode', 1) == 0:
                    return
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        process.kill()                               # last resort: at least stop the direct child
    except Exception:
        pass


def _cancel_pending(probe):
    """True when `probe` (a cheap zero-arg predicate) reports this job was cancelled.

    A probe that RAISES reads as 'not cancelled': a broken cancel channel must never be able to
    kill an otherwise healthy download."""
    if not probe:
        return False
    try:
        return bool(probe())
    except Exception:
        return False


class VerificationError(Exception):
    """Raised when a post-download --verify-strict integrity check fails (strict mode only)."""
    pass


# Future-proof config for pinned API tricks (Android spoofing etc.)
class YTConfig:
    ANDROID_CLIENT = "14.21.54"
    ANDROID_UA = f'com.google.android.youtube/{ANDROID_CLIENT} (Linux; U; Android 4.4; en_US) gzip'
    # Easy to update or fallback (pinned for spoofing tricks)

YT_CONFIG = YTConfig()


def _unavailable_tool_path(name: str) -> str:
    """Absolute non-existent sentinel that can never resolve through cwd/PATH."""
    leaf = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(name or 'tool'))
    return str(
        Path(__file__).resolve()
        / '.yt-final-unavailable-tools'
        / leaf
    )


def _trusted_absolute_tool(path, *, unavailable_name: str) -> str:
    """Return an existing absolute file or a safe absolute missing sentinel."""
    candidate = _existing_absolute_tool(path)
    if candidate is not None:
        return str(candidate)
    return _unavailable_tool_path(unavailable_name)


def _existing_absolute_tool(path) -> Path | None:
    """Return a resolved absolute regular file, never consulting PATH or cwd."""
    try:
        candidate = Path(path).expanduser()
        if candidate.is_absolute() and candidate.is_file():
            return candidate.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    return None


def _certified_managed_runtime_path(name: str) -> str | None:
    """Return only pointer-backed managed Node/Deno binaries for execution."""
    if name not in ('node', 'deno'):
        return None
    try:
        try:
            from .optional_tools import managed_runtime_path
        except ImportError:
            from optional_tools import managed_runtime_path
        selected = managed_runtime_path(name)
        return str(selected) if selected is not None else None
    except Exception:
        return None


def _managed_runtime_discovery(name: str) -> dict:
    """Return provenance-aware report data without promoting legacy binaries."""
    if name not in ('node', 'deno'):
        return {'path': None, 'verified': False, 'source': 'missing'}
    try:
        try:
            from .optional_tools import discover_managed_runtime
        except ImportError:
            from optional_tools import discover_managed_runtime
        info = discover_managed_runtime(name)
        if isinstance(info, dict):
            return {
                'path': info.get('path'),
                'verified': bool(info.get('verified')),
                'source': str(info.get('source') or 'missing'),
            }
    except Exception:
        pass
    return {'path': None, 'verified': False, 'source': 'missing'}


def _js_runtime_execution_path(name: str) -> str | None:
    """Prefer certified managed Node/Deno, then an explicit system PATH tool."""
    managed = _certified_managed_runtime_path(name)
    if managed:
        return managed
    resolved = explicit_path_executable(name)
    return str(resolved) if resolved is not None else None


# =============================================================================
# UPDATE CHECKER (scan-and-decide: never auto-applies; user chooses)
# =============================================================================
class UpdateChecker:
    """Scans for newer versions of the pip-managed components that actually go stale
    (yt-dlp + its nsig solver + the bgutil PO-token plugin) and reports ffmpeg/node/bgutil-server
    for info. It NEVER updates Python packages: `check-updates` is report-only and Python dependency
    changes require a future reviewed application release. A throttled one-line notice on launch
    tells you when something is outdated.

    Why only these: yt-dlp breaks whenever YouTube changes (nsig/SABR/PO-token), so it must stay
    current; ffmpeg/node are stable and updating them blindly risks breakage for no benefit; the
    bgutil plugin and server must move in lockstep, so we warn on mismatch rather than bump one.
    requests/urllib3 earn their slots for a DIFFERENT reason than freshness: their mere presence
    makes yt-dlp prefer its pooled RequestsRH transport, so TCP stays warm across the 10 MiB
    http_chunk_size boundary instead of reconnecting (and restarting the congestion window) every
    chunk — measured +67.6% median throughput, 11/12 interleaved rounds. yt-dlp hard-rejects old
    versions (requests>=2.32.2, urllib3>=2.0.2), so they must be installable AND updatable here.
    regex is the watch-filter safety boundary: its per-operation timeout prevents a persisted or
    submitted regular expression from monopolizing a poller/request worker.

    Performance: probes run in parallel; PyPI latest versions + full-scan results are
    short-TTL cached so reopening the Updates tab is near-instant. Optional managed-tool
    versions come only from the packaged immutable manifest. Pass force=True to bypass
    the scan cache.
    """

    # This is the exact seven-project direct input to the certified dependency
    # lock. Rows are diagnostic only; the sole update action applies the whole
    # hash-locked closure.
    PIP_PACKAGES = [
        'yt-dlp', 'yt-dlp-ejs', 'bgutil-ytdlp-pot-provider',
        'requests', 'urllib3', 'regex', 'packaging',
    ]
    # Import probes prove a dist is really usable (not just metadata present).
    # bgutil is a yt-dlp plugin package (files under yt_dlp_plugins/), not a top-level module.
    PIP_IMPORT_PROBES = {
        'yt-dlp': ('yt_dlp', 'yt_dlp.version'),
        'yt-dlp-ejs': ('yt_dlp_ejs',),
        'bgutil-ytdlp-pot-provider': (
            'yt_dlp_plugins.extractor.getpot_bgutil',
            'yt_dlp_plugins.extractor.getpot_bgutil_http',
        ),
        # No feature of their own here: importing `requests` pulls the whole chain (urllib3/idna/
        # certifi/adapters), which is exactly what yt-dlp needs to select RequestsRH.
        'requests': ('requests',),
        'urllib3': ('urllib3',),
        # Watch title filters use regex's per-operation timeout to bound adversarial patterns.
        'regex': ('regex',),
        # Used for deterministic version comparisons throughout runtime probes.
        'packaging': ('packaging',),
    }
    @staticmethod
    def _cache_path() -> Path:
        try:
            return _app_cache_dir() / 'update_check.json'
        except Exception:
            return (
                Path(os.getenv('LOCALAPPDATA') or (Path.home() / '.cache'))
                / 'yt_final' / 'update_check.json'
            )

    # Full report reuse (tab reopen / repeated poll). PyPI JSON is cached longer inside.
    # Note: do not cache a class-level Path for update_check.json — use _cache_path() so
    # macOS (Library/Caches) and Windows (%LOCALAPPDATA%) stay correct at call time.
    SCAN_CACHE_TTL = 90
    PYPI_CACHE_TTL = 3600
    FFMPEG_RELEASE_CACHE_TTL = 3600
    _scan_mem = {}          # key -> (ts, report)
    _pypi_mem = {}          # pkg -> (ts, version)
    _ffmpeg_release_mem = None  # (ts, validated release version)
    _cache_lock = threading.Lock()

    @staticmethod
    def _subprocess_kw():
        kw = {}
        if _sys.platform.startswith('win'):
            kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        return kw


    @staticmethod
    def _ensure_managed_imports():
        """Refresh caches without activating app-writable managed code."""
        try:
            import importlib
            importlib.invalidate_caches()
        except Exception:
            pass

    @staticmethod
    def _metadata_version(pkg):
        try:
            from importlib.metadata import version, PackageNotFoundError
            try:
                return version(pkg)
            except PackageNotFoundError:
                return None
        except Exception:
            return None

    @classmethod
    def _venv_metadata_version(cls, pkg):
        """Compatibility stub: unproven app-data interpreters are never run."""
        del pkg
        return None

    @classmethod
    def _imports_ok(cls, pkg) -> bool:
        """True when at least one package-specific import probe succeeds in-process."""
        probes = cls.PIP_IMPORT_PROBES.get(pkg) or ()
        if not probes:
            return True
        import importlib
        for name in probes:
            try:
                importlib.import_module(name)
                return True
            except Exception:
                continue
        return False

    @classmethod
    def _venv_imports_ok(cls, pkg) -> bool:
        """Compatibility stub: unproven app-data interpreters are never run."""
        del pkg
        return False

    @classmethod
    def _package_probe(cls, pkg) -> dict:
        """Detect a real install visible to the trusted current process.

        Returns {version, verified, source} where source is process|None.
        """
        cls._ensure_managed_imports()
        version = cls._metadata_version(pkg)
        source = 'process' if version else None
        verified = False
        if version:
            verified = cls._imports_ok(pkg)
            # Metadata without a working import is not a healthy install.
            if not verified:
                # Still report the version so UI can show "broken install".
                pass
        return {
            'version': version,
            'verified': bool(verified) if version else False,
            'source': source,
        }

    @staticmethod
    def _installed(pkg):
        """Back-compat: trusted-process distribution version or None."""
        try:
            return UpdateChecker._package_probe(pkg).get('version')
        except Exception:
            return None

    @classmethod
    def _pip_row_note(cls, *, installed, latest, outdated, verified, source):
        if not installed:
            return 'Not installed (install a future reviewed yt_final release)'
        src = {
            'process': 'visible to this process',
        }.get(source or '', 'detected')
        if not verified:
            return (
                f'Version {installed} found ({src}) but import check failed — '
                'install a future reviewed yt_final release'
            )
        if latest is None:
            return f'Installed {installed} ({src}, verified); latest check unavailable'
        if outdated:
            return f'Installed {installed} ({src}, verified); PyPI has {latest}'
        return f'Installed {installed} ({src}, verified) — up to date'

    @classmethod
    def _pypi_latest(cls, pkg, timeout=3):
        """PyPI latest version with in-process TTL cache (avoids 3 serial HTTP round-trips)."""
        now = time.time()
        with cls._cache_lock:
            hit = cls._pypi_mem.get(pkg)
            if hit and (now - hit[0]) < cls.PYPI_CACHE_TTL:
                return hit[1]
        import urllib.request, json
        ver = None
        try:
            req = urllib.request.Request(
                f'https://pypi.org/pypi/{pkg}/json',
                headers={'User-Agent': 'yt-final-update-checker', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ver = json.load(r)['info']['version']
        except Exception:
            ver = None
        if ver is not None:
            with cls._cache_lock:
                cls._pypi_mem[pkg] = (now, ver)
        return ver

    @staticmethod
    def _normalize_release_tag(tag):
        """Strip common release prefixes (v1.2.3, release-1.2.3) for display + compare."""
        if not tag:
            return None
        t = str(tag).strip()
        if not t or t in ('(not running)', 'installed', '—', '-'):
            return None
        low = t.lower()
        if low.startswith('release-'):
            t = t[8:]
        elif low.startswith('release_'):
            t = t[8:]
        if len(t) >= 2 and (t[0] == 'v' or t[0] == 'V') and t[1].isdigit():
            t = t[1:]
        return t or None

    @staticmethod
    def _looks_prerelease(tag) -> bool:
        """Heuristic for beta/rc/alpha release labels."""
        if not tag:
            return False
        return bool(re.search(
            r'(?:^|[-_.])(alpha|beta|rc|pre|preview|dev|snapshot)(?:\d|[-_.]|$)',
            str(tag), re.I,
        ))

    @classmethod
    def _is_comparable_version(cls, value) -> bool:
        """True when value has digit version tokens (not sentinels like 'installed')."""
        if not value or value in ('(not running)', 'installed', '—', '-'):
            return False
        return bool(re.search(r'\d', str(value)))

    @classmethod
    def _optional_latest_version(
            cls, name, *, platform_name=None, architecture=None):
        """Return only a certified host artifact version; manual-only tools have none."""
        if name not in ('node', 'deno'):
            return None
        try:
            import platform as py_platform
            try:
                from .tool_manifest import artifact_for
            except ImportError:
                from tool_manifest import artifact_for
            artifact = artifact_for(
                name,
                platform_name if platform_name is not None else _sys.platform,
                architecture if architecture is not None else py_platform.machine(),
            )
            return artifact.version
        except Exception:
            return None

    @classmethod
    def _probe_playwright(cls) -> dict:
        """Playwright package + Chromium browser presence (optional legacy tooling; Auth Login uses the native browser, NOT Playwright)."""
        try:
            try:
                from optional_tools import probe_playwright
            except ImportError:
                from Final.optional_tools import probe_playwright  # type: ignore
            return probe_playwright() or {}
        except Exception:
            return {'version': None, 'importable': False, 'chromium': False}

    @classmethod
    def _resolve_js_runtime(cls, name: str) -> dict:
        """Report runtime path/version while preserving managed provenance."""
        discovery = _managed_runtime_discovery(name)
        candidates: list[tuple[str, bool, str]] = []
        if discovery.get('verified') and discovery.get('path'):
            candidates.append((
                str(discovery['path']),
                True,
                'certified-pointer',
            ))
        path_executable = explicit_path_executable(name)
        if path_executable is not None:
            candidates.append((str(path_executable), False, 'system-path'))
        seen = set()
        for path, verified, source in candidates:
            if not path or path in seen:
                continue
            seen.add(path)
            try:
                if not Path(path).is_absolute() or not Path(path).is_file():
                    continue
            except Exception:
                continue
            if name == 'deno':
                ver = cls._cmd_first_line([path, '--version'], r'deno (\S+)', timeout=1.5)
            else:
                ver = cls._cmd_first_line([path, '--version'], timeout=2)
            if ver:
                return {
                    'path': path,
                    'version': ver,
                    'verified': verified,
                    'source': source,
                }
        # Legacy unversioned app files may remain visible in reports, but even
        # a version probe would execute unverified bytes. Return provenance
        # without running them.
        if discovery.get('source') == 'legacy' and discovery.get('path'):
            return {
                'path': str(discovery['path']),
                'version': None,
                'verified': False,
                'source': 'legacy',
            }
        return {
            'path': None,
            'version': None,
            'verified': False,
            'source': 'missing',
        }

    @staticmethod
    def _abspath_tool(path):
        """Return an explicit absolute tool path without cwd lookup."""
        if not path:
            return None
        try:
            candidate = Path(path)
            if candidate.is_absolute() and candidate.is_file():
                return str(candidate.resolve(strict=True))
        except Exception:
            pass
        resolved = explicit_path_executable(Path(str(path)).name)
        return str(resolved) if resolved is not None else None

    @classmethod
    def _ffmpeg_release_latest(cls):
        """Return the validated Gyan release version, cached independently for one hour."""
        now = time.time()
        with cls._cache_lock:
            hit = cls._ffmpeg_release_mem
            if hit and (now - hit[0]) < cls.FFMPEG_RELEASE_CACHE_TTL:
                return hit[1]
        try:
            version = fetch_ffmpeg_release().version
        except Exception:
            return None
        with cls._cache_lock:
            cls._ffmpeg_release_mem = (now, version)
        return version

    @staticmethod
    def _is_managed_ffmpeg(path):
        try:
            return os.path.normcase(os.path.abspath(str(path))) == os.path.normcase(
                os.path.abspath(str(managed_ffmpeg_path())))
        except Exception:
            return False

    @staticmethod
    def _preferred_ffmpeg_path():
        """Prefer the same validated managed pair used by runtime capability discovery."""
        return _trusted_absolute_tool(
            resolve_ffmpeg().path,
            unavailable_name='ffmpeg',
        )

    @staticmethod
    def _ver_tuple(v):
        nums = re.findall(r'\d+', v or '')
        return tuple(int(x) for x in nums) if nums else (0,)

    @classmethod
    def _outdated(cls, installed, latest):
        if not installed or not latest:
            return False
        if not cls._is_comparable_version(installed) or not cls._is_comparable_version(latest):
            return False
        inst_n = cls._normalize_release_tag(installed) or installed
        lat_n = cls._normalize_release_tag(latest) or latest
        # Equal base versions: installed stable is not behind a -beta latest.
        if cls._ver_tuple(inst_n) == cls._ver_tuple(lat_n):
            if cls._looks_prerelease(lat_n) and not cls._looks_prerelease(inst_n):
                return False
            if cls._looks_prerelease(inst_n) and not cls._looks_prerelease(lat_n):
                return True  # beta install, stable latest
            return False
        return cls._ver_tuple(lat_n) > cls._ver_tuple(inst_n)

    @classmethod
    def _cmd_first_line(cls, args, pattern=None, timeout=2):
        try:
            r = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, **cls._subprocess_kw())
            out = (r.stdout or r.stderr).strip()
            if pattern:
                m = re.search(pattern, out)
                if m:
                    return m.group(1)
            return out.splitlines()[0] if out else None
        except Exception:
            return None

    @staticmethod
    def _bgutil_server_version(base='http://127.0.0.1:4416'):
        """Local PO helper. Short timeout — if it is not running, don't stall the whole scan."""
        import urllib.request, json
        try:
            with urllib.request.urlopen(base.rstrip('/') + '/ping', timeout=0.35) as r:
                return json.load(r).get('version')
        except Exception:
            return None

    @classmethod
    def _scan_cache_key(cls, check_pypi, include_optional_tools, pot_base_url, ffmpeg_path):
        return (
            bool(check_pypi),
            bool(include_optional_tools),
            str(pot_base_url or ''),
            str(ffmpeg_path or 'ffmpeg'),
        )

    @classmethod
    def invalidate_cache(cls):
        """Drop the in-memory report cache after an external state change."""
        with cls._cache_lock:
            cls._scan_mem.clear()
            # Keep remote-version memory; the next scan re-reads local state.

    @classmethod
    def record_ffmpeg_release(cls, version):
        """Atomically replace stale release/report caches after a verified install."""
        with cls._cache_lock:
            cls._scan_mem.clear()
            cls._ffmpeg_release_mem = (time.time(), str(version))

    @classmethod
    def scan(cls, check_pypi=True, ffmpeg_path=None, pot_base_url=None,
             include_optional_tools=True, force=False, cache_ttl=None):
        """Return {'pip': [...], 'info': [...], 'warnings': [...], 'summary': {...}}.

        Read-only; never changes anything. `include_optional_tools` adds aria2c / N_m3u8DL-RE /
        probes used by the web UI Updates tab.

        force=False reuses a recent scan (default TTL SCAN_CACHE_TTL seconds). force=True always
        re-probes local tools (PyPI still uses PYPI_CACHE_TTL unless you clear _pypi_mem).
        """
        ffmpeg_path = _trusted_absolute_tool(
            ffmpeg_path if ffmpeg_path is not None else cls._preferred_ffmpeg_path(),
            unavailable_name='ffmpeg',
        )
        ttl = cls.SCAN_CACHE_TTL if cache_ttl is None else float(cache_ttl)
        key = cls._scan_cache_key(check_pypi, include_optional_tools, pot_base_url, ffmpeg_path)
        now = time.time()
        if not force and ttl > 0:
            with cls._cache_lock:
                hit = cls._scan_mem.get(key)
            if hit and (now - hit[0]) < ttl:
                # Shallow copy so callers can stamp checked_at without mutating the cache.
                cached = dict(hit[1])
                cached['pip'] = list(hit[1].get('pip') or [])
                cached['info'] = list(hit[1].get('info') or [])
                cached['warnings'] = list(hit[1].get('warnings') or [])
                cached['summary'] = dict(hit[1].get('summary') or {})
                cached['cached'] = True
                cached['cache_age_s'] = round(now - hit[0], 2)
                return cached

        report = cls._scan_uncached(
            check_pypi=check_pypi,
            ffmpeg_path=ffmpeg_path,
            pot_base_url=pot_base_url,
            include_optional_tools=include_optional_tools,
        )
        report['cached'] = False
        with cls._cache_lock:
            cls._scan_mem[key] = (now, report)
        # Return a copy so cache entry stays stable if caller mutates
        out = dict(report)
        out['pip'] = list(report['pip'])
        out['info'] = list(report['info'])
        out['warnings'] = list(report['warnings'])
        out['summary'] = dict(report['summary'])
        return out

    @classmethod
    def _scan_uncached(cls, check_pypi=True, ffmpeg_path=None, pot_base_url=None,
                       include_optional_tools=True):
        """Parallel local + optional PyPI probes. Dominated by network only when PyPI is cold."""
        ffmpeg_path = _trusted_absolute_tool(
            ffmpeg_path,
            unavailable_name='ffmpeg',
        )
        pot = pot_base_url or 'http://127.0.0.1:4416'
        results = {}
        # Refresh trusted-process import caches before parallel probes.
        cls._ensure_managed_imports()

        def _run(k, fn):
            try:
                return k, fn()
            except Exception:
                return k, None

        jobs = []
        # pip packages: trusted-process metadata plus real import verification.
        for pkg in cls.PIP_PACKAGES:
            jobs.append((f'probe:{pkg}', lambda p=pkg: cls._package_probe(p)))
            if check_pypi:
                jobs.append((f'pypi:{pkg}', lambda p=pkg: cls._pypi_latest(p)))
        jobs.append(('ffmpeg', lambda: cls._cmd_first_line(
            [ffmpeg_path, '-version'], r'ffmpeg version ((?:\d+(?:\.\d+)*)\b|\S+)', timeout=2)))
        jobs.append(('ffmpeg_latest', cls._ffmpeg_release_latest))
        # Resolve PATH + managed locations so install-to-tools-folder is detected.
        jobs.append(('node_info', lambda: cls._resolve_js_runtime('node')))
        jobs.append(('deno_info', lambda: cls._resolve_js_runtime('deno')))
        jobs.append(('bgutil_server', lambda: cls._bgutil_server_version(pot)))
        jobs.append(('plugin', lambda: cls._installed('bgutil-ytdlp-pot-provider')))
        jobs.append(('playwright_info', cls._probe_playwright))
        # "Latest" for optional tools is local provenance data, never a mutable
        # release feed. Unsupported helpers deliberately resolve to None/manual.
        for tool_name in (
                'node', 'deno', 'aria2c', 'N_m3u8DL-RE',
                'bgutil-server', 'playwright'):
            jobs.append((
                f'optional-latest:{tool_name}',
                lambda n=tool_name: cls._optional_latest_version(n),
            ))

        if include_optional_tools:
            def _find_aria():
                try:
                    return YouTubeDownloader._find_aria2c(YouTubeDownloader.__new__(YouTubeDownloader))
                except Exception:
                    return None

            def _find_turbo():
                try:
                    return YouTubeDownloader._find_n_m3u8dl(YouTubeDownloader.__new__(YouTubeDownloader))
                except Exception:
                    return None

            jobs.append(('aria_path', _find_aria))
            jobs.append(('turbo_path', _find_turbo))

        # The provider is report-only in 1.0.0; its diagnostic latest value
        # retains the same explicit PyPI gate as the other Python rows.
        if check_pypi:
            jobs.append((
                'optional-latest:bgutil-plugin',
                lambda: cls._pypi_latest('bgutil-ytdlp-pot-provider'),
            ))

        # Cap workers: enough for all jobs, avoid oversubscription on tiny machines.
        workers = min(16, max(4, len(jobs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_run, k, fn) for k, fn in jobs]
            for fut in as_completed(futs):
                try:
                    k, v = fut.result()
                    results[k] = v
                except Exception:
                    pass

        # aria2 / turbo version after path known (cheap if present)
        aria = results.get('aria_path') if include_optional_tools else None
        turbo = results.get('turbo_path') if include_optional_tools else None
        if aria:
            aria = cls._abspath_tool(aria)
        if turbo:
            turbo = cls._abspath_tool(turbo)
        aria_ver = None
        turbo_ver = None
        if aria:
            aria_ver = cls._cmd_first_line([aria, '--version'], r'aria2 version (\S+)', timeout=2)
        if turbo:
            # N_m3u8DL-RE prints a short banner; prefer a version-like token, else "installed".
            turbo_ver = cls._cmd_first_line(
                [turbo, '--version'],
                r'(\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?)',
                timeout=5,
            )
            if not turbo_ver:
                banner = cls._cmd_first_line([turbo, '--version'], None, timeout=5)
                if banner:
                    turbo_ver = banner.split()[0][:40]
                else:
                    turbo_ver = 'installed'

        report = {'pip': [], 'info': [], 'warnings': []}
        for pkg in cls.PIP_PACKAGES:
            probe = results.get(f'probe:{pkg}') or {}
            if not isinstance(probe, dict):
                probe = {'version': probe, 'verified': bool(probe), 'source': None}
            inst = probe.get('version')
            verified = bool(probe.get('verified')) if inst else False
            source = probe.get('source')
            latest = results.get(f'pypi:{pkg}') if check_pypi else None
            outdated = cls._outdated(inst, latest)
            current = bool(inst) and verified and not outdated
            report['pip'].append({
                'name': pkg,
                'installed': inst,
                'latest': latest,
                'outdated': outdated,
                'updatable': True,
                'verified': verified,
                'source': source,
                'current': current,
                'note': cls._pip_row_note(
                    installed=inst, latest=latest, outdated=outdated,
                    verified=verified, source=source,
                ),
            })
            if inst and not verified:
                report['warnings'].append(
                    f'{pkg} {inst} is on disk but failed the import check — use Reinstall'
                )
        ffmpeg_installed = results.get('ffmpeg')
        ffmpeg_latest = results.get('ffmpeg_latest')
        ffmpeg_managed = cls._is_managed_ffmpeg(ffmpeg_path)
        ffmpeg_outdated = cls._outdated(ffmpeg_installed, ffmpeg_latest)
        ffmpeg_updatable = managed_install_supported()
        os_label = host_os_label()
        locations = managed_locations_by_os()
        managed_location = locations.get(os_label)
        if not ffmpeg_updatable:
            ffmpeg_note = (
                'Managed FFmpeg is certified only for Windows x86_64; '
                + (
                    'install FFmpeg manually or with Homebrew'
                    if os_label == 'macOS'
                    else 'install FFmpeg manually'
                )
            )
        elif ffmpeg_installed is None:
            ffmpeg_note = f'FFmpeg not found; managed install available on this {os_label} host'
        elif ffmpeg_latest is None:
            ffmpeg_note = 'Latest release unavailable; installed version was not marked outdated'
        elif ffmpeg_outdated:
            ffmpeg_note = f'A newer managed FFmpeg release is available for {os_label}'
        elif ffmpeg_managed:
            ffmpeg_note = f'Managed FFmpeg is current ({os_label})'
        else:
            ffmpeg_note = (
                f'System FFmpeg is current on {os_label}; '
                f'managed install path: {managed_location}'
            )
        report['info'].append({
            'name': 'ffmpeg',
            'installed': ffmpeg_installed,
            'latest': ffmpeg_latest,
            'outdated': ffmpeg_outdated,
            'managed': ffmpeg_managed,
            'path': str(ffmpeg_path) if ffmpeg_path else None,
            'updatable': ffmpeg_updatable,
            'managed_location': managed_location,
            'managed_locations': locations,
            'host_os': os_label,
            'host_platform': _sys.platform,
            'note': ffmpeg_note,
        })
        # Report-level host OS lets the UI explain whether Windows-only managed
        # FFmpeg is available and keeps Node/Deno actions host-specific.
        report['host_os'] = os_label
        report['host_platform'] = _sys.platform
        report['managed_locations'] = locations
        def _opt_latest(name):
            return results.get(f'optional-latest:{name}')

        def _opt_outdated(installed, latest):
            # Skip non-version sentinels so "not running" / "installed" never look outdated.
            if not cls._is_comparable_version(installed):
                return False
            return cls._outdated(installed, latest)

        node_info = results.get('node_info') or {}
        if not isinstance(node_info, dict):
            node_info = {}
        node_ver = node_info.get('version')
        node_path = node_info.get('path')
        node_source = node_info.get('source')
        node_verified = bool(node_info.get('verified'))
        node_latest = _opt_latest('node')
        node_outdated = _opt_outdated(node_ver, node_latest)
        if node_source == 'legacy':
            node_note = (
                'An unversioned legacy managed Node file was found but is not '
                'trusted or executed. Install the verified managed artifact.'
            )
        elif not node_ver:
            node_note = (
                'JS nsig solver (Node 22+ recommended). '
                'A verified managed build is available on certified platforms.'
            )
        elif node_outdated:
            node_note = (
                f'JS nsig solver (Node 22+ recommended). '
                f'Update available: {node_latest}.'
            )
        else:
            node_note = (
                'JS nsig solver (Node 22+ recommended). Already covers nsig if present.'
            )
        report['info'].append({
            'name': 'node', 'installed': node_ver,
            'path': node_path,
            'latest': node_latest, 'outdated': node_outdated, 'updatable': False,
            # A managed action exists only when the immutable catalog has one
            # unambiguous artifact for this exact host platform/architecture.
            'installable': node_latest is not None,
            'verified': node_verified,
            'source': node_source,
            'note': node_note,
        })
        deno_info = results.get('deno_info') or {}
        if not isinstance(deno_info, dict):
            deno_info = {}
        deno_ver = deno_info.get('version')
        deno_path = deno_info.get('path')
        deno_source = deno_info.get('source')
        deno_verified = bool(deno_info.get('verified'))
        deno_latest = _opt_latest('deno')
        deno_outdated = _opt_outdated(deno_ver, deno_latest)
        if deno_source == 'legacy':
            deno_note = (
                'An unversioned legacy managed Deno file was found but is not '
                'trusted or executed. Install the verified managed artifact.'
            )
        elif not deno_ver:
            deno_note = (
                'Optional JS runtime for nsig (Node is enough if present). '
                'A verified managed build is available on certified platforms.'
            )
        elif deno_outdated:
            deno_note = f'Optional JS runtime for nsig — update available ({deno_latest}).'
        else:
            deno_note = 'Optional JS runtime for nsig — installed.'
        report['info'].append({
            'name': 'deno', 'installed': deno_ver,
            'path': deno_path,
            'latest': deno_latest, 'outdated': deno_outdated, 'updatable': False,
            'installable': deno_latest is not None,
            'verified': deno_verified,
            'source': deno_source,
            'note': deno_note,
        })
        plugin = results.get('plugin')
        server = results.get('bgutil_server')
        plugin_latest = _opt_latest('bgutil-plugin')
        plugin_outdated = _opt_outdated(plugin, plugin_latest)
        if not plugin:
            plugin_note = (
                'yt-dlp PO-token plugin. Python package changes require a '
                'future reviewed yt_final release.'
            )
        elif plugin_outdated:
            plugin_note = (
                f'yt-dlp PO-token plugin v{plugin} — PyPI has {plugin_latest} '
                f'(pairs with bgutil-server).'
            )
        else:
            plugin_note = f'yt-dlp PO-token plugin v{plugin} (pairs with bgutil-server).'
        report['info'].append({
            'name': 'bgutil-plugin',
            'installed': plugin,
            'latest': plugin_latest, 'outdated': plugin_outdated, 'updatable': False,
            'installable': False,
            'note': plugin_note,
        })
        server_latest = _opt_latest('bgutil-server')
        # Compare only when the local HTTP server answered with a version string.
        server_outdated = _opt_outdated(server, server_latest)
        if server:
            server_note = (
                f'PO-token HTTP helper running ({server})'
                + (f' — newer release {server_latest}.' if server_outdated else '.')
            )
            server_installed = server
        else:
            server_note = (
                'PO-token HTTP helper is not running; tokenless clients remain available. '
                'Automatic installation and implicit script discovery are disabled in '
                '1.0.0; use an explicitly reviewed manual setup if needed.'
            )
            server_installed = '(not running)'
        report['info'].append({
            'name': 'bgutil-server',
            'installed': server_installed,
            'path': None,
            'latest': server_latest, 'outdated': server_outdated, 'updatable': False,
            'installable': False,
            'note': server_note,
        })
        if plugin and server and cls._ver_tuple(plugin) != cls._ver_tuple(server):
            report['warnings'].append(
                f"bgutil plugin ({plugin}) != server ({server}) — update BOTH to match")
        if include_optional_tools:
            aria_latest = _opt_latest('aria2c')
            aria_installed_disp = aria_ver or ('installed' if aria else None)
            aria_outdated = _opt_outdated(aria_ver, aria_latest)
            if not aria:
                aria_note = (
                    'Optional progressive-HTTP engine. Automatic installation '
                    'is unavailable for 1.0.0; install manually from a source you trust.'
                )
            elif aria_outdated:
                aria_note = (
                    f'Progressive HTTP downloads — update available ({aria_latest}).'
                )
            else:
                aria_note = (
                    'Ready for progressive HTTP downloads (optional; DASH/HLS still use built-in).'
                )
            turbo_latest = _opt_latest('N_m3u8DL-RE')
            turbo_installed_disp = turbo_ver or ('installed' if turbo else None)
            turbo_outdated = _opt_outdated(turbo_ver, turbo_latest)
            if not turbo:
                turbo_note = (
                    'Optional turbo HLS engine. Automatic installation is unavailable '
                    'for 1.0.0; install manually from a source you trust.'
                )
            elif turbo_outdated:
                turbo_note = f'Turbo HLS engine — update available ({turbo_latest}).'
            else:
                turbo_note = 'Ready for turbo HLS playlists (DASH stays native).'
            report['info'].append({
                'name': 'aria2c',
                'installed': aria_installed_disp,
                'path': aria,
                'latest': aria_latest,
                'outdated': aria_outdated,
                'updatable': False,
                'installable': False,
                'note': aria_note,
            })
            report['info'].append({
                'name': 'N_m3u8DL-RE',
                # Never put the full filesystem path in "installed" — UI shows path separately.
                'installed': turbo_installed_disp,
                'path': turbo,
                'latest': turbo_latest,
                'outdated': turbo_outdated,
                'updatable': False,
                'installable': False,
                'note': turbo_note,
            })
            # Playwright is optional tooling only — Auth Login uses real Chrome (Google blocks automation).
            pw = results.get('playwright_info') or {}
            if not isinstance(pw, dict):
                pw = {}
            pw_ver = pw.get('version') if pw.get('importable') else None
            pw_latest = _opt_latest('playwright')
            pw_outdated = _opt_outdated(pw_ver, pw_latest)
            pw_chromium = bool(pw.get('chromium'))
            if not pw_ver:
                pw_note = (
                    'Optional. Auth Login uses real Chrome (not Playwright). '
                    'Install only if you need Playwright for other tooling.'
                )
                pw_installed = None
            elif not pw_chromium:
                pw_note = f'Playwright {pw_ver} imported, but Chromium is missing — Install again.'
                pw_installed = f'{pw_ver} (no chromium)'
            elif pw_outdated:
                pw_note = f'Playwright {pw_ver} + Chromium — update available ({pw_latest}).'
                pw_installed = pw_ver
            else:
                pw_note = f'Playwright {pw_ver} + Chromium installed (optional).'
                pw_installed = pw_ver
            report['info'].append({
                'name': 'playwright',
                'installed': pw_installed,
                'latest': pw_latest,
                'outdated': pw_outdated or (bool(pw_ver) and not pw_chromium),
                'updatable': False,
                'installable': False,
                'chromium': pw_chromium,
                'note': pw_note,
            })
        outdated = [p['name'] for p in report['pip'] if p.get('outdated')]
        missing_pip = [p['name'] for p in report['pip'] if not p.get('installed')]
        broken_pip = [
            p['name'] for p in report['pip']
            if p.get('installed') and p.get('verified') is False
        ]
        # Optional helpers (exclude ffmpeg — it has its own section / managed path).
        optional_rows = [
            i for i in report['info']
            if i.get('name') and i.get('name') != 'ffmpeg'
        ]
        optional_outdated = [i['name'] for i in optional_rows if i.get('outdated')]
        optional_missing = [
            i['name'] for i in optional_rows
            if (not i.get('installed')) or i.get('installed') == '(not running)'
        ]
        report['summary'] = {
            'outdated_count': len(outdated),
            'outdated': outdated,
            'missing_pip': missing_pip,
            'broken_pip': broken_pip,
            'optional_outdated': optional_outdated,
            'optional_missing': optional_missing,
            'warning_count': len(report['warnings']),
            # "ok" = download packages healthy (helpers tracked separately for the banner).
            'ok': (
                not outdated and not report['warnings']
                and not missing_pip and not broken_pip
            ),
        }
        return report

    @classmethod
    def print_report(cls, report):
        print("\n=== Update scan ===")
        for p in report['pip']:
            if not p['installed']:
                status = 'not installed'
            elif p.get('verified') is False:
                status = 'BROKEN (import failed)'
            elif p['outdated']:
                status = 'UPDATE AVAILABLE'
            elif p.get('current') or p.get('verified'):
                status = 'up-to-date (verified)'
            else:
                status = 'up-to-date'
            note = f"  {p['note']}" if p.get('note') else ''
            print(
                f"  {p['name']:32} {str(p['installed']):14} -> {str(p['latest'] or '?'):14}"
                f" [{status}]{note}"
            )
        for i in report['info']:
            if i.get('updatable'):
                if i.get('outdated'):
                    tag = 'managed update available'
                elif i.get('managed'):
                    tag = 'managed, current'
                else:
                    tag = 'managed install available'
                latest = i.get('latest')
                suffix = f" -> {latest}" if latest else ''
                note = i.get('note') or tag
                print(f"  {i['name']:32} {str(i['installed']):14}{suffix:16} [{tag}]  {note}")
            else:
                print(f"  {i['name']:32} {str(i['installed']):14} (info only — update manually)")
        for w in report['warnings']:
            print(f"  [!] {w}")
        outdated = [p['name'] for p in report['pip'] if p['outdated']]
        if outdated:
            print(
                "\n  Install a future reviewed yt_final release to update "
                "Python dependencies."
            )
        elif not report['warnings']:
            print(
                "\n  Scanned packages are current. Python dependency changes "
                "ship only in reviewed releases."
            )

    @classmethod
    def run_update(cls, all_=False, assume_yes=False, pot_base_url=None):
        """Reject Python mutation before scan, prompt, filesystem, or process I/O."""
        del cls, all_, assume_yes, pot_base_url
        try:
            from managed_env import (
                CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE,
                require_certified_python_runtime_activation,
            )
            from tool_manifest import UnsupportedManagedInstall
        except ImportError:
            from Final.managed_env import (  # type: ignore
                CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE,
                require_certified_python_runtime_activation,
            )
            from Final.tool_manifest import UnsupportedManagedInstall  # type: ignore
        try:
            require_certified_python_runtime_activation()
        except UnsupportedManagedInstall:
            print(
                "Certified Python update unavailable: "
                f"{CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE}"
            )
            raise SystemExit(1)
        raise SystemExit(1)  # Defensive: the policy function must never return.

    @classmethod
    def passive_notice(cls, ttl_hours=24, pot_base_url=None):
        """Throttled (cached) one-line notice on launch. Non-blocking, never changes anything."""
        try:
            import json
            now = time.time()
            outdated, warnings = None, None
            cache_path = cls._cache_path()
            if cache_path.exists():
                try:
                    c = json.loads(cache_path.read_text())
                    if (now - c.get('ts', 0)) < ttl_hours * 3600:
                        outdated, warnings = c.get('outdated', []), c.get('warnings', [])
                except Exception:
                    pass
            if outdated is None:  # stale or missing -> rescan
                rep = cls.scan(check_pypi=True, pot_base_url=pot_base_url)
                outdated = [p['name'] for p in rep['pip'] if p['outdated']]
                warnings = rep['warnings']
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps({
                        'ts': now, 'outdated': outdated, 'warnings': warnings,
                    }))
                except Exception:
                    pass
            if outdated:
                logger.info(f"[Update] Newer version available: {', '.join(outdated)}. "
                            "Run 'python yt_final.py check-updates' to review; "
                            "install a future reviewed release to update Python dependencies.")
            for w in (warnings or []):
                logger.warning(f"[Update] {w}")
            # Not cached (unlike the version scan): re-check every launch so the reminder persists until
            # fixed. Only fires when THIS runtime lacks yt-dlp's pooled transport (e.g. a managed venv
            # that predates requests being added), which is otherwise invisible but materially slower.
            if not _pooled_transport_available():
                logger.warning(
                    "[Update] Downloads are on the UNPOOLED urllib transport. "
                    "Install a future reviewed yt_final release with requests + urllib3 "
                    "to enable yt-dlp's pooled transport (~+33-67% throughput on fast connections).")
        except Exception:
            logger.debug("update passive notice failed (non-fatal)")


# Optional dependencies (graceful) - none top level now

# Clean import for split module (capability.py) - supports both package and direct script run.
# Cross-alias 'capability' and 'Final.capability' to same module object to prevent
# duplicate class definitions / isinstance failures when mixing import styles.
def _get_capability_detector():
    import sys as _sys
    try:
        from .capability import CapabilityDetector
        _cap_mod = _sys.modules.get(CapabilityDetector.__module__) or _sys.modules.get('Final.capability')
        if _cap_mod is not None:
            _sys.modules.setdefault('capability', _cap_mod)
            _sys.modules.setdefault('Final.capability', _cap_mod)
        return CapabilityDetector
    except ImportError:
        from capability import CapabilityDetector
        _cap_mod = _sys.modules.get('capability')
        if _cap_mod is not None:
            _sys.modules.setdefault('Final.capability', _cap_mod)
            _sys.modules.setdefault('capability', _cap_mod)
        return CapabilityDetector

CapabilityDetector = _get_capability_detector()


def _get_get_detector():
    try:
        from .capability import get_detector
    except ImportError:
        from capability import get_detector
    return get_detector
get_detector = _get_get_detector()

# NOTE: CapabilityDetector is imported from capability.py (split module)


def _get_jobspec():
    """Import JobSpec supporting both package ('Final.jobspec') and direct-script ('jobspec') runs,
    mirroring the capability.py dual-import so module identity stays single."""
    import sys as _sys
    try:
        from .jobspec import JobSpec
        _mod = _sys.modules.get(JobSpec.__module__) or _sys.modules.get('Final.jobspec')
        if _mod is not None:
            _sys.modules.setdefault('jobspec', _mod)
            _sys.modules.setdefault('Final.jobspec', _mod)
        return JobSpec
    except ImportError:
        from jobspec import JobSpec
        _mod = _sys.modules.get('jobspec')
        if _mod is not None:
            _sys.modules.setdefault('Final.jobspec', _mod)
            _sys.modules.setdefault('jobspec', _mod)
        return JobSpec

JobSpec = _get_jobspec()


def _get_journal_cls():
    """Lazy dual-import of DownloadJournal (jobstore.py). Lazy so the CLI fast-path never pays for it."""
    try:
        from .jobstore import DownloadJournal
        return DownloadJournal
    except ImportError:
        from jobstore import DownloadJournal
        return DownloadJournal


def _get_history_cls():
    """Lazy dual-import of HistoryLedger (jobstore.py). Lazy so the CLI fast-path never pays for it."""
    try:
        from .jobstore import HistoryLedger
        return HistoryLedger
    except ImportError:
        from jobstore import HistoryLedger
        return HistoryLedger

# 2. BANDWIDTH MONITOR (from youtube_downloader4.py)
# =============================================================================
class BandwidthMonitor:
    def __init__(self, window_size=8):
        self.speeds = deque(maxlen=window_size)
        self.cached_speed = 50  # Mbps default

    def get_bandwidth(self):
        return self.cached_speed

    def update_speed(self, speed_mbps):
        """Update from progress hook for real monitoring."""
        self.speeds.append(speed_mbps)
        if self.speeds:
            self.cached_speed = sum(self.speeds) / len(self.speeds)

    def get_optimal_chunk_size(self, bandwidth_mbps=None):
        if bandwidth_mbps is None:
            bandwidth_mbps = self.cached_speed
        if bandwidth_mbps >= 100:
            return 64 * 1024 * 1024
        elif bandwidth_mbps >= 50:
            return 48 * 1024 * 1024
        elif bandwidth_mbps >= 25:
            return 32 * 1024 * 1024
        return 16 * 1024 * 1024


# =============================================================================
# 3. FFMPEG PROCESSOR (fixed + enhanced from video_encoder + 2.4)
# =============================================================================
class FFmpegProcessor:
    _active_process_lock = threading.Lock()
    _active_processes = 0

    @classmethod
    def active_process_count(cls) -> int:
        with cls._active_process_lock:
            return cls._active_processes

    @classmethod
    def ffmpeg_busy(cls) -> bool:
        return cls.active_process_count() > 0

    @classmethod
    def _begin_ffmpeg_activity(cls) -> None:
        # Increment while holding the updater's activation gate. An updater
        # therefore sees this launch as busy before it can swap the executable.
        with ffmpeg_launch_gate():
            with cls._active_process_lock:
                cls._active_processes += 1

    @classmethod
    def _end_ffmpeg_activity(cls) -> None:
        with cls._active_process_lock:
            cls._active_processes -= 1

    def __init__(self, detector: CapabilityDetector):
        self.detector = detector
        self.ffmpeg = detector.ffmpeg_path
        self.profiles = self._load_profiles()

    @staticmethod
    def _load_profiles():
        # Static so list-profiles etc can use without instantiating detector/engine.
        # `family` is derived from video_codec in the builder; each profile only sets the knobs
        # relevant to its encoder family. Existing keys are preserved for backward compatibility.
        return {
            # ---- AUTO: fastest working H.264 encoder for THIS machine (resolved in _fallback_chain) ----
            # Sentinel, NOT a real ffmpeg profile: _fallback_chain expands it to the best available HW
            # H.264 (NVENC -> AMF -> QSV -> VideoToolbox) and finally libx264, by runtime working_encoders.
            # container mp4 (all H.264 targets are); NVIDIA hosts resolve to premiere_h264_fast (== old default).
            "auto_h264": {
                "name": "Auto — fastest H.264 for your hardware (Recommended)",
                "auto": "h264", "video_codec": "", "audio_codec": "aac",
                "audio_bitrate": "192k", "container": "mp4",
            },
            # ---- NVENC H.264 (Premiere-friendly defaults) ----
            "premiere_h264_fast": {
                "name": "Premiere H.264 Fast (NVENC)",
                "video_codec": "h264_nvenc", "audio_codec": "aac",
                "preset": "p4", "tune": "hq", "cq": 21, "bf": 3,
                "profile": "high", "level": "4.2", "bitrate": "5M", "audio_bitrate": "192k",
                "use_gpu": True, "use_cuda_surfaces": False, "container": "mp4",
            },
            "premiere_h264_fast_cuda": {
                "name": "Premiere H.264 Fast CUDA (Expert)",
                "video_codec": "h264_nvenc", "audio_codec": "aac",
                "preset": "p4", "tune": "hq", "cq": 21, "profile": "high",
                "bitrate": "6M", "audio_bitrate": "192k",
                "use_gpu": True, "use_cuda_surfaces": True, "container": "mp4",
            },
            "premiere_h264_high": {
                "name": "Premiere H.264 High Quality",
                "video_codec": "h264_nvenc", "audio_codec": "aac",
                "preset": "p6", "tune": "hq", "cq": 19, "bf": 3, "multipass": "fullres", "aq": True,
                "profile": "high", "level": "4.2", "bitrate": "8M", "audio_bitrate": "256k",
                "use_gpu": True, "use_cuda_surfaces": False, "container": "mp4",
            },
            "nvenc_h264_quality": {
                "name": "NVENC H.264 Max Quality (p7)",
                "video_codec": "h264_nvenc", "audio_codec": "aac",
                "preset": "p7", "tune": "hq", "cq": 19, "bf": 3, "multipass": "fullres", "aq": True,
                "profile": "high", "level": "4.2", "audio_bitrate": "256k",
                "use_gpu": True, "container": "mp4",
            },
            # ---- NVENC HEVC ----
            "premiere_h265_4k": {
                "name": "Premiere H.265 4K",
                "video_codec": "hevc_nvenc", "audio_codec": "aac",
                "preset": "p5", "tune": "hq", "cq": 24, "bitrate": "12M",
                "profile": "main", "audio_bitrate": "256k", "tag": "hvc1",
                "use_gpu": True, "use_cuda_surfaces": False, "container": "mp4",
            },
            "nvenc_h265_quality": {
                "name": "NVENC HEVC 10-bit Max Quality (p7)",
                "video_codec": "hevc_nvenc", "audio_codec": "aac",
                "preset": "p7", "tune": "hq", "cq": 24, "bf": 4, "multipass": "fullres", "aq": True,
                "pix_fmt": "p010le", "tag": "hvc1", "profile": "main10", "audio_bitrate": "256k",
                "use_gpu": True, "container": "mp4",
            },
            # ---- NVENC AV1 (Ada RTX 40+ / Blackwell) ----
            "nvenc_av1_fast": {
                "name": "NVENC AV1 Fast (Ada+)",
                "video_codec": "av1_nvenc", "audio_codec": "aac",
                "preset": "p2", "tune": "hq", "cq": 32, "audio_bitrate": "192k",
                "use_gpu": True, "container": "mp4",
            },
            "nvenc_av1_quality": {
                "name": "NVENC AV1 10-bit Quality (Ada+)",
                "video_codec": "av1_nvenc", "audio_codec": "aac",
                "preset": "p7", "tune": "hq", "cq": 30, "bf": 3, "multipass": "fullres", "aq": True,
                "pix_fmt": "p010le", "audio_bitrate": "256k",
                "use_gpu": True, "container": "mp4",
            },
            # ---- AMD AMF ----
            "amf_h264": {
                "name": "AMD AMF H.264 (Quality)",
                "video_codec": "h264_amf", "audio_codec": "aac",
                "amf_quality": "quality", "rc": "vbr_peak", "bitrate": "8M", "maxrate": "16M",
                "audio_bitrate": "192k", "container": "mp4",
            },
            "amf_hevc": {
                "name": "AMD AMF HEVC 10-bit (Quality)",
                "video_codec": "hevc_amf", "audio_codec": "aac",
                "amf_quality": "quality", "rc": "vbr_peak", "bitrate": "8M", "maxrate": "16M",
                "pix_fmt": "p010le", "tag": "hvc1", "audio_bitrate": "256k", "container": "mp4",
            },
            "amf_av1": {
                "name": "AMD AMF AV1 (High Quality)",
                "video_codec": "av1_amf", "audio_codec": "aac",
                "amf_quality": "high_quality", "rc": "vbr_peak", "bitrate": "8M", "maxrate": "16M",
                "pix_fmt": "p010le", "audio_bitrate": "256k", "container": "mp4",
            },
            # ---- Intel QSV ----
            "qsv_h264": {
                "name": "Intel QSV H.264 (Quality)",
                "video_codec": "h264_qsv", "audio_codec": "aac",
                "qsv_preset": "veryslow", "global_quality": 22, "look_ahead": 1,
                "pix_fmt": "nv12", "audio_bitrate": "192k", "container": "mp4",
            },
            "qsv_hevc": {
                "name": "Intel QSV HEVC 10-bit (Quality)",
                "video_codec": "hevc_qsv", "audio_codec": "aac",
                "qsv_preset": "veryslow", "global_quality": 23,
                "pix_fmt": "p010le", "tag": "hvc1", "audio_bitrate": "256k", "container": "mp4",
            },
            "qsv_av1": {
                "name": "Intel QSV AV1 (Quality)",
                "video_codec": "av1_qsv", "audio_codec": "aac",
                "qsv_preset": "veryslow", "global_quality": 27,
                "pix_fmt": "p010le", "audio_bitrate": "256k", "container": "mp4",
            },
            # ---- Apple VideoToolbox (macOS) ----
            "vt_h264": {
                "name": "Apple VideoToolbox H.264 (macOS)",
                "video_codec": "h264_videotoolbox", "audio_codec": "aac",
                "bitrate": "8M", "maxrate": "12M", "profile": "high",
                "audio_bitrate": "192k", "use_gpu": True, "container": "mp4",
            },
            "vt_h264_fast": {
                "name": "Apple VideoToolbox H.264 Fast (macOS)",
                "video_codec": "h264_videotoolbox", "audio_codec": "aac",
                "bitrate": "5M", "maxrate": "8M", "profile": "high",
                "audio_bitrate": "192k", "use_gpu": True, "container": "mp4",
            },
            "vt_hevc": {
                "name": "Apple VideoToolbox HEVC (macOS)",
                "video_codec": "hevc_videotoolbox", "audio_codec": "aac",
                "bitrate": "8M", "maxrate": "14M", "tag": "hvc1",
                "audio_bitrate": "256k", "use_gpu": True, "container": "mp4",
            },
            "vt_prores_hq": {
                "name": "Apple VideoToolbox ProRes HQ (macOS)",
                "video_codec": "prores_videotoolbox", "audio_codec": "pcm_s16le",
                "prores_profile": 3, "pix_fmt": "yuv422p10le",
                "use_gpu": True, "container": "mov",
            },
            # ---- CPU ----
            "premiere_h264_cpu": {
                "name": "CPU Fallback (libx264)",
                "video_codec": "libx264", "audio_codec": "aac",
                "preset": "medium", "crf": 20, "audio_bitrate": "192k",
                "profile": "high", "use_gpu": False, "use_cuda_surfaces": False, "container": "mp4",
            },
            "cpu_h265": {
                "name": "CPU HEVC 10-bit (libx265)",
                "video_codec": "libx265", "audio_codec": "aac",
                "preset": "slow", "crf": 20, "pix_fmt": "yuv420p10le", "tag": "hvc1",
                "x265_params": "keyint=240:min-keyint=24", "audio_bitrate": "192k", "container": "mp4",
            },
            "cpu_av1": {
                "name": "CPU AV1 (libaom, archival)",
                "video_codec": "libaom-av1", "audio_codec": "aac",
                "crf": 30, "cpu_used": 6, "row_mt": True, "tiles": "2x2",
                "pix_fmt": "yuv420p10le", "audio_bitrate": "192k", "container": "mkv",
            },
            "cpu_av1_svt": {
                "name": "CPU AV1 (SVT-AV1, fast)",
                "video_codec": "libsvtav1", "audio_codec": "aac",
                "preset": "6", "crf": 32, "svtav1_params": "tune=0",
                "pix_fmt": "yuv420p10le", "audio_bitrate": "192k", "container": "mkv",
            },
            # ---- SVT-AV1 film-grain synthesis (A5: 15-25% bitrate win on grainy/film) ----
            # film-grain=N re-synthesizes grain the encoder was allowed to discard; denoise=1
            # strips it before encode then re-adds on decode. Gated by try/fallback in
            # encode_with_fallback (older SVT-AV1 errors on unknown params).
            "cpu_av1_svt_filmgrain": {
                "name": "CPU AV1 SVT film-grain (grainy/film archival)",
                "video_codec": "libsvtav1", "audio_codec": "aac",
                "preset": "6", "crf": 30, "svtav1_params": "tune=0:film-grain=8:film-grain-denoise=0",
                "pix_fmt": "yuv420p10le", "audio_bitrate": "192k", "container": "mkv",
            },
            # ---- NVENC AV1 constrained-quality VBR (A6: predictable delivery size) ----
            "nvenc_av1_vbr": {
                "name": "NVENC AV1 constrained-VBR (Ada+, capped size)",
                "video_codec": "av1_nvenc", "audio_codec": "aac",
                "preset": "p6", "tune": "hq", "rc": "vbr", "cq": 30,
                "bitrate": "12M", "maxrate": "20M", "bf": 3, "multipass": "fullres", "aq": True,
                "pix_fmt": "p010le", "audio_bitrate": "256k",
                "use_gpu": True, "container": "mp4",
            },
            # ---- Editing / intermediate (Premiere / Resolve / Avid) ----
            "prores_proxy": {
                "name": "ProRes 422 Proxy (editing)",
                "video_codec": "prores_ks", "audio_codec": "pcm_s16le",
                "prores_profile": 0, "qscale": 13, "pix_fmt": "yuv422p10le",
                "vendor": "apl0", "container": "mov",
            },
            "prores_lt": {
                "name": "ProRes 422 LT (light editing/proxy)",
                "video_codec": "prores_ks", "audio_codec": "pcm_s16le",
                "prores_profile": 1, "qscale": 13, "pix_fmt": "yuv422p10le",
                "vendor": "apl0", "container": "mov",
            },
            "prores_4444": {
                "name": "ProRes 4444 (VFX/compositing, alpha-capable)",
                "video_codec": "prores_ks", "audio_codec": "pcm_s24le",
                "prores_profile": 4, "qscale": 7, "pix_fmt": "yuva444p10le",
                "vendor": "apl0", "container": "mov",
            },
            "prores_hq": {
                "name": "ProRes 422 HQ (mastering)",
                "video_codec": "prores_ks", "audio_codec": "pcm_s16le",
                "prores_profile": 3, "qscale": 9, "pix_fmt": "yuv422p10le",
                "vendor": "apl0", "container": "mov",
            },
            "dnxhr_hq": {
                "name": "DNxHR HQ 8-bit (Avid/Premiere)",
                "video_codec": "dnxhd", "audio_codec": "pcm_s16le",
                "dnxhr_profile": "dnxhr_hq", "pix_fmt": "yuv422p", "container": "mov",
            },
            "dnxhr_hqx": {
                "name": "DNxHR HQX 10-bit (Avid/Premiere)",
                "video_codec": "dnxhd", "audio_codec": "pcm_s16le",
                "dnxhr_profile": "dnxhr_hqx", "pix_fmt": "yuv422p10le", "container": "mov",
            },
            # Classic fixed-bitrate DNxHD (Avid legacy). DNxHD is res+fps locked, so we force a
            # valid combo via the profile-level filter: 1080p25 HQ 8-bit == DNxHD 185.
            "dnxhd_hq_1080p25": {
                "name": "DNxHD 185 (1080p25 HQ 8-bit, Avid legacy)",
                "video_codec": "dnxhd", "audio_codec": "pcm_s16le",
                "dnxhd_bitrate": "185M", "vf": "scale=1920:1080,fps=25,format=yuv422p",
                "pix_fmt": "yuv422p", "container": "mov",
            },
            # ---- Remux / copy ----
            "copy_fast": {
                "name": "Fast Copy / Remux (No Re-encode, mp4)",
                "video_codec": "copy", "audio_codec": "copy",
                "use_gpu": False, "use_cuda_surfaces": False, "container": "mp4",
            },
            "copy_mkv": {
                "name": "Fast Copy / Remux to MKV (most compatible)",
                "video_codec": "copy", "audio_codec": "copy",
                "use_gpu": False, "use_cuda_surfaces": False, "container": "mkv",
            },
        }

    @staticmethod
    def _family(video_codec):
        vc = video_codec or ""
        if vc == "copy":
            return "copy"
        if "nvenc" in vc:
            return "nvenc"
        if "_amf" in vc:
            return "amf"
        if "_qsv" in vc:
            return "qsv"
        # VideoToolbox before generic "prores" (prores_videotoolbox contains both tokens)
        if "videotoolbox" in vc:
            return "videotoolbox"
        if "prores" in vc:
            return "prores"
        if vc == "dnxhd":
            return "dnxhd"
        if vc == "libaom-av1":
            return "aom"
        if vc == "libsvtav1":
            return "svt"
        return "x26x"  # libx264 / libx265

    @staticmethod
    def _bitrate_to_bufsize(br):
        """Parse '5M'/'800k' -> a doubled bufsize string in kbit (e.g. '10000k', '1600k')."""
        try:
            s = str(br).strip()
            unit = s[-1].lower() if s and s[-1].lower() in ('k', 'm', 'g') else ''
            num = float(s[:-1]) if unit else float(s)
            kbit = num * {'k': 1, 'm': 1000, 'g': 1000000, '': 0.001}[unit]
            return f"{int(kbit * 2)}k"
        except Exception:
            return "16000k"

    def output_ext(self, profile_name):
        """Container extension a profile should produce (drives collision-safe naming + remux container)."""
        p = self.profiles.get(profile_name, {})
        cont = p.get("container")
        if cont:
            return "." + cont
        fam = self._family(p.get("video_codec", ""))
        if fam in ("prores", "dnxhd"):
            return ".mov"
        if fam in ("aom", "svt"):
            return ".mkv"
        return ".mp4"

    def build_ffmpeg_command(self, input_path, output_path, profile_name="premiere_h264_fast",
                             *, metadata=None, vf_extra=None, overrides=None, ffmetadata_file=None):
        """Per-family command builder (NVENC / AMF / QSV / libx26x / AV1 CPU / ProRes / DNxHR / copy).

        Keystone K3 added four keyword-only channels (all default-off, so legacy calls are
        byte-for-byte unchanged):
          overrides       — dict shallow-merged over the profile (e.g. {'audio_codec':'flac'}).
          vf_extra        — extra -vf filter graph appended (re-encode only; ignored on copy).
          metadata        — {tag: value} -> -metadata tag=value (skipped if ffmetadata_file given,
                            which already carries the global tags).
          ffmetadata_file — an ffmetadata file (chapters + global tags) muxed in as a 2nd input.
        """
        if profile_name not in self.profiles:
            profile_name = "premiere_h264_fast"
        p = dict(self.profiles[profile_name])          # copy so per-call overrides never mutate the table
        if overrides:
            p.update({k: v for k, v in overrides.items() if v is not None})
        vc = p.get("video_codec", "copy")
        fam = self._family(vc)
        container = p.get("container") or self.output_ext(profile_name).lstrip(".")

        cmd = [self.ffmpeg, '-hide_banner', '-y']

        # Hardware-decode flags MUST precede -i. Only for the explicit NVENC CUDA-surfaces profile.
        use_surfaces = fam == "nvenc" and p.get("use_cuda_surfaces") and self.detector.gpu_info.get('cuda_available')
        if use_surfaces:
            cmd.extend(['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'])

        cmd.extend(['-i', str(input_path)])
        if ffmetadata_file:
            # Second input = chapters + global tags (no media streams).
            cmd.extend(['-i', str(ffmetadata_file)])

        # Video filter graph (re-encode only; -vf is illegal with -c:v copy and with CUDA surfaces).
        if fam != "copy" and not use_surfaces:
            vf_parts = [x for x in (p.get('vf'), vf_extra) if x]
            if vf_parts:
                cmd.extend(['-vf', ','.join(vf_parts)])
        elif use_surfaces and vf_extra:
            logger.warning("[Encode] CUDA-surface profile: --burn-in/subtitle video filter skipped "
                           "(frames live on GPU); use a non-CUDA profile to apply it")

        if fam == "copy":
            if p.get('audio_codec') == 'copy':
                cmd.extend(['-c', 'copy'])
            else:
                cmd.extend(['-c:v', 'copy'])
        else:
            cmd.extend(['-c:v', vc])
            self._append_video_args(cmd, p, fam)
            # pix_fmt: skip when riding CUDA surfaces (frames live on GPU); else apply explicit/default.
            if not use_surfaces:
                cmd.extend(['-pix_fmt', p.get('pix_fmt', 'yuv420p')])
            if p.get('tag'):
                cmd.extend(['-tag:v', p['tag']])

        # Audio
        ac = p.get('audio_codec', 'aac')
        if not (fam == "copy" and ac == 'copy'):
            if ac == 'copy':
                cmd.extend(['-c:a', 'copy'])
            else:
                cmd.extend(['-c:a', ac])
                # bitrate is meaningless for lossless/uncompressed audio codecs
                lossless = str(ac).startswith('pcm') or ac in ('flac', 'alac')
                if p.get('audio_bitrate') and not lossless:
                    cmd.extend(['-b:a', p['audio_bitrate']])

        # Chapters + global metadata muxed from the ffmetadata 2nd input (B1/B2).
        if ffmetadata_file:
            cmd.extend(['-map_metadata', '1', '-map_chapters', '1'])
        elif metadata:
            for k, v in metadata.items():
                if v is None or v == '':
                    continue
                cmd.extend(['-metadata', f'{k}={v}'])

        # Escape hatch for caller-injected output options (HDR color flags A2, -map, etc.).
        extra = p.get('_extra_out_args')
        if extra:
            cmd.extend(str(x) for x in extra)

        # Container finalization
        if container == "mp4":
            cmd.extend(['-movflags', '+faststart'])
        cmd.extend(['-fflags', '+genpts'])
        cmd.append(str(output_path))
        return cmd

    def _append_video_args(self, cmd, p, fam):
        """Family-specific video encoder arguments."""
        if fam == "nvenc":
            cmd.extend(['-preset', str(p.get('preset', 'p4'))])
            if p.get('tune'):
                cmd.extend(['-tune', p['tune']])
            if p.get('profile'):
                cmd.extend(['-profile:v', str(p['profile'])])
            if p.get('level'):
                cmd.extend(['-level:v', str(p['level'])])
            cmd.extend(['-rc', p.get('rc', 'vbr')])
            if p.get('cq') is not None:
                cmd.extend(['-cq', str(p['cq'])])
            if p.get('bitrate'):
                cmd.extend(['-b:v', p['bitrate'], '-maxrate', p.get('maxrate', p['bitrate']),
                            '-bufsize', self._bitrate_to_bufsize(p.get('maxrate', p['bitrate']))])
            else:
                cmd.extend(['-b:v', '0'])  # cq-only capped quality
            if p.get('bf') is not None:
                cmd.extend(['-bf', str(p['bf'])])
            if p.get('multipass'):
                cmd.extend(['-multipass', str(p['multipass'])])
            if p.get('aq'):
                cmd.extend(['-spatial-aq', '1', '-temporal-aq', '1', '-aq-strength', '8'])
            cmd.extend(['-rc-lookahead', '20'])

        elif fam == "amf":
            cmd.extend(['-quality', p.get('amf_quality', 'quality')])
            cmd.extend(['-rc', p.get('rc', 'vbr_peak')])
            if p.get('bitrate'):
                cmd.extend(['-b:v', p['bitrate'], '-maxrate', p.get('maxrate', p['bitrate']),
                            '-bufsize', self._bitrate_to_bufsize(p.get('maxrate', p['bitrate']))])
            # quality-boosting analysis (supported across AMF h264/hevc/av1)
            cmd.extend(['-preanalysis', 'true'])

        elif fam == "qsv":
            cmd.extend(['-preset', p.get('qsv_preset', 'medium')])
            if p.get('global_quality') is not None:
                cmd.extend(['-global_quality', str(p['global_quality'])])
            if p.get('look_ahead') is not None:
                cmd.extend(['-look_ahead', str(p['look_ahead'])])
            cmd.extend(['-extbrc', '1'])

        elif fam in ("x26x",):
            if p.get('crf') is not None:
                cmd.extend(['-crf', str(p['crf'])])
            elif p.get('bitrate'):
                cmd.extend(['-b:v', p['bitrate']])
            cmd.extend(['-preset', p.get('preset', 'medium')])
            if p.get('profile'):
                cmd.extend(['-profile:v', str(p['profile'])])
            if p.get('level'):
                cmd.extend(['-level', str(p['level'])])
            if p.get('x265_params'):
                cmd.extend(['-x265-params', p['x265_params']])

        elif fam == "svt":
            cmd.extend(['-preset', str(p.get('preset', '6')), '-crf', str(p.get('crf', 32))])
            if p.get('svtav1_params'):
                cmd.extend(['-svtav1-params', p['svtav1_params']])

        elif fam == "aom":
            cmd.extend(['-crf', str(p.get('crf', 30)), '-b:v', '0',
                        '-cpu-used', str(p.get('cpu_used', 6))])
            if p.get('row_mt'):
                cmd.extend(['-row-mt', '1'])
            if p.get('tiles'):
                cmd.extend(['-tiles', p['tiles']])

        elif fam == "videotoolbox":
            # Apple HW encode: bitrate-oriented for H.264/HEVC; profile for ProRes VT.
            vc = p.get('video_codec', '')
            if 'prores' in vc:
                if p.get('prores_profile') is not None:
                    cmd.extend(['-profile:v', str(p['prores_profile'])])
            else:
                if p.get('profile'):
                    cmd.extend(['-profile:v', str(p['profile'])])
                if p.get('bitrate'):
                    cmd.extend(['-b:v', p['bitrate']])
                    if p.get('maxrate'):
                        cmd.extend(['-maxrate', p['maxrate']])
                    cmd.extend(['-bufsize', self._bitrate_to_bufsize(p.get('maxrate', p['bitrate']))])
                if p.get('allow_sw') is not None:
                    # allow_sw=0 forces hardware; default lets FFmpeg fall back if needed
                    cmd.extend(['-allow_sw', '1' if p.get('allow_sw') else '0'])

        elif fam == "prores":
            cmd.extend(['-profile:v', str(p.get('prores_profile', 3)),
                        '-qscale:v', str(p.get('qscale', 9)),
                        '-vendor', p.get('vendor', 'apl0')])

        elif fam == "dnxhd":
            if p.get('dnxhd_bitrate'):
                # Classic fixed-bitrate DNxHD: -b:v drives the profile; res/fps are pinned by the
                # profile-level `vf` filter (FFmpeg rejects an illegal bitrate/res/fps combo).
                cmd.extend(['-b:v', str(p['dnxhd_bitrate'])])
            else:
                cmd.extend(['-profile:v', p.get('dnxhr_profile', 'dnxhr_hq')])

    def _fallback_chain(self, profile_name):
        """Ordered, availability-aware list of profiles to try (graceful HW->SW degradation)."""
        # Semantic "auto": the fastest working H.264 encoder on THIS machine (HW-first), then libx264.
        # Resolved here (auto_h264 is a sentinel, not a real ffmpeg profile) so an explicitly-chosen
        # vendor profile is never altered. NVIDIA hosts resolve to premiere_h264_fast — identical to
        # the old default; AMD/Intel/Apple hosts get their working HW H.264 instead of silent CPU.
        if profile_name == 'auto_h264' or self.profiles.get(profile_name, {}).get('auto') == 'h264':
            return self._usable_chain(
                ['premiere_h264_fast', 'amf_h264', 'qsv_h264', 'vt_h264', 'premiere_h264_cpu'])
        if profile_name not in self.profiles:
            logger.warning(f"Unknown profile '{profile_name}', defaulting to premiere_h264_fast")
            profile_name = "premiere_h264_fast"
        chain = [profile_name]
        prof = self.profiles.get(profile_name, {})
        vc = prof.get('video_codec', '')

        # CUDA-surfaces expert profile -> a profile with the SAME video_codec but surfaces off.
        # Look it up by codec rather than string-munging the name (brittle: assumes a '_cuda' suffix
        # and that the non-surfaces twin is named identically minus '_cuda'). Fall back to the old
        # name-replace behavior if no matching surfaces-off profile exists.
        if prof.get('use_cuda_surfaces'):
            non_surface = next(
                (n for n, pf in self.profiles.items()
                 if n != profile_name
                 and pf.get('video_codec') == vc
                 and not pf.get('use_cuda_surfaces')),
                None)
            chain.append(non_surface or profile_name.replace('_cuda', ''))
        # AV1/HEVC HW -> H.264 HW -> CPU
        if vc in ('av1_nvenc',):
            chain += ['nvenc_h265_quality', 'premiere_h264_fast']
        elif vc in ('hevc_nvenc',):
            chain.append('premiere_h264_fast')
        elif vc in ('av1_amf', 'hevc_amf'):
            chain.append('amf_h264')
        elif vc in ('av1_qsv', 'hevc_qsv'):
            chain.append('qsv_h264')
        elif vc in ('hevc_videotoolbox', 'prores_videotoolbox'):
            chain.append('vt_h264')
        # Any HW failure ultimately lands on the CPU encoder
        if any(t in vc for t in ('nvenc', '_amf', '_qsv', 'videotoolbox')):
            chain.append('premiere_h264_cpu')
        # libx265 / libaom / libsvtav1 -> libx264
        if vc in ('libx265', 'libaom-av1', 'libsvtav1'):
            chain.append('premiere_h264_cpu')

        # Keep only existing + currently-usable profiles; dedup preserving order.
        return self._usable_chain(chain)

    def _usable_chain(self, chain):
        """Filter a candidate profile chain to those that exist AND whose encoder is usable on this
        machine; dedup preserving order. libx264 (premiere_h264_cpu) is the guaranteed last-ditch floor."""
        out = []
        for name in dict.fromkeys(chain):
            if name not in self.profiles:
                continue
            enc = self.profiles[name].get('video_codec', '')
            if not self.detector.encoder_usable(enc):
                logger.info(f"[Skip] {name}: encoder '{enc}' not usable on this system")
                continue
            out.append(name)
        if not out:
            out = ['premiere_h264_cpu']  # last-ditch: libx264 is essentially always present
        return out

    def encode_with_fallback(self, input_path, output_path, profile_name="auto_h264", **kw):
        """Try the requested profile, then degrade gracefully (HW->SW) using only usable encoders.
        kw (metadata/vf_extra/overrides/ffmetadata_file) is re-applied to EVERY fallback attempt,
        since the chain rebuilds profiles by name. Returns the profile name that actually SUCCEEDED
        (so callers/history can record what really ran after degradation), or None if all failed."""
        for name in self._fallback_chain(profile_name):
            logger.info(f"[Trying] profile: {self.profiles[name]['name']}")
            cmd = self.build_ffmpeg_command(input_path, output_path, name, **kw)
            if self._run_ffmpeg(cmd):
                return name
        logger.error("[ERROR] All encoding attempts failed.")
        return None

    def mux_sidecars(self, src, dst, sub_files=None, thumb_file=None, container='mp4'):
        """Final stream-copy pass that embeds subtitle sidecars (B3) and/or a thumbnail as cover
        art (B6). Chapters/metadata already baked in survive the copy. Returns True on success.

        Container families differ: MP4/MOV/M4V (ISO-BMFF) need `mov_text` subtitles and an mjpeg
        cover; MKV needs `srt` subtitles and a png cover. YouTube thumbnails are webp, which cannot
        be stream-copied into EITHER family — so the cover is always re-encoded, never `-c copy`."""
        sub_files = list(sub_files or [])
        mp4_family = container in ('mp4', 'mov', 'm4v')
        # Audio-only sources (e.g. --quality audio + --embed-thumbnail) have NO real video stream:
        # `-map 0:V` then matches nothing and ffmpeg aborts ("Stream map '' matches no streams"),
        # silently no-op'ing the embed. Detect a real (non-cover) video stream and branch below.
        has_video = self._has_real_video(src)
        cmd = [self.ffmpeg, '-hide_banner', '-y', '-i', str(src)]
        for s in sub_files:
            cmd.extend(['-i', str(s)])
        thumb_idx = None
        if thumb_file:
            thumb_idx = 1 + len(sub_files)
            cmd.extend(['-i', str(thumb_file)])
        # streams from the media. Capital V (-map 0:V) takes only "real" video streams and EXCLUDES
        # attached pictures (an existing embedded cover), so a pre-existing cover can't collide with
        # the thumbnail we add below as v:1 (-disposition:v:1 attached_pic). For audio-only sources
        # there is no such stream, so map only the audio and let the thumbnail become the sole video.
        if has_video:
            cmd.extend(['-map', '0:V', '-map', '0:a?'])
        else:
            cmd.extend(['-map', '0:a?'])
        for i in range(len(sub_files)):
            cmd.extend(['-map', str(i + 1)])
        # explicitly carry global tags + chapters from the source through this copy pass (the
        # docstring promises chapters survive; don't rely on ffmpeg's format-dependent default).
        cmd.extend(['-map_metadata', '0', '-map_chapters', '0', '-c', 'copy'])
        if sub_files:
            cmd.extend(['-c:s', 'mov_text' if mp4_family else 'srt'])
        if thumb_idx is not None:
            # Re-encode the (webp) thumbnail to a cover the container accepts: mjpeg for MP4-family,
            # png for MKV. Stream-copying webp fails ("codec webp is not supported by this format").
            cover_codec = 'mjpeg' if mp4_family else 'png'
            if has_video:
                # Real video is output v:0; the cover is the SECOND video stream (v:1).
                cmd.extend(['-map', str(thumb_idx), '-c:v:1', cover_codec, '-disposition:v:1', 'attached_pic'])
            else:
                # No real video: the cover IS the only video stream (v:0), so index the codec/
                # disposition at :v:0 — a `:v:1` specifier here would target a nonexistent stream.
                cmd.extend(['-map', str(thumb_idx), '-c:v:0', cover_codec, '-disposition:v:0', 'attached_pic'])
        if mp4_family:
            cmd.extend(['-movflags', '+faststart'])
        cmd.append(str(dst))
        return self._run_ffmpeg(cmd)

    def _run_ffmpeg(self, cmd):
        operation = "mux" if any(
            str(cmd[index]).startswith("-c") and str(cmd[index + 1]) == "copy"
            for index in range(len(cmd) - 1)
        ) else "encode"
        input_count = sum(1 for token in cmd if token == "-i")
        output_name = str(cmd[-1]).replace("\\", "/").rsplit("/", 1)[-1] if cmd else "unknown"
        logger.info(
            f"[FFMPEG] operation={operation} inputs={input_count} output={output_name}"
        )
        start = time.time()
        process = None
        FFmpegProcessor._begin_ffmpeg_activity()
        try:
            # encoding='utf-8', errors='replace': ffmpeg echoes the (often non-ASCII) input/output
            # filename back in its 'Input #0 ...' banner and other stderr diagnostics. Decoding with
            # the locale default (cp1252 on Windows) raises UnicodeDecodeError mid-iteration on the
            # first non-decodable byte, which would skip process.wait() and orphan the ffmpeg child
            # blocked on a full, undrained stderr pipe. Mirror the fix already used for the logging
            # stream handler (see _stream_handler.stream.reconfigure at the top of this module).
            # _ffmpeg_child_group_kw: own group/session on POSIX so the tree kills below can never
            # killpg the app's own group (they SIGKILLed the whole server before this).
            process = subprocess.Popen(cmd, **win_no_window_kw(), **_ffmpeg_child_group_kw(),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       encoding='utf-8', errors='replace', bufsize=1)
            # Bounded ring buffer: only the last few stderr lines are ever used (for the error tail),
            # so don't accumulate the whole encode's stderr (1-2 MB on multi-hour jobs) in a list.
            stderr_lines = deque(maxlen=10)
            drain_exc = []

            def _drain(_pipe=process.stderr):
                # Read stderr in a dedicated thread so the cancel poll below never has to WAIT for
                # ffmpeg to emit a line. The old inline `for line in process.stderr` only checked
                # cancel when a line arrived, so a SILENT phase (startup/probe, or the mp4 +faststart
                # moov relocation at the tail) stayed uncancellable until ffmpeg spoke again. The live
                # progress print stays here, driven by the same lines. A read error is captured and
                # re-raised on the main thread below, preserving the old "stderr read failure fails the
                # encode" behaviour (unreachable with errors='replace', but kept intact).
                try:
                    for line in _pipe:
                        stderr_lines.append(line)
                        if 'time=' in line:
                            m = re.search(r'time=(\d+:\d+:\d+\.\d+)', line)
                            if m:
                                print(f"\r⏱️  {m.group(1)} (elapsed {time.time()-start:.1f}s)",
                                      end='', flush=True)  # keep progress print
                except BaseException as _e:            # noqa: BLE001 - re-raised on the main thread
                    drain_exc.append(_e)

            reader = threading.Thread(target=_drain, daemon=True)
            reader.start()
            try:
                while True:
                    reader.join(0.3)
                    if not reader.is_alive():
                        break                       # ffmpeg closed stderr -> encode finished
                    # Cooperative cancel, polled every 0.3s REGARDLESS of ffmpeg output so it fires
                    # even mid-silence; the probe self-throttles to 0.5s. Only active when a
                    # cancel_check is installed (web/CLI job) AND a cancel was requested — the default
                    # (None) path just waits for stderr to close, exactly as before.
                    if _cancel_pending(getattr(self, 'cancel_check', None)):
                        # TREE kill, not process.kill(): ffmpeg is frequently a WRAPPER on Windows
                        # (the chocolatey/scoop/winget shim launches the real encoder as a child), so
                        # killing the direct child alone leaves the real ffmpeg running — observed
                        # live as orphaned encoders in Win32_Process after a cancel. Same hazard the
                        # turbo path already handles; see _kill_process_tree.
                        _kill_process_tree(process)
                        raise DownloadCancelled('YT_FINAL_CANCEL')
                if drain_exc:
                    raise drain_exc[0]              # stderr read failed -> fail like the old inline read
                print()  # newline after any \r progress prints (prevents overwrite on the next logger)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    _kill_process_tree(process)         # wrapper-safe (see the cancel branch above)
                    process.wait()
            finally:
                # If anything escaped the loop before wait() (a cancel kill, a stray error), force-kill
                # and reap the child so it can never linger blocked on a full stderr pipe nobody drains.
                if process.poll() is None:
                    _kill_process_tree(process)         # wrapper-safe (see the cancel branch above)
                    process.wait()
            success = process.returncode == 0
            if success:
                logger.info(f"[OK] Encode done in {time.time()-start:.1f}s")
            else:
                logger.error(f"[ERROR] Encode failed rc={process.returncode}")
                if stderr_lines:
                    logger.error("Last stderr: " + ''.join(list(stderr_lines)[-5:]).strip())
            return success
        except DownloadCancelled:
            # A cancel is NOT an encode failure — it must propagate to the runner (which records
            # 'cancelled'), never be swallowed into a `return False` by the generic handler below.
            if process is not None and process.poll() is None:
                try:
                    _kill_process_tree(process)          # wrapper-safe (see the cancel branch above)
                    process.wait()
                except Exception:
                    pass
            raise
        except Exception as e:
            logger.error(f"FFmpeg error: {e}")
            if process is not None and process.poll() is None:
                try:
                    _kill_process_tree(process)          # wrapper-safe (see the cancel branch above)
                    process.wait()
                except Exception:
                    pass
            return False
        finally:
            FFmpegProcessor._end_ffmpeg_activity()

    def fast_copy(self, input_path, output_path, **kw):
        """Fast remux / copy - delegates to build for consistency (dedup cmd logic).
        Accepts the K3 keyword channels (metadata/ffmetadata_file/overrides) so a remux can
        still inject chapters + tags without re-encoding."""
        cmd = self.build_ffmpeg_command(input_path, output_path, 'copy_fast', **kw)
        return self._run_ffmpeg(cmd)

    # ------------------------------------------------------------------ #
    # K3 support helpers: ffmetadata, loudness, HDR color (consumed by the engine)
    # ------------------------------------------------------------------ #
    @property
    def ffprobe(self):
        """Return only a real absolute ffprobe sibling of trusted FFmpeg."""
        try:
            ffmpeg_path = Path(self.ffmpeg)
        except (TypeError, ValueError):
            return None
        if not ffmpeg_path.is_absolute() or not ffmpeg_path.is_file():
            return None
        name = ffmpeg_path.name.lower()
        if name == 'ffmpeg.exe':
            candidate = ffmpeg_path.with_name('ffprobe.exe')
        elif name == 'ffmpeg':
            candidate = ffmpeg_path.with_name('ffprobe')
        else:
            return None
        try:
            return str(candidate) if candidate.is_file() else None
        except OSError:
            return None

    @staticmethod
    def _esc_ffmeta(val):
        """Escape a value for an ffmetadata file (= ; # \\ and newlines must be backslash-escaped)."""
        s = str(val)
        for ch in ('\\', '=', ';', '#'):
            s = s.replace(ch, '\\' + ch)
        return s.replace('\r\n', '\n').replace('\n', '\\\n')

    def write_ffmetadata(self, dst_path, tags=None, chapters=None):
        """Write an FFMETADATA1 file (global tags + [CHAPTER] blocks). Returns the path, or
        None if there was nothing to write. Chapters: list of {start, end, title} in seconds."""
        tags = {k: v for k, v in (tags or {}).items() if v not in (None, '')}
        chapters = chapters or []
        if not tags and not chapters:
            return None
        lines = [';FFMETADATA1']
        for k, v in tags.items():
            lines.append(f"{self._esc_ffmeta(k)}={self._esc_ffmeta(v)}")
        for ch in chapters:
            try:
                start = float(ch.get('start_time', ch.get('start', 0)) or 0)
                end = ch.get('end_time', ch.get('end'))
                end = float(end) if end is not None else None
            except (TypeError, ValueError):
                continue
            lines.append('[CHAPTER]')
            lines.append('TIMEBASE=1/1000')
            lines.append(f'START={int(start * 1000)}')
            if end is not None and end > start:
                lines.append(f'END={int(end * 1000)}')
            title = ch.get('title') or ch.get('name')
            if title:
                lines.append(f'title={self._esc_ffmeta(title)}')
        try:
            # newline='' is load-bearing: _esc_ffmeta escapes an embedded newline as a trailing
            # backslash + LF continuation, and text-mode translation turns that LF into CRLF so the
            # backslash escapes the CR instead — ffmpeg then TRUNCATES the tag at that point
            # ("line one\r"). Byte-faithful keeps the continuation intact.
            Path(dst_path).write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='')
            return str(dst_path)
        except OSError as e:
            logger.warning(f"[Meta] Could not write ffmetadata file: {e}")
            return None

    def probe_color(self, src):
        """Return {primaries, transfer, space, range, pix_fmt} for the first video stream (or {})."""
        ffprobe = self.ffprobe
        if not ffprobe:
            return {}
        try:
            cmd = [ffprobe, '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries',
                   'stream=color_primaries,color_transfer,color_space,color_range,pix_fmt',
                   '-of', 'default=noprint_wrappers=1:nokey=0', str(src)]
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 **win_no_window_kw(), timeout=15)
            out = {}
            for line in res.stdout.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    out[k.strip()] = v.strip()
            return out
        except Exception:
            return {}

    def probe_integrity(self, src):
        """Format-level probe for --verify-strict: returns
        {'duration': float|None, 'has_video': bool, 'has_audio': bool, 'size': int} or {} on failure.
        File size comes from stat() (not trusted from ffprobe). Never raises."""
        import json as _json
        ffprobe = self.ffprobe
        if not ffprobe:
            return {}
        try:
            cmd = [ffprobe, '-v', 'error',
                   '-show_entries', 'format=duration',
                   '-show_entries', 'stream=codec_type',
                   '-of', 'json', str(src)]
            res = subprocess.run(cmd, capture_output=True, text=True, **win_no_window_kw(),
                                 encoding='utf-8', errors='replace', timeout=15)
            data = _json.loads(res.stdout or '{}')
            dur = None
            try:
                dur = float(data.get('format', {}).get('duration'))
            except (TypeError, ValueError):
                dur = None
            streams = data.get('streams') or []
            has_video = any((s or {}).get('codec_type') == 'video' for s in streams)
            has_audio = any((s or {}).get('codec_type') == 'audio' for s in streams)
            try:
                size = Path(src).stat().st_size
            except OSError:
                size = 0
            return {'duration': dur, 'has_video': has_video, 'has_audio': has_audio, 'size': size}
        except Exception:
            return {}

    def _has_real_video(self, src):
        """True if `src` carries a decodable, non-cover video stream (an attached_pic/cover image
        does NOT count). Used by mux_sidecars to decide whether it can `-map 0:V`. Returns True on
        probe failure: preserving a real video stream is the safe default (dropping it would be
        catastrophic), and audio-only detection failing merely reverts to prior behaviour."""
        import json as _json
        ffprobe = self.ffprobe
        if not ffprobe:
            return True
        try:
            cmd = [ffprobe, '-v', 'error', '-select_streams', 'v',
                   '-show_entries', 'stream=codec_type:stream_disposition=attached_pic',
                   '-of', 'json', str(src)]
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 **win_no_window_kw(), timeout=15)
            if res.returncode != 0:
                return True   # probe FAILED (missing file / ffprobe error) -> assume real video
            data = _json.loads(res.stdout or '{}')
            streams = data.get('streams') or []
            for s in streams:
                if not (s.get('disposition') or {}).get('attached_pic'):
                    return True   # at least one non-cover video stream present
            return False          # probe succeeded with only cover art / no video -> audio-only
        except Exception:
            return True

    def probe_fps(self, src):
        """Return the exact frame rate (float) of the first video stream, recovered from
        ffprobe's rational r_frame_rate (e.g. '30000/1001' -> 29.97), or None on failure.
        Recovers NTSC fractional rates that yt-dlp reports as rounded floats."""
        ffprobe = self.ffprobe
        if not ffprobe:
            return None
        try:
            cmd = [ffprobe, '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries', 'stream=r_frame_rate',
                   '-of', 'default=noprint_wrappers=1:nokey=1', str(src)]
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 **win_no_window_kw(), timeout=15)
            val = (res.stdout or '').strip().splitlines()[0].strip()
            if '/' in val:
                n, d = val.split('/', 1)
                d = float(d)
                return float(n) / d if d else None
            return float(val) if val else None
        except Exception:
            return None

    def hdr_color_args(self, src):
        """If `src` is HDR (PQ/HLG or BT.2020), return the -color_* flags that carry that
        signalling through a re-encode (A2). Empty list for SDR or on probe failure."""
        c = self.probe_color(src)
        transfer = (c.get('color_transfer') or '').lower()
        primaries = (c.get('color_primaries') or '').lower()
        is_hdr = transfer in ('smpte2084', 'arib-std-b67') or primaries == 'bt2020'
        if not is_hdr:
            return []
        args = []
        if c.get('color_primaries') and c['color_primaries'] != 'unknown':
            args += ['-color_primaries', c['color_primaries']]
        if c.get('color_transfer') and c['color_transfer'] != 'unknown':
            args += ['-color_trc', c['color_transfer']]
        if c.get('color_space') and c['color_space'] != 'unknown':
            args += ['-colorspace', c['color_space']]
        if c.get('color_range') and c['color_range'] != 'unknown':
            args += ['-color_range', c['color_range']]
        if args:
            logger.info(f"[HDR] Carrying color metadata through re-encode: {' '.join(args)}")
        return args

    def sdr_color_args(self, src, family=None):
        """Guarded Rec.709 tagging. Returns -color_* flags ONLY when the source's
        SDR color tags are MISSING or 'unknown'. Never overrides a real tag, and
        returns [] for HDR sources (those are handled by hdr_color_args).

        Scope note: this is applied on the RE-ENCODE path only. Copy/remux is never
        tagged -- h264_metadata/hevc_metadata bitstream filters are codec-specific and
        fragile across the NVENC/AMF/QSV/copy matrix, and container-level color metadata
        via copy is not reliably honored by NLEs. `family` is accepted for forward-safety
        but Rec.709 flags are identical for x26x/nvenc/prores/dnxhd, so no branching is
        needed. For ProRes/DNxHD FFmpeg writes the MOV 'colr' atom from these same flags;
        -color_range is intentionally omitted to avoid full/limited mismatches."""
        c = self.probe_color(src)
        transfer = (c.get('color_transfer') or '').lower()
        primaries = (c.get('color_primaries') or '').lower()
        is_hdr = transfer in ('smpte2084', 'arib-std-b67') or primaries == 'bt2020'
        if is_hdr:
            return []
        missing = lambda v: (not v) or v.lower() == 'unknown'
        args = []
        if missing(c.get('color_primaries')):
            args += ['-color_primaries', 'bt709']
        if missing(c.get('color_transfer')):
            args += ['-color_trc', 'bt709']
        if missing(c.get('color_space')):
            args += ['-colorspace', 'bt709']
        if args:
            logger.info(f"[Color] Tagging untagged SDR as Rec.709: {' '.join(args)}")
        return args

    def make_proxy(self, src, dst):
        """Half-resolution H.264 editing proxy (fast) alongside the master. NVENC if usable, else libx264.
        Editors cut on the lightweight proxy and relink to the master ('Attach Proxies' in Premiere)."""
        nvenc = self.detector.encoder_usable('h264_nvenc')
        cmd = [self.ffmpeg, '-hide_banner', '-y', '-i', str(src),
               '-vf', 'scale=trunc(iw/4)*2:trunc(ih/4)*2',   # half res, even dimensions
               '-c:v', 'h264_nvenc' if nvenc else 'libx264']
        if nvenc:
            cmd += ['-preset', 'p4', '-rc', 'vbr', '-cq', '28', '-b:v', '0']
        else:
            cmd += ['-preset', 'veryfast', '-crf', '26']
        cmd += ['-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(dst)]
        return self._run_ffmpeg(cmd)

    def normalize_audio(self, src, dst, target_lufs=-14.0, tp=-1.5, lra=11.0,
                        audio_codec='aac', audio_bitrate='320k'):
        """Two-pass EBU R128 loudness normalization (A4). Pass 1 measures, pass 2 applies the
        measured values with linear gain. Video is stream-copied. Returns True on success."""
        import json as _json
        try:
            # Measure ONLY the first audio stream (-map 0:a:0). On a multi-audio source the
            # default -af would mix/measure all of them; -map 0:a:0 pins the measurement to the
            # primary track the pass-2 filter is then applied to.
            measure = [self.ffmpeg, '-hide_banner', '-i', str(src),
                       '-map', '0:a:0', '-af',
                       f'loudnorm=I={target_lufs}:TP={tp}:LRA={lra}:print_format=json',
                       '-f', 'null', '-']
            FFmpegProcessor._begin_ffmpeg_activity()
            proc = None
            try:
                # Popen + drain-thread (not subprocess.run) so a cooperative cancel can interrupt the
                # pass-1 measurement, which reads the WHOLE audio stream. Before this, a cancel during
                # loudnorm analysis was ignored until it finished on its own — up to the 900s ceiling
                # on a long stream (the pass-2 apply already goes through _run_ffmpeg, which is
                # cancel-aware). A background reader drains stderr (no pipe-full deadlock) while the
                # main loop polls every 0.3s, so BOTH the cancel and the 900s deadline are honoured
                # even if ffmpeg emits nothing. ffmpeg always emits UTF-8; strict ANSI-codepage decode
                # here made loudnorm silently fail on accented titles. The default (no cancel_check)
                # path behaves exactly like the old blocking run: it just waits for stderr to close.
                proc = subprocess.Popen(measure, **win_no_window_kw(),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        encoding='utf-8', errors='replace', bufsize=1)
                parts = []

                def _drain(_pipe=proc.stderr, _acc=parts):
                    try:
                        for line in _pipe:
                            _acc.append(line)
                    except Exception:
                        pass
                reader = threading.Thread(target=_drain, daemon=True)
                reader.start()
                deadline = time.monotonic() + 900        # monotonic: immune to wall-clock jumps
                while True:
                    reader.join(0.3)
                    if not reader.is_alive():
                        break                       # ffmpeg closed stderr -> measurement finished
                    if _cancel_pending(getattr(self, 'cancel_check', None)):
                        proc.kill()
                        raise DownloadCancelled('YT_FINAL_CANCEL')
                    if time.monotonic() > deadline:
                        proc.kill()
                        logger.error("[Loudnorm] pass-1 exceeded 900s; aborting")
                        return False
                # Bounded wait: the reader ending normally means ffmpeg is already exiting (EOF), so
                # this returns at once. It only actually blocks in the never-observed case where the
                # drain thread died on a read error while ffmpeg keeps writing — cap it so nobody-draining
                # can't wedge the finalize thread on a full stderr pipe.
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                rc = proc.returncode
                stderr = ''.join(parts)
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait()
                FFmpegProcessor._end_ffmpeg_activity()
            if rc != 0:
                logger.error(f"[Loudnorm] pass-1 failed (rc={rc}): {stderr[-400:].strip()}")
                return False
            start, end = stderr.rfind('{'), stderr.rfind('}')
            if start == -1 or end <= start:
                logger.error("[Loudnorm] pass-1 produced no JSON measurement; aborting")
                return False
            stats = _json.loads(stderr[start:end + 1])
            af = (f"loudnorm=I={target_lufs}:TP={tp}:LRA={lra}"
                  f":measured_I={stats.get('input_i', target_lufs)}"
                  f":measured_TP={stats.get('input_tp', tp)}"
                  f":measured_LRA={stats.get('input_lra', lra)}"
                  f":measured_thresh={stats.get('input_thresh', -34.0)}"
                  f":offset={stats.get('target_offset', 0.0)}:linear=true:print_format=summary")
            # -map 0 + metadata/chapters passthrough keeps every stream, chapter marker and tag.
            # The measured values are for a:0, so normalize ONLY a:0: copy all audio, then override
            # a:0 to the target codec (`-c:a copy` + `-c:a:0 <codec>`). This avoids needlessly
            # re-encoding/downgrading secondary audio tracks (e.g. a 5.1 AC-3 commentary).
            apply_cmd = [self.ffmpeg, '-hide_banner', '-y', '-i', str(src),
                         '-map', '0', '-map_metadata', '0', '-map_chapters', '0',
                         '-filter:a:0', af, '-c:v', 'copy', '-c:a', 'copy', '-c:a:0', audio_codec]
            if not str(audio_codec).startswith('pcm') and audio_codec not in ('flac', 'alac'):
                apply_cmd += ['-b:a:0', audio_bitrate]
            if str(dst).lower().endswith('.mp4'):
                apply_cmd += ['-movflags', '+faststart']
            apply_cmd.append(str(dst))
            logger.info(f"[Loudnorm] Normalizing to {target_lufs} LUFS (measured I={stats.get('input_i','?')})")
            return self._run_ffmpeg(apply_cmd)
        except DownloadCancelled:
            # A cancel during pass-1 is NOT a loudnorm failure — propagate it to the runner (which
            # records 'cancelled') instead of letting the generic handler swallow it into return False.
            raise
        except Exception as e:
            logger.error(f"[Loudnorm] failed: {e}")
            return False


# =============================================================================
# 4. YOUTUBE DOWNLOADER (merged best extraction + parallel from 4.py + 2.4)
# =============================================================================
class YouTubeDownloader:
    # Client orders — empirically validated 2026-06-23 against yt-dlp 2026.06.09:
    #
    # EMPIRICAL RESULTS (bgutil HTTP provider at :4416, video 4YrzJ9RZ9qY):
    #   android     -> SABR-only (missing URL warning); PO token accepted but SABR forced
    #   ios         -> 403; needs GVS PO token (not issued by bgutil http provider)
    #   tv          -> DRM experiment active; formats unusable
    #   web         -> SABR-only (missing URL warning); PO token accepted but SABR forced
    #   web_safari  -> HLS m3u8 only (itag 96); NO direct DASH; throttled at ~3-8 MiB/s
    #   mweb        -> DIRECT HTTPS DASH (itag 137/248/399); PO token from bgutil; FAST PATH
    #   android_vr  -> DIRECT HTTPS DASH (itag 137/248/399); NO PO token needed; FAST PATH
    #
    # Fast path = direct googlevideo HTTPS DASH with a solved nsig (`n=` query) so YouTube
    # does not throttle to ~1 MiB/s.
    #
    # EMPIRICAL (2026-07-20, Comcast wired, yt-dlp 2026.07.04, full 401.8 MiB EOF runs — Kimi
    # flow_optimum campaign, 17 arms / 2.3 GiB, cross-checked independently here):
    #   android_vr -> 39.38 MiB/s sustained, zero failures across ~1.9 GiB; NO PO token needed; FAST PATH.
    #                 (2026-07-19 saw android_vr WITHOUT n= at ~1.1 MiB/s; that reversed — it now returns
    #                  nsig-solved n= URLs at full rate, verified 2026-07-20 — so it leads WITH_POT too.)
    #   mweb + PO  -> comparable (~37 MiB/s) BUT fresh-URL issuance instant-403'd 2x today -> fallback.
    #                 player_skip=['configs'] stays REMOVED (it 20-MiB-walled itag 137; see _apply_pot_mode).
    #   Chunk spans >= 12 MiB trickle-feed ~0.7 MiB/s (50x collapse); 10 MiB is the sweet spot (see
    #   http_chunk_size below). Oscillation is server-side per-chunk TTFB (~50ms); pipelining neutral.
    #   No Comcast SNI shaping: desync bypass modes all within +/-13% run noise -> no-op here (bypass
    #   stays default-OFF; the Speed Test remains the detector for genuinely shaped ISPs like Verizon).
    # web_safari is a last resort: HLS m3u8 only (itag 96), throttled at ~3-8 MiB/s.
    CLIENTS_WITH_POT = ['android_vr', 'mweb', 'web_safari', 'default', '-android_sdkless']
    CLIENTS_NO_POT   = ['android_vr', 'web_safari', 'default', '-android_sdkless']

    def __init__(self, detector: CapabilityDetector, out_dir="./downloads", max_workers=8,
                 pot_base_url=None, pot_mode='auto'):
        self.detector = detector
        self.output_path = Path(out_dir)
        self.output_path.mkdir(parents=True, exist_ok=True)
        # Floor at 1 AND cap at 16: a non-positive worker count (e.g. a user-supplied
        # JobSpec.workers of 0 or -1) must degrade to a sane default, not crash
        # ThreadPoolExecutor(max_workers<=0) with a raw ValueError.
        self.max_workers = max(1, min(max_workers or 8, 16))
        # Bounded so the long-running web-server worker (which reuses ONE downloader instance for the
        # life of the process, across every channel-watch/queue job) can't accumulate unbounded
        # history entries. jobstore's HistoryLedger persists completed-job history durably, so this
        # in-memory copy only needs to be a recent-window convenience, not a full ledger.
        self.history = deque(maxlen=200)
        # K2: side-channel of distilled source info (title/uploader/chapters/...) keyed by the
        # str() of each produced output path. Consumed by the engine for metadata + chapter markers.
        # Each entry is pop()'d by the consumer (_finalize) after its single read, so this dict does
        # not grow without bound over the server's lifetime.
        self.last_info = {}
        # Optional live-progress callback (set by the web server's worker per job). When None
        # (the CLI default) the progress hook behaves exactly as before — just prints.
        self.progress_sink = None
        # Cancellation channel, deliberately SEPARATE from progress_sink. The sink is a UI callback
        # that ONLY the web worker installs, so piggybacking cancel on it left every CLI-run queue
        # job structurally unable to observe a cancel at all — `queue cancel` reported success while
        # the download ran to completion. None (the CLI default) is a no-op.
        self.cancel_check = None
        # Last actionable failure reason from download() (consumed by the engine/server on empty result).
        # Also mirrored onto a thread-local so download_parallel()'s concurrent download() calls don't
        # race on these shared fields — each worker reads back its OWN failure via _tls (see download_parallel).
        self.last_error = None
        self.last_error_kind = None
        self._tls = threading.local()
        self.bw = BandwidthMonitor()
        # bgutil PO-token provider config. auto/lazy probe only an explicitly
        # addressed HTTP endpoint; script execution requires explicit script mode.
        self.pot_base_url = pot_base_url or os.getenv('BGUTIL_POT_BASE_URL') or None
        self.pot_mode = (pot_mode or os.getenv('BGUTIL_POT_MODE') or 'auto').lower()
        self._pot_status_cache = None
        # Lazy auto-cookie manager (None until first use). Opt-in via [auth] config.
        self.cookie_manager = None
        self._cookie_manager_lock = threading.Lock()

    def _get_cookie_manager(self):
        if self.cookie_manager is None:
            with self._cookie_manager_lock:
                if self.cookie_manager is None:
                    try:
                        import cookies_auth
                        self.cookie_manager = cookies_auth.build_default_manager(
                            output_dir=str(self.output_path))
                    except Exception as e:
                        logger.debug(f"[auth] manager unavailable: {e}")
                        self.cookie_manager = False  # sentinel: tried, unavailable
        return self.cookie_manager or None

    def _pot_provider_status(self):
        """Detect a usable bgutil PO-token backend. Returns (mode, detail):
        ('http', base_url) if the explicitly addressed provider answers /ping,
        else (None, reason). Default modes never discover app-data/home scripts.
        A working provider unlocks the fast fragmented web clients (web/web_safari) so -N parallelism
        and un-throttled GVS URLs become available."""
        if self._pot_status_cache is not None:
            return self._pot_status_cache
        status = (None, 'no provider backend reachable')
        base = (self.pot_base_url or 'http://127.0.0.1:4416').rstrip('/')
        try:
            import urllib.request
            with urllib.request.urlopen(base + '/ping', timeout=2) as r:
                if getattr(r, 'status', 200) == 200:
                    status = ('http', base)
        except Exception:
            pass
        self._pot_status_cache = status
        mode, detail = status
        if mode:
            logger.info(f"[Speed] PO-token provider active ({mode}: {detail}) -> mweb client (direct DASH itag 137, fast path)")
        else:
            logger.info(
                "[Speed] No PO-token provider reachable -> android_vr client (often throttled ~1 MiB/s). "
                "The bgutil server is manual-only for this release; review upstream setup and provenance "
                "before enabling the mweb fast path."
            )
        return status

    _SCRIPT_HOME_DEFAULT = str(
        (Path.home() / 'bgutil-ytdlp-pot-provider' / 'server').resolve()
    )
    _DISABLED_URL = 'http://127.0.0.1:1'      # closed port -> http provider unavailable (instant connrefused)
    _DISABLED_SCRIPT = _unavailable_tool_path('bgutil-generate-once.js')

    def _apply_pot_mode(self, ea):
        """Configure the bgutil PO-token provider + client order into extractor_args `ea` per self.pot_mode.
        Modes: 'http' (force HTTP server), 'script' (force on-demand node script), 'off' (no tokens,
        direct-URL clients), 'auto' (use reachable HTTP only, else tokenless), and
        'lazy' (tokenless first with reachable HTTP available only for retry)."""
        mode = self.pot_mode
        if mode == 'off':
            ea['youtube']['player_client'] = list(self.CLIENTS_NO_POT)
            ea['youtubepot-bgutilhttp'] = {'base_url': [self._DISABLED_URL]}
            ea['youtubepot-bgutilscript'] = {'script_path': [self._DISABLED_SCRIPT]}
            # Nothing will serve a PO token in this mode, so tell yt-dlp not to ask. Without
            # this the bgutil HTTP plugin still pings the disabled base_url and prints
            # "Error reaching GET http://127.0.0.1:1/ping" on a perfectly normal run -- a line
            # beginning with "Error" for an intended condition, on the DEFAULT mode, which
            # reads as a broken install and invites bug reports for a non-bug.
            ea['youtube']['fetch_pot'] = ['never']
            logger.info("[Speed] PO-token mode=off -> tokenless direct-URL clients (android_vr)")
            return
        if mode == 'lazy':
            # OPT-IN speed mode (NOT the default 'auto'): extract android_vr-first WITHOUT the mweb+PO
            # player fetch — measured ~2.8s faster/extraction, and the downloaded format is android_vr's
            # in the common case (12/12 identical client+format in a paired A/B), so the PO fetch is
            # usually pure waste. A reachable HTTP provider remains wired so the retry
            # planner can escalate to the full mweb+PO client list on a structural failure (age-gate /
            # made-for-kids / no-format / storyboard degradation / 403) — coverage is preserved, the
            # cost is only paid on the videos that actually need it.
            ea['youtube']['player_client'] = list(self.CLIENTS_NO_POT)
            det_mode, detail = self._pot_provider_status()
            if det_mode == 'http':
                ea['youtubepot-bgutilhttp'] = {'base_url': [self.pot_base_url or detail]}
            else:
                ea['youtubepot-bgutilhttp'] = {'base_url': [self._DISABLED_URL]}
                # No provider to escalate to, so do not let yt-dlp ask for a token: the
                # plugin would ping the disabled base_url and print an "Error reaching"
                # line for an intended condition.
                ea['youtube']['fetch_pot'] = ['never']
            ea['youtubepot-bgutilscript'] = {'script_path': [self._DISABLED_SCRIPT]}
            logger.info("[Speed] PO-token mode=lazy -> android_vr first; PO escalates only on failure"
                        + ("" if det_mode else " (no provider detected — escalation will be a no-op)"))
            return
        if mode == 'http':
            ea['youtube']['player_client'] = list(self.CLIENTS_WITH_POT)
            # Do NOT set player_skip=['configs']. It saved ~0.5-1s/extraction, but measured 2026-07-20
            # the skip-configs mweb+PO URL class hard-403s at EXACTLY 20 MiB on itag 137 (the default
            # 1080p H.264 target) — byte-exact, reproduced independently across edges/IP families. The
            # old assumption ("clients don't need the player config for DASH URL construction") was
            # wrong: fetching the config yields URLs that stream clean past 20 MiB. Reliability on the
            # default download beats a sub-second extraction save. (Kimi edge_probe, 2026-07-20.)
            ea['youtubepot-bgutilhttp'] = {'base_url': [self.pot_base_url or 'http://127.0.0.1:4416']}
            ea['youtubepot-bgutilscript'] = {'script_path': [self._DISABLED_SCRIPT]}  # force HTTP only
            logger.info(f"[Speed] PO-token mode=http ({ea['youtubepot-bgutilhttp']['base_url'][0]})")
            return
        if mode == 'script':
            ea['youtube']['player_client'] = list(self.CLIENTS_WITH_POT)
            # No player_skip=['configs'] — see the http branch: it 403-walls itag 137 at 20 MiB.
            ea['youtubepot-bgutilhttp'] = {'base_url': [self._DISABLED_URL]}  # force script only
            home = os.path.abspath(os.path.expanduser(
                os.getenv('BGUTIL_POT_SERVER_HOME') or self._SCRIPT_HOME_DEFAULT
            ))
            ea['youtubepot-bgutilscript'] = {'server_home': [home]}
            logger.info(f"[Speed] PO-token mode=script (home={home})")
            return
        # auto
        det_mode, detail = self._pot_provider_status()
        ea['youtubepot-bgutilscript'] = {'script_path': [self._DISABLED_SCRIPT]}
        if det_mode == 'http':
            ea['youtube']['player_client'] = list(self.CLIENTS_WITH_POT)
            # No player_skip=['configs'] — see the http branch: it 403-walls itag 137 at 20 MiB.
            # Wire the detected provider. An explicit --pot-server overrides the probed base_url.
            ea['youtubepot-bgutilhttp'] = {'base_url': [self.pot_base_url or detail]}
        else:
            ea['youtube']['player_client'] = list(self.CLIENTS_NO_POT)
            ea['youtubepot-bgutilhttp'] = {'base_url': [self._DISABLED_URL]}
            # Nothing will serve a PO token in this mode, so tell yt-dlp not to ask. Without
            # this the bgutil HTTP plugin still pings the disabled base_url and prints
            # "Error reaching GET http://127.0.0.1:1/ping" on a perfectly normal run -- a line
            # beginning with "Error" for an intended condition, on the DEFAULT mode, which
            # reads as a broken install and invites bug reports for a non-bug.
            ea['youtube']['fetch_pot'] = ['never']

    def _find_aria2c(self):
        """Detect aria2c only through an explicit absolute PATH entry.

        NOTE: since yt-dlp 2026.06.09 aria2c is disabled for
        DASH/HLS (CVE-2026-50574), so it now only helps single progressive http URLs. We therefore
        never download or discover a private/home copy for the default path.
        """
        selected = explicit_path_executable(('aria2c', 'aria2c.exe'))
        if selected is None:
            return None
        path = str(selected)
        try:
            res = subprocess.run(
                [path, '--version'],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if res.returncode == 0:
                return path
        except Exception:
            pass
        return None

    def _find_n_m3u8dl(self):
        """Detect turbo only through an explicit absolute PATH entry."""
        names = (
            'N_m3u8DL-RE', 'N_m3u8DL-RE.exe',
            'n_m3u8dl-re', 'n_m3u8dl-re.exe',
            'N_m3u8DL-RE.dll',  # rarely used via dotnet
        )
        selected = explicit_path_executable(names)
        if selected is None:
            return None
        path = str(selected)
        try:
            res = subprocess.run(
                [path, '--version'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Some builds print version on stderr or use -h only.
            if (
                res.returncode == 0
                or 'N_m3u8DL' in ((res.stdout or '') + (res.stderr or ''))
            ):
                return path
        except Exception:
            try:
                res = subprocess.run(
                    [path, '-h'],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode in (0, 1):
                    return path
            except Exception:
                pass
        return None

    def resolve_engine(self, spec=None):
        """Return (effective_engine, reason, aria_path, turbo_path) for this job."""
        try:
            from jobspec import pick_download_engine
        except ImportError:
            from Final.jobspec import pick_download_engine
        requested = 'auto'
        if spec is not None:
            requested = getattr(spec, 'download_engine', None) or 'auto'
        # Cache binary probes on the instance (version checks are slow on Windows).
        if not hasattr(self, '_aria2_cache'):
            self._aria2_cache = self._find_aria2c()
        if not hasattr(self, '_turbo_cache'):
            self._turbo_cache = self._find_n_m3u8dl()
        aria = self._aria2_cache
        turbo = self._turbo_cache
        effective, reason = pick_download_engine(
            requested, aria2_available=bool(aria), turbo_available=bool(turbo))
        return effective, reason, aria, turbo

    @staticmethod
    def _apply_aria2_opts(opts, aria_path, ydl=None):
        """Register aria2 as external downloader on progressive HTTP(S).

        Mutates `opts` and, when provided, the live YoutubeDL.params (required because
        hybrid decides only *after* YoutubeDL is constructed — mutating opts alone is not enough).
        """
        args = ['-x', '16', '-s', '16', '-k', '1M', '--file-allocation=none']
        ed = {'default': aria_path, 'http': aria_path, 'https': aria_path}
        eda = {'default': list(args), 'http': list(args), 'https': list(args)}
        opts['external_downloader'] = ed
        opts['external_downloader_args'] = eda
        if ydl is not None and getattr(ydl, 'params', None) is not None:
            ydl.params['external_downloader'] = ed
            ydl.params['external_downloader_args'] = eda

    @staticmethod
    def _clear_aria2_opts(opts, ydl=None):
        """Remove external downloader so a retry cannot stick on aria2 for dual-DASH."""
        opts.pop('external_downloader', None)
        opts.pop('external_downloader_args', None)
        if ydl is not None and getattr(ydl, 'params', None) is not None:
            ydl.params.pop('external_downloader', None)
            ydl.params.pop('external_downloader_args', None)

    @staticmethod
    def _copy_engine_retry_state(new_opts, old_opts):
        """Copy only per-download engine state across an options rebuild.

        A failed optional engine pins this download to built-in. Cookie/auth
        recovery rebuilds the full yt-dlp options dictionary, so the pin must be
        deliberately restored or Hybrid can select the same failed tool again.
        """
        for key in ('_engine_policy', '_engine_aria', '_engine_turbo',
                    '_engine_builtin_pinned'):
            if key in old_opts:
                new_opts[key] = old_opts[key]
        if old_opts.get('_engine_builtin_pinned'):
            new_opts['_engine_builtin_pinned'] = True
            new_opts['_engine_policy'] = 'builtin'
            new_opts['_engine_aria'] = None
            new_opts['_engine_turbo'] = None
        return new_opts

    def _aria2_compatible(self, spec, info, opts=None) -> tuple:
        """Whether progressive aria2 is safe for this job. Returns (ok, reason_if_not).

        Section/download_ranges + external downloaders are a known mismatch (ranges are
        applied by yt-dlp's native path). Live streams also stay native.
        """
        if opts is not None and opts.get('download_ranges'):
            return False, 'section clip requires built-in'
        if spec is not None and getattr(spec, 'section', None):
            return False, 'section clip requires built-in'
        if self._info_is_live(info):
            return False, 'live stream requires built-in'
        return True, ''

    @staticmethod
    def _info_is_live(info) -> bool:
        if not info:
            return False
        if info.get('is_live') is True:
            return True
        status = str(info.get('live_status') or '').lower()
        return status in ('is_live', 'is_upcoming')

    def _hybrid_pick_after_extract(self, info, spec, opts, ydl=None, *,
                                   policy=None, aria=_ENGINE_UNSET, turbo=_ENGINE_UNSET):
        """After extract_info, choose builtin / turbo / aria2 for this resolved format.

        Returns (fetch_path, turbo_bin) where fetch_path is one of:
          'builtin' | 'turbo' | 'aria2'
        and turbo_bin is the executable path only when fetch_path == 'turbo'.

        policy/aria/turbo should be passed from the current download() attempt so
        parallel jobs on the same instance cannot clobber each other's pick mid-flight.
        Always clears aria2 opts unless the returned path is aria2 (prevents sticky state).
        """
        try:
            from jobspec import classify_stream_kind, decide_download_engine
        except ImportError:
            from Final.jobspec import classify_stream_kind, decide_download_engine
        kind = classify_stream_kind(info)
        if aria is _ENGINE_UNSET:
            aria = getattr(self, '_aria_bin_for_job', None)
        if turbo is _ENGINE_UNSET:
            turbo = getattr(self, '_turbo_bin_available', None) or getattr(self, '_turbo_bin_for_job', None)
        if policy is None:
            policy = getattr(self, '_last_engine', 'builtin')
        aria2_ok, aria2_reason = self._aria2_compatible(spec, info, opts=opts)
        turbo_ok, turbo_reason = self._turbo_compatible(spec, info, opts=opts)
        decision = decide_download_engine(
            policy, kind,
            aria_path=aria,
            turbo_path=turbo,
            aria2_ok=aria2_ok,
            aria2_reason=aria2_reason,
            turbo_ok=turbo_ok,
            turbo_reason=turbo_reason,
            builtin_pinned=bool(opts.get('_engine_builtin_pinned')),
        )

        # Surface the pick + reason for the WebUI (actual engine used ≠ policy "hybrid").
        self._last_fetch_path = decision.path
        self._last_fetch_reason = decision.reason

        # Selection is pure; mutate the live yt-dlp configuration only after the
        # decision is final. Every non-aria2 result clears possible sticky state.
        if decision.path == 'aria2':
            self._apply_aria2_opts(opts, decision.executable, ydl)
            self._turbo_bin_for_job = None
            logger.info(f"[Engine] {decision.reason} ({decision.executable})")
            return 'aria2', None

        self._clear_aria2_opts(opts, ydl)
        if decision.path == 'turbo':
            self._turbo_bin_for_job = decision.executable
            logger.info(f"[Engine] {decision.reason} ({decision.executable})")
            return 'turbo', decision.executable

        self._turbo_bin_for_job = None
        logger.info(f"[Engine] {decision.reason}")
        return 'builtin', None

    def _detect_js_runtimes(self):
        """Detect installed JS runtimes (deno/node/bun) for yt-dlp's EJS nsig solver.

        yt-dlp only enables 'deno' by default; if deno is absent but node/bun exist we must enable them
        explicitly via the js_runtimes option, otherwise nsig/signature solving fails ("Some formats may
        be missing") and YouTube serves throttled / 403 formats.

        Managed pointer validation is intentionally repeated for every options
        build. A cached app-data path would outlive the authorization that
        selected it and could survive a replacement between jobs.
        """
        rts = {}
        versions = {}
        # R1: minimum versions the EJS solver needs to stay reliable in 2026.
        min_major = {'deno': 2, 'node': 22}
        for rt in ('deno', 'node', 'bun'):
            executable = _js_runtime_execution_path(rt)
            if executable:
                # yt-dlp's runtime classes consume this exact schema and pass
                # the configured executable path to the selected runtime.
                rts[rt] = {'path': executable}
                versions[rt] = self._runtime_version(executable)
                mj = versions[rt][0] if versions[rt] else None
                if mj is not None and rt in min_major and mj < min_major[rt]:
                    logger.warning(f"[Speed] {rt} v{'.'.join(map(str, versions[rt]))} is older than the "
                                   f"recommended {rt} {min_major[rt]}+; nsig solving may be unreliable. Update {rt}.")
        if not rts and not getattr(self, '_js_runtime_warned', False):
            logger.warning("[Speed] No JS runtime (deno/node/bun) found -> YouTube nsig throttling can't be "
                           "solved and many formats will be missing/slow. Install verified managed "
                           "Node or Deno; Python changes require a future reviewed yt_final release.")
            self._js_runtime_warned = True
        elif rts and not getattr(self, '_js_runtime_logged', False):
            shown = ', '.join(f"{k} {'.'.join(map(str, versions[k]))}" if versions.get(k) else k for k in rts)
            logger.info(f"[Speed] JS runtime(s) for nsig solving: {shown} (EJS solver)")
            self._js_runtime_logged = True
        return rts

    @staticmethod
    def _runtime_version(rt):
        """Return (major, minor, patch) for a JS runtime, or None if it can't be parsed."""
        try:
            res = subprocess.run([rt, '--version'], capture_output=True, text=True, timeout=4)
            m = re.search(r'(\d+)\.(\d+)\.(\d+)', (res.stdout or '') + (res.stderr or ''))
            if m:
                return tuple(int(x) for x in m.groups())
        except Exception:
            pass
        return None

    def get_api14_opts(self):
        # Base headers + modern client list for better availability + less throttling on some CDNs
        return {
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            },
            'extractor_args': {
                'youtube': {
                    # Default (no PO-token provider) order: PO-token-free direct-URL clients first.
                    # get_optimized_ydl_opts() upgrades this to CLIENTS_WITH_POT when a provider is detected.
                    'player_client': list(self.CLIENTS_NO_POT),
                }
            },
        }

    def _build_format_selector(self, quality, codec=None):
        """Build a robust yt-dlp format string. Defaults to a *smart 1080p H.264* pick that is
        ~3x smaller than 4K VP9 and remains a true stream-copy for Premiere/editing.
        codec: None/'h264'/'avc'  -> prefer avc1 (mp4, edit-friendly, smaller)
               'vp9'              -> prefer vp9
               'av1'/'av01'       -> prefer av1 (smallest at a given res)
               'any'/'best'       -> no codec preference (largest/best available)
        """
        caps = {"4k": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480}
        if quality == "audio":
            return "bestaudio[ext=m4a]/bestaudio/best"

        codec = (codec or "h264").lower()
        # height cap clause; "best" means uncapped
        h = caps.get(quality)
        cap = f"[height<={h}]" if h else ""

        if codec in ("h264", "avc", "avc1"):
            # avc1 tops out at 1080p on YouTube; for higher caps it gracefully falls through.
            return (
                f"bestvideo{cap}[vcodec^=avc1]+bestaudio[ext=m4a]/"
                f"bestvideo{cap}[vcodec^=avc]+bestaudio/"
                f"bestvideo{cap}+bestaudio/best{cap}/best"
            )
        if codec in ("av1", "av01"):
            return (
                f"bestvideo{cap}[vcodec^=av01]+bestaudio/"
                f"bestvideo{cap}+bestaudio/best{cap}/best"
            )
        if codec == "vp9":
            return (
                f"bestvideo{cap}[vcodec^=vp9]+bestaudio/"
                f"bestvideo{cap}+bestaudio/best{cap}/best"
            )
        # any / best -> largest available
        return f"bestvideo{cap}+bestaudio/best{cap}/best"

    def get_optimized_ydl_opts(self, quality="1080p", audio_only=False, cookies_from_browser=None,
                               cookiefile=None, codec=None, section=None, slow_requests=False, source_address=None,
                               spec=None):
        # yt_dlp import is lazy only inside download(); here no reference needed
        base = self.get_api14_opts()

        # PO-token provider wiring. A working provider unlocks the fast fragmented web clients
        # (web/web_safari) so -N parallelizes and GVS URLs are un-throttled. --pot-mode switches backends.
        self._apply_pot_mode(base['extractor_args'])

        fmt = self._build_format_selector(quality, codec)

        # format_sort: bias toward the requested codec so the right variant wins among ties.
        codec_l = (codec or "h264").lower()
        vcodec_pref = {'h264': 'vcodec:h264', 'avc': 'vcodec:h264', 'avc1': 'vcodec:h264',
                       'av1': 'vcodec:av01', 'av01': 'vcodec:av01', 'vp9': 'vcodec:vp9'}.get(codec_l)
        sort = ['res']
        if vcodec_pref:
            sort.append(vcodec_pref)
        sort += ['acodec:aac', 'ext:mp4', 'fps']

        # -N (concurrent_fragment_downloads) only parallelizes GENUINELY fragmented formats — HLS m3u8
        # or http_dash_segments (FragmentFD). The tool's primary mweb+PO path returns a SINGLE-URL
        # https DASH stream, which yt-dlp fetches over ONE connection (HttpFD) where -N is inert; that
        # path's speed comes from the un-throttled nsig-solved GVS URL, not from parallelism. We still
        # set a high -N so the fragmented fallbacks (web_safari HLS, progressive) parallelize — it is a
        # harmless no-op on the single-URL fast path. (aria2c/curl/wget were disabled for DASH/HLS in
        # yt-dlp 2026.06.09, CVE-2026-50574, so external multi-conn splitters aren't an option anyway.)
        # yt-dlp internally caps concurrency at what the server allows.
        concurrent = 32 if quality in ("best", "4k", "1440p", "1080p") else 16
        # http_chunk_size: for DASH (android/tv client path) each fragment is already a
        # discrete request — sub-chunking just adds round-trips. Use a large value (10 MB)
        # so yt-dlp only sub-chunks when a single fragment exceeds that size (rare for 1080p).
        # For progressive http (format 18/22 fallback) a 10 MB chunk is also fine.
        chunk_size = 10 * 1024 * 1024  # 10 MiB — disable sub-chunking on typical DASH frags
        ffmpeg_location = _trusted_absolute_tool(
            getattr(self.detector, 'ffmpeg_path', None),
            unavailable_name='ffmpeg',
        )

        opts = {
            **base,
            'format': fmt,
            'outtmpl': str(self.output_path / '%(title)s [%(id)s].%(ext)s'),
            'merge_output_format': 'mp4',
            'concurrent_fragment_downloads': concurrent,
            'http_chunk_size': chunk_size,
            'socket_timeout': 30,
            'retries': 10,
            'fragment_retries': 10,
            'file_access_retries': 5,
            # 1 extractor retry: let the outer retry loop handle client rotation.
            # Default=3 means 3 internal retries on bot-check (same client each time) before
            # the outer loop fires — wastes time and compounds rate-limit hits on soft-banned IPs.
            'extractor_retries': 1,
            # Verified 2026-07-19 (job j_e8eb2d0e): in engine/API mode ThrottledDownload ABORTS the
            # job ("The download speed is below throttle limit") — it is not the CLI's graceful
            # re-extract. This connection (Verizon 5G Home, mobile-classified CDN mv=m) sustains
            # only ~0.4–1.5 MiB/s, so any threshold above ~0.4 MiB/s fails every large download
            # seconds in (2 MiB/s did exactly that). Keep strictly below the network floor; this
            # option exists only to catch true nsig throttling (~50–80 KiB/s).
            'throttledratelimit': 256 * 1024,
            'skip_unavailable_fragments': True,
            'prefer_ffmpeg': True,
            'writesubtitles': False,
            'writethumbnail': False,
            'progress_hooks': [self._progress_hook],
            'noplaylist': True,
            'format_sort': sort,
            'buffersize': 1024 * 1024,
            'continuedl': True,    # resume an interrupted long-video download from the byte offset (.part + .ytdl
                                   # track progress). Keep the safe .part path (nopart would let an interrupted
                                   # progressive-fallback file look "complete" and be skipped by overwrites=False).
            'overwrites': False,   # collision-safe: never silently clobber an existing file
            # HLS fallback quality: when android/ios unexpectedly returns HLS (geo-block, token miss),
            # hand the stream to ffmpeg (faster than native HLS downloader for long streams) and
            # keep mpegts container during download to avoid a re-mux step. No-ops on DASH path.
            'hls_prefer_native': False,
            'hls_use_mpegts': True,
            # Always pin yt-dlp to an absolute trusted location. A missing tool
            # gets an absolute non-existent sentinel so cwd/PATH cannot supply it.
            'ffmpeg_location': ffmpeg_location,
        }

        # Request pacing to reduce IP soft-ban risk. Use slow_requests=True during/after heavy testing.
        if slow_requests:
            opts['sleep_requests'] = 2.0
            logger.info('[Speed] slow_requests=True: 2s sleep between extraction requests (IP cooldown mode)')
        if source_address:
            opts['source_address'] = source_address
            logger.info(f'[Speed] source_address={source_address} (manual NIC/IP binding)')

        # Enable every installed JS runtime so the EJS nsig/signature solver works (yt-dlp only auto-enables
        # deno). Set the key even when empty so yt-dlp cannot perform its own cwd/PATH lookup.
        js_rts = self._detect_js_runtimes()
        if self.pot_mode == 'script':
            # The script plugin independently asks for Node and Deno. Bind both
            # to trusted absolute paths or safe missing sentinels so it cannot
            # fall back to its cwd-preferring runtime discovery.
            for runtime in ('node', 'deno'):
                js_rts.setdefault(runtime, {
                    'path': _unavailable_tool_path(runtime),
                })
        opts['js_runtimes'] = js_rts

        # Clipped/section download (convenient for grabbing just part of a long video).
        # NOTE: force_keyframes_at_cuts re-muxes via ffmpeg and bypasses native -N parallelism, so it is
        # MUCH slower per MiB; we leave it off by default (cut lands on the nearest keyframe, plenty for
        # clips/benchmarks). For representative throughput numbers, prefer a short *full* video over a section.
        if section:
            rng = self._parse_section(section)
            if rng:
                opts['download_ranges'] = self._download_range_func([rng])
                # Optional precise cut (re-encodes via ffmpeg at range edges — slower).
                if getattr(spec, 'section_precise', False):
                    opts['force_keyframes_at_cuts'] = True
                    logger.info(
                        f"[Section] Limiting download to {section} "
                        f"({rng[0]:.1f}s–{rng[1]:.1f}s; precise keyframe cuts)"
                    )
                else:
                    logger.info(
                        f"[Section] Limiting download to {section} "
                        f"({rng[0]:.1f}s–{rng[1]:.1f}s; nearest keyframe)"
                    )
            else:
                # Whitespace-only / empty after normalize → treat as no clip (not an error).
                if not self._normalize_section_text(section):
                    pass
                else:
                    # Fail closed when a real section was requested but could not be parsed.
                    # Silent full-video download is worse than an explicit error.
                    raise ValueError(
                        f"Invalid clip range '{section}'. Use START-END like 1:30-3:00 or 90-180."
                    )

        # Download engine policy (auto/hybrid|builtin|aria2|turbo) — from JobSpec when present.
        # hybrid defers the final tool pick until after extract_info (see download()).
        # Stash on opts so parallel download() threads don't race on instance fields.
        effective, eng_reason, aria, turbo = self.resolve_engine(spec)
        self._last_engine = effective
        self._last_engine_reason = eng_reason
        self._aria_bin_for_job = aria
        self._turbo_bin_available = turbo  # availability only; active bin set in pick
        self._turbo_bin_for_job = None
        opts['_engine_policy'] = effective
        opts['_engine_aria'] = aria
        opts['_engine_turbo'] = turbo
        opts['_engine_builtin_pinned'] = False
        self._clear_aria2_opts(opts)  # start clean; hybrid/force re-apply after extract
        logger.info(f"[Engine] {eng_reason}")

        if effective == 'aria2' and aria:
            # Forced aria2 pre-register (may still be ignored by yt-dlp on DASH/HLS).
            # Final gate (section/live) still runs in _hybrid_pick_after_extract.
            self._apply_aria2_opts(opts, aria)
            logger.info(f"[Engine] aria2c preselected → {aria}")
        elif effective == 'turbo' and turbo:
            self._turbo_bin_for_job = turbo
            logger.info(f"[Engine] turbo preselected → {turbo}")
        elif effective == 'hybrid':
            logger.info("[Engine] hybrid waiting on format resolve (builtin / turbo / aria2)")

        if not getattr(self, '_speed_tip_shown', False):
            logger.info(
                "[Tip] Biggest speed lever now: verified Node or Deno plus the Python "
                "dependencies shipped by a reviewed yt_final release. The bgutil PO-token helper is "
                "manual-only; review its upstream provenance before enabling it."
            )
            self._speed_tip_shown = True

        # Optional cookies for bypassing throttling / SABR limits (highly recommended)
        # Support both browser (flaky on Windows if Chrome/Edge running) and cookie file (more reliable)
        if cookies_from_browser:
            try:
                opts['cookiesfrombrowser'] = (cookies_from_browser,) if isinstance(cookies_from_browser, str) else cookies_from_browser
            except Exception:
                pass
        if cookiefile:
            opts['cookiefile'] = str(cookiefile)

        # Important: we do NOT put heavy GPU args here anymore.
        # Final quality step is handled by FFmpegProcessor after download.
        pps = []
        if audio_only:
            afmt = (getattr(spec, 'audio_format', None) or 'mp3')
            if afmt and afmt != 'best':
                # A1: real codec/quality selection (mp3/aac/opus/flac/wav/m4a/alac) instead of forced mp3/192.
                pps.append({
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': afmt,
                    'preferredquality': str(getattr(spec, 'audio_quality', None) or '192'),
                })
            else:
                # A1: 'best' keeps the native bestaudio stream (no lossy->lossy); remux only.
                opts['format'] = 'bestaudio/best'
        # else: clean - final encode/remux done outside yt-dlp by FFmpegProcessor

        # Extended JobSpec download-time options (archive, subs, thumbnail, sponsorblock, proxy...).
        if spec is not None:
            self._apply_spec_download_opts(opts, pps, spec, section)

        opts['postprocessors'] = pps
        return opts

    def _apply_spec_download_opts(self, opts, pps, spec, section=None):
        """Layer the JobSpec's download-time options onto a yt-dlp opts dict (in place).
        Only touches keys when the corresponding spec field is set, so the default fast path is intact."""
        # Exact format id overrides the smart selector. Runs AFTER opts['format'] was set by the
        # selector (and after the audio-only 'bestaudio/best' branch), so it cleanly replaces both.
        # Keep format_sort untouched so ties still bias toward the requested codec.
        if getattr(spec, 'format_id', None):
            opts['format'] = str(spec.format_id)
        # C3: persistent download archive (skip for clipped/section downloads — a clip would mark
        # the whole video id as 'done'). Auto-confined to playlist/batch context by the engine.
        if getattr(spec, 'download_archive', None) and not section:
            opts['download_archive'] = str(spec.download_archive)

        # Skip Shorts (long-standing yt-dlp request): filter by URL or <=60s duration.
        if getattr(spec, 'no_shorts', False):
            def _skip_shorts(info_dict, *, incomplete=False):
                url = info_dict.get('webpage_url') or info_dict.get('original_url') or ''
                if '/shorts/' in url:
                    return 'Skipping YouTube Short (shorts URL)'
                dur = info_dict.get('duration')
                if dur is not None and dur <= 60:
                    return f'Skipping Short ({int(dur)}s <= 60s)'
                return None
            opts['match_filter'] = _skip_shorts

        # Network knobs (R4)
        if getattr(spec, 'proxy', None):
            opts['proxy'] = spec.proxy
        if getattr(spec, 'concurrent_fragments', None):
            # Floor at 1 (mirror the max_workers clamp above): yt-dlp's Python API does NOT run the
            # CLI-only validate_positive() check, so a non-positive value flows straight into
            # ThreadPoolExecutor(math.ceil(max_workers/max_progress)) inside the fragment downloader
            # and crashes with 'max_workers must be greater than 0' for any multi-stream DASH job.
            # Cap at 16 as well as floor at 1: an API client passing concurrent_fragments=500 would
            # otherwise start 500 fragment threads. 16 is already generous for HLS/DASH; more does
            # not help against the per-IP gate.
            opts['concurrent_fragment_downloads'] = max(1, min(int(spec.concurrent_fragments), 16))

        # Output organisation (B7) — keep everything rooted under output_path.
        if getattr(spec, 'organize_by', None) == 'uploader':
            opts['outtmpl'] = str(self.output_path / '%(uploader)s' / '%(title)s [%(id)s].%(ext)s')
        elif getattr(spec, 'organize_by', None) == 'date':
            opts['outtmpl'] = str(self.output_path / '%(upload_date>%Y-%m-%d)s' / '%(title)s [%(id)s].%(ext)s')
        if getattr(spec, 'output_template', None):
            tmpl = spec.output_template
            opts['outtmpl'] = tmpl if os.path.isabs(tmpl) else str(self.output_path / tmpl)
        if getattr(spec, 'write_sidecar', False):
            opts['writeinfojson'] = True

        # Subtitles (B3): written as a sidecar; the engine embeds/burns from there.
        # --sub-format chooses srt (default, edit-friendly), vtt (Jellyfin/Plex/browser), or both.
        if getattr(spec, 'sub_langs', None):
            opts['writesubtitles'] = True
            opts['subtitleslangs'] = list(spec.sub_langs)
            if getattr(spec, 'sub_auto', False):
                opts['writeautomaticsub'] = True
            fmt = getattr(spec, 'sub_format', 'srt') or 'srt'
            if fmt == 'vtt':
                opts['subtitlesformat'] = 'vtt/best'
                pps.append({'key': 'FFmpegSubtitlesConvertor', 'format': 'vtt'})
            elif fmt == 'both':
                # keep the NATIVE vtt (no convertor — FFmpegSubtitlesConvertor DELETES the source
                # when formats differ, which left only .srt). The engine derives a sibling .srt
                # post-download so 'both' really delivers both; yt-dlp can't write two in one pass.
                opts['subtitlesformat'] = 'vtt/best'
            else:  # 'srt' (legacy default - byte-identical behaviour)
                opts['subtitlesformat'] = 'srt/best'
                pps.append({'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'})

        # Thumbnail (B6): written as a sidecar; the engine attaches it as cover art.
        # --write-artwork also needs the on-disk thumbnail, independently of --embed-thumbnail.
        if getattr(spec, 'embed_thumbnail', False) or getattr(spec, 'write_artwork', False):
            opts['writethumbnail'] = True

        # SponsorBlock (C8): yt-dlp native. 'mark' adds chapters (fast, container-level); 'remove'
        # cuts the segments out (re-encodes around cuts). Non-fatal if the API is unreachable.
        sb = getattr(spec, 'sponsorblock', None)
        if sb in ('mark', 'remove'):
            if sb == 'mark':
                # yt-dlp's SponsorBlockPP does NOT expand the literal 'all' (only its CLI option
                # callback does) — passing ['all'] queried an unknown category -> ZERO segments.
                try:
                    from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP as _SBPP
                    cats = list(_SBPP.CATEGORIES.keys())
                except Exception:
                    cats = ['sponsor', 'selfpromo', 'interaction', 'intro', 'outro',
                            'preview', 'filler', 'music_offtopic']
                pps.append({'key': 'SponsorBlock', 'categories': cats,
                            'api': 'https://sponsor.ajay.app'})
                pps.append({'key': 'ModifyChapters',
                            'sponsorblock_chapter_title': '[SponsorBlock]: %(category_names)l'})
            else:
                cats = ['sponsor', 'selfpromo', 'interaction']
                pps.append({'key': 'SponsorBlock', 'categories': cats,
                            'api': 'https://sponsor.ajay.app'})
                pps.append({'key': 'ModifyChapters', 'remove_sponsor_segments': cats})

    @staticmethod
    def _time_to_seconds(t):
        """Parse 'SS', 'MM:SS' or 'HH:MM:SS' (float seconds allowed) to seconds."""
        t = str(t).strip()
        if not t:
            return None
        parts = t.split(':')
        if len(parts) > 3:
            return None
        try:
            parts = [float(p) for p in parts]
        except ValueError:
            return None
        if any(p < 0 for p in parts):
            return None
        # For MM:SS / HH:MM:SS, minutes/seconds components must be < 60 (except leading hours).
        if len(parts) >= 2 and parts[-1] >= 60:
            return None
        if len(parts) == 3 and parts[1] >= 60:
            return None
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec

    @staticmethod
    def _normalize_section_text(section):
        """Normalize user clip text: unicode dashes, 'to', whitespace."""
        if section is None:
            return None
        s = str(section).strip()
        if not s:
            return None
        # Unicode dashes / minus often pasted from docs or rich text
        for ch in ('\u2013', '\u2014', '\u2212', '\u2012', '\u2015', '\uFE58', '\uFE63', '\uFF0D'):
            s = s.replace(ch, '-')
        # "1:30 to 3:00" / "1:30 ~ 3:00"
        import re as _re
        s = _re.sub(r'\s+[tT][oO]\s+', '-', s)
        s = _re.sub(r'\s*[~～]\s*', '-', s)
        s = _re.sub(r'\s*-\s*', '-', s)  # collapse spaces around hyphen
        s = s.strip()
        return s or None

    @classmethod
    def _parse_section(cls, section):
        """Parse 'START-END' (e.g. '0:00-2:00', '90-210', '1:00:00-1:02:00') -> (start, end) seconds.

        Accepts unicode dashes and 'to'. Empty start ('-2:00') means from 0.
        Returns None if unparseable or end <= start. Classmethod so web/CLI can validate without a detector.
        """
        s = cls._normalize_section_text(section)
        if not s or '-' not in s:
            return None
        # Use partition so only the first hyphen splits start/end.
        start_s, _, end_s = s.partition('-')
        # An empty start ("-2:00") legitimately means "from the beginning" -> 0.0; but a non-empty,
        # UNPARSEABLE start ("foo-2:00") must reject the whole section rather than silently using 0.0
        # (which would download from the start). Distinguish None-from-empty vs None-from-parse-failure.
        if start_s.strip():
            start = cls._time_to_seconds(start_s)
            if start is None:
                return None
        else:
            start = 0.0
        end = cls._time_to_seconds(end_s)
        if end is None or end <= start:
            return None
        return (start, end)

    @classmethod
    def format_section_range(cls, section):
        """Human-readable validated range, or None if invalid/empty."""
        rng = cls._parse_section(section)
        if not rng:
            return None
        start, end = rng

        def _fmt(sec):
            sec = int(round(sec))
            h, r = divmod(sec, 3600)
            m, s = divmod(r, 60)
            if h:
                return f'{h}:{m:02d}:{s:02d}'
            return f'{m}:{s:02d}'

        return f'{_fmt(start)} → {_fmt(end)} ({int(round(end - start))}s)'

    @staticmethod
    def _download_range_func(ranges):
        """Wrap yt-dlp's download_range_func for time ranges [(start, end), ...].

        Falls back to a minimal compatible callable when yt_dlp.utils is unavailable
        (test stubs). Real downloads always have the full package.
        """
        try:
            from yt_dlp.utils import download_range_func
            return download_range_func(None, ranges)
        except Exception:
            # Minimal stand-in: same shape yt-dlp expects (callable -> list of {start_time,end_time}).
            sealed = [(float(s), float(e)) for s, e in ranges]

            def _fallback(_info_dict=None, _ydl=None):
                return [{'start_time': s, 'end_time': e} for s, e in sealed]

            return _fallback

    @staticmethod
    def _unique_path(path):
        """Return a non-colliding path. If 'name.ext' exists, try 'name (1).ext', 'name (2).ext', ..."""
        p = Path(path)
        if not p.exists():
            return p
        stem, suffix, parent = p.stem, p.suffix, p.parent
        for i in range(1, 10000):
            cand = parent / f"{stem} ({i}){suffix}"
            if not cand.exists():
                return cand
        return p  # pathological fallback

    def _progress_hook(self, d):
        if d.get('status') == 'downloading':
            pct = d.get('_percent_str', '??%').strip()
            spd = d.get('_speed_str', '')
            # Parse speed for BM update (robust: prefer 'speed' key if present, else parse _speed_str; units to Mbps)
            speed_val = d.get('speed')
            if speed_val is not None:
                try:
                    self.bw.update_speed(float(speed_val) / 1024 / 1024 * 8)  # bytes/s to Mbps
                except Exception:
                    logger.debug("BM speed bytes parse failed")
            elif spd:
                try:
                    m = re.search(r'([\d.]+)\s*([A-Za-z]+)?', spd)
                    if m:
                        val = float(m.group(1))
                        unit = (m.group(2) or '').lower()
                        # Robust unit parsing for yt-dlp _speed_str (B/KiB/MiB/GiB + KB/MB/M fallbacks)
                        # Normalize to bytes/s then to Mbps (unified with 'speed' bytes/s path)
                        if 'g' in unit:
                            bytes_s = val * (1024 ** 3)
                        elif 'm' in unit:
                            bytes_s = val * (1024 ** 2)
                        elif 'k' in unit:
                            bytes_s = val * 1024
                        else:
                            bytes_s = val  # B/s , bare num, or unknown
                        mbps = bytes_s / 1024 / 1024 * 8
                        self.bw.update_speed(mbps)
                except Exception:
                    logger.debug("BM speed str parse failed")
            self._emit_progress(d)
            print(f"\r⬇️  {pct} @ {spd}", end='', flush=True)
        elif d.get('status') == 'finished':
            # A cancel pending at the finish tick (download DONE, finalize not yet started) must abort
            # here and propagate — the old code called the sink directly and swallowed EVERY exception,
            # so a DownloadCancelled raised by the web sink was dropped and finalize ran anyway.
            if _cancel_pending(getattr(self, 'cancel_check', None)):
                raise DownloadCancelled('YT_FINAL_CANCEL')
            sink = getattr(self, 'progress_sink', None)
            if sink:
                try:
                    sink({'status': 'finishing', 'percent': 100})
                except DownloadCancelled:
                    raise                       # a cancel raced in after the check — honor it
                except Exception:
                    pass
            print("\n✅ Download segment finished.")

    def _reset_transfer_stats(self):
        """Zero the per-transfer speed accumulators. Called at download() start AND at the top of each
        retry attempt so a slow/failed earlier attempt's samples never contaminate a later successful
        attempt's job-card avg/peak or the throttle hint (B1)."""
        self._speed_samples = []
        self._speed_peak = 0.0             # trim-proof running max (_speed_samples is ring-trimmed)
        self._speed_hi_count = 0           # count of samples >= 8 MiB/s (guards cold-start outliers)
        self._last_downloaded_bytes = 0    # merged transfer bytes (sum of per-stream), throttle gate
        self._stream_bytes = {}            # per-format_id max bytes -> summed for the size gate (B3)

    def _emit_progress(self, d):
        """Normalize a yt-dlp progress dict and push it to progress_sink (for the web UI).

        Cancel is checked FIRST and independently of the sink: the sink is a UI channel only the web
        worker installs, so checking it after the `if not sink: return` guard is exactly why a CLI
        `queue run` job could never observe a cancel."""
        if _cancel_pending(getattr(self, 'cancel_check', None)):
            raise DownloadCancelled('YT_FINAL_CANCEL')
        sink = getattr(self, 'progress_sink', None)
        try:
            # Byte/speed accounting runs UNCONDITIONALLY (before the sink gate): speed_stats(),
            # _report_speed's transfer figure and the throttle hint all read these accumulators,
            # and CLI runs have no sink — gating the accounting on it made those outputs dead code
            # on the CLI. Only the UI payload push below needs the sink.
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes') or 0
            # A merged 1080p job (137+140) downloads each stream SEQUENTIALLY in its own HttpFD, so
            # downloaded_bytes ramps 0->video then resets and ramps 0->audio. Track the per-format-id
            # max and SUM them so the throttle-hint size gate sees the combined transfer, not one
            # stream's max (B3). Fall back to a bare max when the hook carries no format_id.
            fmt_id = str((d.get('info_dict') or {}).get('format_id') or '')
            if downloaded:
                sb = getattr(self, '_stream_bytes', None)
                if sb is None:
                    sb = {}
                    self._stream_bytes = sb
                if fmt_id:
                    sb[fmt_id] = max(sb.get(fmt_id, 0), downloaded)
                    self._last_downloaded_bytes = sum(sb.values())
                else:
                    self._last_downloaded_bytes = max(getattr(self, '_last_downloaded_bytes', 0), downloaded)
            speed = d.get('speed')
            # Track samples for durable avg/peak when the job finishes.
            if speed and isinstance(speed, (int, float)) and speed > 0:
                samples = getattr(self, '_speed_samples', None)
                if samples is None:
                    samples = []
                    self._speed_samples = samples
                samples.append(float(speed))
                # Cap memory on long jobs
                if len(samples) > 4000:
                    del samples[:2000]
                # Running max + hi-sample count SURVIVE the ring trim above — the token-bucket burst is
                # at the START of a download, which the trim would otherwise discard (masking the clamp).
                self._speed_peak = max(getattr(self, '_speed_peak', 0.0), float(speed))
                if float(speed) >= 8 * 1024 * 1024:
                    self._speed_hi_count = getattr(self, '_speed_hi_count', 0) + 1
            if not sink:
                return                               # accounting done; the payload is UI-only
            eng = getattr(self, '_last_fetch_path', None) or getattr(self, '_last_engine', None)
            eng_reason = getattr(self, '_last_fetch_reason', None) or getattr(self, '_last_engine_reason', None)
            payload = {
                'status': 'downloading',
                'downloaded': downloaded,
                'total': total,
                # Clamp: on DASH, total is often a bitrate-derived total_bytes_estimate that
                # downloaded_bytes overshoots near completion -> a raw ratio shows '103.7%'.
                'percent': min(100.0, round(downloaded / total * 100, 1)) if total else None,
                'speed': speed,          # bytes/s
                'eta': d.get('eta'),     # seconds remaining
                'fragment_index': d.get('fragment_index'),
                'fragment_count': d.get('fragment_count'),
            }
            if eng:
                payload['engine'] = eng
            if eng_reason:
                payload['engine_reason'] = eng_reason
            net = getattr(self, '_last_network_hint', None)
            if net:
                payload['network_hint'] = net
            sink(payload)
        except Exception as e:
            # Cooperative cancel: progress_sink may raise DownloadCancelled.
            if (isinstance(e, DownloadCancelled)
                    or type(e).__name__ == 'DownloadCancelled'
                    or 'YT_FINAL_CANCEL' in str(e)):
                raise DownloadCancelled('YT_FINAL_CANCEL') from e
            pass

    def speed_stats(self):
        """Return {avg_speed, peak_speed} in bytes/s from samples collected during download()."""
        samples = getattr(self, '_speed_samples', None) or []
        if not samples:
            return {'avg_speed': None, 'peak_speed': None}
        return {
            'avg_speed': sum(samples) / len(samples),
            # Fold in the trim-proof running max: _speed_samples is ring-trimmed (oldest dropped),
            # which discards the start-of-download burst; max(samples) alone would then contradict the
            # throttle hint's "burst to N MiB/s" (which reads _speed_peak). Reconcile them here.
            'peak_speed': max(max(samples), getattr(self, '_speed_peak', 0.0)),
        }

    @staticmethod
    def _throttle_hint(avg_bps, peak_bps, hi_count, total_bytes, n_samples):
        """Post-download, NON-fatal diagnostic -> a hint string, or None. Flags the token-bucket /
        ISP-shaping signature: the flow demonstrably burst fast (peak) but then SUSTAINED far below it
        (avg) -> a shaping cliff, not a slow link (a slow link never bursts). Self-referential (the
        sustained rate vs the flow's OWN peak), so there is no hardcoded 'fast for this link' guess.

        Gated hard against the known false-positive modes: the natural 0->26 MiB/s per-chunk
        oscillation (avg stays high -> no fire), slow links (never burst -> peak low, hi_count 0 ->
        no fire), and small/short downloads where slow-start dominates. `avg_bps` MUST be the transfer
        sample-mean (speed_stats), not total_bytes/elapsed (which is contaminated by extraction+merge
        time). NEVER aborts anything — the Speed Test is the real, link-relative verdict."""
        MiB = 1024 * 1024
        if not avg_bps or not peak_bps:
            return None
        if total_bytes < 30 * MiB or n_samples < 20:          # too small / too few samples to judge
            return None
        avg_mibs, peak_mibs = avg_bps / MiB, peak_bps / MiB
        # Fire only if the link PROVED it can go fast (>=3 samples >=8 MiB/s, not one cold-start
        # outlier) yet the sustained rate is both absolutely low (<3.5) AND a small fraction of the
        # peak (<=0.45x) -> a clamp, not oscillation.
        if hi_count >= 3 and peak_mibs >= 8.0 and avg_mibs < 3.5 and avg_mibs <= 0.45 * peak_mibs:
            return (f"burst to {peak_mibs:.0f} MiB/s then sustained only {avg_mibs:.1f} MiB/s — possible "
                    f"ISP rate cap / shaping. Run the Speed Test to confirm; the bypass may help.")
        return None

    def result_meta(self):
        """Durable post-run summary for the job card (engine actually used + speed stats)."""
        stats = self.speed_stats()
        eng = getattr(self, '_last_fetch_path', None) or getattr(self, '_last_engine', None)
        reason = (getattr(self, '_last_fetch_reason', None)
                  or getattr(self, '_last_engine_reason', None))
        out = {
            'engine': eng,
            'engine_reason': reason,
            'avg_speed': stats.get('avg_speed'),
            'peak_speed': stats.get('peak_speed'),
            'network_hint': getattr(self, '_last_network_hint', None),
        }
        hint = self._throttle_hint(stats.get('avg_speed'), getattr(self, '_speed_peak', 0.0),
                                   getattr(self, '_speed_hi_count', 0),
                                   getattr(self, '_last_downloaded_bytes', 0),
                                   len(getattr(self, '_speed_samples', None) or []))
        if hint:
            out['throttle_suspected'] = True
            out['throttle_hint'] = hint
        # A --parallel batch that partly failed lists the dropped URLs here so the job card can show
        # "N of M downloaded" instead of a clean 'done'. Reset per job in _run_body; only set by
        # download_parallel on partial success, so a normal single-URL/all-success job never carries it.
        _partial = getattr(self, 'last_partial_errors', None)
        if _partial:
            out['partial_failures'] = [{'url': u, 'error': m, 'kind': k} for u, (m, k) in _partial]
        # The requested output_dir was unusable and we saved elsewhere. Previously this was a
        # logger.warning only, so the card said 'done' while the file sat in the previous folder —
        # invisible unless the user went looking in final_yt.log.
        _odf = getattr(self, 'last_output_dir_fallback', None)
        if _odf:
            out['output_dir_fallback'] = _odf
        # Processing failed and we delivered the RAW download instead. Still a delivery (better than
        # nothing), but the file is not what the profile asked for, so it must not read as a clean win.
        _deg = getattr(self, 'last_degraded', None)
        if _deg:
            out['degraded'] = _deg
        return {k: v for k, v in out.items() if v is not None}

    @staticmethod
    def _summarize_selection(info):
        """Extract the chosen resolution/codecs/size/format_id/ext from an extracted info dict.
        Pure (no network). Shared by _log_quality and the engine's simulate() to avoid drift.
        Returns a dict: {height, vcodec, acodec, filesize, format_id, ext, resolution}."""
        out = {'height': None, 'vcodec': None, 'acodec': None, 'filesize': 0,
               'format_id': None, 'ext': None, 'resolution': None}
        if not info:
            return out
        reqs = info.get('requested_formats') or info.get('requested_downloads')
        out['height'] = info.get('height')
        out['resolution'] = info.get('resolution')
        if reqs:
            # For a merged format yt-dlp already sets the TOP-LEVEL filesize/filesize_approx to the
            # SUM of the component streams, so seeding from it and then adding each component again
            # double-counts (2x) the size. Sum ONLY the per-stream sizes here.
            filesize = 0
            for f in reqs:
                if f.get('vcodec') and f.get('vcodec') != 'none':
                    out['vcodec'] = f.get('vcodec')
                    out['height'] = out['height'] or f.get('height')
                    out['ext'] = out['ext'] or f.get('ext')
                if f.get('acodec') and f.get('acodec') != 'none':
                    out['acodec'] = f.get('acodec')
                filesize += (f.get('filesize') or f.get('filesize_approx') or 0)
            out['format_id'] = '+'.join([str(f.get('format_id', '?')) for f in reqs])
        else:
            # Single-format case: the top-level size is authoritative (no components to sum).
            filesize = info.get('filesize') or info.get('filesize_approx') or 0
            if info.get('vcodec'):
                out['vcodec'], out['acodec'] = info.get('vcodec'), info.get('acodec')
            out['ext'] = info.get('ext')
            out['format_id'] = info.get('format_id')
        out['filesize'] = filesize
        return out

    @staticmethod
    def _network_hint_from_info(info, proxy_active=False):
        """Inspect selected format URL for CDN throttle signals (pure, no network).

        Returns a short human string or None. Empirically (2026-07-19):
          - mv=m  → YouTube mobile-classified edge; raw curl ~0.7–1.5 MiB/s on carrier paths
          - missing n= → nsig not solved; classic ~1 MiB/s throttle
          - pot= present + n= present → best path; remaining limit is CDN/IP not the engine
        """
        if not info:
            return None
        from urllib.parse import urlparse, parse_qs
        fmts = info.get('requested_formats') or ([info] if info.get('url') else [])
        hints = []
        has_n = False
        has_pot = False
        mobile = False
        for f in fmts:
            u = f.get('url') or ''
            if not u:
                continue
            qs = parse_qs(urlparse(u).query)
            if qs.get('n'):
                has_n = True
            if qs.get('pot'):
                has_pot = True
            if (qs.get('mv') or [''])[0] == 'm':
                mobile = True
        if mobile:
            hints.append('cdn=mobile')
        if has_pot:
            hints.append('pot=ok')
        if has_n:
            hints.append('nsig=ok')
        else:
            hints.append('nsig=missing')
        if mobile and has_n:
            if proxy_active:
                hints.append('✓ via proxy — ISP shaping bypassed')
            else:
                hints.append('⚠ ISP throttling video (SNI shaping) — enable a proxy in Settings')
        return ' · '.join(hints) if hints else None

    @staticmethod
    def _log_quality(info):
        """Log the selected resolution/codec/size from a single extracted info dict (no extra network call)."""
        if not info:
            return
        try:
            s = YouTubeDownloader._summarize_selection(info)
            filesize = s['filesize']
            size_str = f"{filesize/1024/1024:.1f}MiB" if filesize else "unknown size"
            res_str = f"{s['height']}p" if s['height'] else (s['resolution'] or "max")
            logger.info(f"[Quality] Selected: {res_str} | vcodec={s['vcodec'] or '?'} | acodec={s['acodec'] or '?'} | "
                        f"{size_str} | title={(info.get('title') or '?')[:60]}")
            if s['format_id']:
                logger.info(f"[Quality] format_id: {s['format_id']}")
            hint = YouTubeDownloader._network_hint_from_info(info)
            if hint:
                logger.info(f"[Speed] Network path: {hint}")
        except Exception:
            logger.debug("Quality logging failed (non-fatal)")

    @staticmethod
    def _format_rows(info):
        """Curate a yt-dlp info dict's 'formats' list into machine-readable rows for list-formats --json.
        Pure (no network): testable by feeding a synthetic info dict."""
        rows = []
        for f in (info.get('formats') or []):
            rows.append({
                'format_id': f.get('format_id'),
                'ext': f.get('ext'),
                'vcodec': f.get('vcodec'),
                'acodec': f.get('acodec'),
                'height': f.get('height'),
                'width': f.get('width'),
                'fps': f.get('fps'),
                'tbr': f.get('tbr'),
                'filesize': f.get('filesize') or f.get('filesize_approx'),
                'format_note': f.get('format_note'),
                'protocol': f.get('protocol'),
                'resolution': f.get('resolution'),
            })
        return rows

    @staticmethod
    def _format_height(f):
        """Best-effort height from a yt-dlp format dict (height field or resolution note)."""
        h = f.get('height')
        if h and isinstance(h, (int, float)) and h > 0:
            return int(h)
        for key in ('resolution', 'format_note', 'format_id'):
            raw = str(f.get(key) or '')
            # 1920x1080 or 1080p
            import re as _re
            m = _re.search(r'(?:^|[xX×])\s*(\d{3,4})\s*$', raw.replace('p', ''))
            if not m:
                m = _re.search(r'(\d{3,4})\s*p\b', raw, _re.I)
            if m:
                try:
                    v = int(m.group(1))
                    if 144 <= v <= 4320:
                        return v
                except ValueError:
                    pass
        return None

    @staticmethod
    def _format_fps(f):
        for key in ('fps', 'average_fps'):
            fps = f.get(key)
            if fps is None:
                continue
            try:
                v = float(fps)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return int(round(v))
        return None

    @staticmethod
    def _height_to_quality_option(h):
        """Map pixel height → Compose Max resolution option (1080p/4k/…)."""
        if h is None:
            return 'best'
        h = int(h)
        if h >= 2160:
            return '4k'
        if h >= 1440:
            return '1440p'
        if h >= 1080:
            return '1080p'
        if h >= 720:
            return '720p'
        if h >= 480:
            return '480p'
        return 'best'

    @staticmethod
    def _probe_resolution_summary(info):
        """From an extract_info dict, compute max height and available resolutions (no network).

        suggested_* = best everyday pick for **edit + download speed** (H.264 ≤1080p when possible),
        not max archive quality. max_* / quality_max_* describe the tallest stream.
        """
        heights = set()
        widths = set()
        codecs = set()
        fps_vals = set()
        h264_heights = set()
        for f in (info.get('formats') or []):
            h = YouTubeDownloader._format_height(f)
            if h:
                heights.add(h)
            w = f.get('width')
            if w and isinstance(w, (int, float)) and w > 0:
                widths.add(int(w))
            fps = YouTubeDownloader._format_fps(f)
            if fps:
                fps_vals.add(fps)
            vc = f.get('vcodec') or ''
            if vc and vc != 'none':
                vcl = vc.lower()
                if vcl.startswith('avc') or 'h264' in vcl:
                    codecs.add('h264')
                    if h:
                        h264_heights.add(h)
                elif vcl.startswith('av01') or 'av1' in vcl:
                    codecs.add('av1')
                elif vcl.startswith('vp9') or vcl.startswith('vp09'):
                    codecs.add('vp9')
                else:
                    codecs.add(vc.split('.')[0])
        # Top-level fallbacks (some extracts only set these on the root info dict)
        if not heights and info.get('height'):
            try:
                heights.add(int(info['height']))
            except (TypeError, ValueError):
                pass
        for key in ('fps', 'average_fps'):
            if info.get(key) is not None:
                try:
                    v = float(info[key])
                    if v > 0:
                        fps_vals.add(int(round(v)))
                except (TypeError, ValueError):
                    pass
        max_h = max(heights) if heights else None
        max_w = max(widths) if widths else None
        h264_max = max(h264_heights) if h264_heights else None

        # --- Edit + speed suggestion (primary "Suggested") ---
        # Prefer H.264 at most 1080p: fastest practical download + edit-friendly.
        if 'h264' in codecs:
            edit_codec = 'h264'
            # Cap edit height at 1080 even if taller H.264 exists (rare)
            edit_h = min(h264_max or 1080, 1080)
            # If only lower H.264 exists, use that
            if h264_max and h264_max < 1080:
                edit_h = h264_max
            # Ensure we don't suggest taller than overall max
            if max_h:
                edit_h = min(edit_h, max_h)
        elif max_h:
            # No H.264: still cap everyday suggestion at 1080 for speed
            edit_h = min(max_h, 1080)
            edit_codec = 'av1' if 'av1' in codecs else ('vp9' if 'vp9' in codecs else 'any')
        else:
            edit_h = None
            edit_codec = 'any'
        suggested = YouTubeDownloader._height_to_quality_option(edit_h)

        # Max-quality path (for "Use max height")
        if max_h and max_h > 1080:
            quality_max = YouTubeDownloader._height_to_quality_option(max_h)
            # H.264 almost never offers true 4K on YT
            if h264_max and h264_max >= max_h:
                codec_max = 'h264'
            elif 'av1' in codecs:
                codec_max = 'av1'
            elif 'vp9' in codecs:
                codec_max = 'vp9'
            else:
                codec_max = 'any'
        else:
            quality_max = suggested
            codec_max = edit_codec

        if max_h and max_h > 1080 and (not h264_max or h264_max <= 1080):
            stream_tip = (
                'Suggested = edit + speed (usually 1080p H.264). '
                f'Max height is {max_h}p — use “Use max height” with AV1/VP9/Any for archive 4K. '
                'H.264 on YouTube almost never goes above 1080p.'
            )
        elif 'h264' in codecs:
            stream_tip = (
                'Suggested = edit + speed: H.264 remuxes cleanly and usually downloads faster than AV1/VP9 4K.'
            )
        elif 'av1' in codecs or 'vp9' in codecs:
            stream_tip = 'No H.264 listed — Suggested uses the best available codec at ≤1080p for speed.'
        else:
            stream_tip = None

        dur = info.get('duration')
        try:
            dur = float(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        return {
            'title': info.get('title'),
            'id': info.get('id'),
            'duration': dur,
            'uploader': info.get('uploader') or info.get('channel') or info.get('uploader_id'),
            'webpage_url': info.get('webpage_url') or info.get('original_url'),
            'thumbnail': info.get('thumbnail'),
            'max_height': max_h,
            'max_width': max_w,
            'available_heights': sorted(heights, reverse=True),
            'codecs': sorted(codecs),
            'fps_values': sorted(fps_vals, reverse=True),
            'h264_max_height': h264_max,
            'stream_tip': stream_tip,
            # Primary suggestion = edit + download speed (not max archive quality)
            'suggested_quality': suggested,
            'suggested_codec': edit_codec,
            'suggested_label': (
                f'{suggested} · {edit_codec.upper() if edit_codec != "h264" else "H.264"} · edit + speed'
            ),
            # Max archive path (Use max height)
            'quality_max': quality_max,
            'codec_max': codec_max,
            'resolution_label': (f'{max_h}p' if max_h else None)
                                or info.get('resolution')
                                or 'unknown',
        }

    def probe_url(self, url, cookies_from_browser=None, cookiefile=None):
        """Extract format list for a URL without downloading. Returns summary dict or raises."""
        import yt_dlp
        if not self._validate_url(url):
            raise ValueError(f'Not a valid YouTube URL: {url}')
        opts = self.get_optimized_ydl_opts(
            quality='best', audio_only=False,
            cookies_from_browser=cookies_from_browser, cookiefile=cookiefile,
            codec='any', section=None)
        opts = dict(opts)
        opts['quiet'] = True
        opts['no_warnings'] = True
        opts['progress_hooks'] = []
        opts.pop('download_archive', None)
        # Don't need concurrent fragments for metadata probe
        opts['skip_download'] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        summary = self._probe_resolution_summary(info or {})
        summary['ok'] = True
        summary['url'] = url
        summary['id'] = (info or {}).get('id')
        summary['title'] = (info or {}).get('title')
        return summary

    @staticmethod
    def _degradation_reason(info):
        """Return a fail-fast reason string if a YouTube extraction is TOTALLY degraded, else None.

        yt-dlp already tries every client in the pool and merges formats; if ALL that comes back is
        storyboards (sb*), then nsig/SABR (or an IP rate-limit) broke every direct-URL client and a
        'download' would silently be a ~0-speed storyboard. Surface that instead.

        Deliberately does NOT gate on a fixed video-itag set (137/136/…): that false-positives and
        hard-fails LIVE streams (HLS itags like 91-96) and audio-only requests, and 140/251 are
        near-universal so such a check never catches the throttle case it was meant for anyway."""
        if not info or info.get('extractor_key') != 'Youtube':
            return None
        formats = info.get('formats') or []
        if formats and all(str(f.get('format_id', '')).startswith('sb') for f in formats):
            return ("storyboards-only formats returned — nsig/SABR broke every direct-URL client "
                    "(install/update a JS runtime such as Node, or the IP may be rate-limited).")
        return None

    @staticmethod
    def _classify_error(msg):
        """Bucket a download-failure message into a category. Single source of truth shared by the
        partial-failure report and _download_error_message (keep the keyword buckets in sync)."""
        m = str(msg or '').lower()
        if not m:
            return 'other'
        if any(s in m for s in ('confirm your age', 'age-restricted', 'age restricted',
                                'inappropriate for some users')):
            return 'agegate'
        if any(s in m for s in ('not a bot', 'sign in', 'confirm you')):
            return 'bot'
        if 'format' in m or 'requested format' in m:
            return 'format'
        # 403/429 soft-block: yt-dlp's HttpFD re-raises any status<500 immediately (no backoff, no
        # Retry-After), and googlevideo trips a ~60s all-403 "forbid" state on retry storms. Bucket
        # these so the retry loop backs off a bounded amount then re-extracts a fresh signed URL.
        # bot/agegate 403s are matched above, so this stays narrow.
        if any(s in m for s in ('http error 429', 'too many requests', 'http error 403', ': forbidden')):
            return 'ratelimit'
        if any(s in m for s in ('timed out', 'timeout', 'connection', 'network', 'unreachable',
                                'temporary failure', 'getaddrinfo', 'ssl', 'reset by peer')):
            return 'network'
        return 'other'

    @staticmethod
    def _is_auth_gate(kind):
        """True if a failure category is fixable by authentication (age gate or bot/sign-in)."""
        return kind in ('bot', 'agegate')

    @staticmethod
    def _distill_info(info):
        """Pull the editorial fields (title/uploader/date/chapters/...) out of a yt-dlp info dict.
        Returns a small plain dict (K2) consumed by the engine for tagging + chapter markers.
        Robust to missing keys and to the merged playlist-entry shape."""
        if not info:
            return {}
        try:
            if info.get('_type') == 'playlist' and info.get('entries'):
                entries = [e for e in info['entries'] if e]
                info = entries[0] if entries else info
            return {
                'title': info.get('title'),
                'uploader': info.get('uploader') or info.get('channel') or info.get('uploader_id'),
                'upload_date': info.get('upload_date'),     # YYYYMMDD
                'description': info.get('description'),
                'id': info.get('id'),
                'webpage_url': info.get('webpage_url'),
                'duration': info.get('duration'),
                'fps': info.get('fps'),
                'language': info.get('language'),
                # playlist/channel context: yt-dlp attaches these to a single-video info dict when a
                # `&list=` URL is fetched (even under noplaylist=True), feeding the NFO auto/episode
                # rule, tvshow.nfo and the <thumb> tag (thumbnail was previously dropped here).
                'playlist': info.get('playlist'),
                'playlist_title': info.get('playlist_title'),
                'playlist_index': info.get('playlist_index'),
                'playlist_id': info.get('playlist_id'),
                'channel': info.get('channel'),
                'channel_id': info.get('channel_id'),
                'thumbnail': info.get('thumbnail'),
                'tags': info.get('tags') or [],
                'categories': info.get('categories') or [],
                'chapters': [
                    {'start_time': c.get('start_time'), 'end_time': c.get('end_time'),
                     'title': c.get('title')}
                    for c in (info.get('chapters') or []) if c
                ],
                # SponsorBlock per-segment categories (yt-dlp `--sponsorblock mark`). Preserved so
                # the marker writer can emit color-coded category markers from the distilled dict.
                'sponsorblock_chapters': [
                    {'category': c.get('category'), 'start_time': c.get('start_time'),
                     'end_time': c.get('end_time')}
                    for c in (info.get('sponsorblock_chapters') or []) if c
                ],
            }
        except Exception:
            logger.debug("info distill failed (non-fatal)")
            return {}

    def _finalize_download_paths(self, downloaded_paths, info, url, quality, start):
        """De-dupe produced paths, record speed/history, return existing Path list."""
        real, _seen = [], set()
        for p in downloaded_paths:
            if not p:
                continue
            try:
                key = str(Path(p).resolve())
            except Exception:
                key = str(p)
            if key in _seen:
                continue
            _seen.add(key)
            pp = Path(p)
            if pp.exists():
                real.append(pp)
        stats = self.speed_stats()
        self._report_speed(real, time.time() - start, stats.get('avg_speed'))
        _th = self._throttle_hint(stats.get('avg_speed'), getattr(self, '_speed_peak', 0.0),
                                  getattr(self, '_speed_hi_count', 0),
                                  getattr(self, '_last_downloaded_bytes', 0),
                                  len(getattr(self, '_speed_samples', None) or []))
        if _th:
            logger.warning(f"[Speed] Throttle suspected — {_th}")
        distilled = self._distill_info(info)
        for p in real:
            self.last_info[str(p)] = distilled
        self.history.append({
            'url': url, 'quality': quality,
            'paths': [str(p) for p in real], 'timestamp': time.time()
        })
        return real

    def _turbo_compatible(self, spec, info, opts=None) -> tuple:
        """Whether turbo may run for this job. Returns (ok, reason_if_not)."""
        if opts is not None and opts.get('download_ranges'):
            return False, 'section clip requires built-in'
        if self._info_is_live(info):
            return False, 'live stream requires built-in'
        if spec is not None:
            if getattr(spec, 'section', None):
                return False, 'section clip requires built-in'
            if getattr(spec, 'sponsorblock', None):
                return False, 'sponsorblock requires built-in'
            if getattr(spec, 'download_archive', None):
                return False, 'download_archive requires built-in'
            if getattr(spec, 'sub_langs', None):
                return False, 'subtitles require built-in'
            if (getattr(spec, 'quality', None) or '') == 'audio' or getattr(spec, 'audio_format', None):
                return False, 'audio extract requires built-in'
        req = (info or {}).get('requested_formats') or []
        if len(req) > 1:
            return False, 'multi-stream (video+audio) DASH uses built-in'
        selected = req[0] if len(req) == 1 and isinstance(req[0], dict) else (info or {})
        proto = str(selected.get('protocol') or '').lower()
        url = str(selected.get('url') or '').lower()
        # yt-dlp's DASH selection URL commonly points at a chosen representation,
        # init segment, or fragment set while manifest_url points at a multi-track
        # MPD. Passing either to N_m3u8DL-RE without exact representation mapping
        # can fail or change the requested quality. Turbo is therefore restricted
        # to a proven HLS media/master playlist; DASH stays on built-in.
        is_hls = 'm3u8' in proto or 'hls' in proto or 'm3u8' in url
        if not is_hls:
            return False, 'DASH or ambiguous manifest requires built-in quality selection'
        return True, ''

    @staticmethod
    def _safe_name_part(s, max_len=120):
        s = re.sub(r'[^\w.\- \[\]()]+', '_', str(s or ''), flags=re.UNICODE)
        s = s.replace('..', '_').strip(' .') or 'video'
        return s[:max_len]

    @staticmethod
    def _sanitize_engine_error(value, max_len=400):
        """Remove signed URLs and sensitive headers from optional-engine diagnostics."""
        text = str(value or '')
        text = re.sub(r'https?://[^\s\'\"]+', '[redacted-url]', text, flags=re.IGNORECASE)
        text = re.sub(
            r'(?i)\b(cookie|authorization|proxy-authorization):\s*[^\r\n]+',
            lambda m: f'{m.group(1)}: [redacted]', text)
        return text[:max_len]

    def _build_turbo_command(self, info, turbo_bin, ydl, spec,
                             out_dir, job_tmp, save_name):
        """Build an N_m3u8DL-RE command plus its selected manifest URL."""
        req = (info or {}).get('requested_formats') or []
        selected = req[0] if len(req) == 1 and isinstance(req[0], dict) else (info or {})
        stream_url = selected.get('url') if isinstance(selected, dict) else None
        if not stream_url and isinstance(info, dict):
            stream_url = info.get('url')

        cmd = [
            str(turbo_bin), str(stream_url or ''),
            '--save-dir', str(out_dir),
            '--save-name', str(save_name),
            '--tmp-dir', str(job_tmp),
            '--thread-count', '16',
            '--no-ansi-color',
        ]

        # Preserve yt-dlp's request context. Format-level headers win over the
        # top-level info, which in turn wins over YoutubeDL defaults.
        headers = {}
        params = getattr(ydl, 'params', None) or {}
        for source in (params.get('http_headers'),
                       (info or {}).get('http_headers'),
                       selected.get('http_headers') if isinstance(selected, dict) else None):
            if isinstance(source, dict):
                headers.update(source)
        cookiejar = getattr(ydl, 'cookiejar', None)
        if cookiejar is not None and stream_url:
            try:
                cookie = cookiejar.get_cookie_header(stream_url)
            except Exception:
                cookie = None
            if cookie:
                headers['Cookie'] = cookie
        for name, value in headers.items():
            if value is None:
                continue
            # Prevent malformed/injected CLI headers from untrusted metadata.
            clean_name = str(name).replace('\r', '').replace('\n', '').strip()
            clean_value = str(value).replace('\r', '').replace('\n', '').strip()
            if clean_name and clean_value:
                cmd.extend(['-H', f'{clean_name}: {clean_value}'])

        proxy = getattr(spec, 'proxy', None) if spec is not None else None
        proxy = proxy or params.get('proxy')
        if proxy:
            cmd.extend(['--custom-proxy', str(proxy)])

        ffmpeg_path = getattr(getattr(self, 'detector', None), 'ffmpeg_path', None)
        if ffmpeg_path:
            cmd.extend(['--ffmpeg-binary-path', str(ffmpeg_path)])
        return cmd, stream_url

    def _run_turbo_cancellable(self, cmd, timeout_s=3600 * 6, creationflags=0):
        """Run N_m3u8DL-RE under cooperative cancel (Popen + poll), mirroring _run_ffmpeg.

        Returns a SimpleNamespace(returncode, stdout, stderr) so callers match subprocess.run.
        Raises DownloadCancelled when cancel_check fires; TimeoutExpired when past timeout_s.
        """
        from types import SimpleNamespace
        popen_kw = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace',
        )
        if creationflags:
            popen_kw['creationflags'] = creationflags
        # Own process group/session so a cancel can kill the WHOLE tree: N_m3u8DL-RE spawns ffmpeg for
        # the merge, and killing only the direct child orphans that muxer (see _kill_process_tree).
        if _sys.platform.startswith('win'):
            popen_kw['creationflags'] = (popen_kw.get('creationflags', 0)
                                         | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        else:
            popen_kw['start_new_session'] = True
        process = subprocess.Popen(cmd, **popen_kw)
        stdout_chunks, stderr_chunks = [], []
        drain_exc = []

        def _drain(pipe, sink):
            try:
                for line in pipe:
                    sink.append(line)
            except BaseException as e:  # noqa: BLE001 - re-raised on the main thread
                drain_exc.append(e)

        readers = [
            threading.Thread(target=_drain, args=(process.stdout, stdout_chunks), daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, stderr_chunks), daemon=True),
        ]
        for t in readers:
            t.start()
        deadline = time.monotonic() + float(timeout_s)
        try:
            while process.poll() is None:
                if _cancel_pending(getattr(self, 'cancel_check', None)):
                    _kill_process_tree(process)
                    raise DownloadCancelled('YT_FINAL_CANCEL')
                if time.monotonic() >= deadline:
                    _kill_process_tree(process)
                    raise subprocess.TimeoutExpired(cmd, timeout_s)
                time.sleep(0.3)
            for t in readers:
                t.join(timeout=2)
            if drain_exc:
                raise drain_exc[0]
            return SimpleNamespace(
                returncode=process.returncode if process.returncode is not None else -1,
                stdout=''.join(stdout_chunks),
                stderr=''.join(stderr_chunks),
            )
        except DownloadCancelled:
            if process.poll() is None:
                try:
                    _kill_process_tree(process)
                    process.wait(timeout=5)
                except Exception:
                    pass
            raise
        finally:
            if process.poll() is None:
                try:
                    _kill_process_tree(process)
                    process.wait(timeout=5)
                except Exception:
                    pass
            for t in readers:
                if t.is_alive():
                    t.join(timeout=1)

    def _download_with_turbo(self, info, turbo_bin, ydl, spec=None):
        """Fetch a single proven HLS playlist with N_m3u8DL-RE. Returns Path or None.

        DASH stays built-in because yt-dlp's selected representation URL cannot be
        safely mapped to N_m3u8DL-RE without risking an interactive or different-quality
        selection. Merged multi-format YouTube also remains built-in.
        """
        if not info or not turbo_bin:
            return None
        ok, why = self._turbo_compatible(spec, info)
        if not ok:
            logger.info(f"[Engine] turbo skipped — {why}")
            return None
        # Prefer the selected format URL.
        stream_url = None
        proto = ''
        try:
            req = info.get('requested_formats') or []
            if len(req) == 1:
                stream_url = req[0].get('url')
                proto = (req[0].get('protocol') or '') + ' ' + (req[0].get('ext') or '')
            elif not req and info.get('url'):
                stream_url = info.get('url')
                proto = (info.get('protocol') or '') + ' ' + (info.get('ext') or '')
        except Exception:
            stream_url = None
        if not stream_url:
            return None
        # Heuristic: only send playlist-style URLs to N_m3u8DL-RE
        u_l = stream_url.lower()
        looks_playlist = (
            'm3u8' in u_l or 'mpd' in u_l or 'manifest' in u_l
            or 'm3u8' in proto.lower() or 'dash' in proto.lower() or 'hls' in proto.lower()
        )
        if not looks_playlist:
            logger.info("[Engine] turbo skipped — format is not a proven HLS playlist")
            return None
        title = (info.get('title') or info.get('id') or 'video').strip()
        vid = self._safe_name_part(info.get('id') or 'dl', 40)
        safe = self._safe_name_part(title, 160)
        out_dir = Path(self.output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        turbo_tmp_root = out_dir / '.turbo_tmp'
        turbo_tmp_root.mkdir(parents=True, exist_ok=True)
        import tempfile as _tempfile
        job_tmp = Path(_tempfile.mkdtemp(prefix=f'{vid}_', dir=str(turbo_tmp_root)))
        job_out = job_tmp / 'output'
        segment_tmp = job_tmp / 'segments'
        job_out.mkdir(parents=True, exist_ok=True)
        segment_tmp.mkdir(parents=True, exist_ok=True)
        save_name = f'{safe} [{vid}]'          # final, user-visible name (unchanged on both platforms)
        # Windows MAX_PATH (260) applies to the STAGED path, which nests 3 extra segments under the
        # output dir; a ~118-char descriptive leaf can push it over. Stage under a short synthetic
        # name and still deliver save_name at the end. On darwin stage_name IS save_name, so the
        # staged filename, glob pattern and argv are byte-for-byte identical to today.
        stage_name = f'stg_{vid}' if _sys.platform.startswith('win') else save_name
        # No --auto-select: the URL is the format yt-dlp already selected.
        cmd, stream_url = self._build_turbo_command(
            info, turbo_bin, ydl, spec, job_out, segment_tmp, stage_name)
        try:
            host = ''
            try:
                from urllib.parse import urlparse
                host = urlparse(stream_url).netloc or ''
            except Exception:
                host = ''
            logger.info(f"[Engine] turbo → {turbo_bin} host={host or '?'}")
            # encoding/errors: the child echoes the (unicode) save-name back as UTF-8; a strict
            # decode under the Windows ANSI code page kills the reader thread AFTER the download
            # finished, and the finally-block rmtree then throws the completed file away.
            # When a cancel_check is installed (web/CLI job), run via Popen + poll so cancel mid-turbo
            # kills the child promptly — subprocess.run blocks up to 6h with no probe. Without a
            # cancel channel keep subprocess.run so existing unit mocks and the default path stay
            # byte-compatible.
            timeout_s = 3600 * 6
            run_kw = dict(capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout_s)
            if _sys.platform.startswith('win'):
                run_kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            if getattr(self, 'cancel_check', None):
                res = self._run_turbo_cancellable(cmd, timeout_s=timeout_s,
                                                  creationflags=run_kw.get('creationflags', 0))
            else:
                res = subprocess.run(cmd, **run_kw)
            if res.returncode != 0:
                detail = self._sanitize_engine_error(res.stderr or res.stdout or '')
                logger.warning(f"[Engine] turbo exit {res.returncode}: {detail}")
                return None
            # The tool writes into this job's isolated staging directory. This avoids
            # stale same-id matches and keeps partial output away from the real output
            # directory when N_m3u8DL-RE fails or is interrupted.
            import glob as _globmod
            candidates = []
            for p in job_out.glob(_globmod.escape(stage_name) + '*'):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in ('.mp4', '.mkv', '.ts', '.m4a', '.webm', '.mov'):
                    continue
                candidates.append(p)
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                staged = candidates[0]
                final_path = self._unique_path(out_dir / f'{save_name}{staged.suffix.lower()}')
                shutil.move(str(staged), str(final_path))
                return final_path
            logger.warning("[Engine] turbo finished but no fresh output file found")
            return None
        except DownloadCancelled:
            # Cancel must reach the runner (records 'cancelled') — never collapse into None/fallback.
            raise
        except Exception as e:
            if (type(e).__name__ == 'DownloadCancelled'
                    or 'YT_FINAL_CANCEL' in str(e)):
                raise DownloadCancelled('YT_FINAL_CANCEL') from e
            logger.warning(f"[Engine] turbo error: {self._sanitize_engine_error(e)}")
            return None
        finally:
            try:
                import shutil as _sh
                if job_tmp.exists():
                    _sh.rmtree(job_tmp, ignore_errors=True)
            except Exception:
                pass

    def _set_last_error(self, msg, kind):
        """Record the last failure reason on BOTH a thread-local and the shared instance attributes.

        The thread-local copy lets download_parallel() read back the reason for the URL a given
        worker handled without racing other workers (each worker's download() runs on its own
        thread). The instance attributes preserve the serial-call contract read by the engine and
        the web server. In a parallel batch the instance writes race, but download_parallel()
        deterministically overwrites them from the per-thread values after the batch completes."""
        self._tls.last_error = msg
        self._tls.last_error_kind = kind
        self.last_error = msg
        self.last_error_kind = kind

    def _warm_probe_caches(self, spec=None):
        """Populate the lazy binary/runtime probe caches once (single-threaded) BEFORE
        download_parallel() fans out worker threads. Otherwise each worker's first
        get_optimized_ydl_opts() observes the still-empty cache attribute and redundantly
        re-runs the same aria2c / N_m3u8DL-RE / PO-token subprocess+network probes.

        JS runtime paths are excluded: managed authorization is refreshed for
        every options/job build.
        """
        try:
            self.resolve_engine(spec)          # -> _aria2_cache, _turbo_cache
        except Exception:
            pass
        if self.pot_mode == 'auto':            # only 'auto' actually probes the provider
            try:
                self._pot_provider_status()    # -> _pot_status_cache
            except Exception:
                pass

    def extract_only(self, url, quality="1080p", cookies_from_browser=None, cookiefile=None,
                     codec=None, section=None, slow_requests=False, source_address=None, spec=None):
        """Sink-free extraction for the batch prefetch pipeline: build the SAME opts download() would
        use for (url, spec) and return info = extract_info(url, download=False), so the resolved format
        URLs are valid when the same (url, spec) is later handed to download(preextracted_info=...).
        extract hits youtubei, NOT the googlevideo CDN, so this never touches the download rate bucket.

        Never raises. Touches no download-path state on self — it only reads the (pre-warmed) probe
        caches and writes self._last_engine, an idempotent same-value hint that download_parallel's
        workers already write concurrently. Returns the info dict, or None on any failure so the caller
        just re-extracts inline (the safe, current behaviour)."""
        try:
            import yt_dlp  # lazy
            normalized_url = normalize_youtube_url(url)
            if normalized_url is None:
                return None
            url = normalized_url
            audio_only = (quality == 'audio')
            # Mirror download()'s auto-auth cookie resolution so the info's formats/URLs line up.
            eff_cf = cookiefile
            use_auth = True if spec is None else bool(getattr(spec, 'use_auth', True))
            if use_auth and not eff_cf and not cookies_from_browser:
                mgr = self._get_cookie_manager()
                if mgr:
                    eff_cf = mgr.get_cookiefile() or eff_cf
            opts = self.get_optimized_ydl_opts(
                quality, audio_only=audio_only, cookies_from_browser=cookies_from_browser,
                cookiefile=eff_cf, codec=codec, section=section, slow_requests=slow_requests,
                source_address=source_address, spec=spec)
            opts = {k: v for k, v in opts.items() if not str(k).startswith('_engine')}
            opts['progress_hooks'] = []       # sink-free: no progress into the current job's sink
            opts['skip_download'] = True
            opts['quiet'] = True
            opts['no_warnings'] = True
            opts.pop('download_archive', None)  # extract regardless; download() still honours the archive
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except DownloadCancelled:
            raise                             # a cancel must escape, never be swallowed as "no prefetch"
        except Exception as e:
            logger.debug(f"[Prefetch] extract_only({url}) failed (will extract inline): {str(e)[:140]}")
            return None

    def download(self, url, quality="1080p", cookies_from_browser=None, cookiefile=None, codec=None, section=None, slow_requests=False, source_address=None, spec=None, preextracted_info=None, _post_download=None):
        """Download single URL. Returns list of real output Paths, or [] on failure (never fabricates).

        Uses a SINGLE extraction (extract_info(download=False) -> process_ie_result(download=True))
        instead of the old probe + diagnostics + real extraction, saving ~10-20s and reducing bot-gate hits.
        Cookie/bot/format failures are handled by the retry planner.

        `spec` (optional JobSpec) carries the new download-time knobs (audio_format, subtitles,
        download_archive, sponsorblock, thumbnail, proxy, ...). When None, behaviour is unchanged.
        """
        # Fresh per-call state (shared engine instance across web jobs). Transfer stats are ALSO reset
        # per-attempt below (B1); this start-reset covers the two early returns before the loop, which
        # would otherwise leave a PRIOR job's samples queryable via speed_stats()/result_meta().
        self._reset_transfer_stats()
        self._last_network_hint = None     # don't leak a prior job's CDN/throttle hint onto this card
        self._last_fetch_path = None
        self._last_fetch_reason = None
        import yt_dlp  # lazy
        original_url = url
        normalized_url = normalize_youtube_url(url)
        if normalized_url is None:
            logger.error(f"[ERROR] Not a valid YouTube URL: {original_url}")
            self._set_last_error(f"Not a valid YouTube URL: {original_url}", 'invalid_url')
            return []
        url = normalized_url

        audio_only = (quality == 'audio')

        def _make_opts(cfb, cf):
            return self.get_optimized_ydl_opts(quality, audio_only=audio_only, cookies_from_browser=cfb,
                                               cookiefile=cf, codec=codec, section=section,
                                               slow_requests=slow_requests, source_address=source_address,
                                               spec=spec)

        try:
            opts = _make_opts(cookies_from_browser, cookiefile)
        except ValueError as e:
            # Invalid --section / clip range: surface as a clean job failure (not a worker crash).
            logger.error(f"[Section] {e}")
            self._set_last_error(str(e), 'invalid_section')
            return []

        # Auto-auth: when the caller supplied no cookies, ask the manager (no-op
        # unless [auth].enabled). Per-job opt-out: JobSpec.use_auth=False → stay anonymous
        # (unless cookiefile was set explicitly as a per-job override).
        use_auth = True if spec is None else bool(getattr(spec, 'use_auth', True))
        if use_auth and not cookiefile and not cookies_from_browser:
            mgr = self._get_cookie_manager()
            if mgr:
                auto_cf = mgr.get_cookiefile()
                if auto_cf:
                    cookiefile = auto_cf
                    opts = _make_opts(None, cookiefile)
        elif not use_auth:
            logger.info('[Auth] use_auth=False for this job — skipping Auth session cookies')

        downloaded_paths = []

        def hook(d):
            if d.get('status') == 'finished':
                filename = d.get('filename') or d.get('info_dict', {}).get('_filename')
                if filename:
                    downloaded_paths.append(Path(filename))

        # Capture the base hooks (the UI/progress hook) ONCE. The retry loop rebuilds opts in
        # several places; re-prepending `hook` to the *growing* list there would accumulate duplicate
        # `hook` copies (double path-append per retry). Always set [hook] + base_hooks instead.
        base_hooks = list(opts.get('progress_hooks', []))
        opts['progress_hooks'] = [hook] + base_hooks

        # Track the failure reason so the engine/server can show an actionable message (not a generic one).
        # Reset via the helper so the per-thread copy is cleared too (a reused pool thread must not
        # carry a previous URL's reason into a run that succeeds or returns early).
        self._set_last_error(None, None)
        browser_cookie_failed = False
        last_exc = None

        def _apply_post_download(paths):
            if _post_download is None:
                return paths
            try:
                return _post_download(paths)
            except BaseException as post_exc:
                # The surrounding retry planner handles downloader failures only. Processing,
                # verification, cancellation, and final-rename failures must preserve their type.
                raise _PostDownloadFailure(post_exc) from post_exc

        output_lock_cm = None
        locked_video_id = None
        locked_output_dir = None

        def _release_output_lock():
            nonlocal output_lock_cm
            if output_lock_cm is not None:
                output_lock_cm.__exit__(None, None, None)
                output_lock_cm = None

        # Retry loop using planner. On bot: keep cookies, only swap client.
        for attempt in range(3):
            try:
                # Fresh slate each attempt: a component file (e.g. a video/audio part of a merge)
                # that reached 'finished' during an EARLIER failed attempt is still on disk (yt-dlp
                # only deletes merge parts after a successful merge) and its path is already in
                # downloaded_paths. If a later attempt then succeeds with a different format/auth path,
                # that abandoned part would leak into the returned list and be treated as a second
                # legitimate output. Clearing here means each attempt only reports files IT produced;
                # a genuine same-format resume re-fires the hook within the (now-succeeding) attempt.
                downloaded_paths.clear()
                self._reset_transfer_stats()   # B1: each attempt's speed stats stand alone (a slow
                                               # failed attempt must not skew the successful one's card)
                start = time.time()
                # Strip private engine stash keys — not valid yt-dlp params.
                ydl_opts = {k: v for k, v in opts.items() if not str(k).startswith('_engine')}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # ONE extraction; reuse the resolved info for the actual download. A batch-prefetched
                    # info (same opts, extracted moments ago) is used ONLY on attempt 0 to skip the
                    # youtubei round-trip; ANY retry re-extracts fresh, which is the built-in staleness
                    # recovery (a stale/forbidden URL just 403s attempt 0 and attempt 1 re-extracts).
                    if attempt == 0 and preextracted_info is not None:
                        info = preextracted_info
                    else:
                        info = ydl.extract_info(url, download=False)
                    # Pre-parse aliases may be unknown/ambiguous. The extractor's final ID is the
                    # authoritative collision key, acquired before turbo/builtin touches output.
                    extracted_video_id = (
                        (info or {}).get('id') if isinstance(info, dict) else None
                    )
                    current_output_dir = Path(self.output_path).expanduser().resolve()
                    if output_lock_cm is None and extracted_video_id:
                        _cm = output_identity_lock(extracted_video_id, current_output_dir)
                        _cm.__enter__()
                        output_lock_cm = _cm
                        locked_video_id = str(extracted_video_id)
                        locked_output_dir = current_output_dir
                    elif output_lock_cm is not None and (
                        str(extracted_video_id or "") != locked_video_id
                        or current_output_dir != locked_output_dir
                    ):
                        raise _OutputIdentityChanged(
                            "authoritative output identity changed across retry"
                        )
                    _deg = self._degradation_reason(info)
                    if _deg:
                        raise yt_dlp.utils.ExtractorError(_deg)
                    self._log_quality(info)
                    guard_reserved_uploader_dir(ydl, info)
                    # Stash CDN/nsig diagnostics for the job card.
                    try:
                        _proxy_on = bool(getattr(spec, 'proxy', None)) if 'spec' in dir() else False
                        self._last_network_hint = self._network_hint_from_info(info, proxy_active=_proxy_on)
                    except Exception:
                        self._last_network_hint = None
                    # Hybrid / forced turbo|aria2: pick path from the *resolved* format.
                    # Must pass ydl so aria2 opts land on ydl.params (opts alone is too late).
                    # Prefer per-opts engine stash (thread-safe under download_parallel).
                    fetch_path, turbo_bin = self._hybrid_pick_after_extract(
                        info, spec, opts, ydl=ydl,
                        policy=opts.get('_engine_policy') or getattr(self, '_last_engine', 'builtin'),
                        aria=opts.get('_engine_aria') if '_engine_aria' in opts
                        else getattr(self, '_aria_bin_for_job', None),
                        turbo=opts.get('_engine_turbo') if '_engine_turbo' in opts
                        else (getattr(self, '_turbo_bin_available', None)
                              or getattr(self, '_turbo_bin_for_job', None)),
                    )
                    self._last_fetch_path = fetch_path
                    # Human reason from last hybrid decision (if set on self by pick helper)
                    if not getattr(self, '_last_fetch_reason', None):
                        self._last_fetch_reason = f'fetch via {fetch_path}'
                    # Push engine to UI immediately (before first progress byte).
                    sink = getattr(self, 'progress_sink', None)
                    if sink:
                        try:
                            sink({
                                'status': 'downloading',
                                'engine': fetch_path,
                                'engine_reason': getattr(self, '_last_fetch_reason', None),
                                'percent': None,
                            })
                        except Exception as e:
                            if type(e).__name__ == 'DownloadCancelled' or 'YT_FINAL_CANCEL' in str(e):
                                raise
                    if fetch_path == 'turbo':
                        turbo_out = self._download_with_turbo(info, turbo_bin, ydl, spec=spec) if turbo_bin else None
                        if turbo_out and Path(turbo_out).exists():
                            downloaded_paths.append(Path(turbo_out))
                            logger.info(f"[Engine] turbo finished in {time.time()-start:.1f}s → {Path(turbo_out).name}")
                            real = self._finalize_download_paths(
                                downloaded_paths, info, url, quality, start)
                            if real:
                                real = _apply_post_download(real)
                                _release_output_lock()
                                return real
                        # Ensure fallback is pure native (no sticky aria2/turbo flags)
                        self._clear_aria2_opts(opts, ydl)
                        self._turbo_bin_for_job = None
                        opts['_engine_builtin_pinned'] = True
                        opts['_engine_policy'] = 'builtin'
                        opts['_engine_aria'] = None
                        opts['_engine_turbo'] = None
                        fetch_path = 'builtin'
                        self._last_fetch_path = 'builtin'
                        self._last_fetch_reason = 'turbo failed — fell back to built-in'
                        logger.warning("[Engine] turbo failed or unsuitable — falling back to built-in")
                    # builtin or aria2 (aria2 on ydl.params when progressive hybrid / forced)
                    try:
                        info = ydl.process_ie_result(info, download=True)
                    except Exception as dl_exc:
                        # Cooperative cancel must not be swallowed as an aria2 failure.
                        if (isinstance(dl_exc, DownloadCancelled)
                                or type(dl_exc).__name__ == 'DownloadCancelled'
                                or 'YT_FINAL_CANCEL' in str(dl_exc)):
                            raise DownloadCancelled('YT_FINAL_CANCEL') from dl_exc
                        # If aria2 fails, reuse the already-resolved info immediately with
                        # yt-dlp's built-in downloader. Routing this through the outer error
                        # planner used to terminate on ordinary aria2 network errors because
                        # they are neither auth nor format errors, despite the log claiming a
                        # built-in retry would occur.
                        if fetch_path == 'aria2':
                            self._clear_aria2_opts(opts, ydl)
                            opts['_engine_builtin_pinned'] = True
                            opts['_engine_policy'] = 'builtin'
                            opts['_engine_aria'] = None
                            opts['_engine_turbo'] = None
                            logger.warning(
                                f"[Engine] aria2 path failed — continuing with built-in: "
                                f"{self._sanitize_engine_error(dl_exc, max_len=180)}")
                            downloaded_paths.clear()
                            fetch_path = 'builtin'
                            self._last_fetch_path = 'builtin'
                            self._last_fetch_reason = 'aria2 failed — fell back to built-in'
                            info = ydl.process_ie_result(info, download=True)
                        else:
                            raise
                    # Authoritative final path(s) from the info dict. For merged formats (1080p =
                    # 137+140) yt-dlp DELETES the per-stream parts the progress hook captured, so we
                    # must add the post-merge filepath or a successful download looks "empty".
                    for rd in (info.get('requested_downloads') or []):
                        fp = rd.get('filepath') or rd.get('_filename')
                        if fp:
                            downloaded_paths.append(Path(fp))
                    if info.get('filepath'):
                        downloaded_paths.append(Path(info['filepath']))
                    try:
                        prepared = ydl.prepare_filename(info) if info else None
                        if prepared:
                            downloaded_paths.append(Path(prepared))
                    except Exception:
                        logger.debug("prepare_filename backup failed")

                real = self._finalize_download_paths(downloaded_paths, info, url, quality, start)
                real = _apply_post_download(real)
                _release_output_lock()
                return real

            except Exception as e:
                keep_lock_for_retry = False
                try:
                    if isinstance(e, _PostDownloadFailure):
                        raise e.cause
                    if isinstance(e, _OutputIdentityChanged):
                        raise
                    # Cooperative cancel must escape the retry planner immediately.
                    if (isinstance(e, DownloadCancelled)
                            or type(e).__name__ == 'DownloadCancelled'
                            or 'YT_FINAL_CANCEL' in str(e)):
                        raise DownloadCancelled('YT_FINAL_CANCEL') from e
                    last_exc = e
                    err_lower = str(e).lower()
                    is_dpapi_or_cookie_load = any(x in err_lower for x in
                                                  ['dpapi', 'decrypt', 'failed to decrypt', 'failed to load cookie',
                                                   'could not copy', 'cookie database'])
                    if is_dpapi_or_cookie_load:
                        browser_cookie_failed = True
                        logger.warning("[Cookies] Browser cookie load failed (DPAPI/Edge/Chrome common on Windows - "
                                       "close all browser windows or export cookies.txt instead). Retrying without browser cookies...")
                        # Drop only the browser-cookie source; preserve cookiefile (not DPAPI-locked).
                        old_opts = opts
                        opts = _make_opts(None, cookiefile)
                        self._copy_engine_retry_state(opts, old_opts)
                        opts['progress_hooks'] = [hook] + base_hooks
                        if cookiefile:
                            logger.info("[Cookies] cookiefile retained for retry (only browser-sourced cookies dropped)")
                        else:
                            logger.info("[Cookies] No cookiefile fallback; retrying unauthenticated")
                        keep_lock_for_retry = True
                        continue
                    # Auto-auth refresh: on an auth/bot gate, ask the manager for fresh
                    # cookies and retry with them (the "automatic" behaviour).
                    # Skip when this job opted out of Auth (use_auth=False).
                    if use_auth and YouTubeDownloader._is_auth_gate(YouTubeDownloader._classify_error(e)):
                        mgr = self._get_cookie_manager()
                        if mgr:
                            mgr.notify_auth_failure(url)
                            fresh = mgr.get_cookiefile()
                            if fresh and fresh != cookiefile:
                                cookiefile = fresh
                                old_opts = opts
                                opts = _make_opts(None, cookiefile)
                                self._copy_engine_retry_state(opts, old_opts)
                                opts['progress_hooks'] = [hook] + base_hooks
                                logger.info("[auth] retrying with refreshed cookies")
                                keep_lock_for_retry = True
                                continue
                    # 403/429 backoff: yt-dlp re-raises rate-limit errors with no backoff of its own, and a
                    # retry storm pushes googlevideo into a ~60s+ all-403 forbid state. Back off before the
                    # planner's fresh-URL retry so we don't throttle ourselves: a short 3s rung (whose
                    # fresh URL clears an expired-URL 403), then a patient ~60s rung before the final
                    # attempt (a per-IP forbid only clears by waiting — re-extract can't help it). The wait
                    # is cancel-aware and shows a heartbeat. Skip on the final attempt (no further retry).
                    if attempt < 2 and YouTubeDownloader._classify_error(e) == 'ratelimit':
                        delay = YouTubeDownloader._ratelimit_backoff_seconds(attempt)
                        logger.warning(f"[Speed] HTTP 403/429 rate-limit — backing off {delay:.0f}s before "
                                       f"retry ({attempt + 2}/3).")
                        self._ratelimit_wait(delay)
                    new_opts = self._next_opts_on_error(
                        e, attempt, opts,
                        cookies_loaded=bool(cookies_from_browser or cookiefile),
                        cookiefile=cookiefile
                    )
                    if new_opts is not None:
                        opts = new_opts
                        opts['progress_hooks'] = [hook] + base_hooks
                        keep_lock_for_retry = True
                        continue
                    logger.error(f"[ERROR] Download failed for {url}: {e}")
                    self._set_last_error(
                        self._download_error_message(e, cookies_from_browser, browser_cookie_failed),
                        self._classify_error(e))
                    return []
                finally:
                    # Preserve the first authoritative lock only for an intentional next attempt.
                    # Any terminal return/raise — including one from auth/error planning itself —
                    # releases the active/waiter reference.
                    if not keep_lock_for_retry:
                        _release_output_lock()
            except BaseException:
                # KeyboardInterrupt/SystemExit and cancellation-like BaseExceptions must not strand
                # a registry reference or leave the extracted identity locked.
                _release_output_lock()
                raise

        logger.error(f"[ERROR] Download failed for {url} after retries (auth/bot/format). "
                     f"Tip: export cookies.txt or install a JS runtime (deno/node) for nsig solving.")
        self._set_last_error(
            self._download_error_message(last_exc, cookies_from_browser, browser_cookie_failed),
            self._classify_error(last_exc))
        _release_output_lock()
        return []

    @staticmethod
    def _download_error_message(exc, cookies_from_browser, browser_cookie_failed):
        """Turn a raw download failure into a short, actionable message for the UI/queue."""
        category = YouTubeDownloader._classify_error(exc)
        # The browser-cookie-load hint (DPAPI / locked DB) is the actionable message WHEN cookies would
        # actually help: an auth gate (bot/agegate) a readable DB would have cleared, or an unclassified
        # failure. But when the FINAL cause is one cookies can't fix (format/ratelimit/network), surface
        # THAT instead of a wrong "close Chrome" (browser_cookie_failed latch-hijack fix, P2).
        if (browser_cookie_failed and cookies_from_browser
                and category not in ('format', 'ratelimit', 'network')):
            b = cookies_from_browser
            return (f"Couldn't read {b} cookies — on Windows the browser locks its cookie database while open. "
                    f"Fully close {b} (end any background {b} processes in Task Manager), then retry — "
                    f"or use a cookies.txt file instead.")
        if category == 'agegate':
            return ("This video is age-restricted — YouTube requires a signed-in session. "
                    "Run: python yt_final.py auth login  (browser login) — or — "
                    "python yt_final.py auth import <cookies.txt>  — then retry.")
        if category == 'bot':
            return ("YouTube bot-check: “Sign in to confirm you're not a bot.” Your IP is temporarily "
                    "rate-limited from many requests. Add cookies (a cookies.txt from a logged-in browser, or "
                    "close your browser and use cookies-from-browser), or wait ~30-60 min and retry. "
                    "Run: python yt_final.py export-cookies for a step-by-step cookies.txt guide.")
        if category == 'network':
            # 'network' is a classified category (_classify_error) but had no branch here, so a
            # down link surfaced raw `getaddrinfo failed` text while every other category got a
            # written remedy. Separate DNS from a proxy/TLS problem: the fixes are different.
            low = str(exc).lower()
            if 'getaddrinfo' in low or 'name or service not known' in low or 'nodename' in low:
                return ("Could not resolve YouTube's address — this looks like DNS, not YouTube. "
                        "Check your connection, VPN, or DNS server, then retry.")
            if 'proxy' in low or 'ssl' in low or 'certificate' in low or 'tls' in low:
                return ("The connection was refused or intercepted before reaching YouTube — check "
                        "any proxy, VPN, or TLS-inspecting antivirus, then retry.")
            return ("Network error reaching YouTube (timeout or connection reset). The tool already "
                    "retried; check your connection and try again.")
        if category == 'format':
            return "No matching format available — try a different quality/codec, or 'best'."
        if category == 'ratelimit':
            return ("YouTube is rate-limiting or soft-blocking this IP (HTTP 403/429). The tool backed "
                    "off and retried; if it keeps failing, wait ~1–2 min and try again, or turn on the "
                    "bypass proxy in the Network tab to route around it.")
        if exc:
            return f"Download failed: {str(exc)[:240]}"
        return "Download failed after retries (auth / bot-check / format)."

    @staticmethod
    def _report_speed(paths, elapsed, transfer_bps=None):
        """Log total bytes for the just-finished download. `elapsed` is END-TO-END — it includes this
        attempt's extract_info + the 137+140 merge/remux — so mib/elapsed UNDERSTATES the transfer rate
        and is labelled the 'job avg'. When transfer samples exist, the true media-transfer rate
        (yt-dlp's own per-callback speed mean) is logged alongside as the honest throughput figure."""
        try:
            total = sum(p.stat().st_size for p in paths if p and p.exists())
            if total and elapsed > 0:
                mib = total / (1024 * 1024)
                msg = (f"[Speed] Downloaded {mib:.1f} MiB in {elapsed:.1f}s "
                       f"= {mib/elapsed:.1f} MiB/s job avg (incl. extract+merge)")
                if transfer_bps:
                    tx = transfer_bps / (1024 * 1024)
                    msg += f"; ~{tx:.1f} MiB/s transfer ({tx*8:.0f} Mbit/s)"
                logger.info(msg)
        except Exception:
            logger.debug("Speed report failed (non-fatal)")

    def download_parallel(self, urls, quality="1080p", max_workers=None, speed_priority=False,
                          cookies_from_browser=None, cookiefile=None, codec=None, section=None,
                          slow_requests=False, source_address=None, spec=None, _post_download=None):
        # Public entry point: callers can bypass FinalYouTubeGPUEngine.run(), so canonicalize before
        # either the speed-priority loop or ThreadPoolExecutor submission. Materializing here also
        # makes one-shot generators safe for the later ordered error reporting passes.
        urls = dedupe_download_urls(urls)
        # Floor at 1 AND cap at 16: a caller-supplied max_workers of 0/-1 (e.g. from JobSpec.workers)
        # must not reach ThreadPoolExecutor(max_workers<=0) (raw ValueError), and an explicit large
        # value from a public API client (workers=500) must not spawn 500 threads — it bypassed the
        # 16 ceiling __init__ applies to the default. Capping costs nothing on the normal path:
        # concurrent media flows 403 on the per-IP gate anyway, so >16 never buys throughput.
        workers = max_workers or self.max_workers
        workers = max(1, min(workers, 16))
        results = []
        if speed_priority:
            # Speed-priority runs per-URL serial quality fallbacks; it never submits to an executor,
            # so don't spin one up (the old code created+tore down a ThreadPoolExecutor for nothing).
            for u in urls:
                res = self._download_with_priority(u, quality, cookies_from_browser=cookies_from_browser,
                                                   cookiefile=cookiefile, codec=codec, section=section,
                                                   slow_requests=slow_requests, source_address=source_address,
                                                   spec=spec, _post_download=_post_download)
                results.extend(res or [])
        else:
            # Warm the lazy probe caches ONCE up front so the worker threads below share a single
            # probe result instead of each racing to re-run the same subprocess/network probes.
            self._warm_probe_caches(spec)
            errors = {}  # url -> (message, kind), captured per-worker from the thread-local (race-free)
            batch_state_guard = threading.Lock()
            terminal_failure = []

            def _close_batch(exc):
                """Atomically transition the batch from open to its first terminal failure."""
                with batch_state_guard:
                    if not terminal_failure:
                        terminal_failure.append(exc)
                    return terminal_failure[0]

            def _first_batch_failure():
                """Return the canonical terminal object without changing an open batch."""
                with batch_state_guard:
                    return terminal_failure[0] if terminal_failure else None

            def _claim_batch_admission():
                """Linearize transfer/callback admission against terminal closure."""
                with batch_state_guard:
                    if terminal_failure:
                        raise terminal_failure[0]

            def _run_one(u):
                # Clear this thread's slot first: a reused pool thread must not report a prior URL's
                # error for a URL whose download() returns early without setting one (e.g. invalid URL).
                self._tls.last_error = None
                self._tls.last_error_kind = None
                post_failures = []

                def _guarded_post_download(paths):
                    try:
                        # Hold the state mutex only for the admission claim. Distinct callbacks
                        # that claim while the batch is open remain free to run concurrently.
                        _claim_batch_admission()
                    except BaseException as exc:
                        post_failures.append(exc)
                        raise exc
                    try:
                        return _post_download(paths)
                    except BaseException as exc:
                        canonical = _close_batch(exc)
                        post_failures.append(canonical)
                        raise canonical

                try:
                    # A worker may dequeue another submitted URL before the collector can
                    # cancel its Future. Recheck the same terminal state here so that URL
                    # never begins transfer work after a peer has already cancelled.
                    _claim_batch_admission()
                    res = _call_with_post_download_contract(
                        self.download,
                        _guarded_post_download if _post_download is not None else None,
                        u, quality, cookies_from_browser, cookiefile, codec,
                        section, slow_requests, source_address, spec,
                    ) or []
                except BaseException as exc:
                    if post_failures:
                        raise _PostDownloadFailure(post_failures[0])
                    if isinstance(exc, _PostDownloadContractError):
                        canonical = _close_batch(exc)
                        raise _PostDownloadFailure(canonical)
                    # Raw cooperative cancellation and every non-Exception BaseException
                    # close the state in the worker before its Future becomes observable.
                    # A concurrently admitted ordinary Exception also yields the already
                    # canonical terminal object instead of racing it at the collector.
                    if (not isinstance(exc, Exception)
                            or isinstance(exc, DownloadCancelled)
                            or type(exc).__name__ == 'DownloadCancelled'
                            or 'YT_FINAL_CANCEL' in str(exc)):
                        canonical = _close_batch(exc)
                        raise canonical
                    canonical = _first_batch_failure()
                    if canonical is not None:
                        raise canonical
                    raise
                return (res, getattr(self._tls, 'last_error', None),
                        getattr(self._tls, 'last_error_kind', None))

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_run_one, u): u for u in urls}
                for fut in as_completed(futs):
                    u = futs[fut]
                    try:
                        res, err, kind = fut.result()
                        if res:
                            results.extend(res)
                        else:
                            # Record EVERY url that produced no file — even one that returned empty
                            # without setting a thread-local error — so it can't vanish from
                            # partial_failures just because no message was captured.
                            errors[u] = (err or 'no file produced (bot-check / no matching format)', kind)
                    except BaseException as e:
                        canonical = None
                        if isinstance(e, _PostDownloadFailure):
                            canonical = _close_batch(e.cause)
                        # A cooperative cancel must ABORT the whole batch, not get demoted to a per-URL
                        # error: DownloadCancelled subclasses Exception, so without this it would land in
                        # errors[u] and the batch would still return the survivors — run() would then
                        # record history/webhook 'done' while the journal resolves 'cancelled' (three
                        # channels, two verdicts). Re-raise so run() reports 'cancelled' in agreement with
                        # the queue row (mirrors _download_with_priority's cancel escape). The 3-pronged
                        # check catches the cross-module twin/string forms too, not just the in-process class.
                        elif (not isinstance(e, Exception)
                                or isinstance(e, DownloadCancelled)
                                or type(e).__name__ == 'DownloadCancelled'
                                or 'YT_FINAL_CANCEL' in str(e)):
                            canonical = _close_batch(e)
                        else:
                            # A worker can have been admitted before a peer closes the batch.
                            # If its ordinary Exception becomes visible later, the first terminal
                            # failure still owns the batch outcome.
                            canonical = _first_batch_failure()

                        if canonical is not None:
                            for pending in futs:
                                if pending is not fut:
                                    pending.cancel()
                            raise canonical
                        # Keep going on the other URLs, but surface WHICH url failed (was swallowed before).
                        logger.error(f"Parallel download error for {u}: {e}")
                        errors[u] = (str(e), YouTubeDownloader._classify_error(e))

            # Partial failure: some URLs produced files, others didn't. The all-failed branch below is
            # the only place last_error gets set (consumers read it only on an empty result), so a batch
            # that succeeds on the survivors would otherwise DISCARD the losers' errors silently — the
            # user gets N files back with no hint that M of their requested URLs failed. Surface each
            # dropped URL in the log so a --parallel batch never hides what it couldn't fetch. (Uses the
            # errors map already collected above; post-download only, nothing on the transfer path.)
            if results and errors:
                # Also carry the dropped URLs into the durable job meta (result_meta -> the job card),
                # so partial failure is visible in the UI/API, not just the server log. The batch still
                # reports 'done' on its survivors (return contract unchanged) — this just stops the
                # failures from vanishing from the structured result. Reset per job in _run_body.
                self.last_partial_errors = [(u, errors[u]) for u in urls if u in errors]
                for u in urls:
                    if u in errors:
                        logger.warning(f"[Parallel] {u} failed and is not in the output: {errors[u][0]}")

            # Deterministically report the batch's failure reason. The instance attributes were
            # written racily by the worker threads (whichever finished last won arbitrarily); overwrite
            # them here from the per-URL map, in original URL order, so the engine/server report is
            # stable and correctly attributed. Consumers read last_error only on an empty result.
            if not results and errors:
                ordered = [(u, errors[u]) for u in urls if u in errors]
                if len(ordered) == 1:
                    self.last_error, self.last_error_kind = ordered[0][1]
                else:
                    self.last_error = " | ".join(f"{u}: {m}" for u, (m, _k) in ordered)
                    kinds = {k for _u, (_m, k) in ordered}
                    self.last_error_kind = kinds.pop() if len(kinds) == 1 else 'multiple'
        return results

    def _download_with_priority(self, url, quality, cookies_from_browser=None, cookiefile=None, codec=None, section=None, slow_requests=False, source_address=None, spec=None, _post_download=None):
        """Internal for speed priority fallbacks (do NOT lower quality except when speed_priority explicitly chosen)"""
        fallbacks = [quality]
        if quality in ["best", "4k", "1440p", "1080p"]:
            fallbacks += ["1080p", "720p", "480p"]
        # dedup preserve order
        fallbacks = list(dict.fromkeys(fallbacks))
        for q in fallbacks:
            post_failures = []

            def _guarded_post_download(paths):
                try:
                    return _post_download(paths)
                except BaseException as exc:
                    post_failures.append(exc)
                    raise

            try:
                res = _call_with_post_download_contract(
                    self.download,
                    _guarded_post_download if _post_download is not None else None,
                    url,
                    q,
                    cookies_from_browser=cookies_from_browser,
                    cookiefile=cookiefile,
                    codec=codec,
                    section=section,
                    slow_requests=slow_requests,
                    source_address=source_address,
                    spec=spec,
                )
                if res:
                    if q != quality:
                        logger.info(f"Used fallback quality {q} for speed")
                    return res
            except Exception as e:
                if post_failures:
                    raise post_failures[0]
                if isinstance(e, _PostDownloadContractError):
                    raise
                # A cooperative cancel must escape immediately — never fall through to a lower
                # fallback quality (that ignores the cancel and can hit another 60s rate-limit wait).
                if (isinstance(e, DownloadCancelled)
                        or type(e).__name__ == 'DownloadCancelled'
                        or 'YT_FINAL_CANCEL' in str(e)):
                    raise
                logger.warning(f"  Fallback {q} failed: {e}")
        return []

    def _validate_url(self, url):
        return normalize_youtube_url(url) is not None

    @staticmethod
    def _ratelimit_backoff_seconds(attempt):
        """Backoff before a 403/429 retry, in two rungs. attempt 0 -> 3s: a short retry re-extracts a
        fresh signed URL, which clears an EXPIRED-url 403. attempt 1+ -> 60s: if the fresh URL ALSO
        403s it is a per-IP forbid (measured 2026-07: the leapfrog gate got an instant 403 regardless
        of connection count), which ONLY waiting clears — re-extract can't help, and googlevideo's
        forbid state lasts ~60s+. Bounded at 60s so an unattended job can't hang indefinitely; a
        still-forbidden job then fails with an actionable message rather than blocking forever."""
        return 3.0 if attempt <= 0 else 60.0

    def _ratelimit_wait(self, delay):
        """Wait `delay`s before a 403/429 retry, in ~1s slices. Each slice pushes a heartbeat to
        progress_sink so the pause is visible ('retrying in Ns') AND cancel-responsive — the sink
        raises DownloadCancelled when the job is cancelled, which we propagate. With no sink attached
        (CLI), it degrades to a plain bounded sleep. Never passes time.sleep a negative value."""
        sink = getattr(self, 'progress_sink', None)
        end = time.time() + max(0.0, delay)
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                break
            # A 60s rate-limit wait is the longest a cancel could otherwise go unobserved on the
            # CLI, which has no sink to translate one.
            if _cancel_pending(getattr(self, 'cancel_check', None)):
                raise DownloadCancelled('YT_FINAL_CANCEL')
            if sink:
                try:
                    sink({'status': 'downloading', 'percent': None,
                          'notice': f'Rate-limited by YouTube — retrying in {max(1, int(remaining + 0.5))}s '
                                    f'(waiting out the per-IP limit).'})
                except Exception as e:
                    if (isinstance(e, DownloadCancelled)
                            or type(e).__name__ == 'DownloadCancelled'
                            or 'YT_FINAL_CANCEL' in str(e)):
                        raise DownloadCancelled('YT_FINAL_CANCEL') from e
                    # A non-cancel sink error must not abort the backoff.
            time.sleep(min(1.0, remaining))

    def _next_opts_on_error(self, err, attempt, opts, *, cookies_loaded=False, cookiefile=None):
        """Pure retry planner (table-driven). On bot: swap client but KEEP cookies. Never drop loaded cookies on bot.
        Returns new opts or None to stop.
        """
        err_lower = str(err).lower()
        # NB: the real bot-challenge message contains the literal "--cookies", so a bare 'cookie'
        # keyword here would mis-classify a bot error as a cookie-DB error and strip cookies before
        # is_bot ever runs. Keep is_cookie_db narrowed to genuine cookie *database* failures, and
        # handle is_bot FIRST below so a bot challenge can never fall into the cookie-strip branch.
        is_cookie_db = any(x in err_lower for x in ['dpapi', 'decrypt', 'failed to decrypt', 'failed to load cookie', 'could not copy', 'cookie database'])
        # Keep in sync with _classify_error()'s format bucket (the single source of truth): only
        # genuine format problems. Bare 'not available'/'no formats' also match geo/region blocks
        # ("...not available in your country"), which a format-fallback retry can't fix — matching
        # them here just burns a retry attempt and diverges from the user-facing classifier.
        is_format = ('format' in err_lower or 'requested format' in err_lower)
        is_bot = ('bot' in err_lower or 'sign in' in err_lower or 'confirm you' in err_lower)

        # LAZY-mode PO escalation (opt-in `pot_mode='lazy'` only): stage 1 ran android_vr-only (no PO
        # fetch, ~2.8s faster). If it failed in a way mweb+PO can fix, retry ONCE with the full PO
        # client list (the provider is already wired into extractor_args by _apply_pot_mode). Placed
        # before the generic branches so the first retry escalates; a further failure then falls through
        # to the normal planner. `bot` is intentionally EXCLUDED here — the tuned is_bot branch below
        # (embedded/tv rotation) handles it. Only escalate when a provider actually exists, else it is a
        # no-op that just burns an attempt. The stash key is `_engine*` so it's stripped before yt-dlp.
        if getattr(self, 'pot_mode', 'auto') == 'lazy' and not opts.get('_engine_po_escalated') and not is_bot:
            kind = YouTubeDownloader._classify_error(err)
            needs_po = (kind in ('agegate', 'format', 'ratelimit')
                        or 'storyboards-only' in err_lower or 'no video formats' in err_lower)
            if needs_po and self._pot_provider_status()[0] is not None:
                new_opts = dict(opts)
                ea = {k: (dict(v) if isinstance(v, dict) else v)
                      for k, v in (opts.get('extractor_args') or {}).items()}
                ea.setdefault('youtube', {})['player_client'] = list(self.CLIENTS_WITH_POT)
                new_opts['extractor_args'] = ea
                new_opts['_engine_po_escalated'] = True
                new_opts['progress_hooks'] = opts.get('progress_hooks', [])
                logger.info(f"[Speed] lazy mode: {kind} on android_vr -> escalating to mweb+PO clients")
                return new_opts

        if is_bot:
            new_opts = dict(opts)
            # keep cookies if loaded; rotate to embedded/tv clients but PRESERVE pot args so the PO
            # token is still applied. A full extractor_args replacement would silently drop the
            # bgutilhttp token and revert to SABR/HLS on web_safari, capping speed at 3-8 MiB/s.
            ea = {k: (dict(v) if isinstance(v, dict) else v)
                  for k, v in (opts.get('extractor_args') or {}).items()}
            # Rotate to embedded/tv clients for the bot retry; keep everything else (pot args).
            ea.setdefault('youtube', {})['player_client'] = ['tv_embedded', 'web_embedded', 'ios', 'tv', 'web_safari']
            new_opts['extractor_args'] = ea
            # Deep-copy http_headers so updating the User-Agent here never aliases/mutates the
            # caller's original headers dict (a shallow dict(opts) shares the same dict object).
            new_opts['http_headers'] = dict(opts.get('http_headers') or {})
            new_opts['http_headers']['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            new_opts['progress_hooks'] = opts.get('progress_hooks', [])
            if attempt >= 1:
                # on repeated bot, also fall back to a safe low-res format that is more likely to be available anonymously
                new_opts['format'] = '18/best[height<=480]'
            return new_opts

        if is_cookie_db and (cookies_loaded or cookiefile):
            new_opts = dict(opts)
            new_opts.pop('cookiesfrombrowser', None)
            new_opts.pop('cookiefile', None)
            new_opts['progress_hooks'] = opts.get('progress_hooks', [])
            return new_opts

        if is_format:
            new_opts = dict(opts)
            new_opts['format'] = 'bestvideo*+bestaudio/best'
            new_opts['progress_hooks'] = opts.get('progress_hooks', [])
            return new_opts

        # 403/429 rate-limit / soft-block: retry (the loop re-extracts a fresh signed URL) rather than
        # aborting. Opts are left unchanged — a fresh URL is what the retry needs. The bounded backoff
        # sleep is applied by the retry loop so this planner stays pure/side-effect-free. Keep the
        # keyword set in sync with _classify_error()'s 'ratelimit' bucket. is_bot is handled first
        # above, so a bot-challenge 403 still rotates clients instead of landing here.
        is_ratelimit = any(s in err_lower for s in
                           ('http error 429', 'too many requests', 'http error 403', ': forbidden'))
        if is_ratelimit:
            new_opts = dict(opts)
            new_opts['progress_hooks'] = opts.get('progress_hooks', [])
            return new_opts

        # ThrottledDownload: in engine/API mode a transient dip below throttledratelimit ABORTS
        # the job ("download speed is below throttle limit"). Retry once without the throttle floor
        # so a legitimately-slow-but-working transfer can finish instead of failing outright.
        if 'throttle' in err_lower or 'below throttle limit' in err_lower:
            new_opts = dict(opts)
            new_opts.pop('throttledratelimit', None)
            new_opts['progress_hooks'] = opts.get('progress_hooks', [])
            return new_opts

        # Transient network error (reset / timeout / name-resolution blip during the brief extraction
        # burst): re-extract via the outer loop, mirroring the ratelimit branch. extractor_retries is
        # cut to 1 on the premise that THIS loop retries — but without this branch a single reset
        # hard-fails, which bites flaky mobile-CDN links (Verizon 5G). Opts unchanged; no backoff (a
        # reset is not a per-IP forbid). Kept in sync with _classify_error()'s 'network' bucket, and
        # narrowed to clearly-transient tokens (not bare 'ssl'/'connection') to avoid retrying hard
        # failures like a genuine cert error.
        is_network = any(s in err_lower for s in
                         ('timed out', 'timeout', 'reset by peer', 'connection reset',
                          'connection aborted', 'temporary failure', 'getaddrinfo', 'unreachable'))
        if is_network:
            new_opts = dict(opts)
            new_opts['progress_hooks'] = opts.get('progress_hooks', [])
            return new_opts

        return None


# =============================================================================
# 5. FINAL ENGINE (orchestrator)
# =============================================================================
class FinalYouTubeGPUEngine:
    def __init__(self, output_dir="./downloads", pot_base_url=None, pot_mode='auto'):
        self.detector = get_detector()
        self.downloader = YouTubeDownloader(self.detector, output_dir,
                                            pot_base_url=pot_base_url, pot_mode=pot_mode)
        self.processor = FFmpegProcessor(self.detector)
        # Serializes the shared downloader.output_path swap+restore in run() so a hypothetical
        # concurrent run() on the same engine can't race on the (shared) output path.
        self._run_output_lock = threading.Lock()
        # Persistent history ledger: every run() outcome is appended (best-effort). Built lazily on
        # first use so library/test callers can disable it (set _history_enabled=False) and so we
        # don't litter ~/.yt_final during unit tests that never finish a real job.
        self._history_enabled = True
        self._history = None
        self._notification_dispatcher = NotificationDispatcher(
            self._production_notification_sender()
        )
        self._notification_futures = set()
        self._notification_futures_lock = threading.Lock()
        logger.info(f"FinalYouTubeGPUEngine ready (output: {output_dir})")

    def _record_history(self, spec, result, status=None):
        """Append one history row per produced output path (or one row if none). Best-effort:
        a ledger write must NEVER fail a job, mirroring the fire-and-forget webhook/prune posture.
        `result` is the list of output paths run() returns, or None on the exception path. `status`
        overrides the derived done/failed verdict — the cancel path passes 'cancelled' so the ledger
        AGREES with the queue outcome instead of mislabelling a user cancel as a failure."""
        if not self._history_enabled:
            return
        try:
            if self._history is None:
                self._history = _get_history_cls()()
            url = (spec.urls[0] if spec.urls else None)
            outs = list(result or [])
            if status is None:
                status = 'done' if outs else 'failed'
            if outs:
                _actual = getattr(self, '_actual_profiles', {})
                for p in outs:
                    self._history.record(url=url, output=str(p),
                                         profile=(_actual.get(str(p)) or spec.profile),
                                         status=status, spec=spec)
            else:
                self._history.record(url=url, output=None, profile=spec.profile,
                                     status=status, spec=spec)
        except Exception:
            pass

    def download_then_process(self, urls, quality="1080p", encode_mode="copy", profile="auto_h264",
                              parallel=False, workers=None, speed_priority=False, cookies_from_browser=None,
                              cookiefile=None, codec=None, section=None, slow_requests=False, source_address=None):
        """Back-compat adapter. The 13-arg legacy signature is preserved forever (CLI/menu/tests
        call it) but it now just builds a JobSpec and delegates to run() — keystone K1."""
        spec = JobSpec(
            urls=[urls] if isinstance(urls, str) else list(urls),
            quality=quality, encode_mode=encode_mode, profile=profile, parallel=parallel,
            workers=workers, speed_priority=speed_priority, cookies_from_browser=cookies_from_browser,
            cookiefile=cookiefile, codec=codec, section=section, slow_requests=slow_requests,
            source_address=source_address,
        )
        return self.run(spec)

    def run(self, spec, on_notification=None):
        """Execute a JobSpec end to end: download -> finalize (encode/remux + metadata + chapters +
        subtitles + loudnorm + HDR + thumbnail) -> on_complete. The single entry point shared by the
        CLI, the batch manifest and the HTTP server."""
        if not isinstance(spec, JobSpec):
            # Parse OUTSIDE the run lock: JobSpec.from_dict() runs dict(spec), i.e. arbitrary
            # __iter__/keys code for a caller-supplied mapping; doing that while HOLDING the
            # non-reentrant _run_output_lock would DEADLOCK if such a mapping re-entered run() on this
            # same engine. On a parse failure, clear stale partials UNDER the lock (so a failed-to-start
            # job can't surface a prior job's meta, and no concurrent run() races on the write) then
            # propagate. Web/CLI always pass a real JobSpec, so this branch is the raw-dict path only.
            try:
                spec = JobSpec.from_dict(spec)
            except Exception:
                with self._run_output_lock:
                    self.downloader.last_partial_errors = None
                raise
        dl = self.downloader
        # Hold the per-engine run lock around the whole job — the partial-failure reset, the output
        # swap, the body and the restore — so a hypothetical concurrent run() on the same engine
        # serializes fully and can't reset/clobber or inherit another job's partial-failure list.
        with self._run_output_lock:
            # Clear the prior job's partial-failure list at the START of this job's exclusive section,
            # before the empty-URL early return, so a no-URL/early-return job can't surface a stale
            # partial_failures inherited from a previous job (the downloader is reused across jobs).
            dl.last_partial_errors = None
            # Same reasoning as above: these are recorded on the SHARED downloader, so a new job must
            # never inherit the previous job's fallback/degradation and report it as its own.
            dl.last_output_dir_fallback = None
            dl.last_degraded = None
            self._actual_profiles = {}   # per-job {final_output_path: profile that actually ran (post-degradation)}
            # Collapse only confirmed single-video aliases (plus exact/full-normalized URL repeats)
            # before ANY worker/prefetch handoff. Playlist/list URLs stay full-URL keyed.
            _raw_urls = list(spec.urls or [])
            urls = dedupe_download_urls(_raw_urls)
            if len(urls) != len(_raw_urls):
                logger.warning(f"[Run] dropped {len(_raw_urls) - len(urls)} duplicate URL(s) from this job")
            if not urls:
                logger.warning("[Run] JobSpec has no URLs")
                return []
            # Surface accepted-but-not-yet-honored knobs (reserved A7 two_pass/bitrate) so the contract
            # never silently no-ops — shared by CLI, batch manifest and the HTTP server via this method.
            for _w in spec.config_warnings():
                logger.warning(f"[Run] {_w}")
            # Per-job save location (web UI / manifest / queue). Redirect the downloader's output path
            # for the duration of this job, then always restore it (the engine's downloader is shared).
            prev_out = dl.output_path
            if spec.output_dir:
                try:
                    dest = Path(spec.output_dir).expanduser()
                    dest.mkdir(parents=True, exist_ok=True)
                    dl.output_path = dest
                    logger.info(f"[Run] saving to {dest}")
                except OSError as e:
                    logger.warning(f"[Run] cannot use output_dir '{spec.output_dir}' ({e}); keeping {prev_out}")
                    dl.output_path = prev_out
                    # Surface it on the job card too — a log line the user never opens is not a report.
                    dl.last_output_dir_fallback = {
                        'requested': str(spec.output_dir), 'used': str(prev_out), 'error': str(e),
                    }
            try:
                _t0 = time.time()
                try:
                    result = self._run_body(spec, urls, dl)
                except Exception as e:
                    # A cancellation is NOT a failure. Without this split, run() logged a cancelled
                    # job to history as 'failed' and fired the webhook 'failed', while the web worker
                    # separately committed the queue row as 'cancelled' — three channels, two verdicts.
                    # Record/notify 'cancelled' on a cancel so the ledger and webhook AGREE with the
                    # queue outcome. Still re-raise unchanged so the worker's own cancel handling runs.
                    is_cancel = (isinstance(e, DownloadCancelled)
                                 or type(e).__name__ == 'DownloadCancelled'
                                 or 'YT_FINAL_CANCEL' in str(e))
                    # Record the outcome to the persistent ledger before propagating (every caller —
                    # CLI download, batch, queue, web — flows through run(), so this one chokepoint
                    # covers all completion sites).
                    self._record_history(spec, None, status='cancelled' if is_cancel else 'failed')
                    self._track_notification(self._spawn_notify(
                        spec, ok=False, files=[], error='' if is_cancel else str(e),
                        elapsed=round(time.time() - _t0, 1),
                        status='cancelled' if is_cancel else 'failed',
                        on_result=on_notification,
                    ))
                    raise
                self._record_history(spec, result)
                files = [str(p) for p in (result or [])]
                self._track_notification(self._spawn_notify(
                    spec, ok=bool(result), files=files, error='',
                    elapsed=round(time.time() - _t0, 1),
                    status='ok' if result else 'empty',
                    on_result=on_notification,
                ))
                return result
            finally:
                dl.output_path = prev_out
                # Drain the per-job metadata side-channel. last_info entries are written on every
                # SUCCESSFUL download (one per delivered path) and are normally removed by _finalize's
                # pop, or by the encode_mode='none' drain. Neither runs when the job never reaches its
                # processing loop — a cancel partway through a batch, or an exception between the
                # download and the loop — so entries for the URLs that already succeeded stay behind.
                # The web engine keeps ONE downloader for the whole process lifetime, so those persist
                # for the life of the server rather than the life of the job.
                #
                # run()'s finally on purpose: it is the only scope that sees every exit path, including
                # the parallel case where `downloaded` is never assigned because download_parallel
                # re-raised the cancel. Runs once per job, after the body, under the lock already held
                # — no contact with the transfer path or its progress hooks.
                try:
                    dl.last_info.clear()
                except Exception:
                    pass

    def simulate(self, spec):
        """Resolve and report the download plan for the first URL WITHOUT downloading, then return a
        plan dict. Distinct from batch --dry-run (which only prints spec.summary()). Routes all info
        extraction through the downloader's extract seam so tests can monkeypatch it."""
        if not isinstance(spec, JobSpec):
            spec = JobSpec.from_dict(spec)
        urls = list(spec.urls or [])
        if not urls:
            logger.warning("[Simulate] JobSpec has no URLs")
            return {'ok': False, 'error': 'no URLs'}
        url = urls[0]
        dl = self.downloader
        opts = dl.get_optimized_ydl_opts(
            spec.quality, audio_only=(spec.quality == 'audio'), codec=spec.codec,
            section=spec.section, spec=spec)
        chosen_format = opts.get('format')
        try:
            import yt_dlp
            opts2 = dict(opts)
            opts2['quiet'] = True
            opts2['progress_hooks'] = []
            opts2.pop('download_archive', None)   # simulate must be archive-independent (archived URL -> empty plan otherwise)
            with yt_dlp.YoutubeDL(opts2) as y:
                info = y.extract_info(url, download=False)
        except Exception as e:
            logger.error(f"[Simulate] {e}")
            return {'ok': False, 'error': str(e)}

        sel = self.downloader._summarize_selection(info or {})

        # container: copy -> source ext; reencode -> profile ext (mirror the Opus->.mkv switch).
        if spec.encode_mode == 'copy':
            container = '.' + (sel.get('ext') or 'mp4')
        elif spec.encode_mode == 'none':
            container = '.' + (sel.get('ext') or 'mp4')
        else:
            container = self.processor.output_ext(spec.profile)
            if spec.audio_codec in ('libopus', 'opus') and container.lower() == '.mp4':
                container = '.mkv'

        # output path (replicate _apply_spec_download_opts choices)
        if spec.output_template:
            out_path = spec.output_template
        elif spec.organize_by == 'uploader':
            out_path = str(dl.output_path / '%(uploader)s' / ('%(title)s [%(id)s]' + container))
        elif spec.organize_by == 'date':
            out_path = str(dl.output_path / '%(upload_date>%Y-%m-%d)s' / ('%(title)s [%(id)s]' + container))
        else:
            out_path = str(dl.output_path / ('%(title)s [%(id)s]' + container))

        # sidecars derived from spec flags
        sidecars = []
        if spec.sub_langs:
            _sf = spec.sub_format or 'srt'
            sidecars.extend(['.srt', '.vtt'] if _sf == 'both' else ['.vtt' if _sf == 'vtt' else '.srt'])
        if spec.embed_thumbnail or spec.write_artwork:
            sidecars.append('thumbnail')
        if spec.write_nfo:
            sidecars.append('.nfo')
        if spec.write_sidecar:
            sidecars.append('.info.json')
        if spec.write_sidecar_text:
            sidecars.append('.description.txt')
        if spec.export_markers:
            sidecars.append(f'markers:{spec.export_markers}')
        if spec.sponsorblock_markers:
            sidecars.append(f'sponsorblock-markers:{spec.sponsorblock_markers}')
        if spec.proxy_pair:
            sidecars.append('proxy(_proxy.mp4)')

        est = sel.get('filesize') or 0
        est_str = f"{est/1024/1024:.1f}MiB" if est else "unknown"
        plan = {
            'ok': True,
            'url': url,
            'title': (info or {}).get('title'),
            'id': (info or {}).get('id'),
            'chosen_format': chosen_format,
            'resolved_format_id': sel.get('format_id'),
            'resolution': (f"{sel['height']}p" if sel.get('height') else (sel.get('resolution') or 'max')),
            'vcodec': sel.get('vcodec'),
            'acodec': sel.get('acodec'),
            'est_size': est_str,
            'encode_mode': spec.encode_mode,
            'profile': spec.profile,
            'container': container,
            'output_path': out_path,
            'sidecars': sidecars,
        }
        logger.info("[Simulate] Plan (no download performed):")
        logger.info(f"[Simulate]   url={plan['url']}  title={(plan['title'] or '?')[:60]}")
        logger.info(f"[Simulate]   format='{plan['chosen_format']}' -> id={plan['resolved_format_id']} "
                    f"({plan['resolution']}, v={plan['vcodec'] or '?'}/a={plan['acodec'] or '?'}, ~{plan['est_size']})")
        logger.info(f"[Simulate]   encode_mode={plan['encode_mode']}  profile={plan['profile']}  container={plan['container']}")
        logger.info(f"[Simulate]   output={plan['output_path']}")
        logger.info(f"[Simulate]   sidecars={plan['sidecars'] or 'none'}")
        return plan

    def _spawn_notify(self, spec, *, ok, files, error, elapsed, status, on_result=None):
        """Submit a job-complete webhook through the bounded dispatcher."""
        if not getattr(spec, 'notify_url', None):
            return
        payload = {
            'event': 'job_complete',
            'ok': bool(ok),
            'status': status,
            'title': (spec.urls or [''])[0],
            'count': len(files or []),
            'files': list(files or []),
            'output': (files[0] if files else ''),
            'error': error or '',
            'elapsed': elapsed,
            'urls': list(spec.urls or []),
        }
        return self._notification_dispatcher.submit(
            spec.notify_url,
            payload,
            getattr(spec, 'notify_template', None) or 'raw',
            on_result=on_result,
        )

    def _track_notification(self, future):
        """Retain unfinished notification work only; tolerate legacy None-return seams."""
        if future is None or not hasattr(future, 'add_done_callback'):
            return None
        if future.done():
            return future
        lock = getattr(self, '_notification_futures_lock', None)
        pending = getattr(self, '_notification_futures', None)
        if lock is None or pending is None:
            return future
        with lock:
            if not future.done():
                pending.add(future)

        def _discard(completed):
            with lock:
                pending.discard(completed)

        future.add_done_callback(_discard)
        return future

    def flush_notifications(self, timeout=5.0):
        """Wait within one bounded budget for the dispatcher's current snapshot."""
        dispatcher = getattr(self, '_notification_dispatcher', None)
        if dispatcher is None:
            return True
        return dispatcher.flush(timeout=timeout)

    def shutdown_notifications(self, timeout=5.0):
        """Fence submissions and settle/cancel notification work within one budget."""
        dispatcher = getattr(self, '_notification_dispatcher', None)
        if dispatcher is None:
            return True
        return dispatcher.shutdown(timeout=timeout)

    @staticmethod
    def _production_notification_sender():
        """Build the terminable boundary used by every production webhook."""
        return DeadlineBoundSender(
            FinalYouTubeGPUEngine._fire_webhook,
            timeout=NOTIFICATION_DELIVERY_TIMEOUT_S,
        )

    @staticmethod
    def _fire_webhook_with_deadline(url, payload, template='raw'):
        return FinalYouTubeGPUEngine._production_notification_sender()(
            url,
            payload,
            template,
        )

    @staticmethod
    def guess_notify_template(url: str) -> str:
        """Infer discord|slack|ntfy|raw from a webhook URL (no network)."""
        from urllib.parse import urlparse
        u = (url or '').strip()
        low = u.lower()
        if 'discord.com/api/webhooks' in low or 'discordapp.com/api/webhooks' in low:
            return 'discord'
        if 'hooks.slack.com' in low:
            return 'slack'
        try:
            p = urlparse(u)
            host = (p.hostname or '').lower()
            path = (p.path or '').strip('/')
            if 'ntfy.sh' in host or host.startswith('ntfy.') or host.endswith('.ntfy.sh'):
                return 'ntfy'
            # self-hosted ntfy: single topic segment, no file-like path
            if path and '/' not in path and host and 'discord' not in host and 'slack' not in host:
                if 'ntfy' in host:
                    return 'ntfy'
        except Exception:
            pass
        return 'raw'

    @staticmethod
    def _vet_notify_url(url: str):
        """SSRF core: resolve ONCE, reject if any address is non-public, and return the vetted IP
        to pin the connection to. Returns (err_or_None, vetted_ip_or_None)."""
        import ipaddress
        import socket
        from urllib.parse import urlparse
        raw = (url or '').strip()
        if not raw:
            return 'notify URL is empty', None
        p = urlparse(raw)
        if p.scheme not in ('http', 'https'):
            return f'notify URL scheme must be http or https (got {p.scheme or "none"})', None
        host = p.hostname or ''
        if not host:
            return 'notify URL has no host', None
        try:
            infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == 'https' else 80))
        except (socket.gaierror, OSError):
            return f'could not resolve notify host: {host}', None
        if not infos:
            return f'could not resolve notify host: {host}', None
        vetted = None
        for _f, _t, _pr, _c, sa in infos:
            try:
                ip = ipaddress.ip_address(sa[0])
            except ValueError:
                continue
            if (ip.is_loopback or ip.is_link_local or ip.is_private
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return 'notify host resolves to a private/loopback/link-local address (blocked)', None
            if vetted is None:
                vetted = sa[0]
        if vetted is None:
            return f'could not resolve notify host: {host}', None
        return None, vetted

    @staticmethod
    def validate_notify_url(url: str) -> str | None:
        """SSRF gate for notify webhooks. Returns None if allowed, else a short error string.

        Only http/https; host must resolve; every resolved address must be public
        (not loopback / link-local / private / reserved / multicast / unspecified).
        """
        return FinalYouTubeGPUEngine._vet_notify_url(url)[0]

    @staticmethod
    def _build_notify_request(template, payload):
        """Map a job payload to (body_bytes, extra_headers) for the chosen channel format.
        Pure/no-network. raw = current generic JSON (default)."""
        import json as _json
        title = str(payload.get('title') or 'yt-ingest')
        status = str(payload.get('status') or '')
        elapsed = payload.get('elapsed')
        output = str(payload.get('output') or '')
        error = str(payload.get('error') or '')
        ok = bool(payload.get('ok'))
        line = f"{'OK' if ok else 'FAILED'}: {title}"
        if output:
            line += f"\n{output}"
        if elapsed is not None:
            line += f"\n{elapsed}s"
        if error:
            line += f"\nerror: {error}"
        if template == 'discord':
            body = _json.dumps({'content': line}).encode('utf-8')
            return body, {'Content-Type': 'application/json'}
        if template == 'slack':
            body = _json.dumps({'text': line}).encode('utf-8')
            return body, {'Content-Type': 'application/json'}
        if template == 'ntfy':
            # plain-text body + Title header (ntfy convention); ASCII-safe header
            hdr_title = title.encode('ascii', 'replace').decode('ascii')
            # strip CR/LF/control chars: a newline in a title would split/inject HTTP headers
            hdr_title = ''.join(c for c in hdr_title if ' ' <= c < '\x7f') or 'yt-ingest'
            return line.encode('utf-8'), {'Content-Type': 'text/plain; charset=utf-8',
                                          'Title': hdr_title}
        # raw (default): current generic JSON payload
        return _json.dumps(payload).encode('utf-8'), {'Content-Type': 'application/json'}

    @staticmethod
    def _fire_webhook(url, payload, template='raw'):
        """Contain and sanitize every transport phase, including pre-open work."""
        try:
            return FinalYouTubeGPUEngine._fire_webhook_transport(
                url,
                payload,
                template,
            )
        except BaseException as exc:
            safe = safe_notification_origin(url)
            error = sanitize_notification_error(
                f"{type(exc).__name__}: {exc}",
                url,
            )
            logger.warning(
                "[Notify] webhook POST to %s failed: %s",
                safe,
                type(exc).__name__,
            )
            return False, error

    @staticmethod
    def _fire_webhook_transport(url, payload, template='raw'):
        """POST a JSON job-complete payload (safe alternative to the on_complete shell hook). Non-fatal.

        SSRF-hardened: only http/https schemes are allowed; the host is RESOLVED and rejected if ANY
        resolved address is private/loopback/link-local/reserved/multicast (so a DNS name pointing at
        an internal IP can't slip past an IP-literal check); redirects are disabled so a 30x can't
        bounce to an internal host.

        Returns (ok: bool, error: str|None). Job completion path ignores the result; /api/notify-test
        uses it so the UI does not claim success when the POST failed.
        """
        import urllib.request
        import http.client
        import socket
        from urllib.parse import urlparse
        err, vetted_ip = FinalYouTubeGPUEngine._vet_notify_url(url)
        if err:
            safe_error = sanitize_notification_error(err, url)
            logger.warning('[Notify] rejected: %s', safe_error)
            return False, safe_error
        _pp = urlparse(url.strip())
        _host = _pp.hostname
        _safe = safe_notification_origin(url)

        # Pin the socket to the vetted IP so a DNS rebind between validation and send can't redirect
        # us to an internal host. The Host header and TLS SNI/cert verification still use the real host.
        class _PinHTTP(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.create_connection((vetted_ip, self.port), self.timeout)
        class _PinHTTPS(http.client.HTTPSConnection):
            def connect(self):
                s = socket.create_connection((vetted_ip, self.port), self.timeout)
                self.sock = self._context.wrap_socket(s, server_hostname=_host)
        class _PinHTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(_PinHTTP, req)
        class _PinHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(_PinHTTPS, req)

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None   # never follow a 30x -> can't be bounced to an internal host
        opener = urllib.request.build_opener(_PinHTTPHandler, _PinHTTPSHandler, _NoRedirect)
        tmpl = template if template in ('discord', 'slack', 'ntfy', 'raw') else 'raw'
        data, extra_headers = FinalYouTubeGPUEngine._build_notify_request(tmpl, payload)
        _MAX_NOTIFY_BODY = 64 * 1024
        if len(data) > _MAX_NOTIFY_BODY:
            msg = 'payload too large; skipping'
            logger.warning(f'[Notify] {msg}')
            return False, msg
        headers = {'User-Agent': 'yt-ingest/notify'}
        headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            opener.open(req, timeout=5.0)
            # Redact the URL in the log: a Discord/Slack webhook embeds a posting SECRET in its path/
            # query, and the log file is world-readable — log only scheme://host[:port].
            logger.info(f'[Notify] {tmpl} POSTed to {_safe}')
            return True, None
        except Exception as e:
            # Exception messages from urllib may include the full Request URL. Preserve the safe
            # destination and exception type for diagnosis without copying a posting secret to disk.
            logger.warning(f'[Notify] webhook POST to {_safe} failed: {type(e).__name__}')
            return False, sanitize_notification_error(
                f"{type(e).__name__}: {e}",
                url,
            )

    def _finalize_downloaded(self, downloaded, spec):
        """Process one downloader result while its extracted output identity lock is held."""
        if spec.encode_mode == "none":
            # encode_mode=none never calls _finalize, which is the sole pop() of last_info. Drain
            # entries for paths we are returning so the long-lived web engine (one downloader for the
            # process lifetime) does not accumulate distilled metadata forever across jobs.
            for p in downloaded or []:
                try:
                    self.downloader.last_info.pop(str(p), None)
                except Exception:
                    pass
            return downloaded

        processed = []
        for src in downloaded:
            if not src or not Path(src).exists():
                continue
            try:
                out = self._finalize(Path(src), spec)
            except VerificationError:
                # strict integrity failure must propagate (nonzero CLI exit / queue 'failed')
                raise
            except DownloadCancelled:
                # A cancel mid-encode/merge/loudnorm must reach run()'s cancelled path — never fall
                # through to the raw-file "Done" fallback below (that would mark the queue ok/done
                # and fire a success webhook after the user hit cancel).
                raise
            except Exception as e:
                # Also treat cross-module Cancel twin / string form as cancel (same contract as run()).
                if (type(e).__name__ == 'DownloadCancelled'
                        or 'YT_FINAL_CANCEL' in str(e)):
                    raise DownloadCancelled('YT_FINAL_CANCEL') from e
                logger.error(f"[Finalize] {src}: {e}")
                # Only fall back to the raw download if it still exists on disk — a finalize step
                # (e.g. _encode_to staging to *.downloadtmp) may have renamed it away, and reporting
                # a nonexistent path as 'Done' would poison on_complete/history with a missing file.
                out = Path(src) if Path(src).exists() else None
                if out is not None:
                    # We ARE delivering something, so the job stays 'done' — but the file is the raw
                    # download, not the requested profile (wrong codec for the NLE, no loudness pass,
                    # no burn-in). Record it so the card can say so instead of reporting a clean win.
                    try:
                        self.downloader.last_degraded = {
                            'stage': 'finalize',
                            'detail': str(e)[:300],
                            'delivered': 'raw download (processing did not run)',
                        }
                    except Exception:
                        pass
            if out:
                processed.append(out)
                logger.info(f"Done: {Path(out).name}")
                if spec.on_complete:
                    self._run_on_complete(spec.on_complete, out)
        return processed

    def _run_body(self, spec, urls, dl):
        # This internal path is exercised directly by library users/tests as well as run(); keep the
        # scheduler boundary independently safe, including for one-shot generators.
        urls = dedupe_download_urls(urls)
        logger.info(f"Downloading {len(urls)} item(s) ... [{spec.summary()}]")
        _post_download = lambda paths: self._finalize_downloaded(paths, spec)
        if spec.parallel and len(urls) > 1:
            if spec.speed_priority:
                logger.info("Note: --speed-priority with --parallel uses per-URL serial fallbacks (to keep simple)")
            parallel_call = dl.download_parallel
            parallel_args = (
                urls,
                spec.quality,
            )
            parallel_kwargs = {
                "max_workers": spec.workers,
                "speed_priority": spec.speed_priority,
                "cookies_from_browser": spec.cookies_from_browser,
                "cookiefile": spec.cookiefile,
                "codec": spec.codec,
                "section": spec.section,
                "slow_requests": spec.slow_requests,
                "source_address": spec.source_address,
                "spec": spec,
            }
            if (
                getattr(parallel_call, "__func__", None)
                is YouTubeDownloader.download_parallel
            ):
                # The native aggregate invokes the callback once per successful
                # item and audits each nested download itself.
                downloaded = parallel_call(
                    *parallel_args,
                    _post_download=_post_download,
                    **parallel_kwargs,
                )
            else:
                # Compatibility adapters return one aggregate result and must
                # prove one synchronous protected callback.
                downloaded = _call_with_post_download_contract(
                    parallel_call,
                    _post_download,
                    *parallel_args,
                    **parallel_kwargs,
                )
        else:
            downloaded = []
            # Extraction-prefetch pipeline: overlap extract_only(NEXT url) with the CURRENT download so a
            # multi-URL batch doesn't stall ~2-5s on youtubei between videos. extract hits youtubei, not
            # the googlevideo CDN, so it adds no download-side load. Plain sequential path only
            # (speed_priority re-extracts per quality fallback — not worth prefetching). One slot, one
            # ahead; a prefetch failure -> None -> download() extracts inline (unchanged behaviour).
            _ex = None
            _pending = None                     # (url, Future) for the NEXT item's extraction
            if (not spec.speed_priority) and len(urls) > 1:
                dl._warm_probe_caches(spec)     # warm caches once so the prefetch thread doesn't race to fill them
                _ex = ThreadPoolExecutor(max_workers=1)
            try:
                for i, u in enumerate(urls):
                    if spec.speed_priority:
                        res = _call_with_post_download_contract(
                            dl._download_with_priority,
                            _post_download,
                            u, spec.quality, cookies_from_browser=spec.cookies_from_browser,
                            cookiefile=spec.cookiefile, codec=spec.codec, section=spec.section,
                            slow_requests=spec.slow_requests, source_address=spec.source_address, spec=spec,
                        )
                        downloaded.extend(res or [])
                        continue
                    pre_info = None
                    if _pending is not None and _pending[0] == u:
                        try:
                            pre_info = _pending[1].result()     # extracted during the previous download
                        except DownloadCancelled:
                            raise
                        except Exception:
                            pre_info = None                     # prefetch failed -> extract inline
                    _pending = None
                    if _ex is not None and i + 1 < len(urls):   # kick off the next url's extraction now
                        nxt = urls[i + 1]
                        _pending = (nxt, _ex.submit(
                            dl.extract_only, nxt, spec.quality,
                            cookies_from_browser=spec.cookies_from_browser, cookiefile=spec.cookiefile,
                            codec=spec.codec, section=spec.section, slow_requests=spec.slow_requests,
                            source_address=spec.source_address, spec=spec))
                    downloaded.extend(_call_with_post_download_contract(
                        dl.download,
                        _post_download,
                        u,
                        spec.quality,
                        cookies_from_browser=spec.cookies_from_browser,
                        cookiefile=spec.cookiefile,
                        codec=spec.codec,
                        section=spec.section,
                        slow_requests=spec.slow_requests,
                        source_address=spec.source_address,
                        spec=spec,
                        preextracted_info=pre_info,
                    ))
            finally:
                if _ex is not None:
                    _ex.shutdown(wait=False, cancel_futures=True)

        return downloaded

    @staticmethod
    def _clip_chapters(chapters, cs, ce):
        """Clamp+rebase chapters onto a clip-local timeline starting at 0. Pure; safe on junk."""
        out = []
        for ch in (chapters or []):
            if not isinstance(ch, dict):
                continue
            try:
                start = float(ch.get('start_time', ch.get('start', 0)) or 0)
                raw_end = ch.get('end_time', ch.get('end'))
                end = float(raw_end) if raw_end is not None else ce
            except (TypeError, ValueError):
                continue
            if end <= cs or start >= ce:            # entirely outside the window -> drop
                continue
            new_start, new_end = max(start, cs) - cs, min(end, ce) - cs
            if new_end <= new_start:                # degenerate after clamping -> drop
                continue
            c = dict(ch)
            c['start_time'] = c['start'] = new_start
            c['end_time'] = c['end'] = new_end
            out.append(c)
        return out

    @staticmethod
    def _rebase_subtitle_text(text, cs, ce):
        """Rebase VTT/SRT subtitle cues onto a clip [cs, ce): drop cues fully outside the window,
        clamp overlapping ones, and shift every kept cue by -cs so it starts at 0. yt-dlp does NOT
        adjust subtitle timestamps for --download-sections, so without this a 90s clip's .srt still
        carried source-relative cues (95-100 instead of 5-10), landing late or off the clip in burn,
        embed and sidecar output alike.

        FAIL-SAFE: returns the ORIGINAL text on any parse failure — source-relative subs beat
        corrupted ones. Same keyframe-drift caveat as chapters (up to one GOP without section_precise)."""
        try:
            arrow = '-->'

            def parse_ts(tok):
                m = re.match(r'^\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*$', tok)
                if not m:
                    return None
                return (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60
                        + int(m.group(3)) + int(m.group(4).ljust(3, '0')) / 1000.0)

            def fmt_ts(sec, sep):
                sec = max(0.0, sec)
                h = int(sec // 3600); sec -= h * 3600
                mm = int(sec // 60); sec -= mm * 60
                s = int(sec); ms = int(round((sec - s) * 1000))
                # Carry a rounding overflow all the way up (ms->s->mm->h) so a cue landing a hair
                # under a whole second can't emit an invalid '...:60.000' / '..:60:..' timestamp.
                if ms >= 1000:
                    s += 1; ms -= 1000
                if s >= 60:
                    mm += 1; s -= 60
                if mm >= 60:
                    h += 1; mm -= 60
                return f"{h:02d}:{mm:02d}:{s:02d}{sep}{ms:03d}"

            norm = text.replace('\r\n', '\n').replace('\r', '\n')
            out_blocks, srt_index = [], 0
            for blk in re.split(r'\n\s*\n', norm):
                lines = blk.split('\n')
                ts_idx = next((i for i, ln in enumerate(lines) if arrow in ln), None)
                if ts_idx is None:
                    out_blocks.append(blk)            # WEBVTT header / STYLE / NOTE -> passthrough
                    continue
                left, _, right = lines[ts_idx].partition(arrow)
                rparts = right.strip().split(None, 1)
                settings = (' ' + rparts[1]) if len(rparts) > 1 else ''
                start = parse_ts(left)
                end = parse_ts(rparts[0]) if rparts else None
                if start is None or end is None:
                    out_blocks.append(blk)            # unparseable timing -> leave the block alone
                    continue
                if end <= cs or start >= ce:
                    continue                          # entirely outside the clip -> drop
                new_s, new_e = max(start, cs) - cs, min(end, ce) - cs
                if new_e <= new_s:
                    continue
                sep = ',' if ',' in left else '.'
                new_lines = list(lines)
                new_lines[ts_idx] = f"{fmt_ts(new_s, sep)} {arrow} {fmt_ts(new_e, sep)}{settings}"
                if ts_idx == 1 and lines[0].strip().isdigit():   # renumber a leading SRT index
                    srt_index += 1
                    new_lines[0] = str(srt_index)
                out_blocks.append('\n'.join(new_lines))
            return '\n\n'.join(out_blocks).rstrip('\n') + '\n'
        except Exception:
            return text

    @staticmethod
    def _rebase_ass_text(text, cs, ce):
        """Rebase ASS/SSA Dialogue+Comment cues onto a clip [cs, ce): drop cues fully outside the
        window, clamp overlappers, shift kept cues by -cs. ASS times are H:MM:SS.cc (centiseconds).
        The [Events] `Format:` line is parsed to locate the Start/End columns (Text is always the last
        field and may itself contain commas, so the split is bounded by the column count). Without
        this, .ass sidecars stay on the source timeline like .srt/.vtt did.

        FAIL-SAFE: returns the ORIGINAL text on any parse failure (or when no parseable Events format
        is found), so a malformed style script is delivered as-is rather than corrupted."""
        try:
            def parse_ts(tok):
                m = re.match(r'^\s*(\d+):(\d{2}):(\d{2})[.:](\d{1,2})\s*$', tok)
                if not m:
                    return None
                return (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                        + int(m.group(4).ljust(2, '0')) / 100.0)

            def fmt_ts(sec):
                sec = max(0.0, sec)
                h = int(sec // 3600); sec -= h * 3600
                mm = int(sec // 60); sec -= mm * 60
                s = int(sec); csec = int(round((sec - s) * 100))
                # Carry a rounding overflow all the way up (cs->s->mm->h) so a cue landing a hair
                # under a whole second can't emit an invalid '...:60.00' timestamp.
                if csec >= 100:
                    s += 1; csec -= 100
                if s >= 60:
                    mm += 1; s -= 60
                if mm >= 60:
                    h += 1; mm -= 60
                return f"{h:d}:{mm:02d}:{s:02d}.{csec:02d}"

            lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
            # Find the [Events] Format: line -> Start/End column indices and the field count.
            si = ei = ntok = None
            text_last = False
            in_events = False
            for ln in lines:
                s = ln.strip()
                low = s.lower()
                if low.startswith('['):
                    in_events = (low == '[events]')
                    continue
                if in_events and low.startswith('format:'):
                    fields = [f.strip().lower() for f in s.split(':', 1)[1].split(',')]
                    ntok = len(fields)
                    if 'start' in fields and 'end' in fields:
                        si, ei = fields.index('start'), fields.index('end')
                    text_last = bool(fields) and fields[-1] == 'text'
                    break
            # The comma-bounded split below assumes the free-form Text field is LAST (the ASS spec
            # mandates this). Without a Start/End column, or if Text isn't last, the split can't be
            # bounded safely -> leave the whole file untouched.
            if si is None or ei is None or not ntok or not text_last:
                return text

            out = []
            for ln in lines:
                low = ln.strip().lower()
                if low.startswith('dialogue:') or low.startswith('comment:'):
                    head, _, rest = ln.partition(':')       # only the FIRST ':' (times have their own)
                    parts = rest.split(',', ntok - 1)       # keep commas inside the trailing Text field
                    if len(parts) < ntok:
                        return text                         # malformed event -> fail the WHOLE file unchanged
                    start, end = parse_ts(parts[si]), parse_ts(parts[ei])
                    if start is None or end is None:
                        return text                         # unparseable timing -> fail the whole file (no
                        #                                     mixed source/clip timeline)
                    if end <= cs or start >= ce:
                        continue                            # entirely outside the clip -> drop
                    new_s, new_e = max(start, cs) - cs, min(end, ce) - cs
                    if new_e <= new_s:
                        continue
                    parts[si], parts[ei] = fmt_ts(new_s), fmt_ts(new_e)
                    out.append(f"{head}:{','.join(parts)}")
                else:
                    out.append(ln)
            return '\n'.join(out)
        except Exception:
            return text

    def _rebase_subtitle_files(self, sub_files, spec):
        """Rewrite each located subtitle sidecar to clip-local time when --section is active. No-op
        without a real section, so the default path is untouched."""
        rng = (YouTubeDownloader._parse_section(spec.section)
               if getattr(spec, 'section', None) else None)
        if not rng or not sub_files:
            return
        import tempfile
        cs, ce = float(rng[0]), float(rng[1])
        for sf in sub_files:
            tmp = None
            try:
                p = Path(sf)
                src_text = p.read_text(encoding='utf-8', errors='replace')
                if p.suffix.lower() in ('.ass', '.ssa'):
                    rebased = self._rebase_ass_text(src_text, cs, ce)
                else:
                    rebased = self._rebase_subtitle_text(src_text, cs, ce)
                # Atomic replace via a UNIQUE same-directory temp, then os.replace: an ENOSPC (or any
                # error) mid-write can't truncate the user's original subtitle — the rewrite lands
                # whole or not at all. mkstemp gives a unique name so a pre-existing/racing file is
                # never clobbered; same dir keeps os.replace atomic (same filesystem). Carry the
                # original file's mode across so os.replace doesn't drop it to the umask default.
                fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + '.', suffix='.tmp')
                tmp = Path(tmp_name)
                try:
                    fh = os.fdopen(fd, 'w', encoding='utf-8')
                except BaseException:
                    # fdopen didn't take ownership of the fd -> close it ourselves. Guard the close
                    # (fdopen may have already closed fd before raising -> EBADF) so a bare `raise`
                    # re-raises the ORIGINAL error (e.g. a KeyboardInterrupt), never an EBADF that the
                    # outer `except OSError` would then swallow.
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
                with fh:                        # fh now owns fd; closed on exit even if write raises
                    fh.write(rebased)
                try:
                    os.chmod(str(tmp), os.stat(str(p)).st_mode & 0o777)
                except OSError:
                    pass                        # best-effort mode preservation
                os.replace(str(tmp), str(p))
                tmp = None
            except OSError:
                if tmp is not None:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                logger.debug("[Section] could not rebase subtitle %s", sf)

    def _clip_local_info(self, info, spec, src):
        """Return `info` describing the DELIVERED CLIP rather than the source video.

        With --section every sidecar downstream of here (ffmetadata chapters, EDL/CSV/FCPXML
        markers, NFO runtime, chapter VTT) still described the full source: an editor importing a
        90s clip got a 600s FCPXML timeline whose markers pointed at media that does not exist.
        Deriving one clipped view HERE means each of those consumers stays untouched and cannot
        disagree with the others.

        Identity when there is no clip — and the guard is _parse_section(), not `spec.section`,
        because a whitespace-only section is TRUTHY yet downloads the whole video, which would
        otherwise rebase a full-length file against a nonexistent window.

        Duration is PROBED from the real file (src is the clipped download and is
        duration-equivalent to the finalized output through every stage here); the requested
        window is only the fallback. Caveat worth knowing: without section_precise, cuts land on
        the nearest keyframe, so rebasing by the REQUESTED start can drift up to one GOP. Duration
        stays accurate; marker positions inherit that drift."""
        # _parse_section is a classmethod on YouTubeDownloader (deliberately, so web/CLI can
        # validate without a detector) — call it on the class, not through self.
        rng = (YouTubeDownloader._parse_section(spec.section)
               if getattr(spec, 'section', None) else None)
        if not rng:
            return info                                   # 99% path: byte-identical behaviour
        cs, ce = float(rng[0]), float(rng[1])
        probed = None
        try:
            probed = (self.processor.probe_integrity(src) or {}).get('duration')
        except Exception:
            probed = None
        clipped = dict(info)
        clipped['duration'] = float(probed) if probed else max(0.0, ce - cs)
        for key in ('chapters', 'sponsorblock_chapters'):
            if info.get(key):
                clipped[key] = self._clip_chapters(info.get(key), cs, ce)
        return clipped

    def _finalize(self, src, spec):
        """Turn one downloaded file into the final edit-ready asset per the JobSpec."""
        if not src or not src.exists():
            return None
        # Mirror the download-side cancel channel onto the processor so a cancel can interrupt a long
        # re-encode / 2-pass loudnorm too, not just the transfer. Re-set every finalize (the engine is
        # reused across jobs), so job B never inherits job A's latched probe.
        self.processor.cancel_check = getattr(self.downloader, 'cancel_check', None)
        # pop (not get): last_info is read exactly once per source path here, after which the entry
        # is dead weight. In the long-running server worker (one downloader for the process lifetime)
        # a get() would leak one several-KB distilled-info entry per completed download forever.
        info = self.downloader.last_info.pop(str(src), {})
        # ONE clipped view, derived before any consumer reads it: ffmetadata, markers/FCPXML, NFO
        # and the chapter VTT below all pull duration/chapters from `info`, so clipping here is what
        # keeps them consistent with each other and with the delivered file. No-op without --section.
        info = self._clip_local_info(info, spec, src)
        mode = spec.encode_mode
        profile = spec.profile

        # --- metadata + chapters -> single ffmetadata file (B1/B2) ---
        tags = self._build_tags(info) if spec.embed_metadata else {}
        # If SponsorBlock ran, yt-dlp already owns the file's chapters; don't overwrite them.
        chapters = info.get('chapters') if (spec.chapters and not spec.sponsorblock) else None
        ffmeta = None
        if tags or chapters:
            # A DETERMINISTIC sibling path (src.with_suffix('.ffmeta')) silently overwrites a
            # user-authored sidecar of that name and then unlinks it below — i.e. destroys their
            # file. Own a unique temp instead, so the cleanup can only ever delete what we created.
            import tempfile
            _fd, _meta_tmp = tempfile.mkstemp(suffix='.ffmeta', prefix='ytf_meta_')
            os.close(_fd)
            ffmeta = self.processor.write_ffmetadata(Path(_meta_tmp), tags=tags, chapters=chapters)
            if not ffmeta:
                Path(_meta_tmp).unlink(missing_ok=True)   # nothing written -> don't leak the temp

        # --- per-call profile overrides (audio codec/bitrate, HDR color) ---
        overrides = {}
        if spec.audio_codec:
            overrides['audio_codec'] = spec.audio_codec
        if spec.audio_bitrate:
            overrides['audio_bitrate'] = spec.audio_bitrate

        # --- subtitles: locate sidecars; burn forces a re-encode ---
        # --sub-format both: yt-dlp wrote only the native .vtt (one format per pass); derive the
        # sibling .srt FIRST so _find_sidecar_subs (srt-preferred) picks it up for burn/embed.
        if spec.sub_langs and getattr(spec, 'sub_format', 'srt') == 'both':
            self._derive_srt_from_vtt(src)
        sub_files = self._find_sidecar_subs(src) if spec.sub_langs else []
        # A --section clip's downloaded subs are still on the SOURCE timeline (yt-dlp doesn't adjust
        # them); rebase before they are burned/embedded/kept so all three describe the delivered clip.
        self._rebase_subtitle_files(sub_files, spec)
        vf_extra = None
        if spec.sub_langs and spec.sub_mode == 'burn' and sub_files:
            if mode == 'copy':
                mode = 'reencode'
            vf_extra = "subtitles=" + self._ff_filter_path(sub_files[0])

        # --- burn-in slate (title/uploader/date/URL + source timecode); forces re-encode ---
        # Composes with the subtitle-burn vf_extra (comma-joined). build_ffmpeg_command skips
        # -vf on copy/CUDA-surfaces, so the mode flip here is what makes the slate actually apply.
        if spec.burn_in:
            if mode == 'copy':
                mode = 'reencode'
            slate = self._burn_in_vf(info)
            vf_extra = ((vf_extra + ',') if vf_extra else '') + slate

        # --- HDR10/HLG passthrough on re-encode (A2) ---
        if mode == 'reencode' and spec.preserve_hdr:
            color = self.processor.hdr_color_args(src)
            if color:
                overrides['_extra_out_args'] = color

        # --- Rec.709 tagging for untagged SDR (reencode only) ---
        # Only applies when HDR did NOT already populate _extra_out_args, so HDR
        # always wins. sdr_color_args also self-guards (returns [] for HDR sources).
        if (mode == 'reencode' and spec.color_tag == 'auto'
                and '_extra_out_args' not in overrides):
            sdr = self.processor.sdr_color_args(src)
            if sdr:
                overrides['_extra_out_args'] = sdr

        # --- output container (+ Opus -> MKV auto-switch) ---
        out_ext = src.suffix if mode == 'copy' else self.processor.output_ext(profile)
        if overrides.get('audio_codec') in ('libopus', 'opus') and out_ext.lower() == '.mp4':
            out_ext = '.mkv'

        # --- dual-mono / split: channelsplit hard-panned L/R into two discrete mono tracks ---
        # MUST run AFTER the color blocks (which guard on '_extra_out_args' not in overrides) and
        # ALWAYS extend (never assign) so Rec.709/HDR flags survive alongside the channelsplit -maps.
        if spec.audio_map in ('dual-mono', 'split'):
            mode, overrides, out_ext = self._plan_audio_map(spec, mode, overrides, out_ext)
            # Fold BOTH the profile's own -vf (e.g. DNxHD scale/fps) AND any vf_extra (burn-in/subtitle)
            # into the channelsplit filter_complex — ffmpeg rejects a simultaneous -vf and -filter_complex.
            prof_vf = self.processor.profiles.get(profile, {}).get('vf')
            combined_vf = ','.join([x for x in (prof_vf, vf_extra) if x]) or None
            amap = self._audio_map_args(spec, out_ext, vf=combined_vf)
            if amap:
                existing = overrides.get('_extra_out_args') or []
                overrides['_extra_out_args'] = list(existing) + amap
                if vf_extra:
                    vf_extra = None
                if prof_vf:
                    overrides['vf'] = ''   # suppress the profile's -vf (now folded into the filter_complex)
        desired = src.with_suffix(out_ext)

        plan = {
            'metadata': tags if (tags and not ffmeta) else None,
            'ffmetadata_file': ffmeta,
            'overrides': overrides or None,
            'vf_extra': vf_extra,
        }
        logger.info(f"Post-processing: {src.name} (mode={mode}, profile={profile})")
        try:
            result = self._encode_to(src, desired, mode, profile, plan)
        finally:
            # Unlink the ffmetadata temp even if _encode_to RAISED. A cancel mid-encode re-raises
            # BaseException out of _finalize, which would otherwise skip the cleanup below and leak
            # ytf_meta_*.ffmeta to disk (one per cancelled metadata job). The `if ffmeta` guard keeps
            # the no-metadata path (ffmeta is None) a no-op — the regression-guard's key reshape.
            if ffmeta:
                try:
                    Path(ffmeta).unlink()
                except OSError:
                    pass
        # ``plan`` belongs to this finalizer call, so parallel encodes cannot
        # overwrite each other's selected fallback profile.
        _actual_profile = plan.get('_actual_profile')
        if not result:
            result = src

        # --- loudness normalization (A4) ---
        if spec.loudnorm is not None and Path(result).exists():
            norm = self.downloader._unique_path(
                Path(result).with_name(Path(result).stem + '_ln' + Path(result).suffix))
            # honor the same audio codec/bitrate the encode used (else loudnorm reverts to aac/320k)
            if self.processor.normalize_audio(result, norm, target_lufs=spec.loudnorm,
                                              audio_codec=(spec.audio_codec or 'aac'),
                                              audio_bitrate=(spec.audio_bitrate or '320k')):
                prev, result = result, norm
                self._cleanup_intermediate(prev, src, result)

        # --- embed subtitles (non-burn) and/or thumbnail (B3/B6) ---
        embed_subs = bool(spec.sub_langs and spec.sub_mode == 'embed' and sub_files)
        thumb = self._find_thumbnail(src) if spec.embed_thumbnail else None
        if (embed_subs or thumb) and Path(result).exists():
            container = Path(result).suffix.lstrip('.').lower()
            muxed = self.downloader._unique_path(
                Path(result).with_name(Path(result).stem + '_emb' + Path(result).suffix))
            if self.processor.mux_sidecars(result, muxed,
                                           sub_files=(sub_files if embed_subs else []),
                                           thumb_file=thumb, container=container):
                prev, result = result, muxed
                self._cleanup_intermediate(prev, src, result)

        # --- post-download integrity check (--verify-strict) FIRST — a strict failure must raise
        #     BEFORE any sidecar is written, so we never orphan markers/NFO/artwork next to a bad file ---
        if result and Path(result).exists():
            ok, reason = self._verify_output(Path(result), src, spec, info=info)
            if not ok:
                if spec.verify_strict:
                    raise VerificationError(f"[Verify] {Path(result).name}: {reason}")
                logger.warning(f"[Verify] {Path(result).name}: {reason} (continuing; not strict)")
            elif spec.verify_strict:
                logger.info(f"[Verify] {Path(result).name}: OK")

        # --- KILLER FEATURE: export YouTube chapters as NLE timeline markers (EDL/CSV/FCPXML) ---
        if spec.export_markers and info.get('chapters') and Path(result).exists():
            try:
                try:
                    from markers import write_markers
                except ImportError:
                    from .markers import write_markers
                fps = self.processor.probe_fps(result) or info.get('fps') or 25
                written = write_markers(Path(result), info['chapters'],
                                        fps=fps,
                                        formats=spec.export_markers,
                                        title=info.get('title'),
                                        duration=info.get('duration'),
                                        source_url=info.get('webpage_url'))
                for w in written:
                    logger.info(f"[Markers] {Path(w).name} ({len(info['chapters'])} chapters)")
                if not written:
                    logger.info("[Markers] no valid format in --export-markers (use edl|csv|fcpxml|all)")
            except Exception as e:
                logger.warning(f"[Markers] export failed: {e}")
        elif spec.export_markers and not info.get('chapters'):
            logger.info("[Markers] this video has no chapters to export")

        # --- SponsorBlock shot-list: color-coded category markers (Sponsor/Intro/Outro/...) ---
        if spec.sponsorblock_markers and info.get('sponsorblock_chapters') and Path(result).exists():
            try:
                try:
                    from markers import write_sponsorblock_markers
                except ImportError:
                    from .markers import write_sponsorblock_markers
                fps = self.processor.probe_fps(result) or info.get('fps') or 25
                written = write_sponsorblock_markers(Path(result), info['sponsorblock_chapters'],
                                                     fps=fps,
                                                     formats=spec.sponsorblock_markers,
                                                     title=info.get('title'),
                                                     duration=info.get('duration'),
                                                     source_url=info.get('webpage_url'))
                for w in written:
                    logger.info(f"[SB-Markers] {Path(w).name} ({len(info['sponsorblock_chapters'])} segments)")
            except Exception as e:
                logger.warning(f"[SB-Markers] export failed: {e}")

        # --- NFO sidecar for Kodi / Jellyfin / Plex ---
        if spec.write_nfo and info and Path(result).exists():
            try:
                try:
                    from nfo import write_nfo, write_tvshow_nfo, _nfo_kind
                except ImportError:
                    from .nfo import write_nfo, write_tvshow_nfo, _nfo_kind
                kind = getattr(spec, 'nfo_kind', 'auto')
                w = write_nfo(Path(result), info, kind=kind)
                if w:
                    logger.info(f"[NFO] wrote {Path(w).name}")
                # Playlist-as-series: when the per-file NFO resolved to <episodedetails> and there is
                # series context, drop a tvshow.nfo in whatever folder organize-by/output-template chose
                # (it follows the media file's parent, so it never fights those layouts).
                if _nfo_kind(info, kind) == 'episode' and (info.get('playlist') or info.get('channel')):
                    tv = write_tvshow_nfo(Path(result).parent, info)
                    if tv:
                        logger.info("[NFO] wrote tvshow.nfo")
            except Exception as e:
                logger.warning(f"[NFO] failed: {e}")

        # --- media-server artwork: sibling poster.jpg / fanart.jpg / folder.jpg ---
        if getattr(spec, 'write_artwork', False) and Path(result).exists():
            try:
                self._write_artwork(src, Path(result), info)
            except Exception as e:
                logger.warning(f"[Artwork] failed: {e}")

        # --- description.txt + chapters.vtt text sidecars ---
        if getattr(spec, 'write_sidecar_text', False) and info and Path(result).exists():
            try:
                try:
                    from markers import write_text_sidecars
                except ImportError:
                    from .markers import write_text_sidecars
                for w in write_text_sidecars(Path(result), info.get('description'), info.get('chapters')):
                    logger.info(f"[Sidecar] wrote {Path(w).name}")
            except Exception as e:
                logger.warning(f"[Sidecar] text sidecars failed: {e}")

        # --- editing proxy (half-res H.264) alongside the master ---
        if spec.proxy_pair and Path(result).exists():
            proxy = self.downloader._unique_path(
                Path(result).with_name(Path(result).stem + '_proxy.mp4'))
            if self.processor.make_proxy(result, proxy):
                logger.info(f"[Proxy] wrote {proxy.name}")

        if _actual_profile and result:
            # Record the profile that ACTUALLY ran (post-degradation) against the FINAL output path,
            # so history logs the real encoder (e.g. 'auto_h264' -> 'amf_h264'), not the requested one.
            # Keyed on the final path, so it survives the loudnorm/mux/proxy renames above.
            try:
                self._actual_profiles[str(Path(result))] = _actual_profile
            except AttributeError:
                self._actual_profiles = {str(Path(result)): _actual_profile}
        return Path(result) if result else None

    def _verify_output(self, result, src, spec, info=None):
        """ffprobe the FINAL output and validate duration/streams/size. Returns (ok, reason|None).
        `src` is the raw download path (last_info key); `result` is the file actually probed.
        Graceful skip (returns OK) when the probe fails or expected duration is unknown.
        `info` is passed in by the caller (_finalize already pop()'d it out of last_info); fall back
        to a bare get() only for standalone callers that didn't supply it."""
        if info is None:
            info = self.downloader.last_info.get(str(src), {})
        probe = self.processor.probe_integrity(result)
        if not probe:
            # ffprobe missing/failed/non-JSON -> probe_integrity returns {}. Under --verify-strict
            # that is a FAILURE, not a pass: the strict contract promises the output was checked, so
            # "couldn't check" must fail closed rather than log OK. Non-strict keeps degrading
            # gracefully (warn + continue).
            if getattr(spec, 'verify_strict', False):
                return (False, "could not probe output (ffprobe unavailable/failed) — strict verification cannot pass")
            logger.warning("[Verify] could not probe output, skipping verification")
            return (True, None)
        # plausible nonzero file size
        if probe.get('size', 0) < 1024:
            return (False, "zero/implausible file size")
        # audio-only mode does not require a video stream. Mirror the downloader (line ~1770):
        # only quality=='audio' produces an audio-only file; --audio-format alone keeps the video.
        audio_only = (spec.quality == 'audio')
        fid = str(getattr(spec, 'format_id', '') or '')
        if fid and '+' not in fid:
            # An explicit SINGLE format id (--format-id 137 / 140; CLI-exposed — see
            # test_workspace_parity) legitimately yields a one-stream file, and verification cannot
            # know which stream the id names. Require at least one stream instead of failing a
            # correct video-only download with 'no audio stream'. A merged id ('137+140') promises
            # both streams and keeps the strict checks below.
            if not (probe.get('has_video') or probe.get('has_audio')):
                return (False, "no audio or video stream")
        else:
            if not audio_only and not probe.get('has_video'):
                return (False, "no video stream")
            # require at least one audio stream (only an explicit single format id may skip audio)
            if not probe.get('has_audio'):
                return (False, "no audio stream")
        # duration check (skip when expected duration is unknown, or when --section clipped the
        # output: the clip is shorter than the full-video info['duration'] -> false-positive).
        want = info.get('duration')
        if want and not spec.section:
            got = probe.get('duration')
            if got is None:
                return (False, "output has no measurable duration")
            tol = max(2.0, 0.01 * want)
            if abs(got - want) > tol:
                return (False, f"duration {got:.1f}s differs from expected {want:.1f}s (tol {tol:.1f}s)")
        return (True, None)

    @staticmethod
    def _cleanup_intermediate(prev, src, final):
        """Unlink a superseded staging file (loudnorm '_ln' / mux '_emb') once the next stage
        has produced a new result. Guards: only delete files THIS method created (stem ends in
        '_ln'/'_emb'), and never the raw download (src) or the final returned path (collision-safe
        unique names mean a real '_ln'/'_emb' user output won't be matched here either)."""
        try:
            prev = Path(prev)
            src = Path(src)
            final = Path(final)
        except Exception:
            return
        # Never touch the raw download or the file we're about to return.
        try:
            if prev.resolve() == src.resolve() or prev.resolve() == final.resolve():
                return
        except Exception:
            if str(prev) == str(src) or str(prev) == str(final):
                return
        # Only our own staged intermediates: stem ends in the stage marker, optionally followed by a
        # collision-safe " (N)" suffix from _unique_path (e.g. "clip_ln" or "clip_ln (1)").
        if not re.search(r'_(ln|emb)( \(\d+\))?$', prev.stem):
            return
        try:
            if prev.exists():
                prev.unlink()
                logger.debug(f"[Finalize] removed intermediate {prev.name}")
        except OSError:
            pass

    def _encode_to(self, src, desired, mode, profile, plan):
        """Collision-safe encode/remux. Same-container -> stage-to-temp in place; different
        container -> unique output, keep the raw download on failure."""
        try:
            same = desired.resolve() == src.resolve()
        except Exception:
            same = (str(desired) == str(src))
        if same:
            temp_src = src.with_suffix(src.suffix + ".downloadtmp")
            encode_ok = False
            try:
                # .replace() (os.replace) not .rename(): POSIX rename silently overwrites, but on
                # Windows rename RAISES FileExistsError when the destination exists. A stale
                # *.downloadtmp from an interrupted run (nothing ever sweeps them) would raise here
                # BEFORE encoding, and the BaseException handler below would then delete the freshly
                # downloaded file and restore the stale one. os.replace overwrites atomically on both.
                if temp_src.exists():
                    logger.warning(f"[encode_to] superseding stale staging file from an interrupted run: {temp_src.name}")
                src.replace(temp_src)
                encode_ok = self._do_encode(temp_src, desired, mode, profile, plan)
                if encode_ok and desired.exists():
                    return desired
                # The encode did NOT yield a usable output (rc=0 but no file, AV lock, etc.): drop the
                # "profile that ran" marker so the restored RAW download is never mislabelled in
                # history as an encoded file. _encode_to owns this — it is the output-existence authority.
                plan['_actual_profile'] = None
                logger.warning("Processing failed; restoring the raw download.")
                # ffmpeg may have left a partial/0-byte `desired`; remove it before restoring the raw file
                if desired.exists():
                    try:
                        desired.unlink()
                    except OSError:
                        pass
                if temp_src.exists():
                    try:
                        temp_src.replace(desired)      # atomic overwrite on both platforms
                        return desired
                    except OSError as rename_err:
                        # Return the raw file's ACTUAL location (temp_src) so downstream gets a path
                        # that exists — returning None made _finalize fall back to the now-missing src.
                        logger.error(f"[encode_to] Could not restore original name ({rename_err}); raw file kept at: {temp_src}")
                        return temp_src
                return None
            except BaseException:
                # BaseException (not just Exception): a Ctrl+C landing inside _do_encode's blocking
                # ffmpeg stderr read is a KeyboardInterrupt, which is NOT an Exception. If we only
                # caught Exception the raw download would stay permanently stranded under the
                # *.downloadtmp name. _do_encode RAISED (not just returned False): the raw download is
                # currently stranded under temp_src. Restore it to `desired` (== src for the
                # same-container path) before propagating, mirroring the False-return recovery above —
                # otherwise the caller's generic handler would report the now-missing original path as
                # a successful 'Done'.
                logger.error("Processing raised; restoring the raw download.", exc_info=True)
                if desired.exists():
                    try:
                        desired.unlink()
                    except OSError:
                        pass
                if temp_src.exists():
                    try:
                        temp_src.replace(desired)      # atomic overwrite on both platforms
                    except OSError as rename_err:
                        logger.error(f"[encode_to] Could not restore original name ({rename_err}); raw file kept at: {temp_src}")
                raise
            finally:
                # Only delete the staged raw download when the encode actually succeeded AND the
                # encoded output exists; an AV/handle lock during restore must never destroy the raw file.
                if encode_ok and temp_src.exists() and desired.exists():
                    try:
                        temp_src.unlink()
                    except OSError:
                        pass
        else:
            dst = self.downloader._unique_path(desired)
            try:
                ok = self._do_encode(src, dst, mode, profile, plan)
            except BaseException:
                # Interrupt/exception mid-encode: the raw download (src) is untouched here (unlike the
                # same-container path), but ffmpeg may have left a partial `dst` behind. Remove it so it
                # doesn't linger as an unreferenced stray, then propagate.
                if dst.exists():
                    try:
                        dst.unlink()
                    except OSError:
                        pass
                raise
            if ok and dst.exists():
                return dst
            plan['_actual_profile'] = None   # raw download returned, not an encoded file
            logger.warning("Processing failed; returning raw download.")
            # Clean up any partial/0-byte output ffmpeg left when it returned failure.
            if dst.exists():
                try:
                    dst.unlink()
                except OSError:
                    pass
            return src

    # ---- finalize helpers -------------------------------------------- #
    def _build_tags(self, info):
        """Map distilled source info to container metadata tags (B2). Date is a 'date' tag only —
        never SMPTE timecode (that corrupts multicam sync)."""
        if not info:
            return {}
        tags = {}
        if info.get('title'):
            tags['title'] = str(info['title'])[:200]
        up = info.get('uploader')
        if up:
            tags['artist'] = str(up)
            tags['album_artist'] = str(up)
        ud = str(info.get('upload_date') or '')
        if len(ud) == 8 and ud.isdigit():
            tags['date'] = f"{ud[0:4]}-{ud[4:6]}-{ud[6:8]}"
        if info.get('webpage_url'):
            tags['comment'] = str(info['webpage_url'])
        desc = info.get('description')
        if desc:
            tags['description'] = ' '.join(str(desc).split())[:500]
        return tags

    def _find_sidecar_subs(self, src):
        import glob as _glob
        base = _glob.escape(str(src.with_suffix('')))
        out = []
        for ext in ('srt', 'vtt', 'ass', 'ssa'):
            out += sorted(_glob.glob(base + f'.*.{ext}'))
        # srt first (loop order) — the burn-in subtitles filter handles srt most reliably;
        # de-dup preserving that order.
        return [Path(p) for p in dict.fromkeys(out)]

    def _derive_srt_from_vtt(self, src):
        """--sub-format both: convert each downloaded .vtt subtitle sidecar to a sibling .srt
        (keeping the .vtt, so 'both' delivers both). Best-effort; never raises into the job."""
        import glob as _glob
        base = _glob.escape(str(src.with_suffix('')))
        for vtt in sorted(_glob.glob(base + '.*.vtt')):
            if vtt.endswith('.chapters.vtt'):
                continue   # chapter sidecar, not a subtitle track
            srt = vtt[:-4] + '.srt'
            if os.path.exists(srt):
                continue
            try:
                self.processor._run_ffmpeg([self.processor.ffmpeg, '-hide_banner', '-y', '-i', vtt, srt])
            except Exception as e:
                logger.warning(f"[Subs] could not derive .srt from {Path(vtt).name}: {e}")

    def _find_thumbnail(self, src):
        import glob as _glob
        base = _glob.escape(str(src.with_suffix('')))
        for ext in ('jpg', 'jpeg', 'png', 'webp'):
            hits = _glob.glob(base + f'.{ext}')
            if hits:
                return Path(hits[0])
        return None

    def _write_artwork(self, src, result, info):
        """Write sibling poster/fanart/folder.jpg from the downloaded thumbnail for media-server tiles.
        Reuses _find_thumbnail + the existing webp/png -> jpg ffmpeg path. Returns written Paths."""
        import shutil
        result = Path(result)
        thumb = self._find_thumbnail(src)
        if thumb is None:
            logger.info("[Artwork] no thumbnail found (download with --embed-thumbnail or --write-artwork)")
            return []

        # Get a .jpg source: copy if already jpeg, else re-encode once via ffmpeg (mirrors mux_sidecars).
        if thumb.suffix.lower() in ('.jpg', '.jpeg'):
            jpg_src = thumb
            tmp_jpg = None
        else:
            tmp_jpg = self.downloader._unique_path(result.with_name(result.stem + '_artwork_tmp.jpg'))
            cmd = [self.processor.ffmpeg, '-hide_banner', '-y', '-i', str(thumb),
                   '-frames:v', '1', str(tmp_jpg)]
            if not self.processor._run_ffmpeg(cmd) or not tmp_jpg.exists():
                logger.warning("[Artwork] thumbnail -> jpg conversion failed")
                return []
            jpg_src = tmp_jpg

        written = []
        try:
            for name in (result.stem + '-poster.jpg', result.stem + '-fanart.jpg'):
                dst = result.with_name(name)
                shutil.copyfile(jpg_src, dst)
                written.append(dst)
            # folder.jpg is directory-level (the show/season tile). Publish a
            # fully-written same-directory temp with an atomic no-replace hard
            # link so two distinct video identity locks cannot both win.
            folder = result.with_name('folder.jpg')
            if self._publish_first_artwork(jpg_src, folder):
                written.append(folder)
        finally:
            if tmp_jpg is not None and tmp_jpg.exists():
                try:
                    tmp_jpg.unlink()
                except OSError:
                    pass
        if written:
            logger.info("[Artwork] wrote " + ", ".join(p.name for p in written))
        return written

    @staticmethod
    @contextmanager
    def _artwork_publication_lock(destination):
        """Hold a stable, never-unlinked sidecar lock across processes."""
        destination = Path(destination)
        lock_path = destination.with_name(
            f".{destination.name}.artwork.publish.lock"
        )
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        locked = False
        try:
            # Windows byte-range locking requires the byte to exist. Concurrent
            # initialization writes the same byte and never replaces the inode.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _publish_first_artwork(source, destination):
        """Atomically publish one directory artwork winner without partial files."""
        import shutil
        import tempfile

        destination = Path(destination)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            shutil.copyfile(source, temp_path)
            try:
                os.link(temp_path, destination)
                return True
            except FileExistsError:
                return False
            except OSError:
                # Some filesystems disallow hard links. Serialize the fallback on
                # a stable sidecar inode shared by every process; never unlinking
                # that lock avoids split-lock races between old and new inodes.
                with FinalYouTubeGPUEngine._artwork_publication_lock(destination):
                    if os.path.lexists(destination):
                        return False
                    os.replace(temp_path, destination)
                    temp_path = None
                    return True
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _ff_filter_path(p):
        """Escape a path for use inside the ffmpeg subtitles= filter (Windows colon/backslash safe).

        Filter option values are parsed TWICE by av_get_token (the graph parser, then the filter's
        own option parser), and inside '...' a backslash can NOT escape the closing quote — so the
        old  \\'  form closed the quote early ('Error parsing filterchain' on any path with an
        apostrophe). The correct form closes the quote, emits \\' via OUTSIDE-quote escapes, and
        reopens:  ' -> '\\\\\\''  — level 1 yields \\' and level 2 yields the literal apostrophe
        (verified against a real ffmpeg: the subtitles= file resolves instead of 'No such file')."""
        s = str(p).replace('\\', '/')
        s = s.replace(':', '\\:').replace("'", "'\\\\\\''").replace('[', '\\[').replace(']', '\\]')
        return f"'{s}'"

    @staticmethod
    def _ff_drawtext_escape(s):
        """Escape an arbitrary string for use as the inner text of a drawtext filter.

        drawtext needs a DIFFERENT escape than subtitles= paths. We wrap the value in
        single quotes at the call site, so here we neutralise everything that could break
        out of those quotes or out of the filtergraph: backslash FIRST (so we don't
        double-escape our own escapes), then the apostrophe (the #1 break in titles),
        colons, '%' (drawtext interprets %{...} as strftime/expansion), and the graph
        delimiters '[' ']' and ','. Newlines/control chars are collapsed to a space and
        the result is truncated to a sane slate width."""
        s = ' '.join(str(s or '').split())          # collapse newlines / control whitespace
        s = s[:80]
        s = s.replace('\\', '\\\\')                  # MUST be first
        # Two-level parse (graph, then drawtext's option parser): inside '...' a backslash cannot
        # escape the closing quote, so \\' broke the graph on every apostrophe-bearing title.
        # Close-escape-reopen instead: level 1 turns '\\\\\\''  into  \\' , level 2 into a literal '
        # (verified against a real ffmpeg — framecrc identical to the same text via textfile=).
        s = s.replace("'", "'\\\\\\''")
        s = s.replace(':', '\\:')
        s = s.replace('%', '\\%')
        s = s.replace('[', '\\[').replace(']', '\\]')
        s = s.replace(',', '\\,')
        return s

    @staticmethod
    def _slate_font():
        """Absolute, filter-escaped path to the bundled slate font (fontconfig is unreliable
        on Windows, so drawtext is always handed an explicit fontfile=)."""
        f = Path(__file__).parent / 'assets' / 'slate.ttf'
        return FinalYouTubeGPUEngine._ff_filter_path(f)

    def _burn_in_vf(self, info):
        """Build the drawtext slate filter string (becomes vf_extra, applied re-encode only).

        Two stacked drawtext filters: a top-left info slate (title / uploader + date / URL)
        and a bottom-right running source timecode. All user text flows through
        _ff_drawtext_escape; the font path through _ff_filter_path. Resolution-independent
        (fontsize=h/30, padding via boxborderw)."""
        info = info or {}
        font = self._slate_font()
        title = self._ff_drawtext_escape(info.get('title') or '')
        uploader = str(info.get('uploader') or '')
        ud = str(info.get('upload_date') or '')
        date = f"{ud[0:4]}-{ud[4:6]}-{ud[6:8]}" if (len(ud) == 8 and ud.isdigit()) else ''
        byline = self._ff_drawtext_escape(' · '.join([x for x in (uploader, date) if x]))
        url = self._ff_drawtext_escape(info.get('webpage_url') or '')
        lines = [x for x in (title, byline, url) if x] or [self._ff_drawtext_escape('(untitled)')]
        slate_text = '\n'.join(lines)

        common = (f"fontfile={font}:fontcolor=white:fontsize=h/30:"
                  f"box=1:boxcolor=black@0.5:boxborderw=8")
        slate = (f"drawtext=text='{slate_text}':{common}:x=12:y=12")
        # %{pts:hms} -> running source timecode; the inner colon must be escaped for the parser.
        tc = (f"drawtext=text='%{{pts\\:hms}}':{common}:x=w-tw-12:y=h-th-12")
        return slate + ',' + tc

    def _audio_map_args(self, spec, out_ext, vf=None):
        """ffmpeg args (appended to _extra_out_args) that split a hard-panned stereo interview
        into two discrete mono tracks. Gated to MOV/MKV (2x mono in mp4 is poorly supported);
        returns [] for mp4 (the caller switches the container) and for the passthrough mode.

        When `vf` is given (a -vf graph from burn-in/subtitle-burn), it is FOLDED into this same
        filter_complex — ffmpeg rejects a simultaneous -vf and -filter_complex — routing video
        through [0:v]<vf>[vout] and mapping [vout] instead of 0:v."""
        if (out_ext or '').lower() not in ('.mov', '.mkv'):
            return []
        if spec.audio_map not in ('dual-mono', 'split'):
            return []
        if vf:
            fc = f'[0:v]{vf}[vout];[0:a]channelsplit=channel_layout=stereo[L][R]'
            vmap = ['-map', '[vout]']
        else:
            fc = '[0:a]channelsplit=channel_layout=stereo[L][R]'
            vmap = ['-map', '0:v']
        return [
            '-filter_complex', fc, *vmap, '-map', '[L]', '-map', '[R]',
            '-metadata:s:a:0', 'title=Mic L', '-metadata:s:a:1', 'title=Mic R',
        ]

    @staticmethod
    def _plan_audio_map(spec, mode, overrides, out_ext):
        """Pure planner for the --audio-map gate (factored out of _finalize so it is unit-testable).

        Returns (mode, overrides, out_ext) with: re-encode forced (filter_complex can't ride
        -c copy), a non-copy audio codec forced (channelsplit needs a real encode), the mp4
        container coerced to .mkv (2x mono support), and the channelsplit+map args EXTENDED onto
        any existing _extra_out_args (NEVER assigned — preserves Rec.709/HDR color flags)."""
        if spec.audio_map not in ('dual-mono', 'split'):
            return mode, overrides, out_ext
        if mode == 'copy':
            mode = 'reencode'
        if overrides.get('audio_codec') in (None, 'copy'):
            overrides['audio_codec'] = (spec.audio_codec if spec.audio_codec not in (None, 'copy')
                                        else 'pcm_s24le')
        if (out_ext or '').lower() not in ('.mov', '.mkv'):
            out_ext = '.mkv'   # channelsplit needs MOV/MKV; coerce ANY other container (webm/mp4/...)
        return mode, overrides, out_ext

    @staticmethod
    def _run_on_complete(cmd_template, path):
        """Run a user hook per finished file. '{}' (or {path}) is replaced with the path.
        Runs WITHOUT a shell and substitutes the path as a single argv token, so a video title
        containing shell metacharacters cannot inject commands."""
        try:
            import shlex
            if os.name == 'nt':
                # posix=False is required on Windows (posix=True eats the backslashes in
                # C:\Tools\hook.bat), but it does NOT strip quotes — it leaves them INSIDE the token,
                # so a quoted path becomes a literal '"C:\Program Files\x"' and CreateProcess fails.
                # Strip every quote (not just a surrounding pair: -i"{}" is legal Windows quoting)
                # BEFORE substitution, so a path that itself contains a quote is never mangled.
                # NOTE: shell=False means no SHELL metacharacter injection, but a .bat/.cmd TARGET
                # is still interpreted by cmd.exe once launched — argument content reaching a batch
                # hook is not automatically safe. Keep hook templates under your own control.
                # Remove only a MATCHED double-quote delimiter pair. Deleting every quote
                # character also ate apostrophes that are literal data (O'Brien) and any quote
                # inside a substituted path. ' is never a delimiter for CreateProcess, so it is
                # left alone entirely.
                def _dq(tok):
                    # Only the two shapes cmd-line quoting actually produces:
                    #   "C:\Program Files\x"   -> fully surrounding pair
                    #   -o"C:\out dir"          -> one trailing quoted segment
                    # Every other token is left BYTE-IDENTICAL; stripping on a mere even count
                    # deleted quotes that were literal data (foo"bar"baz).
                    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
                        return tok[1:-1]
                    if tok.count('"') == 2 and tok.endswith('"') and not tok.startswith('"'):
                        return tok.replace('"', '')
                    return tok
                argv = [_dq(t) for t in shlex.split(cmd_template, posix=False)]
            else:
                argv = shlex.split(cmd_template, posix=True)     # unchanged macOS/Linux path
            argv = [a.replace('{}', str(path)).replace('{path}', str(path)) for a in argv]
            if not argv:
                return
            logger.info(f"[on-complete] {argv}")
            subprocess.run(argv, shell=False, timeout=600)
        except Exception as e:
            logger.warning(f"[on-complete] hook failed: {e}")

    def encode_existing(self, inputs, profile="auto_h264", mode="reencode",
                        audio_codec=None, audio_bitrate=None, loudnorm=None):
        """Encode existing file(s) or dir(s). Dirs glob common video files; collision-safe output names.
        Optional audio overrides + a trailing EBU R128 loudnorm pass (A4)."""
        exts = ("*.mp4", "*.mkv", "*.webm", "*.mov", "*.m4v", "*.avi")
        results = []
        seen = set()
        overrides = {}
        if audio_codec:
            overrides['audio_codec'] = audio_codec
        if audio_bitrate:
            overrides['audio_bitrate'] = audio_bitrate
        plan = {'overrides': overrides or None}

        def _encode_one(f):
            if str(f) in seen:
                return
            seen.add(str(f))
            out_ext = f.suffix if mode == "copy" else self.processor.output_ext(profile)
            out = self.downloader._unique_path(f.with_name(f.stem + "_encoded").with_suffix(out_ext))
            if self._do_encode(f, out, mode, profile, plan):
                if loudnorm is not None and out.exists():
                    norm = self.downloader._unique_path(out.with_name(out.stem + '_ln' + out.suffix))
                    if self.processor.normalize_audio(out, norm, target_lufs=loudnorm):
                        out = norm
                results.append(out)

        for inp in inputs:
            p = Path(inp)
            if p.is_dir():
                files = sorted({g for pat in exts for g in p.glob(pat) if g.is_file()})
                for f in files:
                    _encode_one(f)
            elif p.is_file():
                _encode_one(p)
            else:
                logger.warning(f"[Encode] Not found: {p}")
        return results

    def _do_encode(self, src, dst, mode, profile, plan=None):
        plan = {} if plan is None else plan
        if mode == "copy":
            plan['_actual_profile'] = None
            # copy cannot apply a -vf graph; metadata/chapters/overrides still apply.
            return self.processor.fast_copy(src, dst,
                                            metadata=plan.get('metadata'),
                                            ffmetadata_file=plan.get('ffmetadata_file'),
                                            overrides=plan.get('overrides'))
        else:
            actual_profile = self.processor.encode_with_fallback(
                src,
                dst,
                profile,
                metadata=plan.get('metadata'),
                ffmetadata_file=plan.get('ffmetadata_file'),
                overrides=plan.get('overrides'),
                vf_extra=plan.get('vf_extra'),
            )
            plan['_actual_profile'] = actual_profile
            return actual_profile


# =============================================================================
# INTERACTIVE MENU (user-friendly menu like the original versions)
# =============================================================================
def interactive_menu(check_updates=True):
    """Simple, friendly text menu restored for users who prefer it over CLI."""
    print("\n" + "=" * 62)
    print("Final YouTube + GPU Tool - Interactive Menu")
    print("=" * 62)
    print("No command-line typing needed - just pick numbers!")
    print("Tip: You can still use subcommands for scripting (see --help).")

    # Throttled, non-blocking notice if a newer yt-dlp / plugin is available (you decide via option 10).
    if check_updates:
        UpdateChecker.passive_notice()

    while True:
        print("\n" + "-" * 45)
        print(" 1. Download single video  [best = max quality recommended]")
        print(" 2. Download multiple videos (parallel)")
        print(" 3. Download playlist (parallel)")
        print(" 4. Encode / Remux Existing File(s)")
        print(" 5. List Encoding Profiles")
        print(" 6. GPU & Hardware Information")
        print(" 7. Download History (this session)")
        print(" 8. Troubleshoot / Diagnostics (+speed tips)")
        print(" 9. Speed Priority Download (single, quality fallbacks)")
        print("10. Check for Updates (scan; you choose whether to apply)")
        print("11. Exit")
        print("-" * 45)

        choice = input("Select option (1-11): ").strip()

        if choice == "1":
            _download_interactive()
        elif choice == "2":
            _parallel_download_interactive()
        elif choice == "3":
            _playlist_download_interactive()
        elif choice == "4":
            _encode_interactive()
        elif choice == "5":
            _list_profiles_interactive()
        elif choice == "6":
            _gpu_info_interactive()
        elif choice == "7":
            _history_interactive()
        elif choice == "8":
            _troubleshoot_interactive()
        elif choice == "9":
            _speed_priority_download_interactive()
        elif choice == "10":
            _check_updates_interactive()
        elif choice == "11":
            print("\nThanks for using the tool! Goodbye.\n")
            break
        else:
            print("Invalid choice. Please enter a number 1-11.")


def _check_updates_interactive():
    print("\n--- Check for Updates ---")
    try:
        try:
            from managed_env import CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE
        except ImportError:
            from Final.managed_env import (  # type: ignore
                CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE,
            )
        print(
            "Certified Python update unavailable: "
            f"{CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE}"
        )
        print(
            "Install a future reviewed yt_final release to change "
            "Python dependencies."
        )
    except Exception as e:
        print(f"Error: {e}")
    input("\nPress Enter to return to menu...")


def _download_interactive():
    print("\n--- Download ---")
    url = input("YouTube URL: ").strip()
    if not url:
        print("No URL entered.")
        input("Press Enter to continue...")
        return

    print("\nQualities: 1080p (recommended, fast) | best | 4k | 1440p | 720p | 480p | audio")
    quality = input("Quality [1080p]: ").strip() or "1080p"

    print("\nCodec preference: h264 (smallest/fastest, edit-ready) | av1 | vp9 | any")
    codec = input("Codec [h264]: ").strip().lower() or "h264"
    if codec not in ("h264", "av1", "vp9", "any"):
        codec = "h264"

    section = input("Clip a section? (e.g. 0:00-2:00, Enter for full video): ").strip() or None

    print("\nEncode mode:")
    print("  copy     -> Fast copy/remux (recommended for most)")
    print("  reencode -> GPU re-encode (better for editing)")
    mode = input("Mode [copy]: ").strip().lower() or "copy"

    profile = "auto_h264"
    if mode == "reencode":
        print(f"\nProfile (default: {profile}). See option 5 for the full list.")
        p = input("Profile: ").strip()
        if p:
            profile = p

    # Speed tip: cookies often lift YouTube throttling.
    # Browser cookies are flaky on Windows when the browser is open (Chrome/Edge lock the DB).
    cfb = None
    cookiefile = None
    cookies_ans = input(
        "\nUse cookies for faster speeds? (chrome/edge/firefox | file | Enter to skip)\n"
        "  Tip: close browser completely before choosing chrome/edge, or use a cookies.txt (more reliable)\n> "
    ).strip().lower()

    if cookies_ans in ("edge", "chrome", "firefox", "brave"):
        cfb = cookies_ans
        print(f"[Speed] Will use cookies from {cookies_ans} (close the browser if it fails)")
    elif cookies_ans in ("f", "file", "cookies.txt"):
        cf_path = input("Path to cookies.txt file: ").strip().strip('"')
        if cf_path:
            cookiefile = cf_path
            print(f"[Speed] Will use cookie file: {cookiefile}")

    print("\nStarting...")
    try:
        engine = FinalYouTubeGPUEngine()
        engine.download_then_process(
            url, quality=quality, encode_mode=mode, profile=profile,
            cookies_from_browser=cfb, cookiefile=cookiefile, codec=codec, section=section
        )
    except Exception as e:
        print(f"Error: {e}")
    input("\nPress Enter to return to menu...")


def _encode_interactive():
    print("\n--- Encode Existing ---")
    path = input("File or folder path: ").strip()
    if not path:
        print("No path entered.")
        input("Press Enter to continue...")
        return

    print("\nMode: copy (fast) or reencode")
    mode = input("Mode [copy]: ").strip().lower() or "copy"

    profile = "auto_h264"
    if mode == "reencode":
        p = input(f"Profile [{profile}]: ").strip()
        if p:
            profile = p

    print("\nStarting encode...")
    try:
        engine = FinalYouTubeGPUEngine()
        engine.encode_existing([path], profile=profile, mode=mode)
    except Exception as e:
        print(f"Error: {e}")
    input("\nPress Enter to return to menu...")


def _list_profiles_interactive():
    print("\n--- Available Profiles ---")
    profiles = FFmpegProcessor._load_profiles()
    for k, v in profiles.items():
        print(f"  {k:25} -> {v['name']}")
    input("\nPress Enter to return to menu...")


def _gpu_info_interactive():
    print("\n--- GPU / Hardware Info ---")
    try:
        det = CapabilityDetector()
        print(det.gpu_info)
        rec = det.get_recommended_encoder()
        print(f"\nRecommended encoder: {rec}")
    except Exception as e:
        print(f"Error: {e}")
    input("\nPress Enter to return to menu...")


def _history_interactive():
    print("\n--- History ---")
    rows = []
    try:
        rows = _get_history_cls()().read(limit=25)
    except Exception:
        pass
    if not rows:
        _log_hint = f" or check {_log_path} for past activity" if _log_path else ""
        print(f"No history yet. Run some downloads first{_log_hint}.")
    else:
        for r in rows:
            ts = time.strftime('%Y-%m-%d %H:%M', time.localtime(r.get('ts', 0)))
            print(f"{ts}  {r.get('status', '?'):<6} {r.get('profile', '')}  "
                  f"{r.get('output') or r.get('url')}")
    input("\nPress Enter to return to menu...")


def _pooled_transport_available():
    """True when yt-dlp's pooled RequestsRH transport is usable in THIS runtime — i.e. requests>=2.32.2
    and urllib3>=2.0.2 satisfy yt-dlp's own import gate (yt_dlp/networking exposes _requests exactly
    then). False means only the unpooled urllib handler exists, which drops the TCP connection at every
    http_chunk_size (10 MiB) boundary and restarts slow-start — measured ~+33-67% slower on fast links.
    Derived from yt-dlp's own gate (not a re-check of versions) so it can never drift from what yt-dlp does."""
    try:
        import yt_dlp.networking._requests as _rq
        return getattr(_rq, 'RequestsRH', None) is not None
    except Exception:
        return False


def build_diagnose_result(detector=None, check_pypi=False):
    """Run LIVE toolchain probes and return a list of row dicts (NO printing).

    Each row: {'name': str, 'status': 'pass'|'warn'|'fail', 'detail': str, 'fix': str}.
    Reuses the existing probes (UpdateChecker, _runtime_version, the bgutil /ping check,
    CapabilityDetector) — adds no new detection logic. Every probe is wrapped so a single
    failure degrades that one row instead of crashing the whole command.

    `fail` is reserved for deterministic hard-missing or below-floor dependencies and tools.
    Staleness, missing PO-token provider and CPU-only encoding are `warn` only.
    Network (PyPI) is only hit when check_pypi=True (default False keeps tests/offline clean).
    """
    if detector is None:
        detector = CapabilityDetector()
    rows = []

    # --- Python ---
    try:
        vi = _sys.version_info
        pv = f"{vi.major}.{vi.minor}.{vi.micro}"
        if vi >= (3, 11):
            rows.append({'name': 'Python', 'status': 'pass', 'detail': f"Python {pv}", 'fix': ''})
        else:
            rows.append({'name': 'Python', 'status': 'fail',
                         'detail': f"Python {pv} is too old; requires >=3.11",
                         'fix': 'Install Python 3.11 or newer.'})
    except Exception as e:
        rows.append({'name': 'Python', 'status': 'fail', 'detail': f"probe error: {e}",
                     'fix': 'Install Python 3.11 or newer.'})

    # --- ffmpeg ---
    try:
        configured_ffmpeg = getattr(detector, 'ffmpeg_path', None)
        fpath = _existing_absolute_tool(configured_ffmpeg)
        if fpath is not None:
            ver = (
                detector.gpu_info.get('ffmpeg_version')
                if getattr(detector, 'gpu_info', None)
                else None
            ) or str(fpath)
            rows.append({'name': 'ffmpeg', 'status': 'pass', 'detail': str(ver), 'fix': ''})
        else:
            rows.append({'name': 'ffmpeg', 'status': 'fail',
                         'detail': f"not found ({configured_ffmpeg})",
                         'fix': 'Install ffmpeg and put it on PATH (https://ffmpeg.org/download.html).'})
    except Exception as e:
        rows.append({'name': 'ffmpeg', 'status': 'fail', 'detail': f"probe error: {e}",
                     'fix': 'Install ffmpeg and put it on PATH.'})

    # --- ffprobe (derive next to ffmpeg; ffprobe property lives on FFmpegProcessor, not detector) ---
    try:
        fpath = _existing_absolute_tool(
            getattr(detector, 'ffmpeg_path', None)
        )
        probe = None
        if fpath is not None:
            basename = fpath.name.casefold()
            if basename == 'ffmpeg.exe':
                probe = _existing_absolute_tool(fpath.with_name('ffprobe.exe'))
            elif basename == 'ffmpeg':
                probe = _existing_absolute_tool(fpath.with_name('ffprobe'))
        if probe is not None:
            rows.append({'name': 'ffprobe', 'status': 'pass',
                         'detail': str(probe), 'fix': ''})
        else:
            rows.append({'name': 'ffprobe', 'status': 'fail', 'detail': f"not found ({probe})",
                         'fix': 'ffprobe ships with ffmpeg; install the full ffmpeg build, not a stripped one.'})
    except Exception as e:
        rows.append({'name': 'ffprobe', 'status': 'fail', 'detail': f"probe error: {e}",
                     'fix': 'Install the full ffmpeg build (includes ffprobe).'})

    # --- declared runtime dependencies (+ optional yt-dlp staleness) ---
    try:
        floors = declared_runtime_floors()
        missing_floors = set(UpdateChecker.PIP_PACKAGES) - set(floors)
        if missing_floors:
            raise ValueError(f"missing declared floors: {', '.join(sorted(missing_floors))}")
        for package in UpdateChecker.PIP_PACKAGES:
            minimum = floors[package]
            # Diagnose only the trusted current process; app-writable Python
            # environments are never activated or executed.
            installed = UpdateChecker._installed(package) or installed_distribution_version(package)
            fix = "Install a future reviewed yt_final release."
            if not installed:
                rows.append({'name': package, 'status': 'fail',
                             'detail': f"not installed; requires >={minimum}", 'fix': fix})
                continue
            if not satisfies_floor(installed, minimum):
                rows.append({'name': package, 'status': 'fail',
                             'detail': f"{package} {installed}; requires >={minimum}", 'fix': fix})
                continue

            status, detail, row_fix = 'pass', f"{package} {installed}; requires >={minimum}", ''
            if package == 'yt-dlp' and check_pypi:
                try:
                    latest = UpdateChecker._pypi_latest(package)
                    if UpdateChecker._outdated(installed, latest):
                        status = 'warn'
                        detail = f"yt-dlp {installed} (latest {latest}); requires >={minimum}"
                        row_fix = fix
                except Exception:
                    pass
            rows.append({'name': package, 'status': status, 'detail': detail, 'fix': row_fix})
    except Exception as e:
        rows.append({'name': 'yt-dlp', 'status': 'fail', 'detail': f"dependency contract error: {e}",
                     'fix': "Install a future reviewed yt_final release."})

    # --- HTTP transport (pooled RequestsRH vs unpooled urllib) ---
    # yt-dlp prefers its pooled `requests` handler when requests/urllib3 meet the floors; without them
    # it silently uses the unpooled urllib handler (reconnects each 10 MiB chunk). Surface the ACTIVE
    # runtime's transport so a managed venv missing the fast handler is VISIBLE, not silently slow.
    try:
        if _pooled_transport_available():
            rows.append({'name': 'HTTP transport', 'status': 'pass',
                         'detail': 'pooled RequestsRH active (connections stay warm across chunks)',
                         'fix': ''})
        else:
            rows.append({'name': 'HTTP transport', 'status': 'warn',
                         'detail': 'unpooled urllib only — reconnects each 10 MiB chunk (~+33-67% slower)',
                         'fix': "Install a future reviewed yt_final release containing "
                                "requests>=2.32.2 + urllib3>=2.0.2."})
    except Exception as e:
        rows.append({'name': 'HTTP transport', 'status': 'warn', 'detail': f"probe error: {e}",
                     'fix': 'Install a future reviewed yt_final release with requests + urllib3.'})

    # --- JS runtime (nsig) : replicate _detect_js_runtimes logic without the engine ---
    try:
        min_major = {'deno': 2, 'node': 22}
        found, below = [], []
        for rt in ('deno', 'node', 'bun'):
            executable = _js_runtime_execution_path(rt)
            if executable:
                ver = YouTubeDownloader._runtime_version(executable)
                mj = ver[0] if ver else None
                label = f"{rt} {'.'.join(map(str, ver))}" if ver else rt
                if mj is not None and rt in min_major and mj < min_major[rt]:
                    below.append(label)
                else:
                    found.append(label)
        if found:
            rows.append({'name': 'JS runtime', 'status': 'pass',
                         'detail': 'nsig solver: ' + ', '.join(found), 'fix': ''})
        elif below:
            rows.append({'name': 'JS runtime', 'status': 'warn',
                         'detail': 'present but below recommended: ' + ', '.join(below),
                         'fix': 'Install the verified Node/Deno build; Python changes require a future reviewed release.'})
        else:
            rows.append({'name': 'JS runtime', 'status': 'fail',
                         'detail': 'no deno/node/bun -> nsig throttling unsolved',
                         'fix': 'Install verified Node or Deno; Python changes require a future reviewed release.'})
    except Exception as e:
        rows.append({'name': 'JS runtime', 'status': 'fail', 'detail': f"probe error: {e}",
                     'fix': 'Install verified Node or Deno; Python changes require a future reviewed release.'})

    # --- PO-token provider: probe only the explicitly addressed HTTP endpoint. ---
    try:
        mode, detail = None, 'no provider backend reachable'
        base = (os.getenv('BGUTIL_POT_BASE_URL') or 'http://127.0.0.1:4416').rstrip('/')
        try:
            import urllib.request
            with urllib.request.urlopen(base + '/ping', timeout=2) as r:
                if getattr(r, 'status', 200) == 200:
                    mode, detail = 'http', base
        except Exception:
            pass
        if mode:
            rows.append({'name': 'PO-token', 'status': 'pass',
                         'detail': f"provider active ({mode}: {detail})", 'fix': ''})
        else:
            rows.append({'name': 'PO-token', 'status': 'warn',
                         'detail': 'no provider reachable (works tokenless via android_vr)',
                         'fix': (
                             'Optional: after installing verified Node or Deno, configure the bgutil PO-token '
                             'helper manually only after reviewing its upstream provenance.'
                         )})
    except Exception as e:
        rows.append({'name': 'PO-token', 'status': 'warn', 'detail': f"probe error: {e}",
                     'fix': (
                         'Optional: after installing verified Node or Deno, configure the bgutil PO-token '
                         'helper manually only after reviewing its upstream provenance.'
                     )})

    # --- HW encoders ---
    try:
        working = (detector.gpu_info.get('working_encoders') if getattr(detector, 'gpu_info', None) else None) or []
        if working:
            rows.append({'name': 'HW encoders', 'status': 'pass',
                         'detail': 'verified: ' + ', '.join(working), 'fix': ''})
        else:
            rows.append({'name': 'HW encoders', 'status': 'warn',
                         'detail': 'CPU-only encoding; HW encoder not verified',
                         'fix': 'Install GPU drivers (NVENC/QSV/AMF) for faster encodes; libx264 still works.'})
    except Exception as e:
        rows.append({'name': 'HW encoders', 'status': 'warn', 'detail': f"probe error: {e}",
                     'fix': 'Install GPU drivers for HW encoding; libx264 still works.'})

    return rows


def run_diagnose(detector=None, check_pypi=False):
    """Live toolchain doctor. Prints a pass/warn/FAIL table and returns an exit code
    (1 if any hard check FAILs, else 0). Never raises — per-row probe errors are captured
    by build_diagnose_result and degraded to warn/fail."""
    try:
        rows = build_diagnose_result(detector=detector, check_pypi=check_pypi)
    except Exception as e:
        print(f"  diagnose probe crashed: {e}")
        return 1
    _label = {'pass': 'PASS', 'warn': 'warn', 'fail': 'FAIL'}
    print("\n=== diagnose: toolchain doctor ===")
    for r in rows:
        print(f"  {r['name']:14} {_label.get(r['status'], r['status']):6} {r['detail']}")
        if r['status'] != 'pass' and r.get('fix'):
            print(f"    fix: {r['fix']}")
    rc = 1 if any(r['status'] == 'fail' for r in rows) else 0
    if rc:
        print("\n  One or more hard checks FAILED (see fix lines above).")
    else:
        print("\n  All hard checks pass.")
    return rc


def _troubleshoot_interactive():
    print("\n--- Troubleshoot ---")
    try:
        det = CapabilityDetector()
        logger.info(f"[OK] FFmpeg: {det.ffmpeg_path}")
        logger.info(f"[GPU] {det.gpu_info.get('gpu_name')} | working HW encoders: {det.gpu_info.get('working_encoders')}")
        logger.info("[Speed] yt-dlp 2026.06.09 DISABLED aria2c/curl/wget for DASH/HLS (CVE-2026-50574); this tool now uses")
        logger.info("        the native downloader (-N parallelizes fragmented HLS/segments only; the default")
        logger.info("        single-URL DASH path is one connection). To lift a throttled ~20 MiB/s cap:")
        logger.info("        1) install a verified managed Deno/Node runtime — un-throttles nsig")
        logger.info("        2) install a future reviewed yt_final release for Python dependency changes")
        logger.info("        3) prefer 1080p H.264 (default) — ~3x less data than 4K VP9")
        logger.info("        4) use --cookies cookies.txt (browser DPAPI copy often fails on Windows, yt-dlp#7271)")
    except Exception as e:
        print(f"Error: {e}")
    input("\nPress Enter to return to menu...")


def _parallel_download_interactive():
    """Parallel download for multiple videos, modeled after youtube_downloader4.py"""
    print("\n--- Parallel Download Multiple Videos ---")
    print("Enter YouTube URLs (one per line, blank line to finish):")
    urls = []
    while True:
        u = input().strip()
        if not u:
            break
        urls.append(u)
    if not urls:
        print("No URLs entered.")
        input("Press Enter to continue...")
        return

    print(f"\n{len(urls)} URLs entered.")
    quality = input("Quality [best]: ").strip() or "best"
    workers_str = input(f"Parallel workers (1-16, Enter for auto ~{min(8, os.cpu_count() or 4)}): ").strip()
    workers = int(workers_str) if workers_str.isdigit() else None
    speed_p = input("Use speed priority fallbacks? (y/N): ").strip().lower() == 'y'

    print(f"\n🚀 Starting parallel download with {workers or 'auto'} workers...")
    cfb = None
    cookiefile = None
    cookies_ans = input("Cookies for speed? (chrome/edge | file | Enter skip): ").strip().lower()
    if cookies_ans in ("edge", "chrome", "firefox", "brave"):
        cfb = cookies_ans
    elif cookies_ans in ("f", "file"):
        cf = input("Path to cookies.txt: ").strip().strip('"')
        if cf: cookiefile = cf
    try:
        engine = FinalYouTubeGPUEngine()
        engine.download_then_process(
            urls, quality=quality, parallel=True, workers=workers, speed_priority=speed_p,
            cookies_from_browser=cfb, cookiefile=cookiefile
        )
    except Exception as e:
        print(f"Error: {e}")
    input("\nPress Enter to return to menu...")


def _playlist_download_interactive():
    """Download playlist with parallel, modeled after youtube_downloader4.py"""
    print("\n--- Download Playlist (parallel) ---")
    url = input("Playlist URL: ").strip()
    if not url:
        print("No URL entered.")
        input("Press Enter to continue...")
        return
    max_str = input("Max videos to download (Enter for all): ").strip()
    max_videos = int(max_str) if max_str.isdigit() else None
    quality = input("Quality [best]: ").strip() or "best"
    workers_str = input("Parallel workers (Enter for auto): ").strip()
    workers = int(workers_str) if workers_str.isdigit() else None

    print("\n📋 Extracting playlist...")
    try:
        # Use yt_dlp to extract playlist entries (lazy import)
        import yt_dlp
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'skip_download': True,
            'noplaylist': False,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', []) or []
            if max_videos:
                entries = entries[:max_videos]
            playlist_urls = []
            for e in entries:
                if e:
                    entry_url = e.get('url') or e.get('webpage_url')
                    if entry_url and 'youtube' in str(entry_url):
                        playlist_urls.append(entry_url)
                    else:
                        vid_id = e.get('id')
                        if vid_id:
                            playlist_urls.append(f"https://www.youtube.com/watch?v={vid_id}")
        if not playlist_urls:
            print("❌ No videos found in playlist.")
            input("Press Enter to continue...")
            return
        print(f"✅ Found {len(playlist_urls)} videos in playlist.")
        cfb = None
        cookiefile = None
        cookies_ans = input("Cookies for speed? (chrome/edge | file | Enter skip): ").strip().lower()
        if cookies_ans in ("edge", "chrome", "firefox", "brave"):
            cfb = cookies_ans
        elif cookies_ans in ("f", "file"):
            cf = input("Path to cookies.txt: ").strip().strip('"')
            if cf: cookiefile = cf
        engine = FinalYouTubeGPUEngine()
        engine.download_then_process(
            playlist_urls, quality=quality, parallel=True, workers=workers,
            cookies_from_browser=cfb, cookiefile=cookiefile
        )
    except Exception as e:
        print(f"Error extracting or downloading playlist: {e}")
    input("\nPress Enter to return to menu...")


def _speed_priority_download_interactive():
    """Speed priority like in youtube_downloader4.py"""
    print("\n--- Speed Priority Download (single video) ---")
    url = input("YouTube URL: ").strip()
    if not url:
        print("No URL entered.")
        input("Press Enter to continue...")
        return
    print("\nQualities: best (recommended) | 4k | 1440p | 1080p | 720p | 480p | audio")
    quality = input("Quality [best]: ").strip() or "best"
    print("\n🚀 Starting speed priority download (with quality fallbacks)...")
    cfb = None
    cookiefile = None
    cookies_ans = input("Cookies for speed? (chrome/edge | file | Enter skip): ").strip().lower()
    if cookies_ans in ("edge", "chrome", "firefox", "brave"):
        cfb = cookies_ans
    elif cookies_ans in ("f", "file"):
        cf = input("Path to cookies.txt: ").strip().strip('"')
        if cf: cookiefile = cf
    try:
        engine = FinalYouTubeGPUEngine()
        engine.download_then_process(
            url, quality=quality, speed_priority=True, cookies_from_browser=cfb, cookiefile=cookiefile
        )
    except Exception as e:
        print(f"Error: {e}")
    input("\nPress Enter to return to menu...")


# =============================================================================
# CLI
# =============================================================================
def _load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:
        logger.warning("[Config] tomllib unavailable (need Python 3.11+); cannot read TOML")
        return None
    try:
        return tomllib.loads(Path(path).read_text(encoding='utf-8'))
    except Exception as e:
        logger.warning(f"[Config] parse error in {path}: {e}")
        return None


def _discover_cookiefile(output_dir=None):
    """Return the first existing cookies.txt by precedence, or None. Logs which one.

    Precedence (first existing file wins):
      1. $YT_FINAL_COOKIES (treated as a full path to the file)
      2. ./cookies.txt (cwd)
      3. <output_dir>/cookies.txt (when output_dir is given)
      4. <config dir>/cookies.txt where config dir = $YT_FINAL_HOME or ~/.yt_final
    """
    candidates = []
    env_path = os.environ.get('YT_FINAL_COOKIES')
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / 'cookies.txt')
    if output_dir:
        candidates.append(Path(output_dir) / 'cookies.txt')
    config_dir = os.environ.get('YT_FINAL_HOME')
    config_dir = Path(config_dir) if config_dir else (Path.home() / '.yt_final')
    candidates.append(config_dir / 'cookies.txt')
    for path in candidates:
        try:
            if path.exists():
                logger.info(f"[Cookies] auto-discovered cookies.txt at {path}")
                return str(path)
        except (OSError, ValueError):
            continue
    return None


def _offer_auth_for_agegate(engine, spec) -> bool:
    """On an age-gate failure in an interactive terminal, offer Login/Import/Skip and retry once.
    Returns True iff a retry ran and produced output. Non-interactive -> False (no prompt)."""
    try:
        if not _sys.stdin.isatty():
            return False
    except Exception:
        return False
    print("\n\U0001F512 This video is age-restricted and needs a signed-in session.")
    try:
        choice = (input("   [L] Log in with a browser   [I] Import a cookies.txt   [S] Skip > ")
                  or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False

    import cookies_auth
    import cookie_providers as _cp
    cookiefile = None
    if choice == "l":
        launch = _cp.native_browser_login()
        if not launch.get("ok"):
            print(f"   Browser login failed: {launch.get('error') or 'unknown'}. Try [I] instead.")
            return False
        # Export managed profile → active.txt via the same path as WebUI login.
        try:
            from webserver import auth_login_session
            # native already ran; re-export only
            path = _cp._export_managed_profile(
                launch.get("profile_dir"), browser_name=launch.get("browser") or "chrome")
            if path:
                cookies_auth.replace_active_cookiefile(source=Path(path))
                try:
                    from webserver import _ensure_auth_enabled_in_config
                    _ensure_auth_enabled_in_config()
                except Exception:
                    pass
        except Exception:
            path = _cp.HeadlessProfileProvider(
                profile_dir=launch.get("profile_dir"),
                browser_name=launch.get("browser") or "chrome",
            ).acquire()
        cookiefile = cookies_auth.build_default_manager(
            output_dir=str(getattr(spec, "output_dir", None) or ".")).get_cookiefile()
        if not cookiefile:
            cookiefile = _cp.HeadlessProfileProvider(
                profile_dir=launch.get("profile_dir"),
                browser_name=launch.get("browser") or "chrome",
            ).acquire()
    elif choice == "i":
        try:
            path = input("   Path to cookies.txt > ").strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            return False
        src = Path(path)
        if not src.exists():
            print(f"   No such file: {src}")
            return False
        store = cookies_auth.cookie_store_dir()
        try:
            dest = cookies_auth.replace_active_cookiefile(source=src, store=store)
        except OSError as e:
            print(f"   Could not import cookies: {e}")
            return False
        cookiefile = str(dest)
    else:
        return False

    if not cookiefile:
        print("   Could not obtain cookies. Skipping.")
        return False
    spec.cookiefile = cookiefile
    print("   Retrying with your session...")
    result = engine.run(spec)
    return bool(result)


def _load_config_spec(config_file=None, preset_name=None):
    """Load a TOML config ([defaults] + [preset.NAME]) and return a base JobSpec, or None.
    Falls back to ~/.yt_final/config.toml when no explicit path is given but a preset is named."""
    path = None
    if config_file:
        path = Path(config_file)
    elif preset_name:
        # only fall back to the default config file when a preset is actually requested
        default = Path.home() / '.yt_final' / 'config.toml'
        if default.exists():
            path = default
    if not path or not path.exists():
        if config_file:
            logger.warning(f"[Config] not found: {config_file}")
        return None
    data = _load_toml(path)
    if data is None:
        return None
    merged = dict(data.get('defaults', {}) or {})
    if preset_name:
        preset = (data.get('preset', {}) or {}).get(preset_name)
        if preset is None:
            logger.warning(f"[Config] preset '{preset_name}' not found in {path}")
        else:
            merged.update(preset)
    logger.info(f"[Config] loaded {path.name}" + (f" + preset '{preset_name}'" if preset_name else ""))
    return JobSpec.from_dict(merged)


def _strip_file_sourced_hooks(specs, allow_hooks, source):
    """Drop `on_complete` from specs that came out of a FILE, unless explicitly allowed.

    `on_complete` runs a program when a job finishes (see `_run_on_complete`). Supplying it on the
    command line is fine -- you typed it. Reading it out of a batch manifest or a config.toml is a
    different thing entirely: those files get shared. Publishing this project creates exactly the
    culture that makes that dangerous, because manifests are what people paste into issues to
    reproduce a problem, and `batch --queue` persists the hook into queue.json where the
    always-running worker will honour it later.

    The web surface already treats this as a trust boundary and strips the field from any
    client-supplied job (`webserver.py`: "never let a web client supply a shell hook"). The CLI
    read it from a file with no such gate, and `--dry-run` did not show it. Same threat, same
    answer: opt in with --allow-hooks, or the hook is dropped and the user is told.

    Returns the number of hooks removed.
    """
    removed = 0
    for spec in specs:
        if not getattr(spec, 'on_complete', None):
            continue
        if allow_hooks:
            logger.warning(
                f"[Hook] {source} supplies on-complete: {spec.on_complete!r} "
                "(honored because --allow-hooks was given)")
            continue
        logger.warning(
            f"[Hook] IGNORED an on-complete command from {source}: {spec.on_complete!r}. "
            "A file that runs a program when a download finishes is only honored with "
            "--allow-hooks. Pass --on-complete on the command line if you meant it.")
        spec.on_complete = None
        removed += 1
    return removed


def _load_manifest(path):
    """Load a batch manifest (.json or .toml). Accepts a top-level array of job objects, or a
    {"jobs":[...]} / {"defaults":{...},"jobs":[...]} object. Returns a list of JobSpec."""
    p = Path(path)
    if not p.exists():
        logger.error(f"[Batch] manifest not found: {path}")
        return []
    if p.suffix.lower() == '.toml':
        data = _load_toml(p)
    else:
        import json as _json
        try:
            data = _json.loads(p.read_text(encoding='utf-8'))
        except Exception as e:
            logger.error(f"[Batch] JSON parse error: {e}")
            return []
    if data is None:
        return []
    defaults = {}
    if isinstance(data, dict):
        defaults = dict(data.get('defaults', {}) or {})
        jobs = data.get('jobs', [])
    elif isinstance(data, list):
        jobs = data
    else:
        logger.error("[Batch] manifest must be a list or an object with a 'jobs' array")
        return []
    specs = []
    for j in jobs:
        if isinstance(j, str):
            j = {'urls': [j]}
        merged = dict(defaults)
        merged.update(j or {})
        specs.append(JobSpec.from_dict(merged))
    return specs


def _resolve_redo_spec(ref, queue_file=None, history_file=None):
    """Resolve a `redo` reference to a prior job's durable JobSpec dict, or None.

    Resolution order:
      1. job_id (j_xxxx) in the queue journal -> job['spec']
      2. history ledger: "last"/-1 -> newest row; a (possibly negative) integer index into the rows.
    """
    ref = str(ref).strip()
    # 1) a queue job id
    if ref.startswith('j_'):
        try:
            job = _get_journal_cls()(queue_file).get(ref)
        except Exception:
            job = None
        if job and job.get('spec'):
            return job['spec']
        return None
    # 2) history ledger by index / "last"
    try:
        rows = _get_history_cls()(history_file).read()
    except Exception:
        rows = []
    if not rows:
        return None
    if ref.lower() == 'last':
        idx = -1
    else:
        try:
            idx = int(ref)
        except ValueError:
            return None
    try:
        row = rows[idx]
    except IndexError:
        return None
    return row.get('spec')


def _batch_failed(results) -> bool:
    """True when a batch/queue run genuinely lost an item, so main() can exit nonzero for
    scripts/CI. 'cancelled' (user chose it), 'skipped' and 'done' are NOT failures."""
    return any((r or {}).get('status') == 'failed' for r in (results or []))


NOTIFICATION_SHUTDOWN_TIMEOUT_S = 5.0


def _shutdown_engine_notifications(engine) -> bool:
    """Fence and bound CLI notification teardown without masking command outcomes."""
    try:
        complete = bool(engine.shutdown_notifications(
            timeout=NOTIFICATION_SHUTDOWN_TIMEOUT_S,
        ))
    except Exception:
        logger.exception("[Notify] dispatcher shutdown failed")
        return False
    if not complete:
        logger.warning(
            "[Notify] delivery still running after %.1fs shutdown budget",
            NOTIFICATION_SHUTDOWN_TIMEOUT_S,
        )
    return complete


def _render_partial_report(results, report_json=None):
    """Print a URL->status table + per-category counts for a batch/queue run, and (when report_json is
    given) atomically write a structured JSON report plus a re-runnable <file>.failures.json manifest.

    Each result is a dict: {'label'|'url', 'status', optional 'category', 'error', 'files', 'spec'}.
    'spec' (a JobSpec) is used to build the re-runnable failures manifest."""
    results = results or []
    # Build counts: done / skipped / failed:<category>.
    counts = {}
    for r in results:
        st = r.get('status', 'other')
        if st == 'failed':
            cat = r.get('category') or YouTubeDownloader._classify_error(r.get('error'))
            key = f'failed:{cat}'
        else:
            key = st
        counts[key] = counts.get(key, 0) + 1

    # Aligned URL->status table.
    label_w = max((len(str(r.get('label') or r.get('url') or '')) for r in results), default=0)
    label_w = min(max(label_w, 5), 70)
    print("Partial-failure report:")
    for r in results:
        label = str(r.get('label') or r.get('url') or '')[:label_w]
        st = r.get('status', 'other')
        if st == 'failed':
            cat = r.get('category') or YouTubeDownloader._classify_error(r.get('error'))
            st_disp = f'failed:{cat}'
        else:
            st_disp = st
        tail = ''
        if st == 'failed' and r.get('error'):
            tail = f"  ! {str(r.get('error'))[:60]}"
        print(f"  {label:<{label_w}}  {st_disp}{tail}")
    print("counts:", counts)

    if not report_json:
        return counts

    import json as _json, tempfile as _tempfile, datetime as _dt
    jobs_out = []
    failed_specs = []
    for r in results:
        st = r.get('status', 'other')
        cat = r.get('category') or (YouTubeDownloader._classify_error(r.get('error')) if st == 'failed' else None)
        jobs_out.append({
            'label': r.get('label') or r.get('url'),
            'status': st,
            'category': cat,
            'error': r.get('error'),
            'files': r.get('files') or [],
        })
        if st == 'failed' and r.get('spec') is not None:
            spec = r['spec']
            failed_specs.append(spec.to_dict() if hasattr(spec, 'to_dict') else dict(spec))

    def _atomic_write_json(path, payload):
        path = str(path)
        d = os.path.dirname(os.path.abspath(path)) or '.'
        fd, tmp = _tempfile.mkstemp(dir=d, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                _json.dump(payload, fh, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    report = {
        'generated': _dt.datetime.now().isoformat(timespec='seconds'),
        'counts': counts,
        'jobs': jobs_out,
    }
    try:
        _atomic_write_json(report_json, report)
        logger.info(f"[Report] wrote {report_json}")
        # Re-runnable failures manifest: directly consumable by `batch <file>.failures.json`.
        failures_path = str(report_json) + '.failures.json'
        _atomic_write_json(failures_path, {'jobs': failed_specs})
        logger.info(f"[Report] wrote {failures_path} ({len(failed_specs)} re-runnable job(s))")
    except Exception as e:
        logger.error(f"[Report] could not write report: {e}")
    return counts


def _explicit_overrides(args, subparser, root_parser=None):
    """Return the JobSpec fields the user actually passed on the CLI (value != its parser default),
    so config/preset values are only overridden by explicit flags.

    Some options (notably --output-dir) are registered on the TOP-LEVEL parser, not the `download`
    subparser. `subparser.get_default('output_dir')` then returns a spurious None (the subparser
    never defined that dest), so `val != default` was always True and a preset's output_dir got
    clobbered by the parent parser's default on every run. Compare against whichever parser actually
    owns the option.

    A pure value-vs-default comparison also can't tell "user didn't pass the flag" from "user
    explicitly passed the flag with a value equal to its own argparse default" (e.g. `--quality 1080p`
    when 1080p is also the default). That would silently drop the explicit flag and let a preset's
    differing value win. To disambiguate we additionally scan the raw CLI tokens for each option's
    strings: any dest whose flag was literally typed is ALWAYS treated as an override, regardless of
    whether its value happens to coincide with the parser default."""
    sub_dests = {a.dest for a in getattr(subparser, '_actions', [])}
    root_dests = {a.dest for a in getattr(root_parser, '_actions', [])} if root_parser is not None else set()

    # Map every option string ("--quality", "-q", …) to its dest, then walk the raw argv to see which
    # flags the user literally typed. Handles both "--flag value" and "--flag=value" forms.
    opt_to_dest = {}
    for prs in (subparser, root_parser):
        for a in getattr(prs, '_actions', []) if prs is not None else []:
            for optstr in getattr(a, 'option_strings', []) or []:
                opt_to_dest[optstr] = a.dest
    supplied = set()
    for tok in _sys.argv[1:]:
        if not tok.startswith('-'):
            continue
        key = tok.split('=', 1)[0]
        dest = opt_to_dest.get(key)
        if dest is not None:
            supplied.add(dest)

    out = {}
    for name in JobSpec._field_names():
        if not hasattr(args, name):
            continue
        val = getattr(args, name)
        if val is None:
            continue
        # Prefer the subparser's default; if the option lives only on the top-level parser, compare
        # against THAT default so a value equal to the real default is not treated as an override.
        if name not in sub_dests and name in root_dests:
            default = root_parser.get_default(name)
        else:
            try:
                default = subparser.get_default(name)
            except Exception:
                default = None
        # An explicitly-typed flag always wins; otherwise fall back to the value-vs-default heuristic.
        if name in supplied or val != default:
            out[name] = val
    return out


def _cmd_auth(args) -> int:
    import cookies_auth as _ca
    sub = getattr(args, "auth_cmd", None)
    store = _ca.cookie_store_dir()
    if sub == "status":
        mgr = _ca.build_default_manager()
        st = mgr.status()
        print("[auth] status:")
        for k in ("enabled", "source", "valid", "last_validated_at", "tiers_available"):
            print(f"  {k}: {st.get(k)}")
        return 0
    if sub == "import":
        src = Path(args.path)
        if not src.exists():
            logger.error(f"[auth] no such file: {src}"); return 1
        try:
            dest = _ca.replace_active_cookiefile(source=src, store=store)
        except OSError as e:
            logger.error(f"[auth] could not import cookies: {e}"); return 1
        print(f"[auth] imported cookies -> {dest}")
        return 0
    if sub == "login":
        # Real Chrome/Edge/Brave (no Playwright) — same as WebUI Auth → Login…
        try:
            from webserver import auth_login_session
            r = auth_login_session()
            print(r.get("message") or r.get("error") or r)
            return 0 if r.get("ok") else 1
        except Exception as e:
            import cookie_providers as _cp
            launch = _cp.native_browser_login()
            if not launch.get("ok"):
                print(launch.get("error") or e)
                return 1
            path = _cp._export_managed_profile(
                launch.get("profile_dir"), browser_name=launch.get("browser") or "chrome")
            print(f"[auth] profile cookies -> {path}" if path else "[auth] no cookies exported")
            return 0 if path else 1
    if sub == "refresh":
        mgr = _ca.build_default_manager()
        cf = mgr.refresh_cookiefile()
        print(f"[auth] refreshed: {cf}" if cf else "[auth] no valid cookies obtained")
        return 0 if cf else 1
    if sub == "test":
        mgr = _ca.build_default_manager()
        cf = mgr.get_cookiefile()
        print(f"[auth] valid cookies: {cf}" if cf else "[auth] validation FAILED / no cookies")
        return 0 if cf else 1
    if sub == "clear":
        try:
            _ca.clear_cookie_store(store)
        except OSError as e:
            logger.error(f"[auth] could not clear cookies: {e}")
            return 1
        print("[auth] cleared stored cookies")
        return 0
    logger.error("[auth] unknown subcommand; use status|login|import|refresh|test|clear")
    return 2


def main():
    parser = argparse.ArgumentParser(
        description="Final YouTube + GPU Tool (Best of 2025 merged & fixed)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python yt_final.py download URL --quality best --encode-mode copy
  python yt_final.py download URL --quality best --cookies-from-browser chrome
  python yt_final.py download URL --cookies cookies.txt
  python yt_final.py encode ./video.mp4 --profile premiere_h264_fast
"""
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument('--output-dir', default=Path('./downloads'), type=Path)
    parser.add_argument('--pot-server', dest='pot_base_url', default=None,
                        help='bgutil PO-token provider base URL (default http://127.0.0.1:4416 if running). '
                             'A reachable provider unlocks the fast fragmented web clients for higher throughput.')
    parser.add_argument('--allow-hooks', dest='allow_hooks', action='store_true', default=False,
                        help='Honor an on-complete command read from a batch manifest '
                             'or config file. Off by default: those files get shared, '
                             'and the field runs a program.')
    parser.add_argument('--pot-mode', dest='pot_mode', default='auto',
                        choices=['auto', 'http', 'script', 'off', 'lazy'],
                        help='PO-token backend: auto (detect; default), http (bgutil server :4416), '
                             'script (on-demand node generate_once.js), off (no tokens / direct-URL clients), '
                             'lazy (android_vr first, ~2.8s faster extraction; escalates to PO only on '
                             'age-gate / made-for-kids / no-format failures)')
    parser.add_argument('--no-update-check', dest='no_update_check', action='store_true',
                        help='Skip the throttled "is a newer yt-dlp/plugin available?" notice on launch')
    parser.add_argument('--config', dest='config_file', default=None,
                        help='TOML config with [defaults] and [preset.NAME] tables to seed job settings')
    parser.add_argument('--preset', dest='preset_name', default=None,
                        help='Named preset from the --config file (or ~/.yt_final/config.toml) to apply')

    sub = parser.add_subparsers(dest='cmd', title='subcommands', metavar='COMMAND')

    dl = sub.add_parser('download')
    dl.add_argument('urls', nargs='+')
    dl.add_argument('--quality', default='1080p', choices=['best','4k','1440p','1080p','720p','480p','audio'])
    dl.add_argument('--parallel', action='store_true')
    dl.add_argument('--workers', type=int, default=None, help='Max parallel workers')
    dl.add_argument('--encode-mode', choices=['none', 'copy', 'reencode'], default='copy')
    dl.add_argument('--profile', default='auto_h264')
    dl.add_argument('--speed-priority', action='store_true', help='Use quality fallbacks for speed (from 4.py)')
    dl.add_argument('--cookies-from-browser', dest='cookies_from_browser', default=None,
                    help='Browser cookies (edge/chrome/firefox) - close browser first or it often fails')
    dl.add_argument('--cookies', dest='cookiefile', default=None,
                    help='cookies.txt file (more reliable). Export with extension like "Get cookies.txt"')
    dl.add_argument('--codec', default=None, choices=['h264', 'vp9', 'av1', 'any'],
                    help='Preferred video codec for selection (default: h264 = smaller, edit-friendly, faster)')
    dl.add_argument('--section', default=None,
                    help='Download only a time range, e.g. "0:00-2:00" or "90-210" (great for speed tests)')
    dl.add_argument('--format-id', dest='format_id', default=None,
                    help='Pass an exact yt-dlp format id (e.g. "137+140" or "22"); overrides --quality/--codec selection')
    dl.add_argument('--simulate', dest='simulate', action='store_true',
                    help='Resolve and print the download plan (format/profile/container/est size/sidecars/output path) and exit WITHOUT downloading')
    dl.add_argument('--slow-requests', dest='slow_requests', action='store_true',
                    help='Sleep 2s between extraction requests to recover from IP soft-ban after heavy testing')
    dl.add_argument('--source-address', dest='source_address', default=None,
                    help='Bind to this local IP address (NIC selection, useful for soft-ban recovery)')
    # --- audio (A1) ---
    dl.add_argument('--audio-format', dest='audio_format', default=None,
                    choices=['mp3', 'aac', 'opus', 'flac', 'wav', 'm4a', 'alac', 'best'],
                    help='For --quality audio: target audio format ("best" = remux native bestaudio, no transcode)')
    dl.add_argument('--audio-quality', dest='audio_quality', default=None,
                    help='Audio bitrate (e.g. 320) or VBR 0-10 for the audio-only extractor')
    dl.add_argument('--audio-codec', dest='audio_codec', default=None,
                    help='Override the audio codec of a video profile (copy|aac|pcm_s24le|flac|libopus)')
    dl.add_argument('--audio-bitrate', dest='audio_bitrate', default=None,
                    help='Override the audio bitrate of a video profile (e.g. 320k)')
    dl.add_argument('--audio-map', dest='audio_map', choices=['stereo', 'dual-mono', 'split'], default=None,
                    help='Split a hard-panned stereo interview into two discrete mono tracks '
                         '(dual-mono/split). Gated to MOV/MKV; forces re-mux/re-encode.')
    # --- timeline metadata (B1/B2/B3/B6) ---
    dl.add_argument('--no-chapters', dest='chapters', action='store_false', default=True,
                    help='Do NOT write YouTube chapters as container chapter markers (default: write them)')
    dl.add_argument('--no-metadata', dest='embed_metadata', action='store_false', default=True,
                    help='Do NOT tag title/uploader/date/url into the container (default: tag)')
    dl.add_argument('--embed-thumbnail', dest='embed_thumbnail', action='store_true',
                    help='Embed the video thumbnail as cover art')
    dl.add_argument('--sub-langs', dest='sub_langs', default=None,
                    help='Comma list of subtitle languages to fetch, e.g. "en,es" (writes .srt sidecars)')
    dl.add_argument('--sub-mode', dest='sub_mode', default=None, choices=['sidecar', 'embed', 'burn'],
                    help='What to do with fetched subtitles (default sidecar)')
    dl.add_argument('--sub-auto', dest='sub_auto', action='store_true',
                    help='Also fetch auto-generated (ASR) captions')
    dl.add_argument('--sub-format', dest='sub_format', default='srt', choices=['srt', 'vtt', 'both'],
                    help='Subtitle sidecar format: srt (default), vtt (Jellyfin/Plex/browser), or both')
    dl.add_argument('--write-sidecar', dest='write_sidecar', action='store_true',
                    help='Write the full info.json next to the media (MAM provenance)')
    dl.add_argument('--write-sidecar-text', dest='write_sidecar_text', action='store_true',
                    help='Write <stem>.description.txt and <stem>.chapters.vtt sidecars (Jellyfin/Plex/browser)')
    dl.add_argument('--export-markers', dest='export_markers', default=None,
                    help='Export YouTube chapters as NLE timeline markers: edl|csv|fcpxml|all (comma list). '
                         'EDL=DaVinci Resolve, CSV=Premiere, FCPXML=Final Cut/Premiere.')
    dl.add_argument('--sponsorblock-markers', dest='sponsorblock_markers', default=None,
                    help='Emit color-coded SponsorBlock category markers (Sponsor/Intro/Outro/...) as '
                         '.sponsorblock.* sidecars: edl|csv|fcpxml|all. Requires --sponsorblock mark.')
    dl.add_argument('--no-shorts', dest='no_shorts', action='store_true',
                    help='Skip YouTube Shorts (<=60s or /shorts/ URLs) — useful for channel/playlist pulls')
    dl.add_argument('--write-nfo', dest='write_nfo', action='store_true',
                    help='Write a Kodi/Jellyfin/Plex .nfo sidecar (title/uploader/date/thumbnail)')
    dl.add_argument('--nfo-kind', dest='nfo_kind', choices=['auto', 'movie', 'episode'], default='auto',
                    help='NFO schema: auto (episode if part of a playlist/series, else movie), '
                         'or force movie/episode')
    dl.add_argument('--write-artwork', dest='write_artwork', action='store_true',
                    help='Write sibling poster.jpg / fanart.jpg / folder.jpg from the thumbnail for '
                         'media-server tiles')
    dl.add_argument('--proxy-pair', dest='proxy_pair', action='store_true',
                    help='Also produce a half-res H.264 editing proxy next to the master')
    dl.add_argument('--burn-in', dest='burn_in', action='store_true',
                    help='Burn a drawtext slate (title/uploader/date/URL) + source timecode into the '
                         'video. Forces re-encode; skipped on -c copy and on CUDA-surface profiles.')
    dl.add_argument('--notify-url', dest='notify_url', default=None,
                    help='POST a JSON job-complete payload to this URL (Discord/ntfy/Home Assistant webhook)')
    dl.add_argument('--notify-template', dest='notify_template', default='raw',
                    choices=['discord', 'slack', 'ntfy', 'raw'],
                    help='Shape the --notify-url POST for a channel: discord|slack|ntfy|raw (default raw)')
    # --- sponsor / loudness / color ---
    dl.add_argument('--sponsorblock', dest='sponsorblock', default=None, choices=['mark', 'remove'],
                    help='SponsorBlock: mark segments as chapters, or remove them')
    dl.add_argument('--loudnorm', dest='loudnorm', type=float, default=None,
                    help='Normalize loudness to this integrated LUFS (e.g. -14 for web, -16 for podcast)')
    dl.add_argument('--no-hdr', dest='preserve_hdr', action='store_false', default=True,
                    help='Do NOT carry HDR10/HLG color metadata through a re-encode')
    dl.add_argument('--color-tag', dest='color_tag', choices=['auto', 'off'], default='auto',
                    help='Stamp Rec.709 on untagged/unknown SDR streams during re-encode '
                         '(auto=fill only missing tags; off=never). Copy/remux is never tagged.')
    dl.add_argument('--verify-strict', dest='verify_strict', action='store_true', default=False,
                    help='ffprobe the output and FAIL the job if duration/streams/size look wrong (else just warn)')
    # --- archive / config / automation ---
    dl.add_argument('--download-archive', dest='download_archive', default=None,
                    help='Skip videos already recorded in this archive file (great for channel/playlist sync)')
    dl.add_argument('--proxy', dest='proxy', default=None, help='HTTP/SOCKS proxy URL for downloads')
    dl.add_argument('--download-engine', dest='download_engine', default='auto',
                    choices=['auto', 'builtin', 'hybrid', 'aria2', 'turbo'],
                    help='Fetch path: auto/builtin (default native -N), hybrid (opt-in smart), aria2, turbo')
    dl.add_argument('--concurrent-fragments', dest='concurrent_fragments', type=int, default=None,
                    help='Static override for the number of parallel download fragments (-N)')
    dl.add_argument('--output-template', dest='output_template', default=None,
                    help='yt-dlp output template override')
    dl.add_argument('--organize-by', dest='organize_by', default=None, choices=['uploader', 'date'],
                    help='Place each file in a subfolder by uploader or date')
    dl.add_argument('--on-complete', dest='on_complete', default=None,
                    help='Shell command run per finished file ({} or {path} -> the file path)')

    enc = sub.add_parser('encode')
    enc.add_argument('inputs', nargs='+')
    enc.add_argument('--profile', default='auto_h264')
    enc.add_argument('--mode', choices=['copy', 'reencode'], default='reencode')
    enc.add_argument('--audio-codec', dest='audio_codec', default=None,
                     help='Override audio codec (copy|aac|pcm_s24le|flac|libopus)')
    enc.add_argument('--audio-bitrate', dest='audio_bitrate', default=None, help='Override audio bitrate (e.g. 320k)')
    enc.add_argument('--loudnorm', dest='loudnorm', type=float, default=None,
                     help='Normalize loudness to this integrated LUFS after encoding')

    sub.add_parser('gpu-info')
    sub.add_parser('list-profiles')
    hist = sub.add_parser('history', help='Show the persistent download history ledger')
    hist.add_argument('--limit', type=int, default=50)
    hist.add_argument('--history-file', dest='history_file', default=None)
    hist.add_argument('--json', dest='history_json', action='store_true', help='Emit raw JSONL rows')
    sub.add_parser('troubleshoot')
    sub.add_parser('export-cookies',
                   help="How to export cookies.txt from your browser (bot-wall fix) and where to put it")
    authp = sub.add_parser('auth', help='Manage authenticated-download cookies (status/login/import/refresh/test/clear)')
    asub = authp.add_subparsers(dest='auth_cmd', metavar='ACTION')
    asub.add_parser('status')
    asub.add_parser('login', help='One-time interactive login into a managed profile (needs yt-final[auth])')
    ai = asub.add_parser('import'); ai.add_argument('path', help='cookies.txt to adopt')
    asub.add_parser('refresh', help='Force re-acquire + validate now')
    asub.add_parser('test', help='Validate current cookies against a gated probe video')
    asub.add_parser('clear', help='Delete stored cookies/profile state')
    diag = sub.add_parser('diagnose', help='Live toolchain doctor: pass/warn/FAIL table, nonzero exit on hard failure')
    diag.add_argument('--check-pypi', dest='check_pypi', action='store_true', default=False,
                      help='Also probe PyPI for yt-dlp staleness (network; staleness is a warn, never a hard fail)')
    sub.add_parser(
        'check-updates',
        help='Report installed Python packages and optional tool status (read-only)',
    )
    upd = sub.add_parser(
        'update',
        help='Python updates require installing a future reviewed release',
        description=(
            'Certified Python runtime activation is unavailable in 1.0.0; '
            'install a future reviewed release.'
        ),
    )
    upd.add_argument('--all', dest='update_all', action='store_true',
                     help='Compatibility alias; Python updates still require a future reviewed release')
    upd.add_argument('--yes', dest='assume_yes', action='store_true',
                     help='Compatibility alias; no prompt or Python mutation occurs')

    # --- batch manifest (C2): declarative array of jobs, the dependency-free full-stack surface ---
    bat = sub.add_parser('batch', help='Run a JSON/TOML manifest: an array of job specs')
    bat.add_argument('manifest', help='Path to a .json or .toml manifest (array of job objects, or {jobs:[...]})')
    bat.add_argument('--dry-run', dest='dry_run', action='store_true', help='Parse + print the plan, download nothing')
    bat.add_argument('--queue', dest='to_queue', action='store_true',
                     help='Enqueue into the persistent journal instead of running inline')
    bat.add_argument('--report-json', dest='report_json', default=None,
                     help='Write a structured URL->status report (+ <file>.failures.json re-runnable manifest)')

    # --- persistent queue (C1) ---
    q = sub.add_parser('queue', help='Persistent download queue (add/list/run/retry-failed/clear)')
    q.add_argument('action', choices=['add', 'list', 'run', 'retry-failed', 'clear',
                                       'bump', 'pause', 'resume', 'cancel'])
    q.add_argument('urls', nargs='*', help='URLs (for "add")')
    q.add_argument('--priority', type=int, default=None,
                   help='Priority for queue add/bump (higher runs first; default 0)')
    q.add_argument('--id', dest='job_id', default=None, help='Target job id (for bump/cancel)')
    q.add_argument('--json', dest='spec_json', default=None, help='Full JobSpec as a JSON string (for "add")')
    q.add_argument('--quality', default='1080p')
    q.add_argument('--encode-mode', dest='encode_mode', choices=['none', 'copy', 'reencode'], default='copy')
    q.add_argument('--profile', default='auto_h264')
    q.add_argument('--queue-file', dest='queue_file', default=None, help='Journal path (default ~/.yt_final/queue.json)')
    q.add_argument('--report-json', dest='report_json', default=None,
                   help='For "run": write a structured URL->status report (+ <file>.failures.json re-runnable manifest)')

    # --- re-run a prior job with tweaks (clone durable JobSpec + JobSpec.merged()) ---
    rd = sub.add_parser('redo', help='Re-run a prior job from history/queue with optional tweaks')
    rd.add_argument('ref', help='history index (e.g. 3 or -1), job_id (j_xxxx), or "last"')
    rd.add_argument('--profile')
    rd.add_argument('--encode-mode', dest='encode_mode')
    rd.add_argument('--quality')
    rd.add_argument('--codec')
    rd.add_argument('--output-dir', dest='redo_output_dir', default=None)
    rd.add_argument('--queue', dest='to_queue', action='store_true',
                    help='Enqueue instead of running inline')
    rd.add_argument('--queue-file', dest='queue_file', default=None)
    rd.add_argument('--history-file', dest='history_file', default=None)

    # --- local HTTP server + web UI (C4) ---
    srv = sub.add_parser('serve', help='Start the local web UI (opens in your browser)')
    srv.add_argument('--host', default='127.0.0.1',
                     help='Loopback bind host (default 127.0.0.1)')
    srv.add_argument('--port', type=int, default=8765)
    srv.add_argument('--queue-file', dest='queue_file', default=None)
    srv.add_argument('--no-browser', dest='no_browser', action='store_true',
                     help='Do not auto-open the browser (for advanced use)')

    # --- standalone loudness normalize (A4) ---
    nrm = sub.add_parser('normalize', help='EBU R128 two-pass loudness normalize existing file(s)')
    nrm.add_argument('inputs', nargs='+')
    nrm.add_argument('--lufs', type=float, default=-14.0, help='Target integrated loudness (default -14)')
    nrm.add_argument('--tp', type=float, default=-1.5, help='Max true peak dBTP (default -1.5)')

    # --- format audit / dry-run of selection (audit-formats) ---
    lf = sub.add_parser('list-formats', help='List the formats YouTube offers for a URL (no download)')
    lf.add_argument('url')
    lf.add_argument('--json', dest='lf_json', action='store_true',
                    help='Emit the format list as machine-readable JSON instead of the yt-dlp table')
    lf.add_argument('--format-id', dest='lf_format_id', default=None,
                    help='Restrict/echo a single format id (validates it exists; prints just that row)')

    # --- channel subscribe / auto-archive via RSS (no API key) ---
    wt = sub.add_parser('watch', help='Subscribe to a channel via its RSS feed and grab new uploads')
    wt.add_argument('channel', help='Channel URL, @handle, or channel_id (UC...)')
    wt.add_argument('--archive', dest='watch_archive', default=None,
                    help='Seen-video archive file (default ~/.yt_final/watch_<channelid>.txt)')
    wt.add_argument('--interval', type=int, default=0,
                    help='Poll every N seconds (0 = run once and exit)')
    wt.add_argument('--enqueue', dest='watch_enqueue', action='store_true',
                    help='Add new videos to the persistent queue instead of downloading inline')
    wt.add_argument('--max', dest='watch_max', type=int, default=0,
                    help='Cap how many new videos to take per poll (0 = all in the feed)')
    wt.add_argument('--quality', default='1080p')
    wt.add_argument('--encode-mode', dest='encode_mode', choices=['none', 'copy', 'reencode'], default='copy')
    wt.add_argument('--profile', default='auto_h264')
    wt.add_argument('--no-shorts', dest='no_shorts', action='store_true')
    wt.add_argument('--filter-title', dest='filter_title', default=None,
                    help='only take uploads whose title matches this regex (case-insensitive)')
    wt.add_argument('--min-duration', dest='min_duration', type=int, default=None,
                    help='skip uploads shorter than N seconds')
    wt.add_argument('--max-duration', dest='max_duration', type=int, default=None,
                    help='skip uploads longer than N seconds')
    wt.add_argument('--after', dest='watch_after', default=None,
                    help='only take uploads published on/after YYYYMMDD')
    wt.add_argument('--queue-file', dest='queue_file', default=None)

    args = parser.parse_args()

    _warn_if_file_logging_unavailable()

    if not args.cmd:
        # The interactive menu reads from stdin. A shortcut, a scheduled task, a pythonw launch or
        # any redirected invocation has no TTY, and the menu's first input() then died with a bare
        # EOFError traceback. Print help and exit 2, which is what a CLI is expected to do.
        if not _sys.stdin or not _sys.stdin.isatty():
            parser.print_help()
            print("No subcommand given and no interactive terminal attached.",
                  file=_sys.stderr)
            return 2
        # --no-update-check was evaluated BELOW this branch, so on the bare launch it was silently
        # ignored and interactive_menu() queried pypi.org anyway -- exactly the flag an offline,
        # air-gapped or metered user reaches for.
        interactive_menu(check_updates=not getattr(args, 'no_update_check', False))
        return

    # Throttled, non-blocking "update available?" notice before real work (never auto-applies).
    if args.cmd in ('download', 'encode') and not getattr(args, 'no_update_check', False):
        UpdateChecker.passive_notice(pot_base_url=getattr(args, 'pot_base_url', None))

    if args.cmd == 'check-updates':
        UpdateChecker.print_report(UpdateChecker.scan(pot_base_url=getattr(args, 'pot_base_url', None)))
        return
    if args.cmd == 'update':
        UpdateChecker.run_update(all_=getattr(args, 'update_all', False),
                                 assume_yes=getattr(args, 'assume_yes', False),
                                 pot_base_url=getattr(args, 'pot_base_url', None))
        return

    if args.cmd in ('download', 'encode'):
        engine = FinalYouTubeGPUEngine(args.output_dir, pot_base_url=getattr(args, 'pot_base_url', None),
                                       pot_mode=getattr(args, 'pot_mode', 'auto'))
        try:
            if args.cmd == 'download':
                spec = JobSpec.from_args(args)
                # Only layer config/preset when the user explicitly opted in — the bare fast-path must
                # never silently pick up ~/.yt_final/config.toml (keeps it byte-for-byte unchanged).
                if getattr(args, 'config_file', None) or getattr(args, 'preset_name', None):
                    base = _load_config_spec(args.config_file, args.preset_name)
                    if base is not None:
                        _strip_file_sourced_hooks(
                            [base], getattr(args, 'allow_hooks', False), 'the config file')
                    if base is not None:
                        spec = base.merged(**_explicit_overrides(args, dl, parser))
                # Auto-discover a cookies.txt only when the user passed NEITHER --cookies NOR
                # --cookies-from-browser (an explicit flag, incl. one from config/preset, disables it).
                # Runs AFTER the config/preset merge so the discovered file survives the spec rebuild.
                if not spec.cookiefile and not spec.cookies_from_browser:
                    found = _discover_cookiefile(output_dir=str(args.output_dir))
                    if found:
                        spec.cookiefile = found
                if getattr(args, 'simulate', False):
                    plan = engine.simulate(spec)
                    _sys.exit(0 if plan.get('ok') else 1)
                try:
                    result = engine.run(spec)
                    if not result and getattr(engine.downloader, 'last_error_kind', None) == 'agegate':
                        # _offer_auth_for_agegate returns True iff a retry actually produced output.
                        if _offer_auth_for_agegate(engine, spec):
                            result = True
                        else:
                            logger.error(getattr(engine.downloader, 'last_error', None)
                                         or "Age-restricted video — see: python yt_final.py auth login")
                except VerificationError as e:
                    logger.error(str(e))
                    _sys.exit(1)
                # A totally failed download (bot-check, format, network, 'Video unavailable', …) must
                # propagate a non-zero exit so shell/CI `download URL && next_step` doesn't march on.
                # But an empty result with NO recorded error means every URL was skipped by
                # --download-archive ("nothing new"), which is a success for scripting purposes.
                if not result and (getattr(engine.downloader, 'last_error', None)
                                   or getattr(engine.downloader, 'last_error_kind', None)):
                    _sys.exit(1)
            else:
                encoded = engine.encode_existing(args.inputs, profile=args.profile, mode=args.mode,
                                                 audio_codec=getattr(args, 'audio_codec', None),
                                                 audio_bitrate=getattr(args, 'audio_bitrate', None),
                                                 loudnorm=getattr(args, 'loudnorm', None))
                if not encoded:
                    _sys.exit(1)
        finally:
            _shutdown_engine_notifications(engine)
    elif args.cmd == 'batch':
        specs = _load_manifest(args.manifest)
        _strip_file_sourced_hooks(
            specs, getattr(args, 'allow_hooks', False), f'manifest {args.manifest}')
        if not specs:
            _sys.exit(1)
        logger.info(f"[Batch] {len(specs)} job(s) from {args.manifest}")
        if getattr(args, 'dry_run', False):
            for i, s in enumerate(specs, 1):
                print(f"  {i:>3}. {s.summary()}")
            return
        if getattr(args, 'to_queue', False):
            jq = _get_journal_cls()()
            for s in specs:
                rec = jq.add(s)
                print(f"queued {rec['id']}: {s.summary()}")
            return
        engine = FinalYouTubeGPUEngine(args.output_dir, pot_base_url=getattr(args, 'pot_base_url', None),
                                       pot_mode=getattr(args, 'pot_mode', 'auto'))
        try:
            results = []
            for i, s in enumerate(specs, 1):
                logger.info(f"[Batch] ({i}/{len(specs)}) {s.summary()}")
                try:
                    outs = engine.run(s)
                    if outs:
                        results.append({'label': s.summary(), 'spec': s, 'status': 'done',
                                        'files': [str(p) for p in outs]})
                    else:
                        err = getattr(engine.downloader, 'last_error', None) or 'No file produced'
                        results.append({'label': s.summary(), 'spec': s, 'status': 'failed',
                                        'category': YouTubeDownloader._classify_error(err), 'error': err})
                        logger.error(f"[Batch] job {i} produced no file: {err}")
                except Exception as e:
                    results.append({'label': s.summary(), 'spec': s, 'status': 'failed',
                                    'category': YouTubeDownloader._classify_error(str(e)), 'error': str(e)})
                    logger.error(f"[Batch] job {i} failed: {e}")
            _render_partial_report(results, getattr(args, 'report_json', None))
            if _batch_failed(results):
                _sys.exit(1)      # a batch that lost items must not report success to a script
        finally:
            _shutdown_engine_notifications(engine)
    elif args.cmd == 'queue':
        jq = _get_journal_cls()(getattr(args, 'queue_file', None))
        if args.action == 'add':
            if getattr(args, 'spec_json', None):
                import json as _json
                try:
                    spec = JobSpec.from_dict(_json.loads(args.spec_json))
                except (ValueError, TypeError) as e:
                    logger.error(f"queue add: invalid --json: {e}")
                    _sys.exit(1)
            else:
                spec = JobSpec(urls=list(args.urls), quality=args.quality,
                               encode_mode=args.encode_mode, profile=args.profile)
            if not spec.urls:
                print("queue add: provide URL(s) or --json '{...}'")
                return
            rec = jq.add(spec)
            if getattr(args, 'priority', None) is not None:
                jq.set_priority(rec['id'], args.priority)
            print(f"queued {rec['id']}: {spec.summary()}")
        elif args.action == 'list':
            jobs = jq.list()
            if not jobs:
                print("queue empty" + (" (paused)" if jq.is_paused() else ""))
                return
            for j in jobs:
                err = f"  ! {j.get('last_error','')[:60]}" if j.get('status') == 'failed' else ''
                print(f"  {j['id']}  {j['status']:<9} p{j.get('priority',0)} {j.get('label','')}{err}")
            print("counts:", jq.counts(), ("(paused)" if jq.is_paused() else ""))
        elif args.action == 'bump':
            if not args.job_id:
                print("queue bump: provide --id <job_id>")
                return
            prio = args.priority if args.priority is not None else 100
            j = jq.set_priority(args.job_id, prio)
            print(f"bumped {j['id']} to priority {j['priority']}" if j else f"no such job {args.job_id}")
        elif args.action == 'pause':
            jq.set_paused(True)
            print("queue paused")
        elif args.action == 'resume':
            jq.set_paused(False)
            print("queue resumed")
        elif args.action == 'cancel':
            if not args.job_id:
                print("queue cancel: provide --id <job_id>")
                return
            j = jq.cancel(args.job_id)
            if j:
                print(f"cancelled {j['id']}")
            elif jq.get(args.job_id):
                print(f"cannot cancel {args.job_id} (status: {jq.get(args.job_id).get('status','?')})")
            else:
                print(f"no such job {args.job_id}")
        elif args.action == 'retry-failed':
            print(f"reset {jq.retry_failed()} failed job(s) to queued")
        elif args.action == 'clear':
            print(f"removed {jq.clear(only_finished=True)} finished job(s)")
        elif args.action == 'run':
            if jq.is_paused():
                print("queue is paused — run `queue resume` first (a paused queue drains nothing)")
                return
            if not jq.acquire_run_lock():
                print("another queue runner is already active (lock held)")
                return
            queue_failed = False
            engine = None
            try:
                engine = FinalYouTubeGPUEngine(args.output_dir, pot_base_url=getattr(args, 'pot_base_url', None),
                                               pot_mode=getattr(args, 'pot_mode', 'auto'))
                drained = 0
                results = []
                while True:
                    job = jq.claim_next()   # atomic claim + mark-running (+ attempt cap)
                    if not job:
                        break
                    jid = job['id']
                    logger.info(f"[Queue] running {jid}: {job.get('label','')}")
                    # Safe defaults for the failure path: JobSpec.from_dict is now INSIDE the try (a
                    # corrupt journal spec used to raise BEFORE it, escape the loop, and leave the row
                    # claim_next() had marked 'running' stranded forever — the web-side F1 fix did not
                    # cover this runner).
                    label, jspec = job.get('label') or jid, None
                    try:
                        jspec = JobSpec.from_dict(job['spec'])
                        label = job.get('label') or jspec.summary()
                        # THE cancel channel for this job. Without it `queue cancel` printed success
                        # while this loop ran the download to completion, because cancellation was only
                        # ever delivered through progress_sink — which only the web worker installs.
                        # Rebound every iteration (and cleared in the finally) because `engine` is
                        # reused across the whole loop: a stale probe would cancel the NEXT job.
                        engine.downloader.cancel_check = jq.cancel_probe(jid)

                        def _persist_notification(result, _jid=jid):
                            try:
                                jq.update(
                                    _jid,
                                    notification=persisted_notification(result),
                                )
                            except Exception:
                                logger.exception(
                                    '[Notify] could not persist result for queue job %s',
                                    _jid,
                                )

                        if getattr(jspec, 'notify_url', None):
                            outs = engine.run(
                                jspec,
                                on_notification=_persist_notification,
                            )
                        else:
                            outs = engine.run(jspec)
                        if outs:
                            jq.set_status(jid, 'done', output_paths=outs)
                            results.append({'label': label, 'spec': jspec, 'status': 'done',
                                            'files': [str(p) for p in outs]})
                        else:
                            err = (getattr(engine.downloader, 'last_error', None) or
                                   'No file produced (bot-check / throttle / no format). Try cookies or wait.')
                            jq.set_status(jid, 'failed', error=err)
                            results.append({'label': label, 'spec': jspec, 'status': 'failed',
                                            'category': YouTubeDownloader._classify_error(err), 'error': err})
                    except Exception as e:
                        if (isinstance(e, DownloadCancelled)
                                or type(e).__name__ == 'DownloadCancelled'
                                or 'YT_FINAL_CANCEL' in str(e)):
                            # A cancel is an OUTCOME, not a failure — queue, this report and the
                            # webhook must all agree on that rather than calling it 'failed'.
                            jq.set_status(jid, 'cancelled')
                            logger.info(f"[Queue] {jid} cancelled")
                            results.append({'label': label, 'spec': jspec, 'status': 'cancelled'})
                        else:
                            jq.set_status(jid, 'failed', error=e)
                            logger.error(f"[Queue] {jid} failed: {e}")
                            results.append({'label': label, 'spec': jspec, 'status': 'failed',
                                            'category': YouTubeDownloader._classify_error(str(e)), 'error': str(e)})
                    finally:
                        engine.downloader.cancel_check = None
                        # Closing invariant (mirrors the web worker): if every terminal write above
                        # failed, the row is still 'running' and nothing would look at it again.
                        try:
                            jq.settle_claimed(jid)
                        except Exception:
                            logger.exception('[Queue] settle_claimed failed for %s', jid)
                    drained += 1
                print(f"queue run complete ({drained} job(s) processed)")
                _render_partial_report(results, getattr(args, 'report_json', None))
                queue_failed = _batch_failed(results)
            finally:
                if engine is not None:
                    _shutdown_engine_notifications(engine)
                jq.release_run_lock()
            if queue_failed:
                _sys.exit(1)      # scripts must see a nonzero code when a queued job failed
    elif args.cmd == 'redo':
        spec_dict = _resolve_redo_spec(args.ref, queue_file=getattr(args, 'queue_file', None),
                                       history_file=getattr(args, 'history_file', None))
        if spec_dict is None:
            print(f"redo: could not resolve '{args.ref}' to a prior job (try a job_id, a history "
                  f"index, or 'last')")
            _sys.exit(1)
        base = JobSpec.from_dict(spec_dict)
        overrides = {}
        for k in ('profile', 'encode_mode', 'quality', 'codec'):
            v = getattr(args, k, None)
            if v is not None:
                overrides[k] = v
        if getattr(args, 'redo_output_dir', None) is not None:
            overrides['output_dir'] = args.redo_output_dir
        new_spec = base.merged(**overrides)
        if not new_spec.urls:
            print("redo: resolved spec has no urls")
            _sys.exit(1)
        if getattr(args, 'to_queue', False):
            jq = _get_journal_cls()(getattr(args, 'queue_file', None))
            rec = jq.add(new_spec)
            print(f"queued {rec['id']}: {new_spec.summary()}")
        else:
            engine = FinalYouTubeGPUEngine(args.output_dir, pot_base_url=getattr(args, 'pot_base_url', None),
                                           pot_mode=getattr(args, 'pot_mode', 'auto'))
            try:
                try:
                    redo_result = engine.run(new_spec)
                except VerificationError as e:
                    logger.error(str(e))
                    _sys.exit(1)
                if not redo_result:
                    logger.error(getattr(engine.downloader, 'last_error', None)
                                 or "redo: job produced no output")
                    _sys.exit(1)
            finally:
                _shutdown_engine_notifications(engine)
    elif args.cmd == 'serve':
        try:
            from webserver import serve as _serve
        except ImportError:
            from Final.webserver import serve as _serve
        _serve(host=args.host, port=args.port, output_dir=args.output_dir,
               queue_file=getattr(args, 'queue_file', None),
               pot_base_url=getattr(args, 'pot_base_url', None), pot_mode=getattr(args, 'pot_mode', 'auto'),
               open_browser=not getattr(args, 'no_browser', False))
    elif args.cmd == 'normalize':
        det = get_detector()
        proc = FFmpegProcessor(det)
        ok_any = False
        for inp in args.inputs:
            f = Path(inp)
            if not f.is_file():
                logger.warning(f"[Normalize] not found: {f}")
                continue
            out = YouTubeDownloader._unique_path(f.with_name(f.stem + '_ln' + f.suffix))
            ok_any = proc.normalize_audio(f, out, target_lufs=args.lufs, tp=args.tp) or ok_any
        if not ok_any:
            _sys.exit(1)
    elif args.cmd == 'list-formats':
        with _json_stdout_guard(getattr(args, 'lf_json', False)):
            import yt_dlp
            det = get_detector()
            dler = YouTubeDownloader(det, out_dir=args.output_dir,
                                     pot_base_url=getattr(args, 'pot_base_url', None),
                                     pot_mode=getattr(args, 'pot_mode', 'auto'))
            opts = dler.get_optimized_ydl_opts('best')
            opts['progress_hooks'] = []
            if getattr(args, 'lf_json', False):
                # Machine-readable: extract info, curate rows, emit JSON (no yt-dlp table).
                opts['listformats'] = False
                opts['quiet'] = True
                try:
                    with yt_dlp.YoutubeDL(opts) as y:
                        info = y.extract_info(args.url, download=False)
                except Exception as e:
                    logger.error(f"[list-formats] {e}")
                    _sys.exit(1)
                rows = YouTubeDownloader._format_rows(info or {})
                if getattr(args, 'lf_format_id', None):
                    rows = [r for r in rows if str(r.get('format_id')) == str(args.lf_format_id)]
                    if not rows:
                        logger.error(f"[list-formats] format id '{args.lf_format_id}' not found for {args.url}")
                        _sys.exit(1)
                import json as _json
                print(_json.dumps({'url': args.url, 'id': (info or {}).get('id'),
                                   'title': (info or {}).get('title'), 'formats': rows}, indent=2))
            else:
                opts['listformats'] = True
                opts['quiet'] = False
                try:
                    with yt_dlp.YoutubeDL(opts) as y:
                        y.extract_info(args.url, download=False)
                except Exception as e:
                    logger.error(f"[list-formats] {e}")
                    _sys.exit(1)
    elif args.cmd == 'watch':
        try:
            from watch import (resolve_channel_id, fetch_videos, load_archive, append_archive,
                               match_filters, probe_duration)
            from watch_filters import WatchPatternError, validate_title_pattern
        except ImportError:
            from Final.watch import (resolve_channel_id, fetch_videos, load_archive, append_archive,
                                    match_filters, probe_duration)
            from Final.watch_filters import WatchPatternError, validate_title_pattern
        import time as _time
        try:
            args.filter_title = validate_title_pattern(
                getattr(args, 'filter_title', None))
        except WatchPatternError as e:
            parser.error(str(e))
        cid = resolve_channel_id(args.channel)
        if not cid:
            logger.error(f"[Watch] could not resolve a channel id from '{args.channel}' "
                         f"(try a /channel/UC... URL or the channel_id directly)")
            _sys.exit(1)
        arch = args.watch_archive or str(Path.home() / '.yt_final' / f'watch_{cid}.txt')
        Path(arch).parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"[Watch] channel {cid} | archive {arch}"
                    + (f" | enqueue" if getattr(args, 'watch_enqueue', False) else " | inline"))
        engine_holder = {}
        # Per-video inline failure counter. A permanently-broken video (geo-blocked / deleted /
        # members-only) would otherwise be re-tried on EVERY poll forever; after MAX_WATCH_RETRIES
        # we archive it (mark seen) and stop retrying.
        MAX_WATCH_RETRIES = 5
        fails = {}

        def _poll_once():
            seen = load_archive(arch)
            try:
                vids = fetch_videos(cid)
            except Exception as e:
                logger.error(f"[Watch] feed fetch failed: {e}")
                return
            new = [v for v in vids if v['id'] not in seen]
            # apply per-watch upload filters (title/date are free from RSS; duration is probed
            # lazily, ONLY when a duration bound is set, so the zero-filter path makes no extra calls).
            _filters = {'filter_title': getattr(args, 'filter_title', None),
                        'after': getattr(args, 'watch_after', None),
                        'min_duration': getattr(args, 'min_duration', None),
                        'max_duration': getattr(args, 'max_duration', None)}
            if any(_filters.values()):
                _need_dur = _filters['min_duration'] is not None or _filters['max_duration'] is not None
                filtered = []
                for v in new:
                    dur = probe_duration(v['url']) if _need_dur else None
                    if match_filters(v, _filters, dur):
                        filtered.append(v)
                new = filtered
            if getattr(args, 'watch_max', 0):
                new = new[:args.watch_max]
            if not new:
                logger.info("[Watch] no new videos")
                return
            logger.info(f"[Watch] {len(new)} new video(s)")
            for v in new:
                spec = JobSpec(urls=[v['url']], quality=args.quality, encode_mode=args.encode_mode,
                               profile=args.profile, no_shorts=getattr(args, 'no_shorts', False),
                               output_dir=str(args.output_dir))
                if getattr(args, 'watch_enqueue', False):
                    _get_journal_cls()(getattr(args, 'queue_file', None)).add(spec)
                    append_archive(arch, v['id'])
                    logger.info(f"[Watch] queued {v['id']}: {v['title']}")
                else:
                    if 'eng' not in engine_holder:
                        engine_holder['eng'] = FinalYouTubeGPUEngine(
                            args.output_dir, pot_base_url=getattr(args, 'pot_base_url', None),
                            pot_mode=getattr(args, 'pot_mode', 'auto'))
                    if engine_holder['eng'].run(spec):
                        append_archive(arch, v['id'])   # only mark done on success (failures retry next poll)
                        fails.pop(v['id'], None)
                        logger.info(f"[Watch] downloaded {v['id']}: {v['title']}")
                    else:
                        fails[v['id']] = fails.get(v['id'], 0) + 1
                        if fails[v['id']] >= MAX_WATCH_RETRIES:
                            # Give up on a permanently-failing video: archive it so it stops retrying.
                            append_archive(arch, v['id'])
                            fails.pop(v['id'], None)
                            logger.warning(f"[Watch] {v['id']} failed {MAX_WATCH_RETRIES}x "
                                           f"(geo-blocked/deleted?) — skipping permanently: {v['title']}")
                        else:
                            logger.warning(f"[Watch] failed {v['id']} "
                                           f"(attempt {fails[v['id']]}/{MAX_WATCH_RETRIES}) — will retry next poll")

        if args.interval and args.interval > 0:
            logger.info(f"[Watch] polling every {args.interval}s (Ctrl+C to stop)")
            try:
                while True:
                    _poll_once()
                    _time.sleep(args.interval)
            except KeyboardInterrupt:
                logger.info("[Watch] stopped")
        else:
            _poll_once()
    elif args.cmd == 'gpu-info':
        # lightweight, create only detector
        det = get_detector()
        logger.info(det.gpu_info)
        logger.info("Running quick benchmark for recommended encoder...")
        rec = det.get_recommended_encoder()
        det.benchmark_short_encode(rec[1])
    elif args.cmd == 'list-profiles':
        # lightweight: purely static data, zero detector / zero engine / zero heavy init
        profiles = FFmpegProcessor._load_profiles()
        for k, v in profiles.items():
            print(f"{k}: {v['name']}")
    elif args.cmd == 'history':
        with _json_stdout_guard(getattr(args, 'history_json', False)):
            # Lazy: no engine, no detector. Reads the durable append-only JSONL ledger written by
            # the engine on every completed job (~/.yt_final/history.jsonl by default).
            led = _get_history_cls()(getattr(args, 'history_file', None))
            rows = led.read(limit=getattr(args, 'limit', None))
            if getattr(args, 'history_json', False):
                import json as _json
                for r in rows:
                    print(_json.dumps(r, ensure_ascii=False))
            elif not rows:
                print("📜 no history yet")
            else:
                for r in rows:
                    ts = time.strftime('%Y-%m-%d %H:%M', time.localtime(r.get('ts', 0)))
                    print(f"{ts}  {r.get('status', '?'):<6} {r.get('profile', '')}  "
                          f"{r.get('output') or r.get('url')}")

    elif args.cmd == 'diagnose':
        # Live toolchain doctor: build the structured rows, print the table, exit nonzero on hard FAIL.
        _sys.exit(run_diagnose(get_detector(), check_pypi=getattr(args, 'check_pypi', False)))
    elif args.cmd == 'troubleshoot':
        # Lazy: only detector needed (ffmpeg path + gpu_info). Avoid full engine (downloader + processor + ready log).
        det = get_detector()
        logger.info(f"[OK] FFmpeg: {det.ffmpeg_path}")
        logger.info(f"[GPU] {det.gpu_info.get('gpu_name')} | working HW encoders: {det.gpu_info.get('working_encoders')}")
        logger.info("[Speed] Native -N is the fast path now (aria2c disabled for DASH/HLS in yt-dlp 2026.06.09).")
        logger.info("[Tip] Biggest lever: verified managed Deno/Node plus a reviewed yt_final release.")
        logger.info("[Tip] Default 1080p H.264 is ~3x smaller than 4K VP9. Install reviewed yt_final releases and maintain your manual FFmpeg.")
    elif args.cmd == 'auth':
        _sys.exit(_cmd_auth(args))
    elif args.cmd == 'export-cookies':
        # Self-contained guided walkthrough; no engine/detector. Read-only (prints only).
        config_dir = os.environ.get('YT_FINAL_HOME')
        config_dir = Path(config_dir) if config_dir else (Path.home() / '.yt_final')
        print("\n=== export-cookies: fix the YouTube bot-wall / HTTP 403 ===")
        print("This is the remedy when you see \"Sign in to confirm you're not a bot\" or HTTP 403.")
        print("\n1. Install a browser extension that exports Netscape cookies, e.g.")
        print("   \"Get cookies.txt LOCALLY\" (Chrome/Edge) or a similar add-on.")
        print("2. Log in to https://www.youtube.com in that browser.")
        print("3. Use the extension to export cookies for youtube.com and save the file as 'cookies.txt'.")
        print("4. Drop cookies.txt where auto-discovery looks. It uses the FIRST one found, in order:")
        print("     a) $YT_FINAL_COOKIES   (full path to the file)")
        print("     b) ./cookies.txt       (current working directory)")
        print("     c) <output-dir>/cookies.txt   (your --output-dir, default ./downloads)")
        print(f"     d) {config_dir / 'cookies.txt'}   (config dir: $YT_FINAL_HOME or ~/.yt_final)")
        print("\nWhen present, it is used automatically unless you pass --cookies or --cookies-from-browser.")
    else:
        # Should not reach here normally
        parser.print_help()


# Cross-register module under both 'yt_final' and 'Final.yt_final' names (and ensure cap aliases)
# This prevents duplicate module loads / duplicate CapabilityDetector classes when users
# mix direct imports (sys.path.insert Final dir + import yt_final) with package imports.
def _register_module_aliases():
    import sys as _sys
    _this = _sys.modules[__name__]
    _sys.modules.setdefault('yt_final', _this)
    _sys.modules.setdefault('Final.yt_final', _this)
    # also ensure cap aliases are present
    if 'capability' in _sys.modules and 'Final.capability' in _sys.modules:
        if _sys.modules['capability'] is not _sys.modules['Final.capability']:
            _shared = _sys.modules['capability']
            _sys.modules['Final.capability'] = _shared
            _sys.modules['capability'] = _shared

_register_module_aliases()

if __name__ == "__main__":
    # Propagate the exit status. cli.py already returns main()'s value, so the installed
    # `yt-final` command exited non-zero while `python yt_final.py` always exited 0 -- the
    # same invocation reporting different success to a shell script or a scheduler.
    raise SystemExit(main())
