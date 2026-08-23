#!/usr/bin/env python3
"""
WatchStore — persistent channel "watches" for the web UI's auto-detection panel.

Each watch tracks a YouTube channel and the videos already handled. A background poller (in the
web server) fetches the channel RSS and finds NEW uploads. The per-watch `auto_queue` flag decides
what happens: True -> new videos are enqueued automatically; False -> they land in `pending` for the
user to decide (queue or dismiss). Stdlib only; atomic JSON writes + an in-process mutex.
"""

import copy
import json
import os
import time
import uuid
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    from .state_validation import (
        PersistentStateError,
        canonicalize_jobspec_fields,
        canonicalize_watch_filters,
        quarantine_corrupt_state,
        validate_watch_document,
    )
except ImportError:
    from state_validation import (
        PersistentStateError,
        canonicalize_jobspec_fields,
        canonicalize_watch_filters,
        quarantine_corrupt_state,
        validate_watch_document,
    )


def _home():
    base = os.getenv("YT_FINAL_HOME")
    return Path(base) if base else (Path.home() / ".yt_final")


def _now():
    return time.time()


class WatchStore:
    # A new video whose auto-enqueue keeps failing (returns falsey / raises) would otherwise be
    # retried on EVERY poll forever. After this many attempts we give up and mark it seen so it
    # stops retrying; the failure stays visible in the watch's `auto_queue_errors` map.
    MAX_AUTO_QUEUE_RETRIES = 5

    def __init__(self, path=None):
        self.path = Path(path) if path else (_home() / "watches.json")
        self.dir = self.path.parent
        self._flock_path = self.path.with_suffix(".flock")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._file_mutex = threading.RLock()
        self._file_lock_depth = 0
        self._file_lock_file = None
        # Serializes whole poll cycles (apply_poll / batch_apply_poll) against each other so the
        # auto-enqueue callback (journal I/O) can run WITHOUT holding self._mutex, yet two polls
        # still can't double-enqueue the same new video. self._mutex is only ever held briefly
        # (read->plan->write, then read->commit->write) — never across the enqueue I/O — so the
        # rest of the /api/watches surface (list/get/update/...) is not stalled behind journal I/O.
        self._poll_lock = threading.Lock()
        self._owner_pid = os.getpid()
        # mtime-keyed read cache (mirrors DownloadJournal._read): avoids re-parsing watches.json on
        # every list()/get()/poll when the file hasn't changed. os.replace bumps st_mtime, so a
        # cross-process write invalidates this too.
        self._cache = None
        self._cache_mtime = None
        self._observed_state = False

    def _reset_after_fork_if_needed(self):
        """Reset inherited synchronization/cache state before any child lock."""
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
        self._poll_lock = threading.Lock()
        self._file_lock_depth = 0
        self._cache = None
        self._cache_mtime = None
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
    def _poll_cycle_lock(self):
        self._reset_after_fork_if_needed()
        with self._poll_lock:
            yield

    # Lock order for public APIs is always `_poll_lock` (poll paths only) ->
    # `_mutex` -> `_file_lock`. The file lock is reentrant because `_read` and
    # `_write` enforce it while mutators hold it across their whole transaction.
    @contextmanager
    def _file_lock(self, timeout=5.0):
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

    # ---- persistence (atomic) ---- #
    def _read(self):
        # NOTE (Windows): callers mutate what this returns and write it straight back, so an empty
        # return here PERSISTS a wipe of every watch. A sharing violation (AV / OneDrive / Search
        # holding watches.json) is a PermissionError — an OSError — and must NOT be treated as
        # "corrupt, start clean". Only genuinely unparseable content may reset the document.
        with self._file_lock():
            proven_stale = False
            for attempt in range(8):
                stat_succeeded = False
                try:
                    st = os.stat(self.path)
                    stat_succeeded = True
                    self._observed_state = True
                    key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
                    if self._cache is not None and self._cache_mtime == key:
                        return copy.deepcopy(self._cache)
                    if self._cache is not None:
                        proven_stale = True
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    try:
                        data = validate_watch_document(data)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
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
                    if not stat_succeeded and not self._observed_state:
                        return {"version": 1, "watches": []}
                    if attempt == 7:
                        raise
                    proven_stale = True
                    time.sleep(0.05)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
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
        with self._file_lock():
            snapshot = copy.deepcopy(data)
            validate_watch_document(snapshot)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.dir), prefix=".watches.", suffix=".tmp")
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

    # ---- API ---- #
    def list(self):
        # Return copies so a caller mutating the result can't poison the in-memory read cache.
        # Hold self._mutex (like DownloadJournal.list): _read() now returns the LIVE cached object
        # that writer paths mutate in place under the same lock, so deep-copying it unguarded could
        # race a concurrent add/remove/commit ("dict changed size during iteration").
        with self._mutex_lock():
            return [copy.deepcopy(w) for w in self._read()["watches"]]

    def get(self, wid):
        with self._mutex_lock():
            for w in self._read()["watches"]:
                if w.get("id") == wid:
                    return copy.deepcopy(w)
            return None

    def add(self, channel_id, label=None, url=None, auto_queue=False, defaults=None, filters=None):
        canonical_defaults = (
            canonicalize_jobspec_fields(defaults)
            if defaults is not None
            else {
                "quality": "1080p", "encode_mode": "copy", "no_shorts": True,
            }
        )
        canonical_filters = (
            canonicalize_watch_filters(filters)
            if filters is not None
            else {}
        )
        rec = {
            "id": "w_" + uuid.uuid4().hex[:8],
            "channel_id": channel_id,
            "label": label or channel_id,
            "url": url,
            "auto_queue": bool(auto_queue),
            "defaults": canonical_defaults,
            "filters": canonical_filters,
            "seen": [],
            "pending": [],
            "created_at": _now(),
            "last_checked": None,
            "last_error": None,
        }
        with self._transaction_lock():
            data = self._read()
            # de-dup by channel_id. Re-adding a channel that's already watched is how a user updates
            # its settings (there is no separate "edit" control in the UI), so MERGE the newly-supplied
            # label/url/auto_queue/defaults/filters into the existing record instead of silently
            # dropping them and returning the stale record untouched.
            for w in data["watches"]:
                if w.get("channel_id") == channel_id:
                    if label is not None:
                        w["label"] = label
                    if url is not None:
                        w["url"] = url
                    w["auto_queue"] = bool(auto_queue)
                    if defaults is not None:
                        w["defaults"] = canonical_defaults
                    if filters is not None:
                        w["filters"] = canonical_filters
                    self._write(data)
                    return copy.deepcopy(w)
            data["watches"].append(rec)
            self._write(data)
        return copy.deepcopy(rec)

    def update(self, wid, **fields):
        fields = dict(fields)
        if "defaults" in fields:
            fields["defaults"] = canonicalize_jobspec_fields(fields["defaults"])
        if "filters" in fields:
            fields["filters"] = canonicalize_watch_filters(fields["filters"])
        with self._transaction_lock():
            data = self._read()
            for w in data["watches"]:
                if w.get("id") == wid:
                    w.update(fields)
                    self._write(data)
                    return copy.deepcopy(w)
        return None

    def remove(self, wid):
        with self._transaction_lock():
            data = self._read()
            before = len(data["watches"])
            data["watches"] = [w for w in data["watches"] if w.get("id") != wid]
            changed = len(data["watches"]) != before
            if changed:
                self._write(data)
        return changed

    @staticmethod
    def _pending_entry(v):
        return {
            "id": v["id"],
            "title": v.get("title"),
            "url": v.get("url") or f"https://www.youtube.com/watch?v={v['id']}",
            "published": v.get("published"),
        }

    @staticmethod
    def _reconcile_pending(w, fetched):
        """Keep pending newest-first (RSS order), refresh titles, drop nothing the user still owns.

        - Items still in the feed are ordered exactly like the feed (newest first).
        - Items that fell off the ~15-entry RSS stay at the end (user can still queue/dismiss).
        - Titles/urls/published are refreshed from the feed when present.
        """
        fetched = fetched or []
        feed_order = [v["id"] for v in fetched]
        feed_by_id = {v["id"]: v for v in fetched}
        pending = list(w.get("pending") or [])
        by_id = {}
        for p in pending:
            pid = p.get("id")
            if not pid or pid in by_id:
                continue
            by_id[pid] = p
        ordered = []
        for fid in feed_order:
            if fid not in by_id:
                continue
            cur = dict(by_id.pop(fid))
            src = feed_by_id[fid]
            if src.get("title"):
                cur["title"] = src.get("title")
            if src.get("url"):
                cur["url"] = src.get("url")
            if src.get("published"):
                cur["published"] = src.get("published")
            ordered.append(cur)
        # leftovers (no longer in RSS) keep relative order, after live feed items
        ordered.extend(by_id.values())
        w["pending"] = ordered

    def _cap(self, w):
        if len(w.get("seen") or []) > 500:
            w["seen"] = w["seen"][-500:]
        # soft-cap pending so a never-dismissed list can't grow forever
        if len(w.get("pending") or []) > 80:
            w["pending"] = w["pending"][:80]

    def _plan_poll(self, w, fetched, has_enqueue_cb, error):
        """Phase 1 (caller holds self._mutex; NO blocking I/O). Reconcile everything EXCEPT the
        auto-enqueue callback. Mutates `w` in place and returns (n, mode, to_enqueue):
          - to_enqueue is the list of brand-new videos the caller must run auto_enqueue_fn on
            (non-empty only for an auto_queue watch WITH a callback); their seen/fails bookkeeping
            is deferred to _commit_enqueue so the journal I/O runs outside self._mutex.
          - for every other mode `w` is already fully updated and to_enqueue is []."""
        was_first = w.get("last_checked") is None
        if error:
            # Record the failure but do NOT advance last_checked: a failed fetch must not count
            # as the watch's first successful poll. If it did, `was_first` would be False on the
            # next (first real) success while seen/pending are still empty, so the anti-flood
            # baseline below would be skipped and the whole ~15-entry RSS backlog would flood
            # pending (or auto-download all at once).
            w["last_error"] = str(error)[:300]
            return (0, "error", [])
        w["last_checked"] = _now()
        w["last_error"] = None

        fetched = list(fetched or [])

        # First successful poll: baseline the current RSS as already-seen.
        # Otherwise the whole ~15-entry feed is dumped into pending and looks like "not updating"
        # forever (newest uploads get buried under that initial backlog).
        if was_first and not w.get("seen") and not w.get("pending"):
            w["seen"] = [v["id"] for v in fetched if v.get("id")]
            if len(w["seen"]) > 500:
                w["seen"] = w["seen"][-500:]
            w["pending"] = []
            return (0, "baseline", [])

        seen = set(w.get("seen") or [])
        pending_ids = {v["id"] for v in (w.get("pending") or []) if v.get("id")}
        new, _seen_now = [], set()
        for v in fetched:
            vid = v.get("id")
            if not vid or vid in seen or vid in pending_ids or vid in _seen_now:
                continue
            _seen_now.add(vid)
            new.append(v)

        if w.get("auto_queue"):
            if not has_enqueue_cb:
                # Fail-safe: an auto_queue watch with no enqueue callback can't actually download.
                # Marking the video seen here would silently drop it forever. Instead stash new
                # videos as pending (visible, user-recoverable) so no caller mistake loses a video.
                n = 0
                if new:
                    entries = [self._pending_entry(v) for v in new]
                    w["pending"] = entries + list(w.get("pending") or [])
                    n = len(entries)
                self._cap(w)
                return (n, "pending", [])
            # Defer the enqueue (journal I/O) to _commit_enqueue, which runs OUTSIDE self._mutex.
            # We intentionally do NOT touch seen/fails yet: a crash between phases just re-polls
            # these as new (no lost/duplicate download). self._poll_lock serializes poll cycles so
            # no concurrent poll can double-enqueue them in the meantime.
            return (0, "auto", new)

        # pending mode: prepend newest discoveries (RSS is newest-first) so the top is fresh.
        n = 0
        if new:
            entries = [self._pending_entry(v) for v in new]
            w["pending"] = entries + list(w.get("pending") or [])
            n = len(entries)
        self._reconcile_pending(w, fetched)
        self._cap(w)
        return (n, "pending", [])

    @staticmethod
    def _run_enqueue(w_snapshot, new, auto_enqueue_fn):
        """Phase 2 (NO lock held): invoke the enqueue callback per new video (this is the blocking
        journal I/O). `w_snapshot` is a copy for the callback's read-only defaults access. Returns
        a list of (video, ok, err) outcomes for _commit_enqueue to record."""
        results = []
        for v in new:
            try:
                ok = bool(auto_enqueue_fn(w_snapshot, v))
                err = None if ok else "auto-enqueue returned falsey"
            except Exception as e:
                ok = False
                err = (str(e)[:300] or e.__class__.__name__)
            results.append((v, ok, err))
        return results

    def _commit_enqueue(self, w, results):
        """Phase 3 (caller holds self._mutex; NO blocking I/O). Record the auto-enqueue outcomes
        onto `w` (seen on success / bounded retry counters on failure). Returns count enqueued."""
        fails = dict(w.get("auto_queue_fails") or {})
        errors = dict(w.get("auto_queue_errors") or {})
        seen_list = w.setdefault("seen", [])
        seen_set = set(seen_list)
        n = 0
        for v, ok, err in results:
            vid = v["id"]
            if ok:
                if vid not in seen_set:
                    seen_list.append(vid)
                    seen_set.add(vid)
                fails.pop(vid, None)
                errors.pop(vid, None)
                n += 1
            else:
                # Record the per-video failure (visible via API/UI) and bound the retries:
                # without this the same video is silently retried on every poll forever.
                fails[vid] = fails.get(vid, 0) + 1
                errors[vid] = err
                if fails[vid] >= self.MAX_AUTO_QUEUE_RETRIES:
                    # Give up on a permanently-failing video: mark it seen so it stops retrying.
                    # Its last error stays in auto_queue_errors for the user.
                    if vid not in seen_set:
                        seen_list.append(vid)
                        seen_set.add(vid)
                    fails.pop(vid, None)
        # persist counters; keep the errors map from growing without bound
        if fails:
            w["auto_queue_fails"] = fails
        else:
            w.pop("auto_queue_fails", None)
        if errors:
            if len(errors) > 100:
                errors = dict(list(errors.items())[-100:])
            w["auto_queue_errors"] = errors
        else:
            w.pop("auto_queue_errors", None)
        self._cap(w)
        return n

    def apply_poll(self, wid, fetched, auto_enqueue_fn=None, error=None):
        """Reconcile a fresh RSS fetch against a watch. `fetched` is a list of {id,title,url[,published]}.
        New videos (not in seen, not already pending) are auto-enqueued (auto_enqueue_fn called per
        video, must return truthy on success) or prepended to pending (newest first).
        First poll with empty seen/pending baselines the feed (no flood). Returns (new_count, mode).

        The auto_enqueue callback (which in the web server nests a cross-process, fsync'd journal
        write) runs OUTSIDE self._mutex — only self._poll_lock is held across it — so it never
        stalls the rest of the /api/watches surface (list/get/update/...) behind journal I/O."""
        with self._poll_cycle_lock():
            with self._transaction_lock():
                data = self._read()
                w = next((x for x in data["watches"] if x.get("id") == wid), None)
                if not w:
                    return (0, None)
                n, mode, to_enqueue = self._plan_poll(w, fetched, auto_enqueue_fn is not None, error)
                w_snapshot = copy.deepcopy(w) if to_enqueue else None
                self._write(data)
            if not to_enqueue:
                return (n, mode)
            # journal I/O happens here, with self._mutex RELEASED (still serialized by _poll_lock).
            outcomes = self._run_enqueue(w_snapshot, to_enqueue, auto_enqueue_fn)
            with self._transaction_lock():
                data = self._read()
                w = next((x for x in data["watches"] if x.get("id") == wid), None)
                if w is None:
                    return (0, mode)
                n = self._commit_enqueue(w, outcomes)
                self._write(data)
            return (n, mode)

    def batch_apply_poll(self, results, auto_enqueue_fn=None):
        """Apply many watch poll results. `results` maps watch-id -> (videos, error). Same reconcile
        rules as apply_poll. Auto-enqueue callbacks run OUTSIDE self._mutex (see apply_poll)."""
        with self._poll_cycle_lock():
            enqueue_work = {}   # wid -> (w_snapshot, [new videos])
            with self._transaction_lock():
                data = self._read()
                by_id = {w["id"]: w for w in data["watches"]}
                dirty = False
                for wid, (videos, err) in results.items():
                    w = by_id.get(wid)
                    if not w:
                        continue
                    dirty = True                 # this poll actually touched a stored watch
                    _n, _mode, to_enqueue = self._plan_poll(
                        w, videos, auto_enqueue_fn is not None, err)
                    if to_enqueue:
                        enqueue_work[wid] = (copy.deepcopy(w), to_enqueue)
                # Only persist when a watch was actually matched and updated. Writing
                # unconditionally would durably save an empty document if the read had come back
                # empty (e.g. a transient Windows sharing violation) — erasing every watch.
                if dirty:
                    self._write(data)
            if not enqueue_work:
                return
            # run the enqueue callbacks (journal I/O) with self._mutex RELEASED
            committed = {wid: self._run_enqueue(w_snap, new, auto_enqueue_fn)
                         for wid, (w_snap, new) in enqueue_work.items()}
            with self._transaction_lock():
                data = self._read()
                by_id = {w["id"]: w for w in data["watches"]}
                # Same guard as phase 1: never persist a document we could not verifiably read
                # (a transient read failure would otherwise erase every watch).
                if not any(wid in by_id for wid in committed):
                    return
                for wid, outcomes in committed.items():
                    w = by_id.get(wid)
                    if w is not None:
                        self._commit_enqueue(w, outcomes)
                self._write(data)

    def take_pending(self, wid, video_ids):
        """Remove the given pending videos (or 'all') from a watch and mark them seen.
        Returns the list of {id,title,url} taken (for enqueueing by the caller)."""
        with self._transaction_lock():
            data = self._read()
            w = next((x for x in data["watches"] if x.get("id") == wid), None)
            if not w:
                return []
            pending = w.get("pending", [])
            if video_ids == "all" or video_ids is True:
                taken = list(pending)
            else:
                ids = set(video_ids or [])
                taken = [v for v in pending if v["id"] in ids]
            taken_ids = {v["id"] for v in taken}
            w["pending"] = [v for v in pending if v["id"] not in taken_ids]
            for vid in taken_ids:
                if vid not in w.get("seen", []):
                    w.setdefault("seen", []).append(vid)
            self._write(data)
            return copy.deepcopy(taken)
