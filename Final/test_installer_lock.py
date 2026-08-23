from __future__ import annotations

import importlib
import multiprocessing
import os
import threading
from pathlib import Path

import pytest

import installer_lock
from installer_lock import InstallerBusyError, installer_guard


def _spawn_guard_owner(root: str, ready, release) -> None:
    from installer_lock import installer_guard as child_guard

    with child_guard(Path(root)):
        ready.send("owned")
        if not release.wait(10):
            raise RuntimeError("parent never released spawned guard owner")


def test_same_thread_nesting_preserves_outer_ownership(tmp_path):
    competitor_result = []

    with installer_guard(tmp_path):
        with installer_guard(tmp_path):
            pass

        thread = threading.Thread(
            target=lambda: competitor_result.append(_try_guard(tmp_path)),
        )
        thread.start()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert competitor_result == ["busy"]

    with installer_guard(tmp_path):
        pass


def _try_guard(root: Path) -> str:
    try:
        with installer_guard(root):
            return "entered"
    except InstallerBusyError:
        return "busy"


def test_competing_thread_is_rejected_without_entering_body(tmp_path):
    body_entered = threading.Event()

    with installer_guard(tmp_path):
        def compete():
            with pytest.raises(InstallerBusyError):
                with installer_guard(tmp_path):
                    body_entered.set()

        thread = threading.Thread(target=compete)
        thread.start()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert not body_entered.is_set()


def test_acquiring_reservation_is_observed_as_busy_without_waiting(
        tmp_path, monkeypatch):
    acquire_started = threading.Event()
    allow_acquire = threading.Event()
    original = installer_lock._acquire_file_lock

    def blocked_acquire(lock_path):
        acquire_started.set()
        if not allow_acquire.wait(5):
            raise RuntimeError("test did not release acquisition")
        return original(lock_path)

    monkeypatch.setattr(installer_lock, "_acquire_file_lock", blocked_acquire)
    owner = threading.Thread(target=lambda: _try_guard(tmp_path))
    owner.start()
    assert acquire_started.wait(5)

    assert _try_guard(tmp_path) == "busy"
    allow_acquire.set()
    owner.join(timeout=5)
    assert not owner.is_alive()


def test_releasing_reservation_is_observed_as_busy_without_waiting(
        tmp_path, monkeypatch):
    release_started = threading.Event()
    allow_release = threading.Event()
    original = installer_lock._release_file_lock

    def blocked_release(handle):
        release_started.set()
        if not allow_release.wait(5):
            raise RuntimeError("test did not release cleanup")
        return original(handle)

    monkeypatch.setattr(installer_lock, "_release_file_lock", blocked_release)
    owner = threading.Thread(target=lambda: _try_guard(tmp_path))
    owner.start()
    assert release_started.wait(5)

    assert _try_guard(tmp_path) == "busy"
    allow_release.set()
    owner.join(timeout=5)
    assert not owner.is_alive()


@pytest.mark.parametrize("failure", [RuntimeError("boom"), KeyboardInterrupt()])
def test_body_failure_releases_guard_for_later_acquisition(tmp_path, failure):
    with pytest.raises(type(failure)):
        with installer_guard(tmp_path):
            raise failure

    with installer_guard(tmp_path):
        pass


def test_nested_different_root_is_rejected_without_poisoning_owner(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"

    with installer_guard(first):
        with pytest.raises(ValueError):
            with installer_guard(second):
                pass
        with installer_guard(first):
            pass

    with installer_guard(second):
        pass


def test_lock_file_persists_after_release(tmp_path):
    lock_file = tmp_path / "installer.lock"

    with installer_guard(tmp_path):
        assert lock_file.is_file()

    assert lock_file.is_file()


def test_guard_rejects_symlink_app_root_before_writing_lock(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    app_root = tmp_path / "yt_final"
    try:
        app_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(RuntimeError):
        with installer_guard(app_root):
            pytest.fail("unsafe linked app root entered installer body")

    assert not (outside / "installer.lock").exists()


def test_guard_rejects_reparse_app_root_before_writing_lock(
    tmp_path, monkeypatch
):
    app_root = tmp_path / "yt_final"
    app_root.mkdir()
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

    def reparse_root_lstat(path):
        metadata = real_lstat(path)
        if path == app_root:
            return ReparseMetadata(metadata)
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse_root_lstat)

    with pytest.raises(RuntimeError):
        with installer_guard(app_root):
            pytest.fail("unsafe reparse app root entered installer body")

    assert not (app_root / "installer.lock").exists()


def test_guard_rejects_symlink_lock_leaf_without_mutating_target(tmp_path):
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"external-sentinel")
    app_root = tmp_path / "yt_final"
    app_root.mkdir()
    lock_file = app_root / "installer.lock"
    try:
        lock_file.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(RuntimeError):
        with installer_guard(app_root):
            pytest.fail("unsafe linked lock file entered installer body")

    assert outside.read_bytes() == b"external-sentinel"


def test_guard_rejects_reparse_lock_leaf_before_writing(tmp_path, monkeypatch):
    app_root = tmp_path / "yt_final"
    app_root.mkdir()
    lock_file = app_root / "installer.lock"
    lock_file.write_bytes(b"leaf-sentinel")
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

    def reparse_leaf_lstat(path):
        metadata = real_lstat(path)
        if path == lock_file:
            return ReparseMetadata(metadata)
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse_leaf_lstat)

    with pytest.raises(RuntimeError):
        with installer_guard(app_root):
            pytest.fail("unsafe reparse lock file entered installer body")

    assert lock_file.read_bytes() == b"leaf-sentinel"


def test_guard_rejects_hardlinked_lock_leaf_without_mutating_target(tmp_path):
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"external-sentinel")
    app_root = tmp_path / "yt_final"
    app_root.mkdir()
    lock_file = app_root / "installer.lock"
    try:
        os.link(outside, lock_file)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(RuntimeError):
        with installer_guard(app_root):
            pytest.fail("hardlinked lock file entered installer body")

    assert outside.read_bytes() == b"external-sentinel"


def test_guard_rejects_simulated_symlink_app_root_before_writing_lock(
    tmp_path, monkeypatch
):
    app_root = tmp_path / "yt_final"
    app_root.mkdir()
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == app_root or real_is_symlink(path),
    )

    with pytest.raises(RuntimeError):
        with installer_guard(app_root):
            pytest.fail("unsafe symlink app root entered installer body")

    assert not (app_root / "installer.lock").exists()


def test_guard_rejects_missing_root_beneath_reparse_parent_before_any_write(
    tmp_path, monkeypatch
):
    app_parent = tmp_path / "linked-parent"
    app_parent.mkdir()
    app_root = app_parent / "yt_final"
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

    def reparse_parent_lstat(path):
        metadata = real_lstat(path)
        if path == app_parent:
            return ReparseMetadata(metadata)
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse_parent_lstat)

    with pytest.raises(RuntimeError):
        with installer_guard(app_root):
            pytest.fail("root below unsafe ancestor entered installer body")

    assert not app_root.exists()
    assert not (app_root / "installer.lock").exists()


def test_top_level_and_package_imports_share_process_state(tmp_path):
    package_module = importlib.import_module("Final.installer_lock")
    assert package_module is installer_lock

    result = []
    with installer_lock.installer_guard(tmp_path):
        thread = threading.Thread(
            target=lambda: result.append(
                _try_guard_with(package_module.installer_guard, tmp_path)
            )
        )
        thread.start()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert result == ["busy"]


def _try_guard_with(guard, root: Path) -> str:
    try:
        with guard(root):
            return "entered"
    except InstallerBusyError:
        return "busy"


def test_spawned_process_contention_is_nonblocking_then_recovers(tmp_path):
    context = multiprocessing.get_context("spawn")
    parent_ready, child_ready = context.Pipe(duplex=False)
    release = context.Event()
    process = context.Process(
        target=_spawn_guard_owner,
        args=(str(tmp_path), child_ready, release),
    )
    process.start()
    child_ready.close()
    try:
        assert parent_ready.poll(10)
        assert parent_ready.recv() == "owned"
        with pytest.raises(InstallerBusyError):
            with installer_guard(tmp_path):
                pass
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        parent_ready.close()

    assert process.exitcode == 0
    with installer_guard(tmp_path):
        pass


def test_cleanup_failure_does_not_mask_body_failure(tmp_path, monkeypatch):
    def fail_cleanup(handle):
        try:
            handle.close()
        finally:
            raise OSError("cleanup failed")

    monkeypatch.setattr(installer_lock, "_release_file_lock", fail_cleanup)

    with pytest.raises(RuntimeError, match="body failed"):
        with installer_guard(tmp_path):
            raise RuntimeError("body failed")


def test_one_guard_blocks_certified_runtime_and_ffmpeg_installers(
        tmp_path, monkeypatch):
    import ffmpeg_updater
    import optional_tools

    monkeypatch.setattr(installer_lock, "_default_root", lambda: tmp_path)
    monkeypatch.setattr(
        optional_tools,
        "managed_tools_dir",
        lambda: tmp_path / "tools",
    )
    monkeypatch.setattr(optional_tools.sys, "platform", "win32")
    monkeypatch.setattr(optional_tools.py_platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        ffmpeg_updater, "_platform", lambda: "win32", raising=False)
    monkeypatch.setattr(
        ffmpeg_updater, "_machine", lambda: "x86_64", raising=False)
    ready = threading.Event()
    release = threading.Event()

    def own_guard():
        with installer_guard(tmp_path):
            ready.set()
            assert release.wait(5)

    owner = threading.Thread(target=own_guard)
    owner.start()
    assert ready.wait(5)
    try:
        with pytest.raises(InstallerBusyError):
            optional_tools.install_node(
                opener=lambda *_a, **_k: pytest.fail(
                    "busy certified runtime reached network"
                )
            )
        with pytest.raises(InstallerBusyError):
            ffmpeg_updater.ManagedFFmpegUpdater().install(
                busy=lambda: pytest.fail(
                    "busy FFmpeg install reached activity probe"
                )
            )
    finally:
        release.set()
        owner.join(timeout=5)
    assert not owner.is_alive()
