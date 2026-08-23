import os
import stat
import json
import gc
import signal
import shutil
import struct
import sys
import threading
import time
import weakref
from pathlib import Path

import pytest

import cookie_providers as cp
import cookies_auth as ca
import _cdp_client as cdp


def _make_disposable_windows_source_private(root: Path) -> None:
    """Own a disposable positive fixture as the current user, DACL and all."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    import secureio

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    set_security = advapi32.SetFileSecurityW
    set_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    set_security.restype = wintypes.BOOL
    # Set the OWNER (and group) as well as the DACL, mirroring
    # secureio._windows_private_descriptor. Under an elevated Administrators
    # token -- what a GitHub Actions Windows runner uses -- the OS stamps every
    # object pytest creates with BUILTIN\Administrators as owner, so a
    # DACL-only fixture never satisfies the production current-user-owner gate
    # in _require_windows_profile_owner and the snapshot fails for the wrong
    # reason. Re-owning the fixture as ourselves (always permitted) makes it
    # look like the real user profile these positive cases model.
    sid = secureio._windows_current_user_sid()
    sddl = f"O:{sid}G:{sid}D:P(A;;FA;;;{sid})"
    assert convert(sddl, 1, ctypes.byref(descriptor), None)
    try:
        paths = [root, *root.rglob("*")]
        for path in paths:
            # OWNER | GROUP | DACL_SECURITY_INFORMATION
            assert set_security(str(path), 0x1 | 0x2 | 0x4, descriptor)
    finally:
        kernel32.LocalFree(descriptor)


def _install_poisoned_macos_browser(monkeypatch, tmp_path) -> Path:
    """Create a HOME-controlled Chrome app and make discovery emulate macOS."""
    poisoned_home = tmp_path / "attacker-home"
    fake_browser = (
        poisoned_home
        / "Applications"
        / "Google Chrome.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome"
    )
    fake_browser.parent.mkdir(parents=True)
    fake_browser.write_bytes(b"attacker-controlled browser")
    fake_browser.chmod(0o755)
    monkeypatch.setenv("HOME", str(poisoned_home))
    monkeypatch.setattr(cdp.sys, "platform", "darwin")
    monkeypatch.setattr(
        Path,
        "home",
        classmethod(lambda cls: cls(os.environ["HOME"])),
    )
    monkeypatch.setattr(
        cdp.os,
        "access",
        lambda path, mode: Path(path) == fake_browser and mode == os.X_OK,
    )
    return fake_browser


@pytest.mark.parametrize(
    ("browser", "filename"),
    (
        ("chrome", "chrome.exe"),
        ("edge", "msedge.exe"),
        ("brave", "brave.exe"),
    ),
)
def test_credential_browser_discovery_ignores_cwd_and_path(
        browser, filename, tmp_path, monkeypatch):
    """A downloaded browser-shaped executable must never gain cookie/login authority."""
    poisoned = tmp_path / "download-folder"
    poisoned.mkdir()
    impostor = poisoned / filename
    impostor.write_bytes(b"not a browser")
    monkeypatch.chdir(poisoned)
    monkeypatch.setenv("PATH", str(poisoned))
    monkeypatch.setenv("PATHEXT", ".EXE")

    selected = cdp._find_exe(browser)

    assert selected is None or Path(selected).is_absolute()
    assert selected is None or Path(selected).resolve() != impostor.resolve()


def test_unknown_credential_browser_name_performs_no_executable_search(monkeypatch):
    """The browser allowlist must be checked before consulting any discovery authority."""
    monkeypatch.setattr(
        cdp.shutil,
        "which",
        lambda _name: (_ for _ in ()).throw(
            AssertionError("unknown browser reached PATH discovery")
        ),
    )

    assert cdp._find_exe("not-a-supported-browser") is None


def test_chromium_detector_does_not_restore_path_fallback(monkeypatch, tmp_path):
    """A fail-closed browser resolver must remain authoritative at the provider boundary."""
    poisoned = tmp_path / "chrome.exe"
    poisoned.write_bytes(b"not a browser")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setattr(cdp, "_find_exe", lambda _name: None)

    provider = cp.BrowserExtractProvider(browser="chrome")

    assert provider._detect("chrome") is False


def test_file_provider_unavailable_when_no_file(tmp_path, monkeypatch):
    # Isolate the global cookie store too: without pinning YT_FINAL_HOME the
    # provider's store-fallback candidates (<store>/active.txt, ...) resolve to
    # the REAL ~/.yt_final and pick up a developer's imported cookie file, so the
    # "no file anywhere" precondition only truly holds under an isolated home.
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    monkeypatch.delenv("YT_FINAL_COOKIES", raising=False)
    monkeypatch.chdir(tmp_path)
    p = cp.CookieFileProvider(output_dir=str(tmp_path))
    assert p.available() is False
    assert p.acquire() is None


def test_file_provider_finds_explicit_path(tmp_path):
    f = tmp_path / "my.txt"
    f.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    p = cp.CookieFileProvider(cookiefile=str(f))
    assert p.available() is True
    assert p.acquire() == str(f)


def test_file_provider_finds_imported_active_cookies(tmp_path, monkeypatch):
    # End-to-end seam: `auth import` writes <store>/active.txt; the file
    # provider must discover it even with no explicit path / env var / cwd
    # cookies.txt, proving the auth-import -> download() seam resolves.
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    monkeypatch.delenv("YT_FINAL_COOKIES", raising=False)
    monkeypatch.chdir(tmp_path)
    store = ca.cookie_store_dir()
    store.mkdir(parents=True, exist_ok=True)
    active = store / "active.txt"
    active.write_text("# Netscape HTTP Cookie File\nimported\n", encoding="utf-8")

    p = cp.CookieFileProvider()
    assert p.available() is True
    assert p.acquire() == str(active)


def test_file_provider_env_takes_precedence(tmp_path, monkeypatch):
    f = tmp_path / "env.txt"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setenv("YT_FINAL_COOKIES", str(f))
    monkeypatch.chdir(tmp_path)
    p = cp.CookieFileProvider()
    assert p.acquire() == str(f)


def test_browser_provider_writes_netscape_from_injected_extractor(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    fake_cookies = [{"domain": ".youtube.com", "name": "SID", "value": "z",
                     "secure": True, "expires": 2000000000}]
    p = cp.BrowserExtractProvider(
        browser="firefox",
        extractor=lambda name: fake_cookies,
    )
    assert p.available() is True
    out = p.acquire()
    assert out is not None
    assert Path(out).read_text(encoding="utf-8").count("SID") == 1


def test_browser_provider_none_when_extractor_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    p = cp.BrowserExtractProvider(browser="firefox", extractor=lambda name: None)
    assert p.acquire() is None


def test_yt_auth_domain_uses_dns_label_boundaries():
    for accepted in (".youtube.com", "www.youtube.com", "accounts.google.com", "youtu.be"):
        assert cp._is_yt_auth_domain(accepted), accepted
    for rejected in ("evilgoogle.com", "notyoutube.com", "google.com.evil.test", "", None):
        assert not cp._is_yt_auth_domain(rejected), rejected


def test_filter_and_browser_provider_fail_closed_on_unrelated_cookies(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    unrelated = [{"domain": ".bank.example", "name": "session", "value": "secret"}]
    assert cp._filter_yt_cookies(unrelated) == []

    provider = cp.BrowserExtractProvider(
        browser="firefox",
        extractor=lambda _name: unrelated,
    )
    assert provider.acquire() is None
    assert not (tmp_path / "cookies" / "browser.txt").exists()


def test_managed_profile_filter_coerces_no_matches_to_none_without_file(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(cp, "_wait_profile_unlocked", lambda *_a, **_k: True)
    monkeypatch.setattr(
        cdp,
        "get_all_cookies_via_cdp",
        lambda *_a, **_k: [{"domain": ".bank.example", "name": "session", "value": "secret"}],
    )

    assert cp._export_managed_profile(str(profile)) is None
    assert not (tmp_path / "cookies" / "profile.txt").exists()


def test_native_browser_login_builds_minimal_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    fake_chrome = tmp_path / "Google Chrome"
    fake_chrome.write_text("#!/bin/sh\n")
    fake_chrome.chmod(0o755)
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append(list(cmd))

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(cp, "_find_login_browser", lambda: ("chrome", str(fake_chrome)))
    monkeypatch.setattr(cp.subprocess, "Popen", FakePopen)
    r = cp.native_browser_login()
    assert r["ok"] is True
    assert calls, "Chrome should be launched"
    argv = calls[0]
    assert Path(argv[0]).is_absolute()
    assert any(a.startswith("--user-data-dir=") for a in argv)
    assert not any("remote-debugging" in a for a in argv)
    assert not any("enable-automation" in a for a in argv)
    assert "https://www.youtube.com" in argv


def test_native_browser_login_rejects_relative_executable_before_popen(
        monkeypatch, tmp_path):
    """A broken resolver contract must fail closed at the final process boundary."""
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append(list(cmd))

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(cp, "_find_login_browser", lambda: ("chrome", "chrome.exe"))
    monkeypatch.setattr(cp.subprocess, "Popen", FakePopen)

    result = cp.native_browser_login()

    assert result["ok"] is False
    assert calls == []


def test_native_browser_login_rejects_home_app_before_profile_or_popen(
        monkeypatch, tmp_path):
    """HOME-controlled macOS apps must fail before profile or process work."""
    fake_browser = _install_poisoned_macos_browser(
        monkeypatch,
        tmp_path,
    )
    managed_profile = tmp_path / "must-not-be-created"
    launches = []
    unlocks = []

    class FakePopen:
        def __init__(self, argv, **_kwargs):
            launches.append(list(argv))

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(cp.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        cp,
        "_wait_profile_unlocked",
        lambda *_args, **_kwargs: unlocks.append(True) or True,
    )

    result = cp.native_browser_login(str(managed_profile))

    assert result["ok"] is False
    assert result["code"] == "browser_missing"
    assert not managed_profile.exists()
    assert launches == []
    assert unlocks == []
    assert fake_browser.is_file()


def test_browser_provider_chromium_uses_cdp_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    fake = [{"domain": ".youtube.com", "name": "SAPISID", "value": "q"}]
    p = cp.BrowserExtractProvider(
        browser="chrome",
        chromium_cdp=lambda name: fake,
    )
    out = p.acquire()
    assert out is not None and "SAPISID" in Path(out).read_text(encoding="utf-8")


def test_profile_provider_unavailable_without_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    p = cp.HeadlessProfileProvider(profile_dir=str(tmp_path / "nope"),
                                   exporter=lambda d: None)
    assert p.available() is False


def test_profile_provider_acquires_via_injected_exporter(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    prof = tmp_path / "profile"; prof.mkdir()
    fake = [{"domain": ".youtube.com", "name": "LOGIN_INFO", "value": "v"}]
    p = cp.HeadlessProfileProvider(profile_dir=str(prof), exporter=lambda d: fake)
    monkeypatch.setattr(cp, "native_login_available", lambda: True)
    assert p.available() is True
    out = p.acquire()
    assert out and "LOGIN_INFO" in Path(out).read_text(encoding="utf-8")


class _FakeProc:
    """Stand-in browser process whose terminate() never takes: the first wait()
    (after terminate) raises TimeoutExpired, forcing the kill escalation path."""
    def __init__(self):
        self.terminated = False
        self.killed = False
        self._waits = 0

    def poll(self):
        return None  # report alive so the discovery loop doesn't early-return

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self._waits += 1
        if self._waits == 1:
            raise cdp.subprocess.TimeoutExpired(cmd="fake-browser", timeout=timeout)
        return 0


@pytest.mark.skipif(
    os.name != "nt",
    # The fixture only stubs _WindowsJobProcess.launch, so this drives the
    # Windows half of _launch_browser and the Windows branch of
    # _terminate_process_tree (terminate -> kill). On POSIX _launch_browser
    # takes the subprocess.Popen path and _terminate_process_tree signals a real
    # process group by pid, which a pidless fake cannot stand in for; the POSIX
    # TERM -> KILL escalation has its own test
    # (test_cdp_posix_cleanup_escalates_process_group).
    reason="Windows Job Objects",
)
def test_cdp_cleanup_kills_process_that_ignores_terminate(tmp_path, monkeypatch):
    # Regression: get_all_cookies_via_cdp's cleanup did terminate()+wait(5) inside a
    # bare except-pass, so a browser that didn't exit within 5s of the terminate
    # signal was orphaned. It must now escalate to kill().
    fake = _FakeProc()
    launched = []
    fake_browser = tmp_path / "fake-browser"
    userdir = tmp_path / "private-temp"
    userdir.mkdir()
    monkeypatch.setattr(cdp, "_find_exe", lambda name: str(fake_browser))
    monkeypatch.setattr(cdp, "_default_user_data_dir", lambda name: None)
    # Stub the pre-launch profile work and the endpoint wait so this stays a test
    # about cleanup rather than a race against the wall clock: the old 0.15s
    # budget had to cover real private-tempdir creation (cold ctypes/shell calls)
    # before the launch, and a loaded CI runner blew it, returning before any
    # process existed to terminate. Same seams _install_successful_cdp_flow uses.
    monkeypatch.setattr(cdp, "_make_private_tempdir", lambda: userdir)
    monkeypatch.setattr(
        cdp,
        "_wait_for_owned_endpoint",
        lambda *_a, **_k: (_ for _ in ()).throw(
            TimeoutError("no debugger endpoint")
        ),
    )

    def fake_job_launch(_cls, argv, **_kwargs):
        launched.append(list(argv))
        return fake

    monkeypatch.setattr(
        cdp._WindowsJobProcess,
        "launch",
        classmethod(fake_job_launch),
    )
    result = cdp.get_all_cookies_via_cdp("chrome", timeout=5)
    assert result is None
    assert fake.terminated is True
    assert fake.killed is True       # escalated instead of orphaning the process
    assert fake._waits == 2          # terminate-wait (timed out) + kill-wait
    assert launched and Path(launched[0][0]).is_absolute()


def test_cdp_rejects_relative_executable_before_popen(monkeypatch):
    """CDP must independently enforce an absolute argv[0] at its launch boundary."""
    calls = []

    class FakePopen:
        def __init__(self, argv, **_kwargs):
            calls.append(list(argv))

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(cdp, "_find_exe", lambda _name: "chrome.exe")
    monkeypatch.setattr(cdp, "_default_user_data_dir", lambda _name: None)
    monkeypatch.setattr(cdp.subprocess, "Popen", FakePopen)

    result = cdp.get_all_cookies_via_cdp("chrome", timeout=0)

    assert result is None
    assert calls == []


def test_cdp_rejects_home_app_before_temp_profile_or_popen(
        monkeypatch, tmp_path):
    """HOME-controlled macOS apps must fail before CDP creates any resources."""
    fake_browser = _install_poisoned_macos_browser(
        monkeypatch,
        tmp_path,
    )
    calls = []
    fake_userdir = tmp_path / "must-not-be-created"

    class FakeProc:
        def poll(self):
            return 1

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout=None):
            calls.append("wait")
            return 0

    def record(name, result):
        calls.append(name)
        return result

    monkeypatch.setattr(
        cdp.tempfile,
        "mkdtemp",
        lambda **_kwargs: record("mkdtemp", str(fake_userdir)),
    )
    monkeypatch.setattr(
        cdp,
        "_snapshot_profile",
        lambda *_args: record("snapshot", False),
    )
    monkeypatch.setattr(
        cdp.subprocess,
        "Popen",
        lambda *_args, **_kwargs: record("popen", FakeProc()),
    )
    monkeypatch.setattr(
        cdp.shutil,
        "rmtree",
        lambda *_args, **_kwargs: record("rmtree", None),
    )

    result = cdp.get_all_cookies_via_cdp(
        "chrome",
        timeout=0,
        profile_dir=str(tmp_path / "source-profile"),
    )

    assert result is None
    assert calls == []
    assert not fake_userdir.exists()
    assert fake_browser.is_file()


def test_cdp_snapshot_copies_key_and_cookie_db_owner_only(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    (source / "Local State").write_bytes(b"wrapped-key")
    cookies.write_bytes(b"sqlite-cookie-db")
    destination.mkdir()
    _make_disposable_windows_source_private(source)

    assert cdp._snapshot_profile(source, destination) is True, (
        "snapshot refused; recorded reason: " + repr(cdp.last_snapshot_failure()))

    local_state = destination / "Local State"
    copied_cookies = destination / "Default" / "Network" / "Cookies"
    assert local_state.read_bytes() == b"wrapped-key"
    assert copied_cookies.read_bytes() == b"sqlite-cookie-db"
    if os.name != "nt":
        assert stat.S_IMODE(local_state.stat().st_mode) == 0o600
        assert stat.S_IMODE(copied_cookies.stat().st_mode) == 0o600


def test_cdp_snapshot_failed_cookie_copy_cleans_private_temp(
        tmp_path, monkeypatch):
    import secureio

    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"sqlite-cookie-db")
    destination.mkdir()
    real_replace = secureio.os.replace

    def fail_cookie_replace(temporary, target):
        if Path(target).name == "Cookies":
            raise OSError("snapshot full")
        return real_replace(temporary, target)

    monkeypatch.setattr(secureio.os, "replace", fail_cookie_replace)

    assert cdp._snapshot_profile(source, destination) is False
    copied = destination / "Default" / "Network" / "Cookies"
    assert not copied.exists()
    assert not list(copied.parent.glob(".Cookies.*.tmp"))


def test_browser_extract_warns_when_empty(caplog):
    import logging, cookie_providers
    p = cookie_providers.BrowserExtractProvider(browser="chrome", chromium_cdp=lambda name: None)
    p._detect = lambda name: True                      # pretend the browser is installed
    with caplog.at_level(logging.WARNING, logger="yt_final"):
        assert p.acquire() is None
    assert any("auto-extract" in r.message and "no cookies" in r.message for r in caplog.records), \
        "Tier-3 empty result must warn loudly, not fail silently"


def test_headless_profile_provider_follows_the_installed_login_browser(monkeypatch, tmp_path):
    """CONFIRMED Windows finding: HeadlessProfileProvider hardcoded browser_name='chrome', so on a
    stock Edge-only Windows box the 'profile' tier advertised itself in tiers_available and was walked
    on every acquire, yet _find_exe('chrome') returned None so it could NEVER export a cookie. It must
    default to the SAME browser _find_login_browser picks (chrome/edge/brave order)."""
    import _cdp_client
    prof = tmp_path / "yt_final_chrome"
    prof.mkdir()
    # Edge-only box: only 'edge' resolves to an executable.
    monkeypatch.setattr(_cdp_client, "_find_exe",
                        lambda name: r"C:\Program Files\Microsoft\Edge\msedge.exe" if name == "edge" else None)
    p = cp.HeadlessProfileProvider(profile_dir=str(prof))
    assert p._resolved_browser() == "edge", "must follow the installed browser, not hardcode chrome"
    # an explicit name still wins (callers that know better keep control)
    assert cp.HeadlessProfileProvider(profile_dir=str(prof), browser_name="brave")._resolved_browser() == "brave"
    # and the resolved name is what actually reaches the exporter
    seen = {}
    monkeypatch.setattr(cp, "_export_managed_profile",
                        lambda d, browser_name=None: seen.setdefault("b", browser_name))
    p.acquire()
    assert seen["b"] == "edge", f"exporter got {seen.get('b')!r}, not the resolved browser"


class _ByteSocket:
    """Socket double that preserves byte-level framing and timeout calls."""

    def __init__(self, incoming: bytes):
        self.incoming = bytearray(incoming)
        self.timeouts = []
        self.sent = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, size):
        if not self.incoming:
            return b""
        take = min(size, len(self.incoming))
        data = bytes(self.incoming[:take])
        del self.incoming[:take]
        return data

    def sendall(self, data):
        self.sent.append(bytes(data))

    def getpeername(self):
        return ("127.0.0.1", 9222)

    def close(self):
        self.closed = True


def _http_wire(body: bytes, *, content_length: int | None = None) -> bytes:
    announced = len(body) if content_length is None else content_length
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {announced}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )


def test_cdp_http_discovery_rejects_oversized_body_with_bounded_read(
        monkeypatch):
    """An oversized debugger response is rejected before its body is received."""
    sock = _ByteSocket(_http_wire(b"x" * 33))
    monkeypatch.setattr(
        cdp.socket,
        "create_connection",
        lambda *_a, **_k: sock,
    )

    with pytest.raises(ValueError, match="too large"):
        cdp._read_http_json(
            "http://127.0.0.1:9222/json/version",
            deadline=time.monotonic() + 5,
            max_bytes=32,
        )

    assert bytes(sock.incoming) == b"x" * 33
    assert sock.closed is True


def test_cdp_owned_endpoint_rejects_foreign_debugger_url(
        tmp_path, monkeypatch):
    """A same-port HTTP responder cannot redirect credential CDP to another host."""
    active = tmp_path / "DevToolsActivePort"
    active.write_text("9222\n/devtools/browser/owned", encoding="ascii")
    body = json.dumps({
        "webSocketDebuggerUrl": "ws://attacker.example:9222/devtools/browser/owned",
    }).encode("utf-8")
    sock = _ByteSocket(_http_wire(body))
    monkeypatch.setattr(
        cdp.socket,
        "create_connection",
        lambda *_a, **_k: sock,
    )

    with pytest.raises(ValueError, match="owned loopback"):
        cdp._discover_owned_endpoint(
            tmp_path,
            proc=_FakeProc(),
            deadline=time.monotonic() + 5,
        )


def test_cdp_owned_endpoint_requires_reported_nonzero_port(
        tmp_path, monkeypatch):
    """Port zero is a launch request, never an endpoint authority."""
    (tmp_path / "DevToolsActivePort").write_text(
        "0\n/devtools/browser/owned",
        encoding="ascii",
    )
    monkeypatch.setattr(
        cdp.socket,
        "create_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("invalid active-port file reached HTTP")
        ),
    )

    with pytest.raises(ValueError, match="port"):
        cdp._discover_owned_endpoint(
            tmp_path,
            proc=_FakeProc(),
            deadline=time.monotonic() + 5,
        )


def test_cdp_websocket_handshake_rejects_invalid_accept(monkeypatch):
    """A TCP listener that is not the requested WebSocket must fail closed."""
    response = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: attacker-value\r\n\r\n"
    )
    sock = _ByteSocket(response)
    monkeypatch.setattr(
        cdp.socket,
        "create_connection",
        lambda *_a, **_k: sock,
    )

    with pytest.raises(ValueError, match="handshake"):
        cdp._ws_handshake(
            "127.0.0.1",
            9222,
            "/devtools/browser/owned",
            deadline=time.monotonic() + 5,
        )

    assert sock.closed is True


@pytest.mark.parametrize(
    "frame",
    (
        b"\x81\x7e" + struct.pack(">H", 9) + b"x" * 9,
        b"\x01\x02{}",
    ),
)
def test_cdp_websocket_rejects_oversized_or_fragmented_frame(frame):
    """Frame caps and FIN validation prevent memory abuse and partial JSON."""
    sock = _ByteSocket(frame)

    with pytest.raises(ValueError):
        cdp._ws_recv(
            sock,
            deadline=time.monotonic() + 5,
            max_frame_bytes=8,
        )


def test_cdp_snapshot_rejects_allowlisted_profile_symlink(
        tmp_path):
    """A Default symlink must not copy credential files from outside the profile."""
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    destination = tmp_path / "snapshot"
    source.mkdir()
    cookies = outside / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"outside-cookie")
    destination.mkdir()
    try:
        (source / "Default").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    assert cdp._snapshot_profile(source, destination) is False
    assert not (destination / "Default" / "Network" / "Cookies").exists()


@pytest.mark.skipif(
    os.name != "nt",
    # The marker this fixture plants is Path.is_junction, an NTFS reparse-point
    # concept that only _WindowsProfileRoot consults. _PosixProfileRoot walks
    # the profile with O_NOFOLLOW directory descriptors instead, and its real
    # equivalent -- a genuine symlink -- is covered by
    # test_cdp_snapshot_rejects_allowlisted_profile_symlink just above.
    reason="Windows junction/reparse-point behavior",
)
def test_cdp_snapshot_rejects_allowlisted_profile_junction_marker(
        tmp_path, monkeypatch):
    """Windows junction/reparse traversal is rejected even when it looks like a dir."""
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"junction-cookie")
    destination.mkdir()
    marked = source / "Default"
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda self: self == marked,
        raising=False,
    )

    assert cdp._snapshot_profile(source, destination) is False
    assert not (destination / "Default" / "Network" / "Cookies").exists()


def test_cdp_snapshot_enforces_total_bytes_before_copy(tmp_path):
    """Removing the aggregate byte budget must not copy an oversized cookie set."""
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"12345")
    destination.mkdir()

    assert cdp._snapshot_profile(
        source,
        destination,
        deadline=time.monotonic() + 5,
        max_files=4,
        max_bytes=4,
    ) is False
    assert not (destination / "Default" / "Network" / "Cookies").exists()


def test_cdp_http_read_cannot_finish_after_absolute_deadline(monkeypatch):
    """A body read that consumes the remaining budget cannot return trusted JSON."""
    clock = [100.0]

    class SlowSocket(_ByteSocket):
        def recv(self, size):
            data = super().recv(size)
            clock[0] = 102.0
            return data

    sock = SlowSocket(_http_wire(b"{}"))
    monkeypatch.setattr(cdp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cdp.socket,
        "create_connection",
        lambda *_a, **_k: sock,
    )

    with pytest.raises(TimeoutError):
        cdp._read_http_json(
            "http://127.0.0.1:9222/json/version",
            deadline=101.0,
        )


def test_cdp_websocket_receive_cannot_finish_after_absolute_deadline(
        monkeypatch):
    """A complete zero-byte frame still has to arrive before the shared deadline."""
    clock = [100.0]

    class SlowSocket(_ByteSocket):
        def recv(self, size):
            data = super().recv(size)
            clock[0] = 102.0
            return data

    monkeypatch.setattr(cdp.time, "monotonic", lambda: clock[0])

    with pytest.raises(TimeoutError):
        cdp._ws_recv(
            SlowSocket(b"\x81\x00"),
            deadline=101.0,
        )


def test_cdp_owned_endpoint_rejects_active_port_changed_during_http(
        tmp_path, monkeypatch):
    """The profile-owned authority file must remain stable through discovery."""
    active = tmp_path / "DevToolsActivePort"
    active.write_text("9222\n/devtools/browser/owned", encoding="ascii")
    body = json.dumps({
        "webSocketDebuggerUrl":
            "ws://127.0.0.1:9222/devtools/browser/owned",
    }).encode("utf-8")
    sock = _ByteSocket(_http_wire(body))

    def connect_then_replace(*_args, **_kwargs):
        active.write_text(
            "9333\n/devtools/browser/replaced",
            encoding="ascii",
        )
        return sock

    monkeypatch.setattr(cdp.socket, "create_connection", connect_then_replace)

    with pytest.raises(ValueError, match="changed"):
        cdp._discover_owned_endpoint(
            tmp_path,
            proc=_FakeProc(),
            deadline=time.monotonic() + 5,
        )


def test_cdp_waits_for_partial_active_port_without_trusting_it(
        tmp_path, monkeypatch):
    """A one-line Chrome write is retried, never interpreted as an endpoint."""
    active = tmp_path / "DevToolsActivePort"
    active.write_text("9222\n", encoding="ascii")
    body = json.dumps({
        "webSocketDebuggerUrl":
            "ws://127.0.0.1:9222/devtools/browser/owned",
    }).encode("utf-8")
    sock = _ByteSocket(_http_wire(body))
    sleeps = []

    def finish_write(delay):
        sleeps.append(delay)
        active.write_text(
            "9222\n/devtools/browser/owned",
            encoding="ascii",
        )

    monkeypatch.setattr(cdp.time, "sleep", finish_write)
    monkeypatch.setattr(
        cdp.socket,
        "create_connection",
        lambda *_a, **_k: sock,
    )

    assert cdp._wait_for_owned_endpoint(
        tmp_path,
        proc=_FakeProc(),
        deadline=time.monotonic() + 5,
    ) == ("127.0.0.1", 9222, "/devtools/browser/owned")
    assert len(sleeps) == 1


def test_cdp_endpoint_wait_fails_immediately_when_browser_exits(
        tmp_path, monkeypatch):
    """A dead browser cannot leave discovery polling until timeout."""
    class ExitedProc:
        def poll(self):
            return 23

    monkeypatch.setattr(
        cdp.time,
        "sleep",
        lambda _delay: (_ for _ in ()).throw(
            AssertionError("dead process reached sleep")
        ),
    )

    with pytest.raises(RuntimeError, match="exited"):
        cdp._wait_for_owned_endpoint(
            tmp_path,
            proc=ExitedProc(),
            deadline=time.monotonic() + 5,
        )


def test_cdp_absolute_deadline_starts_before_profile_snapshot(
        tmp_path, monkeypatch):
    """A slow snapshot exhausts the one operation budget before any launch."""
    source = tmp_path / "source"
    source.mkdir()
    userdir = tmp_path / "private-temp"
    userdir.mkdir()
    browser = tmp_path / "browser.exe"
    browser.write_bytes(b"fake")
    clock = [100.0]
    launches = []

    monkeypatch.setattr(cdp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cdp, "_find_exe", lambda _name: str(browser))
    monkeypatch.setattr(cdp, "_default_user_data_dir", lambda _name: source)
    monkeypatch.setattr(cdp, "_make_private_tempdir", lambda: userdir)

    def slow_snapshot(*_args, **_kwargs):
        clock[0] = 102.0
        return True

    monkeypatch.setattr(cdp, "_snapshot_profile", slow_snapshot)
    monkeypatch.setattr(
        cdp.subprocess,
        "Popen",
        lambda *_a, **_k: launches.append(True) or _FakeProc(),
    )
    assert cdp.get_all_cookies_via_cdp("chrome", timeout=1.0) is None
    assert launches == []


def test_cdp_rejects_active_port_that_predates_browser_launch(
        tmp_path, monkeypatch):
    """A stale endpoint file in a supposedly fresh temp profile blocks launch."""
    source = tmp_path / "source"
    source.mkdir()
    userdir = tmp_path / "not-fresh"
    userdir.mkdir()
    (userdir / "DevToolsActivePort").write_text(
        "9222\n/devtools/browser/stale",
        encoding="ascii",
    )
    browser = tmp_path / "browser.exe"
    browser.write_bytes(b"fake")
    launches = []

    monkeypatch.setattr(cdp, "_find_exe", lambda _name: str(browser))
    monkeypatch.setattr(cdp, "_default_user_data_dir", lambda _name: source)
    monkeypatch.setattr(cdp, "_make_private_tempdir", lambda: userdir)
    monkeypatch.setattr(cdp, "_snapshot_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(
        cdp.subprocess,
        "Popen",
        lambda *_a, **_k: launches.append(True) or _FakeProc(),
    )

    assert cdp.get_all_cookies_via_cdp("chrome", timeout=1.0) is None
    assert launches == []


@pytest.mark.parametrize("platform", ("win32", "linux"))
def test_cdp_launch_isolated_process_tree(
        platform, tmp_path, monkeypatch):
    """Removing the new group/session flag must never orphan browser children."""
    browser = tmp_path / "browser.exe"
    browser.write_bytes(b"fake")
    userdir = tmp_path / "profile"
    userdir.mkdir()
    calls = []
    monkeypatch.setattr(cdp.sys, "platform", platform)
    if platform == "win32":
        monkeypatch.setattr(
            cdp._WindowsJobProcess,
            "launch",
            classmethod(
                lambda _cls, argv, **kwargs:
                    calls.append((list(argv), kwargs)) or _FakeProc()
            ),
        )
        monkeypatch.setattr(
            cdp.subprocess,
            "Popen",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("Windows launch bypassed Job containment")
            ),
        )
    else:
        monkeypatch.setattr(
            cdp.subprocess,
            "Popen",
            lambda argv, **kwargs:
                calls.append((list(argv), kwargs)) or _FakeProc(),
        )

    cdp._launch_browser(
        str(browser),
        userdir,
        deadline=time.monotonic() + 5,
    )

    argv, kwargs = calls[0]
    assert "--remote-debugging-port=0" in argv
    assert "--remote-debugging-address=127.0.0.1" in argv
    if platform == "win32":
        assert "deadline" in kwargs
    else:
        assert kwargs["start_new_session"] is True


def test_cdp_launch_rejects_unsupported_process_tree_platform(
        tmp_path, monkeypatch):
    """An OS without a supported tree primitive fails before Popen."""
    browser = tmp_path / "browser"
    browser.write_bytes(b"fake")
    userdir = tmp_path / "profile"
    userdir.mkdir()
    calls = []
    monkeypatch.setattr(cdp.sys, "platform", "unsupported-os")
    monkeypatch.setattr(
        cdp.subprocess,
        "Popen",
        lambda *_a, **_k: calls.append(True),
    )

    with pytest.raises(RuntimeError, match="unsupported"):
        cdp._launch_browser(
            str(browser),
            userdir,
            deadline=time.monotonic() + 5,
        )
    assert calls == []


def test_cdp_windows_cleanup_uses_trusted_tree_kill_with_remaining_budget(
        tmp_path, monkeypatch):
    """Windows cleanup must target the whole tree through fixed authority."""
    taskkill = tmp_path / "Windows" / "System32" / "taskkill.exe"
    taskkill.parent.mkdir(parents=True)
    taskkill.write_bytes(b"fake")
    calls = []

    class LiveProc:
        pid = 4321

        def poll(self):
            return None

        def kill(self):
            raise AssertionError("successful tree kill reached direct kill")

    class Result:
        returncode = 0

    monkeypatch.setattr(cdp.sys, "platform", "win32")
    monkeypatch.setattr(
        cdp,
        "_trusted_taskkill",
        lambda: taskkill,
        raising=False,
    )
    monkeypatch.setattr(
        cdp.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((list(argv), kwargs)) or Result(),
    )

    assert cdp._terminate_process_tree(
        LiveProc(),
        deadline=time.monotonic() + 2,
    ) is True
    argv, kwargs = calls[0]
    assert argv == [
        str(taskkill),
        "/F",
        "/T",
        "/PID",
        "4321",
    ]
    assert 0 < kwargs["timeout"] <= 2


def test_cdp_posix_cleanup_escalates_process_group(
        monkeypatch):
    """A group that ignores TERM receives KILL; descendants share the group."""
    signals = []

    class LiveProc:
        pid = 987

        def __init__(self):
            self.waits = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise cdp.subprocess.TimeoutExpired("browser", timeout)
            return 0

        def kill(self):
            raise AssertionError("group cleanup reached direct kill")

    monkeypatch.setattr(cdp.sys, "platform", "linux")
    monkeypatch.setattr(cdp.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(cdp.os, "getpgid", lambda pid: pid, raising=False)

    def fake_killpg(pgid, sig):
        signals.append((pgid, sig))
        if sig == 0 and any(item[1] == 9 for item in signals):
            raise ProcessLookupError

    monkeypatch.setattr(
        cdp.os,
        "killpg",
        fake_killpg,
        raising=False,
    )

    assert cdp._terminate_process_tree(
        LiveProc(),
        deadline=time.monotonic() + 5,
    ) is True
    assert signals == [
        (987, signal.SIGTERM),
        (987, 0),
        (987, 9),
        (987, 0),
    ]


def test_cdp_posix_cleanup_checks_descendants_after_leader_exit(
        monkeypatch):
    """Leader wait success is not tree success while its process group survives."""
    calls = []
    probes = [0]

    class ExitedLeader:
        pid = 654

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_killpg(pgid, sig):
        calls.append((pgid, sig))
        if sig == 0:
            probes[0] += 1
            if probes[0] > 1:
                raise ProcessLookupError

    monkeypatch.setattr(cdp.sys, "platform", "linux")
    monkeypatch.setattr(cdp.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(cdp.os, "killpg", fake_killpg, raising=False)

    assert cdp._terminate_process_tree(
        ExitedLeader(),
        deadline=time.monotonic() + 5,
    ) is True
    assert calls == [
        (654, signal.SIGTERM),
        (654, 0),
        (654, 9),
        (654, 0),
    ]


def test_cdp_private_temp_creation_cleans_failed_security_setup(
        tmp_path, monkeypatch):
    """A temp profile whose private ACL/mode cannot be proven is removed."""
    monkeypatch.setattr(
        cdp,
        "_secure_private_directory",
        lambda _path: (_ for _ in ()).throw(
            PermissionError("ACL unavailable")
        ),
        raising=False,
    )

    with pytest.raises(PermissionError, match="ACL unavailable"):
        cdp._make_private_tempdir(base_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cdp_snapshot_secures_every_created_subdirectory(
        tmp_path, monkeypatch):
    """A protected root cannot leave Default/Network with inherited broad ACLs."""
    root = tmp_path / "private-root"
    root.mkdir()
    nested = root / "Default" / "Network"
    secured = []
    monkeypatch.setattr(
        cdp,
        "_secure_private_directory",
        lambda path: secured.append(Path(path)),
    )

    cdp._mkdir_private(nested, root=root)

    assert secured == [root, root / "Default", nested]


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_cdp_windows_temp_directory_is_owner_only_at_creation(
        tmp_path, monkeypatch):
    """Tightening after mkdtemp must not hide a public first-directory window."""
    from test_data_security import _assert_windows_owner_only_acl

    monkeypatch.setattr(
        cdp,
        "_secure_private_directory",
        lambda _path: None,
    )
    path = cdp._make_private_tempdir(base_dir=tmp_path)
    try:
        _assert_windows_owner_only_acl(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_cdp_windows_snapshot_subdir_is_owner_only_at_creation(
        tmp_path, monkeypatch):
    """Default/Network are private from creation, before post-create checking."""
    from test_data_security import _assert_windows_owner_only_acl

    root = cdp._make_private_tempdir(base_dir=tmp_path)
    nested = root / "Default" / "Network"
    monkeypatch.setattr(
        cdp,
        "_secure_private_directory",
        lambda _path: None,
    )
    try:
        cdp._mkdir_private(nested, root=root)
        _assert_windows_owner_only_acl(root / "Default")
        _assert_windows_owner_only_acl(nested)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_cdp_temp_cleanup_retries_within_shared_deadline(
        tmp_path, monkeypatch):
    """A transient Windows-style unlink failure gets a bounded second attempt."""
    userdir = tmp_path / "private-temp"
    userdir.mkdir()
    calls = []
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path, **kwargs):
        calls.append((Path(path), kwargs))
        if len(calls) > 1:
            real_rmtree(path)

    monkeypatch.setattr(cdp.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(cdp.time, "sleep", lambda _delay: None)

    assert cdp._cleanup_private_tempdir(
        userdir,
        deadline=time.monotonic() + 5,
    ) is True
    assert len(calls) == 2
    assert not userdir.exists()


def test_cdp_http_slow_trickle_is_interrupted_by_monotonic_deadline(
        monkeypatch):
    """Refreshing the remaining budget per recv stops an endless byte trickle."""
    body = b"{}"
    wire = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    clock = [100.0]

    class TrickleSocket(_ByteSocket):
        def recv(self, size):
            data = super().recv(min(size, 1))
            clock[0] += 0.6
            return data

    sock = TrickleSocket(wire)
    monkeypatch.setattr(cdp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cdp.socket,
        "create_connection",
        lambda *_a, **_k: sock,
    )
    with pytest.raises(TimeoutError):
        cdp._read_http_json(
            "http://127.0.0.1:9222/json/version",
            deadline=101.0,
        )
    assert sock.closed is True


def test_cdp_cookie_command_skips_notifications_until_matching_response(
        tmp_path, monkeypatch):
    """An unrelated CDP event cannot be mistaken for an empty cookie response."""
    browser = tmp_path / "browser.exe"
    browser.write_bytes(b"fake")
    userdir = tmp_path / "private-temp"
    userdir.mkdir()
    proc = _FakeProc()
    sock = _ByteSocket(b"")
    frames = iter((
        json.dumps({
            "method": "Target.targetCreated",
            "params": {"targetInfo": {"targetId": "page-1"}},
        }),
        json.dumps({
            "id": 1,
            "result": {
                "cookies": [{
                    "domain": ".youtube.com",
                    "name": "SID",
                    "value": "secret",
                    "path": "/",
                    "secure": True,
                    "expires": 2_000_000_000,
                }],
            },
        }),
    ))
    monkeypatch.setattr(cdp, "_find_exe", lambda _name: str(browser))
    monkeypatch.setattr(cdp, "_default_user_data_dir", lambda _name: None)
    monkeypatch.setattr(cdp, "_make_private_tempdir", lambda: userdir)
    monkeypatch.setattr(cdp, "_launch_browser", lambda *_a, **_k: proc)
    monkeypatch.setattr(
        cdp,
        "_wait_for_owned_endpoint",
        lambda *_a, **_k:
            ("127.0.0.1", 9222, "/devtools/browser/owned"),
    )
    monkeypatch.setattr(cdp, "_ws_handshake", lambda *_a, **_k: sock)
    monkeypatch.setattr(cdp, "_ws_send", lambda *_a, **_k: None)
    monkeypatch.setattr(cdp, "_ws_recv", lambda *_a, **_k: next(frames))
    monkeypatch.setattr(cdp, "_terminate_process_tree", lambda *_a, **_k: True)

    def cleanup(path, **_kwargs):
        shutil.rmtree(path)
        return True

    monkeypatch.setattr(cdp, "_cleanup_private_tempdir", cleanup)
    monkeypatch.setattr(
        cdp.subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("whole flow bypassed the bounded launch abstraction")
        ),
    )

    assert cdp.get_all_cookies_via_cdp("chrome", timeout=5) == [{
        "domain": ".youtube.com",
        "name": "SID",
        "value": "secret",
        "path": "/",
        "secure": True,
        "expires": 2_000_000_000,
    }]


def test_cdp_active_port_accepts_chromium_exact_bytes_without_trailing_lf(
        tmp_path):
    """Chrome writes exactly two records separated by one LF, with no final LF."""
    active = tmp_path / "DevToolsActivePort"
    active.write_bytes(b"9222\n/devtools/browser/owned")

    port, path, _signature = cdp._active_port_state(
        active,
        time.monotonic() + 5,
    )

    assert (port, path) == (9222, "/devtools/browser/owned")


@pytest.mark.parametrize(
    "payload",
    (
        b"9222\n/devtools/browser/owned\n",
        b"9222\n/devtools/browser/owned\nextra",
        b"9222\n/devtools/browser/owned\nextra\n",
        b"9222",
    ),
)
def test_cdp_active_port_rejects_extra_or_missing_records(tmp_path, payload):
    """Extra records and the old trailing-LF assumption are both malformed."""
    active = tmp_path / "DevToolsActivePort"
    active.write_bytes(payload)

    with pytest.raises((ValueError, cdp._EndpointNotReady)):
        cdp._active_port_state(active, time.monotonic() + 5)


def _install_cdp_flow_until_discovery(
        monkeypatch, tmp_path, *, clock=None):
    browser = tmp_path / "browser.exe"
    browser.write_bytes(b"fake")
    userdir = tmp_path / "private-temp"
    userdir.mkdir()

    class FlowProc:
        pid = 2468

        def poll(self):
            return None

    if clock is not None:
        monkeypatch.setattr(cdp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cdp, "_find_exe", lambda _name: str(browser))
    monkeypatch.setattr(cdp, "_default_user_data_dir", lambda _name: None)
    monkeypatch.setattr(cdp, "_make_private_tempdir", lambda: userdir)
    monkeypatch.setattr(cdp, "_launch_browser", lambda *_a, **_k: FlowProc())
    monkeypatch.setattr(
        cdp,
        "_wait_for_owned_endpoint",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ValueError("discovery rejected")
        ),
    )
    return userdir


def test_cdp_termination_exception_cannot_skip_private_temp_deletion(
        tmp_path, monkeypatch):
    """Cleanup phases are isolated: a tree-cleanup exception cannot leak credentials."""
    userdir = _install_cdp_flow_until_discovery(monkeypatch, tmp_path)
    deleted = []
    monkeypatch.setattr(
        cdp,
        "_terminate_process_tree",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("termination backend failed")
        ),
    )

    def delete(path, **_kwargs):
        deleted.append(Path(path))
        shutil.rmtree(path)
        return True

    monkeypatch.setattr(cdp, "_cleanup_private_tempdir", delete)

    assert cdp.get_all_cookies_via_cdp("chrome", timeout=5) is None
    assert deleted == [userdir]
    assert not userdir.exists()


def test_cdp_termination_cannot_consume_deletion_subbudget(
        tmp_path, monkeypatch):
    """Termination and deletion receive independent slices of the hard deadline."""
    clock = [100.0]
    _install_cdp_flow_until_discovery(monkeypatch, tmp_path, clock=clock)
    deadlines = {}

    def terminate(_proc, *, deadline):
        deadlines["terminate"] = deadline
        clock[0] = deadline
        return False

    def delete(path, *, deadline):
        deadlines["delete"] = deadline
        deadlines["delete_started"] = clock[0]
        shutil.rmtree(path)
        return deadline > clock[0]

    monkeypatch.setattr(cdp, "_terminate_process_tree", terminate)
    monkeypatch.setattr(cdp, "_cleanup_private_tempdir", delete)

    assert cdp.get_all_cookies_via_cdp("chrome", timeout=10) is None
    assert deadlines["delete"] > deadlines["terminate"]
    assert deadlines["delete"] > deadlines["delete_started"]


def test_cdp_retained_launch_failure_reaches_reserved_cleanup(
        tmp_path, monkeypatch):
    userdir = _install_cdp_flow_until_discovery(monkeypatch, tmp_path)
    owners = set()
    retained = []
    terminated = []
    monkeypatch.setattr(
        cdp,
        "_WINDOWS_CLEANUP_OWNERS",
        owners,
    )

    def fail_launch(*_args, **_kwargs):
        owner = cdp._WindowsLaunchCleanupError(202, 303, 404, 101)
        retained.append(owner)
        raise owner

    def terminate(proc, *, deadline):
        terminated.append((proc, deadline))
        proc.process_handle = 0
        proc.thread_handle = 0
        proc.job_handle = 0
        cdp._unregister_windows_cleanup(proc)
        return True

    monkeypatch.setattr(cdp, "_launch_browser", fail_launch)
    monkeypatch.setattr(cdp, "_terminate_process_tree", terminate)
    monkeypatch.setattr(
        cdp,
        "_cleanup_private_tempdir",
        lambda path, **_kwargs: shutil.rmtree(path) is None,
    )

    assert cdp.get_all_cookies_via_cdp("chrome", timeout=5) is None
    assert retained and terminated and terminated[0][0] is retained[0]
    assert owners == set()
    assert not userdir.exists()


def test_cdp_posix_group_signal_failure_never_reports_leader_only_success(
        monkeypatch):
    """A group-signal failure still stops the leader but is never tree success."""
    direct_actions = []

    class LiveProc:
        pid = 765

        def poll(self):
            return None

        def kill(self):
            direct_actions.append("kill")

        def wait(self, timeout=None):
            direct_actions.append("wait")
            return 0

    monkeypatch.setattr(cdp.sys, "platform", "linux")
    monkeypatch.setattr(
        cdp.os,
        "killpg",
        lambda *_a: (_ for _ in ()).throw(PermissionError("denied")),
        raising=False,
    )

    assert cdp._terminate_process_tree(
        LiveProc(),
        deadline=time.monotonic() + 5,
    ) is False
    assert direct_actions == ["kill", "wait"]


def _install_successful_cdp_flow(
        monkeypatch, tmp_path, frames, *, clock=None, cleanup=None):
    browser = tmp_path / "browser.exe"
    browser.write_bytes(b"fake")
    userdir = tmp_path / "private-temp"
    userdir.mkdir()
    proc = _FakeProc()
    sock = _ByteSocket(b"")
    if clock is not None:
        monkeypatch.setattr(cdp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cdp, "_find_exe", lambda _name: str(browser))
    monkeypatch.setattr(cdp, "_default_user_data_dir", lambda _name: None)
    monkeypatch.setattr(cdp, "_make_private_tempdir", lambda: userdir)
    monkeypatch.setattr(cdp, "_launch_browser", lambda *_a, **_k: proc)
    monkeypatch.setattr(
        cdp,
        "_wait_for_owned_endpoint",
        lambda *_a, **_k:
            ("127.0.0.1", 9222, "/devtools/browser/owned"),
    )
    monkeypatch.setattr(cdp, "_ws_handshake", lambda *_a, **_k: sock)
    monkeypatch.setattr(cdp, "_ws_send", lambda *_a, **_k: None)
    monkeypatch.setattr(cdp, "_ws_recv", lambda *_a, **_k: next(frames))
    monkeypatch.setattr(cdp, "_terminate_process_tree", lambda *_a, **_k: True)

    def default_cleanup(path, **_kwargs):
        shutil.rmtree(path)
        return True

    monkeypatch.setattr(
        cdp,
        "_cleanup_private_tempdir",
        cleanup or default_cleanup,
    )
    return userdir


def test_cdp_bool_command_id_is_not_integer_command_authority(
        tmp_path, monkeypatch):
    """JSON true compares equal to 1 in Python but is not a valid CDP command id."""
    frames = iter((
        json.dumps({
            "id": True,
            "result": {"cookies": [{
                "domain": ".youtube.com",
                "name": "WRONG",
                "value": "wrong",
            }]},
        }),
        json.dumps({
            "id": 1,
            "result": {"cookies": [{
                "domain": ".youtube.com",
                "name": "RIGHT",
                "value": "right",
            }]},
        }),
    ))
    _install_successful_cdp_flow(monkeypatch, tmp_path, frames)

    result = cdp.get_all_cookies_via_cdp("chrome", timeout=5)

    assert result and result[0]["name"] == "RIGHT"


def test_cdp_decode_deadline_is_checked_before_cookie_normalization(
        tmp_path, monkeypatch):
    clock = [100.0]
    frame = json.dumps({
        "id": 1,
        "result": {"cookies": [{
            "domain": ".youtube.com",
            "name": "SID",
            "value": "secret",
        }]},
    })
    _install_successful_cdp_flow(
        monkeypatch,
        tmp_path,
        iter((frame,)),
        clock=clock,
    )
    real_loads = json.loads

    def slow_loads(value):
        result = real_loads(value)
        clock[0] = 109.0
        return result

    monkeypatch.setattr(cdp.json, "loads", slow_loads)

    assert cdp.get_all_cookies_via_cdp("chrome", timeout=10) is None


def test_cdp_normalization_and_final_return_recheck_deadline(
        tmp_path, monkeypatch):
    clock = [100.0]

    class SlowCookie(dict):
        def get(self, key, default=None):
            clock[0] = 109.0
            return super().get(key, default)

    message = {
        "id": 1,
        "result": {"cookies": [SlowCookie({
            "domain": ".youtube.com",
            "name": "SID",
            "value": "secret",
        })]},
    }
    _install_successful_cdp_flow(
        monkeypatch,
        tmp_path,
        iter(("ignored",)),
        clock=clock,
    )
    monkeypatch.setattr(cdp.json, "loads", lambda _value: message)

    assert cdp.get_all_cookies_via_cdp("chrome", timeout=10) is None


def test_cdp_final_return_rechecks_deadline_after_cleanup(
        tmp_path, monkeypatch):
    clock = [100.0]
    frame = json.dumps({
        "id": 1,
        "result": {"cookies": [{
            "domain": ".youtube.com",
            "name": "SID",
            "value": "secret",
        }]},
    })

    def slow_cleanup(path, **_kwargs):
        shutil.rmtree(path)
        clock[0] = 111.0
        return True

    _install_successful_cdp_flow(
        monkeypatch,
        tmp_path,
        iter((frame,)),
        clock=clock,
        cleanup=slow_cleanup,
    )

    assert cdp.get_all_cookies_via_cdp("chrome", timeout=10) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows ownership behavior")
def test_cdp_windows_snapshot_rejects_foreign_owned_source_handles(
        tmp_path, monkeypatch):
    import secureio

    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"foreign-cookie")
    destination.mkdir()
    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: "S-1-5-18",
    )
    monkeypatch.setattr(cdp, "_secure_private_directory", lambda _path: None)
    monkeypatch.setattr(
        cdp,
        "atomic_replace_private",
        lambda path, data: Path(path).write_bytes(data),
    )

    assert cdp._snapshot_profile(source, destination) is False
    assert not (destination / "Default" / "Network" / "Cookies").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ownership behavior")
def test_cdp_windows_rejects_foreign_owned_ancestor_handle(monkeypatch):
    import secureio

    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: "S-1-5-21-999-888-777-666",
    )
    monkeypatch.setattr(
        secureio,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-111-222-333-444",
    )

    with pytest.raises(PermissionError, match="ancestor"):
        cdp._require_windows_profile_owner(123, final=False)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership behavior")
def test_cdp_posix_snapshot_rejects_foreign_owned_root(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"foreign-cookie")
    destination.mkdir()
    actual_uid = os.geteuid()
    monkeypatch.setattr(cdp.os, "geteuid", lambda: actual_uid + 1)

    assert cdp._snapshot_profile(source, destination) is False
    assert not (destination / "Default" / "Network" / "Cookies").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink behavior")
def test_cdp_posix_snapshot_rejects_symlinked_ancestor(tmp_path):
    authority = tmp_path / "authority"
    source = authority / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"cookie")
    linked = tmp_path / "linked-authority"
    linked.symlink_to(authority, target_is_directory=True)
    destination.mkdir()

    assert cdp._snapshot_profile(
        linked / "source",
        destination,
    ) is False
    assert not (destination / "Default" / "Network" / "Cookies").exists()


@pytest.mark.skipif(
    os.name != "nt",
    # Same reason as the junction-marker case: only _open_windows_profile_root
    # consults Path.is_junction when walking ancestors. The POSIX ancestor guard
    # is O_NOFOLLOW-based and is covered by
    # test_cdp_posix_snapshot_rejects_symlinked_ancestor just above.
    reason="Windows junction/reparse-point behavior",
)
def test_cdp_snapshot_rejects_linked_ancestor_component(
        tmp_path, monkeypatch):
    source = tmp_path / "authority" / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"cookie")
    destination.mkdir()
    linked_ancestor = source.parent
    original_is_junction = getattr(Path, "is_junction", lambda _self: False)
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda self: self == linked_ancestor or original_is_junction(self),
        raising=False,
    )

    assert cdp._snapshot_profile(source, destination) is False
    assert not (destination / "Default" / "Network" / "Cookies").exists()


def test_cdp_snapshot_reads_opened_source_after_parent_path_swap(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    original_cookie = source / "Default" / "Network" / "Cookies"
    original_cookie.parent.mkdir(parents=True)
    original_cookie.write_bytes(b"original-cookie")
    destination.mkdir()
    _make_disposable_windows_source_private(source)
    real_read = cdp._OpenedProfileFile.read
    swapped = [False]

    def swap_then_read(opened, deadline, max_bytes):
        if not swapped[0]:
            swapped[0] = True
            moved = tmp_path / "moved-source"
            try:
                source.rename(moved)
            except PermissionError:
                # Windows keeps every directory/leaf authority handle open with
                # delete sharing controlled by the filesystem. A denied rename
                # is itself safe: the mutable pathname could not be swapped.
                pass
            else:
                attacker = source / "Default" / "Network" / "Cookies"
                attacker.parent.mkdir(parents=True)
                attacker.write_bytes(b"attacker-cookie")
        return real_read(opened, deadline, max_bytes)

    monkeypatch.setattr(cdp._OpenedProfileFile, "read", swap_then_read)

    assert cdp._snapshot_profile(source, destination) is True, (
        "snapshot refused; recorded reason: " + repr(cdp.last_snapshot_failure()))
    assert (
        destination / "Default" / "Network" / "Cookies"
    ).read_bytes() == b"original-cookie"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_cdp_windows_job_kills_descendant_on_close(tmp_path):
    """A benign grandchild cannot outlive the kill-on-close Job Object."""
    ready = tmp_path / "ready.txt"
    survived = tmp_path / "survived.txt"
    child_code = (
        "import pathlib,time;"
        "time.sleep(1.0);"
        f"pathlib.Path({str(survived)!r}).write_text('survived')"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(30)"
    )
    proc = cdp._WindowsJobProcess.launch(
        [sys.executable, "-c", parent_code],
        deadline=time.monotonic() + 10,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        proc.close_job()
        proc.wait(timeout=5)
        time.sleep(1.2)
        assert not survived.exists()
    finally:
        proc.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_cdp_windows_job_assigns_suspended_process_before_resume(monkeypatch):
    """Mocked API order proves no child can run before Job assignment."""
    import ctypes

    calls = []

    class FakeFunction:
        def __init__(self, name, implementation):
            self.name = name
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            calls.append(self.name)
            return self.implementation(*args)

    class FakeKernel:
        def __init__(self):
            self.CreateJobObjectW = FakeFunction(
                "create_job",
                lambda *_args: 101,
            )
            self.SetInformationJobObject = FakeFunction(
                "set_job_limits",
                lambda *_args: 1,
            )

            def create_process(*args):
                process_info = args[-1]._obj
                process_info.hProcess = 202
                process_info.hThread = 303
                process_info.dwProcessId = 404
                process_info.dwThreadId = 505
                return 1

            self.CreateProcessW = FakeFunction(
                "create_suspended",
                create_process,
            )
            self.AssignProcessToJobObject = FakeFunction(
                "assign_job",
                lambda *_args: 1,
            )
            self.ResumeThread = FakeFunction(
                "resume_thread",
                lambda *_args: 0,
            )
            self.TerminateProcess = FakeFunction(
                "terminate_process",
                lambda *_args: 1,
            )
            self.CloseHandle = FakeFunction(
                "close_handle",
                lambda *_args: 1,
            )

    fake_kernel = FakeKernel()
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: fake_kernel,
    )

    proc = cdp._WindowsJobProcess.launch(
        [sys.executable, "-c", "pass"],
        deadline=time.monotonic() + 5,
    )
    proc.close()

    assert calls.index("create_suspended") < calls.index("assign_job")
    assert calls.index("assign_job") < calls.index("resume_thread")


def test_cdp_posix_final_profile_authority_rejects_foreign_write_bits(
        monkeypatch):
    class Metadata:
        st_uid = 1234
        st_mode = stat.S_IFDIR | 0o777

    monkeypatch.setattr(cdp.os, "geteuid", lambda: 1234, raising=False)

    with pytest.raises(PermissionError, match="writable"):
        cdp._posix_owned_component(Metadata(), final=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission behavior")
@pytest.mark.parametrize("unsafe_part", ("root", "directory", "file"))
def test_cdp_posix_snapshot_rejects_writable_source_authority(
        tmp_path, unsafe_part):
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"cookie")
    destination.mkdir()

    targets = {
        "root": (source, 0o777),
        "directory": (cookies.parent, 0o777),
        "file": (cookies, 0o666),
    }
    target, mode = targets[unsafe_part]
    target.chmod(mode)

    assert cdp._snapshot_profile(source, destination) is False
    assert not (destination / "Default" / "Network" / "Cookies").exists()


@pytest.mark.parametrize(
    ("aces", "rejected"),
    (
        (None, True),
        (((0, 0, 0x00000002, "S-1-1-0"),), True),
        (((0, 0, 0x40000000, "S-1-5-32-545"),), True),
        (((0, 0x08, 0x40000000, "S-1-1-0"),), False),
        (((0, 0, 0x00000001, "S-1-1-0"),), False),
        (((0, 0, 0x40000000, "S-1-5-18"),), False),
        (((0, 0, 0x40000000, "S-1-5-21-current"),), False),
    ),
)
def test_cdp_windows_profile_dacl_rejects_effective_foreign_mutation(
        monkeypatch, aces, rejected):
    monkeypatch.setattr(
        cdp,
        "_windows_handle_attributes",
        lambda _handle: 0x10,
    )
    monkeypatch.setattr(
        cdp,
        "_require_windows_profile_owner",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cdp,
        "_windows_profile_dacl_aces",
        lambda _handle: aces,
        raising=False,
    )
    monkeypatch.setattr(
        cdp,
        "_windows_profile_trusted_sids",
        lambda: {
            "S-1-5-21-current",
            "S-1-5-18",
            "S-1-5-32-544",
        },
        raising=False,
    )

    if rejected:
        with pytest.raises(PermissionError, match="DACL"):
            cdp._validate_windows_profile_handle(
                123,
                directory=True,
                require_current_owner=True,
            )
    else:
        cdp._validate_windows_profile_handle(
            123,
            directory=True,
            require_current_owner=True,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
@pytest.mark.parametrize("directory", (True, False))
def test_cdp_windows_native_dacl_rejects_disposable_everyone_write(
        tmp_path, directory):
    """Only a disposable pytest object receives the adversarial test ACL."""
    import ctypes
    from ctypes import wintypes
    import secureio

    target = tmp_path / ("authority" if directory else "Cookies")
    if directory:
        target.mkdir()
    else:
        target.write_bytes(b"cookie")

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    set_security = advapi32.SetFileSecurityW
    set_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    set_security.restype = wintypes.BOOL
    current_sid = secureio._windows_current_user_sid()
    # Own the fixture as the current user alongside the adversarial DACL. On an
    # elevated-token host (a GitHub Actions Windows runner) pytest's objects are
    # owned by BUILTIN\Administrators, so a DACL-only fixture trips the earlier
    # owner gate ("profile source is not owned by current user") and never
    # reaches the DACL gate this test is about. The Everyone-write ACE below is
    # unchanged -- it must still be rejected.
    sddl = f"O:{current_sid}G:{current_sid}D:P(A;;FA;;;{current_sid})(A;;GW;;;WD)"
    assert convert(sddl, 1, ctypes.byref(descriptor), None)
    try:
        # OWNER | GROUP | DACL_SECURITY_INFORMATION
        assert set_security(str(target), 0x1 | 0x2 | 0x4, descriptor)
    finally:
        kernel32.LocalFree(descriptor)

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = create_file(
        str(target),
        0x00020000 | 0x80,
        0x1 | 0x2 | 0x4,
        None,
        3,
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    assert handle != invalid
    try:
        with pytest.raises(PermissionError, match="DACL"):
            cdp._validate_windows_profile_handle(
                int(handle),
                directory=directory,
                require_current_owner=True,
            )
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.parametrize(
    ("parts", "expiry_call"),
    (
        (("Cookies",), 1),
        (("Network", "Cookies"), 2),
    ),
)
def test_cdp_posix_relative_handle_closes_when_deadline_expires(
        monkeypatch, parts, expiry_call):
    closed = []
    remaining_calls = [0]

    class FakeOS:
        O_RDONLY = os.O_RDONLY
        O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
        O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

        @staticmethod
        def dup(_handle):
            return 11

        @staticmethod
        def open(*_args, **_kwargs):
            return 99

        @staticmethod
        def close(handle):
            closed.append(handle)

    def expire(_deadline):
        remaining_calls[0] += 1
        if remaining_calls[0] == expiry_call:
            raise TimeoutError("expired after relative open")
        return 1.0

    root = object.__new__(cdp._PosixProfileRoot)
    root.handle = 10
    monkeypatch.setattr(cdp, "os", FakeOS)
    monkeypatch.setattr(cdp, "_remaining", expire)

    with pytest.raises(TimeoutError, match="relative open"):
        root.open_file(parts, deadline=123.0)

    assert sorted(closed) == [11, 99]


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_cdp_windows_root_relative_handle_closes_on_deadline(
        tmp_path, monkeypatch):
    import ctypes

    closed = []
    remaining_calls = [0]

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernel:
        def __init__(self):
            self.CreateFileW = FakeFunction(lambda *_args: 10)
            self.CloseHandle = FakeFunction(
                lambda handle: closed.append(
                    int(getattr(handle, "value", handle))
                ) or 1
            )

    def expire(_deadline):
        remaining_calls[0] += 1
        if remaining_calls[0] == 2:
            raise TimeoutError("expired after root child open")
        return 1.0

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: FakeKernel())
    monkeypatch.setattr(
        cdp,
        "_validate_windows_profile_handle",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(cdp, "_nt_open_relative", lambda *_a, **_k: 99)
    monkeypatch.setattr(cdp, "_remaining", expire)

    with pytest.raises(TimeoutError, match="root child open"):
        cdp._open_windows_profile_root(tmp_path, 123.0)

    assert sorted(closed) == [10, 99]


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
@pytest.mark.parametrize(
    ("parts", "expiry_call"),
    (
        (("Cookies",), 1),
        (("Network", "Cookies"), 2),
    ),
)
def test_cdp_windows_relative_handle_closes_when_deadline_expires(
        tmp_path, monkeypatch, parts, expiry_call):
    import ctypes

    (tmp_path / "Network").mkdir(exist_ok=True)
    closed = []
    remaining_calls = [0]

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernel:
        def __init__(self):
            self.CloseHandle = FakeFunction(
                lambda handle: closed.append(
                    int(getattr(handle, "value", handle))
                ) or 1
            )

    def expire(_deadline):
        remaining_calls[0] += 1
        if remaining_calls[0] == expiry_call:
            raise TimeoutError("expired after relative open")
        return 1.0

    root = object.__new__(cdp._WindowsProfileRoot)
    root.path = tmp_path
    root.handle = 10
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: FakeKernel())
    monkeypatch.setattr(cdp, "_nt_open_relative", lambda *_a, **_k: 99)
    monkeypatch.setattr(cdp, "_remaining", expire)

    with pytest.raises(TimeoutError, match="relative open"):
        root.open_file(parts, deadline=123.0)

    assert closed == [99]


def test_cdp_posix_profile_enumeration_rechecks_deadline_after_sort(
        monkeypatch):
    clock = [100.0]
    root = object.__new__(cdp._PosixProfileRoot)
    root.handle = 10

    class Entry:
        def __init__(self, name):
            self.name = name

    class SlowEntries:
        def __iter__(self):
            return iter((Entry("Profile 1"), Entry("Default")))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            clock[0] = 102.0

    monkeypatch.setattr(cdp.os, "scandir", lambda _handle: SlowEntries())
    monkeypatch.setattr(cdp.time, "monotonic", lambda: clock[0])

    with pytest.raises(TimeoutError):
        root.profile_names(deadline=101.0)


def test_cdp_posix_profile_enumeration_keeps_bounded_count(monkeypatch):
    root = object.__new__(cdp._PosixProfileRoot)
    root.handle = 10

    class Entry:
        def __init__(self, name):
            self.name = name

    class Entries:
        def __iter__(self):
            return iter(tuple(
                Entry(f"Profile {index}")
                for index in range(1, 66)
            ))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(cdp.os, "scandir", lambda _handle: Entries())

    with pytest.raises(ValueError, match="profile directory limit"):
        root.profile_names(deadline=time.monotonic() + 5)


def test_cdp_posix_sigkill_group_failure_stops_leader_but_returns_false(
        monkeypatch):
    actions = []

    class LiveProc:
        pid = 876

        def poll(self):
            return None

        def wait(self, timeout=None):
            actions.append("wait")
            if actions.count("wait") == 1:
                raise cdp.subprocess.TimeoutExpired("browser", timeout)
            return 0

        def kill(self):
            actions.append("kill")

    def fake_killpg(_pid, sig):
        if sig == 9:
            raise PermissionError("kill denied")

    monkeypatch.setattr(cdp.sys, "platform", "linux")
    monkeypatch.setattr(cdp.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(cdp.os, "killpg", fake_killpg, raising=False)

    assert cdp._terminate_process_tree(
        LiveProc(),
        deadline=time.monotonic() + 5,
    ) is False
    assert actions == ["wait", "kill", "wait"]


def test_cdp_process_tree_docstring_describes_windows_job_containment():
    doc = cdp._terminate_process_tree.__doc__ or ""
    assert "kill-on-close Job" in doc
    assert "taskkill /T" not in doc


class _RecordingFunction:
    def __init__(self, name, implementation, calls):
        self.name = name
        self.implementation = implementation
        self.calls = calls
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(self.name)
        return self.implementation(*args)


class _FailingLaunchKernel:
    def __init__(
            self,
            *,
            terminate_result,
            wait_result,
            exit_code,
            assign_result=0,
            resume_result=0):
        self.calls = []
        self.closed = []

        def function(name, implementation):
            return _RecordingFunction(name, implementation, self.calls)

        self.CreateJobObjectW = function("create_job", lambda *_args: 101)
        self.SetInformationJobObject = function(
            "set_job_limits",
            lambda *_args: 1,
        )

        def create_process(*args):
            process_info = args[-1]._obj
            process_info.hProcess = 202
            process_info.hThread = 303
            process_info.dwProcessId = 404
            process_info.dwThreadId = 505
            return 1

        self.CreateProcessW = function("create_suspended", create_process)
        self.AssignProcessToJobObject = function(
            "assign_job",
            lambda *_args: assign_result,
        )
        self.ResumeThread = function(
            "resume_thread",
            lambda *_args: resume_result,
        )
        self.TerminateProcess = function(
            "terminate_process",
            lambda *_args: terminate_result,
        )
        self.TerminateJobObject = function(
            "terminate_job",
            lambda *_args: 0,
        )
        self.WaitForSingleObject = function(
            "wait_process",
            lambda *_args: wait_result,
        )

        def get_exit_code(_handle, output):
            output._obj.value = exit_code
            return 1

        self.GetExitCodeProcess = function("get_exit_code", get_exit_code)

        def close_handle(handle):
            self.closed.append(int(getattr(handle, "value", handle)))
            return 1

        self.CloseHandle = function("close_handle", close_handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_cdp_windows_launch_failure_verifies_process_death_before_close(
        monkeypatch):
    import ctypes

    kernel = _FailingLaunchKernel(
        terminate_result=0,
        wait_result=0,
        exit_code=1,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel)

    with pytest.raises(OSError):
        cdp._WindowsJobProcess.launch(
            [sys.executable, "-c", "pass"],
            deadline=time.monotonic() + 5,
        )

    assert kernel.calls.index("terminate_process") < kernel.calls.index(
        "wait_process"
    )
    assert kernel.calls.index("wait_process") < kernel.calls.index(
        "get_exit_code"
    )
    assert sorted(kernel.closed) == [101, 202, 303]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_cdp_windows_launch_failure_retains_handles_while_live_is_unproven(
        monkeypatch):
    import ctypes

    monkeypatch.setattr(cdp, "_WINDOWS_CLEANUP_OWNERS", set())
    kernel = _FailingLaunchKernel(
        terminate_result=0,
        wait_result=258,
        exit_code=259,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel)

    with pytest.raises(Exception) as captured:
        cdp._WindowsJobProcess.launch(
            [sys.executable, "-c", "pass"],
            deadline=time.monotonic() + 5,
        )

    assert getattr(captured.value, "process_handle", None) == 202
    assert getattr(captured.value, "thread_handle", None) == 303
    assert 202 not in kernel.closed
    assert 303 not in kernel.closed


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_cdp_windows_resume_failure_retains_assigned_job_authority(
        monkeypatch):
    import ctypes

    monkeypatch.setattr(cdp, "_WINDOWS_CLEANUP_OWNERS", set())
    kernel = _FailingLaunchKernel(
        terminate_result=0,
        wait_result=258,
        exit_code=259,
        assign_result=1,
        resume_result=0xFFFFFFFF,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel)

    with pytest.raises(Exception) as captured:
        cdp._WindowsJobProcess.launch(
            [sys.executable, "-c", "pass"],
            deadline=time.monotonic() + 5,
        )

    assert getattr(captured.value, "job_handle", None) == 101
    assert 101 not in kernel.closed
    assert kernel.calls.count("terminate_process") == 2
    assert "terminate_job" in kernel.calls


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_cdp_retained_launch_authority_retries_and_closes_after_death(
        monkeypatch):
    import ctypes

    closed = []

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernel:
        def __init__(self):
            self.TerminateJobObject = FakeFunction(lambda *_args: 1)
            self.TerminateProcess = FakeFunction(lambda *_args: 1)
            self.WaitForSingleObject = FakeFunction(lambda *_args: 0)

            def get_exit_code(_handle, output):
                output._obj.value = 1
                return 1

            self.GetExitCodeProcess = FakeFunction(get_exit_code)
            self.CloseHandle = FakeFunction(
                lambda handle: closed.append(
                    int(getattr(handle, "value", handle))
                ) or 1
            )

    retained = cdp._WindowsLaunchCleanupError(202, 303, 404, 101)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: FakeKernel())
    monkeypatch.setattr(cdp.sys, "platform", "win32")

    assert cdp._terminate_process_tree(
        retained,
        deadline=time.monotonic() + 5,
    ) is True
    assert sorted(closed) == [101, 202, 303]
    assert retained.process_handle == 0
    assert retained.thread_handle == 0
    assert retained.job_handle == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_cdp_windows_close_job_failure_keeps_handle_for_retry(monkeypatch):
    import ctypes

    calls = []
    owners = set()

    class FakeFunction:
        argtypes = None
        restype = None

        def __call__(self, handle):
            calls.append(int(getattr(handle, "value", handle)))
            return 0

    class FakeKernel:
        CloseHandle = FakeFunction()

    monkeypatch.setattr(cdp, "_WINDOWS_CLEANUP_OWNERS", owners)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: FakeKernel())
    proc = cdp._WindowsJobProcess(11, 22, 33, ["browser.exe"])

    with pytest.raises(OSError):
        proc.close_job()

    assert calls == [22]
    assert proc._job_handle == 22
    proc._job_handle = 0
    proc._process_handle = 0
    cdp._unregister_windows_cleanup(proc)
    assert owners == set()


def test_cdp_temp_cleanup_never_reports_success_after_slow_rmtree_deadline(
        tmp_path, monkeypatch):
    """A completed but overdue filesystem call is not in-budget cleanup."""
    userdir = tmp_path / "private-temp"
    userdir.mkdir()
    clock = [100.0]
    real_rmtree = shutil.rmtree

    def slow_rmtree(path):
        real_rmtree(path)
        clock[0] = 102.0

    monkeypatch.setattr(cdp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cdp.shutil, "rmtree", slow_rmtree)

    assert cdp._cleanup_private_tempdir(
        userdir,
        deadline=101.0,
    ) is False
    assert not userdir.exists()


def test_cdp_temp_cleanup_rechecks_deadline_after_missing_exists_probe(
        tmp_path, monkeypatch):
    """A slow missing-path probe cannot report in-budget cleanup."""
    userdir = tmp_path / "already-missing"
    clock = [100.0]
    real_exists = Path.exists

    def slow_exists(path):
        result = real_exists(path)
        if path == userdir:
            clock[0] = 102.0
        return result

    monkeypatch.setattr(cdp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(Path, "exists", slow_exists)

    assert cdp._cleanup_private_tempdir(
        userdir,
        deadline=101.0,
    ) is False


def test_cdp_posix_profile_enumeration_bounds_invalid_entries_and_closes(
        monkeypatch):
    """Invalid names cannot force an unbounded materialized directory listing."""
    root = object.__new__(cdp._PosixProfileRoot)
    root.handle = 10
    consumed = []
    closed = []

    class Entry:
        def __init__(self, name):
            self.name = name

    class Entries:
        def __iter__(self):
            for index in range(1, 10_000):
                consumed.append(index)
                yield Entry(f"attacker-{index}")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(True)

    monkeypatch.setattr(cdp.os, "scandir", lambda _handle: Entries())

    with pytest.raises(ValueError, match="profile directory limit"):
        root.profile_names(deadline=time.monotonic() + 5)

    assert len(consumed) == cdp._MAX_PROFILE_DIRECTORY_ENTRIES + 1
    assert closed == [True]


def test_cdp_waits_for_partially_published_active_port_second_line(
        tmp_path, monkeypatch):
    """A plausible prefix of Chromium's websocket record is retried."""
    active = tmp_path / "DevToolsActivePort"
    active.write_bytes(b"9222\n/devtools/bro")
    body = json.dumps({
        "webSocketDebuggerUrl":
            "ws://127.0.0.1:9222/devtools/browser/owned",
    }).encode("utf-8")
    sock = _ByteSocket(_http_wire(body))
    sleeps = []

    def finish_write(delay):
        sleeps.append(delay)
        active.write_bytes(b"9222\n/devtools/browser/owned")

    monkeypatch.setattr(cdp.time, "sleep", finish_write)
    monkeypatch.setattr(
        cdp.socket,
        "create_connection",
        lambda *_a, **_k: sock,
    )

    assert cdp._wait_for_owned_endpoint(
        tmp_path,
        proc=_FakeProc(),
        deadline=time.monotonic() + 5,
    ) == ("127.0.0.1", 9222, "/devtools/browser/owned")
    assert len(sleeps) == 1


def test_cdp_retries_when_active_port_path_lags_advertised_endpoint(
        tmp_path, monkeypatch):
    """A syntactically complete short browser ID can still be mid-publication."""
    active = tmp_path / "DevToolsActivePort"
    active.write_bytes(b"9222\n/devtools/browser/a")
    body = json.dumps({
        "webSocketDebuggerUrl":
            "ws://127.0.0.1:9222/devtools/browser/owned",
    }).encode("utf-8")
    sockets = [
        _ByteSocket(_http_wire(body)),
        _ByteSocket(_http_wire(body)),
    ]
    sleeps = []

    def finish_write(delay):
        sleeps.append(delay)
        active.write_bytes(b"9222\n/devtools/browser/owned")

    monkeypatch.setattr(cdp.time, "sleep", finish_write)
    monkeypatch.setattr(
        cdp.socket,
        "create_connection",
        lambda *_a, **_k: sockets.pop(0),
    )

    assert cdp._wait_for_owned_endpoint(
        tmp_path,
        proc=_FakeProc(),
        deadline=time.monotonic() + 5,
    ) == ("127.0.0.1", 9222, "/devtools/browser/owned")
    assert len(sleeps) == 1
    assert sockets == []


@pytest.mark.parametrize(
    "payload",
    (
        b"not-a-port",
        b"70000\n",
        b"9222\n/attacker",
        b"9222\n/devtools/browseX",
        b"9222\n/devtools/bro\xff",
    ),
)
def test_cdp_active_port_rejects_malformed_not_plausibly_partial(
        tmp_path, payload):
    """Only a structurally plausible publication prefix earns a retry."""
    active = tmp_path / "DevToolsActivePort"
    active.write_bytes(payload)

    with pytest.raises(ValueError):
        cdp._active_port_state(active, time.monotonic() + 5)


@pytest.mark.skipif(
    os.name != "nt",
    # Fakes ctypes.WinDLL, which does not exist on POSIX (the monkeypatch itself
    # raises AttributeError there); the code under test is Windows-only.
    reason="Windows DACL behavior",
)
def test_cdp_windows_dacl_checks_sid_bounds_before_native_sid_calls(
        monkeypatch):
    """A one-byte SID tail must be rejected before IsValidSid can read it."""
    import ctypes

    ace_buffer = (ctypes.c_ubyte * 9)()
    ace_buffer[0] = 0  # ACCESS_ALLOWED_ACE_TYPE
    ace_buffer[2] = 9
    ace_buffer[3] = 0
    ace_buffer[8] = 1
    sid_calls = []

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeAdvapi:
        def __init__(self):
            def get_security_info(
                    _handle, _kind, _flags, _owner, _group,
                    dacl_out, _sacl, _descriptor):
                dacl_out._obj.value = 111
                return 0

            def get_acl_information(_dacl, output, *_args):
                output._obj.AceCount = 1
                return 1

            def get_ace(_dacl, _index, output):
                output._obj.value = ctypes.addressof(ace_buffer)
                return 1

            self.GetSecurityInfo = FakeFunction(get_security_info)
            self.GetAclInformation = FakeFunction(get_acl_information)
            self.GetAce = FakeFunction(get_ace)
            self.ConvertSidToStringSidW = FakeFunction(
                lambda *_args: (_ for _ in ()).throw(
                    AssertionError("truncated SID reached conversion")
                )
            )
            self.IsValidSid = FakeFunction(
                lambda *_args: sid_calls.append(True) or 1
            )
            self.GetLengthSid = FakeFunction(
                lambda *_args: (_ for _ in ()).throw(
                    AssertionError("truncated SID reached length lookup")
                )
            )

    class FakeKernel:
        LocalFree = FakeFunction(lambda *_args: 0)

    advapi = FakeAdvapi()
    kernel = FakeKernel()
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **_kwargs:
            advapi if name == "advapi32" else kernel,
    )

    with pytest.raises(OSError, match="truncated|no SID|exceeds"):
        cdp._windows_profile_dacl_aces(123)

    assert sid_calls == []


@pytest.mark.parametrize(
    ("browser_name", "capability_name"),
    (
        ("chrome", "lpacChromeStableNetworkSandbox"),
        ("brave", "lpacChromeStableNetworkSandbox"),
        ("edge", "lpacChromeNetworkSandbox"),
    ),
)
def test_cdp_windows_network_capability_uses_exact_product_mapping(
        browser_name, capability_name):
    """Stable browser discovery maps to the exact Chromium capability name."""
    assert cdp._windows_network_capability_name(
        browser_name
    ) == capability_name


@pytest.mark.skipif(
    os.name != "nt",
    # Fakes ctypes.WinDLL, which does not exist on POSIX (the monkeypatch itself
    # raises AttributeError there); DeriveCapabilitySidsFromName is Windows-only.
    reason="Windows capability SID behavior",
)
@pytest.mark.parametrize("derived_capability_count", (1, 2))
def test_cdp_windows_capability_derivation_frees_every_native_allocation(
        derived_capability_count, monkeypatch):
    """Every SID and both arrays returned by the native API are released."""
    import ctypes

    group_sid = (ctypes.c_ubyte * 8)()
    capability_sid = (ctypes.c_ubyte * 8)()
    extra_capability_sid = (ctypes.c_ubyte * 8)()
    group_array = (ctypes.c_void_p * 1)(
        ctypes.addressof(group_sid)
    )
    capability_array = (ctypes.c_void_p * 2)(
        ctypes.addressof(capability_sid),
        ctypes.addressof(extra_capability_sid),
    )
    expected_sid = (
        "S-1-15-3-1024-1653277547-598600573-1504598449-"
        "2187120098-1115546814-1860042162-2924348907-3700367812"
    )
    string_buffers = []
    freed = []

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernelBase:
        def __init__(self):
            def derive(
                    name, groups_out, group_count_out,
                    capabilities_out, capability_count_out):
                assert name == "lpacChromeStableNetworkSandbox"
                ctypes.cast(
                    groups_out,
                    ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
                )[0] = ctypes.cast(
                    group_array,
                    ctypes.POINTER(ctypes.c_void_p),
                )
                ctypes.cast(
                    group_count_out,
                    ctypes.POINTER(ctypes.c_uint32),
                )[0] = 1
                ctypes.cast(
                    capabilities_out,
                    ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
                )[0] = ctypes.cast(
                    capability_array,
                    ctypes.POINTER(ctypes.c_void_p),
                )
                ctypes.cast(
                    capability_count_out,
                    ctypes.POINTER(ctypes.c_uint32),
                )[0] = derived_capability_count
                return 1

            self.DeriveCapabilitySidsFromName = FakeFunction(derive)

    class FakeAdvapi:
        def __init__(self):
            def convert(sid, output):
                assert int(sid.value) == ctypes.addressof(capability_sid)
                text = ctypes.create_unicode_buffer(expected_sid)
                string_buffers.append(text)
                output._obj.value = ctypes.addressof(text)
                return 1

            self.ConvertSidToStringSidW = FakeFunction(convert)

    class FakeKernel:
        def __init__(self):
            self.LocalFree = FakeFunction(
                lambda pointer:
                    freed.append(int(getattr(pointer, "value", pointer))) or 0
            )

    kernelbase = FakeKernelBase()
    advapi = FakeAdvapi()
    kernel = FakeKernel()
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **_kwargs: {
            "kernelbase": kernelbase,
            "advapi32": advapi,
            "kernel32": kernel,
        }[name],
    )

    if derived_capability_count == 1:
        assert cdp._derive_windows_capability_sid(
            "lpacChromeStableNetworkSandbox"
        ) == expected_sid
    else:
        with pytest.raises(RuntimeError, match="exactly one SID"):
            cdp._derive_windows_capability_sid(
                "lpacChromeStableNetworkSandbox"
            )
    expected_freed = {
        ctypes.addressof(group_sid),
        ctypes.addressof(capability_sid),
        ctypes.addressof(group_array),
        ctypes.addressof(capability_array),
    }
    if derived_capability_count == 2:
        expected_freed.add(ctypes.addressof(extra_capability_sid))
    assert expected_freed.issubset(set(freed))


def test_cdp_windows_capability_is_scoped_only_to_profile_network_subtree():
    """The LPAC writer is not trusted on root, Local State, or profile root."""
    capability = "S-1-15-3-1024-product"

    assert cdp._windows_profile_mutation_sids(
        ("Default", "Network"),
        capability,
    ) == frozenset({capability})
    assert cdp._windows_profile_mutation_sids(
        ("Default", "Network", "Cookies"),
        capability,
    ) == frozenset({capability})
    assert cdp._windows_profile_mutation_sids(
        ("Local State",),
        capability,
    ) == frozenset()
    assert cdp._windows_profile_mutation_sids(
        ("Default",),
        capability,
    ) == frozenset()
    assert cdp._windows_profile_mutation_sids(
        ("Default", "Cookies"),
        capability,
    ) == frozenset()
    assert cdp._windows_profile_mutation_sids(
        ("attacker", "Network", "Cookies"),
        capability,
    ) == frozenset()


def test_cdp_windows_dacl_rejects_wrong_product_capability(monkeypatch):
    """Only the capability derived for the selected browser may mutate Network."""
    chrome_capability = "S-1-15-3-1024-chrome"
    edge_capability = "S-1-15-3-1024-edge"
    monkeypatch.setattr(
        cdp,
        "_windows_profile_dacl_aces",
        lambda _handle: (
            (0, 0, 0x40000000, chrome_capability),
        ),
    )
    monkeypatch.setattr(
        cdp,
        "_windows_profile_trusted_sids",
        lambda: {
            "S-1-5-21-current",
            "S-1-5-18",
            "S-1-5-32-544",
        },
    )

    with pytest.raises(PermissionError, match="DACL"):
        cdp._require_windows_profile_dacl(
            123,
            allowed_mutation_sids=frozenset({edge_capability}),
        )

    cdp._require_windows_profile_dacl(
        123,
        allowed_mutation_sids=frozenset({chrome_capability}),
    )
    with pytest.raises(PermissionError, match="DACL"):
        cdp._require_windows_profile_dacl(123)


@pytest.mark.parametrize(
    "service_sid",
    (
        "S-1-5-19",
        "S-1-5-20",
        "S-1-5-80-123456789",
    ),
)
def test_cdp_windows_dacl_rejects_service_mutation_authority(
        service_sid, monkeypatch):
    """Service identities and service-SID prefixes are foreign DACL writers."""
    import secureio

    monkeypatch.setattr(
        secureio,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-current",
    )
    monkeypatch.setattr(
        cdp,
        "_windows_profile_dacl_aces",
        lambda _handle: (
            (0, 0, 0x40000000, service_sid),
        ),
    )

    with pytest.raises(PermissionError, match="DACL"):
        cdp._require_windows_profile_dacl(123)


@pytest.mark.skipif(os.name != "nt", reason="Windows ownership behavior")
@pytest.mark.parametrize(
    "owner_sid",
    (
        "S-1-5-19",
        "S-1-5-20",
        "S-1-5-80-123456789",
    ),
)
def test_cdp_windows_ancestor_owner_rejects_service_mutation_authority(
        owner_sid, monkeypatch):
    """Service identities do not gain broad profile authority by SID prefix."""
    import secureio

    monkeypatch.setattr(
        secureio,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-current",
    )
    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: owner_sid,
    )

    with pytest.raises(PermissionError, match="foreign owner"):
        cdp._require_windows_profile_owner(123, final=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows ownership behavior")
def test_cdp_windows_ancestor_allows_only_exact_trustedinstaller_sid(
        monkeypatch):
    """The actual system-root owner is exact, not a service-SID wildcard."""
    import secureio

    trusted_installer = (
        "S-1-5-80-956008885-3418522649-1831038044-"
        "1853292631-2271478464"
    )
    monkeypatch.setattr(
        secureio,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-current",
    )
    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: trusted_installer,
    )

    cdp._require_windows_profile_owner(123, final=False)


@pytest.mark.parametrize("owner_sid", ("S-1-5-18", "S-1-5-32-544"))
def test_cdp_windows_profile_root_narrowly_allows_system_owner(
        owner_sid, monkeypatch):
    """Only the User Data root may use the observed system/admin ownership."""
    import secureio

    monkeypatch.setattr(
        secureio,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-current",
    )
    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: owner_sid,
    )
    # Pin the token's default owner to a plain, non-admin SID. Without this the
    # negative half below silently depends on how the HOST runs pytest: on an
    # elevated Administrators token (a GitHub Actions Windows runner)
    # _windows_current_token_owner_sid() is itself S-1-5-32-544, the owner gate
    # accepts the owner_sid == "S-1-5-32-544" case on that branch, and the
    # pytest.raises below fails with "DID NOT RAISE". The claim this test makes
    # -- the root-only allowance is what admits a system owner, not the final
    # gate -- is about the allowance, so the token must be held constant.
    monkeypatch.setattr(
        secureio,
        "_windows_current_token_owner_sid",
        lambda: "S-1-5-21-current",
    )

    cdp._require_windows_profile_owner(
        123,
        final=True,
        allow_profile_root_system_owner=True,
    )
    with pytest.raises(PermissionError, match="not owned"):
        cdp._require_windows_profile_owner(
            123,
            final=True,
            allow_profile_root_system_owner=False,
        )


_BUILTIN_ADMINISTRATORS_SID = "S-1-5-32-544"


def _make_cdp_snapshot_source(tmp_path):
    """Build the exact positive snapshot fixture the CI-failing tests use.

    Same Local State / Default/Network/Cookies layout and the same
    _make_disposable_windows_source_private() re-owning step as
    test_cdp_snapshot_copies_key_and_cookie_db_owner_only, so a simulation
    built on it cannot pass or fail for a fixture-shaped reason.
    """
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    cookies = source / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    (source / "Local State").write_bytes(b"wrapped-key")
    cookies.write_bytes(b"sqlite-cookie-db")
    destination.mkdir()
    _make_disposable_windows_source_private(source)
    return source, destination


def _simulate_windows_token(monkeypatch, *, object_owner: str, token_owner: str):
    """Make the ownership guard see another host's token, changing nothing else.

    Only two SID seams move: the owner stamped on the objects on disk and the
    owner this token would stamp on objects it creates. The process's own user
    SID is deliberately left REAL, because that is what a GitHub Actions runner
    reports too -- an elevated token's TokenUser is still the runner account,
    only TokenOwner becomes BUILTIN\\Administrators. It also has to stay real for
    the simulation to be faithful: the fixture's on-disk DACL names the real
    current user, so faking the user SID makes _require_windows_profile_dacl
    reject the fixture with "profile source DACL grants mutation rights to a
    foreign SID" and the snapshot then fails for a reason that has nothing to do
    with the owner gate under test (observed, not assumed).
    """
    import secureio

    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: object_owner,
    )
    monkeypatch.setattr(
        secureio,
        "_windows_current_token_owner_sid",
        lambda: token_owner,
    )


def _forbid_profile_read(*_args, **_kwargs):
    raise AssertionError("a refused profile source was read anyway")


@pytest.mark.skipif(os.name != "nt", reason="Windows token ownership behavior")
def test_cdp_snapshot_control_ordinary_token_owner(tmp_path, monkeypatch):
    """CONTROL for the elevated-runner simulation below.

    Same harness, same fixture, ordinary (non-elevated) SID values: the object
    owner and the token's default owner are both the current user, which is what
    this host really reports. It must succeed -- if it does not, the harness
    itself is broken and the elevated case below proves nothing.
    """
    import secureio

    source, destination = _make_cdp_snapshot_source(tmp_path)
    plain_user = secureio._windows_current_user_sid()
    _simulate_windows_token(
        monkeypatch,
        object_owner=plain_user,
        token_owner=plain_user,
    )

    assert cdp._snapshot_profile(source, destination) is True
    assert (destination / "Local State").read_bytes() == b"wrapped-key"
    assert (
        destination / "Default" / "Network" / "Cookies"
    ).read_bytes() == b"sqlite-cookie-db"


@pytest.mark.skipif(os.name != "nt", reason="Windows token ownership behavior")
def test_cdp_snapshot_succeeds_under_simulated_elevated_admin_token(
        tmp_path, monkeypatch):
    """Simulates the elevated Administrators token a Windows CI runner uses.

    On such a runner every object this process creates is stamped
    BUILTIN\\Administrators (the token's default owner), not the plain user SID,
    so the final-component gate in _require_windows_profile_owner used to raise
    "profile source is not owned by current user" and _snapshot_profile returned
    False. That is what failed
    test_cdp_snapshot_copies_key_and_cookie_db_owner_only and
    test_cdp_snapshot_reads_opened_source_after_parent_path_swap on
    windows-x86_64-py314. GitHub Actions is unavailable, so the condition is
    reproduced here at the narrowest seam: the SID lookups themselves.
    """
    import secureio

    source, destination = _make_cdp_snapshot_source(tmp_path)
    plain_user = secureio._windows_current_user_sid()
    # A token's user SID is never the Administrators group, so the acceptance
    # exercised below really is the token-owner branch, not owner == user.
    assert plain_user != _BUILTIN_ADMINISTRATORS_SID
    _simulate_windows_token(
        monkeypatch,
        object_owner=_BUILTIN_ADMINISTRATORS_SID,
        token_owner=_BUILTIN_ADMINISTRATORS_SID,
    )

    assert cdp._snapshot_profile(source, destination) is True
    assert (destination / "Local State").read_bytes() == b"wrapped-key"
    assert (
        destination / "Default" / "Network" / "Cookies"
    ).read_bytes() == b"sqlite-cookie-db"


@pytest.mark.skipif(os.name != "nt", reason="Windows token ownership behavior")
def test_cdp_snapshot_survives_parent_path_swap_under_an_elevated_token(
        tmp_path, monkeypatch):
    """The OTHER CI-failing test's own scenario, replayed under elevation.

    test_cdp_snapshot_reads_opened_source_after_parent_path_swap is the second
    test that failed on windows-x86_64-py314. Its body is reproduced verbatim
    here with the elevated runner's SIDs installed, so the rename-race half of
    that scenario -- not just the plain copy -- is proven under the condition
    the fix addresses: the already-opened handles must still be read through
    the owner gate, and the attacker's replacement directory must not be
    copied in their place.
    """
    import secureio

    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    original_cookie = source / "Default" / "Network" / "Cookies"
    original_cookie.parent.mkdir(parents=True)
    original_cookie.write_bytes(b"original-cookie")
    destination.mkdir()
    _make_disposable_windows_source_private(source)
    plain_user = secureio._windows_current_user_sid()
    assert plain_user != _BUILTIN_ADMINISTRATORS_SID
    _simulate_windows_token(
        monkeypatch,
        object_owner=_BUILTIN_ADMINISTRATORS_SID,
        token_owner=_BUILTIN_ADMINISTRATORS_SID,
    )
    real_read = cdp._OpenedProfileFile.read
    swapped = [False]

    def swap_then_read(opened, deadline, max_bytes):
        if not swapped[0]:
            swapped[0] = True
            moved = tmp_path / "moved-source"
            try:
                source.rename(moved)
            except PermissionError:
                # Windows keeps every directory/leaf authority handle open with
                # delete sharing controlled by the filesystem. A denied rename
                # is itself safe: the mutable pathname could not be swapped.
                pass
            else:
                attacker = source / "Default" / "Network" / "Cookies"
                attacker.parent.mkdir(parents=True)
                attacker.write_bytes(b"attacker-cookie")
        return real_read(opened, deadline, max_bytes)

    monkeypatch.setattr(cdp._OpenedProfileFile, "read", swap_then_read)

    assert cdp._snapshot_profile(source, destination) is True
    assert swapped[0], "the swap seam never ran, so no race was exercised"
    assert (
        destination / "Default" / "Network" / "Cookies"
    ).read_bytes() == b"original-cookie"


@pytest.mark.skipif(os.name != "nt", reason="Windows token ownership behavior")
def test_cdp_snapshot_refuses_admin_owned_source_without_an_elevated_token(
        tmp_path, monkeypatch):
    """The allowance is the TOKEN's owner, never Administrators as a constant.

    Non-elevated token (default owner == the plain user) plus an
    Administrators-owned profile: that directory was created by somebody else's
    elevated process, so it must still be refused and nothing may be read.
    """
    import secureio

    source, destination = _make_cdp_snapshot_source(tmp_path)
    plain_user = secureio._windows_current_user_sid()
    monkeypatch.setattr(cdp._OpenedProfileFile, "read", _forbid_profile_read)
    _simulate_windows_token(
        monkeypatch,
        object_owner=_BUILTIN_ADMINISTRATORS_SID,
        token_owner=plain_user,
    )

    assert cdp._snapshot_profile(source, destination) is False
    assert not (destination / "Local State").exists()
    assert not (destination / "Default" / "Network" / "Cookies").exists()


@pytest.mark.parametrize(
    "foreign_owner",
    (
        "S-1-5-18",  # LocalSystem: reaches and must fail the final gate
        "S-1-5-19",  # LocalService
        "S-1-5-32-545",  # BUILTIN\Users
        "S-1-5-21-999-888-777-1234",  # another human account
    ),
)
@pytest.mark.skipif(os.name != "nt", reason="Windows token ownership behavior")
def test_cdp_snapshot_still_refuses_foreign_owner_under_elevated_token(
        foreign_owner, tmp_path, monkeypatch):
    """Elevation must not turn the owner gate into a pass-through.

    Token owner is Administrators (elevated runner) but the profile on disk
    belongs to a genuinely foreign principal: the snapshot must fail and no
    byte of that profile may be read.

    Scope, so this reads as one end-to-end proof rather than four: only
    S-1-5-18 reaches the FINAL-component gate the fix touches (the ancestor
    gate's separate system-owner allowance admits it, and the leaf gate then
    refuses it). The other three are refused earlier, at the ancestor gate
    ("profile ancestor has a foreign owner"), which the fix does not change.
    Exactness at the final gate for every foreign SID is proved directly by
    test_cdp_profile_owner_gate_accepts_only_the_exact_token_owner below.
    """
    source, destination = _make_cdp_snapshot_source(tmp_path)
    monkeypatch.setattr(cdp._OpenedProfileFile, "read", _forbid_profile_read)
    _simulate_windows_token(
        monkeypatch,
        object_owner=foreign_owner,
        token_owner=_BUILTIN_ADMINISTRATORS_SID,
    )

    assert cdp._snapshot_profile(source, destination) is False
    assert not (destination / "Local State").exists()
    assert not (destination / "Default" / "Network" / "Cookies").exists()


@pytest.mark.parametrize(
    "foreign_owner",
    (
        "S-1-5-18",
        "S-1-5-19",
        "S-1-5-20",
        "S-1-5-32-545",
        "S-1-5-80-123456789",
        "S-1-5-21-999-888-777-666",
    ),
)
def test_cdp_profile_owner_gate_accepts_only_the_exact_token_owner(
        foreign_owner, monkeypatch):
    """The elevated-token allowance is an exact match, not a family of SIDs."""
    import secureio

    monkeypatch.setattr(
        secureio,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-111-222-333-1001",
    )
    monkeypatch.setattr(
        secureio,
        "_windows_current_token_owner_sid",
        lambda: _BUILTIN_ADMINISTRATORS_SID,
    )
    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: _BUILTIN_ADMINISTRATORS_SID,
    )

    cdp._require_windows_profile_owner(123, final=True)

    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: foreign_owner,
    )
    with pytest.raises(PermissionError, match="not owned"):
        cdp._require_windows_profile_owner(123, final=True)


def test_cdp_profile_owner_gate_ignores_a_stale_token_owner(monkeypatch):
    """A non-elevated token never admits Administrators-owned profile data."""
    import secureio

    plain_user = "S-1-5-21-111-222-333-1001"
    monkeypatch.setattr(
        secureio, "_windows_current_user_sid", lambda: plain_user)
    monkeypatch.setattr(
        secureio, "_windows_current_token_owner_sid", lambda: plain_user)
    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: _BUILTIN_ADMINISTRATORS_SID,
    )

    with pytest.raises(PermissionError, match="not owned"):
        cdp._require_windows_profile_owner(123, final=True)
    # The ancestor gate keeps its own, separate system-owner allowance.
    cdp._require_windows_profile_owner(123, final=False)
    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: "S-1-5-21-999-888-777-666",
    )
    with pytest.raises(PermissionError, match="foreign owner"):
        cdp._require_windows_profile_owner(123, final=False)


def test_cdp_threads_browser_name_into_profile_snapshot(
        tmp_path, monkeypatch):
    """The selected executable product is the profile capability authority."""
    source = tmp_path / "source"
    source.mkdir()
    userdir = tmp_path / "private-temp"
    userdir.mkdir()
    browser = tmp_path / "brave.exe"
    browser.write_bytes(b"fake")
    seen = []

    monkeypatch.setattr(cdp, "_find_exe", lambda _name: str(browser))
    monkeypatch.setattr(cdp, "_default_user_data_dir", lambda _name: source)
    monkeypatch.setattr(cdp, "_make_private_tempdir", lambda: userdir)

    def snapshot(*_args, **kwargs):
        seen.append(kwargs.get("browser_name"))
        return False

    monkeypatch.setattr(cdp, "_snapshot_profile", snapshot)
    monkeypatch.setattr(
        cdp,
        "_cleanup_private_tempdir",
        lambda path, **_kwargs: shutil.rmtree(path) is None,
    )

    assert cdp.get_all_cookies_via_cdp(
        "brave",
        timeout=5,
    ) is None
    assert seen == ["brave"]


#: NTSTATUS returned when another process holds the file with a sharing mode
#: that excludes ours. Raised by `_cdp_client._nt_open_relative`.
_STATUS_SHARING_VIOLATION = 0xC0000043


@pytest.mark.skipif(os.name != "nt", reason="installed Windows browser metadata")
@pytest.mark.parametrize("browser_name", ("chrome", "edge", "brave"))
def test_cdp_windows_installed_network_cookie_metadata_is_accepted(
        browser_name):
    """Installed stable profile ACLs validate without reading cookie contents."""
    if cdp._find_exe(browser_name) is None:
        pytest.skip(f"{browser_name} is not installed")
    root_path = cdp._default_user_data_dir(browser_name)
    if root_path is None:
        pytest.skip(f"{browser_name} has no profile root")
    selected = None
    for profile_name in (
            "Default",
            *(f"Profile {index}" for index in range(1, 51))):
        candidate = root_path / profile_name / "Network" / "Cookies"
        if candidate.is_file():
            selected = (profile_name, "Network", "Cookies")
            break
    if selected is None:
        pytest.skip(f"{browser_name} has no Network/Cookies metadata")

    deadline = time.monotonic() + 10
    root = cdp._open_profile_root(
        root_path,
        deadline,
        browser_name=browser_name,
    )
    opened = None
    try:
        try:
            opened = root.open_file(selected, deadline)
        except OSError as error:
            # STATUS_SHARING_VIOLATION. The browser is running right now and
            # holds its cookie database open, so the OS refuses the handle
            # before any ACL is consulted. That is an environmental condition
            # exactly like the three skips above -- and unlike them, it is the
            # NORMAL state on a developer's machine, which made `pytest` fail
            # for anyone who simply had Chrome open. It is not an ACL failure,
            # and the product already degrades gracefully here: the CDP tier
            # catches it (cookie_providers.py) and the engine tells the user to
            # close their browser or export cookies.txt (yt_final.py).
            if f"0x{_STATUS_SHARING_VIOLATION:08x}" not in str(error).lower():
                raise
            pytest.skip(
                f"{browser_name} is running and holds its cookie database open"
            )
        assert opened is not None
        assert opened.size >= 0
    finally:
        if opened is not None:
            opened.close()
        root.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_cdp_retained_launch_authority_survives_gc_and_retries(
        monkeypatch):
    """Failed cleanup cannot garbage-collect live raw process authority."""
    import ctypes

    owners = set()
    state = {"alive": True, "close": False}
    closed = []

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernel:
        def __init__(self):
            self.TerminateJobObject = FakeFunction(lambda *_args: 1)
            self.TerminateProcess = FakeFunction(lambda *_args: 1)
            self.WaitForSingleObject = FakeFunction(lambda *_args: 258)

            def get_exit_code(_handle, output):
                output._obj.value = 259 if state["alive"] else 1
                return 1

            self.GetExitCodeProcess = FakeFunction(get_exit_code)

            def close_handle(handle):
                if not state["close"]:
                    return 0
                closed.append(int(getattr(handle, "value", handle)))
                return 1

            self.CloseHandle = FakeFunction(close_handle)

    monkeypatch.setattr(
        cdp,
        "_WINDOWS_CLEANUP_OWNERS",
        owners,
        raising=False,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: FakeKernel())
    retained = cdp._WindowsLaunchCleanupError(202, 303, 404, 101)
    reference = weakref.ref(retained)

    assert retained.cleanup(deadline=time.monotonic() + 5) is False
    del retained
    gc.collect()

    assert reference() is not None
    assert len(owners) == 1
    state["alive"] = False
    state["close"] = True
    assert cdp._drain_windows_cleanup_registry(
        deadline=time.monotonic() + 5,
    ) is True
    assert sorted(closed) == [101, 202, 303]
    assert owners == set()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")
def test_cdp_successful_launch_retains_failed_thread_close_for_retry(
        monkeypatch):
    """A resumed browser's raw thread handle is durably owned if close fails."""
    import ctypes

    owners = set()
    close_attempts = []
    kernel = _FailingLaunchKernel(
        terminate_result=1,
        wait_result=0,
        exit_code=1,
        assign_result=1,
        resume_result=0,
    )

    def close_handle(handle):
        value = int(getattr(handle, "value", handle))
        close_attempts.append(value)
        if value == 303 and close_attempts.count(303) == 1:
            return 0
        kernel.closed.append(value)
        return 1

    kernel.CloseHandle = _RecordingFunction(
        "close_handle",
        close_handle,
        kernel.calls,
    )
    monkeypatch.setattr(
        cdp,
        "_WINDOWS_CLEANUP_OWNERS",
        owners,
        raising=False,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel)

    proc = cdp._WindowsJobProcess.launch(
        [sys.executable, "-c", "pass"],
        deadline=time.monotonic() + 5,
    )
    try:
        assert any(
            getattr(owner, "handles", ()) == (303,)
            for owner in owners
        )
        assert cdp._drain_windows_cleanup_registry(
            deadline=time.monotonic() + 5,
        ) is True
        assert 303 in kernel.closed
        assert owners == set()
    finally:
        proc.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_cdp_profile_handle_close_failure_is_durably_retried(
        monkeypatch):
    """Profile traversal cannot drop a raw handle after CloseHandle failure."""
    import ctypes

    owners = set()
    attempts = []

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernel:
        def __init__(self):
            def close_handle(handle):
                value = int(getattr(handle, "value", handle))
                attempts.append(value)
                return 0 if len(attempts) == 1 else 1

            self.CloseHandle = FakeFunction(close_handle)

    monkeypatch.setattr(
        cdp,
        "_WINDOWS_CLEANUP_OWNERS",
        owners,
        raising=False,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: FakeKernel())
    opened = cdp._OpenedProfileFile(707, 0, windows=True)

    opened.close()
    del opened
    gc.collect()

    assert any(
        getattr(owner, "handles", ()) == (707,)
        for owner in owners
    )
    assert cdp._drain_windows_cleanup_registry(
        deadline=time.monotonic() + 5,
    ) is True
    assert attempts == [707, 707]
    assert owners == set()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_cdp_retained_handle_cleanup_is_thread_safe(monkeypatch):
    """Concurrent drains close one retained kernel handle at most once."""
    import ctypes

    owners = set()
    attempts = []
    attempts_lock = threading.Lock()
    ready = threading.Barrier(8)

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernel:
        def __init__(self):
            def close_handle(handle):
                value = int(getattr(handle, "value", handle))
                with attempts_lock:
                    attempts.append(value)
                time.sleep(0.02)
                return 1

            self.CloseHandle = FakeFunction(close_handle)

    monkeypatch.setattr(cdp, "_WINDOWS_CLEANUP_OWNERS", owners)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: FakeKernel())
    owner = cdp._WindowsHandleCleanup(808)
    results = []

    def cleanup():
        ready.wait()
        results.append(owner.cleanup(deadline=time.monotonic() + 5))

    threads = [threading.Thread(target=cleanup) for _index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert attempts == [808]
    assert results == [True] * 8
    assert owners == set()


@pytest.mark.skipif(os.name != "nt", reason="Windows cleanup registry")
def test_cdp_next_operation_drains_retained_windows_authority(
        tmp_path, monkeypatch):
    """Every subsequent bounded CDP attempt first retries retained handles."""
    import ctypes

    owners = set()
    closed = []

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernel:
        def __init__(self):
            self.TerminateJobObject = FakeFunction(lambda *_args: 1)
            self.TerminateProcess = FakeFunction(lambda *_args: 1)
            self.WaitForSingleObject = FakeFunction(lambda *_args: 0)

            def get_exit_code(_handle, output):
                output._obj.value = 1
                return 1

            self.GetExitCodeProcess = FakeFunction(get_exit_code)
            self.CloseHandle = FakeFunction(
                lambda handle:
                    closed.append(int(getattr(handle, "value", handle))) or 1
            )

    monkeypatch.setattr(
        cdp,
        "_WINDOWS_CLEANUP_OWNERS",
        owners,
        raising=False,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: FakeKernel())
    cdp._WindowsLaunchCleanupError(202, 303, 404, 101)
    searches = []
    monkeypatch.setattr(
        cdp,
        "_find_exe",
        lambda name: searches.append(name) or None,
    )

    assert cdp.get_all_cookies_via_cdp("chrome", timeout=5) is None
    assert sorted(closed) == [101, 202, 303]
    assert owners == set()
    assert searches == ["chrome"]


def test_profile_snapshot_failure_reason_reaches_the_caller(tmp_path, monkeypatch):
    """A profile that cannot be READ must not be reported as "you did not sign in".

    The layers collapse the cause: _snapshot_profile catches OSError and returns False,
    get_all_cookies_via_cdp turns that into None without raising, and _cdp_export_profile returns
    None. The web UI then told the user to "sign into YouTube fully and try Login again" -- advice
    that cannot help, addressed to someone who had already done exactly that.

    Observed for real: a profile directory whose DACL granted Modify to a stale SID was refused by
    _open_profile_root with PermissionError, and the user was told to sign in again.
    """
    import _cdp_client
    import cookie_providers as cp

    def _refuse(*_args, **_kwargs):
        raise PermissionError("profile source DACL grants mutation rights to a foreign SID")

    dst = tmp_path / "dst"
    dst.mkdir()
    monkeypatch.setattr(_cdp_client, "_open_profile_root", _refuse)
    ok = _cdp_client._snapshot_profile(
        tmp_path, dst, browser_name="chrome", deadline=time.monotonic() + 5)
    assert ok is False
    reason = _cdp_client.last_snapshot_failure()
    assert reason and "foreign SID" in reason, reason


def test_snapshot_failure_reason_is_cleared_between_attempts(tmp_path, monkeypatch):
    """A stale reason must never be reported against a later, different attempt."""
    import _cdp_client

    monkeypatch.setattr(
        _cdp_client, "_open_profile_root",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("first failure")))
    d1 = tmp_path / "d1"
    d1.mkdir()
    _cdp_client._snapshot_profile(tmp_path, d1, browser_name="chrome",
                                  deadline=time.monotonic() + 5)
    assert "first failure" in (_cdp_client.last_snapshot_failure() or "")

    # A later attempt that gets further must not still carry the old reason.
    _cdp_client._record_snapshot_failure(None)
    assert _cdp_client.last_snapshot_failure() is None


class _FakeYoutubeDL:
    """Records the opts and extract_info kwargs the auth probe actually used."""

    calls: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, **kwargs):
        type(self).calls.append({"opts": self.opts, "url": url, "kwargs": kwargs})
        return {"id": "HtVdAasjOgU"}


def test_auth_probe_does_not_wait_on_a_po_token_provider(monkeypatch):
    """The cookie probe must not pay for a PO token it does not need.

    socket_timeout bounds HTTP, but the PO-token provider is a separate process, so it
    bounds nothing there. With a provider installed and failing, the default multi-client
    extraction retried it once per client: measured 111.6s to reach the same verdict a
    client-pinned probe reached in 1.8s. "Test cookies" in the web UI looked hung, and the
    same stall sat between the cookie export and the activation it gates.

    The probe asks exactly one question -- does this cookie file unlock an age-gated video
    -- and the web client answers it without a PO token. It also stops before format
    selection: "Requested format is not available" is a format verdict, not an auth one,
    and treating it as auth failure reports a perfectly valid session as signed out.
    """
    import types

    import cookies_auth

    _FakeYoutubeDL.calls = []
    monkeypatch.setitem(
        __import__("sys").modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_FakeYoutubeDL))

    mgr = cookies_auth.build_default_manager()
    mgr.config["probe_video_id"] = "HtVdAasjOgU"
    assert mgr._default_validator("cookies.txt") is True

    assert len(_FakeYoutubeDL.calls) == 1
    call = _FakeYoutubeDL.calls[0]
    youtube = (call["opts"].get("extractor_args") or {}).get("youtube") or {}

    assert list(youtube.get("fetch_pot") or []) == ["never"], (
        "the probe must decline PO-token fetching; a failing provider cost 110s per call")
    assert list(youtube.get("player_client") or []) == ["web"], (
        "the probe must pin a client that answers the auth question without a PO token")
    assert call["kwargs"].get("process") is False, (
        "the probe must stop before format selection, which fails independently of auth")


class _RaisingYoutubeDL:
    """Raises a chosen extractor error so the validator's classifier can be exercised."""

    message = ""

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, **kwargs):
        raise RuntimeError(type(self).message)


def _validator_verdict(monkeypatch, message):
    import sys as _sys
    import types

    import cookies_auth

    _RaisingYoutubeDL.message = message
    monkeypatch.setitem(_sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_RaisingYoutubeDL))
    mgr = cookies_auth.build_default_manager()
    mgr.config["probe_video_id"] = "HtVdAasjOgU"
    return mgr._default_validator("cookies.txt")


def test_a_bot_wall_is_not_reported_as_a_signed_out_session(monkeypatch):
    """"Sign in to confirm you're not a bot" is a rate/abuse wall, not a logged-out session.

    It matches BOTH "sign in" and "bot" in the classifier's keyword list, so before this it
    returned False -- telling a user with a perfectly valid cookie file that their session
    had failed, and steering them to re-login, which is the one action that cannot clear a
    bot wall. Pinning a single player client to avoid the PO-token stall makes meeting this
    wall MORE likely, so the classifier has to rule it out before the keyword sweep.
    """
    assert _validator_verdict(
        monkeypatch, "ERROR: [youtube] X: Sign in to confirm you're not a bot") is True, (
        "a bot wall was misreported as a signed-out session")


def test_a_genuine_age_gate_still_reports_signed_out(monkeypatch):
    """Control: the real signed-out signal must still be caught.

    Without cookies the age-gated probe answers "Sign in to confirm your age" -- that one IS
    proof the session is not signed in, and must stay a hard False. If the bot-wall carve-out
    were written loosely enough to swallow this too, the validator would report every broken
    cookie file as valid.
    """
    assert _validator_verdict(
        monkeypatch, "ERROR: [youtube] X: Sign in to confirm your age") is False, (
        "a genuinely signed-out session was reported as valid")


def test_an_unrelated_failure_stays_inconclusive(monkeypatch):
    """Network/probe-gone errors must not nuke a possibly-good session."""
    assert _validator_verdict(monkeypatch, "ERROR: unable to download webpage: timed out") is True
