from __future__ import annotations

import base64
import copy
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tarfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

import scripts.verify_release as verifier
import scripts.verify_release as _verify_release_module
from scripts.verify_release import ReleaseVerificationError, verify_release


ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.0.0"
EXPECTED_REQUIRES_DIST = (
    b"Requires-Dist: packaging>=25.0\n"
    b"Requires-Dist: regex>=2026.7.19\n"
    b"Requires-Dist: yt-dlp>=2026.7.4\n"
    b"Requires-Dist: yt-dlp-ejs>=0.8.0\n"
    b"Requires-Dist: bgutil-ytdlp-pot-provider>=1.3.1\n"
    b"Requires-Dist: requests>=2.32.2\n"
    b"Requires-Dist: urllib3>=2.0.2\n"
)
EXPECTED_LICENSE_METADATA = (
    b"License-Expression: Unlicense\n"
    b"License-File: LICENSE\n"
)

PORTABLE_FILES = frozenset(
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

WHEEL_MODULES = frozenset(
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

WHEEL_DATA = frozenset(
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

SDIST_FINAL_PYTHON = frozenset(
    {
        *(f"Final/{name}" for name in WHEEL_MODULES),
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

SDIST_STATIC = frozenset(
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

SDIST_GENERATED = frozenset(
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

SDIST_FILES = SDIST_FINAL_PYTHON | SDIST_STATIC | SDIST_GENERATED


def _sdist_sources_sort_key(relative: str) -> tuple[tuple[int, str], ...]:
    components = relative.split("/")
    return tuple(
        [(1, component) for component in components[:-1]]
        + [(0, components[-1])]
    )


def _canonical_sdist_sources() -> bytes:
    members = SDIST_FILES - {"PKG-INFO", "setup.cfg"}
    return "\n".join(
        sorted(members, key=_sdist_sources_sort_key)
    ).encode("utf-8")


def _canonical_package_metadata(version: str = VERSION) -> bytes:
    """Delegate to the verifier's own expectation instead of restating it.

    This used to be a second, hand-maintained copy of verify_release's METADATA literal. Two
    copies of the same 30-line constant is how it silently drifted from pyproject.toml in the
    first place, and the duplicate made the drift twice as expensive to notice: the release
    verifier rejected every macOS build while these tests stayed green against the stale copy.

    Deriving the fixture here is not circular. These tests exercise the verifier's BEHAVIOUR --
    that it accepts a faithful artifact and rejects a tampered one -- and the independent check
    that the expectation still matches what pyproject declares lives in
    test_release_packaging.py::test_reviewed_package_metadata_still_matches_pyproject.
    """
    return _verify_release_module._trusted_package_metadata(ROOT, version)


def _version_source(version: str) -> bytes:
    return f'__version__ = "{version}"\n'.encode("ascii")


def _body(relative: str, version: str) -> bytes:
    if relative == "Final/_version.py":
        return _version_source(version)
    if relative == "yt_final.egg-info/SOURCES.txt":
        return _canonical_sdist_sources()
    source = ROOT.joinpath(*relative.split("/"))
    if source.is_file():
        return source.read_bytes()
    if relative == "setup.cfg":
        return b"[egg_info]\ntag_build = \ntag_date = 0\n\n"
    if relative == "yt_final.egg-info/dependency_links.txt":
        return b"\n"
    if relative == "yt_final.egg-info/entry_points.txt":
        return b"[console_scripts]\nyt-final = Final.cli:main\n"
    if relative == "yt_final.egg-info/requires.txt":
        return (
            b"packaging>=25.0\n"
            b"regex>=2026.7.19\n"
            b"yt-dlp>=2026.7.4\n"
            b"yt-dlp-ejs>=0.8.0\n"
            b"bgutil-ytdlp-pot-provider>=1.3.1\n"
            b"requests>=2.32.2\n"
            b"urllib3>=2.0.2\n"
        )
    if relative == "yt_final.egg-info/top_level.txt":
        return b"Final\n"
    return f"reviewed fixture: {relative}\n".encode()


def _regular_zip_info(name: str) -> ZipInfo:
    info = ZipInfo(name)
    # ZipInfo normalizes the host separator during construction on Windows.
    # Restore the literal archive spelling so path-gate tests exercise the
    # hostile central-directory name rather than a sanitized surrogate.
    info.filename = name
    info.orig_filename = name
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    mode = 0o755 if name.endswith("Final/Start yt_final.command") else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def _write_portable(
    destination: Path,
    *,
    version_source: str = VERSION,
    omitted: str | None = None,
    overrides: dict[str, bytes] | None = None,
    extra: tuple[tuple[ZipInfo | str, bytes], ...] = (),
) -> None:
    root = f"yt_final-{VERSION}"
    overrides = overrides or {}
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for relative in sorted(PORTABLE_FILES - ({omitted} if omitted else set())):
            body = overrides.get(relative, _body(relative, version_source))
            if (
                relative == "Final/Start yt_final.bat"
                and relative not in overrides
            ):
                text = body.decode("utf-8")
                normalized = text.replace("\r\n", "\n").replace("\r", "\n")
                body = normalized.replace("\n", "\r\n").encode("utf-8")
            archive.writestr(
                _regular_zip_info(f"{root}/{relative}"),
                body,
            )
        for info_or_name, body in extra:
            info = (
                _regular_zip_info(info_or_name)
                if isinstance(info_or_name, str)
                else info_or_name
            )
            archive.writestr(info, body)


def _wheel_record(
    files: dict[str, bytes],
    record_name: str,
    *,
    mutation: str | None = None,
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    rows = []
    for name in sorted(files):
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(files[name]).digest()
        ).rstrip(b"=").decode("ascii")
        rows.append([name, f"sha256={digest}", str(len(files[name]))])
    rows.append([record_name, "", ""])
    if mutation == "missing":
        rows.pop(0)
    elif mutation == "duplicate":
        rows.insert(1, list(rows[0]))
    elif mutation == "digest":
        rows[0][1] = "sha256=" + "A" * 43
    elif mutation == "size":
        rows[0][2] = str(int(rows[0][2]) + 1)
    elif mutation == "self":
        rows[-1][1:] = ["sha256=" + "A" * 43, "0"]
    elif mutation == "extra":
        rows.insert(-1, ["Final/ghost.py", "sha256=" + "A" * 43, "0"])
    for row in rows:
        writer.writerow(row)
    return stream.getvalue().encode()


def _write_wheel(
    destination: Path,
    *,
    omitted: str | None = None,
    version_source: str = VERSION,
    metadata_version: str = VERSION,
    extra: tuple[tuple[ZipInfo | str, bytes], ...] = (),
    record_mutation: str | None = None,
) -> None:
    dist_info = f"yt_final-{VERSION}.dist-info"
    files = {
        **{
            f"Final/{relative}": _body(
                f"Final/{relative}",
                version_source,
            )
            for relative in WHEEL_MODULES | WHEEL_DATA
            if f"Final/{relative}" != omitted
        },
        f"{dist_info}/METADATA": _canonical_package_metadata(
            metadata_version
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: setuptools (83.0.0)\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n\n"
        ),
        f"{dist_info}/entry_points.txt": (
            b"[console_scripts]\nyt-final = Final.cli:main\n"
        ),
        f"{dist_info}/licenses/LICENSE": _body("LICENSE", version_source),
        f"{dist_info}/top_level.txt": b"Final\n",
    }
    for info_or_name, body in extra:
        name = (
            info_or_name
            if isinstance(info_or_name, str)
            else info_or_name.filename
        )
        files[name] = body
    record_name = f"{dist_info}/RECORD"
    files[record_name] = _wheel_record(
        files,
        record_name,
        mutation=record_mutation,
    )
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        extras = {
            (
                item
                if isinstance(item, str)
                else item.filename
            ): item
            for item, _body_bytes in extra
        }
        for name, body in sorted(files.items()):
            info_or_name = extras.get(name, name)
            info = (
                _regular_zip_info(info_or_name)
                if isinstance(info_or_name, str)
                else info_or_name
            )
            if name == record_name:
                info.external_attr = (stat.S_IFREG | 0o664) << 16
            archive.writestr(info, body)


def _write_sdist(
    destination: Path,
    *,
    version_source: str = VERSION,
    metadata_version: str = VERSION,
    egg_metadata_version: str = VERSION,
    omitted: str | None = None,
    mode_overrides: dict[str, int] | None = None,
    pax_overrides: dict[str, dict[str, str]] | None = None,
    overrides: dict[str, bytes] | None = None,
    extra: tuple[tarfile.TarInfo, ...] = (),
) -> None:
    root = f"yt_final-{VERSION}"
    mode_overrides = mode_overrides or {}
    pax_overrides = pax_overrides or {}
    overrides = overrides or {}
    files = {}
    for relative in SDIST_FILES - ({omitted} if omitted else set()):
        if relative == "PKG-INFO":
            body = _canonical_package_metadata(metadata_version)
        elif relative == "yt_final.egg-info/PKG-INFO":
            body = _canonical_package_metadata(egg_metadata_version)
        else:
            body = _body(relative, version_source)
        files[relative] = overrides.get(relative, body)
    directories = {root}
    for relative in files:
        components = relative.split("/")
        for boundary in range(1, len(components)):
            directories.add(root + "/" + "/".join(components[:boundary]))
    with destination.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw,
            mtime=0,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w|",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                members = [
                    *(("directory", name, None) for name in directories),
                    *(
                        ("file", f"{root}/{relative}", body)
                        for relative, body in files.items()
                    ),
                ]
                for kind, name, body in sorted(
                    members,
                    key=lambda item: item[1],
                ):
                    info = tarfile.TarInfo(name)
                    if kind == "directory":
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                    else:
                        relative = name[len(root) + 1 :]
                        info.mode = mode_overrides.get(
                            relative,
                            (
                                0o755
                                if relative == "Final/Start yt_final.command"
                                else 0o644
                            ),
                        )
                        info.pax_headers = dict(
                            pax_overrides.get(relative, {})
                        )
                        info.size = len(body)
                    archive.addfile(
                        info,
                        io.BytesIO(body) if body is not None else None,
                    )
                for member in extra:
                    body = b"target" if member.isfile() else None
                    if body is not None:
                        member.size = len(body)
                    archive.addfile(
                        member,
                        io.BytesIO(body) if body else None,
                    )


def _write_checksums(directory: Path) -> None:
    artifacts = sorted(
        (
            directory / f"yt_final-{VERSION}-portable.zip",
            directory / f"yt_final-{VERSION}-py3-none-any.whl",
            directory / f"yt_final-{VERSION}.tar.gz",
        ),
        key=lambda path: path.name,
    )
    body = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in artifacts
    )
    (directory / "SHA256SUMS").write_text(
        body,
        encoding="ascii",
        newline="\n",
    )


def _rewrite_zip_mode(archive_path: Path, member_name: str, mode: int) -> None:
    with ZipFile(archive_path) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        for info, body in entries:
            if info.filename == member_name:
                info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, body)


def _zip64_directory_spoof() -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_regular_zip_info("one"), b"one")
        archive.writestr(_regular_zip_info("two"), b"two")
    payload = buffer.getvalue()
    end_offset = payload.rfind(b"PK\x05\x06")
    (
        _signature,
        _disk_number,
        _central_disk,
        _entries_on_disk,
        entry_count,
        central_size,
        central_offset,
        _comment_size,
    ) = struct.unpack_from("<4s4H2LH", payload, end_offset)
    zip64_offset = end_offset
    name_size = 100
    classic_header = bytearray(
        payload[central_offset : central_offset + 46]
    )
    struct.pack_into("<H", classic_header, 28, name_size)
    struct.pack_into("<H", classic_header, 30, 0)
    struct.pack_into("<H", classic_header, 32, 0)
    locator = struct.pack(
        "<4sLQL",
        b"PK\x06\x07",
        0,
        zip64_offset,
        1,
    )
    classic_directory = (
        bytes(classic_header)
        + b"A" * (name_size - len(locator))
        + locator
    )
    extensible_size = len(classic_directory) - len(locator)
    zip64_record = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44 + extensible_size,
        45,
        45,
        0,
        0,
        entry_count,
        entry_count,
        central_size,
        central_offset,
    )
    classic_offset = zip64_offset + len(zip64_record)
    classic_end = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        1,
        1,
        len(classic_directory),
        classic_offset,
        0,
    )
    return (
        payload[:end_offset]
        + zip64_record
        + classic_directory
        + classic_end
    )


def _insert_zip_central_gap(payload: bytes, hidden: bytes) -> bytes:
    end_offset = payload.rfind(b"PK\x05\x06")
    central_offset = struct.unpack_from("<L", payload, end_offset + 16)[0]
    mutated = bytearray(
        payload[:central_offset] + hidden + payload[central_offset:]
    )
    struct.pack_into(
        "<L",
        mutated,
        end_offset + len(hidden) + 16,
        central_offset + len(hidden),
    )
    return bytes(mutated)


def _insert_first_zip_local_extra(payload: bytes, extra: bytes) -> bytes:
    end_offset = payload.rfind(b"PK\x05\x06")
    central_offset = struct.unpack_from("<L", payload, end_offset + 16)[0]
    name_size = struct.unpack_from("<H", payload, 26)[0]
    insertion = 30 + name_size
    mutated = bytearray(
        payload[:insertion] + extra + payload[insertion:]
    )
    struct.pack_into("<H", mutated, 28, len(extra))
    struct.pack_into(
        "<L",
        mutated,
        end_offset + len(extra) + 16,
        central_offset + len(extra),
    )
    return bytes(mutated)


def _pad_first_zip_deflate(payload: bytes, padding: bytes) -> bytes:
    end_offset = payload.rfind(b"PK\x05\x06")
    central_offset = struct.unpack_from("<L", payload, end_offset + 16)[0]
    name_size = struct.unpack_from("<H", payload, 26)[0]
    extra_size = struct.unpack_from("<H", payload, 28)[0]
    compressed_size = struct.unpack_from("<L", payload, 18)[0]
    data_end = 30 + name_size + extra_size + compressed_size
    assert data_end == central_offset
    mutated = bytearray(payload[:data_end] + padding + payload[data_end:])
    struct.pack_into("<L", mutated, 18, compressed_size + len(padding))
    shifted_central = central_offset + len(padding)
    struct.pack_into(
        "<L",
        mutated,
        shifted_central + 20,
        compressed_size + len(padding),
    )
    struct.pack_into(
        "<L",
        mutated,
        end_offset + len(padding) + 16,
        shifted_central,
    )
    return bytes(mutated)


def _rewrite_zip_timestamp(
    archive_path: Path,
    member_name: str,
    date_time: tuple[int, int, int, int, int, int],
) -> None:
    with ZipFile(archive_path) as archive:
        entries = [
            (copy.copy(info), archive.read(info))
            for info in archive.infolist()
        ]
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        for info, body in entries:
            if info.filename == member_name:
                info.date_time = date_time
            archive.writestr(info, body)


def _rewrite_sdist_without_directories(archive_path: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        entries = [
            (copy.copy(member), archive.extractfile(member).read())
            for member in archive
            if member.isfile()
        ]
    with archive_path.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw,
            mtime=0,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w|",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                for member, body in entries:
                    archive.addfile(member, io.BytesIO(body))


def make_minimal_release_set(
    directory: Path,
    *,
    portable_version: str = VERSION,
    portable_omitted: str | None = None,
    portable_overrides: dict[str, bytes] | None = None,
    wheel_version: str = VERSION,
    wheel_metadata_version: str = VERSION,
    wheel_omitted: str | None = None,
    sdist_version: str = VERSION,
    sdist_metadata_version: str = VERSION,
    sdist_egg_metadata_version: str = VERSION,
    sdist_omitted: str | None = None,
    sdist_mode_overrides: dict[str, int] | None = None,
    sdist_pax_overrides: dict[str, dict[str, str]] | None = None,
    sdist_overrides: dict[str, bytes] | None = None,
    extra_portable: tuple[tuple[ZipInfo | str, bytes], ...] = (),
    extra_wheel: tuple[tuple[ZipInfo | str, bytes], ...] = (),
    extra_sdist: tuple[tarfile.TarInfo, ...] = (),
    record_mutation: str | None = None,
) -> dict[str, Path]:
    artifacts = {
        "portable": directory / f"yt_final-{VERSION}-portable.zip",
        "wheel": directory / f"yt_final-{VERSION}-py3-none-any.whl",
        "sdist": directory / f"yt_final-{VERSION}.tar.gz",
        "manifest": directory / "SHA256SUMS",
    }
    _write_portable(
        artifacts["portable"],
        version_source=portable_version,
        omitted=portable_omitted,
        overrides=portable_overrides,
        extra=extra_portable,
    )
    _write_wheel(
        artifacts["wheel"],
        omitted=wheel_omitted,
        version_source=wheel_version,
        metadata_version=wheel_metadata_version,
        extra=extra_wheel,
        record_mutation=record_mutation,
    )
    _write_sdist(
        artifacts["sdist"],
        version_source=sdist_version,
        metadata_version=sdist_metadata_version,
        egg_metadata_version=sdist_egg_metadata_version,
        omitted=sdist_omitted,
        mode_overrides=sdist_mode_overrides,
        pax_overrides=sdist_pax_overrides,
        overrides=sdist_overrides,
        extra=extra_sdist,
    )
    _write_checksums(directory)
    return artifacts


def test_verifier_accepts_complete_release_and_reports_reviewed_facts(tmp_path):
    artifacts = make_minimal_release_set(tmp_path)

    report = verify_release(tmp_path)

    assert report["version"] == VERSION
    assert set(report["artifacts"]) == {
        path.name for key, path in artifacts.items() if key != "manifest"
    }
    assert all(
        details["sha256"] == hashlib.sha256(
            (tmp_path / name).read_bytes()
        ).hexdigest()
        for name, details in report["artifacts"].items()
    )
    assert report["archives"]["portable"]["files"] == len(PORTABLE_FILES)
    assert report["archives"]["wheel"]["files"] == (
        len(WHEEL_MODULES) + len(WHEEL_DATA) + 6
    )
    assert report["archives"]["sdist"]["files"] == len(SDIST_FILES)


def test_verifier_accepts_reviewed_batch_with_portable_crlf_normalization(
    tmp_path,
):
    relative = "Final/Start yt_final.bat"
    source_body = b"@echo off\r\nREM mixed source ending\nexit /b 0\n"
    portable_body = b"@echo off\r\nREM mixed source ending\r\nexit /b 0\r\n"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    trusted_source = tmp_path / "trusted-source"
    for name in sorted(SDIST_FILES - SDIST_GENERATED):
        destination = trusted_source.joinpath(*name.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            source_body if name == relative else _body(name, VERSION)
        )
    make_minimal_release_set(
        artifacts,
        portable_overrides={relative: portable_body},
        sdist_overrides={relative: source_body},
    )

    report = verify_release(
        artifacts,
        trusted_source_root=trusted_source,
    )

    assert report["version"] == VERSION


def test_verifier_rejects_checksum_mismatch(tmp_path):
    artifacts = make_minimal_release_set(tmp_path)
    with artifacts["wheel"].open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(ReleaseVerificationError, match="checksum"):
        verify_release(tmp_path)


def test_verifier_rejects_duplicate_checksum_entry(tmp_path):
    artifacts = make_minimal_release_set(tmp_path)
    manifest = artifacts["manifest"]
    first_line = manifest.read_bytes().splitlines(keepends=True)[0]
    with manifest.open("ab") as stream:
        stream.write(first_line)

    with pytest.raises(ReleaseVerificationError, match="duplicate"):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda body: body.replace(b"  ", b" *", 1),
        lambda body: body.replace(b"\n", b"\r\n"),
        lambda body: b"A" + body[1:],
        lambda body: b"\n".join(reversed(body.splitlines())) + b"\n",
        lambda body: body.rstrip(b"\n"),
        lambda body: body + b"\n",
        lambda body: b"\xef\xbb\xbf" + body,
    ),
)
def test_verifier_rejects_noncanonical_checksum_manifest(tmp_path, mutation):
    artifacts = make_minimal_release_set(tmp_path)
    manifest = artifacts["manifest"]
    manifest.write_bytes(mutation(manifest.read_bytes()))

    with pytest.raises(ReleaseVerificationError, match="checksum manifest"):
        verify_release(tmp_path)


@pytest.mark.parametrize("kind", ("missing", "extra"))
def test_verifier_rejects_missing_or_extra_top_level_artifact(tmp_path, kind):
    artifacts = make_minimal_release_set(tmp_path)
    if kind == "missing":
        artifacts["sdist"].unlink()
    else:
        (tmp_path / "release-notes.txt").write_text("unexpected\n")

    with pytest.raises(ReleaseVerificationError, match="artifact set"):
        verify_release(tmp_path)


def test_verifier_rejects_version_disagreement_inside_archive(tmp_path):
    make_minimal_release_set(tmp_path, portable_version="9.9.9")
    _write_checksums(tmp_path)

    with pytest.raises(ReleaseVerificationError, match="version"):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    "name",
    (
        "../escape.py",
        "/absolute.py",
        "C:/escape.py",
        "C:escape.py",
        "yt_final-1.0.0/Final\\escape.py",
        "yt_final-1.0.0//Final/escape.py",
        "yt_final-1.0.0/./Final/escape.py",
        "yt_final-1.0.0/Final/../escape.py",
        "yt_final-1.0.0/Final/module.py:stream",
    ),
)
def test_verifier_rejects_unsafe_zip_archive_path(tmp_path, name):
    make_minimal_release_set(
        tmp_path,
        extra_portable=((name, b"escape\n"),),
    )

    with pytest.raises(ReleaseVerificationError, match="unsafe"):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    "name",
    (
        "yt_final-1.0.0/Final\\escape.py",
        "C:/escape.py",
        "C:escape.py",
        "/absolute.py",
        "yt_final-1.0.0/Final/../escape.py",
    ),
)
def test_verifier_rejects_unsafe_tar_archive_path(tmp_path, name):
    member = tarfile.TarInfo(name)
    member.type = tarfile.REGTYPE
    make_minimal_release_set(tmp_path, extra_sdist=(member,))

    with pytest.raises(ReleaseVerificationError, match="unsafe"):
        verify_release(tmp_path)


def test_verifier_rejects_exact_duplicate_archive_member(tmp_path):
    root = f"yt_final-{VERSION}"
    with pytest.warns(UserWarning, match="Duplicate name"):
        make_minimal_release_set(
            tmp_path,
            extra_portable=((f"{root}/README.md", b"duplicate\n"),),
        )

    with pytest.raises(ReleaseVerificationError, match="duplicate"):
        verify_release(tmp_path)


def test_verifier_rejects_casefold_duplicate_archive_member(tmp_path):
    root = f"yt_final-{VERSION}"
    make_minimal_release_set(
        tmp_path,
        extra_portable=((f"{root}/readme.md", b"collision\n"),),
    )

    with pytest.raises(ReleaseVerificationError, match="duplicate"):
        verify_release(tmp_path)


def test_verifier_rejects_unicode_normalized_duplicate_archive_member(
    tmp_path,
):
    root = f"yt_final-{VERSION}"
    make_minimal_release_set(
        tmp_path,
        extra_portable=(
            (f"{root}/Final/caf\u00e9.py", b"composed\n"),
            (f"{root}/Final/cafe\u0301.py", b"decomposed\n"),
        ),
    )

    with pytest.raises(ReleaseVerificationError, match="duplicate"):
        verify_release(tmp_path)


def test_verifier_rejects_zip_symlink(tmp_path):
    root = f"yt_final-{VERSION}"
    link = ZipInfo(f"{root}/Final/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    make_minimal_release_set(
        tmp_path,
        extra_portable=((link, b"../../outside"),),
    )

    with pytest.raises(ReleaseVerificationError, match="symlink"):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    ("member_type", "description"),
    (
        (stat.S_IFIFO, "FIFO"),
        (stat.S_IFCHR, "character device"),
        (stat.S_IFBLK, "block device"),
        (stat.S_IFSOCK, "socket"),
    ),
)
def test_verifier_rejects_zip_nonregular_entry(
    tmp_path,
    member_type,
    description,
):
    root = f"yt_final-{VERSION}"
    member = ZipInfo(f"{root}/Final/special")
    member.create_system = 3
    member.external_attr = (member_type | 0o600) << 16
    make_minimal_release_set(
        tmp_path,
        extra_portable=((member, b"special"),),
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="regular|FIFO|device",
    ):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    ("member_type", "description"),
    (
        (tarfile.SYMTYPE, "symlink"),
        (tarfile.LNKTYPE, "hardlink"),
        (tarfile.FIFOTYPE, "FIFO"),
        (tarfile.CHRTYPE, "character device"),
        (tarfile.BLKTYPE, "block device"),
        (tarfile.CONTTYPE, "contiguous non-regular file"),
    ),
)
def test_verifier_rejects_tar_nonregular_entry(
    tmp_path,
    member_type,
    description,
):
    member = tarfile.TarInfo(f"yt_final-{VERSION}/Final/special")
    member.type = member_type
    if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
        member.linkname = "../../outside"
    make_minimal_release_set(tmp_path, extra_sdist=(member,))

    with pytest.raises(
        ReleaseVerificationError,
        match="regular|symlink|hardlink|FIFO|device",
    ):
        verify_release(tmp_path)


def test_verifier_rejects_forbidden_portable_member(tmp_path):
    root = f"yt_final-{VERSION}"
    make_minimal_release_set(
        tmp_path,
        extra_portable=((f"{root}/archive/secret.py", b"secret\n"),),
    )

    with pytest.raises(ReleaseVerificationError, match="forbidden"):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    ("artifact", "member"),
    (
        ("portable", "Final/webui/app.js"),
        ("wheel", "Final/assets/slate.ttf"),
        ("sdist", "PKG-INFO"),
    ),
)
def test_verifier_rejects_absent_runtime_asset(tmp_path, artifact, member):
    kwargs = {f"{artifact}_omitted": member}
    make_minimal_release_set(tmp_path, **kwargs)

    with pytest.raises(ReleaseVerificationError, match="member boundary"):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    ("kwargs", "source"),
    (
        ({"wheel_version": "9.9.9"}, "wheel"),
        ({"sdist_version": "9.9.9"}, "sdist"),
        ({"wheel_metadata_version": "9.9.9"}, "wheel METADATA"),
        ({"sdist_metadata_version": "9.9.9"}, "sdist PKG-INFO"),
        (
            {"sdist_egg_metadata_version": "9.9.9"},
            "sdist egg-info PKG-INFO",
        ),
    ),
)
def test_verifier_cross_checks_every_embedded_version(tmp_path, kwargs, source):
    make_minimal_release_set(tmp_path, **kwargs)

    with pytest.raises(ReleaseVerificationError, match="version"):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    "license_metadata",
    (
        b"",
        b"License-Expression: MIT\nLicense-File: LICENSE\n",
        b"License-Expression: Unlicense\nLicense-File: COPYING\n",
        (
            b"License-Expression: Unlicense\n"
            b"License-Expression: Unlicense\n"
            b"License-File: LICENSE\n"
        ),
        (
            b"License-Expression: Unlicense\n"
            b"License-File: LICENSE\n"
            b"License: The Unlicense\n"
        ),
    ),
)
def test_verifier_rejects_noncanonical_pep639_metadata(license_metadata):
    body = (
        b"Metadata-Version: 2.4\n"
        b"Name: yt-final\n"
        b"Version: 1.0.0\n"
        b"Requires-Python: >=3.11\n"
        + license_metadata
        + EXPECTED_REQUIRES_DIST
        + b"\n"
    )

    with pytest.raises(ReleaseVerificationError, match="license|License"):
        verifier._metadata_version(
            body,
            label="test package metadata",
            expected_version=VERSION,
        )


@pytest.mark.parametrize(
    ("member", "body"),
    (
        (
            "WHEEL",
            (
                b"Wheel-Version: 1.0\n"
                b"Generator: attacker\n"
                b"Root-Is-Purelib: false\n"
                b"Tag: cp311-cp311-win_amd64\n\n"
            ),
        ),
        (
            "entry_points.txt",
            b"[console_scripts]\nyt-final = attacker.module:main\n",
        ),
        ("top_level.txt", b"attacker\n"),
        (
            "METADATA",
            (
                b"Metadata-Version: 2.4\n"
                b"Name: yt-final\n"
                b"Version: 1.0.0\n"
                b"Requires-Python: >=99\n"
                b"Requires-Dist: attacker>=1\n\n"
            ),
        ),
    ),
)
def test_verifier_rejects_false_wheel_control_metadata(
    tmp_path,
    member,
    body,
):
    dist_info = f"yt_final-{VERSION}.dist-info"
    make_minimal_release_set(
        tmp_path,
        extra_wheel=((f"{dist_info}/{member}", body),),
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="metadata|WHEEL|entry point|top-level|dependency|Python",
    ):
        verify_release(tmp_path)


def test_verifier_rejects_cross_artifact_runtime_disagreement(tmp_path):
    make_minimal_release_set(
        tmp_path,
        portable_overrides={"Final/cli.py": b"attacker-controlled runtime\n"},
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="cross-artifact|disagree",
    ):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    ("relative", "body"),
    (
        ("Final/Start yt_final.bat", b"@echo off\r\ncalc.exe\r\n"),
        (
            "Final/Start yt_final.command",
            b"#!/bin/sh\nexec /tmp/attacker-controlled\n",
        ),
        (
            "Final/examples/config.example.toml",
            b"output_dir = '/attacker-controlled'\n",
        ),
        ("Final/README.md", b"attacker-controlled operator guidance\n"),
    ),
)
def test_verifier_rejects_portable_sdist_shared_file_disagreement(
    tmp_path,
    relative,
    body,
):
    make_minimal_release_set(
        tmp_path,
        portable_overrides={relative: body},
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="cross-artifact|disagree",
    ):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    ("relative", "mode"),
    (
        ("Final/Start yt_final.command", 0o644),
        ("Final/cli.py", 0o666),
        ("Final/cli.py", 0o777),
    ),
)
def test_verifier_rejects_noncanonical_portable_modes(
    tmp_path,
    relative,
    mode,
):
    artifacts = make_minimal_release_set(tmp_path)
    _rewrite_zip_mode(
        artifacts["portable"],
        f"yt_final-{VERSION}/{relative}",
        mode,
    )
    _write_checksums(tmp_path)

    with pytest.raises(ReleaseVerificationError, match="mode|permission"):
        verify_release(tmp_path)


def test_verifier_rejects_cross_artifact_zip_timestamp_drift(tmp_path):
    artifacts = make_minimal_release_set(tmp_path)
    _rewrite_zip_timestamp(
        artifacts["portable"],
        f"yt_final-{VERSION}/Final/cli.py",
        (2037, 12, 31, 23, 59, 58),
    )
    _write_checksums(tmp_path)

    with pytest.raises(
        ReleaseVerificationError,
        match="timestamp|date",
    ):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    (
        (
            {"sdist_mode_overrides": {"Final/Start yt_final.command": 0o644}},
            "mode|permission",
        ),
        (
            {
                "sdist_pax_overrides": {
                    "Final/cli.py": {"comment": "unreviewed"}
                }
            },
            "PAX|extended",
        ),
    ),
)
def test_verifier_rejects_noncanonical_sdist_metadata(tmp_path, kwargs, match):
    make_minimal_release_set(tmp_path, **kwargs)

    with pytest.raises(ReleaseVerificationError, match=match):
        verify_release(tmp_path)


def test_verifier_bounds_top_level_artifact_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(verifier, "_MAX_ARTIFACT_BYTES", 1024)
    make_minimal_release_set(tmp_path)

    with pytest.raises(ReleaseVerificationError, match="size|resource|limit"):
        verify_release(tmp_path)


def test_verifier_bounds_archive_member_count(monkeypatch):
    monkeypatch.setattr(verifier, "_MAX_ARCHIVE_MEMBERS", 1)
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_regular_zip_info("one"), b"one")
        archive.writestr(_regular_zip_info("two"), b"two")

    with pytest.raises(ReleaseVerificationError, match="member count|resource|limit"):
        verifier._read_zip_files(buffer.getvalue(), "wheel")


def test_verifier_rejects_zip_member_count_before_loading_directory(monkeypatch):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_regular_zip_info("one"), b"one")
        archive.writestr(_regular_zip_info("two"), b"two")
    monkeypatch.setattr(verifier, "_MAX_ARCHIVE_MEMBERS", 1)

    def fail_if_directory_is_loaded(*_args, **_kwargs):
        raise AssertionError("ZIP central directory was loaded before its count gate")

    monkeypatch.setattr(verifier, "ZipFile", fail_if_directory_is_loaded)

    with pytest.raises(ReleaseVerificationError, match="member count|resource|limit"):
        verifier._read_zip_files(buffer.getvalue(), "wheel")


def test_verifier_rejects_zip64_spoof_before_loading_directory(monkeypatch):
    payload = _zip64_directory_spoof()

    def fail_if_directory_is_loaded(*_args, **_kwargs):
        raise AssertionError("spoof reached ZipFile before raw ZIP rejection")

    monkeypatch.setattr(verifier, "ZipFile", fail_if_directory_is_loaded)

    with pytest.raises(
        ReleaseVerificationError,
        match="ZIP64|central|canonical",
    ):
        verifier._read_zip_files(payload, "wheel")


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: _insert_zip_central_gap(payload, b"hidden-gap"),
        lambda payload: _insert_first_zip_local_extra(
            payload,
            b"\xCA\xFE",
        ),
    ),
)
def test_verifier_rejects_unindexed_zip_bytes_and_local_extras(mutation):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_regular_zip_info("one"), b"one")

    with pytest.raises(
        ReleaseVerificationError,
        match="local|extra|gap|boundary|canonical|unindexed",
    ):
        verifier._read_zip_files(mutation(buffer.getvalue()), "wheel")


def test_verifier_rejects_zip_local_central_timestamp_disagreement():
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_regular_zip_info("one"), b"one")
    payload = bytearray(buffer.getvalue())
    local_time = struct.unpack_from("<H", payload, 10)[0]
    struct.pack_into("<H", payload, 10, local_time ^ 1)

    with pytest.raises(
        ReleaseVerificationError,
        match="local|timestamp|metadata",
    ):
        verifier._read_zip_files(bytes(payload), "wheel")


def test_verifier_rejects_deflate_padding_that_dilutes_ratio():
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            _regular_zip_info("large"),
            b"A" * (2 * 1024 * 1024),
        )
    payload = _pad_first_zip_deflate(
        buffer.getvalue(),
        bytes(range(256)) * 80,
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="DEFLATE|compressed|trailing|canonical",
    ):
        verifier._read_zip_files(payload, "wheel")


def test_verifier_bounds_expanded_zip_member_bytes(monkeypatch):
    monkeypatch.setattr(verifier, "_MAX_ARCHIVE_MEMBER_BYTES", 1024)
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_regular_zip_info("large"), b"A" * 2048)

    with pytest.raises(ReleaseVerificationError, match="expanded|size|resource|limit"):
        verifier._read_zip_files(buffer.getvalue(), "wheel")


def test_verifier_bounds_expanded_tar_member_bytes(monkeypatch):
    monkeypatch.setattr(verifier, "_MAX_ARCHIVE_MEMBER_BYTES", 1024)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("large")
        info.mode = 0o644
        info.size = 2048
        archive.addfile(info, io.BytesIO(b"A" * info.size))

    with pytest.raises(ReleaseVerificationError, match="expanded|size|resource|limit"):
        verifier._read_tar_files(buffer.getvalue(), "sdist")


def test_verifier_rejects_hidden_bytes_after_tar_logical_eof(tmp_path):
    artifacts = make_minimal_release_set(tmp_path)
    sdist = artifacts["sdist"]
    tar_stream = gzip.decompress(sdist.read_bytes())
    output = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=output,
        mtime=0,
    ) as compressed:
        compressed.write(tar_stream + b"HIDDEN-AFTER-TAR-EOF")
    sdist.write_bytes(output.getvalue())
    _write_checksums(tmp_path)

    with pytest.raises(
        ReleaseVerificationError,
        match="TAR|tar|trailing|boundary|canonical",
    ):
        verify_release(tmp_path)


def test_verifier_rejects_concatenated_gzip_member(tmp_path):
    artifacts = make_minimal_release_set(tmp_path)
    sdist = artifacts["sdist"]
    sdist.write_bytes(
        sdist.read_bytes()
        + gzip.compress(b"HIDDEN-GZIP-MEMBER", compresslevel=9, mtime=0)
    )
    _write_checksums(tmp_path)

    with pytest.raises(
        ReleaseVerificationError,
        match="gzip|member|trailing|canonical",
    ):
        verify_release(tmp_path)


def test_verifier_rejects_hidden_gnu_tar_extension_record():
    raw = io.BytesIO()
    with tarfile.open(
        fileobj=raw,
        mode="w:",
        format=tarfile.GNU_FORMAT,
    ) as archive:
        target = "yt_final-1.0.0/Final/cli.py"
        extension = tarfile.TarInfo("././@LongLink")
        extension.type = tarfile.GNUTYPE_LONGNAME
        extension_body = (target + "\0").encode()
        extension.size = len(extension_body)
        archive.addfile(extension, io.BytesIO(extension_body))
        member = tarfile.TarInfo(target)
        member.mode = 0o644
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    output = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=output,
        mtime=0,
    ) as compressed:
        compressed.write(raw.getvalue())

    with pytest.raises(
        ReleaseVerificationError,
        match="GNU|PAX|extension|type|canonical",
    ):
        verifier._read_tar_files(output.getvalue(), "sdist")


def test_verifier_rejects_gzip_tar_timestamp_disagreement(tmp_path):
    artifacts = make_minimal_release_set(tmp_path)
    sdist = artifacts["sdist"]
    payload = bytearray(sdist.read_bytes())
    struct.pack_into("<L", payload, 4, 123)
    sdist.write_bytes(payload)
    _write_checksums(tmp_path)

    with pytest.raises(
        ReleaseVerificationError,
        match="timestamp|mtime|gzip",
    ):
        verify_release(tmp_path)


def test_verifier_requires_exact_sdist_directory_members(tmp_path):
    artifacts = make_minimal_release_set(tmp_path)
    _rewrite_sdist_without_directories(artifacts["sdist"])
    _write_checksums(tmp_path)

    with pytest.raises(
        ReleaseVerificationError,
        match="director|boundary|missing",
    ):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    "relative",
    ("setup.py", "pyproject.toml", "MANIFEST.in"),
)
def test_verifier_binds_sdist_sources_to_trusted_checkout(
    tmp_path,
    relative,
):
    make_minimal_release_set(
        tmp_path,
        sdist_overrides={relative: b"attacker-controlled source\n"},
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="trusted|source|disagree",
    ):
        verify_release(tmp_path)


def test_verifier_rejects_unreviewed_generated_setup_config(tmp_path):
    make_minimal_release_set(
        tmp_path,
        sdist_overrides={
            "setup.cfg": (
                b"[metadata]\n"
                b"name = attacker\n"
                b"[options]\n"
                b"py_modules = payload\n"
            )
        },
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="setup.cfg|generated|control",
    ):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    ("arbitrary", "reordered", "missing", "duplicate", "trailing-newline"),
)
def test_verifier_requires_exact_canonical_sdist_sources_manifest(
    tmp_path,
    mutation,
):
    canonical = _canonical_sdist_sources()
    lines = canonical.split(b"\n")
    if mutation == "arbitrary":
        hostile = b"attacker-controlled generated metadata"
    elif mutation == "reordered":
        hostile = b"\n".join(reversed(lines))
    elif mutation == "missing":
        hostile = b"\n".join(lines[:-1])
    elif mutation == "duplicate":
        hostile = canonical + b"\n" + lines[0]
    else:
        hostile = canonical + b"\n"
    make_minimal_release_set(
        tmp_path,
        sdist_overrides={"yt_final.egg-info/SOURCES.txt": hostile},
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="SOURCES|generated|control|manifest|canonical",
    ):
        verify_release(tmp_path)


def test_verifier_rejects_cross_artifact_package_metadata_disagreement(
    tmp_path,
):
    canonical = _canonical_package_metadata()
    header, separator, _description = canonical.partition(b"\r\n\r\n")
    hostile = header + separator + b"attacker-controlled description\r\n"
    dist_info = f"yt_final-{VERSION}.dist-info"
    make_minimal_release_set(
        tmp_path,
        extra_wheel=((f"{dist_info}/METADATA", hostile),),
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="cross-artifact|metadata|disagree",
    ):
        verify_release(tmp_path)


@pytest.mark.parametrize("mutation", ("extra-header", "description"))
def test_verifier_binds_complete_package_metadata_to_trusted_source(
    tmp_path,
    mutation,
):
    canonical = _canonical_package_metadata()
    header, separator, description = canonical.partition(b"\r\n\r\n")
    if mutation == "extra-header":
        hostile = (
            header
            + b"\r\nProject-URL: Support, https://attacker.invalid/phish"
            + separator
            + description
        )
    else:
        hostile = header + separator + b"attacker-controlled description\r\n"
    dist_info = f"yt_final-{VERSION}.dist-info"
    make_minimal_release_set(
        tmp_path,
        extra_wheel=((f"{dist_info}/METADATA", hostile),),
        sdist_overrides={
            "PKG-INFO": hostile,
            "yt_final.egg-info/PKG-INFO": hostile,
        },
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="metadata|trusted|reviewed|description",
    ):
        verify_release(tmp_path)


def test_verifier_bounds_tar_extension_expansion_before_archive_parse(
    monkeypatch,
):
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer,
        mode="w:gz",
        format=tarfile.PAX_FORMAT,
    ) as archive:
        info = tarfile.TarInfo("yt_final-1.0.0/Final/cli.py")
        info.mode = 0o644
        info.size = 1
        info.pax_headers = {"comment": "A" * 2048}
        archive.addfile(info, io.BytesIO(b"x"))
    monkeypatch.setattr(
        verifier,
        "_MAX_TAR_STREAM_BYTES",
        1024,
        raising=False,
    )

    with pytest.raises(
        ReleaseVerificationError,
        match="expanded tar stream.*resource.*limit",
    ):
        verifier._read_tar_files(buffer.getvalue(), "sdist")


def test_verifier_bounds_aggregate_archive_expansion(monkeypatch):
    monkeypatch.setattr(verifier, "_MAX_ARCHIVE_EXPANDED_BYTES", 100)
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_regular_zip_info("one"), b"A" * 60)
        archive.writestr(_regular_zip_info("two"), b"B" * 60)

    with pytest.raises(ReleaseVerificationError, match="aggregate|resource|limit"):
        verifier._read_zip_files(buffer.getvalue(), "wheel")


@pytest.mark.parametrize(
    "mutation",
    ("missing", "duplicate", "digest", "size", "self", "extra"),
)
def test_verifier_rejects_false_or_incomplete_wheel_record(
    tmp_path,
    mutation,
):
    make_minimal_release_set(tmp_path, record_mutation=mutation)

    with pytest.raises(ReleaseVerificationError, match="RECORD"):
        verify_release(tmp_path)


@pytest.mark.parametrize("artifact", ("wheel", "sdist"))
def test_verifier_rejects_safe_but_unreviewed_archive_members(
    tmp_path,
    artifact,
):
    root = f"yt_final-{VERSION}"
    kwargs = (
        {"extra_wheel": (("Final/ghost.py", b"ghost\n"),)}
        if artifact == "wheel"
        else {
            "extra_sdist": (
                tarfile.TarInfo(f"{root}/Final/ghost.py"),
            )
        }
    )
    make_minimal_release_set(tmp_path, **kwargs)

    with pytest.raises(ReleaseVerificationError, match="member boundary"):
        verify_release(tmp_path)


@pytest.mark.parametrize(
    ("artifact", "member_name"),
    (
        ("portable", "yt_final-1.0.0/downloads/output.mp4"),
        ("wheel", "Final/test_backdoor.py"),
        ("sdist", "yt_final-1.0.0/.github/workflows/publish.yml"),
    ),
)
def test_verifier_rejects_forbidden_archive_member(
    tmp_path,
    artifact,
    member_name,
):
    kwargs = {}
    if artifact == "sdist":
        kwargs["extra_sdist"] = (tarfile.TarInfo(member_name),)
    else:
        kwargs[f"extra_{artifact}"] = ((member_name, b"forbidden\n"),)
    make_minimal_release_set(tmp_path, **kwargs)

    with pytest.raises(ReleaseVerificationError, match="forbidden"):
        verify_release(tmp_path)


def test_verifier_rejects_corrupt_archive_with_domain_error(tmp_path):
    artifacts = make_minimal_release_set(tmp_path)
    artifacts["wheel"].write_bytes(b"not a zip file")
    _write_checksums(tmp_path)

    with pytest.raises(ReleaseVerificationError, match="wheel"):
        verify_release(tmp_path)


def test_verifier_rejects_wrong_expected_version_and_names(tmp_path):
    make_minimal_release_set(tmp_path)

    with pytest.raises(ReleaseVerificationError, match="artifact set|version"):
        verify_release(tmp_path, expected_version="1.0.1")


def test_verifier_accepts_explicit_trusted_source_and_epoch(tmp_path):
    make_minimal_release_set(tmp_path)

    report = verify_release(
        tmp_path,
        trusted_source_root=ROOT,
        expected_source_epoch=0,
    )

    assert report["version"] == VERSION


def test_verifier_rejects_explicit_source_epoch_mismatch(tmp_path):
    make_minimal_release_set(tmp_path)

    with pytest.raises(
        ReleaseVerificationError,
        match="epoch|timestamp",
    ):
        verify_release(tmp_path, expected_source_epoch=1)


def test_verifier_cli_prints_json_report(tmp_path):
    make_minimal_release_set(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_release.py"),
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["version"] == VERSION


def test_verifier_cli_accepts_explicit_source_boundary(tmp_path):
    make_minimal_release_set(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_release.py"),
            str(tmp_path),
            "--trusted-source-root",
            str(ROOT),
            "--expected-source-epoch",
            "0",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["version"] == VERSION


def test_verifier_cli_fails_closed_with_actionable_error(tmp_path):
    make_minimal_release_set(tmp_path)
    (tmp_path / "SHA256SUMS").write_bytes(b"not a checksum\n")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_release.py"),
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "release verification failed" in result.stderr.lower()


def test_release_guide_installs_locked_runtime_before_wheel_without_deps():
    guide = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")

    lock_install = guide.index("--require-hashes --only-binary=:all:")
    wheel_install = guide.index("--no-index --no-deps")
    assert lock_install < wheel_install
    assert "Final/approved-python.lock" in guide
    assert "would bypass the certified hash-locked runtime closure" in guide
