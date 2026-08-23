"""Fail-closed verification for one complete yt_final release artifact set."""
from __future__ import annotations

import argparse
import ast
import base64
import csv
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tarfile
from typing import NamedTuple, NoReturn
import unicodedata
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
import zlib


DEFAULT_VERSION = "1.0.0"
_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
)
_CHECKSUM_LINE = re.compile(
    rb"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)\n"
)
_RECORD_DIGEST = re.compile(r"sha256=([A-Za-z0-9_-]{43})")
_CANONICAL_SIZE = re.compile(r"0|[1-9][0-9]*")
_REQUIREMENT = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(>=)\s*"
    r"((?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*)"
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_CHECKSUM_BYTES = 4096
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 512
_MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_EXPANDED_BYTES = 64 * 1024 * 1024
_MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 2 * 1024 * 1024
_MAX_ZIP_MEMBER_NAME_BYTES = 1024
_MAX_TAR_STREAM_BYTES = 72 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200
_COMPRESSION_RATIO_FLOOR_BYTES = 1024 * 1024
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP_CENTRAL_SIGNATURE = b"PK\x01\x02"
_ZIP_LOCAL_SIGNATURE = b"PK\x03\x04"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP_LOCAL_HEADER_SIZE = 30
_ZIP_CENTRAL_HEADER_SIZE = 46
_GZIP_HEADER_SIZE = 10
_GZIP_TRAILER_SIZE = 8
_TAR_BLOCK_SIZE = 512
_TAR_RECORD_SIZE = 20 * _TAR_BLOCK_SIZE
_TRUSTED_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_WINDOWS_INVALID = frozenset('<>:"|?*')
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_EXPECTED_RUNTIME_REQUIREMENTS = {
    "bgutil-ytdlp-pot-provider": ">=1.3.1",
    "packaging": ">=25.0",
    "regex": ">=2026.7.19",
    "requests": ">=2.32.2",
    "urllib3": ">=2.0.2",
    "yt-dlp": ">=2026.7.4",
    "yt-dlp-ejs": ">=0.8.0",
}
_EXPECTED_WHEEL_METADATA = (
    b"Wheel-Version: 1.0\n"
    b"Generator: setuptools (83.0.0)\n"
    b"Root-Is-Purelib: true\n"
    b"Tag: py3-none-any\n"
    b"\n"
)
_EXPECTED_WHEEL_ENTRY_POINTS = (
    b"[console_scripts]\n"
    b"yt-final = Final.cli:main\n"
)
_EXPECTED_WHEEL_TOP_LEVEL = b"Final\n"

_PORTABLE_FILES = frozenset(
    {
        "LICENSE",
        "README.md",
        "Final/README.md",
        "Final/Start yt_final.bat",
        "Final/Start yt_final.command",
        "Final/THIRD_PARTY_NOTICES",
        "Final/__init__.py",
        "Final/__main__.py",
        "Final/_cdp_client.py",
        "Final/_version.py",
        "Final/approved-python.lock",
        "Final/assets/slate.ttf",
        "Final/capability.py",
        "Final/cli.py",
        "Final/cookie_providers.py",
        "Final/cookies_auth.py",
        "Final/dependency_contract.py",
        "Final/desync_proxy.py",
        "Final/examples/config.example.toml",
        "Final/examples/jobs.example.json",
        "Final/executable_trust.py",
        "Final/ffmpeg_updater.py",
        "Final/installer_lock.py",
        "Final/jobspec.py",
        "Final/jobstore.py",
        "Final/managed_env.py",
        "Final/markers.py",
        "Final/nfo.py",
        "Final/notifications.py",
        "Final/optional_tools.py",
        "Final/redaction.py",
        "Final/requirements.txt",
        "Final/secureio.py",
        "Final/settings.py",
        "Final/speedtest.py",
        "Final/state_validation.py",
        "Final/tool_manifest.py",
        "Final/url_identity.py",
        "Final/watch.py",
        "Final/watch_filters.py",
        "Final/watchstore.py",
        "Final/webserver.py",
        "Final/websessions.py",
        "Final/webui/app.js",
        "Final/webui/index.html",
        "Final/webui/style.css",
        "Final/yt_final.py",
    }
)

_WHEEL_MODULES = frozenset(
    {
        "__init__.py",
        "__main__.py",
        "_cdp_client.py",
        "_version.py",
        "capability.py",
        "cli.py",
        "cookie_providers.py",
        "cookies_auth.py",
        "dependency_contract.py",
        "desync_proxy.py",
        "executable_trust.py",
        "ffmpeg_updater.py",
        "installer_lock.py",
        "jobspec.py",
        "jobstore.py",
        "managed_env.py",
        "markers.py",
        "nfo.py",
        "notifications.py",
        "optional_tools.py",
        "redaction.py",
        "secureio.py",
        "settings.py",
        "speedtest.py",
        "state_validation.py",
        "tool_manifest.py",
        "url_identity.py",
        "watch.py",
        "watch_filters.py",
        "watchstore.py",
        "webserver.py",
        "websessions.py",
        "yt_final.py",
    }
)

_WHEEL_DATA = frozenset(
    {
        "approved-python.lock",
        "assets/slate.ttf",
        "requirements.txt",
        "THIRD_PARTY_NOTICES",
        "webui/app.js",
        "webui/index.html",
        "webui/style.css",
    }
)

_SDIST_FINAL_PYTHON = frozenset(
    {
        *(f"Final/{name}" for name in _WHEEL_MODULES),
        "Final/_syntax_check.py",
        "Final/runtime_verify.py",
        "Final/test_agegate.py",
        "Final/test_agegate_cli.py",
        "Final/test_agegate_jobstore.py",
        "Final/test_auth_cli.py",
        "Final/test_auth_integration.py",
        "Final/test_auth_upload.py",
        "Final/test_auth_webserver.py",
        "Final/test_browser_gate_stdout_isolation.py",
        "Final/test_capability_cache_trust.py",
        "Final/test_capability_macos.py",
        "Final/test_cdp_cleanup_contract.py",
        "Final/test_cdp_handshake.py",
        "Final/test_ci_workflow_syntax.py",
        "Final/test_conftest_report_unmask.py",
        "Final/test_cookie_providers.py",
        "Final/test_cookies_auth.py",
        "Final/test_data_security.py",
        "Final/test_desync_proxy.py",
        "Final/test_download_engine.py",
        "Final/test_executable_trust.py",
        "Final/test_ffmpeg_updater.py",
        "Final/test_host_hints.py",
        "Final/test_installer_lock.py",
        "Final/test_jobstore_idempotency.py",
        "Final/test_jobstore_msvcrt.py",
        "Final/test_lastinfo_drain.py",
        "Final/test_managed_env.py",
        "Final/test_notifications.py",
        "Final/test_notify_web.py",
        "Final/test_optional_tools.py",
        "Final/test_package_probe.py",
        "Final/test_platform_api_guards.py",
        "Final/test_poll_lifecycle.py",
        "Final/test_probe_resolution.py",
        "Final/test_quality_contract.py",
        "Final/test_release_artifacts.py",
        "Final/test_release_boundary.py",
        "Final/test_release_documentation.py",
        "Final/test_release_packaging.py",
        "Final/test_release_workflows.py",
        "Final/test_sdist_inventory_consistency.py",
        "Final/test_section_clip.py",
        "Final/test_settings.py",
        "Final/test_shutdown.py",
        "Final/test_skip_gate_quality.py",
        "Final/test_speedtest.py",
        "Final/test_speedtest_endpoint.py",
        "Final/test_speedtest_ui.py",
        "Final/test_startup_banner.py",
        "Final/test_store_fork_safety.py",
        "Final/test_store_wipe_guards.py",
        "Final/test_tool_manifest.py",
        "Final/test_updates_api.py",
        "Final/test_updates_restart.py",
        "Final/test_updates_ui.py",
        "Final/test_url_identity.py",
        "Final/test_verify_release.py",
        "Final/test_watch_execution_trust.py",
        "Final/test_watch_filters.py",
        "Final/test_watch_truth.py",
        "Final/test_websessions.py",
        "Final/test_webui_boot.py",
        "Final/test_webui_session.py",
        "Final/test_webui_structure.py",
        "Final/test_windows_paths.py",
        "Final/test_workspace_a11y.py",
        "Final/test_workspace_parity.py",
        "Final/test_workspace_perf.py",
        "Final/test_workspace_pollers.py",
        "Final/test_workspace_truth.py",
        "Final/test_xml_sidecars.py",
        "Final/test_yt_final.py",
    }
)

_SDIST_STATIC = frozenset(
    {
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "docs/ARCHITECTURE.md",
        "docs/CLI.md",
        "docs/CONFIGURATION.md",
        "docs/RELEASING.md",
        "docs/TROUBLESHOOTING.md",
        "LICENSE",
        "MANIFEST.in",
        "pyproject.toml",
        "README.md",
        "SECURITY.md",
        "setup.py",
        "release/portable-files.txt",
        "requirements/README.md",
        "requirements/build.in",
        "requirements/build.lock",
        "requirements/dev.in",
        "requirements/dev.lock",
        "requirements/release.in",
        "requirements/release.lock",
        "scripts/__init__.py",
        "scripts/build_portable.py",
        "scripts/make_public_tree.py",
        "scripts/verify_release.py",
        "scripts/write_checksums.py",
        "Final/approved-python.lock",
        "Final/assets/slate.ttf",
        "Final/examples/config.example.toml",
        "Final/examples/jobs.example.json",
        "Final/INNOVATION_ROADMAP.md",
        "Final/README.md",
        "Final/requirements.txt",
        "Final/Start yt_final.bat",
        "Final/Start yt_final.command",
        "Final/THIRD_PARTY_NOTICES",
        "Final/webui/app.js",
        "Final/webui/index.html",
        "Final/webui/style.css",
    }
)

_SDIST_GENERATED = frozenset(
    {
        "PKG-INFO",
        "setup.cfg",
        "yt_final.egg-info/PKG-INFO",
        "yt_final.egg-info/SOURCES.txt",
        "yt_final.egg-info/dependency_links.txt",
        "yt_final.egg-info/entry_points.txt",
        "yt_final.egg-info/requires.txt",
        "yt_final.egg-info/top_level.txt",
    }
)

_SDIST_FILES = _SDIST_FINAL_PYTHON | _SDIST_STATIC | _SDIST_GENERATED

_COMMON_FORBIDDEN_COMPONENTS = frozenset(
    {
        ".claude",
        ".git",
        ".github",
        ".pytest_cache",
        ".ruff_cache",
        ".superpowers",
        ".worktrees",
        "__pycache__",
        "archive",
        "build",
        "dist",
        "downloads",
        "mcps",
        "test_out",
        "test_outputs",
    }
)


class ReleaseVerificationError(ValueError):
    """A release set failed an explicit, actionable verification gate."""


class _ZipRecord(NamedTuple):
    name: str
    raw_name: bytes
    date_time: tuple[int, int, int, int, int, int]
    compressed: bytes
    body: bytes


def _fail(message: str) -> NoReturn:
    raise ReleaseVerificationError(message)


def _link_like(path: Path, metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    if getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT:
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int | None]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        getattr(metadata, "st_mtime_ns", None),
    )


def _read_regular_file(path: Path, label: str, *, max_bytes: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        _fail(f"{label} is unavailable: {path}: {exc}")
    if _link_like(path, before) or not stat.S_ISREG(before.st_mode):
        _fail(f"{label} is not a real regular file: {path}")
    if before.st_size < 0 or before.st_size > max_bytes:
        _fail(
            f"{label} exceeds its resource size limit "
            f"({before.st_size} > {max_bytes} bytes): {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail(f"{label} could not be opened safely: {path}: {exc}")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            _fail(f"{label} changed type during open: {path}")
        if opened.st_size < 0 or opened.st_size > max_bytes:
            _fail(
                f"{label} exceeds its resource size limit "
                f"({opened.st_size} > {max_bytes} bytes): {path}"
            )
        if (
            before.st_ino
            and opened.st_ino
            and (before.st_dev, before.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            _fail(f"{label} changed identity during open: {path}")
        chunks = []
        captured = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, max_bytes - captured + 1),
            )
            if not chunk:
                break
            captured += len(chunk)
            if captured > max_bytes:
                _fail(f"{label} grew beyond its resource size limit: {path}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _identity(after) != _identity(opened):
            _fail(f"{label} changed while it was read: {path}")
    finally:
        os.close(descriptor)
    try:
        named_after = path.lstat()
    except OSError as exc:
        _fail(f"{label} disappeared after it was read: {path}: {exc}")
    if (
        _link_like(path, named_after)
        or not stat.S_ISREG(named_after.st_mode)
        or _identity(named_after) != _identity(opened)
    ):
        _fail(f"{label} changed identity after it was read: {path}")
    return b"".join(chunks)


def _expected_names(version: str) -> tuple[str, str, str]:
    if _VERSION_PATTERN.fullmatch(version) is None:
        _fail(f"expected version is not a canonical X.Y.Z release: {version!r}")
    return (
        f"yt_final-{version}-portable.zip",
        f"yt_final-{version}-py3-none-any.whl",
        f"yt_final-{version}.tar.gz",
    )


def _read_artifact_set(
    directory: Path,
    artifact_names: tuple[str, str, str],
) -> dict[str, bytes]:
    try:
        metadata = directory.lstat()
    except OSError as exc:
        _fail(f"artifact directory is unavailable: {directory}: {exc}")
    if _link_like(directory, metadata) or not stat.S_ISDIR(metadata.st_mode):
        _fail(f"artifact input is not a real directory: {directory}")
    expected = {*artifact_names, "SHA256SUMS"}
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        _fail(f"artifact directory cannot be listed: {directory}: {exc}")
    actual_names = [entry.name for entry in entries]
    if len(actual_names) != len(set(actual_names)) or set(actual_names) != expected:
        missing = sorted(expected - set(actual_names))
        extra = sorted(set(actual_names) - expected)
        _fail(
            "release artifact set does not match the exact reviewed boundary; "
            f"missing={missing}, extra={extra}"
        )
    return {
        name: _read_regular_file(
            directory / name,
            "checksum manifest" if name == "SHA256SUMS" else "release artifact",
            max_bytes=(
                _MAX_CHECKSUM_BYTES
                if name == "SHA256SUMS"
                else _MAX_ARTIFACT_BYTES
            ),
        )
        for name in sorted(expected)
    }


def _parse_checksums(
    payload: bytes,
    artifact_names: tuple[str, str, str],
) -> dict[str, str]:
    if not payload or b"\r" in payload or not payload.endswith(b"\n"):
        _fail("checksum manifest must be non-empty, LF-only, and newline-terminated")
    rows = []
    offset = 0
    for line in payload.splitlines(keepends=True):
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            _fail(
                "checksum manifest is not canonical lowercase "
                f"SHA-256 text at byte {offset}"
            )
        rows.append(
            (
                match.group(2).decode("ascii"),
                match.group(1).decode("ascii"),
            )
        )
        offset += len(line)
    names = [name for name, _digest in rows]
    if len(names) != len(set(names)):
        _fail("duplicate checksum manifest entry")
    expected_names = sorted(artifact_names)
    if names != sorted(names):
        _fail("checksum manifest entries are not in canonical filename order")
    if names != expected_names:
        _fail(
            "checksum manifest artifact set does not match the exact "
            f"reviewed boundary: expected={expected_names}, actual={names}"
        )
    return {name: digest for name, digest in rows}


def _component_key(component: str) -> str:
    return unicodedata.normalize("NFC", component).casefold()


def _validate_component(component: str, *, label: str) -> None:
    if component in {"", ".", ".."}:
        _fail(f"unsafe {label} path component: {component!r}")
    if component.endswith((" ", ".")):
        _fail(f"unsafe {label} path has a Windows trailing alias: {component!r}")
    if any(character in _WINDOWS_INVALID for character in component):
        _fail(f"unsafe {label} path has a drive, stream, or invalid character")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in component):
        _fail(f"unsafe {label} path has a control or format character")
    stem = component.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        _fail(f"unsafe {label} path uses a Windows device name: {component!r}")


def _validated_member_name(
    raw_name: str,
    *,
    label: str,
    directory: bool,
) -> tuple[str, str]:
    if not isinstance(raw_name, str) or not raw_name:
        _fail(f"unsafe {label} member has an empty name")
    if "\x00" in raw_name or "\\" in raw_name:
        _fail(f"unsafe {label} member path contains NUL or a backslash")
    name = raw_name
    if directory and name.endswith("/"):
        name = name[:-1]
    if not name or name.startswith("/") or name.endswith("/") or "//" in name:
        _fail(f"unsafe {label} member path is absolute or non-canonical: {raw_name!r}")
    components = name.split("/")
    for component in components:
        _validate_component(component, label=label)
    canonical = "/".join(components)
    if canonical != name:
        _fail(f"unsafe {label} member path is non-canonical: {raw_name!r}")
    key = "/".join(_component_key(component) for component in components)
    return canonical, key


def _zip_type_error(info: ZipInfo, label: str) -> str | None:
    if info.is_dir():
        return f"{label} directory entries are not regular files"
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind in {0, stat.S_IFREG}:
        return None
    if stat.S_ISLNK(mode):
        return f"{label} symlink entries are forbidden"
    if stat.S_ISFIFO(mode):
        return f"{label} FIFO entries are forbidden"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return f"{label} device entries are forbidden"
    return f"{label} non-regular entries are forbidden"


def _reject_excessive_compression(
    *,
    label: str,
    compressed_bytes: int,
    expanded_bytes: int,
) -> None:
    if compressed_bytes < 0 or expanded_bytes < 0:
        _fail(f"{label} has a negative compressed or expanded size")
    if (
        expanded_bytes > _COMPRESSION_RATIO_FLOOR_BYTES
        and (
            compressed_bytes == 0
            or expanded_bytes > compressed_bytes * _MAX_COMPRESSION_RATIO
        )
    ):
        _fail(
            f"{label} exceeds the maximum compression ratio "
            f"({_MAX_COMPRESSION_RATIO}:1)"
        )


def _validate_zip_metadata(info: ZipInfo, label: str) -> None:
    mode = info.external_attr >> 16
    if label == "portable":
        expected_systems = {3}
        expected_modes = {
            (
                0o755
                if info.filename.endswith("/Final/Start yt_final.command")
                else 0o644
            )
        }
    else:
        expected_systems = {0, 3}
        expected_modes = (
            {0o664}
            if info.filename.endswith(".dist-info/RECORD")
            else ({0o666} if info.create_system == 0 else {0o644})
        )
    if (
        info.create_system not in expected_systems
        or info.create_version != 20
        or info.extract_version != 20
        or info.flag_bits not in {0, 0x800}
        or info.volume != 0
        or info.compress_type != ZIP_DEFLATED
        or info.internal_attr != 0
        or info.extra != b""
        or info.comment != b""
    ):
        _fail(f"{label} member has noncanonical ZIP control metadata")
    if (
        stat.S_IFMT(mode) != stat.S_IFREG
        or stat.S_IMODE(mode) not in expected_modes
    ):
        _fail(
            f"{label} member has noncanonical mode/permissions: "
            f"{info.filename}: {oct(stat.S_IMODE(mode))}"
        )


def _decode_zip_datetime(
    raw_date: int,
    raw_time: int,
    *,
    label: str,
) -> tuple[int, int, int, int, int, int]:
    value = (
        1980 + (raw_date >> 9),
        (raw_date >> 5) & 0x0F,
        raw_date & 0x1F,
        raw_time >> 11,
        (raw_time >> 5) & 0x3F,
        (raw_time & 0x1F) * 2,
    )
    try:
        datetime(*value, tzinfo=timezone.utc)
    except ValueError as exc:
        _fail(f"{label} archive has an invalid ZIP timestamp: {exc}")
    return value


def _validate_raw_zip_metadata(
    *,
    label: str,
    name: str,
    create_version: int,
    create_system: int,
    reserved: int,
    extract_version: int,
    flag_bits: int,
    compression: int,
    disk_start: int,
    internal_attr: int,
    external_attr: int,
) -> None:
    if label == "portable":
        expected_systems = {3}
        expected_modes = {
            0o755 if name.endswith("/Final/Start yt_final.command") else 0o644
        }
    else:
        expected_systems = {0, 3}
        expected_modes = (
            {0o664}
            if name.endswith(".dist-info/RECORD")
            else ({0o666} if create_system == 0 else {0o644})
        )
    mode = external_attr >> 16
    if stat.S_ISLNK(mode):
        _fail(f"{label} symlink entries are forbidden")
    if stat.S_ISFIFO(mode):
        _fail(f"{label} FIFO entries are forbidden")
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        _fail(f"{label} device entries are forbidden")
    if stat.S_IFMT(mode) != stat.S_IFREG:
        _fail(f"{label} non-regular entries are forbidden")
    if (
        create_system not in expected_systems
        or create_version != 20
        or reserved != 0
        or extract_version != 20
        or flag_bits not in {0, 0x800}
        or compression != ZIP_DEFLATED
        or disk_start != 0
        or internal_attr != 0
        or external_attr & 0xFFFF
    ):
        _fail(f"{label} member has noncanonical ZIP control metadata")
    if (
        stat.S_IFMT(mode) != stat.S_IFREG
        or stat.S_IMODE(mode) not in expected_modes
    ):
        _fail(
            f"{label} member has noncanonical mode/permissions: "
            f"{name}: {oct(stat.S_IMODE(mode))}"
        )


def _scan_zip_records(payload: bytes, label: str) -> list[_ZipRecord]:
    """Validate and expand one canonical non-ZIP64 archive without ZipFile."""
    eocd_size = 22
    if (
        len(payload) < eocd_size
        or payload[-eocd_size:-eocd_size + 4] != _ZIP_EOCD_SIGNATURE
    ):
        _fail(f"{label} archive has no canonical ZIP end record")
    if (
        len(payload) >= eocd_size + 20
        and payload[-eocd_size - 20:-eocd_size - 16]
        == _ZIP64_LOCATOR_SIGNATURE
    ):
        _fail(f"{label} archive contains forbidden ZIP64 metadata")
    (
        signature,
        disk_number,
        central_disk,
        entries_on_disk,
        entry_count,
        central_size,
        central_offset,
        comment_size,
    ) = struct.unpack_from("<4s4H2LH", payload, len(payload) - eocd_size)
    if signature != _ZIP_EOCD_SIGNATURE:
        _fail(f"{label} archive has no canonical ZIP end record")
    if (
        disk_number != 0
        or central_disk != 0
        or entries_on_disk != entry_count
        or comment_size != 0
    ):
        _fail(f"{label} archive has noncanonical multi-disk or comment metadata")
    if entry_count == 0:
        _fail(f"{label} archive is empty")
    if entry_count > _MAX_ARCHIVE_MEMBERS:
        _fail(
            f"{label} archive member count exceeds its resource limit "
            f"({entry_count} > {_MAX_ARCHIVE_MEMBERS})"
        )
    if central_size > _MAX_ZIP_CENTRAL_DIRECTORY_BYTES:
        _fail(
            f"{label} ZIP central directory exceeds its resource size limit "
            f"({central_size} > {_MAX_ZIP_CENTRAL_DIRECTORY_BYTES} bytes)"
        )
    central_end = central_offset + central_size
    if central_end != len(payload) - eocd_size:
        _fail(f"{label} archive has a noncanonical ZIP central-directory boundary")

    central_records = []
    cursor = central_offset
    while cursor < central_end:
        if (
            central_end - cursor < _ZIP_CENTRAL_HEADER_SIZE
            or payload[cursor:cursor + 4] != _ZIP_CENTRAL_SIGNATURE
        ):
            _fail(f"{label} archive has malformed ZIP central-directory metadata")
        create_version = payload[cursor + 4]
        create_system = payload[cursor + 5]
        extract_version = payload[cursor + 6]
        reserved = payload[cursor + 7]
        (
            flag_bits,
            compression,
            raw_time,
            raw_date,
        ) = struct.unpack_from("<4H", payload, cursor + 8)
        crc32, compressed_size, file_size = struct.unpack_from(
            "<3L",
            payload,
            cursor + 16,
        )
        name_size, extra_size, member_comment_size = struct.unpack_from(
            "<3H",
            payload,
            cursor + 28,
        )
        disk_start, internal_attr = struct.unpack_from(
            "<2H",
            payload,
            cursor + 34,
        )
        external_attr, local_offset = struct.unpack_from(
            "<2L",
            payload,
            cursor + 38,
        )
        if not 0 < name_size <= _MAX_ZIP_MEMBER_NAME_BYTES:
            _fail(
                f"{label} ZIP member name exceeds its resource size limit "
                f"({name_size} > {_MAX_ZIP_MEMBER_NAME_BYTES} bytes)"
            )
        if extra_size != 0 or member_comment_size != 0:
            _fail(f"{label} archive has unreviewed ZIP central-directory metadata")
        name_start = cursor + _ZIP_CENTRAL_HEADER_SIZE
        record_end = name_start + name_size
        if record_end > central_end:
            _fail(f"{label} archive has truncated ZIP central-directory metadata")
        raw_name = payload[name_start:record_end]
        encoding = "utf-8" if flag_bits & 0x800 else "cp437"
        try:
            name = raw_name.decode(encoding)
        except UnicodeDecodeError as exc:
            _fail(f"{label} archive member name is not valid {encoding}: {exc}")
        _validate_raw_zip_metadata(
            label=label,
            name=name,
            create_version=create_version,
            create_system=create_system,
            reserved=reserved,
            extract_version=extract_version,
            flag_bits=flag_bits,
            compression=compression,
            disk_start=disk_start,
            internal_attr=internal_attr,
            external_attr=external_attr,
        )
        central_records.append(
            (
                name,
                raw_name,
                extract_version,
                flag_bits,
                compression,
                raw_time,
                raw_date,
                crc32,
                compressed_size,
                file_size,
                local_offset,
            )
        )
        if len(central_records) > _MAX_ARCHIVE_MEMBERS:
            _fail(
                f"{label} archive member count exceeds its resource limit "
                f"({len(central_records)} > {_MAX_ARCHIVE_MEMBERS})"
            )
        cursor = record_end
    if cursor != central_end or len(central_records) != entry_count:
        _fail(f"{label} archive has an inconsistent ZIP member count")

    records = []
    local_cursor = 0
    expanded_total = 0
    compressed_total = 0
    timestamps = set()
    for (
        name,
        raw_name,
        extract_version,
        flag_bits,
        compression,
        raw_time,
        raw_date,
        crc32,
        compressed_size,
        file_size,
        local_offset,
    ) in central_records:
        if local_offset != local_cursor:
            _fail(f"{label} archive has an unindexed prefix, local record, or gap")
        if (
            central_offset - local_cursor < _ZIP_LOCAL_HEADER_SIZE
            or payload[local_cursor:local_cursor + 4] != _ZIP_LOCAL_SIGNATURE
        ):
            _fail(f"{label} archive has malformed local ZIP metadata")
        (
            local_extract_version,
            local_flag_bits,
            local_compression,
            local_time,
            local_date,
        ) = struct.unpack_from("<5H", payload, local_cursor + 4)
        (
            local_crc32,
            local_compressed_size,
            local_file_size,
        ) = struct.unpack_from("<3L", payload, local_cursor + 14)
        local_name_size, local_extra_size = struct.unpack_from(
            "<2H",
            payload,
            local_cursor + 26,
        )
        if local_extra_size != 0:
            _fail(f"{label} archive member has a noncanonical local ZIP extra field")
        name_start = local_cursor + _ZIP_LOCAL_HEADER_SIZE
        data_start = name_start + local_name_size
        if (
            local_name_size != len(raw_name)
            or payload[name_start:data_start] != raw_name
        ):
            _fail(f"{label} archive local and central member names disagree")
        if (
            local_extract_version,
            local_flag_bits,
            local_compression,
            local_time,
            local_date,
            local_crc32,
            local_compressed_size,
            local_file_size,
        ) != (
            extract_version,
            flag_bits,
            compression,
            raw_time,
            raw_date,
            crc32,
            compressed_size,
            file_size,
        ):
            _fail(f"{label} archive local and central metadata disagree: {name}")
        if file_size > _MAX_ARCHIVE_MEMBER_BYTES:
            _fail(
                f"{label} member expanded size exceeds its resource limit: {name}"
            )
        data_end = data_start + compressed_size
        if data_end > central_offset:
            _fail(f"{label} archive has a truncated local member: {name}")
        compressed = payload[data_start:data_end]
        _reject_excessive_compression(
            label=f"{label} member {name!r}",
            compressed_bytes=len(compressed),
            expanded_bytes=file_size,
        )
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        body = decompressor.decompress(
            compressed,
            _MAX_ARCHIVE_MEMBER_BYTES + 1,
        )
        if (
            len(body) != file_size
            or not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            _fail(
                f"{label} member has a noncanonical or trailing DEFLATE stream: "
                f"{name}"
            )
        if zlib.crc32(body) & 0xFFFFFFFF != crc32:
            _fail(f"{label} member checksum mismatch: {name}")
        date_time = _decode_zip_datetime(raw_date, raw_time, label=label)
        timestamps.add(date_time)
        records.append(_ZipRecord(name, raw_name, date_time, compressed, body))
        expanded_total += len(body)
        compressed_total += len(compressed)
        if expanded_total > _MAX_ARCHIVE_EXPANDED_BYTES:
            _fail(f"{label} aggregate expanded size exceeds its resource limit")
        local_cursor = data_end
    if local_cursor != central_offset:
        _fail(f"{label} archive has unindexed bytes before its central directory")
    if len(timestamps) != 1:
        _fail(f"{label} archive members have inconsistent ZIP timestamps")
    _reject_excessive_compression(
        label=f"{label} archive",
        compressed_bytes=compressed_total,
        expanded_bytes=expanded_total,
    )
    return records


def _preflight_zip_directory(payload: bytes, label: str) -> int:
    """Validate raw ZIP structure before any high-level archive parser."""
    return len(_scan_zip_records(payload, label))


def _read_zip_files(
    payload: bytes,
    label: str,
    *,
    expected_date_time: tuple[int, int, int, int, int, int] | None = None,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    keys: dict[str, str] = {}
    try:
        records = _scan_zip_records(payload, label)
        for record in records:
            canonical, key = _validated_member_name(
                record.name,
                label=label,
                directory=False,
            )
            if canonical in files:
                _fail(f"duplicate {label} archive member: {canonical}")
            previous = keys.get(key)
            if previous is not None:
                _fail(
                    f"duplicate normalized/casefold {label} archive "
                    f"members: {previous!r} and {canonical!r}"
                )
            if (
                expected_date_time is not None
                and record.date_time != expected_date_time
            ):
                _fail(
                    f"{label} member timestamp disagrees with the release "
                    f"source epoch: {canonical}"
                )
            files[canonical] = record.body
            keys[key] = canonical
    except ReleaseVerificationError:
        raise
    except Exception as exc:
        _fail(f"{label} archive could not be read safely: {exc}")
    return files


def _read_bounded_tar_stream(
    payload: bytes,
    label: str,
) -> tuple[bytes, int, int]:
    """Expand exactly one canonical gzip member before parsing any TAR header."""
    if (
        len(payload) < _GZIP_HEADER_SIZE + _GZIP_TRAILER_SIZE
        or payload[:3] != b"\x1f\x8b\x08"
    ):
        _fail(f"{label} archive has no canonical gzip header")
    flags = payload[3]
    mtime = struct.unpack_from("<L", payload, 4)[0]
    xfl = payload[8]
    operating_system = payload[9]
    if flags != 0 or xfl != 2 or operating_system != 255:
        _fail(f"{label} archive has noncanonical gzip control metadata")
    compressed = payload[_GZIP_HEADER_SIZE:]
    try:
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        body = decompressor.decompress(compressed, _MAX_TAR_STREAM_BYTES + 1)
    except zlib.error as exc:
        _fail(f"{label} gzip stream could not be read safely: {exc}")
    if len(body) > _MAX_TAR_STREAM_BYTES:
        _fail(
            f"{label} expanded tar stream exceeds its resource size limit "
            f"({len(body)} > {_MAX_TAR_STREAM_BYTES} bytes)"
        )
    if (
        not decompressor.eof
        or decompressor.unconsumed_tail
        or len(decompressor.unused_data) != _GZIP_TRAILER_SIZE
    ):
        _fail(
            f"{label} archive must contain exactly one canonical gzip member "
            "with no trailing data"
        )
    expected_crc32, expected_size = struct.unpack(
        "<2L",
        decompressor.unused_data,
    )
    if zlib.crc32(body) & 0xFFFFFFFF != expected_crc32:
        _fail(f"{label} gzip stream checksum mismatch")
    if len(body) & 0xFFFFFFFF != expected_size:
        _fail(f"{label} gzip stream size trailer mismatch")
    compressed_size = (
        len(payload) - _GZIP_HEADER_SIZE - _GZIP_TRAILER_SIZE
    )
    _reject_excessive_compression(
        label=f"{label} archive",
        compressed_bytes=compressed_size,
        expanded_bytes=len(body),
    )
    return body, mtime, compressed_size


def _tar_type_error(member: tarfile.TarInfo, label: str) -> str:
    if member.issym():
        return f"{label} symlink entries are forbidden"
    if member.islnk():
        return f"{label} hardlink entries are forbidden"
    if member.isfifo():
        return f"{label} FIFO entries are forbidden"
    if member.ischr() or member.isblk() or member.isdev():
        return f"{label} device entries are forbidden"
    return f"{label} non-regular entries are forbidden"


def _validate_tar_metadata(
    member: tarfile.TarInfo,
    *,
    canonical: str,
    label: str,
) -> None:
    expected_mode = (
        0o755
        if member.isdir()
        or canonical.endswith("/Final/Start yt_final.command")
        else 0o644
    )
    if member.pax_headers:
        _fail(f"{label} member has unreviewed PAX/extended metadata: {canonical}")
    if (
        member.uid != 0
        or member.gid != 0
        or member.uname != ""
        or member.gname != ""
    ):
        _fail(f"{label} member has noncanonical owner metadata: {canonical}")
    if member.mode != expected_mode:
        _fail(
            f"{label} member has noncanonical mode/permissions: "
            f"{canonical}: {oct(member.mode)}"
        )
    if (
        not isinstance(member.mtime, int)
        or isinstance(member.mtime, bool)
        or not 0 <= member.mtime <= 0xFFFFFFFF
    ):
        _fail(f"{label} member has a noncanonical timestamp: {canonical}")


def _parse_tar_octal(
    field: bytes,
    *,
    digits: int,
    label: str,
) -> int:
    if (
        len(field) != digits + 1
        or field[-1:] != b"\0"
        or any(character not in b"01234567" for character in field[:-1])
    ):
        _fail(f"{label} archive has a noncanonical TAR numeric field")
    return int(field[:-1], 8)


def _read_tar_files(
    payload: bytes,
    label: str,
) -> tuple[dict[str, bytes], set[str], int]:
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    keys: dict[str, str] = {}
    try:
        tar_stream, gzip_mtime, compressed_size = _read_bounded_tar_stream(
            payload,
            label,
        )
        if (
            not tar_stream
            or len(tar_stream) % _TAR_RECORD_SIZE != 0
            or len(tar_stream) < _TAR_RECORD_SIZE
        ):
            _fail(f"{label} archive has a noncanonical TAR record boundary")
        cursor = 0
        member_count = 0
        expanded_total = 0
        mtimes = set()
        zero_block = b"\0" * _TAR_BLOCK_SIZE
        while True:
            if cursor + 2 * _TAR_BLOCK_SIZE > len(tar_stream):
                _fail(f"{label} archive has no canonical TAR end marker")
            header = tar_stream[cursor:cursor + _TAR_BLOCK_SIZE]
            if header == zero_block:
                if (
                    tar_stream[
                        cursor + _TAR_BLOCK_SIZE:cursor + 2 * _TAR_BLOCK_SIZE
                    ]
                    != zero_block
                    or any(tar_stream[cursor + 2 * _TAR_BLOCK_SIZE:])
                ):
                    _fail(
                        f"{label} archive has hidden data after its TAR "
                        "logical end"
                    )
                break
            member_count += 1
            if member_count > _MAX_ARCHIVE_MEMBERS:
                _fail(
                    f"{label} archive member count exceeds its resource "
                    f"limit ({member_count} > {_MAX_ARCHIVE_MEMBERS})"
                )
            nul = header[:100].find(b"\0")
            if (
                nul <= 0
                or any(header[nul + 1:100])
                or any(header[345:500])
            ):
                _fail(f"{label} archive has a noncanonical TAR member name")
            try:
                raw_name = header[:nul].decode("utf-8")
            except UnicodeDecodeError as exc:
                _fail(f"{label} TAR member name is not valid UTF-8: {exc}")
            member_type = header[156:157]
            if member_type not in {tarfile.REGTYPE, tarfile.DIRTYPE}:
                _fail(
                    f"{label} archive has a forbidden GNU, PAX, link, or "
                    f"non-regular TAR type: {member_type!r}"
                )
            is_directory = member_type == tarfile.DIRTYPE
            canonical, key = _validated_member_name(
                raw_name,
                label=label,
                directory=is_directory,
            )
            previous = keys.get(key)
            if previous is not None:
                _fail(
                    f"duplicate normalized/casefold {label} archive "
                    f"members: {previous!r} and {canonical!r}"
                )
            keys[key] = canonical
            mode = _parse_tar_octal(
                header[100:108],
                digits=7,
                label=label,
            )
            uid = _parse_tar_octal(
                header[108:116],
                digits=7,
                label=label,
            )
            gid = _parse_tar_octal(
                header[116:124],
                digits=7,
                label=label,
            )
            size = _parse_tar_octal(
                header[124:136],
                digits=11,
                label=label,
            )
            mtime = _parse_tar_octal(
                header[136:148],
                digits=11,
                label=label,
            )
            checksum_field = header[148:156]
            if (
                len(checksum_field) != 8
                or checksum_field[6:] != b"\0 "
                or any(
                    character not in b"01234567"
                    for character in checksum_field[:6]
                )
            ):
                _fail(f"{label} archive has a noncanonical TAR checksum field")
            expected_checksum = int(checksum_field[:6], 8)
            checksum_header = header[:148] + b" " * 8 + header[156:]
            if sum(checksum_header) != expected_checksum:
                _fail(f"{label} archive TAR header checksum mismatch")
            expected_mode = (
                0o755
                if is_directory
                or canonical.endswith("/Final/Start yt_final.command")
                else 0o644
            )
            if (
                uid != 0
                or gid != 0
                or mode != expected_mode
                or header[157:257] != b"\0" * 100
                or header[257:265] != b"ustar\00000"
                or header[265:345] != b"\0" * 80
                or not 0 <= mtime <= 0xFFFFFFFF
            ):
                _fail(
                    f"{label} archive member has noncanonical TAR "
                    f"mode/permissions or owner metadata: {canonical}"
                )
            mtimes.add(mtime)
            data_start = cursor + _TAR_BLOCK_SIZE
            padded_size = (
                (size + _TAR_BLOCK_SIZE - 1) // _TAR_BLOCK_SIZE
            ) * _TAR_BLOCK_SIZE
            data_end = data_start + padded_size
            if data_end > len(tar_stream):
                _fail(f"{label} archive has a truncated TAR member: {canonical}")
            body = tar_stream[data_start:data_start + size]
            if any(tar_stream[data_start + size:data_end]):
                _fail(
                    f"{label} archive member has noncanonical TAR data padding: "
                    f"{canonical}"
                )
            if is_directory:
                if size != 0:
                    _fail(
                        f"{label} directory has a noncanonical size: {canonical}"
                    )
                directories.add(canonical)
            else:
                if size > _MAX_ARCHIVE_MEMBER_BYTES:
                    _fail(
                        f"{label} member expanded size exceeds its resource "
                        f"limit: {canonical}"
                    )
                expanded_total += size
                if expanded_total > _MAX_ARCHIVE_EXPANDED_BYTES:
                    _fail(
                        f"{label} aggregate expanded size exceeds its resource "
                        "limit"
                    )
                files[canonical] = body
            cursor = data_end
        if member_count == 0:
            _fail(f"{label} archive is empty")
        if len(mtimes) != 1:
            _fail(f"{label} archive members have inconsistent timestamps")
        tar_mtime = next(iter(mtimes))
        if gzip_mtime != tar_mtime:
            _fail(f"{label} gzip and TAR member timestamps disagree")
        _reject_excessive_compression(
            label=f"{label} archive files",
            compressed_bytes=compressed_size,
            expanded_bytes=expanded_total,
        )
    except ReleaseVerificationError:
        raise
    except Exception as exc:
        _fail(f"{label} archive could not be read safely: {exc}")
    return files, directories, tar_mtime


def _relative_files(
    files: dict[str, bytes],
    expected_root: str,
    *,
    label: str,
) -> dict[str, bytes]:
    prefix = expected_root + "/"
    if any(not name.startswith(prefix) for name in files):
        _fail(f"{label} member boundary has an unexpected archive root")
    return {name[len(prefix):]: body for name, body in files.items()}


def _forbidden_member(label: str, relative: str) -> str | None:
    components = relative.split("/")
    folded = [component.casefold() for component in components]
    if any(component in _COMMON_FORBIDDEN_COMPONENTS for component in folded):
        return "repository-internal or generated directory"
    if relative.endswith((".log", ".pyc", ".pyo")):
        return "generated log or bytecode"
    if label in {"portable", "wheel"} and any(
        component.startswith("test_") and component.endswith(".py")
        for component in folded
    ):
        return "test source"
    if label == "sdist" and (
        relative.startswith("Final/docs/")
        or relative.startswith("docs/superpowers/")
        or relative.startswith("Final/downloads/")
    ):
        return "internal documentation or generated data"
    return None


def _reject_forbidden(label: str, relatives: set[str]) -> None:
    for relative in sorted(relatives):
        reason = _forbidden_member(label, relative)
        if reason is not None:
            _fail(f"forbidden {label} member ({reason}): {relative}")


def _require_exact_members(
    label: str,
    actual: set[str],
    expected: frozenset[str],
) -> None:
    if actual != expected:
        _fail(
            f"{label} member boundary mismatch; "
            f"missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _metadata_version(body: bytes, *, label: str, expected_version: str) -> None:
    try:
        message = BytesParser(policy=policy.default).parsebytes(body)
    except Exception as exc:
        _fail(f"{label} is not parseable package metadata: {exc}")
    if message.defects:
        _fail(f"{label} contains malformed package metadata")
    metadata_versions = message.get_all("Metadata-Version", [])
    names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    python_versions = message.get_all("Requires-Python", [])
    license_expressions = message.get_all("License-Expression", [])
    license_files = message.get_all("License-File", [])
    legacy_licenses = message.get_all("License", [])
    if metadata_versions != ["2.4"]:
        _fail(
            f"{label} metadata version is not exactly 2.4: "
            f"{metadata_versions!r}"
        )
    if names != ["yt-final"]:
        _fail(f"{label} package name is not exactly 'yt-final': {names!r}")
    if versions != [expected_version]:
        _fail(
            f"{label} version disagrees with {expected_version}: "
            f"{versions!r}"
        )
    if python_versions != [">=3.11"]:
        _fail(
            f"{label} Requires-Python is not exactly >=3.11: "
            f"{python_versions!r}"
        )
    if license_expressions != ["Unlicense"]:
        _fail(
            f"{label} License-Expression is not exactly 'Unlicense': "
            f"{license_expressions!r}"
        )
    if license_files != ["LICENSE"]:
        _fail(
            f"{label} License-File is not exactly 'LICENSE': "
            f"{license_files!r}"
        )
    if legacy_licenses:
        _fail(f"{label} contains legacy License metadata: {legacy_licenses!r}")
    requirements = {}
    for declaration in message.get_all("Requires-Dist", []):
        match = _REQUIREMENT.fullmatch(declaration)
        if match is None:
            _fail(
                f"{label} dependency metadata is not one reviewed lower "
                f"bound: {declaration!r}"
            )
        raw_name, operator, version = match.groups()
        canonical_name = re.sub(r"[-_.]+", "-", raw_name).lower()
        if canonical_name in requirements:
            _fail(
                f"{label} has duplicate dependency metadata: "
                f"{canonical_name}"
            )
        requirements[canonical_name] = operator + version
    if requirements != _EXPECTED_RUNTIME_REQUIREMENTS:
        _fail(
            f"{label} dependency metadata disagrees with the reviewed "
            f"runtime floors: {requirements!r}"
        )



def _first_metadata_divergence(expected: bytes, actual: bytes) -> str:
    """Describe the first line where two METADATA blobs differ, for an actionable failure."""
    expected_lines = expected.split(b"\r\n")
    actual_lines = actual.split(b"\r\n")
    for index in range(max(len(expected_lines), len(actual_lines))):
        want = expected_lines[index] if index < len(expected_lines) else None
        got = actual_lines[index] if index < len(actual_lines) else None
        if want == got:
            continue
        def show(value):
            if value is None:
                return "<no such line>"
            text = value.decode("utf-8", "replace")
            return repr(text if len(text) <= 160 else text[:160] + "...")
        return (
            f"line {index + 1} differs; reviewed={show(want)} built={show(got)} "
            f"(reviewed {len(expected)} bytes, built {len(actual)} bytes)"
        )
    return f"no line differs but byte length does: reviewed {len(expected)}, built {len(actual)}"


def _trusted_package_metadata(
    trusted_source_root: Path,
    expected_version: str,
) -> bytes:
    description = _read_regular_file(
        trusted_source_root / "README.md",
        "trusted package description README.md",
        max_bytes=_MAX_ARCHIVE_MEMBER_BYTES,
    )
    if b"\r" in description:
        _fail("trusted package description README.md has noncanonical newlines")
    try:
        description.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"trusted package description README.md is not valid UTF-8: {exc}")
    return (
        b"Metadata-Version: 2.4\r\n"
        + b"Name: yt-final\r\n"
        + f"Version: {expected_version}\r\n".encode("ascii")
        + b"Summary: Turn YouTube URLs into edit-ready media for Premiere, Resolve, and Avid: hardware-accelerated encoding, NLE marker export, durable queue, and a local web console.\r\n"
        + b"License-Expression: Unlicense\r\n"
        + b"Project-URL: Homepage, https://github.com/EgerDev/yt-final\r\n"
        + b"Project-URL: Source, https://github.com/EgerDev/yt-final\r\n"
        + b"Project-URL: Issues, https://github.com/EgerDev/yt-final/issues\r\n"
        + b"Project-URL: Changelog, https://github.com/EgerDev/yt-final/blob/main/CHANGELOG.md\r\n"
        + b"Keywords: youtube,yt-dlp,ffmpeg,video,download,nle,premiere,resolve,avid,prores,dnxhr,markers,edl,fcpxml\r\n"
        + b"Classifier: Development Status :: 5 - Production/Stable\r\n"
        + b"Classifier: Environment :: Console\r\n"
        + b"Classifier: Environment :: Web Environment\r\n"
        + b"Classifier: Intended Audience :: End Users/Desktop\r\n"
        + b"Classifier: Operating System :: Microsoft :: Windows\r\n"
        + b"Classifier: Operating System :: MacOS :: MacOS X\r\n"
        + b"Classifier: Programming Language :: Python :: 3.11\r\n"
        + b"Classifier: Programming Language :: Python :: 3.12\r\n"
        + b"Classifier: Programming Language :: Python :: 3.13\r\n"
        + b"Classifier: Programming Language :: Python :: 3.14\r\n"
        + b"Classifier: Topic :: Multimedia :: Video\r\n"
        + b"Classifier: Topic :: Multimedia :: Video :: Conversion\r\n"
        + b"Requires-Python: >=3.11\r\n"
        + b"Description-Content-Type: text/markdown\r\n"
        + b"License-File: LICENSE\r\n"
        + b"Requires-Dist: packaging>=25.0\r\n"
        + b"Requires-Dist: regex>=2026.7.19\r\n"
        + b"Requires-Dist: yt-dlp>=2026.7.4\r\n"
        + b"Requires-Dist: yt-dlp-ejs>=0.8.0\r\n"
        + b"Requires-Dist: bgutil-ytdlp-pot-provider>=1.3.1\r\n"
        + b"Requires-Dist: requests>=2.32.2\r\n"
        + b"Requires-Dist: urllib3>=2.0.2\r\n"
        + b"Dynamic: license-file\r\n"
        + b"\r\n"
        + description.replace(b"\n", b"\r\n")
    )


def _version_source(body: bytes, *, label: str, expected_version: str) -> None:
    try:
        source = body.decode("utf-8")
        tree = ast.parse(source, filename=label)
    except (UnicodeDecodeError, SyntaxError) as exc:
        _fail(f"{label} version source is invalid: {exc}")
    values = []
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in targets
        ):
            value = statement.value
            if not isinstance(value, ast.Constant) or not isinstance(
                value.value,
                str,
            ):
                _fail(f"{label} __version__ is not a literal string")
            values.append(value.value)
    if values != [expected_version]:
        _fail(
            f"{label} version disagrees with {expected_version}: "
            f"{values!r}"
        )


def _verify_all_version_sources(
    label: str,
    files: dict[str, bytes],
    expected_version: str,
) -> None:
    sources = {
        name: body
        for name, body in files.items()
        if name.rsplit("/", 1)[-1] == "_version.py"
    }
    if not sources:
        _fail(f"{label} contains no _version.py")
    for name, body in sorted(sources.items()):
        _version_source(
            body,
            label=f"{label}:{name}",
            expected_version=expected_version,
        )


def _verify_wheel_record(files: dict[str, bytes], record_name: str) -> None:
    body = files.get(record_name)
    if body is None:
        _fail("wheel RECORD is missing")
    if not body.endswith(b"\n") or b"\r" in body:
        _fail("wheel RECORD must be non-empty, LF-only, and newline-terminated")
    try:
        text = body.decode("utf-8")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        _fail(f"wheel RECORD is not canonical UTF-8 CSV: {exc}")
    if not rows:
        _fail("wheel RECORD is empty")
    recorded: set[str] = set()
    recorded_keys: set[str] = set()
    for row in rows:
        if len(row) != 3:
            _fail("wheel RECORD rows must have exactly three columns")
        path, digest, size = row
        canonical, key = _validated_member_name(
            path,
            label="wheel RECORD",
            directory=False,
        )
        if canonical in recorded or key in recorded_keys:
            _fail(f"duplicate wheel RECORD path: {canonical}")
        recorded.add(canonical)
        recorded_keys.add(key)
        if canonical == record_name:
            if digest or size:
                _fail("wheel RECORD self-row must have empty hash and size")
            continue
        member = files.get(canonical)
        if member is None:
            _fail(f"wheel RECORD names a missing file: {canonical}")
        digest_match = _RECORD_DIGEST.fullmatch(digest)
        if digest_match is None:
            _fail(f"wheel RECORD digest is not canonical SHA-256: {canonical}")
        expected_digest = base64.urlsafe_b64encode(
            hashlib.sha256(member).digest()
        ).rstrip(b"=").decode("ascii")
        if digest_match.group(1) != expected_digest:
            _fail(f"wheel RECORD checksum mismatch: {canonical}")
        if _CANONICAL_SIZE.fullmatch(size) is None:
            _fail(f"wheel RECORD size is not canonical decimal: {canonical}")
        if int(size) != len(member):
            _fail(f"wheel RECORD size mismatch: {canonical}")
    if recorded != set(files):
        _fail(
            "wheel RECORD paths do not exactly match wheel members; "
            f"missing={sorted(set(files) - recorded)}, "
            f"extra={sorted(recorded - set(files))}"
        )


def _verify_portable(
    payload: bytes,
    expected_version: str,
    expected_date_time: tuple[int, int, int, int, int, int] | None = None,
) -> tuple[dict[str, int], dict[str, bytes]]:
    files = _read_zip_files(
        payload,
        "portable",
        expected_date_time=expected_date_time,
    )
    relative = _relative_files(
        files,
        f"yt_final-{expected_version}",
        label="portable",
    )
    _reject_forbidden("portable", set(relative))
    _require_exact_members("portable", set(relative), _PORTABLE_FILES)
    _verify_all_version_sources("portable", relative, expected_version)
    return {"files": len(relative)}, relative


def _verify_wheel(
    payload: bytes,
    expected_version: str,
    expected_date_time: tuple[int, int, int, int, int, int] | None = None,
) -> tuple[dict[str, int], dict[str, bytes]]:
    files = _read_zip_files(
        payload,
        "wheel",
        expected_date_time=expected_date_time,
    )
    dist_info = f"yt_final-{expected_version}.dist-info"
    expected = frozenset(
        {
            *(f"Final/{name}" for name in _WHEEL_MODULES | _WHEEL_DATA),
            f"{dist_info}/METADATA",
            f"{dist_info}/RECORD",
            f"{dist_info}/WHEEL",
            f"{dist_info}/entry_points.txt",
            f"{dist_info}/licenses/LICENSE",
            f"{dist_info}/top_level.txt",
        }
    )
    _reject_forbidden("wheel", set(files))
    _require_exact_members("wheel", set(files), expected)
    _metadata_version(
        files[f"{dist_info}/METADATA"],
        label="wheel METADATA",
        expected_version=expected_version,
    )
    if files[f"{dist_info}/WHEEL"] != _EXPECTED_WHEEL_METADATA:
        _fail("wheel WHEEL control metadata is not the reviewed pure-Python tag")
    if files[f"{dist_info}/entry_points.txt"] != _EXPECTED_WHEEL_ENTRY_POINTS:
        _fail("wheel entry point metadata is not the reviewed CLI target")
    if files[f"{dist_info}/top_level.txt"] != _EXPECTED_WHEEL_TOP_LEVEL:
        _fail("wheel top-level metadata is not exactly 'Final'")
    _verify_all_version_sources("wheel", files, expected_version)
    _verify_wheel_record(files, f"{dist_info}/RECORD")
    return {"files": len(files)}, files


def _expected_sdist_directories(root: str) -> set[str]:
    directories = {root}
    for relative in _SDIST_FILES:
        components = relative.split("/")
        for boundary in range(1, len(components)):
            directories.add(root + "/" + "/".join(components[:boundary]))
    return directories


def _sdist_sources_sort_key(
    relative: str,
) -> tuple[tuple[int, str], ...]:
    components = relative.split("/")
    return tuple(
        [(1, component) for component in components[:-1]]
        + [(0, components[-1])]
    )


def _expected_sdist_sources() -> bytes:
    members = _SDIST_FILES - {"PKG-INFO", "setup.cfg"}
    return "\n".join(
        sorted(members, key=_sdist_sources_sort_key)
    ).encode("utf-8")


def _verify_sdist(
    payload: bytes,
    expected_version: str,
    trusted_source_root: Path,
) -> tuple[dict[str, int], dict[str, bytes], int]:
    files, directories, archive_mtime = _read_tar_files(payload, "sdist")
    root = f"yt_final-{expected_version}"
    relative = _relative_files(files, root, label="sdist")
    expected_directories = _expected_sdist_directories(root)
    if directories != expected_directories:
        _fail(
            "sdist directory boundary mismatch; "
            f"missing={sorted(expected_directories - directories)}, "
            f"extra={sorted(directories - expected_directories)}"
        )
    _reject_forbidden("sdist", set(relative))
    _require_exact_members("sdist", set(relative), _SDIST_FILES)
    for name in sorted(_SDIST_FILES - _SDIST_GENERATED):
        trusted = _read_regular_file(
            trusted_source_root.joinpath(*name.split("/")),
            f"trusted sdist source {name}",
            max_bytes=_MAX_ARCHIVE_MEMBER_BYTES,
        )
        if relative[name] != trusted:
            _fail(f"sdist file disagrees with trusted source: {name}")
    setup_config = relative["setup.cfg"]
    if b"\r" in setup_config:
        if b"\r\n" not in setup_config:
            _fail("sdist generated setup.cfg has noncanonical newlines")
        setup_config = setup_config.replace(b"\r\n", b"\n")
        if b"\r" in setup_config:
            _fail("sdist generated setup.cfg has noncanonical newlines")
    if setup_config != b"[egg_info]\ntag_build = \ntag_date = 0\n\n":
        _fail("sdist generated setup.cfg is not the reviewed control file")
    if (
        relative["yt_final.egg-info/SOURCES.txt"]
        != _expected_sdist_sources()
    ):
        _fail(
            "sdist generated SOURCES.txt is not the reviewed canonical "
            "source manifest"
        )
    generated_controls = {
        "yt_final.egg-info/dependency_links.txt": b"\n",
        "yt_final.egg-info/entry_points.txt": _EXPECTED_WHEEL_ENTRY_POINTS,
        "yt_final.egg-info/requires.txt": (
            b"packaging>=25.0\n"
            b"regex>=2026.7.19\n"
            b"yt-dlp>=2026.7.4\n"
            b"yt-dlp-ejs>=0.8.0\n"
            b"bgutil-ytdlp-pot-provider>=1.3.1\n"
            b"requests>=2.32.2\n"
            b"urllib3>=2.0.2\n"
        ),
        "yt_final.egg-info/top_level.txt": _EXPECTED_WHEEL_TOP_LEVEL,
    }
    for name, expected in generated_controls.items():
        if relative[name] != expected:
            _fail(f"sdist generated control metadata is unreviewed: {name}")
    _metadata_version(
        relative["PKG-INFO"],
        label="sdist PKG-INFO",
        expected_version=expected_version,
    )
    _metadata_version(
        relative["yt_final.egg-info/PKG-INFO"],
        label="sdist egg-info PKG-INFO",
        expected_version=expected_version,
    )
    _verify_all_version_sources("sdist", relative, expected_version)
    return {"files": len(relative)}, relative, archive_mtime


def _dos_datetime_from_epoch(
    epoch: int,
) -> tuple[int, int, int, int, int, int]:
    try:
        value = datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        _fail(f"sdist timestamp is outside the ZIP date range: {exc}")
    if value.year < 1980:
        value = datetime(1980, 1, 1, tzinfo=timezone.utc)
    if value.year > 2107:
        _fail("sdist timestamp is outside the ZIP date range")
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second - value.second % 2,
    )


def _expected_portable_source_bytes(name: str, body: bytes) -> bytes:
    if name != "Final/Start yt_final.bat":
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"sdist Windows launcher is not valid UTF-8: {exc}")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n").encode("utf-8")


def verify_release(
    dist_dir: Path,
    expected_version: str = DEFAULT_VERSION,
    *,
    trusted_source_root: Path | None = None,
    expected_source_epoch: int | None = None,
) -> dict:
    """Verify one exact wheel/sdist/portable/checksum release set."""
    directory = Path(dist_dir)
    source_root = (
        _TRUSTED_SOURCE_ROOT
        if trusted_source_root is None
        else Path(trusted_source_root)
    )
    try:
        source_metadata = source_root.lstat()
    except OSError as exc:
        _fail(f"trusted source root is unavailable: {source_root}: {exc}")
    if (
        _link_like(source_root, source_metadata)
        or not stat.S_ISDIR(source_metadata.st_mode)
    ):
        _fail(f"trusted source root is not a real directory: {source_root}")
    if (
        expected_source_epoch is not None
        and (
            not isinstance(expected_source_epoch, int)
            or isinstance(expected_source_epoch, bool)
            or not 0 <= expected_source_epoch <= 0xFFFFFFFF
        )
    ):
        _fail(
            "expected source epoch must be an integer from 0 through "
            "4294967295"
        )
    artifact_names = _expected_names(expected_version)
    payloads = _read_artifact_set(directory, artifact_names)
    manifest = _parse_checksums(payloads["SHA256SUMS"], artifact_names)
    artifact_report = {}
    for name in artifact_names:
        digest = hashlib.sha256(payloads[name]).hexdigest()
        if manifest[name] != digest:
            _fail(
                f"checksum mismatch for {name}: "
                f"manifest={manifest[name]}, actual={digest}"
            )
        artifact_report[name] = {
            "sha256": digest,
            "bytes": len(payloads[name]),
        }
    portable_name, wheel_name, sdist_name = artifact_names
    sdist_report, sdist_files, source_epoch = _verify_sdist(
        payloads[sdist_name],
        expected_version,
        source_root,
    )
    if (
        expected_source_epoch is not None
        and source_epoch != expected_source_epoch
    ):
        _fail(
            "sdist timestamp disagrees with the expected source epoch: "
            f"{source_epoch} != {expected_source_epoch}"
        )
    expected_date_time = _dos_datetime_from_epoch(
        source_epoch
        if expected_source_epoch is None
        else expected_source_epoch
    )
    portable_report, portable_files = _verify_portable(
        payloads[portable_name],
        expected_version,
        expected_date_time,
    )
    wheel_report, wheel_files = _verify_wheel(
        payloads[wheel_name],
        expected_version,
        expected_date_time,
    )
    dist_info = f"yt_final-{expected_version}.dist-info"
    wheel_metadata = wheel_files[f"{dist_info}/METADATA"]
    sdist_metadata = sdist_files["PKG-INFO"]
    sdist_egg_metadata = sdist_files["yt_final.egg-info/PKG-INFO"]
    if not (
        wheel_metadata == sdist_metadata == sdist_egg_metadata
    ):
        _fail("cross-artifact package metadata files disagree")
    reviewed_metadata = _trusted_package_metadata(
        source_root,
        expected_version,
    )
    if wheel_metadata != reviewed_metadata:
        # Name the first divergence. Package metadata is public by definition, so quoting a
        # line leaks nothing, and a bare "does not match" on a 15KB blob is unactionable --
        # it cost a full CI round trip per guess to narrow down.
        _fail(
            "package metadata is not the reviewed metadata derived from "
            "the trusted README.md: "
            + _first_metadata_divergence(reviewed_metadata, wheel_metadata)
        )
    shared_runtime = sorted(
        f"Final/{name}" for name in _WHEEL_MODULES | _WHEEL_DATA
    )
    for name in sorted(set(portable_files) & set(sdist_files)):
        if portable_files[name] != _expected_portable_source_bytes(
            name,
            sdist_files[name],
        ):
            _fail(f"cross-artifact portable/sdist files disagree: {name}")
    for name in shared_runtime:
        if not (
            portable_files[name]
            == wheel_files[name]
            == sdist_files[name]
        ):
            _fail(f"cross-artifact runtime files disagree: {name}")
    wheel_license = (
        f"yt_final-{expected_version}.dist-info/licenses/LICENSE"
    )
    if not (
        portable_files["LICENSE"]
        == wheel_files[wheel_license]
        == sdist_files["LICENSE"]
    ):
        _fail("cross-artifact license files disagree")
    archives = {
        "portable": portable_report,
        "wheel": wheel_report,
        "sdist": sdist_report,
    }
    return {
        "version": expected_version,
        "artifacts": artifact_report,
        "archives": archives,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the exact yt_final wheel, sdist, portable ZIP, "
            "and SHA256SUMS release set."
        )
    )
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument(
        "--expected-version",
        default=DEFAULT_VERSION,
        help=f"exact release version (default: {DEFAULT_VERSION})",
    )
    parser.add_argument(
        "--trusted-source-root",
        type=Path,
        default=_TRUSTED_SOURCE_ROOT,
        help=(
            "trusted checkout whose non-generated source bytes must match "
            "the sdist (default: verifier checkout)"
        ),
    )
    parser.add_argument(
        "--expected-source-epoch",
        default=os.environ.get("SOURCE_DATE_EPOCH"),
        help=(
            "exact SOURCE_DATE_EPOCH; defaults to the environment when set, "
            "otherwise the archive epoch is cross-checked internally"
        ),
    )
    arguments = parser.parse_args(argv)
    source_epoch = arguments.expected_source_epoch
    if source_epoch is not None:
        if re.fullmatch(r"0|[1-9][0-9]*", source_epoch) is None:
            parser.error(
                "--expected-source-epoch must be a canonical non-negative "
                "decimal"
            )
        source_epoch = int(source_epoch)
        if source_epoch > 0xFFFFFFFF:
            parser.error("--expected-source-epoch exceeds 4294967295")
    try:
        report = verify_release(
            arguments.dist_dir,
            expected_version=arguments.expected_version,
            trusted_source_root=arguments.trusted_source_root,
            expected_source_epoch=source_epoch,
        )
    except ReleaseVerificationError as exc:
        print(f"Release verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
