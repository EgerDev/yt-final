import sys
import types
import yt_final


class _StubMgr:
    def __init__(self, path): self._p = path; self.notified = 0; self.gets = 0
    def get_cookiefile(self, reason="auto"): self.gets += 1; return self._p
    def notify_auth_failure(self, url=None): self.notified += 1


def _downloader(monkeypatch, mgr):
    det = yt_final.get_detector()
    d = yt_final.YouTubeDownloader(det, out_dir="./downloads")
    d.cookie_manager = mgr
    return d


def test_auth_gate_triggers_refresh_and_retry(monkeypatch, tmp_path):
    """First extract raises a bot error; manager supplies fresh cookies; retry
    uses them and succeeds."""
    cf = str(tmp_path / "fresh.txt"); open(cf, "w").close()
    mgr = _StubMgr(cf)
    d = _downloader(monkeypatch, mgr)

    calls = {"n": 0}

    class _FakeYDL:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def extract_info(self, url, download=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("Sign in to confirm you're not a bot")
            return {"id": "x", "title": "t", "requested_downloads": []}

        def process_ie_result(self, info, download=True): return info
        def prepare_filename(self, info): return str(tmp_path / "x.mp4")

    # download() does a LOCAL lazy `import yt_dlp`, so monkeypatching the
    # yt_final module attribute has no effect (the local import rebinds every
    # call). Inject a fake module into sys.modules instead, so the lazy
    # `import yt_dlp` inside download() picks it up.
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_FakeYDL))
    # engine treats "bot" as retryable and should ask the manager for cookies
    d.download("https://youtu.be/x")
    assert mgr.notified >= 1
    assert mgr.gets >= 1
    assert calls["n"] >= 2  # retried after refresh


def test_no_manager_cookies_preserves_current_behavior(monkeypatch, tmp_path):
    mgr = _StubMgr(None)  # manager yields nothing
    d = _downloader(monkeypatch, mgr)

    class _AlwaysBot:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **k): raise Exception("Sign in to confirm you're not a bot")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_AlwaysBot))
    out = d.download("https://youtu.be/x")
    assert out == []  # fails cleanly, no infinite loop
