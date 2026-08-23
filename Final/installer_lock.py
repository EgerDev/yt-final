"""Process-wide and OS-wide serialization for managed installer mutations."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import importlib
import os
from pathlib import Path
import stat
import sys
import threading
from typing import Iterator


INSTALLER_BUSY_CODE = "busy"
INSTALLER_BUSY_MESSAGE = "Another managed installation is already in progress"


class InstallerBusyError(RuntimeError):
    """Raised when another managed installer owns or is changing the guard."""


class UnsafeInstallerRootError(RuntimeError):
    """Raised before installer I/O when the application root is link-like."""


# Both import styles are supported throughout the application. Register the
# first-loaded module under the other name so they cannot grow separate state.
_MODULE = sys.modules[__name__]
if __name__ == "installer_lock":
    sys.modules.setdefault("Final.installer_lock", _MODULE)
elif __name__ == "Final.installer_lock":
    sys.modules.setdefault("installer_lock", _MODULE)


_STATE_MUTEX = threading.Lock()
_state = "IDLE"
_owner: tuple[int, int] | None = None
_depth = 0
_handle = None
_root: Path | None = None


def _default_root() -> Path:
    try:
        updater = importlib.import_module("ffmpeg_updater")
    except ImportError:  # pragma: no cover - package import style
        updater = importlib.import_module("Final.ffmpeg_updater")
    return getattr(updater, "app_data_dir")()


def _normalized_root(root: Path | None) -> Path:
    selected = _default_root() if root is None else Path(root)
    normalized = Path(os.path.abspath(os.fspath(selected.expanduser())))
    _validate_lock_path(normalized)
    return normalized


def validated_private_root(root: Path) -> Path:
    """Return a lexical private root only when every existing ancestor is real."""
    return _normalized_root(Path(root))


def _validate_lock_path(root: Path) -> None:
    """Validate every existing component before mkdir can follow a link."""
    current = Path(root)
    while True:
        _validate_lock_root(current)
        parent = current.parent
        if parent == current:
            return
        current = parent


def _validate_lock_root(root: Path) -> None:
    """Reject a pre-existing symlink, junction, reparse point, or non-directory."""
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UnsafeInstallerRootError(
            "Managed installer root could not be validated"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    link_like = root.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )
    is_junction = getattr(root, "is_junction", None)
    if is_junction is not None:
        try:
            link_like = link_like or bool(is_junction())
        except OSError:
            link_like = True
    if link_like or not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeInstallerRootError(
            "Managed installer root must be a real application directory"
        )


def _unsafe_lock_file() -> UnsafeInstallerRootError:
    return UnsafeInstallerRootError(
        "Managed installer lock must be a private regular file"
    )


def _validate_lock_file_metadata(lock_path: Path, metadata) -> None:
    """Reject link-like, non-regular, or multiply-linked lock leaves."""
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        or getattr(metadata, "st_nlink", 1) != 1
    ):
        raise _unsafe_lock_file()
    is_junction = getattr(lock_path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                raise _unsafe_lock_file()
        except UnsafeInstallerRootError:
            raise
        except OSError as exc:
            raise _unsafe_lock_file() from exc


def _same_file_identity(first, second) -> bool:
    return (
        getattr(first, "st_dev", None),
        getattr(first, "st_ino", None),
    ) == (
        getattr(second, "st_dev", None),
        getattr(second, "st_ino", None),
    )


def _open_validated_lock_file(lock_path: Path):
    """Open a private lock leaf without following or creating over an entry."""
    base_flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    base_flags |= getattr(os, "O_NOFOLLOW", 0)
    base_flags |= getattr(os, "O_CLOEXEC", 0)

    for _attempt in range(2):
        try:
            before = lock_path.lstat()
        except FileNotFoundError:
            before = None
        except OSError as exc:
            raise _unsafe_lock_file() from exc

        if before is not None:
            _validate_lock_file_metadata(lock_path, before)
            flags = base_flags
        else:
            flags = base_flags | os.O_CREAT | os.O_EXCL

        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except FileExistsError:
            if before is None:
                continue
            raise _unsafe_lock_file()
        except OSError as exc:
            raise _unsafe_lock_file() from exc

        try:
            opened = os.fstat(descriptor)
            _validate_lock_file_metadata(lock_path, opened)
            _validate_lock_path(lock_path.parent)
            after = lock_path.lstat()
            _validate_lock_file_metadata(lock_path, after)
            if not _same_file_identity(opened, after):
                raise _unsafe_lock_file()
            return os.fdopen(descriptor, "r+b")
        except BaseException:
            os.close(descriptor)
            raise

    raise _unsafe_lock_file()


def _acquire_file_lock(lock_path: Path):
    _validate_lock_path(lock_path.parent)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_lock_path(lock_path.parent)
    handle = _open_validated_lock_file(lock_path)
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if (
                    getattr(exc, "winerror", None) in (33, 36)
                    or exc.errno in (errno.EACCES, errno.EAGAIN)
                ):
                    raise InstallerBusyError(INSTALLER_BUSY_MESSAGE) from exc
                raise
        else:
            fcntl = importlib.import_module("fcntl")

            try:
                getattr(fcntl, "flock")(
                    handle.fileno(),
                    getattr(fcntl, "LOCK_EX") | getattr(fcntl, "LOCK_NB"),
                )
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise InstallerBusyError(INSTALLER_BUSY_MESSAGE) from exc
                raise
        return handle
    except BaseException:
        handle.close()
        raise


def _release_file_lock(handle) -> None:
    error: BaseException | None = None
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            getattr(fcntl, "flock")(
                handle.fileno(),
                getattr(fcntl, "LOCK_UN"),
            )
    except BaseException as exc:
        error = exc
    try:
        handle.close()
    except BaseException as exc:
        if error is None:
            error = exc
    if error is not None:
        raise error


@contextmanager
def installer_guard(root: Path | None = None) -> Iterator[None]:
    """Acquire the shared nonblocking installer lock.

    Re-entry is allowed only for the exact owning process/thread and root.
    Other callers fail immediately rather than waiting behind an installer.
    """
    global _state, _owner, _depth, _handle, _root

    selected_root = _normalized_root(root)
    identity = (os.getpid(), threading.get_ident())
    nested = False

    with _STATE_MUTEX:
        if _state == "OWNED" and _owner == identity:
            if _root != selected_root:
                raise ValueError(
                    "nested installer_guard must use the owning installer root"
                )
            _depth += 1
            nested = True
        elif _state != "IDLE":
            raise InstallerBusyError(INSTALLER_BUSY_MESSAGE)
        else:
            _state = "ACQUIRING"
            _owner = identity
            _root = selected_root

    if nested:
        try:
            yield
        finally:
            with _STATE_MUTEX:
                _depth -= 1
        return

    acquired_handle = None
    try:
        acquired_handle = _acquire_file_lock(selected_root / "installer.lock")
    except BaseException:
        with _STATE_MUTEX:
            if _state == "ACQUIRING" and _owner == identity:
                _state = "IDLE"
                _owner = None
                _root = None
        raise

    with _STATE_MUTEX:
        _handle = acquired_handle
        _depth = 1
        _state = "OWNED"

    try:
        yield
    finally:
        body_failed = sys.exc_info()[0] is not None
        release_handle = None
        with _STATE_MUTEX:
            _depth -= 1
            if _depth == 0:
                _state = "RELEASING"
                release_handle = _handle
                _handle = None

        cleanup_error: BaseException | None = None
        if release_handle is not None:
            try:
                _release_file_lock(release_handle)
            except BaseException as exc:
                cleanup_error = exc
            finally:
                with _STATE_MUTEX:
                    _state = "IDLE"
                    _owner = None
                    _root = None

        if cleanup_error is not None and not body_failed:
            raise cleanup_error
