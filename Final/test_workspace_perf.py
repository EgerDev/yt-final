"""Batch D — stop doing work the Workspace does not need.

D1: upsertCard computed a content signature to skip expensive subtree rebuilds, but ~20 further DOM
    writes (pill, dismiss title + onclick, job id + onclick, attempts, the result row and the caveat
    row) ran unconditionally for EVERY visible card on EVERY 1.2s poll. They are now guarded.
    Critically, the progress block and the ageing "N ago" text are deliberately NOT guarded:
    j.progress and j.updated_at are absent from contentSig, so guarding them would freeze live
    progress — which is exactly the trap a naive "move the guard up" falls into.

D2: refreshWatches() polled every 5s for the whole session even while the Watch panel was hidden
    behind another view. Now view-scoped, mirroring the existing visibilitychange behaviour for a
    hidden tab.

D3 is deliberately NOT changed — see test_jobs_payload_scaling_is_documented_not_guessed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

WEBUI = Path(__file__).resolve().parent / "webui"
JS = (WEBUI / "app.js").read_text(encoding="utf-8")
SERVER = (Path(__file__).resolve().parent / "webserver.py").read_text(encoding="utf-8")


def _upsert_body() -> str:
    return JS.split("function upsertCard(", 1)[1].split("\nfunction ", 1)[0]


def test_content_derived_card_writes_are_guarded():
    """The cheap-but-numerous writes must sit behind the unchanged-content guard."""
    body = _upsert_body()
    # everything from the pill to the caveat row is inside a !contentUnchanged block
    assert body.count("if (!contentUnchanged) {") >= 3, \
        "expected the content-derived writes to be guarded, not just the label subtree"
    pill = body.split("const pill = el.querySelector('.pill');", 1)[0]
    # the nearest preceding guard opener must be closer than the nearest preceding closer,
    # i.e. the pill write really is inside a guard
    assert pill.rfind("if (!contentUnchanged) {") > pill.rfind("\n  }\n"), \
        "the pill/dismiss/id/attempts writes are still unconditional"


def test_live_progress_and_relative_time_are_NOT_guarded():
    """These depend on fields absent from contentSig, so guarding them would freeze the UI mid-download.
    This is the regression that a careless optimisation of D1 would introduce."""
    body = _upsert_body()
    sig = body.split("const contentSig = JSON.stringify([", 1)[1].split("]);", 1)[0]
    assert "j.progress" not in sig and "j.updated_at" not in sig, \
        "if these enter the signature the reasoning below changes — revisit the guards"
    # the running-progress branch and the fmtAgo write must be reachable when content is unchanged:
    # they live AFTER the last guard closes and BEFORE the unchanged early-return
    tail = body.split("end of the content-derived guard", 1)[1].split("if (contentUnchanged) return el;", 1)[0]
    assert "if (j.status === 'running')" in tail, "live progress must stay outside the guard"
    assert "fmtAgo(j.updated_at)" in tail, "the ageing 'N ago' text must stay outside the guard"
    assert "el.dataset.start" in body.split("if (!contentUnchanged) {", 2)[-1], \
        "dataset.start feeds tickElapsed and must not be guarded"


def test_watch_polling_is_scoped_to_its_view():
    assert "function syncWatchPolling(" in JS
    fn = JS.split("function syncWatchPolling(", 1)[1].split("\n}", 1)[0]
    assert "'watch'" in fn, "the poller must only run for the watch view"
    assert "clearAuthenticatedInterval(watchTimer)" in fn, \
        "it must stop the session-scoped timer when the view is left"
    assert "refreshWatches()" in fn, "switching TO watch must refresh immediately, not show stale rows"
    assert "syncWatchPolling(name)" in JS, "activateView must drive it"
    # and startPolling must no longer unconditionally arm the watch interval
    sp = JS.split("function startPolling(", 1)[1].split("\n}", 1)[0]
    assert "setInterval(refreshWatches" not in sp, "startPolling must not arm the watch poller directly"


def test_jobs_payload_scaling_is_documented_not_guessed():
    """D3: _jobs_change_token folds a live progress counter into the ETag, so during a download the
    token changes every poll and the whole job list is re-serialized. That is intentional (progress
    IS the thing that changed) and bounding the payload would change the API and the filter counts,
    so it is left alone ON PURPOSE. This test pins the reasoning so a future reader does not 'fix' it
    blindly, and fails if the token stops explaining itself."""
    tok = SERVER.split("def _jobs_change_token(", 1)[1].split("\n    def ", 1)[0]
    assert "progress" in tok.lower(), "the token must still fold live progress (that is the design)"
    assert "304" in tok or "re-serializing" in tok, \
        "keep the docstring that explains why an unchanged poll is cheap"


def test_watch_polling_state_machine_executes_correctly():
    """EXECUTE syncWatchPolling. A source assertion cannot tell 'only on the watch view' from
    'always on' — forcing the view to 'watch' inside the function leaves every token in place and the
    string check still passes. Run the state machine in node instead."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the UI helpers")

    fn = "function syncWatchPolling(" + JS.split("function syncWatchPolling(", 1)[1].split("\n}", 1)[0] + "\n}"
    harness = fn + r"""
var appStarted = true, watchTimer = null, _currentView = 'workspace';
var refreshes = 0, armed = 0, cleared = 0, nextId = 1;
function refreshWatches() { refreshes++; }
function setInterval(fn, ms) { armed++; return nextId++; }
function clearInterval(id) { cleared++; }
function authenticatedInterval(fn, ms) { return setInterval(fn, ms); }
function clearAuthenticatedInterval(id) { clearInterval(id); }
var log = [];
function snap(tag) { log.push([tag, watchTimer !== null, refreshes, armed, cleared]); }
syncWatchPolling('workspace'); snap('workspace');
syncWatchPolling('watch');     snap('watch');
syncWatchPolling('watch');     snap('watch_again');
syncWatchPolling('auth');      snap('auth');
syncWatchPolling('watch');     snap('back_to_watch');
console.log(JSON.stringify(log));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr[-400:]}"
    log = {row[0]: {'armed': row[1], 'refreshes': row[2], 'arms': row[3], 'clears': row[4]}
           for row in json.loads(r.stdout.strip().splitlines()[-1])}

    # on a non-watch view the poller must NOT be running and must not fetch
    assert log['workspace']['armed'] is False and log['workspace']['refreshes'] == 0, log
    # switching to watch arms it AND refreshes immediately (no stale rows)
    assert log['watch']['armed'] is True and log['watch']['refreshes'] == 1, log
    # re-entering the same view must not stack a second interval or re-fetch
    assert log['watch_again']['arms'] == 1 and log['watch_again']['refreshes'] == 1, log
    # leaving watch stops the timer
    assert log['auth']['armed'] is False and log['auth']['clears'] == 1, log
    # and coming back arms it again
    assert log['back_to_watch']['armed'] is True and log['back_to_watch']['arms'] == 2, log


def test_lifecycle_state_is_declared_before_activate_view_can_run():
    """REGRESSION GUARD for a bug this batch introduced and live-testing caught.

    activateView() runs at MODULE LEVEL during boot (to apply the #hash) and now calls
    syncWatchPolling(), which reads appStarted / watchTimer / _currentView. Those were `let`-declared
    ~3800 lines further down, so that call hit the temporal dead zone, threw a ReferenceError, and
    ABORTED the whole script — every later declaration (including `cards`) stayed uninitialized and
    the page loaded dead. node --check cannot see this (it is valid syntax), and the source-assertion
    tests all passed. So: the declarations must precede the first activateView() call site.
    """
    decl = min(JS.index("let jobsTimer"), JS.index("let _currentView"))
    first_call = JS.index("\nactivateView(")
    assert decl < first_call, \
        "lifecycle state must be declared before activateView() runs at module level (TDZ -> dead page)"
    # and syncWatchPolling must only read state, never be invoked at module level itself
    assert "\nsyncWatchPolling(" not in JS, "do not call syncWatchPolling at module level"


def test_card_class_rewrite_logic_executes_correctly():
    """REGRESSION: guarding the result/caveat writes (D1) exposed that `el.className = 'job ' + status`
    is a full REPLACE which WIPES the derived has-result / is-degraded flags. Those flags are now only
    re-applied behind the unchanged-content guard, so rewriting the class list on every 1.2s poll
    stripped the amber pill off a partly-failed job one poll after it appeared. Reproduced live:
    "job done has-result is-degraded" -> "job done".

    The invariant that makes the fix safe: j.status is INSIDE contentSig, so a class rewrite can only
    happen when the content guard also runs — the flags always come straight back."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the UI helpers")
    fn = ("function cardClassNeedsRewrite("
          + JS.split("function cardClassNeedsRewrite(", 1)[1].split("\n}", 1)[0] + "\n}")
    harness = fn + r"""
console.log(JSON.stringify({
  fresh_card:        cardClassNeedsRewrite(true, undefined, 'queued'),
  nothing_changed:   cardClassNeedsRewrite(false, 'running', 'running'),
  status_changed:    cardClassNeedsRewrite(false, 'running', 'done'),
  empty_to_empty:    cardClassNeedsRewrite(false, '', undefined),
  undefined_to_same: cardClassNeedsRewrite(false, undefined, ''),
  done_to_failed:    cardClassNeedsRewrite(false, 'done', 'failed')
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr[-300:]}"
    got = json.loads(r.stdout.strip().splitlines()[-1])
    # THE fix: an unchanged card must not have its class list rewritten
    assert got["nothing_changed"] is False, "this is the regression — the flags get wiped"
    assert got["empty_to_empty"] is False and got["undefined_to_same"] is False, got
    # ...but a new card or a real status change still must be
    assert got["fresh_card"] is True and got["status_changed"] is True and got["done_to_failed"] is True, got
    # ...and the flags must still be (re)applied behind the content guard, so that every rewrite is
    # followed by a re-apply. Checked by BRACE DEPTH, not by matching a closing line: nested blocks
    # close at the same indent, so a textual "nearest closer" heuristic is simply wrong here.
    body = _upsert_body()
    tgl = body.index("classList.toggle('is-degraded'")
    guard_open = body.rfind("if (!contentUnchanged) {", 0, tgl)
    assert guard_open != -1, "no unchanged-content guard precedes the is-degraded toggle"
    depth = 0
    for ch in body[guard_open:tgl]:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:                      # the guard block closed before reaching the toggle
                break
    assert depth > 0, "is-degraded is re-applied OUTSIDE the unchanged-content guard"
    # And the guard must actually be USED at the className site — testing the helper in isolation
    # would still pass while the shipped code rewrote the class list unconditionally.
    assert "if (cardClassNeedsRewrite(isNew, prevStatus, j.status)) el.className = 'job '" in body, \
        "the class rewrite must go through cardClassNeedsRewrite"
    assert "\n  el.className = 'job '" not in body, \
        "the class rewrite is still unconditional — this is the shipped regression"


def test_the_absolute_timestamp_is_memoised_but_the_relative_one_is_not():
    """D1 remainder. Both live on adjacent lines and BOTH depend on j.updated_at, which is deliberately
    absent from contentSig — so neither can go behind the content guard. But they are not the same:

      * updated.textContent ("3m ago") MUST be rewritten every poll. That is the whole feature.
      * updated.title (the absolute time) is a pure function of j.updated_at, yet toLocaleString() is
        Intl-backed and ran for every visible card on every 1.2 s poll.

    So the title is memoised on its own key. Getting this wrong in either direction is silent: guard
    the relative text and the ageing freezes; drop the memo and the cost comes back unnoticed."""
    body = _upsert_body()
    seg = body.split("const ago = fmtAgo(j.updated_at);", 1)[1].split("\n    }", 1)[0]
    assert "updated.textContent = ago" in seg, "the relative text must still be written every poll"
    # the memo key, and BOTH the read and the write of it, must bracket only the title assignment
    assert "el._lastUpdatedTs !== j.updated_at" in seg, "the absolute timestamp is not memoised"
    assert "el._lastUpdatedTs = j.updated_at" in seg, "the memo never stores its key"
    memo = seg.split("if (el._lastUpdatedTs !== j.updated_at) {", 1)[1]
    assert "toLocaleString()" in memo, "toLocaleString must be INSIDE the memo — that is the cost"
    # comments in this region legitimately NAME toLocaleString, so compare code lines only
    before = "\n".join(ln for ln in seg.split("if (el._lastUpdatedTs !== j.updated_at) {", 1)[0].splitlines()
                       if not ln.strip().startswith("//"))
    assert "toLocaleString()" not in before, "a second unmemoised toLocaleString is back"
    assert "updated.textContent" not in memo, \
        "the relative text is now inside the memo — it will freeze and stop ageing"

    # ...and the whole thing must stay ABOVE the content-guard early return, or it freezes anyway
    assert body.index("el._lastUpdatedTs") < body.index("if (contentUnchanged) return el;"), \
        "the timestamp block moved below the content guard — it would stop updating entirely"


def test_copy_button_labels_survive_a_double_click():
    """REAL SHIPPED BUG, settled by execution rather than argument (two independent reviews disagreed).

    copyText captured `const prev = btn.textContent` at click time and restored it 900 ms later. Click
    twice inside that window and the second capture is already 'copied', so the last timer to fire
    restores 'copied' and the button keeps that label for the rest of the session — permanently,
    because upsertCard's content guard never rebuilds a finished card's row again. Before that guard
    existed the 1.2 s rebuild silently healed it, which is why the bug was invisible.

    Worst on the job-id button, where the label IS the data: a double-click replaced the id the user
    was trying to read. Three call sites share the one function (copy path, copy error, the job id)."""
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the UI helpers")
    src = (WEBUI / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    fn = "async function copyText(" + src.split("async function copyText(", 1)[1].split("\n}\n", 1)[0] + "\n}"
    # NOTE: Node >= 21 ships a READ-ONLY `navigator`, so a plain assignment is ignored and copyText
    # silently takes its textarea fallback; and copyText's own catch swallows everything, so a harness
    # gap looks exactly like "the bug is fixed". defineProperty, stub execCommand, and make the catch
    # shout — an earlier version of this harness reported a false PASS for precisely those reasons.
    harness = fn + r"""
Object.defineProperty(globalThis, 'navigator', {
  value: { clipboard: { writeText: () => Promise.resolve() } }, writable: true, configurable: true });
global.document = { createElement: () => ({ select(){}, remove(){}, style:{}, value:'' }),
                    body: { appendChild(){} }, execCommand: () => true };
var swallowed = null;
global.pipelineHint = (ok, msg) => { swallowed = msg; };
var now = 0, timers = [];
global.setTimeout = (f, ms) => { timers.push({at: now+ms, f, done:false}); return timers.length; };
global.clearTimeout = (id) => { if (timers[id-1]) timers[id-1].done = true; };
global.authenticatedTimeout = (f, ms) => setTimeout(f, ms);
global.clearAuthenticatedTimeout = (id) => clearTimeout(id);
global.beginAuthenticatedAction = () => ({
  guard() {},
  isSilentError() { return false; },
  ownControl(btn, options = {}) {
    const previousText = btn.textContent;
    const previousDisabled = Boolean(btn.disabled);
    if (options.disable !== false) btn.disabled = true;
    if (options.text != null) btn.textContent = options.text;
    let released = false;
    return {
      release() {
        if (released) return;
        released = true;
        btn.textContent = previousText;
        btn.disabled = previousDisabled;
      }
    };
  }
});
function tick(ms) { now += ms; timers.forEach(t => { if (!t.done && t.at <= now) { t.done = true; t.f(); } }); }
function mk(label) { return { textContent: label }; }
(async () => {
  const out = {};
  let b = mk('copy path'); await copyText('p', b); tick(3000);
  out.single = b.textContent;
  b = mk('copy path'); await copyText('p', b); now += 100; await copyText('p', b); tick(3000);
  out.double = b.textContent;
  b = mk('copy path');
  for (let i = 0; i < 5; i++) { await copyText('p', b); now += 50; }
  tick(5000); out.five_rapid = b.textContent;
  b = mk('a1b2c3d4e5f'); await copyText('a1b2c3d4e5f', b); now += 100;
  await copyText('a1b2c3d4e5f', b); tick(3000); out.job_id = b.textContent;
  b = mk('copy path'); await copyText('p', b); tick(400); out.mid_window = b.textContent;
  out.swallowed = swallowed;
  console.log(JSON.stringify(out));
})();
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    assert r.returncode == 0, "node failed: " + r.stderr[-400:]
    g = json.loads(r.stdout.strip().splitlines()[-1])

    assert g["swallowed"] is None, "copyText threw — the harness is not exercising the real path: " + str(g)
    # the feedback itself must still work
    assert g["mid_window"] == "copied", "the 'copied' confirmation no longer appears"
    # THE BUG: every one of these must settle back to the original label
    assert g["single"] == "copy path", g
    assert g["double"] == "copy path", "a double click leaves the button reading 'copied' forever"
    assert g["five_rapid"] == "copy path", "rapid clicks still strand the label"
    assert g["job_id"] == "a1b2c3d4e5f", \
        "double-clicking the job id destroys the id — the label IS the data here"
