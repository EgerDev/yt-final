import pytest

import jobstore


def test_set_status_persists_error_kind(tmp_path):
    js = jobstore.DownloadJournal(str(tmp_path / "queue.json"))
    rec = js.add({"urls": ["u"]})
    js.set_status(rec["id"], "failed", error="age", error_kind="agegate")
    got = js.get(rec["id"])
    assert got["last_error_kind"] == "agegate"
    assert got["last_error"] == "age"


def test_failed_write_does_not_poison_cache(tmp_path, monkeypatch):
    """A write that never lands on disk must not leave a phantom mutation visible in-process,
    nor let a later, unrelated successful write launder it onto disk."""
    js = jobstore.DownloadJournal(str(tmp_path / "queue.json"))
    rec = js.add({"urls": ["u"]})
    jid = rec["id"]
    assert js.get(jid)["status"] == "queued"  # prime the read cache

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(jobstore.os, "replace", boom)
    with pytest.raises(OSError):
        js.set_status(jid, "done")
    monkeypatch.undo()

    # The un-persisted 'done' must not be visible: cache invalidated -> reload shows durable 'queued'.
    assert js.get(jid)["status"] == "queued"

    # A later successful, unrelated write must serialize the durable state, not the phantom 'done'.
    js.set_priority(jid, 7)
    got = js.get(jid)
    assert got["status"] == "queued"
    assert got["priority"] == 7


def test_history_read_tail_returns_last_n(tmp_path):
    h = jobstore.HistoryLedger(str(tmp_path / "history.jsonl"))
    for i in range(500):
        h.record(url=f"u{i}", output=f"o{i}", profile="p", status="done", spec={"n": i})
    last = h.read(limit=10)
    assert [r["url"] for r in last] == [f"u{i}" for i in range(490, 500)]
    # limit larger than total returns everything, in order
    allrecs = h.read(limit=10000)
    assert len(allrecs) == 500
    assert allrecs[0]["url"] == "u0" and allrecs[-1]["url"] == "u499"
    # no limit -> full read, in order
    assert len(h.read()) == 500


def test_history_read_tail_tolerates_torn_last_line(tmp_path):
    p = tmp_path / "history.jsonl"
    h = jobstore.HistoryLedger(str(p))
    for i in range(5):
        h.record(url=f"u{i}", output="o", profile="p", status="done", spec={"n": i})
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"ts": 1, "url": "torn"')  # torn partial line, no newline / invalid json
    assert [r["url"] for r in h.read(limit=3)] == ["u2", "u3", "u4"]


def test_history_read_tail_partial_head_drop(tmp_path):
    """File larger than the initial window forces a mid-line window start; the leading partial
    fragment must be dropped, not mis-parsed."""
    h = jobstore.HistoryLedger(str(tmp_path / "history.jsonl"))
    big = "x" * 1000
    for i in range(200):
        h.record(url=f"u{i}", output="o", profile="p", status="done", spec={"n": i, "pad": big})
    assert [r["url"] for r in h.read(limit=5)] == [f"u{i}" for i in range(195, 200)]


def test_history_read_tail_grows_window(tmp_path):
    """When the first window yields fewer than `limit` valid rows, the window grows until it does."""
    h = jobstore.HistoryLedger(str(tmp_path / "history.jsonl"))
    big = "y" * 8000  # each record far exceeds the limit*4096 initial estimate
    for i in range(80):
        h.record(url=f"u{i}", output="o", profile="p", status="done", spec={"pad": big})
    got = h.read(limit=30)
    assert [r["url"] for r in got] == [f"u{i}" for i in range(50, 80)]
