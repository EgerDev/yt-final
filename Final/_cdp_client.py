"""Minimal, bounded DevTools-Protocol client for Chromium cookie extraction."""
from __future__ import annotations
import atexit
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    from .secureio import atomic_replace_private
    from .executable_trust import (
        _windows_known_folder,
        fixed_authority_executable,
        installed_browser_executable,
    )
except ImportError:
    from secureio import atomic_replace_private
    from executable_trust import (
        _windows_known_folder,
        fixed_authority_executable,
        installed_browser_executable,
    )


_MAX_HTTP_BYTES = 64 * 1024
_MAX_WS_HEADER_BYTES = 16 * 1024
_MAX_WS_FRAME_BYTES = 2 * 1024 * 1024
_MAX_PROFILE_FILES = 64
_MAX_PROFILE_DIRECTORY_ENTRIES = 4096
_MAX_PROFILE_BYTES = 512 * 1024 * 1024
_PROFILE_NAMES = ("Default",)
_COOKIE_FILENAMES = ("Cookies", "Cookies-journal", "Cookies-wal", "Cookies-shm")
_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WINDOWS_CLEANUP_LOCK = threading.RLock()
_WINDOWS_CLEANUP_OWNERS: set[object] = set()


class _EndpointNotReady(Exception):
    """The browser has not finished publishing its owned endpoint yet."""


def _register_windows_cleanup(owner) -> None:
    with _WINDOWS_CLEANUP_LOCK:
        _WINDOWS_CLEANUP_OWNERS.add(owner)


def _unregister_windows_cleanup(owner) -> None:
    with _WINDOWS_CLEANUP_LOCK:
        _WINDOWS_CLEANUP_OWNERS.discard(owner)


def _raw_windows_handle(handle) -> int:
    value = getattr(handle, "value", handle)
    return int(value or 0)


def _windows_handle_is_proven_invalid(kernel32, handle: int) -> bool:
    """Prove a failed close refers to no live kernel handle before forgetting it."""
    import ctypes
    from ctypes import wintypes

    try:
        get_information = kernel32.GetHandleInformation
        get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_information.restype = wintypes.BOOL
        flags = wintypes.DWORD()
        ctypes.set_last_error(0)
        if get_information(
                wintypes.HANDLE(handle),
                ctypes.byref(flags)):
            return False
        return ctypes.get_last_error() == 6  # ERROR_INVALID_HANDLE
    except Exception:
        return False


def _close_windows_handle_once(handle: int) -> bool:
    """Close one raw handle or explicitly prove it is already invalid."""
    import ctypes
    from ctypes import wintypes

    value = _raw_windows_handle(handle)
    if not value:
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        if close_handle(wintypes.HANDLE(value)):
            return True
        return _windows_handle_is_proven_invalid(kernel32, value)
    except Exception:
        return False


class _WindowsHandleCleanup:
    """Durably own non-process handles until CloseHandle is proven complete."""

    def __init__(self, *handles: int):
        self._handles = [
            _raw_windows_handle(handle)
            for handle in handles
            if _raw_windows_handle(handle)
        ]
        self._cleanup_lock = threading.RLock()
        if self._handles:
            _register_windows_cleanup(self)

    @property
    def handles(self) -> tuple[int, ...]:
        with self._cleanup_lock:
            return tuple(handle for handle in self._handles if handle)

    def cleanup(self, *, deadline: float) -> bool:
        with self._cleanup_lock:
            for index, handle in enumerate(self._handles):
                if not handle:
                    continue
                try:
                    _remaining(deadline)
                except TimeoutError:
                    return False
                if _close_windows_handle_once(handle):
                    self._handles[index] = 0
            complete = not any(self._handles)
        if complete:
            _unregister_windows_cleanup(self)
        try:
            _remaining(deadline)
        except TimeoutError:
            return False
        return complete


def _durably_close_windows_handle(handle: int) -> bool:
    """Close now, transferring failed authority into the retry registry."""
    value = _raw_windows_handle(handle)
    if not value:
        return True
    if _close_windows_handle_once(value):
        return True
    _WindowsHandleCleanup(value)
    return False


def _drain_windows_cleanup_registry(*, deadline: float) -> bool:
    """Give every retained owner one bounded, thread-safe cleanup attempt."""
    with _WINDOWS_CLEANUP_LOCK:
        owners = tuple(_WINDOWS_CLEANUP_OWNERS)
    complete = True
    for owner in owners:
        try:
            _remaining(deadline)
            if not owner.cleanup(deadline=deadline):
                complete = False
        except Exception:
            complete = False
    try:
        _remaining(deadline)
    except TimeoutError:
        return False
    with _WINDOWS_CLEANUP_LOCK:
        return complete and not _WINDOWS_CLEANUP_OWNERS


def _drain_windows_cleanup_at_exit() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        _drain_windows_cleanup_registry(
            deadline=time.monotonic() + 1.0,
        )
    except Exception:
        pass


atexit.register(_drain_windows_cleanup_at_exit)


def _find_exe(name):
    candidate = installed_browser_executable(name)
    return str(candidate) if candidate is not None else None


def _default_user_data_dir(browser_name):
    """Best-effort OS-default Chromium 'User Data' root for a browser, or None.

    This mirrors the locations yt-dlp's own cookie module inspects, so the CDP
    tier can read the user's REAL logged-in session instead of a blank profile."""
    home = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        roots = {
            "chrome": Path(local) / "Google" / "Chrome" / "User Data",
            "edge": Path(local) / "Microsoft" / "Edge" / "User Data",
            "brave": Path(local) / "BraveSoftware" / "Brave-Browser" / "User Data",
        }
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        roots = {
            "chrome": base / "Google" / "Chrome",
            "edge": base / "Microsoft Edge",
            "brave": base / "BraveSoftware" / "Brave-Browser",
        }
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        roots = {
            "chrome": cfg / "google-chrome",
            "edge": cfg / "microsoft-edge",
            "brave": cfg / "BraveSoftware" / "Brave-Browser",
        }
    root = roots.get(browser_name)
    return root if (root and root.exists()) else None


def _remaining(deadline: float) -> float:
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("CDP operation deadline exceeded")
    return remaining


def _is_link_like(path: Path, metadata=None) -> bool:
    try:
        metadata = metadata or path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return True
        if getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_FLAG:
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _safe_profile_path(root: Path, path: Path, *, leaf_file: bool) -> os.stat_result:
    """Validate every component without following a link/junction/reparse point."""
    relative = path.relative_to(root)
    current = root
    for index, component in enumerate((None, *relative.parts)):
        if component is not None:
            current = current / component
        metadata = current.lstat()
        if _is_link_like(current, metadata):
            raise ValueError(f"linked profile path rejected: {current}")
        is_leaf = index == len(relative.parts)
        if is_leaf and leaf_file:
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"profile input is not a regular file: {current}")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"profile path is not a directory: {current}")
    return metadata


def _allowed_profile_name(name: str) -> bool:
    if name in _PROFILE_NAMES:
        return True
    prefix = "Profile "
    suffix = name[len(prefix):] if name.startswith(prefix) else ""
    return bool(suffix and suffix.isascii() and suffix.isdigit() and int(suffix) > 0)


def _read_private_generated_file(
        path: Path,
        deadline: float,
        max_bytes: int,
) -> bytes:
    """Read a file created beneath our already-private temp profile."""
    _remaining(deadline)
    before = path.lstat()
    if _is_link_like(path, before) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"unsafe profile input: {path}")
    if before.st_size < 0 or before.st_size > max_bytes:
        raise ValueError(f"profile input too large: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.fspath(path), flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"profile input changed type: {path}")
        if (
            getattr(before, "st_ino", None)
            and getattr(opened, "st_ino", None)
            and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError(f"profile input changed during open: {path}")
        if _is_link_like(path, opened) or opened.st_size > max_bytes:
            raise ValueError(f"unsafe profile input: {path}")
        chunks = []
        total = 0
        while True:
            _remaining(deadline)
            chunk = os.read(fd, min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"profile input too large: {path}")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


class _OpenedProfileFile:
    """One source file kept open from authority validation through copying."""

    def __init__(self, handle: int, size: int, *, windows: bool):
        self.handle = int(handle)
        self.size = int(size)
        self.windows = bool(windows)
        self._closed = False

    def read(self, deadline: float, max_bytes: int) -> bytes:
        if self._closed:
            raise ValueError("profile source handle is closed")
        if self.size < 0 or self.size > max_bytes:
            raise ValueError("profile input too large")
        if self.windows:
            return _read_windows_profile_handle(
                self.handle,
                deadline,
                max_bytes,
            )
        chunks = []
        total = 0
        while True:
            _remaining(deadline)
            chunk = os.read(
                self.handle,
                min(64 * 1024, max_bytes - total + 1),
            )
            _remaining(deadline)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("profile input too large")
            chunks.append(chunk)
        after = os.fstat(self.handle)
        if not stat.S_ISREG(after.st_mode) or after.st_size != self.size:
            raise ValueError("profile input changed while open")
        return b"".join(chunks)

    def close(self) -> None:
        if self._closed:
            return
        if self.windows:
            _durably_close_windows_handle(self.handle)
        else:
            os.close(self.handle)
        self.handle = 0
        self._closed = True


def _posix_owned_component(metadata, *, final: bool) -> None:
    current_uid = os.geteuid()
    if final:
        if metadata.st_uid != current_uid:
            raise PermissionError("profile root is not owned by current user")
    elif metadata.st_uid not in (0, current_uid):
        raise PermissionError("profile ancestor has an untrusted owner")
    writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    sticky_root = (
        not final
        and metadata.st_uid == 0
        and bool(metadata.st_mode & stat.S_ISVTX)
    )
    if writable and not sticky_root:
        authority = "source" if final else "ancestor"
        raise PermissionError(
            f"profile {authority} is writable by another user"
        )


def _open_posix_directory_absolute(path: Path, deadline: float) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute():
        raise ValueError("profile root must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(os.path.sep, flags)
    try:
        root_metadata = os.fstat(current_fd)
        _posix_owned_component(root_metadata, final=False)
        parts = absolute.parts[1:]
        for index, component in enumerate(parts):
            _remaining(deadline)
            next_fd = os.open(
                component,
                flags,
                dir_fd=current_fd,
            )
            try:
                _remaining(deadline)
                metadata = os.fstat(next_fd)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError("profile path component is not a directory")
                _posix_owned_component(
                    metadata,
                    final=index == len(parts) - 1,
                )
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


class _PosixProfileRoot:
    def __init__(self, path: Path, deadline: float):
        self.handle = _open_posix_directory_absolute(path, deadline)

    def profile_names(self, deadline: float) -> tuple[str, ...]:
        _remaining(deadline)
        names = []
        scanned = 0
        with os.scandir(self.handle) as entries:
            for entry in entries:
                _remaining(deadline)
                scanned += 1
                if scanned > _MAX_PROFILE_DIRECTORY_ENTRIES:
                    raise ValueError("profile directory limit exceeded")
                if _allowed_profile_name(entry.name):
                    names.append(entry.name)
                    if len(names) > _MAX_PROFILE_FILES:
                        raise ValueError("profile directory limit exceeded")
        _remaining(deadline)
        result = tuple(sorted(names))
        _remaining(deadline)
        return result

    def open_file(
            self,
            parts: tuple[str, ...],
            deadline: float,
    ) -> _OpenedProfileFile | None:
        if not parts or any(
                not part or part in (".", "..") or "/" in part
                for part in parts):
            raise ValueError("invalid profile-relative path")
        directory_fd = os.dup(self.handle)
        flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags_dir |= getattr(os, "O_NOFOLLOW", 0)
        try:
            for component in parts[:-1]:
                _remaining(deadline)
                try:
                    child_fd = os.open(
                        component,
                        flags_dir,
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError:
                    return None
                try:
                    _remaining(deadline)
                    metadata = os.fstat(child_fd)
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise PermissionError(
                            "unsafe profile directory component"
                        )
                    _posix_owned_component(metadata, final=True)
                except BaseException:
                    os.close(child_fd)
                    raise
                os.close(directory_fd)
                directory_fd = child_fd
            flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                file_fd = os.open(
                    parts[-1],
                    flags_file,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return None
            try:
                _remaining(deadline)
                metadata = os.fstat(file_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise PermissionError(
                        "unsafe profile file owner or type"
                    )
                _posix_owned_component(metadata, final=True)
            except BaseException:
                os.close(file_fd)
                raise
            return _OpenedProfileFile(
                file_fd,
                metadata.st_size,
                windows=False,
            )
        finally:
            os.close(directory_fd)

    def close(self) -> None:
        os.close(self.handle)


def _windows_handle_attributes(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    class FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    info = FILE_BASIC_INFO()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    get_info.restype = wintypes.BOOL
    if not get_info(
            wintypes.HANDLE(handle),
            0,
            ctypes.byref(info),
            ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.FileAttributes)


def _windows_network_capability_name(browser_name: str) -> str:
    names = {
        "chrome": "lpacChromeStableNetworkSandbox",
        "brave": "lpacChromeStableNetworkSandbox",
        "edge": "lpacChromeNetworkSandbox",
    }
    try:
        return names[browser_name]
    except KeyError as exc:
        raise ValueError("unsupported Windows browser capability") from exc


def _derive_windows_capability_sid(capability_name: str) -> str:
    """Derive one named Windows capability SID and free all native outputs."""
    import ctypes
    from ctypes import wintypes

    kernelbase = ctypes.WinDLL("kernelbase", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    derive = kernelbase.DeriveCapabilitySidsFromName
    derive.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(wintypes.DWORD),
    ]
    derive.restype = wintypes.BOOL
    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    convert.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    group_sids = ctypes.POINTER(ctypes.c_void_p)()
    group_count = wintypes.DWORD()
    capability_sids = ctypes.POINTER(ctypes.c_void_p)()
    capability_count = wintypes.DWORD()

    def free_sid_array(array, count: int) -> None:
        if not bool(array):
            return
        for index in range(count):
            sid_value = _raw_windows_handle(array[index])
            if sid_value:
                local_free(ctypes.c_void_p(sid_value))
        local_free(ctypes.cast(array, ctypes.c_void_p))

    try:
        if not derive(
                capability_name,
                ctypes.byref(group_sids),
                ctypes.byref(group_count),
                ctypes.byref(capability_sids),
                ctypes.byref(capability_count)):
            raise ctypes.WinError(ctypes.get_last_error())
        if int(capability_count.value) != 1 or not bool(capability_sids):
            raise RuntimeError(
                "Windows capability name did not derive exactly one SID"
            )
        sid_value = _raw_windows_handle(capability_sids[0])
        if not sid_value:
            raise RuntimeError("Windows capability derivation returned null SID")
        sid_text = ctypes.c_void_p()
        if not convert(
                ctypes.c_void_p(sid_value),
                ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = ctypes.wstring_at(sid_text)
        finally:
            if sid_text.value:
                local_free(sid_text)
        if not value.startswith("S-1-15-3-"):
            raise RuntimeError("Windows capability derivation returned wrong SID")
        return value
    finally:
        free_sid_array(group_sids, int(group_count.value))
        free_sid_array(capability_sids, int(capability_count.value))


def _windows_profile_mutation_sids(
        parts: tuple[str, ...],
        network_capability_sid: str | None,
) -> frozenset[str]:
    if (
        network_capability_sid
        and len(parts) >= 2
        and _allowed_profile_name(parts[0])
        and parts[1] == "Network"
    ):
        return frozenset({network_capability_sid})
    return frozenset()


def _require_windows_profile_owner(
        handle: int,
        *,
        final: bool,
        allow_profile_root_system_owner: bool = False,
) -> None:
    try:
        from .secureio import (
            _windows_current_token_owner_sid,
            _windows_current_user_sid,
            _windows_handle_owner_sid,
        )
    except ImportError:
        from secureio import (
            _windows_current_token_owner_sid,
            _windows_current_user_sid,
            _windows_handle_owner_sid,
        )

    owner = _windows_handle_owner_sid(handle)
    current = _windows_current_user_sid()
    if owner == current:
        return
    # Also accept the SID this token stamps as the default OWNER on objects it creates. Under an
    # elevated Administrators token that is the Administrators group, so a profile directory this
    # very process created is not owned by the plain user SID and would otherwise be rejected as
    # "not owned by current user" (observed on CI runners, which run elevated). This does not
    # accept another principal's data: a directory created by a different, non-elevated user
    # carries THAT user's SID, never this token's owner default. Mirrors the same allowance in
    # secureio._require_windows_current_owner.
    if owner == _windows_current_token_owner_sid():
        return
    if final:
        if (
            allow_profile_root_system_owner
            and owner in {
                "S-1-5-18",  # LocalSystem
                "S-1-5-32-544",  # Builtin Administrators
            }
        ):
            return
        raise PermissionError("profile source is not owned by current user")
    trusted_system_owners = {
        "S-1-5-18",  # LocalSystem
        "S-1-5-32-544",  # Builtin Administrators
        # Exact owner observed on the Windows system-drive root.
        (
            "S-1-5-80-956008885-3418522649-1831038044-"
            "1853292631-2271478464"
        ),
    }
    if owner in trusted_system_owners:
        return
    raise PermissionError("profile ancestor has a foreign owner")


def _windows_profile_trusted_sids() -> set[str]:
    try:
        from .secureio import _windows_current_user_sid
    except ImportError:
        from secureio import _windows_current_user_sid

    return {
        _windows_current_user_sid(),
        "S-1-5-18",  # LocalSystem
        "S-1-5-32-544",  # Builtin Administrators
    }


def _windows_profile_dacl_aces(
        handle: int,
) -> tuple[tuple[int, int, int, str], ...] | None:
    """Read active DACL ACE data from an already-open profile handle."""
    import ctypes
    from ctypes import wintypes

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
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
        0x4,  # DACL_SECURITY_INFORMATION
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        if not dacl.value:
            return None
        acl_info = ACL_SIZE_INFORMATION()
        get_acl_info = advapi32.GetAclInformation
        get_acl_info.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_int,
        ]
        get_acl_info.restype = wintypes.BOOL
        if not get_acl_info(
                dacl,
                ctypes.byref(acl_info),
                ctypes.sizeof(acl_info),
                2):  # AclSizeInformation
            raise ctypes.WinError(ctypes.get_last_error())
        get_ace = advapi32.GetAce
        get_ace.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        get_ace.restype = wintypes.BOOL
        convert_sid = advapi32.ConvertSidToStringSidW
        convert_sid.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        convert_sid.restype = wintypes.BOOL
        is_valid_sid = advapi32.IsValidSid
        is_valid_sid.argtypes = [ctypes.c_void_p]
        is_valid_sid.restype = wintypes.BOOL
        get_sid_length = advapi32.GetLengthSid
        get_sid_length.argtypes = [ctypes.c_void_p]
        get_sid_length.restype = wintypes.DWORD
        allow_types = {0, 5, 9, 11}
        non_allow_types = {
            1, 2, 3, 6, 7, 8, 10, 12, 13, 14, 15, 16,
            17, 18, 19, 20, 21,
        }
        entries: list[tuple[int, int, int, str]] = []
        for index in range(int(acl_info.AceCount)):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not ace.value:
                raise OSError("Windows DACL returned a null ACE")
            header = ctypes.cast(
                ace,
                ctypes.POINTER(ACE_HEADER),
            ).contents
            ace_type = int(header.AceType)
            ace_flags = int(header.AceFlags)
            ace_size = int(header.AceSize)
            if ace_size < 8:
                raise OSError("Windows DACL contains a truncated ACE")
            access_mask = int(
                ctypes.c_uint32.from_address(ace.value + 4).value
            )
            if ace_type == 4:  # ACCESS_ALLOWED_COMPOUND_ACE_TYPE
                entries.append((
                    ace_type,
                    ace_flags,
                    access_mask,
                    "<unparsed-compound-allow>",
                ))
                continue
            if ace_type not in allow_types:
                if ace_type in non_allow_types:
                    continue
                raise OSError(
                    f"Windows DACL contains unknown ACE type {ace_type}"
                )
            sid_offset = 8
            if ace_type in (5, 11):
                if ace_size < 12:
                    raise OSError(
                        "Windows DACL contains a truncated object ACE"
                    )
                object_flags = int(
                    ctypes.c_uint32.from_address(ace.value + 8).value
                )
                sid_offset = 12
                if object_flags & 0x1:
                    sid_offset += 16
                if object_flags & 0x2:
                    sid_offset += 16
            sid_header_size = 8
            if ace_size - sid_offset < sid_header_size:
                raise OSError("Windows DACL ACE has a truncated SID")
            subauthority_count = int(
                ctypes.c_ubyte.from_address(
                    ace.value + sid_offset + 1
                ).value
            )
            claimed_sid_length = sid_header_size + 4 * subauthority_count
            if sid_offset + claimed_sid_length > ace_size:
                raise OSError("Windows DACL ACE SID exceeds the ACE")
            sid = ctypes.c_void_p(ace.value + sid_offset)
            if not is_valid_sid(sid):
                raise OSError("Windows DACL ACE has an invalid SID")
            sid_length = int(get_sid_length(sid))
            if (
                sid_length != claimed_sid_length
                or sid_offset + sid_length > ace_size
            ):
                raise OSError("Windows DACL ACE SID exceeds the ACE")
            sid_text = ctypes.c_void_p()
            if not convert_sid(sid, ctypes.byref(sid_text)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                entries.append((
                    ace_type,
                    ace_flags,
                    access_mask,
                    ctypes.wstring_at(sid_text),
                ))
            finally:
                kernel32.LocalFree(sid_text)
        return tuple(entries)
    finally:
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def _require_windows_profile_dacl(
        handle: int,
        *,
        allowed_mutation_sids: frozenset[str] = frozenset(),
) -> None:
    entries = _windows_profile_dacl_aces(handle)
    if entries is None:
        raise PermissionError("profile source DACL grants unrestricted access")
    trusted = _windows_profile_trusted_sids()
    mutation_rights = (
        0x40000000  # GENERIC_WRITE
        | 0x10000000  # GENERIC_ALL
        | 0x01000000  # ACCESS_SYSTEM_SECURITY
        | 0x00010000  # DELETE
        | 0x00040000  # WRITE_DAC
        | 0x00080000  # WRITE_OWNER
        | 0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
        | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
        | 0x00000010  # FILE_WRITE_EA
        | 0x00000040  # FILE_DELETE_CHILD
        | 0x00000100  # FILE_WRITE_ATTRIBUTES
    )
    for _ace_type, ace_flags, access_mask, sid in entries:
        if ace_flags & 0x08:  # INHERIT_ONLY_ACE
            continue
        if not access_mask & mutation_rights:
            continue
        if sid in trusted or sid in allowed_mutation_sids:
            continue
        raise PermissionError(
            "profile source DACL grants mutation rights to a foreign SID"
        )


def _validate_windows_profile_handle(
        handle: int,
        *,
        directory: bool,
        require_current_owner: bool,
        allow_profile_root_system_owner: bool = False,
        allowed_mutation_sids: frozenset[str] = frozenset(),
) -> None:
    attributes = _windows_handle_attributes(handle)
    if attributes & _REPARSE_POINT_FLAG:
        raise ValueError("profile path contains a reparse point")
    is_directory = bool(attributes & 0x10)
    if is_directory != directory:
        raise ValueError("profile handle has an unexpected type")
    _require_windows_profile_owner(
        handle,
        final=require_current_owner,
        allow_profile_root_system_owner=allow_profile_root_system_owner,
    )
    if require_current_owner:
        _require_windows_profile_dacl(
            handle,
            allowed_mutation_sids=allowed_mutation_sids,
        )


def _nt_open_relative(
        parent_handle: int,
        name: str,
        *,
        directory: bool,
        require_current_owner: bool,
        allow_profile_root_system_owner: bool = False,
        allowed_mutation_sids: frozenset[str] = frozenset(),
) -> int | None:
    import ctypes
    from ctypes import wintypes

    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("invalid Windows profile path component")

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class IO_STATUS_BLOCK_UNION(ctypes.Union):
        _fields_ = [
            ("Status", ctypes.c_long),
            ("Pointer", ctypes.c_void_p),
        ]

    class IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [
            ("u", IO_STATUS_BLOCK_UNION),
            ("Information", ctypes.c_size_t),
        ]

    buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    unicode_name = UNICODE_STRING(
        encoded_length,
        encoded_length + 2,
        ctypes.cast(buffer, wintypes.LPWSTR),
    )
    attributes = OBJECT_ATTRIBUTES(
        ctypes.sizeof(OBJECT_ATTRIBUTES),
        wintypes.HANDLE(parent_handle),
        ctypes.pointer(unicode_name),
        0x40,  # OBJ_CASE_INSENSITIVE
        None,
        None,
    )
    status_block = IO_STATUS_BLOCK()
    opened = wintypes.HANDLE()
    desired_access = 0x00020000 | 0x00100000 | 0x80 | 0x1
    create_options = 0x20 | 0x00200000
    create_options |= 0x1 if directory else 0x40
    nt_create = ctypes.WinDLL("ntdll").NtCreateFile
    nt_create.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(OBJECT_ATTRIBUTES),
        ctypes.POINTER(IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    nt_create.restype = ctypes.c_long
    status = int(nt_create(
        ctypes.byref(opened),
        desired_access,
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        0,
        0x1 | 0x2 | 0x4,
        1,  # FILE_OPEN
        create_options,
        None,
        0,
    ))
    if status < 0:
        code = status & 0xFFFFFFFF
        if code in (0xC0000034, 0xC000003A):
            return None
        raise OSError(f"NtCreateFile failed: 0x{code:08x}")
    handle = int(opened.value)
    try:
        _validate_windows_profile_handle(
            handle,
            directory=directory,
            require_current_owner=require_current_owner,
            allow_profile_root_system_owner=(
                allow_profile_root_system_owner
            ),
            allowed_mutation_sids=allowed_mutation_sids,
        )
        return handle
    except BaseException:
        _durably_close_windows_handle(handle)
        raise


def _open_windows_profile_root(path: Path, deadline: float) -> int:
    import ctypes
    from ctypes import wintypes

    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute() or not absolute.drive or str(absolute).startswith("\\\\"):
        raise ValueError("Windows profile root must be a local absolute path")
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        metadata = current.lstat()
        if _is_link_like(current, metadata):
            raise ValueError("linked profile ancestor rejected")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    current_handle = create_file(
        absolute.anchor,
        0x00020000 | 0x00100000 | 0x80 | 0x1,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if current_handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    current_handle = int(current_handle)
    try:
        _validate_windows_profile_handle(
            current_handle,
            directory=True,
            require_current_owner=False,
        )
        for index, component in enumerate(absolute.parts[1:]):
            _remaining(deadline)
            child = _nt_open_relative(
                current_handle,
                component,
                directory=True,
                require_current_owner=index == len(absolute.parts[1:]) - 1,
                allow_profile_root_system_owner=(
                    index == len(absolute.parts[1:]) - 1
                ),
            )
            try:
                _remaining(deadline)
                if child is None:
                    raise FileNotFoundError(path)
            except BaseException:
                if child is not None:
                    _durably_close_windows_handle(child)
                raise
            _durably_close_windows_handle(current_handle)
            current_handle = child
        return current_handle
    except BaseException:
        _durably_close_windows_handle(current_handle)
        raise


def _read_windows_profile_handle(
        handle: int,
        deadline: float,
        max_bytes: int,
) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    size = ctypes.c_longlong()
    if not kernel32.GetFileSizeEx(wintypes.HANDLE(handle), ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    if size.value < 0 or size.value > max_bytes:
        raise ValueError("profile input too large")
    chunks = []
    total = 0
    while total < size.value:
        _remaining(deadline)
        requested = min(64 * 1024, size.value - total)
        buffer = ctypes.create_string_buffer(requested)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(
                wintypes.HANDLE(handle),
                buffer,
                requested,
                ctypes.byref(read),
                None):
            raise ctypes.WinError(ctypes.get_last_error())
        _remaining(deadline)
        if read.value == 0:
            raise OSError("short read from profile input")
        chunks.append(buffer.raw[:read.value])
        total += read.value
    after_size = ctypes.c_longlong()
    if not kernel32.GetFileSizeEx(
            wintypes.HANDLE(handle),
            ctypes.byref(after_size)):
        raise ctypes.WinError(ctypes.get_last_error())
    if after_size.value != size.value:
        raise ValueError("profile input changed while open")
    return b"".join(chunks)


class _WindowsProfileRoot:
    def __init__(
            self,
            path: Path,
            deadline: float,
            browser_name: str | None = None):
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.network_capability_sid = (
            _derive_windows_capability_sid(
                _windows_network_capability_name(browser_name)
            )
            if browser_name is not None
            else None
        )
        self.handle = _open_windows_profile_root(self.path, deadline)

    def profile_names(self, _deadline: float) -> tuple[str, ...]:
        return ("Default",) + tuple(f"Profile {index}" for index in range(1, 51))

    def open_file(
            self,
            parts: tuple[str, ...],
            deadline: float,
    ) -> _OpenedProfileFile | None:
        directory_handle = self.handle
        borrowed = True
        lexical = self.path
        opened_parts: tuple[str, ...] = ()
        try:
            for component in parts[:-1]:
                _remaining(deadline)
                lexical = lexical / component
                opened_parts = (*opened_parts, component)
                try:
                    metadata = lexical.lstat()
                except FileNotFoundError:
                    return None
                if _is_link_like(lexical, metadata):
                    raise ValueError("linked profile component rejected")
                child = _nt_open_relative(
                    directory_handle,
                    component,
                    directory=True,
                    require_current_owner=True,
                    allowed_mutation_sids=_windows_profile_mutation_sids(
                        opened_parts,
                        getattr(self, "network_capability_sid", None),
                    ),
                )
                try:
                    _remaining(deadline)
                    if child is None:
                        return None
                except BaseException:
                    if child is not None:
                        import ctypes

                        _durably_close_windows_handle(child)
                    raise
                if not borrowed:
                    _durably_close_windows_handle(directory_handle)
                directory_handle = child
                borrowed = False
            file_handle = _nt_open_relative(
                directory_handle,
                parts[-1],
                directory=False,
                require_current_owner=True,
                allowed_mutation_sids=_windows_profile_mutation_sids(
                    parts,
                    getattr(self, "network_capability_sid", None),
                ),
            )
            if file_handle is None:
                _remaining(deadline)
                return None
            import ctypes
            from ctypes import wintypes

            try:
                _remaining(deadline)
                size = ctypes.c_longlong()
                if not ctypes.WinDLL(
                        "kernel32",
                        use_last_error=True).GetFileSizeEx(
                            wintypes.HANDLE(file_handle),
                            ctypes.byref(size)):
                    raise ctypes.WinError(ctypes.get_last_error())
                result = _OpenedProfileFile(
                    file_handle,
                    size.value,
                    windows=True,
                )
            except BaseException:
                _durably_close_windows_handle(file_handle)
                raise
            return result
        finally:
            if not borrowed:
                _durably_close_windows_handle(directory_handle)

    def close(self) -> None:
        _durably_close_windows_handle(self.handle)
        self.handle = 0


def _open_profile_root(
        path: Path,
        deadline: float,
        *,
        browser_name: str | None = None):
    if sys.platform.startswith("win"):
        return _WindowsProfileRoot(
            path,
            deadline,
            browser_name=browser_name,
        )
    if sys.platform == "darwin" or sys.platform.startswith((
            "linux", "freebsd", "openbsd", "netbsd")):
        return _PosixProfileRoot(path, deadline)
    raise RuntimeError(f"unsupported profile snapshot platform: {sys.platform}")


def _create_private_directory(path: Path) -> bool:
    """Create one directory owner-only from its first observable instant."""
    path = Path(path)
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes
        try:
            from .secureio import (
                _free_windows_descriptor,
                _windows_private_descriptor,
            )
        except ImportError:
            from secureio import (
                _free_windows_descriptor,
                _windows_private_descriptor,
            )

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL),
            ]

        descriptor = _windows_private_descriptor()
        attributes = SECURITY_ATTRIBUTES(
            ctypes.sizeof(SECURITY_ATTRIBUTES),
            ctypes.c_void_p(descriptor),
            False,
        )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_directory = kernel32.CreateDirectoryW
        create_directory.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(SECURITY_ATTRIBUTES),
        ]
        create_directory.restype = wintypes.BOOL
        try:
            if create_directory(os.fspath(path), ctypes.byref(attributes)):
                return True
            error_code = ctypes.get_last_error()
            if error_code == 183:  # ERROR_ALREADY_EXISTS
                return False
            raise ctypes.WinError(error_code)
        finally:
            _free_windows_descriptor(descriptor)
    try:
        path.mkdir(mode=0o700)
        return True
    except FileExistsError:
        return False


def _mkdir_private(path: Path, *, root: Path | None = None) -> None:
    path = Path(path)
    if root is None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        _secure_private_directory(path)
        return
    root = Path(root)
    relative = path.relative_to(root)
    current = root
    for component in (None, *relative.parts):
        if component is not None:
            current = current / component
            _create_private_directory(current)
        _secure_private_directory(current)


def _remove_snapshot_outputs(root: Path, copied: list[Path]) -> None:
    for path in reversed(copied):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
    try:
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass
    except OSError:
        pass


#: Why the last profile snapshot failed. The snapshot is the only layer that sees the real
#: exception; everything above it collapses to a bare False/None.
_LAST_SNAPSHOT_FAILURE: str | None = None


def _record_snapshot_failure(error: BaseException | None) -> None:
    global _LAST_SNAPSHOT_FAILURE
    _LAST_SNAPSHOT_FAILURE = None if error is None else f"{type(error).__name__}: {error}"


def last_snapshot_failure() -> str | None:
    """The reason the most recent profile snapshot failed, or None."""
    return _LAST_SNAPSHOT_FAILURE


def _snapshot_profile(
        src_root,
        dst_root,
        *,
        browser_name: str | None = None,
        deadline: float | None = None,
        max_files: int = _MAX_PROFILE_FILES,
        max_bytes: int = _MAX_PROFILE_BYTES,
) -> bool:
    """Copy just the cookie-bearing subset of a Chromium User Data dir into
    dst_root (a fresh temp dir). Copying a snapshot avoids the SingletonLock that
    stops Chrome from opening a profile already in use, while still carrying the
    real login state (decryption key in 'Local State' + the per-profile Cookies
    DB). Returns True if any cookie store was copied."""
    _record_snapshot_failure(None)
    src_root = Path(src_root)
    dst_root = Path(dst_root)
    deadline = float("inf") if deadline is None else float(deadline)
    if max_files < 1 or max_bytes < 1:
        return False
    candidates: list[tuple[_OpenedProfileFile, Path, bool]] = []
    copied: list[Path] = []
    profile_root = None
    try:
        _remaining(deadline)
        _safe_profile_path(dst_root, dst_root, leaf_file=False)
        profile_root = _open_profile_root(
            src_root,
            deadline,
            browser_name=browser_name,
        )
        total_bytes = 0

        def add_candidate(
                parts: tuple[str, ...],
                relative: Path,
                is_main_cookie: bool,
        ) -> None:
            nonlocal total_bytes
            _remaining(deadline)
            opened = profile_root.open_file(parts, deadline)
            if opened is None:
                return
            candidates.append((opened, relative, is_main_cookie))
            total_bytes += opened.size
            if len(candidates) > max_files:
                raise ValueError("profile snapshot file limit exceeded")
            if opened.size < 0 or total_bytes > max_bytes:
                raise ValueError("profile snapshot byte limit exceeded")

        add_candidate(("Local State",), Path("Local State"), False)
        for profile_name in profile_root.profile_names(deadline):
            _remaining(deadline)
            if not _allowed_profile_name(profile_name):
                continue
            for parent_parts in (
                    (profile_name, "Network"),
                    (profile_name,)):
                for filename in _COOKIE_FILENAMES:
                    relative_parts = (*parent_parts, filename)
                    add_candidate(
                        relative_parts,
                        Path(*relative_parts),
                        filename == "Cookies",
                    )
        main_cookie_copied = False
        remaining_bytes = max_bytes
        for opened, relative, is_main_cookie in candidates:
            data = opened.read(deadline, remaining_bytes)
            remaining_bytes -= len(data)
            target = dst_root / relative
            _mkdir_private(target.parent, root=dst_root)
            atomic_replace_private(target, data)
            copied.append(target)
            main_cookie_copied = main_cookie_copied or is_main_cookie
        if not main_cookie_copied:
            _remove_snapshot_outputs(dst_root, copied)
            return False
        return True
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        # This is where the cause dies. Callers only ever see False -> None -> "no cookies
        # found", which the web UI then reports as "sign in fully and try again" -- advice that
        # cannot help when the real answer is that the profile directory grants write access to a
        # foreign SID, or that a file is locked. Record it so the caller can say what happened.
        _record_snapshot_failure(error)
        _remove_snapshot_outputs(dst_root, copied)
        return False
    finally:
        for opened, _relative, _main in candidates:
            try:
                opened.close()
            except Exception:
                pass
        if profile_root is not None:
            try:
                profile_root.close()
            except Exception:
                pass


def _read_http_json(
        url: str,
        *,
        deadline: float,
        max_bytes: int = _MAX_HTTP_BYTES,
):
    if max_bytes < 1:
        raise ValueError("invalid HTTP body limit")
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or not (1 <= parsed.port <= 65535)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("untrusted debugger HTTP endpoint")
    request_path = parsed.path or "/"
    sock = socket.create_connection(
        ("127.0.0.1", parsed.port),
        timeout=_remaining(deadline),
    )
    try:
        peer = sock.getpeername()
        if peer[0] != "127.0.0.1" or int(peer[1]) != parsed.port:
            raise ValueError("debugger HTTP peer is not owned loopback")
        request = (
            f"GET {request_path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{parsed.port}\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        _set_socket_deadline(sock, deadline)
        sock.sendall(request)
        _remaining(deadline)
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            if len(header) >= _MAX_WS_HEADER_BYTES:
                raise ValueError("debugger HTTP headers too large")
            header.extend(_recv_exact(sock, 1, deadline))
        try:
            lines = header[:-4].decode("latin-1").split("\r\n")
            status_parts = lines[0].split(" ", 2)
            if (
                len(status_parts) < 2
                or status_parts[0] != "HTTP/1.1"
                or status_parts[1] != "200"
            ):
                raise ValueError
            headers: dict[str, list[str]] = {}
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                if not separator or not name.strip():
                    raise ValueError
                headers.setdefault(name.strip().lower(), []).append(value.strip())
            lengths = headers.get("content-length", [])
            if len(lengths) != 1 or not lengths[0].isascii():
                raise ValueError
            content_length = int(lengths[0], 10)
            if content_length < 0:
                raise ValueError
            if "transfer-encoding" in headers:
                raise ValueError
        except (IndexError, ValueError) as exc:
            raise ValueError("malformed debugger HTTP response") from exc
        if content_length > max_bytes:
            raise ValueError("debugger HTTP response too large")
        body = _recv_exact(sock, content_length, deadline)
        _remaining(deadline)
    finally:
        sock.close()
    if len(body) > max_bytes:
        raise ValueError("debugger HTTP response too large")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed debugger JSON response") from exc


def _active_port_state(
        path: Path,
        deadline: float,
) -> tuple[int, str, tuple[int, int, int, int]]:
    _remaining(deadline)
    metadata = path.lstat()
    if _is_link_like(path, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("unsafe DevToolsActivePort")
    data = _read_private_generated_file(path, deadline, 4096)
    try:
        separator_count = data.count(b"\n")
        if separator_count > 1:
            raise ValueError
        decoded = data.decode("ascii")
        if separator_count == 0:
            if not decoded:
                raise _EndpointNotReady
            if (
                not decoded.isascii()
                or not decoded.isdigit()
                or len(decoded) > 5
                or (len(decoded) == 5 and int(decoded, 10) > 65535)
            ):
                raise ValueError
            raise _EndpointNotReady
        port_text, ws_path = decoded.split("\n")
        if port_text.endswith("\r"):
            port_text = port_text[:-1]
        if (
            not port_text
            or not port_text.isascii()
            or not port_text.isdigit()
        ):
            raise ValueError
        port = int(port_text, 10)
        if not (1 <= port <= 65535):
            raise ValueError
        websocket_prefix = "/devtools/browser/"
        if websocket_prefix.startswith(ws_path):
            raise _EndpointNotReady
    except _EndpointNotReady:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(
            "malformed debugger port or DevToolsActivePort"
        ) from exc
    parsed_path = urlparse(ws_path)
    if (
        parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.query
        or parsed_path.fragment
        or not ws_path.startswith("/devtools/browser/")
        or ws_path.count("/") != 3
        or not ws_path.removeprefix("/devtools/browser/")
    ):
        raise ValueError("invalid debugger websocket path")
    signature = (
        int(getattr(metadata, "st_dev", 0)),
        int(getattr(metadata, "st_ino", 0)),
        int(getattr(metadata, "st_mtime_ns", 0)),
        int(metadata.st_size),
    )
    return port, ws_path, signature


def _discover_owned_endpoint(
        userdir,
        *,
        proc,
        deadline: float,
) -> tuple[str, int, str]:
    if proc.poll() is not None:
        raise RuntimeError("browser exited before debugger discovery")
    active_path = Path(userdir) / "DevToolsActivePort"
    port, ws_path, signature = _active_port_state(active_path, deadline)
    version = _read_http_json(
        f"http://127.0.0.1:{port}/json/version",
        deadline=deadline,
    )
    if proc.poll() is not None:
        raise RuntimeError("browser exited during debugger discovery")
    after_port, after_path, after_signature = _active_port_state(
        active_path,
        deadline,
    )
    if (
        (after_port, after_path) != (port, ws_path)
        or after_signature != signature
    ):
        raise ValueError("DevToolsActivePort changed during discovery")
    if not isinstance(version, dict):
        raise ValueError("malformed debugger version response")
    advertised = version.get("webSocketDebuggerUrl")
    if not isinstance(advertised, str):
        raise ValueError("debugger endpoint missing")
    parsed = urlparse(advertised)
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("debugger endpoint is not the owned loopback endpoint")
    advertised_path = parsed.path
    if (
        not advertised_path.startswith("/devtools/browser/")
        or advertised_path.count("/") != 3
        or not advertised_path.removeprefix("/devtools/browser/")
    ):
        raise ValueError("debugger endpoint is not the owned loopback endpoint")
    if advertised_path != ws_path:
        raise _EndpointNotReady
    return "127.0.0.1", port, ws_path


def _wait_for_owned_endpoint(
        userdir,
        *,
        proc,
        deadline: float,
) -> tuple[str, int, str]:
    while True:
        if proc.poll() is not None:
            raise RuntimeError("browser exited before debugger endpoint was ready")
        try:
            return _discover_owned_endpoint(
                userdir,
                proc=proc,
                deadline=deadline,
            )
        except (FileNotFoundError, _EndpointNotReady, OSError):
            if proc.poll() is not None:
                raise RuntimeError(
                    "browser exited before debugger endpoint was ready"
                )
            remaining = _remaining(deadline)
            time.sleep(min(0.05, remaining))


def _set_socket_deadline(sock, deadline: float) -> None:
    sock.settimeout(_remaining(deadline))


def _recv_exact(sock, count: int, deadline: float) -> bytes:
    output = bytearray()
    while len(output) < count:
        _set_socket_deadline(sock, deadline)
        chunk = sock.recv(count - len(output))
        if not chunk:
            raise ValueError("websocket closed mid-message")
        output.extend(chunk)
    return bytes(output)


def _ws_handshake(
        host,
        port,
        path,
        *,
        deadline: float | None = None,
        max_header_bytes: int = _MAX_WS_HEADER_BYTES,
):
    deadline = (
        time.monotonic() + 10.0
        if deadline is None
        else float(deadline)
    )
    if host != "127.0.0.1" or not (1 <= int(port) <= 65535):
        raise ValueError("untrusted websocket endpoint")
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
           f"Sec-WebSocket-Version: 13\r\n\r\n")
    sock = socket.create_connection(
        (host, port),
        timeout=_remaining(deadline),
    )
    try:
        peer = sock.getpeername()
        if peer[0] != "127.0.0.1" or int(peer[1]) != int(port):
            raise ValueError("websocket peer is not the owned loopback endpoint")
        _set_socket_deadline(sock, deadline)
        sock.sendall(req.encode("ascii"))
        response = bytearray()
        while not response.endswith(b"\r\n\r\n"):
            if len(response) >= max_header_bytes:
                raise ValueError("websocket handshake too large")
            response.extend(_recv_exact(sock, 1, deadline))
        try:
            lines = response[:-4].decode("latin-1").split("\r\n")
            # Validate the STATUS CODE, not the reason phrase. RFC 6455 4.1 requires the client to
            # check for 101; the phrase after it is arbitrary text. Demanding the literal
            # "Switching Protocols" rejected every real Chromium — Chrome 150, Edge 151 and Brave 151
            # all answer "HTTP/1.1 101 WebSocket Protocol Handshake" (verified against all three on
            # this host) — which made the entire CDP cookie tier fail closed wherever a browser is
            # actually installed. The anti-hijack gate is the Sec-WebSocket-Accept HMAC below and is
            # unchanged; the version is still pinned to HTTP/1.1.
            status = lines[0].split(" ", 2)
            if len(status) < 2 or status[0] != "HTTP/1.1" or status[1] != "101":
                raise ValueError
            headers: dict[str, list[str]] = {}
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                if not separator or not name.strip():
                    raise ValueError
                headers.setdefault(name.strip().lower(), []).append(value.strip())
            expected = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            upgrade = ",".join(headers.get("upgrade", [])).lower()
            connection_tokens = {
                token.strip().lower()
                for token in ",".join(headers.get("connection", [])).split(",")
            }
            accepts = headers.get("sec-websocket-accept", [])
            if (
                upgrade != "websocket"
                or "upgrade" not in connection_tokens
                or len(accepts) != 1
                or not hmac.compare_digest(accepts[0], expected)
            ):
                raise ValueError
        except (IndexError, ValueError):
            raise ValueError("invalid websocket handshake")
        return sock
    except BaseException:
        sock.close()
        raise


def _ws_send(s, text, *, deadline: float | None = None):
    deadline = (
        time.monotonic() + 10.0
        if deadline is None
        else float(deadline)
    )
    data = text.encode()
    hdr = bytearray([0x81])
    n = len(data)
    mask = os.urandom(4)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
    hdr += mask
    _set_socket_deadline(s, deadline)
    s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_recv(
        s,
        *,
        deadline: float | None = None,
        max_frame_bytes: int = _MAX_WS_FRAME_BYTES,
):
    deadline = (
        time.monotonic() + 10.0
        if deadline is None
        else float(deadline)
    )
    header = _recv_exact(s, 2, deadline)
    if (
        header[0] != 0x81
        or header[1] & 0x80
    ):
        raise ValueError("unsupported websocket frame")
    ln = header[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _recv_exact(s, 2, deadline))[0]
        if ln < 126:
            raise ValueError("non-canonical websocket frame length")
    elif ln == 127:
        ln = struct.unpack(">Q", _recv_exact(s, 8, deadline))[0]
        if ln < 65536 or ln & (1 << 63):
            raise ValueError("invalid websocket frame length")
    if ln > max_frame_bytes:
        raise ValueError("websocket frame too large")
    payload = _recv_exact(s, ln, deadline)
    _remaining(deadline)
    return payload.decode("utf-8", errors="strict")


def _secure_private_directory(path: Path) -> None:
    """Tighten and verify a freshly created credential-bearing directory."""
    path = Path(path)
    metadata = path.lstat()
    if _is_link_like(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("private temp path is not a real directory")
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes
        try:
            from .secureio import (
                _require_windows_current_owner,
                _tighten_windows_handle,
            )
        except ImportError:
            from secureio import (
                _require_windows_current_owner,
                _tighten_windows_handle,
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
        handle = create_file(
            os.fspath(path),
            0x00020000 | 0x00040000,  # READ_CONTROL | WRITE_DAC
            0x1 | 0x2 | 0x4,
            None,
            3,  # OPEN_EXISTING
            0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            _require_windows_current_owner(int(handle), path)
            _tighten_windows_handle(int(handle))
        finally:
            _durably_close_windows_handle(int(handle))
        verified = path.lstat()
        if _is_link_like(path, verified) or not stat.S_ISDIR(verified.st_mode):
            raise PermissionError("private temp directory changed during ACL setup")
        return
    if sys.platform == "darwin" or sys.platform.startswith((
            "linux", "freebsd", "openbsd", "netbsd")):
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(os.fspath(path), flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise PermissionError("private temp path changed type")
            if hasattr(os, "getuid") and opened.st_uid != os.getuid():
                raise PermissionError("private temp directory has a foreign owner")
            os.fchmod(fd, 0o700)
            verified = os.fstat(fd)
            if stat.S_IMODE(verified.st_mode) != 0o700:
                raise PermissionError("private temp directory mode is not owner-only")
        finally:
            os.close(fd)
        return
    raise RuntimeError(f"unsupported private-directory platform: {sys.platform}")


def _make_private_tempdir(*, base_dir=None) -> Path:
    if sys.platform.startswith("win"):
        base = (
            Path(base_dir)
            if base_dir is not None
            else ((_windows_known_folder("local_app_data") or Path()) / "Temp")
        )
        if (
            not base.is_absolute()
            or not base.exists()
            or _is_link_like(base)
            or not base.is_dir()
        ):
            raise PermissionError("trusted Windows temp root is unavailable")
        path = None
        for _attempt in range(128):
            candidate = base / f"ytf_cdp_{secrets.token_hex(16)}"
            if _create_private_directory(candidate):
                path = candidate
                break
        if path is None:
            raise FileExistsError("could not reserve a private CDP temp directory")
    else:
        path = Path(tempfile.mkdtemp(prefix="ytf_cdp_", dir=base_dir))
    try:
        _secure_private_directory(path)
        return path
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise


def _cleanup_private_tempdir(path, *, deadline: float) -> bool:
    """Remove one owned temp profile with bounded retries and no age sweep."""
    root = Path(path)
    for _attempt in range(40):
        try:
            _remaining(deadline)
        except TimeoutError:
            return False
        exists = root.exists()
        try:
            _remaining(deadline)
        except TimeoutError:
            return False
        if not exists:
            return True
        try:
            metadata = root.lstat()
            if _is_link_like(root, metadata) or not stat.S_ISDIR(metadata.st_mode):
                return False
            shutil.rmtree(root)
        except FileNotFoundError:
            try:
                _remaining(deadline)
            except TimeoutError:
                return False
        except OSError:
            pass
        try:
            _remaining(deadline)
        except TimeoutError:
            return False
        exists = root.exists()
        try:
            _remaining(deadline)
        except TimeoutError:
            return False
        if not exists:
            return True
        try:
            remaining = _remaining(deadline)
        except TimeoutError:
            return False
        time.sleep(min(0.05, remaining))
    exists = root.exists()
    try:
        _remaining(deadline)
    except TimeoutError:
        return False
    return not exists


def _trusted_taskkill() -> Path | None:
    if not sys.platform.startswith("win"):
        return None
    windows_root = _windows_known_folder("windows")
    if windows_root is None:
        return None
    return fixed_authority_executable(
        (windows_root,),
        windows_root / "System32" / "taskkill.exe",
        allow_internal_links=False,
        platform=sys.platform,
    )


class _WindowsLaunchCleanupError(RuntimeError):
    """Own raw launch handles when a suspended process may still be alive."""

    def __init__(
            self,
            process_handle: int,
            thread_handle: int,
            pid: int,
            job_handle: int = 0):
        super().__init__(
            "could not prove failed suspended Windows process was stopped"
        )
        self.process_handle = int(process_handle)
        self.thread_handle = int(thread_handle)
        self.pid = int(pid)
        self.job_handle = int(job_handle)
        self._cleanup_lock = threading.RLock()
        if self.process_handle or self.thread_handle or self.job_handle:
            _register_windows_cleanup(self)

    def cleanup(self, *, deadline: float) -> bool:
        """Retry bounded termination while retaining every unclosed handle."""
        import ctypes
        from ctypes import wintypes

        with self._cleanup_lock:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if self.job_handle:
                try:
                    terminate_job = kernel32.TerminateJobObject
                    terminate_job.argtypes = [
                        wintypes.HANDLE,
                        wintypes.UINT,
                    ]
                    terminate_job.restype = wintypes.BOOL
                    terminate_job(
                        wintypes.HANDLE(self.job_handle),
                        1,
                    )
                except Exception:
                    pass
            stopped = (
                not self.process_handle
                or _stop_created_windows_process(
                    kernel32,
                    self.process_handle,
                    deadline=deadline,
                )
            )
            if not stopped:
                return False
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            complete = True
            for attribute in (
                    "job_handle",
                    "thread_handle",
                    "process_handle"):
                handle = int(getattr(self, attribute))
                if not handle:
                    continue
                try:
                    closed = bool(close_handle(wintypes.HANDLE(handle)))
                    if not closed:
                        closed = _windows_handle_is_proven_invalid(
                            kernel32,
                            handle,
                        )
                except Exception:
                    closed = False
                if closed:
                    setattr(self, attribute, 0)
                else:
                    complete = False
        if complete:
            _unregister_windows_cleanup(self)
        try:
            _remaining(deadline)
        except TimeoutError:
            return False
        return complete


def _stop_created_windows_process(
        kernel32,
        process_handle: int,
        *,
        deadline: float,
) -> bool:
    """Boundedly terminate, wait for, and prove death of a raw process handle."""
    import ctypes
    from ctypes import wintypes

    terminate_process = kernel32.TerminateProcess
    terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_process.restype = wintypes.BOOL
    wait_process = kernel32.WaitForSingleObject
    wait_process.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_process.restype = wintypes.DWORD
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_exit_code.restype = wintypes.BOOL
    try:
        terminate_process(wintypes.HANDLE(process_handle), 1)
    except Exception:
        pass
    try:
        remaining = _remaining(deadline)
    except TimeoutError:
        remaining = 0.0
    milliseconds = max(
        0,
        min(0xFFFFFFFE, int(remaining * 1000 + 0.999)),
    )
    try:
        wait_process(wintypes.HANDLE(process_handle), milliseconds)
    except Exception:
        pass
    exit_code = wintypes.DWORD()
    try:
        if not get_exit_code(
                wintypes.HANDLE(process_handle),
                ctypes.byref(exit_code)):
            return False
    except Exception:
        return False
    return exit_code.value != 259  # STILL_ACTIVE


class _WindowsJobProcess:
    """Suspended-created process assigned to a kill-on-close Job before resume."""

    def __init__(self, process_handle: int, job_handle: int, pid: int, argv):
        self._process_handle = int(process_handle)
        self._job_handle = int(job_handle)
        self.pid = int(pid)
        self.args = list(argv)
        self._cleanup_lock = threading.RLock()

    @classmethod
    def launch(cls, argv, *, deadline: float):
        import ctypes
        from ctypes import wintypes

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD),
                ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE),
                ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        if (
            not argv
            or not Path(argv[0]).is_absolute()
            or sys.platform != "win32"
        ):
            raise ValueError("Windows Job launch requires an absolute executable")
        _remaining(deadline)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        create_job.restype = wintypes.HANDLE
        set_job = kernel32.SetInformationJobObject
        set_job.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        set_job.restype = wintypes.BOOL
        create_process = kernel32.CreateProcessW
        create_process.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW),
            ctypes.POINTER(PROCESS_INFORMATION),
        ]
        create_process.restype = wintypes.BOOL
        assign_job = kernel32.AssignProcessToJobObject
        assign_job.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign_job.restype = wintypes.BOOL
        resume_thread = kernel32.ResumeThread
        resume_thread.argtypes = [wintypes.HANDLE]
        resume_thread.restype = wintypes.DWORD
        job = create_job(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        process_info = PROCESS_INFORMATION()
        assigned_to_job = False
        try:
            limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = 0x2000
            if not set_job(
                    wintypes.HANDLE(job),
                    9,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            startup = STARTUPINFOW()
            startup.cb = ctypes.sizeof(startup)
            command_line = ctypes.create_unicode_buffer(
                subprocess.list2cmdline([str(item) for item in argv])
            )
            creation_flags = (
                0x00000004  # CREATE_SUSPENDED
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
            _remaining(deadline)
            if not create_process(
                    str(argv[0]),
                    command_line,
                    None,
                    None,
                    False,
                    creation_flags,
                    None,
                    None,
                    ctypes.byref(startup),
                    ctypes.byref(process_info)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                _remaining(deadline)
                if not assign_job(
                        wintypes.HANDLE(job),
                        process_info.hProcess):
                    raise ctypes.WinError(ctypes.get_last_error())
                assigned_to_job = True
                if resume_thread(process_info.hThread) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
                _remaining(deadline)
            except BaseException as launch_error:
                stopped = _stop_created_windows_process(
                    kernel32,
                    int(process_info.hProcess),
                    deadline=deadline,
                )
                if not stopped and assigned_to_job:
                    try:
                        kernel32.TerminateJobObject(
                            wintypes.HANDLE(job),
                            1,
                        )
                    except Exception:
                        pass
                    stopped = _stop_created_windows_process(
                        kernel32,
                        int(process_info.hProcess),
                        deadline=deadline,
                    )
                if not stopped:
                    retained_job = int(job) if assigned_to_job else 0
                    retained = _WindowsLaunchCleanupError(
                        int(process_info.hProcess),
                        int(process_info.hThread),
                        int(process_info.dwProcessId),
                        retained_job,
                    )
                    process_info.hProcess = None
                    process_info.hThread = None
                    if assigned_to_job:
                        job = None
                    raise retained from launch_error
                raise
            finally:
                if process_info.hThread:
                    _durably_close_windows_handle(
                        int(process_info.hThread)
                    )
                    process_info.hThread = None
            result = cls(
                int(process_info.hProcess),
                int(job),
                int(process_info.dwProcessId),
                argv,
            )
            process_info.hProcess = None
            job = None
            return result
        finally:
            if process_info.hThread:
                _durably_close_windows_handle(int(process_info.hThread))
            if process_info.hProcess:
                _durably_close_windows_handle(int(process_info.hProcess))
            if job:
                _durably_close_windows_handle(int(job))

    def poll(self):
        if not self._process_handle:
            return 0
        import ctypes
        from ctypes import wintypes

        exit_code = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.GetExitCodeProcess(
                wintypes.HANDLE(self._process_handle),
                ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return None if exit_code.value == 259 else int(exit_code.value)

    def wait(self, timeout=None):
        if not self._process_handle:
            return 0
        import ctypes
        from ctypes import wintypes

        milliseconds = (
            0xFFFFFFFF
            if timeout is None
            else max(0, min(0xFFFFFFFE, int(float(timeout) * 1000 + 0.999)))
        )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait_result = kernel32.WaitForSingleObject(
            wintypes.HANDLE(self._process_handle),
            milliseconds,
        )
        if wait_result == 258:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if wait_result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def close_job(self) -> None:
        with self._cleanup_lock:
            if not self._job_handle:
                return
            import ctypes
            from ctypes import wintypes

            handle = self._job_handle
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
                if not _windows_handle_is_proven_invalid(kernel32, handle):
                    _register_windows_cleanup(self)
                    raise ctypes.WinError(ctypes.get_last_error())
            self._job_handle = 0
            if not self._process_handle:
                _unregister_windows_cleanup(self)

    def terminate(self) -> None:
        if not self._job_handle:
            return
        import ctypes
        from ctypes import wintypes

        if not ctypes.WinDLL(
                "kernel32",
                use_last_error=True).TerminateJobObject(
                    wintypes.HANDLE(self._job_handle),
                    1):
            raise ctypes.WinError(ctypes.get_last_error())

    kill = terminate

    def close(self) -> None:
        with self._cleanup_lock:
            job_error = None
            try:
                self.close_job()
            except Exception as exc:
                job_error = exc
            process_error = None
            if self._process_handle:
                import ctypes
                from ctypes import wintypes

                handle = self._process_handle
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
                    if not _windows_handle_is_proven_invalid(
                            kernel32,
                            handle):
                        _register_windows_cleanup(self)
                        process_error = ctypes.WinError(
                            ctypes.get_last_error()
                        )
                if process_error is None:
                    self._process_handle = 0
            if not self._job_handle and not self._process_handle:
                _unregister_windows_cleanup(self)
            if job_error is not None:
                raise job_error
            if process_error is not None:
                raise process_error

    def cleanup(self, *, deadline: float) -> bool:
        try:
            _remaining(deadline)
            self.close()
            _remaining(deadline)
        except Exception:
            return False
        return not self._job_handle and not self._process_handle

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _launch_browser(exe, userdir, *, deadline: float):
    _remaining(deadline)
    executable = Path(exe)
    if not executable.is_absolute():
        raise ValueError("browser executable must be absolute")
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        return _WindowsJobProcess.launch(
            [
                str(executable),
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                "--headless=new",
                f"--user-data-dir={Path(userdir)}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            deadline=deadline,
        )
    elif sys.platform == "darwin" or sys.platform.startswith((
            "linux", "freebsd", "openbsd", "netbsd")):
        kwargs["start_new_session"] = True
    else:
        raise RuntimeError(
            f"unsupported CDP process-tree platform: {sys.platform}"
        )
    proc = subprocess.Popen(
        [
            str(executable),
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--headless=new",
            f"--user-data-dir={Path(userdir)}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        **kwargs,
    )
    return proc


def _direct_process_fallback(proc, deadline: float) -> bool:
    try:
        proc.kill()
    except Exception:
        return False
    try:
        proc.wait(timeout=_remaining(deadline))
    except Exception:
        pass
    try:
        return proc.poll() is not None
    except Exception:
        return False


def _posix_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_process_tree(proc, *, deadline: float) -> bool:
    """Best-effort bounded group/tree shutdown for a process launched above.

    POSIX sessions remain addressable by the leader pid after leader exit.
    Windows browser descendants are contained by a kill-on-close Job Object;
    the fixed-authority taskkill path is only a legacy-process fallback.
    """
    if sys.platform.startswith("win"):
        if isinstance(proc, _WindowsLaunchCleanupError):
            return proc.cleanup(deadline=deadline)
        if isinstance(proc, _WindowsJobProcess):
            success = False
            try:
                proc.close_job()
                proc.wait(timeout=_remaining(deadline))
                success = proc.poll() is not None
            except Exception:
                success = False
            finally:
                try:
                    proc.close()
                except Exception:
                    success = False
            return success
        helper = getattr(proc, "_ytf_taskkill", None) or _trusted_taskkill()
        pid = getattr(proc, "pid", None)
        if helper is not None and isinstance(pid, int) and pid > 0:
            try:
                result = subprocess.run(
                    [
                        str(helper),
                        "/F",
                        "/T",
                        "/PID",
                        str(pid),
                    ],
                    capture_output=True,
                    timeout=_remaining(deadline),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if getattr(result, "returncode", 1) == 0:
                    return True
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=min(0.1, _remaining(deadline)))
            return True
        except Exception:
            return _direct_process_fallback(proc, deadline)
    if sys.platform == "darwin" or sys.platform.startswith((
            "linux", "freebsd", "openbsd", "netbsd")):
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            try:
                return proc.poll() is not None
            except Exception:
                return False
        except Exception:
            _direct_process_fallback(proc, deadline)
            return False
        try:
            remaining = _remaining(deadline)
            proc.wait(timeout=min(0.25, max(0.01, remaining / 2)))
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            if not _posix_group_exists(pid):
                return True
        if not _posix_group_exists(pid):
            return True
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except Exception:
            _direct_process_fallback(proc, deadline)
            return False
        try:
            proc.wait(timeout=min(0.1, _remaining(deadline)))
        except Exception:
            pass
        while True:
            if not _posix_group_exists(pid):
                return True
            try:
                remaining = _remaining(deadline)
            except TimeoutError:
                return False
            time.sleep(min(0.01, remaining))
    return False


def get_all_cookies_via_cdp(browser_name: str, timeout: float = 20.0,
                            profile_dir: str | None = None):
    """Read decrypted cookies from a Chromium browser's REAL profile via CDP.

    A snapshot of the user's default (or explicitly given) profile is copied into
    a temp --user-data-dir so Chrome can open it without fighting the running
    browser's SingletonLock, yet the login cookies are present. If no real
    profile can be snapshotted, we do NOT navigate anywhere (so YouTube can't seed
    throwaway CONSENT/VISITOR cookies), and an empty result honestly yields None
    rather than a misleading 'success'."""
    # Clear here, not in _snapshot_profile: this function has several early returns
    # (bad timeout, no drainable cleanup registry, no browser exe) that never reach the
    # snapshot step, so a reason recorded by a PREVIOUS attempt would survive and be
    # reported as though it explained this one.
    _record_snapshot_failure(None)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        return None
    started = time.monotonic()
    deadline = started + timeout_value
    # Reserve part of the one hard budget for tree shutdown and private-profile
    # removal. All work still has a monotonic absolute deadline; cleanup cannot
    # extend beyond the caller's end-to-end deadline.
    cleanup_reserve = min(2.0, max(0.02, timeout_value * 0.25))
    work_deadline = deadline - cleanup_reserve
    termination_deadline = work_deadline + cleanup_reserve * 0.4
    deletion_deadline = deadline
    userdir: Path | None = None
    proc = None
    sock = None
    result = None
    terminated = True
    cleaned = True
    try:
        _remaining(work_deadline)
        if (
            sys.platform.startswith("win")
            and not _drain_windows_cleanup_registry(
                deadline=work_deadline,
            )
        ):
            return None
        exe = _find_exe(browser_name)
        if not exe or not Path(exe).is_absolute():
            return None
        _remaining(work_deadline)
        src_root = (
            Path(profile_dir)
            if profile_dir
            else _default_user_data_dir(browser_name)
        )
        userdir = _make_private_tempdir()
        _remaining(work_deadline)
        if src_root:
            snapshotted = _snapshot_profile(
                src_root,
                userdir,
                browser_name=browser_name,
                deadline=work_deadline,
            )
            if not snapshotted:
                return None
        _remaining(work_deadline)
        active_path = userdir / "DevToolsActivePort"
        if os.path.lexists(active_path):
            raise ValueError("stale DevToolsActivePort predates browser launch")
        try:
            proc = _launch_browser(exe, userdir, deadline=work_deadline)
        except _WindowsLaunchCleanupError as retained_launch:
            proc = retained_launch
            raise
        _remaining(work_deadline)
        host, port, ws_path = _wait_for_owned_endpoint(
            userdir,
            proc=proc,
            deadline=work_deadline,
        )
        sock = _ws_handshake(
            host,
            port,
            ws_path,
            deadline=work_deadline,
        )
        _ws_send(
            sock,
            json.dumps({
                "id": 1,
                "method": "Storage.getCookies",
            }),
            deadline=work_deadline,
        )
        cookies = None
        while True:
            raw = _ws_recv(sock, deadline=work_deadline)
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("malformed CDP websocket JSON") from exc
            _remaining(work_deadline)
            if not isinstance(message, dict):
                raise ValueError("malformed CDP websocket message")
            message_id = message.get("id")
            _remaining(work_deadline)
            if type(message_id) is not int or message_id != 1:
                continue
            if "error" in message:
                return None
            response_result = message.get("result")
            _remaining(work_deadline)
            if not isinstance(response_result, dict):
                raise ValueError("malformed CDP cookie response")
            cookies = response_result.get("cookies")
            _remaining(work_deadline)
            if not isinstance(cookies, list) or len(cookies) > 10_000:
                raise ValueError("malformed CDP cookie list")
            break
        normalized = []
        for cookie in cookies:
            _remaining(work_deadline)
            if not isinstance(cookie, dict):
                raise ValueError("malformed CDP cookie")
            domain = str(cookie.get("domain", ""))
            _remaining(work_deadline)
            name = str(cookie.get("name", ""))
            _remaining(work_deadline)
            value = str(cookie.get("value", ""))
            _remaining(work_deadline)
            path = str(cookie.get("path", "/"))
            _remaining(work_deadline)
            secure = bool(cookie.get("secure"))
            _remaining(work_deadline)
            expires = int(cookie.get("expires", 0) or 0)
            _remaining(work_deadline)
            normalized.append({
                "domain": domain,
                "name": name,
                "value": value,
                "path": path,
                "secure": secure,
                "expires": expires,
            })
        _remaining(work_deadline)
        result = normalized or None
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        TypeError,
        ValueError,
    ) as error:
        # Sibling of the swallow in _snapshot_profile: this one discards the reason a CDP read
        # failed AFTER the browser answered. Record it, or a successful cookie fetch that trips a
        # later check is indistinguishable from never having logged in.
        _record_snapshot_failure(error)
        result = None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if proc is not None:
            try:
                terminated = _terminate_process_tree(
                    proc,
                    deadline=termination_deadline,
                )
            except Exception:
                terminated = False
        if userdir is not None:
            try:
                cleaned = _cleanup_private_tempdir(
                    userdir,
                    deadline=deletion_deadline,
                )
            except Exception:
                cleaned = False
    if not terminated:
        # A browser we could not kill still holds the snapshot of the user's cookies. That is an
        # ongoing exposure, so fail closed.
        return None
    if not cleaned and result is not None:
        # Gated on `result`: when the CDP read ITSELF failed, the except above already recorded
        # why, and overwriting it with a sentence that opens "read cookies" would tell the user
        # the opposite of what happened -- webserver.py surfaces this string verbatim.
        #
        # The cookies were read successfully and only the temporary snapshot could not be deleted.
        # On Windows, Chrome's "Cache" and "Shared Dictionary" subdirectories routinely refuse
        # deletion after the process exits -- permanently, not transiently (measured: still denied
        # after 15s of retries), and with no process holding them.
        #
        # Discarding the result here protected nothing: the snapshot stays on disk either way, so
        # failing closed left the user with BOTH a leftover directory AND no login, while the
        # browser had already answered with a complete, valid cookie set. That is how "Login with
        # Chrome" came to fail on Windows for a user who had signed in correctly.
        #
        # Report where the leftover is -- this module has no logger by design, so use the same
        # channel the caller already reads -- and return what was asked for.
        _record_snapshot_failure(OSError(
            f"read cookies, but could not delete the temporary profile snapshot at {userdir}; "
            "it holds a copy of your browser cookies and should be deleted manually"))
    try:
        _remaining(deadline)
    except TimeoutError:
        return None
    return result
