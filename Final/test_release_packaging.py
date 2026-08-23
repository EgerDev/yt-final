import base64
import ast
import csv
from pathlib import Path, PurePosixPath
from email import message_from_string
import gzip
import hashlib
import importlib
import inspect
import io
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import textwrap
import tomllib
from types import SimpleNamespace
import unicodedata
from zipfile import ZipFile

import pytest
from Final.executable_trust import fixed_authority_executable
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

# Probed ONCE at import, before any test can patch subprocess.run. Tests that shell out to
# `git -C ROOT` assert on repository state and cannot run from an sdist or portable zip, where git
# exits 128. Computing this lazily inside the helper would add a subprocess call that
# test_phase_b_release_git_strips_every_ambient_git_variable counts and fails on.
_ROOT_IS_GIT_CHECKOUT = subprocess.run(
    ["git", "rev-parse", "--git-dir"],
    cwd=ROOT, check=False, capture_output=True, text=True,
).returncode == 0

FINAL_DIR = ROOT / "Final"
REQUIREMENTS_DIR = ROOT / "requirements"

# The reproducible-sdist writer's file-identity guard (setup.py:_metadata_identity) compares
# os.stat(path) against os.fstat(fd). On CPython >= 3.14 on Windows those two query paths return a
# different st_ctime_ns for the SAME unchanged file, so the guard false-positives and the writer
# raises "... identity changed". st_ctime is a real part of that guard (it catches a same-size
# in-place edit whose mtime was restored — ctime cannot be reset via utime), so it must NOT be
# dropped. The reviewed release sdist is built ONLY on the 3.11 cell (matrix upload:true), where
# the guard works; the py314-Windows cell is a portability check that does not build a release, so
# gate the writer-exercising tests there rather than weaken a supply-chain integrity guard.
def _sdist_identity_guard_self_consistent() -> bool:
    """True when os.stat(path) and os.fstat(fd) agree for one unchanged file.

    Probe the actual condition instead of hardcoding an interpreter version. The original gate was
    `sys.version_info[:2] != (3, 11)`, which skips 29 cases (including an attacker-model test) on
    every non-3.11 cell whether or not the divergence is real — and evidence from a py314 CI log
    suggests it may not be. A probe is correct under both hypotheses: where the query paths really
    do disagree it still skips, and where they agree the coverage comes back.
    """
    # Mirrors setup.py::_metadata_identity's fields. Deliberately NOT imported from setup.py:
    # importing that module executes its setup() call. Keep this tuple in step with it.
    def identity(metadata):
        return (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IFMT(metadata.st_mode),
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            getattr(metadata, "st_file_attributes", None),
            getattr(metadata, "st_reparse_tag", None),
        )

    handle, name = tempfile.mkstemp(prefix=".sdist-identity-probe-")
    try:
        os.write(handle, b"probe")
        os.fsync(handle)
        return identity(os.stat(name)) == identity(os.fstat(handle))
    except Exception:
        return True                      # cannot probe -> assume healthy, do not skip blindly
    finally:
        os.close(handle)
        try:
            os.unlink(name)
        except OSError:
            pass


_SDIST_WRITER_UNSUPPORTED = not _sdist_identity_guard_self_consistent()
_skip_sdist_writer_on_win_nonreviewed = pytest.mark.skipif(
    _SDIST_WRITER_UNSUPPORTED,
    reason=(
        "this interpreter's os.stat and os.fstat disagree for an unchanged file, so setup.py's "
        "sdist identity guard false-positives here; the guard itself is load-bearing "
        "(st_ctime catches a same-size in-place edit whose mtime was restored) and must not be "
        "weakened, so the writer-exercising tests are skipped on such a host instead"
    ),
)

REVIEWED_LOCK_INPUTS = {
    "build.in": {
        "build==1.5.0",
        "pip==26.2",
        "pip-tools==7.6.0",
        "setuptools==83.0.0",
        "wheel==0.47.0",
    },
    "dev.in": {
        "-r ../Final/requirements.txt",
        "mypy==2.3.0",
        "pip-audit==2.10.1",
        "pytest==9.1.1",
        "ruff==0.16.0",
        "setuptools==83.0.0",
        "types-regex==2026.7.19.20260720",
    },
    "release.in": {
        "-r ../Final/requirements.txt",
        "-r build.in",
        "twine==7.0.0",
    },
}

EXPECTED_RUNTIME_LOCK_NAMES = {
    "bgutil-ytdlp-pot-provider",
    "certifi",
    "charset-normalizer",
    "idna",
    "packaging",
    "regex",
    "requests",
    "urllib3",
    "yt-dlp",
    "yt-dlp-ejs",
}

EXPECTED_BUILD_LOCK_NAMES = {
    "build",
    "click",
    "colorama",
    "packaging",
    "pip",
    "pip-tools",
    "pyproject-hooks",
    "setuptools",
    "wheel",
}

EXPECTED_DEV_LOCK_NAMES = EXPECTED_RUNTIME_LOCK_NAMES | {
    "ast-serialize",
    "boolean-py",
    "cachecontrol",
    "colorama",
    "cyclonedx-python-lib",
    "defusedxml",
    "filelock",
    "iniconfig",
    "librt",
    "license-expression",
    "markdown-it-py",
    "mdurl",
    "msgpack",
    "mypy",
    "mypy-extensions",
    "packageurl-python",
    "pathspec",
    "pip",
    "pip-api",
    "pip-audit",
    "pip-requirements-parser",
    "platformdirs",
    "pluggy",
    "py-serializable",
    "pygments",
    "pyparsing",
    "pytest",
    "rich",
    "ruff",
    "setuptools",
    "sortedcontainers",
    "tomli",
    "tomli-w",
    "types-regex",
    "typing-extensions",
}

EXPECTED_RELEASE_LOCK_NAMES = {
    "backports-tarfile",
    "bgutil-ytdlp-pot-provider",
    "build",
    "certifi",
    "charset-normalizer",
    "click",
    "colorama",
    "docutils",
    "id",
    "idna",
    "importlib-metadata",
    "jaraco-classes",
    "jaraco-context",
    "jaraco-functools",
    "keyring",
    "markdown-it-py",
    "mdurl",
    "more-itertools",
    "nh3",
    "packaging",
    "pip",
    "pip-tools",
    "pygments",
    "pyproject-hooks",
    "pywin32-ctypes",
    "readme-renderer",
    "regex",
    "requests",
    "requests-toolbelt",
    "rfc3986",
    "rich",
    "setuptools",
    "twine",
    "urllib3",
    "wheel",
    "yt-dlp",
    "yt-dlp-ejs",
    "zipp",
}

LOCK_CONTRACTS = (
    (
        Path("requirements/build.lock"),
        Path("requirements/build.in"),
        EXPECTED_BUILD_LOCK_NAMES,
        "#\n"
        "# This file is autogenerated by pip-compile with Python 3.11\n"
        "# by the following command:\n"
        "#\n"
        "#    pip-compile --allow-unsafe --generate-hashes --newline=LF "
        "--no-emit-index-url --no-emit-trusted-host "
        "--output-file=requirements/build.lock --strip-extras "
        "requirements/build.in\n"
        "#\n",
    ),
    (
        Path("requirements/dev.lock"),
        Path("requirements/dev.in"),
        EXPECTED_DEV_LOCK_NAMES,
        "#\n"
        "# This file is autogenerated by pip-compile with Python 3.11\n"
        "# by the following command:\n"
        "#\n"
        "#    pip-compile --allow-unsafe --generate-hashes --newline=LF "
        "--no-emit-index-url --no-emit-trusted-host "
        "--output-file=requirements/dev.lock --strip-extras "
        "requirements/dev.in\n"
        "#\n",
    ),
    (
        Path("requirements/release.lock"),
        Path("requirements/release.in"),
        EXPECTED_RELEASE_LOCK_NAMES,
        "#\n"
        "# This file is autogenerated by pip-compile with Python 3.11\n"
        "# by the following command:\n"
        "#\n"
        "#    pip-compile --allow-unsafe --generate-hashes --newline=LF "
        "--no-emit-index-url --no-emit-trusted-host "
        "--output-file=requirements/release.lock --strip-extras "
        "requirements/release.in\n"
        "#\n",
    ),
    (
        Path("Final/approved-python.lock"),
        Path("Final/requirements.txt"),
        EXPECTED_RUNTIME_LOCK_NAMES,
        "#\n"
        "# This file is autogenerated by pip-compile with Python 3.11\n"
        "# by the following command:\n"
        "#\n"
        "#    pip-compile --allow-unsafe --generate-hashes --newline=LF "
        "--no-emit-index-url --no-emit-trusted-host "
        "--output-file=Final/approved-python.lock --strip-extras "
        "Final/requirements.txt\n"
        "#\n",
    ),
)

EXPECTED_DIRECT_LOCK_VERSIONS = {
    "runtime": {
        "bgutil-ytdlp-pot-provider": "1.3.1",
        "packaging": "26.2",
        "regex": "2026.7.19",
        "requests": "2.34.2",
        "urllib3": "2.7.0",
        "yt-dlp": "2026.7.4",
        "yt-dlp-ejs": "0.8.0",
    },
    "build": {
        "build": "1.5.0",
        "pip": "26.2",
        "pip-tools": "7.6.0",
        "setuptools": "83.0.0",
        "wheel": "0.47.0",
    },
    "dev": {
        "bgutil-ytdlp-pot-provider": "1.3.1",
        "mypy": "2.3.0",
        "packaging": "26.2",
        "pip-audit": "2.10.1",
        "pytest": "9.1.1",
        "regex": "2026.7.19",
        "requests": "2.34.2",
        "ruff": "0.16.0",
        "setuptools": "83.0.0",
        "types-regex": "2026.7.19.20260720",
        "urllib3": "2.7.0",
        "yt-dlp": "2026.7.4",
        "yt-dlp-ejs": "0.8.0",
    },
    "release": {
        "bgutil-ytdlp-pot-provider": "1.3.1",
        "build": "1.5.0",
        "packaging": "26.2",
        "pip": "26.2",
        "pip-tools": "7.6.0",
        "regex": "2026.7.19",
        "requests": "2.34.2",
        "setuptools": "83.0.0",
        "twine": "7.0.0",
        "urllib3": "2.7.0",
        "wheel": "0.47.0",
        "yt-dlp": "2026.7.4",
        "yt-dlp-ejs": "0.8.0",
    },
}

NON_RUNTIME_DISTRIBUTIONS = {
    "ast-serialize",
    "boolean-py",
    "build",
    "cachecontrol",
    "colorama",
    "cyclonedx-python-lib",
    "defusedxml",
    "filelock",
    "iniconfig",
    "librt",
    "license-expression",
    "markdown-it-py",
    "mdurl",
    "msgpack",
    "mypy",
    "mypy-extensions",
    "packageurl-python",
    "pathspec",
    "pip",
    "pip-api",
    "pip-audit",
    "pip-requirements-parser",
    "pip-tools",
    "platformdirs",
    "pluggy",
    "py-serializable",
    "pygments",
    "pyparsing",
    "pytest",
    "rich",
    "ruff",
    "setuptools",
    "sortedcontainers",
    "tomli",
    "tomli-w",
    "twine",
    "types-regex",
    "typing-extensions",
    "wheel",
}

TIER_ONE_WHEEL_TARGETS = (
    ("windows-x86_64-py311", "win_amd64", "311", "cp311"),
    ("windows-x86_64-py314", "win_amd64", "314", "cp314"),
    ("macos-x86_64-py311", "macosx_10_15_x86_64", "311", "cp311"),
    ("macos-x86_64-py314", "macosx_10_15_x86_64", "314", "cp314"),
    ("macos-arm64-py311", "macosx_11_0_arm64", "311", "cp311"),
    ("macos-arm64-py314", "macosx_11_0_arm64", "314", "cp314"),
)

NATIVE_TARGETS = {
    "windows-x86_64-py311": ("win32", "x86_64", (3, 11)),
    "windows-x86_64-py314": ("win32", "x86_64", (3, 14)),
    "macos-x86_64-py311": ("darwin", "x86_64", (3, 11)),
    "macos-x86_64-py314": ("darwin", "x86_64", (3, 14)),
    "macos-arm64-py311": ("darwin", "arm64", (3, 11)),
    "macos-arm64-py314": ("darwin", "arm64", (3, 14)),
}

WHEEL_EXCLUDED_MODULES = {
    "_syntax_check.py",
    "runtime_verify.py",
}

WHEEL_RUNTIME_MODULES = {
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

WHEEL_PACKAGE_DATA = {
    "approved-python.lock",
    "assets/slate.ttf",
    "requirements.txt",
    "THIRD_PARTY_NOTICES",
    "webui/app.js",
    "webui/index.html",
    "webui/style.css",
}

SDIST_FINAL_PYTHON_MEMBERS = {
    "Final/__init__.py",
    "Final/__main__.py",
    "Final/_cdp_client.py",
    "Final/_syntax_check.py",
    "Final/_version.py",
    "Final/capability.py",
    "Final/cli.py",
    "Final/cookie_providers.py",
    "Final/cookies_auth.py",
    "Final/dependency_contract.py",
    "Final/desync_proxy.py",
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
    "Final/runtime_verify.py",
    "Final/secureio.py",
    "Final/settings.py",
    "Final/speedtest.py",
    "Final/state_validation.py",
    "Final/test_agegate_cli.py",
    "Final/test_agegate_jobstore.py",
    "Final/test_agegate.py",
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
    "Final/test_release_boundary.py",
    "Final/test_release_artifacts.py",
    "Final/test_release_documentation.py",
    "Final/test_release_packaging.py",
    "Final/test_release_workflows.py",
    "Final/test_sdist_inventory_consistency.py",
    "Final/test_section_clip.py",
    "Final/test_settings.py",
    "Final/test_shutdown.py",
    "Final/test_skip_gate_quality.py",
    "Final/test_speedtest_endpoint.py",
    "Final/test_speedtest_ui.py",
    "Final/test_speedtest.py",
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
    "Final/tool_manifest.py",
    "Final/url_identity.py",
    "Final/watch_filters.py",
    "Final/watch.py",
    "Final/watchstore.py",
    "Final/webserver.py",
    "Final/websessions.py",
    "Final/yt_final.py",
}

WHEEL_DIST_INFO_MEMBERS = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "licenses/LICENSE",
    "top_level.txt",
}

WHEEL_DIST_INFO_ROOT = "yt_final-1.0.0.dist-info"
EXPECTED_WHEEL_METADATA = (
    b"Wheel-Version: 1.0\n"
    b"Generator: setuptools (83.0.0)\n"
    b"Root-Is-Purelib: true\n"
    b"Tag: py3-none-any\n"
    b"\n"
)
EXPECTED_WHEEL_ENTRY_POINTS = (
    b"[console_scripts]\n"
    b"yt-final = Final.cli:main\n"
)
EXPECTED_WHEEL_TOP_LEVEL = b"Final\n"

SDIST_STATIC_SOURCE_MEMBERS = {
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
    "release/portable-files.txt",
    "requirements/build.in",
    "requirements/build.lock",
    "requirements/dev.in",
    "requirements/dev.lock",
    "requirements/README.md",
    "requirements/release.in",
    "requirements/release.lock",
    "scripts/__init__.py",
    "scripts/build_portable.py",
    "scripts/make_public_tree.py",
    "scripts/verify_release.py",
    "scripts/write_checksums.py",
    "SECURITY.md",
    "setup.py",
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

SDIST_GENERATED_MEMBERS = {
    "PKG-INFO",
    "setup.cfg",
    "yt_final.egg-info/PKG-INFO",
    "yt_final.egg-info/SOURCES.txt",
    "yt_final.egg-info/dependency_links.txt",
    "yt_final.egg-info/entry_points.txt",
    "yt_final.egg-info/requires.txt",
    "yt_final.egg-info/top_level.txt",
}

RUNTIME_DEPENDENCY_FLOORS = {
    "bgutil-ytdlp-pot-provider": ">=1.3.1",
    "packaging": ">=25.0",
    "regex": ">=2026.7.19",
    "requests": ">=2.32.2",
    "urllib3": ">=2.0.2",
    "yt-dlp": ">=2026.7.4",
    "yt-dlp-ejs": ">=0.8.0",
}


def _effective_lines(path):
    return {
        line.split("#", 1)[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    }


def _unique_metadata_requirements(metadata):
    requirements = {}
    for declaration in metadata.get_all("Requires-Dist") or []:
        requirement = Requirement(declaration)
        name = canonicalize_name(requirement.name)
        assert name not in requirements, (
            f"duplicate Requires-Dist for canonical distribution {name}"
        )
        requirements[name] = requirement
    return requirements


def _validated_wheel_name(name):
    assert isinstance(name, str) and name, "wheel member name is empty"
    assert "\x00" not in name, "wheel member name contains NUL"
    assert "\\" not in name, "wheel member name contains a backslash"
    assert not name.startswith("/"), "wheel member name is absolute"
    assert not name.endswith("/"), "wheel directory entries are forbidden"
    parts = name.split("/")
    assert all(part not in {"", ".", ".."} for part in parts), (
        "wheel member name is not canonical"
    )
    assert not any(":" in part for part in parts), (
        "wheel member name contains a drive or stream component"
    )
    return unicodedata.normalize("NFC", name).casefold()


def _validated_wheel_files(wheel_path):
    files = {}
    normalized_names = set()
    with ZipFile(wheel_path) as wheel:
        infos = wheel.infolist()
        assert infos, "wheel is empty"
        for info in infos:
            assert not info.is_dir(), "wheel directory entries are forbidden"
            assert info.orig_filename == info.filename, (
                "wheel member name contains a NUL-truncated suffix"
            )
            name = info.filename
            normalized = _validated_wheel_name(name)
            assert name not in files, f"duplicate wheel member: {name}"
            assert normalized not in normalized_names, (
                f"duplicate normalized/casefold wheel member: {name}"
            )
            body = wheel.read(info)
            assert info.file_size == len(body)
            files[name] = body
            normalized_names.add(normalized)

    record_name = f"{WHEEL_DIST_INFO_ROOT}/RECORD"
    wheel_metadata_name = f"{WHEEL_DIST_INFO_ROOT}/WHEEL"
    entry_points_name = f"{WHEEL_DIST_INFO_ROOT}/entry_points.txt"
    top_level_name = f"{WHEEL_DIST_INFO_ROOT}/top_level.txt"
    required_control_members = {
        record_name,
        wheel_metadata_name,
        entry_points_name,
        top_level_name,
    }
    assert required_control_members <= set(files), (
        "wheel control metadata is incomplete"
    )
    assert files[wheel_metadata_name] == EXPECTED_WHEEL_METADATA
    assert files[entry_points_name] == EXPECTED_WHEEL_ENTRY_POINTS
    assert files[top_level_name] == EXPECTED_WHEEL_TOP_LEVEL

    record_body = files[record_name]
    assert record_body.endswith(b"\n") and b"\r" not in record_body, (
        "wheel RECORD must use canonical LF lines"
    )
    try:
        record_text = record_body.decode("utf-8")
        rows = list(
            csv.reader(
                io.StringIO(record_text, newline=""),
                strict=True,
            )
        )
    except (UnicodeDecodeError, csv.Error):
        raise AssertionError("wheel RECORD is not valid UTF-8 CSV") from None
    assert rows, "wheel RECORD is empty"

    recorded_paths = set()
    recorded_normalized_paths = set()
    for row in rows:
        assert len(row) == 3, "wheel RECORD rows must have three columns"
        path, digest, size = row
        normalized = _validated_wheel_name(path)
        assert path not in recorded_paths, (
            f"duplicate wheel RECORD path: {path}"
        )
        assert normalized not in recorded_normalized_paths, (
            f"duplicate normalized/casefold wheel RECORD path: {path}"
        )
        recorded_paths.add(path)
        recorded_normalized_paths.add(normalized)

        if path == record_name:
            assert digest == "" and size == "", (
                "wheel RECORD self-row must have empty hash and size"
            )
            continue

        assert path in files, f"wheel RECORD names a missing file: {path}"
        digest_match = re.fullmatch(
            r"sha256=([A-Za-z0-9_-]{43})",
            digest,
        )
        assert digest_match, (
            "wheel RECORD hashes must be unpadded base64url SHA-256"
        )
        expected_digest = base64.urlsafe_b64encode(
            hashlib.sha256(files[path]).digest()
        ).rstrip(b"=").decode("ascii")
        assert digest_match.group(1) == expected_digest, (
            f"wheel RECORD hash mismatch: {path}"
        )
        assert re.fullmatch(r"0|[1-9][0-9]*", size), (
            f"wheel RECORD size is not canonical decimal: {path}"
        )
        assert int(size) == len(files[path]), (
            f"wheel RECORD size mismatch: {path}"
        )

    assert recorded_paths == set(files), (
        "wheel RECORD paths must exactly equal wheel file members"
    )
    return files


def _locked_requirements(path):
    text = path.read_text(encoding="utf-8")
    assert not text.startswith("\ufeff"), f"{path} has a UTF-8 BOM"
    assert "\r" not in text, f"{path} is not LF-only"
    assert "--index-url" not in text
    assert "--extra-index-url" not in text
    assert "--trusted-host" not in text

    blocks = []
    current = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace():
            assert current, f"orphan continuation in {path}: {raw_line!r}"
            current.append(stripped)
        else:
            if current:
                blocks.append(current)
            current = [stripped]
    if current:
        blocks.append(current)

    locked = {}
    for block in blocks:
        assert block[0].endswith("\\"), (
            f"lock declaration is not continued in {path}: {block[0]!r}"
        )
        declaration = block[0].removesuffix("\\").strip()
        assert re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*==[^\s;\\]+",
            declaration,
        ), f"unsupported lock declaration in {path}: {declaration!r}"
        requirement = Requirement(declaration)
        assert not requirement.extras, (
            f"extras are forbidden in {path}: {declaration!r}"
        )
        assert requirement.url is None, (
            f"direct URL requirement in {path}: {declaration!r}"
        )
        assert requirement.marker is None, (
            f"markers require a separately reviewed lock design in {path}: "
            f"{declaration!r}"
        )
        specifiers = tuple(requirement.specifier)
        assert len(specifiers) == 1
        assert specifiers[0].operator == "=="
        assert "*" not in specifiers[0].version

        name = canonicalize_name(requirement.name)
        assert name not in locked, f"duplicate lock requirement in {path}: {name}"
        hash_values = []
        for index, continuation in enumerate(block[1:]):
            match = re.fullmatch(
                r"--hash=sha256:([0-9a-f]{64})(?:\s+\\)?",
                continuation,
            )
            assert match, (
                f"unsupported lock continuation in {path}: {continuation!r}"
            )
            is_last = index == len(block[1:]) - 1
            assert continuation.endswith("\\") is not is_last, (
                f"invalid continuation chain in {path}: {continuation!r}"
            )
            hash_values.append(match.group(1))
        assert hash_values, f"unhashed lock requirement in {path}: {name}"
        assert len(hash_values) == len(set(hash_values)), (
            f"duplicate artifact hash in {path}: {name}"
        )
        locked[name] = (
            Version(specifiers[0].version),
            requirement.marker,
            set(hash_values),
        )
    assert locked, f"empty lock: {path}"
    return locked


def _runtime_lock_path():
    return FINAL_DIR / "approved-python.lock"


def _assert_exact_lock_names(lock_path, expected_names):
    actual_names = set(_locked_requirements(lock_path))
    assert actual_names == expected_names, (
        f"unexpected lock closure in {lock_path}: "
        f"expected={sorted(expected_names)!r}, actual={sorted(actual_names)!r}"
    )


def _assert_generated_hash_lock(
    lock_path, *, input_path, expected_names, expected_header
):
    text = lock_path.read_text(encoding="utf-8")
    relative_lock = lock_path.resolve().relative_to(ROOT.resolve()).as_posix()
    assert f"--output-file={relative_lock}" in expected_header
    assert expected_header.endswith(f"{input_path.as_posix()}\n#\n")
    assert text.startswith(expected_header), (
        f"{lock_path} does not have the exact verbatim pip-tools header"
    )
    _assert_exact_lock_names(lock_path, expected_names)


def _sanitized_python_env(environment_updates=None):
    environment = os.environ.copy()
    for name in tuple(environment):
        upper_name = name.upper()
        if (
            upper_name.startswith(("PIP_", "PYTHON", "XDG_"))
            or upper_name
            in {
                "APPDATA",
                "HOME",
                "LOCALAPPDATA",
                "TEMP",
                "TMP",
                "TMPDIR",
                "USERPROFILE",
            }
        ):
            environment.pop(name, None)
    if environment_updates:
        environment.update(environment_updates)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return environment


def _run_isolated_python(
    python, *arguments, environment_updates=None, **subprocess_options
):
    assert "env" not in subprocess_options
    environment = _sanitized_python_env(environment_updates)
    return subprocess.run(
        [str(python), "-I", *map(str, arguments)],
        env=environment,
        **subprocess_options,
    )


def _run_isolated_python_module(
    python, module, *arguments, **subprocess_options
):
    command = ["-m", module]
    if module == "pip":
        command.append("--isolated")
    command.extend(arguments)
    return _run_isolated_python(
        python, *command, **subprocess_options
    )


def _run_validated_lock_python(
    lock_python, *arguments, **subprocess_options
):
    validated_lock_python = _validated_lock_python(lock_python)
    return _run_isolated_python(
        validated_lock_python, *arguments, **subprocess_options
    )


def _run_validated_lock_python_module(
    lock_python, module, *arguments, **subprocess_options
):
    command = ["-m", module]
    if module == "pip":
        command.append("--isolated")
    command.extend(arguments)
    return _run_validated_lock_python(
        lock_python, *command, **subprocess_options
    )


def _assert_windows_64bit_executable(path, error):
    import ctypes

    assert path.suffix.lower() == ".exe", error
    binary_type = ctypes.c_uint()
    get_binary_type = ctypes.WinDLL(
        "kernel32", use_last_error=True
    ).GetBinaryTypeW
    get_binary_type.argtypes = (
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint),
    )
    get_binary_type.restype = ctypes.c_int
    assert get_binary_type(str(path), ctypes.byref(binary_type)), error
    assert binary_type.value == 6, error  # SCS_64BIT_BINARY


def _validated_lock_python(lock_python):
    error = (
        "YT_FINAL_LOCK_PYTHON must be an absolute, existing, executable "
        "regular file at a link-free path with stable resolved identity"
    )
    try:
        candidate = Path(lock_python)
        assert candidate.is_absolute(), error
        validated = fixed_authority_executable(
            (Path(candidate.anchor),),
            candidate,
            allow_internal_links=False,
        )
        assert validated is not None, error
        candidate_metadata = candidate.lstat()
        validated_metadata = validated.lstat()
        assert stat.S_ISREG(candidate_metadata.st_mode), error
        assert stat.S_ISREG(validated_metadata.st_mode), error
        assert os.path.samefile(candidate, validated), error
        assert os.path.samestat(
            candidate_metadata, validated_metadata
        ), error
        if os.name == "nt":
            _assert_windows_64bit_executable(validated, error)
        else:
            assert os.access(validated, os.X_OK), error

        revalidated = fixed_authority_executable(
            (Path(candidate.anchor),),
            candidate,
            allow_internal_links=False,
        )
        assert revalidated is not None, error
        revalidated_metadata = revalidated.lstat()
        assert os.path.samefile(candidate, revalidated), error
        assert os.path.samestat(
            candidate_metadata, revalidated_metadata
        ), error
    except (OSError, RuntimeError, TypeError, ValueError):
        raise AssertionError(error) from None
    return str(revalidated)


def _verified_lock_toolchain(lock_python, expected_python=(3, 11)):
    validated_lock_python = _validated_lock_python(lock_python)
    probe = _run_validated_lock_python(
        validated_lock_python,
        "-c",
        (
            "import json, os, platform, struct, sys, sysconfig; "
            "from importlib.metadata import version; "
            "print(json.dumps({"
            "'abi_flags': getattr(sys, 'abiflags', ''), "
            "'cache_tag': sys.implementation.cache_tag, "
            "'executable': os.path.realpath(sys.executable), "
            "'gil_disabled': bool("
            "sysconfig.get_config_var('Py_GIL_DISABLED')), "
            "'implementation': sys.implementation.name, "
            "'machine': platform.machine(), "
            "'pip': version('pip'), "
            "'pip-tools': version('pip-tools'), "
            "'platform': sys.platform, "
            "'platform_tag': sysconfig.get_platform(), "
            "'pointer_bits': struct.calcsize('P') * 8, "
            "'python': [sys.version_info.major, sys.version_info.minor], "
            "'trace_refs': bool("
            "sysconfig.get_config_var('Py_TRACE_REFS'))"
            "}))"
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    actual = json.loads(probe.stdout)
    assert actual.get("pip") == "26.2", (
        "reviewed lock toolchain requires pip==26.2"
    )
    assert actual.get("pip-tools") == "7.6.0", (
        "reviewed lock toolchain requires pip-tools==7.6.0"
    )
    assert actual.get("implementation") == "cpython", (
        "reviewed lock toolchain requires CPython"
    )
    assert actual.get("pointer_bits") == 64, (
        "reviewed lock toolchain requires a 64-bit process"
    )
    assert tuple(actual.get("python", ())) == expected_python, (
        "reviewed lock toolchain requires "
        f"Python {expected_python[0]}.{expected_python[1]}"
    )
    assert actual.get("gil_disabled") is False, (
        "free-threaded Python cannot certify standard-ABI lock evidence"
    )
    assert actual.get("trace_refs") is False, (
        "trace-reference Python cannot certify standard-ABI lock evidence"
    )
    expected_cache_tag = (
        f"cpython-{expected_python[0]}{expected_python[1]}"
    )
    assert actual.get("abi_flags") == "", (
        "reviewed lock toolchain requires the standard ABI"
    )
    assert actual.get("cache_tag") == expected_cache_tag, (
        "reviewed lock toolchain requires the standard ABI"
    )
    reported_executable = actual.get("executable")
    same_interpreter_error = (
        "toolchain probe must report the same interpreter selected by "
        "YT_FINAL_LOCK_PYTHON"
    )
    try:
        reported_path = Path(reported_executable)
        assert reported_path.is_absolute(), same_interpreter_error
        assert os.path.samefile(
            validated_lock_python, reported_path
        ), same_interpreter_error
    except (OSError, TypeError, ValueError):
        raise AssertionError(same_interpreter_error) from None

    revalidated_lock_python = _validated_lock_python(validated_lock_python)
    assert os.path.samefile(
        validated_lock_python, revalidated_lock_python
    ), same_interpreter_error
    return revalidated_lock_python, actual


def _verified_build_toolchain(
    build_python,
    *,
    environment_updates,
    cwd,
    lock_path=None,
    environment_label="build",
):
    validated_build_python = _validated_lock_python(build_python)
    assert not _path_is_within_or_equal(validated_build_python, ROOT), (
        "release toolchain interpreters must be outside the repository"
    )
    probe = _run_validated_lock_python(
        validated_build_python,
        "-c",
        (
            "import json, os, platform, struct, sys, sysconfig\n"
            "from importlib.metadata import distributions\n"
            "from packaging.utils import canonicalize_name\n"
            "versions = {}\n"
            "duplicates = []\n"
            "for distribution in distributions():\n"
            "    name = canonicalize_name(distribution.metadata['Name'])\n"
            "    if name in versions:\n"
            "        duplicates.append(name)\n"
            "    versions[name] = distribution.version\n"
            "print(json.dumps({"
            "'abi_flags': getattr(sys, 'abiflags', ''), "
            "'cache_tag': sys.implementation.cache_tag, "
            "'debug': bool(sysconfig.get_config_var('Py_DEBUG')), "
            "'duplicates': sorted(duplicates), "
            "'executable': os.path.realpath(sys.executable), "
            "'gil_disabled': bool("
            "sysconfig.get_config_var('Py_GIL_DISABLED')), "
            "'implementation': sys.implementation.name, "
            "'machine': platform.machine(), "
            "'platform': sys.platform, "
            "'platform_tag': sysconfig.get_platform(), "
            "'pointer_bits': struct.calcsize('P') * 8, "
            "'python': [sys.version_info.major, sys.version_info.minor], "
            "'trace_refs': bool("
            "sysconfig.get_config_var('Py_TRACE_REFS')), "
            "'versions': versions"
            "}))"
        ),
        environment_updates=environment_updates,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    actual = json.loads(probe.stdout)
    python_version = tuple(actual.get("python", ()))
    assert python_version in {(3, 11), (3, 14)}, (
        "release builds require Python 3.11 or 3.14"
    )
    assert actual.get("implementation") == "cpython", (
        "release builds require CPython"
    )
    assert actual.get("pointer_bits") == 64, (
        "release builds require a 64-bit process"
    )
    assert actual.get("gil_disabled") is False, (
        "free-threaded Python cannot build release artifacts"
    )
    assert actual.get("trace_refs") is False, (
        "trace-reference Python cannot build release artifacts"
    )
    assert actual.get("debug") is False, (
        "debug Python cannot build release artifacts"
    )
    assert actual.get("abi_flags") == "", (
        "release builds require the standard CPython ABI"
    )
    assert actual.get("cache_tag") == (
        f"cpython-{python_version[0]}{python_version[1]}"
    ), "release builds require the standard CPython ABI"

    expected_platform = sys.platform
    assert expected_platform in {"win32", "darwin"}, (
        "release builds are supported only on Windows and macOS"
    )
    assert actual.get("platform") == expected_platform, (
        "release build interpreter must be native to this host"
    )
    normalized_machine = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    host_machine = normalized_machine.get(
        str(platform.machine()).lower(),
        str(platform.machine()).lower(),
    )
    actual_machine = normalized_machine.get(
        str(actual.get("machine", "")).lower(),
        str(actual.get("machine", "")).lower(),
    )
    assert actual_machine == host_machine, (
        "release build interpreter must match the native host architecture"
    )
    platform_tag = str(actual.get("platform_tag", "")).lower()
    if expected_platform == "win32":
        assert platform_tag == {
            "x86_64": "win-amd64",
            "arm64": "win-arm64",
        }[host_machine], (
            "release build interpreter must match the native host architecture"
        )
    else:
        assert platform_tag.startswith("macosx-")
        assert platform_tag.endswith(
            f"-{host_machine}"
        ) or platform_tag.endswith("-universal2"), (
            "release build interpreter must match the native host architecture"
        )

    selected_lock = lock_path or REQUIREMENTS_DIR / "build.lock"
    expected_versions = {
        name: str(contract[0])
        for name, contract in _locked_requirements(
            selected_lock
        ).items()
    }
    assert actual.get("duplicates") == [], (
        f"{environment_label} environment contains duplicate canonical "
        "distributions"
    )
    assert actual.get("versions") == expected_versions, (
        f"{environment_label} environment must exactly match {selected_lock}"
    )

    reported_executable = actual.get("executable")
    same_interpreter_error = (
        "build toolchain probe must report the selected interpreter"
    )
    try:
        reported_path = Path(reported_executable)
        assert reported_path.is_absolute(), same_interpreter_error
        assert os.path.samefile(
            validated_build_python, reported_path
        ), same_interpreter_error
    except (OSError, TypeError, ValueError):
        raise AssertionError(same_interpreter_error) from None
    revalidated_build_python = _validated_lock_python(
        validated_build_python
    )
    assert os.path.samefile(
        validated_build_python, revalidated_build_python
    ), same_interpreter_error
    return revalidated_build_python


def _verified_release_toolchain(
    release_python,
    *,
    environment_updates,
    cwd,
):
    return _verified_build_toolchain(
        release_python,
        environment_updates=environment_updates,
        cwd=cwd,
        lock_path=REQUIREMENTS_DIR / "release.lock",
        environment_label="release",
    )


def _commit_source_date_epoch(reported):
    assert isinstance(reported, str)
    assert re.fullmatch(r"[1-9][0-9]*\n", reported), (
        "commit timestamp must be one canonical decimal SOURCE_DATE_EPOCH line"
    )
    return int(reported[:-1])


def _run_release_git(
    *arguments,
    environment_updates,
    cwd,
    timeout=30,
):
    # Every caller runs `git -C ROOT ...`, i.e. asserts on REPOSITORY state. The source also ships as
    # an sdist and a portable zip, where git exits 128 ("not a git repository") and these tests fail
    # for a reason unrelated to the contract they protect. Gate once here rather than in each caller.
    # The probe runs at IMPORT time, not here: test_phase_b_release_git_strips_every_ambient_git_variable
    # patches subprocess.run and asserts this helper makes exactly ONE call, so probing inside it
    # changes the very behaviour that test measures.
    if not _ROOT_IS_GIT_CHECKOUT:
        pytest.skip("not a git checkout — repository-state assertions do not apply here")
    environment = _sanitized_python_env(environment_updates)
    for name in tuple(environment):
        if name.upper().startswith("GIT_"):
            environment.pop(name, None)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return subprocess.run(
        ["git", "-C", str(ROOT), *map(str, arguments)],
        env=environment,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _release_head_identity(*, environment_updates, cwd):
    resolved = _run_release_git(
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        environment_updates=environment_updates,
        cwd=cwd,
    )
    assert resolved.returncode == 0, resolved.stdout + resolved.stderr
    assert re.fullmatch(r"[0-9a-f]{40}\n", resolved.stdout), (
        "git must resolve HEAD to one immutable full commit id"
    )
    commit = resolved.stdout[:-1]

    timestamp = _run_release_git(
        "show",
        "--no-patch",
        "--format=%ct",
        commit,
        environment_updates=environment_updates,
        cwd=cwd,
    )
    assert timestamp.returncode == 0, timestamp.stdout + timestamp.stderr
    return commit, _commit_source_date_epoch(timestamp.stdout)


def _validated_tar_member_name(member):
    name = member.name
    assert isinstance(name, str) and name
    assert "\x00" not in name and "\\" not in name
    if member.isdir() and name.endswith("/"):
        name = name[:-1]
    assert name and not name.startswith("/")
    assert "//" not in name and not name.endswith("/")
    parts = name.split("/")
    assert all(part not in {"", ".", ".."} for part in parts)
    assert all(":" not in part for part in parts)
    canonical = PurePosixPath(*parts).as_posix()
    assert canonical == name
    normalized = unicodedata.normalize("NFC", canonical).casefold()
    return canonical, normalized


def _safe_extract_release_tar(
    archive_path,
    destination,
    *,
    require_single_root=False,
):
    archive_path = Path(archive_path)
    destination = Path(destination)
    assert archive_path.is_file()
    assert not destination.exists()

    reviewed = []
    normalized_names = set()
    roots = set()
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            assert member.isfile() or member.isdir(), (
                "release archives may contain only regular files and "
                "directories"
            )
            canonical, normalized = _validated_tar_member_name(member)
            assert normalized not in normalized_names, (
                f"duplicate normalized release archive member: {canonical}"
            )
            normalized_names.add(normalized)
            roots.add(canonical.split("/", 1)[0])
            reviewed.append((member, canonical))

        assert reviewed, "release archive is empty"
        if require_single_root:
            assert len(roots) == 1, (
                "sdist must contain exactly one top-level source directory"
            )

        destination.mkdir(parents=True)
        extracted_directories = []
        for member, canonical in reviewed:
            target = destination.joinpath(*canonical.split("/"))
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                extracted_directories.append((target, member.mtime))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            assert not target.exists()
            source = archive.extractfile(member)
            assert source is not None
            body = source.read()
            assert len(body) == member.size
            with target.open("xb") as stream:
                stream.write(body)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            os.utime(target, (member.mtime, member.mtime))
        for target, modified in reversed(extracted_directories):
            os.utime(target, (modified, modified))

    if require_single_root:
        return destination / next(iter(roots))
    return destination


def _archive_clean_head_source(
    scratch,
    label,
    commit,
    *,
    environment_updates,
    cwd,
):
    scratch = Path(scratch)
    archive_path = scratch / f"{label}.tar"
    source = scratch / label
    assert not archive_path.exists() and not source.exists()
    archived = _run_release_git(
        "archive",
        "--format=tar",
        f"--output={archive_path}",
        commit,
        environment_updates=environment_updates,
        cwd=cwd,
        timeout=120,
    )
    assert archived.returncode == 0, archived.stdout + archived.stderr
    assert archive_path.is_file() and archive_path.stat().st_size > 0
    _safe_extract_release_tar(archive_path, source)
    assert (source / "pyproject.toml").is_file()
    assert not (source / ".git").exists()
    return source


def _build_python_distributions(
    build_python,
    source,
    artifact_dir,
    epoch,
    *,
    environment_updates,
    cwd,
):
    source = Path(source)
    artifact_dir = Path(artifact_dir)
    assert source.is_dir()
    assert not artifact_dir.exists()
    build = _run_validated_lock_python_module(
        build_python,
        "build",
        "--no-isolation",
        "--wheel",
        "--sdist",
        "--outdir",
        str(artifact_dir),
        str(source),
        environment_updates={
            **environment_updates,
            "PIP_NO_INDEX": "1",
            "SOURCE_DATE_EPOCH": str(epoch),
        },
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    expected = {
        "yt_final-1.0.0-py3-none-any.whl",
        "yt_final-1.0.0.tar.gz",
    }
    actual = {
        path.name
        for path in artifact_dir.iterdir()
        if path.is_file()
    }
    assert actual == expected
    assert all(path.is_file() for path in artifact_dir.iterdir())
    return (
        artifact_dir / "yt_final-1.0.0-py3-none-any.whl",
        artifact_dir / "yt_final-1.0.0.tar.gz",
    )


def _twine_check_release(
    release_python,
    wheel,
    sdist,
    *,
    environment_updates=None,
):
    options = {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 120,
    }
    if environment_updates is not None:
        working_directory = Path(environment_updates["HOME"])
        assert working_directory.is_absolute() and working_directory.is_dir()
        assert not _path_is_within_or_equal(working_directory, ROOT)
        options.update(
            {
                "environment_updates": environment_updates,
                "cwd": working_directory,
            }
        )
    checked = _run_validated_lock_python_module(
        release_python,
        "twine",
        "check",
        str(wheel),
        str(sdist),
        **options,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def _run_outer_pip(
    release_python,
    target_python,
    *arguments,
    environment_updates,
    timeout,
):
    cache_dir = Path(environment_updates["PIP_CACHE_DIR"])
    working_directory = Path(environment_updates["HOME"])
    assert cache_dir.is_absolute() and cache_dir.is_dir()
    assert working_directory.is_absolute() and working_directory.is_dir()
    assert not _path_is_within_or_equal(cache_dir, ROOT)
    assert not _path_is_within_or_equal(working_directory, ROOT)
    result = _run_validated_lock_python_module(
        release_python,
        "pip",
        "--cache-dir",
        str(cache_dir),
        "--python",
        str(target_python),
        *arguments,
        environment_updates=environment_updates,
        cwd=working_directory,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _release_state_environment(state_root, pip_cache):
    state_root = Path(state_root)
    pip_cache = Path(pip_cache)
    assert state_root.is_absolute() and pip_cache.is_absolute()
    assert not _path_is_within_or_equal(state_root, ROOT)
    assert not _path_is_within_or_equal(pip_cache, ROOT)
    paths = {
        "APPDATA": state_root / "appdata",
        "HOME": state_root / "home",
        "LOCALAPPDATA": state_root / "local-appdata",
        "TEMP": state_root / "temp",
        "TMP": state_root / "temp",
        "TMPDIR": state_root / "temp",
        "USERPROFILE": state_root / "home",
        "XDG_CACHE_HOME": state_root / "xdg-cache",
        "XDG_CONFIG_HOME": state_root / "xdg-config",
        "XDG_DATA_HOME": state_root / "xdg-data",
        "XDG_RUNTIME_DIR": state_root / "xdg-runtime",
        "XDG_STATE_HOME": state_root / "xdg-state",
    }
    for path in set(paths.values()) | {pip_cache}:
        path.mkdir(parents=True, exist_ok=True)
    return {
        **{name: str(path) for name, path in paths.items()},
        "PIP_CACHE_DIR": str(pip_cache),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _controller_evidence_context(scratch, cache_path):
    assert sys.flags.dont_write_bytecode and sys.dont_write_bytecode, (
        "release evidence controller must disable bytecode writes"
    )
    parent_state_names = {
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
    }
    for name, raw_value in os.environ.items():
        upper_name = name.upper()
        if (
            upper_name not in parent_state_names
            and not upper_name.startswith("XDG_")
        ):
            continue
        value = Path(raw_value)
        assert value.is_absolute(), (
            f"parent state {name} must be an absolute external path"
        )
        assert not _path_is_within_or_equal(value, ROOT), (
            f"parent state {name} must be outside the repository"
        )
    controller_python = Path(sys.executable)
    assert controller_python.is_absolute() and controller_python.is_file(), (
        "controller interpreter must be an absolute existing file"
    )
    assert not _path_is_within_or_equal(controller_python, ROOT), (
        "controller interpreter must be outside the repository"
    )

    scratch = Path(scratch)
    assert scratch.is_absolute() and scratch.is_dir(), (
        "release evidence scratch must be an absolute existing directory"
    )
    scratch = scratch.resolve(strict=True)
    assert not _path_is_within_or_equal(scratch, ROOT), (
        "release evidence scratch must be outside the repository"
    )

    temporary_root = Path(tempfile.gettempdir())
    assert temporary_root.is_absolute() and temporary_root.is_dir(), (
        "effective temporary root must be an absolute existing directory"
    )
    assert not _path_is_within_or_equal(temporary_root, ROOT), (
        "effective temporary root must be outside the repository"
    )
    _assert_external_controller_cache(cache_path)

    pip_cache = scratch / "controller-pip-cache"
    environment = _release_state_environment(
        scratch / "controller-state",
        pip_cache,
    )
    cwd = Path(environment["HOME"])
    for name in (
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    ):
        value = Path(environment[name])
        assert value.is_absolute() and value.is_dir()
        assert not _path_is_within_or_equal(value, ROOT)
    return scratch, environment, cwd


def _environment_python(environment_root):
    environment_root = Path(environment_root)
    if os.name == "nt":
        return environment_root / "Scripts" / "python.exe"
    return environment_root / "bin" / "python"


def _create_pipless_environment(
    release_python,
    environment_root,
    *,
    environment_updates,
    cwd,
):
    environment_root = Path(environment_root)
    assert not environment_root.exists()
    created = _run_validated_lock_python_module(
        release_python,
        "venv",
        "--without-pip",
        "--copies",
        str(environment_root),
        environment_updates=environment_updates,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    environment_python = _environment_python(environment_root)
    assert environment_python.is_file()
    return environment_python


def test_phase_b_pipless_environment_copies_its_interpreter(
        monkeypatch, tmp_path):
    """The later trust check rejects POSIX venv symlinks, so creation must copy."""
    environment_root = tmp_path / "pipless"
    captured = []

    def fake_run(release_python, module, *arguments, **options):
        captured.append((release_python, module, arguments, options))
        environment_python = _environment_python(environment_root)
        environment_python.parent.mkdir(parents=True)
        environment_python.write_bytes(b"reviewed interpreter")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(
        sys.modules[__name__],
        "_run_validated_lock_python_module",
        fake_run,
    )

    actual = _create_pipless_environment(
        "C:/reviewed/release-python.exe",
        environment_root,
        environment_updates={"HOME": str(tmp_path)},
        cwd=tmp_path,
    )

    assert actual == _environment_python(environment_root)
    assert captured[0][0:3] == (
        "C:/reviewed/release-python.exe",
        "venv",
        ("--without-pip", "--copies", str(environment_root)),
    )


def _install_locked_runtime_environment(
    release_python,
    environment_root,
    runtime_lock,
    wheel,
    state_root,
    pip_cache,
):
    environment_updates = _release_state_environment(
        state_root,
        pip_cache,
    )
    environment_python = _create_pipless_environment(
        release_python,
        environment_root,
        environment_updates=environment_updates,
        cwd=Path(environment_updates["HOME"]),
    )
    _run_outer_pip(
        release_python,
        environment_python,
        "install",
        "--require-hashes",
        "--only-binary=:all:",
        "-r",
        str(runtime_lock),
        environment_updates=environment_updates,
        timeout=600,
    )
    _run_outer_pip(
        release_python,
        environment_python,
        "install",
        "--no-index",
        "--no-deps",
        str(wheel),
        environment_updates=environment_updates,
        timeout=120,
    )
    _run_outer_pip(
        release_python,
        environment_python,
        "check",
        environment_updates=environment_updates,
        timeout=120,
    )
    return environment_python, environment_updates


def _path_is_within(candidate, parent):
    try:
        Path(candidate).resolve(strict=True).relative_to(
            Path(parent).resolve(strict=True)
        )
    except ValueError:
        return False
    return True


def _path_is_within_or_equal(candidate, parent):
    candidate = Path(candidate).resolve(strict=False)
    parent = Path(parent).resolve(strict=False)
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _workspace_relative_path(relative):
    assert isinstance(relative, str) and relative
    assert "\x00" not in relative and "\\" not in relative
    path = PurePosixPath(relative)
    assert not path.is_absolute()
    assert all(part not in {"", ".", ".."} for part in path.parts)
    assert path.as_posix() == relative
    return ROOT.joinpath(*path.parts)


def _workspace_path_evidence(path):
    path = Path(path)
    before = path.lstat()
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        getattr(before, "st_mtime_ns", None),
        stat.S_IFMT(before.st_mode),
    )
    evidence = {
        "identity": identity,
        "kind": "other",
        "sha256": None,
    }
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        stat.S_ISLNK(before.st_mode)
        or bool(
            reparse_point
            and getattr(before, "st_file_attributes", 0) & reparse_point
        )
        or (
            getattr(path, "is_junction", None) and path.is_junction()
        )
    ):
        evidence["kind"] = "link"
        evidence["sha256"] = hashlib.sha256(
            os.readlink(path).encode("utf-8", "surrogatepass")
        ).hexdigest()
    elif stat.S_ISDIR(before.st_mode):
        evidence["kind"] = "directory"
    elif stat.S_ISREG(before.st_mode):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = path.lstat()
        assert (
            after.st_dev,
            after.st_ino,
            after.st_size,
            getattr(after, "st_mtime_ns", None),
            stat.S_IFMT(after.st_mode),
        ) == identity, f"workspace path changed during audit: {path}"
        evidence["kind"] = "file"
        evidence["sha256"] = digest.hexdigest()
    return evidence


def _nul_separated_git_paths(
    *arguments,
    environment_updates,
    cwd,
):
    result = _run_release_git(
        *arguments,
        "-z",
        environment_updates=environment_updates,
        cwd=cwd,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if not result.stdout:
        return ()
    assert result.stdout.endswith("\x00")
    paths = tuple(result.stdout[:-1].split("\x00"))
    assert all(paths)
    return paths


def _workspace_write_snapshot(*, environment_updates, cwd):
    status = _run_release_git(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "-z",
        environment_updates=environment_updates,
        cwd=cwd,
        timeout=120,
    )
    assert status.returncode == 0, status.stdout + status.stderr

    reviewed_paths = _nul_separated_git_paths(
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        environment_updates=environment_updates,
        cwd=cwd,
    )
    assert len(reviewed_paths) == len(set(reviewed_paths))
    reviewed = {}
    reviewed_directories = set()
    for relative in reviewed_paths:
        path = _workspace_relative_path(relative)
        reviewed[relative] = (
            _workspace_path_evidence(path)
            if path.exists() or path.is_symlink()
            else None
        )
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            reviewed_directories.add(parent.as_posix())
            parent = parent.parent

    ignored_paths = _nul_separated_git_paths(
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        environment_updates=environment_updates,
        cwd=cwd,
    )
    ignored = {}
    ignored_directories = set()
    for relative in ignored_paths:
        path = _workspace_relative_path(relative)
        assert path.exists() or path.is_symlink(), (
            f"ignored path vanished during workspace audit: {relative}"
        )
        ignored[relative] = _workspace_path_evidence(path)
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            ignored_directories.add(parent.as_posix())
            parent = parent.parent

    directory_evidence = {}
    for relative in sorted(ignored_directories | reviewed_directories):
        path = _workspace_relative_path(relative)
        if path.is_dir():
            directory_evidence[relative] = _workspace_path_evidence(path)

    top_level = {}
    for path in sorted(ROOT.iterdir(), key=lambda item: item.name.casefold()):
        if path.name == ".git":
            continue
        top_level[path.name] = _workspace_path_evidence(path)

    output_roots = {}
    for relative in (
        ".pytest_cache",
        "__pycache__",
        "build",
        "cache",
        "dist",
        "pip",
        "yt_final.egg-info",
    ):
        path = ROOT / relative
        output_roots[relative] = (
            _workspace_path_evidence(path)
            if path.exists() or path.is_symlink()
            else None
        )
    return {
        "directories": directory_evidence,
        "ignored": ignored,
        "output_roots": output_roots,
        "reviewed": reviewed,
        "status": status.stdout,
        "top_level": top_level,
    }


def _assert_external_controller_cache(cache_path):
    if cache_path is None:
        return
    cache_path = Path(cache_path)
    assert cache_path.is_absolute(), "pytest cache must be an absolute path"
    assert not _path_is_within_or_equal(cache_path, ROOT), (
        "pytest cache must be outside the repository"
    )


def _assert_external_runtime(
    release_python,
    environment_python,
    environment_root,
    source,
    unrelated_cwd,
    environment_updates,
):
    unrelated_cwd = Path(unrelated_cwd)
    unrelated_cwd.mkdir(parents=True, exist_ok=True)
    probe = _run_isolated_python(
        environment_python,
        "-c",
        (
            "import json, os, sys\n"
            "from importlib.metadata import distributions\n"
            "from packaging.utils import canonicalize_name\n"
            "import Final\n"
            "versions = {}\n"
            "duplicates = []\n"
            "for distribution in distributions():\n"
            "    name = canonicalize_name(distribution.metadata['Name'])\n"
            "    if name in versions:\n"
            "        duplicates.append(name)\n"
            "    versions[name] = distribution.version\n"
            "print(json.dumps({"
            "'cwd': os.getcwd(), "
            "'duplicates': sorted(duplicates), "
            "'final_file': os.path.realpath(Final.__file__), "
            "'prefix': os.path.realpath(sys.prefix), "
            "'versions': versions"
            "}))"
        ),
        environment_updates=environment_updates,
        cwd=unrelated_cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    actual = json.loads(probe.stdout)
    expected_versions = {
        name: str(contract[0])
        for name, contract in _locked_requirements(
            Path(source) / "Final" / "approved-python.lock"
        ).items()
    }
    expected_versions["yt-final"] = "1.0.0"
    assert actual["duplicates"] == []
    assert actual["versions"] == expected_versions
    assert Path(actual["cwd"]).resolve() == unrelated_cwd.resolve()
    assert _path_is_within(actual["final_file"], environment_root)
    assert not _path_is_within(actual["final_file"], source)
    assert _path_is_within(actual["prefix"], environment_root)

    console = (
        Path(environment_root) / "Scripts" / "yt-final.exe"
        if os.name == "nt"
        else Path(environment_root) / "bin" / "yt-final"
    )
    assert console.is_file()
    for arguments, expected in (
        (("--help",), None),
        (("--version",), "1.0.0"),
        (("list-profiles",), None),
    ):
        result = subprocess.run(
            [str(console), *arguments],
            env={**_sanitized_python_env(), **environment_updates},
            cwd=unrelated_cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if expected is not None:
            assert result.stdout.strip() == expected

    module_help = _run_isolated_python_module(
        environment_python,
        "Final",
        "--help",
        environment_updates=environment_updates,
        cwd=unrelated_cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert module_help.returncode == 0, (
        module_help.stdout + module_help.stderr
    )


_RELEASE_API_PROGRAM = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "source = Path(sys.argv[1])\n"
    "artifacts = Path(sys.argv[2])\n"
    "epoch = int(sys.argv[3])\n"
    "wheel = Path(sys.argv[4])\n"
    "sdist = Path(sys.argv[5])\n"
    "sys.path.insert(0, str(source))\n"
    "from scripts.build_portable import build_portable\n"
    "from scripts.write_checksums import write_sha256sums\n"
    "portable = build_portable(source, artifacts, epoch)\n"
    "manifest = write_sha256sums(\n"
    "    [wheel, sdist, portable], artifacts / 'SHA256SUMS'\n"
    ")\n"
    "print(json.dumps({\n"
    "    'manifest': manifest.name,\n"
    "    'portable': portable.name,\n"
    "}, sort_keys=True))\n"
)


def _build_portable_and_checksums(
    build_python,
    source,
    artifact_dir,
    epoch,
    wheel,
    sdist,
    *,
    environment_updates,
    cwd,
):
    built = _run_validated_lock_python(
        build_python,
        "-c",
        _RELEASE_API_PROGRAM,
        str(source),
        str(artifact_dir),
        str(epoch),
        str(wheel),
        str(sdist),
        environment_updates={
            **environment_updates,
            "PIP_NO_INDEX": "1",
            "SOURCE_DATE_EPOCH": str(epoch),
        },
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    result = json.loads(built.stdout)
    assert result == {
        "manifest": "SHA256SUMS",
        "portable": "yt_final-1.0.0-portable.zip",
    }
    portable = Path(artifact_dir) / result["portable"]
    manifest = Path(artifact_dir) / result["manifest"]
    expected_names = {
        "SHA256SUMS",
        "yt_final-1.0.0-portable.zip",
        "yt_final-1.0.0-py3-none-any.whl",
        "yt_final-1.0.0.tar.gz",
    }
    assert {
        path.name
        for path in Path(artifact_dir).iterdir()
        if path.is_file()
    } == expected_names
    assert all(path.is_file() for path in Path(artifact_dir).iterdir())
    return portable, manifest


def _assert_checksum_manifest(manifest, artifacts):
    artifacts = sorted(map(Path, artifacts), key=lambda path: path.name)
    expected = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in artifacts
    ).encode("ascii")
    actual = Path(manifest).read_bytes()
    assert actual == expected
    assert b"\r" not in actual
    assert len(actual.splitlines()) == 3


def _artifact_hashes(artifacts):
    return {
        Path(path).name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in artifacts
    }


def _reviewed_generated_sdist_members(source):
    source = Path(source)
    egg_info_members = (
        SDIST_GENERATED_MEMBERS - {"PKG-INFO", "setup.cfg"}
    )
    assert all(
        relative.startswith("yt_final.egg-info/")
        for relative in egg_info_members
    )
    generated = {}
    for relative in egg_info_members:
        path = source.joinpath(*PurePosixPath(relative).parts)
        assert path.is_file(), f"missing generated build output: {path}"
        generated[relative] = path.read_bytes()
    generated["PKG-INFO"] = generated["yt_final.egg-info/PKG-INFO"]
    newline = b"\r\n" if os.name == "nt" else b"\n"
    generated["setup.cfg"] = (
        b"[egg_info]"
        + newline
        + b"tag_build = "
        + newline
        + b"tag_date = 0"
        + newline
        + newline
    )
    assert set(generated) == SDIST_GENERATED_MEMBERS
    return generated


def _reviewed_sdist_source_members(source, source_members=None):
    source = Path(source)
    source_members = set(
        source_members
        if source_members is not None
        else SDIST_STATIC_SOURCE_MEMBERS | SDIST_FINAL_PYTHON_MEMBERS
    )
    reviewed = {}
    for relative in source_members:
        pure = PurePosixPath(relative)
        assert (
            pure.as_posix() == relative
            and not pure.is_absolute()
            and all(part not in {"", ".", ".."} for part in pure.parts)
        )
        path = source.joinpath(*pure.parts)
        metadata = path.lstat()
        assert stat.S_ISREG(metadata.st_mode), (
            f"sdist source member is not a regular file: {relative}"
        )
        reviewed[relative] = path.read_bytes()
    assert set(reviewed) == source_members
    return reviewed


def _assert_reproducible_sdist(
    sdist,
    source,
    epoch,
    *,
    source_member_bytes,
    generated_members=None,
):
    source = Path(source)
    source_member_bytes = dict(source_member_bytes)
    generated_members = dict(
        generated_members
        if generated_members is not None
        else _reviewed_generated_sdist_members(source)
    )
    assert set(source_member_bytes).isdisjoint(generated_members)
    expected_bodies = {}
    for relative, body in source_member_bytes.items():
        pure = PurePosixPath(relative)
        assert (
            pure.as_posix() == relative
            and not pure.is_absolute()
            and all(part not in {"", ".", ".."} for part in pure.parts)
        )
        assert isinstance(body, bytes)
        expected_bodies[relative] = body
    for relative, body in generated_members.items():
        pure = PurePosixPath(relative)
        assert (
            pure.as_posix() == relative
            and not pure.is_absolute()
            and all(part not in {"", ".", ".."} for part in pure.parts)
        )
        assert isinstance(body, bytes)
        expected_bodies[relative] = body

    archive_root = "yt_final-1.0.0"
    expected_names = {archive_root}
    expected_file_names = set()
    for relative in expected_bodies:
        pure = PurePosixPath(relative)
        expected_file_names.add(f"{archive_root}/{pure.as_posix()}")
        for parent in pure.parents:
            if parent == PurePosixPath("."):
                continue
            expected_names.add(f"{archive_root}/{parent.as_posix()}")
    expected_names.update(expected_file_names)
    expected_sequence = sorted(expected_names)

    raw = Path(sdist).read_bytes()
    assert raw[:4] == b"\x1f\x8b\x08\x00"
    assert struct.unpack("<I", raw[4:8])[0] == epoch
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        actual_sequence = [member.name for member in members]
        assert actual_sequence == expected_sequence, (
            "sdist member order/content boundary mismatch: "
            f"expected={expected_sequence!r}, actual={actual_sequence!r}"
        )
        for member in members:
            canonical, _normalized = _validated_tar_member_name(member)
            assert canonical == member.name
            assert member.pax_headers == {}, (
                f"sdist PAX headers are forbidden: {member.name}"
            )
            assert member.mtime == epoch
            assert member.uid == 0 and member.gid == 0
            assert member.uname == "" and member.gname == ""
            relative = member.name.removeprefix(f"{archive_root}/")
            expected_mode = (
                0o755
                if member.isdir()
                or relative == "Final/Start yt_final.command"
                else 0o644
            )
            assert member.mode == expected_mode, (
                f"noncanonical sdist mode for {member.name}: "
                f"{oct(member.mode)}"
            )
            if member.name in expected_file_names:
                assert member.isfile()
                body = archive.extractfile(member)
                assert body is not None
                actual_body = body.read()
                assert member.size == len(actual_body)
                assert actual_body == expected_bodies[relative], (
                    f"sdist content mismatch: {member.name}"
                )
            else:
                assert member.isdir()


def _create_locked_build_environment(
    release_python,
    environment_root,
    build_lock,
    state_root,
    pip_cache,
):
    environment_updates = _release_state_environment(
        state_root,
        pip_cache,
    )
    environment_python = _create_pipless_environment(
        release_python,
        environment_root,
        environment_updates=environment_updates,
        cwd=Path(environment_updates["HOME"]),
    )
    _run_outer_pip(
        release_python,
        environment_python,
        "install",
        "--require-hashes",
        "--only-binary=:all:",
        "-r",
        str(build_lock),
        environment_updates=environment_updates,
        timeout=600,
    )
    return _verified_build_toolchain(
        environment_python,
        environment_updates=environment_updates,
        cwd=Path(environment_updates["HOME"]),
    )


def _rebuild_wheel_from_sdist(
    build_python,
    source,
    artifact_dir,
    epoch,
    *,
    environment_updates,
    cwd,
):
    artifact_dir = Path(artifact_dir)
    assert not artifact_dir.exists()
    build = _run_validated_lock_python_module(
        build_python,
        "build",
        "--no-isolation",
        "--wheel",
        "--outdir",
        str(artifact_dir),
        str(source),
        environment_updates={
            **environment_updates,
            "PIP_NO_INDEX": "1",
            "SOURCE_DATE_EPOCH": str(epoch),
        },
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    artifacts = list(artifact_dir.iterdir())
    assert [path.name for path in artifacts] == [
        "yt_final-1.0.0-py3-none-any.whl"
    ]
    assert artifacts[0].is_file()
    return artifacts[0]


def _verified_native_target(lock_python):
    target_name = os.environ.get("YT_FINAL_NATIVE_TARGET")
    assert target_name, "YT_FINAL_NATIVE_TARGET is required"
    assert target_name in NATIVE_TARGETS, (
        f"unsupported YT_FINAL_NATIVE_TARGET: {target_name!r}"
    )

    expected_platform, expected_machine, expected_python = NATIVE_TARGETS[
        target_name
    ]
    validated_lock_python, actual = _verified_lock_toolchain(
        lock_python, expected_python=expected_python
    )
    normalized_machine = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(str(actual["machine"]).lower(), str(actual["machine"]).lower())
    assert actual["platform"] == expected_platform, (
        f"native lock evidence requires operating system {expected_platform}"
    )
    assert normalized_machine == expected_machine, (
        "native lock evidence requires the exact process architecture "
        f"{expected_machine}"
    )
    platform_tag = str(actual.get("platform_tag", "")).lower()
    if expected_platform == "win32":
        expected_platform_tag = {
            "x86_64": "win-amd64",
            "arm64": "win-arm64",
        }[expected_machine]
        assert platform_tag == expected_platform_tag, (
            "native lock evidence requires the exact process architecture "
            f"{expected_machine}"
        )
    else:
        assert platform_tag.startswith("macosx-"), (
            "native lock evidence requires the exact macOS process platform"
        )
        assert platform_tag.endswith(
            f"-{expected_machine}"
        ) or platform_tag.endswith("-universal2"), (
            "native lock evidence requires the exact process architecture "
            f"{expected_machine}"
        )
    return validated_lock_python, actual


@pytest.fixture(scope="session")
def release_distributions(tmp_path_factory):
    requested_build_python = os.environ.get("YT_FINAL_BUILD_PYTHON")
    if not requested_build_python:
        pytest.skip(
            "set YT_FINAL_BUILD_PYTHON to the canonical absolute interpreter "
            "of an environment installed from requirements/build.lock"
        )
    scratch = tmp_path_factory.mktemp("release-distributions")
    scratch, environment, cwd = _controller_evidence_context(scratch, None)
    build_python = _verified_build_toolchain(
        requested_build_python,
        environment_updates=environment,
        cwd=cwd,
    )
    source = scratch / "source"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".worktrees",
            ".superpowers",
            ".venv",
            ".artifacts",
            "__pycache__",
            ".pytest_cache",
            "pytest-of-*",
            "build",
            "dist",
            "node_modules",
            "*.egg-info",
            "downloads",
            "test_out",
            "*.log",
        ),
    )
    poison_files = {
        ".claude/settings.local.json": "{}",
        ".github/workflows/poison.yml": "poison",
        ".superpowers/internal.md": "poison",
        ".worktrees/internal.txt": "poison",
        "archive/legacy-tools/poison.py": "raise RuntimeError",
        "build/poison.txt": "poison",
        "dist/poison.txt": "poison",
        "downloads/poison.txt": "poison",
        "Final/docs/superpowers/internal.md": "poison",
        "Final/downloads/poison.txt": "poison",
        "Final/final_yt.log": "secret",
        "Final/__pycache__/poison.pyc": "poison",
        "mcps/poison.py": "raise RuntimeError",
        "test_out/poison.txt": "poison",
    }
    for relative, body in poison_files.items():
        poison = source / relative
        poison.parent.mkdir(parents=True, exist_ok=True)
        poison.write_text(body, encoding="utf-8")

    artifact_dir = scratch / "artifacts"
    artifact_dir.mkdir()
    build = _run_validated_lock_python_module(
        build_python,
        "build",
        "--no-isolation",
        "--wheel",
        "--sdist",
        "--outdir",
        str(artifact_dir),
        str(source),
        environment_updates={**environment, "PIP_NO_INDEX": "1"},
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(artifact_dir.glob("*.whl"))
    sdists = list(artifact_dir.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    return {
        "build_output": build.stdout + build.stderr,
        "build_python": build_python,
        "scratch": scratch,
        "sdist": sdists[0],
        "source": source,
        "wheel": wheels[0],
    }


@pytest.fixture(scope="session")
def installed_wheel(release_distributions):
    scratch = release_distributions["scratch"]
    build_python = release_distributions["build_python"]

    target = scratch / "installed"
    install = _run_validated_lock_python_module(
        build_python,
        "pip",
        "install",
        "--no-deps",
        "--no-index",
        "--target",
        str(target),
        str(release_distributions["wheel"]),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    return target


def test_release_metadata_has_one_approved_identity():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "yt-final"
    assert data["project"]["requires-python"] == ">=3.11"
    assert data["project"]["dynamic"] == ["version", "dependencies"]
    assert data["project"]["scripts"]["yt-final"] == "Final.cli:main"
    assert data["tool"]["setuptools"]["dynamic"]["version"]["attr"] == (
        "Final._version.__version__"
    )


def test_release_metadata_uses_pep639_and_explicit_package_data():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["license"] == "Unlicense"
    assert data["project"]["license-files"] == ["LICENSE"]
    assert data["tool"]["setuptools"]["include-package-data"] is False


def test_release_build_has_no_license_or_package_configuration_warnings(
    release_distributions,
):
    output = release_distributions["build_output"]
    forbidden = {
        "deprecated project.license table": (
            "`project.license` as a TOML table is deprecated"
        ),
        **{
            package: (
                f"Package '{package}' is absent from the "
                "`packages` configuration."
            )
            for package in ("Final.assets", "Final.examples", "Final.webui")
        },
    }

    assert [
        label for label, warning in forbidden.items() if warning in output
    ] == []


def test_phase_c_quality_configuration_is_exact_and_fail_closed():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["pytest"]["ini_options"] == {
        "testpaths": ["Final"],
        "pythonpath": ["Final"],
        "strict_config": True,
        "strict_markers": True,
        "addopts": "--tb=short",
    }
    assert data["tool"]["ruff"] == {
        "target-version": "py311",
        "respect-gitignore": True,
        "exclude": [
            ".artifacts",
            ".git",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            ".worktrees",
            "__pycache__",
            "archive",
            "build",
            "dist",
            "downloads",
            "test_out",
            "test_outputs",
            "*.egg-info",
        ],
        "lint": {
            "select": ["E9", "F63", "F7", "F82"],
        },
    }


def _run_project_pytest(project, *arguments):
    return _run_isolated_python_module(
        sys.executable,
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *arguments,
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_phase_c_pytest_scopes_root_collection_and_pythonpath(tmp_path):
    project = tmp_path / "project"
    final = project / "Final"
    final.mkdir(parents=True)
    shutil.copy2(ROOT / "pyproject.toml", project / "pyproject.toml")
    (final / "task6_import_probe.py").write_text(
        "VALUE = 'reviewed'\n",
        encoding="utf-8",
        newline="\n",
    )
    (final / "test_task6_probe.py").write_text(
        "from task6_import_probe import VALUE\n\n"
        "def test_pythonpath_contract():\n"
        "    assert VALUE == 'reviewed'\n",
        encoding="utf-8",
        newline="\n",
    )
    (project / "test_outside_final.py").write_text(
        "raise AssertionError('testpaths escaped Final')\n",
        encoding="utf-8",
        newline="\n",
    )

    collected = _run_project_pytest(project, "--collect-only")

    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert "Final/test_task6_probe.py::test_pythonpath_contract" in (
        collected.stdout
    )
    assert "test_outside_final" not in collected.stdout + collected.stderr


def test_phase_c_pytest_strict_markers_reject_unknown_marker(tmp_path):
    project = tmp_path / "project"
    final = project / "Final"
    final.mkdir(parents=True)
    shutil.copy2(ROOT / "pyproject.toml", project / "pyproject.toml")
    (final / "test_task6_marker.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.task6_unknown\n"
        "def test_unknown_marker():\n"
        "    pass\n",
        encoding="utf-8",
        newline="\n",
    )

    collected = _run_project_pytest(project, "--collect-only")

    assert collected.returncode != 0
    assert "task6_unknown" in collected.stdout + collected.stderr


def test_phase_c_pytest_strict_config_rejects_unknown_option(tmp_path):
    project = tmp_path / "project"
    final = project / "Final"
    final.mkdir(parents=True)
    body = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    header = "[tool.pytest.ini_options]\n"
    assert header in body
    (project / "pyproject.toml").write_text(
        body.replace(
            header,
            f"{header}task6_unknown = true\n",
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    (final / "test_task6_config.py").write_text(
        "def test_config_probe():\n"
        "    pass\n",
        encoding="utf-8",
        newline="\n",
    )

    collected = _run_project_pytest(project, "--collect-only")

    assert collected.returncode != 0
    assert "task6_unknown" in collected.stdout + collected.stderr


def test_version_is_exactly_1_0_0():
    from Final import __version__
    assert __version__ == "1.0.0"


def test_console_wrapper_preserves_top_level_import(monkeypatch):
    called = []
    fake = type("FakeMain", (), {"main": staticmethod(lambda: called.append(True))})
    monkeypatch.setitem(sys.modules, "yt_final", fake)
    cli = importlib.import_module("Final.cli")
    assert cli.main() is None
    assert called == [True]
    assert str(Path(cli.__file__).resolve().parent) == sys.path[0]


def test_installed_wheel_excludes_tests_but_keeps_runtime(installed_wheel):
    installed = {
        path.relative_to(installed_wheel).as_posix()
        for path in installed_wheel.rglob("*")
        if path.is_file()
    }
    packaged_tests = sorted(
        path
        for path in installed
        if path.startswith("Final/test_") and path.endswith(".py")
    )
    assert packaged_tests == []
    assert {
        "Final/__init__.py",
        "Final/__main__.py",
        "Final/_version.py",
        "Final/cli.py",
        "Final/watch_filters.py",
        "Final/yt_final.py",
        "Final/approved-python.lock",
        "Final/requirements.txt",
        "Final/THIRD_PARTY_NOTICES",
        "Final/assets/slate.ttf",
        "Final/webui/index.html",
        "Final/webui/app.js",
        "Final/webui/style.css",
    } <= installed


def test_release_wheel_contains_exact_runtime_boundary(release_distributions):
    classified_modules = {
        path.name
        for path in FINAL_DIR.glob("*.py")
        if not path.name.startswith("test_")
        and path.name not in WHEEL_EXCLUDED_MODULES
    }
    assert classified_modules == WHEEL_RUNTIME_MODULES
    expected_package = {
        f"Final/{name}"
        for name in WHEEL_RUNTIME_MODULES | WHEEL_PACKAGE_DATA
    }
    validated_files = _validated_wheel_files(
        release_distributions["wheel"]
    )
    with ZipFile(release_distributions["wheel"]) as wheel:
        member_list = [
            info.filename
            for info in wheel.infolist()
            if not info.is_dir()
        ]
    assert len(member_list) == len(set(member_list))
    members = set(member_list)

    dist_info_roots = {
        name.split("/", 1)[0]
        for name in members
        if name.startswith("yt_final-") and ".dist-info/" in name
    }
    assert dist_info_roots == {"yt_final-1.0.0.dist-info"}
    expected_dist_info = {
        f"yt_final-1.0.0.dist-info/{name}"
        for name in WHEEL_DIST_INFO_MEMBERS
    }
    assert members == expected_package | expected_dist_info
    assert set(validated_files) == members


def test_release_wheel_package_data_matches_reviewed_sources(
    release_distributions,
):
    with ZipFile(release_distributions["wheel"]) as wheel:
        for relative in WHEEL_PACKAGE_DATA:
            assert wheel.read(f"Final/{relative}") == (
                FINAL_DIR / relative
            ).read_bytes()


def test_release_wheel_runtime_and_license_bytes_match_reviewed_sources(
    release_distributions,
):
    with ZipFile(release_distributions["wheel"]) as wheel:
        for module in WHEEL_RUNTIME_MODULES:
            assert wheel.read(f"Final/{module}") == (
                FINAL_DIR / module
            ).read_bytes()
        assert wheel.read(
            "yt_final-1.0.0.dist-info/licenses/LICENSE"
        ) == (ROOT / "LICENSE").read_bytes()


def _synthetic_record_rows(files):
    rows = []
    for name, body in files.items():
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(body).digest()
        ).rstrip(b"=").decode("ascii")
        rows.append([name, f"sha256={digest}", str(len(body))])
    rows.append([f"{WHEEL_DIST_INFO_ROOT}/RECORD", "", ""])
    return rows


def _record_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _write_synthetic_wheel(
    destination,
    *,
    file_overrides=None,
    record_mutation=None,
    extra_entries=(),
):
    files = {
        "Final/__init__.py": b"",
        f"{WHEEL_DIST_INFO_ROOT}/METADATA": (
            b"Metadata-Version: 2.4\n"
            b"Name: yt-final\n"
            b"Version: 1.0.0\n\n"
        ),
        f"{WHEEL_DIST_INFO_ROOT}/WHEEL": EXPECTED_WHEEL_METADATA,
        f"{WHEEL_DIST_INFO_ROOT}/entry_points.txt": (
            EXPECTED_WHEEL_ENTRY_POINTS
        ),
        f"{WHEEL_DIST_INFO_ROOT}/licenses/LICENSE": b"reviewed license\n",
        f"{WHEEL_DIST_INFO_ROOT}/top_level.txt": EXPECTED_WHEEL_TOP_LEVEL,
    }
    files.update(file_overrides or {})
    rows = _synthetic_record_rows(files)
    if record_mutation == "missing-row":
        rows.pop(0)
    elif record_mutation == "duplicate-row":
        rows.append(list(rows[0]))
    elif record_mutation == "extra-row":
        rows.append(["Final/ghost.py", "sha256=" + "A" * 43, "0"])
    elif record_mutation == "wrong-algorithm":
        rows[0][1] = "sha512=" + "A" * 43
    elif record_mutation == "wrong-digest":
        rows[0][1] = "sha256=" + "A" * 43
    elif record_mutation == "wrong-size":
        rows[0][2] = str(int(rows[0][2]) + 1)
    elif record_mutation == "self-hash":
        rows[-1][1] = "sha256=" + "A" * 43
        rows[-1][2] = "0"
    files[f"{WHEEL_DIST_INFO_ROOT}/RECORD"] = _record_bytes(rows)

    with ZipFile(destination, "w") as wheel:
        for name, body in files.items():
            wheel.writestr(name, body)
        for name, body in extra_entries:
            wheel.writestr(name, body)
    return destination


def test_strict_wheel_validator_accepts_complete_synthetic_wheel(tmp_path):
    wheel = _write_synthetic_wheel(tmp_path / "valid.whl")
    assert set(_validated_wheel_files(wheel)) == {
        "Final/__init__.py",
        f"{WHEEL_DIST_INFO_ROOT}/METADATA",
        f"{WHEEL_DIST_INFO_ROOT}/RECORD",
        f"{WHEEL_DIST_INFO_ROOT}/WHEEL",
        f"{WHEEL_DIST_INFO_ROOT}/entry_points.txt",
        f"{WHEEL_DIST_INFO_ROOT}/licenses/LICENSE",
        f"{WHEEL_DIST_INFO_ROOT}/top_level.txt",
    }


def test_strict_wheel_validator_rejects_directory_escape_and_empty_metadata(
    tmp_path,
):
    wheel_path = tmp_path / "escape.whl"
    with ZipFile(wheel_path, "w") as wheel:
        wheel.writestr("../../escape/", b"")
        for name in ("RECORD", "WHEEL", "entry_points.txt", "top_level.txt"):
            wheel.writestr(f"{WHEEL_DIST_INFO_ROOT}/{name}", b"")

    with pytest.raises(AssertionError):
        _validated_wheel_files(wheel_path)


@pytest.mark.parametrize(
    "name",
    (
        "../escape.py",
        "Final/../../escape.py",
        "/absolute.py",
        r"\absolute.py",
        r"Final\module.py",
        "C:/escape.py",
        "C:escape.py",
        "Final/module.py:stream",
        "Final/\x00escape.py",
        "Final//module.py",
        "Final/./module.py",
        "Final/module.py/",
    ),
)
def test_strict_wheel_name_gate_rejects_unsafe_names(name):
    with pytest.raises(AssertionError):
        _validated_wheel_name(name)


@pytest.mark.parametrize(
    "duplicates",
    (
        ("Final/CLI.py", "Final/cli.py"),
        ("Final/caf\u00e9.py", "Final/cafe\u0301.py"),
    ),
)
def test_strict_wheel_validator_rejects_normalized_casefold_duplicates(
    tmp_path, duplicates
):
    wheel = _write_synthetic_wheel(
        tmp_path / "duplicate.whl",
        extra_entries=((duplicates[0], b"one"), (duplicates[1], b"two")),
    )
    with pytest.raises(AssertionError, match="duplicate"):
        _validated_wheel_files(wheel)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-row",
        "duplicate-row",
        "extra-row",
        "wrong-algorithm",
        "wrong-digest",
        "wrong-size",
        "self-hash",
    ),
)
def test_strict_wheel_record_rejects_incomplete_or_false_rows(
    tmp_path, mutation
):
    wheel = _write_synthetic_wheel(
        tmp_path / f"{mutation}.whl",
        record_mutation=mutation,
    )
    with pytest.raises(AssertionError):
        _validated_wheel_files(wheel)


@pytest.mark.parametrize(
    ("member", "body"),
    (
        (
            "WHEEL",
            EXPECTED_WHEEL_METADATA.replace(
                b"Root-Is-Purelib: true",
                b"Root-Is-Purelib: false",
            ),
        ),
        (
            "WHEEL",
            EXPECTED_WHEEL_METADATA.replace(
                b"Tag: py3-none-any",
                b"Tag: cp311-cp311-win_amd64",
            ),
        ),
        (
            "WHEEL",
            EXPECTED_WHEEL_METADATA.replace(
                b"Generator: setuptools (83.0.0)",
                b"Generator: unknown",
            ),
        ),
        ("entry_points.txt", b"[console_scripts]\nyt-final = bad:main\n"),
        ("top_level.txt", b"final\n"),
    ),
)
def test_strict_wheel_validator_rejects_false_control_metadata(
    tmp_path, member, body
):
    wheel = _write_synthetic_wheel(
        tmp_path / f"false-{member.replace('.', '-')}.whl",
        file_overrides={f"{WHEEL_DIST_INFO_ROOT}/{member}": body},
    )
    with pytest.raises(AssertionError):
        _validated_wheel_files(wheel)


def test_release_wheel_metadata_has_only_compatible_runtime_floors(
    release_distributions,
):
    with ZipFile(release_distributions["wheel"]) as wheel:
        metadata_name = next(
            name
            for name in wheel.namelist()
            if name.endswith(".dist-info/METADATA")
        )
        metadata = message_from_string(
            wheel.read(metadata_name).decode("utf-8")
        )
    requirements = _unique_metadata_requirements(metadata)

    assert metadata["Name"] == "yt-final"
    assert metadata["Version"] == "1.0.0"
    assert metadata["Requires-Python"] == ">=3.11"
    assert {
        name: str(requirement.specifier)
        for name, requirement in requirements.items()
    } == RUNTIME_DEPENDENCY_FLOORS
    assert all(
        len(tuple(requirement.specifier)) == 1
        and next(iter(requirement.specifier)).operator == ">="
        and requirement.marker is None
        and requirement.url is None
        and not requirement.extras
        for requirement in requirements.values()
    )
    assert not set(requirements) & NON_RUNTIME_DISTRIBUTIONS


def test_release_wheel_metadata_has_exact_pep639_license(
    release_distributions,
):
    with ZipFile(release_distributions["wheel"]) as wheel:
        metadata_name = next(
            name
            for name in wheel.namelist()
            if name.endswith(".dist-info/METADATA")
        )
        metadata = message_from_string(
            wheel.read(metadata_name).decode("utf-8")
        )

    assert metadata.get_all("License-Expression") == ["Unlicense"]
    assert metadata.get_all("License-File") == ["LICENSE"]
    assert metadata.get_all("License") is None


def test_wheel_metadata_gate_rejects_duplicate_canonical_requirements():
    metadata = message_from_string(
        "Metadata-Version: 2.4\n"
        "Name: yt-final\n"
        "Version: 1.0.0\n"
        "Requires-Dist: requests>=2.32.2\n"
        "Requires-Dist: Requests>=2.32.2\n"
        "\n"
    )
    with pytest.raises(AssertionError, match="duplicate Requires-Dist"):
        _unique_metadata_requirements(metadata)


def test_release_sdist_contains_exact_supported_source_boundary(
    release_distributions,
):
    source_python_members = {
        f"Final/{path.name}" for path in FINAL_DIR.glob("*.py")
    }
    assert source_python_members == SDIST_FINAL_PYTHON_MEMBERS
    expected_sources = (
        SDIST_STATIC_SOURCE_MEMBERS | SDIST_FINAL_PYTHON_MEMBERS
    )
    expected = SDIST_GENERATED_MEMBERS | expected_sources
    with tarfile.open(release_distributions["sdist"], "r:gz") as sdist:
        members = sdist.getmembers()
        roots = {member.name.split("/", 1)[0] for member in members}
        assert roots == {"yt_final-1.0.0"}
        assert all(member.isdir() or member.isfile() for member in members)
        actual = {
            member.name.split("/", 1)[1]
            for member in members
            if member.isfile()
        }

    assert actual == expected


def test_release_sdist_excludes_real_ignored_log_and_repository_material(
    release_distributions,
):
    assert (release_distributions["source"] / "Final/final_yt.log").is_file()
    with tarfile.open(release_distributions["sdist"], "r:gz") as sdist:
        members = {
            member.name.split("/", 1)[1]
            for member in sdist.getmembers()
            if "/" in member.name
        }
    forbidden_prefixes = (
        ".claude/",
        ".github/",
        ".superpowers/",
        ".worktrees/",
        "archive/",
        "build/",
        "dist/",
        "downloads/",
        "Final/docs/",
        "Final/downloads/",
        "mcps/",
        "test_out/",
    )
    assert "Final/final_yt.log" not in members
    assert not any(
        member.endswith((".log", ".pyc"))
        or "__pycache__/" in member
        or member.startswith(forbidden_prefixes)
        for member in members
    )


def test_release_build_commands_are_structurally_bound_to_reviewed_classes():
    import ast

    tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8"))
    release_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "ReleaseBuildPy"
    )
    assert [
        base.id for base in release_class.bases if isinstance(base, ast.Name)
    ] == ["build_py"]
    sdist_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "ReleaseSdist"
    )
    assert [
        base.id for base in sdist_class.bases if isinstance(base, ast.Name)
    ] == ["sdist"]
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "make_archive"
        for node in sdist_class.body
    )
    method = next(
        node
        for node in release_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "find_package_modules"
    )
    constants = {
        node.value
        for node in ast.walk(method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "Final" in constants
    assert "test_" in constants
    assert any(
        isinstance(node, ast.Compare)
        and any(
            isinstance(operator, ast.NotIn)
            for operator in node.ops
        )
        and any(
            isinstance(comparator, ast.Name)
            and comparator.id == "RELEASE_EXCLUDED_MODULES"
            for comparator in node.comparators
        )
        for node in ast.walk(method)
    )

    exclusions = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "RELEASE_EXCLUDED_MODULES"
            for target in node.targets
        )
    )
    assert {
        element.value
        for element in exclusions.value.elts
        if isinstance(element, ast.Constant)
    } == {"_syntax_check", "runtime_verify"}
    setup_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setup"
    )
    cmdclasses = [
        keyword.value
        for keyword in setup_call.keywords
        if keyword.arg == "cmdclass"
    ]
    assert len(cmdclasses) == 1
    cmdclass = cmdclasses[0]
    assert isinstance(cmdclass, ast.Dict)
    assert [
        key.value for key in cmdclass.keys if isinstance(key, ast.Constant)
    ] == ["build_py", "sdist"]
    assert [
        value.id
        for value in cmdclass.values
        if isinstance(value, ast.Name)
    ] == ["ReleaseBuildPy", "ReleaseSdist"]


def test_release_dependency_inputs_pin_the_reviewed_toolchain():
    for name, expected in REVIEWED_LOCK_INPUTS.items():
        assert _effective_lines(REQUIREMENTS_DIR / name) == expected


@pytest.mark.parametrize(
    ("lock_path", "input_path", "expected_names", "expected_header"),
    LOCK_CONTRACTS,
)
def test_release_locks_are_generated_hash_locked_and_fail_closed(
    lock_path, input_path, expected_names, expected_header
):
    _assert_generated_hash_lock(
        ROOT / lock_path,
        input_path=input_path,
        expected_names=expected_names,
        expected_header=expected_header,
    )


@pytest.mark.parametrize(
    "body",
    [
        (
            "requests==2.34.2 \\\n"
            f"    --hash=sha256:{'a' * 64} \\\n"
            "    --only-binary=:all:\n"
        ),
        (
            "requests[socks]==2.34.2 \\\n"
            f"    --hash=sha256:{'a' * 64}\n"
        ),
        (
            "requests==2.34.2 \\\n"
            f"    --hash=sha256:{'a' * 64} \\\n"
            "    --hash=sha256:not-a-digest\n"
        ),
        (
            'requests==2.34.2 ; python_version >= "3.11" \\\n'
            f"    --hash=sha256:{'a' * 64}\n"
        ),
    ],
)
def test_lock_gate_rejects_unknown_continuations_and_extras(tmp_path, body):
    lock_path = tmp_path / "malicious.lock"
    lock_path.write_text(body, encoding="utf-8", newline="\n")
    with pytest.raises(AssertionError):
        _locked_requirements(lock_path)


def test_lock_closure_gate_rejects_an_extra_official_package(tmp_path):
    lock_path = tmp_path / "runtime.lock"
    body = (FINAL_DIR / "approved-python.lock").read_text(encoding="utf-8")
    body += (
        "\nattrs==25.4.0 \\\n"
        f"    --hash=sha256:{'a' * 64}\n"
    )
    lock_path.write_text(body, encoding="utf-8", newline="\n")
    with pytest.raises(AssertionError, match="unexpected lock closure"):
        _assert_exact_lock_names(lock_path, EXPECTED_RUNTIME_LOCK_NAMES)


def test_approved_runtime_lock_is_exactly_the_runtime_dependency_closure():
    from Final.dependency_contract import declared_runtime_floors

    locked = _locked_requirements(_runtime_lock_path())
    assert set(locked) == EXPECTED_RUNTIME_LOCK_NAMES
    assert set(locked).isdisjoint(NON_RUNTIME_DISTRIBUTIONS)

    for name, floor in declared_runtime_floors().items():
        assert name in locked
        assert locked[name][0] >= Version(floor)


def test_environment_locks_contain_only_their_declared_layers():
    runtime_names = set(_locked_requirements(_runtime_lock_path()))
    build_names = set(_locked_requirements(REQUIREMENTS_DIR / "build.lock"))
    dev_names = set(_locked_requirements(REQUIREMENTS_DIR / "dev.lock"))
    release_names = set(_locked_requirements(REQUIREMENTS_DIR / "release.lock"))

    assert runtime_names == EXPECTED_RUNTIME_LOCK_NAMES
    assert build_names == EXPECTED_BUILD_LOCK_NAMES
    assert dev_names == EXPECTED_DEV_LOCK_NAMES
    assert release_names == EXPECTED_RELEASE_LOCK_NAMES


def test_lock_files_regenerate_byte_for_byte_with_reviewed_tools(tmp_path):
    if os.environ.get("YT_FINAL_VERIFY_LOCK_REGEN") != "1":
        pytest.skip(
            "set YT_FINAL_VERIFY_LOCK_REGEN=1 for exact network regeneration"
        )

    lock_python = os.environ.get("YT_FINAL_LOCK_PYTHON")
    assert lock_python, "YT_FINAL_LOCK_PYTHON is required"
    lock_python, _ = _verified_lock_toolchain(lock_python)

    copied_inputs = {input_path for _, input_path, _, _ in LOCK_CONTRACTS}
    for input_path in copied_inputs:
        destination = tmp_path / input_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / input_path, destination)

    for lock_path, input_path, _, _ in LOCK_CONTRACTS:
        destination = tmp_path / lock_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = _run_validated_lock_python_module(
            lock_python,
            "piptools",
            "compile",
            "--allow-unsafe",
            "--generate-hashes",
            "--newline=lf",
            "--no-emit-index-url",
            "--no-emit-trusted-host",
            "--resolver=backtracking",
            "--strip-extras",
            f"--output-file={lock_path.as_posix()}",
            input_path.as_posix(),
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, (
            f"{lock_path} regeneration failed\n{result.stdout}{result.stderr}"
        )
        assert destination.read_bytes() == (ROOT / lock_path).read_bytes()


def test_runtime_lock_has_wheels_for_every_tier_one_target(tmp_path):
    if os.environ.get("YT_FINAL_VERIFY_LOCK_WHEELS") != "1":
        pytest.skip(
            "set YT_FINAL_VERIFY_LOCK_WHEELS=1 for the network-backed wheel matrix"
        )

    candidate = _runtime_lock_path()
    lock_python = os.environ.get("YT_FINAL_LOCK_PYTHON")
    assert lock_python, "YT_FINAL_LOCK_PYTHON is required"
    lock_python, _ = _verified_lock_toolchain(lock_python)
    locked_names = set(_locked_requirements(candidate))
    assert locked_names == EXPECTED_RUNTIME_LOCK_NAMES

    for label, platform, python_version, abi in TIER_ONE_WHEEL_TARGETS:
        destination = tmp_path / label
        destination.mkdir()
        result = _run_validated_lock_python_module(
            lock_python,
            "pip",
            "download",
            "--disable-pip-version-check",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "--implementation=cp",
            f"--platform={platform}",
            f"--python-version={python_version}",
            f"--abi={abi}",
            "--dest",
            str(destination),
            "-r",
            str(candidate),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, (
            f"{label} wheel resolution failed\n{result.stdout}{result.stderr}"
        )
        downloads = list(destination.iterdir())
        assert len(downloads) == len(locked_names)
        assert all(path.suffix == ".whl" for path in downloads)


def test_wheel_evidence_requires_an_explicit_compiler(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_VERIFY_LOCK_WHEELS", "1")
    monkeypatch.delenv("YT_FINAL_LOCK_PYTHON", raising=False)

    def unexpected_subprocess(*args, **kwargs):
        raise AssertionError("wheel gate ran without an explicit compiler")

    monkeypatch.setattr(subprocess, "run", unexpected_subprocess)
    with pytest.raises(AssertionError, match="YT_FINAL_LOCK_PYTHON is required"):
        test_runtime_lock_has_wheels_for_every_tier_one_target(tmp_path)


def _reviewed_tool_identity(**overrides):
    platform_tag = sysconfig.get_platform()
    if sys.platform == "win32":
        machine = {
            "win-amd64": "AMD64",
            "win-arm64": "ARM64",
            "win32": "x86",
        }.get(platform_tag, platform_tag)
    else:
        machine = os.uname().machine
    identity = {
        "abi_flags": getattr(sys, "abiflags", ""),
        "cache_tag": sys.implementation.cache_tag,
        "executable": str(Path(sys.executable).resolve(strict=True)),
        "gil_disabled": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "implementation": sys.implementation.name,
        "machine": machine,
        "pip": "26.2",
        "pip-tools": "7.6.0",
        "platform": sys.platform,
        "platform_tag": platform_tag,
        "pointer_bits": struct.calcsize("P") * 8,
        "python": [
            sys.version_info.major,
            sys.version_info.minor,
        ],
        "trace_refs": bool(sysconfig.get_config_var("Py_TRACE_REFS")),
    }
    identity.update(overrides)
    return identity


def _successful_reviewed_tool_probe(command, **overrides):
    return subprocess.CompletedProcess(
        args=command,
        returncode=0,
        stdout=json.dumps(_reviewed_tool_identity(**overrides)) + "\n",
        stderr="",
    )


def _synthetic_build_tool_identity(python_version=(3, 11), **overrides):
    identity = _reviewed_tool_identity(
        cache_tag=f"cpython-{python_version[0]}{python_version[1]}",
        python=list(python_version),
    )
    identity.update(
        {
            "debug": False,
            "duplicates": [],
            "versions": {
                name: str(contract[0])
                for name, contract in _locked_requirements(
                    REQUIREMENTS_DIR / "build.lock"
                ).items()
            },
        }
    )
    identity.update(overrides)
    return identity


def _install_synthetic_build_probes(monkeypatch, identity):
    interpreter = str(Path(sys.executable).resolve(strict=True))
    calls = []

    monkeypatch.setattr(
        sys.modules[__name__],
        "_validated_lock_python",
        lambda _candidate: interpreter,
    )

    def probe(_python, *arguments, **_kwargs):
        calls.append(arguments)
        if len(calls) == 1:
            payload = identity
        else:
            versions = identity["versions"]
            payload = {
                "build": versions["build"],
                "names": sorted(versions),
                "setuptools": versions["setuptools"],
                "wheel": versions["wheel"],
            }
        return subprocess.CompletedProcess(
            args=arguments,
            returncode=0,
            stdout=json.dumps(payload) + "\n",
            stderr="",
        )

    monkeypatch.setattr(
        sys.modules[__name__],
        "_run_validated_lock_python",
        probe,
    )
    return interpreter, calls


@pytest.mark.parametrize("python_version", ((3, 11), (3, 14)))
def test_build_toolchain_accepts_supported_standard_native_cpython(
    tmp_path, monkeypatch, python_version
):
    interpreter, calls = _install_synthetic_build_probes(
        monkeypatch,
        _synthetic_build_tool_identity(python_version),
    )

    environment = _release_state_environment(
        tmp_path / "state",
        tmp_path / "cache",
    )
    assert _verified_build_toolchain(
        interpreter,
        environment_updates=environment,
        cwd=Path(environment["HOME"]),
    ) == interpreter
    assert len(calls) == 1


def _wrong_native_identity():
    if sys.platform == "win32":
        current = str(platform.machine()).lower()
        return (
            {"machine": "ARM64", "platform_tag": "win-arm64"}
            if current in {"amd64", "x86_64"}
            else {"machine": "AMD64", "platform_tag": "win-amd64"}
        )
    current = str(platform.machine()).lower()
    return (
        {"machine": "arm64", "platform_tag": "macosx-11.0-arm64"}
        if current in {"amd64", "x86_64"}
        else {"machine": "x86_64", "platform_tag": "macosx-10.15-x86_64"}
    )


@pytest.mark.parametrize(
    "overrides",
    (
        {"python": [3, 10], "cache_tag": "cpython-310"},
        {"gil_disabled": True},
        {"trace_refs": True},
        {"debug": True},
        pytest.param(_wrong_native_identity(), id="wrong-native-architecture"),
    ),
)
def test_build_toolchain_rejects_unsupported_or_nonstandard_cpython(
    tmp_path, monkeypatch, overrides
):
    identity = _synthetic_build_tool_identity()
    identity.update(overrides)
    interpreter, _calls = _install_synthetic_build_probes(
        monkeypatch, identity
    )

    with pytest.raises(AssertionError):
        _verified_build_toolchain(
            interpreter,
            environment_updates=_release_state_environment(
                tmp_path / "state",
                tmp_path / "cache",
            ),
            cwd=tmp_path,
        )


@pytest.mark.parametrize(
    "distribution",
    ("click", "colorama", "packaging", "pyproject-hooks"),
)
def test_build_toolchain_rejects_every_indirect_lock_version_mismatch(
    tmp_path, monkeypatch, distribution
):
    identity = _synthetic_build_tool_identity()
    identity["versions"][distribution] = "0.0"
    interpreter, _calls = _install_synthetic_build_probes(
        monkeypatch, identity
    )

    with pytest.raises(AssertionError, match="build.lock"):
        _verified_build_toolchain(
            interpreter,
            environment_updates=_release_state_environment(
                tmp_path / "state",
                tmp_path / "cache",
            ),
            cwd=tmp_path,
        )


@pytest.mark.parametrize("candidate", ("python", "tools/python.exe"))
def test_lock_toolchain_rejects_non_absolute_interpreters_before_subprocess(
    candidate, monkeypatch
):
    launched = []

    def reviewed_probe(command, **kwargs):
        launched.append(command)
        return _successful_reviewed_tool_probe(command)

    monkeypatch.setattr(subprocess, "run", reviewed_probe)
    with pytest.raises(AssertionError, match="absolute"):
        _verified_lock_toolchain(candidate)
    assert launched == []


def test_lock_toolchain_rejects_a_missing_interpreter_before_subprocess(
    tmp_path, monkeypatch
):
    missing = tmp_path / "missing-python.exe"
    launched = []

    def reviewed_probe(command, **kwargs):
        launched.append(command)
        return _successful_reviewed_tool_probe(command)

    monkeypatch.setattr(subprocess, "run", reviewed_probe)
    with pytest.raises(AssertionError, match="exist"):
        _verified_lock_toolchain(str(missing))
    assert launched == []


def test_lock_toolchain_rejects_a_directory_before_subprocess(
    tmp_path, monkeypatch
):
    directory = tmp_path / "python.exe"
    directory.mkdir()
    launched = []

    def reviewed_probe(command, **kwargs):
        launched.append(command)
        return _successful_reviewed_tool_probe(command)

    monkeypatch.setattr(subprocess, "run", reviewed_probe)
    with pytest.raises(AssertionError, match="regular file"):
        _verified_lock_toolchain(str(directory))
    assert launched == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits are required")
def test_lock_toolchain_rejects_a_non_executable_posix_file_before_subprocess(
    tmp_path, monkeypatch
):
    inert = tmp_path / "python"
    inert.write_bytes(b"inert")
    inert.chmod(0o600)
    launched = []

    def reviewed_probe(command, **kwargs):
        launched.append(command)
        return _successful_reviewed_tool_probe(command)

    monkeypatch.setattr(subprocess, "run", reviewed_probe)
    with pytest.raises(AssertionError, match="executable"):
        _verified_lock_toolchain(str(inert))
    assert launched == []


@pytest.mark.skipif(os.name != "nt", reason="requires Windows executable proof")
@pytest.mark.parametrize(
    ("name", "body"),
    (
        ("python.exe", b"inert"),
        ("python.txt", b"inert"),
        ("python", b"inert"),
        ("fake-python.cmd", b"@echo off\r\necho fake\r\n"),
    ),
)
def test_lock_toolchain_rejects_non_pe_windows_files_before_subprocess(
    tmp_path, monkeypatch, name, body
):
    inert = tmp_path / name
    inert.write_bytes(body)
    launched = []

    def reviewed_probe(command, **kwargs):
        launched.append(command)
        return _successful_reviewed_tool_probe(command)

    monkeypatch.setattr(subprocess, "run", reviewed_probe)
    with pytest.raises(AssertionError, match="executable"):
        _verified_lock_toolchain(str(inert))
    assert launched == []


@pytest.mark.parametrize("poison_kind", ("symlink", "junction", "reparse"))
def test_lock_toolchain_rejects_link_like_path_components_before_subprocess(
    poison_kind, tmp_path, monkeypatch
):
    poisoned = tmp_path / "poisoned"
    interpreter = poisoned / "python.exe"
    poisoned.mkdir()
    interpreter.write_bytes(b"interpreter")

    if poison_kind == "symlink":
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: path == poisoned or real_is_symlink(path),
        )
    elif poison_kind == "junction":
        real_is_junction = getattr(Path, "is_junction", lambda _path: False)
        monkeypatch.setattr(
            Path,
            "is_junction",
            lambda path: path == poisoned or real_is_junction(path),
            raising=False,
        )
    else:
        real_lstat = Path.lstat

        class ReparseMetadata:
            def __init__(self, metadata):
                self._metadata = metadata
                self.st_mode = metadata.st_mode
                self.st_file_attributes = (
                    getattr(metadata, "st_file_attributes", 0) | 0x400
                )

            def __getattr__(self, name):
                return getattr(self._metadata, name)

        def reparse_lstat(path):
            metadata = real_lstat(path)
            return (
                ReparseMetadata(metadata)
                if path == poisoned
                else metadata
            )

        monkeypatch.setattr(Path, "lstat", reparse_lstat)

    launched = []

    def reviewed_probe(command, **kwargs):
        launched.append(command)
        return _successful_reviewed_tool_probe(command)

    monkeypatch.setattr(subprocess, "run", reviewed_probe)
    with pytest.raises(AssertionError, match="link-free"):
        _verified_lock_toolchain(str(interpreter))
    assert launched == []


def test_lock_toolchain_rejects_a_resolved_identity_mismatch_before_subprocess(
    tmp_path, monkeypatch
):
    interpreter = tmp_path / "python.exe"
    replacement = tmp_path / "replacement.exe"
    interpreter.write_bytes(b"interpreter")
    replacement.write_bytes(b"replacement")
    real_resolve = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path, strict=False: (
            replacement
            if path == interpreter
            else real_resolve(path, strict=strict)
        ),
    )
    launched = []

    def reviewed_probe(command, **kwargs):
        launched.append(command)
        return _successful_reviewed_tool_probe(command)

    monkeypatch.setattr(subprocess, "run", reviewed_probe)
    with pytest.raises(AssertionError, match="identity"):
        _verified_lock_toolchain(str(interpreter))
    assert launched == []


@pytest.mark.skipif(
    sys.version_info[:2] != (3, 11),
    reason=(
        "the reviewed lock toolchain is CPython 3.11 (built/uploaded only on the py311 cell); "
        "this acceptance test uses the running interpreter as the candidate, so it is meaningful "
        "only under 3.11"
    ),
)
def test_lock_toolchain_uses_an_absolute_real_interpreter(monkeypatch):
    interpreter = Path(sys.executable).resolve(strict=True)
    launched = []

    def reviewed_probe(command, **kwargs):
        launched.append(command)
        return _successful_reviewed_tool_probe(command)

    monkeypatch.setattr(subprocess, "run", reviewed_probe)
    validated, actual = _verified_lock_toolchain(str(interpreter))

    assert validated == str(interpreter)
    assert Path(validated).is_absolute()
    assert Path(validated).is_file()
    assert launched[0][0] == str(interpreter)
    assert actual["pip-tools"] == "7.6.0"


@pytest.mark.parametrize(
    ("module", "arguments", "expected"),
    (
        (
            "pip",
            ("download", "artifact"),
            [
                "C:/reviewed/python.exe",
                "-I",
                "-m",
                "pip",
                "--isolated",
                "download",
                "artifact",
            ],
        ),
        (
            "piptools",
            ("compile", "requirements.in"),
            [
                "C:/reviewed/python.exe",
                "-I",
                "-m",
                "piptools",
                "compile",
                "requirements.in",
            ],
        ),
        (
            "venv",
            ("--without-pip", "environment"),
            [
                "C:/reviewed/python.exe",
                "-I",
                "-m",
                "venv",
                "--without-pip",
                "environment",
            ],
        ),
        (
            "build",
            ("--version",),
            [
                "C:/reviewed/python.exe",
                "-I",
                "-m",
                "build",
                "--version",
            ],
        ),
        (
            "pytest",
            ("--version",),
            [
                "C:/reviewed/python.exe",
                "-I",
                "-m",
                "pytest",
                "--version",
            ],
        ),
        (
            "twine",
            ("--version",),
            [
                "C:/reviewed/python.exe",
                "-I",
                "-m",
                "twine",
                "--version",
            ],
        ),
    ),
)
def test_evidence_module_runner_uses_exact_isolated_command_and_clean_env(
    monkeypatch, module, arguments, expected
):
    monkeypatch.setenv("PYTHONPATH", "C:/poison/pythonpath")
    monkeypatch.setenv("PYTHONHOME", "C:/poison/pythonhome")
    monkeypatch.setenv("PIP_INDEX_URL", "https://poison.invalid/simple")
    captured = []

    def capture(command, **kwargs):
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", capture)
    _run_isolated_python_module(
        "C:/reviewed/python.exe",
        module,
        *arguments,
        check=False,
        capture_output=True,
        text=True,
    )

    assert len(captured) == 1
    command, kwargs = captured[0]
    assert command == expected
    assert "PYTHONPATH" not in kwargs["env"]
    assert "PYTHONHOME" not in kwargs["env"]
    assert "PIP_INDEX_URL" not in kwargs["env"]
    assert kwargs["env"]["PYTHONNOUSERSITE"] == "1"
    assert kwargs["env"]["PIP_NO_INPUT"] == "1"
    assert kwargs["env"]["PIP_CONFIG_FILE"] == os.devnull


@pytest.mark.parametrize(
    "module",
    ("pip", "piptools", "venv", "build", "pytest", "twine"),
)
def test_evidence_module_runner_never_executes_pythonpath_poison(
    tmp_path, monkeypatch, module
):
    poison_root = tmp_path / "poison"
    poison_package = poison_root / module
    poison_package.mkdir(parents=True)
    (poison_package / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / f"{module}-executed"
    (poison_package / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        "raise SystemExit(86)\n",
        encoding="utf-8",
    )
    contaminated_env = os.environ.copy()
    contaminated_env["PYTHONPATH"] = str(poison_root)
    control = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        env=contaminated_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert control.returncode == 86
    assert marker.read_text(encoding="utf-8") == "executed"
    marker.unlink()

    monkeypatch.setenv("PYTHONPATH", str(poison_root))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "poison-home"))
    result = _run_isolated_python_module(
        sys.executable,
        module,
        "--help",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 86
    assert not marker.exists()


def test_evidence_python_module_launches_use_the_isolated_runner():
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    direct_module_launches = []

    class DirectModuleLaunchVisitor(ast.NodeVisitor):
        def __init__(self):
            self.function_name = None

        def visit_FunctionDef(self, node):
            previous = self.function_name
            self.function_name = node.name
            self.generic_visit(node)
            self.function_name = previous

        def visit_Call(self, node):
            is_subprocess_run = (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr == "run"
            )
            if (
                is_subprocess_run
                and node.args
                and isinstance(node.args[0], (ast.List, ast.Tuple))
                and any(
                    isinstance(element, ast.Constant)
                    and element.value == "-m"
                    for element in node.args[0].elts
                )
                and self.function_name
                != "test_evidence_module_runner_never_executes_pythonpath_poison"
            ):
                direct_module_launches.append(node.lineno)
            self.generic_visit(node)

    DirectModuleLaunchVisitor().visit(tree)
    assert direct_module_launches == []


def test_lock_python_module_runner_revalidates_and_uses_returned_path(
    monkeypatch
):
    requested = "C:/operator-selected/python.exe"
    canonical = "C:/reviewed/python.exe"
    validations = []
    launched = []

    def validate(candidate):
        validations.append(candidate)
        return canonical

    def capture(command, **kwargs):
        launched.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(
        sys.modules[__name__], "_validated_lock_python", validate
    )
    monkeypatch.setattr(subprocess, "run", capture)
    _run_validated_lock_python_module(
        requested,
        "pip",
        "check",
        check=False,
        capture_output=True,
        text=True,
    )

    assert validations == [requested]
    assert launched == [
        [
            canonical,
            "-I",
            "-m",
            "pip",
            "--isolated",
            "check",
        ]
    ]


@pytest.mark.parametrize(
    ("tool", "wrong_version", "expected_error"),
    (
        ("pip", "26.1.1", "pip==26.2"),
        ("pip-tools", "7.5.0", "pip-tools==7.6.0"),
    ),
)
def test_wheel_evidence_rejects_the_wrong_toolchain(
    tmp_path, monkeypatch, tool, wrong_version, expected_error
):
    monkeypatch.setenv("YT_FINAL_VERIFY_LOCK_WHEELS", "1")
    monkeypatch.setenv(
        "YT_FINAL_LOCK_PYTHON",
        str(Path(sys.executable).resolve(strict=True)),
    )
    versions = {"pip": "26.2", "pip-tools": "7.6.0"}
    versions[tool] = wrong_version
    wrong_tools = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(versions) + "\n",
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: wrong_tools)

    with pytest.raises(AssertionError, match=expected_error):
        test_runtime_lock_has_wheels_for_every_tier_one_target(tmp_path)


def test_native_lock_gate_requires_an_explicit_ci_target(monkeypatch):
    monkeypatch.delenv("YT_FINAL_NATIVE_TARGET", raising=False)
    with pytest.raises(AssertionError, match="YT_FINAL_NATIVE_TARGET"):
        _verified_native_target(sys.executable)


def _current_native_target_name():
    normalized_machine = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(platform.machine().lower(), platform.machine().lower())
    current = (
        sys.platform,
        normalized_machine,
        (sys.version_info.major, sys.version_info.minor),
    )
    for name, expected in NATIVE_TARGETS.items():
        if expected == current:
            return name
    pytest.skip(f"current interpreter is not a Tier-1 native target: {current!r}")


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    (
        ("implementation", "pypy", "CPython"),
        ("pointer_bits", 32, "64-bit"),
        # macOS trips the earlier platform_tag.startswith("macosx-") check and raises
        # "process platform"; Windows raises "process architecture". Both mean "bad platform_tag
        # rejected" — accept either so this passes on every Tier-1 runner.
        ("platform_tag", "incompatible-process-tag", r"process (architecture|platform)"),
        ("gil_disabled", True, "free-threaded"),
        ("abi_flags", "t", "standard ABI"),
        ("cache_tag", "cpython-311t", "standard ABI"),
        ("executable", None, "same interpreter"),
        ("machine", "unexpected", "process architecture"),
        ("platform", "unexpected", "operating system"),
        ("python", [3, 10], "Python 3."),
        ("trace_refs", True, "trace-reference"),
    ),
)
def test_native_gate_rejects_untrusted_interpreter_identity_before_venv(
    tmp_path, monkeypatch, field, value, expected_error
):
    target_name = _current_native_target_name()
    selected_python = str(Path(sys.executable).resolve(strict=True))
    identity = _reviewed_tool_identity()
    identity[field] = (
        str(tmp_path / "different-python.exe")
        if field == "executable"
        else value
    )
    if field == "executable":
        Path(identity[field]).write_bytes(b"inert")
    launched = []

    def identity_probe(command, **kwargs):
        launched.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(identity) + "\n",
            "",
        )

    monkeypatch.setenv("YT_FINAL_VERIFY_NATIVE_LOCK", "1")
    monkeypatch.setenv("YT_FINAL_LOCK_PYTHON", selected_python)
    monkeypatch.setenv("YT_FINAL_NATIVE_TARGET", target_name)
    monkeypatch.setattr(subprocess, "run", identity_probe)

    with pytest.raises(AssertionError, match=expected_error):
        test_dependency_locks_install_natively_with_hashes(tmp_path)
    assert len(launched) == 1
    assert launched[0][0] == selected_python
    assert launched[0][1:3] == ["-I", "-c"]


def test_dependency_locks_install_natively_with_hashes(tmp_path):
    if os.environ.get("YT_FINAL_VERIFY_NATIVE_LOCK") != "1":
        pytest.skip(
            "set YT_FINAL_VERIFY_NATIVE_LOCK=1 on each native CI matrix target"
        )

    lock_python = os.environ.get("YT_FINAL_LOCK_PYTHON")
    assert lock_python, "YT_FINAL_LOCK_PYTHON is required"
    lock_python, native = _verified_native_target(lock_python)
    lock_cases = (
        ("runtime", _runtime_lock_path(), EXPECTED_RUNTIME_LOCK_NAMES),
        ("build", REQUIREMENTS_DIR / "build.lock", EXPECTED_BUILD_LOCK_NAMES),
        ("dev", REQUIREMENTS_DIR / "dev.lock", EXPECTED_DEV_LOCK_NAMES),
        (
            "release",
            REQUIREMENTS_DIR / "release.lock",
            EXPECTED_RELEASE_LOCK_NAMES,
        ),
    )
    tool_commands = {
        "runtime": (
            None,
            ("-c", "import packaging, regex, requests, urllib3, yt_dlp"),
        ),
        "build": ("build", ("--version",)),
        "dev": ("pytest", ("--version",)),
        "release": ("twine", ("--version",)),
    }
    for label, lock_path, expected_names in lock_cases:
        environment = tmp_path / label
        create = _run_validated_lock_python_module(
            lock_python,
            "venv",
            "--without-pip",
            str(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert create.returncode == 0, create.stdout + create.stderr
        target_python = environment / (
            "Scripts/python.exe"
            if native["platform"] == "win32"
            else "bin/python"
        )

        install = _run_validated_lock_python_module(
            lock_python,
            "pip",
            "--python",
            str(target_python),
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(lock_path),
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert install.returncode == 0, (
            f"{label} lock install failed\n{install.stdout}{install.stderr}"
        )

        check = _run_validated_lock_python_module(
            lock_python,
            "pip",
            "--python",
            str(target_python),
            "check",
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert check.returncode == 0, (
            f"{label} lock pip check failed\n{check.stdout}{check.stderr}"
        )
        assert "No broken requirements found." in check.stdout

        metadata_probe = _run_isolated_python(
            target_python,
            "-c",
            (
                "import json, re; "
                "from importlib.metadata import distributions; "
                "normalized=lambda value: "
                "re.sub(r'[-_.]+', '-', value).lower(); "
                "print(json.dumps({"
                "normalized(item.metadata['Name']): item.version "
                "for item in distributions()"
                "}, sort_keys=True))"
            ),
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert metadata_probe.returncode == 0, (
            metadata_probe.stdout + metadata_probe.stderr
        )
        installed = json.loads(metadata_probe.stdout)
        locked = _locked_requirements(lock_path)
        expected_versions = {
            name: str(details[0]) for name, details in locked.items()
        }
        assert set(expected_versions) == expected_names
        assert installed == expected_versions
        for name, version in EXPECTED_DIRECT_LOCK_VERSIONS[label].items():
            assert installed[name] == version

        tool_module, tool_arguments = tool_commands[label]
        tool_runner = (
            _run_isolated_python_module
            if tool_module is not None
            else _run_isolated_python
        )
        tool_probe = tool_runner(
            target_python,
            *(
                (tool_module, *tool_arguments)
                if tool_module is not None
                else tool_arguments
            ),
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert tool_probe.returncode == 0, (
            f"{label} direct-tool probe failed\n"
            f"{tool_probe.stdout}{tool_probe.stderr}"
        )


def test_managed_runtime_licenses_are_disclosed_with_pinned_sources():
    notices = (FINAL_DIR / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")

    assert "Node.js 24.18.0" in notices
    assert "nodejs/node/blob/v24.18.0/LICENSE" in notices
    assert "Deno 2.9.4" in notices
    assert "Copyright 2018-2026 the Deno authors" in notices
    assert "Permission is hereby granted, free of charge" in notices


def test_managed_ffmpeg_notice_discloses_gpl_source_and_macos_restriction():
    notices = (FINAL_DIR / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    normalized = " ".join(notices.split())

    assert "Gyan FFmpeg 8.1.2 essentials" in notices
    assert "GPL version 3" in notices
    assert "FFmpeg/FFmpeg/commit/38b88335f9" in notices
    assert "approved-ffmpeg.zip" in notices
    assert "Managed FFmpeg download is disabled on macOS" in notices
    assert "for educational purposes only" in normalized
    assert "osxexperts.net" in notices


def test_product_readme_does_not_advertise_mutable_install_commands():
    readme = (FINAL_DIR / "README.md").read_text(encoding="utf-8")

    for forbidden in (
        "pip install -U",
        "docker run",
        "git clone https://",
        "npm install",
    ):
        assert forbidden not in readme
    assert "complete approved hash-locked Python set" in readme
    assert "immutable manifest" in readme


def test_installed_wheel_metadata_declares_reviewed_regex_floor(installed_wheel):
    metadata_path = next(installed_wheel.glob("yt_final-*.dist-info/METADATA"))
    metadata = message_from_string(metadata_path.read_text(encoding="utf-8"))
    requirements = {
        canonicalize_name(requirement.name): requirement
        for requirement in map(Requirement, metadata.get_all("Requires-Dist") or [])
    }

    assert str(requirements["regex"].specifier) == ">=2026.7.19"


def test_installed_wheel_preserves_package_interfaces(installed_wheel):
    probe = _run_isolated_python(
        sys.executable,
        "-c",
        (
            "import sys; "
            f"sys.path.insert(0, {str(installed_wheel)!r}); "
            "import Final; "
            "from Final.cli import main; "
            "assert Final.__version__ == '1.0.0'; "
            "assert callable(main)"
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr


@pytest.mark.parametrize(
    ("launcher_name", "minimum_occurrences"),
    [
        ("Start yt_final.bat", 4),
        ("Start yt_final.command", 1),
    ],
)
def test_launchers_structurally_require_python_3_11(
    launcher_name, minimum_occurrences
):
    launcher = (FINAL_DIR / launcher_name).read_text(encoding="utf-8")
    floors = re.findall(
        r"sys\.version_info\s*>=\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", launcher
    )
    assert len(floors) >= minimum_occurrences
    assert set(floors) == {("3", "11")}


def _write_fake_posix_python(path, minor):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        f"  exit {0 if minor >= 11 else 1}\n"
        "fi\n"
        'printf "%s\\n" "$*" > "$LAUNCH_LOG"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_poison_posix_python(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'printf "executed\\n" > "$MANAGED_PROBE_LOG"\n'
        "exit 97\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _find_posix_bash():
    if os.name == "nt":
        for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Git"
            / "bin"
            / "bash.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Git"
            / "usr"
            / "bin"
            / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash")


@pytest.mark.parametrize(("minor", "accepted"), [(10, False), (11, True)])
def test_macos_launcher_enforces_python_floor(tmp_path, minor, accepted):
    bash = _find_posix_bash()
    if bash is None:
        pytest.skip("bash is unavailable")

    fake_home = tmp_path / "home"
    managed_python = (
        fake_home
        / "Library"
        / "Application Support"
        / "yt_final"
        / "venv"
        / "bin"
        / "python"
    )
    _write_fake_posix_python(managed_python, minor)
    fake_bin = tmp_path / "bin"
    for name in ("python3", "python"):
        _write_fake_posix_python(fake_bin / name, minor)

    launch_log = tmp_path / "launch.log"
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["PATH"] = os.pathsep.join((str(fake_bin), env.get("PATH", "")))
    env["LAUNCH_LOG"] = str(launch_log)
    result = subprocess.run(
        [bash, str(FINAL_DIR / "Start yt_final.command")],
        input="\n",
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert (result.returncode == 0) is accepted
    assert launch_log.exists() is accepted
    if accepted:
        assert launch_log.read_text(encoding="utf-8").strip() == "yt_final.py serve"
    else:
        assert "Python 3.11 or newer was not found" in result.stdout


def test_macos_launcher_never_probes_or_executes_app_data_venv(tmp_path):
    """A poisoned legacy managed interpreter must remain completely untouched."""
    bash = _find_posix_bash()
    if bash is None:
        pytest.skip("bash is unavailable")

    fake_home = tmp_path / "home"
    managed_python = (
        fake_home
        / "Library"
        / "Application Support"
        / "yt_final"
        / "venv"
        / "bin"
        / "python"
    )
    _write_poison_posix_python(managed_python)
    fake_bin = tmp_path / "bin"
    _write_fake_posix_python(fake_bin / "python3", 11)
    launch_log = tmp_path / "launch.log"
    managed_probe_log = tmp_path / "managed-probe.log"
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["PATH"] = os.pathsep.join((str(fake_bin), env.get("PATH", "")))
    env["LAUNCH_LOG"] = str(launch_log)
    env["MANAGED_PROBE_LOG"] = str(managed_probe_log)

    result = subprocess.run(
        [bash, str(FINAL_DIR / "Start yt_final.command")],
        input="\n",
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert launch_log.read_text(encoding="utf-8").strip() == "yt_final.py serve"
    assert not managed_probe_log.exists()


def _write_fake_windows_py(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "@echo off\r\n"
        'if "%~2"=="-c" (\r\n'
        '  if "%FAKE_PY_MINOR%"=="10" exit /b 1\r\n'
        "  exit /b 0\r\n"
        ")\r\n"
        '> "%LAUNCH_LOG%" echo %*\r\n'
        "exit /b 0\r\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(os.name != "nt", reason="requires cmd.exe")
@pytest.mark.parametrize(("minor", "accepted"), [(10, False), (11, True)])
def test_windows_launcher_enforces_python_floor(tmp_path, minor, accepted):
    fake_bin = tmp_path / "bin"
    _write_fake_windows_py(fake_bin / "py.cmd")
    launch_log = tmp_path / "launch.log"
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    env["PATH"] = os.pathsep.join(
        (str(fake_bin), os.environ["SystemRoot"] + "\\System32")
    )
    env["FAKE_PY_MINOR"] = str(minor)
    env["LAUNCH_LOG"] = str(launch_log)
    result = subprocess.run(
        [
            os.environ["COMSPEC"],
            "/d",
            "/c",
            "call",
            str(FINAL_DIR / "Start yt_final.bat"),
        ],
        input="\n",
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert (result.returncode == 0) is accepted
    assert launch_log.exists() is accepted
    if accepted:
        assert launch_log.read_text(encoding="utf-8").strip() == (
            "-3 yt_final.py serve"
        )
    else:
        assert "No usable Python 3.11 or newer found" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="requires cmd.exe")
def test_windows_launcher_never_probes_or_executes_app_data_venv(tmp_path):
    """The Windows launcher must not run a legacy app-data python even to probe it."""
    fake_bin = tmp_path / "bin"
    _write_fake_windows_py(fake_bin / "py.cmd")
    managed_python = (
        tmp_path
        / "local-app-data"
        / "yt_final"
        / "venv"
        / "Scripts"
        / "python.exe"
    )
    managed_python.parent.mkdir(parents=True)
    shutil.copy2(sys.executable, managed_python)
    (managed_python.parents[1] / "pyvenv.cfg").write_text(
        f"home = {sys.base_prefix}\n"
        "include-system-site-packages = false\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n"
        f"executable = {Path(sys.base_prefix) / 'python.exe'}\n",
        encoding="utf-8",
    )
    poison_dir = tmp_path / "poison"
    poison_dir.mkdir()
    managed_probe_log = tmp_path / "managed-probe.log"
    (poison_dir / "sitecustomize.py").write_text(
        "import os\n"
        "with open(os.environ['MANAGED_PROBE_LOG'], 'w', encoding='utf-8') as stream:\n"
        "    stream.write('executed')\n"
        "os._exit(37)\n",
        encoding="utf-8",
    )
    launch_log = tmp_path / "launch.log"
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    env["PATH"] = os.pathsep.join(
        (
            str(fake_bin),
            str(Path(sys.executable).resolve().parent),
            str(Path(sys.base_prefix)),
            os.environ["SystemRoot"] + "\\System32",
        )
    )
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(poison_dir)
    env["MANAGED_PROBE_LOG"] = str(managed_probe_log)
    env["FAKE_PY_MINOR"] = "11"
    env["LAUNCH_LOG"] = str(launch_log)

    subprocess.run(
        [str(managed_python), "-c", "pass"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    if not managed_probe_log.exists():
        pytest.skip("copied Python cannot load the poison fixture on this host")
    managed_probe_log.unlink()

    result = subprocess.run(
        [
            os.environ["COMSPEC"],
            "/d",
            "/c",
            "call",
            str(FINAL_DIR / "Start yt_final.bat"),
        ],
        input="\n",
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert launch_log.read_text(encoding="utf-8").strip() == (
        "-3 yt_final.py serve"
    )
    assert not managed_probe_log.exists()


@pytest.mark.parametrize(
    "reported",
    (
        "",
        "0\n",
        "01\n",
        "+1\n",
        "-1\n",
        " 1\n",
        "1 \n",
        "1\r\n",
        "1\n2\n",
    ),
)
def test_commit_source_date_epoch_rejects_noncanonical_git_output(reported):
    with pytest.raises(AssertionError, match="SOURCE_DATE_EPOCH"):
        _commit_source_date_epoch(reported)


def test_commit_source_date_epoch_accepts_one_strict_decimal_line():
    assert _commit_source_date_epoch("1722124800\n") == 1722124800


def test_phase_b_distribution_build_uses_the_exact_release_command(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    artifacts = tmp_path / "artifacts"
    source.mkdir()
    captured = []

    def build(python, module, *arguments, **options):
        captured.append((python, module, arguments, options))
        artifacts.mkdir()
        (artifacts / "yt_final-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
        (artifacts / "yt_final-1.0.0.tar.gz").write_bytes(b"sdist")
        return subprocess.CompletedProcess(arguments, 0, "built\n", "")

    monkeypatch.setattr(
        sys.modules[__name__],
        "_run_validated_lock_python_module",
        build,
    )
    environment = _release_state_environment(
        tmp_path / "state",
        tmp_path / "cache",
    )
    cwd = Path(environment["HOME"])

    wheel, sdist = _build_python_distributions(
        "C:/reviewed/python.exe",
        source,
        artifacts,
        1722124800,
        environment_updates=environment,
        cwd=cwd,
    )

    assert (wheel.name, sdist.name) == (
        "yt_final-1.0.0-py3-none-any.whl",
        "yt_final-1.0.0.tar.gz",
    )
    assert captured == [
        (
            "C:/reviewed/python.exe",
            "build",
            (
                "--no-isolation",
                "--wheel",
                "--sdist",
                "--outdir",
                str(artifacts),
                str(source),
            ),
            {
                "environment_updates": {
                    **environment,
                    "PIP_NO_INDEX": "1",
                    "SOURCE_DATE_EPOCH": "1722124800",
                },
                "cwd": cwd,
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 300,
            },
        )
    ]


def test_phase_b_twine_checks_only_the_publishable_python_artifacts(
    tmp_path, monkeypatch
):
    wheel = tmp_path / "yt_final-1.0.0-py3-none-any.whl"
    sdist = tmp_path / "yt_final-1.0.0.tar.gz"
    portable = tmp_path / "yt_final-1.0.0-portable.zip"
    for artifact in (wheel, sdist, portable):
        artifact.write_bytes(b"artifact")
    captured = []

    def run(python, module, *arguments, **options):
        captured.append((python, module, arguments, options))
        return subprocess.CompletedProcess(arguments, 0, "PASSED\n", "")

    monkeypatch.setattr(
        sys.modules[__name__],
        "_run_validated_lock_python_module",
        run,
    )
    _twine_check_release(
        "C:/reviewed/release-python.exe",
        wheel,
        sdist,
    )

    assert captured == [
        (
            "C:/reviewed/release-python.exe",
            "twine",
            ("check", str(wheel), str(sdist)),
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 120,
            },
        )
    ]
    assert str(portable) not in captured[0][2]


@pytest.mark.parametrize(
    "member_name",
    (
        "../escape",
        "root/../../escape",
        "/absolute",
        r"C:/escape",
        r"root\escape",
        "root//duplicate-separator",
        "root/./dot",
    ),
)
def test_phase_b_safe_tar_extraction_rejects_unsafe_names(
    tmp_path, member_name
):
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo(member_name)
        body = b"unsafe"
        member.size = len(body)
        stream.addfile(member, io.BytesIO(body))

    with pytest.raises(AssertionError):
        _safe_extract_release_tar(archive, tmp_path / "output")


def test_phase_b_safe_tar_extraction_rejects_links(tmp_path):
    archive = tmp_path / "link.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("root/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../escape"
        stream.addfile(member)

    with pytest.raises(AssertionError, match="regular files and directories"):
        _safe_extract_release_tar(archive, tmp_path / "output")


def test_phase_b_release_toolchain_selects_the_exact_release_lock(
    tmp_path, monkeypatch
):
    captured = []

    def verify(python, **options):
        captured.append((python, options))
        return "C:/reviewed/release-python.exe"

    monkeypatch.setattr(
        sys.modules[__name__],
        "_verified_build_toolchain",
        verify,
    )
    environment = _release_state_environment(
        tmp_path / "state",
        tmp_path / "cache",
    )
    cwd = Path(environment["HOME"])
    assert _verified_release_toolchain(
        "C:/requested/python.exe",
        environment_updates=environment,
        cwd=cwd,
    ) == (
        "C:/reviewed/release-python.exe"
    )
    assert captured == [
        (
            "C:/requested/python.exe",
            {
                "environment_updates": environment,
                "cwd": cwd,
                "lock_path": REQUIREMENTS_DIR / "release.lock",
                "environment_label": "release",
            },
        )
    ]


def test_phase_b_outer_pip_targets_a_pipless_environment(
    tmp_path, monkeypatch
):
    target = tmp_path / "runtime" / "Scripts" / "python.exe"
    home = tmp_path / "home"
    cache = tmp_path / "pip-cache"
    home.mkdir()
    cache.mkdir()
    captured = []

    def run(python, module, *arguments, **options):
        captured.append((python, module, arguments, options))
        return subprocess.CompletedProcess(arguments, 0, "installed\n", "")

    monkeypatch.setattr(
        sys.modules[__name__],
        "_run_validated_lock_python_module",
        run,
    )
    _run_outer_pip(
        "C:/reviewed/release-python.exe",
        target,
        "install",
        "--no-index",
        "artifact.whl",
        environment_updates={
            "HOME": str(home),
            "PIP_CACHE_DIR": str(cache),
        },
        timeout=300,
    )

    assert captured == [
        (
            "C:/reviewed/release-python.exe",
            "pip",
            (
                "--cache-dir",
                str(cache),
                "--python",
                str(target),
                "install",
                "--no-index",
                "artifact.whl",
            ),
            {
                "environment_updates": {
                    "HOME": str(home),
                    "PIP_CACHE_DIR": str(cache),
                },
                "cwd": home,
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 300,
            },
        )
    ]


def test_phase_b_safe_tar_extraction_accepts_regular_files(tmp_path):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        directory = tarfile.TarInfo("yt_final-1.0.0")
        directory.type = tarfile.DIRTYPE
        stream.addfile(directory)
        member = tarfile.TarInfo("yt_final-1.0.0/README.md")
        body = b"reviewed\n"
        member.size = len(body)
        stream.addfile(member, io.BytesIO(body))

    root = _safe_extract_release_tar(
        archive,
        tmp_path / "output",
        require_single_root=True,
    )
    assert root == tmp_path / "output" / "yt_final-1.0.0"
    assert (root / "README.md").read_bytes() == b"reviewed\n"


def test_phase_b_release_state_must_be_absolute_and_outside_workspace(
    tmp_path,
):
    with pytest.raises(AssertionError):
        _release_state_environment(ROOT / "state", tmp_path / "cache")
    with pytest.raises(AssertionError):
        _release_state_environment(tmp_path / "state", ROOT / "pip")
    with pytest.raises(AssertionError):
        _release_state_environment(Path("state"), tmp_path / "cache")

    environment = _release_state_environment(
        tmp_path / "state",
        tmp_path / "cache",
    )
    for name in (
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "PIP_CACHE_DIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    ):
        assert Path(environment[name]).is_absolute()
        assert not _path_is_within_or_equal(environment[name], ROOT)


def test_phase_b_sanitized_python_environment_forces_no_bytecode(
    monkeypatch
):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    assert _sanitized_python_env()["PYTHONDONTWRITEBYTECODE"] == "1"


def _enable_test_controller_bytecode_guard(monkeypatch):
    monkeypatch.setattr(
        sys,
        "flags",
        SimpleNamespace(dont_write_bytecode=1),
    )
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


def test_phase_b_controller_evidence_context_requires_external_state(
    tmp_path, monkeypatch
):
    _enable_test_controller_bytecode_guard(monkeypatch)
    cache = tmp_path / "pytest-cache"
    scratch, environment, cwd = _controller_evidence_context(
        tmp_path,
        cache,
    )
    assert scratch == tmp_path.resolve()
    assert cwd.is_absolute() and cwd.is_dir()
    assert not _path_is_within_or_equal(cwd, ROOT)
    for name in (
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    ):
        assert name in environment
        assert Path(environment[name]).is_absolute()
        assert not _path_is_within_or_equal(environment[name], ROOT)

    assert _controller_evidence_context(tmp_path, None)[0] == scratch

    with monkeypatch.context() as changed:
        changed.setattr(sys, "executable", str(ROOT / "python.exe"))
        with pytest.raises(AssertionError, match="controller interpreter"):
            _controller_evidence_context(tmp_path, cache)

    with pytest.raises(AssertionError, match="scratch"):
        _controller_evidence_context(ROOT, cache)
    with pytest.raises(AssertionError, match="pytest cache"):
        _controller_evidence_context(tmp_path, ROOT / ".pytest_cache")

    with monkeypatch.context() as changed:
        changed.setattr(tempfile, "gettempdir", lambda: str(ROOT / "temp"))
        with pytest.raises(AssertionError, match="temporary"):
            _controller_evidence_context(tmp_path, cache)

    with monkeypatch.context() as changed:
        changed.setattr(
            sys,
            "flags",
            SimpleNamespace(dont_write_bytecode=0),
        )
        with pytest.raises(AssertionError, match="bytecode"):
            _controller_evidence_context(tmp_path, cache)


@pytest.mark.parametrize(
    "name",
    (
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    ),
)
def test_phase_b_controller_rejects_workspace_parent_state(
    tmp_path, monkeypatch, name
):
    _enable_test_controller_bytecode_guard(monkeypatch)
    monkeypatch.setenv(name, str(ROOT / "ambient-controller" / name))
    with pytest.raises(AssertionError, match="parent state"):
        _controller_evidence_context(tmp_path, None)


def test_phase_b_controller_requires_runtime_bytecode_guard(
    tmp_path, monkeypatch
):
    _enable_test_controller_bytecode_guard(monkeypatch)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    with pytest.raises(AssertionError, match="bytecode"):
        _controller_evidence_context(tmp_path, None)


def test_phase_b_evidence_runtime_cannot_inherit_workspace_state(
    tmp_path, monkeypatch
):
    poisoned_names = (
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "PYTHONUSERBASE",
        "PIP_CONFIG_FILE",
        "PIP_INDEX_URL",
    )
    for name in poisoned_names:
        monkeypatch.setenv(name, str(ROOT / "ambient-state" / name))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")

    environment = _release_state_environment(
        tmp_path / "controller-state",
        tmp_path / "pip-cache",
    )
    cwd = Path(environment["HOME"])
    captured = _run_isolated_python(
        sys.executable,
        "-c",
        "import json, os; print(json.dumps(dict(os.environ)))",
        environment_updates=environment,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert captured.returncode == 0, captured.stdout + captured.stderr
    actual = json.loads(captured.stdout)
    assert actual["PYTHONDONTWRITEBYTECODE"] == "1"
    assert actual["PIP_CONFIG_FILE"] == os.devnull
    for name in poisoned_names:
        if name in environment:
            assert actual[name] == environment[name]
        elif name == "PIP_CONFIG_FILE":
            assert actual[name] == os.devnull
        else:
            assert name not in actual
    assert all(
        not _path_is_within_or_equal(value, ROOT)
        for name, value in actual.items()
        if name.upper()
        in {
            "APPDATA",
            "HOME",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            "TMPDIR",
            "USERPROFILE",
        }
        or name.upper().startswith("XDG_")
    )


def test_phase_b_evidence_helpers_require_environment_and_external_cwd():
    for helper in (
        _verified_build_toolchain,
        _verified_release_toolchain,
        _run_release_git,
        _release_head_identity,
        _archive_clean_head_source,
        _workspace_write_snapshot,
        _build_python_distributions,
        _build_portable_and_checksums,
        _rebuild_wheel_from_sdist,
        _create_pipless_environment,
    ):
        signature = inspect.signature(helper)
        for name in ("environment_updates", "cwd"):
            parameter = signature.parameters[name]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is inspect.Parameter.empty


def test_phase_b_every_nested_evidence_call_forwards_environment_and_cwd():
    edges = (
        (_verified_build_toolchain, "_run_validated_lock_python"),
        (_verified_release_toolchain, "_verified_build_toolchain"),
        (_release_head_identity, "_run_release_git"),
        (_archive_clean_head_source, "_run_release_git"),
        (
            _build_python_distributions,
            "_run_validated_lock_python_module",
        ),
        (_create_pipless_environment, "_run_validated_lock_python_module"),
        (_install_locked_runtime_environment, "_create_pipless_environment"),
        (_nul_separated_git_paths, "_run_release_git"),
        (_workspace_write_snapshot, "_run_release_git"),
        (_workspace_write_snapshot, "_nul_separated_git_paths"),
        (_build_portable_and_checksums, "_run_validated_lock_python"),
        (_create_locked_build_environment, "_create_pipless_environment"),
        (_create_locked_build_environment, "_verified_build_toolchain"),
        (_rebuild_wheel_from_sdist, "_run_validated_lock_python_module"),
        (
            test_phase_b_clean_distribution_build_and_external_install,
            "_workspace_write_snapshot",
        ),
        (
            test_phase_b_clean_distribution_build_and_external_install,
            "_verified_build_toolchain",
        ),
        (
            test_phase_b_clean_distribution_build_and_external_install,
            "_verified_release_toolchain",
        ),
        (
            test_phase_b_clean_distribution_build_and_external_install,
            "_release_head_identity",
        ),
        (
            test_phase_b_clean_distribution_build_and_external_install,
            "_archive_clean_head_source",
        ),
        (
            test_phase_b_clean_distribution_build_and_external_install,
            "_build_python_distributions",
        ),
        (
            test_phase_b_clean_distribution_build_and_external_install,
            "_build_portable_and_checksums",
        ),
        (
            test_phase_b_clean_distribution_build_and_external_install,
            "_rebuild_wheel_from_sdist",
        ),
    )
    for caller, callee_name in edges:
        tree = ast.parse(textwrap.dedent(inspect.getsource(caller)))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == callee_name
        ]
        assert calls, f"{caller.__name__} must call {callee_name}"
        for call in calls:
            keywords = {
                keyword.arg: keyword.value for keyword in call.keywords
            }
            assert {"environment_updates", "cwd"} <= set(keywords), (
                f"{caller.__name__} must forward environment_updates and cwd "
                f"to every {callee_name} call"
            )
            expected_environment_name = (
                "controller_environment"
                if caller
                is test_phase_b_clean_distribution_build_and_external_install
                else "environment_updates"
            )
            expected_cwd_name = (
                "controller_cwd"
                if caller
                is test_phase_b_clean_distribution_build_and_external_install
                else (
                    "environment_updates"
                    if caller
                    in {
                        _install_locked_runtime_environment,
                        _create_locked_build_environment,
                    }
                    else "cwd"
                )
            )
            environment_names = {
                node.id
                for node in ast.walk(keywords["environment_updates"])
                if isinstance(node, ast.Name)
            }
            cwd_names = {
                node.id
                for node in ast.walk(keywords["cwd"])
                if isinstance(node, ast.Name)
            }
            assert expected_environment_name in environment_names
            assert expected_cwd_name in cwd_names


def test_phase_b_build_toolchain_rejects_an_interpreter_inside_workspace(
    tmp_path, monkeypatch
):
    inside = str(ROOT / ".artifacts" / "poison" / "python.exe")
    monkeypatch.setattr(
        sys.modules[__name__],
        "_validated_lock_python",
        lambda _candidate: inside,
    )

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("inside-workspace interpreter was executed")

    monkeypatch.setattr(
        sys.modules[__name__],
        "_run_validated_lock_python",
        unexpected_probe,
    )
    with pytest.raises(AssertionError, match="outside"):
        _verified_build_toolchain(
            inside,
            environment_updates=_release_state_environment(
                tmp_path / "state",
                tmp_path / "cache",
            ),
            cwd=tmp_path,
        )


def test_phase_b_workspace_snapshot_detects_ignored_cache_mutation(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    initialized = subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert initialized.returncode == 0, initialized.stderr
    (repository / ".gitignore").write_text(
        ".review-cache/\n",
        encoding="utf-8",
    )
    environment = _release_state_environment(
        tmp_path / "state",
        tmp_path / "cache",
    )
    cwd = Path(environment["HOME"])
    monkeypatch.setattr(sys.modules[__name__], "ROOT", repository)
    cache_root = repository / ".review-cache"
    mutation_root = cache_root / "phase-b-review-mutation"
    mutation = mutation_root / "poison.bin"
    before = _workspace_write_snapshot(
        environment_updates=environment,
        cwd=cwd,
    )
    try:
        mutation_root.mkdir(parents=True)
        mutation.write_bytes(b"ignored cache mutation\n")
        during = _workspace_write_snapshot(
            environment_updates=environment,
            cwd=cwd,
        )
        relative = mutation.relative_to(ROOT).as_posix()
        assert during != before
        assert relative in during["ignored"]
        evidence = during["ignored"][relative]
        assert evidence["sha256"] == hashlib.sha256(
            b"ignored cache mutation\n"
        ).hexdigest()
        assert len(evidence["identity"]) == 5
    finally:
        if cache_root.exists():
            shutil.rmtree(cache_root)
    assert _workspace_write_snapshot(
        environment_updates=environment,
        cwd=cwd,
    ) == before


def test_phase_b_workspace_snapshot_hashes_already_dirty_tracked_files(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    initialized = subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert initialized.returncode == 0, initialized.stderr
    tracked = repository / "tracked.txt"
    tracked.write_bytes(b"committed\n")
    for command in (
        ["git", "-C", str(repository), "add", "tracked.txt"],
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Phase B",
            "-c",
            "user.email=phase-b.invalid",
            "commit",
            "--quiet",
            "-m",
            "baseline",
        ],
    ):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    environment = _release_state_environment(
        tmp_path / "state",
        tmp_path / "cache",
    )
    cwd = Path(environment["HOME"])
    monkeypatch.setattr(sys.modules[__name__], "ROOT", repository)
    tracked.write_bytes(b"dirty-one\n")
    before = _workspace_write_snapshot(
        environment_updates=environment,
        cwd=cwd,
    )
    tracked.write_bytes(b"dirty-two\n")
    after = _workspace_write_snapshot(
        environment_updates=environment,
        cwd=cwd,
    )

    assert before["status"] == after["status"]
    assert before["reviewed"]["tracked.txt"]["sha256"] != (
        after["reviewed"]["tracked.txt"]["sha256"]
    )
    assert before != after


def test_phase_b_controller_cache_must_be_outside_workspace(tmp_path):
    assert _assert_external_controller_cache(None) is None
    with pytest.raises(AssertionError, match="pytest cache"):
        _assert_external_controller_cache(ROOT / ".pytest_cache")
    assert _assert_external_controller_cache(tmp_path / "pytest-cache") is None


def test_phase_b_release_git_strips_every_ambient_git_variable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GIT_DIR", "C:/poison/repository.git")
    monkeypatch.setenv("gIt_WoRk_TrEe", "C:/poison/worktree")
    monkeypatch.setenv("Git_Object_Directory", "C:/poison/objects")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    captured = []

    def run(command, **options):
        captured.append((command, options))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    environment = _release_state_environment(
        tmp_path / "state",
        tmp_path / "cache",
    )
    cwd = Path(environment["HOME"])
    _run_release_git(
        "status",
        "--porcelain=v1",
        environment_updates=environment,
        cwd=cwd,
    )

    assert len(captured) == 1
    command, options = captured[0]
    assert command[:3] == ["git", "-C", str(ROOT)]
    assert options["cwd"] == cwd
    assert {
        name: value
        for name, value in options["env"].items()
        if name.upper().startswith("GIT_")
    } == {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def test_phase_b_release_git_ignores_a_poisoned_other_repository(
    tmp_path, monkeypatch
):
    environment = _release_state_environment(
        tmp_path / "state",
        tmp_path / "cache",
    )
    cwd = Path(environment["HOME"])
    expected_head = _run_release_git(
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        environment_updates=environment,
        cwd=cwd,
    )
    expected_status = _run_release_git(
        "status",
        "--porcelain=v1",
        "-z",
        environment_updates=environment,
        cwd=cwd,
    )
    assert expected_head.returncode == expected_status.returncode == 0

    other = tmp_path / "other-repository"
    initialized = subprocess.run(
        ["git", "init", "--quiet", str(other)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert initialized.returncode == 0, initialized.stderr
    (other / "poison.txt").write_text("other repository\n", encoding="utf-8")
    for command in (
        ["git", "-C", str(other), "add", "poison.txt"],
        [
            "git",
            "-C",
            str(other),
            "-c",
            "user.name=Phase B",
            "-c",
            "user.email=phase-b.invalid",
            "commit",
            "--quiet",
            "-m",
            "poison",
        ],
    ):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    poison = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git" / "objects"),
        "GIT_COMMON_DIR": str(other / ".git"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.bare",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_DIR": str(other / ".git"),
        "GIT_INDEX_FILE": str(other / ".git" / "index"),
        "GIT_NAMESPACE": "poison",
        "GIT_OBJECT_DIRECTORY": str(other / ".git" / "objects"),
        "GIT_WORK_TREE": str(other),
    }
    for name, value in poison.items():
        monkeypatch.setenv(name, value)

    actual_head = _run_release_git(
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        environment_updates=environment,
        cwd=cwd,
    )
    actual_status = _run_release_git(
        "status",
        "--porcelain=v1",
        "-z",
        environment_updates=environment,
        cwd=cwd,
    )
    archive = tmp_path / "reviewed-head.tar"
    archived = _run_release_git(
        "archive",
        "--format=tar",
        f"--output={archive}",
        expected_head.stdout.strip(),
        environment_updates=environment,
        cwd=cwd,
    )

    assert actual_head.returncode == 0, actual_head.stderr
    assert actual_head.stdout == expected_head.stdout
    assert actual_status.returncode == 0, actual_status.stderr
    assert actual_status.stdout == expected_status.stdout
    assert archived.returncode == 0, archived.stderr
    extracted = _safe_extract_release_tar(
        archive,
        tmp_path / "reviewed-head",
    )
    assert (extracted / "pyproject.toml").is_file()
    assert not (extracted / "poison.txt").exists()


def _load_reviewed_setup_module(monkeypatch):
    import importlib.util
    import setuptools

    setup_calls = []
    monkeypatch.setattr(
        setuptools,
        "setup",
        lambda **keywords: setup_calls.append(keywords),
    )
    spec = importlib.util.spec_from_file_location(
        "_yt_final_reviewed_setup",
        ROOT / "setup.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(setup_calls) == 1
    return module, setup_calls[0]


@pytest.mark.skipif(os.name != "nt", reason="Windows metadata contract")
def test_phase_b_windows_batch_handle_and_external_pep517_sdist(
    tmp_path, monkeypatch, pytestconfig
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    launcher = tmp_path / "Start yt_final.bat"
    launcher_body = b"@echo off\r\nexit /b 0\r\n"
    launcher.write_bytes(launcher_body)
    path_metadata = launcher.lstat()
    descriptor = os.open(
        launcher,
        os.O_RDONLY | getattr(os, "O_BINARY", 0),
    )
    try:
        handle_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    assert stat.S_IFMT(path_metadata.st_mode) == stat.S_IFMT(
        handle_metadata.st_mode
    )
    assert setup_module._capture_regular_file(
        launcher,
        path_metadata,
    ) == launcher_body

    requested_build_python = os.environ.get("YT_FINAL_BUILD_PYTHON")
    if not requested_build_python:
        pytest.skip("YT_FINAL_BUILD_PYTHON is required for the PEP 517 smoke")
    pytest_cache = getattr(pytestconfig, "cache", None)
    scratch, environment, cwd = _controller_evidence_context(
        tmp_path,
        getattr(pytest_cache, "_cachedir", None),
    )
    build_python = _verified_build_toolchain(
        requested_build_python,
        environment_updates=environment,
        cwd=cwd,
    )
    version = _run_validated_lock_python(
        build_python,
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        environment_updates=environment,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert version.returncode == 0, version.stdout + version.stderr
    if version.stdout != "3.11\n":
        pytest.skip("the path/fd regression requires the locked Python 3.11")

    source = scratch / "pep517-batch-source"
    package = source / "Final"
    package.mkdir(parents=True)
    (source / "setup.py").write_bytes((ROOT / "setup.py").read_bytes())
    (source / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools==83.0.0"]\n'
        'build-backend = "setuptools.build_meta"\n',
        encoding="utf-8",
        newline="\n",
    )
    (source / "setup.cfg").write_text(
        "[metadata]\n"
        "name = yt-final-pep517-smoke\n"
        "version = 1.0.0\n"
        "\n"
        "[options]\n"
        "packages = find:\n"
        "include_package_data = True\n"
        "\n"
        "[options.package_data]\n"
        "Final =\n"
        "    *.bat\n",
        encoding="utf-8",
        newline="\n",
    )
    (package / "__init__.py").write_bytes(b"")
    (package / "Start yt_final.bat").write_bytes(launcher_body)
    artifacts = scratch / "pep517-batch-artifacts"
    built = _run_validated_lock_python_module(
        build_python,
        "build",
        "--no-isolation",
        "--sdist",
        "--outdir",
        str(artifacts),
        str(source),
        environment_updates={
            **environment,
            "PIP_NO_INDEX": "1",
            "SOURCE_DATE_EPOCH": "1722124800",
        },
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    sdists = tuple(artifacts.glob("*.tar.gz"))
    assert len(sdists) == 1
    with tarfile.open(sdists[0], "r:gz") as archive:
        assert any(
            member.name.endswith("/Final/Start yt_final.bat")
            for member in archive.getmembers()
        )


def _create_real_windows_junction(link, target):
    created = subprocess.run(
        [
            os.environ["COMSPEC"],
            "/d",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    metadata = Path(link).lstat()
    assert getattr(metadata, "st_file_attributes", 0) & (
        stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction contract")
@pytest.mark.parametrize("junction_role", ("source", "publication"))
@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_rejects_real_ntfs_junctions(
    tmp_path, monkeypatch, junction_role
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    external = Path(tempfile.mkdtemp(prefix="yt-final-junction-target-"))
    junction = (
        source / "linked-source"
        if junction_role == "source"
        else tmp_path / "publication-parent"
    )
    if junction_role == "source":
        (external / "outside.txt").write_bytes(b"outside\n")
        destination = tmp_path / "yt_final-1.0.0.tar.gz"
    else:
        destination = junction / "yt_final-1.0.0.tar.gz"

    try:
        _create_real_windows_junction(junction, external)
        assert _workspace_path_evidence(junction)["kind"] == "link"
        expected = (
            "sdist source links"
            if junction_role == "source"
            else "publication parent"
        )
        with pytest.raises(ValueError, match=expected):
            setup_module._write_reproducible_gztar(
                source,
                destination,
                "yt_final-1.0.0",
                1722124800,
            )
    finally:
        if os.path.lexists(junction):
            os.rmdir(junction)
        shutil.rmtree(external)


@pytest.mark.skipif(os.name != "nt", reason="Windows hardlink contract")
@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_rejects_real_outside_hardlink_swap(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    member = source / "README.md"
    original = b"reviewed-one\n"
    poisoned = b"attacker-two\n"
    assert len(original) == len(poisoned)
    member.write_bytes(original)
    external = Path(tempfile.mkdtemp(prefix="yt-final-hardlink-alias-"))
    alias = external / "outside-alias.txt"
    try:
        os.link(member, alias)
        reviewed = member.lstat()
        assert reviewed.st_nlink == 2
        alias.write_bytes(poisoned)
        os.utime(
            alias,
            ns=(reviewed.st_atime_ns, reviewed.st_mtime_ns),
        )
        current = member.lstat()
        assert member.read_bytes() == poisoned
        assert (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        ) == (
            reviewed.st_dev,
            reviewed.st_ino,
            reviewed.st_size,
            reviewed.st_mtime_ns,
            reviewed.st_ctime_ns,
        )
        with pytest.raises(ValueError, match="source file link count"):
            setup_module._capture_regular_file(member, reviewed)
    finally:
        shutil.rmtree(external)


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_writer_normalizes_time_identity_and_bytes(
    tmp_path, monkeypatch
):
    setup_module, setup_keywords = _load_reviewed_setup_module(monkeypatch)
    assert setup_keywords["cmdclass"]["sdist"] is setup_module.ReleaseSdist

    source = tmp_path / "source"
    nested = source / "nested"
    launcher_dir = source / "Final"
    nested.mkdir(parents=True)
    launcher_dir.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    (nested / "module.py").write_bytes(b"VALUE = 1\n")
    launcher = launcher_dir / "Start yt_final.command"
    unrelated_executable = source / "ambient-executable"
    launcher.write_bytes(b"#!/bin/sh\n")
    unrelated_executable.write_bytes(b"ambient\n")
    launcher.chmod(0o600)
    unrelated_executable.chmod(0o777)
    epoch = 1722124800
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    setup_module._write_reproducible_gztar(
        source,
        first,
        "yt_final-1.0.0",
        epoch,
    )
    for path in (
        source,
        nested,
        launcher_dir,
        source / "README.md",
        nested / "module.py",
        launcher,
        unrelated_executable,
    ):
        os.utime(path, (epoch + 777, epoch + 777))
    launcher.chmod(0o777)
    unrelated_executable.chmod(0o600)
    setup_module._write_reproducible_gztar(
        source,
        second,
        "yt_final-1.0.0",
        epoch,
    )

    first_bytes = first.read_bytes()
    assert first_bytes == second.read_bytes()
    assert first_bytes[:3] == b"\x1f\x8b\x08"
    assert first_bytes[3] & 0x08 == 0, (
        "reproducible gzip headers must not embed an output filename"
    )
    assert struct.unpack("<I", first_bytes[4:8])[0] == epoch
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "yt_final-1.0.0",
            "yt_final-1.0.0/Final",
            "yt_final-1.0.0/Final/Start yt_final.command",
            "yt_final-1.0.0/README.md",
            "yt_final-1.0.0/ambient-executable",
            "yt_final-1.0.0/nested",
            "yt_final-1.0.0/nested/module.py",
        ]
        assert all(member.mtime == epoch for member in members)
        assert all(member.uid == 0 and member.gid == 0 for member in members)
        assert all(member.uname == "" and member.gname == "" for member in members)
        assert {
            member.name: member.mode for member in members
        } == {
            "yt_final-1.0.0": 0o755,
            "yt_final-1.0.0/Final": 0o755,
            "yt_final-1.0.0/Final/Start yt_final.command": 0o755,
            "yt_final-1.0.0/README.md": 0o644,
            "yt_final-1.0.0/ambient-executable": 0o644,
            "yt_final-1.0.0/nested": 0o755,
            "yt_final-1.0.0/nested/module.py": 0o644,
        }
        assert all(member.pax_headers == {} for member in members)
        assert archive.extractfile(
            "yt_final-1.0.0/README.md"
        ).read() == b"reviewed\n"
        assert archive.extractfile(
            "yt_final-1.0.0/nested/module.py"
        ).read() == b"VALUE = 1\n"


def _write_synthetic_sdist_gate_fixture(
    destination,
    source,
    epoch,
    *,
    order,
    pax_member=None,
):
    with Path(destination).open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            mtime=epoch,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w|",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                root = tarfile.TarInfo("yt_final-1.0.0")
                root.type = tarfile.DIRTYPE
                root.mode = 0o755
                root.mtime = epoch
                root.uid = root.gid = 0
                root.uname = root.gname = ""
                archive.addfile(root)
                for relative in order:
                    body = (Path(source) / relative).read_bytes()
                    member = tarfile.TarInfo(
                        f"yt_final-1.0.0/{relative}"
                    )
                    member.mode = 0o644
                    member.mtime = epoch
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    member.size = len(body)
                    if relative == pax_member:
                        member.pax_headers = {"comment": "poison"}
                    archive.addfile(member, io.BytesIO(body))


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_gate_rejects_source_content_mismatch(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    readme = source / "README.md"
    readme.write_bytes(b"reviewed\n")
    archive = tmp_path / "release.tar.gz"
    epoch = 1722124800
    setup_module._write_reproducible_gztar(
        source,
        archive,
        "yt_final-1.0.0",
        epoch,
    )

    with pytest.raises(AssertionError, match="content mismatch"):
        _assert_reproducible_sdist(
            archive,
            source,
            epoch,
            source_member_bytes={"README.md": b"different reviewed source\n"},
            generated_members={},
        )


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_gate_rejects_build_mutated_source(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    readme = source / "README.md"
    readme.write_bytes(b"reviewed immutable source\n")
    immutable_source_members = {"README.md": readme.read_bytes()}
    readme.write_bytes(b"mutated by build process!\n")
    archive = tmp_path / "release.tar.gz"
    epoch = 1722124800
    setup_module._write_reproducible_gztar(
        source,
        archive,
        "yt_final-1.0.0",
        epoch,
    )

    with pytest.raises(AssertionError, match="content mismatch"):
        _assert_reproducible_sdist(
            archive,
            source,
            epoch,
            source_member_bytes=immutable_source_members,
            generated_members={},
        )


def test_phase_b_sdist_gate_rejects_noncanonical_full_member_order(
    tmp_path
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a\n")
    (source / "b.txt").write_bytes(b"b\n")
    archive = tmp_path / "wrong-order.tar.gz"
    epoch = 1722124800
    _write_synthetic_sdist_gate_fixture(
        archive,
        source,
        epoch,
        order=("b.txt", "a.txt"),
    )

    with pytest.raises(AssertionError, match="member order"):
        _assert_reproducible_sdist(
            archive,
            source,
            epoch,
            source_member_bytes={
                "a.txt": b"a\n",
                "b.txt": b"b\n",
            },
            generated_members={},
        )


def test_phase_b_sdist_gate_rejects_member_pax_headers(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    archive = tmp_path / "pax.tar.gz"
    epoch = 1722124800
    _write_synthetic_sdist_gate_fixture(
        archive,
        source,
        epoch,
        order=("README.md",),
        pax_member="README.md",
    )

    with pytest.raises(AssertionError, match="PAX"):
        _assert_reproducible_sdist(
            archive,
            source,
            epoch,
            source_member_bytes={"README.md": b"reviewed\n"},
            generated_members={},
        )


@pytest.mark.parametrize(
    "value",
    ("", "01", "+1", "-1", " 1", "1 ", "1\n", "not-decimal"),
)
@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_rejects_noncanonical_source_date_epoch(
    value, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", value)
    with pytest.raises(ValueError, match="SOURCE_DATE_EPOCH"):
        setup_module._source_date_epoch()


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_rejects_completed_temp_path_substitution(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    destination.write_bytes(b"prior release")
    reserved = []
    real_mkstemp = setup_module.tempfile.mkstemp
    real_fdopen = setup_module.os.fdopen
    real_replace = setup_module.os.replace

    def reserve(**options):
        descriptor, name = real_mkstemp(**options)
        reserved.append(Path(name))
        return descriptor, name

    class SwapAfterClose:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self.stream.__enter__()

        def __exit__(self, *exception):
            result = self.stream.__exit__(*exception)
            if exception[0] is None:
                temporary = reserved[-1]
                reviewed = temporary.with_name(f"{temporary.name}.reviewed")
                real_replace(temporary, reviewed)
                temporary.write_bytes(b"substituted publication bytes")
            return result

    def fdopen(*arguments, **options):
        return SwapAfterClose(real_fdopen(*arguments, **options))

    monkeypatch.setattr(setup_module.tempfile, "mkstemp", reserve)
    monkeypatch.setattr(setup_module.os, "fdopen", fdopen)

    with pytest.raises(ValueError, match="publication file identity"):
        setup_module._write_reproducible_gztar(
            source,
            destination,
            "yt_final-1.0.0",
            1722124800,
        )

    assert destination.read_bytes() == b"prior release"
    assert list(tmp_path.glob(f".{destination.name}.publish-*")) == []


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_rejects_same_length_source_swap_before_open(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    member = source / "README.md"
    member.write_bytes(b"reviewed-one\n")
    metadata = member.stat()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"attacker-two\n")
    assert replacement.stat().st_size == metadata.st_size
    os.utime(
        replacement,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
    )
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    destination.write_bytes(b"prior release")
    real_open = setup_module.os.open
    swapped = []

    def racing_open(path, *arguments, **options):
        if Path(path) == member and not swapped:
            os.replace(replacement, member)
            swapped.append(True)
        return real_open(path, *arguments, **options)

    monkeypatch.setattr(setup_module.os, "open", racing_open)
    with pytest.raises(ValueError, match="source file identity"):
        setup_module._write_reproducible_gztar(
            source,
            destination,
            "yt_final-1.0.0",
            1722124800,
        )
    assert swapped == [True]
    assert destination.read_bytes() == b"prior release"


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_rejects_directory_inventory_change_during_capture(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    member = source / "README.md"
    member.write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    real_open = setup_module.os.open
    changed = []

    def racing_open(path, *arguments, **options):
        descriptor = real_open(path, *arguments, **options)
        if Path(path) == member and not changed:
            (source / "late.txt").write_bytes(b"late\n")
            changed.append(True)
        return descriptor

    monkeypatch.setattr(setup_module.os, "open", racing_open)
    with pytest.raises(ValueError, match="source directory inventory"):
        setup_module._write_reproducible_gztar(
            source,
            destination,
            "yt_final-1.0.0",
            1722124800,
        )
    assert changed == [True]
    assert not destination.exists()


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_publication_mode_and_fsync_order(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    destination.write_bytes(b"prior release")
    events = []
    publication_directories = []
    parent_descriptor = 987654
    real_mkdtemp = setup_module.tempfile.mkdtemp
    real_open = setup_module.os.open
    real_fsync = setup_module.os.fsync
    real_close = setup_module.os.close
    real_replace = setup_module.os.replace
    windows_flags = []

    def private_directory(**options):
        path = Path(real_mkdtemp(**options))
        publication_directories.append(path)
        assert path.parent == destination.parent
        assert stat.S_ISDIR(path.lstat().st_mode)
        return str(path)

    def open_path(path, *arguments, **options):
        if Path(path) == destination.parent:
            events.append("parent-open")
            return parent_descriptor
        return real_open(path, *arguments, **options)

    def fsync(descriptor):
        if descriptor == parent_descriptor:
            events.append("parent-fsync")
            return None
        events.append("file-fsync")
        return real_fsync(descriptor)

    def close(descriptor):
        if descriptor == parent_descriptor:
            events.append("parent-close")
            return None
        return real_close(descriptor)

    def replace(source_path, destination_path):
        assert stat.S_IMODE(Path(source_path).lstat().st_mode) == 0o644
        events.append("replace")
        return real_replace(source_path, destination_path)

    class MoveFile:
        argtypes = None
        restype = None

        def __call__(self, source_path, destination_path, flags):
            temporary = Path(source_path)
            assert temporary.parent in publication_directories
            assert temporary.stat().st_mode & stat.S_IWRITE
            windows_flags.append(flags)
            events.append("replace")
            real_replace(source_path, destination_path)
            return 1

    class Kernel32:
        MoveFileExW = MoveFile()

    monkeypatch.setattr(setup_module.tempfile, "mkdtemp", private_directory)
    monkeypatch.setattr(setup_module.os, "open", open_path)
    monkeypatch.setattr(setup_module.os, "fsync", fsync)
    monkeypatch.setattr(setup_module.os, "close", close)
    if setup_module.os.name == "nt":
        monkeypatch.setattr(
            setup_module.ctypes,
            "WinDLL",
            lambda *_args, **_kwargs: Kernel32(),
        )
    else:
        monkeypatch.setattr(setup_module.os, "replace", replace)

    setup_module._write_reproducible_gztar(
        source,
        destination,
        "yt_final-1.0.0",
        1722124800,
    )
    assert publication_directories
    assert events.index("file-fsync") < events.index("replace")
    if setup_module.os.name == "nt":
        assert windows_flags == [0x1 | 0x8]
        assert "parent-fsync" not in events
    else:
        assert events.index("replace") < events.index("parent-fsync")
        assert events[-1] == "parent-close"
    assert destination.read_bytes() != b"prior release"
    assert not any(path.exists() for path in publication_directories)


@pytest.mark.parametrize("cleanup_fault", ("close", "unlink"))
@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_cleanup_preserves_primary_failure(
    tmp_path, monkeypatch, cleanup_fault
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    destination.write_bytes(b"prior release")
    real_close = setup_module.os.close
    real_mkstemp = setup_module.tempfile.mkstemp
    real_unlink = Path.unlink
    cleanup_attempted = []
    publication_descriptors = []

    monkeypatch.setattr(
        setup_module.os,
        "fdopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("forced primary failure")
        ),
    )
    if cleanup_fault == "close":
        def reserve(**options):
            descriptor, name = real_mkstemp(**options)
            publication_descriptors.append(descriptor)
            return descriptor, name

        def close_then_fail(descriptor):
            real_close(descriptor)
            if descriptor in publication_descriptors:
                raise OSError("forced close cleanup failure")

        def observe_unlink(path, *arguments, **options):
            cleanup_attempted.append(Path(path))
            return real_unlink(path, *arguments, **options)

        monkeypatch.setattr(setup_module.tempfile, "mkstemp", reserve)
        monkeypatch.setattr(setup_module.os, "close", close_then_fail)
        monkeypatch.setattr(Path, "unlink", observe_unlink)
    else:
        def fail_unlink(path, *_arguments, **_options):
            cleanup_attempted.append(Path(path))
            raise OSError("forced unlink cleanup failure")

        monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(OSError, match="forced primary failure") as failure:
        setup_module._write_reproducible_gztar(
            source,
            destination,
            "yt_final-1.0.0",
            1722124800,
        )
    assert cleanup_attempted
    assert any(
        "cleanup" in note.lower()
        for note in getattr(failure.value, "__notes__", ())
    )
    assert destination.read_bytes() == b"prior release"


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_surfaces_sole_cleanup_failure(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"

    def fail_unlink(path, *_arguments, **_options):
        raise OSError("forced sole cleanup failure")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(OSError, match="forced sole cleanup failure"):
        setup_module._write_reproducible_gztar(
            source,
            destination,
            "yt_final-1.0.0",
            1722124800,
        )
    assert destination.is_file()
    assert list(tmp_path.glob(f".{destination.name}.publish-*")) == []


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_parent_fsync_failure_is_not_suppressed(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    monkeypatch.setattr(
        setup_module.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("forced parent open failure")
        ),
    )
    with pytest.raises(OSError, match="forced parent open failure"):
        setup_module._fsync_parent_directory(tmp_path)


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_source_close_cannot_mask_capture_failure(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    member = source / "README.md"
    member.write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    source_descriptors = set()
    real_open = setup_module.os.open
    real_close = setup_module.os.close

    def open_source(path, *arguments, **options):
        descriptor = real_open(path, *arguments, **options)
        if Path(path) == member:
            source_descriptors.add(descriptor)
        return descriptor

    def close_then_fail(descriptor):
        real_close(descriptor)
        if descriptor in source_descriptors:
            raise OSError("forced source close failure")

    real_read = setup_module.os.read

    def controlled_read(descriptor, size):
        if descriptor in source_descriptors:
            raise OSError("forced source read failure")
        return real_read(descriptor, size)

    monkeypatch.setattr(setup_module.os, "open", open_source)
    monkeypatch.setattr(setup_module.os, "read", controlled_read)
    monkeypatch.setattr(setup_module.os, "close", close_then_fail)
    with pytest.raises(OSError, match="forced source read failure") as failure:
        setup_module._write_reproducible_gztar(
            source,
            destination,
            "yt_final-1.0.0",
            1722124800,
        )
    assert any(
        "source close" in note.lower()
        for note in getattr(failure.value, "__notes__", ())
    )
    assert not destination.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode/ownership contract")
@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_rejects_writable_publication_parent(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(ValueError, match="publication parent"):
            setup_module._write_reproducible_gztar(
                source,
                destination,
                "yt_final-1.0.0",
                1722124800,
            )
    finally:
        tmp_path.chmod(0o700)


@pytest.mark.parametrize(
    "failure_point",
    ("addfile", "close", "replace"),
)
@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_publication_preserves_prior_on_every_failure(
    tmp_path, monkeypatch, failure_point
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    destination.write_bytes(b"prior release")

    if failure_point == "addfile":
        monkeypatch.setattr(
            setup_module.tarfile.TarFile,
            "addfile",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("forced addfile failure")
            ),
        )
    elif failure_point == "close":
        original_close = setup_module.gzip.GzipFile.close
        close_calls = []

        def fail_first_close(archive):
            close_calls.append(True)
            result = original_close(archive)
            if len(close_calls) == 1:
                raise OSError("forced close failure")
            return result

        monkeypatch.setattr(
            setup_module.gzip.GzipFile,
            "close",
            fail_first_close,
        )
    else:
        monkeypatch.setattr(
            setup_module,
            "_atomic_replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("forced replace failure")
            ),
        )

    with pytest.raises(OSError, match=f"forced {failure_point} failure"):
        setup_module._write_reproducible_gztar(
            source,
            destination,
            "yt_final-1.0.0",
            1722124800,
        )

    assert destination.read_bytes() == b"prior release"
    assert list(tmp_path.glob(f".{destination.name}.publish-*")) == []


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_never_uses_tarfile_add(tmp_path, monkeypatch):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    monkeypatch.setattr(
        setup_module.tarfile.TarFile,
        "add",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("TarFile.add is forbidden")
        ),
    )
    setup_module._write_reproducible_gztar(
        source,
        destination,
        "yt_final-1.0.0",
        1722124800,
    )
    with tarfile.open(destination, "r:gz") as archive:
        assert archive.extractfile(
            "yt_final-1.0.0/README.md"
        ).read() == b"reviewed\n"


@_skip_sdist_writer_on_win_nonreviewed
def test_phase_b_sdist_publication_fsyncs_then_atomically_replaces(
    tmp_path, monkeypatch
):
    setup_module, _setup_keywords = _load_reviewed_setup_module(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_bytes(b"reviewed\n")
    destination = tmp_path / "yt_final-1.0.0.tar.gz"
    destination.write_bytes(b"prior release")
    events = []
    reservations = []
    real_mkstemp = setup_module.tempfile.mkstemp
    real_fsync = setup_module.os.fsync
    real_replace = setup_module._atomic_replace

    def reserve(**options):
        reservations.append(options)
        return real_mkstemp(**options)

    def fsync(descriptor):
        events.append("fsync")
        return real_fsync(descriptor)

    def replace(source_path, destination_path):
        events.append("replace")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(setup_module.tempfile, "mkstemp", reserve)
    monkeypatch.setattr(setup_module.os, "fsync", fsync)
    monkeypatch.setattr(setup_module, "_atomic_replace", replace)

    result = setup_module._write_reproducible_gztar(
        source,
        destination,
        "yt_final-1.0.0",
        1722124800,
    )

    assert result == destination
    assert events.index("fsync") < events.index("replace")
    assert len(reservations) == 1
    assert reservations[0]["prefix"] == "artifact-"
    assert reservations[0]["suffix"] == ".tmp"
    publication_directory = Path(reservations[0]["dir"])
    assert publication_directory.parent == destination.parent
    assert publication_directory.name.startswith(
        f".{destination.name}.publish-"
    )
    assert destination.read_bytes() != b"prior release"
    assert list(tmp_path.glob(f".{destination.name}.publish-*")) == []


def test_phase_b_clean_distribution_build_and_external_install(
    tmp_path,
    pytestconfig,
):
    if os.environ.get("YT_FINAL_VERIFY_DISTRIBUTIONS") != "1":
        pytest.skip("set YT_FINAL_VERIFY_DISTRIBUTIONS=1 to run release Q&A")

    pytest_cache = getattr(pytestconfig, "cache", None)
    tmp_path, controller_environment, controller_cwd = (
        _controller_evidence_context(
            tmp_path,
            getattr(pytest_cache, "_cachedir", None),
        )
    )
    requested_build_python = os.environ.get("YT_FINAL_BUILD_PYTHON")
    requested_release_python = os.environ.get("YT_FINAL_RELEASE_PYTHON")
    assert requested_build_python, "YT_FINAL_BUILD_PYTHON is required"
    assert requested_release_python, "YT_FINAL_RELEASE_PYTHON is required"
    assert not (ROOT / "pip").exists()
    workspace_before = _workspace_write_snapshot(
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    build_python = _verified_build_toolchain(
        requested_build_python,
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    release_python = _verified_release_toolchain(
        requested_release_python,
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    commit, epoch = _release_head_identity(
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    assert re.fullmatch(r"[0-9a-f]{40}", commit)

    source = _archive_clean_head_source(
        tmp_path,
        "clean-source-a",
        commit,
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    source_member_bytes = _reviewed_sdist_source_members(source)
    artifact_dir = tmp_path / "release-a"
    wheel, sdist = _build_python_distributions(
        build_python,
        source,
        artifact_dir,
        epoch,
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    portable, manifest = _build_portable_and_checksums(
        build_python,
        source,
        artifact_dir,
        epoch,
        wheel,
        sdist,
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    _validated_wheel_files(wheel)
    _assert_reproducible_sdist(
        sdist,
        source,
        epoch,
        source_member_bytes=source_member_bytes,
    )
    _assert_checksum_manifest(manifest, (wheel, sdist, portable))
    pip_cache = tmp_path / "pip-cache"
    release_tool_environment = _release_state_environment(
        tmp_path / "release-tool-state",
        pip_cache,
    )
    _twine_check_release(
        release_python,
        wheel,
        sdist,
        environment_updates=release_tool_environment,
    )

    runtime_root = tmp_path / "runtime-a"
    runtime_python, runtime_environment = (
        _install_locked_runtime_environment(
            release_python,
            runtime_root,
            source / "Final" / "approved-python.lock",
            wheel,
            tmp_path / "state-a",
            pip_cache,
        )
    )
    _assert_external_runtime(
        release_python,
        runtime_python,
        runtime_root,
        source,
        tmp_path / "unrelated-a",
        runtime_environment,
    )

    source_b = _archive_clean_head_source(
        tmp_path,
        "clean-source-b",
        commit,
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    source_member_bytes_b = _reviewed_sdist_source_members(source_b)
    artifact_dir_b = tmp_path / "release-b"
    wheel_b, sdist_b = _build_python_distributions(
        build_python,
        source_b,
        artifact_dir_b,
        epoch,
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    portable_b, manifest_b = _build_portable_and_checksums(
        build_python,
        source_b,
        artifact_dir_b,
        epoch,
        wheel_b,
        sdist_b,
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    _validated_wheel_files(wheel_b)
    _assert_reproducible_sdist(
        sdist_b,
        source_b,
        epoch,
        source_member_bytes=source_member_bytes_b,
    )
    _assert_checksum_manifest(manifest_b, (wheel_b, sdist_b, portable_b))

    release_a = (wheel, sdist, portable)
    release_b = (wheel_b, sdist_b, portable_b)
    assert [path.name for path in release_a] == [
        path.name for path in release_b
    ]
    assert _artifact_hashes(release_a) == _artifact_hashes(release_b)
    for artifact_a, artifact_b in zip(release_a, release_b, strict=True):
        assert artifact_a.read_bytes() == artifact_b.read_bytes(), (
            f"same-commit release artifact is not byte reproducible: "
            f"{artifact_a.name}"
        )
    assert manifest.read_bytes() == manifest_b.read_bytes()

    extracted_sdist = _safe_extract_release_tar(
        sdist,
        tmp_path / "extracted-sdist",
        require_single_root=True,
    )
    sdist_build_python = _create_locked_build_environment(
        release_python,
        tmp_path / "sdist-build-environment",
        extracted_sdist / "requirements" / "build.lock",
        tmp_path / "sdist-build-state",
        pip_cache,
    )
    rebuilt_wheel = _rebuild_wheel_from_sdist(
        sdist_build_python,
        extracted_sdist,
        tmp_path / "sdist-wheel",
        epoch,
        environment_updates=controller_environment,
        cwd=controller_cwd,
    )
    _validated_wheel_files(rebuilt_wheel)
    assert rebuilt_wheel.read_bytes() == wheel.read_bytes(), (
        "the same-epoch wheel rebuilt from the sdist is not byte-identical"
    )

    rebuilt_runtime_root = tmp_path / "runtime-from-sdist"
    rebuilt_runtime_python, rebuilt_runtime_environment = (
        _install_locked_runtime_environment(
            release_python,
            rebuilt_runtime_root,
            extracted_sdist / "Final" / "approved-python.lock",
            rebuilt_wheel,
            tmp_path / "state-from-sdist",
            pip_cache,
        )
    )
    _assert_external_runtime(
        release_python,
        rebuilt_runtime_python,
        rebuilt_runtime_root,
        extracted_sdist,
        tmp_path / "unrelated-from-sdist",
        rebuilt_runtime_environment,
    )
    assert not (ROOT / "pip").exists()
    assert _workspace_write_snapshot(
        environment_updates=controller_environment,
        cwd=controller_cwd,
    ) == workspace_before, (
        "distribution verification changed repository state"
    )


def test_pyproject_does_not_mix_a_license_expression_with_a_license_classifier():
    """PEP 639 forbids the pair, and setuptools raises rather than warning.

    pyproject.toml declared BOTH `license = "Unlicense"` (a PEP 639 SPDX expression) and the
    legacy `License :: OSI Approved :: ...` classifier. setuptools 83.0.0 -- the exact version
    pinned in [build-system].requires and in requirements/build.lock -- raises InvalidConfigError
    on that combination, before any wheel work begins.

    The blast radius was everything that builds: `pip install .` is the README's only install
    instruction and it died with a traceback, and CI invokes the same backend unconditionally in
    the Task 5 distribution gate and the release-artifact build, so every one of the six matrix
    cells went red.

    None of that showed up locally, because the distribution gate is opt-in behind
    YT_FINAL_VERIFY_DISTRIBUTIONS=1 and skipped by a plain `python -m pytest`. This test is
    deliberately cheap -- it parses one file, builds nothing -- so the conflict is caught by any
    run on any platform instead of only by a release.
    """
    import tomllib

    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project") or {}
    classifiers = [c for c in (project.get("classifiers") or []) if c.startswith("License ::")]

    if isinstance(project.get("license"), str):
        assert not classifiers, (
            "pyproject.toml sets a PEP 639 license expression "
            f"(license = {project['license']!r}) AND these legacy classifiers: {classifiers}. "
            "setuptools raises InvalidConfigError on that pair, which breaks `pip install .` "
            "and every CI cell. Drop the classifier -- the expression already carries the licence."
        )


def test_reviewed_package_metadata_still_matches_pyproject():
    """The reviewed METADATA expectation must track what pyproject actually declares.

    scripts/verify_release.py hardcodes the exact METADATA bytes a release is allowed to
    publish, deliberately: deriving it from the build would make the check tautological.
    The cost of that choice is that it silently rots when pyproject gains a field.

    It had rotted badly. The expectation named neither Summary, nor Keywords, nor any of the
    four Project-URLs, nor any of the twelve Classifiers that pyproject declares -- so every
    macOS cell failed the release verifier with "package metadata is not the reviewed
    metadata derived from the trusted README.md". Nobody saw it because CI had not executed a
    step in weeks.

    This does not re-derive the expectation; it only asserts that every value pyproject
    declares is present in it. A genuine review is still required to change the literal, but
    forgetting to is now caught in milliseconds rather than by a release.
    """
    import tomllib

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "scripts"))
    import verify_release

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    metadata = verify_release._trusted_package_metadata(root, "1.0.0")
    header = metadata.split(b"\r\n\r\n", 1)[0].decode("utf-8")

    missing = []
    if project.get("description"):
        if f"Summary: {project['description']}" not in header:
            missing.append("Summary")
    for classifier in project.get("classifiers") or []:
        if f"Classifier: {classifier}" not in header:
            missing.append(f"Classifier: {classifier}")
    if project.get("keywords"):
        if f"Keywords: {','.join(project['keywords'])}" not in header:
            missing.append("Keywords")
    for label, url in (project.get("urls") or {}).items():
        if f"Project-URL: {label}, {url}" not in header:
            missing.append(f"Project-URL: {label}")

    assert not missing, (
        "scripts/verify_release.py::_trusted_package_metadata has drifted from "
        f"pyproject.toml and is missing: {missing}. A release built from this pyproject "
        "would be rejected by the release verifier."
    )
