"""Regression tests for the durable-store wipe guards (jobstore + watchstore).

Every defect found in four review rounds was found by READING the code, never by a failing test —
these are the behaviors whose silent regression costs real user data, so they get executed here.

The invariant under test: a transient OS error (a Windows sharing violation from AV / a backup or
sync agent holding the file) must NEVER be laundered into "the document is empty". Callers are all
read-modify-WRITE, so returning an empty document persists the wipe on the very next mutation.
"""
import builtins
import json
import math
import os
import pathlib
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import pytest

import jobstore
import state_validation
import watchstore
from jobspec import JobSpec
from state_validation import (
    PersistentStateError,
    quarantine_corrupt_state,
    validate_queue_document,
    validate_watch_document,
)


STORES = [
    (jobstore.DownloadJournal, "jobs", "queue.json"),
    (watchstore.WatchStore, "watches", "watches.json"),
]


def _make(cls, tmp, key, seeded=None):
    path = os.path.join(tmp, "s.json")
    store = cls(path=path)
    if seeded is not None:
        pathlib.Path(path).write_text(
            json.dumps({"version": 1, key: seeded}), encoding="utf-8")
    return store, pathlib.Path(path)


@pytest.mark.parametrize("cls,key,_name", STORES)
def test_missing_file_bootstraps_empty(cls, key, _name, tmp_path):
    """First run must still work — FileNotFoundError is the ONE case that may return empty."""
    store, _ = _make(cls, str(tmp_path), key)
    assert store._read()[key] == []


def _assert_persistent_state_error(caught, path, original):
    error = caught.value
    assert error.__class__.__name__ == "PersistentStateError"
    assert error.path == path
    assert not path.exists()
    assert error.backup_path.read_bytes() == original
    assert error.reason
    return error


@pytest.mark.parametrize("cls,key,_name", STORES)
def test_invalid_json_is_quarantined_byte_for_byte_and_raises(cls, key, _name, tmp_path):
    """Invalid JSON is user data too: preserve its exact bytes and make the failure explicit."""
    store, path = _make(cls, str(tmp_path), key)
    original = b'{ "important": true,\r\nnot-json\xff'
    path.write_bytes(original)

    with pytest.raises(PersistentStateError) as caught:
        if key == "jobs":
            store.add(JobSpec(urls=["https://youtu.be/abcdefghijk"]))
        else:
            store.add("UC" + "a" * 22)

    _assert_persistent_state_error(caught, path, original)


@pytest.mark.parametrize("payload", [
    {"version": 99, "metadata": {"important": True}},
    {"version": 1, "jobs": "not-a-list"},
    {"version": 1, "jobs": [{"id": 7, "status": "mystery"}]},
])
def test_invalid_queue_is_quarantined_and_never_overwritten(tmp_path, payload):
    path = tmp_path / "queue.json"
    original = json.dumps(payload).encode()
    path.write_bytes(original)
    journal = jobstore.DownloadJournal(path)

    with pytest.raises(PersistentStateError) as caught:
        journal.add(JobSpec(urls=["https://youtu.be/abcdefghijk"]))

    _assert_persistent_state_error(caught, path, original)


@pytest.mark.parametrize("payload", [
    {"version": 99, "metadata": {"important": True}},
    {"version": 1, "watches": "not-a-list"},
    {"version": 1, "watches": [{"id": 7, "channel_id": []}]},
])
def test_invalid_watch_state_is_quarantined_and_never_overwritten(tmp_path, payload):
    path = tmp_path / "watches.json"
    original = json.dumps(payload).encode()
    path.write_bytes(original)
    store = watchstore.WatchStore(path)

    with pytest.raises(PersistentStateError) as caught:
        store.add("UC" + "a" * 22)

    _assert_persistent_state_error(caught, path, original)


def _queue_record(**overrides):
    record = {
        "id": "j_keep",
        "spec": JobSpec(urls=["https://youtu.be/abcdefghijk"]).to_dict(),
        "status": "queued",
        "attempt_count": 0,
        "last_error": None,
        "output_paths": [],
        "created_at": 1.0,
        "updated_at": 1.0,
        "label": "keep",
    }
    record.update(overrides)
    return record


def _watch_record(**overrides):
    record = {
        "id": "w_keep",
        "channel_id": "UC" + "a" * 22,
        "label": "keep",
        "url": None,
        "auto_queue": False,
        "defaults": {"quality": "1080p", "encode_mode": "copy", "no_shorts": True},
        "filters": {},
        "seen": [],
        "pending": [],
        "created_at": 1.0,
        "last_checked": None,
        "last_error": None,
    }
    record.update(overrides)
    return record


def _valid_seed_record(key, identifier):
    if key == "jobs":
        return _queue_record(id=identifier)
    return _watch_record(id=identifier, channel_id="UC" + identifier)


def test_valid_legacy_and_unknown_queue_fields_are_preserved_verbatim():
    document = {
        "version": 1,
        "jobs": [
            {
                **_queue_record(spec={
                    "url": "https://youtu.be/abcdefghijk",
                    "future_job_option": {"keep": True},
                }),
                "future_record_field": [1, 2, 3],
            },
        ],
        "paused": False,
        "future_document_field": {"keep": "exactly"},
    }

    validated = validate_queue_document(document)

    assert validated is document
    assert validated["future_document_field"] == {"keep": "exactly"}
    assert validated["jobs"][0]["future_record_field"] == [1, 2, 3]
    assert validated["jobs"][0]["spec"]["future_job_option"] == {"keep": True}


def test_legacy_queue_rows_reload_without_request_id_and_new_keys_persist(tmp_path):
    path = tmp_path / "queue.json"
    legacy = _queue_record()
    path.write_text(
        json.dumps({"version": 1, "jobs": [legacy]}),
        encoding="utf-8",
    )

    journal = jobstore.DownloadJournal(path)
    assert journal.get(legacy["id"]) == legacy

    keyed = journal.add(
        JobSpec(urls=["https://youtu.be/zyxwvutsrqp"]),
        request_id="persist-key",
    )
    assert keyed["request_id"] == "persist-key"
    assert jobstore.DownloadJournal(path).get(keyed["id"])["request_id"] == "persist-key"


@pytest.mark.parametrize("request_id", [
    None,
    True,
    7,
    "",
    "1234567",
    "x" * 129,
    "valid-id\n",
    "valid-id\x7f",
    "valid-id-é",
])
def test_queue_validator_rejects_malformed_persisted_request_ids(request_id):
    with pytest.raises(ValueError, match="request_id"):
        validate_queue_document({
            "version": 1,
            "jobs": [_queue_record(request_id=request_id)],
        })


@pytest.mark.parametrize("request_id", [
    " " * 8,
    "x" * 8,
    "x" * 128,
    "~" * 8,
])
def test_queue_validator_accepts_request_id_boundaries_without_normalizing(request_id):
    document = {
        "version": 1,
        "jobs": [_queue_record(request_id=request_id)],
    }
    assert validate_queue_document(document) is document
    assert document["jobs"][0]["request_id"] == request_id


@pytest.mark.parametrize("mutate", [
    lambda d: None,
    lambda d: d.pop("version"),
    lambda d: d.update(version=True),
    lambda d: d.update(version=2),
    lambda d: d.pop("jobs"),
    lambda d: d.update(jobs={}),
    lambda d: d["jobs"].append("not-an-object"),
    lambda d: d["jobs"][0].pop("id"),
    lambda d: d["jobs"][0].update(id=""),
    lambda d: d["jobs"][0].update(id=7),
    lambda d: d["jobs"][0].pop("spec"),
    lambda d: d["jobs"][0].update(spec=[]),
    lambda d: d["jobs"][0]["spec"].update(urls={"bad": "container"}),
    lambda d: d["jobs"][0]["spec"].update(urls=["ok", 7]),
    lambda d: d["jobs"][0]["spec"].update(parallel="false"),
    lambda d: d["jobs"][0]["spec"].update(workers="2"),
    lambda d: d["jobs"][0]["spec"].update(loudnorm="loud"),
    lambda d: d["jobs"][0]["spec"].update(sub_langs="en"),
    lambda d: d["jobs"][0].pop("status"),
    lambda d: d["jobs"][0].update(status="mystery"),
    lambda d: d["jobs"][0].pop("attempt_count"),
    lambda d: d["jobs"][0].update(attempt_count=-1),
    lambda d: d["jobs"][0].update(attempt_count=True),
    lambda d: d["jobs"][0].pop("last_error"),
    lambda d: d["jobs"][0].pop("output_paths"),
    lambda d: d["jobs"][0].pop("created_at"),
    lambda d: d["jobs"][0].pop("updated_at"),
    lambda d: d["jobs"][0].pop("label"),
    lambda d: d["jobs"][0].update(created_at="yesterday"),
    lambda d: d["jobs"][0].update(updated_at=math.nan),
    lambda d: d["jobs"][0].update(last_error=[]),
    lambda d: d["jobs"][0].update(output_paths="movie.mp4"),
    lambda d: d["jobs"][0].update(output_paths=[7]),
    lambda d: d["jobs"][0].update(label=[]),
    lambda d: d.update(paused="false"),
], ids=[
    "document-type", "missing-version", "boolean-version", "unsupported-version",
    "missing-jobs", "jobs-container", "record-container", "missing-id", "empty-id",
    "id-type", "missing-spec", "spec-container", "urls-container", "urls-item",
    "boolean-spec-field", "integer-spec-field", "number-spec-field", "list-spec-field",
    "missing-status", "status-value", "missing-attempt-count", "negative-attempt-count",
    "boolean-attempt-count", "missing-last-error", "missing-output-paths",
    "missing-created-at", "missing-updated-at", "missing-label", "created-at-type",
    "updated-at-finite", "last-error-type", "output-paths-container",
    "output-paths-item", "label-type", "paused-type",
])
def test_queue_validator_rejects_every_incompatible_schema_field(mutate):
    document = {"version": 1, "jobs": [_queue_record()]}
    if mutate(document) is None and document == {"version": 1, "jobs": [_queue_record()]}:
        document = []

    with pytest.raises(ValueError):
        validate_queue_document(document)


def test_queue_validator_rejects_duplicate_job_identifiers():
    document = {"version": 1, "jobs": [_queue_record(), _queue_record()]}
    with pytest.raises(ValueError, match="duplicate"):
        validate_queue_document(document)


@pytest.mark.parametrize("status", [[], {}], ids=["list", "dict"])
def test_queue_validator_rejects_unhashable_status_as_schema_error(status):
    with pytest.raises(ValueError, match="status"):
        validate_queue_document({
            "version": 1,
            "jobs": [_queue_record(status=status)],
        })


def test_validators_reject_integers_outside_the_finite_float_domain():
    enormous = json.loads("1" + ("0" * 1000))
    queue = {"version": 1, "jobs": [_queue_record(
        created_at=enormous,
        updated_at=enormous,
    )]}
    watch = {"version": 1, "watches": [_watch_record(
        created_at=enormous,
        last_checked=enormous,
    )]}

    with pytest.raises(ValueError, match="finite"):
        validate_queue_document(queue)
    with pytest.raises(ValueError, match="finite"):
        validate_watch_document(watch)


def test_claim_next_quarantines_huge_timestamp_before_float_conversion(tmp_path):
    path = tmp_path / "queue.json"
    enormous = json.loads("1" + ("0" * 1000))
    original = json.dumps({
        "version": 1,
        "jobs": [_queue_record(created_at=enormous, updated_at=enormous)],
    }).encode()
    path.write_bytes(original)

    with pytest.raises(PersistentStateError) as caught:
        jobstore.DownloadJournal(path).claim_next()

    _assert_persistent_state_error(caught, path, original)


def test_jobspec_rejects_huge_loudnorm_as_value_error():
    enormous = json.loads("1" + ("0" * 1000))

    with pytest.raises(ValueError, match="finite"):
        JobSpec.from_dict({"loudnorm": enormous})


def test_valid_legacy_and_unknown_watch_fields_are_preserved_verbatim():
    document = {
        "version": 1,
        "watches": [
            {
                **_watch_record(
                    defaults={
                        "quality": "1080p",
                        "future_job_option": {"keep": True},
                    },
                    filters={
                        "filter_title": "release",
                        "future_filter": ["keep"],
                    },
                    pending=[{
                        "id": "vid1",
                        "title": "Legacy pending item",
                        "url": "https://youtu.be/vid1",
                    }],
                ),
                "future_record_field": {"keep": 1},
            },
        ],
        "future_document_field": ["keep"],
    }

    validated = validate_watch_document(document)

    assert validated is document
    assert validated["future_document_field"] == ["keep"]
    watch = validated["watches"][0]
    assert watch["future_record_field"] == {"keep": 1}
    assert watch["defaults"]["future_job_option"] == {"keep": True}
    assert watch["filters"]["future_filter"] == ["keep"]
    assert "published" not in watch["pending"][0]


@pytest.mark.parametrize("mutate", [
    lambda d: None,
    lambda d: d.pop("version"),
    lambda d: d.update(version=True),
    lambda d: d.update(version=2),
    lambda d: d.pop("watches"),
    lambda d: d.update(watches={}),
    lambda d: d["watches"].append("not-an-object"),
    lambda d: d["watches"][0].pop("id"),
    lambda d: d["watches"][0].update(id=""),
    lambda d: d["watches"][0].update(id=7),
    lambda d: d["watches"][0].pop("channel_id"),
    lambda d: d["watches"][0].update(channel_id=[]),
    lambda d: d["watches"][0].pop("label"),
    lambda d: d["watches"][0].update(label=[]),
    lambda d: d["watches"][0].pop("url"),
    lambda d: d["watches"][0].update(url=[]),
    lambda d: d["watches"][0].pop("auto_queue"),
    lambda d: d["watches"][0].update(auto_queue="false"),
    lambda d: d["watches"][0].pop("defaults"),
    lambda d: d["watches"][0].update(defaults=[]),
    lambda d: d["watches"][0]["defaults"].update(no_shorts="true"),
    lambda d: d["watches"][0].update(filters=[]),
    lambda d: d["watches"][0]["filters"].update(filter_title=[]),
    lambda d: d["watches"][0]["filters"].update(after=20260101),
    lambda d: d["watches"][0]["filters"].update(min_duration="60"),
    lambda d: d["watches"][0].pop("seen"),
    lambda d: d["watches"][0].update(seen="vid1"),
    lambda d: d["watches"][0].update(seen=["vid1", 7]),
    lambda d: d["watches"][0].pop("pending"),
    lambda d: d["watches"][0].update(pending={}),
    lambda d: d["watches"][0].update(pending=[{}]),
    lambda d: d["watches"][0].update(pending=[{"id": 7}]),
    lambda d: d["watches"][0].update(pending=[{"id": "v", "title": []}]),
    lambda d: d["watches"][0].update(pending=[{"id": "v", "url": []}]),
    lambda d: d["watches"][0].update(pending=[{"id": "v", "published": []}]),
    lambda d: d["watches"][0].pop("created_at"),
    lambda d: d["watches"][0].update(created_at="yesterday"),
    lambda d: d["watches"][0].pop("last_checked"),
    lambda d: d["watches"][0].update(last_checked=math.inf),
    lambda d: d["watches"][0].pop("last_error"),
    lambda d: d["watches"][0].update(last_error=[]),
    lambda d: d["watches"][0].update(auto_queue_fails=[]),
    lambda d: d["watches"][0].update(auto_queue_fails={"vid1": -1}),
    lambda d: d["watches"][0].update(auto_queue_errors={"vid1": []}),
], ids=[
    "document-type", "missing-version", "boolean-version", "unsupported-version",
    "missing-watches", "watches-container", "record-container", "missing-id",
    "empty-id", "id-type", "missing-channel-id", "channel-id-type", "missing-label",
    "label-type", "missing-url", "url-type", "missing-auto-queue", "auto-queue-type",
    "missing-defaults", "defaults-container", "defaults-field-type", "filters-container",
    "filter-title-type", "after-type", "duration-type", "missing-seen", "seen-container",
    "seen-item", "missing-pending", "pending-container", "pending-missing-id",
    "pending-id-type", "pending-title-type", "pending-url-type",
    "pending-published-type", "missing-created-at", "created-at-type",
    "missing-last-checked", "last-checked-finite", "missing-last-error",
    "last-error-type", "auto-queue-fails-container", "auto-queue-fails-value",
    "auto-queue-errors-value",
])
def test_watch_validator_rejects_every_incompatible_schema_field(mutate):
    document = {"version": 1, "watches": [_watch_record()]}
    if mutate(document) is None and document == {"version": 1, "watches": [_watch_record()]}:
        document = []

    with pytest.raises(ValueError):
        validate_watch_document(document)


def test_watch_validator_rejects_duplicate_watch_identifiers():
    document = {"version": 1, "watches": [_watch_record(), _watch_record()]}
    with pytest.raises(ValueError, match="duplicate"):
        validate_watch_document(document)


def test_quarantine_uses_utc_microseconds_and_monotonic_collision_suffixes(tmp_path):
    path = tmp_path / "queue.json"
    stamp = "20260728T010203456789Z"
    occupied = [
        tmp_path / f"queue.corrupt-{stamp}.json",
        tmp_path / f"queue.corrupt-{stamp}-1.json",
    ]
    for index, candidate in enumerate(occupied):
        candidate.write_bytes(f"older-{index}".encode())
    original = b"\x00exact-original\r\n"
    path.write_bytes(original)

    backup = quarantine_corrupt_state(
        path,
        clock=lambda: datetime(2026, 7, 28, 1, 2, 3, 456789, tzinfo=timezone.utc),
    )

    assert backup == tmp_path / f"queue.corrupt-{stamp}-2.json"
    assert backup.read_bytes() == original
    assert not path.exists()
    assert [candidate.read_bytes() for candidate in occupied] == [b"older-0", b"older-1"]


def test_quarantine_name_selection_never_clobbers_a_raced_backup(
        tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    stamp = "20260728T010203456789Z"
    raced = tmp_path / f"queue.corrupt-{stamp}.json"
    raced.write_bytes(b"concurrent-backup")
    original = b"source-bytes"
    path.write_bytes(original)
    real_exists = pathlib.Path.exists

    # Simulate the exact stale exists() result from the reviewed TOCTOU:
    # the destination exists at rename time even though selection observed False.
    monkeypatch.setattr(
        pathlib.Path,
        "exists",
        lambda candidate: False if candidate == raced else real_exists(candidate),
    )

    backup = quarantine_corrupt_state(
        path,
        clock=lambda: datetime(2026, 7, 28, 1, 2, 3, 456789, tzinfo=timezone.utc),
    )

    assert raced.read_bytes() == b"concurrent-backup"
    assert backup == tmp_path / f"queue.corrupt-{stamp}-1.json"
    assert backup.read_bytes() == original


@pytest.mark.parametrize("cls,key", [
    (jobstore.DownloadJournal, "jobs"),
    (watchstore.WatchStore, "watches"),
])
def test_quarantine_failure_is_fail_closed_and_keeps_source_bytes(
        cls, key, tmp_path, monkeypatch):
    path = tmp_path / ("queue.json" if key == "jobs" else "watches.json")
    original = b"{broken"
    path.write_bytes(original)
    store = cls(path)

    def deny_replace(_source, _destination):
        raise PermissionError(13, "quarantine denied")

    monkeypatch.setattr(state_validation.os, "replace", deny_replace)
    monkeypatch.setattr(
        store, "_write",
        lambda _data: pytest.fail("store tried to replace state after quarantine failed"),
    )
    with pytest.raises(PermissionError, match="quarantine denied"):
        if key == "jobs":
            store.add(JobSpec(urls=["https://youtu.be/abcdefghijk"]))
        else:
            store.add("UC" + "a" * 22)
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.corrupt-*.json")) == []


@pytest.mark.parametrize("operation", ["add", "update"])
def test_queue_mutators_never_write_after_validation_failure(tmp_path, operation, monkeypatch):
    path = tmp_path / "queue.json"
    original = json.dumps({
        "version": 1,
        "jobs": [_queue_record(status="not-a-status")],
    }).encode()
    path.write_bytes(original)
    journal = jobstore.DownloadJournal(path)
    monkeypatch.setattr(
        journal, "_write",
        lambda _data: pytest.fail(f"{operation} wrote after queue validation failed"),
    )

    with pytest.raises(PersistentStateError) as caught:
        if operation == "add":
            journal.add(JobSpec(urls=["https://youtu.be/zyxwvutsrqp"]))
        else:
            journal.update("j_keep", label="changed")

    _assert_persistent_state_error(caught, path, original)


@pytest.mark.parametrize("operation", ["add", "update", "apply_poll"])
def test_watch_mutators_never_write_after_validation_failure(tmp_path, operation, monkeypatch):
    path = tmp_path / "watches.json"
    original = json.dumps({
        "version": 1,
        "watches": [_watch_record(filters="not-an-object")],
    }).encode()
    path.write_bytes(original)
    store = watchstore.WatchStore(path)
    monkeypatch.setattr(
        store, "_write",
        lambda _data: pytest.fail(f"{operation} wrote after watch validation failed"),
    )

    with pytest.raises(PersistentStateError) as caught:
        if operation == "add":
            store.add("UC" + "b" * 22)
        elif operation == "update":
            store.update("w_keep", label="changed")
        else:
            store.apply_poll("w_keep", [])

    _assert_persistent_state_error(caught, path, original)


@pytest.mark.parametrize("cls,key,_name", STORES)
def test_persistent_oserror_raises_rather_than_returning_empty(cls, key, _name, tmp_path, monkeypatch):
    """THE core guard: a persistent sharing violation must raise, never return an empty document.

    Returning empty here is what silently erased the whole queue / every channel watch on Windows.
    """
    store, _ = _make(
        cls, str(tmp_path), key, seeded=[_valid_seed_record(key, "keep")])

    def boom(*a, **k):
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(os, "stat", boom)
    with pytest.raises(PermissionError):
        store._read()


@pytest.mark.parametrize("cls,key,_name", STORES)
def test_transient_oserror_serves_cache_when_freshness_unknown(cls, key, _name, tmp_path, monkeypatch):
    """If os.stat is what failed we cannot know the cache is stale — serving it beats crashing."""
    store, _ = _make(
        cls, str(tmp_path), key, seeded=[_valid_seed_record(key, "cached")])
    store._read()                                    # warm the cache

    monkeypatch.setattr(os, "stat", lambda *a, **k: (_ for _ in ()).throw(
        PermissionError(13, "sharing violation")))
    assert store._read()[key][0]["id"] == "cached"


@pytest.mark.parametrize("cls,key,_name", STORES)
def test_never_serves_a_cache_already_proven_stale(cls, key, _name, tmp_path, monkeypatch):
    """If stat SUCCEEDS and shows a different file, the cache is proven stale.

    Serving it would silently revert another process's committed write. Even when the subsequent
    open() then fails on every retry, the answer must be 'raise', not 'return the old data'.
    """
    store, p = _make(
        cls, str(tmp_path), key, seeded=[_valid_seed_record(key, "v1")])
    store._read()                                    # warm the cache

    # another process commits a change (new mtime AND size)
    p.write_text(json.dumps({
        "version": 1,
        key: [_valid_seed_record(key, "v1"), _valid_seed_record(key, "v2")],
    }), encoding="utf-8")

    monkeypatch.setattr(builtins, "open", lambda *a, **k: (_ for _ in ()).throw(
        PermissionError(13, "sharing violation")))
    with pytest.raises(PermissionError):
        store._read()


@pytest.mark.parametrize("cls,key,_name", STORES)
def test_open_after_successful_stat_disappearance_is_retried_not_bootstrapped(
        cls, key, _name, tmp_path, monkeypatch):
    """A post-stat rename race must not be confused with an absent first-run document."""
    store, path = _make(
        cls, str(tmp_path), key, seeded=[_valid_seed_record(key, "keep")])
    original_open = builtins.open
    first_state_open = True

    def disappear_once(file, *args, **kwargs):
        nonlocal first_state_open
        if first_state_open and pathlib.Path(file) == path:
            first_state_open = False
            raise FileNotFoundError(path)
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", disappear_once)
    if key == "jobs":
        store.add(JobSpec(urls=["https://youtu.be/zyxwvutsrqp"]), label="new")
    else:
        store.add("UC" + "n" * 22, label="new")

    identifiers = {record["id"] for record in cls(path).list()}
    assert "keep" in identifiers
    assert len(identifiers) == 2


@pytest.mark.parametrize("cls,key,_name", STORES)
def test_read_only_calls_wait_for_the_cross_process_state_lock(
        cls, key, _name, tmp_path):
    """Every read/parse/validate path participates in the sidecar lock protocol."""
    store, path = _make(
        cls, str(tmp_path), key, seeded=[_valid_seed_record(key, "keep")])
    held = tmp_path / "held"
    release = tmp_path / "release"
    script = r"""
import os
import pathlib
import sys
import time

state, held, release = map(pathlib.Path, sys.argv[1:])
lock_path = state.with_suffix(".flock")
lock_path.touch(exist_ok=True)
fd = open(lock_path, "r+b")
if os.name == "nt":
    import msvcrt
    msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
else:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)
held.write_text("held", encoding="utf-8")
while not release.exists():
    time.sleep(0.01)
if os.name == "nt":
    fd.seek(0)
    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
else:
    fcntl.flock(fd, fcntl.LOCK_UN)
fd.close()
"""
    process = subprocess.Popen([
        sys.executable, "-c", script, str(path), str(held), str(release),
    ])
    read_done = threading.Event()
    reader = threading.Thread(target=lambda: (store.list(), read_done.set()))
    try:
        deadline = time.monotonic() + 5
        while not held.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert held.exists(), f"lock holder exited with {process.poll()}"
        reader.start()
        assert not read_done.wait(0.25), "read-only state access ignored the sidecar lock"
        release.write_text("release", encoding="utf-8")
        reader.join(5)
        assert read_done.is_set()
    finally:
        release.touch(exist_ok=True)
        process.wait(timeout=5)
        if reader.is_alive():
            reader.join(5)


@pytest.mark.parametrize("cls,key,module,_name", [
    (jobstore.DownloadJournal, "jobs", jobstore, "queue"),
    (watchstore.WatchStore, "watches", watchstore, "watch"),
])
def test_stale_invalid_reader_cannot_quarantine_a_new_valid_replacement(
        cls, key, module, _name, tmp_path, monkeypatch):
    """The invalid bytes selected for quarantine and the moved bytes are one locked identity."""
    path = tmp_path / f"{_name}.json"
    invalid = b'{"version":1,"broken":true}'
    path.write_bytes(invalid)
    reader_store = cls(path)
    writer_store = cls(path)
    entered_validation = threading.Event()
    release_validation = threading.Event()
    writer_done = threading.Event()
    reader_errors = []
    validator_name = f"validate_{_name}_document"
    original_validator = getattr(module, validator_name)

    def pause_on_invalid(document):
        try:
            return original_validator(document)
        except ValueError:
            entered_validation.set()
            assert release_validation.wait(5)
            raise

    monkeypatch.setattr(module, validator_name, pause_on_invalid)
    reader = threading.Thread(
        target=lambda: _capture_exception(reader_errors, reader_store.list))
    valid = {
        "version": 1,
        key: [_valid_seed_record(key, "replacement")],
    }
    writer = threading.Thread(
        target=lambda: (writer_store._write(valid), writer_done.set()))
    try:
        reader.start()
        assert entered_validation.wait(5)
        writer.start()
        assert not writer_done.wait(0.25), (
            "writer replaced the pathname while an invalid reader still owned it")
        release_validation.set()
        reader.join(5)
        writer.join(5)
    finally:
        release_validation.set()
        reader.join(5) if reader.is_alive() else None
        writer.join(5) if writer.is_alive() else None

    assert len(reader_errors) == 1
    assert isinstance(reader_errors[0], PersistentStateError)
    assert writer_done.is_set()
    assert json.loads(path.read_text(encoding="utf-8")) == valid
    backups = list(tmp_path.glob(f"{_name}.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == invalid


def _capture_exception(errors, fn):
    try:
        fn()
    except BaseException as error:
        errors.append(error)


def test_queue_plain_dict_input_is_canonical_before_cache_and_persistence(tmp_path):
    path = tmp_path / "queue.json"
    journal = jobstore.DownloadJournal(path)
    record = journal.add({
        "urls": "https://youtu.be/abcdefghijk",
        "parallel": "false",
        "workers": "2",
        "future_option": {"keep": True},
    })

    expected = {
        "urls": ["https://youtu.be/abcdefghijk"],
        "parallel": False,
        "workers": 2,
        "future_option": {"keep": True},
    }
    assert {key: record["spec"][key] for key in expected} == expected
    assert jobstore.DownloadJournal(path).get(record["id"])["spec"] == record["spec"]


def test_watch_inputs_are_canonical_before_cache_and_persistence(tmp_path):
    path = tmp_path / "watches.json"
    store = watchstore.WatchStore(path)
    record = store.add(
        "UC" + "c" * 22,
        defaults={
            "parallel": "false",
            "workers": "2",
            "future_option": {"keep": True},
        },
        filters={
            "min_duration": "60",
            "max_duration": "120",
            "future_filter": ["keep"],
        },
    )

    fresh = watchstore.WatchStore(path).get(record["id"])
    assert fresh["defaults"]["parallel"] is False
    assert fresh["defaults"]["workers"] == 2
    assert fresh["defaults"]["future_option"] == {"keep": True}
    assert fresh["filters"] == {
        "min_duration": 60,
        "max_duration": 120,
        "future_filter": ["keep"],
    }


@pytest.mark.parametrize("field", ["defaults", "filters"])
@pytest.mark.parametrize("value", [[], 0, ""], ids=["list", "zero", "empty-string"])
def test_watch_add_rejects_falsey_non_mapping_nested_settings(
        field, value, tmp_path):
    path = tmp_path / "watches.json"
    store = watchstore.WatchStore(path)

    with pytest.raises(ValueError, match="object"):
        store.add("UC" + "c" * 22, **{field: value})

    assert store.list() == []


def test_watch_add_preserves_explicit_empty_mapping_settings(tmp_path):
    path = tmp_path / "watches.json"
    record = watchstore.WatchStore(path).add(
        "UC" + "c" * 22,
        defaults={},
        filters={},
    )

    assert record["defaults"] == {}
    assert record["filters"] == {}
    fresh = watchstore.WatchStore(path).get(record["id"])
    assert fresh["defaults"] == {}
    assert fresh["filters"] == {}


@pytest.mark.parametrize("field", ["defaults", "filters"])
def test_watch_update_rejects_non_mapping_nested_settings_without_rewriting(
        field, tmp_path):
    path = tmp_path / "watches.json"
    store = watchstore.WatchStore(path)
    record = store.add("UC" + "c" * 22)
    before = store.get(record["id"])

    with pytest.raises(ValueError):
        store.update(record["id"], **{field: []})

    assert store.get(record["id"]) == before
    assert watchstore.WatchStore(path).get(record["id"]) == before


@pytest.mark.parametrize("cls,key,_name", STORES)
def test_invalid_mutation_cannot_poison_cache_or_durable_state(
        cls, key, _name, tmp_path):
    path = tmp_path / f"{_name}.json"
    if key == "jobs":
        store = cls(path)
        record = store.add(JobSpec(urls=["https://youtu.be/abcdefghijk"]))
        mutate = lambda: store.update(record["id"], status=[])
    else:
        store = cls(path)
        record = store.add("UC" + "c" * 22)
        mutate = lambda: store.update(record["id"], seen="not-a-list")

    with pytest.raises(ValueError):
        mutate()

    assert path.exists()
    assert list(tmp_path.glob("*.corrupt-*.json")) == []
    current = store.get(record["id"])
    fresh = cls(path).get(record["id"])
    assert current == fresh
    assert current["status" if key == "jobs" else "seen"] == (
        "queued" if key == "jobs" else [])


@pytest.mark.parametrize("cls,key,_name", STORES)
def test_returned_nested_values_cannot_poison_same_instance_cache(
        cls, key, _name, tmp_path):
    path = tmp_path / f"{_name}.json"
    store = cls(path)
    if key == "jobs":
        record = store.add(JobSpec(urls=["https://youtu.be/abcdefghijk"]))
        record["spec"]["urls"].append("poison")
        store.get(record["id"])["spec"]["urls"].append("poison-2")
        assert store.get(record["id"])["spec"]["urls"] == [
            "https://youtu.be/abcdefghijk"]
    else:
        record = store.add("UC" + "c" * 22)
        record["defaults"]["future"] = "poison"
        store.get(record["id"])["defaults"]["future"] = "poison-2"
        assert "future" not in store.get(record["id"])["defaults"]


def test_batch_apply_poll_does_not_persist_an_unverified_document(tmp_path, monkeypatch):
    """The RSS poller must not write a document it could not verifiably read.

    Writing unconditionally is what erased every watch: a bad read returned empty, and the
    unconditional _write then made that empty document durable.
    """
    store, p = _make(
        watchstore.WatchStore,
        str(tmp_path),
        "watches",
        seeded=[_watch_record(id="w1", channel_id="c1")],
    )
    wrote = []
    monkeypatch.setattr(store, "_write", lambda data: wrote.append(data))
    # results reference a watch id that is NOT in the document (the shape a bad/empty read produces)
    store.batch_apply_poll({"w_missing": ([], None)})
    assert wrote == [], "persisted a document with no matching watch — this is the wipe path"


def test_batch_apply_poll_still_writes_for_a_real_watch(tmp_path):
    """Guard must not be so strict that a legitimate poll stops persisting."""
    store, _ = _make(
        watchstore.WatchStore,
        str(tmp_path),
        "watches",
        seeded=[_watch_record(id="w1", channel_id="c1")],
    )
    store.batch_apply_poll({"w1": ([{"id": "vid1", "title": "t", "url": "u"}], None)})
    # the poll was recorded durably (last_checked set, or the video landed in pending)
    w = store.get("w1")
    assert w["last_checked"] is not None or w["pending"], "a real poll was not persisted"
