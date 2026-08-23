"""Owner-only operational data and deterministic log-redaction tests."""
from __future__ import annotations

import ctypes
import json
import logging
import multiprocessing
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

try:
    from . import jobstore
    from .jobspec import JobSpec
    from .redaction import SensitiveDataFilter, redact_sensitive_text
except ImportError:
    import jobstore
    from jobspec import JobSpec
    from redaction import SensitiveDataFilter, redact_sensitive_text


class _StreamSwapProbe(logging.StreamHandler):
    """Real StreamHandler with observable direct-assignment failures for guard tests."""

    def __init__(
            self,
            label,
            stream,
            events,
            *,
            fail_on_redirect=False,
            fail_on_restore=False):
        self._probe_label = label
        self._probe_original_stream = stream
        self._probe_events = events
        self._probe_fail_on_redirect = fail_on_redirect
        self._probe_fail_on_restore = fail_on_restore
        self._probe_tracking = False
        super().__init__(stream)
        self._probe_tracking = True

    def __setattr__(self, name, value):
        if name == "stream" and getattr(self, "_probe_tracking", False):
            restoring = value is self._probe_original_stream
            action = "restore" if restoring else "redirect"
            self._probe_events.append((self._probe_label, action))
            if restoring and self._probe_fail_on_restore:
                raise RuntimeError(f"{self._probe_label} restore failed")
            if not restoring and self._probe_fail_on_redirect:
                raise RuntimeError(f"{self._probe_label} redirect failed")
        super().__setattr__(name, value)

    def reset_for_cleanup(self):
        self._probe_tracking = False
        self._probe_fail_on_redirect = False
        self._probe_fail_on_restore = False
        self.stream = self._probe_original_stream


def _history_process_writer(path: str, worker: int, rows: int) -> None:
    ledger = jobstore.HistoryLedger(path)
    for row in range(rows):
        ledger.record(
            url=f"https://youtu.be/process-{worker}-{row}",
            output=f"process-{worker}-{row}.mp4",
            profile="copy",
            status="done",
            spec={"urls": [f"https://youtu.be/process-{worker}-{row}"]},
        )


def _windows_sddl(path: Path) -> str:
    if os.name != "nt":
        pytest.skip("Windows security descriptors")
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    security = 0x1 | 0x2 | 0x4  # OWNER | GROUP | DACL
    get_named = advapi32.GetNamedSecurityInfoW
    get_named.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_named.restype = wintypes.DWORD
    result = get_named(
        str(path),
        1,  # SE_FILE_OBJECT
        security,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    assert result == 0, ctypes.FormatError(result)
    text = ctypes.c_void_p()
    length = wintypes.DWORD()
    convert = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    try:
        assert convert(
            descriptor,
            1,
            security,
            ctypes.byref(text),
            ctypes.byref(length),
        ), ctypes.WinError(ctypes.get_last_error())
        return ctypes.wstring_at(text)
    finally:
        if text.value:
            kernel32.LocalFree(text)
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def _windows_sddl_sid_token(sid: str) -> str:
    """Return the exact text SDDL renders a SID as, which for well-known SIDs is a 2-letter alias.

    ConvertSecurityDescriptorToStringSecurityDescriptorW substitutes aliases in ACE trustee fields
    just as it does in O:/G:, so a raw "S-..." SID cannot be compared against a rendered descriptor
    directly. Round-tripping one ACE through both converters yields the rendering that descriptor
    will use, without hard-coding an alias table that could drift from the OS.
    """
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    to_binary = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    to_binary.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    to_binary.restype = wintypes.BOOL
    assert to_binary(
        f"D:(A;;FA;;;{sid})", 1, ctypes.byref(descriptor), None
    ), ctypes.WinError(ctypes.get_last_error())
    text = ctypes.c_void_p()
    length = wintypes.DWORD()
    to_text = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    to_text.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    to_text.restype = wintypes.BOOL
    try:
        assert to_text(
            descriptor, 1, 0x4, ctypes.byref(text), ctypes.byref(length)
        ), ctypes.WinError(ctypes.get_last_error())
        rendered = ctypes.wstring_at(text)
    finally:
        if text.value:
            kernel32.LocalFree(text)
        kernel32.LocalFree(descriptor)
    return rendered.split("D:", 1)[1].strip("()").split(";")[5]


def _assert_windows_owner_only_acl(path: Path) -> None:
    """Assert this process's own identity is the sole principal the protected DACL grants."""
    try:
        from . import secureio
    except ImportError:
        import secureio

    sddl = _windows_sddl(path)
    dacl = sddl.split("D:", 1)[1].split("S:", 1)[0]
    aces = re.findall(r"\([^)]*\)", dacl)
    assert dacl.startswith("P"), sddl        # protected: the parent cannot inject access
    assert len(aces) == 1, sddl              # exactly one trustee
    assert ";ID;" not in aces[0], sddl       # that trustee is not an inherited ACE
    assert aces[0].startswith("(A;"), sddl   # ...and it is an allow ACE
    # Compare the ACE's trustee FIELD (component 5 of the ";"-separated ACE) against the current
    # user, instead of substring-testing the owner text against the whole ACE. That is stronger on
    # both counts: a substring test would also accept a two-letter alias that merely happens to
    # occur inside the rights field, and "the owner is the sole grantee" would still pass a file
    # owned by -- and granted solely to -- some OTHER principal. What a credential file has to
    # prove is that the one account that can open it is us.
    trustee = aces[0].strip("()").split(";")[5]
    assert trustee == _windows_sddl_sid_token(
        secureio._windows_current_user_sid()
    ), sddl
    # The owner must be this process's own identity too. Accept the token's default owner as well
    # as the user SID -- the same rule secureio._require_windows_current_owner applies -- because
    # under an elevated Administrators token Windows stamps BUILTIN\Administrators on objects
    # created outside our creation-time descriptor. Any other owner is a foreign principal.
    owner_match = re.search(r"O:(.+?)G:", sddl)
    assert owner_match, sddl
    assert owner_match.group(1) in {
        _windows_sddl_sid_token(secureio._windows_current_user_sid()),
        _windows_sddl_sid_token(secureio._windows_current_token_owner_sid()),
    }, sddl


def _assert_windows_handle_closed(handle: int) -> None:
    if os.name != "nt":
        pytest.skip("Windows handle validation")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_handle_information = kernel32.GetHandleInformation
    get_handle_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_handle_information.restype = wintypes.BOOL
    flags = wintypes.DWORD()
    ctypes.set_last_error(0)
    assert not get_handle_information(
        wintypes.HANDLE(handle), ctypes.byref(flags)
    ), f"Windows handle {handle} remained valid after owner rejection"
    error = ctypes.get_last_error()
    assert error == 6, (
        f"GetHandleInformation({handle}) failed with {error}, "
        "expected ERROR_INVALID_HANDLE (6)"
    )


def test_redaction_removes_url_and_proxy_credentials():
    raw = (
        "notify=https://hooks.example.test/secret/path?token=abc "
        "proxy=http://user:pass@proxy.example.test:8080"
    )
    clean = redact_sensitive_text(raw)
    assert "secret/path" not in clean
    assert "token=abc" not in clean
    assert "user:pass" not in clean
    assert "hooks.example.test" in clean
    assert "proxy.example.test" in clean


def test_redaction_removes_labeled_private_values_and_preserves_context():
    raw = (
        "job=j_123 status=done "
        "cookiefile=C:\\Users\\Alice\\private\\cookies.txt "
        "on-complete='C:\\Tools\\publish.exe --token swordfish' "
        "output=/Users/alice/Private Downloads/customer/video.mp4 retries=2"
    )
    clean = redact_sensitive_text(raw)
    assert "j_123" in clean
    assert "status=done" in clean
    assert "retries=2" in clean
    assert "Alice" not in clean
    assert "swordfish" not in clean
    assert "customer" not in clean
    assert clean.count("<redacted") >= 3


def test_redaction_leaves_nonsecret_operational_context_unchanged():
    raw = "[Queue] j_123 failed: retry count 2; encoder unavailable"
    assert redact_sensitive_text(raw) == raw


def test_redaction_preserves_punctuation_around_sensitive_values():
    clean_path = redact_sensitive_text(
        "engine ready (output: /Users/alice/private downloads)"
    )
    clean_url = redact_sensitive_text(
        "download https://youtu.be/private?token=abc: failed"
    )
    assert clean_path == "engine ready (output: <redacted-path>)"
    assert clean_url == "download https://youtu.be/<redacted>: failed"


def test_redaction_handles_parenthesized_path_and_bracketed_ipv6_url():
    raw = (
        "notify=https://hooks.example.test/tenant(secret)/hook?token=abc "
        "proxy=http://user:pass@[2001:db8::1]:8080/private?key=xyz"
    )
    clean = redact_sensitive_text(raw)
    assert clean == (
        "notify=https://hooks.example.test/<redacted> "
        "proxy=http://[2001:db8::1]:8080/<redacted>"
    )
    assert "tenant(secret)" not in clean
    assert "user:pass" not in clean
    assert "key=xyz" not in clean


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "proxy=http://us'er:pa'ss@example.test/private/O'Neil?token=abc, "
            "status=ready",
            "proxy=http://example.test/<redacted>, status=ready",
        ),
        (
            "notify='https://hooks.example.test/tenant/O'Neil?token=abc' "
            "status=ready",
            "notify='https://hooks.example.test/<redacted>' status=ready",
        ),
    ],
)
def test_redaction_keeps_apostrophes_inside_urls_until_the_real_boundary(
        raw, expected):
    clean = redact_sensitive_text(raw)
    assert clean == expected
    assert "us'er" not in clean
    assert "O'Neil" not in clean
    assert "token=abc" not in clean
    assert "status=ready" in clean


def test_redaction_scans_past_apostrophe_in_single_quoted_windows_path():
    raw = (
        r"[Run] cannot use output_dir 'C:\Users\O'Neil\Private Output' "
        r"(denied) status=blocked"
    )
    clean = redact_sensitive_text(raw)
    assert clean == (
        "[Run] cannot use output_dir <redacted-path> "
        "(denied) status=blocked"
    )
    assert "O'Neil" not in clean
    assert "Private Output" not in clean


@pytest.mark.parametrize(
    ("raw", "secret", "context"),
    [
        (
            r"[Cookies] using cookie file: C:\Users\Alice\My Cookies\cookies.txt",
            r"C:\Users\Alice",
            "[Cookies] using cookie file: <redacted-path>",
        ),
        (
            r"[Cookies] auto-discovered cookies.txt at C:\Users\Alice\cookies.txt",
            r"C:\Users\Alice",
            "[Cookies] auto-discovered cookies.txt at <redacted-path>",
        ),
        (
            r"[Run] cannot use output_dir 'C:\Users\Alice\Private Output' (denied)",
            r"C:\Users\Alice",
            "[Run] cannot use output_dir <redacted-path> (denied)",
        ),
        (
            r"Output #0, mp4, to 'C:\Users\Alice\Private Output\video.mp4':",
            r"C:\Users\Alice",
            "Output #0, mp4, to <redacted-path>:",
        ),
        (
            r"output=\\server\private share\customer\video.mp4 status=done",
            r"\\server\private",
            "output=<redacted-path> status=done",
        ),
        (
            r"output=\\?\C:\Users\Alice\private\video.mp4 status=done",
            r"\\?\C:\Users\Alice",
            "output=<redacted-path> status=done",
        ),
        (
            r"cookiefile=\\?\UNC\server\private share\cookies.txt status=ready",
            r"\\?\UNC\server",
            "cookiefile=<redacted-path> status=ready",
        ),
    ],
)
def test_redaction_covers_product_path_log_formats(raw, secret, context):
    clean = redact_sensitive_text(raw)
    assert secret not in clean
    assert clean == context


def test_sensitive_filter_redacts_formatted_log_arguments():
    record = logging.LogRecord(
        "yt_final",
        logging.INFO,
        __file__,
        1,
        "notify=%s status=%s",
        ("https://hooks.example.test/private/token?key=abc", "queued"),
        None,
    )
    assert SensitiveDataFilter().filter(record) is True
    clean = record.getMessage()
    assert "hooks.example.test" in clean
    assert "private/token" not in clean
    assert "key=abc" not in clean
    assert "status=queued" in clean


def test_sensitive_filter_redacts_exception_diagnostics():
    try:
        raise RuntimeError(
            "webhook https://hooks.example.test/private/token?key=abc failed"
        )
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "yt_final",
        logging.ERROR,
        __file__,
        1,
        "notify failed status=%s",
        ("retrying",),
        exc_info,
    )

    assert SensitiveDataFilter().filter(record) is True
    rendered = logging.Formatter("%(message)s").format(record)
    assert "hooks.example.test" in rendered
    assert "private/token" not in rendered
    assert "key=abc" not in rendered
    assert "status=retrying" in rendered


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows ACLs are authoritative")
def test_history_is_owner_only_from_first_byte(tmp_path, monkeypatch):
    """Neutral umask + disabled trailing chmod proves os.open itself uses 0600."""
    try:
        from . import secureio
    except ImportError:
        import secureio

    monkeypatch.setattr(secureio, "ensure_private_file", lambda *_args, **_kwargs: None)
    old_umask = os.umask(0)
    try:
        path = tmp_path / "history.jsonl"
        jobstore.HistoryLedger(path).record(
            url="https://youtu.be/abc",
            output="/private/out.mp4",
            profile="copy",
            status="done",
            spec={"urls": ["https://youtu.be/abc"]},
        )
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor semantics")
def test_private_tightening_uses_open_descriptor_not_path(tmp_path, monkeypatch):
    try:
        from . import secureio
    except ImportError:
        import secureio

    path = tmp_path / "private.txt"
    path.write_bytes(b"private")
    os.chmod(path, 0o666)

    def path_chmod_is_a_bug(*_args, **_kwargs):
        raise AssertionError("path-based chmod reintroduces a TOCTOU window")

    monkeypatch.setattr(secureio.os, "chmod", path_chmod_is_a_bug)
    secureio.ensure_private_file(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_windows_private_append_is_owner_only_at_creation(tmp_path, monkeypatch):
    try:
        from . import secureio
    except ImportError:
        import secureio

    monkeypatch.setattr(
        secureio,
        "_tighten_windows_handle",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    path = tmp_path / "history.jsonl"
    secureio.append_private_line(path, b'{"private": true}\n')

    _assert_windows_owner_only_acl(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_windows_history_and_log_have_owner_only_acls(tmp_path):
    history = tmp_path / "history.jsonl"
    jobstore.HistoryLedger(history).record(
        url="https://youtu.be/acl",
        output="video.mp4",
        profile="copy",
        status="done",
        spec={"urls": ["https://youtu.be/acl"]},
    )

    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # The log no longer follows the working directory -- it lives under the app data dir, because
    # opening it relative to the CWD at import time crashed the program outright when that
    # directory was not writable. Pin it into tmp_path with the documented override so these
    # assertions still inspect the real file this run wrote.
    env["YT_FINAL_LOG"] = str(tmp_path / "final_yt.log")
    result = subprocess.run(
        [sys.executable, "-c", "from Final import yt_final; yt_final.logger.info('ready')"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    _assert_windows_owner_only_acl(history)
    _assert_windows_owner_only_acl(tmp_path / "final_yt.log")


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_windows_existing_file_is_tightened_by_handle(tmp_path):
    try:
        from . import secureio
    except ImportError:
        import secureio

    path = tmp_path / "existing-private.txt"
    path.write_bytes(b"private")
    secureio.ensure_private_file(path)
    _assert_windows_owner_only_acl(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_windows_tightens_a_file_whose_dacl_grants_only_modify(tmp_path):
    """An owner implicitly holds READ_CONTROL and WRITE_DAC, but never WRITE_OWNER.

    Adding WRITE_OWNER to the open mask therefore makes CreateFileW fail outright with
    ERROR_ACCESS_DENIED on an ordinary file we own whose DACL grants only "Modify" -- the shape a
    file inherits from many real-world parent ACLs -- which would break ensure_private_file() and
    the private append-log path on files that used to open fine. Tightening must still succeed.
    """
    try:
        from . import secureio
    except ImportError:
        import secureio
    from ctypes import wintypes

    path = tmp_path / "modify-only.txt"
    path.write_bytes(b"private")

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    set_security = advapi32.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    # 0x1301BF == FILE_GENERIC_READ|FILE_GENERIC_WRITE|FILE_GENERIC_EXECUTE|DELETE ("Modify"):
    # nobody, not even the current user, is granted WRITE_DAC or WRITE_OWNER by this DACL.
    sddl = f"D:P(A;;0x1301BF;;;{secureio._windows_current_user_sid()})"
    assert convert(sddl, 1, ctypes.byref(descriptor), None), ctypes.WinError(
        ctypes.get_last_error()
    )
    try:
        assert set_security(str(path), 0x4, descriptor), ctypes.WinError(
            ctypes.get_last_error()
        )
    finally:
        kernel32.LocalFree(descriptor)

    # Precondition: this host must implicitly grant the owner WRITE_DAC on the file, otherwise
    # nothing is tightenable here and the regression this guards cannot be observed.
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
    probe = create_file(
        str(path),
        0x00020000 | 0x00040000,  # READ_CONTROL | WRITE_DAC
        0x1 | 0x2 | 0x4,
        None,
        3,  # OPEN_EXISTING
        0x80,
        None,
    )
    if probe == ctypes.c_void_p(-1).value:
        pytest.skip("host denies the owner implicit WRITE_DAC on a Modify-only DACL")
    kernel32.CloseHandle(wintypes.HANDLE(probe))

    secureio.ensure_private_file(path)
    _assert_windows_owner_only_acl(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows owner validation")
def test_windows_handle_owner_helper_reads_live_current_owner(tmp_path):
    try:
        from . import secureio
    except ImportError:
        import secureio
    import msvcrt

    path = tmp_path / "current-owner.txt"
    secureio.append_private_line(path, b"private\n")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        assert secureio._windows_handle_owner_sid(
            msvcrt.get_osfhandle(fd)
        ) == secureio._windows_current_user_sid()
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name != "nt", reason="Windows owner validation")
def test_windows_owner_mismatch_fails_before_tightening_or_append(
        tmp_path, monkeypatch):
    try:
        from . import secureio
    except ImportError:
        import secureio

    path = tmp_path / "foreign-owner-history.jsonl"
    original = b'{"existing": true}\n'
    path.write_bytes(original)
    original_sddl = _windows_sddl(path)
    opened_handles = []

    def foreign_owner(handle):
        opened_handles.append(int(handle))
        return "S-1-5-18"

    monkeypatch.setattr(
        secureio, "_windows_handle_owner_sid", foreign_owner, raising=False
    )

    def tightening_after_mismatch_is_a_bug(*_args, **_kwargs):
        raise AssertionError("foreign-owned file was tightened before rejection")

    monkeypatch.setattr(
        secureio, "_tighten_windows_handle", tightening_after_mismatch_is_a_bug
    )
    for attempt in range(32):
        with pytest.raises(PermissionError) as caught:
            secureio.append_private_line(path, b'{"new": true}\n')
        assert len(opened_handles) == attempt + 1
        # Check the just-captured value before another open can legitimately reuse it.
        _assert_windows_handle_closed(opened_handles[-1])
        assert str(caught.value) == (
            f"refusing to access private file not owned by current user: {path}"
        )

    assert path.read_bytes() == original
    assert _windows_sddl(path) == original_sddl


@pytest.mark.skipif(os.name != "nt", reason="Windows owner validation")
def test_windows_borrowed_descriptor_rejects_foreign_owner_before_tightening(
        tmp_path, monkeypatch):
    try:
        from . import secureio
    except ImportError:
        import secureio

    path = tmp_path / "borrowed-foreign-owner.txt"
    original = b"private\n"
    path.write_bytes(original)
    original_sddl = _windows_sddl(path)
    monkeypatch.setattr(
        secureio,
        "_windows_handle_owner_sid",
        lambda _handle: "S-1-5-18",
    )

    def tightening_after_mismatch_is_a_bug(*_args, **_kwargs):
        raise AssertionError("borrowed foreign-owned descriptor was tightened")

    monkeypatch.setattr(
        secureio, "_tighten_windows_handle", tightening_after_mismatch_is_a_bug
    )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        with pytest.raises(
                PermissionError,
                match="^refusing to access private file not owned by current user$"):
            secureio.ensure_private_fd(fd)
        os.fstat(fd)
    finally:
        os.close(fd)

    assert path.read_bytes() == original
    assert _windows_sddl(path) == original_sddl


@pytest.mark.skipif(os.name != "nt", reason="Windows owner validation")
def test_windows_accepts_files_owned_by_the_token_owner_but_not_a_foreign_user(
        monkeypatch):
    """Under an elevated Administrators token the OS makes new files Administrators-owned, so a file
    THIS process created is not owned by the plain user SID. The guard must accept ownership by the
    token's default owner SID (what this token stamps on objects it creates) while still refusing a
    genuinely foreign owner."""
    try:
        from . import secureio
    except ImportError:
        import secureio

    admin_owner = "S-1-5-32-544"          # BUILTIN\\Administrators — the elevated token owner
    plain_user = "S-1-5-21-1-2-3-1001"    # the non-elevated user SID (differs from the owner)
    monkeypatch.setattr(secureio, "_windows_current_user_sid", lambda: plain_user)
    monkeypatch.setattr(
        secureio, "_windows_current_token_owner_sid", lambda: admin_owner)

    # owned by the token's default owner -> accepted (the file we created under elevation)
    monkeypatch.setattr(secureio, "_windows_handle_owner_sid", lambda _h: admin_owner)
    secureio._require_windows_current_owner(0)   # must NOT raise

    # owned by the plain user -> still accepted
    monkeypatch.setattr(secureio, "_windows_handle_owner_sid", lambda _h: plain_user)
    secureio._require_windows_current_owner(0)

    # owned by a DIFFERENT principal (LocalSystem) -> still refused
    monkeypatch.setattr(secureio, "_windows_handle_owner_sid", lambda _h: "S-1-5-18")
    with pytest.raises(PermissionError, match="not owned by current user"):
        secureio._require_windows_current_owner(0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows ACLs are authoritative")
def test_log_is_owner_only_from_first_byte_and_handlers_redact(tmp_path):
    """A fresh process catches both unsafe FileHandler creation and missing filters."""
    root = Path(__file__).resolve().parents[1]
    code = """
import logging
import os
import stat
from Final import secureio
secureio.ensure_private_file = lambda *_args, **_kwargs: None
os.umask(0)
from Final import yt_final
yt_final.logger.info(
    "notify=https://hooks.example.test/private/token?key=abc status=ready"
)
logging.shutdown()
path = "final_yt.log"
assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
handlers = logging.getLogger().handlers
assert yt_final._stream_handler in handlers
assert yt_final._file_handler in handlers
assert all(
    any(isinstance(f, yt_final.SensitiveDataFilter) for f in handler.filters)
    for handler in (yt_final._stream_handler, yt_final._file_handler)
)
text = open(path, encoding="utf-8").read()
assert "hooks.example.test" in text
assert "private/token" not in text
assert "key=abc" not in text
assert "status=ready" in text
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # The log no longer follows the working directory -- it lives under the app data dir, because
    # opening it relative to the CWD at import time crashed the program outright when that
    # directory was not writable. Pin it into tmp_path with the documented override so these
    # assertions still inspect the real file this run wrote.
    env["YT_FINAL_LOG"] = str(tmp_path / "final_yt.log")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "private/token" not in result.stdout
    assert "key=abc" not in result.stdout
    assert "status=ready" in result.stdout


def test_all_product_logger_names_reach_private_filtered_handlers(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = """
import logging
root_logger = logging.getLogger()
root_level_before = root_logger.level
assert root_logger.handlers == []
from Final import yt_final
product_names = [
    "Final.yt_final",
    "yt_final",
    "Final.webserver",
    "webserver",
    "Final.optional_tools",
    "optional_tools",
    "Final.managed_env",
    "managed_env",
    "Final.ffmpeg_updater",
    "ffmpeg_updater",
    "Final.capability",
    "capability",
    "Final.markers",
    "markers",
    "Final.nfo",
    "nfo",
]
for index, name in enumerate(product_names):
    logging.getLogger(name).info(
        "product-marker-%d notify=https://hooks.example.test/private-%d?token=abc",
        index,
        index,
    )
unrelated = logging.getLogger("unrelated.library")
unrelated.setLevel(logging.INFO)
unrelated.info("unrelated-info-sentinel")
logging.shutdown()
log_text = open("final_yt.log", encoding="utf-8").read()
assert root_logger.level == root_level_before
assert yt_final._stream_handler in root_logger.handlers
assert yt_final._file_handler in root_logger.handlers
for index, name in enumerate(product_names):
    marker = "product-marker-%d " % index
    assert log_text.count(marker) == 1, (name, log_text)
    assert "private-%d" % index not in log_text
assert "token=abc" not in log_text
assert "unrelated-info-sentinel" not in log_text
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # The log no longer follows the working directory -- it lives under the app data dir, because
    # opening it relative to the CWD at import time crashed the program outright when that
    # directory was not writable. Pin it into tmp_path with the documented override so these
    # assertions still inspect the real file this run wrote.
    env["YT_FINAL_LOG"] = str(tmp_path / "final_yt.log")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for index in range(16):
        assert result.stdout.count(f"product-marker-{index} ") == 1
        assert f"private-{index}" not in result.stdout
    assert "token=abc" not in result.stdout
    assert "unrelated-info-sentinel" not in result.stdout


def test_logging_is_deterministic_when_root_is_preconfigured(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = """
import io
import logging
external = io.StringIO()
external_handler = logging.StreamHandler(external)
root_logger = logging.getLogger()
root_logger.addHandler(external_handler)
root_logger.setLevel(logging.WARNING)
root_handlers_before = list(root_logger.handlers)
external_filters_before = list(external_handler.filters)
from Final import yt_final
logging.getLogger("Final.webserver").info(
    "notify=https://hooks.example.test/private/token?key=abc sibling-status=ready"
)
unrelated = logging.getLogger("unrelated.library")
unrelated.setLevel(logging.INFO)
unrelated.info("unrelated-info-sentinel")
logging.shutdown()
external_text = external.getvalue()
log_text = open("final_yt.log", encoding="utf-8").read()
assert "private/token" not in external_text
assert "key=abc" not in external_text
assert "sibling-status=ready" in external_text
assert "unrelated-info-sentinel" in external_text
assert "private/token" not in log_text
assert "key=abc" not in log_text
assert "sibling-status=ready" in log_text
assert "unrelated-info-sentinel" not in log_text
assert root_logger.level == logging.WARNING
assert all(handler in root_logger.handlers for handler in root_handlers_before)
assert external_handler.filters == external_filters_before
assert yt_final._stream_handler in root_logger.handlers
assert yt_final._file_handler in root_logger.handlers
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # The log no longer follows the working directory -- it lives under the app data dir, because
    # opening it relative to the CWD at import time crashed the program outright when that
    # directory was not writable. Pin it into tmp_path with the documented override so these
    # assertions still inspect the real file this run wrote.
    env["YT_FINAL_LOG"] = str(tmp_path / "final_yt.log")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "private/token" not in result.stdout
    assert "key=abc" not in result.stdout


def test_json_stream_is_attached_deduplicated_and_redirects_product_logs(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = """
import importlib
import logging
import sys
root_logger = logging.getLogger()
stdout_handler = logging.StreamHandler(sys.stdout)
root_logger.addHandler(stdout_handler)
root_logger.setLevel(logging.WARNING)
root_handlers_before = list(root_logger.handlers)
from Final import yt_final
importlib.reload(yt_final)
assert root_logger.level == logging.WARNING
assert all(handler in root_logger.handlers for handler in root_handlers_before)
assert yt_final._stream_handler in root_logger.handlers
assert yt_final._file_handler in root_logger.handlers
yt_final._stream_handler.stream = sys.stderr
logging.getLogger("webserver").info(
    "json-product-ready notify=https://hooks.example.test/private/token?key=abc"
)
print('{"ok":true}')
logging.shutdown()
log_text = open("final_yt.log", encoding="utf-8").read()
assert log_text.count("json-product-ready") == 1
assert "private/token" not in log_text
assert "key=abc" not in log_text
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # The log no longer follows the working directory -- it lives under the app data dir, because
    # opening it relative to the CWD at import time crashed the program outright when that
    # directory was not writable. Pin it into tmp_path with the documented override so these
    # assertions still inspect the real file this run wrote.
    env["YT_FINAL_LOG"] = str(tmp_path / "final_yt.log")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == '{"ok":true}'
    assert result.stderr.count("json-product-ready") == 1
    assert "private/token" not in result.stderr
    assert "key=abc" not in result.stderr


def test_list_formats_json_redirects_swapped_file_handler_in_fresh_process(tmp_path):
    """A FileHandler directly swapped to stdout must not corrupt or duplicate JSON output."""
    root = Path(__file__).resolve().parents[1]
    code = """
import json
import logging
import sys

root_logger = logging.getLogger()
external_handler = logging.FileHandler("host-owned.log", encoding="utf-8")
external_file_stream = external_handler.stream
external_handler.stream = sys.stdout
root_logger.addHandler(external_handler)
root_logger.setLevel(logging.WARNING)

from Final import yt_final
import yt_dlp

root_handlers_before = list(root_logger.handlers)
external_filters_before = list(external_handler.filters)
external_formatter_before = external_handler.formatter
external_level_before = external_handler.level
root_level_before = root_logger.level
format_rows = yt_final.YouTubeDownloader._format_rows

class FakeDownloader:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_optimized_ydl_opts(self, _quality):
        return {}

    _format_rows = staticmethod(format_rows)

class FakeYoutubeDL:
    def __init__(self, _opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        assert download is False
        logging.getLogger("unrelated.library").warning("third-party-json-warning")
        logging.getLogger("Final.yt_final").info("product-list-formats-status")
        return {
            "id": "video-id",
            "title": "Video title",
            "formats": [{"format_id": "18", "ext": "mp4", "height": 360}],
        }

yt_final.get_detector = lambda: object()
yt_final.YouTubeDownloader = FakeDownloader
yt_dlp.YoutubeDL = FakeYoutubeDL
sys.argv = ["yt_final.py", "list-formats", "https://example.test/video", "--json"]
try:
    yt_final.main()
    assert external_handler.stream is sys.stdout
    assert list(root_logger.handlers) == root_handlers_before
    assert list(external_handler.filters) == external_filters_before
    assert external_handler.formatter is external_formatter_before
    assert external_handler.level == external_level_before
    assert root_logger.level == root_level_before
finally:
    root_logger.removeHandler(external_handler)
    external_handler.stream = external_file_stream
    external_handler.close()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # The log no longer follows the working directory -- it lives under the app data dir, because
    # opening it relative to the CWD at import time crashed the program outright when that
    # directory was not writable. Pin it into tmp_path with the documented override so these
    # assertions still inspect the real file this run wrote.
    env["YT_FINAL_LOG"] = str(tmp_path / "final_yt.log")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["id"] == "video-id"
    assert payload["formats"][0]["format_id"] == "18"
    assert "third-party-json-warning" not in result.stdout
    assert result.stderr.count("third-party-json-warning") == 1
    assert result.stderr.count("product-list-formats-status") == 1


def test_history_json_redirects_swapped_file_handler_in_fresh_process(tmp_path):
    """History JSONL stays parseable with a host FileHandler whose live stream is stdout."""
    root = Path(__file__).resolve().parents[1]
    code = """
import logging
import sys

root_logger = logging.getLogger()
external_handler = logging.FileHandler("host-owned.log", encoding="utf-8")
external_file_stream = external_handler.stream
external_handler.stream = sys.stdout
root_logger.addHandler(external_handler)
root_logger.setLevel(logging.WARNING)

from Final import yt_final

root_handlers_before = list(root_logger.handlers)
external_filters_before = list(external_handler.filters)
external_formatter_before = external_handler.formatter
external_level_before = external_handler.level
root_level_before = root_logger.level

class FakeHistory:
    def __init__(self, _path):
        pass

    def read(self, limit=None):
        assert limit == 50
        logging.getLogger("unrelated.library").warning("third-party-history-warning")
        logging.getLogger("Final.webserver").info("product-history-status")
        return [
            {"status": "done", "id": "history-row-1"},
            {"status": "failed", "id": "history-row-2"},
        ]

yt_final._get_history_cls = lambda: FakeHistory
sys.argv = ["yt_final.py", "history", "--json"]
try:
    yt_final.main()
    assert external_handler.stream is sys.stdout
    assert list(root_logger.handlers) == root_handlers_before
    assert list(external_handler.filters) == external_filters_before
    assert external_handler.formatter is external_formatter_before
    assert external_handler.level == external_level_before
    assert root_logger.level == root_level_before
finally:
    root_logger.removeHandler(external_handler)
    external_handler.stream = external_file_stream
    external_handler.close()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # The log no longer follows the working directory -- it lives under the app data dir, because
    # opening it relative to the CWD at import time crashed the program outright when that
    # directory was not writable. Pin it into tmp_path with the documented override so these
    # assertions still inspect the real file this run wrote.
    env["YT_FINAL_LOG"] = str(tmp_path / "final_yt.log")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        {"status": "done", "id": "history-row-1"},
        {"status": "failed", "id": "history-row-2"},
    ]
    assert "third-party-history-warning" not in result.stdout
    assert result.stderr.count("third-party-history-warning") == 1
    assert result.stderr.count("product-history-status") == 1


def test_history_json_restores_swapped_file_handler_policy_after_success(
        monkeypatch, tmp_path):
    """JSONL isolation restores a stdout-swapped FileHandler and leaves real files alone."""
    import io

    try:
        from . import yt_final
    except ImportError:
        import yt_final

    command_stdout = io.StringIO()
    command_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", command_stdout)
    monkeypatch.setattr(sys, "stderr", command_stderr)

    root_logger = logging.getLogger()
    root_level_before = root_logger.level
    managed_stream_before = yt_final._stream_handler.stream
    managed_file_stream_before = yt_final._file_handler.stream
    external_handler = logging.FileHandler(
        tmp_path / "host-owned-history.log", encoding="utf-8"
    )
    external_file_stream = external_handler.stream
    external_handler.stream = sys.stdout
    external_handler.setLevel(logging.WARNING)
    external_formatter = logging.Formatter("external:%(message)s")
    external_filter = logging.Filter()
    external_handler.setFormatter(external_formatter)
    external_handler.addFilter(external_filter)
    external_filters_before = list(external_handler.filters)
    root_logger.addHandler(external_handler)
    root_handlers_during_test = list(root_logger.handlers)

    class FakeHistory:
        def __init__(self, _path):
            pass

        def read(self, limit=None):
            assert limit == 50
            assert external_handler.stream is command_stderr
            assert yt_final._file_handler.stream is managed_file_stream_before
            logging.getLogger("unrelated.library").warning("during-history-json")
            return [
                {"status": "done", "id": "history-row-1"},
                {"status": "failed", "id": "history-row-2"},
            ]

    monkeypatch.setattr(yt_final, "_get_history_cls", lambda: FakeHistory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["yt_final.py", "history", "--json"],
    )

    try:
        yt_final.main()
        payload_text = command_stdout.getvalue()
        restored_external_stream = external_handler.stream
        restored_managed_stream = yt_final._stream_handler.stream
        restored_managed_file_stream = yt_final._file_handler.stream
        restored_filters = list(external_handler.filters)
        restored_formatter = external_handler.formatter
        restored_level = external_handler.level
        restored_root_level = root_logger.level
        restored_root_handlers = list(root_logger.handlers)
        logging.getLogger("unrelated.library").warning("after-history-json")
        post_command_text = command_stdout.getvalue()
    finally:
        yt_final._stream_handler.stream = managed_stream_before
        root_logger.removeHandler(external_handler)
        external_handler.stream = external_file_stream
        external_handler.close()

    assert [json.loads(line) for line in payload_text.splitlines()] == [
        {"status": "done", "id": "history-row-1"},
        {"status": "failed", "id": "history-row-2"},
    ]
    assert "during-history-json" not in payload_text
    assert "during-history-json" in command_stderr.getvalue()
    assert "after-history-json" in post_command_text
    assert restored_external_stream is command_stdout
    assert restored_managed_stream is managed_stream_before
    assert restored_managed_file_stream is managed_file_stream_before
    assert restored_filters == external_filters_before
    assert restored_formatter is external_formatter
    assert restored_level == logging.WARNING
    assert restored_root_level == root_level_before
    assert restored_root_handlers == root_handlers_during_test


def test_list_formats_json_restores_swapped_file_handler_policy_on_system_exit(
        monkeypatch, tmp_path):
    """SystemExit restores a stdout-swapped FileHandler without changing host policy."""
    import io
    import yt_dlp

    try:
        from . import yt_final
    except ImportError:
        import yt_final

    command_stdout = io.StringIO()
    command_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", command_stdout)
    monkeypatch.setattr(sys, "stderr", command_stderr)

    root_logger = logging.getLogger()
    root_level_before = root_logger.level
    managed_stream_before = yt_final._stream_handler.stream
    managed_file_stream_before = yt_final._file_handler.stream
    external_handler = logging.FileHandler(
        tmp_path / "host-owned-formats.log", encoding="utf-8"
    )
    external_file_stream = external_handler.stream
    external_handler.stream = sys.stdout
    external_handler.setLevel(logging.WARNING)
    external_formatter = logging.Formatter("external:%(message)s")
    external_filter = logging.Filter()
    external_handler.setFormatter(external_formatter)
    external_handler.addFilter(external_filter)
    external_filters_before = list(external_handler.filters)
    root_logger.addHandler(external_handler)
    root_handlers_during_test = list(root_logger.handlers)

    class FakeDownloader:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_optimized_ydl_opts(self, _quality):
            return {}

    class FailingYoutubeDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            assert download is False
            assert external_handler.stream is command_stderr
            assert yt_final._file_handler.stream is managed_file_stream_before
            logging.getLogger("unrelated.library").warning("during-list-formats-error")
            raise RuntimeError("expected extraction failure")

    monkeypatch.setattr(yt_final, "get_detector", lambda: object())
    monkeypatch.setattr(yt_final, "YouTubeDownloader", FakeDownloader)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FailingYoutubeDL)
    monkeypatch.setattr(
        sys,
        "argv",
        ["yt_final.py", "list-formats", "https://example.test/video", "--json"],
    )

    try:
        with pytest.raises(SystemExit) as exc_info:
            yt_final.main()
        restored_external_stream = external_handler.stream
        restored_managed_stream = yt_final._stream_handler.stream
        restored_managed_file_stream = yt_final._file_handler.stream
        restored_filters = list(external_handler.filters)
        restored_formatter = external_handler.formatter
        restored_level = external_handler.level
        restored_root_level = root_logger.level
        restored_root_handlers = list(root_logger.handlers)
        command_text_after_error = command_stdout.getvalue()
        logging.getLogger("unrelated.library").warning("after-list-formats-error")
        post_command_text = command_stdout.getvalue()
    finally:
        yt_final._stream_handler.stream = managed_stream_before
        root_logger.removeHandler(external_handler)
        external_handler.stream = external_file_stream
        external_handler.close()

    assert exc_info.value.code == 1
    assert command_text_after_error == ""
    assert "during-list-formats-error" in command_stderr.getvalue()
    assert "expected extraction failure" in command_stderr.getvalue()
    assert "after-list-formats-error" in post_command_text
    assert restored_external_stream is command_stdout
    assert restored_managed_stream is managed_stream_before
    assert restored_managed_file_stream is managed_file_stream_before
    assert restored_filters == external_filters_before
    assert restored_formatter is external_formatter
    assert restored_level == logging.WARNING
    assert restored_root_level == root_level_before
    assert restored_root_handlers == root_handlers_during_test


def test_json_guard_surfaces_restore_failure_after_success_inside_outer_except(
        monkeypatch):
    """An unrelated handled exception must not make successful guard cleanup best-effort."""
    import io

    try:
        from . import yt_final
    except ImportError:
        import yt_final

    command_stdout = io.StringIO()
    command_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", command_stdout)
    monkeypatch.setattr(sys, "stderr", command_stderr)

    events = []
    failing_handler = _StreamSwapProbe(
        "failing", command_stdout, events, fail_on_restore=True
    )
    root_logger = logging.getLogger()
    managed_stream_before = yt_final._stream_handler.stream
    root_logger.addHandler(failing_handler)

    try:
        try:
            raise LookupError("unrelated outer error")
        except LookupError:
            with pytest.raises(RuntimeError, match="^failing restore failed$"):
                with yt_final._json_stdout_guard():
                    assert failing_handler.stream is command_stderr
        failing_stream_after = failing_handler.stream
        managed_stream_after = yt_final._stream_handler.stream
    finally:
        yt_final._stream_handler.stream = managed_stream_before
        failing_handler.reset_for_cleanup()
        root_logger.removeHandler(failing_handler)

    assert events == [("failing", "redirect"), ("failing", "restore")]
    assert failing_stream_after is command_stderr
    assert managed_stream_after is managed_stream_before


@pytest.mark.parametrize(
    "body_error",
    [
        pytest.param(RuntimeError("json body failed"), id="exception"),
        pytest.param(SystemExit(23), id="system-exit"),
    ],
)
def test_json_guard_preserves_body_error_during_reverse_best_effort_restore(
        monkeypatch, body_error):
    """Body errors stay primary while every stream restoration is attempted in LIFO order."""
    import io

    try:
        from . import yt_final
    except ImportError:
        import yt_final

    command_stdout = io.StringIO()
    command_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", command_stdout)
    monkeypatch.setattr(sys, "stderr", command_stderr)

    events = []
    first_handler = _StreamSwapProbe("first", command_stdout, events)
    failing_handler = _StreamSwapProbe(
        "failing", command_stdout, events, fail_on_restore=True
    )
    last_handler = _StreamSwapProbe("last", command_stdout, events)
    root_logger = logging.getLogger()
    managed_stream_before = yt_final._stream_handler.stream
    for handler in (first_handler, failing_handler, last_handler):
        root_logger.addHandler(handler)

    try:
        with pytest.raises(type(body_error)) as exc_info:
            with yt_final._json_stdout_guard():
                assert first_handler.stream is command_stderr
                assert failing_handler.stream is command_stderr
                assert last_handler.stream is command_stderr
                raise body_error
        first_stream_after = first_handler.stream
        failing_stream_after = failing_handler.stream
        last_stream_after = last_handler.stream
        managed_stream_after = yt_final._stream_handler.stream
    finally:
        yt_final._stream_handler.stream = managed_stream_before
        for handler in (last_handler, failing_handler, first_handler):
            handler.reset_for_cleanup()
            root_logger.removeHandler(handler)

    assert exc_info.value is body_error
    assert events == [
        ("first", "redirect"),
        ("failing", "redirect"),
        ("last", "redirect"),
        ("last", "restore"),
        ("failing", "restore"),
        ("first", "restore"),
    ]
    assert first_stream_after is command_stdout
    assert failing_stream_after is command_stderr
    assert last_stream_after is command_stdout
    assert managed_stream_after is managed_stream_before


def test_json_guard_preserves_setup_error_when_restoration_also_fails(monkeypatch):
    """A setup exception remains primary while partially redirected handlers are restored."""
    import io

    try:
        from . import yt_final
    except ImportError:
        import yt_final

    command_stdout = io.StringIO()
    command_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", command_stdout)
    monkeypatch.setattr(sys, "stderr", command_stderr)

    events = []
    restore_failing_handler = _StreamSwapProbe(
        "first", command_stdout, events, fail_on_restore=True
    )
    setup_failing_handler = _StreamSwapProbe(
        "second", command_stdout, events, fail_on_redirect=True
    )
    root_logger = logging.getLogger()
    managed_stream_before = yt_final._stream_handler.stream
    root_logger.addHandler(restore_failing_handler)
    root_logger.addHandler(setup_failing_handler)

    try:
        with pytest.raises(RuntimeError, match="^second redirect failed$"):
            with yt_final._json_stdout_guard():
                raise AssertionError("guard body must not run after setup failure")
        restore_failing_stream_after = restore_failing_handler.stream
        setup_failing_stream_after = setup_failing_handler.stream
        managed_stream_after = yt_final._stream_handler.stream
    finally:
        yt_final._stream_handler.stream = managed_stream_before
        setup_failing_handler.reset_for_cleanup()
        restore_failing_handler.reset_for_cleanup()
        root_logger.removeHandler(setup_failing_handler)
        root_logger.removeHandler(restore_failing_handler)

    assert events == [
        ("first", "redirect"),
        ("second", "redirect"),
        ("second", "restore"),
        ("first", "restore"),
    ]
    assert restore_failing_stream_after is command_stderr
    assert setup_failing_stream_after is command_stdout
    assert managed_stream_after is managed_stream_before


def test_json_guard_restores_all_streams_without_masking_body_error(monkeypatch):
    """Host StreamHandler callbacks cannot interrupt cleanup or replace the body error."""
    import io

    try:
        from . import yt_final
    except ImportError:
        import yt_final

    command_stdout = io.StringIO()
    command_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", command_stdout)
    monkeypatch.setattr(sys, "stderr", command_stderr)

    class HostileSetStreamHandler(logging.StreamHandler):
        def setStream(self, stream):
            raise RuntimeError("host stream callback must not run")

    root_logger = logging.getLogger()
    managed_stream_before = yt_final._stream_handler.stream
    ordinary_handler = logging.StreamHandler(command_stdout)
    hostile_handler = HostileSetStreamHandler(command_stdout)
    root_logger.addHandler(ordinary_handler)
    root_logger.addHandler(hostile_handler)

    try:
        with pytest.raises(RuntimeError, match="^json body failed$"):
            with yt_final._json_stdout_guard():
                assert yt_final._stream_handler.stream is command_stderr
                assert ordinary_handler.stream is command_stderr
                assert hostile_handler.stream is command_stderr
                raise RuntimeError("json body failed")
        managed_stream_after = yt_final._stream_handler.stream
        ordinary_stream_after = ordinary_handler.stream
        hostile_stream_after = hostile_handler.stream
    finally:
        yt_final._stream_handler.stream = managed_stream_before
        ordinary_handler.stream = command_stdout
        hostile_handler.stream = command_stdout
        root_logger.removeHandler(hostile_handler)
        root_logger.removeHandler(ordinary_handler)

    assert managed_stream_after is managed_stream_before
    assert ordinary_stream_after is command_stdout
    assert hostile_stream_after is command_stdout


def test_history_concurrent_writers_leave_only_valid_jsonl(tmp_path):
    path = tmp_path / "history.jsonl"
    errors: list[BaseException] = []

    def write_rows(worker: int) -> None:
        ledger = jobstore.HistoryLedger(path)
        try:
            for row in range(25):
                ledger.record(
                    url=f"https://youtu.be/{worker}-{row}",
                    output=f"out-{worker}-{row}.mp4",
                    profile="copy",
                    status="done",
                    spec={"urls": [f"https://youtu.be/{worker}-{row}"]},
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_rows, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    records = [json.loads(line) for line in lines]
    assert len({record["url"] for record in records}) == 200


def test_history_cross_process_writers_leave_only_valid_jsonl(tmp_path):
    path = tmp_path / "history.jsonl"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_history_process_writer, args=(str(path), worker, 40))
        for worker in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)

    assert all(not process.is_alive() for process in processes)
    assert [process.exitcode for process in processes] == [0] * 6
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 240
    records = [json.loads(line) for line in lines]
    assert len({record["url"] for record in records}) == 240


def test_history_uses_single_private_append_helper(tmp_path, monkeypatch):
    captured: list[tuple[Path, bytes, int]] = []

    def append(path: Path, line: bytes, mode: int = 0o600) -> None:
        captured.append((Path(path), line, mode))

    monkeypatch.setattr(jobstore, "append_private_line", append, raising=False)
    path = tmp_path / "history.jsonl"
    record = jobstore.HistoryLedger(path).record(
        url="https://youtu.be/helper",
        output="out.mp4",
        profile="copy",
        status="done",
        spec={"urls": ["https://youtu.be/helper"]},
    )

    assert len(captured) == 1
    written_path, line, mode = captured[0]
    assert written_path == path
    assert mode == 0o600
    assert line.endswith(b"\n")
    assert json.loads(line) == record


def test_history_schema_round_trips_through_redo_consumer(tmp_path):
    try:
        from .yt_final import _resolve_redo_spec
    except ImportError:
        from yt_final import _resolve_redo_spec

    path = tmp_path / "history.jsonl"
    original = JobSpec(
        urls=["https://youtu.be/schema"],
        quality="720p",
        profile="prores4444",
        output_dir=str(tmp_path / "private output"),
        proxy="http://user:pass@proxy.example.test:8080",
    )
    jobstore.HistoryLedger(path).record(
        url=original.urls[0],
        output=str(tmp_path / "private output" / "video.mov"),
        profile=original.profile,
        status="done",
        spec=original,
        job_id="j_schema",
        ts=123.5,
    )

    stored = _resolve_redo_spec("last", history_file=path)
    rerun = JobSpec.from_dict(stored)
    assert rerun.to_dict() == original.to_dict()


def test_ffmpeg_info_log_is_summary_not_full_argv(monkeypatch, caplog):
    try:
        from . import yt_final
    except ImportError:
        import yt_final

    class FinishedProcess:
        returncode = 0
        stderr = ()

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=None):
            return 0

    monkeypatch.setattr(yt_final.subprocess, "Popen", lambda *_args, **_kwargs: FinishedProcess())
    monkeypatch.setattr(
        yt_final.FFmpegProcessor, "_begin_ffmpeg_activity", classmethod(lambda cls: None)
    )
    monkeypatch.setattr(
        yt_final.FFmpegProcessor, "_end_ffmpeg_activity", classmethod(lambda cls: None)
    )
    processor = object.__new__(yt_final.FFmpegProcessor)
    processor.cancel_check = None
    cmd = [
        "ffmpeg",
        "-i",
        "C:\\Users\\Alice\\secret input.mp4",
        "-i",
        "/Users/alice/private/subtitle.srt",
        "-c:v",
        "libx264",
        "/Users/alice/private/final.mp4",
    ]

    caplog.set_level(logging.INFO, logger=yt_final.logger.name)
    assert processor._run_ffmpeg(cmd) is True

    launch = next(record.getMessage() for record in caplog.records if "[FFMPEG]" in record.getMessage())
    assert "operation=encode" in launch
    assert "inputs=2" in launch
    assert "output=final.mp4" in launch
    assert "Alice" not in launch
    assert "subtitle.srt" not in launch
    assert "libx264" not in launch


def test_unlabelled_absolute_paths_do_not_leak_the_account_name():
    """The labelled rules only fire on known phrasings (cookies:, output:, saving to, ...). Real log
    lines and traceback frames use none of them, so an absolute path in unlabelled prose wrote the
    operator's home directory — and on Windows that contains their username — verbatim into
    final_yt.log.

    Reproduced before the fix: of five representative lines, the two labelled ones redacted and the
    three unlabelled ones leaked."""
    import pathlib
    from redaction import redact_sensitive_text

    home = str(pathlib.Path.home())
    if len(home.rstrip(chr(92) + "/")) < 4:
        pytest.skip("degenerate home directory; the prefix rule is disabled by design")

    leaky = [
        "Saved " + home + chr(92) + "Videos" + chr(92) + "clip.mp4 (120 MiB)",
        'File "' + home + chr(92) + "Final" + chr(92) + 'yt_final.py", line 42',
        "scanning " + home.replace(chr(92), "/") + "/AppData for profiles",
        "repr: " + home.replace(chr(92), chr(92) * 2),
    ]
    for line in leaky:
        out = redact_sensitive_text(line)
        assert home.lower() not in out.lower(), "home directory leaked: " + out
        assert "<home>" in out, "expected the prefix to be replaced, got: " + out

    # the labelled rules must still win where they apply — this is a backstop, not a replacement
    assert "<redacted-path>" in redact_sensitive_text("cookies: " + home + chr(92) + "cookies.txt")

    # ...and a path that is NOT under the home directory must survive intact, or logs become useless
    other = "D:" + chr(92) + "Footage" + chr(92) + "raw.mp4"
    assert other in redact_sensitive_text("reading " + other)


def test_the_home_prefix_rule_reaches_the_real_logger():
    """The existing coverage exercises redact_sensitive_text() directly. That cannot show whether the
    filter is actually ATTACHED to the handler that writes final_yt.log — a correct function nobody
    calls redacts nothing. Drive a real LogRecord through the shipped filter instead."""
    import logging
    import pathlib
    from redaction import SensitiveDataFilter

    home = str(pathlib.Path.home())
    if len(home.rstrip(chr(92) + "/")) < 4:
        pytest.skip("degenerate home directory; the prefix rule is disabled by design")

    record = logging.LogRecord(
        name="yt_final", level=logging.INFO, pathname=__file__, lineno=1,
        msg="delivered %s", args=(home + chr(92) + "Videos" + chr(92) + "out.mp4",), exc_info=None,
    )
    assert SensitiveDataFilter().filter(record) is True
    rendered = record.getMessage()
    assert home.lower() not in rendered.lower(), "the attached filter let the path through: " + rendered


def test_home_prefix_rule_is_disabled_for_a_degenerate_root(monkeypatch):
    """A drive-root home would turn EVERY absolute path in every log line into <home>, destroying the
    log's usefulness to fix a privacy problem that does not exist at that path.

    The chosen root must survive the empty-string filter or the test proves nothing: "/" strips to ""
    and is dropped by the falsy check whether or not the guard exists — my first version used it and
    stayed green with the guard removed. A drive root strips to "C:", which the length guard is the
    only thing stopping from becoming a prefix that matches every path on the volume."""
    import pathlib
    import redaction

    drive_root = "C:" + chr(92)
    monkeypatch.setattr(
        pathlib.Path, "home", classmethod(lambda cls: pathlib.PurePath(drive_root))
    )
    assert redaction._home_prefix_variants() == (), \
        "a drive-root home became a redaction prefix — every path on that volume would read <home>"


def test_log_does_not_land_in_the_working_directory(tmp_path):
    """Importing the engine must not drop a log file wherever the shell happens to be.

    `_log_path` was a bare Path("final_yt.log") opened at IMPORT time, before argparse -- so even
    `yt-final --version` wrote a log into the current directory, and `serve` scattered one per
    launch directory. The log is also the one place the user is told to look for a traceback, and
    it was never in the same place twice.
    """
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.pop("YT_FINAL_LOG", None)
    result = subprocess.run(
        [sys.executable, str(root / "Final" / "yt_final.py"), "--version"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr[-400:]
    assert not list(tmp_path.iterdir()), (
        "running --version left files in the working directory", list(tmp_path.iterdir()))


def test_unwritable_log_location_degrades_instead_of_killing_the_import(tmp_path):
    """A log that cannot be opened must not stop the program from starting.

    The open happened at import, so a non-writable working directory raised a bare
    PermissionError pointing at a logging line before any command ran. An elevated cmd.exe starts
    in the Windows system directory, so "run as administrator" alone was enough to make the
    tool refuse to start, with an error naming nothing the user had done.
    """
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    # A path that cannot be created: a file where a directory would have to be.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    env["YT_FINAL_LOG"] = str(blocker / "final_yt.log")
    result = subprocess.run(
        [sys.executable, "-c",
         "from Final import yt_final;"
         "print('LOGPATH', yt_final._log_path);"
         "print('ERR', type(yt_final._file_logging_error).__name__);"
         "yt_final.logger.info('console only')"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr[-600:]
    assert "LOGPATH None" in result.stdout, result.stdout
    assert "ERR NoneType" not in result.stdout, "the failure should have been recorded"
