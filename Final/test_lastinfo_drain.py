"""The per-job metadata side-channel must not survive the job.

`downloader.last_info` maps a delivered path -> distilled yt-dlp metadata. It is written once per
successful download (yt_final.py, `self.last_info[str(p)] = distilled`) and normally removed either by
_finalize's pop, or by the encode_mode='none' drain in _finalize_downloaded.

Neither runs when the job never reaches its processing loop. Two real paths:
  * a cancel partway through a serial batch — URLs 1..k-1 already wrote entries, URL k raises
    DownloadCancelled, and _run_body never reaches the loop;
  * the parallel path, where download_parallel re-raises the cancel so the `downloaded` list is never
    even assigned and there is nothing local to drain.

The CLI does not care — the process exits. The web engine keeps ONE downloader for the whole server
lifetime, so those entries persist until restart. Each holds ~20 fields including the description
(YouTube caps ~5,000 chars) plus chapters/sponsorblock, so it is kilobytes per abandoned path — small,
unbounded, and trivially avoidable.

These tests drive the REAL run() with the download and history/notify collaborators stubbed, so they
exercise the actual finally block rather than asserting a string.
"""
from __future__ import annotations

import pytest

import yt_final


class _Cancel(Exception):
    """Stand-in for the engine's cancellation exception."""


def _engine_with(monkeypatch, *, seeds, raiser):
    """A FinalYouTubeGPUEngine whose run() body seeds last_info then fails, exercising the finally."""
    eng = yt_final.FinalYouTubeGPUEngine.__new__(yt_final.FinalYouTubeGPUEngine)

    class _DL:
        def __init__(self):
            self.last_info = {}
            self.output_path = "orig"

    eng.downloader = _DL()
    # _run_body is where the real download + processing happens; replace it with a stand-in that
    # reproduces the leak shape — some paths succeed (writing entries) and then the job dies.
    def _body(*a, **k):
        for p in seeds:
            eng.downloader.last_info[p] = {"title": "x" * 100, "description": "y" * 5000}
        if raiser is not None:
            raise raiser
        return list(seeds)
    monkeypatch.setattr(eng, "_run_body", _body, raising=False)
    return eng


def _run(eng, spec):
    """Call the real run() with the surrounding machinery neutralised."""
    return yt_final.FinalYouTubeGPUEngine.run(eng, spec)


@pytest.fixture
def spec():
    return yt_final.JobSpec(urls=["https://youtu.be/aaaaaaaaaaa"])


def _prepare(eng, monkeypatch):
    """Neutralise everything run() touches besides the drain under test."""
    import threading
    eng._run_output_lock = threading.RLock()
    for name in ("_record_history", "_spawn_notification", "_notify", "_emit"):
        if hasattr(yt_final.FinalYouTubeGPUEngine, name):
            monkeypatch.setattr(eng, name, lambda *a, **k: None, raising=False)


def test_a_cancel_partway_through_a_batch_does_not_leave_metadata_behind(monkeypatch, spec):
    """THE LEAK. Two URLs already downloaded and wrote entries; the third cancels. Before the drain
    those two entries stayed in a downloader that the web server never recreates."""
    eng = _engine_with(monkeypatch, seeds=("/dl/one.mp4", "/dl/two.mp4"), raiser=_Cancel("stopped"))
    _prepare(eng, monkeypatch)
    with pytest.raises(_Cancel):
        _run(eng, spec)
    assert eng.downloader.last_info == {}, (
        "metadata for the already-downloaded paths survived a cancelled job — in the web engine that "
        "downloader lives for the whole server lifetime: " + repr(list(eng.downloader.last_info))
    )


def test_an_exception_before_the_processing_loop_does_not_leak_either(monkeypatch, spec):
    """Same shape, different cause: anything raised between the download and the loop that would have
    popped the entries. The drain is in a finally precisely so the cause does not matter."""
    eng = _engine_with(monkeypatch, seeds=("/dl/only.mp4",), raiser=RuntimeError("boom"))
    _prepare(eng, monkeypatch)
    with pytest.raises(RuntimeError):
        _run(eng, spec)
    assert eng.downloader.last_info == {}, eng.downloader.last_info


def test_a_successful_job_also_leaves_nothing_behind(monkeypatch, spec):
    """The happy path already pops per-path inside _finalize, but the drain must not depend on that
    having happened — a job that returns normally must also leave the side-channel empty."""
    eng = _engine_with(monkeypatch, seeds=("/dl/done.mp4",), raiser=None)
    _prepare(eng, monkeypatch)
    out = _run(eng, spec)
    assert out == ["/dl/done.mp4"]
    assert eng.downloader.last_info == {}, eng.downloader.last_info


def test_the_drain_restores_output_path_too(monkeypatch, spec):
    """The drain shares run()'s finally with the output_path restore. Adding it must not shadow or
    skip that — a swallowed exception in the drain would leave the downloader pointed at the job's
    directory for every later job."""
    eng = _engine_with(monkeypatch, seeds=("/dl/one.mp4",), raiser=_Cancel("stopped"))
    _prepare(eng, monkeypatch)
    with pytest.raises(_Cancel):
        _run(eng, spec)
    assert eng.downloader.output_path == "orig", "the output_path restore was disturbed"


def test_finalize_pops_only_its_own_path(tmp_path, spec):
    """The end-of-job clear is a BACKSTOP, not a replacement. _finalize's per-path pop is what keeps
    memory flat WHILE a long batch runs — without it a 200-URL job holds 200 distilled entries until
    the very end.

    This was a whole-file substring assertion (`"...last_info.pop(str(src), {})" in src`). That reads
    all 9,000+ lines of yt_final.py and matches anywhere, including inside a comment — and the comment
    directly above the real pop happens to contain the words it looked for. Downgrading pop() to get()
    left the test green. So: exercise the real _finalize instead.

    The second assertion is the load-bearing half. A blanket `last_info.clear()` inside _finalize
    would satisfy "my path is gone" while destroying the sibling entries a batch still needs."""
    eng = yt_final.FinalYouTubeGPUEngine.__new__(yt_final.FinalYouTubeGPUEngine)

    mine = tmp_path / "mine.mp4"
    mine.write_bytes(b"x")                      # _finalize returns early unless the file exists
    sibling = tmp_path / "still-downloading.mp4"

    class _DL:
        cancel_check = None
        def __init__(self):
            self.last_info = {str(mine): {"title": "mine"}, str(sibling): {"title": "sibling"}}

    class _Proc:
        cancel_check = None
        @staticmethod
        def probe_integrity(_p):
            raise RuntimeError("stop here — the pop under test is above this point")

    eng.downloader = _DL()
    eng.processor = _Proc()

    try:
        yt_final.FinalYouTubeGPUEngine._finalize(eng, mine, spec)
    except Exception:
        # The pop is the first thing _finalize does after the existence check, so whatever the rest of
        # the pipeline does with a 1-byte fake mp4 is irrelevant to what is asserted below.
        pass

    assert str(mine) not in eng.downloader.last_info, \
        "_finalize no longer pops its own entry — memory grows for the whole batch"
    assert str(sibling) in eng.downloader.last_info, \
        "_finalize removed a SIBLING path's entry — a per-path pop became a blanket clear"


def test_the_encode_mode_none_drain_is_still_there():
    """The other narrower drain: encode_mode='none' returns before the _finalize loop, so that path
    never reaches the pop above and needs its own. Bounded to that branch rather than grepping the
    whole file, so a match cannot come from unrelated code."""
    from pathlib import Path
    src = Path(__file__).resolve().parent.joinpath("yt_final.py").read_text(encoding="utf-8")
    branch = src.split('if spec.encode_mode == "none":', 1)[1].split("return downloaded", 1)[0]
    code = "\n".join(ln for ln in branch.splitlines() if not ln.strip().startswith("#"))
    assert "last_info.pop(" in code, \
        "the encode_mode='none' drain is gone — that path never reaches _finalize"
