"""Executable discovery must never inherit implicit current-directory search."""
from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest


_MACOS_BROWSER_APPS = (
    (
        "chrome",
        "Google Chrome.app/Contents/MacOS/Google Chrome",
    ),
    (
        "edge",
        "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ),
    (
        "brave",
        "Brave Browser.app/Contents/MacOS/Brave Browser",
    ),
)


@pytest.mark.parametrize(("browser", "app_relative"), _MACOS_BROWSER_APPS)
def test_macos_browser_discovery_rejects_home_applications(
        browser, app_relative, monkeypatch, tmp_path):
    """Mutable HOME must never become browser-executable authority."""
    import executable_trust

    poisoned_home = tmp_path / "attacker-home"
    fake_browser = poisoned_home / "Applications" / app_relative
    fake_browser.parent.mkdir(parents=True)
    fake_browser.write_bytes(b"attacker-controlled browser")
    fake_browser.chmod(0o755)
    monkeypatch.setenv("HOME", str(poisoned_home))
    monkeypatch.setattr(
        Path,
        "home",
        classmethod(lambda cls: cls(os.environ["HOME"])),
    )
    monkeypatch.setattr(
        executable_trust.os,
        "access",
        lambda path, mode: Path(path) == fake_browser and mode == os.X_OK,
    )

    assert executable_trust.installed_browser_executable(
        browser,
        platform="darwin",
    ) is None


@pytest.mark.parametrize(
    ("browser", "expected_relatives"),
    (
        (
            "chrome",
            (
                "Google Chrome.app/Contents/MacOS/Google Chrome",
                "Chromium.app/Contents/MacOS/Chromium",
            ),
        ),
        (
            "edge",
            ("Microsoft Edge.app/Contents/MacOS/Microsoft Edge",),
        ),
        (
            "brave",
            ("Brave Browser.app/Contents/MacOS/Brave Browser",),
        ),
    ),
)
def test_macos_browser_candidates_keep_only_fixed_applications(
        browser, expected_relatives):
    """Supported macOS vendors retain fixed /Applications discovery."""
    import executable_trust

    system_root = Path("/Applications")

    assert executable_trust._browser_candidate_specs(
        browser,
        "darwin",
    ) == tuple(
        ((system_root,), system_root / relative, False)
        for relative in expected_relatives
    )


@pytest.mark.parametrize("poison_kind", ("symlink", "junction", "reparse"))
def test_fixed_browser_authority_rejects_link_like_component(
        poison_kind, monkeypatch, tmp_path):
    """No link-like component may redirect a fixed Windows browser authority."""
    import executable_trust

    root = tmp_path / "Program Files"
    poisoned = root / "Google"
    browser = poisoned / "Chrome" / "Application" / "chrome.exe"
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"trusted-location-browser")
    monkeypatch.setattr(
        executable_trust,
        "_browser_candidate_specs",
        lambda _browser, _platform: (((root,), browser, False),),
    )

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
            return ReparseMetadata(metadata) if path == poisoned else metadata

        monkeypatch.setattr(Path, "lstat", reparse_lstat)

    assert executable_trust.installed_browser_executable(
        "chrome",
        platform="win32",
    ) is None


def test_linux_fixed_browser_link_must_resolve_within_system_authority(
        monkeypatch, tmp_path):
    """A distro-style browser link may not escape its fixed system roots."""
    import executable_trust

    system_root = tmp_path / "usr"
    candidate = system_root / "bin" / "google-chrome"
    outside = tmp_path / "user-writable" / "google-chrome"
    candidate.parent.mkdir(parents=True)
    outside.parent.mkdir()
    outside.write_bytes(b"poison")
    monkeypatch.setattr(
        executable_trust,
        "_browser_candidate_specs",
        lambda _browser, _platform: (((system_root,), candidate, True),),
    )
    real_is_file = Path.is_file
    real_resolve = Path.resolve
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: path == candidate or real_is_file(path),
    )
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path, strict=False: (
            outside if path == candidate else real_resolve(path, strict=strict)
        ),
    )
    monkeypatch.setattr(
        executable_trust.os,
        "access",
        lambda path, mode: path == candidate and mode == os.X_OK,
    )

    assert executable_trust.installed_browser_executable(
        "chrome",
        platform="linux",
    ) is None


def test_linux_fixed_browser_keeps_safe_system_link_invocation_path(
        monkeypatch, tmp_path):
    """Validated distro links must keep argv[0] semantics such as /snap/bin apps."""
    import executable_trust

    system_root = tmp_path / "usr"
    candidate = system_root / "bin" / "google-chrome"
    target = system_root / "lib" / "chrome" / "google-chrome"
    candidate.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"system browser")
    monkeypatch.setattr(
        executable_trust,
        "_browser_candidate_specs",
        lambda _browser, _platform: (((system_root,), candidate, True),),
    )
    real_resolve = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path, strict=False: (
            target if path == candidate else real_resolve(path, strict=strict)
        ),
    )
    monkeypatch.setattr(
        executable_trust.os,
        "access",
        lambda path, mode: path == target and mode == os.X_OK,
    )

    assert executable_trust.installed_browser_executable(
        "chrome",
        platform="linux",
    ) == candidate


def test_linux_fixed_browser_ignores_missing_unrelated_system_root(
        monkeypatch, tmp_path):
    """An absent /snap-style authority must not hide a valid /usr browser."""
    import executable_trust

    system_root = tmp_path / "usr"
    missing_root = tmp_path / "snap"
    candidate = system_root / "bin" / "google-chrome"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"system browser")
    monkeypatch.setattr(
        executable_trust,
        "_browser_candidate_specs",
        lambda _browser, _platform: (
            ((system_root, missing_root), candidate, True),
        ),
    )
    monkeypatch.setattr(
        executable_trust.os,
        "access",
        lambda path, mode: path == candidate and mode == os.X_OK,
    )

    assert executable_trust.installed_browser_executable(
        "chrome",
        platform="linux",
    ) == candidate


def test_explicit_path_resolver_ignores_cwd_and_empty_path_entry(
        monkeypatch, tmp_path):
    from executable_trust import explicit_path_executable

    poisoned_cwd = tmp_path / "poisoned"
    poisoned_cwd.mkdir()
    (poisoned_cwd / "ffmpeg.exe").write_bytes(b"poison")
    monkeypatch.chdir(poisoned_cwd)

    assert explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value=os.pathsep,
        platform="win32",
    ) is None


def test_explicit_path_resolver_returns_only_absolute_file_in_named_directory(
        tmp_path):
    from executable_trust import explicit_path_executable

    tools = tmp_path / "manual-tools"
    tools.mkdir()
    executable = tools / "ffmpeg.exe"
    executable.write_bytes(b"manual")
    # The resolver always asks the real host whether the file is executable, so
    # on POSIX hosts running this Windows-shaped case the fixture needs the
    # execute bit; without it the resolver is right to refuse it. Windows
    # ignores the mode bits.
    executable.chmod(0o755)

    assert explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value=str(tools),
        platform="win32",
    ) == executable


def test_explicit_path_resolver_skips_relative_path_entries(
        monkeypatch, tmp_path):
    from executable_trust import explicit_path_executable

    relative = tmp_path / "relative-tools"
    relative.mkdir()
    (relative / "ffmpeg.exe").write_bytes(b"poison")
    monkeypatch.chdir(tmp_path)

    assert explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value="relative-tools",
        platform="win32",
    ) is None


def test_macos_optional_runtime_never_launches_cwd_xattr(
        monkeypatch, tmp_path):
    import optional_tools

    poisoned = tmp_path / "download-folder"
    poisoned.mkdir()
    (poisoned / "xattr").write_bytes(b"poison")
    runtime = tmp_path / "node"
    runtime.write_bytes(b"runtime")
    monkeypatch.chdir(poisoned)
    monkeypatch.setattr(optional_tools.sys, "platform", "darwin")
    launched = []
    monkeypatch.setattr(
        optional_tools.subprocess,
        "run",
        lambda command, **_kwargs: launched.append(list(command)),
    )

    optional_tools._chmod_exec(runtime)

    assert all(Path(command[0]).is_absolute() for command in launched)
    assert all(Path(command[0]) != poisoned / "xattr" for command in launched)


def test_macos_gpu_probe_never_launches_cwd_os_helpers(
        monkeypatch, tmp_path):
    import capability

    poisoned = tmp_path / "download-folder"
    poisoned.mkdir()
    (poisoned / "system_profiler").write_bytes(b"poison")
    (poisoned / "sysctl").write_bytes(b"poison")
    monkeypatch.chdir(poisoned)
    monkeypatch.setattr(capability.sys, "platform", "darwin")
    detector = capability.CapabilityDetector.__new__(
        capability.CapabilityDetector
    )
    detector.gpu_info = {
        "gpu_name": "Unknown",
        "working_encoders": [],
        "apple": False,
    }
    launched = []
    monkeypatch.setattr(
        capability.subprocess,
        "run",
        lambda command, **_kwargs: launched.append(list(command)),
    )

    detector._get_apple_gpu_details()

    assert all(Path(command[0]).is_absolute() for command in launched)
    assert all(
        Path(command[0]).parent != poisoned
        for command in launched
    )


def test_posix_resolver_skips_inert_file_before_later_executable(
        monkeypatch, tmp_path):
    import executable_trust

    inert_dir = tmp_path / "inert"
    executable_dir = tmp_path / "executable"
    inert_dir.mkdir()
    executable_dir.mkdir()
    inert = inert_dir / "node.exe"
    executable = executable_dir / "node.exe"
    inert.write_bytes(b"not executable")
    executable.write_bytes(b"executable")
    monkeypatch.setattr(
        executable_trust.os,
        "access",
        lambda path, mode: Path(path) == executable and mode == os.X_OK,
    )

    assert executable_trust.explicit_path_executable(
        "node",
        path_value=f"{inert_dir};{executable_dir}",
        platform="win32",
    ) == executable


def test_windows_cancel_never_launches_poisoned_cwd_taskkill(
        monkeypatch, tmp_path):
    import yt_final

    poisoned = tmp_path / "download-folder"
    poisoned.mkdir()
    (poisoned / "taskkill.exe").write_bytes(b"poison")
    monkeypatch.chdir(poisoned)
    monkeypatch.setattr(yt_final._sys, "platform", "win32")
    monkeypatch.setattr(
        yt_final,
        "_windows_system_executable",
        lambda _name: None,
        raising=False,
    )
    launched = []
    monkeypatch.setattr(
        yt_final.subprocess,
        "run",
        lambda command, **_kwargs: launched.append(list(command)),
    )

    class Process:
        pid = 123

        def __init__(self):
            self.killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    process = Process()
    yt_final._kill_process_tree(process)

    assert launched == []
    assert process.killed is True


def test_windows_cancel_uses_only_absolute_system_taskkill(
        monkeypatch, tmp_path):
    import yt_final

    system_taskkill = tmp_path / "Windows" / "System32" / "taskkill.exe"
    system_taskkill.parent.mkdir(parents=True)
    system_taskkill.write_bytes(b"system")
    monkeypatch.setattr(yt_final._sys, "platform", "win32")
    monkeypatch.setattr(
        yt_final,
        "_windows_system_executable",
        lambda name: system_taskkill if name == "taskkill.exe" else None,
        raising=False,
    )
    launched = []
    monkeypatch.setattr(
        yt_final.subprocess,
        "run",
        lambda command, **_kwargs: (
            launched.append(list(command))
            or type("Result", (), {"returncode": 0})()
        ),
    )

    class Process:
        pid = 456

        def __init__(self):
            self.killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    process = Process()
    yt_final._kill_process_tree(process)

    assert launched == [
        [str(system_taskkill), "/F", "/T", "/PID", "456"]
    ]
    assert Path(launched[0][0]).is_absolute()
    assert process.killed is False


def test_unavailable_tool_sentinel_is_structurally_uncreatable(
        monkeypatch, tmp_path):
    import yt_final

    source = tmp_path / "application" / "yt_final.py"
    source.parent.mkdir()
    source.write_bytes(b"source file")
    old_sentinel = (
        source.parent
        / ".yt-final-unavailable-tools"
        / "ffmpeg"
    )
    old_sentinel.parent.mkdir()
    old_sentinel.write_bytes(b"poison")
    monkeypatch.setattr(yt_final, "__file__", str(source))

    sentinel = Path(yt_final._unavailable_tool_path("ffmpeg"))

    assert sentinel != old_sentinel
    assert source in sentinel.parents
    assert source.is_file()
    assert not sentinel.is_file()
    with pytest.raises((FileExistsError, NotADirectoryError, OSError)):
        sentinel.parent.mkdir(parents=True, exist_ok=True)


class _PosixExecuteBitModel:
    """POSIX ``chmod``/``os.access`` semantics, reproducible on any host.

    ``executable_trust`` gates every discovered candidate on
    ``os.access(candidate, os.X_OK)``. On macOS and Linux that is False for a
    file created without an execute bit, so a fixture that writes a binary and
    forgets ``chmod(0o755)`` is simply not discovered there. Windows has no
    execute bit at all: ``os.access(..., X_OK)`` answers True for every existing
    file, and ``chmod`` only toggles the read-only flag -- ``chmod(0o755)`` on a
    plain Windows file still reads back ``st_mode == 0o100666``. That is why the
    failure mode is invisible on a Windows developer host and fatal on every
    POSIX CI cell, and why it has to be modelled rather than observed here.

    The model keeps the real host authoritative wherever it can: ``chmod`` is
    still applied for real, and on a host whose inodes carry POSIX mode bits the
    unrecorded fallback is the real ``st_mode``, so the model degenerates to the
    thing it models. Windows inodes carry no such bits -- ``os.stat`` there
    fabricates ``0o100777`` for any ``.exe``/``.bat``/``.cmd``/``.com`` name and
    ``0o100666`` for everything else, purely from the suffix -- so on Windows a
    path nobody chmod-ed is modelled as a freshly created ``0o644`` file, which
    is what ``write_bytes`` produces on POSIX. Only the two OS primitives
    ``executable_trust`` consults are modelled; no code under test is rewritten.
    """

    _UNTOUCHED_CREATION_MODE = 0o644

    def __init__(self):
        self._modes: dict[str, int] = {}
        self._real_access = None

    @staticmethod
    def _key(path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def install(self, monkeypatch) -> "_PosixExecuteBitModel":
        import executable_trust

        # Captured before patching: ``executable_trust.os`` is the shared ``os``
        # module, so the replacements must not shadow the real implementations.
        self._real_access = os.access
        real_chmod = os.chmod

        def chmod(path, mode, *args, **kwargs):
            self._modes[self._key(path)] = mode
            try:
                real_chmod(path, mode, *args, **kwargs)
            except (NotImplementedError, OSError):
                pass

        # ``Path.chmod`` delegates to ``os.chmod``, so one seam covers both the
        # ``path.chmod(0o755)`` fixtures use and any ``os.chmod`` in the code.
        monkeypatch.setattr(os, "chmod", chmod)
        monkeypatch.setattr(executable_trust.os, "access", self.access)
        return self

    def _has_execute_bit(self, path) -> bool:
        mode = self._modes.get(self._key(path))
        if mode is None:
            if os.name == "nt":
                mode = self._UNTOUCHED_CREATION_MODE
            else:
                try:
                    mode = Path(path).stat().st_mode
                except OSError:
                    return False
        return bool(mode & stat.S_IXUSR)

    def access(self, path, mode, **kwargs) -> bool:
        if mode & os.X_OK and not self._has_execute_bit(path):
            return False
        # Everything that is not the execute question stays with the real host,
        # so existence and readability are never faked.
        return self._real_access(path, mode & ~os.X_OK, **kwargs)


def test_explicit_path_resolution_obeys_this_host_real_execute_check(tmp_path):
    """Control: unsimulated, the resolver's verdict is the host's own X_OK."""
    import executable_trust

    tools = tmp_path / "manual-tools"
    tools.mkdir()
    candidate = tools / "ffmpeg.exe"
    candidate.write_bytes(b"manual")

    # Nothing is patched here. Windows answers True for any existing file;
    # macOS and Linux answer False until the execute bit is set. Either way the
    # resolver must agree with the host, which is what makes the simulated case
    # below attributable to the execute bit alone rather than to the fixture.
    assert executable_trust.explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value=str(tools),
    ) == (candidate if os.access(candidate, os.X_OK) else None)

    candidate.chmod(0o755)

    assert os.access(candidate, os.X_OK) is True
    assert executable_trust.explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value=str(tools),
    ) == candidate


def test_explicit_path_resolution_needs_the_execute_bit_under_posix_access(
        monkeypatch, tmp_path):
    """The execute bit, not the file's existence, decides POSIX discovery."""
    import executable_trust

    tools = tmp_path / "manual-tools"
    tools.mkdir()
    candidate = tools / "ffmpeg.exe"
    candidate.write_bytes(b"manual")
    model = _PosixExecuteBitModel().install(monkeypatch)

    assert model.access(candidate, os.F_OK) is True
    assert model.access(candidate, os.X_OK) is False
    assert candidate.is_file() is True
    assert executable_trust.explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value=str(tools),
    ) is None

    candidate.chmod(0o755)

    assert model.access(candidate, os.X_OK) is True
    assert executable_trust.explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value=str(tools),
    ) == candidate


def test_posix_access_model_keeps_refusing_untrusted_path_entries(
        monkeypatch, tmp_path):
    """An execute bit must never buy an implicit current-directory search."""
    import executable_trust

    poisoned = tmp_path / "download-folder"
    poisoned.mkdir()
    weapon = poisoned / "ffmpeg.exe"
    weapon.write_bytes(b"poison")
    monkeypatch.chdir(poisoned)
    model = _PosixExecuteBitModel().install(monkeypatch)
    weapon.chmod(0o755)

    assert model.access(weapon, os.X_OK) is True
    # Positive control: the very same executable is reachable through an
    # explicit absolute entry, so the refusals below are about the PATH entry
    # and not about the model refusing everything.
    assert executable_trust.explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value=str(poisoned),
    ) == weapon
    assert executable_trust.explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value=os.pathsep,
    ) is None
    assert executable_trust.explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value=".",
    ) is None
    assert executable_trust.explicit_path_executable(
        ("ffmpeg.exe", "ffmpeg"),
        path_value="download-folder",
    ) is None


_BORROWED_UPDATER_TESTS = (
    # Both reach explicit_path_executable() through a file the fixture writes
    # and then expects PATH discovery to find, so both are only correct on a
    # POSIX CI cell if the fixture sets the execute bit.
    "test_install_rejects_release_that_breaks_available_nvidia_encoding",
    "test_capability_detector_falls_back_when_managed_pair_is_invalid",
)


def _borrow_updater_test(name, monkeypatch, request, tmp_path):
    """Run an updater test verbatim, under its own module's autouse fixture.

    The fixture is not re-created here. Its body is fetched from
    ``test_ffmpeg_updater`` and executed, because a hand-written stand-in that
    drifts from the real one produces a result about the stand-in.
    """
    import test_ffmpeg_updater

    fixture = test_ffmpeg_updater._default_win32_platform_for_gyan_fixtures
    body = getattr(fixture, "_fixture_function", None) or getattr(
        fixture,
        "__wrapped__",
        fixture,
    )
    body(monkeypatch, request, tmp_path)
    getattr(test_ffmpeg_updater, name)(monkeypatch, tmp_path)


@pytest.mark.parametrize("name", _BORROWED_UPDATER_TESTS)
def test_borrowed_updater_test_passes_unsimulated_on_this_host(
        name, monkeypatch, request, tmp_path):
    """Control: the borrowing harness runs the repo's own tests unchanged."""
    _borrow_updater_test(name, monkeypatch, request, tmp_path)


@pytest.mark.parametrize("name", _BORROWED_UPDATER_TESTS)
def test_borrowed_updater_test_survives_posix_execute_bit_rules(
        name, monkeypatch, request, tmp_path):
    """The updater fixtures must stay discoverable where X_OK is enforced.

    Without the fixtures' ``chmod(0o755)`` the PATH reference resolves to None
    under POSIX rules: ``verify_hardware_compatibility()`` then returns before
    comparing encoders and the losing release installs, and the capability
    fallback finds no system FFmpeg at all. Both assertions in the borrowed
    tests fail in that state, which is exactly what every non-Windows CI cell
    reported. Running them here keeps that guarded on a Windows host.
    """
    _PosixExecuteBitModel().install(monkeypatch)
    _borrow_updater_test(name, monkeypatch, request, tmp_path)
