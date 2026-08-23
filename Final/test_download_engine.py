"""Tests for download_engine selection + hybrid stream classification."""
from pathlib import Path

import pytest

from jobspec import (
    JobSpec,
    DownloadEngineDecision,
    normalize_download_engine,
    pick_download_engine,
    decide_download_engine,
    classify_stream_kind,
    VALID_DOWNLOAD_ENGINES,
)


def test_normalize_aliases():
    assert normalize_download_engine(None) == "auto"
    assert normalize_download_engine("built-in") == "builtin"
    assert normalize_download_engine("ARIA2C") == "aria2"
    assert normalize_download_engine("N_m3u8DL-RE") == "turbo"
    assert normalize_download_engine("nope") == "auto"
    assert normalize_download_engine("hybrid") == "hybrid"


def test_pick_auto_is_builtin_not_hybrid():
    eng, reason = pick_download_engine("auto", aria2_available=True, turbo_available=True)
    assert eng == "builtin"
    assert "hybrid" not in reason.lower() or "auto" in reason.lower()
    assert eng != "hybrid"


def test_pick_hybrid_opt_in():
    eng, reason = pick_download_engine("hybrid", aria2_available=True, turbo_available=True)
    assert eng == "hybrid"
    assert "hybrid" in reason.lower()


@pytest.mark.parametrize(('kind', 'aria', 'turbo', 'expected', 'executable'), [
    ('multi_dash', '/aria2c', '/turbo', 'builtin', None),
    ('single_dash', '/aria2c', '/turbo', 'builtin', None),
    ('progressive', '/aria2c', '/turbo', 'aria2', '/aria2c'),
    ('fragment_playlist', '/aria2c', '/turbo', 'turbo', '/turbo'),
    ('unknown', '/aria2c', '/turbo', 'builtin', None),
])
def test_hybrid_decision_matrix(kind, aria, turbo, expected, executable):
    decision = decide_download_engine(
        'hybrid', kind, aria_path=aria, turbo_path=turbo)
    assert isinstance(decision, DownloadEngineDecision)
    assert decision.path == expected
    assert decision.stream_kind == kind
    assert decision.executable == executable
    assert decision.reason


def test_hybrid_decision_missing_optional_binaries_falls_back():
    progressive = decide_download_engine('hybrid', 'progressive')
    playlist = decide_download_engine('hybrid', 'fragment_playlist')
    assert progressive.path == 'builtin'
    assert 'not installed' in progressive.reason
    assert playlist.path == 'builtin'
    assert 'not installed' in playlist.reason


def test_hybrid_decision_compatibility_gates_and_pin():
    aria = decide_download_engine(
        'hybrid', 'progressive', aria_path='/aria2c',
        aria2_ok=False, aria2_reason='section clip requires built-in')
    assert aria.path == 'builtin'
    assert 'section clip' in aria.reason

    turbo = decide_download_engine(
        'hybrid', 'fragment_playlist', turbo_path='/turbo',
        turbo_ok=False, turbo_reason='subtitles require built-in')
    assert turbo.path == 'builtin'
    assert 'subtitles' in turbo.reason

    pinned = decide_download_engine(
        'hybrid', 'progressive', aria_path='/aria2c', builtin_pinned=True)
    assert pinned.path == 'builtin'
    assert 'pinned' in pinned.reason


def test_forced_engine_decisions_reject_incompatible_streams():
    aria = decide_download_engine('aria2', 'multi_dash', aria_path='/aria2c')
    turbo = decide_download_engine('turbo', 'progressive', turbo_path='/turbo')
    assert aria.path == 'builtin'
    assert 'incompatible' in aria.reason
    assert turbo.path == 'builtin'
    assert 'incompatible' in turbo.reason


def test_classify_multi_dash():
    assert classify_stream_kind({'requested_formats': [{}, {}]}) == 'multi_dash'
    assert classify_stream_kind({
        'requested_formats': [
            {'vcodec': 'avc1', 'acodec': 'none', 'protocol': 'https', 'url': 'https://googlevideo/v'},
            {'vcodec': 'none', 'acodec': 'mp4a', 'protocol': 'https', 'url': 'https://googlevideo/a'},
        ]
    }) == 'multi_dash'


def test_classify_youtube_single_video_only_https_is_not_progressive():
    """Itag 137-style: protocol https, video only — must NOT go to aria2."""
    kind = classify_stream_kind({
        'requested_formats': [{
            'protocol': 'https',
            'url': 'https://rr1---sn-xxx.googlevideo.com/videoplayback?id=1',
            'vcodec': 'avc1.640028',
            'acodec': 'none',
            'height': 1080,
        }]
    })
    assert kind == 'single_dash'


def test_classify_progressive_av():
    kind = classify_stream_kind({
        'requested_formats': [{
            'protocol': 'https',
            'url': 'https://googlevideo.com/videoplayback',
            'vcodec': 'avc1',
            'acodec': 'mp4a.40.2',
            'height': 360,
        }]
    })
    assert kind == 'progressive'


def test_classify_codecless_http_selection_is_unknown_not_progressive():
    assert classify_stream_kind({
        'protocol': 'https',
        'url': 'https://cdn.example.test/file.bin',
    }) == 'unknown'
    assert classify_stream_kind({
        'requested_formats': [{
            'protocol': 'https',
            'url': 'https://cdn.example.test/file.bin',
        }],
    }) == 'unknown'


def test_classify_fragment_playlist():
    assert classify_stream_kind({
        'requested_formats': [{'protocol': 'm3u8_native', 'url': 'https://x/master.m3u8',
                               'vcodec': 'avc1', 'acodec': 'mp4a'}]
    }) == 'fragment_playlist'
    assert classify_stream_kind({
        'requested_formats': [{'protocol': 'https', 'url': 'https://x/a.mpd',
                               'vcodec': 'avc1', 'acodec': 'none'}]
    }) == 'fragment_playlist'
    assert classify_stream_kind({
        'requested_formats': [{'protocol': 'https', 'fragments': [{'url': 'a'}],
                               'vcodec': 'avc1', 'acodec': 'none'}]
    }) == 'fragment_playlist'


def test_pick_aria2_requires_binary():
    assert pick_download_engine("aria2", aria2_available=False)[0] == "builtin"
    assert pick_download_engine("aria2", aria2_available=True)[0] == "aria2"


def test_pick_turbo_requires_binary():
    assert pick_download_engine("turbo", turbo_available=False)[0] == "builtin"
    assert pick_download_engine("turbo", turbo_available=True)[0] == "turbo"


def test_jobspec_roundtrip_download_engine():
    s = JobSpec.from_dict({"urls": ["https://youtu.be/x"], "download_engine": "turbo"})
    assert s.download_engine == "turbo"
    assert s.to_dict()["download_engine"] == "turbo"
    s2 = JobSpec.from_dict(s.to_dict())
    assert s2.download_engine == "turbo"
    h = JobSpec.from_dict({"urls": ["u"], "download_engine": "hybrid"})
    assert h.download_engine == "hybrid"


def test_jobspec_default_engine_is_auto():
    s = JobSpec.from_dict({"urls": ["u"]})
    assert s.download_engine == "auto"
    assert s.download_engine in VALID_DOWNLOAD_ENGINES


def test_webui_html_has_engine_dropdown():
    from pathlib import Path
    html = (Path(__file__).parent / "webui" / "index.html").read_text(encoding="utf-8")
    js = (Path(__file__).parent / "webui" / "app.js").read_text(encoding="utf-8")
    assert 'id="download_engine"' in html
    assert 'value="auto"' in html
    assert 'value="builtin"' in html
    assert 'value="hybrid"' in html
    assert 'value="aria2"' in html
    assert 'value="turbo"' in html
    assert "download_engine" in js
    # Hybrid must not be the selected default
    assert 'value="auto" selected' in html or "value='auto' selected" in html
    assert 'value="hybrid" selected' not in html


def test_turbo_compatible_gates():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    hls = {"requested_formats": [{
        "protocol": "m3u8_native", "url": "https://x/media.m3u8"}]}
    ok, _ = d._turbo_compatible(None, hls)
    assert ok is True
    ok, why = d._turbo_compatible(None, {"requested_formats": [{}, {}]})
    assert ok is False and "multi-stream" in why
    s = JobSpec.from_dict({"urls": ["u"], "section": "0:00-1:00"})
    ok, why = d._turbo_compatible(s, hls)
    assert ok is False and "section" in why


def test_turbo_rejects_dash_representation_that_is_not_a_selected_manifest_track():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    info = {'requested_formats': [{
        'protocol': 'http_dash_segments',
        'url': 'https://cdn.example.test/video/init.mp4',
        'manifest_url': 'https://cdn.example.test/master.mpd',
        'manifest_stream_number': 7,
        'fragments': [{'url': 'https://cdn.example.test/video/seg-1.m4s'}],
        'vcodec': 'avc1',
        'acodec': 'none',
    }]}
    ok, why = d._turbo_compatible(None, info)
    assert ok is False
    assert 'DASH' in why

    opts = {}
    path, executable = d._hybrid_pick_after_extract(
        info, None, opts, ydl=None,
        policy='hybrid', aria='/aria2c', turbo='/turbo')
    assert (path, executable) == ('builtin', None)


def _turbo_test_ydl():
    class CookieJar:
        def get_cookie_header(self, url):
            assert url == 'https://cdn.example.test/master.m3u8?token=secret'
            return 'SID=secret'

    class FakeYdl:
        params = {
            'http_headers': {'User-Agent': 'test-agent', 'X-Default': 'yes'},
            'proxy': 'http://fallback-proxy',
        }
        cookiejar = CookieJar()

    return FakeYdl()


def _turbo_test_info():
    return {
        'id': 'abc123',
        'title': 'Turbo Clip',
        'url': 'https://cdn.example.test/master.m3u8?token=secret',
        'protocol': 'm3u8_native',
        'http_headers': {'Referer': 'https://example.test/watch'},
    }


def test_turbo_command_forwards_request_context(tmp_path):
    from types import SimpleNamespace
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    d.detector = SimpleNamespace(ffmpeg_path='C:\\tools\\ffmpeg.exe')
    spec = JobSpec(urls=['https://example.test/watch'], proxy='http://job-proxy')

    cmd, stream_url = d._build_turbo_command(
        _turbo_test_info(), 'C:\\tools\\N_m3u8DL-RE.exe',
        _turbo_test_ydl(), spec, tmp_path, tmp_path / 'temp', 'safe-name')

    assert stream_url == _turbo_test_info()['url']
    assert '--no-ansi-color' in cmd
    assert 'User-Agent: test-agent' in cmd
    assert 'X-Default: yes' in cmd
    assert 'Referer: https://example.test/watch' in cmd
    assert 'Cookie: SID=secret' in cmd
    assert cmd[cmd.index('--custom-proxy') + 1] == 'http://job-proxy'
    assert cmd[cmd.index('--ffmpeg-binary-path') + 1] == 'C:\\tools\\ffmpeg.exe'


def test_turbo_execution_reaches_subprocess_and_returns_fresh_output(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import yt_final
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.output_path = tmp_path
    d.detector = SimpleNamespace(ffmpeg_path='ffmpeg')
    captured = {}

    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        save_dir = Path(cmd[cmd.index('--save-dir') + 1])
        save_name = cmd[cmd.index('--save-name') + 1]
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f'{save_name}.mp4').write_bytes(b'fresh-media')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(yt_final.subprocess, 'run', fake_run)
    result = d._download_with_turbo(
        _turbo_test_info(), 'N_m3u8DL-RE', _turbo_test_ydl(),
        spec=JobSpec(urls=['https://example.test/watch']))

    assert result == tmp_path / 'Turbo Clip [abc123].mp4'
    assert captured['cmd'][0] == 'N_m3u8DL-RE'
    assert not list((tmp_path / '.turbo_tmp').glob('*'))


def test_turbo_failure_redacts_secrets_and_cleans_temp(monkeypatch, tmp_path, caplog):
    from types import SimpleNamespace
    import yt_final
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.output_path = tmp_path
    d.detector = SimpleNamespace(ffmpeg_path='ffmpeg')
    partial = tmp_path / 'Turbo Clip [abc123].mp4'

    def fake_run(cmd, **kwargs):
        save_dir = Path(cmd[cmd.index('--save-dir') + 1])
        save_name = cmd[cmd.index('--save-name') + 1]
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f'{save_name}.mp4').write_bytes(b'partial-media')
        return SimpleNamespace(
            returncode=2,
            stdout='',
            stderr='failed https://cdn.example.test/master.m3u8?token=secret Cookie: SID=secret')

    monkeypatch.setattr(yt_final.subprocess, 'run', fake_run)
    result = d._download_with_turbo(
        _turbo_test_info(), 'N_m3u8DL-RE', _turbo_test_ydl(),
        spec=JobSpec(urls=['https://example.test/watch']))

    assert result is None
    assert 'token=secret' not in caplog.text
    assert 'SID=secret' not in caplog.text
    assert not partial.exists()
    assert not list((tmp_path / '.turbo_tmp').glob('*'))


def test_turbo_timeout_redacts_command_and_cleans_temp(monkeypatch, tmp_path, caplog):
    from types import SimpleNamespace
    import subprocess
    import yt_final
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.output_path = tmp_path
    d.detector = SimpleNamespace(ffmpeg_path='ffmpeg')

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])

    monkeypatch.setattr(yt_final.subprocess, 'run', fake_run)
    result = d._download_with_turbo(
        _turbo_test_info(), 'N_m3u8DL-RE', _turbo_test_ydl(),
        spec=JobSpec(urls=['https://example.test/watch']))

    assert result is None
    assert 'token=secret' not in caplog.text
    assert 'SID=secret' not in caplog.text
    assert not list((tmp_path / '.turbo_tmp').glob('*'))


def test_turbo_does_not_return_stale_same_id_output(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import os
    import time
    import yt_final
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.output_path = tmp_path
    d.detector = SimpleNamespace(ffmpeg_path='ffmpeg')
    stale = tmp_path / 'Old Turbo Clip [abc123].mp4'
    stale.write_bytes(b'old-media')
    old_time = time.time() - 60
    os.utime(stale, (old_time, old_time))

    monkeypatch.setattr(
        yt_final.subprocess, 'run',
        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout='', stderr=''))
    result = d._download_with_turbo(
        _turbo_test_info(), 'N_m3u8DL-RE', _turbo_test_ydl(),
        spec=JobSpec(urls=['https://example.test/watch']))

    assert result is None
    assert stale.exists()
    assert not list((tmp_path / '.turbo_tmp').glob('*'))


def test_turbo_interrupt_cleans_temp_and_propagates(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import yt_final
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.output_path = tmp_path
    d.detector = SimpleNamespace(ffmpeg_path='ffmpeg')

    def interrupt(cmd, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(yt_final.subprocess, 'run', interrupt)
    with pytest.raises(KeyboardInterrupt):
        d._download_with_turbo(
            _turbo_test_info(), 'N_m3u8DL-RE', _turbo_test_ydl(),
            spec=JobSpec(urls=['https://example.test/watch']))
    assert not list((tmp_path / '.turbo_tmp').glob('*'))


def test_turbo_cancel_propagates_from_download_with_turbo(monkeypatch, tmp_path):
    """With cancel_check installed, DownloadCancelled from the turbo runner must propagate out of
    _download_with_turbo (never collapse to None / silent fallback)."""
    from types import SimpleNamespace
    import yt_final
    from yt_final import DownloadCancelled
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.output_path = tmp_path
    d.detector = SimpleNamespace(ffmpeg_path='ffmpeg')
    d.cancel_check = lambda: True

    def boom(cmd, timeout_s=3600 * 6, creationflags=0):
        raise DownloadCancelled('YT_FINAL_CANCEL')
    d._run_turbo_cancellable = boom
    with pytest.raises(DownloadCancelled):
        d._download_with_turbo(
            _turbo_test_info(), 'N_m3u8DL-RE', _turbo_test_ydl(),
            spec=JobSpec(urls=['https://example.test/watch']))
    assert not list((tmp_path / '.turbo_tmp').glob('*'))


def test_run_turbo_cancellable_kills_and_raises():
    """Unit-level: _run_turbo_cancellable polls cancel_check and kills the child promptly."""
    import sys as _sys
    import time as _t
    import yt_final
    from yt_final import DownloadCancelled
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.cancel_check = lambda: True
    forever = [_sys.executable, '-c',
               'import time\nwhile True: time.sleep(0.05)']
    t0 = _t.monotonic()
    with pytest.raises(DownloadCancelled):
        d._run_turbo_cancellable(forever, timeout_s=30)
    assert _t.monotonic() - t0 < 5.0


def test_run_turbo_cancellable_kills_the_whole_process_tree(tmp_path):
    """Cancel must kill DESCENDANTS, not only the direct child.

    N_m3u8DL-RE spawns ffmpeg for the merge (we hand it --ffmpeg-binary-path). Killing just the
    parent orphans that muxer: it keeps writing into the job temp dir the caller immediately
    rmtree's — on Windows those unlinks then fail and .turbo_tmp survives with multi-GB partials —
    and it holds the inherited pipes open, pinning the reader threads and their fds.
    """
    import os as _os
    import sys as _sys
    import time as _t

    def _alive(pid):
        """Non-destructive liveness probe. os.kill(pid, 0) is a harmless query on POSIX, but on
        Windows os.kill TerminateProcess'es the target for any non-CTRL signal — so there we must
        query via OpenProcess + GetExitCodeProcess instead (STILL_ACTIVE == 259)."""
        if _os.name != 'nt':
            try:
                _os.kill(pid, 0)
                return True
            except OSError:
                return False
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False                               # can't open -> already gone
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259                   # STILL_ACTIVE (a killed proc returns its code)
        finally:
            k.CloseHandle(h)

    def _reap(pid):                                    # best-effort cleanup so a failure doesn't leak
        if _os.name == 'nt':
            import subprocess
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        else:
            try:
                _os.kill(pid, 9)
            except OSError:
                pass

    import yt_final
    from yt_final import DownloadCancelled
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    marker = tmp_path / 'grandchild.pid'
    child_src = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    d.cancel_check = lambda: marker.exists()          # cancel as soon as the grandchild is up
    with pytest.raises(DownloadCancelled):
        d._run_turbo_cancellable([_sys.executable, '-c', child_src], timeout_s=30)
    gpid = int(marker.read_text().strip())
    for _ in range(60):                                # the tree-kill is async; give it a beat
        if not _alive(gpid):
            return                                     # grandchild gone -> whole tree was killed
        _t.sleep(0.1)
    _reap(gpid)                                        # don't leak it if we're about to fail
    raise AssertionError('grandchild survived the cancel — process tree was not killed')


def test_parallel_same_id_turbo_jobs_use_distinct_temp_roots(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    import threading
    import yt_final
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.output_path = tmp_path
    d.detector = SimpleNamespace(ffmpeg_path='ffmpeg')
    barrier = threading.Barrier(2)
    temp_dirs = []
    lock = threading.Lock()

    def fake_run(cmd, **kwargs):
        temp_dir = cmd[cmd.index('--tmp-dir') + 1]
        with lock:
            temp_dirs.append(temp_dir)
        barrier.wait(timeout=5)
        return SimpleNamespace(returncode=2, stdout='', stderr='expected test failure')

    monkeypatch.setattr(yt_final.time, 'time', lambda: 1234.567)
    monkeypatch.setattr(yt_final.subprocess, 'run', fake_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(
            d._download_with_turbo,
            _turbo_test_info(), 'N_m3u8DL-RE', _turbo_test_ydl(),
            JobSpec(urls=['https://example.test/watch'])) for _ in range(2)]
        assert [f.result() for f in futures] == [None, None]

    assert len(temp_dirs) == 2
    assert len(set(temp_dirs)) == 2
    assert not list((tmp_path / '.turbo_tmp').glob('*'))


def _hybrid_download_harness(monkeypatch, tmp_path, resolved_info, process_callback):
    """Return a minimally initialized real downloader plus fake-YoutubeDL counters."""
    import sys
    import threading
    from types import SimpleNamespace
    import yt_final

    state = SimpleNamespace(
        extractions=0,
        processes=0,
        process_had_aria2=[],
        extracted_urls=[],
        two_extracted=threading.Event(),
        guard=threading.Lock(),
    )

    class FakeYdl:
        def __init__(self, opts):
            self.params = dict(opts)
            self.cookiejar = SimpleNamespace(get_cookie_header=lambda url: None)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            assert download is False
            with state.guard:
                state.extractions += 1
                state.extracted_urls.append(url)
                if state.extractions >= 2:
                    state.two_extracted.set()
            resolved = resolved_info(url, state) if callable(resolved_info) else resolved_info
            return dict(resolved)

        def process_ie_result(self, info, download=True):
            assert download is True
            with state.guard:
                state.processes += 1
                state.process_had_aria2.append('external_downloader' in self.params)
            return process_callback(self, info, state)

        def prepare_filename(self, info):
            return info.get('filepath') or str(tmp_path / 'prepared.mp4')

    monkeypatch.setitem(sys.modules, 'yt_dlp', SimpleNamespace(YoutubeDL=FakeYdl))
    d = yt_final.YouTubeDownloader.__new__(yt_final.YouTubeDownloader)
    d.output_path = tmp_path
    d.detector = SimpleNamespace(ffmpeg_path='ffmpeg')
    d.history = []
    d.last_info = {}
    d._tls = threading.local()
    d._get_cookie_manager = lambda: None
    d._log_quality = lambda info: None
    d._report_speed = lambda *a, **k: None      # now takes an optional transfer_bps arg
    d.get_optimized_ydl_opts = lambda *args, **kwargs: {
        '_engine_policy': 'hybrid',
        '_engine_aria': '/aria2c',
        '_engine_turbo': '/turbo',
        '_engine_builtin_pinned': False,
        'progress_hooks': [],
    }
    return d, state


def _write_builtin_result(tmp_path, ydl, info):
    out = tmp_path / 'builtin-result.mp4'
    out.write_bytes(b'builtin-media')
    result = dict(info)
    result['filepath'] = str(out)
    result['requested_downloads'] = [{'filepath': str(out)}]
    for hook in ydl.params.get('progress_hooks', []):
        hook({'status': 'finished', 'filename': str(out)})
    return result


def test_schemeless_default_https_port_is_normalized_before_extraction(
        monkeypatch, tmp_path):
    video_id = "dQw4w9WgXcQ"
    info = {
        "id": video_id,
        "requested_formats": [
            {
                "vcodec": "avc1",
                "acodec": "none",
                "protocol": "https",
                "url": "https://x/video.mp4",
            },
            {
                "vcodec": "none",
                "acodec": "mp4a",
                "protocol": "https",
                "url": "https://x/audio.m4a",
            },
        ],
    }
    downloader, state = _hybrid_download_harness(
        monkeypatch,
        tmp_path,
        info,
        lambda ydl, resolved, _state: _write_builtin_result(
            tmp_path, ydl, resolved
        ),
    )

    result = downloader.download(
        f"youtube.com:443/watch?v={video_id}",
        spec=JobSpec(
            urls=[f"youtube.com:443/watch?v={video_id}"],
            download_engine="hybrid",
        ),
    )

    assert result == [tmp_path / "builtin-result.mp4"]
    assert state.extracted_urls == [
        f"https://youtube.com/watch?v={video_id}",
    ]


def test_hybrid_full_flow_multi_dash_uses_builtin(monkeypatch, tmp_path):
    info = {'id': 'dash', 'requested_formats': [
        {'vcodec': 'avc1', 'acodec': 'none', 'protocol': 'https', 'url': 'https://x/v'},
        {'vcodec': 'none', 'acodec': 'mp4a', 'protocol': 'https', 'url': 'https://x/a'},
    ]}
    d, state = _hybrid_download_harness(
        monkeypatch, tmp_path, info,
        lambda ydl, resolved, _state: _write_builtin_result(tmp_path, ydl, resolved))

    result = d.download('https://youtu.be/dash', spec=JobSpec(urls=['https://youtu.be/dash'], download_engine='hybrid'))

    assert result == [tmp_path / 'builtin-result.mp4']
    assert state.extractions == 1
    assert state.process_had_aria2 == [False]


def test_hybrid_full_flow_aria2_failure_falls_back_same_extraction(monkeypatch, tmp_path, caplog):
    info = {'id': 'progressive', 'requested_formats': [{
        'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'https',
        'url': 'https://x/video.mp4',
    }]}

    def process(ydl, resolved, state):
        if state.process_had_aria2[-1]:
            raise RuntimeError(
                'aria2 failed https://cdn.example.test/video.mp4?token=secret Cookie: SID=secret')
        return _write_builtin_result(tmp_path, ydl, resolved)

    d, state = _hybrid_download_harness(monkeypatch, tmp_path, info, process)
    result = d.download(
        'https://youtu.be/progressive',
        spec=JobSpec(urls=['https://youtu.be/progressive'], download_engine='hybrid'))

    assert result == [tmp_path / 'builtin-result.mp4']
    assert state.extractions == 1
    assert state.process_had_aria2 == [True, False]
    assert 'token=secret' not in caplog.text
    assert 'SID=secret' not in caplog.text


def test_hybrid_full_flow_turbo_failure_falls_back_same_extraction(monkeypatch, tmp_path):
    info = {'id': 'manifest', 'requested_formats': [{
        'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'm3u8_native',
        'url': 'https://x/master.m3u8',
    }]}
    d, state = _hybrid_download_harness(
        monkeypatch, tmp_path, info,
        lambda ydl, resolved, _state: _write_builtin_result(tmp_path, ydl, resolved))
    d._download_with_turbo = lambda *args, **kwargs: None

    result = d.download(
        'https://youtu.be/manifest',
        spec=JobSpec(urls=['https://youtu.be/manifest'], download_engine='hybrid'))

    assert result == [tmp_path / 'builtin-result.mp4']
    assert state.extractions == 1
    assert state.process_had_aria2 == [False]


def test_hybrid_full_flow_turbo_success_skips_builtin(monkeypatch, tmp_path):
    info = {'id': 'manifest', 'requested_formats': [{
        'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'm3u8_native',
        'url': 'https://x/master.m3u8',
    }]}
    d, state = _hybrid_download_harness(
        monkeypatch, tmp_path, info,
        lambda ydl, resolved, _state: pytest.fail('built-in should not run'))
    turbo_out = tmp_path / 'turbo-result.mp4'
    turbo_out.write_bytes(b'turbo-media')
    d._download_with_turbo = lambda *args, **kwargs: turbo_out

    result = d.download(
        'https://youtu.be/manifest',
        spec=JobSpec(urls=['https://youtu.be/manifest'], download_engine='hybrid'))

    assert result == [turbo_out]
    assert state.extractions == 1
    assert state.processes == 0


def test_unknown_aliases_with_same_extracted_id_serialize_turbo_fallback_through_finalize(
        monkeypatch, tmp_path):
    """The post-extract lock starts before turbo and remains held through fallback + finalization."""
    import threading

    info = {'id': 'dQw4w9WgXcQ', 'requested_formats': [{
        'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'm3u8_native',
        'url': 'https://x/master.m3u8',
    }]}
    second_process = threading.Event()

    def process(ydl, resolved, state):
        if state.processes >= 2:
            second_process.set()
        return _write_builtin_result(tmp_path, ydl, resolved)

    d, state = _hybrid_download_harness(monkeypatch, tmp_path, info, process)
    turbo_calls = []
    d._download_with_turbo = lambda *args, **kwargs: (turbo_calls.append(1) or None)
    first_finalize = threading.Event()
    release_first = threading.Event()
    finalizer_calls = []

    def finalize(paths):
        finalizer_calls.append(list(paths))
        if len(finalizer_calls) == 1:
            first_finalize.set()
            assert release_first.wait(3)
        return paths

    urls = [
        'https://www.youtube.com/redirect?q=first',
        'https://www.youtube.com/attribution_link?u=second',
    ]
    results, errors = {}, {}

    def run_one(url):
        try:
            results[url] = d.download(
                url,
                spec=JobSpec(urls=[url], download_engine='hybrid'),
                _post_download=finalize,
            )
        except BaseException as exc:
            errors[url] = exc

    first = threading.Thread(target=run_one, args=(urls[0],))
    first.start()
    assert first_finalize.wait(3), f'first worker did not reach finalization: {errors}'
    second = threading.Thread(target=run_one, args=(urls[1],))
    second.start()
    assert state.two_extracted.wait(3), f'second alias never completed extraction: {errors}'
    assert not second_process.is_set(), 'same extracted ID entered fallback while first finalized'

    release_first.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert second_process.is_set()
    assert len(turbo_calls) == 2
    assert len(finalizer_calls) == 2
    assert all(value == [tmp_path / 'builtin-result.mp4'] for value in results.values())


def test_retry_fails_closed_if_authoritative_extracted_id_changes(monkeypatch, tmp_path):
    """Never release/acquire a different key mid-retry: fail closed without lock-order risk."""
    first_id = 'dQw4w9WgXcQ'
    second_id = 'aB3dE5gH7_k'

    def resolved(_url, state):
        video_id = first_id if state.extractions == 1 else second_id
        return {
            'id': video_id,
            'requested_formats': [
                {
                    'vcodec': 'avc1',
                    'acodec': 'none',
                    'protocol': 'https',
                    'url': 'https://x/video.mp4',
                },
                {
                    'vcodec': 'none',
                    'acodec': 'mp4a',
                    'protocol': 'https',
                    'url': 'https://x/audio.m4a',
                },
            ],
        }

    def process(ydl, info, state):
        if state.processes == 1:
            raise RuntimeError('Sign in to confirm you are not a bot')
        return _write_builtin_result(tmp_path, ydl, info)

    d, state = _hybrid_download_harness(monkeypatch, tmp_path, resolved, process)

    class RefreshingManager:
        calls = 0

        def get_cookiefile(self):
            self.calls += 1
            return None if self.calls == 1 else str(tmp_path / 'fresh-cookies.txt')

        def notify_auth_failure(self, _url):
            pass

    manager = RefreshingManager()
    d._get_cookie_manager = lambda: manager

    with pytest.raises(RuntimeError, match='identity changed across retry'):
        d.download(
            'https://www.youtube.com/redirect?q=unstable',
            spec=JobSpec(
                urls=['https://www.youtube.com/redirect?q=unstable'],
                download_engine='hybrid',
            ),
        )

    assert state.extractions == 2
    assert state.processes == 1


def test_alias_waits_while_first_download_is_between_outer_retry_attempts(
        monkeypatch, tmp_path):
    """The first authoritative lock remains owned during auth-refresh retry planning."""
    import threading

    info = {
        'id': 'dQw4w9WgXcQ',
        'requested_formats': [
            {
                'vcodec': 'avc1',
                'acodec': 'none',
                'protocol': 'https',
                'url': 'https://x/video.mp4',
            },
            {
                'vcodec': 'none',
                'acodec': 'mp4a',
                'protocol': 'https',
                'url': 'https://x/audio.m4a',
            },
        ],
    }

    def process(ydl, resolved, state):
        if state.processes == 1:
            raise RuntimeError('Sign in to confirm you are not a bot')
        return _write_builtin_result(tmp_path, ydl, resolved)

    d, state = _hybrid_download_harness(monkeypatch, tmp_path, info, process)
    retry_planner_entered = threading.Event()
    allow_retry = threading.Event()

    class PausingManager:
        def notify_auth_failure(self, _url):
            retry_planner_entered.set()
            assert allow_retry.wait(3)

        def get_cookiefile(self):
            return str(tmp_path / 'fresh-cookies.txt')

    d._get_cookie_manager = lambda: PausingManager()
    urls = [
        'https://www.youtube.com/redirect?q=first',
        'https://www.youtube.com/attribution_link?u=second',
    ]
    results, errors = {}, {}

    def run_one(url):
        try:
            results[url] = d.download(
                url,
                cookiefile=str(tmp_path / 'old-cookies.txt'),
                spec=JobSpec(urls=[url], download_engine='hybrid'),
            )
        except BaseException as exc:
            errors[url] = exc

    first = threading.Thread(target=run_one, args=(urls[0],))
    first.start()
    assert retry_planner_entered.wait(3), f'first worker did not reach retry planner: {errors}'
    second = threading.Thread(target=run_one, args=(urls[1],))
    second.start()
    assert state.two_extracted.wait(3), f'second alias did not finish extraction: {errors}'
    with state.guard:
        assert state.processes == 1, 'alias entered processing during the outer retry gap'

    allow_retry.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert state.extractions == 3
    assert state.processes == 3
    assert all(value == [tmp_path / 'builtin-result.mp4'] for value in results.values())


@pytest.mark.parametrize('error_name', ['verification', 'cancel', 'keyboard'])
def test_post_download_exception_preserves_type_skips_retry_and_releases_lock(
        monkeypatch, tmp_path, error_name):
    import yt_final

    info = {
        'id': 'dQw4w9WgXcQ',
        'requested_formats': [
            {
                'vcodec': 'avc1',
                'acodec': 'none',
                'protocol': 'https',
                'url': 'https://x/video.mp4',
            },
            {
                'vcodec': 'none',
                'acodec': 'mp4a',
                'protocol': 'https',
                'url': 'https://x/audio.m4a',
            },
        ],
    }
    d, state = _hybrid_download_harness(
        monkeypatch,
        tmp_path,
        info,
        lambda ydl, resolved, _state: _write_builtin_result(tmp_path, ydl, resolved),
    )
    error = {
        'verification': yt_final.VerificationError('bad media'),
        'cancel': yt_final.DownloadCancelled('YT_FINAL_CANCEL'),
        'keyboard': KeyboardInterrupt('stop'),
    }[error_name]

    def fail_finalize(_paths):
        raise error

    with pytest.raises(type(error)) as caught:
        d.download(
            'https://www.youtube.com/redirect?q=first',
            spec=JobSpec(
                urls=['https://www.youtube.com/redirect?q=first'],
                download_engine='hybrid',
            ),
            _post_download=fail_finalize,
        )

    assert caught.value is error
    assert state.extractions == 1
    assert state.processes == 1
    # Same key can be acquired again immediately: terminal callback errors never strand it.
    assert d.download(
        'https://www.youtube.com/attribution_link?u=second',
        spec=JobSpec(
            urls=['https://www.youtube.com/attribution_link?u=second'],
            download_engine='hybrid',
        ),
    ) == [tmp_path / 'builtin-result.mp4']


def test_hybrid_aria2_pin_survives_auth_refresh_rebuild(monkeypatch, tmp_path):
    info = {'id': 'progressive', 'requested_formats': [{
        'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'https',
        'url': 'https://x/video.mp4',
    }]}

    def process(ydl, resolved, state):
        if state.process_had_aria2[-1]:
            raise RuntimeError('aria2 connection failed')
        if state.processes == 2:
            raise RuntimeError('Sign in to confirm you are not a bot')
        return _write_builtin_result(tmp_path, ydl, resolved)

    d, state = _hybrid_download_harness(monkeypatch, tmp_path, info, process)

    class RefreshingManager:
        calls = 0

        def get_cookiefile(self):
            self.calls += 1
            return None if self.calls == 1 else str(tmp_path / 'fresh-cookies.txt')

        def notify_auth_failure(self, url):
            assert url == 'https://youtu.be/progressive'

    manager = RefreshingManager()
    d._get_cookie_manager = lambda: manager

    result = d.download(
        'https://youtu.be/progressive',
        spec=JobSpec(urls=['https://youtu.be/progressive'], download_engine='hybrid'))

    assert result == [tmp_path / 'builtin-result.mp4']
    assert state.extractions == 2
    assert state.process_had_aria2 == [True, False, False]


def test_hybrid_turbo_failure_pin_survives_auth_refresh_rebuild(monkeypatch, tmp_path):
    info = {'id': 'manifest', 'requested_formats': [{
        'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'm3u8_native',
        'url': 'https://x/master.m3u8',
    }]}

    def process(ydl, resolved, state):
        if state.processes == 1:
            raise RuntimeError('Sign in to confirm you are not a bot')
        return _write_builtin_result(tmp_path, ydl, resolved)

    d, state = _hybrid_download_harness(monkeypatch, tmp_path, info, process)
    turbo_calls = []

    def fail_turbo(*args, **kwargs):
        turbo_calls.append(1)
        return None

    d._download_with_turbo = fail_turbo

    class RefreshingManager:
        calls = 0

        def get_cookiefile(self):
            self.calls += 1
            return None if self.calls == 1 else str(tmp_path / 'fresh-cookies.txt')

        def notify_auth_failure(self, url):
            assert url == 'https://youtu.be/manifest'

    manager = RefreshingManager()
    d._get_cookie_manager = lambda: manager

    result = d.download(
        'https://youtu.be/manifest',
        spec=JobSpec(urls=['https://youtu.be/manifest'], download_engine='hybrid'))

    assert result == [tmp_path / 'builtin-result.mp4']
    assert state.extractions == 2
    assert len(turbo_calls) == 1
    assert state.process_had_aria2 == [False, False]


def test_hybrid_parallel_picks_keep_live_ydl_params_isolated():
    from concurrent.futures import ThreadPoolExecutor
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)

    class FakeYdl:
        def __init__(self):
            self.params = {}

    progressive_ydl = FakeYdl()
    playlist_ydl = FakeYdl()

    def progressive_pick():
        opts = {}
        result = d._hybrid_pick_after_extract(
            {'requested_formats': [{
                'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'https',
                'url': 'https://x/video.mp4',
            }]}, None, opts, ydl=progressive_ydl,
            policy='hybrid', aria='/aria2c', turbo='/turbo')
        return result, opts

    def playlist_pick():
        opts = {}
        result = d._hybrid_pick_after_extract(
            {'requested_formats': [{
                'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'm3u8_native',
                'url': 'https://x/master.m3u8',
            }]}, None, opts, ydl=playlist_ydl,
            policy='hybrid', aria='/aria2c', turbo='/turbo')
        return result, opts

    with ThreadPoolExecutor(max_workers=2) as pool:
        progressive_future = pool.submit(progressive_pick)
        playlist_future = pool.submit(playlist_pick)
        progressive_result = progressive_future.result()
        playlist_result = playlist_future.result()

    assert progressive_result[0] == ('aria2', None)
    assert playlist_result[0] == ('turbo', '/turbo')
    assert 'external_downloader' in progressive_ydl.params
    assert 'external_downloader' not in playlist_ydl.params


def test_hybrid_pick_multi_dash_builtin():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {}
    path, tbin = d._hybrid_pick_after_extract(
        {'requested_formats': [
            {'vcodec': 'avc1', 'acodec': 'none', 'protocol': 'https', 'url': 'https://v'},
            {'vcodec': 'none', 'acodec': 'mp4a', 'protocol': 'https', 'url': 'https://a'},
        ]},
        None, opts, ydl=None,
        policy='hybrid', aria='/aria2c', turbo='/turbo',
    )
    assert path == 'builtin'
    assert tbin is None
    assert 'external_downloader' not in opts


def test_hybrid_pick_progressive_uses_aria2():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {}
    path, tbin = d._hybrid_pick_after_extract(
        {'requested_formats': [{
            'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'https',
            'url': 'https://googlevideo.com/videoplayback',
        }]},
        None, opts, ydl=None,
        policy='hybrid', aria='C:\\aria2c.exe', turbo=None,
    )
    assert path == 'aria2'
    assert tbin is None
    assert opts.get('external_downloader', {}).get('https') == 'C:\\aria2c.exe'


def test_hybrid_pick_single_dash_not_aria2():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {}
    path, tbin = d._hybrid_pick_after_extract(
        {'requested_formats': [{
            'vcodec': 'avc1', 'acodec': 'none', 'protocol': 'https',
            'url': 'https://googlevideo.com/videoplayback',
        }]},
        None, opts, ydl=None,
        policy='hybrid', aria='C:\\aria2c.exe', turbo='/turbo',
    )
    assert path == 'builtin'
    assert tbin is None
    assert 'external_downloader' not in opts


def test_hybrid_pick_playlist_turbo():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {}
    path, tbin = d._hybrid_pick_after_extract(
        {'requested_formats': [{
            'protocol': 'm3u8_native', 'url': 'https://x/master.m3u8',
            'vcodec': 'avc1', 'acodec': 'mp4a',
        }]},
        None, opts, ydl=None,
        policy='hybrid', aria=None, turbo='/N_m3u8DL-RE',
    )
    assert path == 'turbo'
    assert tbin == '/N_m3u8DL-RE'


def test_hybrid_pick_progressive_section_skips_aria2():
    """download_ranges / section must stay on built-in (aria2 ignores ranges)."""
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {'download_ranges': object()}
    path, _ = d._hybrid_pick_after_extract(
        {'requested_formats': [{
            'vcodec': 'avc1', 'acodec': 'mp4a', 'protocol': 'https',
            'url': 'https://googlevideo.com/videoplayback',
        }]},
        None, opts, ydl=None,
        policy='hybrid', aria='/aria2c', turbo=None,
    )
    assert path == 'builtin'
    assert 'external_downloader' not in opts


def test_hybrid_pick_live_skips_turbo():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {}
    path, tbin = d._hybrid_pick_after_extract(
        {
            'is_live': True,
            'requested_formats': [{
                'protocol': 'm3u8_native', 'url': 'https://x/master.m3u8',
                'vcodec': 'avc1', 'acodec': 'mp4a',
            }],
        },
        None, opts, ydl=None,
        policy='hybrid', aria=None, turbo='/N_m3u8DL-RE',
    )
    assert path == 'builtin'
    assert tbin is None


def test_forced_turbo_multi_dash_falls_back():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {}
    path, tbin = d._hybrid_pick_after_extract(
        {'requested_formats': [{}, {}]},
        None, opts, ydl=None,
        policy='turbo', aria=None, turbo='/N_m3u8DL-RE',
    )
    assert path == 'builtin'
    assert tbin is None


@pytest.mark.parametrize('info', [
    {'requested_formats': [{}, {}]},
    {'requested_formats': [{
        'protocol': 'm3u8_native', 'url': 'https://x/master.m3u8',
        'vcodec': 'avc1', 'acodec': 'mp4a',
    }]},
])
def test_forced_aria2_rejects_non_progressive_streams(info):
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {}
    path, executable = d._hybrid_pick_after_extract(
        info, None, opts, ydl=None,
        policy='aria2', aria='/aria2c', turbo='/turbo')
    assert (path, executable) == ('builtin', None)
    assert 'external_downloader' not in opts


def test_forced_turbo_rejects_progressive_stream():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {}
    path, executable = d._hybrid_pick_after_extract(
        {'requested_formats': [{
            'protocol': 'https', 'url': 'https://x/video.mp4',
            'vcodec': 'avc1', 'acodec': 'mp4a',
        }]},
        None, opts, ydl=None,
        policy='turbo', aria='/aria2c', turbo='/turbo')
    assert (path, executable) == ('builtin', None)
    assert 'external_downloader' not in opts


def test_explicit_missing_binaries_do_not_resurrect_stale_instance_values():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    d._aria_bin_for_job = '/stale/aria2c'
    d._turbo_bin_available = '/stale/turbo'

    progressive_opts = {}
    path, executable = d._hybrid_pick_after_extract(
        {'requested_formats': [{
            'protocol': 'https', 'url': 'https://x/video.mp4',
            'vcodec': 'avc1', 'acodec': 'mp4a',
        }]},
        None, progressive_opts, ydl=None,
        policy='hybrid', aria=None, turbo=None)
    assert (path, executable) == ('builtin', None)
    assert 'external_downloader' not in progressive_opts

    playlist_opts = {}
    path, executable = d._hybrid_pick_after_extract(
        {'requested_formats': [{
            'protocol': 'm3u8_native', 'url': 'https://x/master.m3u8',
            'vcodec': 'avc1', 'acodec': 'mp4a',
        }]},
        None, playlist_opts, ydl=None,
        policy='hybrid', aria=None, turbo=None)
    assert (path, executable) == ('builtin', None)


def test_copy_engine_retry_state_keeps_builtin_pin_and_clears_optional_bins():
    from yt_final import YouTubeDownloader
    old = {
        '_engine_policy': 'hybrid',
        '_engine_aria': '/aria2c',
        '_engine_turbo': '/turbo',
        '_engine_builtin_pinned': True,
    }
    new = {'format': 'best'}
    YouTubeDownloader._copy_engine_retry_state(new, old)
    assert new['format'] == 'best'
    assert new['_engine_builtin_pinned'] is True
    assert new['_engine_policy'] == 'builtin'
    assert new['_engine_aria'] is None
    assert new['_engine_turbo'] is None


def test_builtin_retry_pin_overrides_hybrid_progressive_pick():
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {'_engine_builtin_pinned': True}
    path, executable = d._hybrid_pick_after_extract(
        {'requested_formats': [{
            'protocol': 'https', 'url': 'https://x/video.mp4',
            'vcodec': 'avc1', 'acodec': 'mp4a',
        }]},
        None, opts, ydl=None,
        policy='hybrid', aria='/aria2c', turbo='/turbo')
    assert (path, executable) == ('builtin', None)
    assert 'external_downloader' not in opts


@pytest.mark.parametrize('message', [
    'Sign in to confirm you are not a bot',
    'Requested format is not available',
])
def test_retry_planner_preserves_private_engine_state(message):
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)
    opts = {
        '_engine_policy': 'builtin',
        '_engine_aria': None,
        '_engine_turbo': None,
        '_engine_builtin_pinned': True,
    }
    new = d._next_opts_on_error(message, 0, opts)
    assert new is not None
    assert new['_engine_builtin_pinned'] is True
    assert new['_engine_policy'] == 'builtin'


def test_apply_aria2_mutates_ydl_params():
    from yt_final import YouTubeDownloader
    opts = {}
    class FakeYdl:
        params = {}
    y = FakeYdl()
    YouTubeDownloader._apply_aria2_opts(opts, '/aria2c', ydl=y)
    assert y.params['external_downloader']['https'] == '/aria2c'
    YouTubeDownloader._clear_aria2_opts(opts, ydl=y)
    assert 'external_downloader' not in opts
    assert 'external_downloader' not in y.params


def test_concurrent_fragments_clamped_to_positive():
    """A non-positive --concurrent-fragments (reachable via CLI and the JobSpec/web API) must floor
    to 1, not flow straight into yt-dlp's ThreadPoolExecutor(math.ceil(max_workers/max_progress)) and
    crash any multi-stream DASH job with 'max_workers must be greater than 0'."""
    from yt_final import YouTubeDownloader
    d = YouTubeDownloader.__new__(YouTubeDownloader)  # bypass __init__; branch under test uses no self
    for bad in (-2, -1):
        opts = {}
        d._apply_spec_download_opts(opts, [], JobSpec(urls=['https://youtu.be/x'],
                                                      concurrent_fragments=bad))
        assert opts['concurrent_fragment_downloads'] == 1, f"{bad} was not clamped to 1"
    # a positive value passes through unchanged
    opts = {}
    d._apply_spec_download_opts(opts, [], JobSpec(urls=['https://youtu.be/x'],
                                                  concurrent_fragments=8))
    assert opts['concurrent_fragment_downloads'] == 8


def test_classify_empty_info_unknown():
    assert classify_stream_kind({}) == 'unknown'
    assert classify_stream_kind(None) == 'unknown'


def test_kill_process_tree_falls_back_when_taskkill_fails(
        monkeypatch, tmp_path):
    """CONFIRMED: the win32 branch ran taskkill then `return`ed unconditionally, so a FAILING taskkill
    (nonzero rc — e.g. access denied on an elevated child) never reached the documented last-resort
    process.kill(). _terminate_bgutil_child already checks the rc; this must match it."""
    import yt_final
    monkeypatch.setattr(yt_final._sys, 'platform', 'win32')
    taskkill = tmp_path / 'Windows' / 'System32' / 'taskkill.exe'
    taskkill.parent.mkdir(parents=True)
    taskkill.write_bytes(b'system')
    monkeypatch.setattr(
        yt_final,
        '_windows_system_executable',
        lambda name: taskkill if name == 'taskkill.exe' else None,
    )
    launched = []
    monkeypatch.setattr(
        yt_final.subprocess,
        'run',
        lambda command, **_kwargs: (
            launched.append(list(command))
            or type('R', (), {
                'returncode': 1,
                'stdout': b'',
                'stderr': b'',
            })()
        ),
    )
    killed = {'n': 0}

    class _P:
        pid = 4242
        def poll(self): return None          # still alive
        def kill(self): killed['n'] += 1

    yt_final._kill_process_tree(_P())
    assert launched and Path(launched[0][0]).is_absolute()
    assert killed['n'] == 1, 'a failing taskkill must fall through to process.kill()'


def test_kill_process_tree_does_not_double_kill_on_taskkill_success(
        monkeypatch, tmp_path):
    """A SUCCESSFUL taskkill must still short-circuit — no redundant kill() on the tree we just reaped."""
    import yt_final
    monkeypatch.setattr(yt_final._sys, 'platform', 'win32')
    taskkill = tmp_path / 'Windows' / 'System32' / 'taskkill.exe'
    taskkill.parent.mkdir(parents=True)
    taskkill.write_bytes(b'system')
    monkeypatch.setattr(
        yt_final,
        '_windows_system_executable',
        lambda name: taskkill if name == 'taskkill.exe' else None,
    )
    launched = []
    monkeypatch.setattr(
        yt_final.subprocess,
        'run',
        lambda command, **_kwargs: (
            launched.append(list(command))
            or type('R', (), {
                'returncode': 0,
                'stdout': b'',
                'stderr': b'',
            })()
        ),
    )
    killed = {'n': 0}

    class _P:
        pid = 4242
        def poll(self): return None
        def kill(self): killed['n'] += 1

    yt_final._kill_process_tree(_P())
    assert launched and Path(launched[0][0]).is_absolute()
    assert killed['n'] == 0, 'successful taskkill should not be followed by process.kill()'


def test_run_ffmpeg_cancel_kills_the_whole_tree_not_just_the_shim(tmp_path):
    r"""A cancelled ENCODE must reap ffmpeg's whole process tree, not only the direct child.

    Found live on Windows: the app resolves ffmpeg to a WRAPPER on common installs — the chocolatey
    shim C:\ProgramData\chocolatey\bin\ffmpeg.exe launches the real encoder at
    chocolatey\lib\ffmpeg\tools\...\ffmpeg.exe as a CHILD (scoop/winget shims behave the same).
    _run_ffmpeg used a bare process.kill(), which terminates only the shim and leaves the real ffmpeg
    running: two orphaned encoders were observed via Win32_Process after a cancel. The turbo path
    already solved this with _kill_process_tree (see its comment about orphaning the muxer); the
    encode path must do the same.
    """
    import os as _os
    import subprocess
    import sys as _sys
    import time as _t
    import yt_final
    from yt_final import DownloadCancelled

    def _alive(pid):
        if _os.name != 'nt':
            try:
                _os.kill(pid, 0)
                return True
            except OSError:
                return False
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259                   # STILL_ACTIVE
        finally:
            k.CloseHandle(h)

    proc = yt_final.FFmpegProcessor.__new__(yt_final.FFmpegProcessor)
    marker = tmp_path / 'grandchild.pid'
    # stand-in for a shim: spawns the "real encoder" then lingers
    shim_src = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    proc.cancel_check = lambda: marker.exists()        # cancel as soon as the grandchild exists
    with pytest.raises(DownloadCancelled):
        proc._run_ffmpeg([_sys.executable, '-c', shim_src])
    gpid = int(marker.read_text().strip())
    for _ in range(60):                                 # tree-kill is async; give it a beat
        if not _alive(gpid):
            return                                      # grandchild gone -> whole tree reaped
        _t.sleep(0.1)
    # don't leak the grandchild if we're about to fail
    if _os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/PID', str(gpid)], capture_output=True)
    else:
        try:
            _os.kill(gpid, 9)
        except OSError:
            pass
    raise AssertionError('the real encoder survived the cancel — only the shim was killed')


def test_ffmpeg_child_group_kw_makes_the_posix_child_a_group_leader(monkeypatch):
    """CONFIRMED: _kill_process_tree's POSIX branch is killpg(getpgid(child), SIGKILL) and its
    docstring REQUIRES the child to lead its own group/session — but _run_ffmpeg's Popen never set
    start_new_session (only the turbo path did), so the ffmpeg child shared the APP's process group
    and every cancel/timeout kill SIGKILLed the whole app (web server included) on macOS/Linux.
    Windows stays {}: taskkill /T walks the tree by pid, and a new console process group would
    change Ctrl+C delivery under "Start yt_final.bat"."""
    import yt_final
    monkeypatch.setattr(yt_final._sys, 'platform', 'linux')
    assert yt_final._ffmpeg_child_group_kw() == {'start_new_session': True}
    monkeypatch.setattr(yt_final._sys, 'platform', 'darwin')
    assert yt_final._ffmpeg_child_group_kw() == {'start_new_session': True}
    monkeypatch.setattr(yt_final._sys, 'platform', 'win32')
    assert yt_final._ffmpeg_child_group_kw() == {}


def test_run_ffmpeg_passes_child_group_kwargs_to_popen(tmp_path, monkeypatch):
    """_run_ffmpeg must actually FEED _ffmpeg_child_group_kw() into its Popen — prove the plumbing
    with a harmless observable kwarg (cwd) instead of the POSIX-only start_new_session."""
    import sys as _sys2
    import yt_final
    monkeypatch.setattr(yt_final, '_ffmpeg_child_group_kw', lambda: {'cwd': str(tmp_path)})
    out = tmp_path / 'child_cwd.txt'
    proc = yt_final.FFmpegProcessor.__new__(yt_final.FFmpegProcessor)
    assert proc._run_ffmpeg([
        _sys2.executable, '-c',
        f"import os, pathlib; pathlib.Path({str(out)!r}).write_text(os.getcwd())",
    ]) is True
    assert out.read_text().strip().rstrip('\\/') == str(tmp_path).rstrip('\\/'), \
        '_run_ffmpeg did not pass _ffmpeg_child_group_kw() kwargs to Popen'
