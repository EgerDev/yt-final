"""Pure unit tests for max-resolution probe summary (no network)."""
from yt_final import YouTubeDownloader


def test_probe_resolution_summary_max_height():
    info = {
        'title': 'Demo',
        'id': 'abc',
        'duration': 120,
        'uploader': 'Channel',
        'formats': [
            {'height': 720, 'width': 1280, 'vcodec': 'avc1.64001f', 'acodec': 'none', 'fps': 30},
            {'height': 1080, 'width': 1920, 'vcodec': 'avc1.640028', 'acodec': 'none', 'fps': 60},
            {'height': 2160, 'width': 3840, 'vcodec': 'vp09.00.50.08', 'acodec': 'none', 'fps': 60},
            {'height': None, 'vcodec': 'none', 'acodec': 'mp4a.40.2'},
        ],
    }
    s = YouTubeDownloader._probe_resolution_summary(info)
    assert s['max_height'] == 2160
    assert s['max_width'] == 3840
    assert 2160 in s['available_heights']
    # Suggested = edit + speed (not max archive): H.264 capped at 1080p
    assert s['suggested_quality'] == '1080p'
    assert s['suggested_codec'] == 'h264'
    assert s['quality_max'] == '4k'
    assert s['codec_max'] in ('av1', 'vp9', 'any')
    assert 'h264' in s['codecs'] or 'vp9' in s['codecs']
    assert s['resolution_label'] == '2160p'
    assert s['uploader'] == 'Channel'
    assert s['h264_max_height'] == 1080
    assert 60 in s['fps_values']
    assert s.get('stream_tip')


def test_probe_resolution_summary_1080():
    info = {
        'formats': [
            {'height': 480, 'vcodec': 'avc1', 'acodec': 'none'},
            {'height': 1080, 'vcodec': 'avc1', 'acodec': 'none'},
        ],
    }
    s = YouTubeDownloader._probe_resolution_summary(info)
    assert s['max_height'] == 1080
    assert s['suggested_quality'] == '1080p'


def test_probe_ui_hook_present():
    from pathlib import Path
    base = Path(__file__).parent / 'webui'
    html = (base / 'index.html').read_text(encoding='utf-8')
    js = (base / 'app.js').read_text(encoding='utf-8')
    css = (base / 'style.css').read_text(encoding='utf-8')
    assert 'id="probe_res"' in html
    assert 'id="probe_pop"' in html
    assert 'id="probe_pop_heights"' in html
    assert 'id="probe_apply_suggested"' in html
    assert 'Apply edit + speed' in html
    assert 'Use tallest stream' in html
    assert '/api/probe' in js
    assert 'openProbePop' in js
    assert 'applyProbeSuggested' in js
    assert 'applyProbeMax' in js
    assert 'normalizeQualityOption' in js
    assert 'heightToQualityOption' in js
    assert '.probe-pop' in css
    assert '.probe-chip' in css


def test_height_to_quality_option():
    H = YouTubeDownloader._height_to_quality_option
    assert H(2160) == '4k'
    assert H(1440) == '1440p'
    assert H(1080) == '1080p'
    assert H(720) == '720p'


def test_format_fps_and_height_helpers():
    assert YouTubeDownloader._format_fps({'fps': 29.97}) == 30
    assert YouTubeDownloader._format_fps({'average_fps': 60}) == 60
    assert YouTubeDownloader._format_height({'height': 1080}) == 1080
    assert YouTubeDownloader._format_height({'resolution': '1920x1080'}) == 1080
    assert YouTubeDownloader._format_height({'format_note': '1080p'}) == 1080
