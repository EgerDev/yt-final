import errno
import json
import multiprocessing
import os
import queue
import select
import stat
import time
import threading
from pathlib import Path
import pytest
import cookies_auth as ca


def _multiprocess_preexisting_manager(store, ready, start, result_queue):
    store = Path(store)
    observed = []

    def validator(path):
        observed.append(Path(path).read_bytes())
        return True

    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        validator=validator,
        store=store,
    )
    ready.set()
    if not start.wait(10):
        result_queue.put(("timeout", observed))
        return
    result_queue.put((manager.validate_existing_file(), observed))


def _multiprocess_long_wait_manager(store, ready, start, result_queue):
    store = Path(store)
    calls = {"n": 0}

    class CountingProvider:
        name = "file"

        def available(self):
            return True

        def acquire(self):
            calls["n"] += 1
            return str(store / "active.txt")

    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        providers=[CountingProvider()],
        validator=lambda _path: True,
        clock=lambda: 1001.0,
        store=store,
    )
    ready.set()
    if not start.wait(15):
        result_queue.put(("start-timeout", None, calls["n"]))
        return
    try:
        result_queue.put(("ok", manager.get_cookiefile(), calls["n"]))
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}", calls["n"]))


def test_auth_config_dir_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    assert ca.auth_config_dir() == tmp_path


def test_load_auth_config_defaults_when_no_file(tmp_path):
    cfg = ca.load_auth_config(config_dir=tmp_path)
    assert cfg["enabled"] is False
    assert cfg["order"] == ["file", "profile", "browser"]
    assert cfg["validate_ttl_min"] == 45
    assert cfg["browser"] == "auto"


def test_load_auth_config_reads_toml_overrides(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[auth]\nenabled = true\nvalidate_ttl_min = 10\norder = [\"file\"]\n",
        encoding="utf-8",
    )
    cfg = ca.load_auth_config(config_dir=tmp_path)
    assert cfg["enabled"] is True
    assert cfg["validate_ttl_min"] == 10
    assert cfg["order"] == ["file"]
    # untouched keys keep defaults
    assert cfg["browser"] == "auto"


def test_write_netscape_roundtrips_a_cookie(tmp_path):
    out = tmp_path / "c.txt"
    ca.write_netscape(
        [{"domain": ".youtube.com", "name": "SID", "value": "abc",
          "secure": True, "expires": 2000000000}],
        out,
    )
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File")
    line = [l for l in text.splitlines() if l and not l.startswith("#")][0]
    fields = line.split("\t")
    assert fields[0] == ".youtube.com"
    assert fields[1] == "TRUE"          # includeSubdomains (leading dot)
    assert fields[3] == "TRUE"          # secure
    assert fields[5] == "SID"
    assert fields[6] == "abc"


def test_write_netscape_chmods_600(tmp_path):
    out = tmp_path / "sub" / "c.txt"
    ca.write_netscape(
        [{"domain": ".youtube.com", "name": "SID", "value": "abc",
          "secure": True, "expires": 2000000000}],
        out,
    )
    assert out.exists()
    if os.name == "posix":
        assert (out.stat().st_mode & 0o777) == 0o600
    # on non-POSIX platforms chmod is a harmless no-op; existence is enough


def test_failed_atomic_replace_preserves_previous_cookiefile(monkeypatch, tmp_path):
    import secureio

    target = tmp_path / "cookies.txt"
    target.write_bytes(b"old-complete")
    monkeypatch.setattr(
        secureio.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("boom")),
    )

    with pytest.raises(OSError, match="boom"):
        secureio.atomic_replace_private(target, b"new-complete")

    assert target.read_bytes() == b"old-complete"
    assert not list(tmp_path.glob(".cookies.txt.*.tmp"))


def test_atomic_replace_is_owner_only_at_creation(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX mode bits; Windows profile ACLs are authoritative")
    import secureio

    monkeypatch.setattr(secureio.os, "chmod", lambda *_: None)
    old_umask = os.umask(0)
    try:
        target = tmp_path / "cookies.txt"
        secureio.atomic_replace_private(target, b"private")
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_replace_concurrent_reader_observes_only_complete_jars(
        monkeypatch, tmp_path):
    import secureio

    target = tmp_path / "cookies.txt"
    old = b"old-complete\n" * 1024
    new = b"new-complete\n" * 1024
    target.write_bytes(old)
    replace_entered = threading.Event()
    allow_replace = threading.Event()
    real_replace = secureio.os.replace

    def blocked_replace(source, destination):
        assert Path(source).parent == target.parent
        assert Path(destination) == target
        replace_entered.set()
        assert allow_replace.wait(5)
        real_replace(source, destination)

    monkeypatch.setattr(secureio.os, "replace", blocked_replace)
    errors = []

    def writer():
        try:
            secureio.atomic_replace_private(target, new)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    assert replace_entered.wait(5)
    observed = {target.read_bytes() for _ in range(50)}
    allow_replace.set()
    thread.join(5)
    assert not thread.is_alive()
    assert not errors
    observed.add(target.read_bytes())
    assert observed == {old, new}


def test_copy_private_atomic_preserves_source_and_tightens_destination(tmp_path):
    import secureio

    source = tmp_path / "browser.txt"
    destination = tmp_path / "active.txt"
    source.write_bytes(b"complete-browser-cookie-jar")
    destination.write_bytes(b"old")
    if os.name != "nt":
        destination.chmod(0o644)

    secureio.copy_private_atomic(source, destination)

    assert source.read_bytes() == b"complete-browser-cookie-jar"
    assert destination.read_bytes() == source.read_bytes()
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_write_netscape_waits_for_atomic_store_lock(tmp_path):
    writer_entered = threading.Event()
    allow_writer = threading.Event()
    write_done = threading.Event()
    destination = tmp_path / "browser.txt"

    def writer():
        with ca.cookie_store_write_lock(tmp_path):
            writer_entered.set()
            assert allow_writer.wait(5)

    def export():
        ca.write_netscape(
            [{"domain": ".youtube.com", "name": "SID", "value": "abc"}],
            destination,
        )
        write_done.set()

    writer_thread = threading.Thread(target=writer)
    export_thread = threading.Thread(target=export)
    writer_thread.start()
    assert writer_entered.wait(5)
    export_thread.start()
    try:
        assert not write_done.wait(0.2)
        assert not destination.exists()
    finally:
        allow_writer.set()
    writer_thread.join(5)
    export_thread.join(5)
    assert write_done.is_set()
    assert destination.exists()


def test_cookie_store_dir_does_not_create_directory(monkeypatch, tmp_path):
    # FIX 3: computing the path must be a true no-op — a disabled/default
    # install should not create ~/.yt_final/cookies/ on first call.
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    d = ca.cookie_store_dir()
    assert d == tmp_path / "cookies"
    assert not d.exists()


def test_disabled_manager_get_cookiefile_creates_no_store_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    mgr = ca.build_default_manager()
    assert mgr.get_cookiefile() is None
    assert not (tmp_path / "cookies").exists()


def test_state_roundtrip(tmp_path):
    st = ca.CookieState(path=str(tmp_path / "a.txt"), source="file",
                        fetched_at=1.0, last_validated_at=2.0, valid=True)
    ca.save_state(tmp_path, st)
    got = ca.load_state(tmp_path)
    assert got == st


def test_load_state_missing_returns_empty(tmp_path):
    got = ca.load_state(tmp_path)
    assert got.valid is False and got.path is None


class _FakeProvider:
    def __init__(self, name, path):
        self.name = name; self._path = path; self.calls = 0
    def available(self): return self._path is not None
    def acquire(self):
        self.calls += 1
        return self._path


def _mgr(tmp_path, providers, valid=True, enabled=True):
    t = {"n": 1000.0}
    def clock(): return t["n"]
    m = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": enabled},
        providers=providers,
        validator=lambda cf: valid,
        clock=clock,
        store=tmp_path,
    )
    return m, t


def test_disabled_returns_none(tmp_path):
    fp = _FakeProvider("file", "x.txt")
    m, _ = _mgr(tmp_path, [fp], enabled=False)
    assert m.get_cookiefile() is None
    assert fp.calls == 0


def test_acquires_from_first_available_and_validates(tmp_path):
    cookie_file = tmp_path / "x.txt"
    cookie_file.write_text("test cookies")
    fp = _FakeProvider("file", str(cookie_file))
    m, _ = _mgr(tmp_path, [fp], valid=True)
    assert m.get_cookiefile() == str(cookie_file)
    assert fp.calls == 1


def test_fresh_cache_skips_reacquire(tmp_path):
    cookie_file = tmp_path / "x.txt"
    cookie_file.write_text("test cookies")
    fp = _FakeProvider("file", str(cookie_file))
    m, _ = _mgr(tmp_path, [fp])
    m.get_cookiefile(); m.get_cookiefile()
    assert fp.calls == 1  # second call served from fresh cache, no re-acquire


def test_notify_auth_failure_forces_reacquire(tmp_path):
    cookie_file = tmp_path / "x.txt"
    cookie_file.write_text("test cookies")
    fp = _FakeProvider("file", str(cookie_file))
    m, _ = _mgr(tmp_path, [fp])
    m.get_cookiefile()
    m.notify_auth_failure()
    m.get_cookiefile()
    assert fp.calls == 2  # invalidated → re-acquired


def test_stale_ttl_triggers_revalidation(tmp_path):
    cookie_file = tmp_path / "x.txt"
    cookie_file.write_text("test cookies")
    fp = _FakeProvider("file", str(cookie_file))
    m, t = _mgr(tmp_path, [fp])
    m.get_cookiefile()
    t["n"] += 46 * 60  # exceed 45-min TTL
    m.get_cookiefile()
    assert fp.calls == 1  # still valid on revalidation → no re-acquire, but validated again


def test_returns_none_when_nothing_validates(tmp_path):
    cookie_file = tmp_path / "x.txt"
    cookie_file.write_text("test cookies")
    fp = _FakeProvider("file", str(cookie_file))
    m, _ = _mgr(tmp_path, [fp], valid=False)
    assert m.get_cookiefile() is None


def test_save_state_atomic_under_concurrent_writers(tmp_path):
    # Regression: save_state used a bare write_text (non-atomic), so a concurrent
    # reader could observe a truncated/half-written file and load_state would
    # silently fall back to an empty CookieState (valid=False), resetting the auth
    # cache. With temp+os.replace under a .flock sidecar every read sees one
    # complete state.
    observed = []

    def worker(i):
        st = ca.CookieState(path=str(tmp_path / f"c{i}.txt"), source="file",
                            fetched_at=float(i), last_validated_at=float(i), valid=True)
        for _ in range(30):
            ca.save_state(tmp_path, st)
            observed.append(ca.load_state(tmp_path).valid)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Every observed read was a complete, valid state — never a corrupt fallback.
    assert observed and all(observed)


def test_save_state_strictly_raises_when_atomic_publication_fails(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        ca,
        "atomic_replace_private",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state full")),
    )

    with pytest.raises(OSError, match="state full"):
        ca.save_state(tmp_path, ca.CookieState(valid=True))


def test_get_cookiefile_concurrent_calls_validate_once(tmp_path):
    # Regression: N parallel-download worker threads share one CookieManager and
    # called get_cookiefile() with no locking, each independently re-running the
    # real network validation / provider walk. The store-keyed lock now serializes
    # them so only the first winner validates; the rest reuse its adopted cache.
    cookie_file = tmp_path / "x.txt"
    cookie_file.write_text("test cookies")
    fp = _FakeProvider("file", str(cookie_file))
    calls = {"n": 0}

    def slow_validator(cf):
        calls["n"] += 1
        time.sleep(0.05)  # widen the race window
        return True

    m = ca.CookieManager(config={**ca._DEFAULTS, "enabled": True},
                         providers=[fp], validator=slow_validator,
                         clock=lambda: 1000.0, store=tmp_path)
    out = []

    def worker():
        out.append(m.get_cookiefile())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(o == str(cookie_file) for o in out)
    assert fp.calls == 1   # provider walked once, not once per thread
    assert calls["n"] == 1  # network validator ran once, not 8×


def test_simultaneous_preexisting_managers_reuse_first_provider_adoption(tmp_path):
    cookie_file = tmp_path / "provider.txt"
    cookie_file.write_text("complete-cookie-jar", encoding="utf-8")
    calls = {"n": 0}

    class SharedProvider:
        name = "file"

        def available(self):
            return True

        def acquire(self):
            calls["n"] += 1
            time.sleep(0.05)
            return str(cookie_file)

    managers = [
        ca.CookieManager(
            config={**ca._DEFAULTS, "enabled": True},
            providers=[SharedProvider()],
            validator=lambda _path: True,
            clock=lambda: 1000.0,
            store=tmp_path,
        )
        for _ in range(2)
    ]
    results = []
    threads = [
        threading.Thread(target=lambda manager=m: results.append(manager.get_cookiefile()))
        for m in managers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert results == [str(cookie_file), str(cookie_file)]
    assert calls["n"] == 1


def test_preexisting_manager_status_reloads_invalidated_store_state(tmp_path):
    active = tmp_path / "active.txt"
    active.write_bytes(b"old-complete")
    ca.save_state(
        tmp_path,
        ca.CookieState(
            path=str(active),
            source="file",
            fetched_at=1.0,
            last_validated_at=1000.0,
            valid=True,
        ),
    )
    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        store=tmp_path,
    )

    ca.replace_active_cookiefile(data=b"new-complete", store=tmp_path)
    status = manager.status()

    assert active.read_bytes() == b"new-complete"
    assert status["has_file"] is True
    assert status["valid"] is False


def test_preexisting_manager_get_does_not_reuse_stale_ttl_after_replace(tmp_path):
    active = tmp_path / "active.txt"
    active.write_bytes(b"old-complete")
    ca.save_state(
        tmp_path,
        ca.CookieState(
            path=str(active),
            source="file",
            fetched_at=1.0,
            last_validated_at=1000.0,
            valid=True,
        ),
    )
    validations = []
    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        providers=[_FakeProvider("file", str(active))],
        validator=lambda path: validations.append(Path(path).read_bytes()) or False,
        clock=lambda: 1001.0,
        store=tmp_path,
    )

    ca.replace_active_cookiefile(data=b"new-complete", store=tmp_path)

    assert manager.get_cookiefile() is None
    assert validations == [b"new-complete"]


def test_store_transaction_blocks_preexisting_manager_in_another_process(tmp_path):
    active = tmp_path / "active.txt"
    active.write_bytes(b"old-complete")
    ca.save_state(
        tmp_path,
        ca.CookieState(path=str(active), source="file", valid=True),
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    start = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_multiprocess_preexisting_manager,
        args=(str(tmp_path), ready, start, result_queue),
    )
    process.start()
    try:
        assert ready.wait(10)
        with ca.cookie_store_write_lock(tmp_path):
            ca.replace_active_cookiefile(data=b"new-complete", store=tmp_path)
            start.set()
            with pytest.raises(queue.Empty):
                result_queue.get(timeout=0.4)
        result, observed = result_queue.get(timeout=10)
        assert result[0] is True
        assert observed == [b"new-complete"]
    finally:
        start.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_pid_change_resets_inherited_reentrancy_and_lock_maps(
        tmp_path, monkeypatch):
    key = str(tmp_path.resolve())
    ca._STORE_LOCK_DEPTHS.values = {key: 1}
    inherited_locks = ca._STORE_LOCKS
    real_pid = os.getpid()
    monkeypatch.setattr(ca.os, "getpid", lambda: real_pid + 1000)

    with ca.cookie_store_write_lock(tmp_path):
        pass

    assert (tmp_path / ".cookie-store.lock").exists()
    assert ca._STORE_LOCKS is not inherited_locks
    assert ca._STORE_LOCK_DEPTHS.values == {}


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX os.fork/flock")
def test_posix_fork_child_cannot_bypass_inherited_transaction(tmp_path):
    read_fd, write_fd = os.pipe()
    child_pid = None
    try:
        with ca.cookie_store_write_lock(tmp_path):
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    with ca.cookie_store_write_lock(tmp_path):
                        os.write(write_fd, b"entered")
                    os._exit(0)
                except BaseException:
                    os._exit(2)
            os.close(write_fd)
            write_fd = -1
            readable, _, _ = select.select([read_fd], [], [], 0.4)
            assert readable == []
        readable, _, _ = select.select([read_fd], [], [], 10)
        assert readable == [read_fd]
        assert os.read(read_fd, 64) == b"entered"
        waited, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert waited > 0
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if child_pid:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)


@pytest.mark.skipif(os.name != "nt", reason="Windows lock wait semantics")
def test_windows_waiter_beyond_five_seconds_reloads_winner_once(tmp_path):
    active = tmp_path / "active.txt"
    active.write_bytes(b"winner-complete")
    ca.save_state(tmp_path, ca.CookieState())
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    start = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_multiprocess_long_wait_manager,
        args=(str(tmp_path), ready, start, result_queue),
    )
    process.start()
    try:
        assert ready.wait(10)
        with ca.cookie_store_write_lock(tmp_path):
            ca.save_state(
                tmp_path,
                ca.CookieState(
                    path=str(active),
                    source="winner",
                    fetched_at=1000.0,
                    last_validated_at=1000.0,
                    valid=True,
                ),
            )
            start.set()
            time.sleep(5.4)
            with pytest.raises(queue.Empty):
                result_queue.get_nowait()
        status, result, provider_calls = result_queue.get(timeout=10)
        assert status == "ok"
        assert result == str(active)
        assert provider_calls == 0
    finally:
        start.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_clear_preserves_stable_transaction_lock_identity(tmp_path):
    active = tmp_path / "active.txt"
    active.write_text("complete-cookie-jar", encoding="utf-8")
    with ca.cookie_store_write_lock(tmp_path):
        pass
    lock_path = tmp_path / ".cookie-store.lock"
    assert lock_path.exists()
    before = lock_path.stat()

    ca.clear_cookie_store(tmp_path)

    after = lock_path.stat()
    assert lock_path.exists()
    if os.name == "posix":
        assert after.st_ino == before.st_ino


@pytest.mark.parametrize("failure", ["chmod", "parent_fsync"])
def test_valid_state_post_replace_failure_remains_invalid_for_new_manager(
        tmp_path, monkeypatch, failure):
    import secureio

    active = tmp_path / "active.txt"
    active.write_bytes(b"complete-cookie-jar")
    ca.save_state(tmp_path, ca.CookieState())
    if failure == "chmod":
        real_tighten = secureio.ensure_private_file

        def fail_final_state_chmod(path, mode=0o600):
            if Path(path).name == "state.json":
                raise OSError("state chmod failed")
            return real_tighten(path, mode)

        monkeypatch.setattr(secureio, "ensure_private_file", fail_final_state_chmod)
    else:
        real_fsync_parent = secureio._fsync_parent

        def fail_state_parent_fsync(path):
            if Path(path).name == "state.json":
                raise OSError("state parent fsync failed")
            return real_fsync_parent(path)

        monkeypatch.setattr(secureio, "_fsync_parent", fail_state_parent_fsync)

    valid = ca.CookieState(
        path=str(active),
        source="file",
        fetched_at=1000.0,
        last_validated_at=1000.0,
        valid=True,
    )
    with pytest.raises(OSError):
        ca.save_state(tmp_path, valid)

    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert raw["valid"] is True
    assert (tmp_path / ".state-invalid").exists()
    assert ca.load_state(tmp_path) == ca.CookieState()
    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        providers=[],
        clock=lambda: 1001.0,
        store=tmp_path,
    )
    assert manager.status()["valid"] is False


def test_valid_state_directory_open_failure_keeps_both_guards(
        tmp_path, monkeypatch):
    active = tmp_path / "active.txt"
    active.write_bytes(b"complete-cookie-jar")
    ca.save_state(tmp_path, ca.CookieState())

    def fail_directory_open(_store):
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(
        ca, "_open_state_commit_directory", fail_directory_open, raising=False
    )
    with pytest.raises(ca.StatePersistenceError):
        ca.save_state(
            tmp_path,
            ca.CookieState(
                path=str(active),
                source="file",
                fetched_at=1000.0,
                last_validated_at=1000.0,
                valid=True,
            ),
        )

    assert (tmp_path / ".state-invalid").exists()
    assert (tmp_path / ".state-invalid.backup").exists()
    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        providers=[],
        clock=lambda: 1001.0,
        store=tmp_path,
    )
    assert manager.status()["valid"] is False


def test_valid_state_directory_fsync_failure_keeps_backup_guard(
        tmp_path, monkeypatch):
    active = tmp_path / "active.txt"
    active.write_bytes(b"complete-cookie-jar")
    ca.save_state(tmp_path, ca.CookieState())

    def fail_directory_fsync(_directory_fd):
        raise OSError("directory fsync failed")

    monkeypatch.setattr(
        ca, "_fsync_state_commit_directory", fail_directory_fsync, raising=False
    )
    with pytest.raises(ca.StatePersistenceError):
        ca.save_state(
            tmp_path,
            ca.CookieState(
                path=str(active),
                source="file",
                fetched_at=1000.0,
                last_validated_at=1000.0,
                valid=True,
            ),
        )

    assert not (tmp_path / ".state-invalid").exists()
    assert (tmp_path / ".state-invalid.backup").exists()
    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        providers=[],
        clock=lambda: 1001.0,
        store=tmp_path,
    )
    assert manager.status()["valid"] is False


def test_valid_state_backup_unlink_failure_keeps_backup_guard(
        tmp_path, monkeypatch):
    active = tmp_path / "active.txt"
    active.write_bytes(b"complete-cookie-jar")
    ca.save_state(tmp_path, ca.CookieState())
    real_unlink = Path.unlink

    def fail_backup_unlink(path, *args, **kwargs):
        if path.name == ".state-invalid.backup":
            raise OSError("backup guard unlink failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_unlink)
    with pytest.raises(ca.StatePersistenceError):
        ca.save_state(
            tmp_path,
            ca.CookieState(
                path=str(active),
                source="file",
                fetched_at=1000.0,
                last_validated_at=1000.0,
                valid=True,
            ),
        )

    assert not (tmp_path / ".state-invalid").exists()
    assert (tmp_path / ".state-invalid.backup").exists()
    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        providers=[],
        clock=lambda: 1001.0,
        store=tmp_path,
    )
    assert manager.status()["valid"] is False


def test_successful_valid_state_commit_is_reusable(tmp_path):
    active = tmp_path / "active.txt"
    active.write_bytes(b"complete-cookie-jar")
    ca.save_state(
        tmp_path,
        ca.CookieState(
            path=str(active),
            source="file",
            fetched_at=1000.0,
            last_validated_at=1000.0,
            valid=True,
        ),
    )

    assert not (tmp_path / ".state-invalid").exists()
    assert not (tmp_path / ".state-invalid.backup").exists()
    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        providers=[],
        validator=lambda _path: pytest.fail("fresh committed state was revalidated"),
        clock=lambda: 1001.0,
        store=tmp_path,
    )
    assert manager.status()["valid"] is True
    assert manager.get_cookiefile() == str(active)


def test_reappearing_backup_guard_forces_safe_revalidation(tmp_path):
    active = tmp_path / "active.txt"
    active.write_bytes(b"complete-cookie-jar")
    ca.save_state(
        tmp_path,
        ca.CookieState(
            path=str(active),
            source="file",
            fetched_at=1000.0,
            last_validated_at=1000.0,
            valid=True,
        ),
    )
    ca.atomic_replace_private(
        tmp_path / ".state-invalid.backup",
        b"crash-equivalent invalid\n",
    )
    calls = {"provider": 0, "validator": 0}

    class ActiveProvider:
        name = "file"

        def available(self):
            return True

        def acquire(self):
            calls["provider"] += 1
            return str(active)

    def validate(path):
        calls["validator"] += 1
        return path == str(active)

    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        providers=[ActiveProvider()],
        validator=validate,
        clock=lambda: 1001.0,
        store=tmp_path,
    )
    assert manager.status()["valid"] is False
    assert manager.get_cookiefile() == str(active)
    assert calls == {"provider": 1, "validator": 1}
    assert not (tmp_path / ".state-invalid").exists()
    assert not (tmp_path / ".state-invalid.backup").exists()


def test_manager_construction_waits_for_atomic_store_update(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    built = threading.Event()

    def hold_writer_lock():
        with ca.cookie_store_write_lock(tmp_path):
            entered.set()
            assert release.wait(5)

    def build_manager():
        ca.CookieManager(config={**ca._DEFAULTS, "enabled": True}, store=tmp_path)
        built.set()

    holder = threading.Thread(target=hold_writer_lock)
    constructor = threading.Thread(target=build_manager)
    holder.start()
    assert entered.wait(5)
    constructor.start()
    try:
        assert not built.wait(0.2)
    finally:
        release.set()
    holder.join(5)
    constructor.join(5)
    assert built.is_set()


def test_validate_existing_holds_lock_against_atomic_store_writer(tmp_path):
    active = tmp_path / "active.txt"
    active.write_text("complete-cookie-jar", encoding="utf-8")
    validator_entered = threading.Event()
    allow_validator = threading.Event()
    writer_entered = threading.Event()

    def blocking_validator(_path):
        validator_entered.set()
        assert allow_validator.wait(5)
        return True

    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        validator=blocking_validator,
        store=tmp_path,
    )
    result = []
    validation = threading.Thread(
        target=lambda: result.append(manager.validate_existing_file())
    )

    def writer():
        with ca.cookie_store_write_lock(tmp_path):
            writer_entered.set()

    validation.start()
    assert validator_entered.wait(5)
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    try:
        assert not writer_entered.wait(0.2)
    finally:
        allow_validator.set()
    validation.join(5)
    writer_thread.join(5)
    assert result and result[0][0] is True
    assert writer_entered.is_set()


def test_refresh_holds_one_lock_across_invalidate_and_atomic_reacquire(tmp_path):
    active = tmp_path / "active.txt"
    active.write_text("complete-cookie-jar", encoding="utf-8")
    ca.save_state(
        tmp_path,
        ca.CookieState(
            path=str(active),
            source="file",
            fetched_at=1.0,
            last_validated_at=1.0,
            valid=True,
        ),
    )
    provider_entered = threading.Event()
    allow_provider = threading.Event()
    writer_entered = threading.Event()

    class BlockingProvider(_FakeProvider):
        def acquire(self):
            self.calls += 1
            provider_entered.set()
            assert allow_provider.wait(5)
            return self._path

    manager = ca.CookieManager(
        config={**ca._DEFAULTS, "enabled": True},
        providers=[BlockingProvider("file", str(active))],
        validator=lambda _path: True,
        store=tmp_path,
    )
    refreshed = []
    refresh = threading.Thread(
        target=lambda: refreshed.append(manager.refresh_cookiefile())
    )

    def writer():
        with ca.cookie_store_write_lock(tmp_path):
            writer_entered.set()

    refresh.start()
    assert provider_entered.wait(5)
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    try:
        assert not writer_entered.wait(0.2)
    finally:
        allow_provider.set()
    refresh.join(5)
    writer_thread.join(5)
    assert refreshed == [str(active)]
    assert writer_entered.is_set()


def test_clear_cookie_store_waits_for_atomic_writer_and_resets_state(tmp_path):
    active = tmp_path / "active.txt"
    active.write_text("complete-cookie-jar", encoding="utf-8")
    ca.save_state(
        tmp_path,
        ca.CookieState(path=str(active), source="file", valid=True),
    )
    writer_entered = threading.Event()
    allow_writer = threading.Event()
    clear_done = threading.Event()

    def writer():
        with ca.cookie_store_write_lock(tmp_path):
            writer_entered.set()
            assert allow_writer.wait(5)

    def clear():
        ca.clear_cookie_store(tmp_path)
        clear_done.set()

    writer_thread = threading.Thread(target=writer)
    clear_thread = threading.Thread(target=clear)
    writer_thread.start()
    assert writer_entered.wait(5)
    clear_thread.start()
    try:
        assert not clear_done.wait(0.2)
        assert active.exists()
    finally:
        allow_writer.set()
    writer_thread.join(5)
    clear_thread.join(5)
    assert clear_done.is_set()
    assert not active.exists()
    assert ca.load_state(tmp_path) == ca.CookieState()


def test_consent_shown_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    cfg = {**ca._DEFAULTS, "enabled": True}
    assert ca.maybe_show_consent(cfg) is True
    # Marker must exist after first call; banner text only on TTY (CLI).
    assert (tmp_path / "cookies" / ".consent").exists()
    first = capsys.readouterr().out.lower()
    if first:  # TTY path
        assert "burner" in first or "terms" in first
    assert ca.maybe_show_consent(cfg) is False  # second call silent


def test_manager_warns_when_no_usable_cookies(caplog, tmp_path):
    import logging
    from cookies_auth import CookieManager

    class DeadProvider:
        name = "file"
        def available(self): return False
        def acquire(self): return None

    mgr = CookieManager(config={"enabled": True}, providers=[DeadProvider()], store=tmp_path)
    with caplog.at_level(logging.WARNING, logger="yt_final"):
        assert mgr.get_cookiefile() is None
    assert any("no usable cookies" in r.message for r in caplog.records), \
        "auth-on-but-empty must surface a clear warning"
