"""Watch duration probes must not inherit current-directory executable search."""
from __future__ import annotations

import http.server
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest


try:
    import watch
    import webserver
except ImportError:  # pragma: no cover - repository-root pytest invocation
    from Final import watch, webserver


def _poisoned_working_directory(monkeypatch, tmp_path: Path) -> Path:
    poisoned = tmp_path / "poisoned-cwd"
    poisoned.mkdir()
    executable = poisoned / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    executable.write_bytes(b"not a trusted executable")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.chdir(poisoned)
    return executable


def _clear_duration_cache():
    with watch._DURATION_CACHE_LOCK:
        watch._DURATION_CACHE.clear()


def test_probe_duration_enforces_hard_total_deadline(monkeypatch, tmp_path):
    """A slow extractor lifecycle must be killed, not merely given socket timeouts."""
    # probe_duration spawns a real `python -I -m yt_dlp` child, so the time that child needs
    # merely to REACH the endpoint (interpreter start plus the very large yt_dlp import) is a
    # property of the host, not of the code under test. A fixed budget sized for a dev laptop
    # turns into "the child never got going" on a slow, contended CI runner. So measure the
    # cold-start cost once against an endpoint that answers immediately, derive the probe
    # budget from it, and skip when the sandbox cannot run an isolated yt-dlp at all. The
    # assertion the test exists to make -- the probe is killed at its hard total deadline
    # instead of waiting out the endpoint -- is unchanged.
    request_started = threading.Event()
    warmup_reached = threading.Event()
    release_slow = threading.Event()
    slow_hold = [60.0]

    class SlowHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/warmup"):
                warmup_reached.set()
            else:
                request_started.set()
                # Interruptible stand-in for a hung extractor: the probe must kill its child
                # long before this returns, and teardown can release it immediately.
                release_slow.wait(slow_hold[0])
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"late")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    class SlowServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False

    server = SlowServer(("127.0.0.1", 0), SlowHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    class SlowYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, *, download):
            release_slow.wait(slow_hold[0])
            return None

    monkeypatch.setitem(
        sys.modules,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=SlowYoutubeDL),
    )
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    base = f"http://127.0.0.1:{server.server_port}"
    _clear_duration_cache()
    try:
        # Skip on the POSITIVE capability, not on "the request never arrived": a host without an
        # importable isolated yt_dlp genuinely cannot observe this deadline, but a host that HAS
        # one and still never reaches loopback is a real defect that must stay red.
        try:
            isolated_import = subprocess.run(
                [sys.executable, "-I", "-c", "import yt_dlp"],
                capture_output=True, timeout=120,
            ).returncode
        except (OSError, subprocess.SubprocessError):
            isolated_import = 1
        if isolated_import != 0:
            pytest.skip(
                "this host cannot run an isolated `python -I -m yt_dlp` child, so the probe's "
                "hard deadline cannot be observed here"
            )
        warm_started = time.monotonic()
        watch.probe_duration(f"{base}/warmup", timeout=60)
        startup_cost = time.monotonic() - warm_started
        assert warmup_reached.is_set(), (
            "isolated yt-dlp is importable but never reached the warmup endpoint"
        )
        if startup_cost > 15:
            pytest.skip(
                f"isolated yt-dlp needs {startup_cost:.1f}s just to start on this host, which "
                "is no longer distinguishable from the deadline being measured"
            )
        # Scale the budget WITH the measured cold start rather than adding a fixed second, so the
        # slack still covers run-to-run jitter on a host where the start itself is slow. Hold the
        # slow endpoint far past that budget so only a kill can end the probe.
        probe_timeout = max(1.5, startup_cost * 1.5 + 1.0)
        slow_hold[0] = probe_timeout * 4 + 5
        _clear_duration_cache()
        started = time.monotonic()
        result = watch.probe_duration(f"{base}/hang", timeout=probe_timeout)
        elapsed = time.monotonic() - started
    finally:
        release_slow.set()
        _clear_duration_cache()
        server.shutdown()
        server.server_close()

    assert result is None
    assert request_started.is_set(), "isolated yt-dlp never reached the slow endpoint"
    assert elapsed < 2 * probe_timeout, (
        f"probe exceeded its hard total deadline: {elapsed:.3f}s > {2 * probe_timeout:.3f}s"
    )


def test_probe_duration_does_not_launch_cwd_tool_when_isolated_module_fails(
        monkeypatch, tmp_path):
    """An unavailable trusted module must fail closed, not execute cwd/yt-dlp."""
    poisoned = _poisoned_working_directory(monkeypatch, tmp_path)
    monkeypatch.setenv("PATH", os.pathsep)
    launched = []

    def poisoned_run(command, **kwargs):
        launched.append(command)
        if command[1:4] == ["-I", "-m", "yt_dlp"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
        return subprocess.CompletedProcess(command, 0, stdout="666\n", stderr="")

    monkeypatch.setattr(watch.subprocess, "run", poisoned_run)
    _clear_duration_cache()
    try:
        result = watch.probe_duration(
            "https://www.youtube.com/watch?v=poisoned001"
        )
    finally:
        _clear_duration_cache()

    assert result is None
    assert len(launched) == 1
    assert Path(launched[0][0]).is_absolute()
    assert launched[0][1:4] == ["-I", "-m", "yt_dlp"]
    assert Path(launched[0][0]) != poisoned


def test_probe_duration_uses_only_absolute_explicit_path_fallback(
        monkeypatch, tmp_path):
    """A user-authorized PATH tool remains supported, but is launched absolutely."""
    tools = tmp_path / "manual-tools"
    tools.mkdir()
    executable = tools / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    executable.write_bytes(b"explicitly authorized")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tools))
    launched = []

    def authorized_run(command, **kwargs):
        launched.append(command)
        if command[1:4] == ["-I", "-m", "yt_dlp"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing")
        return subprocess.CompletedProcess(command, 0, stdout="234.0\n", stderr="")

    monkeypatch.setattr(watch.subprocess, "run", authorized_run)
    _clear_duration_cache()
    try:
        result = watch.probe_duration(
            "https://www.youtube.com/watch?v=authorized1"
        )
    finally:
        _clear_duration_cache()

    assert result == 234
    assert len(launched) == 2
    assert Path(launched[0][0]).is_absolute()
    assert launched[0][1:4] == ["-I", "-m", "yt_dlp"]
    assert Path(launched[1][0]) == executable.resolve()


def test_webserver_watch_duration_prefers_isolated_python_module_over_cwd_tool(
        monkeypatch, tmp_path):
    """The webserver's duration-filter path must use the same trusted probe."""
    poisoned = _poisoned_working_directory(monkeypatch, tmp_path)
    monkeypatch.setenv("PATH", os.pathsep)
    launched = []

    def poisoned_run(command, **kwargs):
        launched.append(command)
        if command[1:4] == ["-I", "-m", "yt_dlp"]:
            return subprocess.CompletedProcess(command, 0, stdout="123.9\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="666\n", stderr="")

    monkeypatch.setattr(watch.subprocess, "run", poisoned_run)
    video = {
        "id": "safevideo01",
        "title": "Safe duration",
        "published": "2026-07-28T00:00:00+00:00",
        "url": "https://www.youtube.com/watch?v=safevideo01",
    }
    monkeypatch.setattr(watch, "fetch_feed", lambda channel_id: ("Channel", [video]))
    watched = {
        "channel_id": "UC0000000000000000000000",
        "filters": {"min_duration": 100, "max_duration": 200},
    }
    _clear_duration_cache()
    try:
        result = webserver.ServerState._fetch_one(None, watched)
    finally:
        _clear_duration_cache()

    assert result == [video]
    assert len(launched) == 1
    assert Path(launched[0][0]).is_absolute()
    assert launched[0][1:4] == ["-I", "-m", "yt_dlp"]
    assert Path(launched[0][0]) != poisoned
