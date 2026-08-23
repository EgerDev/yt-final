"""Shutdown must not orphan child processes on Windows.

Confirmed Windows finding: the queue worker runs as a daemon thread and serve()'s finally never
joined it, so on Ctrl+C / window-close the interpreter finalized and killed the worker MID-ENCODE.
That skips _run_ffmpeg's `finally` (yt_final.py ~2194), which is the only thing that force-kills and
reaps the ffmpeg child — and because children are spawned with CREATE_NO_WINDOW they are detached
from the console, so a console Ctrl+C cannot reach them either (verified with
GetConsoleProcessList: the parent pid is ABSENT from the child's console process list). Net effect
on Windows: a stray ffmpeg.exe keeps running after the app exits.

Fix has two halves, both asserted here:
  1. the per-job cancel probe also fires when the server is stopping, so an in-flight
     download/encode aborts within ~0.5s (the engine polls it every 0.3s) and unwinds its finally;
  2. serve()'s finally JOINS the worker (bounded) so that unwinding actually happens before exit.
"""
import inspect
import sys
from types import SimpleNamespace

import pytest
import webserver


def test_job_cancel_probe_fires_on_server_stop(monkeypatch, tmp_path):
    """The probe handed to a running job must report 'cancelled' once the server is shutting down,
    even while the journal itself says the job is fine — that is what lets the encode's finally run
    (and kill ffmpeg) instead of being torn down as a daemon thread at interpreter exit."""
    monkeypatch.setenv("YT_FINAL_HOME", str(tmp_path))
    st = webserver.ServerState(str(tmp_path / "dl"), queue_file=str(tmp_path / "queue.json"))
    try:
        # Journal says "not cancelled" for the whole test; only the stop event should flip the probe.
        monkeypatch.setattr(st.journal, "cancel_probe", lambda jid, **kw: (lambda: False))
        probe = st.job_cancel_probe("j_whatever")
        assert probe() is False, "must not report cancel while the server is running"
        st.stop.set()
        assert probe() is True, "must report cancel once the server is stopping"
    finally:
        st.stop.set()
        try:
            st.journal.release_run_lock()
        except Exception:
            pass


def test_running_job_gets_the_stop_aware_probe():
    """worker_loop must hand the job the STOP-AWARE probe, not the bare journal probe — otherwise
    half 1 of the fix is wired up but never used."""
    src = inspect.getsource(webserver.ServerState.worker_loop)
    assert "job_cancel_probe(" in src, "worker_loop must use the stop-aware probe"


def test_serve_joins_the_worker_before_exit():
    """serve()'s finally must join the worker (bounded) so an in-flight encode unwinds its finally
    and reaps ffmpeg, instead of dying as a daemon thread. Unbounded would hang shutdown, so the
    join must carry a timeout."""
    src = inspect.getsource(webserver.serve)
    helper = "_join_worker_and_shutdown_notifications("
    assert helper in src, "serve() must run the bounded worker/notification teardown"
    # and the stop event must be set before joining, else the join waits for the whole queue
    assert src.index("stop.set()") < src.index(helper), "set stop before joining"


class _CliBaseException(BaseException):
    pass


@pytest.mark.parametrize("command", ["download", "batch", "queue-run", "redo"])
@pytest.mark.parametrize("outcome", ["success", "error", "base-exception"])
def test_cli_execution_paths_shutdown_notification_dispatcher_in_finally(
    monkeypatch,
    tmp_path,
    command,
    outcome,
):
    import yt_final
    from jobspec import JobSpec
    from jobstore import DownloadJournal

    instances = []

    class FakeEngine:
        def __init__(self, *_args, **_kwargs):
            self.downloader = SimpleNamespace(
                last_error=None,
                last_error_kind=None,
                cancel_check=None,
            )
            self.shutdown_calls = []
            instances.append(self)

        def run(self, _spec):
            if outcome == "error":
                raise RuntimeError("engine failed")
            if outcome == "base-exception":
                raise _CliBaseException("stop now")
            return [tmp_path / "output.mp4"]

        def shutdown_notifications(self, timeout=5.0):
            self.shutdown_calls.append(timeout)
            return True

    monkeypatch.setattr(yt_final, "FinalYouTubeGPUEngine", FakeEngine)
    monkeypatch.setattr(yt_final, "_discover_cookiefile", lambda **_kwargs: None)
    argv = [
        "yt_final.py",
        "--no-update-check",
        "--output-dir",
        str(tmp_path / "downloads"),
    ]
    if command == "download":
        argv += ["download", "https://youtu.be/abcdefghijk"]
    elif command == "batch":
        monkeypatch.setattr(
            yt_final,
            "_load_manifest",
            lambda _path: [JobSpec(urls=["https://youtu.be/abcdefghijk"])],
        )
        argv += ["batch", str(tmp_path / "manifest.json")]
    elif command == "queue-run":
        queue_path = tmp_path / "queue.json"
        DownloadJournal(queue_path).add(
            JobSpec(urls=["https://youtu.be/abcdefghijk"])
        )
        argv += ["queue", "run", "--queue-file", str(queue_path)]
    else:
        monkeypatch.setattr(
            yt_final,
            "_resolve_redo_spec",
            lambda *_args, **_kwargs: JobSpec(
                urls=["https://youtu.be/abcdefghijk"]
            ).to_dict(),
        )
        argv += ["redo", "last"]
    monkeypatch.setattr(sys, "argv", argv)

    if outcome == "base-exception":
        with pytest.raises(_CliBaseException):
            yt_final.main()
    elif outcome == "error" and command in {"batch", "queue-run"}:
        with pytest.raises(SystemExit) as caught:
            yt_final.main()
        assert caught.value.code == 1
    elif outcome == "error":
        with pytest.raises(RuntimeError, match="engine failed"):
            yt_final.main()
    else:
        yt_final.main()

    assert len(instances) == 1
    assert instances[0].shutdown_calls == [5.0]


def test_server_shutdown_joins_worker_then_closes_cached_dispatcher_only():
    order = []

    class Worker:
        def is_alive(self):
            return True

        def join(self, timeout):
            order.append(("join", timeout))

    class Engine:
        def shutdown_notifications(self, timeout):
            order.append(("shutdown", timeout))
            return True

    state = SimpleNamespace(_engine=Engine())
    assert webserver._join_worker_and_shutdown_notifications(state, Worker()) is True
    assert order == [
        ("join", webserver.WORKER_SHUTDOWN_JOIN_S),
        ("shutdown", 5.0),
    ]

    order.clear()
    state = SimpleNamespace(_engine=None)
    assert webserver._join_worker_and_shutdown_notifications(state, Worker()) is True
    assert order == [("join", webserver.WORKER_SHUTDOWN_JOIN_S)]
