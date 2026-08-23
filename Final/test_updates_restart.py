"""Certified update UI outcome and concurrency contracts.

Task 7 retired winget/Homebrew and their transient ``needs_restart`` result.
Node and Deno now activate verified versioned artifacts inside the app-managed
tools directory; Python updates apply one whole hash-locked set. These tests
preserve the still-relevant success/failure/outdated display and busy-state
contracts without demanding the retired package-manager behavior.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
JS = (HERE / "webui" / "app.js").read_text(encoding="utf-8")
TOOLS = (HERE / "optional_tools.py").read_text(encoding="utf-8")
SERVER = (HERE / "webserver.py").read_text(encoding="utf-8")


def test_backend_has_no_retired_package_manager_restart_outcome():
    """Certified managed artifacts activate atomically and never invoke winget."""
    assert "winget" not in TOOLS.lower()
    assert "'needs_restart': True" not in TOOLS
    assert "needs_restart" not in JS
    # The route still forwards the certified/manual dispatcher result verbatim.
    assert "'result': result," in SERVER, "the tools route no longer forwards the installer result"


def test_both_consumers_go_through_the_shared_outcome_helper():
    """Two call sites had the same bug. Testing the helper alone would pass while a call site still
    flattened the state — that exact gap shipped a regression earlier today."""
    assert "function installOutcome(" in JS
    assert JS.count("installOutcome(") >= 3, "expected the helper plus both call sites"
    # the speed-test consumer
    st = JS.split("a.kind === 'install_tool'", 1)[1].split("} else if", 1)[0]
    assert "installOutcome(" in st, "the Speed Test installer still decides its own label"
    assert "ok ? 'Installed ✓'" not in st, "the two-outcome flattening is back"
    # the Updates consumer
    upd = JS.split("if (body.report) renderUpdates(body.report);", 1)[1].split("\n  } catch", 1)[0]
    assert "installOutcome(" in upd, "the Updates installer still decides its own label"
    assert "outcome.level !== 'ok'" in upd, "an outdated certified artifact must remain a warning"


def test_install_outcome_logic_executes_correctly():
    """Execute the certified success/failure/outdated branches."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    fn = ("function installOutcome("
          + JS.split("function installOutcome(", 1)[1].split("\n}\n", 1)[0] + "\n}")
    harness = fn + r"""
var O = installOutcome;
console.log(JSON.stringify({
  plain_ok:     O({ok: true, result: {ok: true, message: 'node ready'}}, 'node'),
  ok_no_msg:    O({ok: true, result: {ok: true}}, 'node'),
  still_behind: O({ok: true, result: {ok: true, outdated: true, message: 'node 18 installed'}}, 'node'),
  row_behind:   O({ok: true, result: {ok: true}, report: {info: [{name: 'node', outdated: true}]}}, 'node'),
  row_other:    O({ok: true, result: {ok: true}, report: {info: [{name: 'deno', outdated: true}]}}, 'node'),
  failed:       O({ok: false, error: 'certified artifact failed checksum verification'}, 'node'),
  threw:        O(null, 'node'),
  no_tool:      O(null, undefined)
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-400:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    assert g["plain_ok"] == {"level": "ok", "label": "Installed ✓", "detail": "node ready"}, g["plain_ok"]
    assert g["ok_no_msg"]["level"] == "ok" and g["ok_no_msg"]["detail"], g["ok_no_msg"]

    # the pre-existing "succeeded but not latest" half-truth keeps its own warn state
    assert g["still_behind"]["level"] == "warn", g["still_behind"]
    assert g["row_behind"]["level"] == "warn", g["row_behind"]
    # ...and another tool being outdated must NOT taint this one
    assert g["row_other"]["level"] == "ok", g["row_other"]

    # failures stay failures, including a thrown request
    assert g["failed"]["level"] == "bad" and "checksum" in g["failed"]["detail"], g["failed"]
    assert g["threw"]["level"] == "bad", g["threw"]
    assert g["no_tool"]["detail"], "must not render 'undefined' at the user"
    assert "undefined" not in g["no_tool"]["detail"], g["no_tool"]


def test_optional_tool_actions_execute_only_for_certified_host_rows():
    """The browser must honor server provenance and keep manual-only tools immutable."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")

    allowlist_start = JS.index("const OPTIONAL_INSTALL_TOOLS = new Set([")
    allowlist_end = JS.index("]);", allowlist_start) + len("]);")
    allowlist = JS[allowlist_start:allowlist_end]
    fn = (
        "function toolActionHtml("
        + JS.split("function toolActionHtml(", 1)[1].split("\n}\n", 1)[0]
        + "\n}"
    )
    harness = allowlist + "\n" + fn + r"""
function esc(value) { return String(value); }
console.log(JSON.stringify({
  unsupported_node: toolActionHtml({
    name: 'node', installable: false, kind: 'info', installed: null
  }),
  certified_node: toolActionHtml({
    name: 'node', installable: true, kind: 'info', installed: null
  }),
  manual_aria: toolActionHtml({
    name: 'aria2c', installable: true, kind: 'info', installed: null
  }),
  manual_playwright: toolActionHtml({
    name: 'playwright', installable: true, kind: 'info', installed: null
  })
}));
"""
    result = subprocess.run(
        [node, "-e", harness],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, "node failed: " + result.stderr[-400:]
    rendered = json.loads(result.stdout.strip().splitlines()[-1])

    assert "data-tool-install" not in rendered["unsupported_node"]
    assert "data-tool-install" in rendered["certified_node"]
    assert "data-tool-install" not in rendered["manual_aria"]
    assert "data-tool-install" not in rendered["manual_playwright"]
    assert "r.installable && OPTIONAL_INSTALL_TOOLS.has(r.name)" in JS
    assert "'aria2c'" not in allowlist
    assert "'N_m3u8DL-RE'" not in allowlist
    assert "'playwright'" not in allowlist
    assert "'bgutil-server'" not in allowlist


def test_a_running_install_survives_a_re_render():
    """setUpdatesBusy disables the .upd-act buttons that exist AT THAT MOMENT. renderUpdates then does
    body.innerHTML = html, recreating every one of them ENABLED. So leaving the Updates tab and coming
    back during a multi-minute install re-armed every Install button, and a second click launched a
    concurrent managed mutation against the same environment.

    Three separate paths re-render: the quiet tab-open, the phase-2 background refresh, and an OS-tab
    click. All go through the same innerHTML assignment, so the busy state is re-applied there."""
    assert "let _updatesBusy" in JS, "no record of an install being in flight"
    # it must be declared with the other Updates module state, ABOVE both users — a `let` read from
    # code boot can reach must be declared first or the temporal dead zone kills the whole script
    assert JS.index("let _updatesBusy") < JS.index("function setUpdatesBusy("),         "the flag is declared below its writer (temporal dead zone risk)"
    assert JS.index("let _updatesBusy") < JS.index("function renderUpdates("),         "the flag is declared below its reader (temporal dead zone risk)"

    setter = JS.split("function setUpdatesBusy(", 1)[1].split("\n}", 1)[0]
    assert "_updatesBusy = !!busy" in setter, "the busy state is never recorded"

    render = JS.split("function renderUpdates(", 1)[1].split("\nasync function ", 1)[0]
    code = "\n".join(ln for ln in render.splitlines()
                     if not ln.strip().startswith(("//", "*", "/*")))
    assert "body.innerHTML = html" in code
    reapply = [
        ln for ln in code.splitlines()
        if "applyUpdatesControlState()" in ln
    ]
    assert reapply, "the re-render does not restore the busy state — buttons come back enabled"
    # ...and it must come AFTER the wipe, or it applies to elements about to be replaced
    assert code.index("body.innerHTML = html") < code.index(reapply[0].strip()),         "the busy state is re-applied before the table is rebuilt"

    # and a nav-tab refresh must not re-probe underneath a running install
    ru = JS.split("async function refreshUpdates(", 1)[1].split("\n  const hint", 1)[0]
    assert "_updatesBusy" in ru and "return" in ru,         "opening the Updates tab mid-install still triggers a refresh"


def test_updates_busy_state_executes_correctly():
    """The installer owner cannot re-enable controls while a scan still owns them."""
    import shutil as _sh
    node = _sh.which("node")
    if not node:
        pytest.skip("node not available to execute the UI helpers")
    state_start = JS.index("function applyUpdatesControlState(")
    state_end = JS.index("function setUpdatesScanBusy(", state_start)
    setter = JS[state_start:state_end]
    harness = setter + r"""
var _updatesBusy = false;
var _updatesScanBusy = true;
var acts = [{disabled:false}, {disabled:false}];
var named = {};
['updates_check'].forEach(function (i) { named[i] = {disabled:false}; });
function $(id) { return named[id] || null; }
global.document = { querySelectorAll: function (sel) { return sel === '.upd-act' ? acts : []; } };
var out = {};
setUpdatesBusy(true);
out.busy_flag = _updatesBusy;
out.busy_acts = acts.map(function (b) { return b.disabled; });
out.busy_named = Object.keys(named).map(function (k) { return named[k].disabled; });
setUpdatesBusy(false);
out.idle_flag = _updatesBusy;
out.idle_acts = acts.map(function (b) { return b.disabled; });
out.idle_named = Object.keys(named).map(function (k) { return named[k].disabled; });
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-300:]
    g = json.loads(r.stdout.strip().splitlines()[-1])
    assert g["busy_flag"] is True, "the flag is not recorded, so a re-render cannot restore it"
    assert all(g["busy_acts"]) and all(g["busy_named"]), g
    assert g["idle_flag"] is False, g
    assert all(g["idle_acts"]) and all(g["idle_named"]), (
        "clearing installer ownership re-enabled controls still owned by the scan"
    )
    assert "updates_install" not in setter and "updates_apply" not in setter


def _scan_runtime_source() -> str:
    """Return the real scan lifecycle functions without the large render function."""
    refresh = JS.index("async function refreshUpdates(")
    lifecycle = JS.find("function invalidateUpdateScan(", 0, refresh)
    start = lifecycle if lifecycle >= 0 else refresh
    end = JS.index("async function runFFmpegAction(", start)
    return JS[start:end]


def _fetch_updates_source() -> str:
    start = JS.index("async function fetchUpdatesReport(")
    end = JS.index("async function refreshUpdates(", start)
    return JS[start:end]


def _function_slice(start: str, end: str) -> str:
    return JS[JS.index(start):JS.index(end, JS.index(start))]


_SCAN_HARNESS = r"""
let _updatesReport = null;
let _updatesReportAt = 0;
let _updatesBusy = false;
let _updatesScanBusy = false;
let _updatesScanGeneration = 0;
let _updatesScanController = null;
let _updatesInstallOwner = null;
let _updatesBgTimer = null;
const UPDATES_CLIENT_TTL_MS = 75000;
let sessionGeneration = 1;
let now = 100000;
let nextTimer = 1;
const timers = new Map();
const requests = [];
const renders = [];
const actionButtons = [{disabled: false}, {disabled: false}];
const speedInstallButtons = [];

function classList() {
  const values = new Set();
  return {
    add(value) { values.add(value); },
    remove(value) { values.delete(value); },
    toggle(value, on) { if (on) values.add(value); else values.delete(value); },
    contains(value) { return values.has(value); },
  };
}
const elements = {
  updates_hint: {className: '', textContent: '', disabled: false},
  updates_check: {className: '', textContent: '', disabled: false},
  updates_body: {
    className: '',
    textContent: '',
    innerHTML: '',
    disabled: false,
    classList: classList(),
  },
};
function $(id) { return elements[id] || null; }
global.document = {
  querySelectorAll(selector) {
    if (selector === '.upd-act') return actionButtons;
    if (selector === '.st-rec-btn[data-update-install="1"]') {
      return speedInstallButtons;
    }
    return [];
  },
};
global.performance = {now() { return now; }};
const realDateNow = Date.now;
Date.now = () => now;

function abortError(message = 'aborted') {
  const error = new Error(message);
  error.name = 'AbortError';
  return error;
}
function deferredRequest(options) {
  let resolve;
  let reject;
  const promise = new Promise((ok, bad) => { resolve = ok; reject = bad; });
  const request = {options: options || {}, resolve, reject, promise};
  requests.push(request);
  return promise;
}
function fetchUpdatesReport(options = {}) { return deferredRequest(options); }
function authenticatedTimeout(callback, delay) {
  const id = nextTimer++;
  timers.set(id, {callback, delay});
  return id;
}
function clearAuthenticatedTimeout(id) { timers.delete(id); }
function beginAuthenticatedAction() {
  const owner = sessionGeneration;
  return {
    isCurrent() { return owner === sessionGeneration; },
    guard() {
      if (owner !== sessionGeneration) throw abortError('stale session');
    },
    isSilentError(error) {
      return owner !== sessionGeneration || !!(error && error.name === 'AbortError');
    },
    effect(callback) {
      if (owner !== sessionGeneration) return false;
      callback();
      return true;
    },
  };
}
function showUpdatesSkeleton() {
  elements.updates_body.classList.add('is-loading');
  elements.updates_body.innerHTML = 'Scanning tools';
}
function esc(value) { return String(value); }
function renderUpdates(report) {
  _updatesReport = report;
  renders.push(report && report.id);
}
function tick() { return Promise.resolve().then(() => Promise.resolve()); }
"""


def _run_update_node(body: str, *, extra_source: str = "") -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute update concurrency contracts")
    script = (
        _SCAN_HARNESS
        + "\n"
        + _scan_runtime_source()
        + "\n"
        + extra_source
        + "\n"
        + body
    )
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, "node failed: " + result.stderr[-1600:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_update_fetch_forwards_scan_signal_through_both_authenticated_branches():
    """Removing either signal forwarding lets invalidated transport continue consuming work."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute update request contracts")
    script = _fetch_updates_source() + r"""
let TOKEN = 'signed-in';
const controller = new AbortController();
const calls = [];
async function api(path, options) {
  calls.push({branch: 'api', path, options});
  return {id: 'api-report'};
}
async function sessionFetch(path, options) {
  calls.push({branch: 'sessionFetch', path, options});
  return {ok: true, statusText: '', id: 'response'};
}
async function sessionJson() { return {id: 'session-report'}; }

(async () => {
  const apiReport = await fetchUpdatesReport({
    force: true, pypi: true, signal: controller.signal,
  });
  TOKEN = '';
  const sessionReport = await fetchUpdatesReport({
    force: false, pypi: false, signal: controller.signal,
  });
  if (apiReport.id !== 'api-report' || sessionReport.id !== 'session-report') {
    throw new Error('fetchUpdatesReport changed response behavior');
  }
  if (calls.length !== 2
      || calls[0].options.signal !== controller.signal
      || calls[1].options.signal !== controller.signal) {
    throw new Error('scan AbortSignal was not forwarded through both branches');
  }
  console.log(JSON.stringify({branches: calls.map((call) => call.branch)}));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, "node failed: " + result.stderr[-1200:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "branches": ["api", "sessionFetch"]
    }


def test_inverse_quiet_scans_render_only_the_new_generation():
    got = _run_update_node(
        r"""
(async () => {
  const older = refreshUpdates({quiet: true});
  await tick();
  const newer = refreshUpdates({quiet: true});
  await tick();
  if (requests.length !== 2) throw new Error('two scans did not issue two phase-one requests');
  const oldSignal = requests[0].options.signal;
  if (!oldSignal || !oldSignal.aborted) throw new Error('new scan did not abort old signal');

  requests[1].resolve({id: 'newer', pip: [], info: [], summary: {}});
  await newer;
  requests[0].resolve({id: 'older', pip: [], info: [], summary: {}});
  await older;
  await tick();

  if (renders.join(',') !== 'newer') {
    throw new Error('inverse completion rendered stale data: ' + renders.join(','));
  }
  console.log(JSON.stringify({
    renders,
    oldAborted: oldSignal.aborted,
    timers: timers.size,
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    assert got == {"renders": ["newer"], "oldAborted": True, "timers": 1}


def test_resolved_response_cannot_render_after_a_new_generation_starts():
    """A response can resolve before its awaiting continuation receives a microtask."""
    got = _run_update_node(
        r"""
(async () => {
  const older = refreshUpdates({quiet: true});
  await tick();
  requests[0].resolve({id: 'resolved-old', pip: [], info: [], summary: {}});

  // Do not yield: begin the replacement generation after resolution but before the old await
  // continuation can render.
  const newer = refreshUpdates({quiet: true});
  await tick();
  if (requests.length !== 2) throw new Error('replacement scan did not start');
  if (renders.length !== 0) {
    throw new Error('resolved stale response rendered before generation guard');
  }

  requests[1].resolve({id: 'newer', pip: [], info: [], summary: {}});
  await Promise.all([older, newer]);
  if (renders.join(',') !== 'newer') {
    throw new Error('resolved stale response rendered: ' + renders.join(','));
  }
  console.log(JSON.stringify({renders, oldAborted: requests[0].options.signal.aborted}));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    assert got == {"renders": ["newer"], "oldAborted": True}


def test_quiet_generation_releases_superseded_explicit_scan_ownership():
    """Back/hash navigation can start a quiet refresh while an explicit request is pending."""
    got = _run_update_node(
        r"""
(async () => {
  const explicit = refreshUpdates({quiet: false});
  await tick();
  if (!_updatesScanBusy || !elements.updates_check.disabled) {
    throw new Error('explicit scan never claimed controls');
  }

  const quiet = refreshUpdates({quiet: true});
  await tick();
  if (_updatesScanBusy || elements.updates_check.disabled) {
    throw new Error('quiet replacement retained superseded explicit ownership');
  }
  requests[1].resolve({id: 'quiet-new', pip: [], info: [], summary: {}});
  requests[0].resolve({id: 'explicit-old', pip: [], info: [], summary: {}});
  await Promise.all([explicit, quiet]);
  if (renders.join(',') !== 'quiet-new') {
    throw new Error('superseded explicit scan rendered: ' + renders.join(','));
  }
  console.log(JSON.stringify({
    renders,
    scanBusy: _updatesScanBusy,
    oldAborted: requests[0].options.signal.aborted,
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    assert got == {
        "renders": ["quiet-new"],
        "scanBusy": False,
        "oldAborted": True,
    }


def test_old_quiet_phase_two_cannot_overwrite_completed_explicit_scan():
    got = _run_update_node(
        r"""
(async () => {
  const quiet = refreshUpdates({quiet: true});
  await tick();
  requests[0].resolve({id: 'fast', pip: [], info: [], summary: {}});
  await quiet;
  if (timers.size !== 1) throw new Error('quiet scan did not queue phase two');
  const queued = Array.from(timers.values())[0];
  queued.callback();
  await tick();
  if (requests.length !== 2) throw new Error('quiet phase two did not start');
  const phaseTwoSignal = requests[1].options.signal;

  const explicit = refreshUpdates({quiet: false});
  await tick();
  if (requests.length !== 3) throw new Error('explicit scan did not start');
  if (!phaseTwoSignal || !phaseTwoSignal.aborted) {
    throw new Error('explicit scan did not abort old phase two');
  }
  requests[2].resolve({id: 'explicit', ok: true, pip: [], info: [], summary: {}});
  await explicit;
  requests[1].resolve({id: 'old-full', pip: [], info: [], summary: {}});
  await tick();

  if (renders.join(',') !== 'fast,explicit') {
    throw new Error('old phase two repainted after explicit scan: ' + renders.join(','));
  }
  if (_updatesScanBusy || _updatesBusy || elements.updates_check.disabled) {
    throw new Error('completed explicit scan retained control ownership');
  }
  console.log(JSON.stringify({renders, phaseTwoAborted: phaseTwoSignal.aborted}));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    assert got == {
        "renders": ["fast", "explicit"],
        "phaseTwoAborted": True,
    }


def test_cached_reopen_invalidates_pending_phase_two_before_returning():
    got = _run_update_node(
        r"""
(async () => {
  const first = refreshUpdates({quiet: true});
  await tick();
  requests[0].resolve({id: 'cached', pip: [], info: [], summary: {}});
  await first;
  const oldSignal = requests[0].options.signal;
  if (timers.size !== 1) throw new Error('first quiet scan did not queue phase two');
  _updatesReportAt = now;

  await refreshUpdates({quiet: true});
  if (!oldSignal || !oldSignal.aborted) {
    throw new Error('cached reopen did not invalidate the old generation');
  }
  if (timers.size !== 0 || _updatesBgTimer !== null) {
    throw new Error('cached reopen left old phase two queued');
  }
  if (requests.length !== 1) throw new Error('fresh client cache unexpectedly hit the network');
  console.log(JSON.stringify({
    oldAborted: oldSignal.aborted,
    timers: timers.size,
    renders,
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    assert got == {
        "oldAborted": True,
        "timers": 0,
        "renders": ["cached", "cached"],
    }


def test_stale_timer_callback_preserves_the_newer_timer_owner():
    got = _run_update_node(
        r"""
(async () => {
  const first = refreshUpdates({quiet: true});
  await tick();
  requests[0].resolve({id: 'first', pip: [], info: [], summary: {}});
  await first;
  const staleTimerId = _updatesBgTimer;
  const staleCallback = timers.get(staleTimerId).callback;

  now += UPDATES_CLIENT_TTL_MS + 1;
  const second = refreshUpdates({quiet: true});
  await tick();
  requests[1].resolve({id: 'second', pip: [], info: [], summary: {}});
  await second;
  const currentTimerId = _updatesBgTimer;
  if (currentTimerId === staleTimerId) throw new Error('new scan reused stale timer ownership');

  staleCallback();
  await tick();
  if (_updatesBgTimer !== currentTimerId || !timers.has(currentTimerId)) {
    throw new Error('stale timer callback cleared the newer timer');
  }
  if (requests.length !== 2) throw new Error('stale timer callback launched a request');
  console.log(JSON.stringify({
    currentTimerPreserved: true,
    renders,
    requests: requests.length,
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    assert got == {
        "currentTimerPreserved": True,
        "renders": ["first", "second"],
        "requests": 2,
    }


def test_current_non_abort_scan_failure_keeps_existing_error_ui():
    got = _run_update_node(
        r"""
(async () => {
  const scan = refreshUpdates({quiet: false});
  await tick();
  requests[0].reject(new Error('network down'));
  await scan;
  if (elements.updates_hint.className !== 'hint bad'
      || !elements.updates_hint.textContent.includes('network down')) {
    throw new Error('current scan failure did not update the hint');
  }
  if (!elements.updates_body.innerHTML.includes('Scan failed')
      || !elements.updates_body.innerHTML.includes('network down')) {
    throw new Error('current scan failure did not update the empty state');
  }
  if (_updatesScanBusy || elements.updates_check.disabled) {
    throw new Error('current failed scan retained control ownership');
  }
  console.log(JSON.stringify({
    hintClass: elements.updates_hint.className,
    scanBusy: _updatesScanBusy,
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    assert got == {"hintClass": "hint bad", "scanBusy": False}


def test_session_reset_aborts_and_invalidates_update_scan_state():
    reset_source = _function_slice(
        "function resetAuthenticatedUiState(",
        "document.addEventListener('visibilitychange'",
    )
    got = _run_update_node(
        r"""
(async () => {
  const scan = refreshUpdates({quiet: false});
  await tick();
  const oldSignal = requests[0].options.signal;
  const oldGeneration = _updatesScanGeneration;
  resetAuthenticatedUiState();
  if (!oldSignal || !oldSignal.aborted) throw new Error('session reset did not abort scan');
  if (_updatesScanGeneration <= oldGeneration) {
    throw new Error('session reset did not advance scan generation');
  }
  if (_updatesScanController !== null || _updatesBgTimer !== null) {
    throw new Error('session reset retained scan resources');
  }
  if (_updatesBusy || _updatesScanBusy || elements.updates_check.disabled) {
    throw new Error('session reset retained control ownership');
  }
  requests[0].resolve({id: 'stale-after-reset', pip: [], info: [], summary: {}});
  await scan;
  if (renders.length) throw new Error('reset scan rendered stale response');
  console.log(JSON.stringify({
    aborted: oldSignal.aborted,
    generationAdvanced: _updatesScanGeneration > oldGeneration,
    renders,
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
""",
        extra_source=(
            r"""
function resetAuthenticatedControls() {}
function resetRerunState() {}
let jobsInFlight = true;
let stRunning = true;
function stStopPhases() {}
const watchActionsInFlight = new Set(['watch']);
"""
            + reset_source
        ),
    )
    assert got == {
        "aborted": True,
        "generationAdvanced": True,
        "renders": [],
    }


_INSTALLER_HARNESS = r"""
const installRequests = [];
let TOKEN = 'signed-in';

function installDeferred(kind, path, options) {
  let resolve;
  let reject;
  const promise = new Promise((ok, bad) => { resolve = ok; reject = bad; });
  installRequests.push({kind, path, options: options || {}, resolve, reject});
  return promise;
}
function api(path, options) { return installDeferred('api', path, options); }
function sessionFetch(path, options) {
  return installDeferred('sessionFetch', path, options);
}
async function sessionJson(response) { return response.json(); }
function confirm() { return true; }
function hostOsFromReport() { return 'Windows'; }
function installOutcome(body, tool) {
  const b = body || {};
  const result = b.result || {};
  if (!b.ok) {
    return {level: 'bad', label: 'Open Updates →', detail: b.error || tool + ' failed'};
  }
  return {level: 'ok', label: 'Installed ✓', detail: result.message || tool + ' ready'};
}
function esc(value) { return String(value); }

function controlOwner(element, options = {}) {
  const before = {
    disabled: !!element.disabled,
    text: element.textContent,
    title: element.title,
  };
  let committed = false;
  if (options.disable !== false) element.disabled = true;
  if (Object.prototype.hasOwnProperty.call(options, 'text')) {
    element.textContent = options.text;
  }
  return {
    release() {
      if (committed) return false;
      element.disabled = before.disabled;
      element.textContent = before.text;
      element.title = before.title;
      return true;
    },
    commit(callback) {
      callback();
      committed = true;
      return true;
    },
  };
}
function beginAuthenticatedAction() {
  const owner = sessionGeneration;
  return {
    isCurrent() { return owner === sessionGeneration; },
    guard() {
      if (owner !== sessionGeneration) throw abortError('stale session');
    },
    isSilentError(error) {
      return owner !== sessionGeneration || !!(error && error.name === 'AbortError');
    },
    effect(callback) {
      if (owner !== sessionGeneration) return false;
      callback();
      return true;
    },
    ownControl(element, options) { return controlOwner(element, options); },
  };
}
"""


@pytest.mark.parametrize("consumer", ["optional", "ffmpeg", "speed"])
def test_each_installer_invalidates_pending_scan_and_keeps_its_owner(consumer):
    """All current installer entry points defeat abort-ignoring stale scan doubles."""
    optional_source = _function_slice(
        "async function runOptionalToolInstall(",
        "function shortInstallLabel(",
    )
    ffmpeg_source = _function_slice(
        "async function runFFmpegAction(",
        "if ($('updates_check'))",
    )
    speed_source = _function_slice(
        "async function stDoAction(",
        "function stRenderRec(",
    )
    source = {
        "optional": optional_source,
        "ffmpeg": ffmpeg_source,
        "speed": speed_source,
    }[consumer]
    body = (
        r"""
(async () => {
  const scan = refreshUpdates({quiet: false});
  await tick();
  if (requests.length !== 1) throw new Error('explicit scan did not start');
  const oldSignal = requests[0].options.signal;
  const btn = {
    dataset: {
      systemTool: 'node',
      version: '8.1.2',
      osTarget: 'Windows',
      managedLocation: 'managed-folder',
    },
    disabled: false,
    textContent: 'Install',
    title: '',
    classList: classList(),
    parentNode: null,
  };
"""
        + {
            "optional": "  const install = runOptionalToolInstall(btn);\n",
            "ffmpeg": "  const install = runFFmpegAction(btn);\n",
            "speed": r"""
  const install = stDoAction({
    label: 'Install Node',
    action: {
      kind: 'install_tool',
      endpoint: '/api/updates/tools',
      payload: {tool: 'node'},
    },
  }, btn);
""",
        }[consumer]
        + r"""
  await tick();
  if (!oldSignal || !oldSignal.aborted) {
    throw new Error('installer did not abort pending scan');
  }
  if (!_updatesBusy || _updatesScanBusy || !elements.updates_check.disabled) {
    throw new Error('installer did not exclusively own Updates controls');
  }
  if (installRequests.length !== 1) throw new Error('installer request did not start');

  requests[0].resolve({id: 'stale-scan', ok: true, pip: [], info: [], summary: {}});
  await scan;
  if (renders.includes('stale-scan')) throw new Error('stale scan rendered during installer');
  if (!_updatesBusy || !elements.updates_check.disabled) {
    throw new Error('stale scan finally released installer-owned controls');
  }
"""
        + (
            r"""
  installRequests[0].resolve({
    ok: true,
    json: async () => ({
      ok: true,
      result: {ok: true, message: 'managed ready'},
      report: {id: 'installer-report', pip: [], info: [], summary: {}},
    }),
  });
"""
            if consumer in {"optional", "ffmpeg"}
            else r"""
  installRequests[0].resolve({
    ok: true,
    result: {ok: true, message: 'managed ready'},
    report: {id: 'installer-report', pip: [], info: [], summary: {}},
  });
"""
        )
        + r"""
  await install;
  if (_updatesBusy || _updatesScanBusy || elements.updates_check.disabled) {
    throw new Error('completed installer retained global ownership');
  }
"""
        + (
            r"""
  if (btn.textContent !== 'Installed ✓' || !btn.disabled
      || !btn.classList.contains('is-done')) {
    throw new Error('Speed committed control was undone by global finally');
  }
"""
            if consumer == "speed"
            else ""
        )
        + r"""
  console.log(JSON.stringify({
    consumer: '"""
        + consumer
        + r"""',
    oldAborted: oldSignal.aborted,
    staleRendered: renders.includes('stale-scan'),
    busy: _updatesBusy,
    committed: btn.classList.contains('is-done'),
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    got = _run_update_node(
        body,
        extra_source=_INSTALLER_HARNESS + "\n" + source,
    )
    assert got == {
        "consumer": consumer,
        "oldAborted": True,
        "staleRendered": False,
        "busy": False,
        "committed": consumer == "speed",
    }


def test_updates_installer_owner_rejects_concurrent_speed_mutation():
    """A Speed recommendation lives outside .upd-act and must not steal shared ownership."""
    optional_source = _function_slice(
        "async function runOptionalToolInstall(",
        "function shortInstallLabel(",
    )
    speed_source = _function_slice(
        "async function stDoAction(",
        "function stRenderRec(",
    )
    got = _run_update_node(
        r"""
(async () => {
  const updatesButton = {
    dataset: {systemTool: 'node'},
    disabled: false,
    textContent: 'Install',
    title: '',
    classList: classList(),
    parentNode: null,
  };
  const speedButton = {
    dataset: {updateInstall: '1'},
    disabled: false,
    textContent: 'Install Node',
    title: 'Install the approved Node artifact',
    classList: classList(),
    parentNode: null,
  };
  speedInstallButtons.push(speedButton);

  const updatesInstall = runOptionalToolInstall(updatesButton);
  await tick();
  if (installRequests.length !== 1 || !_updatesBusy || !_updatesInstallOwner) {
    throw new Error('Updates installer did not establish sole ownership');
  }
  const firstOwner = _updatesInstallOwner;
  if (!speedButton.disabled) throw new Error('shared ownership did not disable Speed installer');

  const speedInstall = stDoAction({
    label: 'Install Node',
    action: {
      kind: 'install_tool',
      endpoint: '/api/updates/tools',
      payload: {tool: 'node'},
    },
  }, speedButton);
  await tick();
  if (installRequests.length !== 1) throw new Error('Speed launched a concurrent installer POST');
  if (_updatesInstallOwner !== firstOwner || !_updatesBusy) {
    throw new Error('rejected Speed install stole or released first ownership');
  }
  if (!speedButton.disabled || !speedButton.title.includes('already running')) {
    throw new Error('rejected Speed install lost stable busy feedback');
  }

  installRequests[0].resolve({
    ok: true,
    json: async () => ({
      ok: true,
      result: {ok: true, message: 'managed ready'},
      report: {id: 'installer-report', pip: [], info: [], summary: {}},
    }),
  });
  await Promise.all([updatesInstall, speedInstall]);
  if (_updatesBusy || _updatesInstallOwner !== null) {
    throw new Error('first installer did not release its ownership');
  }
  if (speedButton.disabled
      || speedButton.title !== 'Install the approved Node artifact') {
    throw new Error('Speed busy feedback did not restore after first owner completed');
  }
  console.log(JSON.stringify({
    requests: installRequests.length,
    ownerPreserved: true,
    speedRestored: !speedButton.disabled,
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
""",
        extra_source=(
            _INSTALLER_HARNESS
            + "\n"
            + optional_source
            + "\n"
            + speed_source
        ),
    )
    assert got == {
        "requests": 1,
        "ownerPreserved": True,
        "speedRestored": True,
    }


@pytest.mark.parametrize("consumer", ["optional", "ffmpeg"])
def test_updates_callers_reject_an_existing_installer_owner(consumer):
    """Disabled UI is not the lock; direct callers must also stop before their POST."""
    source = {
        "optional": _function_slice(
            "async function runOptionalToolInstall(",
            "function shortInstallLabel(",
        ),
        "ffmpeg": _function_slice(
            "async function runFFmpegAction(",
            "if ($('updates_check'))",
        ),
    }[consumer]
    body = (
        r"""
(async () => {
  const firstOwner = beginUpdatesInstall();
  const btn = {
    dataset: {
      systemTool: 'node',
      version: '8.1.2',
      osTarget: 'Windows',
      managedLocation: 'managed-folder',
    },
    disabled: true,
    textContent: 'Install',
    title: '',
    classList: classList(),
    parentNode: null,
  };
"""
        + (
            "  const rejected = runOptionalToolInstall(btn);\n"
            if consumer == "optional"
            else "  const rejected = runFFmpegAction(btn);\n"
        )
        + r"""
  await rejected;
  if (installRequests.length !== 0) throw new Error('rejected caller emitted a POST');
  if (_updatesInstallOwner !== firstOwner || !_updatesBusy) {
    throw new Error('rejected caller changed the existing owner');
  }
  if (!elements.updates_hint.textContent.includes('already running')) {
    throw new Error('rejected caller did not provide stable busy feedback');
  }
  if (!finishUpdatesInstall(firstOwner) || _updatesBusy || _updatesInstallOwner) {
    throw new Error('first owner did not finish cleanly');
  }
  console.log(JSON.stringify({
    consumer: '"""
        + consumer
        + r"""',
    requests: installRequests.length,
    ownerPreserved: true,
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )
    got = _run_update_node(
        body,
        extra_source=_INSTALLER_HARNESS + "\n" + source,
    )
    assert got == {
        "consumer": consumer,
        "requests": 0,
        "ownerPreserved": True,
    }


def test_speed_install_recommendation_reflects_shared_installer_busy_state():
    """The rendered Speed control is marked, disabled, explained, and later restored."""
    render_source = _function_slice(
        "function stRenderRec(",
        "// --- run history",
    )
    got = _run_update_node(
        r"""
const created = [];
document.createElement = (tagName) => {
  const node = {
    tagName,
    className: '',
    textContent: '',
    title: '',
    type: '',
    disabled: false,
    dataset: {},
    classList: classList(),
    children: [],
    appendChild(child) { this.children.push(child); return child; },
    addEventListener() {},
  };
  created.push(node);
  return node;
};

_updatesBusy = true;
stRenderRec({
  title: 'Install Node',
  detail: 'Approved runtime',
  action: {kind: 'install_tool', label: 'Install Node'},
});
const button = created.find((node) => node.className === 'st-rec-btn');
if (!button || button.dataset.updateInstall !== '1') {
  throw new Error('rendered Speed installer lacks its shared-owner marker');
}
if (!button.disabled || !button.title.includes('already running')) {
  throw new Error('rendered Speed installer ignored active ownership');
}
speedInstallButtons.push(button);
_updatesBusy = false;
applyUpdatesControlState();
if (button.disabled || button.title !== '') {
  throw new Error('rendered Speed installer did not restore after ownership ended');
}
console.log(JSON.stringify({
  marked: button.dataset.updateInstall,
  restored: !button.disabled,
}));
""",
        extra_source=render_source,
    )
    assert got == {"marked": "1", "restored": True}
