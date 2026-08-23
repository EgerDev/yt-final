#!/usr/bin/env python3
"""
DownloadJournal — a durable, dependency-free job queue (keystone C1).

This is the "full-stack" backbone the innovation review chose over a database or a
heavyweight server: a single JSON file at ~/.yt_final/queue.json, written atomically
(temp + os.replace), with a single-writer pidfile lock so two `queue run` processes
can't corrupt it. It replaces the dead in-memory `self.history` with intent that
survives reboots, and it is what the manifest (`batch`) and the HTTP server (`serve`)
both read and write.

Record shape (JSON):
{
  "id": "j_ab12cd34",
  "spec": { ...JobSpec.to_dict()... },
  "status": "queued|running|done|failed|cancelled",
  "attempt_count": 0,
  "last_error": null,
  "output_paths": [],
  "created_at": 1750000000.0,
  "updated_at": 1750000000.0,
  "label": "https://youtu.be/... @ 1080p/h264"
}
"""

import copy
import errno
import hashlib
import json
import os
import time
import uuid
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    from .secureio import append_private_line
    from .state_validation import (
        PersistentStateError,
        canonicalize_jobspec_fields,
        quarantine_corrupt_state,
        validate_queue_document,
        validate_request_id,
    )
except ImportError:
    from secureio import append_private_line
    from state_validation import (
        PersistentStateError,
        canonicalize_jobspec_fields,
        quarantine_corrupt_state,
        validate_queue_document,
        validate_request_id,
    )

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
_ACTIVE = {STATUS_QUEUED, STATUS_RUNNING}


class _RequestGateState:
    """One process-local owner/waiter group for a durable request-id lock."""

    def __init__(self):
        self.mutex = threading.RLock()
        self.users = 0
        self.depth = 0
        self.file = None


_REQUEST_GATES_PID = os.getpid()
_REQUEST_GATES_MUTEX = threading.Lock()
_REQUEST_GATES = {}


def _reset_request_gates_after_fork():
    """Replace inherited thread locks before a child can wait on them."""
    global _REQUEST_GATES_PID, _REQUEST_GATES_MUTEX, _REQUEST_GATES
    current_pid = os.getpid()
    if current_pid == _REQUEST_GATES_PID:
        return
    for gate in list(_REQUEST_GATES.values()):
        inherited_file = gate.file
        gate.file = None
        if inherited_file is not None:
            try:
                inherited_file.close()
            except (OSError, ValueError):
                pass
    _REQUEST_GATES = {}
    _REQUEST_GATES_MUTEX = threading.Lock()
    _REQUEST_GATES_PID = current_pid


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_request_gates_after_fork)


def default_dir():
    base = os.getenv("YT_FINAL_HOME")
    if base:
        return Path(base)
    return Path.home() / ".yt_final"


def _now():
    return time.time()


class JournalLockError(RuntimeError):
    pass


class DownloadJournal:
    def __init__(self, path=None):
        self.dir = (Path(path).parent if path else default_dir())
        self.path = Path(path) if path else (default_dir() / "queue.json")
        self.lock_path = self.path.with_suffix(".lock")
        self._flock_path = self.path.with_suffix(".flock")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._hold_lock = False
        self.lock_holder_pid = None   # pid blocking acquire_run_lock, for an actionable message
        # In-process mutex: the pidfile lock only guards across PROCESSES; within one process the
        # ThreadingHTTPServer runs many request threads + a worker that all read-modify-write the
        # JSON, so every mutator must hold this to keep its read->mutate->write atomic.
        self._mutex = threading.RLock()
        # `_read` and `_write` enforce the sidecar protocol themselves. Public mutators already
        # hold it around a whole read-modify-write transaction, so nesting must not reacquire the
        # non-reentrant OS lock. Holding this RLock for the whole context also serializes threads.
        self._file_mutex = threading.RLock()
        self._file_lock_depth = 0
        self._file_lock_file = None
        self._owner_pid = os.getpid()
        # Read cache: avoid re-parsing the whole file on every op. Invalidated by st_mtime change
        # (including cross-process writes, which bump mtime via os.replace).
        self._cache = None
        self._cache_mtime = None
        self._observed_state = False

    def _reset_after_fork_if_needed(self):
        """Discard synchronization/cache state inherited from another PID.

        This method must run before acquiring any possibly inherited mutex. A
        forked child closes only its duplicate of the parent's sidecar file
        description; the parent's duplicate and lock remain alive.
        """
        current_pid = os.getpid()
        if current_pid == self._owner_pid:
            return
        inherited_file = self._file_lock_file
        self._file_lock_file = None
        if inherited_file is not None:
            try:
                inherited_file.close()
            except (OSError, ValueError):
                pass
        self._mutex = threading.RLock()
        self._file_mutex = threading.RLock()
        self._file_lock_depth = 0
        self._cache = None
        self._cache_mtime = None
        self._hold_lock = False
        self.lock_holder_pid = None
        # Publish the new PID only after every inherited primitive is replaced.
        self._owner_pid = current_pid

    @contextmanager
    def _mutex_lock(self):
        self._reset_after_fork_if_needed()
        with self._mutex:
            yield

    @contextmanager
    def _transaction_lock(self):
        self._reset_after_fork_if_needed()
        with self._mutex, self._file_lock():
            yield

    @contextmanager
    def _request_gate(self, request_id):
        """Serialize one request id without holding the queue-global lock.

        The per-process registry handles threads and separate journal instances.
        A stable hashed sidecar handles other processes. Registry entries are
        reference-counted away after their last owner/waiter, but the sidecar is
        deliberately never unlinked because replacing a locked inode would let a
        later contender bypass its owner.
        """
        _reset_request_gates_after_fork()
        digest = hashlib.sha256(request_id.encode("ascii")).hexdigest()
        gate_path = self.dir / ".request-locks" / f"{digest}.lock"
        registry_key = os.path.normcase(os.path.abspath(gate_path))
        with _REQUEST_GATES_MUTEX:
            gate = _REQUEST_GATES.get(registry_key)
            if gate is None:
                gate = _RequestGateState()
                _REQUEST_GATES[registry_key] = gate
            gate.users += 1

        acquired_mutex = False
        try:
            gate.mutex.acquire()
            acquired_mutex = True
            if gate.depth:
                gate.depth += 1
                try:
                    yield
                finally:
                    gate.depth -= 1
                return

            gate_path.parent.mkdir(parents=True, exist_ok=True)
            gate_path.touch(exist_ok=True)
            fd = open(gate_path, "r+b")
            gate.file = fd
            locked = False
            try:
                if os.name == "nt":
                    import msvcrt
                    fd.seek(0)
                    while True:
                        try:
                            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                            locked = True
                            break
                        except OSError as error:
                            if error.errno not in (
                                    errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                                raise
                            time.sleep(0.02)
                else:
                    import fcntl
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX)
                            locked = True
                            break
                        except InterruptedError:
                            continue
                gate.depth = 1
                try:
                    yield
                finally:
                    gate.depth = 0
            finally:
                try:
                    if locked:
                        if os.name == "nt":
                            import msvcrt
                            fd.seek(0)
                            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    if gate.file is fd:
                        gate.file = None
                    fd.close()
        finally:
            if acquired_mutex:
                gate.mutex.release()
            with _REQUEST_GATES_MUTEX:
                gate.users -= 1
                if (gate.users == 0
                        and _REQUEST_GATES.get(registry_key) is gate):
                    del _REQUEST_GATES[registry_key]

    # ---- cross-process file lock (.flock sidecar) --------------------- #
    @contextmanager
    def _file_lock(self, timeout=5.0):
        # Lock a sidecar (.flock) rather than queue.json itself, so the OS lock never blocks the
        # os.replace that swaps queue.json into place during an atomic write. The in-process RLock
        # is held for the full context: another thread cannot mistake our depth for its own.
        self._reset_after_fork_if_needed()
        with self._file_mutex:
            if self._file_lock_depth:
                self._file_lock_depth += 1
                try:
                    yield
                finally:
                    self._file_lock_depth -= 1
                return
            self._flock_path.touch(exist_ok=True)
            fd = open(self._flock_path, "r+b")
            self._file_lock_file = fd
            locked = False
            try:
                if os.name == "nt":
                    import msvcrt
                    deadline = time.monotonic() + timeout
                    while True:
                        try:
                            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                            locked = True
                            break
                        except OSError:
                            if time.monotonic() > deadline:
                                raise TimeoutError(f"lock {self._flock_path} timeout")
                            time.sleep(0.02)
                else:
                    import fcntl
                    deadline = time.monotonic() + timeout
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            locked = True
                            break
                        except OSError:
                            if time.monotonic() > deadline:
                                raise TimeoutError(f"lock {self._flock_path} timeout")
                            time.sleep(0.02)
                self._file_lock_depth = 1
                try:
                    yield
                finally:
                    self._file_lock_depth = 0
            finally:
                try:
                    # only release if we actually acquired it — unlocking an unlocked region raises
                    # OSError on Windows and would mask the original TimeoutError.
                    if locked:
                        if os.name == "nt":
                            import msvcrt
                            fd.seek(0)
                            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    if self._file_lock_file is fd:
                        self._file_lock_file = None
                    fd.close()

    # ---- low-level persistence (atomic) ------------------------------- #
    def _read(self):
        # NOTE (Windows): the MUTATING callers of _read() are read-modify-WRITE under the same lock, so
        # returning an empty journal here PERSISTS the wipe on the next mutation. A Windows sharing
        # violation (AV / backup / sync agent holding queue.json) surfaces as PermissionError, which
        # is an OSError — it must NOT be laundered into "corrupt, start clean". Only a genuinely
        # unparseable file may reset the journal; transient OS errors retry, then re-raise.
        # No .exists() pre-check: let the os.stat below raise so a stat-level PermissionError reaches
        # the handlers instead of being turned into False by exists().
        with self._file_lock():
            proven_stale = False
            for attempt in range(8):
                stat_succeeded = False
                try:
                    st = os.stat(self.path)
                    stat_succeeded = True
                    self._observed_state = True
                    # Device/inode improve best-effort replacement detection; mtime/size
                    # detect in-place changes and preserve the coarse-NTFS cache guard.
                    key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
                    if self._cache is not None and self._cache_mtime == key:
                        return copy.deepcopy(self._cache)
                    if self._cache is not None:
                        proven_stale = True
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    try:
                        data = validate_queue_document(data)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                        # Best-effort re-stat immediately before quarantine. Cooperating writers
                        # cannot enter because we hold the sidecar; a non-cooperating writer can
                        # still race the following portable pathname rename.
                        current = os.stat(self.path)
                        current_key = (
                            current.st_dev,
                            current.st_ino,
                            current.st_mtime_ns,
                            current.st_size,
                        )
                        if current_key != key:
                            proven_stale = True
                            if attempt == 7:
                                raise
                            time.sleep(0.05)
                            continue
                        self._cache = None
                        self._cache_mtime = None
                        backup = quarantine_corrupt_state(self.path)
                        raise PersistentStateError(
                            self.path, backup, str(error)) from error
                    self._cache = copy.deepcopy(data)
                    self._cache_mtime = key
                    return copy.deepcopy(data)
                except FileNotFoundError:
                    # Only an initial stat absence is first-run bootstrap. If stat already found
                    # the file (or this instance observed it earlier), disappearance is a race.
                    if not stat_succeeded and not self._observed_state:
                        return {"version": 1, "jobs": []}
                    if attempt == 7:
                        raise
                    proven_stale = True
                    time.sleep(0.05)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                    # json.load failed before schema validation; apply the same best-effort guard.
                    current = os.stat(self.path)
                    current_key = (
                        current.st_dev,
                        current.st_ino,
                        current.st_mtime_ns,
                        current.st_size,
                    )
                    if current_key != key:
                        proven_stale = True
                        if attempt == 7:
                            raise
                        time.sleep(0.05)
                        continue
                    self._cache = None
                    self._cache_mtime = None
                    backup = quarantine_corrupt_state(self.path)
                    raise PersistentStateError(
                        self.path, backup, str(error)) from error
                except OSError:
                    if attempt == 7:
                        if self._cache is not None and not proven_stale:
                            return copy.deepcopy(self._cache)
                        raise
                    time.sleep(0.05)

    def _write(self, data):
        """Atomic write: temp file in same dir + os.replace (rename is atomic on one volume)."""
        with self._file_lock():
            snapshot = copy.deepcopy(data)
            validate_queue_document(snapshot)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.dir), prefix=".queue.", suffix=".tmp")
            try:
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(snapshot, f, indent=2, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                    for attempt in range(8):
                        try:
                            os.replace(tmp, self.path)
                            break
                        except PermissionError:
                            if attempt == 7:
                                raise
                            time.sleep(0.05)
                    self._observed_state = True
                    self._cache = copy.deepcopy(snapshot)
                    try:
                        st = os.stat(self.path)
                        self._cache_mtime = (
                            st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
                    except OSError:
                        self._cache_mtime = None
                except BaseException:
                    self._cache = None
                    self._cache_mtime = None
                    raise
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    # ---- single-writer run lock (pidfile) ----------------------------- #
    def acquire_run_lock(self):
        """Refuse to start a second `queue run`/`serve` driver. Steals a stale lock
        whose PID is no longer alive. Returns True on success."""
        self._reset_after_fork_if_needed()
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text().strip() or "0")
            except (ValueError, OSError):
                pid = 0
            if pid and _pid_alive(pid):
                # Record WHO blocked us so the caller can print an actionable message. This matters
                # most on Windows: closing the console (CTRL_CLOSE_EVENT) or Task Manager "End task"
                # skips release_run_lock entirely, and Windows recycles PIDs from a compact pool, so
                # an unrelated process can inherit the PID and make this lock look held forever.
                self.lock_holder_pid = pid
                return False
            self.lock_holder_pid = None
            # stale lock -> reclaim
            try:
                self.lock_path.unlink()
            except OSError:
                pass
        try:
            # O_EXCL makes creation fail if another process raced us.
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self._hold_lock = True
            # Auto-recover jobs left 'running' by a crashed driver. Safe here: the pidfile lock is
            # separate from the .flock, so recover_orphaned_running can take _file_lock without
            # deadlocking against anything held by acquire_run_lock.
            try:
                self.recover_orphaned_running()
            except Exception:
                pass
            return True
        except FileExistsError:
            # Lost the create race: recover the winner's pid so the caller's message is actionable
            # rather than "pid None".
            try:
                self.lock_holder_pid = int(self.lock_path.read_text().strip() or "0") or None
            except (ValueError, OSError):
                self.lock_holder_pid = None
            return False

    def release_run_lock(self):
        self._reset_after_fork_if_needed()
        if self._hold_lock and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except OSError:
                pass
        self._hold_lock = False

    def __enter__(self):
        if not self.acquire_run_lock():
            raise JournalLockError(
                f"Another queue runner holds {self.lock_path}. "
                f"Only one `queue run`/`serve` may write the journal at a time."
            )
        return self

    def __exit__(self, *exc):
        self.release_run_lock()
        return False

    # ---- public API --------------------------------------------------- #
    @staticmethod
    def _new_job_record(spec, label, request_id=None):
        """Build a fresh queued-job record. Shared by add()/add_if_url_absent() so they can never
        drift on which fields a new job carries."""
        spec_dict = canonicalize_jobspec_fields(spec)
        if label is None and hasattr(spec, "summary"):
            try:
                label = spec.summary()
            except Exception:
                label = None
        record = {
            "id": "j_" + uuid.uuid4().hex[:8],
            "spec": spec_dict,
            "status": STATUS_QUEUED,
            "attempt_count": 0,
            "priority": 0,
            "last_error": None,
            "last_error_kind": None,
            "output_paths": [],
            "created_at": _now(),
            "updated_at": _now(),
            "label": label or "job",
        }
        if request_id is not None:
            record["request_id"] = request_id
        return record

    def add(self, spec, label=None, request_id=None):
        """Append a queued job, or replay the first row committed for *request_id*."""
        if request_id is not None:
            return self.add_idempotent(request_id, lambda: spec, label)
        with self._transaction_lock():
            data = self._read()
            rec = self._new_job_record(spec, label)
            data["jobs"].append(rec)
            self._maybe_prune(data)
            self._write(data)
        return copy.deepcopy(rec)

    def add_idempotent(self, request_id, spec_factory, label=None):
        """Resolve or create one request row without global-locking validation."""
        checked_request_id = validate_request_id(request_id)
        with self._request_gate(checked_request_id):
            existing = self._get_by_request_id_checked(checked_request_id)
            if existing is not None:
                return existing
            if not callable(spec_factory):
                raise TypeError("spec_factory must be callable")
            spec = spec_factory()
            rec = self._new_job_record(spec, label, checked_request_id)
            with self._transaction_lock():
                data = self._read()
                for existing in data["jobs"]:
                    if existing.get("request_id") == checked_request_id:
                        return copy.deepcopy(existing)
                data["jobs"].append(rec)
                self._maybe_prune(data)
                self._write(data)
            return copy.deepcopy(rec)

    def _get_by_request_id_checked(self, checked_request_id):
        """Look up an already-validated key under one short queue lock."""
        with self._mutex_lock():
            for job in self._read()["jobs"]:
                if job.get("request_id") == checked_request_id:
                    return copy.deepcopy(job)
        return None

    def get_by_request_id(self, request_id):
        """Return a copied live row for an exact validated idempotency key."""
        checked_request_id = validate_request_id(request_id)
        return self._get_by_request_id_checked(checked_request_id)

    def add_if_url_absent(self, spec, url, label=None):
        """Atomic add-if-absent: append the job ONLY when no existing job (any status) already
        carries `url`. The check and the append happen under ONE lock, closing the list()-then-add()
        race in the watch auto-enqueue path — two concurrent polls could each see the url absent and
        both add it, downloading the same video twice. Returns the new record, or None when a job
        for the url already exists (an idempotent no-op)."""
        rec = self._new_job_record(spec, label)
        with self._transaction_lock():
            data = self._read()
            for j in data["jobs"]:
                if url in ((j.get("spec") or {}).get("urls") or []):
                    return None                      # already queued/done -> skip
            data["jobs"].append(rec)
            self._maybe_prune(data)
            self._write(data)
        return rec

    def list(self, status=None):
        with self._mutex_lock():
            jobs = self._read()["jobs"]
            if status:
                jobs = [j for j in jobs if j.get("status") == status]
            # Return shallow COPIES — callers (e.g. the webserver injecting ephemeral 'progress')
            # must never mutate the cached dicts, or those keys leak into durable queue.json.
            return [dict(j) for j in jobs]

    def get(self, job_id):
        with self._mutex_lock():
            for j in self._read()["jobs"]:
                if j.get("id") == job_id:
                    # Return a shallow COPY (parity with list()) — callers must never mutate the
                    # cached dict, or ephemeral keys (e.g. webserver's 'progress') leak into queue.json.
                    return dict(j)
            return None

    def update(self, job_id, **fields):
        with self._transaction_lock():
            data = self._read()
            for j in data["jobs"]:
                if j.get("id") == job_id:
                    j.update(fields)
                    j["updated_at"] = _now()
                    self._write(data)
                    return j
        return None

    def set_status(self, job_id, status, error=None, output_paths=None, bump_attempt=False,
                   error_kind=None, result=None):
        with self._transaction_lock():
            data = self._read()
            for j in data["jobs"]:
                if j.get("id") == job_id:
                    # Compare-and-set: resolve a pending cancel WITH the terminal write, inside this
                    # one locked section (the caller decides the outcome from a snapshot taken
                    # milliseconds earlier). A late cancel only overrides a FAILED outcome -> cancelled.
                    # If the job actually COMPLETED ('done') it landed real bytes and run() already
                    # recorded history + fired the webhook as success, so a cancel that arrives after
                    # that is a no-op: the row stays 'done', agreeing with the file/history/webhook
                    # instead of contradicting them. (Cancels that land DURING the download instead
                    # raise DownloadCancelled from the body and never reach a 'done' proposal here.)
                    if status == STATUS_FAILED and j.get("cancel_requested"):
                        status = STATUS_CANCELLED
                        error = error_kind = None
                    j["status"] = status
                    if error is not None:
                        j["last_error"] = str(error)[:2000]
                    if error_kind is not None:
                        j["last_error_kind"] = str(error_kind)
                    # Clear stale failure info when a job transitions off 'failed' without a fresh
                    # error (e.g. a retried job that now succeeds), so 'done' records don't carry a
                    # contradictory last_error/last_error_kind from a previous attempt.
                    if status != STATUS_FAILED and error is None:
                        j["last_error"] = None
                        j["last_error_kind"] = None
                    if output_paths is not None:
                        j["output_paths"] = [str(p) for p in output_paths]
                    if result is not None and isinstance(result, dict):
                        # Durable post-run stats: engine used, avg/peak speed, elapsed, etc.
                        j["result"] = result
                    if bump_attempt:
                        j["attempt_count"] = int(j.get("attempt_count", 0)) + 1
                    if status != STATUS_RUNNING:
                        j.pop("cancel_requested", None)
                    j["updated_at"] = _now()
                    self._write(data)
                    return j
        return None

    def next_queued(self):
        with self._mutex_lock():
            for j in self._read()["jobs"]:
                if j.get("status") == STATUS_QUEUED:
                    return j
            return None

    def claim_next(self, max_attempts=5):
        """Atomically find the next queued job and mark it running (single locked critical section,
        so two threads never claim the same job). Skips jobs past max_attempts. Picks the highest
        priority, FIFO tie-break by created_at ascending. Returns None when the queue is paused (the
        gate only blocks NEW claims; a job already running is never touched). Returns the claimed
        record (already 'running') or None."""
        with self._transaction_lock():
            data = self._read()
            if data.get("paused"):
                return None
            candidates = [
                j for j in data["jobs"]
                if j.get("status") == STATUS_QUEUED
                and int(j.get("attempt_count", 0)) < max_attempts
            ]
            if not candidates:
                return None
            # Do NOT reorder data["jobs"] on disk — pick the winner with a key-scan so insertion
            # order (and the single-job FIFO contract) stays byte-identical.
            j = min(candidates, key=lambda j: (-int(j.get("priority", 0)),
                                               float(j.get("created_at", 0))))
            j["status"] = STATUS_RUNNING
            j["attempt_count"] = int(j.get("attempt_count", 0)) + 1
            j["updated_at"] = _now()
            self._write(data)
            return j
        return None

    def set_priority(self, job_id, priority):
        """Set a job's priority (higher runs first; FIFO tie-break). Returns the job or None."""
        with self._transaction_lock():
            data = self._read()
            for j in data["jobs"]:
                if j.get("id") == job_id:
                    # Coerce defensively: the /api/priority route passes whatever JSON value the
                    # client sent (string, null, list, ...). Bad input keeps the current priority
                    # instead of raising an unhandled ValueError/TypeError in the server.
                    try:
                        j["priority"] = int(priority)
                    except (TypeError, ValueError):
                        j["priority"] = int(j.get("priority", 0) or 0)
                    j["updated_at"] = _now()
                    self._write(data)
                    return j
        return None

    def set_paused(self, paused):
        """Set the document-level paused flag. While paused, claim_next returns None (the worker
        idles without claiming new jobs; any running job finishes untouched). Returns the bool."""
        with self._transaction_lock():
            data = self._read()
            data["paused"] = bool(paused)
            self._write(data)
            return bool(paused)

    def is_paused(self):
        with self._mutex_lock():
            return bool(self._read().get("paused"))

    def cancel(self, job_id):
        """Cancel a queued/failed job immediately, or request cancel for a RUNNING job.

        - queued/failed → status=cancelled (returns job)
        - running → sets cancel_requested=True (returns job with that flag; worker aborts via progress hook)
        - missing/done/cancelled → None
        """
        with self._transaction_lock():
            data = self._read()
            for j in data["jobs"]:
                if j.get("id") == job_id:
                    st = j.get("status")
                    if st == STATUS_RUNNING:
                        j["cancel_requested"] = True
                        j["updated_at"] = _now()
                        self._write(data)
                        return j
                    if st in (STATUS_QUEUED, STATUS_FAILED):
                        j["status"] = STATUS_CANCELLED
                        j.pop("cancel_requested", None)
                        j["updated_at"] = _now()
                        self._write(data)
                        return j
                    return None
        return None

    def is_cancel_requested(self, job_id) -> bool:
        with self._mutex_lock():
            j = self.get(job_id)
            return bool(j and j.get("cancel_requested"))

    def cancel_probe(self, job_id, min_interval=0.5):
        """Return a cheap zero-arg predicate: True once this job is cancel-requested (or gone).

        Both runners build the cancel channel from here so they cannot drift. Throttled because the
        download hot path calls it on every progress tick (and from several download_parallel
        threads); LATCHING because a transient journal read error must never un-see a cancel we
        already observed. A read error reads as 'not cancelled' — a flaky journal must not be able
        to kill a healthy download.

        No lock around `state`: the transition is monotonic False->True and idempotent, so the worst
        a race can cost is one duplicate read."""
        state = {'hit': False, 'next': 0.0}

        def probe():
            if state['hit']:
                return True
            now = time.monotonic()
            if now < state['next']:
                return False
            state['next'] = now + min_interval
            try:
                j = self.get(job_id)
            except Exception:
                return False
            # Three shapes of "this job should stop", because cancel() takes different routes by
            # state: a RUNNING job gets cancel_requested=True, a queued/failed one is written
            # straight to status=cancelled, and a dismissed card is gone from the journal entirely.
            # Checking only the flag would miss the latter two.
            if (j is None
                    or j.get("cancel_requested")
                    or j.get("status") == STATUS_CANCELLED):
                state['hit'] = True
            return state['hit']

        return probe

    def remove(self, job_id):
        """Drop a job from the journal by id (any status). Returns the removed record or None.

        Used by the pipeline card × dismiss control. Removing a RUNNING job also means the
        worker should abort (progress hook treats a missing job like cancel_requested).
        """
        with self._transaction_lock():
            data = self._read()
            for i, j in enumerate(data["jobs"]):
                if j.get("id") == job_id:
                    removed = data["jobs"].pop(i)
                    self._write(data)
                    return removed
            return None

    def retry_failed(self):
        """Flip every failed job back to queued. Returns the count reset."""
        with self._transaction_lock():
            data = self._read()
            n = 0
            for j in data["jobs"]:
                if j.get("status") == STATUS_FAILED:
                    j["status"] = STATUS_QUEUED
                    j["attempt_count"] = 0  # let retry-failed actually retry past the attempt cap
                    j["last_error"] = None  # requeue with a clean slate for the fresh attempt
                    j["last_error_kind"] = None
                    j["updated_at"] = _now()
                    n += 1
            if n:
                self._write(data)
        return n

    def clear(self, only_finished=True):
        """Remove finished (done/failed/cancelled) jobs, or everything if only_finished=False."""
        with self._transaction_lock():
            data = self._read()
            before = len(data["jobs"])
            if only_finished:
                data["jobs"] = [j for j in data["jobs"] if j.get("status") in _ACTIVE]
            else:
                data["jobs"] = []
            self._write(data)
            return before - len(data["jobs"])

    def counts(self):
        with self._mutex_lock():
            c = {STATUS_QUEUED: 0, STATUS_RUNNING: 0, STATUS_DONE: 0,
                 STATUS_FAILED: 0, STATUS_CANCELLED: 0}
            for j in self._read()["jobs"]:
                c[j.get("status", STATUS_QUEUED)] = c.get(j.get("status", STATUS_QUEUED), 0) + 1
            return c

    def recover_orphaned_running(self, max_attempts=5):
        """Re-queue (or fail) jobs left in 'running' by a crashed driver. Returns the count touched."""
        with self._transaction_lock():
            data = self._read()
            n = 0
            for j in data["jobs"]:
                if j.get("status") == STATUS_RUNNING:
                    if int(j.get("attempt_count", 0)) >= max_attempts:
                        j["status"] = STATUS_FAILED
                        j["last_error"] = "Recovered from stuck running state (process crash)."
                    else:
                        j["status"] = STATUS_QUEUED
                    j["updated_at"] = _now()
                    n += 1
            if n:
                self._write(data)
        return n

    def settle_claimed(self, job_id, max_attempts=5):
        """Close out a row THIS process claimed but never wrote a terminal status for.

        claim_next() makes the row durably 'running'. If the worker iteration dies before its own
        terminal write lands, nothing in-process ever looks at that row again — recover_orphaned_running
        only runs when a driver takes the run lock at startup — so the card spins forever. Same policy
        as that recovery pass, plus honouring a cancel that was already requested.

        Deliberately a SINGLE locked read-modify-write rather than get() + set_status() in the caller:
        the two-call form is exactly the check-then-write race it exists to close. Returns None in the
        99% case (row gone or already terminal, i.e. the worker's own set_status got there first)."""
        with self._transaction_lock():
            data = self._read()
            for j in data["jobs"]:
                if j.get("id") != job_id:
                    continue
                if j.get("status") != STATUS_RUNNING:
                    return None                      # worker settled it already — nothing to do
                if j.pop("cancel_requested", None):
                    j["status"] = STATUS_CANCELLED
                elif int(j.get("attempt_count", 0)) >= max_attempts:
                    j["status"] = STATUS_FAILED
                    j["last_error"] = "Worker stopped without recording a result (see server log)."
                else:
                    j["status"] = STATUS_QUEUED
                j["updated_at"] = _now()
                self._write(data)
                return j
        return None

    def _maybe_prune(self, data, threshold=2000, keep_days=30):
        """When the journal grows past `threshold` jobs, archive terminal (done/failed/cancelled)
        jobs older than `keep_days` into queue_archive.json (single append write) and drop them
        from the live queue. Mutates `data` in place; caller writes it."""
        jobs = data.get("jobs", [])
        if len(jobs) < threshold:
            return
        cutoff = _now() - keep_days * 86400
        terminal = {STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED}
        to_archive = [
            j for j in jobs
            if j.get("status") in terminal and float(j.get("updated_at", 0) or 0) < cutoff
        ]
        # Age rule alone can leave a busy queue above threshold if everything is < keep_days old.
        # If still over, archive the OLDEST remaining terminal jobs (regardless of age) until the
        # live count is back at/under threshold, so the queue can't grow without bound.
        picked = {id(j) for j in to_archive}
        over = (len(jobs) - len(to_archive)) - threshold
        if over > 0:
            oldest_terminal = sorted(
                (j for j in jobs if j.get("status") in terminal and id(j) not in picked),
                key=lambda j: float(j.get("updated_at", 0) or 0),
            )
            to_archive.extend(oldest_terminal[:over])
        if not to_archive:
            return
        archive_path = self.path.with_name("queue_archive.json")
        try:
            if archive_path.exists():
                with open(archive_path, "r", encoding="utf-8") as f:
                    arch = json.load(f)
                if not isinstance(arch, dict) or "jobs" not in arch:
                    arch = {"version": 1, "jobs": []}
            else:
                arch = {"version": 1, "jobs": []}
            arch["jobs"].extend(to_archive)
            afd, atmp = tempfile.mkstemp(dir=str(self.dir), prefix=".queue_archive.", suffix=".tmp")
            try:
                with os.fdopen(afd, "w", encoding="utf-8") as f:
                    json.dump(arch, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                for attempt in range(8):
                    try:
                        os.replace(atmp, archive_path)
                        break
                    except PermissionError:
                        if attempt == 7:
                            raise
                        time.sleep(0.05)
            finally:
                if os.path.exists(atmp):
                    try:
                        os.unlink(atmp)
                    except OSError:
                        pass
        except (OSError, json.JSONDecodeError):
            # If archiving fails for any reason, leave the live queue untouched (never lose jobs).
            return
        archived_ids = {id(j) for j in to_archive}
        data["jobs"] = [j for j in jobs if id(j) not in archived_ids]


HISTORY_FILE = "history.jsonl"


class HistoryLedger:
    """Append-only JSONL ledger of completed jobs (keystone: persistent history).

    Unlike the queue (temp+os.replace whole-file rewrite), history grows forever, so the safe
    discipline here is append-with-flock: a single locked O_APPEND write per record, fsynced, with
    the same .flock sidecar lock used by DownloadJournal so concurrent writers (CLI + web worker)
    never interleave a partial line. read() tolerates a torn final line."""

    def __init__(self, path=None):
        self.path = Path(path) if path else (default_dir() / HISTORY_FILE)
        self.dir = self.path.parent
        self.dir.mkdir(parents=True, exist_ok=True)
        self._flock_path = self.path.with_suffix(".jsonl.lock")
        self._mutex = threading.RLock()

    # ---- cross-process file lock (.flock sidecar) --------------------- #
    @contextmanager
    def _file_lock(self, timeout=5.0):
        # Body copied verbatim from DownloadJournal._file_lock (the project rules forbid refactoring
        # unrelated code; duplicating the ~25-line helper is the surgical choice over a shared mixin).
        self._flock_path.touch(exist_ok=True)
        fd = open(self._flock_path, "r+b")
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"lock {self._flock_path} timeout")
                        time.sleep(0.02)
            else:
                import fcntl
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"lock {self._flock_path} timeout")
                        time.sleep(0.02)
            yield
        finally:
            try:
                if locked:
                    if os.name == "nt":
                        import msvcrt
                        fd.seek(0)
                        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                fd.close()

    def record(self, *, url, output, profile, status, spec=None, job_id=None, ts=None):
        """Append one completed-job row. The full spec.to_dict() is stored so re-run-with-tweaks can
        clone the durable JobSpec; url/output/profile/status/ts are denormalized for cheap listing."""
        rec = {"ts": ts or _now(), "url": url, "output": output,
               "profile": profile, "status": status,
               "job_id": job_id,
               "spec": (spec.to_dict() if hasattr(spec, "to_dict") else spec)}
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        with self._mutex, self._file_lock():
            append_private_line(self.path, line)
        return rec

    @staticmethod
    def _decode_lines(chunk):
        """Decode+json-parse newline-delimited bytes, tolerating blank/torn lines. Decode per-line
        inside the try/except: a line ending mid multi-byte UTF-8 sequence (torn write) must be
        skipped, not crash the whole read."""
        out = []
        for raw in chunk.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue   # tolerate a torn last line / partial write
        return out

    def read(self, limit=None):
        if not self.path.exists():
            return []
        with self._mutex, self._file_lock():
            if not limit:
                with open(self.path, "rb") as f:
                    return self._decode_lines(f.read())
            # Bounded tail read: history.jsonl is append-only and never rotated, so scanning the
            # whole file to return only the last `limit` rows costs O(total history), not O(limit).
            # Instead read a window from EOF, growing it only if it doesn't yield `limit` valid rows.
            try:
                filesize = os.stat(self.path).st_size
            except OSError:
                return []
            want = max(65536, limit * 4096)
            while True:
                start = max(0, filesize - want)
                with open(self.path, "rb") as f:
                    f.seek(start)
                    chunk = f.read()
                if start > 0:
                    # Our window may begin mid-line; drop that leading partial fragment so it isn't
                    # mis-parsed as a whole record. Everything after the first newline is complete.
                    nl = chunk.find(b"\n")
                    chunk = chunk[nl + 1:] if nl != -1 else b""
                out = self._decode_lines(chunk)
                if len(out) >= limit or start == 0:
                    return out[-limit:]
                want *= 4


def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # No os.kill(0) semantics on Windows; probe via tasklist is heavy, so
            # use the Win32 OpenProcess via ctypes. A handle held open elsewhere can keep
            # OpenProcess succeeding for an already-exited process (zombie), so confirm the
            # process is STILL_ACTIVE via GetExitCodeProcess before reporting it alive.
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong(0)
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ValueError):
        return False
