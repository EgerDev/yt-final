import pytest
import os
import time
import threading
import jobstore
from jobspec import JobSpec
from state_validation import PersistentStateError

from jobstore import DownloadJournal

def test_msvcrt_lock_simulation(monkeypatch, tmp_path):
    """Simulate Windows msvcrt locking during a queue read-modify-write race."""
    q_file = tmp_path / "queue.json"
    journal = DownloadJournal(str(q_file))
    
    monkeypatch.setattr(os, "name", "nt")
    
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2
        
        def __init__(self):
            self.locked = False
            
        def locking(self, fd, mode, nbytes):
            if mode == self.LK_NBLCK:
                if self.locked:
                    raise OSError("File locked")
                self.locked = True
            elif mode == self.LK_UNLCK:
                self.locked = False

    fake_msvcrt = FakeMsvcrt()
    import sys
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    
    journal.add(JobSpec(urls=["https://youtu.be/abcdefghijk"]))
    
    def worker1():
        with journal._file_lock():
            data = journal._read()
            time.sleep(0.1)
            rec = journal._new_job_record(
                JobSpec(urls=["https://youtu.be/bbbbbbbbbbb"]), "w1")
            rec["id"] = "w1"
            data["jobs"].append(rec)
            journal._write(data)
            
    def worker2():
        time.sleep(0.02)
        with journal._file_lock():
            data = journal._read()
            rec = journal._new_job_record(
                JobSpec(urls=["https://youtu.be/ccccccccccc"]), "w2")
            rec["id"] = "w2"
            data["jobs"].append(rec)
            journal._write(data)

    t1 = threading.Thread(target=worker1)
    t2 = threading.Thread(target=worker2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    jobs = journal.list()
    assert len(jobs) == 3
    assert jobs[1]["id"] == "w1"
    assert jobs[2]["id"] == "w2"


# Unlike the lock simulation above, this test drives the CORRUPT branch of _read(), which calls
# state_validation.quarantine_corrupt_state() -> Path(path). pathlib picks its flavour from
# os.name at call time, so the os.name="nt" simulation makes that Path() a WindowsPath, which a
# POSIX host refuses to instantiate ("cannot instantiate 'WindowsPath' on your system"). The
# quarantine-under-lock contract therefore can only be exercised on a real Windows host.
@pytest.mark.skipif(
    os.name != "nt",
    reason="quarantine path builds a pathlib.Path under the os.name='nt' simulation; "
           "POSIX cannot instantiate WindowsPath",
)
def test_invalid_queue_is_quarantined_while_msvcrt_lock_is_held(
        monkeypatch, tmp_path):
    q_file = tmp_path / "queue.json"
    original = b'{"version": 1, "jobs": "invalid"}'
    q_file.write_bytes(original)
    journal = DownloadJournal(str(q_file))
    monkeypatch.setattr(os, "name", "nt")

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.locked = False

        def locking(self, _fd, mode, _nbytes):
            if mode == self.LK_NBLCK:
                if self.locked:
                    raise OSError("File locked")
                self.locked = True
            elif mode == self.LK_UNLCK:
                self.locked = False

    fake_msvcrt = FakeMsvcrt()
    import sys
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    real_quarantine = __import__(
        "state_validation").quarantine_corrupt_state
    observed = []

    def quarantine_while_locked(path):
        observed.append(fake_msvcrt.locked)
        return real_quarantine(path)

    monkeypatch.setattr(
        jobstore, "quarantine_corrupt_state", quarantine_while_locked, raising=False)

    with pytest.raises(PersistentStateError):
        journal.add(JobSpec(urls=["https://youtu.be/abcdefghijk"]))

    assert observed == [True]
    assert fake_msvcrt.locked is False
    assert not q_file.exists()
