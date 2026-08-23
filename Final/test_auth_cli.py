import os
import threading

import cookies_auth
import secureio
import yt_final


def test_auth_status_reports_disabled_by_default(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    rc = yt_final._cmd_auth(type("A", (), {"auth_cmd": "status"})())
    out = capsys.readouterr().out.lower()
    assert rc == 0
    assert "enabled" in out


def test_auth_import_copies_file_into_store(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    src = tmp_path / "src.txt"; src.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    rc = yt_final._cmd_auth(type("A", (), {"auth_cmd": "import", "path": str(src)})())
    assert rc == 0
    assert (tmp_path / "cookies" / "active.txt").exists()


def test_auth_import_chmods_600(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    src = tmp_path / "src.txt"; src.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    rc = yt_final._cmd_auth(type("A", (), {"auth_cmd": "import", "path": str(src)})())
    assert rc == 0
    dest = tmp_path / "cookies" / "active.txt"
    assert dest.exists()
    if os.name == "posix":
        assert (dest.stat().st_mode & 0o777) == 0o600


def test_auth_import_atomic_failure_preserves_previous_cookiefile(
        monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    store = tmp_path / "cookies"
    store.mkdir(parents=True)
    active = store / "active.txt"
    active.write_bytes(b"old-complete")
    source = tmp_path / "src.txt"
    source.write_bytes(b"new-complete")
    monkeypatch.setattr(
        secureio.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("boom")),
    )

    rc = yt_final._cmd_auth(
        type("A", (), {"auth_cmd": "import", "path": str(source)})()
    )

    assert rc == 1
    assert active.read_bytes() == b"old-complete"


def test_auth_import_atomically_resets_stale_validation_state(
        monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    store = tmp_path / "cookies"
    source = tmp_path / "src.txt"
    source.write_bytes(b"new-complete")
    cookies_auth.save_state(
        store,
        cookies_auth.CookieState(
            path=str(store / "active.txt"),
            source="file",
            fetched_at=1.0,
            last_validated_at=1.0,
            valid=True,
        ),
    )

    rc = yt_final._cmd_auth(
        type("A", (), {"auth_cmd": "import", "path": str(source)})()
    )

    assert rc == 0
    assert cookies_auth.load_state(store) == cookies_auth.CookieState()


def test_auth_refresh_holds_one_store_lock_across_invalidate_and_reacquire(
        monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    store = tmp_path / "cookies"
    store.mkdir(parents=True)
    active = store / "active.txt"
    active.write_text("complete-cookie-jar", encoding="utf-8")
    cookies_auth.save_state(
        store,
        cookies_auth.CookieState(path=str(active), source="file", valid=True),
    )
    manager = cookies_auth.CookieManager(
        config={**cookies_auth._DEFAULTS, "enabled": True},
        providers=[],
        validator=lambda _path: True,
        store=store,
    )
    invalidated = threading.Event()
    allow_refresh = threading.Event()
    writer_entered = threading.Event()
    original_notify = manager.notify_auth_failure

    def blocked_notify(*args, **kwargs):
        original_notify(*args, **kwargs)
        invalidated.set()
        assert allow_refresh.wait(5)

    manager.notify_auth_failure = blocked_notify
    monkeypatch.setattr(
        cookies_auth,
        "build_default_manager",
        lambda output_dir=None: manager,
    )
    refresh_thread = threading.Thread(
        target=lambda: yt_final._cmd_auth(
            type("A", (), {"auth_cmd": "refresh"})()
        )
    )

    def writer():
        with cookies_auth.cookie_store_write_lock(store):
            writer_entered.set()

    refresh_thread.start()
    assert invalidated.wait(5)
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    try:
        assert not writer_entered.wait(0.2)
    finally:
        allow_refresh.set()
    refresh_thread.join(5)
    writer_thread.join(5)
    assert writer_entered.is_set()


def test_auth_clear_removes_store(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    store = tmp_path / "cookies"; store.mkdir(parents=True)
    (store / "active.txt").write_text("x", encoding="utf-8")
    rc = yt_final._cmd_auth(type("A", (), {"auth_cmd": "clear"})())
    assert rc == 0
    assert not (store / "active.txt").exists()


def test_auth_clear_waits_for_atomic_store_writer(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    store = tmp_path / "cookies"
    store.mkdir(parents=True)
    active = store / "active.txt"
    active.write_text("complete-cookie-jar", encoding="utf-8")
    writer_entered = threading.Event()
    allow_writer = threading.Event()
    clear_done = threading.Event()

    def writer():
        with cookies_auth.cookie_store_write_lock(store):
            writer_entered.set()
            assert allow_writer.wait(5)

    def clear():
        yt_final._cmd_auth(type("A", (), {"auth_cmd": "clear"})())
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


def test_auth_clear_reports_failure_and_preserves_jars_when_state_save_fails(
        monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    store = tmp_path / "cookies"
    store.mkdir(parents=True)
    active = store / "active.txt"
    active.write_bytes(b"complete-cookie-jar")
    monkeypatch.setattr(
        cookies_auth,
        "save_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state full")),
    )

    rc = yt_final._cmd_auth(type("A", (), {"auth_cmd": "clear"})())

    assert rc == 1
    assert active.read_bytes() == b"complete-cookie-jar"
