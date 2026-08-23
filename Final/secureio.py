"""Durable owner-only file replacement helpers for local credential data."""
from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path


_APPEND_LOCK = threading.RLock()
_WINDOWS_SID_LOCK = threading.Lock()
_WINDOWS_USER_SID: str | None = None
_WINDOWS_TOKEN_OWNER_SID: str | None = None


def _windows_error(code: int | None = None) -> OSError:
    import ctypes

    return ctypes.WinError(code if code is not None else ctypes.get_last_error())


def _windows_current_user_sid() -> str:
    """Return the process token's user SID in SDDL form."""
    global _WINDOWS_USER_SID
    if _WINDOWS_USER_SID is not None:
        return _WINDOWS_USER_SID
    with _WINDOWS_SID_LOCK:
        if _WINDOWS_USER_SID is not None:
            return _WINDOWS_USER_SID
        import ctypes
        from ctypes import wintypes

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("Sid", ctypes.c_void_p),
                ("Attributes", wintypes.DWORD),
            ]

        class TOKEN_USER(ctypes.Structure):
            _fields_ = [("User", SID_AND_ATTRIBUTES)]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        token = wintypes.HANDLE()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise _windows_error()
        try:
            needed = wintypes.DWORD()
            advapi32.GetTokenInformation.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            advapi32.GetTokenInformation.restype = wintypes.BOOL
            advapi32.GetTokenInformation(
                token, 1, None, 0, ctypes.byref(needed))
            if ctypes.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
                raise _windows_error()
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                    token, 1, buffer, needed, ctypes.byref(needed)):
                raise _windows_error()
            token_user = ctypes.cast(
                buffer, ctypes.POINTER(TOKEN_USER)).contents
            text = ctypes.c_void_p()
            advapi32.ConvertSidToStringSidW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
            if not advapi32.ConvertSidToStringSidW(
                    token_user.User.Sid, ctypes.byref(text)):
                raise _windows_error()
            try:
                _WINDOWS_USER_SID = ctypes.wstring_at(text)
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
    sid = _WINDOWS_USER_SID
    if sid is None:
        raise OSError("Windows SID conversion returned no value")
    return sid


def _windows_current_token_owner_sid() -> str:
    """Return the SID the current process token assigns as OWNER to objects it creates.

    This is TokenUser for an ordinary process, but for a process running with an elevated
    Administrators token Windows makes the default owner the Administrators group (S-1-5-32-544).
    Files this process CREATES therefore come back owned by that SID, so it is legitimately "owned
    by me". Accepting it (below) does not widen the guard to other users: a file planted by a
    different, non-elevated user is owned by THAT user's SID, never by this token's owner default.
    """
    global _WINDOWS_TOKEN_OWNER_SID
    if _WINDOWS_TOKEN_OWNER_SID is not None:
        return _WINDOWS_TOKEN_OWNER_SID
    with _WINDOWS_SID_LOCK:
        if _WINDOWS_TOKEN_OWNER_SID is not None:
            return _WINDOWS_TOKEN_OWNER_SID
        import ctypes
        from ctypes import wintypes

        class TOKEN_OWNER(ctypes.Structure):
            _fields_ = [("Owner", ctypes.c_void_p)]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        token = wintypes.HANDLE()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise _windows_error()
        try:
            needed = wintypes.DWORD()
            advapi32.GetTokenInformation.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            advapi32.GetTokenInformation.restype = wintypes.BOOL
            advapi32.GetTokenInformation(
                token, 4, None, 0, ctypes.byref(needed))  # 4 == TokenOwner
            if ctypes.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
                raise _windows_error()
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                    token, 4, buffer, needed, ctypes.byref(needed)):
                raise _windows_error()
            token_owner = ctypes.cast(
                buffer, ctypes.POINTER(TOKEN_OWNER)).contents
            text = ctypes.c_void_p()
            advapi32.ConvertSidToStringSidW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
            if not advapi32.ConvertSidToStringSidW(
                    token_owner.Owner, ctypes.byref(text)):
                raise _windows_error()
            try:
                _WINDOWS_TOKEN_OWNER_SID = ctypes.wstring_at(text)
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
    sid = _WINDOWS_TOKEN_OWNER_SID
    if sid is None:
        raise OSError("Windows SID conversion returned no value")
    return sid


def _windows_private_descriptor() -> int:
    """Allocate a protected, current-user-only security descriptor."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    # Set the OWNER (and group) explicitly to the current user, not just the DACL. Under an
    # elevated Administrators token the OS otherwise stamps new objects with the Administrators
    # group as owner (O:BA), which is broader than owner-only. This only takes effect where the
    # descriptor is applied at CREATION (CreateFileW/CreateDirectoryW SECURITY_ATTRIBUTES), which
    # is exactly where it is needed: the object manager honours the descriptor's owner and rejects
    # a SID the token may not assign with ERROR_INVALID_OWNER (1307). The process token's own user
    # SID is always assignable, so this can never make a create fail. Applying the same descriptor
    # to an EXISTING handle is a separate operation that needs WRITE_OWNER access, which we
    # deliberately do not request — see _open_private_windows.
    sid = _windows_current_user_sid()
    sddl = f"O:{sid}G:{sid}D:P(A;;FA;;;{sid})"
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise _windows_error()
    value = descriptor.value
    if value is None:
        raise OSError("Windows returned a null security descriptor")
    return int(value)


def _free_windows_descriptor(descriptor: int) -> None:
    import ctypes

    ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(
        ctypes.c_void_p(descriptor))


def _windows_handle_owner_sid(handle: int) -> str:
    """Return the owner SID for the file represented by an open HANDLE."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    owner = ctypes.c_void_p()
    get_info = advapi32.GetSecurityInfo
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_info.restype = wintypes.DWORD
    result = get_info(
        wintypes.HANDLE(handle),
        1,  # SE_FILE_OBJECT
        0x1,  # OWNER_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise _windows_error(result)
    try:
        if not owner.value:
            raise OSError("Windows file security descriptor has no owner")
        text = ctypes.c_void_p()
        convert = advapi32.ConvertSidToStringSidW
        convert.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        convert.restype = wintypes.BOOL
        if not convert(owner, ctypes.byref(text)):
            raise _windows_error()
        try:
            return ctypes.wstring_at(text)
        finally:
            ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(text)
    finally:
        if descriptor.value:
            _free_windows_descriptor(int(descriptor.value))


def _require_windows_current_owner(
        handle: int, path: Path | None = None) -> None:
    owner = _windows_handle_owner_sid(handle)
    # Accept the file when it is owned by the current user, OR by the SID this token assigns as the
    # default owner of objects it creates. Under an elevated Administrators token that default is
    # the Administrators group, so files this very process creates come back Administrators-owned
    # (standard on CI runners). This does not accept files owned by a DIFFERENT user — those carry
    # that user's SID, never this token's owner default.
    if owner == _windows_current_user_sid():
        return
    if owner == _windows_current_token_owner_sid():
        return
    message = "refusing to access private file not owned by current user"
    if path is not None:
        message += f": {path}"
    raise PermissionError(message)


def _tighten_windows_handle(handle: int, descriptor: int | None = None) -> None:
    """Replace a file HANDLE's inherited DACL with the protected private DACL."""
    import ctypes
    from ctypes import wintypes

    owned_descriptor = descriptor is None
    if descriptor is None:
        descriptor = _windows_private_descriptor()
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        ]
        get_dacl.restype = wintypes.BOOL
        if not get_dacl(
                ctypes.c_void_p(descriptor),
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted)):
            raise _windows_error()
        if not present or not dacl.value:
            raise OSError("private Windows descriptor has no DACL")
        # Only the DACL is transplanted here. Adding OWNER_SECURITY_INFORMATION would need
        # WRITE_OWNER on the handle, which callers legitimately do not hold (_cdp_client's
        # _secure_private_directory opens with READ_CONTROL|WRITE_DAC only, and asking for
        # WRITE_OWNER at open time is denied outright on a file we own whose DACL grants merely
        # "Modify" — both measured on Windows 11). The protected owner-only DACL is the security
        # guarantee; the owner is set where it can be, at creation time, by the descriptor itself.
        set_info = advapi32.SetSecurityInfo
        set_info.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        set_info.restype = wintypes.DWORD
        result = set_info(
            wintypes.HANDLE(handle),
            1,  # SE_FILE_OBJECT
            0x80000004,  # PROTECTED_DACL | DACL_SECURITY_INFORMATION
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise _windows_error(result)
    finally:
        if owned_descriptor:
            _free_windows_descriptor(descriptor)


def _open_private_windows(path: Path, flags: int) -> int:
    """Open/create a file with a private DACL and return a CRT descriptor."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    descriptor = _windows_private_descriptor()
    security_attributes = SECURITY_ATTRIBUTES(
        ctypes.sizeof(SECURITY_ATTRIBUTES),
        ctypes.c_void_p(descriptor),
        False,
    )
    # READ_CONTROL | WRITE_DAC | SYNCHRONIZE. Deliberately NOT WRITE_OWNER: an object's owner is
    # implicitly granted READ_CONTROL and WRITE_DAC but never WRITE_OWNER, so asking for it makes
    # CreateFileW fail with ERROR_ACCESS_DENIED on an existing file that we own whose DACL grants
    # only "Modify" (measured) — which would break ensure_private_file()/the append log path on
    # perfectly ordinary files. The owner is set at creation by the security descriptor instead.
    desired_access = 0x00020000 | 0x00040000 | 0x00100000
    if flags & os.O_RDWR:
        desired_access |= 0x80000000 | 0x40000000
    elif flags & os.O_WRONLY:
        desired_access |= 0x00000004 if flags & os.O_APPEND else 0x40000000
    else:
        desired_access |= 0x80000000
    if flags & os.O_CREAT and flags & os.O_EXCL:
        creation = 1  # CREATE_NEW
    elif flags & os.O_CREAT:
        creation = 4  # OPEN_ALWAYS
    else:
        creation = 3  # OPEN_EXISTING
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        os.fspath(path),
        desired_access,
        0x1 | 0x2 | 0x4,
        ctypes.byref(security_attributes),
        creation,
        0x80,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = _windows_error()
        _free_windows_descriptor(descriptor)
        if getattr(error, "winerror", None) in (80, 183):
            raise FileExistsError(error.errno, str(error), os.fspath(path))
        if getattr(error, "winerror", None) in (2, 3):
            raise FileNotFoundError(error.errno, str(error), os.fspath(path))
        raise error
    try:
        _require_windows_current_owner(int(handle), path)
        _tighten_windows_handle(int(handle), descriptor)
        crt_flags = flags & (
            os.O_APPEND | os.O_WRONLY | os.O_RDWR | getattr(os, "O_BINARY", 0)
        )
        fd = msvcrt.open_osfhandle(int(handle), crt_flags)
        handle = None
        return fd
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)
        _free_windows_descriptor(descriptor)


def ensure_private_fd(fd: int, mode: int = 0o600) -> None:
    """Tighten the file represented by an already-open descriptor."""
    if os.name == "nt":
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        _require_windows_current_owner(handle)
        _tighten_windows_handle(handle)
    elif hasattr(os, "fchmod"):
        os.fchmod(fd, mode)
    else:
        raise NotImplementedError("descriptor-based permission tightening unavailable")


def open_private_file(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open/create a private file, securing it before callers can write bytes."""
    target = Path(path)
    if flags & os.O_CREAT:
        target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        return _open_private_windows(target, flags)
    fd = os.open(os.fspath(target), flags, mode)
    try:
        ensure_private_fd(fd, mode)
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_private_file(path: Path, mode: int = 0o600) -> None:
    """Tighten an existing file through the descriptor actually opened."""
    fd = open_private_file(Path(path), os.O_RDONLY, mode)
    os.close(fd)


def append_private_line(path: Path, line: bytes, mode: int = 0o600) -> None:
    """Append one complete line durably without a public first-byte window."""
    if not isinstance(line, bytes):
        raise TypeError("line must be bytes")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0)
    with _APPEND_LOCK:
        fd = open_private_file(target, flags, mode)
        try:
            ensure_private_fd(fd, mode)
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(
                    f"short write while appending private line: {written}/{len(line)}"
                )
            os.fsync(fd)
        finally:
            os.close(fd)


def _open_private_temp(target: Path, mode: int) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    for _ in range(128):
        temporary = target.with_name(
            f".{target.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            return open_private_file(temporary, flags, mode), temporary
        except FileExistsError:
            continue
    raise FileExistsError(f"could not reserve a private temporary file for {target}")


def _replace_with_windows_retry(source: Path, destination: Path) -> None:
    for attempt in range(8):
        try:
            os.replace(os.fspath(source), os.fspath(destination))
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05)


def _fsync_parent(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_fd = os.open(os.fspath(path.parent), flags)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def atomic_replace_private(
        path: Path, data: bytes, mode: int = 0o600) -> None:
    """Publish complete bytes atomically without exposing a public temp file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temporary: Path | None = None
    try:
        fd, temporary = _open_private_temp(target, mode)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            remaining = memoryview(data)
            while remaining:
                written = output.write(remaining)
                if not written:
                    raise OSError("short write while replacing private file")
                remaining = remaining[written:]
            output.flush()
            os.fsync(output.fileno())
            ensure_private_fd(output.fileno(), mode)
        _replace_with_windows_retry(temporary, target)
        ensure_private_file(target, mode)
        _fsync_parent(target)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def copy_private_atomic(source: Path, destination: Path) -> None:
    """Copy a complete file through the owner-only atomic replacement path."""
    atomic_replace_private(Path(destination), Path(source).read_bytes())
