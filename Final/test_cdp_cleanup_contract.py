"""Behavioural contract for what get_all_cookies_via_cdp does when cleanup fails.

These drive the real function with the browser/socket layer faked out, so they fail if the
DECISION changes -- unlike a test that greps the module's own source, which keeps passing
against a body that has been inverted.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _cdp_client  # noqa: E402


class _FakeSocket:
    def close(self):
        pass


def _drive(monkeypatch, tmp_path, *, terminated=True, cleaned=True, cookies=None,
           exe="C:/fake/chrome.exe", read_raises=None):
    """Run get_all_cookies_via_cdp against a faked browser and return (result, reason)."""
    userdir = tmp_path / "snap"
    userdir.mkdir(exist_ok=True)

    monkeypatch.setattr(_cdp_client, "_drain_windows_cleanup_registry",
                        lambda **kw: True, raising=False)
    monkeypatch.setattr(_cdp_client, "_find_exe", lambda name: exe)
    monkeypatch.setattr(_cdp_client, "_default_user_data_dir", lambda name: tmp_path / "src")
    monkeypatch.setattr(_cdp_client, "_make_private_tempdir", lambda *a, **k: userdir)
    monkeypatch.setattr(_cdp_client, "_snapshot_profile", lambda *a, **k: True)
    monkeypatch.setattr(_cdp_client, "_launch_browser", lambda *a, **k: object())
    monkeypatch.setattr(_cdp_client, "_wait_for_owned_endpoint",
                        lambda *a, **k: ("127.0.0.1", 9222, "/ws"))
    monkeypatch.setattr(_cdp_client, "_ws_handshake", lambda *a, **k: _FakeSocket())
    monkeypatch.setattr(_cdp_client, "_ws_send", lambda *a, **k: None)

    def fake_recv(*a, **k):
        if read_raises is not None:
            raise read_raises
        payload = [] if cookies is None else cookies
        return json.dumps({"id": 1, "result": {"cookies": payload}})

    monkeypatch.setattr(_cdp_client, "_ws_recv", fake_recv)
    monkeypatch.setattr(_cdp_client, "_terminate_process_tree", lambda *a, **k: terminated)
    monkeypatch.setattr(_cdp_client, "_cleanup_private_tempdir", lambda *a, **k: cleaned)

    result = _cdp_client.get_all_cookies_via_cdp("chrome", timeout=20.0)
    return result, _cdp_client.last_snapshot_failure()


ONE_COOKIE = [{"domain": ".youtube.com", "name": "SID", "value": "v",
               "path": "/", "secure": True, "expires": 0}]


def test_cookies_survive_a_snapshot_that_cannot_be_deleted(monkeypatch, tmp_path):
    """A successful read must not be thrown away because the temp copy would not delete.

    On Windows this is routine and permanent: Chrome creates the snapshot's Cache and
    Shared Dictionary subdirectories with a DACL granting only an AppContainer SID, so the
    owning user has no ACE and deletion is denied forever. Discarding the cookies protected
    nothing -- the snapshot stays on disk either way -- it just meant a user who signed in
    correctly was told they had not.
    """
    result, reason = _drive(monkeypatch, tmp_path, terminated=True, cleaned=False,
                            cookies=ONE_COOKIE)
    assert result is not None, "a successful read was discarded because cleanup failed"
    assert result[0]["name"] == "SID"
    assert reason and "could not delete" in reason, (
        "the leftover snapshot holds the user's cookies; its location must be recorded")


def test_a_browser_that_will_not_die_still_fails_closed(monkeypatch, tmp_path):
    """The other half must NOT relax: an unkillable browser is an ongoing exposure.

    This is the mutation that a source-grep test cannot catch -- it passes happily against a
    body that returns the cookies here.
    """
    result, _ = _drive(monkeypatch, tmp_path, terminated=False, cleaned=True,
                       cookies=ONE_COOKIE)
    assert result is None, "a browser that could not be terminated must fail closed"


def test_a_failed_read_is_not_reported_as_a_successful_one(monkeypatch, tmp_path):
    """When the READ failed, the recorded reason must not open with "read cookies".

    webserver.py surfaces this string to the user verbatim. The leftover-snapshot recorder
    used to run unconditionally, so a read that failed AND a cleanup that failed produced a
    message asserting the cookies had been read -- the opposite of what happened.
    """
    boom = OSError("websocket died mid-read")
    result, reason = _drive(monkeypatch, tmp_path, terminated=True, cleaned=False,
                            read_raises=boom)
    assert result is None
    assert reason is not None
    assert not reason.startswith("OSError: read cookies"), (
        f"a failed read was reported as a successful one: {reason}")
    assert "websocket died mid-read" in reason, (
        "the real cause of the failed read was overwritten")


def test_an_early_return_does_not_inherit_the_previous_attempts_reason(monkeypatch, tmp_path):
    """Attempt N must not be explained by attempt N-1's failure.

    The reason was cleared inside _snapshot_profile, but several early returns (no browser
    exe among them) never reach that call, so a stale message survived and was shown as
    though it described the new attempt.
    """
    _, first = _drive(monkeypatch, tmp_path, terminated=True, cleaned=False,
                      cookies=ONE_COOKIE)
    assert first and "could not delete" in first

    result, second = _drive(monkeypatch, tmp_path, exe=None)
    assert result is None
    assert second is None, f"stale reason leaked into a new attempt: {second}"
