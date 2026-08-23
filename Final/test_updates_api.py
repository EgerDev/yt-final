"""Updates tab backend: report shape, scan cache, and managed FFmpeg API."""
import http.client
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import installer_lock
import webserver
from installer_lock import (
    INSTALLER_BUSY_CODE,
    INSTALLER_BUSY_MESSAGE,
    InstallerBusyError,
)


def _node_json(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the Updates UI")
    result = subprocess.run(
        [node, "-e", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, f"node failed: {result.stderr[-1000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _js_function_source(js, name):
    marker = f"function {name}("
    assert marker in js, f"missing required frontend function {name}()"
    body = js.split(marker, 1)[1].split("\n}\n", 1)[0]
    return marker + body + "\n}"


@pytest.fixture
def live_server(monkeypatch, tmp_path):
    """Run the real handler on localhost so auth and status codes are covered."""
    import http.server

    monkeypatch.setenv('YT_FINAL_HOME', str(tmp_path))
    monkeypatch.setenv('YT_FINAL_WEBUI_PASSWORD', 'updates-api-test-password')
    state = webserver.ServerState(str(tmp_path / 'dl'), queue_file=str(tmp_path / 'queue.json'))
    httpd = None
    thread = None
    thread_started = False
    try:
        httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), webserver.Handler
        )
        httpd.state = state
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        thread_started = True
        host, port = httpd.server_address

        login_connection = http.client.HTTPConnection(host, port, timeout=10)
        try:
            login_connection.request(
                'POST',
                '/api/login',
                body=json.dumps({'password': 'updates-api-test-password'}),
                headers={
                    'Content-Type': 'application/json',
                    'Origin': f'http://{host}:{port}',
                },
            )
            login_response = login_connection.getresponse()
            login_body = json.loads(login_response.read())
        finally:
            login_connection.close()
        assert login_response.status == 200
        session_token = login_body['token']

        def post(
                payload=None, *, token=session_token, origin=None,
                path='/api/updates/ffmpeg'):
            connection = http.client.HTTPConnection(host, port, timeout=10)
            headers = {'Content-Type': 'application/json'}
            if token is not None:
                headers['X-Token'] = token
            if origin is not None:
                headers['Origin'] = origin
            try:
                connection.request(
                    'POST', path, body=json.dumps(payload or {}), headers=headers)
                response = connection.getresponse()
                body = json.loads(response.read())
                return response.status, body
            finally:
                connection.close()

        yield post, state
    finally:
        try:
            if httpd is not None and thread_started:
                httpd.shutdown()
        finally:
            try:
                if thread is not None and thread_started:
                    thread.join(timeout=5)
                    if thread.is_alive():
                        raise AssertionError(
                            "live server thread did not stop within 5 seconds"
                        )
            finally:
                try:
                    if httpd is not None:
                        httpd.server_close()
                finally:
                    try:
                        state.stop.set()
                    finally:
                        try:
                            state.journal.release_run_lock()
                        except Exception:
                            pass


def test_live_server_fixture_waits_for_serve_thread_exit(monkeypatch, tmp_path):
    """Fixture teardown must not return while its exact server thread is alive."""
    real_thread = threading.Thread
    target_returned = threading.Event()
    allow_thread_exit = threading.Event()
    serve_threads = []

    class DelayedServeThread(real_thread):
        def run(self):
            try:
                super().run()
            finally:
                target_returned.set()
                allow_thread_exit.wait(5)

    def thread_factory(*args, **kwargs):
        target = kwargs.get("target")
        if target is None and len(args) > 1:
            target = args[1]
        if getattr(target, "__name__", "") == "serve_forever":
            thread = DelayedServeThread(*args, **kwargs)
            serve_threads.append(thread)
            return thread
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", thread_factory)
    fixture = live_server.__wrapped__(monkeypatch, tmp_path)
    releaser = None
    try:
        next(fixture)
        assert len(serve_threads) == 1

        def release_after_target_returns():
            if target_returned.wait(5):
                time.sleep(0.2)
            allow_thread_exit.set()

        releaser = real_thread(target=release_after_target_returns, daemon=True)
        releaser.start()
        fixture.close()
        assert not serve_threads[0].is_alive()
    finally:
        allow_thread_exit.set()
        fixture.close()
        if serve_threads:
            serve_threads[0].join(5)
        if releaser is not None:
            releaser.join(5)


def test_live_server_fixture_cleans_up_after_login_failure(monkeypatch, tmp_path):
    """A setup exception must still release every resource the fixture owns."""
    real_thread = threading.Thread
    serve_threads = []
    servers = []
    connections = []

    def thread_factory(*args, **kwargs):
        target = kwargs.get("target")
        if target is None and len(args) > 1:
            target = args[1]
        if getattr(target, "__name__", "") == "serve_forever":
            thread = real_thread(*args, **kwargs)
            serve_threads.append(thread)
            servers.append(target.__self__)
            return thread
        return real_thread(*args, **kwargs)

    class FailingLoginConnection:
        def __init__(self, *_args, **_kwargs):
            self.closed = False
            connections.append(self)

        def request(self, *_args, **_kwargs):
            raise RuntimeError("forced login failure")

        def close(self):
            self.closed = True

    monkeypatch.setattr(threading, "Thread", thread_factory)
    monkeypatch.setattr(http.client, "HTTPConnection", FailingLoginConnection)
    fixture = live_server.__wrapped__(monkeypatch, tmp_path)
    try:
        with pytest.raises(RuntimeError, match="forced login failure"):
            next(fixture)
        assert len(serve_threads) == len(servers) == len(connections) == 1
        state = servers[0].state
        assert not serve_threads[0].is_alive()
        assert servers[0].fileno() == -1
        assert state.stop.is_set()
        assert not state.journal._hold_lock
        assert not state.journal.lock_path.exists()
        assert connections[0].closed
    finally:
        fixture.close()
        if servers:
            state = servers[0].state
            if serve_threads and serve_threads[0].is_alive():
                servers[0].shutdown()
                serve_threads[0].join(5)
            servers[0].server_close()
            state.stop.set()
            state.journal.release_run_lock()


@pytest.mark.parametrize(
    ("failure_stage", "expected_error"),
    [
        ("request", RuntimeError),
        ("getresponse", RuntimeError),
        ("read", RuntimeError),
        ("json", json.JSONDecodeError),
    ],
)
def test_post_closes_connection_when_response_processing_fails(
        live_server, monkeypatch, failure_stage, expected_error):
    """The yielded request helper owns its connection on every error path."""
    connections = []

    class FailingPostResponse:
        status = 200

        def read(self):
            if failure_stage == "read":
                raise RuntimeError("forced read failure")
            if failure_stage == "json":
                return b"{"
            return b'{"ok": true}'

    class FailingPostConnection:
        def __init__(self, host, port=None, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.closed = False
            connections.append(self)

        def request(self, *_args, **_kwargs):
            if failure_stage == "request":
                raise RuntimeError("forced request failure")

        def getresponse(self):
            if failure_stage == "getresponse":
                raise RuntimeError("forced getresponse failure")
            return FailingPostResponse()

        def close(self):
            self.closed = True

    monkeypatch.setattr(http.client, "HTTPConnection", FailingPostConnection)
    with pytest.raises(expected_error):
        live_server[0]({})
    assert len(connections) == 1
    assert connections[0].closed


def _scan_ffmpeg(
        monkeypatch, tmp_path, *, installed, latest, managed=False,
        platform='win32', machine=None):
    import sys
    import yt_final
    ffmpeg_updater = sys.modules[
        yt_final.managed_install_supported.__module__
    ]

    local = tmp_path / 'local'
    if machine is None:
        machine = 'arm64' if platform == 'darwin' else 'x86_64'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setattr(yt_final._sys, 'platform', platform)
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda p=platform: p, raising=False)
    monkeypatch.setattr(ffmpeg_updater, '_machine', lambda m=machine: m, raising=False)
    if platform.startswith('win'):
        managed_path = local / 'yt_final' / 'ffmpeg' / 'current' / 'bin' / 'ffmpeg.exe'
    else:
        monkeypatch.setattr(ffmpeg_updater.Path, 'home', classmethod(lambda cls: tmp_path))
        managed_path = (
            tmp_path / 'Library' / 'Application Support' / 'yt_final' / 'ffmpeg'
            / 'current' / 'bin' / 'ffmpeg'
        )
    ffmpeg_path = managed_path if managed else tmp_path / 'system' / 'ffmpeg.exe'
    ffmpeg_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_path.write_bytes(b'ffmpeg fixture')
    monkeypatch.setattr(yt_final.UpdateChecker, '_installed', classmethod(lambda cls, pkg: '1.0'))
    monkeypatch.setattr(
        yt_final.UpdateChecker, '_package_probe',
        classmethod(lambda cls, pkg: {
            'version': '1.0', 'verified': True, 'source': 'process',
        }),
    )
    monkeypatch.setattr(yt_final.UpdateChecker, '_pypi_latest', classmethod(lambda cls, pkg, timeout=3: '1.0'))
    # Isolate from the real bgutil PO server on :4416. Left live, its version (e.g. 1.3.1) leaks into the
    # scan and mismatches the mocked plugin (1.0), producing a spurious "plugin != server" warning that
    # flips summary.ok False and breaks `warnings == []`. Whether the 0.35s probe wins the race is
    # timing/order dependent (passes alone, fails in the full suite), so pin it to "not running".
    monkeypatch.setattr(yt_final.UpdateChecker, '_bgutil_server_version',
                        staticmethod(lambda base=None: None))

    def command(cls, args, pattern=None, timeout=2):
        if str(args[0]) != str(ffmpeg_path) or installed is None:
            return None
        banner = f'ffmpeg version {installed} Copyright (c) FFmpeg developers'
        match = re.search(pattern, banner) if pattern else None
        return match.group(1) if match else banner.splitlines()[0]

    monkeypatch.setattr(yt_final.UpdateChecker, '_cmd_first_line', classmethod(command))
    monkeypatch.setattr(
        yt_final.UpdateChecker,
        '_ffmpeg_release_latest',
        classmethod(lambda cls: latest),
        raising=False,
    )
    report = yt_final.UpdateChecker._scan_uncached(
        check_pypi=True,
        ffmpeg_path=str(ffmpeg_path),
        include_optional_tools=False,
    )
    return report, next(row for row in report['info'] if row['name'] == 'ffmpeg')


def test_updates_report_payload_shape():
    r = webserver.updates_report_payload(check_pypi=False, force=True)
    assert r.get('ok') is True or r.get('error')
    assert 'pip' in r and isinstance(r['pip'], list)
    assert 'info' in r and isinstance(r['info'], list)
    assert 'summary' in r and isinstance(r['summary'], dict)
    assert 'checked_at' in r
    # pip rows for known packages
    names = {p.get('name') for p in r['pip']}
    assert 'yt-dlp' in names
    # optional tools listed in info
    info_names = {p.get('name') for p in r['info']}
    assert 'ffmpeg' in info_names
    assert 'aria2c' in info_names or 'N_m3u8DL-RE' in info_names


def test_update_checker_tracks_all_seven_certified_direct_projects():
    """Scan/update result semantics must cover the complete declared lock input."""
    import yt_final

    assert set(yt_final.UpdateChecker.PIP_PACKAGES) == {
        'packaging',
        'regex',
        'yt-dlp',
        'yt-dlp-ejs',
        'bgutil-ytdlp-pot-provider',
        'requests',
        'urllib3',
    }


def test_updates_scan_cache_reuses_report():
    """Second scan without force should hit the short TTL cache."""
    try:
        from yt_final import UpdateChecker
    except ImportError:
        from Final.yt_final import UpdateChecker
    UpdateChecker.invalidate_cache()
    a = webserver.updates_report_payload(check_pypi=False, force=True)
    assert a.get('ok') is True
    assert a.get('cached') is False
    b = webserver.updates_report_payload(check_pypi=False, force=False)
    assert b.get('ok') is True
    assert b.get('cached') is True
    assert b.get('cache_age_s') is not None


def test_ffmpeg_scan_marks_outdated_system_install_as_updatable(monkeypatch, tmp_path):
    import yt_final
    import ffmpeg_updater

    monkeypatch.setattr(yt_final._sys, 'platform', 'win32')
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    report, ffmpeg = _scan_ffmpeg(
        monkeypatch, tmp_path, installed='7.1.1', latest='8.1.2', managed=False)

    assert ffmpeg['name'] == 'ffmpeg'
    assert ffmpeg['installed'] == '7.1.1'
    assert ffmpeg['latest'] == '8.1.2'
    assert ffmpeg['outdated'] is True
    assert ffmpeg['managed'] is False
    assert ffmpeg['path'] == str(tmp_path / 'system' / 'ffmpeg.exe')
    assert ffmpeg['updatable'] is True
    assert ffmpeg['note'] == 'A newer managed FFmpeg release is available for Windows'
    assert ffmpeg['managed_location'] == r'%LOCALAPPDATA%\yt_final\ffmpeg'
    assert ffmpeg['host_os'] == 'Windows'
    assert ffmpeg['managed_locations']['Windows'] == r'%LOCALAPPDATA%\yt_final\ffmpeg'
    assert set(ffmpeg['managed_locations']) == {'Windows'}
    assert report['host_os'] == 'Windows'
    assert report['summary']['outdated_count'] == 0
    assert report['summary']['ok'] is True


def test_ffmpeg_scan_marks_current_managed_install(monkeypatch, tmp_path):
    _, ffmpeg = _scan_ffmpeg(
        monkeypatch, tmp_path, installed='8.1.2', latest='8.1.2', managed=True)

    assert ffmpeg['latest'] == '8.1.2'
    assert ffmpeg['outdated'] is False
    assert ffmpeg['managed'] is True
    assert ffmpeg['updatable'] is True
    assert ffmpeg['note'] == 'Managed FFmpeg is current (Windows)'


def test_ffmpeg_scan_disables_managed_action_on_linux(monkeypatch, tmp_path):
    _, ffmpeg = _scan_ffmpeg(
        monkeypatch, tmp_path, installed='7.1.1', latest='8.1.2', managed=False,
        platform='linux')

    assert ffmpeg['updatable'] is False
    assert 'windows x86_64' in ffmpeg['note'].lower()


def test_ffmpeg_scan_keeps_managed_action_enabled_on_windows(monkeypatch, tmp_path):
    _, ffmpeg = _scan_ffmpeg(
        monkeypatch, tmp_path, installed='7.1.1', latest='8.1.2', managed=False,
        platform='win32')

    assert ffmpeg['updatable'] is True


def test_ffmpeg_scan_disables_restricted_managed_action_on_darwin(
        monkeypatch, tmp_path):
    _, ffmpeg = _scan_ffmpeg(
        monkeypatch, tmp_path, installed='7.1.1', latest='8.1.2', managed=False,
        platform='darwin', machine='arm64')

    assert ffmpeg['updatable'] is False
    assert ffmpeg['host_os'] == 'macOS'
    assert set(ffmpeg['managed_locations']) == {'Windows'}
    assert 'windows x86_64' in ffmpeg['note'].lower()
    assert 'homebrew' in ffmpeg['note'].lower()


def test_ffmpeg_scan_normalizes_gyan_build_banner_version(monkeypatch, tmp_path):
    _, ffmpeg = _scan_ffmpeg(
        monkeypatch,
        tmp_path,
        installed='8.1.2-essentials_build-www.gyan.dev',
        latest='8.1.2',
        managed=True,
    )

    assert ffmpeg['installed'] == '8.1.2'
    assert ffmpeg['installed'] == ffmpeg['latest']


def test_ffmpeg_scan_preserves_snapshot_version_token(monkeypatch, tmp_path):
    _, ffmpeg = _scan_ffmpeg(
        monkeypatch,
        tmp_path,
        installed='N-123456-gabcdef',
        latest=None,
        managed=False,
    )

    assert ffmpeg['installed'] == 'N-123456-gabcdef'


def test_ffmpeg_scan_release_lookup_failure_is_not_a_pip_warning(monkeypatch, tmp_path):
    report, ffmpeg = _scan_ffmpeg(
        monkeypatch, tmp_path, installed='8.1.2', latest=None, managed=True)

    assert ffmpeg['latest'] is None
    assert ffmpeg['outdated'] is False
    assert report['warnings'] == []
    assert report['summary']['warning_count'] == 0
    assert report['summary']['ok'] is True


def test_ffmpeg_scan_note_says_not_found_when_no_binary_is_installed(monkeypatch, tmp_path):
    _, ffmpeg = _scan_ffmpeg(
        monkeypatch, tmp_path, installed=None, latest='8.1.2', managed=False)

    assert 'not found' in ffmpeg['note'].lower()
    assert 'managed install' in ffmpeg['note'].lower()
    assert 'current' not in ffmpeg['note'].lower()


def test_forced_scan_reuses_valid_ffmpeg_release_metadata(monkeypatch, tmp_path):
    import yt_final
    import ffmpeg_updater

    local = tmp_path / 'local'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    calls = []
    release = ffmpeg_updater.FFmpegRelease('8.1.2', 'https://example.invalid/release.zip', 'a' * 64)
    monkeypatch.setattr(yt_final, 'fetch_ffmpeg_release', lambda: calls.append(True) or release, raising=False)
    monkeypatch.setattr(yt_final.UpdateChecker, '_installed', classmethod(lambda cls, pkg: '1.0'))
    monkeypatch.setattr(yt_final.UpdateChecker, '_pypi_latest', classmethod(lambda cls, pkg, timeout=3: '1.0'))
    # Isolate from the real bgutil PO server on :4416 (see _scan_ffmpeg) so a live version can't leak in.
    monkeypatch.setattr(yt_final.UpdateChecker, '_bgutil_server_version',
                        staticmethod(lambda base=None: None))
    monkeypatch.setattr(
        yt_final.UpdateChecker,
        '_cmd_first_line',
        classmethod(lambda cls, args, pattern=None, timeout=2: '8.1.2' if args[0] == 'ffmpeg' else None),
    )
    yt_final.UpdateChecker.invalidate_cache()
    yt_final.UpdateChecker._ffmpeg_release_mem = None

    first = yt_final.UpdateChecker.scan(
        check_pypi=True, include_optional_tools=False, force=True)
    second = yt_final.UpdateChecker.scan(
        check_pypi=True, include_optional_tools=False, force=True)

    assert first['cached'] is False
    assert second['cached'] is False
    assert calls == [True]


def test_default_scan_prefers_valid_managed_ffmpeg_pair(monkeypatch, tmp_path):
    import yt_final

    local = tmp_path / 'local'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setattr(yt_final._sys, 'platform', 'win32')
    managed = local / 'yt_final' / 'ffmpeg' / 'current' / 'bin' / 'ffmpeg.exe'
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b'managed ffmpeg fixture')
    resolver_calls = []

    def certified_resolution(*args, **kwargs):
        resolver_calls.append((args, kwargs))
        assert args == ()
        assert kwargs == {}
        return type('Resolution', (), {'path': str(managed)})()

    monkeypatch.setattr(
        yt_final,
        'resolve_ffmpeg',
        certified_resolution,
        raising=False,
    )
    monkeypatch.setattr(yt_final, 'managed_ffmpeg_path', lambda: managed, raising=False)
    monkeypatch.setattr(yt_final.UpdateChecker, '_ffmpeg_release_latest',
                        classmethod(lambda cls: '8.1.2'))
    monkeypatch.setattr(yt_final.UpdateChecker, '_installed', classmethod(lambda cls, pkg: '1.0'))
    # Isolate from the real bgutil PO server on :4416 (see _scan_ffmpeg) so a live version can't leak in.
    monkeypatch.setattr(yt_final.UpdateChecker, '_bgutil_server_version',
                        staticmethod(lambda base=None: None))
    monkeypatch.setattr(
        yt_final.UpdateChecker,
        '_cmd_first_line',
        classmethod(lambda cls, args, pattern=None, timeout=2:
                    '8.1.2' if str(args[0]) == str(managed) else None),
    )
    yt_final.UpdateChecker.invalidate_cache()

    report = yt_final.UpdateChecker.scan(
        check_pypi=False, include_optional_tools=False, force=True)
    ffmpeg = next(row for row in report['info'] if row['name'] == 'ffmpeg')

    assert ffmpeg['installed'] == '8.1.2'
    assert ffmpeg['managed'] is True
    assert ffmpeg['path'] == str(managed)
    assert resolver_calls == [((), {})]


def test_shared_ffmpeg_resolver_ignores_untrusted_caller_working_directory(
        monkeypatch, tmp_path):
    import capability

    project = tmp_path / 'project'
    project.mkdir()
    executable = project / 'ffmpeg.exe'
    executable.write_bytes(b'ffmpeg')
    calls = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return type('Completed', (), {
            'returncode': 0,
            'stdout': 'ffmpeg version 8.1.2 Copyright\n',
            'stderr': '',
        })()

    resolution = capability.resolve_ffmpeg(
        managed_path_func=lambda: tmp_path / 'missing' / 'ffmpeg.exe',
        probe_pair_func=lambda root: (_ for _ in ()).throw(RuntimeError('missing')),
        runner=runner,
        which=lambda name: None,
        cwd=project,
        platform='win32',
        application_dir=tmp_path / 'trusted-app',
    )

    assert resolution.path is None
    assert resolution.version_line is None
    assert resolution.managed is False
    assert calls == []


def test_new_managed_pair_wins_over_stale_system_scan_cache(monkeypatch, tmp_path):
    import yt_final

    system = tmp_path / 'system' / 'ffmpeg.exe'
    managed = tmp_path / 'managed' / 'bin' / 'ffmpeg.exe'
    system.parent.mkdir(parents=True)
    system.write_bytes(b'system ffmpeg fixture')
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b'managed ffmpeg fixture')
    resolutions = iter((system, managed))
    monkeypatch.setattr(
        yt_final,
        'resolve_ffmpeg',
        lambda **kwargs: type('Resolution', (), {'path': str(next(resolutions))})(),
        raising=False,
    )

    def scan_uncached(cls, **kwargs):
        path = kwargs['ffmpeg_path']
        return {
            'pip': [],
            'info': [{'name': 'ffmpeg', 'path': path}],
            'warnings': [],
            'summary': {},
        }

    monkeypatch.setattr(
        yt_final.UpdateChecker,
        '_scan_uncached',
        classmethod(scan_uncached),
    )
    yt_final.UpdateChecker.invalidate_cache()

    first = yt_final.UpdateChecker.scan(
        check_pypi=False, include_optional_tools=False, force=False)
    second = yt_final.UpdateChecker.scan(
        check_pypi=False, include_optional_tools=False, force=False)

    assert first['info'][0]['path'] == str(system)
    assert second['info'][0]['path'] == str(managed)
    assert second['cached'] is False


def test_updates_html_tab_present():
    from pathlib import Path
    html = (Path(__file__).parent / 'webui' / 'index.html').read_text(encoding='utf-8')
    js = (Path(__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')
    assert 'id="nav-updates"' in html
    assert 'id="view-updates"' in html
    assert 'id="updates_check"' in html
    assert 'id="updates_all"' not in html
    assert 'id="updates_install"' not in html
    assert 'id="updates_apply"' not in html
    assert 'future reviewed yt_final release' in html
    assert 'runCertifiedPythonSet' not in js
    assert "JSON.stringify({ action: 'all' })" not in js


def test_ffmpeg_update_ui_renders_managed_row_action_labels():
    from pathlib import Path
    js = (Path(__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')

    assert 'data-system-tool="ffmpeg"' in js
    assert 'ffmpegActionForOs' in js
    assert 'Install for ${selectedOs}' in js or 'Install for' in js
    assert 'Install managed' in js or 'Reinstall (${selectedOs})' in js
    assert 'toolRowHtml' in js
    assert 'upd-section' in js or 'Download packages' in js


def test_ffmpeg_update_ui_suppresses_action_when_scan_is_not_updatable():
    from pathlib import Path
    js = (Path(__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')

    harness = r"""
let HOST_MANAGED_LOCATIONS = { Windows: 'C:/managed' };
function esc(value) { return String(value == null ? '' : value); }
"""
    harness += _js_function_source(js, "ffmpegSetupPresentation")
    harness += _js_function_source(js, "ffmpegActionForOs")
    harness += r"""
function snapshot(markup) {
  const label = markup.match(/>([^<]*)<\/button>/);
  const title = markup.match(/ title="([^"]*)"/);
  return {
    enabled: markup.includes('data-system-tool="ffmpeg"'),
    disabled: /\sdisabled(?:\s|>)/.test(markup),
    label: label ? label[1] : null,
    title: title ? title[1] : null,
  };
}
const eligible = {
  installed: null, latest: '7.1', managed: false, outdated: true,
  updatable: true, managed_locations: { Windows: 'C:/managed' },
};
const ineligible = Object.assign({}, eligible, { updatable: false });
console.log(JSON.stringify({
  windowsEligible: snapshot(ffmpegActionForOs(eligible, 'Windows', 'Windows')),
  macPreview: snapshot(ffmpegActionForOs(eligible, 'macOS', 'Windows')),
  hostMismatch: snapshot(ffmpegActionForOs(eligible, 'Windows', 'macOS')),
  unknownHost: snapshot(ffmpegActionForOs(eligible, 'Windows', null)),
  rowIneligible: snapshot(ffmpegActionForOs(ineligible, 'Windows', 'Windows')),
  eligibleSetup: ffmpegSetupPresentation('Windows', 'Windows', true),
  ineligibleSetup: ffmpegSetupPresentation('Windows', 'Windows', false),
}));
"""
    assert _node_json(harness) == {
        "windowsEligible": {
            "enabled": True,
            "disabled": False,
            "label": "Install for Windows",
            "title": (
                "Install private managed FFmpeg for Windows "
                "(does not replace system FFmpeg)"
            ),
        },
        "macPreview": {
            "enabled": False,
            "disabled": True,
            "label": "Manual FFmpeg",
            "title": "macOS setup is manual-only; managed FFmpeg installs only for Windows",
        },
        "hostMismatch": {
            "enabled": False,
            "disabled": True,
            "label": "Unavailable on this host",
            "title": "Windows managed FFmpeg cannot be installed on a macOS host",
        },
        "unknownHost": {
            "enabled": False,
            "disabled": True,
            "label": "Unavailable on this host",
            "title": "Windows managed FFmpeg cannot be installed until the host is confirmed",
        },
        "rowIneligible": {
            "enabled": False,
            "disabled": True,
            "label": "Install unavailable",
            "title": "This FFmpeg scan result is not eligible for managed installation",
        },
        "eligibleSetup": {
            "heading": "Managed install (Windows)",
            "locationLabel": "Private folder",
            "note": "This matches your host. The button installs only into the private folder.",
        },
        "ineligibleSetup": {
            "heading": "Managed install (Windows)",
            "locationLabel": "Private folder",
            "note": (
                "This matches the Windows target, but this scan result is not eligible "
                "for managed installation."
            ),
        },
    }


def test_managed_ffmpeg_documentation_states_windows_only_and_macos_manual():
    from pathlib import Path
    readme = (Path(__file__).parent / 'README.md').read_text(encoding='utf-8')

    assert re.search(r'Windows', readme)
    assert re.search(r'macOS', readme, re.IGNORECASE)
    assert 'Windows x86_64' in readme
    assert 'manual or Homebrew' in readme
    assert re.search(r'Start yt_final\.command', readme)


def test_ffmpeg_update_ui_posts_empty_payload_and_preserves_report():
    from pathlib import Path
    js = (Path(__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')

    assert "sessionFetch('/api/updates/ffmpeg'" in js
    assert 'body: JSON.stringify({})' in js
    assert 'Downloading and validating FFmpeg…' in js
    assert 'if (body.report) renderUpdates(body.report);' in js
    assert 'finally {' in js
    assert 'setUpdatesBusy(false);' in js
    assert 'control.release();' in js


def test_clicked_ffmpeg_row_shows_progress_text_and_restores_label():
    from pathlib import Path
    js = (Path(__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')

    ffmpeg_action = js[js.index('async function runFFmpegAction'):js.index("if ($('updates_check'))")]
    assert "action.ownControl(btn, { text: 'Downloading…' })" in ffmpeg_action
    assert "btn.textContent = 'Installing…';" in ffmpeg_action
    assert 'control.release();' in ffmpeg_action
    assert 'runCertifiedPythonSet' not in js


def test_ffmpeg_update_ui_uses_delegation_and_managed_install_copy():
    from pathlib import Path
    html = (Path(__file__).parent / 'webui' / 'index.html').read_text(encoding='utf-8')
    js = (Path(__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')

    assert "btn.dataset.systemTool === 'ffmpeg'" in js
    assert 'latest stable' in js
    assert 'managedLocation' in js or 'managed_location' in js or 'managed-location' in js
    assert 'Private managed location' in js or 'managed location' in js.lower()
    static_os_group = html[
        html.index('id="updates_os_bar"'):html.index('id="updates_os_detect"')
    ]
    assert 'role="group"' in static_os_group
    assert 'role="tab"' not in static_os_group and 'role="tablist"' not in static_os_group
    for os_name in ('Windows', 'macOS'):
        assert f'type="button" class="upd-os-tab" data-os="{os_name}"' in static_os_group
        assert f'data-os="{os_name}"' in js
    assert static_os_group.count('aria-pressed="false"') == 2

    dynamic_os_group = js[js.index('id="updates_os_bar_live"'):js.index('id="updates_os_detect_live"')]
    assert 'role="group"' in dynamic_os_group
    assert 'role="tab"' not in dynamic_os_group and 'role="tablist"' not in dynamic_os_group
    assert dynamic_os_group.count('type="button" class="upd-os-tab"') == 2
    assert dynamic_os_group.count('aria-pressed=') == 2

    assert '_updatesOsTab' in js and 'syncUpdatesOsTabs' in js
    assert 'function effectiveUpdatesOs(report)' in js
    assert 'const selectedOs = effectiveUpdatesOs(report);' in js
    setup_copy = js[
        js.index('function ffmpegSetupPresentation'):
        js.index('function ffmpegActionForOs')
    ]
    assert "const managedTarget = selectedOs === 'Windows';" in setup_copy
    assert "selectedOs === hostOs" in setup_copy
    assert 'Manual setup (macOS)' in setup_copy
    render_updates = js[
        js.index('function renderUpdates'):
        js.index('async function refreshUpdates')
    ]
    assert (
        'const setup = ffmpegSetupPresentation(selectedOs, hostOs, !!ffmpeg.updatable);'
        in render_updates
    )
    assert '<h4>${esc(setup.heading)}</h4>' in render_updates
    assert '${esc(setup.locationLabel)}:' in render_updates
    assert '${esc(setup.note)}' in render_updates
    assert r'%LOCALAPPDATA%\yt_final\ffmpeg' in html or 'LOCALAPPDATA' in html
    assert 'manual or Homebrew FFmpeg' in html
    # Redesigned clarity surfaces
    assert 'upd-glossary' in html
    assert 'Host computer' in html or 'This computer' in html
    assert 'System FFmpeg' in html and 'Managed FFmpeg' in html
    assert 'Download packages' in js
    assert 'id="updates_host_card"' in html


def test_updates_ui_does_not_default_host_os_to_macos():
    """Windows hosts must not auto-select the macOS FFmpeg tab before scan."""
    from pathlib import Path
    js = (Path(__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')
    # Old bug: `hostOsFromReport(report) || 'macOS'`
    assert "hostOsFromReport(report) || 'macOS'" not in js
    assert "hostOsFromReport(report) || \"macOS\"" not in js
    assert 'Never default to macOS' in js or 'detecting' in js


def test_api_health_reports_host_os(monkeypatch):
    """ /api/health is the shared detection source for the whole UI. """
    from pathlib import Path

    import ffmpeg_updater
    import webserver

    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'darwin', raising=False)
    # Build a minimal handler response by calling the same helpers the route uses.
    host = ffmpeg_updater.host_os_label()
    locs = ffmpeg_updater.managed_locations_by_os()
    assert host == 'macOS'
    assert set(locs) == {'Windows'}

    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'win32', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', r'C:\Users\test\AppData\Local')
    assert ffmpeg_updater.host_os_label() == 'Windows'

    # Route body shape (live handler path through updates helpers already exercised).
    # Structural: webserver health block includes the keys.
    src = Path(webserver.__file__).read_text(encoding='utf-8')
    assert "'host_os'" in src or '"host_os"' in src
    assert 'host_os_label' in src
    assert 'managed_locations' in src


def test_update_checker_cache_path_matches_platform_helper(monkeypatch, tmp_path):
    """Passive update cache must use cache_dir(), not a stale class Path."""
    import sys
    import yt_final

    ffmpeg_updater = sys.modules[yt_final._app_cache_dir.__module__]
    monkeypatch.setattr(ffmpeg_updater, '_platform', lambda: 'darwin', raising=False)
    monkeypatch.setattr(ffmpeg_updater.Path, 'home', classmethod(lambda cls: tmp_path))
    path = yt_final.UpdateChecker._cache_path()
    assert path == tmp_path / 'Library' / 'Caches' / 'yt_final' / 'update_check.json'
    # No divergent class-level CACHE pointing at ~/.cache
    assert not hasattr(yt_final.UpdateChecker, 'CACHE') or (
        Path(yt_final.UpdateChecker.CACHE) == path
    )


def test_windows_and_darwin_managed_roots_diverge(monkeypatch, tmp_path):
    """Shipped helpers must not share one path shape for both OSes."""
    import ffmpeg_updater as fu

    monkeypatch.setattr(fu, '_platform', lambda: 'win32', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'winlocal'))
    win_root = fu.managed_root()
    win_bin = fu.ffmpeg_binary_name()
    win_disp = fu.managed_root_display()

    monkeypatch.setattr(fu, '_platform', lambda: 'darwin', raising=False)
    monkeypatch.setattr(fu.Path, 'home', classmethod(lambda cls: tmp_path / 'machome'))
    mac_root = fu.managed_root()
    mac_bin = fu.ffmpeg_binary_name()
    mac_disp = fu.managed_root_display()

    assert win_bin == 'ffmpeg.exe' and mac_bin == 'ffmpeg'
    assert win_root != mac_root
    assert 'AppData' in str(win_root) or 'winlocal' in str(win_root)
    assert 'Application Support' in str(mac_root)
    assert 'LOCALAPPDATA' in win_disp
    assert 'Application Support' in mac_disp
    assert fu.managed_install_supported('win32', 'x86_64') is True
    assert fu.managed_install_supported('darwin', 'arm64') is False
    assert fu.managed_install_supported('darwin', 'x86_64') is False
    assert fu.managed_install_supported('linux') is False


@pytest.mark.parametrize("mode", ["auto", "install", "update", "all", "bogus"])
def test_python_update_freeze_precedes_guard_scan_report_and_process_io(
        monkeypatch, mode):
    """Every legacy mutation label is one stable, side-effect-free response."""
    import managed_env

    class SideEffectReached(BaseException):
        pass

    def forbidden(*_args, **_kwargs):
        raise SideEffectReached("frozen Python update reached side-effecting code")

    monkeypatch.setattr(webserver, "installer_guard", forbidden)
    monkeypatch.setattr(webserver.subprocess, "run", forbidden)
    monkeypatch.setattr(webserver, "updates_report_payload", forbidden)
    monkeypatch.setattr(
        webserver,
        "_apply_pip_updates_guarded",
        forbidden,
        raising=False,
    )

    result = webserver.apply_pip_updates(mode=mode)

    assert result == {
        "ok": False,
        "updated": [],
        "mode": "certified-release",
        "requested_mode": mode if mode != "bogus" else "auto",
        "code": managed_env.CERTIFIED_RUNTIME_UNAVAILABLE_CODE,
        "error": managed_env.CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE,
        "log": "",
    }


@pytest.mark.parametrize('selection', [None, [], ['yt-dlp'], ['evil-package']])
def test_direct_python_update_rejects_explicit_selection_before_guard(
        monkeypatch, selection):
    """Internal callers cannot bypass the route by passing even an empty/null selection."""
    class GuardReached(BaseException):
        pass

    monkeypatch.setattr(
        webserver,
        'installer_guard',
        lambda *_a, **_k: (_ for _ in ()).throw(
            GuardReached('explicit package selection reached installer guard')
        ),
    )

    with pytest.raises(ValueError, match='future reviewed yt_final release'):
        webserver.apply_pip_updates(packages=selection)


@pytest.mark.parametrize('selection', [None, [], ['yt-dlp'], 'yt-dlp', {}])
def test_api_rejects_any_packages_key_before_python_mutation(
        monkeypatch, live_server, selection):
    """Presence of the key—not merely its value—closes the former per-row API."""
    post, _ = live_server
    calls = []

    def mutation_forbidden(**_kwargs):
        calls.append(dict(_kwargs))
        return {'ok': True, 'updated': ['unexpected'], 'mode': 'legacy'}

    monkeypatch.setattr(webserver, 'apply_pip_updates', mutation_forbidden)

    status, body = post(
        {'action': 'all', 'packages': selection},
        path='/api/updates',
    )

    assert status == 400
    assert body == {
        'ok': False,
        'code': 'per-package-disabled',
        'error': (
            'Per-package updates are disabled; '
            'install a future reviewed yt_final release'
        ),
    }
    assert calls == []


@pytest.mark.parametrize('action', ['auto', 'install', 'update', 'all'])
def test_api_bulk_actions_never_pass_a_packages_argument(
        monkeypatch, live_server, action):
    """Legacy action names may remain compatible only as aliases for whole-lock install."""
    post, _ = live_server
    calls = []

    def whole_lock_only(**kwargs):
        calls.append(dict(kwargs))
        return {
            'ok': True,
            'updated': ['yt-dlp', 'requests'],
            'mode': 'certified-lock',
            'log': 'installed certified closure',
        }

    monkeypatch.setattr(webserver, 'apply_pip_updates', whole_lock_only)

    status, body = post({'action': action}, path='/api/updates')

    assert status == 200
    assert body['ok'] is True
    assert body['mode'] == 'certified-lock'
    assert calls == [{'mode': action, 'pot_base_url': None}]


@pytest.mark.parametrize('action', ['auto', 'install', 'update', 'all'])
def test_python_update_route_returns_stable_reviewed_release_conflict(
        live_server, action):
    import managed_env

    post, _ = live_server
    status, body = post({'action': action}, path='/api/updates')

    assert status == 409
    assert body == {
        'ok': False,
        'updated': [],
        'mode': 'certified-release',
        'requested_mode': action,
        'code': managed_env.CERTIFIED_RUNTIME_UNAVAILABLE_CODE,
        'error': managed_env.CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE,
        'log': '',
    }


def test_cli_update_contains_no_mutable_helper_or_per_package_instructions():
    """CLI guidance must not advertise a bypass around certified mutations."""
    import yt_final

    source = Path(yt_final.__file__).read_text(encoding='utf-8')
    update_source = source[
        source.index('    def run_update('):
        source.index('    def passive_notice(')
    ]

    for forbidden in (
        'pip install -U',
        'docker pull',
        'git pull',
        'npm install',
    ):
        assert forbidden not in update_source


def test_updates_ui_exposes_reviewed_release_guidance_and_no_python_mutation():
    """The browser reports Python status without minting a mutation control."""
    root = Path(webserver.__file__).resolve().parent
    js = (root / 'webui' / 'app.js').read_text(encoding='utf-8')
    html = (root / 'webui' / 'index.html').read_text(encoding='utf-8')
    requirements = (root / 'requirements.txt').read_text(encoding='utf-8')

    assert 'data-pkg=' not in js
    assert 'body.packages' not in js
    assert 'updates_install' not in html
    assert 'updates_apply' not in html
    assert 'id="updates_all"' not in html
    assert 'future reviewed yt_final release' in html
    assert 'runCertifiedPythonSet' not in js
    assert "JSON.stringify({ action: 'all' })" not in js
    optional_allowlist = js[
        js.index('const OPTIONAL_INSTALL_TOOLS'):
        js.index('function syncUpdatesOsTabs')
    ]
    assert "'node'" in optional_allowlist
    assert "'deno'" in optional_allowlist
    for manual_only in (
        "'aria2c'",
        "'N_m3u8DL-RE'",
        "'playwright'",
        "'bgutil-server'",
        "'bgutil-plugin'",
    ):
        assert manual_only not in optional_allowlist
    assert 'install on Updates' not in html
    assert 'install on the <strong>Updates</strong> tab' not in html
    assert 'the Updates tab\n# can install it for you' not in requirements
    assert 'private venv' not in requirements.lower()
    assert 'aria2c, turbo, and Playwright require manual installation' in html


def test_manual_optional_tool_api_returns_guidance_before_guard_or_report(
        monkeypatch, live_server):
    """Manual-only dispatch must not acquire a mutation lock or force a scan."""
    import optional_tools

    class UnexpectedIo(BaseException):
        pass

    post, _ = live_server
    monkeypatch.setattr(
        webserver,
        'installer_guard',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            UnexpectedIo('manual guidance reached installer guard')
        ),
    )
    monkeypatch.setattr(
        webserver,
        'updates_report_payload',
        lambda **_kwargs: (_ for _ in ()).throw(
            UnexpectedIo('manual guidance triggered a forced update report')
        ),
    )

    status, body = post({'tool': 'aria2c'}, path='/api/updates/tools')

    guidance = optional_tools.MANUAL_INSTALL_GUIDANCE['aria2c']
    assert status == 422
    assert body == {
        'ok': False,
        'error': guidance,
        'code': 'unsupported',
        'result': {
            'ok': False,
            'tool': 'aria2c',
            'status': 'manual',
            'code': 'unsupported',
            'message': guidance,
        },
    }


@pytest.mark.parametrize(
    ('path', 'payload'),
    [
        ('/api/updates/tools', {'tool': 'aria2c'}),
        ('/api/updates/ffmpeg', {}),
        ('/api/updates', {'action': 'all'}),
    ],
)
def test_authenticated_mutation_api_maps_busy_before_report(
        monkeypatch, live_server, path, payload):
    import optional_tools

    post, _ = live_server

    def busy(*_args, **_kwargs):
        raise InstallerBusyError(INSTALLER_BUSY_MESSAGE)

    if path == '/api/updates/tools':
        monkeypatch.setattr(optional_tools, 'install_optional_tool', busy)
    elif path == '/api/updates/ffmpeg':
        class BusyUpdater:
            install = busy
        monkeypatch.setattr(
            webserver, 'ManagedFFmpegUpdater', BusyUpdater, raising=False)
    else:
        monkeypatch.setattr(webserver, 'apply_pip_updates', busy)
    monkeypatch.setattr(
        webserver,
        'updates_report_payload',
        lambda **_kwargs: pytest.fail('busy mutation must not make a report'),
    )

    status, body = post(payload, path=path)

    assert status == 409
    assert body == {
        'ok': False,
        'code': INSTALLER_BUSY_CODE,
        'error': INSTALLER_BUSY_MESSAGE,
    }


def test_optional_tool_api_maps_dispatcher_normalized_busy_to_409(
        monkeypatch, live_server):
    """The real dispatcher returns a typed busy result instead of re-raising."""
    import optional_tools

    post, _ = live_server
    monkeypatch.setattr(
        optional_tools,
        'install_optional_tool',
        lambda _tool: {
            'ok': False,
            'tool': 'node',
            'code': INSTALLER_BUSY_CODE,
            'error': INSTALLER_BUSY_MESSAGE,
        },
    )
    monkeypatch.setattr(
        webserver,
        'updates_report_payload',
        lambda **_kwargs: pytest.fail(
            'normalized busy result must not make a report'
        ),
    )

    status, body = post({'tool': 'node'}, path='/api/updates/tools')

    assert status == 409
    assert body == {
        'ok': False,
        'code': INSTALLER_BUSY_CODE,
        'error': INSTALLER_BUSY_MESSAGE,
    }


@pytest.mark.parametrize(
    ('path', 'payload'),
    [
        ('/api/updates/tools', {'tool': 'aria2c'}),
        ('/api/updates/ffmpeg', {}),
        ('/api/updates', {'action': 'install', 'packages': ['yt-dlp']}),
    ],
)
def test_unauthenticated_busy_mutation_api_remains_401(
        monkeypatch, live_server, path, payload):
    post, _ = live_server
    called = []

    def busy(*_args, **_kwargs):
        called.append(True)
        raise InstallerBusyError(INSTALLER_BUSY_MESSAGE)

    if path == '/api/updates/ffmpeg':
        class BusyUpdater:
            install = busy
        monkeypatch.setattr(
            webserver, 'ManagedFFmpegUpdater', BusyUpdater, raising=False)
    elif path == '/api/updates':
        monkeypatch.setattr(webserver, 'apply_pip_updates', busy)
    else:
        import optional_tools
        monkeypatch.setattr(optional_tools, 'install_optional_tool', busy)

    status, body = post(payload, token=None, path=path)

    assert status == 401
    assert body['error'] == 'unauthorized'
    assert called == []


@pytest.mark.parametrize(
    'packages',
    [
        'yt-dlp',
        ['not-a-managed-package'],
    ],
)
def test_pip_route_disables_legacy_package_selection_before_mutation(
        monkeypatch, live_server, packages):
    post, _ = live_server
    monkeypatch.setattr(
        webserver,
        'apply_pip_updates',
        lambda **_kwargs: pytest.fail(
            'disabled package selection must not reach mutation helper'
        ),
    )

    status, body = post(
        {'action': 'install', 'packages': packages},
        path='/api/updates',
    )

    assert status == 400
    assert body['ok'] is False
    assert body['code'] == 'per-package-disabled'
    assert body['error'] == (
        'Per-package updates are disabled; '
        'install a future reviewed yt_final release'
    )


def test_ffmpeg_update_route_requires_mutation_auth(monkeypatch, live_server):
    post, _ = live_server
    called = []

    class FakeUpdater:
        def install(self, *, busy):
            called.append(True)
            return {'ok': True, 'status': 'installed', 'version': '8.1.2',
                    'checksum_verified': True, 'message': 'installed'}

    monkeypatch.setattr(webserver, 'ManagedFFmpegUpdater', FakeUpdater, raising=False)
    status, body = post({}, token=None)

    assert status == 401
    assert body['error'] == 'unauthorized'
    assert called == []


@pytest.mark.parametrize('field', ['url', 'archive_url', 'path', 'destination'])
def test_ffmpeg_update_route_rejects_client_controlled_locations(
        monkeypatch, live_server, field):
    post, _ = live_server
    called = []
    reports = []

    class FakeUpdater:
        def install(self, *, busy):
            called.append(True)
            raise AssertionError('forbidden payload must not reach updater')

    monkeypatch.setattr(webserver, 'ManagedFFmpegUpdater', FakeUpdater, raising=False)
    monkeypatch.setattr(
        webserver,
        'updates_report_payload',
        lambda **kwargs: reports.append(kwargs) or {'ok': True, 'fresh': True},
    )
    status, body = post({field: 'https://attacker.invalid/payload.zip'})

    assert status == 400
    assert field in body['error']
    assert body['ok'] is False
    assert body['report']['fresh'] is True
    assert reports == [{'check_pypi': True, 'pot_base_url': None, 'force': True}]
    assert called == []


def test_ffmpeg_active_gate_maps_to_stable_409_without_report(
        monkeypatch, live_server):
    post, _ = live_server

    class FakeUpdater:
        def install(self, *, busy):
            assert busy() is False
            return {'ok': False, 'status': 'busy', 'version': None,
                    'checksum_verified': False, 'message': 'internal detail'}

    monkeypatch.setattr(webserver, 'ManagedFFmpegUpdater', FakeUpdater, raising=False)
    monkeypatch.setattr(webserver.FFmpegProcessor, 'ffmpeg_busy', classmethod(lambda cls: False))
    monkeypatch.setattr(
        webserver,
        'updates_report_payload',
        lambda **kwargs: pytest.fail('active-gate busy must not make a report'),
    )
    status, body = post({})

    assert status == 409
    assert body == {
        'ok': False,
        'code': INSTALLER_BUSY_CODE,
        'error': INSTALLER_BUSY_MESSAGE,
    }


@pytest.mark.parametrize(
    'internal_message',
    [
        'darwin x86_64 is not a certified target',
        r'unsupported host details at C:\private\release-cache',
    ],
)
def test_ffmpeg_update_route_maps_unsupported_platform_before_report_or_process_io(
        monkeypatch, live_server, internal_message):
    post, _ = live_server

    class FakeUpdater:
        def install(self, *, busy):
            return {
                'ok': False,
                'status': 'unsupported',
                'code': 'unsupported',
                'version': None,
                'checksum_verified': False,
                'message': internal_message,
            }

    monkeypatch.setattr(webserver, 'ManagedFFmpegUpdater', FakeUpdater, raising=False)
    monkeypatch.setattr(
        webserver,
        'updates_report_payload',
        lambda **_kwargs: pytest.fail(
            'unsupported host must return before forcing an update report'
        ),
    )
    monkeypatch.setattr(
        webserver.subprocess,
        'run',
        lambda *_args, **_kwargs: pytest.fail(
            'unsupported host must return before process I/O'
        ),
    )

    status, body = post({})

    assert status == 400
    assert body == {
        'ok': False,
        'code': 'unsupported',
        'error': (
            'Managed FFmpeg is certified only for Windows x86_64; '
            'install FFmpeg manually (Homebrew is recommended on macOS).'
        ),
    }
    assert internal_message not in json.dumps(body)


def test_ffmpeg_update_route_returns_500_and_fresh_report_on_failure(
        monkeypatch, live_server):
    post, _ = live_server
    reports = []

    class FakeUpdater:
        def install(self, *, busy):
            return {'ok': False, 'status': 'failed', 'version': '8.1.2',
                    'checksum_verified': False,
                    'message': 'failure at C:\\Temp\\.ffmpeg-update-secret\\release.zip?sig=secret'}

    monkeypatch.setattr(webserver, 'ManagedFFmpegUpdater', FakeUpdater, raising=False)
    monkeypatch.setattr(
        webserver,
        'updates_report_payload',
        lambda **kwargs: reports.append(kwargs) or {'ok': True, 'fresh': True},
    )
    status, body = post({})

    assert status == 500
    assert body['ok'] is False
    assert body['report']['fresh'] is True
    assert reports[0]['force'] is True
    serialized = json.dumps(body)
    assert '.ffmpeg-update-secret' not in serialized
    assert 'sig=secret' not in serialized


def test_ffmpeg_update_route_success_forces_report_and_invalidates_caches(
        monkeypatch, live_server):
    post, state = live_server
    events = []
    state._engine = object()

    class FakeUpdater:
        def install(self, *, busy):
            events.append('install')
            return {'ok': True, 'status': 'installed', 'version': '8.1.2',
                    'path': r'C:\Users\name\AppData\Local\yt_final\ffmpeg\current\bin\ffmpeg.exe',
                    'checksum_verified': True, 'message': 'Managed FFmpeg 8.1.2 installed'}

    monkeypatch.setattr(webserver, 'ManagedFFmpegUpdater', FakeUpdater, raising=False)
    monkeypatch.setattr(webserver, 'invalidate_update_scan_cache',
                        lambda: events.append('scan-cache'), raising=False)
    monkeypatch.setattr(webserver, 'seed_update_ffmpeg_release_cache',
                        lambda version: events.append(('release-cache', version)), raising=False)
    monkeypatch.setattr(webserver, 'invalidate_capability_cache',
                        lambda: events.append('capability-cache'), raising=False)
    monkeypatch.setattr(
        webserver,
        'updates_report_payload',
        lambda **kwargs: events.append(('report', kwargs)) or {'ok': True, 'fresh': True},
    )
    status, body = post({})

    assert status == 200
    assert body['ok'] is True
    assert body['result']['version'] == '8.1.2'
    assert body['result']['checksum_verified'] is True
    assert 'path' not in body['result']
    assert body['report']['fresh'] is True
    assert state._engine is None
    assert events == [
        'install',
        'scan-cache',
        ('release-cache', '8.1.2'),
        'capability-cache',
        ('report', {'check_pypi': True, 'pot_base_url': None, 'force': True}),
    ]


def test_successful_install_seeds_release_cache_before_forced_report(monkeypatch):
    import yt_final

    yt_final.UpdateChecker._ffmpeg_release_mem = (time.time(), '8.0.0')

    class FakeUpdater:
        def install(self, *, busy):
            return {
                'ok': True,
                'status': 'installed',
                'version': '8.1.2',
                'checksum_verified': True,
                'message': 'installed',
            }

    monkeypatch.setattr(webserver, 'ManagedFFmpegUpdater', FakeUpdater)
    monkeypatch.setattr(webserver, 'invalidate_capability_cache', lambda: None)

    def report(**kwargs):
        return {
            'ok': True,
            'info': [{
                'name': 'ffmpeg',
                'installed': '8.1.2',
                'latest': yt_final.UpdateChecker._ffmpeg_release_latest(),
            }],
        }

    monkeypatch.setattr(webserver, 'updates_report_payload', report)

    status, body = webserver.apply_ffmpeg_update()
    ffmpeg = body['report']['info'][0]

    assert status == 200
    assert ffmpeg['installed'] == '8.1.2'
    assert ffmpeg['latest'] == '8.1.2'


@pytest.mark.parametrize(
    'failure_code, expected_status',
    [
        ('network', 502),
        ('checksum', 422),
        ('archive', 422),
        ('disk', 500),
        ('probe', 422),
        ('recovery', 500),
    ],
)
def test_ffmpeg_update_api_maps_allowlisted_actionable_failures(
        monkeypatch, failure_code, expected_status):
    import ffmpeg_updater

    class FakeUpdater:
        def install(self, *, busy):
            return {
                'ok': False,
                'status': 'failed',
                'code': failure_code,
                'message': ffmpeg_updater.PUBLIC_FAILURE_MESSAGES[failure_code],
            }

    monkeypatch.setattr(webserver, 'ManagedFFmpegUpdater', FakeUpdater)
    monkeypatch.setattr(webserver, 'updates_report_payload', lambda **kwargs: {'ok': True})

    status, body = webserver.apply_ffmpeg_update()

    assert status == expected_status
    assert body['code'] == failure_code
    assert body['error'] == ffmpeg_updater.PUBLIC_FAILURE_MESSAGES[failure_code]
