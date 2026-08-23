"""Build the allowlisted, byte-reproducible portable release ZIP."""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import sys
import tempfile
from typing import Callable
import unicodedata
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


_ALLOWLIST = PurePosixPath("release/portable-files.txt")
_RECURSIVE_SUFFIX = "/**"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WIN_FILE_READ_ATTRIBUTES = 0x0080
_WIN_GENERIC_READ = 0x80000000
_WIN_DELETE = 0x00010000
_WIN_SHARE_READ = 0x00000001
_WIN_SHARE_WRITE = 0x00000002
_WIN_SHARE_DELETE = 0x00000004
_WIN_OPEN_EXISTING = 3
_WIN_OPEN_REPARSE_POINT = 0x00200000
_WIN_BACKUP_SEMANTICS = 0x02000000
_WIN_DIRECTORY_ATTRIBUTE = 0x00000010
_WIN_FILE_DISPOSITION_INFO = 4
_WIN_FILE_DISPOSITION_INFO_EX = 21
_WIN_FILE_RENAME_INFO = 3
_WIN_DISPOSITION_DELETE = 0x00000001
_WIN_DISPOSITION_POSIX_SEMANTICS = 0x00000002
_WIN_DISPOSITION_IGNORE_READONLY = 0x00000010
_BINARY_SUFFIXES = {
    ".exe",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mkv",
    ".mp4",
    ".png",
    ".ttf",
    ".webm",
    ".woff",
    ".woff2",
    ".zip",
}
_CRLF_SUFFIXES = {".bat", ".cmd", ".ps1"}
_FORBIDDEN_PARTS = {
    ".cache",
    ".claude",
    ".git",
    ".github",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".superpowers",
    ".tox",
    ".venv",
    ".worktrees",
    "__pycache__",
    "archive",
    "build",
    "cache",
    "caches",
    "dist",
    "docs",
    "downloads",
    "encoder",
    "htmlcov",
    "log",
    "logs",
    "mcps",
    "output",
    "outputs",
    "requirements",
    "scaled",
    "test_out",
    "test_outputs",
    "tests",
    "wheels",
}
_FORBIDDEN_NAMES = {
    "_syntax_check.py",
    "conftest.py",
    "innovation_roadmap.md",
    "runtime_verify.py",
}
_FORBIDDEN_SUFFIXES = {
    ".log",
    ".pyc",
    ".pyo",
}
_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_STEMS = {
    "aux",
    "clock$",
    "con",
    "conin$",
    "conout$",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    *(
        f"com{number}"
        for number in (
            "\N{SUPERSCRIPT ONE}"
            "\N{SUPERSCRIPT TWO}"
            "\N{SUPERSCRIPT THREE}"
        )
    ),
    *(
        f"lpt{number}"
        for number in (
            "\N{SUPERSCRIPT ONE}"
            "\N{SUPERSCRIPT TWO}"
            "\N{SUPERSCRIPT THREE}"
        )
    ),
}


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _normalize_windows_path(path) -> str:
    raw = os.fspath(path).replace("/", "\\")
    folded = raw.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        raw = "\\\\" + raw[8:]
    elif folded.startswith("\\\\?\\"):
        raw = raw[4:]
    return ntpath.normcase(ntpath.normpath(raw))


def _windows_create_handle(
        path: Path,
        *,
        directory: bool,
        access: int | None = None,
        share_delete: bool = True,
        share_write: bool = True,
) -> int:
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    if access is None:
        access = (
            _WIN_FILE_READ_ATTRIBUTES
            if directory
            else _WIN_GENERIC_READ
        )
    flags = _WIN_OPEN_REPARSE_POINT
    if directory:
        flags |= _WIN_BACKUP_SEMANTICS
    share_mode = _WIN_SHARE_READ
    if share_write:
        share_mode |= _WIN_SHARE_WRITE
    if share_delete:
        share_mode |= _WIN_SHARE_DELETE
    handle = create_file(
        os.fspath(path),
        access,
        share_mode,
        None,
        _WIN_OPEN_EXISTING,
        flags,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_handle_information(
        handle: int,
) -> tuple[tuple[int, int], int]:
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    information = _ByHandleFileInformation()
    get_information = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
    if not get_information(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    file_index = (
        information.nFileIndexHigh << 32
        | information.nFileIndexLow
    )
    return (
        information.dwVolumeSerialNumber,
        file_index,
    ), information.dwFileAttributes


def _windows_final_handle_path(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    get_final_path = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final_path.restype = wintypes.DWORD
    size = 32_768
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = get_final_path(handle, buffer, size, 0)
        if length == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if length < size:
            return _normalize_windows_path(buffer.value)
        size = length + 1


def _open_windows_shared_file_descriptor(
        path: Path,
        *,
        delete_access: bool = False,
        share_delete: bool = True,
        share_write: bool = True,
) -> int:
    import msvcrt

    access = _WIN_GENERIC_READ
    if delete_access:
        access |= _WIN_DELETE
    handle = _windows_create_handle(
        path,
        directory=False,
        access=access,
        share_delete=share_delete,
        share_write=share_write,
    )
    try:
        _identity, attributes = _windows_handle_information(handle)
        if attributes & _REPARSE_POINT or attributes & _WIN_DIRECTORY_ATTRIBUTE:
            raise ValueError(
                f"publication source is not a real regular file: {path}"
            )
        descriptor = msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        _windows_close_handle(handle)
        raise
    return descriptor


def _windows_delete_handle_entry(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class _FileDispositionInfoEx(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD)]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    set_information = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    extended = _FileDispositionInfoEx(
        _WIN_DISPOSITION_DELETE
        | _WIN_DISPOSITION_POSIX_SEMANTICS
        | _WIN_DISPOSITION_IGNORE_READONLY
    )
    if set_information(
            handle,
            _WIN_FILE_DISPOSITION_INFO_EX,
            ctypes.byref(extended),
            ctypes.sizeof(extended)):
        return
    basic = _FileDispositionInfo(True)
    if not set_information(
            handle,
            _WIN_FILE_DISPOSITION_INFO,
            ctypes.byref(basic),
            ctypes.sizeof(basic)):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_rename_handle_entry(
        handle: int,
        destination: Path,
) -> None:
    import ctypes
    from ctypes import wintypes

    name = os.fspath(destination)

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (len(name) + 1)),
        ]

    information = _FileRenameInfo()
    information.Flags = 0
    information.RootDirectory = None
    information.FileNameLength = len(name.encode("utf-16-le"))
    information.FileName = name
    set_information = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    if not set_information(
            handle,
            _WIN_FILE_RENAME_INFO,
            ctypes.byref(information),
            ctypes.sizeof(information)):
        raise ctypes.WinError(ctypes.get_last_error())


def _posix_link_held_descriptor(
        descriptor: int,
        directory_descriptor: int,
        name: str,
) -> None:
    import ctypes

    linkat = ctypes.CDLL(None, use_errno=True).linkat
    linkat.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    linkat.restype = ctypes.c_int
    encoded_name = os.fsencode(name)
    attempts = (
        (descriptor, b"", 0x1000),
        (-100, os.fsencode(f"/proc/self/fd/{descriptor}"), 0x400),
    )
    last_error = errno.EOPNOTSUPP
    for source_descriptor, source_name, flags in attempts:
        if linkat(
                source_descriptor,
                source_name,
                directory_descriptor,
                encoded_name,
                flags) == 0:
            return
        last_error = ctypes.get_errno()
    raise OSError(
        last_error,
        os.strerror(last_error),
        name,
    )


class _DirectoryAnchor:
    def __init__(self, path: Path, *, label: str):
        self.path = _absolute(Path(path))
        self.label = label
        self._descriptor = None
        self._handle = None
        reviewed = _require_real_kind(self.path, directory=True)
        if os.name == "nt":
            handle = _windows_create_handle(self.path, directory=True)
            try:
                identity, attributes = _windows_handle_information(handle)
                if (
                    attributes & _REPARSE_POINT
                    or not attributes & _WIN_DIRECTORY_ATTRIBUTE
                ):
                    raise ValueError(
                        f"{label} is a symlink or reparse point: {self.path}"
                    )
                if (
                    _windows_final_handle_path(handle)
                    != _normalize_windows_path(self.path)
                ):
                    raise ValueError(
                        f"{label} escaped its reviewed path: {self.path}"
                    )
            except Exception:
                _windows_close_handle(handle)
                raise
            self._handle = handle
            self.identity = identity
        else:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(self.path, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _object_identity(opened) != _object_identity(reviewed)
            ):
                os.close(descriptor)
                raise ValueError(
                    f"{label} identity changed during review: {self.path}"
                )
            self._descriptor = descriptor
            self.identity = _object_identity(opened)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()

    def close(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        if self._handle is not None:
            _windows_close_handle(self._handle)
            self._handle = None

    def require_path_identity(self) -> None:
        _require_real_kind(self.path, directory=True)
        if os.name == "nt":
            current = _windows_create_handle(self.path, directory=True)
            try:
                identity, attributes = _windows_handle_information(current)
                if attributes & _REPARSE_POINT or identity != self.identity:
                    raise ValueError(
                        f"{self.label} identity changed: {self.path}"
                    )
            finally:
                _windows_close_handle(current)
        elif _object_identity(self.path.lstat()) != self.identity:
            raise ValueError(
                f"{self.label} identity changed: {self.path}"
            )

    def _current_windows_path(self) -> str:
        if self._handle is None:
            raise ValueError(f"{self.label} anchor is closed")
        return _windows_final_handle_path(self._handle)

    def duplicate_posix_descriptor(self) -> int:
        if self._descriptor is None:
            raise ValueError(f"{self.label} has no POSIX directory anchor")
        return os.dup(self._descriptor)

    def verify_file_descriptor(
            self,
            descriptor: int,
            relative: PurePosixPath,
    ) -> tuple[int, int]:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(
                f"{self.label} child changed type: {relative}"
            )
        expected = self.path.joinpath(*relative.parts)
        named = _require_real_kind(expected, directory=False)
        identity = _object_identity(opened)
        if _object_identity(named) != identity:
            raise ValueError(
                f"{self.label} child identity changed: {relative}"
            )
        if os.name == "nt":
            import msvcrt

            actual_path = _windows_final_handle_path(
                msvcrt.get_osfhandle(descriptor)
            )
            expected_path = _normalize_windows_path(ntpath.join(
                self._current_windows_path(),
                *relative.parts,
            ))
            if actual_path != expected_path:
                raise ValueError(
                    f"{self.label} child escaped anchor: {relative}"
                )
        self.require_path_identity()
        return identity

    def unlink_child(
            self,
            relative: PurePosixPath,
            *,
            missing_ok: bool = False,
    ) -> None:
        if len(relative.parts) != 1:
            raise ValueError(
                f"{self.label} unlink must name one direct child: {relative}"
            )
        self.require_path_identity()
        if os.name == "nt":
            path = self.path / relative.name
            try:
                handle = _windows_create_handle(
                    path,
                    directory=False,
                    access=_WIN_FILE_READ_ATTRIBUTES | _WIN_DELETE,
                )
            except FileNotFoundError:
                if missing_ok:
                    return
                raise
            try:
                actual = _windows_final_handle_path(handle)
                expected = _normalize_windows_path(ntpath.join(
                    self._current_windows_path(),
                    relative.name,
                ))
                if actual != expected:
                    raise ValueError(
                        f"{self.label} child escaped anchor: {relative}"
                    )
                _windows_delete_handle_entry(handle)
            finally:
                _windows_close_handle(handle)
        else:
            if self._descriptor is None:
                raise ValueError(f"{self.label} anchor is closed")
            try:
                os.unlink(
                    relative.name,
                    dir_fd=self._descriptor,
                )
            except FileNotFoundError:
                if not missing_ok:
                    raise
        self.require_path_identity()

    def rename_held_child(
            self,
            descriptor: int,
            source: PurePosixPath,
            destination: PurePosixPath,
    ) -> None:
        if len(source.parts) != 1 or len(destination.parts) != 1:
            raise ValueError(
                f"{self.label} rename must stay within direct children"
            )
        self.verify_file_descriptor(descriptor, source)
        if os.name == "nt":
            import msvcrt

            if self._handle is None:
                raise ValueError(f"{self.label} anchor is closed")
            _windows_rename_handle_entry(
                msvcrt.get_osfhandle(descriptor),
                self.path / destination.name,
            )
        else:
            if self._descriptor is None:
                raise ValueError(f"{self.label} anchor is closed")
            if sys.platform.startswith("linux"):
                _posix_link_held_descriptor(
                    descriptor,
                    self._descriptor,
                    destination.name,
                )
            elif os.rename in os.supports_dir_fd:
                # macOS and the BSDs have neither linkat(AT_EMPTY_PATH) nor
                # /proc/self/fd, so a held descriptor cannot name itself and
                # the link-based restore above is unavailable there. Refusing
                # is not the safe answer: _rollback_publication has already
                # unlinked the destination before it calls this, so raising
                # would destroy the previously published artifact the rollback
                # exists to put back. renameat() against the anchored
                # directory descriptor keeps the directory itself unswappable,
                # and the verify_file_descriptor() calls that bracket this
                # branch still prove the name now at the destination is the
                # held inode, so a substituted backup fails closed instead of
                # being published.
                os.rename(
                    source.name,
                    destination.name,
                    src_dir_fd=self._descriptor,
                    dst_dir_fd=self._descriptor,
                )
            else:
                raise OSError(
                    errno.ENOTSUP,
                    "held-descriptor rollback publication is unavailable",
                    destination.name,
                )
        self.verify_file_descriptor(descriptor, destination)


def _link_like(path: Path, metadata=None) -> bool:
    metadata = metadata if metadata is not None else path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        return True
    if getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT:
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _require_real_kind(
        path: Path,
        *,
        directory: bool | None = None,
):
    metadata = path.lstat()
    if _link_like(path, metadata):
        raise ValueError(f"symlink or reparse point is forbidden: {path}")
    if directory is True and not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"allowlisted source is not a directory: {path}")
    if directory is False and not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"allowlisted source is not a regular file: {path}")
    return metadata


def _validate_portable_component(component: str) -> None:
    if not component or component in (".", ".."):
        raise ValueError(f"invalid portable component: {component!r}")
    if component[-1] in (" ", "."):
        raise ValueError(
            f"portable component has a Windows trailing alias: {component!r}"
        )
    if any(character in _WINDOWS_INVALID_CHARACTERS for character in component):
        raise ValueError(
            f"portable component contains a Windows-invalid character: "
            f"{component!r}"
        )
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in component
    ):
        raise ValueError(
            f"portable component contains a control or format character: "
            f"{component!r}"
        )
    device_stem = component.split(".", 1)[0].casefold()
    if device_stem in _WINDOWS_RESERVED_STEMS:
        raise ValueError(
            f"portable component uses a Windows reserved device name: "
            f"{component!r}"
        )


def _portable_component_key(component: str) -> str:
    _validate_portable_component(component)
    return unicodedata.normalize("NFC", component).casefold()


def _validate_destination(raw: str) -> PurePosixPath:
    if not raw:
        raise ValueError("portable destination is empty")
    if "\\" in raw:
        raise ValueError("portable destinations must use POSIX separators")
    if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).drive:
        raise ValueError(f"absolute or drive-qualified destination: {raw}")
    if any(character in raw for character in "*?[]"):
        raise ValueError(f"unsupported allowlist glob: {raw}")
    components = raw.split("/")
    for component in components:
        _validate_portable_component(component)
    destination = PurePosixPath(*components)
    if destination.as_posix() != raw:
        raise ValueError(f"non-canonical portable destination: {raw}")
    return destination


def _validate_member_destinations(
        destinations,
) -> None:
    seen: dict[tuple[str, ...], PurePosixPath] = {}
    seen_directory_spellings: dict[
        tuple[str, ...],
        tuple[str, ...],
    ] = {}
    for destination in destinations:
        key = tuple(
            _portable_component_key(component)
            for component in destination.parts
        )
        for length in range(1, len(key)):
            prefix_key = key[:length]
            spelling = destination.parts[:length]
            previous_spelling = seen_directory_spellings.get(prefix_key)
            if (
                previous_spelling is not None
                and previous_spelling != spelling
            ):
                raise ValueError(
                    "normalized directory component collision in portable "
                    f"allowlist: {previous_spelling} and {spelling}"
                )
            seen_directory_spellings[prefix_key] = spelling
        previous = seen.get(key)
        if previous is not None:
            raise ValueError(
                "duplicate, overlap, or normalized collision in portable "
                f"allowlist: {previous} and {destination}"
            )
        for previous_key, previous_destination in seen.items():
            shared = min(len(key), len(previous_key))
            if (
                key[:shared] == previous_key[:shared]
                and len(key) != len(previous_key)
            ):
                raise ValueError(
                    "file/directory prefix collision in portable allowlist: "
                    f"{previous_destination} and {destination}"
                )
        seen[key] = destination


def _validated_source(
        root: Path,
        destination: PurePosixPath,
        *,
        directory: bool,
) -> Path:
    current = root
    for component in destination.parts:
        current = current / component
        _require_real_kind(current)
    _require_real_kind(current, directory=directory)
    return current


def _expanded_directory(
        root: Path,
        destination: PurePosixPath,
) -> list[tuple[Path, PurePosixPath]]:
    source_root = _validated_source(
        root,
        destination,
        directory=True,
    )
    pending = [(source_root, destination)]
    expanded: list[tuple[Path, PurePosixPath]] = []
    while pending:
        source_directory, destination_directory = pending.pop()
        for child in sorted(source_directory.iterdir(), key=lambda item: item.name):
            child_destination = _validate_destination(
                f"{destination_directory.as_posix()}/{child.name}"
            )
            metadata = _require_real_kind(child)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append((child, child_destination))
            elif stat.S_ISREG(metadata.st_mode):
                expanded.append((child, child_destination))
            else:
                raise ValueError(
                    f"allowlisted tree contains a non-regular entry: {child}"
                )
    if not expanded:
        raise ValueError(
            f"recursive allowlist directory contains no files: {destination}"
        )
    return expanded


def _forbidden_member(destination: PurePosixPath) -> bool:
    folded_parts = tuple(part.casefold() for part in destination.parts)
    name = folded_parts[-1]
    if any(part in _FORBIDDEN_PARTS for part in folded_parts):
        return True
    if any(part.startswith(".") for part in folded_parts):
        return True
    if (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name in _FORBIDDEN_NAMES
    ):
        return True
    return any(name.endswith(suffix) for suffix in _FORBIDDEN_SUFFIXES)


def _read_allowlist_entries(
        root: Path,
        *,
        anchor: _DirectoryAnchor,
) -> list[tuple[PurePosixPath, bool]]:
    allowlist_path = _validated_source(
        root,
        _ALLOWLIST,
        directory=False,
    )
    text = _stable_source_bytes(
        allowlist_path,
        root=root,
        destination=_ALLOWLIST,
        anchor=anchor,
    ).decode("utf-8", errors="strict")
    declared = []
    for line_number, source_line in enumerate(text.split("\n"), start=1):
        if not source_line.strip() or source_line.lstrip().startswith("#"):
            continue
        if source_line != source_line.strip():
            raise ValueError(
                "portable allowlist member lines cannot have leading or "
                f"trailing whitespace (line {line_number})"
            )
        line = source_line
        recursive = line.endswith(_RECURSIVE_SUFFIX)
        raw_destination = (
            line[:-len(_RECURSIVE_SUFFIX)]
            if recursive
            else line
        )
        try:
            destination = _validate_destination(raw_destination)
        except ValueError as exc:
            raise ValueError(
                f"invalid portable allowlist line {line_number}: {exc}"
            ) from exc
        declared.append((destination, recursive))
    if not declared:
        raise ValueError("portable allowlist contains no portable files")
    return declared


def _expand_allowlist_entries(
        root: Path,
        declared: list[tuple[PurePosixPath, bool]],
) -> list[tuple[Path, PurePosixPath]]:
    entries: list[tuple[Path, PurePosixPath]] = []
    for destination, recursive in declared:
        if recursive:
            entries.extend(_expanded_directory(root, destination))
        else:
            source = _validated_source(
                root,
                destination,
                directory=False,
            )
            entries.append((source, destination))
    for _source, destination in entries:
        if _forbidden_member(destination):
            raise ValueError(
                f"forbidden member expanded from portable allowlist: {destination}"
            )
    _validate_member_destinations(
        destination
        for _source, destination in entries
    )
    return sorted(entries, key=lambda item: item[1].as_posix())


def portable_members(
        repo_root: Path,
) -> list[tuple[Path, PurePosixPath]]:
    """Expand the narrow portable allowlist into sorted regular files."""
    root = _absolute(Path(repo_root))
    with _DirectoryAnchor(root, label="portable repository root") as anchor:
        return _expand_allowlist_entries(
            root,
            _read_allowlist_entries(root, anchor=anchor),
        )


def _path_is_within(path: Path, parent: Path) -> bool:
    path_text = os.path.normcase(os.path.abspath(os.fspath(path)))
    parent_text = os.path.normcase(os.path.abspath(os.fspath(parent)))
    try:
        return os.path.commonpath((path_text, parent_text)) == parent_text
    except ValueError:
        return False


def _reject_recursive_output_overlap(
        root: Path,
        output: Path,
        declared: list[tuple[PurePosixPath, bool]],
) -> None:
    for destination, recursive in declared:
        if not recursive:
            continue
        source = _validated_source(
            root,
            destination,
            directory=True,
        )
        if _path_is_within(output, source):
            raise ValueError(
                "portable output overlaps recursive allowlist source: "
                f"{destination}"
            )
        canonical_source = source.resolve(strict=True)
        canonical_output = output.resolve(strict=False)
        if _path_is_within(canonical_output, canonical_source):
            raise ValueError(
                "portable output canonically overlaps recursive allowlist "
                f"source: {destination}"
            )


def _read_version(
        root: Path,
        *,
        anchor: _DirectoryAnchor,
) -> tuple[str, tuple[int, int, int, int | None], bytes]:
    destination = PurePosixPath("Final/_version.py")
    version_path = _validated_source(
        root,
        destination,
        directory=False,
    )
    version_identity = _source_identity(
        _require_real_kind(version_path, directory=False)
    )
    version_bytes = _stable_source_bytes(
        version_path,
        root=root,
        destination=destination,
        anchor=anchor,
        expected_identity=version_identity,
    )
    tree = ast.parse(
        version_bytes.decode("utf-8", errors="strict"),
        filename=os.fspath(version_path),
    )
    values = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        value = node.value
        if (
            any(isinstance(target, ast.Name)
                and target.id == "__version__"
                for target in targets)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            values.append(value.value)
    if len(values) != 1 or not _VERSION_PATTERN.fullmatch(values[0]):
        raise ValueError("Final._version must define one static X.Y.Z version")
    return values[0], version_identity, version_bytes


def _dos_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    if type(epoch) is not int:
        raise TypeError("portable ZIP epoch must be an integer")
    try:
        value = datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("portable ZIP epoch is outside the DOS range") from exc
    if value.year < 1980 or value.year > 2107:
        raise ValueError("portable ZIP epoch must be within 1980 through 2107")
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second - value.second % 2,
    )


def _source_identity(metadata) -> tuple[int, int, int, int | None]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        getattr(metadata, "st_mtime_ns", None),
    )


def _object_identity(metadata) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _require_temporary_identity(
        path: Path,
        identity: tuple[int, int],
        descriptor: int | None = None,
) -> None:
    metadata = _require_real_kind(path, directory=False)
    if _object_identity(metadata) != identity:
        raise ValueError(
            f"portable temporary path identity changed: {path}"
        )
    if (
        descriptor is not None
        and _object_identity(os.fstat(descriptor)) != identity
    ):
        raise ValueError(
            f"portable temporary descriptor identity changed: {path}"
        )


def _unlink_owned_temporary(
        path: Path,
        identity: tuple[int, int],
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if (
        not _link_like(path, metadata)
        and stat.S_ISREG(metadata.st_mode)
        and _object_identity(metadata) == identity
    ):
        path.unlink()


def _digest_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _open_publication_guard(
        path: Path,
        *,
        delete_access: bool = False,
        share_delete: bool = True,
        share_write: bool = True,
) -> int:
    if os.name == "nt":
        descriptor = _open_windows_shared_file_descriptor(
            path,
            delete_access=delete_access,
            share_delete=share_delete,
            share_write=share_write,
        )
    else:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise ValueError(
            f"publication source is not a regular file: {path}"
        )
    return descriptor


@dataclass
class _PublicationBackup:
    path: Path
    identity: tuple[int, int]
    digest: str
    descriptor: int


def _copy_descriptor(source: int, destination: int) -> None:
    os.lseek(source, 0, os.SEEK_SET)
    os.lseek(destination, 0, os.SEEK_SET)
    os.ftruncate(destination, 0)
    while True:
        chunk = os.read(source, 1024 * 1024)
        if not chunk:
            break
        view = memoryview(chunk)
        while view:
            written = os.write(destination, view)
            if written <= 0:
                raise OSError("publication backup write made no progress")
            view = view[written:]
    os.fsync(destination)
    os.lseek(source, 0, os.SEEK_SET)
    os.lseek(destination, 0, os.SEEK_SET)


def _snapshot_existing_destination(
        destination: Path,
        directory_anchor: _DirectoryAnchor,
) -> _PublicationBackup | None:
    try:
        source = _open_publication_guard(destination)
    except FileNotFoundError:
        return None
    descriptor = -1
    backup_path = None
    backup_identity = None
    retained = False
    try:
        relative = PurePosixPath(destination.name)
        directory_anchor.verify_file_descriptor(source, relative)
        source_digest = _digest_descriptor(source)
        descriptor, backup_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".rollback",
            dir=directory_anchor.path,
        )
        backup_path = Path(backup_name)
        backup_identity = _object_identity(os.fstat(descriptor))
        directory_anchor.verify_file_descriptor(
            descriptor,
            PurePosixPath(backup_path.name),
        )
        _copy_descriptor(source, descriptor)
        if (
            _digest_descriptor(source) != source_digest
            or _digest_descriptor(descriptor) != source_digest
        ):
            raise ValueError(
                f"existing destination changed during backup: {destination}"
            )
        os.close(descriptor)
        descriptor = -1
        descriptor = _open_publication_guard(
            backup_path,
            delete_access=True,
        )
        if _object_identity(os.fstat(descriptor)) != backup_identity:
            raise ValueError(
                f"publication backup identity changed: {backup_path}"
            )
        directory_anchor.verify_file_descriptor(
            descriptor,
            PurePosixPath(backup_path.name),
        )
        if _digest_descriptor(descriptor) != source_digest:
            raise ValueError(
                f"publication backup content changed: {backup_path}"
            )
        result = _PublicationBackup(
            backup_path,
            backup_identity,
            source_digest,
            descriptor,
        )
        descriptor = -1
        retained = True
        return result
    finally:
        os.close(source)
        if descriptor >= 0:
            os.close(descriptor)
        if (
            not retained
            and backup_path is not None
            and backup_identity is not None
        ):
            _unlink_owned_temporary(
                backup_path,
                backup_identity,
            )


def _discard_publication_backup(
        backup: _PublicationBackup | None,
) -> None:
    if backup is None:
        return
    cleanup_error = None
    if backup.descriptor >= 0:
        descriptor = backup.descriptor
        backup.descriptor = -1
        try:
            os.close(descriptor)
        except BaseException as exc:
            cleanup_error = exc
    try:
        _unlink_owned_temporary(backup.path, backup.identity)
    except BaseException as exc:
        if cleanup_error is None:
            cleanup_error = exc
        elif hasattr(cleanup_error, "add_note"):
            cleanup_error.add_note(
                f"additional backup cleanup error: {exc!r}"
            )
    if cleanup_error is not None:
        raise cleanup_error


def _rollback_publication(
        destination: Path,
        backup: _PublicationBackup | None,
        directory_anchor: _DirectoryAnchor,
) -> None:
    destination_relative = PurePosixPath(destination.name)
    directory_anchor.unlink_child(
        destination_relative,
        missing_ok=True,
    )
    if backup is None:
        return
    backup_relative = PurePosixPath(backup.path.name)
    try:
        directory_anchor.verify_file_descriptor(
            backup.descriptor,
            backup_relative,
        )
        if _digest_descriptor(backup.descriptor) != backup.digest:
            raise ValueError(
                f"publication rollback content changed: {backup.path}"
            )
        directory_anchor.rename_held_child(
            backup.descriptor,
            backup_relative,
            destination_relative,
        )
        if _digest_descriptor(backup.descriptor) != backup.digest:
            raise ValueError(
                f"restored destination content changed: {destination}"
            )
        directory_anchor.verify_file_descriptor(
            backup.descriptor,
            destination_relative,
        )
        directory_anchor.unlink_child(
            backup_relative,
            missing_ok=True,
        )
        directory_anchor.verify_file_descriptor(
            backup.descriptor,
            destination_relative,
        )
    except BaseException as restore_error:
        cleanup_errors = []
        for relative in (destination_relative, backup_relative):
            try:
                directory_anchor.unlink_child(
                    relative,
                    missing_ok=True,
                )
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            cleanup_error = cleanup_errors[0]
            if hasattr(cleanup_error, "add_note"):
                cleanup_error.add_note(
                    f"restore failure was: {restore_error!r}"
                )
                for additional in cleanup_errors[1:]:
                    cleanup_error.add_note(
                        f"additional cleanup failure: {additional!r}"
                    )
            raise cleanup_error from restore_error
        raise


def _verified_atomic_replace(
        temporary: Path,
        temporary_identity: tuple[int, int],
        destination: Path,
        expected_digest: str,
        directory_anchor: _DirectoryAnchor,
        boundary_validator: Callable[[], None] | None = None,
) -> None:
    guard = _open_publication_guard(temporary)
    final_guard = None
    backup = None
    replace_started = False
    try:
        _require_temporary_identity(
            temporary,
            temporary_identity,
            guard,
        )
        directory_anchor.verify_file_descriptor(
            guard,
            PurePosixPath(temporary.name),
        )
        if _digest_descriptor(guard) != expected_digest:
            raise ValueError(
                f"publication temporary content changed: {temporary}"
            )
        directory_anchor.require_path_identity()
        backup = _snapshot_existing_destination(
            destination,
            directory_anchor,
        )
        if boundary_validator is not None:
            boundary_validator()
        replace_started = True
        os.replace(temporary, destination)
        directory_anchor.verify_file_descriptor(
            guard,
            PurePosixPath(destination.name),
        )
        if _digest_descriptor(guard) != expected_digest:
            raise ValueError(
                f"published content changed during replace: {destination}"
            )
        final_guard = _open_publication_guard(
            destination,
            share_delete=False,
            share_write=False,
        )
        if (
            _object_identity(os.fstat(final_guard))
            != temporary_identity
        ):
            raise ValueError(
                f"published destination identity changed: {destination}"
            )
        directory_anchor.verify_file_descriptor(
            final_guard,
            PurePosixPath(destination.name),
        )
        if _digest_descriptor(final_guard) != expected_digest:
            raise ValueError(
                f"published destination content changed: {destination}"
            )
        directory_anchor.verify_file_descriptor(
            final_guard,
            PurePosixPath(destination.name),
        )
        if boundary_validator is not None:
            boundary_validator()
            if _digest_descriptor(final_guard) != expected_digest:
                raise ValueError(
                    f"published destination content changed: {destination}"
                )
            boundary_validator()
            directory_anchor.verify_file_descriptor(
                final_guard,
                PurePosixPath(destination.name),
            )
        if (
            _object_identity(os.fstat(final_guard))
            != temporary_identity
        ):
            raise ValueError(
                f"published destination identity changed: {destination}"
            )
    except BaseException as operation_error:
        close_error = None
        if final_guard is not None:
            descriptor = final_guard
            final_guard = None
            try:
                os.close(descriptor)
            except BaseException as exc:
                close_error = exc
        rollback_error = None
        if replace_started:
            try:
                _rollback_publication(
                    destination,
                    backup,
                    directory_anchor,
                )
            except BaseException as exc:
                rollback_error = exc
        if close_error is not None and hasattr(operation_error, "add_note"):
            operation_error.add_note(
                f"final guard close also failed: {close_error!r}"
            )
        if rollback_error is not None:
            if hasattr(rollback_error, "add_note"):
                rollback_error.add_note(
                    f"publication failure was: {operation_error!r}"
                )
            raise rollback_error from operation_error
        raise
    finally:
        cleanup_errors = []
        if final_guard is not None:
            try:
                os.close(final_guard)
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            os.close(guard)
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            _discard_publication_backup(backup)
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            active_error = sys.exc_info()[1]
            if active_error is None:
                raise cleanup_errors[0]
            if hasattr(active_error, "add_note"):
                for cleanup_error in cleanup_errors:
                    active_error.add_note(
                        f"additional publication cleanup error: "
                        f"{cleanup_error!r}"
                    )


def _open_anchored_source(
        anchor: _DirectoryAnchor,
        destination: PurePosixPath,
) -> int:
    if os.name == "nt":
        anchor.require_path_identity()
        source = anchor.path.joinpath(*destination.parts)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        descriptor = None
        try:
            descriptor = os.open(source, flags)
            anchor.verify_file_descriptor(descriptor, destination)
            result = descriptor
            descriptor = None
            return result
        except OSError as exc:
            raise ValueError(
                f"portable source changed during anchored open: {destination}"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = None
    descriptor = None
    try:
        anchor.require_path_identity()
        current = anchor.duplicate_posix_descriptor()
        for component in destination.parts[:-1]:
            child = os.open(
                component,
                directory_flags,
                dir_fd=current,
            )
            try:
                os.close(current)
            except OSError:
                os.close(child)
                raise
            current = child
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            destination.parts[-1],
            file_flags,
            dir_fd=current,
        )
        os.close(current)
        current = None
        anchor.verify_file_descriptor(descriptor, destination)
        result = descriptor
        descriptor = None
        return result
    except (OSError, TypeError, NotImplementedError) as exc:
        raise ValueError(
            f"portable source changed or escaped anchored containment: "
            f"{destination}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if current is not None:
            os.close(current)


def _normalized_windows_handle_path(descriptor: int) -> str:
    import msvcrt

    return _windows_final_handle_path(msvcrt.get_osfhandle(descriptor))


def _stable_source_bytes(
        source: Path,
        *,
        root: Path,
        destination: PurePosixPath,
        anchor: _DirectoryAnchor,
        expected_identity: tuple[int, int, int, int | None] | None = None,
) -> bytes:
    reviewed = _validated_source(
        root,
        destination,
        directory=False,
    )
    if _absolute(reviewed) != _absolute(source):
        raise ValueError(
            f"portable source is outside its reviewed destination: {destination}"
        )
    before = _require_real_kind(reviewed, directory=False)
    before_identity = _source_identity(before)
    if (
        expected_identity is not None
        and before_identity != expected_identity
    ):
        raise ValueError(
            f"portable source identity changed after review: {destination}"
        )
    descriptor = _open_anchored_source(anchor, destination)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"portable source changed type: {destination}")
        if _source_identity(opened) != before_identity:
            raise ValueError(
                f"portable source identity changed during open: {destination}"
            )
        if os.name == "nt":
            expected_path = _normalize_windows_path(_absolute(reviewed))
            if _normalized_windows_handle_path(descriptor) != expected_path:
                raise ValueError(
                    "portable source escaped anchored containment: "
                    f"{destination}"
                )
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size != opened.st_size
            or getattr(after, "st_mtime_ns", None)
            != getattr(opened, "st_mtime_ns", None)
        ):
            raise ValueError(
                f"portable source changed during read: {destination}"
            )
        path_after = _validated_source(
            root,
            destination,
            directory=False,
        )
        if _source_identity(
                _require_real_kind(path_after, directory=False)
        ) != _source_identity(opened):
            raise ValueError(
                f"portable source identity changed after read: {destination}"
            )
        anchor.require_path_identity()
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _normalized_member_bytes(
        source: Path,
        destination: PurePosixPath,
        *,
        root: Path,
        anchor: _DirectoryAnchor,
        expected_identity: tuple[int, int, int, int | None],
) -> bytes:
    data = _stable_source_bytes(
        source,
        root=root,
        destination=destination,
        anchor=anchor,
        expected_identity=expected_identity,
    )
    return _normalized_bytes(data, destination)


def _normalized_bytes(
        data: bytes,
        destination: PurePosixPath,
) -> bytes:
    suffix = destination.suffix.casefold()
    if suffix in _BINARY_SUFFIXES:
        return data
    text = data.decode("utf-8", errors="strict")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if suffix in _CRLF_SUFFIXES:
        normalized = normalized.replace("\n", "\r\n")
    return normalized.encode("utf-8")


def _zip_info(
        filename: str,
        date_time: tuple[int, int, int, int, int, int],
        mode: int,
) -> ZipInfo:
    info = ZipInfo(filename=filename, date_time=date_time)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.flag_bits = 0
    info.volume = 0
    info.internal_attr = 0
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.extra = b""
    info.comment = b""
    return info


def build_portable(
        repo_root: Path,
        output_dir: Path,
        epoch: int,
) -> Path:
    """Build and atomically replace the deterministic portable ZIP."""
    root = _absolute(Path(repo_root))
    with _DirectoryAnchor(
            root,
            label="portable repository root",
    ) as root_anchor:
        date_time = _dos_datetime(epoch)
        output = _absolute(Path(output_dir))
        declared = _read_allowlist_entries(
            root,
            anchor=root_anchor,
        )
        _reject_recursive_output_overlap(root, output, declared)
        version, version_identity, version_bytes = _read_version(
            root,
            anchor=root_anchor,
        )
        members = _expand_allowlist_entries(root, declared)
        snapshots = []
        version_destination = PurePosixPath("Final/_version.py")
        for source, relative in members:
            reviewed_source = _validated_source(
                root,
                relative,
                directory=False,
            )
            reviewed_identity = _source_identity(
                _require_real_kind(reviewed_source, directory=False)
            )
            if relative == version_destination:
                if reviewed_identity != version_identity:
                    raise ValueError(
                        "Final/_version.py identity changed after "
                        "version review"
                    )
                member_bytes = _normalized_bytes(
                    version_bytes,
                    relative,
                )
            else:
                member_bytes = _normalized_member_bytes(
                    source,
                    relative,
                    root=root,
                    anchor=root_anchor,
                    expected_identity=reviewed_identity,
                )
            snapshots.append((relative, member_bytes))
        root_anchor.require_path_identity()

        output.mkdir(parents=True, exist_ok=True)
        with _DirectoryAnchor(
                output,
                label="portable output directory",
        ) as output_anchor:
            _reject_recursive_output_overlap(root, output, declared)
            root_anchor.require_path_identity()
            filename = f"yt_final-{version}-portable.zip"
            destination = output / filename
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{filename}.",
                suffix=".tmp",
                dir=output,
            )
            temporary = Path(temporary_name)
            temporary_identity = _object_identity(os.fstat(descriptor))
            prefix = PurePosixPath(f"yt_final-{version}")
            expected_digest = None
            try:
                output_anchor.verify_file_descriptor(
                    descriptor,
                    PurePosixPath(temporary.name),
                )
                with os.fdopen(descriptor, "w+b") as stream:
                    descriptor = -1
                    with ZipFile(
                            stream,
                            mode="w",
                            compression=ZIP_DEFLATED,
                            compresslevel=9,
                            strict_timestamps=True) as archive:
                        archive.comment = b""
                        for relative, member_bytes in snapshots:
                            archive_name = (prefix / relative).as_posix()
                            mode = (
                                0o755
                                if relative.suffix == ".command"
                                else 0o644
                            )
                            archive.writestr(
                                _zip_info(archive_name, date_time, mode),
                                member_bytes,
                                compress_type=ZIP_DEFLATED,
                                compresslevel=9,
                            )
                    stream.flush()
                    os.fsync(stream.fileno())
                    _require_temporary_identity(
                        temporary,
                        temporary_identity,
                        stream.fileno(),
                    )
                    output_anchor.verify_file_descriptor(
                        stream.fileno(),
                        PurePosixPath(temporary.name),
                    )
                    expected_digest = _digest_descriptor(
                        stream.fileno()
                    )
                _verified_atomic_replace(
                    temporary,
                    temporary_identity,
                    destination,
                    expected_digest,
                    output_anchor,
                )
                root_anchor.require_path_identity()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                _unlink_owned_temporary(
                    temporary,
                    temporary_identity,
                )
            return destination


def _required_cli_epoch(parser: argparse.ArgumentParser, value: int | None) -> int:
    if value is not None:
        return value
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        parser.error("--epoch or SOURCE_DATE_EPOCH is required")
    try:
        return int(raw, 10)
    except ValueError:
        parser.error("SOURCE_DATE_EPOCH must be an integer")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the deterministic yt_final portable ZIP.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--epoch", type=int)
    arguments = parser.parse_args(argv)
    epoch = _required_cli_epoch(parser, arguments.epoch)
    artifact = build_portable(
        arguments.repo_root,
        arguments.output_dir,
        epoch,
    )
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
