"""Managed FFmpeg release fetch + install for the approved Windows Gyan build."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import platform as py_platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
import urllib.parse
import urllib.request
import uuid
import zipfile

try:
    from installer_lock import (
        UnsafeInstallerRootError,
        installer_guard,
        validated_private_root,
    )
except ImportError:  # pragma: no cover
    from Final.installer_lock import (  # type: ignore
        UnsafeInstallerRootError,
        installer_guard,
        validated_private_root,
    )

try:
    from executable_trust import explicit_path_executable
except ImportError:  # pragma: no cover
    from Final.executable_trust import explicit_path_executable  # type: ignore

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


# Compatibility labels mirror the immutable manifest. They are not discovery
# endpoints and no metadata is fetched from them.
RELEASE_ARCHIVE_URL = (
    'https://www.gyan.dev/ffmpeg/builds/packages/'
    'ffmpeg-8.1.2-essentials_build.zip'
)
ARCHIVE_TIMEOUT_SECONDS = 30
ARCHIVE_TOTAL_TIMEOUT_SECONDS = 10 * 60
PROBE_TIMEOUT_SECONDS = 10
MAX_ARCHIVE_BYTES = 160 * 1024 * 1024
MAX_ZIP_MEMBERS = 500
MAX_EXPANDED_BYTES = 750 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
APPROVED_FFMPEG_ARCHIVE = 'approved-ffmpeg.zip'
APPROVED_FFPROBE_ARCHIVE = 'approved-ffprobe.zip'

_NUMERIC_VERSION = re.compile(r'\d+(?:\.\d+)*\Z')
_GYAN_VERSION = re.compile(r'\bffmpeg\s+version\s+(\d+(?:\.\d+)*)\b')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_TOOL_VERSION = re.compile(r'\b(ffmpeg|ffprobe)\s+version\s+(\d+(?:\.\d+)*)\b')
_DRIVE_PREFIX = re.compile(r'^[A-Za-z]:')
_FFMPEG_LAUNCH_GATE = threading.Lock()
ACTIVATION_MARKER_NAME = '.activation-transaction.json'
_MARKER_VERSION = 1
logger = logging.getLogger(__name__)

PUBLIC_FAILURE_MESSAGES = {
    'network': 'Secure HTTPS FFmpeg download failed; check the network connection and retry',
    'checksum': 'FFmpeg download failed checksum verification; retry later',
    'archive': 'FFmpeg archive is invalid, unsafe, or exceeds the download limit',
    'disk': 'FFmpeg installation could not write to disk; check space and permissions',
    'probe': 'FFmpeg binaries could not be validated on this system',
    'compatibility': 'The new FFmpeg build is incompatible with hardware encoding on this system',
    'recovery': 'FFmpeg activation recovery requires manual attention',
}


def _platform() -> str:
    return sys.platform


def _machine() -> str:
    return py_platform.machine().lower()


class FFmpegUpdateError(RuntimeError):
    """Raised when trusted FFmpeg release metadata is unavailable or invalid."""

    def __init__(self, message: str, *, code: str = 'archive'):
        super().__init__(message)
        self.code = code


class FFmpegBusyError(FFmpegUpdateError):
    """Raised when activation cannot safely continue while FFmpeg is active."""


def ffmpeg_launch_gate():
    """Return the shared gate coordinating launch activation and binary swaps."""
    return _FFMPEG_LAUNCH_GATE


def managed_install_supported(
    platform: str | None = None,
    machine: str | None = None,
) -> bool:
    """True only for the redistributable Windows managed-build contract."""
    plat = platform if platform is not None else _platform()
    arch = machine if machine is not None else _machine()
    if not plat.startswith('win'):
        return False
    try:
        artifact_for('ffmpeg', plat, arch)
        return True
    except UnsupportedManagedInstall:
        return False


def ffmpeg_binary_name(platform: str | None = None) -> str:
    plat = platform if platform is not None else _platform()
    return 'ffmpeg.exe' if plat.startswith('win') else 'ffmpeg'


def ffprobe_binary_name(platform: str | None = None) -> str:
    plat = platform if platform is not None else _platform()
    return 'ffprobe.exe' if plat.startswith('win') else 'ffprobe'


def app_data_dir(platform: str | None = None) -> Path:
    """OS-appropriate durable app data root for yt_final (managed tools, state)."""
    plat = platform if platform is not None else _platform()
    if plat.startswith('win'):
        local_appdata = os.environ.get('LOCALAPPDATA')
        if local_appdata:
            return Path(local_appdata) / 'yt_final'
        # Mirror cache_dir()'s graceful fallback instead of hard-failing: a stripped/service
        # environment without LOCALAPPDATA otherwise bricks every managed-tool path.
        return Path.home() / 'AppData' / 'Local' / 'yt_final'
    if plat == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'yt_final'
    xdg = os.environ.get('XDG_DATA_HOME')
    base = Path(xdg) if xdg else (Path.home() / '.local' / 'share')
    return base / 'yt_final'


def cache_dir(platform: str | None = None) -> Path:
    """OS-appropriate cache root for yt_final (ephemeral capability/update caches)."""
    plat = platform if platform is not None else _platform()
    if plat.startswith('win'):
        local_appdata = os.environ.get('LOCALAPPDATA')
        if local_appdata:
            return Path(local_appdata) / 'yt_final'
        return Path.home() / 'AppData' / 'Local' / 'yt_final'
    if plat == 'darwin':
        return Path.home() / 'Library' / 'Caches' / 'yt_final'
    xdg = os.environ.get('XDG_CACHE_HOME')
    base = Path(xdg) if xdg else (Path.home() / '.cache')
    return base / 'yt_final'


def managed_root_display(platform: str | None = None) -> str:
    """User-facing managed FFmpeg location string for UI/docs."""
    plat = platform if platform is not None else _platform()
    if plat.startswith('win'):
        return r'%LOCALAPPDATA%\yt_final\ffmpeg'
    if plat == 'darwin':
        return '~/Library/Application Support/yt_final/ffmpeg'
    return '~/.local/share/yt_final/ffmpeg'


def host_os_label(platform: str | None = None) -> str:
    """Short human OS name for the Updates UI (Windows / macOS / Linux)."""
    plat = platform if platform is not None else _platform()
    if plat.startswith('win'):
        return 'Windows'
    if plat == 'darwin':
        return 'macOS'
    return 'Linux'


def managed_locations_by_os() -> dict[str, str]:
    """Display paths only for supported managed-install operating systems."""
    return {
        'Windows': managed_root_display('win32'),
    }


def _require_https(url: str, resource: str) -> None:
    if urllib.parse.urlsplit(url).scheme.lower() != 'https':
        raise FFmpegUpdateError(f'{resource} must use HTTPS', code='network')


class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject every redirect for exact-final-URL production artifacts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FFmpegUpdateError(
            'Certified FFmpeg artifacts do not permit redirects',
            code='network',
        )


_HTTPS_OPENER = urllib.request.build_opener(HTTPSOnlyRedirectHandler())


def _open_https(opener, url: str, *, timeout: int, approved_url: str | None = None):
    """Open one exact approved resource and validate final URL before body reads."""
    _require_https(url, 'FFmpeg release endpoint')
    response = (opener or _HTTPS_OPENER.open)(url, timeout=timeout)
    final_url = response.geturl() if hasattr(response, 'geturl') else None
    try:
        _require_https(final_url, 'FFmpeg release response')
        if final_url != (approved_url or url):
            raise FFmpegUpdateError(
                'FFmpeg response final URL does not match the approved artifact',
                code='network',
            )
        status = getattr(response, 'status', None)
        if status is None and hasattr(response, 'getcode'):
            status = response.getcode()
        if status is not None and 300 <= int(status) < 400:
            raise FFmpegUpdateError(
                'Certified FFmpeg artifacts do not permit redirects',
                code='network',
            )
    except Exception:
        response.close()
        raise
    return response


@dataclass(frozen=True)
class FFmpegRelease:
    version: str
    archive_url: str
    sha256: str
    secondary_archive_url: str | None = None
    secondary_sha256: str | None = None
    artifact: ToolArtifact | None = field(default=None, compare=False, repr=False)
    secondary_artifact: ToolArtifact | None = field(
        default=None,
        compare=False,
        repr=False,
    )


def parse_ffmpeg_version(text: str) -> str | None:
    """Extract the numeric release version from a Gyan FFmpeg banner."""
    match = _GYAN_VERSION.search(text)
    return match.group(1) if match else None


def version_tuple(value: str | None) -> tuple[int, ...]:
    """Convert a strictly numeric dotted version into comparable components."""
    if value is None or not _NUMERIC_VERSION.fullmatch(value):
        return ()
    return tuple(int(component) for component in value.split('.'))


def _release_from_artifacts(
    primary: ToolArtifact,
    secondary: ToolArtifact | None = None,
) -> FFmpegRelease:
    if secondary is not None and secondary.version != primary.version:
        raise FFmpegUpdateError(
            'Approved ffmpeg and ffprobe versions do not match',
            code='unsupported',
        )
    return FFmpegRelease(
        version=primary.version,
        archive_url=primary.url,
        sha256=primary.sha256,
        secondary_archive_url=secondary.url if secondary else None,
        secondary_sha256=secondary.sha256 if secondary else None,
        artifact=primary,
        secondary_artifact=secondary,
    )


def fetch_gyan_release(opener=None, timeout: int | None = None) -> FFmpegRelease:
    """Return the fixed Windows x86_64 manifest entry without metadata I/O."""
    del opener, timeout
    try:
        return _release_from_artifacts(
            artifact_for('ffmpeg', 'win32', 'x86_64')
        )
    except UnsupportedManagedInstall as exc:
        raise FFmpegUpdateError(str(exc), code='unsupported') from exc


def fetch_darwin_release(
    opener=None,
    timeout: int | None = None,
    *,
    machine: str | None = None,
) -> FFmpegRelease:
    """Fail closed: the prior provider restricts its binaries to education."""
    del opener, timeout, machine
    raise FFmpegUpdateError(
        'Managed FFmpeg installation is unavailable on macOS; '
        'install FFmpeg manually or with Homebrew',
        code='unsupported',
    )


def fetch_release(
    opener=None,
    timeout: int | None = None,
    *,
    platform: str | None = None,
    machine: str | None = None,
) -> FFmpegRelease:
    """Select the exact approved host artifact without filesystem/network I/O."""
    del opener, timeout
    plat = platform if platform is not None else _platform()
    arch = machine if machine is not None else _machine()
    if plat.startswith('win'):
        try:
            return _release_from_artifacts(artifact_for('ffmpeg', plat, arch))
        except UnsupportedManagedInstall as exc:
            raise FFmpegUpdateError(str(exc), code='unsupported') from exc
    raise FFmpegUpdateError(
        'Managed FFmpeg installation is supported only on Windows x86_64',
        code='unsupported',
    )


def managed_root(platform: str | None = None) -> Path:
    """Return the application-private FFmpeg installation root."""
    return app_data_dir(platform) / 'ffmpeg'


def managed_ffmpeg_path(platform: str | None = None) -> Path:
    """Return the active managed ffmpeg executable path."""
    return managed_root(platform) / 'current' / 'bin' / ffmpeg_binary_name(platform)


def _probe_version(output: str, expected_tool: str) -> str:
    match = _TOOL_VERSION.search(output)
    if not match or match.group(1) != expected_tool:
        raise FFmpegUpdateError(
            f'{expected_tool} returned an invalid version banner', code='probe')
    return match.group(2)


def probe_pair(
    root: Path,
    runner=subprocess.run,
    *,
    platform: str | None = None,
) -> tuple[str, Path, Path]:
    """Validate and version a release root containing the managed binary pair."""
    root = Path(root)
    plat = platform if platform is not None else _platform()
    preferred = (ffmpeg_binary_name(plat), ffprobe_binary_name(plat))
    name_pairs = (preferred, ('ffmpeg.exe', 'ffprobe.exe'), ('ffmpeg', 'ffprobe'))
    ffmpeg_path = ffprobe_path = None
    seen: set[tuple[str, str]] = set()
    for ffmpeg_name, ffprobe_name in name_pairs:
        key = (ffmpeg_name, ffprobe_name)
        if key in seen:
            continue
        seen.add(key)
        candidate_ff = root / 'bin' / ffmpeg_name
        candidate_fp = root / 'bin' / ffprobe_name
        if candidate_ff.is_file() and candidate_fp.is_file():
            ffmpeg_path, ffprobe_path = candidate_ff, candidate_fp
            break
    if ffmpeg_path is None or ffprobe_path is None:
        raise FFmpegUpdateError(
            f'FFmpeg archive must contain {preferred[0]} and {preferred[1]}', code='probe')

    versions = []
    for tool, path in (('ffmpeg', ffmpeg_path), ('ffprobe', ffprobe_path)):
        try:
            completed = runner(
                [str(path), '-version'],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=True,
            )
        except Exception as exc:
            raise FFmpegUpdateError(f'Unable to probe managed {tool}', code='probe') from exc
        output = f'{completed.stdout or ""}\n{completed.stderr or ""}'
        versions.append(_probe_version(output, tool))

    if versions[0] != versions[1]:
        raise FFmpegUpdateError('ffmpeg and ffprobe versions do not match', code='probe')
    return versions[0], ffmpeg_path, ffprobe_path


def verify_hardware_compatibility(
    candidate_ffmpeg: Path,
    *,
    reference_ffmpeg: Path | None = None,
    runner=subprocess.run,
    platform: str | None = None,
) -> None:
    """Reject an update that loses NVENC encoders working in the active build."""
    reference = Path(reference_ffmpeg) if reference_ffmpeg is not None else None
    if reference is None or not reference.is_file():
        resolved = explicit_path_executable(
            ('ffmpeg.exe', 'ffmpeg'),
            platform=platform or _platform(),
        )
        if not resolved:
            return
        reference = Path(resolved)

    def usable(executable: Path, encoder: str) -> bool:
        command = [
            str(executable), '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=black:size=320x240:duration=0.15:rate=30',
            '-c:v', encoder, '-b:v', '1M', '-pix_fmt', 'yuv420p',
            '-f', 'null', '-',
        ]
        try:
            completed = runner(
                command, capture_output=True, text=True,
                timeout=PROBE_TIMEOUT_SECONDS, check=False)
            return completed.returncode == 0
        except Exception:
            return False

    lost = [
        encoder for encoder in ('h264_nvenc', 'hevc_nvenc', 'av1_nvenc')
        if usable(reference, encoder) and not usable(candidate_ffmpeg, encoder)
    ]
    if lost:
        raise FFmpegUpdateError(
            'New FFmpeg build loses working hardware encoders: ' + ', '.join(lost),
            code='compatibility',
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while True:
            chunk = handle.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _hash_stream(stream) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(DOWNLOAD_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _download_url_to_path(
    url: str,
    destination: Path,
    expected_sha256: str | None,
    opener=None,
    *,
    clock=time.monotonic,
    artifact: ToolArtifact | None = None,
) -> bool:
    """Download one exact approved HTTPS archive and verify before returning."""
    expected = (
        expected_sha256.strip().lower()
        if isinstance(expected_sha256, str)
        else ''
    )
    if not _SHA256.fullmatch(expected):
        raise FFmpegUpdateError(
            'FFmpeg release checksum is missing or malformed',
            code='checksum',
        )
    if artifact is not None and (
        url != artifact.url or expected != artifact.sha256
    ):
        raise FFmpegUpdateError(
            'FFmpeg release does not match approved artifact provenance',
            code='network',
        )
    _require_https(url, 'FFmpeg release endpoint')

    digest = hashlib.sha256()
    downloaded = 0
    started = clock()
    final_url = None
    try:
        with _open_https(
            opener,
            url,
            timeout=ARCHIVE_TIMEOUT_SECONDS,
            approved_url=url,
        ) as response:
            final_url = response.geturl()
            with destination.open('wb') as archive_file:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if clock() - started > ARCHIVE_TOTAL_TIMEOUT_SECONDS:
                        raise FFmpegUpdateError(
                            'FFmpeg archive download exceeded the total deadline',
                            code='network')
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_ARCHIVE_BYTES:
                        raise FFmpegUpdateError(
                            'FFmpeg archive exceeds the download limit', code='archive')
                    digest.update(chunk)
                    archive_file.write(chunk)
    except FFmpegUpdateError:
        destination.unlink(missing_ok=True)
        raise
    except ChecksumError as exc:
        destination.unlink(missing_ok=True)
        raise FFmpegUpdateError(
            'FFmpeg archive checksum mismatch',
            code='checksum',
        ) from exc
    except UnsupportedManagedInstall as exc:
        destination.unlink(missing_ok=True)
        raise FFmpegUpdateError(
            'FFmpeg artifact provenance could not be verified',
            code='network',
        ) from exc
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise FFmpegUpdateError('Unable to download FFmpeg archive', code='network') from exc
    if digest.hexdigest() != expected:
        destination.unlink(missing_ok=True)
        raise FFmpegUpdateError('FFmpeg archive checksum mismatch', code='checksum')
    if artifact is not None:
        try:
            verify_downloaded_artifact(
                artifact,
                destination,
                final_url=final_url,
                redirected=False,
            )
        except ChecksumError as exc:
            destination.unlink(missing_ok=True)
            raise FFmpegUpdateError(
                'FFmpeg archive checksum mismatch',
                code='checksum',
            ) from exc
        except UnsupportedManagedInstall as exc:
            destination.unlink(missing_ok=True)
            raise FFmpegUpdateError(
                'FFmpeg artifact provenance could not be verified',
                code='network',
            ) from exc
    return True


def _download_archive(
    release: FFmpegRelease,
    destination: Path,
    opener=None,
    *,
    clock=time.monotonic,
) -> bool:
    return _download_url_to_path(
        release.archive_url,
        destination,
        release.sha256,
        opener,
        clock=clock,
        artifact=release.artifact,
    )


def _safe_member_path(destination: Path, info: zipfile.ZipInfo) -> Path:
    normalized = info.filename.replace('\\', '/')
    if not normalized or normalized.startswith('/') or _DRIVE_PREFIX.match(normalized):
        raise FFmpegUpdateError('FFmpeg archive contains an unsafe member path')
    pure_path = PurePosixPath(normalized)
    if '..' in pure_path.parts or any(_DRIVE_PREFIX.match(part) for part in pure_path.parts):
        raise FFmpegUpdateError('FFmpeg archive contains an unsafe member path')
    target = (destination / Path(*pure_path.parts)).resolve()
    try:
        target.relative_to(destination.resolve())
    except ValueError as exc:
        raise FFmpegUpdateError('FFmpeg archive member escapes the staging directory') from exc
    return target


def _inspect_archive(archive: zipfile.ZipFile, destination: Path) -> list[tuple[zipfile.ZipInfo, Path]]:
    members = archive.infolist()
    if len(members) > MAX_ZIP_MEMBERS:
        raise FFmpegUpdateError('FFmpeg archive contains too many members')
    if sum(info.file_size for info in members) > MAX_EXPANDED_BYTES:
        raise FFmpegUpdateError('FFmpeg archive expanded size exceeds the limit')

    inspected = []
    seen = set()
    for info in members:
        target = _safe_member_path(destination, info)
        target_key = os.path.normcase(str(target))
        if target_key in seen:
            raise FFmpegUpdateError('FFmpeg archive contains duplicate member paths')
        seen.add(target_key)
        if info.create_system == 3:
            mode_type = stat.S_IFMT(info.external_attr >> 16)
            if mode_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise FFmpegUpdateError('FFmpeg archive contains a link-like member')
        inspected.append((info, target))
    return inspected


def _extract_archive(archive_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            inspected = _inspect_archive(archive, destination)
            extracted_bytes = 0
            for info, target in inspected:
                if info.is_dir() or info.filename.endswith(('/', '\\')):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open('wb') as output:
                    while True:
                        chunk = source.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        extracted_bytes += len(chunk)
                        if extracted_bytes > MAX_EXPANDED_BYTES:
                            raise FFmpegUpdateError('FFmpeg archive expanded size exceeds the limit')
                        output.write(chunk)
    except FFmpegUpdateError:
        raise
    except OSError as exc:
        raise FFmpegUpdateError('Unable to write extracted FFmpeg archive', code='disk') from exc
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise FFmpegUpdateError('Unable to safely extract FFmpeg archive', code='archive') from exc


def _find_candidate(
    staging: Path,
    *,
    platform: str | None = None,
    release: FFmpegRelease | None = None,
) -> Path:
    """Locate the unique release root that contains a managed ffmpeg/ffprobe pair."""
    plat = platform if platform is not None else _platform()
    if plat.startswith('win'):
        if release is None:
            raise FFmpegUpdateError(
                'Approved Windows FFmpeg release identity is missing',
                code='archive',
            )
        expected_root = (
            Path(staging)
            / f'ffmpeg-{release.version}-essentials_build'
        )
        expected_bin = expected_root / 'bin'
        expected_files = (
            expected_bin / 'ffmpeg.exe',
            expected_bin / 'ffprobe.exe',
        )
        if not all(
            path.is_file() and not path.is_symlink()
            for path in expected_files
        ):
            raise FFmpegUpdateError(
                'Windows FFmpeg archive is missing its approved regular binary pair',
                code='archive',
            )
        alternate_pairs = {
            path.parent.parent
            for name in ('ffmpeg.exe', 'ffmpeg')
            for path in Path(staging).rglob(name)
            if path.parent.name.lower() == 'bin'
            and (
                (path.parent / 'ffprobe.exe').is_file()
                or (path.parent / 'ffprobe').is_file()
            )
        }
        if alternate_pairs != {expected_root}:
            raise FFmpegUpdateError(
                'Windows FFmpeg archive must contain one approved release root',
                code='archive',
            )
        return expected_root

    pairs = (
        (ffmpeg_binary_name(plat), ffprobe_binary_name(plat)),
        # Accept either naming convention in archives (cross-platform fixtures).
        ('ffmpeg.exe', 'ffprobe.exe'),
        ('ffmpeg', 'ffprobe'),
    )
    candidates: set[Path] = set()
    for ffmpeg_name, ffprobe_name in pairs:
        for path in staging.rglob(ffmpeg_name):
            if path.parent.name.lower() != 'bin':
                continue
            if (path.parent / ffprobe_name).is_file():
                candidates.add(path.parent.parent)
    if len(candidates) == 1:
        return candidates.pop()
    if len(candidates) > 1:
        raise FFmpegUpdateError('FFmpeg archive must contain exactly one binary pair')
    return _normalize_bare_binaries(staging, platform=plat)


def _normalize_bare_binaries(staging: Path, *, platform: str | None = None) -> Path:
    """Arrange top-level ffmpeg/ffprobe binaries into a managed release layout."""
    plat = platform if platform is not None else _platform()
    ffmpeg_name = ffmpeg_binary_name(plat)
    ffprobe_name = ffprobe_binary_name(plat)

    def _find_tool(name: str) -> Path | None:
        matches = [
            path for path in staging.rglob(name)
            if path.is_file() and path.name == name
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    ffmpeg_src = _find_tool(ffmpeg_name) or _find_tool('ffmpeg') or _find_tool('ffmpeg.exe')
    ffprobe_src = _find_tool(ffprobe_name) or _find_tool('ffprobe') or _find_tool('ffprobe.exe')
    if ffmpeg_src is None or ffprobe_src is None:
        raise FFmpegUpdateError('FFmpeg archive must contain exactly one binary pair')

    release_root = staging / 'normalized_release'
    bin_dir = release_root / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_dst = bin_dir / ffmpeg_name
    ffprobe_dst = bin_dir / ffprobe_name
    shutil.copy2(ffmpeg_src, ffmpeg_dst)
    shutil.copy2(ffprobe_src, ffprobe_dst)
    _ensure_executable(ffmpeg_dst)
    _ensure_executable(ffprobe_dst)
    return release_root


def _ensure_executable(path: Path) -> None:
    """Mark a managed binary executable on POSIX hosts (no-op on Windows)."""
    if _platform().startswith('win'):
        return
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


_REPARSE_POINT_FLAG = getattr(
    stat,
    'FILE_ATTRIBUTE_REPARSE_POINT',
    0x400,
)


def _persistent_entry_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _persistent_entry_is_link_or_reparse(path: Path, metadata) -> bool:
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


def _persistent_entry_within(path: Path, trusted_root: Path) -> bool:
    try:
        resolved_root = trusted_root.resolve(strict=True)
        resolved_path = path.resolve(strict=False)
        resolved_path.relative_to(resolved_root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _unsafe_persistent_layout() -> FFmpegUpdateError:
    return FFmpegUpdateError(
        'Managed FFmpeg persistent layout is unsafe',
        code='recovery',
    )


def _validate_persistent_entry(
    path: Path,
    trusted_root: Path,
    *,
    expected: str | None = None,
):
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _unsafe_persistent_layout() from exc
    if (
        _persistent_entry_is_link_or_reparse(path, metadata)
        or not _persistent_entry_within(path, trusted_root)
    ):
        raise _unsafe_persistent_layout()
    if expected == 'directory' and not stat.S_ISDIR(metadata.st_mode):
        raise _unsafe_persistent_layout()
    if expected == 'regular' and not stat.S_ISREG(metadata.st_mode):
        raise _unsafe_persistent_layout()
    if expected is None and not (
        stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
    ):
        raise _unsafe_persistent_layout()
    return metadata


def _validate_persistent_layout(root: Path) -> None:
    """Reject unsafe existing persistent entries before using or mutating them."""
    root = Path(root)
    if not _persistent_entry_exists(root):
        return

    _validate_persistent_entry(root, root.parent, expected='directory')
    stack = [root]
    rollbacks: list[Path] = []
    while stack:
        directory = stack.pop()
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            raise _unsafe_persistent_layout() from exc
        for child in children:
            metadata = _validate_persistent_entry(child, root)
            if directory == root and child.name.startswith('.rollback-'):
                rollbacks.append(child)
            if stat.S_ISDIR(metadata.st_mode):
                stack.append(child)

    current = root / 'current'
    marker = root / ACTIVATION_MARKER_NAME
    metadata_path = root / 'metadata.json'
    for regular_path in (marker, metadata_path):
        if _persistent_entry_exists(regular_path):
            _validate_persistent_entry(
                regular_path,
                root,
                expected='regular',
            )

    persistent_trees = [current, *rollbacks]
    for tree in persistent_trees:
        if not _persistent_entry_exists(tree):
            continue
        _validate_persistent_entry(tree, root, expected='directory')
        bin_dir = tree / 'bin'
        if _persistent_entry_exists(bin_dir):
            _validate_persistent_entry(
                bin_dir,
                root,
                expected='directory',
            )
        for name in ('ffmpeg.exe', 'ffprobe.exe', 'ffmpeg', 'ffprobe'):
            binary = bin_dir / name
            if _persistent_entry_exists(binary):
                _validate_persistent_entry(
                    binary,
                    root,
                    expected='regular',
                )


def _read_checked_regular_bytes(
    trusted_root: Path,
    path: Path,
    *,
    limit: int,
) -> bytes:
    """Read one contained regular file through the exact descriptor validated."""
    trusted_root = Path(trusted_root)
    path = Path(path)
    before = _validate_persistent_entry(
        path,
        trusted_root,
        expected='regular',
    )
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or opened.st_size > limit
        ):
            raise _unsafe_persistent_layout()
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            payload = stream.read(limit + 1)
        after = os.fstat(descriptor)
        if (
            len(payload) > limit
            or len(payload) != opened.st_size
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise _unsafe_persistent_layout()
        return payload
    except FFmpegUpdateError:
        raise
    except OSError as exc:
        raise _unsafe_persistent_layout() from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _hash_checked_regular_file(
    trusted_root: Path,
    path: Path,
    *,
    limit: int = MAX_EXPANDED_BYTES,
) -> str:
    """Stream-hash a contained regular file without materializing the binary."""
    trusted_root = Path(trusted_root)
    path = Path(path)
    before = _validate_persistent_entry(
        path,
        trusted_root,
        expected='regular',
    )
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or opened.st_size > limit
        ):
            raise _unsafe_persistent_layout()
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            while True:
                chunk = stream.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise _unsafe_persistent_layout()
                digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            total != opened.st_size
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise _unsafe_persistent_layout()
        return digest.hexdigest()
    except FFmpegUpdateError:
        raise
    except OSError as exc:
        raise _unsafe_persistent_layout() from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _archive_member_digests(
    archive_bytes: bytes,
    required_tools: tuple[str, ...],
) -> dict[str, str]:
    """Hash the unique regular tool members from immutable archive bytes."""
    matches: dict[str, list[str]] = {tool: [] for tool in required_tools}
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = archive.infolist()
            if (
                len(members) > MAX_ZIP_MEMBERS
                or sum(info.file_size for info in members) > MAX_EXPANDED_BYTES
            ):
                raise FFmpegUpdateError(
                    'FFmpeg archive proof exceeds structural limits',
                    code='archive',
                )
            seen: set[str] = set()
            for info in members:
                normalized = info.filename.replace('\\', '/')
                pure = PurePosixPath(normalized)
                key = normalized.casefold()
                if (
                    not normalized
                    or pure.is_absolute()
                    or '..' in pure.parts
                    or _DRIVE_PREFIX.match(normalized)
                    or key in seen
                ):
                    raise FFmpegUpdateError(
                        'FFmpeg archive proof contains an unsafe member',
                        code='archive',
                    )
                seen.add(key)
                if info.is_dir() or normalized.endswith('/'):
                    continue
                if info.create_system == 3:
                    entry_type = stat.S_IFMT(info.external_attr >> 16)
                    if entry_type not in (0, stat.S_IFREG):
                        raise FFmpegUpdateError(
                            'FFmpeg archive proof contains a link-like member',
                            code='archive',
                        )
                basename = pure.name.lower()
                for tool in required_tools:
                    accepted = {tool, f'{tool}.exe'}
                    if basename in accepted:
                        with archive.open(info) as stream:
                            matches[tool].append(_hash_stream(stream))
    except FFmpegUpdateError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise FFmpegUpdateError(
            'FFmpeg archive proof is invalid',
            code='archive',
        ) from exc
    if any(len(matches[tool]) != 1 for tool in required_tools):
        raise FFmpegUpdateError(
            'FFmpeg archive proof does not contain one approved binary pair',
            code='archive',
        )
    return {tool: matches[tool][0] for tool in required_tools}


def _release_version_matches(probed: str, approved: str) -> bool:
    return (
        probed == approved
        or probed.startswith(approved + '.')
        or approved.startswith(probed + '.')
    )


def _manifest_binary_digests(release: FFmpegRelease) -> dict[str, str]:
    """Return independently approved executable digests for this release."""
    artifact = release.artifact
    if artifact is None:
        raise FFmpegUpdateError(
            'FFmpeg release lacks executable digest provenance',
            code='checksum',
        )
    if release.secondary_artifact is not None:
        ffprobe_digest = release.secondary_artifact.binary_sha256
    else:
        ffprobe_digest = artifact.companion_binary_sha256
    if not isinstance(ffprobe_digest, str) or not _SHA256.fullmatch(
        ffprobe_digest
    ):
        raise FFmpegUpdateError(
            'FFmpeg release lacks ffprobe digest provenance',
            code='checksum',
        )
    return {
        'ffmpeg': artifact.binary_sha256,
        'ffprobe': ffprobe_digest,
    }


def _probe_manifest_certified_pair(
    release_root: Path,
    release: FFmpegRelease,
    *,
    runner=subprocess.run,
    platform: str | None = None,
) -> tuple[str, Path, Path]:
    """Authenticate the selected binaries directly for an execution handoff."""
    release_root = Path(release_root)
    plat = platform if platform is not None else _platform()
    _validate_persistent_layout(release_root)
    expected = _manifest_binary_digests(release)
    ffmpeg_path = release_root / 'bin' / ffmpeg_binary_name(plat)
    ffprobe_path = release_root / 'bin' / ffprobe_binary_name(plat)
    installed = {
        'ffmpeg': _hash_checked_regular_file(release_root, ffmpeg_path),
        'ffprobe': _hash_checked_regular_file(release_root, ffprobe_path),
    }
    if any(
        not hmac.compare_digest(installed[tool], expected[tool])
        for tool in ('ffmpeg', 'ffprobe')
    ):
        raise FFmpegUpdateError(
            'Managed FFmpeg binaries do not match approved executable digests',
            code='checksum',
        )
    version, probed_ffmpeg, probed_ffprobe = probe_pair(
        release_root,
        runner=runner,
        platform=plat,
    )
    if (
        probed_ffmpeg != ffmpeg_path
        or probed_ffprobe != ffprobe_path
        or not _release_version_matches(version, release.version)
    ):
        raise FFmpegUpdateError(
            'Managed FFmpeg pair does not match approved release metadata',
            code='probe',
        )
    return version, probed_ffmpeg, probed_ffprobe


def _certified_release_pair(
    release_root: Path,
    release: FFmpegRelease,
    *,
    runner=subprocess.run,
    platform: str | None = None,
) -> tuple[str, Path, Path]:
    """Prove persisted binaries came from manifest-authenticated archive bytes."""
    release_root = Path(release_root)
    plat = platform if platform is not None else _platform()
    _validate_persistent_layout(release_root)

    primary_bytes = _read_checked_regular_bytes(
        release_root,
        release_root / APPROVED_FFMPEG_ARCHIVE,
        limit=MAX_ARCHIVE_BYTES,
    )
    try:
        verify_sha256(primary_bytes, release.sha256)
    except ChecksumError as exc:
        raise FFmpegUpdateError(
            'Managed FFmpeg archive proof failed checksum verification',
            code='checksum',
        ) from exc
    if release.secondary_archive_url:
        primary_digests = _archive_member_digests(primary_bytes, ('ffmpeg',))
        del primary_bytes
        secondary_bytes = _read_checked_regular_bytes(
            release_root,
            release_root / APPROVED_FFPROBE_ARCHIVE,
            limit=MAX_ARCHIVE_BYTES,
        )
        try:
            verify_sha256(
                secondary_bytes,
                str(release.secondary_sha256 or ''),
            )
        except ChecksumError as exc:
            raise FFmpegUpdateError(
                'Managed ffprobe archive proof failed checksum verification',
                code='checksum',
            ) from exc
        secondary_digests = _archive_member_digests(
            secondary_bytes,
            ('ffprobe',),
        )
        del secondary_bytes
        expected = {**primary_digests, **secondary_digests}
    else:
        expected = _archive_member_digests(
            primary_bytes,
            ('ffmpeg', 'ffprobe'),
        )
    if release.artifact is not None:
        manifest_expected = _manifest_binary_digests(release)
        if any(
            not hmac.compare_digest(expected[tool], manifest_expected[tool])
            for tool in ('ffmpeg', 'ffprobe')
        ):
            raise FFmpegUpdateError(
                'Approved archive members do not match executable digests',
                code='checksum',
            )
        expected = manifest_expected

    ffmpeg_path = release_root / 'bin' / ffmpeg_binary_name(plat)
    ffprobe_path = release_root / 'bin' / ffprobe_binary_name(plat)
    installed = {
        'ffmpeg': _hash_checked_regular_file(release_root, ffmpeg_path),
        'ffprobe': _hash_checked_regular_file(release_root, ffprobe_path),
    }
    if any(
        not hmac.compare_digest(installed[tool], expected[tool])
        for tool in ('ffmpeg', 'ffprobe')
    ):
        raise FFmpegUpdateError(
            'Managed FFmpeg binaries do not match approved archive proof',
            code='checksum',
        )

    version, probed_ffmpeg, probed_ffprobe = probe_pair(
        release_root,
        runner=runner,
        platform=plat,
    )
    if (
        probed_ffmpeg != ffmpeg_path
        or probed_ffprobe != ffprobe_path
        or not _release_version_matches(version, release.version)
    ):
        raise FFmpegUpdateError(
            'Managed FFmpeg pair does not match approved release metadata',
            code='probe',
        )
    return version, probed_ffmpeg, probed_ffprobe


def certified_managed_pair(
    *,
    root: Path | None = None,
    runner=subprocess.run,
) -> tuple[str, Path, Path]:
    """Return a pair only after direct immutable executable-digest proof."""
    root = Path(root) if root is not None else managed_root()
    try:
        validated_private_root(root.parent)
    except UnsafeInstallerRootError as exc:
        raise _unsafe_persistent_layout() from exc
    _validate_persistent_layout(root)
    release = fetch_release(
        platform=_platform(),
        machine=_machine(),
    )
    return _probe_manifest_certified_pair(
        root / 'current',
        release,
        runner=runner,
    )


def _write_metadata(
    root: Path,
    release: FFmpegRelease,
    ffmpeg_path: Path,
    *,
    checksum_verified: bool,
) -> None:
    _validate_persistent_layout(root)
    metadata = {
        'version': release.version,
        'path': str(ffmpeg_path),
        'sha256': release.sha256,
        'checksum_verified': bool(checksum_verified),
    }
    descriptor, temporary_name = tempfile.mkstemp(prefix='.metadata-', suffix='.json', dir=root)
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.write_text(json.dumps(metadata, sort_keys=True), encoding='utf-8')
        os.replace(temporary_path, root / 'metadata.json')
    finally:
        temporary_path.unlink(missing_ok=True)


def _marker_path(root: Path) -> Path:
    return root / ACTIVATION_MARKER_NAME


def _write_activation_marker(root: Path, rollback: Path) -> None:
    _validate_persistent_layout(root)
    marker = _marker_path(root)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix='.activation-', suffix='.json', dir=root)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps({'version': _MARKER_VERSION, 'rollback': rollback.name}),
            encoding='utf-8',
        )
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_activation_marker(root: Path) -> None:
    _validate_persistent_layout(root)
    _marker_path(root).unlink(missing_ok=True)


def _selected_rollback(root: Path, marker: Path) -> Path:
    payload = json.loads(marker.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('invalid activation marker')
    rollback_name = payload.get('rollback')
    if (payload.get('version') != _MARKER_VERSION
            or not isinstance(rollback_name, str)
            or not rollback_name.startswith('.rollback-')
            or Path(rollback_name).name != rollback_name):
        raise ValueError('invalid activation marker')
    return root / rollback_name


def _remove_selected_rollback(root: Path, selected: Path) -> None:
    root = Path(root)
    selected = Path(selected)
    if (
        selected.parent != root
        or not selected.name.startswith('.rollback-')
    ):
        raise _unsafe_persistent_layout()
    _validate_persistent_layout(root)
    if not _persistent_entry_exists(selected):
        raise _unsafe_persistent_layout()
    _validate_persistent_entry(
        selected,
        root,
        expected='directory',
    )
    shutil.rmtree(selected)


def _metadata_certifies_current(
    root: Path,
    current: Path,
    *,
    runner=subprocess.run,
) -> bool:
    """Return whether atomic metadata proves the active tree was committed."""
    metadata_path = root / 'metadata.json'
    try:
        metadata_stat = metadata_path.lstat()
        reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
        if (
            not stat.S_ISREG(metadata_stat.st_mode)
            or metadata_path.is_symlink()
            or bool(getattr(metadata_stat, 'st_file_attributes', 0) & reparse_flag)
            or metadata_stat.st_size > 16 * 1024
        ):
            return False
        payload = json.loads(metadata_path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or set(payload) != {
            'version', 'path', 'sha256', 'checksum_verified',
        }:
            return False
        release = fetch_release(
            platform=_platform(),
            machine=_machine(),
        )
        if (
            payload.get('version') != release.version
            or payload.get('sha256') != release.sha256
            or payload.get('checksum_verified') is not True
        ):
            return False
        probed_version, ffmpeg_path, _ = _certified_release_pair(
            current,
            release,
            runner=runner,
        )
        return (
            _release_version_matches(probed_version, release.version)
            and payload.get('path') == str(ffmpeg_path)
        )
    except Exception:
        return False


def recover_interrupted_activation(
    *,
    root: Path | None = None,
    runner=subprocess.run,
) -> bool:
    """Resolve an interrupted activation without accepting ambiguous state."""
    root = Path(root) if root is not None else managed_root()
    with installer_guard(root.parent):
        return _recover_interrupted_activation_locked(root=root, runner=runner)


def _recover_interrupted_activation_locked(
    *,
    root: Path,
    runner=subprocess.run,
) -> bool:
    _validate_persistent_layout(root)
    marker = _marker_path(root)
    if not _persistent_entry_exists(marker):
        return False
    current = root / 'current'
    try:
        selected = _selected_rollback(root, marker)
        candidates = sorted(root.glob('.rollback-*'), key=lambda path: path.name)

        # The marker is written before the first rename. With no rollback
        # artifact, an existing current tree therefore remains the old,
        # committed tree and only the marker needs clearing.
        if current.exists() and not candidates:
            _remove_activation_marker(root)
            return False

        # Any additional, missing, non-directory, or unprobeable rollback
        # makes ownership ambiguous. Never guess which tree should be active.
        if len(candidates) != 1 or candidates[0] != selected:
            raise ValueError('rollback state is missing, corrupt, or ambiguous')

        # Metadata replacement is the commit boundary. A crash after that
        # atomic write must retain the certified current tree and only finish
        # transaction cleanup.
        if current.exists() and _metadata_certifies_current(
                root, current, runner=runner):
            _remove_selected_rollback(root, selected)
            _remove_activation_marker(root)
            return False

        try:
            release = fetch_release(
                platform=_platform(),
                machine=_machine(),
            )
            _certified_release_pair(
                selected,
                release,
                runner=runner,
            )
        except Exception as exc:
            raise ValueError(
                'rollback state is missing, corrupt, or ambiguous') from exc

        _validate_persistent_layout(root)
        with ffmpeg_launch_gate():
            if current.exists():
                shutil.rmtree(current)
            os.replace(selected, current)
        _remove_activation_marker(root)
        return True
    except FFmpegUpdateError:
        raise
    except Exception as exc:
        raise FFmpegUpdateError(
            'Interrupted FFmpeg activation could not be recovered',
            code='recovery',
        ) from exc


def _result(
    *,
    ok: bool,
    status: str,
    version: str | None,
    checksum_verified: bool,
    message: str,
    code: str | None = None,
) -> dict:
    result = {
        'ok': ok,
        'status': status,
        'version': version,
        # Unsupported targets must return before even resolving platform
        # filesystem locations.
        'path': None if status == 'unsupported' else str(managed_ffmpeg_path()),
        'checksum_verified': checksum_verified,
        'message': message,
    }
    if code is not None:
        result['code'] = code
    return result


def _recover_pre_commit(root: Path, rollback: Path | None, *, activated: bool,
                        busy: Callable[[], bool] = lambda: False) -> None:
    """Restore the previous current tree and clear its marker on success.

    Holds ffmpeg_launch_gate() around the swap and re-checks busy() first — the same discipline
    as the forward swap and recover_interrupted_activation. Without it, a job that grabbed the
    gate and launched ffmpeg from ``current`` right after activation could see the tree
    half-deleted mid-recovery (rmtree stops at the in-use exe on Windows), with the intact
    rollback never restored. A busy abort leaves everything untouched: better an un-recovered
    but WHOLE tree than a broken half-deleted one."""
    current = root / 'current'
    try:
        with ffmpeg_launch_gate():
            if busy():
                raise FFmpegUpdateError(
                    'FFmpeg recovery skipped: ffmpeg is in use', code='recovery')
            _validate_persistent_layout(root)
            rollback_exists = (
                rollback is not None
                and _persistent_entry_exists(rollback)
            )
            if current.exists() and (activated or rollback_exists):
                shutil.rmtree(current)
            if rollback_exists:
                os.replace(rollback, current)
            _remove_activation_marker(root)
    except FFmpegUpdateError:
        raise
    except Exception as exc:
        raise FFmpegUpdateError(
            'Automatic FFmpeg recovery failed', code='recovery') from exc


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, FFmpegUpdateError):
        return exc.code
    if isinstance(exc, OSError):
        return 'disk'
    return 'archive'


def _log_failure(code: str, exc: Exception) -> None:
    # Deliberately log only bounded structural diagnostics. Exception text can
    # contain signed URLs and private temporary paths supplied by lower layers.
    logger.warning(
        'Managed FFmpeg update failed code=%s exception=%s',
        code,
        type(exc).__name__[:80],
    )


class ManagedFFmpegUpdater:
    """Install a verified managed FFmpeg release using a rollback-safe swap."""

    def install(
        self,
        *,
        busy: Callable[[], bool],
        opener=None,
        runner=subprocess.run,
    ) -> dict:
        if not managed_install_supported(_platform(), _machine()):
            return _result(
                ok=False,
                status='unsupported',
                version=None,
                checksum_verified=False,
                message=(
                    'Managed FFmpeg is certified only for Windows x86_64; '
                    'use a manual or Homebrew FFmpeg installation on macOS'
                ),
                code='unsupported',
            )
        try:
            release = fetch_release(
                platform=_platform(),
                machine=_machine(),
            )
        except FFmpegUpdateError as exc:
            if exc.code != 'unsupported':
                code = exc.code if exc.code in PUBLIC_FAILURE_MESSAGES else 'archive'
                _log_failure(code, exc)
                return _result(
                    ok=False,
                    status='failed',
                    version=None,
                    checksum_verified=False,
                    message=PUBLIC_FAILURE_MESSAGES[code],
                    code=code,
                )
            return _result(
                ok=False,
                status='unsupported',
                version=None,
                checksum_verified=False,
                message=(
                    'Managed FFmpeg is certified only for Windows x86_64; '
                    'use a manual or Homebrew FFmpeg installation on macOS'
                ),
                code='unsupported',
            )
        with installer_guard():
            return self._install_guarded(
                busy=busy,
                opener=opener,
                runner=runner,
                release=release,
            )

    def _install_guarded(
        self,
        *,
        busy: Callable[[], bool],
        opener=None,
        runner=subprocess.run,
        release: FFmpegRelease,
    ) -> dict:
        if busy():
            return _result(
                ok=False,
                status='busy',
                version=None,
                checksum_verified=False,
                message='FFmpeg is busy',
            )

        checksum_verified = False
        temporary_root = None
        rollback = None
        activated = False
        committed = False
        try:
            root = managed_root()
            _validate_persistent_layout(root)
            recover_interrupted_activation(root=root, runner=runner)
            root.parent.mkdir(parents=True, exist_ok=True)
            temporary_root = Path(tempfile.mkdtemp(prefix='.ffmpeg-update-', dir=root.parent))
            archive_path = temporary_root / 'release.zip'
            staging = temporary_root / 'staging'
            staging.mkdir()

            primary_verified = _download_archive(release, archive_path, opener)
            _extract_archive(archive_path, staging)
            secondary_verified = True
            secondary_path = None
            if release.secondary_archive_url:
                secondary_path = temporary_root / 'release-secondary.zip'
                secondary_verified = _download_url_to_path(
                    release.secondary_archive_url,
                    secondary_path,
                    release.secondary_sha256,
                    opener,
                    artifact=release.secondary_artifact,
                )
                _extract_archive(secondary_path, staging)
            # Primary verified only when a digest was required and matched; dual
            # archives require both sides verified when digests are published.
            checksum_verified = primary_verified and (
                secondary_verified if release.secondary_archive_url else True
            )
            if not checksum_verified:
                raise FFmpegUpdateError(
                    'FFmpeg artifact checksum verification was not completed',
                    code='checksum',
                )
            candidate = _find_candidate(staging, release=release)
            primary_proof = candidate / APPROVED_FFMPEG_ARCHIVE
            secondary_proof = candidate / APPROVED_FFPROBE_ARCHIVE
            if (
                _persistent_entry_exists(primary_proof)
                or (
                    secondary_path is not None
                    and _persistent_entry_exists(secondary_proof)
                )
            ):
                raise FFmpegUpdateError(
                    'FFmpeg archive collides with reserved provenance proof',
                    code='archive',
                )
            os.replace(archive_path, primary_proof)
            if secondary_path is not None:
                os.replace(secondary_path, secondary_proof)
            version, _, _ = _certified_release_pair(
                candidate,
                release,
                runner=runner,
            )
            # Allow patch-level drift (e.g. metadata "8.1" vs banner "8.1.2").
            if version != release.version and not (
                version.startswith(release.version + '.')
                or release.version.startswith(version + '.')
            ):
                # Also accept when metadata version is a prefix of the banner version.
                rel_t = version_tuple(release.version)
                got_t = version_tuple(version)
                if not rel_t or not got_t or rel_t != got_t[:len(rel_t)]:
                    raise FFmpegUpdateError(
                        'Downloaded FFmpeg version does not match release metadata',
                        code='probe')

            current = root / 'current'
            _validate_persistent_layout(root)
            trusted_reference = None
            if current.exists():
                try:
                    _, trusted_reference, _ = _certified_release_pair(
                        current,
                        release,
                        runner=runner,
                    )
                except Exception:
                    # An old/unproven app-data tree is never a probe reference.
                    trusted_reference = None
            verify_hardware_compatibility(
                candidate / 'bin' / ffmpeg_binary_name(),
                reference_ffmpeg=trusted_reference,
                runner=runner,
            )
            _validate_persistent_layout(root)
            root.mkdir(parents=True, exist_ok=True)
            _validate_persistent_layout(root)
            with ffmpeg_launch_gate():
                _validate_persistent_layout(root)
                if busy():
                    raise FFmpegBusyError('FFmpeg is busy')
                if current.exists():
                    rollback = root / f'.rollback-{uuid.uuid4().hex}'
                    _write_activation_marker(root, rollback)
                    os.replace(current, rollback)
                os.replace(candidate, current)
                activated = True

            post_version, ffmpeg_path, _ = _certified_release_pair(
                current,
                release,
                runner=runner,
            )
            if post_version != version:
                raise FFmpegUpdateError(
                    'Activated FFmpeg version does not match release metadata',
                    code='probe')
            # Atomic metadata replacement is the transaction commit boundary.
            _write_metadata(
                root,
                release,
                ffmpeg_path,
                checksum_verified=checksum_verified,
            )
            committed = True

            message = f'Managed FFmpeg {release.version} installed'
            rollback_cleanup_succeeded = True
            if rollback is not None:
                try:
                    _remove_selected_rollback(root, rollback)
                    rollback = None
                except Exception:
                    rollback_cleanup_succeeded = False
                    message += '; rollback cleanup deferred'
            if rollback_cleanup_succeeded:
                try:
                    _remove_activation_marker(root)
                except Exception:
                    message += '; activation cleanup deferred'
            return _result(
                ok=True,
                status='installed',
                version=release.version,
                checksum_verified=checksum_verified,
                message=message,
            )
        except Exception as exc:
            code = _failure_code(exc)
            if not committed and (rollback is not None or activated):
                try:
                    _recover_pre_commit(managed_root(), rollback, activated=activated, busy=busy)
                except FFmpegUpdateError as recovery_exc:
                    exc = recovery_exc
                    code = 'recovery'
            _log_failure(code, exc)
            busy_failure = isinstance(exc, FFmpegBusyError)
            return _result(
                ok=False,
                status='busy' if busy_failure else 'failed',
                version=release.version,
                checksum_verified=checksum_verified,
                message='FFmpeg is busy' if busy_failure else PUBLIC_FAILURE_MESSAGES[code],
                code=None if busy_failure else code,
            )
        finally:
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)
