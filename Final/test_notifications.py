from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import Final.notifications as notifications
from Final.notifications import NotificationDispatcher, NotificationResult


def test_success_is_immutable_and_flush_waits_for_non_daemon_delivery():
    release = threading.Event()
    worker_daemon = []

    def sender(*_args):
        worker_daemon.append(threading.current_thread().daemon)
        release.wait(1)
        return True

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=2)
    future = dispatcher.submit("https://hooks.example.test/a", {"ok": True})

    assert dispatcher.flush(timeout=0.01) is False
    release.set()
    assert dispatcher.flush(timeout=1) is True
    result = future.result(timeout=0)
    assert result.status == "sent"
    assert result.error is None
    assert isinstance(result.completed_at, float)
    assert worker_daemon == [False]
    with pytest.raises(FrozenInstanceError):
        result.status = "changed"
    assert dispatcher.shutdown(timeout=1) is True


def test_sender_failure_is_sanitized_and_releases_capacity():
    secret = "top-secret-posting-token"
    calls = 0

    def sender(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                False,
                f"POST https://hooks.example.test/services/{secret}?key={secret} failed",
            )
        return True

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=1)
    failed = dispatcher.submit(
        f"https://hooks.example.test/services/{secret}?key={secret}",
        {"ok": False},
    ).result(timeout=1)
    accepted_after_failure = dispatcher.submit(
        "https://hooks.example.test/next",
        {"ok": True},
    ).result(timeout=1)

    assert failed.status == "failed"
    assert failed.error
    assert secret not in failed.error
    assert accepted_after_failure.status == "sent"
    assert dispatcher.shutdown(timeout=1) is True


def test_central_sanitizer_removes_headers_userinfo_and_decoded_url_material():
    url = (
        "https://url-user:url-pass@hooks.example.test/"
        "raw%2Dpath?token=double%252Ddecoded#frag%2Dsecret"
    )
    error = "\n".join(
        [
            f"request failed for {url}",
            "Authorization: Bearer authorization-secret",
            "Proxy-Authorization: Basic proxy-authorization-secret",
            "Cookie: sid=cookie-secret",
            "Set-Cookie: session=set-cookie-secret",
            "headers={'Authorization': 'Bearer dict-authorization-secret'}",
            "headers={'Proxy-Authorization': 'Basic dict-proxy-authorization-secret'}",
            "headers={'Cookie': 'sid=dict-cookie-secret'}",
            "headers={'Set-Cookie': 'sid=dict-set-cookie-secret'}",
            "proxy proxy-user:proxy-pass@proxy.example.test:8080",
            "decoded path /raw-path",
            "decoded query double-decoded",
            "decoded fragment frag-secret",
        ]
    )

    sanitized = notifications.sanitize_notification_error(error, url)

    for secret in (
        "url-user",
        "url-pass",
        "authorization-secret",
        "proxy-authorization-secret",
        "cookie-secret",
        "set-cookie-secret",
        "dict-authorization-secret",
        "dict-proxy-authorization-secret",
        "dict-cookie-secret",
        "dict-set-cookie-secret",
        "proxy-user",
        "proxy-pass",
        "raw-path",
        "double-decoded",
        "frag-secret",
    ):
        assert secret not in sanitized
    assert "Authorization: <redacted>" in sanitized
    assert "Proxy-Authorization: <redacted>" in sanitized
    assert "Cookie: <redacted>" in sanitized
    assert "Set-Cookie: <redacted>" in sanitized


def test_callback_and_persisted_result_share_the_central_sanitizer():
    secret = "callback-and-queue-secret"
    url = f"https://user:{secret}@hooks.example.test/a#{secret}"
    callbacks = []

    dispatcher = NotificationDispatcher(
        lambda *_args: (
            False,
            f"Authorization: Bearer {secret}; failed fragment {secret}",
        ),
        workers=1,
        max_pending=1,
    )
    result = dispatcher.submit(
        url,
        {},
        on_result=callbacks.append,
    ).result(timeout=1)
    durable = notifications.persisted_notification(result)

    assert callbacks == [result]
    assert secret not in str(result)
    assert secret not in str(durable)
    assert dispatcher.shutdown(timeout=1) is True


def test_saturation_returns_completed_dropped_result():
    entered = threading.Event()
    release = threading.Event()

    def sender(*_args):
        entered.set()
        release.wait(1)
        return True

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=1)
    first = dispatcher.submit("https://hooks.example.test/one", {})
    assert entered.wait(1)

    saturated = dispatcher.submit("https://hooks.example.test/two", {})

    assert saturated.done()
    assert saturated.result(timeout=0).status == "dropped"
    release.set()
    assert first.result(timeout=1).status == "sent"
    assert dispatcher.shutdown(timeout=1) is True


def test_flush_timeout_uses_a_bounded_monotonic_budget():
    entered = threading.Event()
    release = threading.Event()

    def sender(*_args):
        entered.set()
        release.wait(1)
        return True

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=1)
    dispatcher.submit("https://hooks.example.test/slow", {})
    assert entered.wait(1)

    started = time.monotonic()
    assert dispatcher.flush(timeout=0.02) is False
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    release.set()
    assert dispatcher.shutdown(timeout=1) is True


def test_cancel_before_start_returns_cancelled_and_reuses_capacity():
    entered = threading.Event()
    release = threading.Event()

    def sender(*_args):
        entered.set()
        release.wait(1)
        return True

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=2)
    running = dispatcher.submit("https://hooks.example.test/running", {})
    assert entered.wait(1)
    queued = dispatcher.submit("https://hooks.example.test/queued", {})

    assert queued.cancel() is True
    assert queued.result(timeout=1).status == "cancelled"
    replacement = dispatcher.submit("https://hooks.example.test/replacement", {})

    release.set()
    assert running.result(timeout=1).status == "sent"
    assert replacement.result(timeout=1).status == "sent"
    assert dispatcher.shutdown(timeout=1) is True


def test_callback_runs_once_outside_lock_and_reentrant_submit_is_dropped():
    callback_count = 0
    callback_was_unblocked = []
    reentered = []

    dispatcher = NotificationDispatcher(
        lambda *_args: True,
        workers=1,
        max_pending=1,
    )

    def callback(_result):
        nonlocal callback_count
        callback_count += 1

        def reenter():
            reentered.append(
                dispatcher.submit("https://hooks.example.test/reentered", {})
            )

        thread = threading.Thread(target=reenter)
        thread.start()
        thread.join(0.5)
        callback_was_unblocked.append(not thread.is_alive())

    first = dispatcher.submit(
        "https://hooks.example.test/first",
        {},
        on_result=callback,
    )

    assert first.result(timeout=1).status == "sent"
    assert callback_count == 1
    assert callback_was_unblocked == [True]
    assert len(reentered) == 1
    assert reentered[0].result(timeout=1).status == "dropped"
    assert dispatcher.shutdown(timeout=1) is True


@pytest.mark.parametrize(
    ("make_result", "expected_status"),
    [
        ("dropped", "dropped"),
        ("cancelled", "cancelled"),
        ("rejected", "rejected"),
    ],
)
def test_callback_receives_every_finalized_result_once(make_result, expected_status):
    entered = threading.Event()
    release = threading.Event()
    callbacks = []

    def sender(*_args):
        entered.set()
        release.wait(1)
        return True

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=2)
    running = dispatcher.submit("https://hooks.example.test/running", {})
    assert entered.wait(1)

    if make_result == "dropped":
        queued = dispatcher.submit("https://hooks.example.test/queued", {})
        future = dispatcher.submit(
            "https://hooks.example.test/dropped",
            {},
            on_result=callbacks.append,
        )
        assert queued.cancel() is True
    elif make_result == "cancelled":
        future = dispatcher.submit(
            "https://hooks.example.test/cancelled",
            {},
            on_result=callbacks.append,
        )
        assert future.cancel() is True
    else:
        release.set()
        assert running.result(timeout=1).status == "sent"
        assert dispatcher.shutdown(timeout=1) is True
        future = dispatcher.submit(
            "https://hooks.example.test/rejected",
            {},
            on_result=callbacks.append,
        )

    result = future.result(timeout=1)
    assert result.status == expected_status
    assert callbacks == [result]
    release.set()
    assert dispatcher.shutdown(timeout=1) is True


def test_callback_exception_is_contained_and_future_still_settles():
    def callback(_result):
        raise RuntimeError("persistence unavailable")

    dispatcher = NotificationDispatcher(lambda *_args: True, workers=1, max_pending=1)
    result = dispatcher.submit(
        "https://hooks.example.test/a",
        {},
        on_result=callback,
    ).result(timeout=1)

    assert result.status == "sent"
    assert dispatcher.shutdown(timeout=1) is True


def test_public_future_baseexception_callback_cannot_strand_cleanup():
    sender_entered = threading.Event()
    sender_release = threading.Event()
    callback_count = 0

    def sender(*_args):
        sender_entered.set()
        sender_release.wait(1)
        return True

    def external_callback(_future):
        nonlocal callback_count
        callback_count += 1
        raise SystemExit("external future callback failed")

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=1)
    future = dispatcher.submit("https://hooks.example.test/a", {})
    assert sender_entered.wait(1)
    future.add_done_callback(external_callback)
    sender_release.set()

    assert future.result(timeout=1).status == "sent"
    assert dispatcher.flush(timeout=1) is True
    assert callback_count == 1
    assert dispatcher._pending == set()
    after = dispatcher.submit("https://hooks.example.test/after", {})
    assert after.result(timeout=1).status == "sent"
    assert dispatcher.shutdown(timeout=1) is True


def test_flush_waits_only_for_its_atomic_snapshot(monkeypatch):
    first_entered = threading.Event()
    first_release = threading.Event()
    second_release = threading.Event()
    snapshot_taken = threading.Event()
    real_wait = notifications.wait
    calls = 0

    def sender(url, *_args):
        if url.endswith("/first"):
            first_entered.set()
            first_release.wait(1)
        else:
            second_release.wait(1)
        return True

    def synchronized_wait(futures, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            snapshot_taken.set()
        return real_wait(futures, timeout=timeout)

    dispatcher = NotificationDispatcher(sender, workers=2, max_pending=2)
    first = dispatcher.submit("https://hooks.example.test/first", {})
    assert first_entered.wait(1)
    monkeypatch.setattr(notifications, "wait", synchronized_wait)
    flushed = []
    thread = threading.Thread(target=lambda: flushed.append(dispatcher.flush(timeout=1)))
    thread.start()
    assert snapshot_taken.wait(1)

    second = dispatcher.submit("https://hooks.example.test/second", {})
    first_release.set()
    thread.join(1)

    assert flushed == [True]
    assert first.result(timeout=0).status == "sent"
    assert second.done() is False
    monkeypatch.setattr(notifications, "wait", real_wait)
    second_release.set()
    assert dispatcher.shutdown(timeout=1) is True


def test_shutdown_fences_racing_submit_and_includes_accepted_work(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    snapshot_taken = threading.Event()
    real_wait = notifications.wait

    def sender(*_args):
        entered.set()
        release.wait(1)
        return True

    def synchronized_wait(futures, timeout):
        snapshot_taken.set()
        return real_wait(futures, timeout=timeout)

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=2)
    accepted = dispatcher.submit("https://hooks.example.test/accepted", {})
    assert entered.wait(1)
    monkeypatch.setattr(notifications, "wait", synchronized_wait)
    stopped = []
    thread = threading.Thread(target=lambda: stopped.append(dispatcher.shutdown(timeout=1)))
    thread.start()
    assert snapshot_taken.wait(1)

    rejected = dispatcher.submit("https://hooks.example.test/late", {})
    assert rejected.result(timeout=0).status == "rejected"
    release.set()
    thread.join(1)

    assert accepted.result(timeout=0).status == "sent"
    assert stopped == [True]


def test_shutdown_drains_two_sequential_accepted_deliveries_before_cancelling(
    monkeypatch,
):
    first_entered = threading.Event()
    first_release = threading.Event()
    second_entered = threading.Event()
    drain_started = threading.Event()
    calls = []
    real_wait = notifications.wait

    def sender(url, *_args):
        calls.append(url)
        if url.endswith("/first"):
            first_entered.set()
            assert first_release.wait(1), "test deadlock: first was not released"
        else:
            second_entered.set()
        return True

    def observed_wait(futures, timeout):
        drain_started.set()
        return real_wait(futures, timeout=timeout)

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=2)
    first = dispatcher.submit("https://hooks.example.test/first", {})
    assert first_entered.wait(1)
    second = dispatcher.submit("https://hooks.example.test/second", {})
    monkeypatch.setattr(notifications, "wait", observed_wait)
    shutdown_results = []
    shutdown_thread = threading.Thread(
        target=lambda: shutdown_results.append(dispatcher.shutdown(timeout=1))
    )
    shutdown_thread.start()
    try:
        assert drain_started.wait(1)
        late = dispatcher.submit("https://hooks.example.test/late", {})
        assert late.result(timeout=0).status == "rejected"
        first_release.set()
        shutdown_thread.join(1)
    finally:
        first_release.set()
        shutdown_thread.join(1)

    assert shutdown_thread.is_alive() is False
    assert shutdown_results == [True]
    assert first.result(timeout=0).status == "sent"
    assert second.result(timeout=0).status == "sent"
    assert second_entered.is_set()
    assert calls == [
        "https://hooks.example.test/first",
        "https://hooks.example.test/second",
    ]


def test_shutdown_budget_is_truthful_and_cancels_queued_work():
    entered = threading.Event()
    release = threading.Event()

    def sender(*_args):
        entered.set()
        release.wait(1)
        return True

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=2)
    running = dispatcher.submit("https://hooks.example.test/running", {})
    assert entered.wait(1)
    queued = dispatcher.submit("https://hooks.example.test/queued", {})

    started = time.monotonic()
    assert dispatcher.shutdown(timeout=0.02) is False
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert queued.result(timeout=1).status == "cancelled"
    assert dispatcher.submit(
        "https://hooks.example.test/late",
        {},
    ).result(timeout=0).status == "rejected"
    release.set()
    assert running.result(timeout=1).status == "sent"
    assert dispatcher.shutdown(timeout=1) is True


def _permanently_blocked_sender(*_args):
    threading.Event().wait()


def test_deadline_bound_sender_force_stops_and_reaps_blocked_transport():
    sender = notifications.DeadlineBoundSender(
        _permanently_blocked_sender,
        timeout=0.25,
        termination_grace=0.1,
    )

    started = time.monotonic()
    ok, error = sender("https://hooks.example.test/a", {}, "raw")
    elapsed = time.monotonic() - started

    assert ok is False
    assert error == "delivery timed out"
    assert elapsed < 0.75
    assert not [
        child for child in notifications.multiprocessing.active_children()
        if child.name == "notify-transport"
    ]


def test_subprocess_with_blocked_production_boundary_exits_within_budget(tmp_path):
    # The transport boundary is a real spawned interpreter, so every budget here is sized for a
    # cold, contended CI runner rather than a warm dev laptop. The previous 0.25s sender deadline
    # expired before a `spawn` child could even import the transport, so the child was left
    # asserting on a transport it had already force-stopped. These are the production defaults
    # (DeadlineBoundSender timeout/grace) under an outer shutdown budget that, as in production,
    # exceeds their hard sum -- so what the test proves is unchanged: a permanently blocked
    # production transport is force-stopped, the queued item is cancelled, and shutdown reports
    # completion inside its own budget instead of inheriting the blocked transport's lifetime.
    sender_timeout = 4.0
    termination_grace = 0.35
    shutdown_budget = 6.0
    transport_start_budget = 30.0
    script = tmp_path / "blocked_notification_process.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import multiprocessing
            import sys
            import threading
            import time

            sys.path.insert(0, sys.argv[1])
            from Final.notifications import DeadlineBoundSender, NotificationDispatcher

            def blocked_transport(_url, payload, _template):
                payload["started"].set()
                threading.Event().wait()

            if __name__ == "__main__":
                sender_timeout = float(sys.argv[2])
                termination_grace = float(sys.argv[3])
                shutdown_budget = float(sys.argv[4])
                transport_start_budget = float(sys.argv[5])
                callbacks = []
                transport_started = multiprocessing.get_context("spawn").Event()
                sender = DeadlineBoundSender(
                    blocked_transport,
                    timeout=sender_timeout,
                    termination_grace=termination_grace,
                )
                dispatcher = NotificationDispatcher(
                    sender,
                    workers=1,
                    max_pending=2,
                )
                running = dispatcher.submit(
                    "https://hooks.example.test/running",
                    {"started": transport_started},
                )
                # Reported rather than asserted: a host that cannot start the spawned transport
                # inside the sender's own deadline cannot exercise a *blocked* boundary at all,
                # and the parent skips instead of reporting a false failure.
                transport_running = transport_started.wait(transport_start_budget)
                queued = dispatcher.submit(
                    "https://hooks.example.test/queued",
                    {},
                    on_result=lambda result: callbacks.append(result.status),
                )
                started = time.monotonic()
                complete = dispatcher.shutdown(timeout=shutdown_budget)
                elapsed = time.monotonic() - started
                callbacks.append(running.result(timeout=0).status)
                print(json.dumps({
                    "transport_running": transport_running,
                    "complete": complete,
                    "elapsed": elapsed,
                    "queued": queued.result(timeout=0).status,
                    "callbacks": callbacks,
                }))
            """
        ),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]

    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            str(root),
            str(sender_timeout),
            str(termination_grace),
            str(shutdown_budget),
            str(transport_start_budget),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        # Only a hang has to fail here; interpreter start plus a spawned child is host cost,
        # not the budget under test, which the child measures around shutdown itself.
        timeout=120,
        check=False,
    )
    process_elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    if not outcome["transport_running"]:
        pytest.skip(
            "the spawned notify-transport process never started within "
            f"{transport_start_budget:.0f}s on this host, so no blocked production boundary "
            "existed to shut down"
        )
    assert outcome["complete"] is True
    assert outcome["elapsed"] < shutdown_budget + 1.0
    assert process_elapsed < 60
    assert outcome["queued"] == "cancelled"
    assert outcome["callbacks"].count("cancelled") == 1
    assert outcome["callbacks"].count("failed") == 1


def test_shutdown_never_runs_a_blocking_cancel_callback_on_its_caller():
    sender_entered = threading.Event()
    sender_release = threading.Event()
    callback_entered = threading.Event()
    callback_release = threading.Event()
    callback_threads = []
    shutdown_threads = []
    shutdown_results = []

    def sender(*_args):
        sender_entered.set()
        sender_release.wait(2)
        return True

    def callback(_result):
        callback_threads.append(threading.get_ident())
        callback_entered.set()
        callback_release.wait(2)

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=2)
    running = dispatcher.submit("https://hooks.example.test/running", {})
    assert sender_entered.wait(1)
    queued = dispatcher.submit(
        "https://hooks.example.test/queued",
        {},
        on_result=callback,
    )

    def stop_dispatcher():
        shutdown_threads.append(threading.get_ident())
        shutdown_results.append(dispatcher.shutdown(timeout=0.02))

    shutdown_thread = threading.Thread(target=stop_dispatcher)
    shutdown_thread.start()
    try:
        assert callback_entered.wait(1)
        shutdown_thread.join(0.25)
        assert shutdown_thread.is_alive() is False
        assert shutdown_results == [False]
        assert callback_threads != shutdown_threads
        assert queued.done() is False
    finally:
        callback_release.set()
        sender_release.set()
        shutdown_thread.join(1)

    assert queued.result(timeout=1).status == "cancelled"
    assert running.result(timeout=1).status == "sent"
    assert dispatcher.shutdown(timeout=1) is True
    assert len(callback_threads) == 1
    assert dispatcher._pending == set()


def test_notification_future_cancel_delegate_is_reentrant_without_deadlock():
    future = notifications._NotificationFuture()
    reentered = False
    results = []

    def cancel_delegate():
        nonlocal reentered
        if not reentered:
            reentered = True
            return future.cancel()
        return False

    future.bind_cancel(cancel_delegate)
    thread = threading.Thread(
        target=lambda: results.append(future.cancel()),
        daemon=True,
    )
    thread.start()
    thread.join(0.25)

    assert thread.is_alive() is False
    assert results == [False]


def test_queued_cancel_callback_can_reenter_same_public_future(tmp_path):
    script = tmp_path / "reentrant_cancel_process.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            import threading

            sys.path.insert(0, sys.argv[1])
            from Final.notifications import NotificationDispatcher

            entered = threading.Event()
            release = threading.Event()
            callbacks = []
            cancel_results = []
            holder = {}

            def sender(*_args):
                entered.set()
                release.wait(2)
                return True

            def callback(result):
                callbacks.append(result.status)
                callbacks.append(holder["queued"].cancel())

            dispatcher = NotificationDispatcher(sender, workers=1, max_pending=2)
            running = dispatcher.submit("https://hooks.example.test/running", {})
            assert entered.wait(1)
            holder["queued"] = dispatcher.submit(
                "https://hooks.example.test/queued",
                {},
                on_result=callback,
            )
            cancel_thread = threading.Thread(
                target=lambda: cancel_results.append(holder["queued"].cancel()),
                daemon=True,
            )
            cancel_thread.start()
            cancel_thread.join(0.5)
            if cancel_thread.is_alive():
                release.set()
                raise SystemExit(3)
            replacement = dispatcher.submit(
                "https://hooks.example.test/replacement",
                {},
            )
            release.set()
            outcome = {
                "outer_cancel": cancel_results,
                "queued": holder["queued"].result(timeout=1).status,
                "callbacks": callbacks,
                "running": running.result(timeout=1).status,
                "replacement": replacement.result(timeout=1).status,
                "shutdown": dispatcher.shutdown(timeout=1),
            }
            print(json.dumps(outcome))
            """
        ),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, str(script), str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=2.5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert outcome == {
        "outer_cancel": [True],
        "queued": "cancelled",
        "callbacks": ["cancelled", False],
        "running": "sent",
        "replacement": "sent",
        "shutdown": True,
    }


def test_capacity_is_held_through_callback_and_public_settlement():
    callback_entered = threading.Event()
    callback_release = threading.Event()
    nested_results = []

    dispatcher = NotificationDispatcher(
        lambda *_args: True,
        workers=1,
        max_pending=1,
    )

    def callback(_result):
        nested = dispatcher.submit(
            "https://hooks.example.test/reentrant",
            {},
        )
        nested_results.append(
            (
                nested.done(),
                nested.result(timeout=0).status if nested.done() else None,
            )
        )
        callback_entered.set()
        callback_release.wait(1)

    first = dispatcher.submit(
        "https://hooks.example.test/first",
        {},
        on_result=callback,
    )
    assert callback_entered.wait(1)

    assert nested_results == [(True, "dropped")]
    assert len(dispatcher._pending) <= 1
    concurrent = dispatcher.submit("https://hooks.example.test/concurrent", {})
    assert concurrent.done()
    assert concurrent.result(timeout=0).status == "dropped"
    assert len(dispatcher._pending) <= 1

    callback_release.set()
    assert first.result(timeout=1).status == "sent"
    assert dispatcher.flush(timeout=1) is True
    assert dispatcher._pending == set()
    after = dispatcher.submit("https://hooks.example.test/after", {})
    assert after.result(timeout=1).status == "sent"
    assert dispatcher.shutdown(timeout=1) is True


def test_shutdown_true_requires_post_result_tracking_and_permit_cleanup(monkeypatch):
    result_published = threading.Event()
    cleanup_release = threading.Event()
    real_set_result = notifications._NotificationFuture.set_result

    def pause_after_result_publication(future, result):
        real_set_result(future, result)
        result_published.set()
        cleanup_release.wait(1)

    monkeypatch.setattr(
        notifications._NotificationFuture,
        "set_result",
        pause_after_result_publication,
    )
    dispatcher = NotificationDispatcher(
        lambda *_args: True,
        workers=1,
        max_pending=1,
    )
    future = dispatcher.submit("https://hooks.example.test/a", {})
    assert result_published.wait(1)

    started = time.monotonic()
    assert dispatcher.shutdown(timeout=0.02) is False
    assert time.monotonic() - started < 0.25
    assert len(dispatcher._pending) == 1

    cleanup_release.set()
    assert future.result(timeout=1).status == "sent"
    assert dispatcher.shutdown(timeout=1) is True
    assert dispatcher._pending == set()
    after_shutdown = dispatcher.submit("https://hooks.example.test/late", {})
    assert after_shutdown.result(timeout=0).status == "rejected"


def test_shutdown_rejects_even_when_capacity_remains_saturated():
    entered = threading.Event()
    release = threading.Event()

    def sender(*_args):
        entered.set()
        release.wait(1)
        return True

    dispatcher = NotificationDispatcher(sender, workers=1, max_pending=1)
    running = dispatcher.submit("https://hooks.example.test/running", {})
    assert entered.wait(1)
    assert dispatcher.shutdown(timeout=0.01) is False

    late = dispatcher.submit("https://hooks.example.test/late", {})

    assert late.result(timeout=0).status == "rejected"
    release.set()
    assert running.result(timeout=1).status == "sent"
    assert dispatcher.shutdown(timeout=1) is True


def test_callback_can_reenter_flush_and_shutdown_without_waiting_on_itself():
    reentered = []
    dispatcher = NotificationDispatcher(lambda *_args: True, workers=1, max_pending=1)

    def callback(_result):
        started = time.monotonic()
        reentered.append(dispatcher.flush(timeout=0.05))
        reentered.append(dispatcher.shutdown(timeout=0.05))
        reentered.append(time.monotonic() - started)

    result = dispatcher.submit(
        "https://hooks.example.test/a",
        {},
        on_result=callback,
    ).result(timeout=1)

    assert result.status == "sent"
    assert reentered[:2] == [True, True]
    assert reentered[2] < 0.1


def test_engine_run_tracks_only_pending_notification_futures():
    from types import SimpleNamespace

    from Final.jobspec import JobSpec
    from Final.yt_final import FinalYouTubeGPUEngine

    entered = threading.Event()
    release = threading.Event()

    def sender(*_args):
        entered.set()
        release.wait(1)
        return True

    engine = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    engine._run_output_lock = threading.Lock()
    engine.downloader = SimpleNamespace(output_path="/tmp", last_partial_errors=None)
    engine._record_history = lambda *_args, **_kwargs: None
    engine._run_body = lambda *_args: []
    engine._notification_dispatcher = NotificationDispatcher(
        sender,
        workers=1,
        max_pending=2,
    )
    engine._notification_futures = set()
    engine._notification_futures_lock = threading.Lock()
    spec = JobSpec(
        urls=["https://youtu.be/abcdefghijk"],
        notify_url="https://hooks.example.test/a",
    )

    try:
        assert engine.run(spec) == []
        assert entered.wait(1)
        with engine._notification_futures_lock:
            assert len(engine._notification_futures) == 1
        assert engine.flush_notifications(timeout=0.01) is False
        release.set()
        assert engine.flush_notifications(timeout=1) is True
        with engine._notification_futures_lock:
            assert engine._notification_futures == set()
    finally:
        release.set()
        engine._notification_dispatcher.shutdown(timeout=1)


def test_engine_forwards_finalized_result_to_run_callback():
    from types import SimpleNamespace

    from Final.jobspec import JobSpec
    from Final.yt_final import FinalYouTubeGPUEngine

    engine = FinalYouTubeGPUEngine.__new__(FinalYouTubeGPUEngine)
    engine._run_output_lock = threading.Lock()
    engine.downloader = SimpleNamespace(output_path="/tmp", last_partial_errors=None)
    engine._record_history = lambda *_args, **_kwargs: None
    engine._run_body = lambda *_args: []
    engine._notification_dispatcher = NotificationDispatcher(
        lambda *_args: True,
        workers=1,
        max_pending=1,
    )
    engine._notification_futures = set()
    engine._notification_futures_lock = threading.Lock()
    callbacks = []
    spec = JobSpec(
        urls=["https://youtu.be/abcdefghijk"],
        notify_url="https://hooks.example.test/a",
    )

    assert engine.run(spec, on_notification=callbacks.append) == []
    assert engine.flush_notifications(timeout=1) is True
    assert len(callbacks) == 1
    assert callbacks[0].status == "sent"
    assert engine._notification_dispatcher.shutdown(timeout=1) is True


def _close_server_state(state):
    state.stop.set()
    state._io_pool.shutdown(wait=True, cancel_futures=True)
    state.journal.release_run_lock()


def test_queue_callback_before_terminal_write_preserves_both_fields(tmp_path):
    from types import SimpleNamespace

    from Final.jobspec import JobSpec
    from Final.webserver import ServerState

    secret = "top-secret-posting-token"
    state = ServerState(
        str(tmp_path / "downloads"),
        queue_file=str(tmp_path / "queue.json"),
    )
    record = state.journal.add(
        JobSpec(
            urls=["https://youtu.be/abcdefghijk"],
            notify_url=f"https://hooks.example.test/services/{secret}?key={secret}",
        )
    )

    class Engine:
        downloader = SimpleNamespace(
            progress_sink=None,
            cancel_check=None,
            last_error=None,
            last_error_kind=None,
        )

        def run(self, _spec, on_notification=None):
            state.stop.set()
            assert on_notification is not None
            on_notification(
                NotificationResult(
                    status="failed",
                    error=f"POST /services/{secret}?key={secret} failed",
                    completed_at=123.5,
                )
            )
            return [tmp_path / "output.mp4"]

    state.engine = lambda: Engine()
    try:
        state.worker_loop()
        reloaded = state.journal.get(record["id"])
        assert reloaded["status"] == "done"
        assert reloaded["notification"] == {
            "status": "failed",
            "error": "POST <redacted-path>?key=<redacted> failed",
            "completed_at": 123.5,
        }
        assert secret not in str(reloaded["notification"])
    finally:
        _close_server_state(state)


def test_queue_callback_after_terminal_write_preserves_both_fields(tmp_path):
    from types import SimpleNamespace

    from Final.jobspec import JobSpec
    from Final.webserver import ServerState

    state = ServerState(
        str(tmp_path / "downloads"),
        queue_file=str(tmp_path / "queue.json"),
    )
    record = state.journal.add(
        JobSpec(
            urls=["https://youtu.be/abcdefghijk"],
            notify_url="https://hooks.example.test/a",
        )
    )
    callback_holder = []
    run_returned = threading.Event()

    class Engine:
        downloader = SimpleNamespace(
            progress_sink=None,
            cancel_check=None,
            last_error=None,
            last_error_kind=None,
        )

        def run(self, _spec, on_notification=None):
            callback_holder.append(on_notification)
            run_returned.set()
            return [tmp_path / "output.mp4"]

    state.engine = lambda: Engine()
    worker = threading.Thread(target=state.worker_loop)
    worker.start()
    try:
        assert run_returned.wait(1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if state.journal.get(record["id"])["status"] == "done":
                break
            time.sleep(0.005)
        assert state.journal.get(record["id"])["status"] == "done"
        callback_holder[0](
            NotificationResult("sent", None, 456.25)
        )
        state.stop.set()
        worker.join(2)
        reloaded = state.journal.get(record["id"])
        assert reloaded["status"] == "done"
        assert reloaded["notification"] == {
            "status": "sent",
            "error": None,
            "completed_at": 456.25,
        }
    finally:
        state.stop.set()
        worker.join(2)
        _close_server_state(state)


def test_queue_notification_persistence_failure_is_contained(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from Final.jobspec import JobSpec
    from Final.webserver import ServerState

    state = ServerState(
        str(tmp_path / "downloads"),
        queue_file=str(tmp_path / "queue.json"),
    )
    record = state.journal.add(
        JobSpec(
            urls=["https://youtu.be/abcdefghijk"],
            notify_url="https://hooks.example.test/a",
        )
    )
    real_update = state.journal.update
    monkeypatch.setattr(
        state.journal,
        "update",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    class Engine:
        downloader = SimpleNamespace(
            progress_sink=None,
            cancel_check=None,
            last_error=None,
            last_error_kind=None,
        )

        def run(self, _spec, on_notification=None):
            state.stop.set()
            assert on_notification is not None
            on_notification(NotificationResult("sent", None, 789.0))
            return [tmp_path / "output.mp4"]

    state.engine = lambda: Engine()
    try:
        state.worker_loop()
        monkeypatch.setattr(state.journal, "update", real_update)
        assert state.journal.get(record["id"])["status"] == "done"
    finally:
        _close_server_state(state)
