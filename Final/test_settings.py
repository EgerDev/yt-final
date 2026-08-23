import tomllib
from pathlib import Path
import settings


def test_validate_proxy_accepts_passwordless_socks():
    assert settings.validate_proxy("socks5://127.0.0.1:1080") == (True, "")

def test_validate_proxy_empty_is_ok():
    assert settings.validate_proxy("") == (True, "")

def test_validate_proxy_rejects_bad_scheme():
    ok, err = settings.validate_proxy("ftp://host:21")
    assert ok is False and "scheme" in err

def test_validate_proxy_rejects_credentials():
    ok, err = settings.validate_proxy("socks5://user:pass@host:1080")
    assert ok is False and "passwordless" in err
    ok2, _ = settings.validate_proxy("socks5://user@host:1080")
    assert ok2 is False

def test_validate_proxy_requires_host_and_port():
    assert settings.validate_proxy("socks5://:1080")[0] is False
    assert settings.validate_proxy("socks5://host")[0] is False

def test_load_defaults_when_no_file(tmp_path):
    assert settings.load_network_settings(tmp_path) == {
        "enabled": False, "proxy": "", "desync": False, "desync_mode": "oob", "doh": False}

def test_save_then_load_roundtrip(tmp_path):
    settings.save_network_settings(True, "socks5://127.0.0.1:1080", tmp_path)
    assert settings.load_network_settings(tmp_path) == {
        "enabled": True, "proxy": "socks5://127.0.0.1:1080", "desync": False, "desync_mode": "oob", "doh": False}

def test_save_does_not_clobber_other_tables(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[auth]\nenabled = true\n', encoding="utf-8")
    settings.save_network_settings(True, "socks5://127.0.0.1:1080", tmp_path)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["auth"]["enabled"] is True
    assert data["network"]["proxy"] == "socks5://127.0.0.1:1080"

def test_save_updates_existing_network_table(tmp_path):
    settings.save_network_settings(True, "socks5://127.0.0.1:1080", tmp_path)
    settings.save_network_settings(False, "", tmp_path)
    assert settings.load_network_settings(tmp_path) == {
        "enabled": False, "proxy": "", "desync": False, "desync_mode": "oob", "doh": False}


def test_save_replaces_a_network_header_at_eof_without_trailing_newline(tmp_path):
    """CONFIRMED: the strip regex required a newline AFTER the header line, so a config ending in a
    bare '[network]' (hand edit / truncation) was not stripped — the save appended a SECOND
    [network] table and tomllib then failed on every load. Both load_network_settings and
    load_auth_config swallow that, so the just-saved proxy AND the whole [auth] section silently
    read as defaults until the file was repaired by hand."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[auth]\nenabled = true\n[network]', encoding="utf-8")   # no trailing newline
    settings.save_network_settings(True, "socks5://127.0.0.1:1080", tmp_path)
    text = cfg.read_text(encoding="utf-8")
    assert text.count("[network]") == 1, text
    data = tomllib.loads(text)                       # must still parse — no duplicate table
    assert data["network"]["proxy"] == "socks5://127.0.0.1:1080"
    assert data["auth"]["enabled"] is True           # other tables survive
    assert settings.load_network_settings(tmp_path)["proxy"] == "socks5://127.0.0.1:1080"


def test_doh_and_tlsrec_persist(tmp_path):
    """DoH toggle round-trips through config.toml, tlsrec is a valid whitelisted mode, and doh
    defaults False when not written."""
    settings.save_network_settings(True, "socks5://x:1", tmp_path, desync=True, desync_mode="tlsrec", doh=True)
    n = settings.load_network_settings(tmp_path)
    assert n["doh"] is True and n["desync_mode"] == "tlsrec" and n["desync"] is True
    settings.save_network_settings(True, "socks5://x:1", tmp_path, desync=True, desync_mode="oob")  # doh omitted
    assert settings.load_network_settings(tmp_path)["doh"] is False
    assert settings.normalize_desync_mode("tlsrec") == "tlsrec"      # tlsrec whitelisted
    assert settings.normalize_desync_mode("bogus") == "oob"          # unknown -> default

def test_resolve_unset_disabled_is_none():
    assert settings.resolve_job_proxy("", False, "socks5://x:1") is None

def test_resolve_unset_enabled_uses_default():
    assert settings.resolve_job_proxy("", True, "socks5://x:1") == "socks5://x:1"

def test_resolve_per_job_overrides():
    assert settings.resolve_job_proxy("socks5://y:2", True, "socks5://x:1") == "socks5://y:2"

def test_resolve_direct_sentinel_forces_none():
    assert settings.resolve_job_proxy("direct", True, "socks5://x:1") is None
    assert settings.resolve_job_proxy("DIRECT", True, "socks5://x:1") is None

def test_serverstate_loads_and_sets_network(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import importlib, cookies_auth, webserver
    importlib.reload(cookies_auth)  # pick up YT_FINAL_HOME
    st = webserver.ServerState(str(tmp_path / "out"), queue_file=str(tmp_path / "q.json"))
    assert st.default_proxy_enabled is False
    assert st.default_proxy == ""
    st.set_network_settings(True, "socks5://127.0.0.1:1080")
    assert st.default_proxy_enabled is True
    assert st.default_proxy == "socks5://127.0.0.1:1080"
    # persisted:
    assert settings.load_network_settings(tmp_path)["proxy"] == "socks5://127.0.0.1:1080"

def test_jobspec_builder_rejects_credentialed_proxy():
    import webserver
    try:
        webserver._web_jobspec_from_payload(
            {"urls": ["https://x"], "proxy": "socks5://u:p@h:1080"})
        assert False, "should have raised"
    except ValueError as e:
        assert "passwordless" in str(e)

def test_jobspec_builder_allows_direct_and_blank():
    import webserver
    for v in ("", "direct", "DIRECT"):
        spec = webserver._web_jobspec_from_payload({"urls": ["https://x"], "proxy": v})
        assert spec.proxy in (None, v) or spec.proxy == v  # builder leaves value; handler resolves

def test_settings_payload_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import importlib, cookies_auth, webserver
    importlib.reload(cookies_auth)
    st = webserver.ServerState(str(tmp_path/"o"), queue_file=str(tmp_path/"q.json"))
    st.set_network_settings(True, "socks5://127.0.0.1:1080")
    payload = webserver.network_settings_payload(st)
    assert payload["network"]["enabled"] is True
    assert payload["network"]["proxy"] == "socks5://127.0.0.1:1080"
    assert payload["network"]["proxy_set"] is True


def test_watch_enqueue_bakes_effective_default_proxy(tmp_path, monkeypatch):
    """CONFIRMED: /api/jobs bakes resolve_job_proxy(..., *effective_default_proxy()) into every
    UI-submitted spec, but _enqueue_from_watch built its JobSpec from the watch defaults alone and
    worker_loop injects nothing at run time — so every auto-enqueued watch download (and every
    Watch-panel 'queue') ran DIRECT even with the bypass or a privacy proxy enabled."""
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import importlib, cookies_auth, webserver
    importlib.reload(cookies_auth)
    st = webserver.ServerState(str(tmp_path / "o"), queue_file=str(tmp_path / "q.json"))
    try:
        st.set_network_settings(True, "socks5://127.0.0.1:1080")
        w = st.watches.add("UCwatchproxy", label="c", url="https://youtube.com/@c",
                           auto_queue=True, defaults={"quality": "720p"})
        assert st._enqueue_from_watch(w, {"url": "https://youtu.be/x1", "id": "x1", "title": "t"})
        jobs = [j for j in st.journal.list() if "youtu.be/x1" in (j["spec"].get("urls") or [""])[0]]
        assert jobs, "watch enqueue produced no job"
        assert jobs[0]["spec"].get("proxy") == "socks5://127.0.0.1:1080", \
            "watch-enqueued job must carry the effective default proxy like /api/jobs does"
        # and the per-job 'direct' sentinel in a watch's defaults still wins over the default
        w2 = st.watches.add("UCdirect", label="d", url="https://youtube.com/@d",
                            auto_queue=True, defaults={"quality": "720p", "proxy": "direct"})
        assert st._enqueue_from_watch(w2, {"url": "https://youtu.be/x2", "id": "x2", "title": "t"})
        jobs2 = [j for j in st.journal.list() if "youtu.be/x2" in (j["spec"].get("urls") or [""])[0]]
        assert jobs2 and not jobs2[0]["spec"].get("proxy"), \
            "an explicit 'direct' in the watch defaults must resolve to no proxy"
    finally:
        st.stop.set()
        try:
            st.journal.release_run_lock()
        except Exception:
            pass


def test_desync_roundtrip_and_backcompat(tmp_path):
    # legacy 3-arg call (enabled, proxy, config_dir) still works, desync defaults false
    settings.save_network_settings(True, "socks5://127.0.0.1:1080", tmp_path)
    assert settings.load_network_settings(tmp_path)["desync"] is False
    # desync as keyword persists and round-trips, no-clobber of [auth]
    (tmp_path / "config.toml").write_text('[auth]\nenabled = true\n', encoding="utf-8")
    settings.save_network_settings(False, "", tmp_path, desync=True)
    got = settings.load_network_settings(tmp_path)
    assert got["desync"] is True and got["enabled"] is False
    import tomllib
    assert tomllib.loads((tmp_path / "config.toml").read_text())["auth"]["enabled"] is True


def test_desync_mode_roundtrip_and_whitelist(tmp_path):
    settings.save_network_settings(True, "", tmp_path, desync=True, desync_mode="frag")
    assert settings.load_network_settings(tmp_path)["desync_mode"] == "frag"
    # an unknown/injected mode falls back to the safe default, never reaches argv
    settings.save_network_settings(True, "", tmp_path, desync=True, desync_mode="; rm -rf /")
    assert settings.load_network_settings(tmp_path)["desync_mode"] == "oob"
    assert settings.normalize_desync_mode("FRAG_OOB") == "frag_oob"
    assert settings.normalize_desync_mode(None) == "oob"


def test_serverstate_desync_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import importlib, cookies_auth, webserver
    importlib.reload(cookies_auth)
    st = webserver.ServerState(str(tmp_path / "o"), queue_file=str(tmp_path / "q.json"))
    # remote proxy on, desync off -> remote proxy is effective
    st.set_network_settings(True, "socks5://127.0.0.1:1080", False)
    assert st.effective_default_proxy() == (True, "socks5://127.0.0.1:1080")
    # desync flag persists to config
    st.set_network_settings(False, "", True)
    assert st.desync_enabled is True
    assert settings.load_network_settings(tmp_path)["desync"] is True
    st.stop_desync()


def test_stop_desync_reaps_child_after_force_kill(tmp_path, monkeypatch):
    """When terminate() doesn't reap in time, stop_desync must kill AND wait() the child, so the
    port is actually released and no orphan listener survives to fool the next readiness probe."""
    import subprocess
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import webserver
    st = webserver.ServerState(str(tmp_path / "o"), queue_file=str(tmp_path / "q.json"))

    class _StubbornProc:
        def __init__(self):
            self.terminated = False
            self.killed = False
            self.waits = []          # timeouts passed to wait(), in order
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self._alive = False      # kill actually reaps this child

        def wait(self, timeout=None):
            self.waits.append(timeout)
            assert self.terminated, "wait() called before terminate()"
            if not self.killed:
                # first wait() (post-terminate) times out -> forces the kill branch
                raise subprocess.TimeoutExpired(cmd="desync", timeout=timeout)
            return 0

    p = _StubbornProc()
    st.desync_proc = p
    st.stop_desync()
    assert p.terminated and p.killed, "must escalate terminate -> kill"
    assert len(p.waits) >= 2, "must wait() AGAIN after kill (reap), not only after terminate"
    assert st.desync_proc is None
    # double-stop is a no-op and must not raise
    st.stop_desync()
    assert st.desync_proc is None


def test_validate_proxy_rejects_control_chars_and_quotes():
    assert settings.validate_proxy('http://127.0.0.1:1080/"\nenabled = true')[0] is False
    assert settings.validate_proxy("socks5://127.0.0.1:1080\r\n[auth]")[0] is False
    assert settings.validate_proxy("socks5://127.0.0.1:1080'")[0] is False
    assert settings.validate_proxy("socks5://127.0.0.1:1080")[0] is True


def test_desync_toggle_preserves_remote_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    import importlib, cookies_auth, webserver
    importlib.reload(cookies_auth)
    st = webserver.ServerState(str(tmp_path / "o"), queue_file=str(tmp_path / "q.json"))
    st.set_network_settings(True, "socks5://127.0.0.1:1080", False)
    # a desync-only update (proxy/enabled left unchanged) must NOT clear the saved proxy
    st.set_network_settings(st.default_proxy_enabled, st.default_proxy, True)
    assert st.default_proxy == "socks5://127.0.0.1:1080" and st.default_proxy_enabled is True
    assert st.desync_enabled is True
    st.stop_desync()


def test_https_proxy_rejected_working_schemes_accepted():
    ok, err = settings.validate_proxy("https://proxy:8443")   # needs requests/curl_cffi -> unusable
    assert ok is False and "scheme" in err
    for good in ("http://p:8080", "socks4://p:9050", "socks5://p:1080", "socks5h://p:1080"):
        assert settings.validate_proxy(good)[0] is True, good


def test_find_existing_download(tmp_path):
    import webserver
    out = tmp_path / "dl"; out.mkdir()
    assert webserver.find_existing_download("abc12345678", str(out)) == {"found": False}
    assert webserver.find_existing_download("", str(out)) == {"found": False}
    f = out / "Cool Video [abc12345678].mp4"; f.write_bytes(b"x" * 2048)
    r = webserver.find_existing_download("abc12345678", str(out))
    assert r["found"] and r["path"] == str(f) and r["size"] == 2048
    (out / "Half [q9999999999].mp4.part").write_bytes(b"z")   # partials must NOT count
    assert webserver.find_existing_download("q9999999999", str(out)) == {"found": False}


def test_find_existing_excludes_fragment_temp_files(tmp_path):
    import webserver
    out = tmp_path / "dl"; out.mkdir()
    (out / "Movie [vid00000001].f137.mp4.part-Frag3").write_bytes(b"x" * 500)  # in-progress fragment
    (out / "Movie [vid00000001].mp4.temp").write_bytes(b"y" * 500)             # post-process temp
    assert webserver.find_existing_download("vid00000001", str(out)) == {"found": False}
    (out / "Movie [vid00000001].mp4").write_bytes(b"z" * 4096)                 # the real completed file
    r = webserver.find_existing_download("vid00000001", str(out))
    assert r["found"] and r["path"].endswith("Movie [vid00000001].mp4")
