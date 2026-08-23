"""Bounded, tracked notification delivery."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import multiprocessing
import re
import threading
import time
from urllib.parse import parse_qsl, unquote, urlsplit


@dataclass(frozen=True)
class NotificationResult:
    status: str
    error: str | None
    completed_at: float


_SENSITIVE_HEADER_RE = re.compile(
    r"(?i)(?P<header_quote>['\"]?)"
    r"(?P<header_name>Proxy-Authorization|Authorization|Set-Cookie|Cookie)"
    r"(?P=header_quote)\s*:\s*[^\r\n]*"
)
_URL_RE = re.compile(
    r"(?i)\b(?:https?|ftp|socks4|socks5)://[^\s\"'<>]+"
)
_USERINFO_RE = re.compile(
    r"(?<![\w])[^:\s/@]+:[^@\s/]+@"
)


def _decoded_variants(value):
    current = str(value or "")
    seen = set()
    for _ in range(4):
        if not current or current in seen:
            break
        seen.add(current)
        yield current
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded


def _url_sensitive_material(raw_url):
    values = {str(raw_url or "").strip()}
    try:
        parsed = urlsplit(str(raw_url or "").strip())
    except (TypeError, ValueError):
        return values

    for credential in (parsed.username, parsed.password):
        values.update(_decoded_variants(credential))

    for component_name, component in (
        ("path", parsed.path),
        ("query", parsed.query),
        ("fragment", parsed.fragment),
    ):
        for variant in _decoded_variants(component):
            if len(variant) >= 3:
                values.add(variant)
            if component_name == "path":
                values.update(
                    segment for segment in variant.split("/")
                    if len(segment) >= 3
                )

    try:
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        query_items = []
    for key, value in query_items:
        for item in (key, value):
            values.update(
                variant for variant in _decoded_variants(item)
                if len(variant) >= 3
            )
    return values


def sanitize_notification_error(error, url=None) -> str:
    """Keep bounded diagnostics while removing notification credentials."""
    text = str(error or "")
    text = _SENSITIVE_HEADER_RE.sub(
        lambda match: f"{match.group('header_name')}: <redacted>",
        text,
    )
    raw_url = str(url or "").strip()
    if raw_url:
        for secret in sorted(
            (item for item in _url_sensitive_material(raw_url) if item),
            key=len,
            reverse=True,
        ):
            text = text.replace(secret, "<redacted>")
    text = _URL_RE.sub("<redacted-url>", text)
    text = _USERINFO_RE.sub("<redacted-userinfo>@", text)
    text = re.sub(
        r"(?i)\b(token|secret|key|password|signature|sig|auth)=([^&\s;]+)",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"(?<!\w)/(?:[^\s?#/]+/)*[^\s?#/]+", "<redacted-path>", text)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split()).strip()
    return (text or "delivery failed")[:500]


def safe_notification_origin(url) -> str:
    """Return a credential-free scheme://host[:port] display value."""
    try:
        parsed = urlsplit(str(url or "").strip())
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if scheme not in ("http", "https") or not host:
            return "webhook destination"
        display_host = f"[{host}]" if ":" in host else host
        port = parsed.port
        return f"{scheme}://{display_host}" + (f":{port}" if port else "")
    except (TypeError, ValueError):
        return "webhook destination"


def persisted_notification(result) -> dict:
    """Convert one finalized result to the exact durable queue-row shape."""
    error = getattr(result, "error", None)
    return {
        "status": str(getattr(result, "status", "failed")),
        "error": sanitize_notification_error(error) if error is not None else None,
        "completed_at": float(getattr(result, "completed_at")),
    }


def _deadline_sender_process(connection, sender, args):
    """Run one transport in an OS process so every blocking phase is stoppable."""
    try:
        outcome = sender(*args)
        if isinstance(outcome, tuple):
            ok = bool(outcome[0]) if outcome else False
            error = outcome[1] if len(outcome) > 1 else None
        else:
            ok = bool(outcome)
            error = None
        url = args[0] if args else None
        connection.send(
            (
                ok,
                None if ok or error is None
                else sanitize_notification_error(error, url),
            )
        )
    except BaseException as exc:
        url = args[0] if args else None
        try:
            connection.send(
                (
                    False,
                    sanitize_notification_error(
                        f"{type(exc).__name__}: {exc}",
                        url,
                    ),
                )
            )
        except BaseException:  # noqa: S110 -- A failed child pipe has no fallback channel.
            pass
    finally:
        try:
            connection.close()
        except BaseException:  # noqa: S110 -- Child-process teardown is best effort.
            pass


class DeadlineBoundSender:
    """Run a cooperative sender behind a terminable, reaped process boundary.

    `timeout` includes normally reserved process-reap time; a last-resort kill
    may consume at most one additional `termination_grace`. Production keeps
    that hard sum below the outer dispatcher shutdown budget. The sender must
    be importable/pickleable by multiprocessing's spawn context. Arbitrary
    dispatcher-only test/injected senders retain the narrower cooperative
    thread contract.
    """

    def __init__(self, sender, timeout=4.0, termination_grace=0.35):
        timeout = float(timeout)
        termination_grace = float(termination_grace)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if termination_grace <= 0:
            raise ValueError("termination_grace must be positive")
        self.sender = sender
        self.timeout = timeout
        self.termination_grace = min(termination_grace, timeout / 2)

    @staticmethod
    def _join_for(process, seconds):
        process.join(timeout=max(0.0, seconds))
        return not process.is_alive()

    def _stop_and_reap(self, process, deadline):
        if not process.is_alive():
            process.join(timeout=0)
            return
        try:
            process.terminate()
        except BaseException:  # noqa: S110 -- Kill escalation must still be attempted.
            pass
        remaining = max(0.0, deadline - time.monotonic())
        if self._join_for(process, min(self.termination_grace / 2, remaining)):
            return
        try:
            process.kill()
        except BaseException:  # noqa: S110 -- Join/reap remains authoritative.
            pass
        self._join_for(process, max(0.0, deadline - time.monotonic()))
        if process.is_alive():
            # `process.start()` itself can consume the nominal budget on a
            # severely loaded host. Keep one final, documented grace bounded
            # and still below the production dispatcher's outer shutdown
            # budget so a killed transport is always joined before return.
            try:
                process.kill()
            except BaseException:  # noqa: S110 -- Final join must still run.
                pass
            process.join(timeout=self.termination_grace)

    def __call__(self, *args):
        deadline = time.monotonic() + self.timeout
        context = multiprocessing.get_context("spawn")
        receiver, child_sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_deadline_sender_process,
            args=(child_sender, self.sender, args),
            name="notify-transport",
        )
        process.daemon = False
        try:
            process.start()
            child_sender.close()
            result_deadline = deadline - self.termination_grace
            wait_for = max(0.0, result_deadline - time.monotonic())
            if receiver.poll(wait_for):
                try:
                    result = receiver.recv()
                except (EOFError, OSError):
                    result = (False, "delivery process ended without a result")
                self._stop_and_reap(process, deadline)
                return result
            self._stop_and_reap(process, deadline)
            return False, "delivery timed out"
        except BaseException as exc:
            if process.pid is not None:
                self._stop_and_reap(process, deadline)
            return (
                False,
                sanitize_notification_error(
                    f"{type(exc).__name__}: {exc}",
                    args[0] if args else None,
                ),
            )
        finally:
            try:
                child_sender.close()
            except BaseException:  # noqa: S110 -- Continue closing sibling resources.
                pass
            try:
                receiver.close()
            except BaseException:  # noqa: S110 -- Continue closing sibling resources.
                pass
            try:
                if not process.is_alive():
                    process.close()
            except BaseException:  # noqa: S110 -- Process is already reaped or unusable.
                pass


class _NotificationFuture(Future):
    """A result Future whose queued cancellation resolves to a result object."""

    def __init__(self):
        super().__init__()
        self._cancel_lock = threading.Lock()
        self._cancel_delegate = None
        self._cancel_in_progress = False

    def bind_cancel(self, delegate):
        with self._cancel_lock:
            self._cancel_delegate = delegate

    def cancel(self):
        with self._cancel_lock:
            if (
                self.done()
                or self._cancel_delegate is None
                or self._cancel_in_progress
            ):
                return False
            delegate = self._cancel_delegate
            self._cancel_in_progress = True
        try:
            cancelled = bool(delegate())
        except BaseException:
            with self._cancel_lock:
                self._cancel_in_progress = False
            raise
        if not cancelled:
            with self._cancel_lock:
                self._cancel_in_progress = False
        return cancelled


class NotificationDispatcher:
    def __init__(self, sender, workers=2, max_pending=32):
        workers = int(workers)
        max_pending = int(max_pending)
        if workers < 1:
            raise ValueError("workers must be at least 1")
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self._sender = sender
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="notify",
        )
        self._completion_executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="notify-complete",
        )
        self._capacity = threading.BoundedSemaphore(max_pending)
        self._lock = threading.RLock()
        self._pending = set()
        self._settlements = {}
        self._finishing = set()
        self._state = "OPEN"
        self._completion_closed = False
        self._callback_context = threading.local()

    def _shutdown_drain_timeout(self, budget):
        """Reserve enough of one caller budget to cancel and settle queued work."""
        budget = max(0.0, float(budget))
        if budget <= 0:
            return 0.0
        if isinstance(self._sender, DeadlineBoundSender):
            transport_bound = (
                self._sender.timeout
                + self._sender.termination_grace
            )
            # A queued production transport may consume its whole hard bound
            # once admitted. Only let it start while that much caller budget
            # remains. A running transport that reaches its own hard deadline
            # enters cleanup early via `_transport_done`.
            return max(0.0, budget - transport_bound)
        # Injected/thread-only senders have no enforceable hard duration.
        # Spend most of the requested budget draining accepted work while
        # retaining a fixed fraction for asynchronous cancellation callbacks.
        return budget * 0.8

    @staticmethod
    def _result(status, error=None):
        return NotificationResult(
            status=status,
            error=error,
            completed_at=time.time(),
        )

    def _invoke_callback(self, callback, result, current_future=None):
        if callback is None:
            return
        previous = getattr(self._callback_context, "future", None)
        self._callback_context.future = current_future
        try:
            callback(result)
        except BaseException:  # noqa: S110 -- Optional callbacks cannot break shutdown.
            # Notification completion and shutdown must survive optional persistence/UI callbacks.
            pass
        finally:
            if previous is None:
                try:
                    del self._callback_context.future
                except AttributeError:
                    pass
            else:
                self._callback_context.future = previous

    def _completed_future(self, status, error=None, callback=None):
        future = _NotificationFuture()
        result = self._result(status, error)
        self._invoke_callback(callback, result)
        future.set_result(result)
        return future

    def _deliver(self, url, payload, template):
        try:
            outcome = self._sender(url, payload, template)
            if isinstance(outcome, tuple):
                ok = bool(outcome[0]) if outcome else False
                error = outcome[1] if len(outcome) > 1 else None
            else:
                ok = bool(outcome)
                error = None
            if ok:
                return self._result("sent")
            return self._result(
                "failed",
                sanitize_notification_error(error or "delivery failed", url),
            )
        except BaseException as exc:
            return self._result(
                "failed",
                sanitize_notification_error(
                    f"{type(exc).__name__}: {exc}",
                    url,
                ),
            )

    def _close_completion_executor_if_idle(self):
        should_close = False
        with self._lock:
            if (
                self._state == "CLOSED"
                and not self._pending
                and not self._completion_closed
            ):
                self._completion_closed = True
                should_close = True
        if should_close:
            self._completion_executor.shutdown(
                wait=False,
                cancel_futures=False,
            )

    def _schedule_finish(self, public, internal, callback):
        try:
            self._completion_executor.submit(
                self._finish,
                public,
                internal,
                callback,
            )
        except RuntimeError:
            # The completion executor closes only after `_pending` reaches
            # zero, so a tracked future cannot legitimately reach this path.
            # Keep the failure explicit rather than running a user callback
            # inline on an arbitrary cancellation/shutdown caller.
            return

    def _transport_done(self, public, internal, callback):
        self._schedule_finish(public, internal, callback)
        if internal.cancelled():
            return
        try:
            result = internal.result()
        except BaseException:
            return
        if not (
            getattr(result, "status", None) == "failed"
            and getattr(result, "error", None) == "delivery timed out"
        ):
            return
        with self._lock:
            if self._state == "OPEN":
                return
            queued_peers = tuple(
                future for future in self._pending
                if future is not public
            )
        # Future.cancel() succeeds only for executor work that is still
        # queued. Copy under the dispatcher lock; invoke outside it.
        for future in queued_peers:
            future.cancel()

    def _finish(self, public, internal, callback):
        with self._lock:
            if public in self._finishing or public.done():
                return
            self._finishing.add(public)
        if internal.cancelled():
            result = self._result("cancelled")
        else:
            try:
                result = internal.result()
            except BaseException as exc:
                result = self._result(
                    "failed",
                    sanitize_notification_error(
                        f"{type(exc).__name__}: {exc}",
                    ),
                )
        self._invoke_callback(callback, result, public)
        settlement: Future[None] | None = None
        try:
            public.set_result(result)
        except BaseException:  # noqa: S110 -- Settlement cleanup must always complete.
            # External Future callbacks are not part of the dispatcher
            # callback contract and may raise BaseException from
            # Future.set_result(). The authoritative result is already
            # published; never let such a callback strand lifecycle state.
            pass
        finally:
            with self._lock:
                self._pending.discard(public)
                settlement = self._settlements.pop(public, None)
                self._finishing.discard(public)
                self._capacity.release()
            if settlement is not None:
                settlement.set_result(None)
            self._close_completion_executor_if_idle()

    def submit(self, url, payload, template="raw", on_result=None):
        internal: Future[NotificationResult] | None = None
        public: _NotificationFuture | None = None
        rejection_status = None
        rejection = None
        with self._lock:
            if self._state != "OPEN":
                rejection_status = "rejected"
                rejection = "notification dispatcher is closed"
            elif not self._capacity.acquire(blocking=False):
                rejection_status = "dropped"
                rejection = "notification capacity is full"
            else:
                public = _NotificationFuture()
                settlement = Future[None]()
                self._pending.add(public)
                self._settlements[public] = settlement
                try:
                    internal = self._executor.submit(
                        self._deliver,
                        url,
                        payload,
                        template,
                    )
                except BaseException:
                    self._pending.discard(public)
                    self._settlements.pop(public, None)
                    self._capacity.release()
                    rejection_status = "rejected"
                    rejection = "notification dispatcher rejected delivery"
                else:
                    public.bind_cancel(internal.cancel)
        if rejection is not None:
            return self._completed_future(
                rejection_status,
                rejection,
                on_result,
            )
        if internal is None or public is None:
            raise RuntimeError("notification submission invariant was not satisfied")
        internal.add_done_callback(
            lambda done: self._transport_done(public, done, on_result)
        )
        return public

    def flush(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            current = getattr(self._callback_context, "future", None)
            snapshot = tuple(
                settlement
                for future, settlement in self._settlements.items()
                if future is not current
            )
        if not snapshot:
            return True
        _, unfinished = wait(
            snapshot,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        return not unfinished

    def shutdown(self, timeout=5.0):
        budget = max(0.0, float(timeout))
        started = time.monotonic()
        deadline = started + budget
        drain_deadline = started + self._shutdown_drain_timeout(budget)
        with self._lock:
            if self._state == "OPEN":
                self._state = "CLOSING"
            current = getattr(self._callback_context, "future", None)
            snapshot = tuple(
                (future, self._settlements[future])
                for future in self._pending
                if future is not current
            )
        settlement_snapshot = tuple(
            settlement for _public, settlement in snapshot
        )
        if settlement_snapshot:
            wait(
                settlement_snapshot,
                timeout=max(0.0, drain_deadline - time.monotonic()),
            )
        for public, settlement in snapshot:
            if not settlement.done():
                public.cancel()
        if settlement_snapshot:
            wait(
                settlement_snapshot,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        with self._lock:
            complete = all(
                future.done() for future in settlement_snapshot
            )
            self._state = "CLOSED"
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._close_completion_executor_if_idle()
        return complete
