import cookies_auth
import secureio
import yt_final


class _FakeDL:
    def __init__(self): self.last_error_kind = "agegate"; self.last_error = "age"


class _FakeEngine:
    def __init__(self, retry_result):
        self.downloader = _FakeDL(); self._retry_result = retry_result; self.ran = []
    def run(self, spec):
        self.ran.append(getattr(spec, "cookiefile", None))
        return self._retry_result


class _Spec:
    def __init__(self): self.cookiefile = None


def test_non_tty_never_prompts(monkeypatch):
    monkeypatch.setattr(yt_final._sys.stdin, "isatty", lambda: False, raising=False)
    eng = _FakeEngine(retry_result=["x.mp4"])
    assert yt_final._offer_auth_for_agegate(eng, _Spec()) is False
    assert eng.ran == []  # no retry attempted


def test_import_path_sets_cookiefile_and_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(yt_final._sys.stdin, "isatty", lambda: True, raising=False)
    cookies = tmp_path / "c.txt"; cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    # user picks Import, then supplies the path
    answers = iter(["I", str(cookies)])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    eng = _FakeEngine(retry_result=["ok.mp4"])
    spec = _Spec()
    assert yt_final._offer_auth_for_agegate(eng, spec) is True
    assert spec.cookiefile is not None          # a cookiefile was resolved and set
    assert eng.ran and eng.ran[-1] == spec.cookiefile  # retried WITH it


def test_import_path_atomic_failure_preserves_previous_cookiefile(
        monkeypatch, tmp_path):
    monkeypatch.setattr(yt_final._sys.stdin, "isatty", lambda: True, raising=False)
    source = tmp_path / "c.txt"
    source.write_bytes(b"new-complete")
    store = tmp_path / "cookies"
    store.mkdir(parents=True)
    active = store / "active.txt"
    active.write_bytes(b"old-complete")
    answers = iter(["I", str(source)])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    monkeypatch.setattr(
        secureio.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("boom")),
    )
    engine = _FakeEngine(retry_result=["ok.mp4"])

    assert yt_final._offer_auth_for_agegate(engine, _Spec()) is False
    assert active.read_bytes() == b"old-complete"
    assert engine.ran == []


def test_login_path_atomic_failure_preserves_previous_cookiefile(
        monkeypatch, tmp_path):
    import cookie_providers as providers

    monkeypatch.setattr(yt_final._sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "L")
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    store = tmp_path / "cookies"
    store.mkdir(parents=True)
    active = store / "active.txt"
    active.write_bytes(b"old-complete")
    source = tmp_path / "profile.txt"
    source.write_bytes(b"new-complete")
    monkeypatch.setattr(
        providers,
        "native_browser_login",
        lambda: {
            "ok": True,
            "profile_dir": str(tmp_path / "profile"),
            "browser": "chrome",
        },
    )
    monkeypatch.setattr(
        providers,
        "_export_managed_profile",
        lambda *args, **kwargs: str(source),
    )

    class NoFallbackProvider:
        def __init__(self, *args, **kwargs):
            pass

        def acquire(self):
            return None

    monkeypatch.setattr(providers, "HeadlessProfileProvider", NoFallbackProvider)
    monkeypatch.setattr(
        cookies_auth,
        "build_default_manager",
        lambda output_dir=None: type(
            "Manager",
            (),
            {"get_cookiefile": lambda self: str(active)},
        )(),
    )
    monkeypatch.setattr(
        secureio.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("boom")),
    )
    engine = _FakeEngine(retry_result=["ok.mp4"])

    # The failed replacement leaves the complete prior jar available, so the
    # age-gate retry may still proceed with that prior session.
    assert yt_final._offer_auth_for_agegate(engine, _Spec()) is True
    assert active.read_bytes() == b"old-complete"
    assert engine.ran == [str(active)]


def test_skip_returns_false(monkeypatch):
    monkeypatch.setattr(yt_final._sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "S")
    eng = _FakeEngine(retry_result=["x"])
    assert yt_final._offer_auth_for_agegate(eng, _Spec()) is False
    assert eng.ran == []
