import pytest
import sys
from unittest.mock import MagicMock
from yt_final import YouTubeDownloader, get_detector

def test_windows_max_path_staging(monkeypatch):
    """Test that on Windows, yt_final.py uses a short staging name instead of the full save_name."""
    det = get_detector()
    dl = YouTubeDownloader(det, out_dir="test_out")

    import yt_dlp
    monkeypatch.setattr(sys, "platform", "win32")

    info = {"id": "abcdefgh", "title": "A Very Long Title That Could Exceed Max Path When Combined"}
    spec = MagicMock()

    dl._turbo_compatible = MagicMock(return_value=(True, ""))
    dl._build_turbo_command = MagicMock(side_effect=Exception("Stop execution"))
    dl._hybrid_pick_after_extract = MagicMock(return_value=("turbo", "mock_bin"))
    dl._log_quality = MagicMock()
    dl._network_hint_from_info = MagicMock()
    import shutil
    monkeypatch.setattr(shutil, "which", lambda *a, **k: "mock_path")

    class MockYDL:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, *args, **kwargs):
            return {
                "extractor_key": "Youtube",
                "requested_formats": [{"url": "http://mock.m3u8", "protocol": "m3u8_native"}],
                "title": info["title"],
                "id": info["id"]
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", lambda opts: MockYDL())

    mock_popen = MagicMock()
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    try:
        dl.download("https://www.youtube.com/watch?v=aqz-KE-bpKQ", spec)
    except Exception:
        pass

    dl._build_turbo_command.assert_called_once()
    args = dl._build_turbo_command.call_args[0]
    stage_name = args[-1]

    assert stage_name == f"stg_{info['id']}"
