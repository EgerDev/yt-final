"""Cookie providers for yt_final's CookieManager (best-effort, never raise up)."""
from __future__ import annotations
import logging
import os
import sys
import subprocess
from pathlib import Path

from cookies_auth import auth_config_dir, write_netscape, cookie_store_dir

logger = logging.getLogger("yt_final")


class CookieProvider:
    name = "base"

    def available(self) -> bool:
        return False

    def acquire(self) -> str | None:
        return None


class CookieFileProvider(CookieProvider):
    name = "file"

    def __init__(self, cookiefile: str | None = None, output_dir: str | None = None):
        self._explicit = cookiefile
        self._output_dir = output_dir

    def _candidates(self) -> list[Path]:
        c: list[Path] = []
        if self._explicit:
            c.append(Path(self._explicit))
        env = os.environ.get("YT_FINAL_COOKIES")
        if env:
            c.append(Path(env))
        c.append(Path.cwd() / "cookies.txt")
        if self._output_dir:
            c.append(Path(self._output_dir) / "cookies.txt")
        c.append(cookie_store_dir() / "active.txt")
        c.append(cookie_store_dir() / "cookies.txt")
        c.append(auth_config_dir() / "cookies.txt")
        return c

    def _first_existing(self) -> str | None:
        for p in self._candidates():
            try:
                if p.exists() and p.stat().st_size > 0:
                    return str(p)
            except (OSError, ValueError):
                continue
        return None

    def available(self) -> bool:
        return self._first_existing() is not None

    def acquire(self) -> str | None:
        path = self._first_existing()
        if path:
            logger.info(f"[auth] using cookie file: {path}")
        return path


def _firefox_extract(browser_name: str):
    """Extract Firefox cookies via yt-dlp's cookie machinery (no DPAPI lock)."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = extract_cookies_from_browser("firefox")
        out = []
        for c in jar:
            if "youtube.com" in (c.domain or "") or "google.com" in (c.domain or ""):
                out.append({"domain": c.domain, "name": c.name, "value": c.value,
                            "path": c.path or "/", "secure": bool(c.secure),
                            "expires": int(c.expires or 0)})
        return out or None
    except Exception as e:
        logger.debug(f"[auth] firefox extract failed: {e}")
        return None


def _chromium_cdp_extract(browser_name: str):
    """Launch a Chromium browser with remote debugging and pull decrypted cookies
    via DevTools Protocol Network.getAllCookies. Sidesteps app-bound encryption.
    Returns list[dict] or None. Best-effort; never raises."""
    try:
        from _cdp_client import get_all_cookies_via_cdp  # Task 5b helper (same file OK)
        return get_all_cookies_via_cdp(browser_name)
    except Exception as e:
        logger.debug(f"[auth] chromium CDP extract failed: {e}")
        return None


class BrowserExtractProvider(CookieProvider):
    name = "browser"

    def __init__(self, browser: str = "auto", extractor=None, chromium_cdp=None):
        self.browser = browser
        # A caller-supplied extractor produces cookies without a real installed browser, so the
        # provider is usable even where none is detected (e.g. a CI runner with no Firefox). In
        # production nothing injects an extractor, so this leaves the normal detection path intact.
        self._custom_extractor = extractor is not None
        self._extractor = extractor or _firefox_extract
        self._chromium_cdp = chromium_cdp or _chromium_cdp_extract

    def _order(self) -> list[str]:
        if self.browser and self.browser != "auto":
            return [self.browser]
        return ["firefox", "chrome", "edge", "brave"]

    def _detect(self, name: str) -> bool:
        if name == "firefox":
            # firefox profiles dir existence is enough of a signal
            candidates = [Path.home() / "AppData/Roaming/Mozilla/Firefox",
                          Path.home() / ".mozilla/firefox",
                          Path.home() / "Library/Application Support/Firefox"]
            return any(c.exists() for c in candidates)
        # Prefer CDP client's OS-aware binary lookup (macOS .app / Windows Program Files).
        try:
            from _cdp_client import _find_exe
            return _find_exe(name) is not None
        except Exception:
            return False

    def available(self) -> bool:
        if self._custom_extractor:
            return True
        return any(self._detect(n) for n in self._order())

    def acquire(self) -> str | None:
        tried: list[str] = []
        for name in self._order():
            if not self._detect(name) and self.browser == "auto":
                continue
            tried.append(name)
            cookies = (self._extractor(name) if name == "firefox"
                       else self._chromium_cdp(name))
            if cookies:
                # Keep ONLY Google/YouTube rows for the auth store. FI4-04: FAIL CLOSED — a browser
                # with cookies but no YouTube/Google ones must not have its unrelated site sessions
                # (bank/email/…) copied into our plaintext store; skip it and fall through to the
                # "no cookies found" warning below. Label-boundary match (no evilgoogle.com).
                yt = [c for c in cookies if _is_yt_auth_domain(c.get("domain"))]
                if not yt:
                    continue                          # found cookies but none ours -> fall to warning
                out = cookie_store_dir() / "browser.txt"
                write_netscape(yt, out)
                logger.info(f"[auth] extracted cookies from {name} ({len(yt)} rows)")
                return str(out)
        if tried:
            # NEVER fail silently: a browser was present but auto-extract yielded no cookies (commonly
            # Windows app-bound encryption). Say so, and point at the tiers that actually work — so the
            # user gets a clear next step instead of a cryptic auth error deep in a later download.
            logger.warning(
                "[auth] auto-extract from %s found no cookies (best-effort; defeated by Windows "
                "app-bound encryption). Import a cookies.txt or use the one-time login in the Auth tab.",
                "/".join(tried))
        return None

    def list_detected(self) -> list[str]:
        """Browser names this provider can attempt on this machine."""
        return [n for n in self._order() if self._detect(n)]


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _default_profile_dir() -> str:
    # Dedicated Chromium user-data-dir for Auth Login (not the user's daily Chrome).
    return str(cookie_store_dir() / "yt_final_chrome")


def _find_login_browser() -> tuple[str | None, str | None]:
    """Return (browser_name, executable_path) for interactive login."""
    try:
        from _cdp_client import _find_exe
    except Exception:
        return None, None
    for name in ("chrome", "edge", "brave"):
        exe = _find_exe(name)
        if exe:
            return name, exe
    return None, None


def native_login_available() -> bool:
    return _find_login_browser()[1] is not None


_YT_AUTH_DOMAINS = ("youtube.com", "google.com", "youtu.be", "ggpht.com")


def _is_yt_auth_domain(domain: str | None) -> bool:
    """True iff the cookie domain is a YouTube/Google auth domain, matched on DNS-LABEL boundaries —
    so `evilgoogle.com` (a substring of google.com) does NOT match."""
    cd = (domain or "").lower().lstrip(".")
    return any(cd == d or cd.endswith("." + d) for d in _YT_AUTH_DOMAINS)


def _filter_yt_cookies(cookies: list[dict] | None) -> list[dict]:
    """FI4-04: return ONLY the YouTube/Google rows — never fall back to the full jar. Importing from a
    browser with no matching cookie must NOT copy unrelated site sessions (bank/email/…) into the
    plaintext cookie store; callers coerce [] -> None ("no cookies found")."""
    if not cookies:
        return []
    return [c for c in cookies if _is_yt_auth_domain(c.get("domain"))]


#: Why the last managed-profile export failed, for the caller to put in front of the user.
#: Cleared on each attempt so a stale reason can never be reported against a fresh one.
_LAST_EXPORT_FAILURE: str | None = None


def _record_export_failure(error: BaseException | None) -> None:
    global _LAST_EXPORT_FAILURE
    _LAST_EXPORT_FAILURE = None if error is None else f"{type(error).__name__}: {error}"


def _record_export_failure_text(text: str | None) -> None:
    global _LAST_EXPORT_FAILURE
    if text:
        _LAST_EXPORT_FAILURE = text


def last_export_failure() -> str | None:
    """The reason the most recent profile export failed, or None."""
    return _LAST_EXPORT_FAILURE


def _cdp_export_profile(profile_dir: str, browser_name: str = "chrome"):
    """Read cookies from a Chromium user-data-dir via snapshot + CDP (no UI login)."""
    _record_export_failure(None)
    try:
        from _cdp_client import get_all_cookies_via_cdp
        cookies = get_all_cookies_via_cdp(browser_name, profile_dir=profile_dir)
        return _filter_yt_cookies(cookies) or None
    except Exception as e:
        # WARNING, not debug. This is the only place the real reason exists, and the caller can
        # only see "no cookies" -- which it then reports to the user as "you did not sign in".
        # A profile refused for its directory permissions looked identical to never logging in.
        logger.warning("[auth] could not read the login profile: %s: %s", type(e).__name__, e)
        _record_export_failure(e)
        return None
    finally:
        # The snapshot layer is the only one that sees the real exception; get_all_cookies_via_cdp
        # collapses it to None without raising, so pull the reason across rather than inventing one.
        if _LAST_EXPORT_FAILURE is None:
            try:
                from _cdp_client import last_snapshot_failure
            except ImportError:
                from Final._cdp_client import last_snapshot_failure  # type: ignore
            _record_export_failure_text(last_snapshot_failure())


def _quit_hint() -> str:
    """Platform-accurate 'how to fully quit the browser' phrasing. Windows has no Cmd+Q, and a
    lingering background/tray chrome.exe is exactly what keeps the cookie DB locked there."""
    if sys.platform == 'darwin':
        return "macOS: Cmd+Q on that window’s app instance"
    if sys.platform.startswith('win'):
        return ("Windows: close every window, then check the system tray / Task Manager "
                "for a lingering chrome.exe background process")
    return "close every window, then check for a lingering chrome process"   # Linux/other


def _profile_lock_present(profile_dir: Path) -> bool:
    # 'lockfile' is the Windows Chromium artifact; macOS/Linux never create a file by that name,
    # so including it is inert on POSIX.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"):
        if (profile_dir / name).exists():
            return True
    return False


def _wait_profile_unlocked(profile_dir: Path, timeout: float = 30.0) -> bool:
    """Wait until Chromium releases the profile lock after the user quits."""
    import time
    # Windows' ProcessSingleton uses a kernel mutex + a lockfile it does not reliably remove, so
    # there is nothing pollable; give a fixed grace instead of spinning to a false timeout.
    # (Must sit AFTER `import time` — `time` is function-local, so touching it earlier raises.)
    if os.name == 'nt':
        time.sleep(min(2.0, timeout))
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _profile_lock_present(profile_dir):
            time.sleep(0.4)  # brief flush grace
            if not _profile_lock_present(profile_dir):
                return True
        time.sleep(0.25)
    return not _profile_lock_present(profile_dir)


def _export_managed_profile(profile_dir: str | None = None, browser_name: str = "chrome") -> str | None:
    """Export managed login profile cookies → profile.txt. Returns path or None."""
    pdir = Path(profile_dir or _default_profile_dir())
    if not pdir.is_dir():
        return None
    _wait_profile_unlocked(pdir, timeout=20.0)
    cookies = _cdp_export_profile(str(pdir), browser_name=browser_name)
    if not cookies:
        return None
    out = cookie_store_dir() / "profile.txt"
    write_netscape(cookies, out)
    logger.info("[auth] exported %d cookies from managed profile", len(cookies))
    return str(out)


class HeadlessProfileProvider(CookieProvider):
    name = "profile"

    def __init__(self, profile_dir: str | None = None, exporter=None, browser_name: str | None = None):
        self.profile_dir = profile_dir or _default_profile_dir()
        # None => resolve at acquire time from what is actually installed (see _resolved_browser).
        self.browser_name = browser_name
        self._exporter = exporter  # optional override for tests

    def _resolved_browser(self) -> str:
        """Which installed Chromium to drive for the export.

        Defaults to the SAME browser _find_login_browser() picks (chrome, then edge, then brave)
        instead of the literal "chrome" this used to hardcode. On a stock Windows box where Edge is
        the only Chromium, _find_exe("chrome") returns None, so the export bailed out and this tier
        could never produce a file — while available() (dir exists + native_login_available(), which
        Edge alone satisfies) still advertised it in tiers_available and made every acquire walk it.
        The managed profile is created by whichever browser _find_login_browser() chose, so resolving
        it the same way keeps writer and reader in agreement. An explicit browser_name always wins."""
        if self.browser_name:
            return self.browser_name
        return _find_login_browser()[0] or "chrome"

    def available(self) -> bool:
        return Path(self.profile_dir).exists() and native_login_available()

    def acquire(self) -> str | None:
        if self._exporter is not None:
            cookies = self._exporter(self.profile_dir)
            if not cookies:
                return None
            out = cookie_store_dir() / "profile.txt"
            write_netscape(cookies, out)
            return str(out)
        return _export_managed_profile(self.profile_dir, browser_name=self._resolved_browser())


def native_browser_login(profile_dir: str | None = None) -> dict:
    """Open real installed Chrome/Edge/Brave for human YouTube login (no Playwright).

    Google blocks Playwright-driven Chrome ("browser may not be secure") because
    CDP/automation is present. Launching the normal browser binary with only a
    dedicated --user-data-dir is accepted for Google sign-in.

    Blocks until that browser process exits (user should Cmd+Q / quit the window).
    Returns {ok, browser, exe, profile_dir, error?}.
    """
    name, exe = _find_login_browser()
    if not exe or not Path(exe).is_absolute():
        return {
            "ok": False,
            "error": (
                "No Chrome/Edge/Brave found. Install Google Chrome, then retry — "
                "or use Import from browser / cookies.txt."
            ),
            "code": "browser_missing",
        }
    pdir = Path(profile_dir or _default_profile_dir())
    pdir.mkdir(parents=True, exist_ok=True)

    # Minimal flags only — no remote-debugging, no automation switches.
    cmd = [
        exe,
        f"--user-data-dir={pdir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "https://www.youtube.com",
    ]
    logger.info("[auth] native login: %s profile=%s", name, pdir)
    print(
        f"Opening {name} for YouTube login (private yt_final profile).\n"
        "Sign in fully (2FA OK). When done, QUIT that browser completely "
        f"({_quit_hint()}) so cookies can be saved."
    )
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return {"ok": False, "error": f"Could not launch {name}: {e}", "code": "launch_failed"}

    try:
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        try:
            proc.terminate()
        except Exception:
            pass
        return {
            "ok": False,
            "error": "Login timed out after 1 hour. Close the browser and try again.",
            "code": "timeout",
            "browser": name,
            "profile_dir": str(pdir),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "browser": name, "profile_dir": str(pdir)}

    # Chrome may leave locks briefly after process exit.
    _wait_profile_unlocked(pdir, timeout=15.0)
    return {
        "ok": True,
        "browser": name,
        "exe": exe,
        "profile_dir": str(pdir),
    }


def playwright_login(profile_dir: str | None = None) -> bool:
    """Backward-compatible alias → native (non-Playwright) browser login."""
    return bool(native_browser_login(profile_dir).get("ok"))
