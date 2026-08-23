"""POSIX fork regressions for inherited durable-store lock state."""

import json
import os
import select
import tempfile
import unittest
from pathlib import Path

import jobstore
import watchstore
from jobspec import JobSpec


@unittest.skipUnless(hasattr(os, "fork"), "requires POSIX os.fork")
class ForkedStoreLockTests(unittest.TestCase):
    def _run_inherited_lock_case(self, store_kind):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{store_kind}.json"
            if store_kind == "queue":
                store = jobstore.DownloadJournal(path)
                store.add(
                    JobSpec(urls=["https://youtu.be/seed0000000"]),
                    label="seed",
                )
                key = "jobs"
                parent_record = store._new_job_record(
                    JobSpec(urls=["https://youtu.be/parent00000"]),
                    "parent",
                )
                parent_record["id"] = "parent"

                def child_mutation():
                    store.add(
                        JobSpec(urls=["https://youtu.be/child000000"]),
                        label="child",
                    )

                def durable_markers():
                    return {
                        record["label"]
                        for record in jobstore.DownloadJournal(path).list()
                    }
            else:
                store = watchstore.WatchStore(path)
                store.add("UC" + ("s" * 22), label="seed")
                key = "watches"
                parent_record = {
                    "id": "parent",
                    "channel_id": "UC" + ("p" * 22),
                    "label": "parent",
                    "url": None,
                    "auto_queue": False,
                    "defaults": {},
                    "filters": {},
                    "seen": [],
                    "pending": [],
                    "created_at": 1.0,
                    "last_checked": None,
                    "last_error": None,
                }

                def child_mutation():
                    store.add("UC" + ("c" * 22), label="child")

                def durable_markers():
                    return {
                        record["label"]
                        for record in watchstore.WatchStore(path).list()
                    }

            ready_read, ready_write = os.pipe()
            done_read, done_write = os.pipe()
            child_pid = None
            child_was_blocked = False
            child_message = b""
            exit_code = None
            try:
                # Fork while the store object carries an acquired mutex, depth=1,
                # an open sidecar description, and a warm read cache.
                with store._mutex, store._file_lock():
                    stale_parent_data = store._read()
                    child_pid = os.fork()
                    if child_pid == 0:
                        os.close(ready_read)
                        os.close(done_read)
                        try:
                            os.write(ready_write, b"R")
                            child_mutation()
                            os.write(done_write, b"D")
                            os._exit(0)
                        except BaseException as error:
                            message = (
                                f"E:{type(error).__name__}:{error}"
                            ).encode("utf-8", "replace")
                            os.write(done_write, message[:2000])
                            os._exit(1)

                    os.close(ready_write)
                    os.close(done_write)
                    self.assertEqual(os.read(ready_read, 1), b"R")
                    child_was_blocked = not bool(
                        select.select([done_read], [], [], 0.35)[0])
                    stale_parent_data[key].append(parent_record)
                    store._write(stale_parent_data)

                readable = select.select([done_read], [], [], 10)[0]
                self.assertTrue(readable, "forked child did not finish after lock release")
                child_message = os.read(done_read, 2000)
                _, status = os.waitpid(child_pid, 0)
                child_pid = None
                exit_code = os.waitstatus_to_exitcode(status)
            finally:
                for fd in (ready_read, done_read):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if child_pid:
                    try:
                        os.kill(child_pid, 9)
                    except ProcessLookupError:
                        pass
                    os.waitpid(child_pid, 0)

            self.assertTrue(
                child_was_blocked,
                "child reused inherited lock depth instead of reacquiring the sidecar",
            )
            self.assertEqual(exit_code, 0, child_message.decode("utf-8", "replace"))
            self.assertEqual(child_message, b"D")
            self.assertTrue({"parent", "child"}.issubset(durable_markers()))
            # The final document must remain independently parseable.
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)

    def test_forked_queue_child_reacquires_and_preserves_both_updates(self):
        self._run_inherited_lock_case("queue")

    def test_forked_watch_child_reacquires_and_preserves_both_updates(self):
        self._run_inherited_lock_case("watch")

    def test_forked_watch_child_discards_inherited_poll_mutex(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.json"
            store = watchstore.WatchStore(path)
            watch = store.add("UC" + ("s" * 22), label="seed")
            done_read, done_write = os.pipe()
            child_pid = None
            try:
                with store._poll_lock:
                    child_pid = os.fork()
                    if child_pid == 0:
                        os.close(done_read)
                        try:
                            store.apply_poll(watch["id"], [])
                            os.write(done_write, b"D")
                            os._exit(0)
                        except BaseException as error:
                            os.write(
                                done_write,
                                f"E:{type(error).__name__}:{error}".encode(
                                    "utf-8", "replace")[:2000],
                            )
                            os._exit(1)
                    os.close(done_write)
                    readable = select.select([done_read], [], [], 1)[0]
                    message = os.read(done_read, 2000) if readable else b""

                self.assertTrue(
                    readable,
                    "child deadlocked on the poll mutex inherited from its parent",
                )
                _, status = os.waitpid(child_pid, 0)
                child_pid = None
                self.assertEqual(
                    os.waitstatus_to_exitcode(status),
                    0,
                    message.decode("utf-8", "replace"),
                )
                self.assertEqual(message, b"D")
            finally:
                try:
                    os.close(done_read)
                except OSError:
                    pass
                if child_pid:
                    try:
                        os.kill(child_pid, 9)
                    except ProcessLookupError:
                        pass
                    os.waitpid(child_pid, 0)


if __name__ == "__main__":
    unittest.main()
