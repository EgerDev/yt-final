"""Executable timeout contracts for the authenticated web UI request path."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


APP_JS = (Path(__file__).resolve().parent / "webui" / "app.js").read_text(
    encoding="utf-8"
)


def _timeout_helper_source() -> str:
    start = APP_JS.index("function combinedAbortSignal(")
    end = APP_JS.index("/** Remove both the current session", start)
    source = APP_JS[start:end]
    assert "async function fetchWithTimeout(" in source, (
        "fetchWithTimeout must sit immediately after combinedAbortSignal"
    )
    return source


def _session_fetch_source() -> str:
    start = APP_JS.index("async function sessionFetch(")
    end = APP_JS.index("async function sessionJson(", start)
    return APP_JS[start:end]


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI timeout helpers")
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, "node failed: " + result.stderr[-1200:]
    return json.loads(result.stdout.strip().splitlines()[-1])


_NODE_TEST_SUPPORT = r"""
function check(value, message) {
  if (!value) throw new Error(message);
}
function abortError() {
  const error = new Error('aborted');
  error.name = 'AbortError';
  return error;
}
function pendingFetch(_url, options = {}) {
  return new Promise((_resolve, reject) => {
    const abort = () => reject(abortError());
    if (options.signal && options.signal.aborted) abort();
    else if (options.signal) options.signal.addEventListener('abort', abort, { once: true });
  });
}
function installFakeTimers() {
  let nextId = 1;
  const timers = new Map();
  global.setTimeout = (fn, delay) => {
    const id = nextId++;
    timers.set(id, { fn, delay });
    return id;
  };
  global.clearTimeout = (id) => timers.delete(id);
  return timers;
}
function disableNativeTimeout() {
  const NativeAbortSignal = global.AbortSignal;
  global.AbortSignal = {
    any: typeof NativeAbortSignal.any === 'function'
      ? NativeAbortSignal.any.bind(NativeAbortSignal)
      : undefined,
  };
}
"""


def test_timeout_fallback_aborts_pending_fetch_and_clears_timer():
    script = _timeout_helper_source() + _NODE_TEST_SUPPORT + r"""
(async () => {
  disableNativeTimeout();
  const timers = installFakeTimers();
  let observedSignal = null;
  global.fetch = (url, options) => {
    observedSignal = options.signal;
    return pendingFetch(url, options);
  };

  const resultPromise = fetchWithTimeout('/pending', {}, 73)
    .then(() => ({ resolved: true }), (error) => ({ name: error.name }));
  check(timers.size === 1, 'fallback deadline did not arm one timer');
  const timer = Array.from(timers.values())[0];
  check(timer.delay === 73, 'fallback deadline used the wrong delay');
  timer.fn();
  const result = await resultPromise;

  check(result.name === 'AbortError', 'fallback deadline did not reject with AbortError');
  check(observedSignal && observedSignal.aborted, 'fetch did not receive the aborted deadline');
  check(timers.size === 0, 'settled timeout left its fallback timer live');
  console.log(JSON.stringify({ result, timers: timers.size }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    got = _run_node(script)
    assert got == {"result": {"name": "AbortError"}, "timers": 0}


def test_timeout_fallback_clears_timers_after_resolve_and_reject():
    script = _timeout_helper_source() + _NODE_TEST_SUPPORT + r"""
(async () => {
  disableNativeTimeout();
  const timers = installFakeTimers();

  global.fetch = async () => ({ ok: true, source: 'resolved' });
  const resolved = await fetchWithTimeout('/resolved', {}, 41);
  check(resolved.source === 'resolved', 'resolved fetch result was not preserved');
  check(timers.size === 0, 'resolved fetch left its fallback timer live');

  global.fetch = async () => { throw new Error('network rejected'); };
  let rejected = '';
  try {
    await fetchWithTimeout('/rejected', {}, 42);
  } catch (error) {
    rejected = error.message;
  }
  check(rejected === 'network rejected', 'rejected fetch error was not preserved');
  check(timers.size === 0, 'rejected fetch left its fallback timer live');
  console.log(JSON.stringify({ resolved: resolved.source, rejected, timers: timers.size }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    got = _run_node(script)
    assert got == {
        "resolved": "resolved",
        "rejected": "network rejected",
        "timers": 0,
    }


def test_timeout_fallback_handles_missing_abortsignal_global():
    script = _timeout_helper_source() + _NODE_TEST_SUPPORT + r"""
(async () => {
  global.AbortSignal = undefined;
  const timers = installFakeTimers();
  let receivedSignal = false;
  global.fetch = async (_url, options) => {
    receivedSignal = !!(options && options.signal);
    return { ok: true };
  };

  const result = await fetchWithTimeout('/no-abort-signal-global', {}, 19);
  check(result.ok, 'resolved result was not returned');
  check(receivedSignal, 'fallback controller signal did not reach fetch');
  check(timers.size === 0, 'missing-global fallback left a timer live');
  console.log(JSON.stringify({ ok: result.ok, receivedSignal, timers: timers.size }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    got = _run_node(script)
    assert got == {"ok": True, "receivedSignal": True, "timers": 0}


def test_timeout_native_branch_composes_caller_signal_without_fallback_timer():
    script = _timeout_helper_source() + _NODE_TEST_SUPPORT + r"""
(async () => {
  const deadline = new AbortController();
  let timeoutMs = null;
  let fallbackTimerCalls = 0;
  Object.defineProperty(AbortSignal, 'timeout', {
    configurable: true,
    value(delay) {
      timeoutMs = delay;
      return deadline.signal;
    },
  });
  global.setTimeout = () => {
    fallbackTimerCalls += 1;
    throw new Error('native timeout branch armed a fallback timer');
  };
  global.clearTimeout = () => {};
  let fetchSignal = null;
  global.fetch = (url, options) => {
    fetchSignal = options.signal;
    return pendingFetch(url, options);
  };
  const caller = new AbortController();

  const resultPromise = fetchWithTimeout(
    '/native-timeout',
    { signal: caller.signal },
    88,
  ).then(() => ({ resolved: true }), (error) => ({ name: error.name }));
  check(timeoutMs === 88, 'native AbortSignal.timeout received the wrong deadline');
  check(fetchSignal && fetchSignal !== caller.signal, 'caller and deadline were not composed');
  caller.abort();
  const result = await resultPromise;

  check(result.name === 'AbortError', 'caller cancellation did not reach native branch fetch');
  check(fallbackTimerCalls === 0, 'native branch used a fallback timer');
  console.log(JSON.stringify({ timeoutMs, result, fallbackTimerCalls }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    got = _run_node(script)
    assert got == {
        "timeoutMs": 88,
        "result": {"name": "AbortError"},
        "fallbackTimerCalls": 0,
    }


def test_session_fetch_timeout_composes_caller_and_session_signals():
    script = (
        _timeout_helper_source()
        + _session_fetch_source()
        + _NODE_TEST_SUPPORT
        + r"""
let activeContext = null;
function loadSession() {
  return activeContext
    ? { token: activeContext.token, expires_at: 9999999999 }
    : null;
}
function sessionLost(message) { throw new Error('unexpected sessionLost: ' + message); }
function currentSessionContext() { return activeContext; }
function assertCurrentSession(context) {
  if (context !== activeContext) throw new Error('stale test session');
}
const SESSION_RESPONSE_CONTEXT = new WeakMap();

(async () => {
  disableNativeTimeout();
  const timers = installFakeTimers();
  const outcomes = [];

  for (const cancelledBy of ['caller', 'session']) {
    const session = new AbortController();
    const caller = new AbortController();
    activeContext = { token: 'token-' + cancelledBy, signal: session.signal };
    let requestOptions = null;
    global.fetch = (url, options) => {
      requestOptions = options;
      return pendingFetch(url, options);
    };
    const supplied = {
      method: 'POST',
      headers: { 'X-Test': cancelledBy },
      signal: caller.signal,
      timeoutMs: 321,
    };

    const resultPromise = sessionFetch('/authenticated-' + cancelledBy, supplied)
      .then(() => ({ resolved: true }), (error) => ({ name: error.name }));
    check(timers.size === 1, cancelledBy + ' request did not retain a separate deadline');
    check(requestOptions && !Object.prototype.hasOwnProperty.call(requestOptions, 'timeoutMs'),
      'private timeoutMs leaked into native fetch options');
    check(supplied.timeoutMs === 321, 'sessionFetch mutated the caller options');
    check(requestOptions.headers.get('X-Token') === 'token-' + cancelledBy,
      'session token header was not preserved');
    check(requestOptions.headers.get('X-Test') === cancelledBy,
      'caller header was not preserved');

    if (cancelledBy === 'caller') caller.abort();
    else session.abort();
    const result = await resultPromise;
    check(result.name === 'AbortError', cancelledBy + ' cancellation did not reach fetch');
    check(timers.size === 0, cancelledBy + ' cancellation left the deadline timer live');
    outcomes.push({ cancelledBy, name: result.name });
  }
  console.log(JSON.stringify({ outcomes, timers: timers.size }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    got = _run_node(script)
    assert got == {
        "outcomes": [
            {"cancelledBy": "caller", "name": "AbortError"},
            {"cancelledBy": "session", "name": "AbortError"},
        ],
        "timers": 0,
    }


def test_timeout_manual_composition_releases_every_upstream_listener():
    script = (
        _timeout_helper_source()
        + _session_fetch_source()
        + _NODE_TEST_SUPPORT
        + r"""
const NativeAbortControllerForLeakTest = global.AbortController;
const trackedSignals = [];
let internalControllerNumber = 0;

function trackedController(label, { abortDuringAdd = false } = {}) {
  const nativeController = new NativeAbortControllerForLeakTest();
  const listeners = new Map();
  let raced = false;
  const signal = {
    get aborted() { return nativeController.signal.aborted; },
    get reason() { return nativeController.signal.reason; },
    addEventListener(type, listener, options) {
      if (type !== 'abort') {
        nativeController.signal.addEventListener(type, listener, options);
        return;
      }
      const wrapped = (event) => {
        if (options && options.once) listeners.delete(listener);
        if (typeof listener === 'function') listener.call(signal, event);
        else listener.handleEvent(event);
      };
      listeners.set(listener, wrapped);
      nativeController.signal.addEventListener(type, wrapped, options);
      if (abortDuringAdd && !raced) {
        raced = true;
        nativeController.abort();
      }
    },
    removeEventListener(type, listener, options) {
      if (type !== 'abort') {
        nativeController.signal.removeEventListener(type, listener, options);
        return;
      }
      const wrapped = listeners.get(listener);
      if (!wrapped) return;
      listeners.delete(listener);
      nativeController.signal.removeEventListener(type, wrapped, options);
    },
  };
  const record = {
    label,
    signal,
    abort(reason) { nativeController.abort(reason); },
    listenerCount() { return listeners.size; },
  };
  trackedSignals.push(record);
  return record;
}

class TrackedAbortController {
  constructor() {
    this.record = trackedController('internal-' + (++internalControllerNumber));
    this.signal = this.record.signal;
  }
  abort(reason) { this.record.abort(reason); }
}

global.AbortController = TrackedAbortController;
global.AbortSignal = {};
const timers = installFakeTimers();
let activeContext = null;
function loadSession() {
  return activeContext
    ? { token: activeContext.token, expires_at: 9999999999 }
    : null;
}
function sessionLost(message) { throw new Error('unexpected sessionLost: ' + message); }
function currentSessionContext() { return activeContext; }
function assertCurrentSession(context) {
  if (context !== activeContext) throw new Error('stale test session');
}
const SESSION_RESPONSE_CONTEXT = new WeakMap();

function suppliedOptions(caller, label) {
  return {
    method: 'POST',
    headers: { 'X-Test': label },
    signal: caller.signal,
    timeoutMs: 101,
  };
}

function assertOptionsUnchanged(options, caller, label) {
  check(options.method === 'POST', label + ' mutated method');
  check(options.headers && options.headers['X-Test'] === label,
    label + ' mutated headers');
  check(options.signal === caller.signal, label + ' mutated signal');
  check(options.timeoutMs === 101, label + ' mutated timeoutMs');
  check(Object.keys(options).sort().join(',') === 'headers,method,signal,timeoutMs',
    label + ' mutated option keys');
}

function assertNoLeaks(label) {
  check(timers.size === 0, label + ' left ' + timers.size + ' fallback timer(s)');
  const leaked = trackedSignals
    .filter((record) => record.listenerCount() !== 0)
    .map((record) => record.label + ':' + record.listenerCount());
  check(leaked.length === 0, label + ' left upstream listeners: ' + leaked.join(','));
}

async function runScenario(label, settle) {
  const session = trackedController(label + '-session');
  const caller = trackedController(label + '-caller');
  activeContext = { token: 'token-' + label, signal: session.signal };
  const supplied = suppliedOptions(caller, label);
  let pending = null;
  if (settle === 'resolve') {
    global.fetch = async () => ({ status: 200, label });
  } else if (settle === 'reject') {
    global.fetch = async () => { throw new Error('network-' + label); };
  } else {
    global.fetch = (url, options) => pendingFetch(url, options);
  }

  pending = sessionFetch('/' + label, supplied)
    .then((value) => ({ value }), (error) => ({ error }));
  if (settle === 'caller') caller.abort();
  else if (settle === 'session') session.abort();
  else if (settle === 'deadline') {
    check(timers.size === 1, label + ' did not arm one fallback timer');
    Array.from(timers.values())[0].fn();
  }
  const outcome = await pending;

  if (settle === 'resolve') {
    check(outcome.value && outcome.value.label === label,
      label + ' lost its resolved response: ' + JSON.stringify({
        value: outcome.value,
        error: outcome.error && {
          name: outcome.error.name,
          message: outcome.error.message,
        },
      }));
  } else if (settle === 'reject') {
    check(outcome.error && outcome.error.message === 'network-' + label,
      label + ' lost its rejection');
  } else {
    check(outcome.error && outcome.error.name === 'AbortError',
      label + ' did not reject with AbortError');
  }
  assertOptionsUnchanged(supplied, caller, label);
  assertNoLeaks(label);
}

(async () => {
  await runScenario('resolve', 'resolve');
  await runScenario('reject', 'reject');
  await runScenario('caller-abort', 'caller');
  await runScenario('session-abort', 'session');
  await runScenario('deadline-abort', 'deadline');

  const alreadyAborted = trackedController('already-aborted-caller');
  alreadyAborted.abort();
  const alreadySession = trackedController('already-aborted-session');
  activeContext = { token: 'already-token', signal: alreadySession.signal };
  const alreadyOptions = suppliedOptions(alreadyAborted, 'already-aborted');
  global.fetch = (url, options) => pendingFetch(url, options);
  const alreadyOutcome = await sessionFetch('/already-aborted', alreadyOptions)
    .then((value) => ({ value }), (error) => ({ error }));
  check(alreadyOutcome.error && alreadyOutcome.error.name === 'AbortError',
    'already-aborted caller did not cancel');
  assertOptionsUnchanged(alreadyOptions, alreadyAborted, 'already-aborted');
  assertNoLeaks('already-aborted');

  const racingCaller = trackedController(
    'racing-caller',
    { abortDuringAdd: true },
  );
  const racingSession = trackedController('racing-session');
  activeContext = { token: 'racing-token', signal: racingSession.signal };
  const racingOptions = suppliedOptions(racingCaller, 'racing');
  const racingOutcome = await sessionFetch('/racing', racingOptions)
    .then((value) => ({ value }), (error) => ({ error }));
  check(racingOutcome.error && racingOutcome.error.name === 'AbortError',
    'registration-racing caller did not cancel');
  assertOptionsUnchanged(racingOptions, racingCaller, 'racing');
  assertNoLeaks('racing');

  const repeatedSession = trackedController('repeated-session');
  activeContext = { token: 'repeated-token', signal: repeatedSession.signal };
  global.fetch = async (_url, options) => ({ status: 200, signal: options.signal });
  for (let index = 0; index < 25; index += 1) {
    const caller = trackedController('repeated-caller-' + index);
    const options = suppliedOptions(caller, 'repeat-' + index);
    const response = await sessionFetch('/repeat-' + index, options);
    check(response.status === 200, 'repeated success lost response ' + index);
    assertOptionsUnchanged(options, caller, 'repeat-' + index);
    assertNoLeaks('repeat-' + index);
  }

  console.log(JSON.stringify({
    scenarios: 7,
    repeatedSuccesses: 25,
    timers: timers.size,
    liveListeners: trackedSignals.reduce(
      (total, record) => total + record.listenerCount(),
      0,
    ),
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    got = _run_node(script)
    assert got == {
        "scenarios": 7,
        "repeatedSuccesses": 25,
        "timers": 0,
        "liveListeners": 0,
    }


def test_session_fetch_without_timeout_uses_raw_fetch():
    script = (
        _timeout_helper_source()
        + _session_fetch_source()
        + _NODE_TEST_SUPPORT
        + r"""
const context = { token: 'ordinary-token', signal: new AbortController().signal };
function loadSession() { return { token: context.token, expires_at: 9999999999 }; }
function sessionLost(message) { throw new Error('unexpected sessionLost: ' + message); }
function currentSessionContext() { return context; }
function assertCurrentSession(candidate) {
  if (candidate !== context) throw new Error('stale test session');
}
const SESSION_RESPONSE_CONTEXT = new WeakMap();

(async () => {
  let nativeDeadlineCalls = 0;
  Object.defineProperty(AbortSignal, 'timeout', {
    configurable: true,
    value() {
      nativeDeadlineCalls += 1;
      return new AbortController().signal;
    },
  });
  let fetchCalls = 0;
  let requestOptions = null;
  const response = { status: 200 };
  global.fetch = async (_url, options) => {
    fetchCalls += 1;
    requestOptions = options;
    return response;
  };

  const result = await sessionFetch('/ordinary', { headers: { 'X-Test': 'raw' } });
  check(result === response, 'ordinary raw fetch response was not returned');
  check(fetchCalls === 1, 'ordinary request did not call raw fetch exactly once');
  check(nativeDeadlineCalls === 0, 'ordinary request entered the timeout helper');
  check(!Object.prototype.hasOwnProperty.call(requestOptions, 'timeoutMs'),
    'private timeout option reached raw fetch');
  check(requestOptions.headers.get('X-Token') === 'ordinary-token',
    'ordinary request lost its session header');
  console.log(JSON.stringify({ fetchCalls, nativeDeadlineCalls }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    got = _run_node(script)
    assert got == {"fetchCalls": 1, "nativeDeadlineCalls": 0}
