import io
import sys
import types

import webserver
import yt_final


def _cp1252_strict_stdout():
    """A stdout that behaves like a pipe/file redirected under a non-UTF-8 (Windows cp1252)
    locale: the banner's U+26A0 '⚠' glyph is unencodable, so a bare print('⚠…') raises
    UnicodeEncodeError. errors='strict' makes that failure hard rather than silently mangled."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def test_ready_banner_survives_cp1252_stdout(monkeypatch):
    """serve()'s startup banner must not abort with UnicodeEncodeError when stdout is a
    cp1252/strict pipe. Regression: encoding '\\u26a0' to cp1252 raises 'charmap' codec can't
    encode character, which — being unwrapped — aborted serve() before serve_forever().

    The slow-transport warning supplies a guaranteed non-ASCII glyph; _has_lock=True skips the
    run-lock notice so no real journal is needed."""
    fake_state = types.SimpleNamespace(allow_public=False, _has_lock=True)
    monkeypatch.setattr(yt_final, "_pooled_transport_available", lambda: False)
    stream = _cp1252_strict_stdout()
    monkeypatch.setattr(sys, "stdout", stream)

    # The bug surfaces as a raised UnicodeEncodeError here; the fix makes stdout encoding-safe
    # first, so this returns normally.
    webserver._print_ready_banner(fake_state, "http://127.0.0.1:8765/")

    stream.flush()
    out = stream.buffer.getvalue().decode("utf-8", "replace")
    # Banner ran to completion (didn't die on the first '⚠' line).
    assert "SLOW TRANSPORT" in out
    assert "yt_final is ready" in out


def test_local_startup_banner_retains_loopback_url_without_public_mode(monkeypatch, capsys):
    fake_state = types.SimpleNamespace(allow_public=True, _has_lock=True)
    monkeypatch.setattr(yt_final, "_pooled_transport_available", lambda: True)

    webserver._print_ready_banner(fake_state, "http://127.0.0.1:8765/")

    out = capsys.readouterr().out
    assert "Open:" in out
    assert "http://127.0.0.1:8765/" in out
    assert "PUBLIC/LAN MODE" not in out
    assert "API token" not in out
