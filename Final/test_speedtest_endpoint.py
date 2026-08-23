"""Orchestration tests for webserver.run_speedtest with mocked network fetchers (no sockets)."""
import webserver


class FakeState:
    """Minimal ServerState stand-in that records desync lifecycle calls and the current mode."""
    def __init__(self, desync_enabled=False, running=False, mode="oob"):
        self.output_dir = "./downloads"
        self._engine = None
        self.pot_base_url = None
        self.pot_mode = "auto"
        self.desync_port = 8799
        self.desync_enabled = desync_enabled
        self.desync_mode = mode
        self._running = running
        self.last_mode = mode if running else None
        self.calls = []

    def desync_running(self):
        return self._running

    def start_desync(self, mode=None):
        self.calls.append("start")
        self._running = True
        self.last_mode = mode or self.desync_mode

    def restart_desync(self, mode):
        self.calls.append(f"restart:{mode}")
        self._running = True
        self.last_mode = mode
        return True

    def stop_desync(self):
        self.calls.append("stop")
        self._running = False

    def ensure_desync(self):
        self.calls.append("ensure")
        self._running = self.desync_enabled
        if self._running:
            self.last_mode = self.desync_mode

    def desync_health(self):
        if not self.desync_enabled:
            return "off"
        return "running" if self._running else "unreachable"


def _install(monkeypatch, st, direct=1.0, bypass_by_mode=None, ceiling=13.2, gcs=12.0,
             media=True, proxy_mibs=9.0):
    bypass_by_mode = bypass_by_mode if bypass_by_mode is not None else {"oob": 8.0}
    bypass_url = f"http://127.0.0.1:{st.desync_port}"

    def fake_fetch(url, proxy=None, **kw):
        if url == webserver.SPEEDTEST_CEILING_URL:
            return ceiling
        if url in webserver.SPEEDTEST_GCS_URLS:      # same-owner control candidate list
            return gcs
        if proxy is None:
            return direct
        if proxy == bypass_url:
            return bypass_by_mode.get(st.last_mode)   # depends on the mode the sweep set
        return proxy_mibs                             # user proxy leg (re-extracted url)

    def fake_extract(state, cookiefile, proxy=None):
        if not media:
            return None
        return {"url": "https://x.googlevideo.com/videoplayback?id=1&proxy=" + str(bool(proxy)),
                "headers": {"User-Agent": "x"}, "id": "aqz"}

    monkeypatch.setattr(webserver, "_timed_media_fetch", fake_fetch)
    monkeypatch.setattr(webserver, "_speedtest_extract_media", fake_extract)
    monkeypatch.setattr(webserver, "_speedtest_egress", lambda: {"org": "Verizon Business"})
    monkeypatch.setattr(webserver, "_speedtest_engine_caps",
                        lambda s: {"js_runtime": True, "po_provider": "running", "ffmpeg": True})
    import cookies_auth
    monkeypatch.setattr(cookies_auth, "build_default_manager", lambda *a, **k: None)


def test_median_and_confidence_helpers():
    assert webserver._median([1.0, 3.0, 2.0]) == 2.0
    assert webserver._median([2.0, 4.0]) == 3.0
    assert webserver._median([None, 0.0, 5.0]) == 5.0
    assert webserver._median([None, None]) is None
    assert webserver._confidence([8.0, 8.2], [1.0, 1.1]) == "high"
    assert webserver._confidence([8.0, 20.0], [1.0, 1.1]) == "low"   # noisy leg
    assert webserver._confidence([8.0]) == "low"                     # single sample


def _install_varying(monkeypatch, st, direct_seq, bypass=20.0, ceiling=110.0, gcs=100.0):
    """Like _install but the DIRECT leg returns a sequence (to model a jittery CDN)."""
    seq = iter(direct_seq)
    bypass_url = f"http://127.0.0.1:{st.desync_port}"

    def fake_fetch(url, proxy=None, **kw):
        if url == webserver.SPEEDTEST_CEILING_URL:
            return ceiling
        if url in webserver.SPEEDTEST_GCS_URLS:
            return gcs
        if proxy is None:
            return next(seq, direct_seq[-1])
        if proxy == bypass_url:
            return bypass
        return bypass

    monkeypatch.setattr(webserver, "_timed_media_fetch", fake_fetch)
    monkeypatch.setattr(webserver, "_speedtest_extract_media",
                        lambda state, cookiefile, proxy=None: {
                            "url": "https://x.googlevideo.com/v?id=1", "headers": {"User-Agent": "x"}, "id": "a"})
    monkeypatch.setattr(webserver, "_speedtest_egress", lambda: {"org": "Home Fiber"})
    monkeypatch.setattr(webserver, "_speedtest_engine_caps",
                        lambda s: {"js_runtime": True, "po_provider": "running", "ffmpeg": True})
    import cookies_auth
    monkeypatch.setattr(cookies_auth, "build_default_manager", lambda *a, **k: None)


def test_run_speedtest_surfaces_direct_range_when_jittery(monkeypatch):
    # The real Windows behaviour we measured: back-to-back direct probes read 14 then 62 MiB/s.
    # run_speedtest must record lo/hi and analyze must surface the variability note.
    st = FakeState()
    _install_varying(monkeypatch, st, direct_seq=[14.0, 62.0])
    res = webserver.run_speedtest(st, user_proxy=None)
    mm = res["measurements"]
    assert mm["youtube_direct_lo"] == 14.0 and mm["youtube_direct_hi"] == 62.0
    assert mm["confidence"] == "low"
    assert res["variability"] and res["variability"]["lo"] == 14.0 and res["variability"]["hi"] == 62.0
    assert "14" in res["variability"]["note"] and "62" in res["variability"]["note"]


def _counting_install(monkeypatch, st, direct_fn, bypass=20.0):
    calls = {"direct": 0}
    bypass_url = f"http://127.0.0.1:{st.desync_port}"

    def fake_fetch(url, proxy=None, **kw):
        if url == webserver.SPEEDTEST_CEILING_URL:
            return 110.0
        if url in webserver.SPEEDTEST_GCS_URLS:
            return 100.0
        if proxy is None:
            calls["direct"] += 1
            return direct_fn(calls["direct"])
        return bypass

    monkeypatch.setattr(webserver, "_timed_media_fetch", fake_fetch)
    monkeypatch.setattr(webserver, "_speedtest_extract_media",
                        lambda state, cookiefile, proxy=None: {
                            "url": "https://x.googlevideo.com/v?id=1", "headers": {"User-Agent": "x"}, "id": "a"})
    monkeypatch.setattr(webserver, "_speedtest_egress", lambda: {"org": "Home Fiber"})
    monkeypatch.setattr(webserver, "_speedtest_engine_caps",
                        lambda s: {"js_runtime": True, "po_provider": "running", "ffmpeg": True})
    import cookies_auth
    monkeypatch.setattr(cookies_auth, "build_default_manager", lambda *a, **k: None)
    return calls


def test_run_speedtest_adaptive_resamples_only_when_jittery(monkeypatch):
    # Direct alternates 14/62 forever -> confidence never settles -> resample past the baseline 2,
    # but HARD-CAPPED at 4 so a jittery line can't balloon same-IP load.
    st = FakeState()
    calls = _counting_install(monkeypatch, st, lambda n: 14.0 if n % 2 else 62.0)
    webserver.run_speedtest(st, user_proxy=None)
    assert 2 < calls["direct"] <= 4, calls


def test_run_speedtest_no_adaptive_when_stable(monkeypatch):
    # Stable direct (40 every probe) -> confidence high after the baseline 2 -> no extra sampling.
    st = FakeState()
    calls = _counting_install(monkeypatch, st, lambda n: 40.0)
    webserver.run_speedtest(st, user_proxy=None)
    assert calls["direct"] == 2, calls


def test_orchestration_throttled_fixable_oob(monkeypatch):
    st = FakeState()
    _install(monkeypatch, st, direct=1.0, bypass_by_mode={"oob": 8.0})
    res = webserver.run_speedtest(st, user_proxy=None)
    assert res["verdict"]["state"] == "throttled_fixable"
    assert res["verdict"]["recommended"] == "enable_bypass"
    mm = res["measurements"]
    assert mm["youtube_direct_mibs"] == 1.0 and mm["youtube_bypass_mibs"] == 8.0
    assert mm["bypass_mode"] == "oob"
    assert mm["desync_enabled"] is False


def test_mode_sweep_picks_frag_when_oob_fails(monkeypatch):
    # oob doesn't clear it (1.1) but frag does (8.0) -> the swept winner is frag, carried into payload
    st = FakeState()
    _install(monkeypatch, st, direct=1.0, bypass_by_mode={"oob": 1.1, "frag": 8.0, "frag_oob": 7.0})
    res = webserver.run_speedtest(st, user_proxy=None)
    assert res["measurements"]["bypass_mode"] == "frag"
    assert res["measurements"]["youtube_bypass_mibs"] == 8.0
    assert "restart:oob" in st.calls and "restart:frag" in st.calls
    top = next(r for r in res["recommendations"] if r["id"] == "enable_bypass")
    assert top["action"]["payload"] == {"desync": True, "desync_mode": "frag"}


def test_bypass_active_switch_mode_when_frag_beats_running_oob(monkeypatch):
    # Bypass already on (oob), oob doesn't clear it here but frag does -> bypass_active + switch offer.
    st = FakeState(desync_enabled=True, running=True, mode="oob")
    _install(monkeypatch, st, direct=1.1, bypass_by_mode={"oob": 1.1, "frag": 8.0, "frag_oob": 7.0})
    res = webserver.run_speedtest(st, user_proxy=None)
    assert res["verdict"]["state"] == "bypass_active"
    assert res["verdict"]["recommended"] == "switch_mode"
    mm = res["measurements"]
    assert mm["saved_bypass_mibs"] == 1.1 and mm["bypass_mode"] == "frag"


def test_poisoned_control_does_not_suppress_sweep(monkeypatch):
    # A shaped GCS control (2.0 on a 13 link) must NOT flip throttled_ish false and early-exit on oob;
    # the sweep must use the same trust rule the engine does and reach frag.
    st = FakeState()
    _install(monkeypatch, st, direct=1.5, bypass_by_mode={"oob": 1.6, "frag": 8.0, "frag_oob": 7.0},
             ceiling=13.0, gcs=2.0)
    res = webserver.run_speedtest(st, user_proxy=None)
    assert "restart:frag" in st.calls
    assert res["measurements"]["bypass_mode"] == "frag"
    assert res["verdict"]["state"] == "throttled_fixable"


def test_temp_sweep_restores_desync_when_off(monkeypatch):
    st = FakeState(desync_enabled=False, running=False)
    _install(monkeypatch, st, direct=1.0, bypass_by_mode={"oob": 8.0})
    webserver.run_speedtest(st, user_proxy=None)
    assert any(c.startswith("restart:") for c in st.calls)   # swept the child
    assert st.calls[-1] == "stop"                            # saved state OFF -> stopped last
    assert st._running is False


def test_leaves_running_child_up_when_enabled(monkeypatch):
    st = FakeState(desync_enabled=True, running=True)
    _install(monkeypatch, st, direct=1.0, bypass_by_mode={"oob": 8.0})
    webserver.run_speedtest(st, user_proxy=None)
    assert st._running is True                               # ensure keeps it up (enabled)


def test_user_proxy_reextracts_and_measures(monkeypatch):
    # BUG C1: the proxy leg must RE-EXTRACT through the proxy (IP-locked url), not replay home url.
    st = FakeState()
    _install(monkeypatch, st, direct=1.0, bypass_by_mode={"oob": 1.1}, proxy_mibs=9.0)
    res = webserver.run_speedtest(st, user_proxy="socks5://10.0.0.9:1080")
    mm = res["measurements"]
    assert mm["youtube_proxy_mibs"] == 9.0
    assert mm["proxy_label"] == "socks5://10.0.0.9:1080"
    assert mm["proxy_url"] == "socks5://10.0.0.9:1080"
    assert res["verdict"]["recommended"] == "use_proxy"


def test_failed_direct_fetch_is_none_not_zero(monkeypatch):
    # BUG #1: a failed direct fetch (None) must degrade, never read as an extreme throttle.
    st = FakeState()
    _install(monkeypatch, st, direct=None, bypass_by_mode={"oob": 8.0})
    res = webserver.run_speedtest(st, user_proxy=None)
    assert res["measurements"]["youtube_direct_mibs"] is None
    assert res["verdict"]["state"] == "degraded"
    assert st.calls[-1] == "stop"                            # still restored (saved OFF -> stopped)


def test_extraction_failure_degrades_and_never_touches_child(monkeypatch):
    st = FakeState(desync_enabled=False, running=False)
    _install(monkeypatch, st, media=False)
    res = webserver.run_speedtest(st, user_proxy=None)
    assert res["measurements"]["extraction_ok"] is False
    assert res["verdict"]["state"] == "degraded"
    assert st.calls == []                                    # no media -> desync untouched


def test_speedtest_extract_media_degraded_signal(monkeypatch):
    """Canary health signal: degraded=False when a DIRECT https DASH video stream is present, True
    when only SABR/HLS video remains, None when nothing playable. Regression guard against the
    itag-set check that reported 'ok' on throttled videos (140/251 audio satisfied it)."""
    import yt_dlp
    st = FakeState()

    class _FakeY:
        info = None
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **k): return _FakeY.info

    def run(formats):
        _FakeY.info = {"id": "x", "formats": formats}
        monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeY)
        return webserver._speedtest_extract_media(st, None)

    m = run([{"url": "u", "vcodec": "avc1", "protocol": "https", "height": 720}])
    assert m and m["degraded"] is False                                   # direct DASH -> healthy
    m2 = run([{"url": "u", "vcodec": "avc1", "protocol": "m3u8_native", "height": 720}])
    assert m2 and m2["degraded"] is True                                  # SABR/HLS only -> degraded
    assert run([{"format_id": "sb0", "url": "u", "vcodec": "none"}]) is None   # storyboards only -> None
