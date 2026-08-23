"""Web notify: SSRF gate + jobspec sanitize + template guess (no real network POSTs)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Match test_yt_final: package-style Final.* needs workspace root on sys.path
_here = Path(__file__).resolve()
_root = _here.parents[1] if _here.parent.name == 'Final' else _here.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from Final.yt_final import FinalYouTubeGPUEngine as E
import Final.notifications as notifications
import Final.yt_final as yt_module
from Final.webserver import _web_jobspec_from_payload, _notify_test


def test_validate_notify_rejects_ssrf():
    for bad in (
        'file:///etc/passwd',
        'http://127.0.0.1:8765/',
        'http://169.254.169.254/latest/meta-data/',
        'http://10.0.0.1/',
        'http://localhost/',
        'ftp://x/y',
        '',
    ):
        err = E.validate_notify_url(bad)
        assert err, f'expected reject for {bad!r}'


def test_guess_notify_template():
    assert E.guess_notify_template('https://discord.com/api/webhooks/1/abc') == 'discord'
    assert E.guess_notify_template('https://discordapp.com/api/webhooks/1/abc') == 'discord'
    assert E.guess_notify_template('https://hooks.slack.com/services/T/B/X') == 'slack'
    assert E.guess_notify_template('https://ntfy.sh/mytopic') == 'ntfy'
    assert E.guess_notify_template('https://example.com/hooks/generic') == 'raw'


def test_web_jobspec_strips_shell_allows_empty_notify():
    spec = _web_jobspec_from_payload({
        'urls': ['https://youtu.be/x'],
        'on_complete': 'rm -rf /',
        'notify_url': '',
    })
    assert spec.on_complete is None
    assert not spec.notify_url


def test_web_jobspec_rejects_private_notify():
    with pytest.raises(ValueError, match='private|loopback|scheme|resolve'):
        _web_jobspec_from_payload({
            'urls': ['https://youtu.be/x'],
            'notify_url': 'http://127.0.0.1/hook',
        })


def test_web_jobspec_auto_template_discord(monkeypatch):
    # Skip DNS SSRF resolution for a public-looking discord URL
    monkeypatch.setattr(E, 'validate_notify_url', staticmethod(lambda u: None))
    spec = _web_jobspec_from_payload({
        'urls': ['https://youtu.be/x'],
        'notify_url': 'https://discord.com/api/webhooks/1/token',
        'notify_template': 'auto',
    })
    assert spec.notify_url.startswith('https://discord.com/')
    assert spec.notify_template == 'discord'
    assert spec.on_complete is None


def test_notify_test_rejects_private():
    r = _notify_test('http://127.0.0.1/x')
    assert r['ok'] is False
    assert r.get('error')


def test_notify_test_ok_false_when_post_fails(monkeypatch):
    """UI Test button must not claim success if the HTTP POST fails."""
    import urllib.request

    class _BoomOpener:
        def open(self, *a, **k):
            raise OSError('connection refused')

    monkeypatch.setattr(E, 'validate_notify_url', staticmethod(lambda u: None))
    monkeypatch.setattr(E, '_vet_notify_url',
                        staticmethod(lambda _u: (None, '93.184.216.34')))
    monkeypatch.setattr(
        urllib.request, 'build_opener',
        lambda *a, **k: _BoomOpener(),
    )
    monkeypatch.setattr(
        E,
        "_fire_webhook_with_deadline",
        staticmethod(lambda *_args, **_kwargs: (False, "connection refused")),
        raising=False,
    )
    r = _notify_test('https://example.com/hook', template='raw')
    assert r['ok'] is False, r
    assert r.get('error')
    assert 'connection refused' in str(r.get('error')).lower() or 'fail' in str(r.get('error')).lower()


def test_notify_test_success_exposes_only_the_destination_origin(monkeypatch):
    secret = "notify-test-success-secret"
    url = (
        f"https://api-user:{secret}@hooks.example.test:8443/"
        f"services/{secret}?token={secret}#{secret}"
    )
    monkeypatch.setattr(
        E,
        "validate_notify_url",
        staticmethod(lambda _url: pytest.fail(
            "notify-test performed DNS outside the production deadline"
        )),
    )
    monkeypatch.setattr(
        E,
        "_fire_webhook_with_deadline",
        staticmethod(lambda *_args, **_kwargs: (True, None)),
        raising=False,
    )
    monkeypatch.setattr(
        E,
        "_fire_webhook",
        staticmethod(lambda *_args, **_kwargs: pytest.fail(
            "notify-test bypassed the production deadline"
        )),
    )

    response = _notify_test(url, template="raw")

    assert response["ok"] is True
    assert response["message"] == (
        "Test raw POST sent to https://hooks.example.test:8443"
    )
    assert secret not in str(response)
    assert "api-user" not in str(response)


def test_production_webhook_deadline_is_strictly_below_shutdown_budget():
    sender = E._production_notification_sender()

    # The suite exercises both supported import modes (`Final.yt_final` and
    # top-level `yt_final`), which can legitimately load the same class under
    # two module identities.
    assert type(sender).__name__ == notifications.DeadlineBoundSender.__name__
    assert (
        sender.timeout + sender.termination_grace
        < yt_module.NOTIFICATION_SHUTDOWN_TIMEOUT_S
    )


def test_notify_test_sanitizes_transport_errors_and_outer_exceptions(monkeypatch):
    secret = "notify-test-boundary-secret"
    url = f"https://hooks.example.test/services/{secret}?token={secret}"

    monkeypatch.setattr(
        E,
        "_fire_webhook_with_deadline",
        staticmethod(
            lambda *_args: (
                False,
                f"Authorization: Bearer {secret}; failed URL {url}"
            )
        ),
    )
    transport_response = _notify_test(url, template="raw")

    monkeypatch.setattr(
        E,
        "_fire_webhook_with_deadline",
        staticmethod(lambda *_args: (_ for _ in ()).throw(
            RuntimeError(f"Cookie: sid={secret}; failed URL {url}")
        )),
    )
    exception_response = _notify_test(url, template="raw")

    assert transport_response["ok"] is False
    assert exception_response["ok"] is False
    assert secret not in str(transport_response)
    assert secret not in str(exception_response)


def test_fire_webhook_returns_false_on_network_error(monkeypatch):
    import urllib.request

    class _BoomOpener:
        def open(self, *a, **k):
            raise OSError('connection refused')

    monkeypatch.setattr(E, 'validate_notify_url', staticmethod(lambda u: None))
    monkeypatch.setattr(E, '_vet_notify_url',
                        staticmethod(lambda _u: (None, '93.184.216.34')))
    monkeypatch.setattr(
        urllib.request, 'build_opener',
        lambda *a, **k: _BoomOpener(),
    )
    ok, err = E._fire_webhook('https://example.com/hook', {'ok': True, 'title': 't'}, 'raw')
    assert ok is False
    assert err and 'connection refused' in err.lower()


def test_fire_webhook_sanitizes_pre_open_exception_boundary(monkeypatch, caplog):
    import logging

    secret = "pre-open-secret"
    url = f"https://hooks.example.test/services/{secret}?token={secret}"
    monkeypatch.setattr(
        E,
        "_vet_notify_url",
        staticmethod(lambda _url: (None, "93.184.216.34")),
    )
    monkeypatch.setattr(
        E,
        "_build_notify_request",
        staticmethod(lambda *_args: (_ for _ in ()).throw(
            ValueError(
                f"Proxy-Authorization: Basic {secret}; failed URL {url}"
            )
        )),
    )

    with caplog.at_level(logging.WARNING):
        ok, error = E._fire_webhook(url, {"ok": True}, "raw")

    assert ok is False
    assert error
    assert secret not in error
    assert secret not in "\n".join(
        record.getMessage() for record in caplog.records
    )


def test_fire_webhook_logs_only_redacted_destination_on_success(monkeypatch, caplog):
    import logging
    import urllib.request

    secret = "top-secret-posting-token"
    monkeypatch.setattr(E, "_vet_notify_url", staticmethod(lambda _u: (None, "93.184.216.34")))
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_a, **_k: type("_Opener", (), {"open": lambda self, *_a, **_k: object()})(),
    )
    with caplog.at_level(logging.INFO):
        ok, err = E._fire_webhook(
            f"https://hooks.example:8443/services/{secret}?key={secret}",
            {"ok": True},
            "raw",
        )
    assert ok is True and err is None
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "https://hooks.example:8443" in messages
    assert secret not in messages


def test_fire_webhook_failure_exception_cannot_leak_secret_to_log_or_api(monkeypatch, caplog):
    import logging
    import urllib.request

    secret = "top-secret-posting-token"

    class _BoomOpener:
        def open(self, *_a, **_k):
            raise OSError(f"failed URL /services/{secret}")

    monkeypatch.setattr(E, "_vet_notify_url", staticmethod(lambda _u: (None, "93.184.216.34")))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a, **_k: _BoomOpener())
    with caplog.at_level(logging.WARNING):
        ok, err = E._fire_webhook(
            f"https://hooks.example/services/{secret}",
            {"ok": True},
            "raw",
        )
    assert ok is False and err
    assert secret not in err
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "https://hooks.example" in messages
    assert "OSError" in messages
    assert secret not in messages

    monkeypatch.setattr(
        E,
        "_fire_webhook_with_deadline",
        staticmethod(lambda *_args, **_kwargs: (False, err)),
        raising=False,
    )
    response = _notify_test(
        f"https://hooks.example/services/{secret}",
        template="raw",
    )
    assert response["ok"] is False
    assert secret not in str(response.get("error"))


def test_fire_webhook_uses_five_second_request_timeout(monkeypatch):
    import urllib.request

    timeouts = []

    class _Opener:
        def open(self, _request, timeout):
            timeouts.append(timeout)
            return object()

    monkeypatch.setattr(
        E,
        "_vet_notify_url",
        staticmethod(lambda _url: (None, "93.184.216.34")),
    )
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: _Opener())

    ok, err = E._fire_webhook(
        "https://hooks.example/services/redacted",
        {"ok": True},
        "raw",
    )

    assert ok is True and err is None
    assert timeouts == [5.0]
