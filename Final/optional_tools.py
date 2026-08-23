"""Certified Node/Deno runtimes, discovery, and manual-only tool guidance."""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import os
import platform as py_platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from ffmpeg_updater import app_data_dir, HTTPSOnlyRedirectHandler
except ImportError:  # pragma: no cover
    from Final.ffmpeg_updater import (  # type: ignore
        app_data_dir, HTTPSOnlyRedirectHandler,
    )

try:
    from installer_lock import (
        INSTALLER_BUSY_CODE,
        INSTALLER_BUSY_MESSAGE,
        InstallerBusyError,
        UnsafeInstallerRootError,
        installer_guard,
        validated_private_root,
    )
except ImportError:  # pragma: no cover
    from Final.installer_lock import (  # type: ignore
        INSTALLER_BUSY_CODE,
        INSTALLER_BUSY_MESSAGE,
        InstallerBusyError,
        UnsafeInstallerRootError,
        installer_guard,
        validated_private_root,
    )

try:
    from tool_manifest import (
        ChecksumError,
        ToolArtifact,
        UnsupportedManagedInstall,
        artifact_for,
        verify_downloaded_artifact,
        verify_sha256,
    )
except ImportError:  # pragma: no cover
    from Final.tool_manifest import (  # type: ignore
        ChecksumError,
        ToolArtifact,
        UnsupportedManagedInstall,
        artifact_for,
        verify_downloaded_artifact,
        verify_sha256,
    )


DOWNLOAD_TIMEOUT = 60
DOWNLOAD_TOTAL_TIMEOUT = 180
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_RUNTIME_BINARY_BYTES = 256 * 1024 * 1024


class OptionalToolError(RuntimeError):
    def __init__(self, message: str, *, code: str = 'failed'):
        super().__init__(message)
        self.code = code


_CHECKSUM_FAILURE_MESSAGE = (
    'Certified managed artifact failed checksum verification'
)
_UNSUPPORTED_FAILURE_MESSAGE = (
    'Certified managed installation is unavailable for this platform or artifact'
)
_NETWORK_FAILURE_MESSAGE = 'Certified managed artifact download failed'
_INSTALL_FAILURE_MESSAGE = 'Certified managed tool installation failed'


def _manifest_failure_result(
    tool: str,
    failure: ChecksumError | UnsupportedManagedInstall,
) -> dict:
    if isinstance(failure, ChecksumError):
        code = 'checksum'
        message = _CHECKSUM_FAILURE_MESSAGE
    else:
        code = 'unsupported'
        message = _UNSUPPORTED_FAILURE_MESSAGE
    return {
        'ok': False,
        'tool': tool,
        'code': code,
        'error': message,
    }


MANUAL_INSTALL_GUIDANCE = {
    'aria2c': (
        'Automatic aria2c installation is unavailable because no certified '
        'binary digest is published; install aria2c manually.'
    ),
    'N_m3u8DL-RE': (
        'Automatic N_m3u8DL-RE installation is unavailable because its '
        'download redirects cannot be pinned; install it manually.'
    ),
    'playwright': (
        'Automatic Playwright browser installation is unavailable without '
        'a packaged immutable browser manifest; install it manually.'
    ),
    'bgutil-server': (
        'Automatic bgutil server installation is unavailable without a '
        'certified immutable server artifact; install it manually.'
    ),
}

PYTHON_LOCK_ONLY_RESULT = {
    'ok': False,
    'tool': 'bgutil-plugin',
    'status': 'reviewed-release',
    'code': 'certified-runtime-unavailable',
    'message': (
        'Install a future reviewed yt_final release to change the bgutil '
        'Python provider; in-app Python mutation is disabled in 1.0.0.'
    ),
}


def _manual_result(tool: str, message: str | None = None) -> dict:
    return {
        'ok': False,
        'tool': tool,
        'status': 'manual',
        'code': 'unsupported',
        'message': message or MANUAL_INSTALL_GUIDANCE[tool],
    }


def managed_tools_dir() -> Path:
    return app_data_dir() / 'tools'


def managed_aria2_path() -> Path:
    name = 'aria2c.exe' if sys.platform.startswith('win') else 'aria2c'
    return managed_tools_dir() / 'aria2' / name


def managed_turbo_path() -> Path:
    name = 'N_m3u8DL-RE.exe' if sys.platform.startswith('win') else 'N_m3u8DL-RE'
    return managed_tools_dir() / 'N_m3u8DL-RE' / name


def managed_bgutil_repo_dir() -> Path:
    """App-private clone of brainicism/bgutil-ytdlp-pot-provider (script-mode PO tokens)."""
    return app_data_dir() / 'bgutil-ytdlp-pot-provider'


def managed_bgutil_script_path() -> Path:
    """generate_once.js used by yt-dlp's bgutil script provider (Windows-style, no Docker)."""
    return (
        managed_bgutil_repo_dir()
        / 'server' / 'build' / 'generate_once.js'
    )


def find_bgutil_script_path() -> Path | None:
    """Locate generate_once.js: managed app dir first, then ~/bgutil-ytdlp-pot-provider (Windows home layout)."""
    candidates = [
        managed_bgutil_script_path(),
        Path.home() / 'bgutil-ytdlp-pot-provider' / 'server' / 'build' / 'generate_once.js',
    ]
    # Windows users sometimes keep the clone under the profile with a capital B
    if sys.platform.startswith('win'):
        candidates.append(
            Path.home() / 'Bgutil-ytdlp-pot-provider' / 'server' / 'build' / 'generate_once.js'
        )
    for p in candidates:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def installable_tools() -> frozenset[str]:
    """Tool names that the Updates UI may install (matches report info.name)."""
    return frozenset({'deno', 'node'})


def probe_playwright() -> dict:
    """Return {version, chromium, importable} for Auth Login / Updates scan."""
    try:
        try:
            from managed_env import inject_managed_site_packages
        except ImportError:
            from Final.managed_env import inject_managed_site_packages  # type: ignore
        inject_managed_site_packages()
    except Exception:
        pass
    ver = None
    importable = False
    chromium = False
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            ver = version('playwright')
        except PackageNotFoundError:
            ver = None
    except Exception:
        ver = None
    try:
        import playwright  # noqa: F401
        importable = True
    except Exception:
        importable = False
    if importable:
        # Heuristic: browsers live under ms-playwright or PLAYWRIGHT_BROWSERS_PATH
        roots = []
        env = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
        if env:
            roots.append(Path(env))
        home = Path.home()
        roots.extend([
            home / 'Library' / 'Caches' / 'ms-playwright',
            home / '.cache' / 'ms-playwright',
            home / 'AppData' / 'Local' / 'ms-playwright',
        ])
        for root in roots:
            try:
                if root.is_dir() and any(root.glob('chromium-*')):
                    chromium = True
                    break
            except Exception:
                continue
    return {
        'version': ver if importable else None,
        'importable': importable,
        'chromium': chromium,
    }


class _RejectArtifactRedirects(HTTPSOnlyRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UnsupportedManagedInstall(
            'Certified managed artifacts do not permit redirects'
        )


_ARTIFACT_OPENER = urllib.request.build_opener(_RejectArtifactRedirects())


def _download_verified_artifact(
    artifact: ToolArtifact,
    *,
    opener=None,
    limit: int = MAX_ARCHIVE_BYTES,
    clock=None,
    total_timeout: float = DOWNLOAD_TOTAL_TIMEOUT,
) -> bytes:
    clock = clock or time.monotonic
    started_at = clock()

    def require_time_remaining() -> None:
        if clock() - started_at >= total_timeout:
            raise OptionalToolError(
                _NETWORK_FAILURE_MESSAGE,
                code='network',
            )

    if opener is None:
        request = urllib.request.Request(
            artifact.url,
            headers={
                'User-Agent': 'yt_final-optional-tools/1.0',
                'Accept': 'application/octet-stream',
            },
        )
        response = _ARTIFACT_OPENER.open(request, timeout=DOWNLOAD_TIMEOUT)
    else:
        response = opener(artifact.url, DOWNLOAD_TIMEOUT)
    with response:
        if not hasattr(response, 'geturl'):
            raise UnsupportedManagedInstall(
                'Downloaded artifact response did not report its final URL'
            )
        final_url = response.geturl()
        if final_url != artifact.url:
            raise UnsupportedManagedInstall(
                'Downloaded artifact final URL does not match its approved direct URL'
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            require_time_remaining()
            chunk = response.read(
                min(DOWNLOAD_CHUNK_BYTES, limit - total + 1)
            )
            require_time_remaining()
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise OptionalToolError(
                    'Download exceeds size limit',
                    code='archive',
                )
            chunks.append(chunk)
        payload = b''.join(chunks)
    verify_downloaded_artifact(
        artifact,
        payload,
        final_url=final_url,
        redirected=final_url != artifact.url,
    )
    return payload


def _safe_unpack(
    data: bytes,
    dest: Path,
    *,
    archive_kind: str,
) -> None:
    """Extract regular files with only the manifest-declared archive parser.

    Symlinks, hardlinks, devices, and members escaping ``dest`` are skipped.
    Parser errors propagate when the bytes do not match ``archive_kind``.
    """
    dest = Path(dest).resolve()

    def _target(name: str):
        t = (dest / name).resolve()
        return t if dest in t.parents else None      # inside dest at any depth, else reject

    if archive_kind == 'tar.gz':
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue                          # skip dirs, symlinks, hardlinks, devices
                t = _target(m.name)
                src = tf.extractfile(m) if t is not None else None
                if src is None:
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                with src, t.open('wb') as out:
                    shutil.copyfileobj(src, out)
        return
    if archive_kind == 'zip':
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                entry_type = stat.S_IFMT(mode)
                if entry_type not in (0, stat.S_IFREG):
                    continue
                t = _target(info.filename.replace('\\', '/'))
                if t is None:
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, t.open('wb') as out:
                    shutil.copyfileobj(src, out)
        return
    raise ValueError(f'Unsupported archive kind: {archive_kind}')


def _chmod_exec(path: Path) -> None:
    if sys.platform.startswith('win'):
        return
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    if sys.platform == 'darwin':
        xattr = Path('/usr/bin/xattr')
        if not xattr.is_file():
            return
        try:
            subprocess.run(
                [str(xattr), '-dr', 'com.apple.quarantine', str(path)],
                capture_output=True, timeout=5, check=False,
            )
        except Exception:
            pass


_ACTIVE_RELEASE_NAME = re.compile(r'\d+(?:\.\d+)*-[0-9a-f]{64}\Z')
_REPARSE_POINT_FLAG = getattr(
    stat,
    'FILE_ATTRIBUTE_REPARSE_POINT',
    0x400,
)
_ACTIVATION_FAILURE_MESSAGE = (
    'Certified runtime activation failed; previous installation remains unchanged'
)
_ACTIVATION_ROLLBACK_FAILURE_MESSAGE = (
    'Certified runtime activation could not be verified; candidate files were retained'
)


def _runtime_binary_relative(tool: str, platform_name: str | None = None) -> Path:
    platform_name = platform_name or sys.platform
    if tool == 'node':
        if platform_name.startswith('win'):
            return Path('node.exe')
        return Path('bin') / 'node'
    if tool == 'deno':
        if platform_name.startswith('win'):
            return Path('bin') / 'deno.exe'
        return Path('bin') / 'deno'
    raise ValueError(f'Unknown managed runtime: {tool}')


def _runtime_archive_relative(artifact: ToolArtifact) -> Path:
    suffix = 'tar.gz' if artifact.archive_kind == 'tar.gz' else 'zip'
    return Path(f'approved-artifact.{suffix}')


def _hash_stream(stream) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _approved_archive_binary_sha256(
    artifact: ToolArtifact,
    archive_bytes: bytes,
    tool: str,
) -> str:
    """Derive the executable digest from the manifest-authenticated archive."""
    names = {'node.exe', 'node'} if tool == 'node' else {'deno.exe', 'deno'}
    matches: list[str] = []
    if artifact.archive_kind == 'zip':
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for info in archive.infolist():
                if info.is_dir() or Path(info.filename).name not in names:
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                entry_type = stat.S_IFMT(mode)
                if entry_type not in (0, stat.S_IFREG):
                    raise _activation_error()
                with archive.open(info, 'r') as stream:
                    matches.append(_hash_stream(stream))
    elif artifact.archive_kind == 'tar.gz':
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as archive:
            for member in archive.getmembers():
                if Path(member.name).name not in names:
                    continue
                if not member.isfile():
                    raise _activation_error()
                stream = archive.extractfile(member)
                if stream is None:
                    raise _activation_error()
                with stream:
                    matches.append(_hash_stream(stream))
    else:
        raise _activation_error()
    if len(matches) != 1:
        raise _activation_error()
    return matches[0]


def _read_safe_regular_bytes(
    trusted_root: Path,
    path: Path,
    *,
    limit: int,
) -> bytes:
    """Read one already-contained regular file without reopening its pathname."""
    _require_safe_regular_beneath(trusted_root, path)
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size <= 0
            or opened.st_size > limit
        ):
            raise _activation_error()
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            payload = stream.read(limit + 1)
        after = os.fstat(descriptor)
        if (
            len(payload) > limit
            or len(payload) != opened.st_size
            or (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
            ) != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise _activation_error()
        return payload
    except OSError as exc:
        raise _activation_error() from exc
    finally:
        os.close(descriptor)


def _sha256_safe_regular_file(
    trusted_root: Path,
    path: Path,
    *,
    limit: int,
) -> str:
    """Stream-hash the exact checked file descriptor with bounded memory."""
    _require_safe_regular_beneath(trusted_root, path)
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size <= 0
            or opened.st_size > limit
        ):
            raise _activation_error()
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise _activation_error()
                digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            total != opened.st_size
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise _activation_error()
        return digest.hexdigest()
    except OSError as exc:
        raise _activation_error() from exc
    finally:
        os.close(descriptor)


def _installed_runtime_matches_approved_archive(
    artifact: ToolArtifact,
    release_directory: Path,
    selected: Path,
    tool: str,
) -> bool:
    try:
        archive_path = release_directory / _runtime_archive_relative(artifact)
        archive_bytes = _read_safe_regular_bytes(
            release_directory,
            archive_path,
            limit=MAX_ARCHIVE_BYTES,
        )
        installed_binary = _sha256_safe_regular_file(
            release_directory,
            selected,
            limit=MAX_RUNTIME_BINARY_BYTES,
        )
        verify_sha256(archive_bytes, artifact.sha256)
        archive_binary = _approved_archive_binary_sha256(
            artifact,
            archive_bytes,
            tool,
        )
        return (
            hmac.compare_digest(archive_binary, artifact.binary_sha256)
            and hmac.compare_digest(installed_binary, artifact.binary_sha256)
        )
    except (
        ChecksumError,
        OptionalToolError,
        OSError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ):
        return False


def _installed_runtime_matches_manifest(
    artifact: ToolArtifact,
    release_directory: Path,
    selected: Path,
) -> bool:
    """Authenticate an execution handoff without reopening the retained archive."""
    try:
        installed_binary = _sha256_safe_regular_file(
            release_directory,
            selected,
            limit=MAX_RUNTIME_BINARY_BYTES,
        )
        return hmac.compare_digest(installed_binary, artifact.binary_sha256)
    except (OptionalToolError, OSError):
        return False


def _legacy_managed_runtime_path(tool: str) -> Path:
    return managed_tools_dir() / tool / _runtime_binary_relative(tool)


def _activation_error() -> OptionalToolError:
    return OptionalToolError(
        _ACTIVATION_FAILURE_MESSAGE,
        code='activate',
    )


def _path_entry_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _path_is_link_or_reparse(path: Path, metadata=None) -> bool:
    if metadata is None:
        metadata = path.lstat()
    if path.is_symlink():
        return True
    if (
        getattr(metadata, 'st_file_attributes', 0)
        & _REPARSE_POINT_FLAG
    ):
        return True
    is_junction = getattr(path, 'is_junction', None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    return False


def _resolved_within(path: Path, trusted_root: Path) -> bool:
    try:
        resolved_root = trusted_root.resolve(strict=True)
        resolved_path = path.resolve(strict=False)
        resolved_path.relative_to(resolved_root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _safe_directory_entry(path: Path, trusted_root: Path) -> bool:
    try:
        metadata = path.lstat()
        return (
            stat.S_ISDIR(metadata.st_mode)
            and not _path_is_link_or_reparse(path, metadata)
            and _resolved_within(path, trusted_root)
        )
    except OSError:
        return False


def _safe_regular_entry(path: Path, trusted_root: Path) -> bool:
    try:
        metadata = path.lstat()
        return (
            stat.S_ISREG(metadata.st_mode)
            and not _path_is_link_or_reparse(path, metadata)
            and _resolved_within(path, trusted_root)
        )
    except OSError:
        return False


def _safe_directory_chain(trusted_root: Path, directory: Path) -> bool:
    try:
        relative = directory.relative_to(trusted_root)
    except ValueError:
        return False
    if not _safe_directory_entry(trusted_root, trusted_root):
        return False
    current = trusted_root
    for component in relative.parts:
        current = current / component
        if not _safe_directory_entry(current, trusted_root):
            return False
    return True


def _safe_regular_beneath(trusted_root: Path, path: Path) -> bool:
    return (
        _safe_directory_chain(trusted_root, path.parent)
        and _safe_regular_entry(path, trusted_root)
    )


def _require_safe_directory_chain(trusted_root: Path, directory: Path) -> None:
    if not _safe_directory_chain(trusted_root, directory):
        raise _activation_error()


def _require_safe_regular_beneath(trusted_root: Path, path: Path) -> None:
    if not _safe_regular_beneath(trusted_root, path):
        raise _activation_error()


def _require_safe_partial_runtime_release(
    trusted_root: Path,
    release_directory: Path,
    destination: Path,
) -> None:
    """Validate a contained release without trusting an absent executable."""
    try:
        release_directory.relative_to(trusted_root)
        relative_destination = destination.relative_to(release_directory)
    except ValueError as exc:
        raise _activation_error() from exc
    if not _resolved_within(release_directory, trusted_root):
        raise _activation_error()
    if not _path_entry_exists(release_directory):
        _require_safe_directory_chain(
            trusted_root,
            release_directory.parent,
        )
        return
    _require_safe_directory_chain(trusted_root, release_directory)
    if _path_entry_exists(destination):
        _require_safe_regular_beneath(release_directory, destination)
        return
    current = release_directory
    for component in relative_destination.parent.parts:
        if component in ('', '.'):
            continue
        current = current / component
        if not _path_entry_exists(current):
            break
        _require_safe_directory_chain(release_directory, current)


def _require_existing_runtime_layout_safe(
    tool: str,
    tools_root: Path,
    root: Path,
    versions: Path,
    pointer: Path,
    release_directory: Path,
    destination: Path,
    *,
    allow_partial_release: bool = False,
) -> bytes | None:
    """Reject a known unsafe persistent layout before artifact network I/O."""
    try:
        if _path_entry_exists(tools_root):
            _require_safe_directory_chain(tools_root, tools_root)
        if _path_entry_exists(root):
            _require_safe_directory_chain(tools_root, root)
        if _path_entry_exists(versions):
            _require_safe_directory_chain(tools_root, versions)
        if _path_entry_exists(pointer):
            _require_safe_regular_beneath(tools_root, pointer)
            if pointer.stat().st_size > 256:
                raise _activation_error()
            pointer_text = pointer.read_text(encoding='ascii')
            if not pointer_text.endswith('\n'):
                raise _activation_error()
            active_release = pointer_text[:-1]
            if (
                '\n' in active_release
                or not _ACTIVE_RELEASE_NAME.fullmatch(active_release)
            ):
                raise _activation_error()
            active_directory = versions / active_release
            active_binary = active_directory / _runtime_binary_relative(tool)
            if (
                allow_partial_release
                and active_directory == release_directory
            ):
                _require_safe_partial_runtime_release(
                    root,
                    active_directory,
                    active_binary,
                )
            else:
                _require_safe_directory_chain(root, active_directory)
                _require_safe_regular_beneath(
                    active_directory,
                    active_binary,
                )
            previous_pointer = pointer.read_bytes()
        else:
            previous_pointer = None
        if _path_entry_exists(release_directory):
            if allow_partial_release:
                _require_safe_partial_runtime_release(
                    root,
                    release_directory,
                    destination,
                )
            else:
                _require_safe_directory_chain(root, release_directory)
                _require_safe_regular_beneath(
                    release_directory,
                    destination,
                )
        return previous_pointer
    except OptionalToolError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _activation_error() from exc


def _restore_runtime_pointer(
    root: Path,
    pointer: Path,
    previous_pointer: bytes | None,
) -> bool:
    """Best-effort atomic rollback after the new pointer was already promoted."""
    temporary_pointer: Path | None = None
    try:
        _require_safe_directory_chain(root, root)
        if previous_pointer is None:
            if _path_entry_exists(pointer):
                pointer.unlink()
            return not _path_entry_exists(pointer)
        descriptor, pointer_name = tempfile.mkstemp(
            prefix='.current-rollback-',
            dir=root,
        )
        temporary_pointer = Path(pointer_name)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(previous_pointer)
            stream.flush()
            os.fsync(stream.fileno())
        _require_safe_regular_beneath(root, temporary_pointer)
        os.replace(temporary_pointer, pointer)
        temporary_pointer = None
        return True
    except (OSError, OptionalToolError):
        return False
    finally:
        if temporary_pointer is not None:
            try:
                temporary_pointer.unlink()
            except OSError:
                pass


def managed_runtime_path(tool: str) -> Path | None:
    """Return only a certified pointer-selected runtime suitable for execution."""
    try:
        artifact = artifact_for(tool, sys.platform, py_platform.machine())
    except UnsupportedManagedInstall:
        return None
    tools_root = managed_tools_dir()
    try:
        validated_private_root(tools_root.parent)
    except UnsafeInstallerRootError:
        return None
    root = tools_root / tool
    versions = root / 'versions'
    pointer = root / 'current'
    try:
        if (
            not _safe_directory_chain(tools_root, versions)
            or not _safe_regular_beneath(tools_root, pointer)
        ):
            return None
        pointer_text = pointer.read_text(encoding='ascii')
        if not pointer_text.endswith('\n'):
            return None
        release = pointer_text[:-1]
        if (
            '\n' in release
            or not _ACTIVE_RELEASE_NAME.fullmatch(release)
            or release != f'{artifact.version}-{artifact.sha256}'
        ):
            return None
        release_directory = versions / release
        selected = release_directory / _runtime_binary_relative(tool)
        if (
            not _safe_directory_chain(root, release_directory)
            or not _safe_regular_beneath(release_directory, selected)
            or not _resolved_within(release_directory, root)
            or not _resolved_within(selected, release_directory)
        ):
            return None
        if not _installed_runtime_matches_manifest(
            artifact,
            release_directory,
            selected,
        ):
            return None
        return selected
    except (OSError, UnicodeError, ValueError):
        pass
    return None


def discover_managed_runtime(tool: str) -> dict:
    """Report certified and legacy layouts without promoting legacy trust."""
    certified = managed_runtime_path(tool)
    if certified is not None:
        return {
            'path': str(certified),
            'verified': True,
            'source': 'certified-pointer',
        }
    legacy = _legacy_managed_runtime_path(tool)
    try:
        if legacy.is_file():
            return {
                'path': str(legacy),
                'verified': False,
                'source': 'legacy',
            }
    except OSError:
        pass
    return {
        'path': None,
        'verified': False,
        'source': 'missing',
    }


def _managed_runtime_path(tool: str) -> Path | None:
    """Backward-compatible private alias for certified execution lookup."""
    return managed_runtime_path(tool)


def _unique_archive_binary(root: Path, names: tuple[str, ...]) -> Path:
    matches = [
        candidate
        for candidate in root.rglob('*')
        if candidate.is_file() and candidate.name in names
    ]
    if len(matches) != 1:
        raise OptionalToolError(
            'Certified archive must contain exactly one expected binary',
            code='archive',
        )
    return matches[0]


def _probe_manifest_binary(
    tool: str,
    path: Path,
    expected_version: str,
    *,
    runner=subprocess.run,
) -> None:
    completed = runner(
        [str(path), '--version'],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    output = ((completed.stdout or '') + '\n' + (completed.stderr or '')).strip()
    if completed.returncode != 0:
        raise OptionalToolError(
            f'Certified {tool} binary failed its version probe',
            code='probe',
        )
    if tool == 'node':
        match = re.search(r'\bv?(\d+(?:\.\d+)+)\b', output)
    else:
        match = re.search(r'\bdeno\s+(\d+(?:\.\d+)+)\b', output, re.IGNORECASE)
    if match is None or match.group(1) != expected_version:
        raise OptionalToolError(
            f'Certified {tool} binary version does not match its manifest',
            code='probe',
        )


def _install_certified_runtime(
    tool: str,
    artifact: ToolArtifact,
    *,
    runner=subprocess.run,
    opener=None,
) -> dict:
    tools_root = managed_tools_dir()
    root = tools_root / tool
    versions = root / 'versions'
    pointer = root / 'current'
    release = f'{artifact.version}-{artifact.sha256}'
    release_directory = versions / release
    destination = release_directory / _runtime_binary_relative(tool)
    previous_pointer = _require_existing_runtime_layout_safe(
        tool,
        tools_root,
        root,
        versions,
        pointer,
        release_directory,
        destination,
        allow_partial_release=True,
    )
    payload = _download_verified_artifact(artifact, opener=opener)
    try:
        tools_root.mkdir(parents=True, exist_ok=True)
        _require_safe_directory_chain(tools_root, tools_root)
        root.mkdir(exist_ok=True)
        _require_safe_directory_chain(tools_root, root)
        versions.mkdir(exist_ok=True)
        _require_safe_directory_chain(tools_root, versions)
        if _path_entry_exists(pointer):
            _require_safe_regular_beneath(tools_root, pointer)
    except OptionalToolError:
        raise
    except OSError as exc:
        raise _activation_error() from exc

    if not _resolved_within(release_directory, root):
        raise _activation_error()
    names = ('node.exe', 'node') if tool == 'node' else ('deno.exe', 'deno')
    with tempfile.TemporaryDirectory(
        prefix=f'.{tool}-verified-',
        dir=versions,
    ) as temporary_name:
        temporary = Path(temporary_name)
        _require_safe_directory_chain(versions, temporary)
        extracted = temporary / 'extracted'
        extracted.mkdir()
        _require_safe_directory_chain(temporary, extracted)
        try:
            _safe_unpack(
                payload,
                extracted,
                archive_kind=artifact.archive_kind,
            )
        except (tarfile.TarError, zipfile.BadZipFile) as exc:
            raise OptionalToolError(
                'Certified artifact could not be safely extracted',
                code='archive',
            ) from exc
        source = _unique_archive_binary(extracted, names)
        _require_safe_regular_beneath(extracted, source)
        staged_release = temporary / 'release'
        staged = staged_release / _runtime_binary_relative(tool)
        staged.parent.mkdir(parents=True)
        shutil.copy2(source, staged)
        staged_archive = staged_release / _runtime_archive_relative(artifact)
        staged_archive.write_bytes(payload)
        _chmod_exec(staged)
        _require_safe_regular_beneath(staged_release, staged)
        _require_safe_regular_beneath(staged_release, staged_archive)
        if not _installed_runtime_matches_approved_archive(
            artifact,
            staged_release,
            staged,
            tool,
        ):
            raise _activation_error()
        _probe_manifest_binary(
            tool,
            staged,
            artifact.version,
            runner=runner,
        )
        promoted = False
        quarantined_release: Path | None = None

        def rollback_release_promotion() -> bool:
            """Restore the exact pre-transaction release directory, if any."""
            nonlocal quarantined_release
            try:
                if promoted and _path_entry_exists(release_directory):
                    _require_safe_directory_chain(root, release_directory)
                    if not _resolved_within(release_directory, root):
                        raise _activation_error()
                    shutil.rmtree(release_directory)
                if quarantined_release is not None:
                    quarantined_binary = (
                        quarantined_release / _runtime_binary_relative(tool)
                    )
                    _require_safe_partial_runtime_release(
                        versions,
                        quarantined_release,
                        quarantined_binary,
                    )
                    if _path_entry_exists(release_directory):
                        raise _activation_error()
                    os.replace(quarantined_release, release_directory)
                    quarantined_release = None
                    _require_safe_partial_runtime_release(
                        root,
                        release_directory,
                        destination,
                    )
                return True
            except (OSError, OptionalToolError):
                return False

        if _path_entry_exists(release_directory):
            _require_safe_partial_runtime_release(
                root,
                release_directory,
                destination,
            )
            if not _installed_runtime_matches_approved_archive(
                artifact,
                release_directory,
                destination,
                tool,
            ):
                quarantine_destination = (
                    versions / f'.corrupt-{release}-{uuid.uuid4().hex}'
                )
                if (
                    _path_entry_exists(quarantine_destination)
                    or not _resolved_within(quarantine_destination, versions)
                ):
                    raise _activation_error()
                try:
                    os.replace(release_directory, quarantine_destination)
                    quarantined_release = quarantine_destination
                    _require_safe_partial_runtime_release(
                        versions,
                        quarantined_release,
                        quarantined_release / _runtime_binary_relative(tool),
                    )
                    _require_safe_directory_chain(temporary, staged_release)
                    _require_safe_regular_beneath(staged_release, staged)
                    os.replace(staged_release, release_directory)
                    promoted = True
                    _require_safe_directory_chain(root, release_directory)
                    _require_safe_regular_beneath(
                        release_directory,
                        destination,
                    )
                    if not _installed_runtime_matches_approved_archive(
                        artifact,
                        release_directory,
                        destination,
                        tool,
                    ):
                        raise _activation_error()
                except (OSError, OptionalToolError) as exc:
                    if not rollback_release_promotion():
                        raise OptionalToolError(
                            _ACTIVATION_ROLLBACK_FAILURE_MESSAGE,
                            code='activate',
                        ) from exc
                    if isinstance(exc, OptionalToolError):
                        raise
                    raise _activation_error() from exc
        else:
            try:
                _require_safe_directory_chain(temporary, staged_release)
                _require_safe_regular_beneath(staged_release, staged)
                if not _resolved_within(release_directory, root):
                    raise _activation_error()
                os.replace(staged_release, release_directory)
                promoted = True
                _require_safe_directory_chain(root, release_directory)
                _require_safe_regular_beneath(release_directory, destination)
                if not _installed_runtime_matches_approved_archive(
                    artifact,
                    release_directory,
                    destination,
                    tool,
                ):
                    raise _activation_error()
            except OptionalToolError:
                if promoted:
                    shutil.rmtree(release_directory, ignore_errors=True)
                raise
            except OSError as exc:
                if promoted:
                    shutil.rmtree(release_directory, ignore_errors=True)
                raise _activation_error() from exc

        temporary_pointer: Path | None = None
        pointer_replaced = False
        try:
            latest_previous_pointer = _require_existing_runtime_layout_safe(
                tool,
                tools_root,
                root,
                versions,
                pointer,
                release_directory,
                destination,
            )
            if latest_previous_pointer != previous_pointer:
                raise _activation_error()
            descriptor, pointer_name = tempfile.mkstemp(
                prefix='.current-',
                dir=root,
            )
            temporary_pointer = Path(pointer_name)
            with os.fdopen(descriptor, 'w', encoding='ascii', newline='\n') as stream:
                stream.write(release + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            _require_safe_regular_beneath(root, temporary_pointer)
            os.replace(temporary_pointer, pointer)
            pointer_replaced = True
            temporary_pointer = None
            _require_safe_regular_beneath(tools_root, pointer)
        except (OptionalToolError, OSError) as exc:
            pointer_rollback_ok = (
                not pointer_replaced
                or _restore_runtime_pointer(root, pointer, previous_pointer)
            )
            # If the pointer rollback failed, it still selects the candidate;
            # deleting that release would create a dangling executable path.
            release_rollback_ok = (
                not promoted
                or not pointer_rollback_ok
                or rollback_release_promotion()
            )
            if not pointer_rollback_ok or not release_rollback_ok:
                raise OptionalToolError(
                    _ACTIVATION_ROLLBACK_FAILURE_MESSAGE,
                    code='activate',
                ) from exc
            if isinstance(exc, OptionalToolError):
                raise
            raise _activation_error() from exc
        finally:
            if temporary_pointer is not None:
                try:
                    temporary_pointer.unlink()
                except OSError:
                    pass
        if quarantined_release is not None:
            try:
                _require_safe_directory_chain(versions, quarantined_release)
                shutil.rmtree(quarantined_release)
                quarantined_release = None
            except (OSError, OptionalToolError):
                # The certified release is already atomically selected. A
                # non-selected corrupt quarantine is inert and may be cleaned
                # on a later maintenance pass.
                pass
    return {
        'ok': True,
        'tool': tool,
        'path': str(destination),
        'version': artifact.version,
        'checksum_verified': True,
        'message': f'Installed certified {tool} {artifact.version} to {destination}',
        'method': 'verified_manifest',
    }


def install_aria2c(*, runner=subprocess.run) -> dict:
    """Return stable manual guidance; no certified 1.0.0 binary is available."""
    return _manual_result('aria2c')


def install_n_m3u8dl(*, runner=subprocess.run) -> dict:
    """Return stable manual guidance; approved exact-final redirects are unavailable."""
    return _manual_result('N_m3u8DL-RE')


def install_node(*, runner=subprocess.run, opener=None) -> dict:
    """Install the sole immutable Node artifact approved for this host."""
    try:
        artifact = artifact_for('node', sys.platform, py_platform.machine())
    except UnsupportedManagedInstall:
        return _manual_result(
            'node',
            'No certified managed Node artifact is available for this '
            'platform and architecture; install Node manually.',
        )
    with installer_guard(managed_tools_dir().parent):
        return _install_certified_runtime(
            'node',
            artifact,
            runner=runner,
            opener=opener,
        )

def install_deno(*, runner=subprocess.run, opener=None) -> dict:
    """Install the sole immutable Deno artifact approved for this host."""
    try:
        artifact = artifact_for('deno', sys.platform, py_platform.machine())
    except UnsupportedManagedInstall:
        return _manual_result(
            'deno',
            'No certified managed Deno artifact is available for this '
            'platform and architecture; install Deno manually.',
        )
    with installer_guard(managed_tools_dir().parent):
        return _install_certified_runtime(
            'deno',
            artifact,
            runner=runner,
            opener=opener,
        )

def install_bgutil_plugin(*, runner=subprocess.run) -> dict:
    """Direct provider mutation is disabled; the certified lock owns this package."""
    return dict(PYTHON_LOCK_ONLY_RESULT)

def install_playwright(*, runner=subprocess.run) -> dict:
    """Return manual guidance because browser payloads lack packaged provenance."""
    return _manual_result('playwright')

def install_bgutil_script(*, runner=subprocess.run) -> dict:
    """Return manual guidance because no immutable server source is certified."""
    return _manual_result('bgutil-server')

def install_bgutil_server(*, runner=subprocess.run) -> dict:
    """Return manual guidance because no immutable server artifact is certified."""
    return _manual_result('bgutil-server')

def install_optional_tool(
    tool: str,
    *,
    runner=subprocess.run,
    opener=None,
) -> dict:
    """Dispatch installer for a known optional tool name."""
    key = (tool or '').strip()
    # Accept a few aliases from the UI / API
    aliases = {
        'aria2c': 'aria2c',
        'aria2': 'aria2c',
        'N_m3u8DL-RE': 'N_m3u8DL-RE',
        'n_m3u8dl-re': 'N_m3u8DL-RE',
        'turbo': 'N_m3u8DL-RE',
        'deno': 'deno',
        'node': 'node',
        'nodejs': 'node',
        'playwright': 'playwright',
        'bgutil-server': 'bgutil-server',
        'bgutil_server': 'bgutil-server',
        'bgutil-plugin': 'bgutil-plugin',
        'bgutil-ytdlp-pot-provider': 'bgutil-plugin',
        'bgutil': 'bgutil-plugin',
    }
    resolved = aliases.get(key)
    if not resolved:
        return {
            'ok': False, 'tool': tool, 'code': 'unsupported',
            'error': f'Unknown optional tool: {tool}',
        }
    if resolved == 'aria2c':
        return install_aria2c()
    if resolved == 'N_m3u8DL-RE':
        return install_n_m3u8dl()
    if resolved == 'playwright':
        return install_playwright()
    if resolved == 'bgutil-plugin':
        return install_bgutil_plugin()
    if resolved == 'bgutil-server':
        return install_bgutil_server()
    try:
        if resolved == 'deno':
            return install_deno(runner=runner, opener=opener)
        if resolved == 'node':
            return install_node(runner=runner, opener=opener)
        return {
            'ok': False, 'tool': resolved, 'code': 'unsupported',
            'error': f'No installer for {resolved}',
        }
    except InstallerBusyError:
        return {
            'ok': False,
            'tool': resolved,
            'code': INSTALLER_BUSY_CODE,
            'error': INSTALLER_BUSY_MESSAGE,
        }
    except OptionalToolError as exc:
        return {
            'ok': False, 'tool': resolved, 'code': exc.code,
            'error': str(exc),
        }
    except (ChecksumError, UnsupportedManagedInstall) as exc:
        return _manifest_failure_result(resolved, exc)
    except OSError as exc:
        logger.warning(
            'optional tool network I/O failed for %s (%s)',
            resolved,
            type(exc).__name__,
        )
        return {
            'ok': False,
            'tool': resolved,
            'code': 'network',
            'error': _NETWORK_FAILURE_MESSAGE,
        }
    except Exception:
        logger.exception('optional tool install failed')
        return {
            'ok': False, 'tool': resolved, 'code': 'failed',
            'error': _INSTALL_FAILURE_MESSAGE,
        }


_MODULE = sys.modules[__name__]
if __name__ == 'optional_tools':
    sys.modules.setdefault('Final.optional_tools', _MODULE)
elif __name__ == 'Final.optional_tools':
    sys.modules.setdefault('optional_tools', _MODULE)
