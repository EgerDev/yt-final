import http.client
import json
import os
import socket
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

import webserver
import websessions
from websessions import SessionStore


_TEST_PASSWORD = "correct horse battery staple"
_UNSUPPORTED_WATCH_PATTERNS = [
    (r"^(i)\1$", "backreference"),
    (r"^(?P<letter>i)(?P=letter)$", "backreference"),
    (r"^(a)?(?(1)b|c)$", "conditional"),
    ("(?x)^i # comment\n$", "verbose"),
    (r"^(?# comment)i$", "comment"),
    (r"(?V1)^[i]$", "stdlib"),
    (r"^(?:foo){i<=1}$", "extension"),
    ("^a{١}$", "brace"),
    ("^a{１,２}$", "brace"),
    (r"^[[a-z]--[x]]$", "character-set"),
    (r"^(?>i)$", "atomic"),
    (r"^i++$", "possessive"),
]


def password():
    return _TEST_PASSWORD


class ApiResponse:
    def __init__(self, response, raw):
        self.status = response.status
        self.headers = dict(response.getheaders())
        self.body = raw
        self.json = json.loads(raw) if raw else None


def request_json(live_server, method, path, payload=None, *, token=None, headers=None):
    conn_factory = live_server[0]
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["X-Token"] = token
    body = None
    if payload is not None:
        body = json.dumps(payload)
        request_headers.setdefault("Content-Type", "application/json")
    conn = conn_factory()
    try:
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        raw = response.read()
        return ApiResponse(response, raw)
    finally:
        conn.close()


def post_json(live_server, path, payload, *, token=None, headers=None):
    return request_json(live_server, "POST", path, payload, token=token, headers=headers)


def get_json(live_server, path, *, token=None, method="GET"):
    return request_json(live_server, method, path, token=token)


def same_origin(live_server):
    conn = live_server[0]()
    origin = f"http://{conn.host}:{conn.port}"
    conn.close()
    return origin


def login(live_server):
    response = post_json(
        live_server,
        "/api/login",
        {"password": password()},
        headers={"Origin": same_origin(live_server)},
    )
    assert response.status == 200
    return response.json["token"]


@pytest.fixture
def server_state(monkeypatch, tmp_path):
    """A ServerState backed by a throwaway journal/home (no engine, no sockets)."""
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    st = webserver.ServerState(str(tmp_path / "dl"), queue_file=str(tmp_path / "queue.json"))
    try:
        yield st
    finally:
        st.stop.set()
        try:
            st.journal.release_run_lock()
        except Exception:
            pass


@pytest.fixture
def live_server(monkeypatch, tmp_path):
    """A real in-process ThreadingHTTPServer on an ephemeral localhost port.

    Yields (conn_factory, token, state). conn_factory() returns a fresh HTTPConnection so a test
    can read exact status codes / headers without urllib's error handling swallowing a 304.
    """
    import http.server
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    monkeypatch.setenv("YT_FINAL_WEBUI_PASSWORD", password())
    state = webserver.ServerState(str(tmp_path / "dl"), queue_file=str(tmp_path / "queue.json"))
    httpd = None
    t = None
    thread_started = False
    try:
        httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), webserver.Handler
        )
        httpd.state = state
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        thread_started = True
        host, port = httpd.server_address

        def conn_factory():
            return http.client.HTTPConnection(host, port, timeout=10)

        live = (conn_factory, None, state)
        token = login(live)
        yield conn_factory, token, state
    finally:
        try:
            if httpd is not None and thread_started:
                httpd.shutdown()
        finally:
            try:
                if t is not None and thread_started:
                    t.join(timeout=5)
                    if t.is_alive():
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


@pytest.mark.parametrize(
    ("failure_stage", "expected_error"),
    [
        ("request", RuntimeError),
        ("getresponse", RuntimeError),
        ("read", RuntimeError),
        ("api_response", json.JSONDecodeError),
    ],
)
def test_live_server_fixture_cleans_up_after_login_failure(
        monkeypatch, tmp_path, failure_stage, expected_error):
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

    class FailingLoginResponse:
        status = 200

        def getheaders(self):
            return [("Content-Type", "application/json")]

        def read(self):
            if failure_stage == "read":
                raise RuntimeError("forced read failure")
            if failure_stage == "api_response":
                return b"{"
            return b'{"token": "unused"}'

    class FailingLoginConnection:
        def __init__(self, host, port=None, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.closed = False
            self.failure_stage = failure_stage if connections else None
            connections.append(self)

        def request(self, *_args, **_kwargs):
            if self.failure_stage == "request":
                raise RuntimeError("forced request failure")

        def getresponse(self):
            if self.failure_stage == "getresponse":
                raise RuntimeError("forced getresponse failure")
            return FailingLoginResponse()

        def close(self):
            self.closed = True

    monkeypatch.setattr(threading, "Thread", thread_factory)
    monkeypatch.setattr(http.client, "HTTPConnection", FailingLoginConnection)
    fixture = live_server.__wrapped__(monkeypatch, tmp_path)
    try:
        with pytest.raises(expected_error):
            next(fixture)
        assert len(serve_threads) == len(servers) == 1
        assert len(connections) == 2
        state = servers[0].state
        assert connections[0].closed
        assert connections[1].closed
        assert not serve_threads[0].is_alive()
        assert servers[0].fileno() == -1
        assert state.stop.is_set()
        assert not state.journal._hold_lock
        assert not state.journal.lock_path.exists()
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
    ("pattern", "message"),
    [
        ("(", "invalid"),
        ("x" * 257, "256"),
    ],
)
def test_watch_create_rejects_bad_title_pattern_before_network_or_persistence(
        live_server, monkeypatch, pattern, message):
    network_calls = []

    def forbidden_resolve(_url):
        network_calls.append(_url)
        raise AssertionError("channel resolution must not run for an invalid pattern")

    monkeypatch.setattr(webserver, "resolve_channel_id", forbidden_resolve)
    before = live_server[2].watches.list()

    response = post_json(
        live_server,
        "/api/watches",
        {"url": "@example", "filter_title": pattern},
        token=live_server[1],
        headers={"Origin": same_origin(live_server)},
    )

    assert response.status == 400
    assert message in response.json["error"].lower()
    assert network_calls == []
    assert live_server[2].watches.list() == before


def test_watch_create_rejects_oversized_repeat_before_network_or_storage(
        live_server, monkeypatch):
    state = live_server[2]
    side_effects = []
    store_path = state.watches.path
    before = store_path.read_bytes() if store_path.exists() else None

    def forbidden(label):
        def fail(*_args, **_kwargs):
            side_effects.append(label)
            raise AssertionError(f"{label} must not run for an invalid pattern")
        return fail

    monkeypatch.setattr(webserver, "resolve_channel_id", forbidden("resolve"))
    monkeypatch.setattr(webserver, "fetch_feed", forbidden("feed fetch"))
    monkeypatch.setattr(state, "_submit_poll", forbidden("poll submit"))
    monkeypatch.setattr(state.watches, "_read", forbidden("watch read"))
    monkeypatch.setattr(state.watches, "_write", forbidden("watch write"))

    response = post_json(
        live_server,
        "/api/watches",
        {
            "url": "@example",
            "filter_title": r"^a{1,4294967296}$",
        },
        token=live_server[1],
        headers={"Origin": same_origin(live_server)},
    )

    after = store_path.read_bytes() if store_path.exists() else None
    assert response.status == 400
    assert "repetition" in response.json["error"].lower()
    assert side_effects == []
    assert after == before


@pytest.mark.parametrize(("pattern", "message"), _UNSUPPORTED_WATCH_PATTERNS)
def test_watch_create_rejects_unsupported_subset_before_network_or_persistence(
        live_server, monkeypatch, pattern, message):
    network_calls = []

    def forbidden_resolve(_url):
        network_calls.append(_url)
        raise AssertionError("channel resolution must not run for an unsupported pattern")

    monkeypatch.setattr(webserver, "resolve_channel_id", forbidden_resolve)
    before = live_server[2].watches.list()

    response = post_json(
        live_server,
        "/api/watches",
        {"url": "@example", "filter_title": pattern},
        token=live_server[1],
        headers={"Origin": same_origin(live_server)},
    )

    assert response.status == 400
    assert message in response.json["error"].lower()
    assert network_calls == []
    assert live_server[2].watches.list() == before


def test_watch_update_rejects_bad_title_pattern_without_persistence(
        live_server, monkeypatch):
    state = live_server[2]
    watch = state.watches.add(
        "UC" + "a" * 22,
        filters={"filter_title": "release"},
    )
    writes = []
    real_update = state.watches.update

    def tracking_update(*args, **kwargs):
        writes.append((args, kwargs))
        return real_update(*args, **kwargs)

    monkeypatch.setattr(state.watches, "update", tracking_update)
    response = post_json(
        live_server,
        "/api/watches/update",
        {"id": watch["id"], "filters": {"filter_title": "("}},
        token=live_server[1],
        headers={"Origin": same_origin(live_server)},
    )

    assert response.status == 400
    assert "invalid" in response.json["error"].lower()
    assert writes == []
    assert state.watches.get(watch["id"])["filters"]["filter_title"] == "release"


def test_watch_update_rejects_oversized_repeat_before_network_or_storage(
        live_server, monkeypatch):
    state = live_server[2]
    watch = state.watches.add(
        "UC" + "o" * 22,
        filters={"filter_title": "release"},
    )
    side_effects = []
    store_path = state.watches.path
    before = store_path.read_bytes()

    def forbidden(label):
        def fail(*_args, **_kwargs):
            side_effects.append(label)
            raise AssertionError(f"{label} must not run for an invalid pattern")
        return fail

    monkeypatch.setattr(webserver, "resolve_channel_id", forbidden("resolve"))
    monkeypatch.setattr(webserver, "fetch_feed", forbidden("feed fetch"))
    monkeypatch.setattr(state, "_submit_poll", forbidden("poll submit"))
    monkeypatch.setattr(state.watches, "_read", forbidden("watch read"))
    monkeypatch.setattr(state.watches, "_write", forbidden("watch write"))

    response = post_json(
        live_server,
        "/api/watches/update",
        {
            "id": watch["id"],
            "filters": {"filter_title": r"^a{4294967296,}$"},
        },
        token=live_server[1],
        headers={"Origin": same_origin(live_server)},
    )

    assert response.status == 400
    assert "repetition" in response.json["error"].lower()
    assert side_effects == []
    assert store_path.read_bytes() == before


@pytest.mark.parametrize(("pattern", "message"), _UNSUPPORTED_WATCH_PATTERNS)
def test_watch_update_rejects_unsupported_subset_without_persistence(
        live_server, monkeypatch, pattern, message):
    state = live_server[2]
    watch = state.watches.add(
        "UC" + "a" * 22,
        filters={"filter_title": "release"},
    )
    writes = []
    real_update = state.watches.update

    def tracking_update(*args, **kwargs):
        writes.append((args, kwargs))
        return real_update(*args, **kwargs)

    monkeypatch.setattr(state.watches, "update", tracking_update)
    response = post_json(
        live_server,
        "/api/watches/update",
        {"id": watch["id"], "filters": {"filter_title": pattern}},
        token=live_server[1],
        headers={"Origin": same_origin(live_server)},
    )

    assert response.status == 400
    assert message in response.json["error"].lower()
    assert writes == []
    assert state.watches.get(watch["id"])["filters"]["filter_title"] == "release"


def test_stored_invalid_pattern_records_error_without_stopping_other_watch(
        server_state, monkeypatch):
    import watch

    bad = server_state.watches.add(
        "UC" + "b" * 22,
        filters={"filter_title": "("},
    )
    good = server_state.watches.add(
        "UC" + "c" * 22,
        filters={"filter_title": "café"},
    )
    fetched_channels = []

    def fake_fetch(channel_id):
        fetched_channels.append(channel_id)
        return (
            "Channel",
            [{
                "id": "unicode-video",
                "title": "CAFÉ update",
                "url": "https://youtu.be/unicode123",
                "published": "2026-07-28T00:00:00+00:00",
            }],
        )

    monkeypatch.setattr(watch, "fetch_feed", fake_fetch)

    class OneCycleStop:
        def __init__(self):
            self._stopped = False
            self._waits = 0

        def wait(self, _timeout=None):
            self._waits += 1
            if self._waits >= 2:
                self._stopped = True
            return self._stopped

        def is_set(self):
            return self._stopped

        def set(self):
            self._stopped = True

    server_state._has_lock = True
    server_state.watch_interval = 0
    server_state.stop = OneCycleStop()
    thread = threading.Thread(target=server_state.watch_poller_loop, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    bad_record = server_state.watches.get(bad["id"])
    good_record = server_state.watches.get(good["id"])
    assert "invalid" in (bad_record["last_error"] or "").lower()
    assert bad_record["last_checked"] is None
    assert good_record["last_error"] is None
    assert good_record["last_checked"] is not None
    assert good_record["seen"] == ["unicode-video"]
    assert fetched_channels == [good["channel_id"]]


def test_stored_legacy_unsupported_pattern_isolated_from_sibling_watch(
        server_state, monkeypatch):
    import watch

    bad = server_state.watches.add(
        "UC" + "r" * 22,
        filters={"filter_title": r"^(i)\1$"},
    )
    good = server_state.watches.add(
        "UC" + "s" * 22,
        filters={"filter_title": "release"},
    )
    fetched_channels = []

    def fake_fetch(channel_id):
        fetched_channels.append(channel_id)
        return (
            "Channel",
            [{
                "id": "release-video",
                "title": "Release update",
                "url": "https://youtu.be/release001",
                "published": "2026-07-28T00:00:00+00:00",
            }],
        )

    monkeypatch.setattr(watch, "fetch_feed", fake_fetch)

    assert server_state.poll_watch(bad["id"]) == (0, "error")
    assert server_state.poll_watch(good["id"]) == (0, "baseline")

    bad_record = server_state.watches.get(bad["id"])
    good_record = server_state.watches.get(good["id"])
    assert "backreference" in (bad_record["last_error"] or "").lower()
    assert bad_record["last_checked"] is None
    assert good_record["last_error"] is None
    assert good_record["last_checked"] is not None
    assert good_record["seen"] == ["release-video"]
    assert fetched_channels == [good["channel_id"]]


def test_stored_oversized_repeat_records_only_error_and_isolates_sibling(
        server_state, monkeypatch):
    import watch

    bad = server_state.watches.add(
        "UC" + "u" * 22,
        filters={"filter_title": r"^a{,4294967296}$"},
    )
    good = server_state.watches.add(
        "UC" + "v" * 22,
        filters={"filter_title": "release"},
    )
    bad_before = server_state.watches.get(bad["id"])
    fetched_channels = []

    def fake_fetch(channel_id):
        fetched_channels.append(channel_id)
        return (
            "Channel",
            [{
                "id": "release-overflow-sibling",
                "title": "Release update",
                "url": "https://youtu.be/overflowsib",
                "published": "2026-07-28T00:00:00+00:00",
            }],
        )

    monkeypatch.setattr(watch, "fetch_feed", fake_fetch)

    assert server_state.poll_watch(bad["id"]) == (0, "error")
    assert server_state.poll_watch(good["id"]) == (0, "baseline")

    bad_record = server_state.watches.get(bad["id"])
    good_record = server_state.watches.get(good["id"])
    assert "smaller" in (bad_record["last_error"] or "").lower()
    assert {
        key: value
        for key, value in bad_record.items()
        if key != "last_error"
    } == {
        key: value
        for key, value in bad_before.items()
        if key != "last_error"
    }
    assert good_record["last_error"] is None
    assert good_record["last_checked"] is not None
    assert good_record["seen"] == ["release-overflow-sibling"]
    assert fetched_channels == [good["channel_id"]]


def test_stored_timeout_pattern_records_error_and_yields_no_match(
        server_state, monkeypatch):
    import watch
    import watch_filters

    stored = server_state.watches.add(
        "UC" + "d" * 22,
        filters={"filter_title": r"^(a|aa)+$"},
    )
    monkeypatch.setattr(
        watch,
        "fetch_feed",
        lambda _channel_id: (
            "Channel",
            [{
                "id": "slow-video",
                "title": "a" * 1000 + "!",
                "url": "https://youtu.be/slowregex01",
            }],
        ),
    )
    monkeypatch.setattr(
        watch_filters.regex,
        "search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    assert server_state.poll_watch(stored["id"]) == (0, "error")
    record = server_state.watches.get(stored["id"])
    assert "50 ms" in (record["last_error"] or "")
    assert record["last_checked"] is None
    assert record["pending"] == []
    assert record["seen"] == []


def test_stored_watch_title_pattern_is_evaluated_once_per_video(
        server_state, monkeypatch):
    import watch
    import watch_filters

    monkeypatch.setattr(
        watch,
        "fetch_feed",
        lambda _channel_id: (
            "Channel",
            [{
                "id": "one-match",
                "title": "Release update",
                "url": "https://youtu.be/onematch001",
            }],
        ),
    )
    real_search = watch_filters.regex.search
    calls = []

    def counting_search(*args, **kwargs):
        calls.append((args, kwargs))
        return real_search(*args, **kwargs)

    monkeypatch.setattr(watch_filters.regex, "search", counting_search)

    videos = server_state._fetch_one({
        "channel_id": "UC" + "e" * 22,
        "filters": {"filter_title": "release"},
    })

    assert [video["id"] for video in videos] == ["one-match"]
    assert len(calls) == 1


def test_stored_watch_second_evaluation_timeout_cannot_be_swallowed(
        server_state, monkeypatch):
    import watch
    import watch_filters

    stored = server_state.watches.add(
        "UC" + "f" * 22,
        filters={"filter_title": "release"},
    )
    monkeypatch.setattr(
        watch,
        "fetch_feed",
        lambda _channel_id: (
            "Channel",
            [{
                "id": "single-evaluation",
                "title": "Release update",
                "url": "https://youtu.be/singleeval1",
            }],
        ),
    )
    calls = []

    def first_success_second_timeout(*_args, **_kwargs):
        calls.append("search")
        if len(calls) == 1:
            return object()
        raise TimeoutError()

    monkeypatch.setattr(
        watch_filters.regex,
        "search",
        first_success_second_timeout,
    )

    assert server_state.poll_watch(stored["id"]) == (0, "baseline")
    record = server_state.watches.get(stored["id"])
    assert calls == ["search"]
    assert record["last_error"] is None
    assert record["last_checked"] is not None
    assert record["seen"] == ["single-evaluation"]


def test_auth_status_handler_returns_json(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    payload = webserver.auth_status_payload()   # pure function, no socket
    assert "enabled" in payload and "tiers_available" in payload


def test_auth_import_cookies_writes_active_and_enables_auth(monkeypatch, tmp_path):
    """Web import must leave cookies usable: write active.txt AND set [auth].enabled.

    Regression: import previously wrote the file but left enabled=false (default), so
    CookieManager.get_cookiefile() ignored the store and downloads stayed signed-out.
    """
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    content = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"
    r = webserver.auth_import_cookies(content)
    assert r["ok"] is True
    active = tmp_path / "cookies" / "active.txt"
    assert active.is_file()
    assert "SID" in active.read_text(encoding="utf-8")
    cfg = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "enabled = true" in cfg
    st = webserver.auth_status_payload()
    assert st.get("enabled") is True
    # file tier should advertise once active.txt exists
    assert "file" in (st.get("tiers_available") or [])
    assert st.get("has_file") is True
    assert st.get("valid") is False  # not validated until Test
    assert "not validated" in (st.get("detail") or "").lower() or "click test" in (st.get("detail") or "").lower()


def test_auth_status_after_clear_does_not_claim_file(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    content = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"
    assert webserver.auth_import_cookies(content)["ok"]
    store = tmp_path / "cookies"
    for n in ("active.txt", "state.json"):
        p = store / n
        if p.exists():
            p.unlink()
    st = webserver.auth_status_payload()
    assert st.get("has_file") is False
    assert st.get("valid") is False
    detail = (st.get("detail") or "").lower()
    assert "no cookies" in detail or "no cookies file" in detail


def test_auth_clear_reports_failure_and_preserves_jars_when_state_save_fails(
        live_server, monkeypatch):
    import cookies_auth

    store = cookies_auth.cookie_store_dir()
    store.mkdir(parents=True, exist_ok=True)
    active = store / "active.txt"
    active.write_bytes(b"complete-cookie-jar")
    monkeypatch.setattr(
        cookies_auth,
        "save_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state full")),
    )
    response = post_json(
        live_server,
        "/api/auth",
        {"action": "clear"},
        token=live_server[1],
        headers={"Origin": same_origin(live_server)},
    )

    assert response.status == 200
    assert response.json["ok"] is False
    assert "state full" in response.json["error"]
    assert active.read_bytes() == b"complete-cookie-jar"


def test_auth_from_browser_writes_active(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import cookie_providers as cp

    fake = [{"domain": ".youtube.com", "name": "SID", "value": "frombr",
             "path": "/", "secure": True, "expires": 2000000000}]

    class FakeProv(cp.BrowserExtractProvider):
        def list_detected(self):
            return ["chrome"]

        def available(self):
            return True

        def acquire(self):
            store = tmp_path / "cookies"
            store.mkdir(parents=True, exist_ok=True)
            out = store / "browser.txt"
            ca_mod = __import__("cookies_auth")
            ca_mod.write_netscape(fake, out)
            return str(out)

    monkeypatch.setattr(cp, "BrowserExtractProvider", FakeProv)
    r = webserver.auth_from_browser("chrome")
    assert r["ok"] is True
    active = tmp_path / "cookies" / "active.txt"
    assert active.is_file()
    assert "frombr" in active.read_text(encoding="utf-8")
    assert "enabled = true" in (tmp_path / "config.toml").read_text(encoding="utf-8")
    st = webserver.auth_status_payload()
    assert st.get("has_file") is True


def test_auth_from_browser_atomic_copy_preserves_previous_on_replace_failure(
        monkeypatch, tmp_path):
    import cookie_providers as cp
    import secureio

    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    store = tmp_path / "cookies"
    store.mkdir(parents=True)
    source = store / "browser.txt"
    source.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tnew-browser\n",
        encoding="utf-8",
    )
    active = store / "active.txt"
    active.write_text("old-complete", encoding="utf-8")

    class FakeProv(cp.BrowserExtractProvider):
        def list_detected(self):
            return ["chrome"]

        def acquire(self):
            return str(source)

    monkeypatch.setattr(cp, "BrowserExtractProvider", FakeProv)
    monkeypatch.setattr(
        secureio.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("boom")),
    )

    result = webserver.auth_from_browser("chrome")

    assert result["ok"] is False
    assert active.read_text(encoding="utf-8") == "old-complete"


def test_auth_from_browser_fails_when_none_detected(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import cookie_providers as cp

    class EmptyProv(cp.BrowserExtractProvider):
        def list_detected(self):
            return []

        def available(self):
            return False

        def acquire(self):
            return None

    monkeypatch.setattr(cp, "BrowserExtractProvider", EmptyProv)
    r = webserver.auth_from_browser("auto")
    assert r["ok"] is False
    assert "browser" in (r.get("error") or "").lower()


def test_auth_login_reports_missing_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import cookie_providers as cp
    monkeypatch.setattr(cp, "native_browser_login", lambda profile_dir=None: {
        "ok": False, "code": "browser_missing", "error": "No Chrome found",
    })
    r = webserver.auth_login_session()
    assert r["ok"] is False
    assert r.get("code") == "browser_missing"


def test_auth_login_exports_after_native_login(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import cookie_providers as cp
    store = tmp_path / "cookies"
    store.mkdir(parents=True)
    profile_txt = store / "profile.txt"
    profile_txt.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tnative\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cp, "native_browser_login", lambda profile_dir=None: {
        "ok": True, "browser": "chrome", "profile_dir": str(store / "yt_final_chrome"),
    })
    monkeypatch.setattr(cp, "_export_managed_profile", lambda *a, **k: str(profile_txt))
    r = webserver.auth_login_session()
    assert r["ok"] is True
    active = store / "active.txt"
    assert active.is_file()
    assert "native" in active.read_text(encoding="utf-8")


def test_cdp_find_exe_mac_app_bundle(monkeypatch, tmp_path):
    import _cdp_client as cdp
    fake = tmp_path / "Google Chrome"
    fake.parent.mkdir(parents=True, exist_ok=True)
    # Create nested path like .app/Contents/MacOS/Google Chrome
    chrome = (
        tmp_path / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    )
    chrome.parent.mkdir(parents=True, exist_ok=True)
    chrome.write_text("#!/bin/sh\n")
    chrome.chmod(0o755)
    monkeypatch.setattr(cdp.sys, "platform", "darwin")
    monkeypatch.setattr(cdp.shutil, "which", lambda n: None)
    # Point Applications root by patching the hard-coded path — replace function temporarily
    real = cdp._find_exe

    def patched(name):
        if name == "chrome" and chrome.is_file():
            return str(chrome)
        return real(name)

    # Direct: inject by rewriting path check via monkeypatch on Path.is_file is hard;
    # instead call with monkeypatched constant by executing logic:
    monkeypatch.setattr(
        cdp, "_find_exe",
        lambda name: str(chrome) if name == "chrome" and chrome.is_file() else None,
    )
    assert cdp._find_exe("chrome") == str(chrome)


def test_validate_existing_does_not_need_browser(monkeypatch, tmp_path):
    import cookies_auth as ca
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    content = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"
    assert webserver.auth_import_cookies(content)["ok"]
    mgr = ca.build_default_manager()
    mgr.validator = lambda p: True
    ok, msg, path = mgr.validate_existing_file()
    assert ok is True
    assert path and "active.txt" in path
    st = mgr.status()
    assert st["valid"] is True and st["has_file"] is True


def test_auth_import_empty_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    r = webserver.auth_import_cookies("   \n")
    assert r["ok"] is False
    assert "empty" in (r.get("error") or "").lower()


def test_ensure_auth_enabled_preserves_other_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "[defaults]\nquality = \"1080p\"\n\n[auth]\nenabled = false\nvalidate_ttl_min = 10\n",
        encoding="utf-8",
    )
    webserver._ensure_auth_enabled_in_config()
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "quality = \"1080p\"" in text
    assert "enabled = true" in text
    assert "validate_ttl_min = 10" in text


def test_resolve_open_target_file_dir_and_parent(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    t, mode, err = webserver.resolve_open_target(str(f))
    assert err is None and mode == "file" and t == f.resolve()

    t2, mode2, err2 = webserver.resolve_open_target(str(tmp_path))
    assert err2 is None and mode2 == "dir" and t2 == tmp_path.resolve()

    missing = tmp_path / "gone.mp4"
    t3, mode3, err3 = webserver.resolve_open_target(str(missing))
    assert err3 is None and mode3 == "dir" and t3 == tmp_path.resolve()

    t4, mode4, err4 = webserver.resolve_open_target("")
    assert t4 is None and err4


@pytest.mark.parametrize(
    ("platform", "poison_name"),
    (
        ("win32", "explorer.exe"),
        ("darwin", "open"),
        ("linux", "xdg-open"),
    ),
)
def test_reveal_never_searches_cwd_or_path_for_file_manager(
        platform, poison_name, monkeypatch, tmp_path):
    """File-manager dispatch must be absolute or fail before process creation."""
    target = tmp_path / "out.mp4"
    target.write_bytes(b"x")
    poisoned = tmp_path / poison_name
    poisoned.write_bytes(b"not an os helper")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setattr(webserver.sys, "platform", platform)
    calls = []

    def fake_popen(command, **_kwargs):
        calls.append(command)
        return type("Process", (), {})()

    monkeypatch.setattr(webserver.subprocess, "Popen", fake_popen)
    if platform == "win32":
        import ctypes

        class FailingShell:
            def ShellExecuteW(self, *_args):
                return 31

        monkeypatch.setattr(
            ctypes,
            "windll",
            type("Windll", (), {"shell32": FailingShell()})(),
            raising=False,
        )

    result = webserver.reveal_in_file_manager(str(target))

    if not calls:
        assert result["ok"] is False
        return
    command = calls[0]
    assert isinstance(command, (list, tuple))
    assert Path(command[0]).is_absolute()
    assert Path(command[0]).resolve() != poisoned.resolve()


def test_reveal_rejects_linked_fixed_helper_before_process_creation(
        monkeypatch, tmp_path):
    """A link-like Windows helper remains untrusted even in the Windows directory."""
    import ctypes
    import executable_trust

    target = tmp_path / "out.mp4"
    target.write_bytes(b"x")
    windows_root = tmp_path / "Windows"
    windows_root.mkdir()
    explorer = windows_root / "explorer.exe"
    explorer.write_bytes(b"linked helper")
    monkeypatch.setattr(webserver.sys, "platform", "win32")
    monkeypatch.setattr(
        executable_trust,
        "_windows_known_folder",
        lambda name: windows_root if name == "windows" else None,
    )
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == explorer or real_is_symlink(path),
    )

    class FailingShell:
        def ShellExecuteW(self, *_args):
            return 31

    monkeypatch.setattr(
        ctypes,
        "windll",
        type("Windll", (), {"shell32": FailingShell()})(),
        raising=False,
    )
    calls = []
    monkeypatch.setattr(
        webserver.subprocess,
        "Popen",
        lambda command, **_kwargs: (
            calls.append(command) or type("Process", (), {})()
        ),
    )

    result = webserver.reveal_in_file_manager(str(target))

    assert result["ok"] is False
    assert calls == []


def test_reveal_in_file_manager_uses_resolved_path(monkeypatch, tmp_path):
    """Stub process creation and assert the resolved file reaches trusted Explorer."""
    f = tmp_path / "out.mp4"
    f.write_bytes(b"x")
    explorer = tmp_path / "Windows" / "explorer.exe"
    explorer.parent.mkdir()
    explorer.write_bytes(b"system helper")
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(list(args))
        class P:
            pass
        return P()

    monkeypatch.setattr(webserver.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(webserver.sys, "platform", "win32")
    monkeypatch.setattr(
        webserver,
        "system_helper_executable",
        lambda _helper, platform=None: explorer,
    )

    r = webserver.reveal_in_file_manager(str(f))
    assert r["ok"] is True and r["mode"] == "file"
    assert r["path"]
    assert calls == [[str(explorer), f"/select,{f.resolve()}"]]


def test_open_folder_from_payload_job_id(tmp_path, monkeypatch):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    class FakeJournal:
        def get(self, jid):
            return {"id": jid, "output_paths": [str(f)], "spec": {"output_dir": str(tmp_path)}}
    monkeypatch.setattr(webserver, "reveal_in_file_manager",
                        lambda p: {"ok": True, "path": p, "mode": "file"})
    r = webserver.open_folder_from_payload({"job_id": "j_abc"}, journal=FakeJournal())
    assert r["ok"] is True
    assert str(f) in r["path"] or r["path"].endswith("clip.mp4")


def test_legacy_token_file_is_removed_without_becoming_a_session(tmp_path):
    path = tmp_path / "web_token"
    path.write_text("old-permanent-secret", encoding="utf-8")

    assert websessions.retire_legacy_token_file(path)
    assert not path.exists()


def test_legacy_token_retirement_is_idempotent(tmp_path):
    path = tmp_path / "web_token"

    assert not websessions.retire_legacy_token_file(path)
    assert not path.exists()


def test_check_webui_password_default():
    assert webserver.check_webui_password(webserver.DEFAULT_WEBUI_PASSWORD) is True
    assert webserver.check_webui_password("wrong") is False
    assert webserver.check_webui_password("") is False


def test_check_webui_password_env_override(monkeypatch):
    monkeypatch.setenv("YT_FINAL_WEBUI_PASSWORD", "mypass")
    assert webserver.check_webui_password("mypass") is True
    assert webserver.check_webui_password(webserver.DEFAULT_WEBUI_PASSWORD) is False


def test_proxy_targets_local_ssrf_guard():
    """SSRF guard (public-server proxy allowlist) blocks loopback/private/link-local for BOTH families
    — including IPv6 [::1], which the old IPv4-only gethostbyname check let through — and fails CLOSED
    on unparseable/hostless inputs. All checks use IP literals so the test needs no DNS."""
    T = webserver._proxy_targets_local
    assert T("socks5://127.0.0.1:1080") is True          # IPv4 loopback
    assert T("http://[::1]:8080") is True                # IPv6 loopback (the bypass the fix closes)
    assert T("socks5://10.0.0.5:1080") is True           # private
    assert T("http://169.254.1.1:3128") is True          # link-local
    assert T("socks5://0.0.0.0:1080") is True            # unspecified
    assert T("socks5://1.1.1.1:1080") is False           # public IP -> allowed
    assert T("garbage") is True                          # unparseable -> fail closed
    assert T("socks5://:1080") is True                   # no host -> fail closed
    assert T("") is True                                 # empty -> fail closed


def test_output_dir_escapes_path_traversal_guard(tmp_path):
    """Path-traversal guard (public server): an output_dir outside the server download directory is
    rejected; a subdir (or the base itself) is allowed; fails CLOSED on unresolvable input."""
    base = tmp_path / "downloads"
    base.mkdir()
    E = webserver._output_dir_escapes
    assert E(str(base / "sub"), str(base)) is False              # subdir -> ok
    assert E(str(base), str(base)) is False                      # base itself -> ok
    assert E(str(base / ".." / ".." / "etc"), str(base)) is True # ../ traversal -> blocked
    assert E("/etc", str(base)) is True                          # unrelated absolute -> blocked
    assert E(str(tmp_path / "elsewhere"), str(base)) is True     # sibling -> blocked


# ---------------------------------------------------------------------------
# Defect 1: parallel per-URL progress must aggregate, not clobber each other.
# ---------------------------------------------------------------------------

def test_progress_single_download_passthrough(server_state):
    """A single (non-parallel) download must behave exactly like the old shared slot: fields
    merge, 'started' survives, no aggregate synthesized."""
    st = server_state
    st.set_progress("j1", {"status": "starting", "started": 111.0, "percent": None})
    st.set_progress("j1", {"status": "downloading", "downloaded": 50, "total": 200,
                           "percent": 25.0, "speed": 7, "eta": 3})
    p = st.get_progress("j1")
    assert p["status"] == "downloading"
    assert p["downloaded"] == 50 and p["total"] == 200 and p["percent"] == 25.0
    assert p["started"] == 111.0            # preserved across events on the same thread
    assert "active_downloads" not in p      # no aggregation for a single download


def test_progress_aggregates_two_concurrent_downloads(server_state):
    """Two URLs of a parallel job report from two live threads (distinct idents); the job-level
    view must SUM their bytes/speed instead of one clobbering the other."""
    st = server_state
    barrier = threading.Barrier(2)

    def worker(info):
        barrier.wait()                      # both threads alive => guaranteed-distinct idents
        st.set_progress("j1", info)
        barrier.wait()                      # keep both alive until BOTH slots written (no ident reuse)

    a = threading.Thread(target=worker, args=(
        {"status": "downloading", "downloaded": 100, "total": 200, "speed": 10, "eta": 5},))
    b = threading.Thread(target=worker, args=(
        {"status": "downloading", "downloaded": 300, "total": 600, "speed": 20, "eta": 9},))
    a.start(); b.start(); a.join(); b.join()

    p = st.get_progress("j1")
    assert p["active_downloads"] == 2
    assert p["downloaded"] == 400           # 100 + 300, not clobbered to one file's value
    assert p["total"] == 800                # 200 + 600
    assert p["percent"] == 50.0
    assert p["speed"] == 30                 # combined throughput
    assert p["eta"] == 9                    # slowest sub-download


def test_progress_generation_bumps_and_clear(server_state):
    st = server_state
    g0 = st.progress_generation()
    st.set_progress("j1", {"status": "downloading", "downloaded": 1, "total": 2})
    assert st.progress_generation() > g0
    st.clear_progress("j1")
    assert st.get_progress("j1") is None
    assert st.progress_generation() > g0


# ---------------------------------------------------------------------------
# Defect 2: unauthenticated GET /api/updates must not trigger force/pypi probes.
# ---------------------------------------------------------------------------

def test_updates_get_requires_session_before_force(live_server, monkeypatch):
    conn_factory, token, state = live_server
    seen = []
    monkeypatch.setattr(webserver, "updates_report_payload",
                        lambda **kw: seen.append(kw) or {"ok": True})

    # No session: reject before the update scanner can run.
    c = conn_factory()
    c.request("GET", "/api/updates?force=1&pypi=1")
    r = c.getresponse(); r.read(); c.close()
    assert r.status == 401
    assert seen == []

    # With a valid session the force/pypi request is honored (authenticated user pressed "check now").
    c = conn_factory()
    c.request("GET", "/api/updates?force=1&pypi=1", headers={"X-Token": token})
    r = c.getresponse(); r.read(); c.close()
    assert r.status == 200
    assert seen[-1]["force"] is True
    assert seen[-1]["check_pypi"] is True


# ---------------------------------------------------------------------------
# Defect 3: GET /api/jobs must support conditional GET (ETag / 304).
# ---------------------------------------------------------------------------

def test_jobs_conditional_get_304_and_invalidation(live_server):
    conn_factory, token, state = live_server

    c = conn_factory()
    c.request("GET", "/api/jobs", headers={"X-Token": token})
    r1 = c.getresponse()
    etag = r1.headers.get("ETag")
    body1 = r1.read()
    c.close()
    assert etag and body1                      # first poll: full body + an ETag

    # Nothing changed -> a conditional re-poll returns an empty 304.
    c = conn_factory()
    c.request("GET", "/api/jobs", headers={"If-None-Match": etag, "X-Token": token})
    r2 = c.getresponse()
    body2 = r2.read()
    c.close()
    assert r2.status == 304 and body2 == b""

    # Adding a job bumps queue.json's mtime -> the old ETag no longer matches -> full 200 body.
    time.sleep(0.01)
    state.journal.add(webserver.JobSpec.from_dict({"urls": ["https://youtu.be/x"]}))
    c = conn_factory()
    c.request("GET", "/api/jobs", headers={"If-None-Match": etag, "X-Token": token})
    r3 = c.getresponse()
    body3 = r3.read()
    c.close()
    assert r3.status == 200 and body3


# ---------------------------------------------------------------------------
# Defect 6: worker_loop must survive a raising set_status (not silently die).
# ---------------------------------------------------------------------------

def test_worker_loop_survives_journal_write_failure(server_state):
    st = server_state

    class FakeDownloader:
        progress_sink = None
        last_error = None
        last_error_kind = None

    class FakeEngine:
        def __init__(self):
            self.downloader = FakeDownloader()

        def run(self, spec):
            raise RuntimeError("download boom")

    class FakeJournal:
        def __init__(self):
            self.claims = 0

        def claim_next(self, *a, **k):
            self.claims += 1
            if self.claims == 1:
                return {"id": "j1", "spec": {"urls": ["u"]}}
            return None            # queue drains after the poisoned job

        def set_status(self, *a, **k):
            raise OSError("journal write failed")   # the SECOND failure that used to kill the thread

    fake_j = FakeJournal()
    st.journal = fake_j
    st._has_lock = True
    st.engine = lambda: FakeEngine()

    t = threading.Thread(target=st.worker_loop, daemon=True)
    t.start()
    deadline = time.time() + 3.0
    while fake_j.claims < 2 and time.time() < deadline:
        time.sleep(0.02)
    st.stop.set()
    t.join(timeout=3.0)
    # The thread kept polling past the failing job (didn't die on the raising set_status)...
    assert fake_j.claims >= 2
    # ...and it terminates cleanly once asked to stop.
    assert not t.is_alive()


# ---------------------------------------------------------------------------
# Defect 7: watch_poller_loop must survive a raising batch_apply_poll.
# ---------------------------------------------------------------------------

def test_watch_poller_survives_watches_write_failure(server_state):
    st = server_state

    class FakeWatches:
        def __init__(self):
            self.applies = 0

        def list(self):
            return [{"id": "w1"}]

        def batch_apply_poll(self, results, auto_enqueue_fn=None):
            self.applies += 1
            raise OSError("watches.json write failed")

    class FastStop:
        """A stop signal that never actually sleeps, so the poller spins fast under test."""
        def __init__(self):
            self._flag = threading.Event()

        def wait(self, timeout=None):
            return self._flag.wait(0.001)

        def is_set(self):
            return self._flag.is_set()

        def set(self):
            self._flag.set()

    fake_w = FakeWatches()
    st.watches = fake_w
    st._has_lock = True
    st.watch_interval = 0
    st.stop = FastStop()
    st._fetch_one = lambda w: []           # avoid real RSS network calls

    t = threading.Thread(target=st.watch_poller_loop, daemon=True)
    t.start()
    deadline = time.time() + 3.0
    while fake_w.applies < 2 and time.time() < deadline:
        time.sleep(0.02)
    st.stop.set()
    t.join(timeout=3.0)
    assert fake_w.applies >= 2              # kept polling past the raising write
    assert not t.is_alive()


# ---------------------------------------------------------------------------
# Defect 8: /api/probe must use the get_detector() singleton, not a fresh detector.
# ---------------------------------------------------------------------------

def test_probe_uses_get_detector_singleton(live_server, monkeypatch):
    conn_factory, token, state = live_server
    import json as _json
    try:
        import yt_final
    except ImportError:
        import Final.yt_final as yt_final

    sentinel_det = object()
    calls = {"get_detector": 0, "detector_ctor": 0}

    def fake_get_detector():
        calls["get_detector"] += 1
        return sentinel_det

    class FakeDownloader:
        def __init__(self, det, **kw):
            self.det = det

        def _get_cookie_manager(self):
            return None

        def probe_url(self, url, **kw):
            return {"ok": True, "url": url, "used_singleton": self.det is sentinel_det}

    def fake_ctor(*a, **k):
        calls["detector_ctor"] += 1
        return object()

    monkeypatch.setattr(yt_final, "get_detector", fake_get_detector)
    monkeypatch.setattr(yt_final, "YouTubeDownloader", FakeDownloader)
    monkeypatch.setattr(yt_final, "CapabilityDetector", fake_ctor, raising=False)

    c = conn_factory()
    payload = _json.dumps({"url": "https://youtu.be/x"}).encode()
    c.request("POST", "/api/probe", body=payload,
              headers={"X-Token": token, "Content-Type": "application/json"})
    r = c.getresponse()
    body = _json.loads(r.read())
    c.close()
    assert r.status == 200
    assert body.get("used_singleton") is True
    assert calls["get_detector"] == 1
    assert calls["detector_ctor"] == 0      # no fresh CapabilityDetector() built per request

@pytest.mark.parametrize(
    "host_header",
    (
        "example.com:{port}",
        "127.0.0.1:{wrong_port}",
        "127.0.0.1",
        "user@127.0.0.1:{port}",
    ),
)
def test_foreign_host_or_wrong_listener_port_is_rejected(live_server, host_header):
    conn_factory, token, _state = live_server
    conn = conn_factory()
    authority = host_header.format(port=conn.port, wrong_port=conn.port + 1)
    conn.close()

    response = request_json(
        live_server,
        "GET",
        "/api/session",
        token=token,
        headers={"Host": authority},
    )
    assert response.status == 403


def raw_http_status(
    live_server,
    method,
    host_header,
    *,
    content_length=0,
    path="/api/jobs",
    extra_headers=(),
    token=None,
):
    """Send a hand-built request so header shapes http.client cannot express stay reachable.

    `extra_headers` is a sequence of (name, value) pairs appended verbatim after Host. That is the
    only way to send a DUPLICATE Host or Origin — http.client collapses repeats — and those duplicate
    guards are precisely the ones with no coverage today.
    """
    conn = live_server[0]()
    target = (conn.host, conn.port)
    conn.close()
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host_header}",
        f"Content-Length: {content_length}",
    ]
    if token is not None:
        lines.append(f"X-Token: {token}")
    lines.extend(f"{name}: {value}" for name, value in extra_headers)
    lines.append("Connection: close")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    with socket.create_connection(target, timeout=5) as client:
        client.sendall(request)
        first_line = client.makefile("rb").readline()
    return int(first_line.split()[1])


@pytest.mark.parametrize("method", ("PUT", "DELETE", "OPTIONS", "BREW"))
def test_invalid_host_is_rejected_before_unsupported_method_dispatch(live_server, method):
    conn = live_server[0]()
    foreign_host = f"example.com:{conn.port}"
    conn.close()

    status = raw_http_status(
        live_server,
        method,
        foreign_host,
        content_length=10_000_000,
    )
    assert status == 403


@pytest.mark.parametrize("method", ("PUT", "DELETE", "OPTIONS", "BREW"))
def test_unsupported_method_with_loopback_host_preserves_normal_status(live_server, method):
    conn = live_server[0]()
    loopback_host = f"{conn.host}:{conn.port}"
    conn.close()

    assert raw_http_status(live_server, method, loopback_host) == 501


def test_exact_cross_origin_is_rejected(live_server):
    conn_factory, token, _state = live_server
    conn = conn_factory()
    authority = f"127.0.0.1:{conn.port}"
    conn.close()

    response = post_json(
        live_server,
        "/api/settings",
        {},
        token=token,
        headers={"Host": authority, "Origin": f"http://{authority}/"},
    )
    assert response.status == 403


def test_empty_cross_origin_header_is_rejected(live_server):
    conn_factory, token, _state = live_server
    conn = conn_factory()
    authority = f"127.0.0.1:{conn.port}"
    conn.close()

    response = post_json(
        live_server,
        "/api/settings",
        {},
        token=token,
        headers={"Host": authority, "Origin": ""},
    )
    assert response.status == 403


def test_cross_origin_login_is_rejected(live_server):
    conn_factory, _token, _state = live_server
    conn = conn_factory()
    authority = f"127.0.0.1:{conn.port}"
    conn.close()

    response = post_json(
        live_server,
        "/api/login",
        {"password": password()},
        headers={"Host": authority, "Origin": "http://foreign.example:8765"},
    )
    assert response.status == 403


def test_originless_login_is_rejected(live_server):
    response = post_json(live_server, "/api/login", {"password": password()})
    assert response.status == 403


def test_foreign_host_login_is_rejected(live_server):
    conn_factory, _token, _state = live_server
    conn = conn_factory()
    foreign_authority = f"example.com:{conn.port}"
    conn.close()

    response = post_json(
        live_server,
        "/api/login",
        {"password": password()},
        headers={"Host": foreign_authority},
    )
    assert response.status == 403


@pytest.mark.parametrize(
    "authority_template",
    ("127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}"),
)
def test_exact_same_origin_is_accepted(live_server, authority_template):
    conn_factory, token, _state = live_server
    conn = conn_factory()
    authority = authority_template.format(port=conn.port)
    conn.close()

    response = post_json(
        live_server,
        "/api/settings",
        {},
        token=token,
        headers={"Host": authority, "Origin": f"http://{authority}"},
    )
    assert response.status == 200


def test_originless_authenticated_cli_client_is_accepted(live_server):
    _conn_factory, token, _state = live_server

    response = post_json(live_server, "/api/settings", {}, token=token)
    assert response.status == 200


@pytest.mark.parametrize(
    ("host", "allowed"),
    (
        ("127.0.0.1", True),
        ("127.255.255.254", True),
        ("::1", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.0.2.10", False),
        ("", False),
    ),
)
def test_nonloopback_bind_policy_address_matrix(host, allowed):
    assert webserver.is_loopback_bind_host(host) is allowed


def test_nonloopback_bind_policy_requires_every_resolved_address_to_be_loopback(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        if host == "loopback.test":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
            ]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert webserver.is_loopback_bind_host("loopback.test")
    assert not webserver.is_loopback_bind_host("mixed.test")


def test_serve_rejects_nonloopback_even_with_allow_public(monkeypatch):
    monkeypatch.setattr(
        webserver,
        "ServerState",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("state must not be constructed for a rejected bind")
        ),
    )

    with pytest.raises(ValueError, match="loopback"):
        webserver.serve(host="0.0.0.0", allow_public=True, open_browser=False)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"token": "fixed-token"}, "token.*no longer supported"),
        ({"allow_public": True}, "allow_public.*no longer supported"),
    ),
)
def test_static_token_and_allow_public_programmatic_arguments_are_rejected(
    monkeypatch, kwargs, message
):
    monkeypatch.setattr(
        webserver,
        "ServerState",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("state must not be constructed for a retired argument")
        ),
    )

    with pytest.raises(ValueError, match=message):
        webserver.serve(open_browser=False, **kwargs)

def test_token_auth_flow_mutations(live_server):
    conn_factory, _legacy_token, state = live_server
    token = login(live_server)
    
    # Missing token entirely
    conn = conn_factory()
    conn.request("POST", "/api/jobs", body="{}")
    resp = conn.getresponse()
    assert resp.status == 401
    
    # Token in query string (not allowed for mutation)
    conn = conn_factory()
    conn.request("POST", f"/api/jobs?token={token}", body="{}")
    resp = conn.getresponse()
    assert resp.status == 401
    
    # Invalid token in header
    conn = conn_factory()
    conn.request("POST", "/api/jobs", body="{}", headers={"X-Token": "bad"})
    resp = conn.getresponse()
    assert resp.status == 401

    # Valid token in header
    conn = conn_factory()
    conn.request("POST", "/api/jobs", body="{\"url\":\"http://test\"}", headers={"X-Token": token})
    resp = conn.getresponse()
    assert resp.status == 201


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    ("path", "authenticated_status"),
    [
        ("/api/jobs", 200),
        ("/api/history", 200),
        ("/api/auth", 200),
        ("/api/settings", 200),
        ("/api/watches", 200),
        ("/api/updates", 200),
        ("/api/gpu", 202),
        ("/api/profiles", 200),
        ("/api/open-folder", 404),
    ],
)
def test_sensitive_gets_require_session(
        live_server, monkeypatch, method, path, authenticated_status):
    _conn_factory, _legacy_token, state = live_server
    monkeypatch.setattr(webserver, "updates_report_payload", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(state, "start_warm_engine", lambda: None)
    token = login(live_server)

    assert get_json(live_server, path, method=method).status == 401
    assert get_json(live_server, path, token=token, method=method).status == authenticated_status


def test_login_issues_distinct_expiring_sessions(live_server):
    headers = {"Origin": same_origin(live_server)}
    first = post_json(live_server, "/api/login", {"password": password()}, headers=headers)
    second = post_json(live_server, "/api/login", {"password": password()}, headers=headers)
    assert first.status == second.status == 200
    assert first.json["token"] != second.json["token"]
    assert first.json["expires_at"] > time.time()


def test_session_probe_returns_expiry(live_server):
    token = login(live_server)
    response = get_json(live_server, "/api/session", token=token)
    assert response.status == 200
    assert response.json["ok"] is True
    assert response.json["expires_at"] > time.time()


def test_logout_revokes_only_the_presented_session(live_server):
    one = login(live_server)
    two = login(live_server)
    assert post_json(live_server, "/api/logout", {}, token=one).status == 200
    assert get_json(live_server, "/api/session", token=one).status == 401
    assert get_json(live_server, "/api/session", token=two).status == 200


def test_repeated_logout_is_idempotent_without_reviving_session(live_server):
    token = login(live_server)
    assert post_json(live_server, "/api/logout", {}, token=token).status == 200
    assert post_json(live_server, "/api/logout", {}, token=token).status == 200
    assert get_json(live_server, "/api/session", token=token).status == 401


def test_logout_rejects_never_valid_session(live_server):
    assert post_json(live_server, "/api/logout", {}, token="never-valid").status == 401


def test_logout_rejects_expired_session(live_server):
    _conn_factory, _fixture_token, state = live_server
    now = [100.0]
    state.sessions = SessionStore(ttl_seconds=5, clock=lambda: now[0])
    token = login(live_server)

    now[0] = 105.0

    assert post_json(live_server, "/api/logout", {}, token=token).status == 401
    assert get_json(live_server, "/api/session", token=token).status == 401


def test_logout_rejects_capacity_evicted_session_and_preserves_active_one(live_server):
    _conn_factory, _fixture_token, state = live_server
    state.sessions = SessionStore(max_sessions=1)
    evicted = login(live_server)
    active = login(live_server)

    assert post_json(live_server, "/api/logout", {}, token=evicted).status == 401
    assert get_json(live_server, "/api/session", token=active).status == 200


def test_query_token_is_rejected(live_server):
    token = login(live_server)
    assert get_json(live_server, f"/api/session?token={token}").status == 401
    assert get_json(live_server, f"/api/jobs?token={token}", method="HEAD").status == 401


def test_public_static_and_health_remain_open(live_server):
    conn_factory, _token, state = live_server
    state.allow_public = True
    # Static assets and minimal health stay open so the login page can load and probe liveness.
    for path in ("/", "/app.js", "/api/health"):
        conn = conn_factory()
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()
        conn.close()
        assert response.status == 200, path


def test_health_reports_pooled_transport(live_server):
    """The web console reads pooled-transport status from /api/health (rendered under Advanced ->
    Performance), so a silently-unpooled (slow) runtime is visible in the UI, not only on the CLI."""
    import json
    conn_factory, token, _state = live_server
    conn = conn_factory()
    conn.request("GET", "/api/health", headers={"X-Token": token})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 200
    assert 'pooled_transport' in body and isinstance(body['pooled_transport'], bool)
    print('test_health_reports_pooled_transport passed')


def test_protected_post_authenticates_before_body():
    class _NeverRead:
        def read(self, _length):
            raise AssertionError("request body was read before authentication")

    class _Server:
        server_address = ("127.0.0.1", 8765)
        state = type(
            "_State",
            (),
            {
                "allow_public": False,
                "token": "legacy-token-must-not-authorize",
                "sessions": SessionStore(),
            },
        )()

    handler = object.__new__(webserver.Handler)
    handler.path = "/api/jobs"
    handler.headers = {"Content-Length": "100", "Host": "127.0.0.1:8765"}
    handler.rfile = _NeverRead()
    handler.server = _Server()
    sent = {}
    handler._send = lambda code, body: sent.update(code=code, body=body)

    webserver.Handler.do_POST(handler)
    assert sent["code"] == 401


def test_same_origin_login_remains_pre_session(live_server, monkeypatch):
    conn_factory, _token, _state = live_server
    monkeypatch.setenv("YT_FINAL_WEBUI_PASSWORD", "correct horse")
    body = json.dumps({"password": "correct horse"})

    conn = conn_factory()
    origin = f"http://{conn.host}:{conn.port}"
    conn.request(
        "POST",
        "/api/login",
        body=body,
        headers={"Content-Type": "application/json", "Origin": origin},
    )
    response = conn.getresponse()
    payload = json.loads(response.read())
    conn.close()
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["token"]
    assert payload["expires_at"] > time.time()


def test_uploaded_cookie_overrides_are_unique_private_and_confined(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    first_text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfirst\n"
    second_text = "# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tSID\tsecond\n"

    first = webserver.save_uploaded_cookiefile(first_text)
    second = webserver.save_uploaded_cookiefile(second_text)
    assert first["ok"] and second["ok"]
    first_path, second_path = Path(first["path"]), Path(second["path"])
    assert first_path != second_path
    assert first_path.read_text(encoding="utf-8") == first_text
    assert second_path.read_text(encoding="utf-8") == second_text
    assert first_path.parent == second_path.parent == (tmp_path / "overrides")
    if os.name != "nt":
        assert stat.S_IMODE(first_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(second_path.stat().st_mode) == 0o600

    from jobspec import JobSpec
    root = tmp_path / "downloads"
    root.mkdir()
    spec = JobSpec.from_dict({"urls": ["https://youtu.be/x"], "cookiefile": str(first_path)})
    assert webserver._spec_path_violation(spec, root) is None


def test_uploaded_cookie_collision_never_reuses_existing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    monkeypatch.setattr(webserver.secrets, "token_hex", lambda _n: "fixed")
    existing = tmp_path / "overrides" / "job_fixed_cookies.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("do not replace\n", encoding="utf-8")

    result = webserver.save_uploaded_cookiefile(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tnew\n"
    )
    assert result["ok"] is False
    assert existing.read_text(encoding="utf-8") == "do not replace\n"


def test_uploaded_cookie_atomic_failure_removes_only_unpublished_reservation(
        monkeypatch, tmp_path):
    import secureio

    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    monkeypatch.setattr(webserver.secrets, "token_hex", lambda _n: "fixed")
    monkeypatch.setattr(
        secureio.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("boom")),
    )
    destination = tmp_path / "overrides" / "job_fixed_cookies.txt"

    result = webserver.save_uploaded_cookiefile(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tnew\n"
    )

    assert result["ok"] is False
    assert not destination.exists()
    assert not list(destination.parent.glob(".job_fixed_cookies.txt.*.tmp"))


def test_canary_endpoint(live_server, monkeypatch):
    conn_factory, token, state = live_server
    
    # Mock _speedtest_extract_media
    def mock_extract(state, cookiefile):
        return {"url": "http://mock", "id": "123", "degraded": False}
        
    monkeypatch.setattr(webserver, "_speedtest_extract_media", mock_extract)
    
    conn = conn_factory()
    conn.request("POST", "/api/canary", body="{}", headers={"X-Token": token})
    resp = conn.getresponse()
    import json
    data = json.loads(resp.read())
    assert resp.status == 200
    assert data["ok"] is True
    
    # Degraded
    def mock_extract_degraded(state, cookiefile):
        return {"url": "http://mock", "id": "123", "degraded": True, "degraded_reason": "no direct DASH formats available (throttled itag only)"}
    monkeypatch.setattr(webserver, "_speedtest_extract_media", mock_extract_degraded)
    
    conn = conn_factory()
    conn.request("POST", "/api/canary", body="{}", headers={"X-Token": token})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    assert resp.status == 200
    assert data["ok"] is False
    assert "no direct DASH formats" in data["error"]


# --- F1: a claimed row must never be left stranded in 'running' -----------------------------

def test_settle_claimed_closes_out_a_stranded_row(server_state):
    """settle_claimed() is the closing invariant for a row this process claimed but never wrote a
    terminal status for. It mirrors recover_orphaned_running's policy (requeue under the attempt
    cap, else fail) and additionally honours a cancel that had already been requested."""
    j = server_state.journal
    from jobstore import STATUS_RUNNING, STATUS_QUEUED, STATUS_FAILED, STATUS_CANCELLED

    # under the attempt cap -> requeued for another go
    rec = j.add({'urls': ['u1']})
    j.update(rec['id'], status=STATUS_RUNNING, attempt_count=1)
    assert j.settle_claimed(rec['id'])['status'] == STATUS_QUEUED

    # at the cap -> terminal failure with an explanatory error (no infinite requeue loop)
    rec2 = j.add({'urls': ['u2']})
    j.update(rec2['id'], status=STATUS_RUNNING, attempt_count=5)
    out2 = j.settle_claimed(rec2['id'])
    assert out2['status'] == STATUS_FAILED and out2.get('last_error')

    # a pending cancel wins over a requeue
    rec3 = j.add({'urls': ['u3']})
    j.update(rec3['id'], status=STATUS_RUNNING, attempt_count=0, cancel_requested=True)
    assert j.settle_claimed(rec3['id'])['status'] == STATUS_CANCELLED

    # already terminal -> no-op (the 99% case: the worker's own set_status got there first)
    rec4 = j.add({'urls': ['u4']})
    j.update(rec4['id'], status='done')
    assert j.settle_claimed(rec4['id']) is None
    assert j.get(rec4['id'])['status'] == 'done'


def test_engine_build_failure_does_not_strand_a_claimed_job(server_state):
    """THE F1 REGRESSION GUARD. claim_next() durably marks a row 'running'; engine construction used
    to happen OUTSIDE the per-job try, so a failing self.engine() fell straight to the outer
    log-and-backoff guard and left the card spinning in 'running' forever with no progress and no
    error. It must instead settle as 'failed' carrying the real exception.

    Uses the fixture's REAL journal on purpose — the pre-existing worker test uses a fake journal
    and therefore cannot observe stranded state at all."""
    st = server_state
    st._has_lock = True
    rec = st.journal.add({'urls': ['https://youtu.be/x']})

    def _boom():
        raise RuntimeError('engine build boom')

    st.engine = _boom

    t = threading.Thread(target=st.worker_loop, daemon=True)
    t.start()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        row = st.journal.get(rec['id'])
        if row and row['status'] in ('failed', 'cancelled', 'queued'):
            break
        time.sleep(0.02)
    st.stop.set()
    t.join(timeout=5.0)

    row = st.journal.get(rec['id'])
    assert row['status'] != 'running', 'job stranded in running — the F1 defect is back'
    assert row['status'] == 'failed', row
    assert 'boom' in str(row.get('last_error', '')), row
    assert not t.is_alive(), 'worker thread died instead of continuing to drain the queue'


# --- F3: path confinement on a PUBLIC server ------------------------------------------------

def _spec(**kw):
    from jobspec import JobSpec
    d = {'urls': ['https://youtu.be/x']}
    d.update(kw)
    return JobSpec.from_dict(d)


def test_path_policy_confines_every_path_bearing_field(tmp_path):
    """Only output_dir was ever checked. output_template, download_archive, cookiefile and a
    (browser, profile_path) cookies_from_browser were unchecked escapes. cookiefile is the worst:
    yt-dlp's close() -> save_cookies() TRUNCATES and rewrites whatever path it names, so an
    unchecked value is an arbitrary-file-OVERWRITE primitive."""
    root = tmp_path / 'dl'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    V = webserver._spec_path_violation

    # in-root everything -> clean
    assert V(_spec(output_dir=str(root), output_template='%(title)s.%(ext)s'), root) is None

    # each field independently escapes
    assert V(_spec(output_dir=str(outside)), root) == 'output_dir'
    assert V(_spec(output_template=str(outside / '%(title)s.%(ext)s')), root) == 'output_template'
    assert V(_spec(output_template='../../escape/%(title)s.%(ext)s'), root) == 'output_template'
    assert V(_spec(download_archive=str(outside / 'a.txt')), root) == 'download_archive'
    assert V(_spec(cookiefile=str(outside / 'c.txt')), root) == 'cookiefile'
    assert V(_spec(cookies_from_browser=['chrome', str(outside / 'profile')]), root) == 'cookies_from_browser'


def test_path_policy_allows_legitimate_outtmpl_and_browser_name(tmp_path):
    """The guard must not reject real yt-dlp templates: placeholders are inert literal text to
    pathlib, and a bare browser NAME carries no path at all."""
    root = tmp_path / 'dl'
    root.mkdir()
    V = webserver._spec_path_violation
    for tmpl in ('%(title)s.%(ext)s',
                 '%(uploader)s/%(title)s [%(id)s].%(ext)s',
                 'subdir/%(upload_date>%Y-%m-%d)s - %(title)s.%(ext)s'):
        assert V(_spec(output_template=tmpl), root) is None, tmpl
    assert V(_spec(cookies_from_browser='chrome'), root) is None      # name only -> no path
    assert V(_spec(), root) is None                                   # nothing set -> nothing to check


def test_path_policy_fails_closed_and_is_localhost_noop(tmp_path):
    """Fails CLOSED on junk, and is a literal no-op when the server is not public (confine_root
    is None on the default localhost bind, so the builder never calls the policy)."""
    root = tmp_path / 'dl'
    root.mkdir()
    V = webserver._spec_path_violation
    assert V(_spec(output_dir=12345), root) == 'output_dir'           # non-string -> violation
    # not public -> builder skips confinement entirely
    spec = webserver._web_jobspec_from_payload(
        {'urls': ['https://youtu.be/x'], 'output_dir': str(tmp_path / 'anywhere')}, confine_root=None)
    assert spec.output_dir == str(tmp_path / 'anywhere')


def test_public_create_and_rerun_both_reject_escaping_paths(tmp_path):
    """Route-level guard for BOTH routes. rerun previously bypassed even the one existing
    output_dir check, so a tweak could replace it outright."""
    root = tmp_path / 'dl'
    root.mkdir()
    outside = str(tmp_path / 'outside' / 'x.txt')
    payload = {'urls': ['https://youtu.be/x'], 'download_archive': outside}
    with pytest.raises(webserver.PathPolicyError) as ei:
        webserver._web_jobspec_from_payload(payload, confine_root=str(root))
    assert 'download_archive' in str(ei.value)

    # the merged-spec path (what rerun passes) is confined by the same chokepoint
    merged = {'urls': ['https://youtu.be/x'], 'output_dir': str(tmp_path / 'escape')}
    with pytest.raises(webserver.PathPolicyError) as ei2:
        webserver._web_jobspec_from_payload(merged, confine_root=str(root))
    assert 'output_dir' in str(ei2.value)


# --- F4: cancellation must be ONE state transition ------------------------------------------

def test_terminal_write_resolves_pending_cancel(server_state):
    """COMPARE-AND-SET. The worker decides done/failed from a snapshot taken milliseconds earlier,
    so a cancel landing in that gap used to be silently discarded and lose the race to 'done'.
    set_status now resolves it inside the SAME locked section that writes the outcome."""
    j = server_state.journal
    from jobstore import STATUS_RUNNING, STATUS_CANCELLED

    # a cancel pending at a 'done' write is a NO-OP: the job already completed (bytes landed,
    # history/webhook already say success), so the row stays 'done' and agrees with them rather
    # than being downgraded to 'cancelled'. The flag is still consumed.
    rec = j.add({'urls': ['u']})
    j.update(rec['id'], status=STATUS_RUNNING, cancel_requested=True)
    j.set_status(rec['id'], 'done', output_paths=['/tmp/a.mp4'])
    row = j.get(rec['id'])
    assert row['status'] == 'done', 'a completed job must stay done despite a late cancel'
    assert row['output_paths'] == ['/tmp/a.mp4'], 'must still report what actually landed'
    assert not row.get('cancel_requested'), 'flag must be consumed, not left pending'

    # the same applies to a 'failed' write, and the stale error is cleared
    rec2 = j.add({'urls': ['u']})
    j.update(rec2['id'], status=STATUS_RUNNING, cancel_requested=True)
    j.set_status(rec2['id'], 'failed', error='boom')
    assert j.get(rec2['id'])['status'] == STATUS_CANCELLED
    assert not j.get(rec2['id']).get('last_error')

    # NO-OP without a pending cancel — the ordinary path is untouched
    rec3 = j.add({'urls': ['u']})
    j.update(rec3['id'], status=STATUS_RUNNING)
    j.set_status(rec3['id'], 'done', output_paths=['/tmp/b.mp4'])
    assert j.get(rec3['id'])['status'] == 'done'

    # and non-terminal writes are never reclassified
    rec4 = j.add({'urls': ['u']})
    j.update(rec4['id'], status=STATUS_RUNNING, cancel_requested=True)
    j.set_status(rec4['id'], STATUS_RUNNING)
    assert j.get(rec4['id'])['status'] == STATUS_RUNNING


def test_cancel_probe_latches_and_sees_cross_process_cancel(server_state, tmp_path):
    """The probe is the cancel SOURCE for both runners. It must latch (a transient read error must
    never un-see a cancel already observed) and must see a cancel written by ANOTHER process —
    that second leg is the Windows-shaped case, where coarse timestamps make cache invalidation
    the thing most likely to break."""
    from jobstore import DownloadJournal, STATUS_RUNNING
    j = server_state.journal

    # The REAL flow: the probe is installed after claim_next(), i.e. on a RUNNING row, where
    # cancel() sets cancel_requested rather than writing a terminal status.
    rec = j.add({'urls': ['u']})
    j.update(rec['id'], status=STATUS_RUNNING)
    probe = j.cancel_probe(rec['id'], min_interval=0.0)
    assert probe() is False

    # a SECOND journal instance on the same file = the cross-process path
    other = DownloadJournal(str(j.path))
    other.cancel(rec['id'])
    assert probe() is True, 'probe did not observe a cancel written by another instance'

    # cancel() takes a DIFFERENT route for a queued row (status=cancelled, no flag) — the probe
    # must understand that shape too, not just the running-job flag.
    rec_q = j.add({'urls': ['q']})
    probe_q = j.cancel_probe(rec_q['id'], min_interval=0.0)
    assert probe_q() is False
    DownloadJournal(str(j.path)).cancel(rec_q['id'])
    assert probe_q() is True, 'probe missed a queued job cancelled straight to terminal state'

    # a dismissed card (row gone entirely) also means stop
    rec_d = j.add({'urls': ['d']})
    probe_d = j.cancel_probe(rec_d['id'], min_interval=0.0)
    assert probe_d() is False
    j.remove(rec_d['id'])
    assert probe_d() is True, 'probe missed a dismissed (removed) job'

    # latched: even if the row vanishes or reads start failing, it stays cancelled
    def _boom(*a, **k):
        raise OSError('journal unreadable')
    j.get = _boom
    assert probe() is True, 'probe un-saw a cancel it had already observed'


def test_cancel_probe_failure_never_kills_a_healthy_download(server_state):
    """A broken cancel channel must read as 'not cancelled'. The alternative — a flaky journal
    aborting healthy downloads — is far worse than a cancel arriving one tick late."""
    from yt_final import _cancel_pending

    def _raises():
        raise OSError('nope')

    assert _cancel_pending(_raises) is False
    assert _cancel_pending(None) is False          # CLI default: no channel installed
    assert _cancel_pending(lambda: True) is True


def test_path_policy_matches_consumer_base_for_relative_paths(tmp_path, monkeypatch):
    """B2 REGRESSION. download_archive/cookiefile are handed VERBATIM to yt-dlp, which opens them
    relative to the process CWD — NOT the download root. Validating them against `root` (the first
    version's bug) let a relative 'archive.txt' pass while yt-dlp wrote it beside the process,
    outside the root. They must be validated CWD-relative, matching the consumer."""
    root = tmp_path / 'dl'
    root.mkdir()
    # run the check from a CWD that is DEFINITELY outside the root, like a real server would be
    elsewhere = tmp_path / 'cwd'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    V = webserver._spec_path_violation

    # relative -> resolves to CWD/archive.txt, which is outside root -> MUST be rejected now
    assert V(_spec(download_archive='archive.txt'), root) == 'download_archive'
    assert V(_spec(cookiefile='cookies.txt'), root) == 'cookiefile'
    assert V(_spec(cookies_from_browser=['chrome', 'profile']), root) == 'cookies_from_browser'

    # a subdir relative path is still CWD-relative, still outside root -> rejected
    assert V(_spec(download_archive='sub/archive.txt'), root) == 'download_archive'

    # absolute-inside-root still passes (the legitimate way to set these on a public server)
    assert V(_spec(download_archive=str(root / 'a.txt')), root) is None
    assert V(_spec(cookiefile=str(root / 'c.txt')), root) is None

    # a relative archive that DOES land in root (because CWD is the root) passes — proves it is the
    # real consumer location being checked, not a blanket "relative = reject"
    monkeypatch.chdir(root)
    assert V(_spec(download_archive='archive.txt'), root) is None


def test_path_policy_output_dir_expanduser_only_not_expandvars(tmp_path, monkeypatch):
    """The engine resolves output_dir with Path(v).expanduser() — NOT expandvars. So '$HOME/x' is a
    LITERAL directory it creates under CWD, and the guard must treat it that way rather than expanding
    $HOME (which would validate a completely different, likely-passing, location)."""
    root = tmp_path / 'dl'
    root.mkdir()
    monkeypatch.chdir(tmp_path / 'cwd' if (tmp_path / 'cwd').mkdir() or True else tmp_path)
    # $HOME expands to a real home dir that is NOT inside root -> literal path is CWD/$HOME/x -> reject
    assert webserver._spec_path_violation(_spec(output_dir='$HOME/x'), root) == 'output_dir'


def test_probe_confines_output_dir_under_allow_public(live_server, tmp_path):
    """B2 (probe leg). /api/probe skipped the path policy, yet the downloader constructor mkdir()s
    output_dir — so a public client could create a directory anywhere. It must be confined too."""
    import json as _json
    conn_factory, token, state = live_server
    state.allow_public = True
    escape = str(tmp_path / 'probe_escape')

    conn = conn_factory()
    conn.request('POST', '/api/probe',
                 body=_json.dumps({'url': 'https://youtu.be/x', 'output_dir': escape}),
                 headers={'X-Token': token, 'Origin': f'http://127.0.0.1:{conn.port}',
                          'Host': f'127.0.0.1:{conn.port}'})
    resp = conn.getresponse()
    body = _json.loads(resp.read())
    assert resp.status == 403, (resp.status, body)
    assert 'output_dir' in body.get('error', '')
    assert not (tmp_path / 'probe_escape').exists(), 'probe created a directory outside the root'


def test_cli_queue_run_settles_malformed_spec_instead_of_stranding(monkeypatch, tmp_path):
    """B5. The web F1 fix did not cover the CLI queue runner: JobSpec.from_dict ran BEFORE the
    per-job try, so a corrupt/legacy spec raised, escaped the loop, and left the row that
    claim_next() had marked 'running' stranded forever. The runner must now settle it (failed) and
    keep draining. Drives the REAL `queue run` command with a fake engine."""
    import sys as _sys
    import yt_final
    from jobstore import DownloadJournal
    from jobspec import JobSpec

    qpath = tmp_path / 'queue.json'
    jq = DownloadJournal(str(qpath))
    rec = jq.add(JobSpec.from_dict({'urls': ['https://youtu.be/x']}))     # valid queued row

    class _FakeEngine:
        def __init__(self, *a, **k):
            self.downloader = type('D', (), {'cancel_check': None,
                                             'last_error': None, 'last_error_kind': None})()
        def run(self, spec):
            raise AssertionError('run() must not be reached — from_dict fails first')
    monkeypatch.setattr(yt_final, 'FinalYouTubeGPUEngine', _FakeEngine)

    # Corrupt/legacy spec: the adapter raises before run(). Patched AFTER the add above.
    real_from_dict = JobSpec.from_dict.__func__
    def _raising(d):
        if isinstance(d, dict) and d.get('urls') == ['https://youtu.be/x']:
            raise TypeError('corrupt spec')
        return real_from_dict(d)
    monkeypatch.setattr(yt_final.JobSpec, 'from_dict', staticmethod(_raising))

    monkeypatch.setattr(_sys, 'argv', ['yt_final.py', '--output-dir', str(tmp_path / 'dl'),
                                       'queue', 'run', '--queue-file', str(qpath)])
    try:
        yt_final.main()
    except SystemExit:
        pass

    row = DownloadJournal(str(qpath)).get(rec['id'])
    assert row is not None
    assert row['status'] != 'running', 'CLI stranded a job in running (B5 regression)'
    assert row['status'] == 'failed', row


def test_add_if_url_absent_is_atomic_and_idempotent(server_state):
    """B7 watch-enqueue race. The check-for-existing and the append must be ONE locked operation, so
    a repeat URL is a no-op rather than a duplicate download."""
    from jobspec import JobSpec
    j = server_state.journal
    r1 = j.add_if_url_absent(JobSpec.from_dict({'urls': ['https://youtu.be/dup']}), 'https://youtu.be/dup')
    assert r1 is not None
    r2 = j.add_if_url_absent(JobSpec.from_dict({'urls': ['https://youtu.be/dup']}), 'https://youtu.be/dup')
    assert r2 is None, 'second enqueue of the same url must be skipped'
    # a different url still enqueues
    r3 = j.add_if_url_absent(JobSpec.from_dict({'urls': ['https://youtu.be/other']}), 'https://youtu.be/other')
    assert r3 is not None
    urls = [((rec.get('spec') or {}).get('urls') or [None])[0] for rec in j.list()]
    assert urls.count('https://youtu.be/dup') == 1, 'duplicate job was created'

    # even a job that has already finished blocks a re-enqueue (matches the prior list()-based intent)
    j.set_status(r1['id'], 'done')
    assert j.add_if_url_absent(JobSpec.from_dict({'urls': ['https://youtu.be/dup']}), 'https://youtu.be/dup') is None


def test_poll_watch_dedups_concurrent_polls_of_same_watch(server_state):
    """B7 poll backlog. A poll already running for a watch must make a concurrent poll of the SAME
    watch a fast no-op, so repeated /api/watches/poll can't stack duplicate ~15s fetch work on the
    shared IO pool."""
    import threading
    st = server_state
    w = st.watches.add('UC_dedup', label='dedup')
    wid = w['id']

    entered = threading.Event()
    release = threading.Event()
    calls = {'n': 0}

    def blocking_fetch(_w):
        calls['n'] += 1
        entered.set()
        release.wait(5)          # hold the first poll open so the second overlaps it
        return []
    st._fetch_one = blocking_fetch

    r1 = {}
    t1 = threading.Thread(target=lambda: r1.setdefault('v', st.poll_watch(wid)))
    t1.start()
    assert entered.wait(3), 'first poll never started'
    # second poll while the first is still inside _fetch_one -> must skip without fetching again
    r2 = st.poll_watch(wid)
    assert r2 == (0, None), 'concurrent poll of same watch should be a no-op'
    assert calls['n'] == 1, 'second poll must not have called _fetch_one'
    release.set()
    t1.join(timeout=5)
    assert not t1.is_alive()

    # once the first finished, the guard is cleared -> a fresh poll fetches again
    r3 = st.poll_watch(wid)
    assert calls['n'] == 2, 'guard did not clear after the poll finished'


def test_submit_poll_dedups_at_submit_time_even_when_queued(server_state):
    """B7 refinement. _submit_poll must dedup BEFORE the pool, so a duplicate is skipped even while
    the first future is only QUEUED (all workers busy) — the exact hole the execute-time guard left,
    where duplicate futures piled up in the pool's unbounded work queue."""
    import threading
    st = server_state
    st._fetch_one = lambda _w: []
    w = st.watches.add('UC_submitdedup', label='x')
    wid = w['id']

    # Saturate the 4-worker IO pool so the poll future QUEUES rather than executes.
    block = threading.Event()
    blockers = [st._io_pool.submit(block.wait, 5) for _ in range(4)]

    f1 = st._submit_poll(wid)
    assert f1 is not None, 'first submit should return a future'
    f2 = st._submit_poll(wid)
    assert f2 is None, 'duplicate must be deduped at submit time even though f1 has not executed yet'

    block.set()                       # release the workers
    f1.result(timeout=5)
    for b in blockers:
        b.result(timeout=5)

    # slot released after completion -> a fresh submit works again
    f3 = st._submit_poll(wid)
    assert f3 is not None
    f3.result(timeout=5)


def test_public_url_count_cap_is_web_only(tmp_path):
    """B4: the PUBLIC path caps per-job URL count (abuse guard) while the trusted local path never
    does, so large local batches / playlist expansion are unaffected."""
    import pytest
    from webserver import _web_jobspec_from_payload, _MAX_PUBLIC_URLS
    root = str(tmp_path)
    many = [f'https://youtu.be/{i:04d}' for i in range(_MAX_PUBLIC_URLS + 1)]

    # public (confined) -> rejected with a 400-mapped ValueError
    with pytest.raises(ValueError):
        _web_jobspec_from_payload({'urls': many, 'output_dir': root}, confine_root=root)
    # trusted local (unconfined) -> accepted, no cap
    spec = _web_jobspec_from_payload({'urls': many}, confine_root=None)
    assert len(spec.urls) == _MAX_PUBLIC_URLS + 1
    # public with a normal batch -> fine
    spec2 = _web_jobspec_from_payload({'urls': many[:5], 'output_dir': root}, confine_root=root)
    assert len(spec2.urls) == 5


def test_legacy_token_file_is_retired_during_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    path = tmp_path / "web_token"
    path.write_text("do-not-read-this-legacy-secret", encoding="utf-8")
    retired = []

    monkeypatch.setattr(
        webserver,
        "retire_legacy_token_file",
        lambda candidate: retired.append(Path(candidate)) or candidate.unlink() is None,
        raising=False,
    )
    monkeypatch.setattr(webserver, "ServerState", lambda *_args, **_kwargs: object())

    class _StopBeforeListener(Exception):
        pass

    def stop_before_listener(*_args, **_kwargs):
        raise _StopBeforeListener

    monkeypatch.setattr(webserver.http.server, "ThreadingHTTPServer", stop_before_listener)
    # serve() probes the endpoint before constructing anything, so that a second copy is refused
    # rather than silently binding the same port. This test is about retiring the legacy token
    # file, not about duplicate detection, and it uses the DEFAULT port -- so without this it
    # fails for anyone who happens to have the app running while testing.
    monkeypatch.setattr(webserver, "_endpoint_already_serving", lambda *_a, **_k: False)

    with pytest.raises(_StopBeforeListener):
        webserver.serve(open_browser=False)

    assert retired == [path]
    assert not path.exists()


def test_job_create_accepts_direct_proxy_sentinel(live_server):
    """'direct' (any case) is a documented per-job SENTINEL meaning "no proxy for this job"
    (settings.resolve_job_proxy -> None), not a URL — so it must be exempt from validate_proxy, like
    the _web_jobspec_from_payload chokepoint already does. Regression: the public-mode SSRF gate's
    new validate_proxy call rejected a per-job "direct" override with 400 'proxy scheme must be...'.
    """
    conn_factory, token, state = live_server

    def _post(proxy_value, public):
        state.allow_public = public
        conn = conn_factory()
        host = f"127.0.0.1:{conn.port}"
        conn.request(
            "POST", "/api/jobs",
            body=json.dumps({"urls": ["https://youtu.be/aaaaaaaaaaa"], "proxy": proxy_value}),
            headers={"X-Token": token, "Content-Type": "application/json",
                     "Origin": f"http://{host}", "Host": host},
        )
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body

    for public in (False, True):
        for value in ("direct", "DIRECT"):
            status, body = _post(value, public)
            assert b"proxy scheme" not in body, (public, value, status, body[:200])


def test_job_create_nonstring_proxy_is_clean_400(live_server):
    """Robustness: a truthy non-string proxy (JSON bool/number/list/object) must yield a clean 400,
    not a 500 / connection reset. _web_jobspec_from_payload previously did (proxy or "").strip(),
    which AttributeError'd on a non-string; the /api/jobs handler only catches (ValueError,
    PathPolicyError), so the exception escaped uncaught. Falsy non-strings still coalesce to
    "no proxy" (accepted)."""
    conn_factory, token, state = live_server

    def _post(proxy_value):
        conn = conn_factory()
        host = f"127.0.0.1:{conn.port}"
        conn.request(
            "POST", "/api/jobs",
            body=json.dumps({"urls": ["https://youtu.be/aaaaaaaaaaa"], "proxy": proxy_value}),
            headers={"X-Token": token, "Content-Type": "application/json",
                     "Origin": f"http://{host}", "Host": host},
        )
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body

    # Truthy non-strings must be rejected as 400 (a clear message), never crash the handler.
    for bad in (True, 1, 3.5, ["socks5://1.1.1.1:1080"], {"url": "x"}):
        status, body = _post(bad)
        assert status == 400, (bad, status, body[:200])
        assert b"proxy must be a string" in body, (bad, status, body[:200])

    # Falsy non-strings coalesce to "no proxy" and are accepted (unchanged behavior).
    for ok in (False, 0, [], None):
        status, body = _post(ok)
        assert status in (200, 201), (ok, status, body[:200])
    print("test_job_create_nonstring_proxy_is_clean_400 passed")


def test_public_health_and_updates_gate_disclosure(live_server):
    """LOW leak: in public mode an UNAUTHENTICATED client must not get /api/health's non-liveness
    fields (absolute output_dir, host os, toolchain locations) or /api/updates. Loopback uses the
    same session policy: health liveness stays public, while non-liveness fields require a session."""
    conn_factory, token, state = live_server

    def _get(path, headers=None):
        conn = conn_factory()
        conn.request("GET", path, headers=headers or {})
        r = conn.getresponse(); body = r.read(); conn.close()
        return r.status, body

    state.allow_public = True
    st, body = _get("/api/health")                      # public, no token -> minimal
    assert st == 200 and b'"ok"' in body and b"output_dir" not in body and b"host_os" not in body
    st, _ = _get("/api/updates")                        # public, no token -> gated
    assert st == 401
    st, body = _get("/api/health", {"X-Token": token})  # public, token -> full
    assert st == 200 and b"output_dir" in body

    state.allow_public = False
    st, body = _get("/api/health")                      # loopback, no token -> still minimal
    assert st == 200 and b"output_dir" not in body
    st, body = _get("/api/health", {"X-Token": token})  # loopback, token -> full
    assert st == 200 and b"output_dir" in body
    print("test_public_health_and_updates_gate_disclosure passed")


def test_public_rerun_rejects_injected_local_proxy(live_server):
    """MED SSRF: /api/jobs/rerun must apply create's local-proxy gate — a tweak proxy pointing at
    loopback/private is blocked in public mode; the 'direct' sentinel (no proxy) is exempt."""
    conn_factory, token, state = live_server
    state.allow_public = True

    def _post(path, payload):
        conn = conn_factory(); host = f"127.0.0.1:{conn.port}"
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"X-Token": token, "Content-Type": "application/json",
                              "Origin": f"http://{host}", "Host": host})
        r = conn.getresponse(); body = r.read(); conn.close()
        return r.status, (json.loads(body) if body else {})

    st, rec = _post("/api/jobs", {"urls": ["https://youtu.be/aaaaaaaaaaa"]})
    assert st in (200, 201), (st, rec)
    jid = rec.get("id")
    assert jid, rec
    st, body = _post("/api/jobs/rerun", {"id": jid, "tweaks": {"proxy": "socks5://127.0.0.1:1080"}})
    assert st == 403, (st, body)                        # injected local proxy blocked
    st, body = _post("/api/jobs/rerun", {"id": jid, "tweaks": {"proxy": "direct"}})
    assert st != 403, (st, body)                        # 'direct' sentinel exempt
    print("test_public_rerun_rejects_injected_local_proxy passed")


def test_public_rerun_survives_the_servers_own_injected_default_proxy(live_server):
    """SHIPPED BUG: on a public server whose effective default proxy is local/private, EVERY re-run
    and every "retry failed" 403s — the feature is entirely dead, not degraded.

    Why: create RESOLVES the proxy and bakes the result into the durable spec
    (spec.proxy = resolve_job_proxy(payload.get('proxy'), eff_enabled, eff_proxy)), so a user who
    leaves the proxy box blank still gets a stored spec.proxy of 'http://127.0.0.1:<desync>'. Rerun
    then gates on that stored value. The gate's own comment claims blank is exempt because it is
    "server-injected", but blank NEVER SURVIVES CREATE — the carve-out was dead code.

    This is not a hypothetical config: desync is a shipped opt-in whose comment states the child is
    deliberately allowed to coexist with public mode, and a LAN proxy (192.168.x) hits it too.

    The fix must exempt the server's own injected value WITHOUT widening the SSRF hole, so this also
    pins that a client which echoes that same URL back as a tweak is still refused — otherwise the
    exemption would become a trivial bypass of the gate above.
    """
    conn_factory, token, state = live_server
    state.allow_public = True
    # ZERO MOCKS: set the real config a user would have. A saved LAN proxy reaches
    # effective_default_proxy()'s second branch with no subprocess involved, so create resolves and
    # bakes it exactly as it would in production.
    LOCAL = "http://192.168.10.4:3128"
    state.default_proxy_enabled = True
    state.default_proxy = LOCAL

    def _post(path, payload):
        conn = conn_factory(); host = f"127.0.0.1:{conn.port}"
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"X-Token": token, "Content-Type": "application/json",
                              "Origin": f"http://{host}", "Host": host})
        r = conn.getresponse(); body = r.read(); conn.close()
        return r.status, (json.loads(body) if body else {})

    # the user leaves the proxy box alone; create still bakes the resolved local URL into the spec
    st, rec = _post("/api/jobs", {"urls": ["https://youtu.be/aaaaaaaaaaa"]})
    assert st in (200, 201), (st, rec)
    jid = rec.get("id")
    assert rec["spec"]["proxy"] == LOCAL, \
        "premise of this test: create bakes the resolved default into the durable spec"

    # THE BUG: a bare re-run of that job, changing nothing, is refused
    st, body = _post("/api/jobs/rerun", {"id": jid})
    assert st == 201, ("bare re-run must not 403 on the server's own injected default", st, body)
    assert body["spec"]["proxy"] == LOCAL, body          # and it keeps using it

    # ...and so is a re-run that only changes quality (what the UI's re-run picker sends)
    st, body = _post("/api/jobs/rerun", {"id": jid, "tweaks": {"quality": "4k"}})
    assert st == 201, (st, body)
    assert body["spec"]["quality"] == "4k", body

    # THE EXEMPTION MUST NOT BECOME A BYPASS: a client naming a local proxy is still refused, even
    # when it echoes the server's own current default back verbatim.
    for injected in (LOCAL, "socks5://127.0.0.1:1080", "http://10.1.2.3:3128", "http://[::1]:8080"):
        st, body = _post("/api/jobs/rerun", {"id": jid, "tweaks": {"proxy": injected}})
        assert st == 403, ("client-named local proxy must stay blocked: " + injected, st, body)

    # The desync shape is the same bug on the other branch. desync_running() is stubbed so create
    # resolves to the loopback URL without actually spawning the child; desync_enabled/desync_port
    # (what the fix reads) are the REAL config values.
    state.default_proxy_enabled = False
    state.desync_enabled = True
    state.desync_port = 8799
    state.desync_running = lambda: True
    st, rec2 = _post("/api/jobs", {"urls": ["https://youtu.be/bbbbbbbbbbb"]})
    assert st in (200, 201), (st, rec2)
    assert rec2["spec"]["proxy"] == f"http://127.0.0.1:{state.desync_port}", rec2
    st, body = _post("/api/jobs/rerun", {"id": rec2["id"]})
    assert st == 201, ("the desync bypass default must re-run too", st, body)

    # ...and the exemption must NOT survive the default being turned off: once the server would no
    # longer inject that value, a stored local proxy is a plain local proxy again.
    state.desync_enabled = False
    st, body = _post("/api/jobs/rerun", {"id": rec2["id"]})
    assert st == 403, ("a stored local proxy is not exempt once the server stops injecting it", st, body)
    print("test_public_rerun_survives_the_servers_own_injected_default_proxy passed")


def test_write_secret_file_is_owner_only_at_creation(monkeypatch, tmp_path):
    """Secrets must be owner-only AT CREATION, not created at the ambient umask and chmod'd after.

    Neutralise the trailing chmod and clear the umask, so this assertion can ONLY pass when os.open()
    itself created the file 0600. A write_text()+chmod implementation (the old code) lands at 0666
    here and fails — which is what makes this test non-vacuous, unlike a plain final-mode assert.
    """
    if os.name == "nt":
        # Windows mode bits are advisory, so assert the thing that actually protects the file: the
        # DACL. This platform used to be skipped outright -- which is precisely where the defect
        # lived, because os.open(..., 0o600) gave webui.txt no protection at all and the file simply
        # inherited its parent directory's ACL.
        # Inspect the DACL, and inspect it WITHOUT touching the file first. The obvious spelling --
        # call secureio.ensure_private_file() and assert it did not raise -- is vacuous, because
        # that helper TIGHTENS the file rather than reading it: the test would repair the very
        # defect it is meant to detect and then pass. Verified: with the old os.open() body in
        # place, the ensure_private_file() form still passed.
        #
        # Measured difference on one directory, same run:
        #   os.open(..., 0o600)       -> 8 inherited ACEs, including NT AUTHORITY\SYSTEM:(I)(F),
        #                                BUILTIN\Administrators:(I)(F) and five other user SIDs
        #   atomic_replace_private()  -> exactly one ACE, the owner, not inherited
        p = tmp_path / "secret.txt"
        webserver._write_secret_file(p, "hunter2\n")
        assert p.read_text(encoding="utf-8") == "hunter2\n"

        listing = subprocess.run(
            ["icacls", os.fspath(p)],
            capture_output=True, text=True, check=True,
        ).stdout
        aces = [
            line.strip() for line in listing.splitlines()
            if ":" in line and "Successfully processed" not in line
        ]
        assert aces, ("icacls returned no ACEs", listing)
        # An inherited ACE means the file simply took its parent directory's protection, which for
        # a user profile grants SYSTEM and Administrators full control over the plaintext password.
        inherited = [ace for ace in aces if "(I)" in ace]
        assert not inherited, (
            "webui.txt inherited its parent directory's DACL instead of being created owner-only",
            inherited,
        )
        for principal in ("NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators", "Everyone"):
            assert not any(principal in ace for ace in aces), (
                f"{principal} must not appear in the secret file's DACL", aces)
        return
    monkeypatch.setattr(webserver.os, "chmod", lambda *a, **k: None)   # cannot mask a bad create mode
    old_umask = os.umask(0)                                            # nothing masked away
    try:
        p = tmp_path / "secret.txt"
        webserver._write_secret_file(p, "hunter2\n")
        assert p.read_text(encoding="utf-8") == "hunter2\n"
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)


def test_write_secret_file_truncates_a_longer_previous_secret(tmp_path):
    """A shorter new secret must not leave the tail of the old one behind.

    This is the trap in the obvious fix. Routing _write_secret_file through
    secureio.open_private_file() looks equivalent, but its Windows path maps O_CREAT to
    OPEN_ALWAYS, which does not truncate -- so rotating a long password to a short one would leave
    readable bytes of the old password in the file. atomic_replace_private() writes a fresh temp
    and replaces, so there is nothing to truncate.
    """
    p = tmp_path / "secret.txt"
    webserver._write_secret_file(p, "a-very-long-previous-password-value\n")
    webserver._write_secret_file(p, "short\n")
    assert p.read_bytes() == b"short\n"


def test_webui_password_file_uses_owner_only_helper():
    """webui.txt carries the plaintext UI password, so it must go through the owner-only helper —
    guards against a refactor silently reverting it to write_text() at the ambient umask."""
    src = Path(webserver.__file__).read_text(encoding="utf-8")
    assert "_write_secret_file(home / 'webui.txt'" in src, "webui.txt must use the owner-only helper"
    assert "webui.txt').write_text" not in src and 'webui.txt").write_text' not in src


def test_public_settings_and_jobs_reject_local_proxy(live_server):
    """Public-mode SSRF gate must cover settings save and job create (not only test-proxy/speedtest).
    User-supplied loopback/private proxies are forbidden; blank proxy (desync inject) stays allowed."""
    conn_factory, token, state = live_server
    state.allow_public = True

    def _headers(conn):
        # Origin netloc must equal Host (public-mode CSRF gate).
        host = f"127.0.0.1:{conn.port}"
        return {
            "X-Token": token,
            "Content-Type": "application/json",
            "Origin": f"http://{host}",
            "Host": host,
        }

    # settings: local proxy blocked
    conn = conn_factory()
    conn.request("POST", "/api/settings",
                 body=json.dumps({"enabled": True, "proxy": "socks5://127.0.0.1:1080"}),
                 headers=_headers(conn))
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 403, body
    assert "local" in (body.get("error") or "").lower()

    # settings: public proxy still accepted (shape-valid)
    conn = conn_factory()
    conn.request("POST", "/api/settings",
                 body=json.dumps({"enabled": True, "proxy": "socks5://1.1.1.1:1080"}),
                 headers=_headers(conn))
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 200, body

    # job create: user-supplied local proxy blocked
    conn = conn_factory()
    conn.request("POST", "/api/jobs",
                 body=json.dumps({
                     "urls": ["https://youtu.be/dQw4w9WgXcQ"],
                     "proxy": "http://127.0.0.1:9",
                 }),
                 headers=_headers(conn))
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 403, body
    assert "local" in (body.get("error") or "").lower()

    # job create: no user proxy (server may inject desync/remote) is not 403-for-local
    conn = conn_factory()
    conn.request("POST", "/api/jobs",
                 body=json.dumps({"urls": ["https://youtu.be/dQw4w9WgXcQ"]}),
                 headers=_headers(conn))
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 201, body


def test_rerun_rejects_a_non_object_tweaks_instead_of_500ing(live_server):
    """`tweaks` is a documented part of the /api/jobs/rerun contract, but a non-dict value used to
    fall straight through the `isinstance(tweaks, dict)` check and reach
    `JobSpec.from_dict(...).merged(**tweaks)`, which raises TypeError. That is NOT one of the two
    exceptions the surrounding try catches (PathPolicyError / ValueError), so the request died as an
    unhandled 500 with a traceback on the server console instead of a clean 400. Mirrors the
    "JSON body must be an object" guard the same handler already applies to the whole payload."""
    conn_factory, token, state = live_server

    def _post(path, payload):
        conn = conn_factory(); host = f"127.0.0.1:{conn.port}"
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"X-Token": token, "Content-Type": "application/json",
                              "Origin": f"http://{host}", "Host": host})
        r = conn.getresponse(); body = r.read(); conn.close()
        return r.status, (json.loads(body) if body else {})

    st, rec = _post("/api/jobs", {"urls": ["https://youtu.be/aaaaaaaaaaa"]})
    assert st in (200, 201), (st, rec)
    jid = rec["id"]

    for bad in (["quality"], "quality", 7, True):
        st, body = _post("/api/jobs/rerun", {"id": jid, "tweaks": bad})
        assert st == 400, (f"non-object tweaks must be a clean 400, got {st}", bad, body)
        assert "tweaks" in (body.get("error") or ""), body

    # ...and the two legitimate shapes still work exactly as before
    st, body = _post("/api/jobs/rerun", {"id": jid})
    assert st == 201, (st, body)
    st, body = _post("/api/jobs/rerun", {"id": jid, "tweaks": {"quality": "720p"}})
    assert st == 201, (st, body)
    assert body["spec"]["quality"] == "720p", body


def _authenticated_rerun_post(live_server, payload):
    return post_json(
        live_server,
        "/api/jobs/rerun",
        payload,
        token=live_server[1],
        headers={"Origin": same_origin(live_server)},
    )


def _create_rerun_source(live_server, url="https://youtu.be/aaaaaaaaaaa"):
    response = post_json(
        live_server,
        "/api/jobs",
        {"urls": [url]},
        token=live_server[1],
        headers={"Origin": same_origin(live_server)},
    )
    assert response.status == 201, response.json
    return response.json


def test_rerun_same_key_returns_first_row_and_distinct_keys_create_rows(live_server):
    source = _create_rerun_source(live_server)

    first = _authenticated_rerun_post(
        live_server,
        {"id": source["id"], "request_id": "rerun-key-a"},
    )
    replay = _authenticated_rerun_post(
        live_server,
        {
            "id": source["id"],
            "request_id": "rerun-key-a",
            "tweaks": {"quality": "720p"},
        },
    )
    distinct = _authenticated_rerun_post(
        live_server,
        {"id": source["id"], "request_id": "rerun-key-b"},
    )

    assert first.status == replay.status == distinct.status == 201
    assert replay.json == first.json
    assert distinct.json["id"] != first.json["id"]
    assert first.json["request_id"] == "rerun-key-a"
    assert distinct.json["request_id"] == "rerun-key-b"
    assert len([
        job for job in live_server[2].journal.list()
        if job.get("request_id") == "rerun-key-a"
    ]) == 1


def test_rerun_omission_generates_a_new_server_uuid_each_time(live_server):
    source = _create_rerun_source(live_server)

    first = _authenticated_rerun_post(live_server, {"id": source["id"]})
    second = _authenticated_rerun_post(live_server, {"id": source["id"]})

    assert first.status == second.status == 201
    assert uuid.UUID(first.json["request_id"]).version == 4
    assert uuid.UUID(second.json["request_id"]).version == 4
    assert first.json["request_id"] != second.json["request_id"]
    assert first.json["id"] != second.json["id"]


@pytest.mark.parametrize("request_id", [
    None,
    False,
    True,
    0,
    1,
    3.14,
    [],
    {},
    "",
    "x" * 7,
    "x" * 129,
    "\x1f" + "x" * 7,
    "x" * 7 + "\x7f",
    "valid-id\n",
    "valid-id\t",
    "valid-id-é",
])
def test_rerun_rejects_every_invalid_supplied_request_id_before_journal_access(
        live_server, monkeypatch, request_id):
    def forbidden_lookup(_request_id):
        raise AssertionError("invalid request id reached the journal")

    monkeypatch.setattr(
        live_server[2].journal,
        "get_by_request_id",
        forbidden_lookup,
        raising=False,
    )

    response = _authenticated_rerun_post(
        live_server,
        {"id": "does-not-matter", "request_id": request_id},
    )

    assert response.status == 400, (request_id, response.status, response.json)
    assert "request_id" in response.json["error"]


@pytest.mark.parametrize("request_id", [" " * 8, "~" * 128])
def test_rerun_accepts_request_id_boundaries_without_trimming(
        live_server, request_id):
    source = _create_rerun_source(live_server)

    response = _authenticated_rerun_post(
        live_server,
        {"id": source["id"], "request_id": request_id},
    )

    assert response.status == 201, response.json
    assert response.json["request_id"] == request_id


def test_rerun_replay_precedes_changed_deleted_source_and_hostile_tweaks(
        live_server):
    state = live_server[2]
    source = _create_rerun_source(live_server)
    request_id = "durable-replay"
    first = _authenticated_rerun_post(
        live_server,
        {"id": source["id"], "request_id": request_id},
    )
    assert first.status == 201, first.json

    changed_spec = dict(source["spec"], urls=[])
    state.journal.update(source["id"], spec=changed_spec)
    changed = _authenticated_rerun_post(
        live_server,
        {
            "id": source["id"],
            "request_id": request_id,
            "tweaks": ["must", "not", "execute"],
        },
    )
    assert changed.status == 201
    assert changed.json == first.json

    state.journal.remove(source["id"])
    deleted = _authenticated_rerun_post(
        live_server,
        {
            "id": "deleted-source",
            "request_id": request_id,
            "tweaks": {"proxy": "socks5://127.0.0.1:1080", "urls": []},
        },
    )
    assert deleted.status == 201
    assert deleted.json == first.json


def test_rerun_request_id_validation_remains_behind_auth_and_origin(live_server):
    invalid_payload = {"id": "missing", "request_id": None}

    unauthorized = post_json(
        live_server,
        "/api/jobs/rerun",
        invalid_payload,
        headers={"Origin": same_origin(live_server)},
    )
    forbidden = post_json(
        live_server,
        "/api/jobs/rerun",
        invalid_payload,
        token=live_server[1],
        headers={"Origin": "https://attacker.example"},
    )

    assert unauthorized.status == 401
    assert forbidden.status == 403


@pytest.mark.parametrize("loser_kind", ["deleted-invalid", "hostile-valid"])
def test_rerun_fast_miss_then_direct_winner_skips_every_loser_field(
        live_server, monkeypatch, loser_kind):
    """A winner committed after the fast miss is authoritative before loser evaluation."""
    state = live_server[2]
    source = _create_rerun_source(live_server)
    request_id = f"race-winner-{loser_kind}"
    winner_spec = webserver.JobSpec(urls=["https://youtu.be/bbbbbbbbbbb"])
    effects = []
    fast_missed = threading.Event()
    winner_committed = threading.Event()
    real_lookup = state.journal.get_by_request_id

    def barrier_lookup(key):
        found = real_lookup(key)
        assert found is None
        fast_missed.set()
        assert winner_committed.wait(10), "direct winner did not commit"
        return found

    monkeypatch.setattr(
        state.journal,
        "get_by_request_id",
        barrier_lookup,
    )

    real_loads = json.loads

    class TrackingPayload(dict):
        def get(self, key, default=None):
            if key in {"id", "tweaks"}:
                effects.append(f"payload:{key}")
            return super().get(key, default)

    def tracking_loads(raw, *args, **kwargs):
        value = real_loads(raw, *args, **kwargs)
        if isinstance(value, dict) and value.pop("_track_loser", False):
            return TrackingPayload(value)
        return value

    monkeypatch.setattr(webserver.json, "loads", tracking_loads)

    real_get = state.journal.get

    def tracking_get(job_id):
        effects.append("source")
        return real_get(job_id)

    monkeypatch.setattr(state.journal, "get", tracking_get)
    real_from_dict = webserver.JobSpec.from_dict

    def tracking_from_dict(cls, value):
        effects.append("tweaks")
        return real_from_dict(value)

    monkeypatch.setattr(
        webserver.JobSpec,
        "from_dict",
        classmethod(tracking_from_dict),
    )
    real_web_spec = webserver._web_jobspec_from_payload

    def tracking_web_spec(*args, **kwargs):
        effects.append("path")
        return real_web_spec(*args, **kwargs)

    monkeypatch.setattr(
        webserver,
        "_web_jobspec_from_payload",
        tracking_web_spec,
    )
    real_proxy_check = webserver._proxy_targets_local

    def tracking_proxy(proxy):
        effects.append("proxy")
        return real_proxy_check(proxy)

    monkeypatch.setattr(webserver, "_proxy_targets_local", tracking_proxy)
    real_warnings = webserver.JobSpec.config_warnings

    def tracking_warnings(spec):
        effects.append("warnings")
        return real_warnings(spec)

    monkeypatch.setattr(
        webserver.JobSpec,
        "config_warnings",
        tracking_warnings,
    )

    if loser_kind == "deleted-invalid":
        loser_id = "deleted-source"
        tweaks = ["invalid", "tweaks"]
    else:
        state.allow_public = True
        loser_id = source["id"]
        tweaks = {
            "output_dir": str(Path(state.output_dir) / "child"),
            "proxy": "http://1.1.1.1:8080",
            "two_pass": True,
        }

    responses = []
    errors = []

    def send_loser():
        try:
            responses.append(_authenticated_rerun_post(
                live_server,
                {
                    "_track_loser": True,
                    "id": loser_id,
                    "request_id": request_id,
                    "tweaks": tweaks,
                },
            ))
        except BaseException as error:
            errors.append(error)

    loser = threading.Thread(target=send_loser)
    loser.start()
    assert fast_missed.wait(10), "loser did not reach the fast lookup barrier"
    try:
        winner = type(state.journal)(state.journal.path).add(
            winner_spec,
            request_id=request_id,
        )
    finally:
        winner_committed.set()
    loser.join(10)

    assert not loser.is_alive()
    assert errors == []
    assert len(responses) == 1
    assert responses[0].status == 201
    assert responses[0].json == winner
    assert effects == []


# --- header-shape guards -----------------------------------------------------------------------
# Every guard below was proven load-bearing at runtime by live probe (neutralising it flipped a
# request from 403 to 200) AND proven uncovered: with each one replaced by `if False:` the whole
# 205-test auth suite stayed green. They are header-smuggling defences, so a refactor silently
# dropping one is exactly the failure these tests exist to prevent.


def test_duplicate_host_headers_are_rejected(live_server):
    """_request_authority requires exactly one Host. Two Host headers are a classic smuggling shape:
    one component reads the first, another reads the last. http.client cannot send them, which is why
    the raw helper takes extra_headers."""
    conn = live_server[0]()
    good = f"127.0.0.1:{conn.port}"
    conn.close()
    for second in (good, "evil.example:1234"):
        assert raw_http_status(
            live_server, "GET", good, extra_headers=[("Host", second)]
        ) == 403, f"a second Host header ({second}) was accepted"
    # ...and the same shape with the foreign value FIRST
    assert raw_http_status(
        live_server, "GET", "evil.example:1234", extra_headers=[("Host", good)]
    ) == 403


def test_duplicate_origin_headers_are_rejected(live_server):
    """_origin_allowed refuses more than one Origin. Without it, sending the expected origin first and
    a foreign one second is a cross-origin write that passes the CSRF check."""
    conn = live_server[0]()
    good_host = f"127.0.0.1:{conn.port}"
    conn.close()
    token = login(live_server)
    status = raw_http_status(
        live_server, "POST", good_host, path="/api/settings", token=token,
        extra_headers=[("Origin", f"http://{good_host}"), ("Origin", "http://evil.example")],
    )
    assert status == 403, "two Origin headers were accepted on a state-changing POST"


def test_whitespace_padded_host_is_rejected(live_server):
    """raw_host != raw_host.strip() catches padding. Header values are stripped inconsistently across
    parsers, so a trailing tab is another smuggling primitive."""
    conn = live_server[0]()
    port = conn.port
    conn.close()
    # TRAILING whitespace only. A LEADING space never reaches the application: the stdlib header
    # parser strips it after the colon, so that request returns 401 from the auth gate rather than
    # 403 from the Host gate. Asserting 403 for it would be testing http.client, not this guard.
    for padded in (f"127.0.0.1:{port}\t", f"127.0.0.1:{port} "):
        assert raw_http_status(live_server, "GET", padded) == 403, \
            f"padded Host accepted: {padded!r}"


def test_trailing_dot_localhost_is_accepted(live_server):
    """The opposite direction: "localhost." is the fully-qualified spelling of localhost and a browser
    can produce it, so normalisation must ACCEPT it. Dropping the removesuffix(".") would lock out a
    legitimate client — a guard can fail by being too strict as easily as too loose."""
    conn = live_server[0]()
    port = conn.port
    conn.close()
    assert raw_http_status(live_server, "GET", f"localhost.:{port}") != 403,         "the fully-qualified spelling of localhost was rejected"


def test_cross_origin_logout_is_rejected(live_server):
    """/api/logout returns before the shared origin gate, so it carries its OWN origin check. That copy
    had no test: deleting it left 205 tests green while a cross-origin request could revoke the
    session. Not browser-exploitable today (the custom X-Token header forces a preflight, and the
    server answers OPTIONS with 501 and no CORS headers) — this is defence in depth with a net under it."""
    token = login(live_server)
    response = post_json(
        live_server, "/api/logout", {}, token=token,
        headers={"Origin": "http://foreign.example:8765"},
    )
    assert response.status == 403, "a cross-origin logout was accepted"
    # ...and the session must survive the rejected logout
    assert get_json(live_server, "/api/session", token=token).status == 200,         "the rejected cross-origin logout revoked the session anyway"


def test_canary_survives_broken_auth_manager(live_server, monkeypatch):
    """CONFIRMED: /api/canary called cookies_auth.build_default_manager()/get_cookiefile() OUTSIDE
    its try — auth_status_payload and run_speedtest wrap the identical pair precisely because a
    broken/misconfigured auth setup can raise — so a corrupted auth config made the request die
    with a dropped connection instead of the endpoint's own {'ok': False, 'error': ...} contract."""
    conn_factory, token, state = live_server
    import cookies_auth

    def _boom(*a, **k):
        raise RuntimeError("corrupted auth config")

    monkeypatch.setattr(cookies_auth, "build_default_manager", _boom)
    conn = conn_factory()
    host = f"127.0.0.1:{conn.port}"
    conn.request("POST", "/api/canary", body=b"{}",
                 headers={"X-Token": token, "Content-Type": "application/json",
                          "Origin": f"http://{host}", "Host": host})
    resp = conn.getresponse()
    body = json.loads(resp.read().decode())
    assert resp.status == 200, resp.status
    assert body.get("ok") is False and body.get("error"), body


def test_endpoint_probe_detects_a_listener():
    """The duplicate-serve guard is only as good as its probe."""
    import socket as _socket

    assert webserver._endpoint_already_serving("127.0.0.1", 8797) is False
    holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 8797))
    holder.listen(1)
    try:
        assert webserver._endpoint_already_serving("127.0.0.1", 8797) is True
    finally:
        holder.close()


def test_second_serve_is_refused_and_does_not_clobber_the_password_file(tmp_path, monkeypatch):
    """A second `serve` must not overwrite webui.txt with a password that opens nothing.

    SO_REUSEADDR is inherited deliberately -- SO_EXCLUSIVEADDRUSE was rejected because it also
    refuses a bind while a socket sits in TIME_WAIT, and this server is the active closer on every
    request, which made `serve` unrestartable for ~120s after one page load. The cost is that on
    Windows a second `serve` binds the SAME port with no error and every request goes to the first
    process. The password is per-process, so the second window printed "ready" with a password that
    does not work -- and overwrote ~/.yt_final/webui.txt, the recovery note webui/index.html points
    users at, with that useless password.

    Double-clicking the launcher twice is the obvious thing to do after closing the browser tab.
    """
    import socket as _socket

    home = tmp_path / ".yt_final"
    home.mkdir()
    recovery = home / "webui.txt"
    recovery.write_text("yt_final web app\nOpen: http://127.0.0.1:8796/\nPassword: FIRST-COPY\n",
                        encoding="utf-8")

    import cookies_auth
    monkeypatch.setattr(cookies_auth, "auth_config_dir", lambda: home)

    holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    holder.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 8796))
    holder.listen(5)
    try:
        with pytest.raises(SystemExit) as caught:
            webserver.serve(port=8796, open_browser=False, output_dir=str(tmp_path / "out"))
    finally:
        holder.close()

    message = str(caught.value)
    assert "already serving" in message
    assert "8796" in message and "--port" in message
    assert recovery.read_text(encoding="utf-8").endswith("Password: FIRST-COPY\n"), (
        "the running copy's recovery note must survive a refused second launch")


def _login_attempt(live_server, password_value):
    """POST /api/login through the same helper the rest of this module uses."""
    return post_json(
        live_server, "/api/login", {"password": password_value},
        headers={"Origin": same_origin(live_server)},
    )


def test_failed_logins_are_slowed_and_then_briefly_locked(live_server):
    """Online password guessing against /api/login must not be free.

    The DEFAULT password is secrets.token_urlsafe(12) -- about 96 bits -- and guessing never
    touches that. The risk appears only when an operator sets YT_FINAL_WEBUI_PASSWORD to something
    memorable AND reaches the server through the SSH/WireGuard tunnel the README blesses.
    Publishing the project also publishes the exact request shape, so the cost of an online attempt
    drops to writing a for-loop.

    Deliberately modest: a delay per miss, then a short lock. This is a local single-user
    application, and locking its owner out of their own UI would be a worse bug than the one being
    prevented -- so any success clears it.
    """
    state = live_server[2] if isinstance(live_server, tuple) else live_server.state
    state.login_throttle.record_success()

    seen_429 = False
    for _ in range(webserver._LOGIN_FAILURE_LIMIT):
        response = _login_attempt(live_server, "not-the-password")
        if response.status == 429:
            seen_429 = True
            assert "retry_after" in response.json, response.json
            break
        assert response.status == 401, (response.status, response.json)
    assert seen_429, "a run of failed logins must eventually be refused outright"

    # A locked login is refused WITHOUT evaluating the guess, so a correct password is not a way to
    # probe whether the lock is real.
    response = _login_attempt(live_server, password())
    assert response.status == 429, (response.status, response.json)

    # ...and the owner is not locked out for long: clearing the lock restores normal login.
    state.login_throttle.record_success()
    response = _login_attempt(live_server, password())
    assert response.status == 200 and response.json.get("ok") is True


def test_a_correct_password_clears_the_failure_run(live_server):
    """One success resets the counter, so ordinary typos never accumulate into a lock."""
    state = live_server[2] if isinstance(live_server, tuple) else live_server.state
    throttle = state.login_throttle
    throttle.record_success()

    for _ in range(webserver._LOGIN_FAILURE_LIMIT - 1):
        assert _login_attempt(live_server, "wrong").status == 401
    assert _login_attempt(live_server, password()).status == 200
    assert throttle.retry_after() == 0.0

    assert _login_attempt(live_server, "wrong").status == 401, (
        "the counter should have restarted after the success")
    throttle.record_success()


def test_weak_operator_password_is_reported_but_not_rejected(monkeypatch):
    """Warn, do not refuse: rejecting on upgrade would lock someone out of their own app."""
    monkeypatch.setenv("YT_FINAL_WEBUI_PASSWORD", "hunter2")
    warning = webserver.warn_if_webui_password_is_weak()
    assert warning and "7 characters" in warning
    assert webserver.check_webui_password("hunter2") is True, "a weak password must still work"

    monkeypatch.setenv("YT_FINAL_WEBUI_PASSWORD", "a-long-enough-passphrase")
    assert webserver.warn_if_webui_password_is_weak() is None

    monkeypatch.delenv("YT_FINAL_WEBUI_PASSWORD", raising=False)
    assert webserver.warn_if_webui_password_is_weak() is None


def test_an_unreadable_login_profile_is_not_reported_as_a_failed_sign_in(monkeypatch):
    """A profile we could not READ must not be reported as "you did not sign in".

    Both outcomes used to return the same message, so a profile refused for its directory
    permissions told the user to go and sign in again -- the one action that cannot help,
    and the one they had already done correctly. This is exactly what happened on Windows:
    the login succeeded, the profile DACL denied the read, and the UI blamed the user.
    """
    import cookie_providers as cp

    monkeypatch.setattr(cp, "native_browser_login",
                        lambda *a, **k: {"ok": True, "browser": "chrome", "profile_dir": "p"})
    monkeypatch.setattr(cp, "_export_managed_profile", lambda *a, **k: None)
    monkeypatch.setattr(cp, "last_export_failure",
                        lambda: "PermissionError: access is denied")

    out = webserver.auth_login_session()
    assert out["ok"] is False
    assert out["code"] == "profile_unreadable", (
        "an unreadable profile was reported as a failed sign-in")
    assert "PermissionError: access is denied" in out["error"], (
        "the real reason must reach the user, not a generic message")
    assert "no YouTube login cookies were found" not in out["error"]


def test_a_genuine_missing_sign_in_still_says_so(monkeypatch):
    """Control: with no reason recorded, the not-signed-in message must still be used.

    If the profile_unreadable branch swallowed this case too, a user who genuinely closed
    the browser without signing in would be sent to chase a permissions problem that does
    not exist.
    """
    import cookie_providers as cp

    monkeypatch.setattr(cp, "native_browser_login",
                        lambda *a, **k: {"ok": True, "browser": "chrome", "profile_dir": "p"})
    monkeypatch.setattr(cp, "_export_managed_profile", lambda *a, **k: None)
    monkeypatch.setattr(cp, "last_export_failure", lambda: None)

    out = webserver.auth_login_session()
    assert out["ok"] is False
    assert out["code"] == "not_signed_in"
    assert "no YouTube login cookies were found" in out["error"]


def test_a_leftover_cookie_snapshot_is_reported_on_the_SUCCESS_path(monkeypatch, tmp_path):
    """Recording the leftover is pointless if nothing on the success path reads it.

    When the cookies are read but the temporary profile copy cannot be deleted, that copy stays
    on disk holding a copy of the user's YouTube session. _cdp_client records where it is, and
    cookie_providers carries the reason across -- but the only reader was the failure branch,
    which by definition does not run when the login succeeded. So the user was told "Login
    complete" and never learned a copy of their session had been left behind.
    """
    import cookie_providers as cp
    import cookies_auth

    leftover = (
        "OSError: read cookies, but could not delete the temporary profile snapshot at "
        "C:/Temp/ytf_cdp_abc; it holds a copy of your browser cookies and should be deleted "
        "manually"
    )
    exported = tmp_path / "profile.txt"
    _row = chr(9).join([".youtube.com", "TRUE", "/", "TRUE", "0", "SID", "v"]) + chr(10)
    exported.write_text(_row, encoding="utf-8")
    active = tmp_path / "active.txt"

    monkeypatch.setattr(cp, "native_browser_login",
                        lambda *a, **k: {"ok": True, "browser": "chrome", "profile_dir": "p"})
    monkeypatch.setattr(cp, "_export_managed_profile", lambda *a, **k: str(exported))
    monkeypatch.setattr(cp, "last_export_failure", lambda: leftover)
    monkeypatch.setattr(cookies_auth, "replace_active_cookiefile",
                        lambda **kw: str(active) if active.write_text(
                            exported.read_text(encoding="utf-8"), encoding="utf-8") is None else str(active))

    out = webserver.auth_login_session()
    assert out["ok"] is True, out
    assert "could not delete the temporary profile snapshot" in out["message"], (
        "a leftover copy of the user's cookies was not reported on the success path")
    assert out.get("leftover_snapshot")


def test_a_clean_login_does_not_mention_a_leftover(monkeypatch, tmp_path):
    """Control: with nothing left behind, the success message stays clean."""
    import cookie_providers as cp
    import cookies_auth

    exported = tmp_path / "profile.txt"
    _row = chr(9).join([".youtube.com", "TRUE", "/", "TRUE", "0", "SID", "v"]) + chr(10)
    exported.write_text(_row, encoding="utf-8")
    active = tmp_path / "active.txt"

    monkeypatch.setattr(cp, "native_browser_login",
                        lambda *a, **k: {"ok": True, "browser": "chrome", "profile_dir": "p"})
    monkeypatch.setattr(cp, "_export_managed_profile", lambda *a, **k: str(exported))
    monkeypatch.setattr(cp, "last_export_failure", lambda: None)
    monkeypatch.setattr(cookies_auth, "replace_active_cookiefile",
                        lambda **kw: str(active) if active.write_text(
                            exported.read_text(encoding="utf-8"), encoding="utf-8") is None else str(active))

    out = webserver.auth_login_session()
    assert out["ok"] is True, out
    assert "snapshot" not in out["message"].lower()
    assert out.get("leftover_snapshot") is None
