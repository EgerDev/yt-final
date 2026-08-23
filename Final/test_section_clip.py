"""Regression: clip section parse + download_ranges + web jobspec reject invalid clips."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_here = Path(__file__).resolve()
_root = _here.parents[1] if _here.parent.name == 'Final' else _here.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from Final.yt_final import YouTubeDownloader
from Final.jobspec import JobSpec
from Final.webserver import _web_jobspec_from_payload


def _dl():
    det = MagicMock()
    det.ffmpeg_path = 'ffmpeg'
    det.gpu_info = {
        'working_encoders': [],
        'available_encoders': set(),
        'cuda_available': False,
    }
    det.encoder_usable = lambda e: True
    return YouTubeDownloader(det, out_dir='.')


@pytest.mark.parametrize(
    'text,ok',
    [
        ('1:30-3:00', True),
        (' 1:30 - 3:00 ', True),
        ('1:30–3:00', True),
        ('1:30—3:00', True),
        ('1:30 to 3:00', True),
        ('90-180', True),
        ('-2:00', True),
        ('3:00-1:00', False),
        ('bad', False),
        ('1:99-2:00', False),
    ],
)
def test_parse_section_forms(text, ok):
    rng = YouTubeDownloader._parse_section(text)
    assert (rng is not None and rng[0] < rng[1]) is ok


def test_download_opts_sets_ranges_and_precise_flag():
    dl = _dl()
    opts = dl.get_optimized_ydl_opts(section='1:30-3:00')
    assert 'download_ranges' in opts
    assert not opts.get('force_keyframes_at_cuts')

    spec = JobSpec(urls=['https://youtu.be/x'], section='1:30-3:00', section_precise=True)
    opts2 = dl.get_optimized_ydl_opts(section=spec.section, spec=spec)
    assert opts2.get('force_keyframes_at_cuts') is True
    assert 'download_ranges' in opts2


def test_invalid_section_raises_on_opts():
    dl = _dl()
    with pytest.raises(ValueError, match='Invalid clip range'):
        dl.get_optimized_ydl_opts(section='not-a-range')


def test_whitespace_section_is_not_an_error():
    dl = _dl()
    opts = dl.get_optimized_ydl_opts(section='   ')
    assert 'download_ranges' not in opts


def test_web_jobspec_rejects_invalid_clip():
    with pytest.raises(ValueError, match='[Cc]lip range|Invalid'):
        _web_jobspec_from_payload({
            'urls': ['https://youtu.be/x'],
            'section': '5:00-1:00',
        })


def test_web_jobspec_normalizes_en_dash():
    spec = _web_jobspec_from_payload({
        'urls': ['https://youtu.be/x'],
        'section': '1:30–3:00',
        'section_precise': True,
    })
    assert spec.section == '1:30-3:00'
    assert spec.section_precise is True
    assert YouTubeDownloader._parse_section(spec.section) == (90.0, 180.0)


def test_use_auth_jobspec_roundtrip():
    on = _web_jobspec_from_payload({'urls': ['https://youtu.be/x'], 'use_auth': True})
    assert on.use_auth is True
    off = _web_jobspec_from_payload({'urls': ['https://youtu.be/x'], 'use_auth': False})
    assert off.use_auth is False
    d = off.to_dict()
    assert d.get('use_auth') is False
    again = JobSpec.from_dict(d)
    assert again.use_auth is False
    # omitted → default True
    default = JobSpec.from_dict({'urls': ['https://youtu.be/x']})
    assert default.use_auth is True
