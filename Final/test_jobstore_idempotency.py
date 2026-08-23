"""Durable request-id behavior at the DownloadJournal transaction boundary."""

import multiprocessing
import os
import re
import threading

import pytest

import jobstore
from jobspec import JobSpec
from jobstore import DownloadJournal


def _spec(url="https://youtu.be/abcdefghijk"):
    return JobSpec(urls=[url])


def _spawn_add(path, request_id, ready, start, results):
    """Spawn-safe worker: each process owns a distinct journal instance."""
    ready.put(True)
    if not start.wait(10):
        results.put({"error": "start gate timed out"})
        return
    try:
        results.put(DownloadJournal(path).add(
            _spec(), request_id=request_id))
    except BaseException as error:
        results.put({"error": f"{type(error).__name__}: {error}"})


def _spawn_idempotent_winner(path, request_id, entered, release, results):
    """Hold one request's validation open so a process contender queues behind it."""
    def factory():
        entered.set()
        if not release.wait(15):
            raise TimeoutError("winner validation release timed out")
        return _spec("https://youtu.be/winnerabcde")

    try:
        record = DownloadJournal(path).add_idempotent(request_id, factory)
        results.put({"role": "winner", "record": record})
    except BaseException as error:
        results.put({
            "role": "winner",
            "error": f"{type(error).__name__}: {error}",
        })


def _spawn_idempotent_loser(
        path, request_id, attempting, factory_called, results):
    """A same-key loser must return the winner without invoking this factory."""
    def factory():
        factory_called.set()
        return _spec("https://youtu.be/loserabcdef")

    attempting.set()
    try:
        record = DownloadJournal(path).add_idempotent(request_id, factory)
        results.put({"role": "loser", "record": record})
    except BaseException as error:
        results.put({
            "role": "loser",
            "error": f"{type(error).__name__}: {error}",
        })


class _FactoryAbort(BaseException):
    """A non-Exception failure used to prove keyed-lock cleanup is unconditional."""


def test_same_request_id_returns_the_first_durable_row_and_copies(tmp_path):
    path = tmp_path / "queue.json"
    journal = DownloadJournal(path)

    first = journal.add(_spec(), request_id="same-key")
    replay = journal.add(
        _spec("https://youtu.be/zyxwvutsrqp"),
        label="changed",
        request_id="same-key",
    )

    assert replay == first
    assert replay is not first
    assert journal.list() == [first]
    replay["spec"]["urls"].append("https://example.test/poison")
    assert DownloadJournal(path).get(first["id"]) == first


def test_distinct_and_omitted_request_ids_allocate_distinct_rows(tmp_path):
    journal = DownloadJournal(tmp_path / "queue.json")

    keyed_a = journal.add(_spec(), request_id="distinct-a")
    keyed_b = journal.add(_spec(), request_id="distinct-b")
    omitted_a = journal.add(_spec())
    omitted_b = journal.add(_spec())

    assert len({keyed_a["id"], keyed_b["id"], omitted_a["id"], omitted_b["id"]}) == 4
    assert keyed_a["request_id"] == "distinct-a"
    assert keyed_b["request_id"] == "distinct-b"
    assert "request_id" not in omitted_a
    assert "request_id" not in omitted_b


def test_same_instance_threads_commit_one_row_for_one_request_id(tmp_path):
    journal = DownloadJournal(tmp_path / "queue.json")
    barrier = threading.Barrier(8)
    results = []
    errors = []

    def add():
        try:
            barrier.wait()
            results.append(journal.add(_spec(), request_id="thread-key"))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=add) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert len(results) == 8
    assert len({record["id"] for record in results}) == 1
    assert len(journal.list()) == 1


def test_separate_journal_instances_commit_one_row_for_one_request_id(tmp_path):
    path = tmp_path / "queue.json"
    journals = [DownloadJournal(path), DownloadJournal(path)]
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def add(journal):
        try:
            barrier.wait()
            results.append(journal.add(_spec(), request_id="instance-key"))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=add, args=(journal,)) for journal in journals]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert len({record["id"] for record in results}) == 1
    assert len(DownloadJournal(path).list()) == 1


def test_spawned_processes_commit_one_row_for_one_request_id_without_test_sleeps(
        tmp_path):
    """Use spawn on every OS so this exercises the real Windows launch path too."""
    path = tmp_path / "queue.json"
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_spawn_add,
            args=(path, "process-key", ready, start, results),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    assert [ready.get(timeout=10) for _ in processes] == [True, True]
    start.set()
    records = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(10)

    assert not [process for process in processes if process.is_alive()]
    assert [process.exitcode for process in processes] == [0, 0]
    assert not [record for record in records if "error" in record]
    assert len({record["id"] for record in records}) == 1
    assert len(DownloadJournal(path).list()) == 1


def test_blocked_validation_does_not_hold_the_queue_global_lock(tmp_path):
    """Unbounded DNS/path validation for key A cannot stall unrelated queue work."""
    path = tmp_path / "queue.json"
    journal = DownloadJournal(path)
    seed = journal.add(_spec("https://youtu.be/seedabcdefg"))
    validation_entered = threading.Event()
    release_validation = threading.Event()
    unrelated_progressed = threading.Event()
    winner_records = []
    observations = {}
    errors = []

    def blocked_factory():
        validation_entered.set()
        if not release_validation.wait(10):
            raise TimeoutError("blocked validation release timed out")
        return _spec("https://youtu.be/blockedabcd")

    def add_blocked_key():
        try:
            winner_records.append(
                journal.add_idempotent("blocked-key-a", blocked_factory))
        except BaseException as error:
            errors.append(error)

    def do_unrelated_work():
        try:
            peer = DownloadJournal(path)
            observations["listed"] = peer.list()
            observations["updated"] = peer.update(
                seed["id"], label="updated while validation waits")
            observations["claimed"] = peer.claim_next()
            observations["other_key"] = peer.add_idempotent(
                "other-key-b",
                lambda: _spec("https://youtu.be/otherabcdef"),
            )
            unrelated_progressed.set()
        except BaseException as error:
            errors.append(error)

    blocked_thread = threading.Thread(target=add_blocked_key)
    blocked_thread.start()
    assert validation_entered.wait(5)

    unrelated_thread = threading.Thread(target=do_unrelated_work)
    unrelated_thread.start()
    try:
        assert unrelated_progressed.wait(1), (
            "list/update/claim and a distinct request key stalled behind "
            "another key's validation"
        )
    finally:
        release_validation.set()
        blocked_thread.join(10)
        unrelated_thread.join(10)

    assert not blocked_thread.is_alive()
    assert not unrelated_thread.is_alive()
    assert errors == []
    assert observations["listed"][0]["id"] == seed["id"]
    assert observations["updated"]["label"] == "updated while validation waits"
    assert observations["claimed"]["id"] == seed["id"]
    assert observations["other_key"]["request_id"] == "other-key-b"
    assert winner_records[0]["request_id"] == "blocked-key-a"


def test_same_key_threads_across_instances_skip_loser_validation(tmp_path):
    path = tmp_path / "queue.json"
    winner_journal = DownloadJournal(path)
    loser_journal = DownloadJournal(path)
    validation_entered = threading.Event()
    release_validation = threading.Event()
    loser_attempting = threading.Event()
    loser_finished = threading.Event()
    loser_factory_called = threading.Event()
    records = {}
    errors = []

    def winner_factory():
        validation_entered.set()
        if not release_validation.wait(10):
            raise TimeoutError("winner validation release timed out")
        return _spec("https://youtu.be/winnerabcde")

    def loser_factory():
        loser_factory_called.set()
        return _spec("https://youtu.be/loserabcdef")

    def add_winner():
        try:
            records["winner"] = winner_journal.add_idempotent(
                "shared-thread-key", winner_factory)
        except BaseException as error:
            errors.append(error)

    def add_loser():
        loser_attempting.set()
        try:
            records["loser"] = loser_journal.add_idempotent(
                "shared-thread-key", loser_factory)
        except BaseException as error:
            errors.append(error)
        finally:
            loser_finished.set()

    winner_thread = threading.Thread(target=add_winner)
    winner_thread.start()
    assert validation_entered.wait(5)
    loser_thread = threading.Thread(target=add_loser)
    loser_thread.start()
    assert loser_attempting.wait(5)
    try:
        assert not loser_factory_called.wait(0.25)
        assert not loser_finished.is_set()
    finally:
        release_validation.set()
        winner_thread.join(10)
        loser_thread.join(10)

    assert not winner_thread.is_alive()
    assert not loser_thread.is_alive()
    assert errors == []
    assert records["loser"] == records["winner"]
    assert loser_factory_called.is_set() is False
    assert DownloadJournal(path).list() == [records["winner"]]


def test_same_owner_can_reenter_its_request_gate_without_deadlock(tmp_path):
    path = tmp_path / "queue.json"
    journal = DownloadJournal(path)
    nested_records = []

    def outer_factory():
        nested_records.append(journal.add(
            _spec("https://youtu.be/nestedabcde"),
            request_id="reentrant-request-key",
        ))
        return _spec("https://youtu.be/discardabcd")

    outer = journal.add_idempotent(
        "reentrant-request-key",
        outer_factory,
    )

    assert outer == nested_records[0]
    assert outer["spec"]["urls"] == ["https://youtu.be/nestedabcde"]
    assert journal.list() == [outer]
    assert jobstore._REQUEST_GATES == {}


@pytest.mark.parametrize(
    "start_method",
    ["spawn"] + (["fork"] if os.name != "nt" else []),
)
def test_same_key_process_loser_skips_validation(
        tmp_path, start_method):
    path = tmp_path / "queue.json"
    context = multiprocessing.get_context(start_method)
    entered = context.Event()
    release = context.Event()
    loser_attempting = context.Event()
    loser_factory_called = context.Event()
    results = context.Queue()
    winner = context.Process(
        target=_spawn_idempotent_winner,
        args=(path, "shared-process-key", entered, release, results),
    )
    loser = context.Process(
        target=_spawn_idempotent_loser,
        args=(
            path,
            "shared-process-key",
            loser_attempting,
            loser_factory_called,
            results,
        ),
    )

    winner.start()
    assert entered.wait(10)
    loser.start()
    assert loser_attempting.wait(10)
    try:
        assert not loser_factory_called.wait(0.5)
    finally:
        release.set()
    records = [results.get(timeout=15) for _ in range(2)]
    winner.join(10)
    loser.join(10)

    assert not winner.is_alive()
    assert not loser.is_alive()
    assert winner.exitcode == 0
    assert loser.exitcode == 0
    assert not [record for record in records if "error" in record]
    by_role = {record["role"]: record["record"] for record in records}
    assert by_role["loser"] == by_role["winner"]
    assert loser_factory_called.is_set() is False
    assert DownloadJournal(path).list() == [by_role["winner"]]


@pytest.mark.parametrize("error_type", [RuntimeError, _FactoryAbort])
def test_factory_failures_release_keyed_lock_without_reserving_a_row(
        tmp_path, error_type):
    path = tmp_path / "queue.json"
    journal = DownloadJournal(path)

    def failing_factory():
        raise error_type("validation failed")

    with pytest.raises(error_type, match="validation failed"):
        journal.add_idempotent("factory-failure-key", failing_factory)

    assert journal.get_by_request_id("factory-failure-key") is None
    assert journal.list() == []
    assert jobstore._REQUEST_GATES == {}

    recovered = DownloadJournal(path).add_idempotent(
        "factory-failure-key",
        lambda: _spec("https://youtu.be/recoveredabc"),
    )

    assert recovered["request_id"] == "factory-failure-key"
    assert journal.list() == [recovered]
    assert jobstore._REQUEST_GATES == {}


def test_request_gate_file_name_is_hashed_stable_and_never_removed(tmp_path):
    path = tmp_path / "queue.json"
    request_id = "path/../unsafe key with spaces"
    journal = DownloadJournal(path)

    first = journal.add(_spec(), request_id=request_id)
    gate_dir = tmp_path / ".request-locks"
    gate_files = list(gate_dir.iterdir())

    assert len(gate_files) == 1
    gate_path = gate_files[0]
    assert re.fullmatch(r"[0-9a-f]{64}\.lock", gate_path.name)
    assert gate_path.resolve().parent == gate_dir.resolve()
    inode = (gate_path.stat().st_dev, gate_path.stat().st_ino)

    replay = DownloadJournal(path).add(
        _spec("https://youtu.be/ignoredabcd"),
        request_id=request_id,
    )

    assert replay == first
    assert gate_path.exists()
    assert (gate_path.stat().st_dev, gate_path.stat().st_ino) == inode
    assert list(gate_dir.iterdir()) == [gate_path]
    assert jobstore._REQUEST_GATES == {}
