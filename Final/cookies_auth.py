"""Automatic cookie/session management for yt_final (lazy, single-identity)."""
from __future__ import annotations
import os
import logging
import json
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass, asdict

try:
    from .secureio import atomic_replace_private, copy_private_atomic
except ImportError:
    from secureio import atomic_replace_private, copy_private_atomic

logger = logging.getLogger("yt_final")

# In-process serialization of the read-check-validate-adopt-save sequence in
# CookieManager, keyed by the resolved on-disk store path. This is what makes N
# parallel-download worker threads (which share ONE cached CookieManager) plus
# the per-request managers built by /api/probe, /api/auth and worker_loop — all
# living in the same webserver process and all pointed at the same store — converge
# on a single validation/provider walk instead of each independently re-running the
# real network probe or launching a headless browser against the shared profile.
_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()
_STORE_LOCK_DEPTHS = threading.local()
_STORE_LOCK_NAME = ".cookie-store.lock"
_STATE_INVALID_NAME = ".state-invalid"
_STATE_INVALID_BACKUP_NAME = ".state-invalid.backup"
_LOCK_PROCESS_PID = os.getpid()
_ACTIVE_LOCK_FDS: dict[int, object] = {}


def _reset_process_lock_state(pid: int | None = None) -> None:
    """Discard synchronization state inherited from another process."""
    global _STORE_LOCKS, _STORE_LOCKS_GUARD, _STORE_LOCK_DEPTHS
    global _LOCK_PROCESS_PID, _ACTIVE_LOCK_FDS

    for raw_fd in tuple(_ACTIVE_LOCK_FDS):
        try:
            os.close(raw_fd)
        except OSError:
            pass
    _ACTIVE_LOCK_FDS = {}
    _STORE_LOCKS = {}
    _STORE_LOCKS_GUARD = threading.Lock()
    _STORE_LOCK_DEPTHS = threading.local()
    _LOCK_PROCESS_PID = os.getpid() if pid is None else pid


def _ensure_process_lock_state() -> None:
    pid = os.getpid()
    if pid != _LOCK_PROCESS_PID:
        _reset_process_lock_state(pid)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_lock_state)


def _lock_for_store(store: Path) -> threading.RLock:
    _ensure_process_lock_state()
    try:
        key = str(Path(store).resolve())
    except OSError:
        key = str(store)
    with _STORE_LOCKS_GUARD:
        lk = _STORE_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _STORE_LOCKS[key] = lk
        return lk


@contextmanager
def cookie_store_write_lock(store: Path):
    """One re-entrant in-process + cross-process transaction for a cookie store.

    Lock order is always the store-keyed RLock first, then the stable lock-file
    inode. Nested calls in the same thread reacquire only the RLock.
    """
    _ensure_process_lock_state()
    store = Path(store)
    try:
        key = str(store.resolve())
    except OSError:
        key = str(store)
    process_lock = _lock_for_store(store)
    with process_lock:
        depths = getattr(_STORE_LOCK_DEPTHS, "values", None)
        if depths is None:
            depths = {}
            _STORE_LOCK_DEPTHS.values = depths
        depth = depths.get(key, 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        store.mkdir(parents=True, exist_ok=True)
        with _file_lock(store / _STORE_LOCK_NAME):
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)


@contextmanager
def _file_lock(lock_path: Path):
    """Lock one stable per-store inode; callers must never delete/replace it."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    raw_fd = os.open(
        os.fspath(lock_path),
        os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
        0o600,
    )
    lock_token = object()
    _ACTIVE_LOCK_FDS[raw_fd] = lock_token
    try:
        os.chmod(os.fspath(lock_path), 0o600)
        if os.fstat(raw_fd).st_size == 0:
            os.write(raw_fd, b"\0")
            os.fsync(raw_fd)
        # The context owns raw_fd explicitly. The non-owning wrapper can safely
        # be closed after a fork reset without closing a reused descriptor.
        fd = os.fdopen(raw_fd, "r+b", buffering=0, closefd=False)
    except Exception:
        if _ACTIVE_LOCK_FDS.get(raw_fd) is lock_token:
            _ACTIVE_LOCK_FDS.pop(raw_fd, None)
            os.close(raw_fd)
        raise
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            fd.seek(0)
            while True:
                try:
                    msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.02)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        try:
            owns_raw_fd = _ACTIVE_LOCK_FDS.get(raw_fd) is lock_token
            if locked and owns_raw_fd:
                try:
                    if os.name == "nt":
                        import msvcrt
                        fd.seek(0)
                        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                fd.close()
            except OSError:
                pass
            if _ACTIVE_LOCK_FDS.get(raw_fd) is lock_token:
                _ACTIVE_LOCK_FDS.pop(raw_fd, None)
                try:
                    os.close(raw_fd)
                except OSError:
                    pass

_DEFAULTS = {
    "enabled": False,
    "order": ["file", "profile", "browser"],
    "cookiefile": None,
    "browser": "auto",
    "validate_ttl_min": 45,
    # Known age-restricted video used as an authenticated-session probe.
    # VERIFY/refresh at implementation time; overridable via config.
    "probe_video_id": "HtVdAasjOgU",
}


def auth_config_dir() -> Path:
    env = os.environ.get("YT_FINAL_HOME")
    return Path(env) if env else (Path.home() / ".yt_final")


def cookie_store_dir() -> Path:
    # NOTE: intentionally does NOT mkdir — a disabled/default install must not
    # create ~/.yt_final/cookies/ just by computing this path. Write sites
    # (write_netscape, save_state, `auth import`) create the dir themselves.
    return auth_config_dir() / "cookies"


def load_auth_config(config_dir: Path | None = None) -> dict:
    cfg = dict(_DEFAULTS)
    base = config_dir if config_dir is not None else auth_config_dir()
    toml_path = Path(base) / "config.toml"
    try:
        import tomllib
        if toml_path.exists():
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            section = data.get("auth", {})
            if isinstance(section, dict):
                cfg.update({k: v for k, v in section.items() if k in _DEFAULTS})
    except Exception as e:  # never fatal — fall back to defaults
        logger.debug(f"[auth] config load failed, using defaults: {e}")
    return cfg


def write_netscape(cookies: list[dict], path: Path) -> Path:
    lines = ["# Netscape HTTP Cookie File", "# Generated by yt_final", ""]
    for c in cookies:
        domain = c.get("domain", "")
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        cpath = c.get("path", "/") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires", 0) or 0)
        name = c.get("name", "")
        value = c.get("value", "")
        lines.append("\t".join([domain, include_sub, cpath, secure,
                                str(expires), name, value]))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    with cookie_store_write_lock(Path(path).parent):
        save_state(Path(path).parent, CookieState())
        atomic_replace_private(Path(path), payload)
    return Path(path)


@dataclass
class CookieState:
    path: str | None = None
    source: str | None = None
    fetched_at: float = 0.0
    last_validated_at: float = 0.0
    valid: bool = False


class StatePersistenceError(OSError):
    """A strict state publication failed; callers must abort the transaction."""


def _open_state_commit_directory(store: Path) -> int | None:
    if os.name == "nt":
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    return os.open(os.fspath(store), flags)


def _fsync_state_commit_directory(directory_fd: int | None) -> None:
    if directory_fd is not None:
        os.fsync(directory_fd)


def _close_state_commit_directory(directory_fd: int | None) -> None:
    if directory_fd is not None:
        os.close(directory_fd)


def _load_state_unlocked(store: Path) -> CookieState:
    store = Path(store)
    if (
        (store / _STATE_INVALID_NAME).exists()
        or (store / _STATE_INVALID_BACKUP_NAME).exists()
    ):
        return CookieState()
    p = store / "state.json"
    try:
        if p.exists():
            return CookieState(**json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        logger.debug(f"[auth] state load failed: {e}")
    return CookieState()


def load_state(store: Path) -> CookieState:
    store = Path(store)
    with cookie_store_write_lock(store):
        return _load_state_unlocked(store)


def _save_state_unlocked(store: Path, state: CookieState) -> None:
    store = Path(store)
    payload = json.dumps(asdict(state), separators=(",", ":")).encode("utf-8")
    invalid = store / _STATE_INVALID_NAME
    invalid_backup = store / _STATE_INVALID_BACKUP_NAME
    try:
        # Two durable sentinels permit a fail-closed handoff: at least one stays
        # visible through every fallible durability operation.
        atomic_replace_private(invalid, b"invalid\n")
        atomic_replace_private(invalid_backup, b"invalid\n")
        atomic_replace_private(store / "state.json", payload)
        if not state.valid:
            return

        # Pre-open while both guards still block readers. Primary removal is
        # durably ordered while backup remains; backup removal is the final
        # fallible operation, so a crash may resurrect it only fail-closed.
        directory_fd = _open_state_commit_directory(store)
        try:
            invalid.unlink()
            _fsync_state_commit_directory(directory_fd)
        except Exception:
            try:
                _close_state_commit_directory(directory_fd)
            except Exception:
                pass
            raise
        _close_state_commit_directory(directory_fd)
        invalid_backup.unlink()
    except Exception as e:
        raise StatePersistenceError(f"state save failed: {e}") from e


def save_state(store: Path, state: CookieState) -> None:
    store = Path(store)
    with cookie_store_write_lock(store):
        _save_state_unlocked(store, state)


def clear_cookie_store(store: Path) -> None:
    """Strictly invalidate first, then remove jars without deleting the lock."""
    store = Path(store)
    with cookie_store_write_lock(store):
        save_state(store, CookieState())
        for name in (
            "active.txt",
            "browser.txt",
            "profile.txt",
            # Legacy pre-transaction sidecar only. The stable transaction lock is
            # deliberately absent so its inode identity survives every Clear.
            "state.json.lock",
        ):
            try:
                (store / name).unlink()
            except FileNotFoundError:
                pass


def replace_active_cookiefile(*, data: bytes | None = None,
                              source: Path | None = None,
                              store: Path | None = None) -> Path:
    """Replace active.txt and reset validation state under one store lock."""
    if (data is None) == (source is None):
        raise ValueError("provide exactly one of data or source")
    store = Path(store) if store is not None else cookie_store_dir()
    store.mkdir(parents=True, exist_ok=True)
    active = store / "active.txt"
    with cookie_store_write_lock(store):
        # Invalidate strictly before bytes can change. If replacement later fails
        # (including after os.replace during chmod/parent-fsync), metadata remains
        # safely invalid for whichever complete bytes are now present.
        save_state(store, CookieState())
        if source is not None:
            copy_private_atomic(Path(source), active)
        else:
            atomic_replace_private(active, data)
    return active


# chmod(0o600) is a silent no-op on Windows, so state the protection that actually applies there
# (the per-user profile ACL) rather than printing a promise the OS never honored.
_PERM_NOTE = ('restricted to your Windows user profile' if os.name == 'nt' else 'chmod 600')
_CONSENT_TEXT = (
    "[auth] Authenticated downloads enabled.\n"
    "  • Use a BURNER Google account — YouTube may rate-limit or suspend accounts\n"
    "    whose cookies are used with downloaders. This is against YouTube's Terms.\n"
    f"  • Cookies are stored LOCALLY in plaintext ({_PERM_NOTE}), not uploaded to yt_final\n"
    "    or analytics, and sent only with your configured authenticated requests.\n"
    "  • You performed the login yourself; yt_final never types your password.\n"
)


def maybe_show_consent(config: dict) -> bool:
    """One-shot consent banner. Prints only on a real terminal (not under `serve`)."""
    if not config.get("enabled"):
        return False
    store = cookie_store_dir()
    marker = store / ".consent"
    if marker.exists():
        return False
    try:
        store.mkdir(parents=True, exist_ok=True)
        marker.write_text("ack", encoding="utf-8")
    except Exception:
        pass
    # Avoid flooding serve.log / pytest capture; CLI (TTY) still sees the banner.
    try:
        import sys
        if getattr(sys.stdout, "isatty", lambda: False)():
            print(_CONSENT_TEXT)
    except Exception:
        pass
    return True


def build_default_manager(output_dir: str | None = None) -> "CookieManager":
    import cookie_providers as cp
    cfg = load_auth_config()
    maybe_show_consent(cfg)
    factory = {
        "file": lambda: cp.CookieFileProvider(
            cookiefile=cfg.get("cookiefile"), output_dir=output_dir),
        "profile": lambda: cp.HeadlessProfileProvider(),
        "browser": lambda: cp.BrowserExtractProvider(browser=cfg.get("browser", "auto")),
    }
    providers = [factory[name]() for name in cfg.get("order", []) if name in factory]
    return CookieManager(config=cfg, providers=providers)


class CookieManager:
    def __init__(self, config=None, providers=None, validator=None,
                 clock=time.time, store: Path | None = None):
        self.config = config if config is not None else load_auth_config()
        self.providers = providers or []
        self.validator = validator or self._default_validator
        self.clock = clock
        self.store = Path(store) if store is not None else cookie_store_dir()
        self._lock = _lock_for_store(self.store)
        if self.store.exists():
            with cookie_store_write_lock(self.store):
                self.state = _load_state_unlocked(self.store)
        else:
            # Preserve the disabled/default no-side-effect contract. Any later
            # enabled operation enters the transaction and reloads from disk.
            self.state = CookieState()
        self._last_empty_warn = 0.0  # rate-limit the "no usable cookies" warning
        # Serialize get_cookiefile()/notify_auth_failure() across every manager that
        # targets this same store path within the process (shared parallel-download
        # manager + per-request managers), so they don't race the check-then-adopt.
    # --- public seam -----------------------------------------------------
    def get_cookiefile(self, reason: str = "auto") -> str | None:
        if not self.config.get("enabled"):
            return None
        # One lock for the whole check-validate-adopt-save sequence: concurrent
        # callers (parallel-download workers / probe handlers) block here and reuse
        # the first winner's freshly-adopted cache instead of each independently
        # re-running the network probe or walking the browser providers.
        with cookie_store_write_lock(self.store):
            self.state = _load_state_unlocked(self.store)
            ttl = float(self.config.get("validate_ttl_min", 45)) * 60.0
            now = self.clock()
            st = self.state
            # 1) fresh + valid cache
            if st.valid and st.path and Path(st.path).exists() \
                    and (now - st.last_validated_at) < ttl:
                return st.path
            # 2) have a path but stale/suspect → revalidate in place
            if st.path and Path(st.path).exists():
                self._publish_state(CookieState())
                if self.validator(st.path):
                    self._adopt(
                        st.path,
                        st.source or "cache",
                        now,
                        revalidate=True,
                        prior=st,
                    )
                    return st.path
            # 3) walk providers
            if self.state != CookieState():
                self._publish_state(CookieState())
            else:
                # A durable invalid marker must exist before providers can mutate
                # browser.txt/profile.txt or return a different cookie path.
                save_state(self.store, self.state)
            for p in self.providers:
                try:
                    if not p.available():
                        continue
                    path = p.acquire()
                except StatePersistenceError:
                    raise
                except Exception as e:
                    logger.debug(f"[auth] provider {getattr(p,'name','?')} failed: {e}")
                    continue
                if path and Path(path).exists() and self.validator(path):
                    self._adopt(path, p.name, now, revalidate=False)
                    return path
            # nothing worked — auth is ON but no tier produced a usable cookie. Surface it loudly
            # (rate-limited) so the user isn't left with a cryptic bot/age-gate failure downstream.
            if now - self._last_empty_warn > 300:
                self._last_empty_warn = now
                names = "/".join(getattr(p, "name", "?") for p in self.providers) or "none"
                logger.warning(
                    "[auth] enabled but no usable cookies from any source (%s). Set up cookies in the "
                    "Auth tab: import a cookies.txt or run the one-time login. (Auto-extract from "
                    "Chrome/Edge/Brave is best-effort and yields nothing on Windows app-bound encryption.)",
                    names)
            return None

    def notify_auth_failure(self, url: str | None = None) -> None:
        with cookie_store_write_lock(self.store):
            self.state = _load_state_unlocked(self.store)
            if self.state.path or self.state.valid:
                # Clear the FULL state, not just `valid=False`: get_cookiefile's
                # step-2 ("have a path but stale/suspect -> revalidate in place")
                # ignores the `valid` flag and only checks that `st.path` exists,
                # so a merely-invalidated path would be silently revalidated and
                # reused instead of forcing the provider chain to re-acquire.
                self._publish_state(CookieState())
                logger.info("[auth] cookies flagged for refresh after auth failure")

    def refresh_cookiefile(self) -> str | None:
        """Invalidate and reacquire without allowing a writer between steps."""
        with cookie_store_write_lock(self.store):
            self.notify_auth_failure()
            return self.get_cookiefile(reason="refresh")

    def _discover_cookie_file(self) -> str | None:
        """Best-effort path to an on-disk cookies.txt without launching browsers."""
        st = self.state
        if st.path and Path(st.path).exists() and Path(st.path).stat().st_size > 0:
            return st.path
        for name in ("active.txt", "cookies.txt", "browser.txt", "profile.txt"):
            p = self.store / name
            try:
                if p.exists() and p.stat().st_size > 0:
                    return str(p)
            except OSError:
                continue
        # Config / env / cwd candidates (same as CookieFileProvider, without browser).
        try:
            import cookie_providers as cp
            file_p = cp.CookieFileProvider(
                cookiefile=self.config.get("cookiefile"),
            )
            return file_p._first_existing()
        except Exception:
            return None

    def status(self) -> dict:
        with cookie_store_write_lock(self.store):
            self.state = _load_state_unlocked(self.store)
            return self._status_unlocked()

    def _status_unlocked(self) -> dict:
        tiers = []
        for p in self.providers:
            try:
                if p.available():
                    tiers.append(p.name)
            except Exception:
                pass
        path = self._discover_cookie_file()
        size = 0
        if path:
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
        enabled = bool(self.config.get("enabled"))
        valid = bool(self.state.valid) and bool(path)
        # Human-readable detail for UI / CLI (single source of truth).
        if not enabled and not path:
            detail = "Auth is off and no cookies file is present. Import cookies.txt to enable."
        elif not enabled and path:
            detail = "Cookies file found but auth is disabled in config — import again or set [auth] enabled = true."
        elif enabled and not path:
            detail = "Auth is on but no cookies file is present. Import cookies.txt, then click Test."
        elif enabled and path and not valid:
            detail = "Cookies on file — not validated yet. Click Test to verify login against YouTube."
        elif enabled and path and valid:
            detail = "Signed-in cookies are active for age-gated / throttled downloads."
        else:
            detail = "Status unknown."
        browsers = []
        try:
            import cookie_providers as cp
            browsers = cp.BrowserExtractProvider(
                browser=self.config.get("browser") or "auto",
            ).list_detected()
        except Exception:
            browsers = []
        playwright_ok = False
        native_login = False
        try:
            import cookie_providers as cp
            playwright_ok = bool(cp._playwright_available())
            native_login = bool(cp.native_login_available())
        except Exception:
            pass
        return {
            "enabled": enabled,
            "source": self.state.source,
            "valid": valid,
            "has_file": bool(path),
            "cookie_path": path,
            "cookie_bytes": size,
            "fetched_at": self.state.fetched_at,
            "last_validated_at": self.state.last_validated_at,
            "tiers_available": tiers,
            "browsers_detected": browsers,
            "playwright_available": playwright_ok,
            "native_login_available": native_login,
            "detail": detail,
        }

    def validate_existing_file(self) -> tuple[bool, str, str | None]:
        """Re-validate cookies already on disk. Does not launch browser extractors.

        Returns (ok, message, path).
        """
        with cookie_store_write_lock(self.store):
            self.state = _load_state_unlocked(self.store)
            path = self._discover_cookie_file()
            if not path:
                return False, "No cookies on file — import cookies.txt first", None
            if not self.config.get("enabled"):
                return False, "Auth is disabled in config — import again to enable", path
            prior = self.state
            self._publish_state(CookieState())
            try:
                ok = bool(self.validator(path))
            except Exception as e:
                return False, f"Validation error: {e}", path
            now = self.clock()
            if ok:
                self._adopt(
                    path,
                    prior.source or "file",
                    now,
                    revalidate=bool(prior.path),
                    prior=prior,
                )
                return True, f"Cookies validated ({Path(path).name})", path
            return False, "Cookies failed validation (signed out, expired, or blocked)", path

    # --- internals -------------------------------------------------------
    def _publish_state(self, state: CookieState) -> None:
        save_state(self.store, state)
        self.state = state

    def _adopt(self, path, source, now, revalidate, prior=None):
        prior = prior or self.state
        state = CookieState(
            path=str(path), source=source,
            fetched_at=prior.fetched_at if revalidate else now,
            last_validated_at=now, valid=True,
        )
        self._publish_state(state)

    def _default_validator(self, cookiefile: str) -> bool:
        """Probe a known age-gated video; success ⇒ logged-in. Inconclusive
        (probe removed / network) keeps prior state by returning True only when
        extraction clearly succeeds."""
        vid = self.config.get("probe_video_id")
        if not vid:
            return True  # no probe configured → trust the file
        try:
            import yt_dlp
            # socket_timeout bounds every underlying HTTP op: this validator runs
            # synchronously on the per-download / per-probe hot path whenever the
            # cookie cache is stale, so an unresponsive network must not hang the
            # caller indefinitely.
            #
            # socket_timeout does NOT bound the PO-token provider: that runs as a
            # separate process. With a provider installed but failing, the default
            # multi-client extraction re-tried it once per client and took ~110s to
            # reach the same verdict this pinned probe reaches in ~2s -- long enough
            # that "Test cookies" in the web UI looked hung. This probe only asks
            # "does this cookie file unlock an age-gated video", which the web client
            # answers without a PO token, so it declines to fetch one.
            opts = {"quiet": True, "skip_download": True, "cookiefile": cookiefile,
                    "extractor_retries": 1, "socket_timeout": 8,
                    "extractor_args": {"youtube": {"player_client": ["web"],
                                                   "fetch_pot": ["never"]}}}
            with yt_dlp.YoutubeDL(opts) as y:
                # process=False stops after extraction. Picking a downloadable format
                # is a different question from "is this session signed in", and it
                # fails on its own terms ("Requested format is not available") even
                # when the cookies are perfectly valid.
                info = y.extract_info(f"https://www.youtube.com/watch?v={vid}",
                                      download=False, process=False)
            return bool(info and info.get("id"))
        except Exception as e:
            logger.debug(f"[auth] validation probe inconclusive: {e}")
            # Distinguish auth failure from probe-gone: only auth-ish errors fail.
            msg = str(e).lower()
            # A bot wall is not a signed-out session. YouTube phrases it "Sign in to confirm
            # you're not a bot", which matches BOTH "sign in" and "bot" below, so it has to be
            # ruled out first or a perfectly valid cookie file gets reported as signed out --
            # and re-logging-in is the one thing that cannot clear a bot wall. Pinning a single
            # client to avoid the PO-token stall makes meeting this wall more likely, not less,
            # so treat it the way every other ambiguous failure is treated: inconclusive.
            if "not a bot" in msg:
                return True
            if any(k in msg for k in ("sign in", "bot", "consent", "private",
                                      "login required")):
                return False
            return True  # inconclusive → don't nuke a possibly-good session
