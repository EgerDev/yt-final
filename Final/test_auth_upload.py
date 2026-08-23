import os
import stat
import threading

import pytest

import webserver


NETSCAPE = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tabc\n"


def test_import_writes_active_txt(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    res = webserver.auth_import_cookies(NETSCAPE)
    assert res["ok"] is True
    active = tmp_path / "cookies" / "active.txt"
    assert active.exists() and "SID" in active.read_text(encoding="utf-8")


def test_import_creates_active_owner_only_before_final_tightening(
        monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX mode bits; Windows profile ACLs are authoritative")
    import secureio

    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    monkeypatch.setattr(secureio.os, "chmod", lambda *_: None)
    old_umask = os.umask(0)
    try:
        result = webserver.auth_import_cookies(NETSCAPE)
    finally:
        os.umask(old_umask)

    active = tmp_path / "cookies" / "active.txt"
    assert result["ok"] is True
    assert stat.S_IMODE(active.stat().st_mode) == 0o600


def test_failed_import_replace_preserves_previous_cookiefile(monkeypatch, tmp_path):
    import secureio

    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    active = tmp_path / "cookies" / "active.txt"
    active.parent.mkdir(parents=True)
    active.write_bytes(b"old-complete")
    monkeypatch.setattr(
        secureio.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("boom")),
    )

    result = webserver.auth_import_cookies(NETSCAPE)

    assert result["ok"] is False
    assert active.read_bytes() == b"old-complete"


def test_import_strict_state_failure_preserves_previous_cookiefile(
        monkeypatch, tmp_path):
    import cookies_auth

    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    active = tmp_path / "cookies" / "active.txt"
    active.parent.mkdir(parents=True)
    active.write_bytes(b"old-complete")
    monkeypatch.setattr(
        cookies_auth,
        "save_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state full")),
    )

    result = webserver.auth_import_cookies(NETSCAPE)

    assert result["ok"] is False
    assert active.read_bytes() == b"old-complete"


def test_import_post_publication_failure_leaves_state_invalid(
        monkeypatch, tmp_path):
    import cookies_auth
    import secureio

    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    store = tmp_path / "cookies"
    active = store / "active.txt"
    store.mkdir(parents=True)
    active.write_bytes(b"old-complete")
    cookies_auth.save_state(
        store,
        cookies_auth.CookieState(path=str(active), source="file", valid=True),
    )
    real_fsync_parent = secureio._fsync_parent

    def fail_after_active_publication(path):
        if path.name == "active.txt":
            raise OSError("directory fsync failed")
        return real_fsync_parent(path)

    monkeypatch.setattr(secureio, "_fsync_parent", fail_after_active_publication)

    result = webserver.auth_import_cookies(NETSCAPE)

    assert result["ok"] is False
    assert active.read_bytes() == NETSCAPE.encode("utf-8")
    assert cookies_auth.load_state(store) == cookies_auth.CookieState()


def test_atomic_store_writer_holds_lock_through_state_update(
        monkeypatch, tmp_path):
    import cookies_auth

    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    first_at_state_update = threading.Event()
    allow_first_state_update = threading.Event()
    second_done = threading.Event()
    real_save_state = cookies_auth.save_state

    def blocked_first_save(store, state):
        if threading.current_thread().name == "first-cookie-writer":
            first_at_state_update.set()
            assert allow_first_state_update.wait(5)
        return real_save_state(store, state)

    monkeypatch.setattr(cookies_auth, "save_state", blocked_first_save)
    first = NETSCAPE.replace("abc", "first")
    second = NETSCAPE.replace("abc", "second")
    results = {}

    def import_first():
        results["first"] = webserver.auth_import_cookies(first)

    def import_second():
        results["second"] = webserver.auth_import_cookies(second)
        second_done.set()

    first_thread = threading.Thread(
        target=import_first,
        name="first-cookie-writer",
    )
    second_thread = threading.Thread(
        target=import_second,
        name="second-cookie-writer",
    )
    first_thread.start()
    assert first_at_state_update.wait(5)
    second_thread.start()
    try:
        assert not second_done.wait(0.2)
        # Fail-closed ordering publishes invalid state before cookie bytes. While
        # writer one is paused in that strict state save, no new active jar exists.
        assert not (tmp_path / "cookies" / "active.txt").exists()
    finally:
        allow_first_state_update.set()
    first_thread.join(5)
    second_thread.join(5)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert results["first"]["ok"] is True
    assert results["second"]["ok"] is True
    assert "second" in (
        tmp_path / "cookies" / "active.txt"
    ).read_text(encoding="utf-8")


def test_import_rejects_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    res = webserver.auth_import_cookies("   \n  ")
    assert res["ok"] is False and "error" in res
    assert not (tmp_path / "cookies" / "active.txt").exists()


def test_import_rejects_oversize(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    res = webserver.auth_import_cookies("x" * (1_000_001))
    assert res["ok"] is False and "error" in res


def test_import_rejects_non_netscape(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    res = webserver.auth_import_cookies("just some random text\nno tabs here\n")
    assert res["ok"] is False
    assert "netscape" in (res.get("error") or "").lower()


def test_import_rejects_without_youtube_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    res = webserver.auth_import_cookies(
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tfoo\tbar\n"
    )
    assert res["ok"] is False
    assert "youtube" in (res.get("error") or "").lower()
