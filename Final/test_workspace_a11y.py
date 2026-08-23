"""Batch B — Workspace accessibility: keyboard reach, announcements, and focus discipline.

Each defect was verified in the shipped code first:
  * the cookies "Upload…" control was a <label for> pointing at a `hidden` file input — a label is not
    focusable and a hidden input is out of the tab order, so it was MOUSE-ONLY;
  * job status transitions were never announced (the list has no live region, and putting one on the
    list itself would babble, since progress rewrites ~1/s per running job);
  * the probe popover is role="dialog" but had no focus-in, no Esc, and no focus return;
  * #cookiefile had no programmatic label (a plain <span> sat next to it);
  * the preset delete button's accessible name was the glyph ✕ (name-from-content beats title);
  * a document-level Escape collapsed the Advanced drawer regardless of where focus was, dropping
    focus to <body> mid-typing.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

WEBUI = Path(__file__).resolve().parent / "webui"
HTML = (WEBUI / "index.html").read_text(encoding="utf-8")
JS = (WEBUI / "app.js").read_text(encoding="utf-8")
CSS = (WEBUI / "style.css").read_text(encoding="utf-8")


class _MarkupIndex(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[tuple[str, dict[str, str | None]]] = []
        self.by_id: dict[str, tuple[str, dict[str, str | None]]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        parsed = dict(attrs)
        self.nodes.append((tag, parsed))
        if parsed.get("id"):
            self.by_id[str(parsed["id"])] = (tag, parsed)


MARKUP = _MarkupIndex()
MARKUP.feed(HTML)


def _node_json(source: str) -> object:
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the UI")
    result = subprocess.run(
        [node, "-e", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, f"node failed: {result.stderr[-1000:]}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _function_source(name: str) -> str:
    marker = f"function {name}("
    assert marker in JS, f"missing required frontend function {name}()"
    body = JS.split(marker, 1)[1].split("\n}\n", 1)[0]
    return marker + body + "\n}"


def _view_navigation_source() -> str:
    start = "// ---- view navigation"
    end = "// ---- compose ----"
    assert start in JS and end in JS
    start_at = JS.index("\n", JS.index(start)) + 1
    end_at = JS.index(end, start_at)
    return JS[start_at:end_at]


def test_login_status_region_and_local_copy_are_accessible_and_truthful():
    """The login feedback node is stable/live, while product copy stays local without erasing
    legitimate loopback proxy instructions elsewhere in the page."""
    node = MARKUP.by_id.get("login-msg")
    assert node is not None, "expected one stable #login-msg announcement node"
    tag, attrs = node
    assert tag == "p"
    assert attrs.get("role") == "status"
    assert attrs.get("aria-live") == "polite"
    assert {"hint", "login-hint"} <= set((attrs.get("class") or "").split())
    assert len(re.findall(r'\bid="login-msg"', HTML)) == 1

    gate = HTML.split('<div id="login-gate"', 1)[1].split("</form>", 1)[0]
    assert "local application · session stored in this browser" in gate
    assert "single user" not in gate.lower()
    assert "127.0.0.1" not in gate
    assert "launcher" in gate and "~/.yt_final/webui.txt" in gate

    footer = HTML.split('<footer class="shell foot">', 1)[1].split("</footer>", 1)[0]
    assert "stdlib http · local application · single-writer journal" in footer
    assert "single user" not in footer.lower()
    assert "127.0.0.1" not in footer

    # The wording cleanup is deliberately scoped: these are truthful proxy/tunnel examples.
    assert "socks5://127.0.0.1:1080" in HTML
    assert 'placeholder="127.0.0.1:1080"' in HTML


def test_login_feedback_mutation_log_and_focus_contract_executes():
    """Execute the sole feedback writer: repeat announcements are silent, severity attributes
    precede text, classes survive, and status updates never steal focus."""
    fn = _function_source("setLoginFeedback")
    harness = fn + r"""
const writes = [];
const timerWrites = [];
const classNames = new Set(['hint', 'login-hint', 'hint-flash']);
const attrs = new Map([['role', 'status'], ['aria-live', 'polite']]);
const node = {
  _text: '',
  classList: {
    contains(name) { return classNames.has(name); },
    add(...names) {
      for (const name of names) {
        if (!classNames.has(name)) {
          classNames.add(name);
          writes.push(`class:${name}=on`);
        }
      }
    },
    remove(...names) {
      for (const name of names) {
        if (classNames.delete(name)) writes.push(`class:${name}=off`);
      }
    },
    toggle(name, force) {
      const next = force === undefined ? !classNames.has(name) : !!force;
      if (next === classNames.has(name)) return next;
      if (next) classNames.add(name); else classNames.delete(name);
      writes.push(`class:${name}=${next ? 'on' : 'off'}`);
      return next;
    },
  },
  getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
  setAttribute(name, value) {
    const next = String(value);
    if (attrs.get(name) === next) return;
    attrs.set(name, next);
    writes.push(`attr:${name}=${next}`);
  },
};
Object.defineProperty(node, 'textContent', {
  get() { return this._text; },
  set(value) {
    this._text = String(value);
    writes.push(`text:${this._text}`);
  },
});
function $(id) { return id === 'login-msg' ? node : null; }
const password = { id: 'login-password' };
global.document = { activeElement: password };
global.setTimeout = () => { timerWrites.push('setTimeout'); return 1; };
global.clearTimeout = () => { timerWrites.push('clearTimeout'); };

function step(run) {
  writes.length = 0;
  timerWrites.length = 0;
  const focused = document.activeElement;
  const changed = run();
  return {
    changed,
    writes: [...writes],
    timer_writes: [...timerWrites],
    focus_unchanged: document.activeElement === focused,
    role: node.getAttribute('role'),
    live: node.getAttribute('aria-live'),
    text: node.textContent,
    classes: [...classNames].sort(),
  };
}

const out = {
  initial: step(() => setLoginFeedback()),
  connecting: step(() => setLoginFeedback('Signing in…', 'status')),
  repeat_connecting: step(() => setLoginFeedback('Signing in…', 'status')),
  severity_only_alert: step(() => setLoginFeedback('Signing in…', 'alert')),
  severity_only_status: step(() => setLoginFeedback('Signing in…', 'status')),
  alert: step(() => setLoginFeedback('Wrong password', 'alert')),
  new_alert: step(() => setLoginFeedback('Invalid session response', 'alert')),
  signed_out: step(() => setLoginFeedback('Signed out', 'status')),
  clear: step(() => setLoginFeedback('', 'status')),
  repeat_clear: step(() => setLoginFeedback('', 'status')),
  normalized: step(() => setLoginFeedback('Neutral', 'unexpected')),
};
console.log(JSON.stringify(out));
"""
    got = _node_json(harness)

    assert got["initial"]["changed"] is False
    assert got["initial"]["writes"] == []
    assert got["connecting"]["writes"] == ["text:Signing in…"]
    assert got["repeat_connecting"]["changed"] is False
    assert got["repeat_connecting"]["writes"] == []

    assert got["severity_only_alert"]["writes"] == [
        "attr:role=alert",
        "attr:aria-live=assertive",
        "class:bad=on",
    ]
    assert got["severity_only_alert"]["text"] == "Signing in…"
    assert got["severity_only_alert"]["role"] == "alert"
    assert got["severity_only_alert"]["live"] == "assertive"
    assert got["severity_only_status"]["writes"] == [
        "attr:role=status",
        "attr:aria-live=polite",
        "class:bad=off",
    ]
    assert got["severity_only_status"]["text"] == "Signing in…"
    assert got["severity_only_status"]["role"] == "status"
    assert got["severity_only_status"]["live"] == "polite"

    alert_writes = got["alert"]["writes"]
    assert alert_writes[-1] == "text:Wrong password"
    assert alert_writes.count("text:Wrong password") == 1
    for mutation in ("attr:role=alert", "attr:aria-live=assertive", "class:bad=on"):
        assert mutation in alert_writes
        assert alert_writes.index(mutation) < len(alert_writes) - 1
    assert got["alert"]["role"] == "alert"
    assert got["alert"]["live"] == "assertive"

    assert got["new_alert"]["writes"] == ["text:Invalid session response"]
    status_writes = got["signed_out"]["writes"]
    assert status_writes[-1] == "text:Signed out"
    for mutation in ("attr:role=status", "attr:aria-live=polite", "class:bad=off"):
        assert mutation in status_writes
        assert status_writes.index(mutation) < len(status_writes) - 1
    assert got["signed_out"]["role"] == "status"
    assert got["signed_out"]["live"] == "polite"

    assert got["clear"]["writes"] == ["text:"]
    assert got["repeat_clear"]["changed"] is False
    assert got["repeat_clear"]["writes"] == []
    assert got["normalized"]["writes"] == ["text:Neutral"]
    assert got["normalized"]["role"] == "status"
    assert got["normalized"]["live"] == "polite"

    for name in (
        "connecting",
        "severity_only_alert",
        "severity_only_status",
        "alert",
        "new_alert",
        "signed_out",
        "clear",
        "normalized",
    ):
        assert got[name]["changed"] is True, f"{name} mutated state but returned false"
    for state in got.values():
        assert state["focus_unchanged"] is True
        assert state["timer_writes"] == []
        assert {"hint", "login-hint", "hint-flash"} <= set(state["classes"])


def test_cookie_upload_is_keyboard_reachable():
    """A <label for> + hidden input cannot be tabbed to. The visible control must be a real button
    that forwards to the input, so the existing change handler is untouched."""
    assert not re.search(r'<label[^>]*for="cookiefile_upload"', HTML), \
        "Upload… is still a <label> (not focusable) — keyboard users cannot reach it"
    m = re.search(r'<button[^>]*id="cookiefile_upload_btn"[^>]*>', HTML)
    assert m, "expected a real <button> for Upload…"
    assert "cookiefile_upload_btn" in JS and ".click()" in JS, \
        "the button must forward to the hidden file input's picker"
    # the input stays hidden (we do not want two visible controls) and keeps its change handler
    assert re.search(r'<input[^>]*id="cookiefile_upload"[^>]*hidden', HTML)
    assert "$('cookiefile_upload').addEventListener('change'" in JS


def test_terminal_job_transitions_are_announced_once():
    """Announce done/failed/cancelled — and ONLY the transition. A live region over #jobs would talk
    continuously because the progress text rewrites about once a second per running job."""
    assert 'id="jobs_live"' in HTML, "expected a dedicated live region"
    live = re.search(r'<p[^>]*id="jobs_live"[^>]*>', HTML).group(0)
    assert 'aria-live="polite"' in live and 'visually-hidden' in live
    assert ".visually-hidden{" in CSS, "the live region needs an accessible-but-invisible class"
    # it must NOT be display:none / visibility:hidden (that removes it from the a11y tree)
    vh = CSS.split(".visually-hidden{", 1)[1].split("}", 1)[0]
    assert "display:none" not in vh and "visibility:hidden" not in vh
    assert "function announceJobStatus(" in JS
    # The decision lives in a PURE helper so it can be executed (see the truth-table test below).
    # The original inline version asserted only `prevStatus === j.status`, which passed while still
    # announcing every finished job on FIRST paint (prevStatus is undefined then) — a string
    # assertion could not see that, so the guard now has to be exercised, not matched.
    assert "function jobStatusAnnouncement(" in JS
    body = JS.split("function jobStatusAnnouncement(", 1)[1].split("\n}", 1)[0]
    assert "!prev" in body, "must stay silent when there is no known previous status (first paint)"
    assert "prev === next" in body, "must announce the TRANSITION, not every poll"
    for st in ("'done'", "'failed'", "'cancelled'"):
        assert st in body, f"terminal state {st} not announced"
    assert "announceJobStatus(j, prevStatus)" in JS, "the render path must call it with the transition"


def test_announcement_logic_executes_correctly():
    """EXECUTE the announcement guard. This is the test that would have caught the regression the
    string assertions missed: on a card's FIRST render prevStatus is undefined, so the old
    `prevStatus === j.status` check passed and a page reload read out every finished job in the store.
    Reproduced live before this fix ("a finished" on load)."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the UI helpers")
    fn = ("function jobStatusAnnouncement("
          + JS.split("function jobStatusAnnouncement(", 1)[1].split("\n}", 1)[0] + "\n}")
    harness = fn + r"""
var out = {
  first_paint_done:      jobStatusAnnouncement(undefined, 'done'),      // page reload: must be SILENT
  first_paint_failed:    jobStatusAnnouncement(undefined, 'failed'),    // silent
  first_paint_empty_prev:jobStatusAnnouncement('', 'cancelled'),        // silent
  running_to_done:       jobStatusAnnouncement('running', 'done'),
  running_to_failed:     jobStatusAnnouncement('running', 'failed'),
  running_to_cancelled:  jobStatusAnnouncement('running', 'cancelled'),
  repeat_done:           jobStatusAnnouncement('done', 'done'),         // same state: silent
  queued_to_running:     jobStatusAnnouncement('queued', 'running'),    // not terminal: silent
  weird_status:          jobStatusAnnouncement('running', 'constructor')// must not resolve via proto
};
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr[-300:]}"
    got = json.loads(r.stdout.strip().splitlines()[-1])
    # silence on first paint is the whole point of the fix
    for k in ("first_paint_done", "first_paint_failed", "first_paint_empty_prev",
              "repeat_done", "queued_to_running", "weird_status"):
        assert got[k] == "", f"{k} must be silent, got {got[k]!r}"
    assert got["running_to_done"] == "finished", got
    assert got["running_to_failed"] == "failed", got
    assert got["running_to_cancelled"] == "was cancelled", got


def test_probe_popover_manages_focus_and_closes_on_escape():
    """role="dialog" with no focus handling: opening left focus behind and closing dropped it to
    <body>. It stays aria-modal="false" (focus is not trapped), so this is placement + return only."""
    assert 'aria-modal="false"' in HTML, "must not claim to be modal — focus is not trapped"
    op = JS.split("function openProbePop(", 1)[1].split("\n}", 1)[0]
    assert ".focus(" in op, "opening must place focus inside the dialog"
    cl = JS.split("function closeProbePop(", 1)[1].split("\n}", 1)[0]
    assert ".focus(" in cl, "closing must return focus to the invoking control"
    assert "function probePopOpen(" in JS
    esc = JS.split("if (e.key !== 'Escape'", 1)[1].split("});", 1)[0]
    assert "probePopOpen()" in esc and "closeProbePop()" in esc, "Esc must close the popover first"


def test_cookiefile_has_a_programmatic_label():
    assert re.search(r'<label[^>]*for="cookiefile"[^>]*>Cookies file</label>', HTML), \
        "#cookiefile still has no associated <label>"


def test_preset_delete_has_a_real_accessible_name():
    m = re.search(r'<button[^>]*id="preset_del"[^>]*>', HTML)
    assert m and 'aria-label=' in m.group(0), \
        "the accessible name would be the glyph ✕ (name-from-content beats title)"


def test_escape_does_not_destroy_focus_inside_the_advanced_drawer():
    """Collapsing a container that owns focus sends focus to <body>. If focus is inside the drawer,
    Esc must step focus out instead of collapsing it."""
    esc = JS.split("if (e.key !== 'Escape'", 1)[1].split("});", 1)[0]
    assert "activeElement" in esc, "the handler must consider where focus actually is"
    assert "a.contains(active)" in esc, "focus inside the drawer must be handled separately"
    assert "advToggle" in esc, "step focus out to the toggle rather than dropping it"


def test_focus_is_never_handed_to_an_element_that_cannot_hold_it():
    """canTakeFocus exists because focusing a disabled or unrendered element silently drops focus to
    <body> — worse than leaving it alone. closeProbePop is called defensively right after
    `probe_res.disabled = true`, which is exactly that case. Pure, so the truth table is EXECUTED."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available to execute the UI helpers")
    fn = "function canTakeFocus(" + JS.split("function canTakeFocus(", 1)[1].split("\n}", 1)[0] + "\n}"
    harness = fn + r"""
console.log(JSON.stringify({
  null_el:     canTakeFocus(null),
  undefined_el:canTakeFocus(undefined),
  disabled:    canTakeFocus({ disabled: true,  offsetParent: {} }),
  unrendered:  canTakeFocus({ disabled: false, offsetParent: null }),
  both_bad:    canTakeFocus({ disabled: true,  offsetParent: null }),
  good:        canTakeFocus({ disabled: false, offsetParent: {} })
}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr[-300:]}"
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["good"] is True, got
    for k in ("null_el", "undefined_el", "disabled", "unrendered", "both_bad"):
        assert got[k] is False, f"{k} must not be given focus: {got}"


def test_close_probe_pop_only_moves_focus_when_it_was_open():
    """It is called defensively on paths where the popover was never shown (e.g. 'Paste a YouTube URL
    first'). Taking focus there steals it for no reason."""
    body = JS.split("function closeProbePop(", 1)[1].split("\n}", 1)[0]
    assert "wasOpen" in body and "if (!wasOpen) return;" in body, \
        "closeProbePop must not touch focus when the popover was not open"
    assert "canTakeFocus(" in body, "and must not focus a disabled/unrendered target"


def test_reopening_the_probe_popover_does_not_steal_focus_from_its_own_buttons():
    """applyProbeSuggested / applyProbeMax re-enter openProbePop to refresh the chip states, and they
    are triggered by buttons INSIDE the popover — so focusing on a refresh pulls focus off the control
    the user just pressed."""
    body = JS.split("function openProbePop(", 1)[1].split("\n  pop.hidden = false;", 1)[0]
    assert "const wasOpen = !pop.hidden;" in body, "must record whether this is an open or a refresh"
    tail = JS.split("function openProbePop(", 1)[1].split("\n  pop.hidden = false;", 1)[1]
    assert "if (!wasOpen) {" in tail, "focus must only be taken on a real open"


def test_escape_only_collapses_details_in_the_visible_view():
    """document.querySelector('details[open]') matched the first one in DOCUMENT order — normally in a
    hidden view (the Field guide has several), so Esc silently collapsed something invisible while
    appearing to do nothing."""
    esc = JS.split("if (e.key !== 'Escape'", 1)[1].split("\n});", 1)[0]
    assert "document.querySelector('details[open]')" not in esc, \
        "still collapsing the first open <details> anywhere in the document"
    assert ".view.is-active" in esc, "the fallback must be scoped to the visible view"
    assert "openDetails.contains(active)" in esc and "summary" in esc, \
        "closing a section that owns focus must move focus to its summary first"


def test_no_hidden_file_input_is_driven_by_a_label_anywhere():
    """A <label for> pointing at a hidden <input type="file"> is MOUSE-ONLY: the label is not
    focusable and the input is out of the tab order, so keyboard users cannot open the picker at all.

    The compose panel had this and it was fixed, with the reasoning written into the markup. The Auth
    panel had the identical pattern one view over and kept it — so this scan is document-wide rather
    than per-panel, which is the only version that would have caught the second instance."""
    import re
    hidden_file_ids = set(re.findall(r'<input[^>]*type="file"[^>]*id="([^"]+)"[^>]*hidden', HTML))
    hidden_file_ids |= set(re.findall(r'<input[^>]*id="([^"]+)"[^>]*type="file"[^>]*hidden', HTML))
    assert hidden_file_ids, "no hidden file inputs found — did the markup change shape?"
    for fid in sorted(hidden_file_ids):
        assert 'for="%s"' % fid not in HTML, (
            'a <label for="%s"> drives a hidden file input — keyboard users cannot reach it; '
            'use a real <button> that forwards .click()' % fid)
    # ...and each one must have a real button wired to it in JS
    for fid in sorted(hidden_file_ids):
        assert ("$('%s').click()" % fid) in JS, (
            "no button forwards a click to the hidden input #%s" % fid)


def test_main_tab_markup_has_exact_panel_mapping_and_one_roving_entry_point():
    """Removing one aria-controls, leaving two tabs tabbable, or exposing two panels breaks the
    composite tab contract before JavaScript even runs."""
    expected = {
        "workspace": "view-workspace",
        "watch": "view-watch",
        "auth": "view-auth",
        "speedtest": "view-speedtest",
        "updates": "view-updates",
        "guide": "view-guide",
    }
    tablists = [
        attrs for _, attrs in MARKUP.nodes
        if attrs.get("role") == "tablist"
    ]
    assert len(tablists) == 1, "the main .app-nav must be the document's sole tablist"
    assert "app-nav" in str(tablists[0].get("class", "")).split()

    tabs = []
    panels = []
    for name, panel_id in expected.items():
        tab_tag, tab = MARKUP.by_id[f"nav-{name}"]
        panel_tag, panel = MARKUP.by_id[panel_id]
        assert tab_tag == "button" and tab.get("type") == "button"
        assert tab.get("role") == "tab"
        assert tab.get("aria-controls") == panel_id
        assert panel_tag == "div" and panel.get("role") == "tabpanel"
        assert panel.get("aria-labelledby") == f"nav-{name}"
        tabs.append(tab)
        panels.append(panel)

    assert [tab.get("aria-selected") for tab in tabs] == [
        "true", "false", "false", "false", "false", "false",
    ]
    assert [tab.get("tabindex") for tab in tabs] == ["0", "-1", "-1", "-1", "-1", "-1"]
    assert ["hidden" not in panel for panel in panels] == [
        True, False, False, False, False, False,
    ]


def test_activate_view_and_main_tab_keyboard_execute_full_roving_sequence():
    """Execute the real navigation block. This catches wrong wrap math, missing preventDefault,
    focus that lags selection, duplicate tabbable tabs/panels, and lost hash/effect/polling calls."""
    harness = r"""
const names = ['workspace', 'watch', 'auth', 'speedtest', 'updates', 'guide'];
const focusLog = [];
const pollCalls = [];
let guideEnters = 0;
let updatesRuns = 0;
let _currentView = 'workspace';
let appStarted = false;

function classes(initial = []) {
  const values = new Set(initial);
  return {
    add(...xs) {
      xs.forEach((x) => {
        values.add(x);
        if (x === 'is-ready') guideEnters += 1;
      });
    },
    remove(...xs) { xs.forEach((x) => values.delete(x)); },
    toggle(x, force) {
      if (force === true) { values.add(x); return true; }
      if (force === false) { values.delete(x); return false; }
      if (values.has(x)) { values.delete(x); return false; }
      values.add(x); return true;
    },
    contains(x) { return values.has(x); },
  };
}
function element(id, classNames = []) {
  return {
    id,
    hidden: false,
    tabIndex: -1,
    dataset: {},
    attrs: {},
    listeners: {},
    classList: classes(classNames),
    offsetWidth: 100,
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return this.attrs[name] == null ? null : this.attrs[name]; },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    focus() { document.activeElement = this; focusLog.push(this.id); },
    scrollIntoView() {},
    closest(selector) {
      return selector === '.nav-tab[data-view]' && this.classList.contains('nav-tab') ? this : null;
    },
    matches() { return false; },
  };
}

const outside = element('outside');
const tabs = names.map((name, index) => {
  const tab = element('nav-' + name, ['nav-tab'].concat(index === 0 ? ['is-active'] : []));
  tab.dataset.view = name;
  tab.tabIndex = index === 0 ? 0 : -1;
  tab.attrs['aria-selected'] = index === 0 ? 'true' : 'false';
  return tab;
});
const panels = names.map((name, index) => {
  const panel = element('view-' + name, ['view'].concat(index === 0 ? ['is-active'] : []));
  panel.hidden = index !== 0;
  return panel;
});
const guideScroll = element('guide_scroll');
const byId = new Map([...tabs, ...panels, guideScroll].map((el) => [el.id, el]));
const nav = element('main-nav', ['app-nav']);
nav.querySelectorAll = (selector) => selector === '.nav-tab[data-view]' ? tabs : [];
nav.contains = (candidate) => tabs.includes(candidate);
nav.matches = (selector) => selector === '.app-nav';

global.document = {
  activeElement: outside,
  getElementById: (id) => byId.get(id) || null,
  querySelector: (selector) => selector === '.app-nav' ? nav : null,
  querySelectorAll: (selector) => selector === '.nav-tab[data-view]' ? tabs : [],
};
global.$ = (id) => document.getElementById(id);
global.location = { pathname: '/', search: '?keep=1', hash: '#workspace' };
global.history = {
  replaceState(_state, _title, next) {
    const parsed = new URL(String(next), 'http://127.0.0.1');
    location.pathname = parsed.pathname;
    location.search = parsed.search;
    location.hash = parsed.hash;
  },
};
const windowListeners = {};
global.window = {
  addEventListener(type, fn) { windowListeners[type] = fn; },
  matchMedia() { return { matches: false }; },
};
global.matchMedia = window.matchMedia;
function authenticatedAnimationFrame(fn) { fn(); return 1; }
function refreshUpdates() { updatesRuns += 1; }
function syncWatchPolling(name) {
  if (name) _currentView = name;
  pollCalls.push(_currentView);
}
"""
    harness += _view_navigation_source()
    harness += r"""
function snapshot(event) {
  return {
    prevented: event ? event.prevented : 0,
    selected: tabs.filter((tab) => tab.getAttribute('aria-selected') === 'true').map((tab) => tab.dataset.view),
    tabbable: tabs.filter((tab) => tab.tabIndex === 0).map((tab) => tab.dataset.view),
    visible: panels.filter((panel) => !panel.hidden).map((panel) => panel.id.replace(/^view-/, '')),
    focus: document.activeElement && document.activeElement.id,
    hash: location.hash,
    current: _currentView,
    poll: pollCalls[pollCalls.length - 1],
    guide_enters: guideEnters,
    updates_runs: updatesRuns,
  };
}
function keyEvent(key, currentTarget, target) {
  return {
    key, currentTarget, target, prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
}

const initial = snapshot(null);
appStarted = true;
activateView('watch', { focus: true });
guideEnters = 0;
updatesRuns = 0;
const sequence = [];
for (const key of ['ArrowRight', 'End', 'Home', 'ArrowLeft', 'ArrowRight']) {
  const event = keyEvent(key, nav, document.activeElement);
  nav.listeners.keydown(event);
  sequence.push(snapshot(event));
}

document.activeElement = outside;
activateView('auth');
const defaultFocus = snapshot(null);
document.activeElement = outside;
activateView('updates', { focus: true });
const explicitFocus = snapshot(null);
document.activeElement = outside;
activateView('not-a-view');
const invalid = snapshot(null);
document.activeElement = outside;
location.hash = '#guide';
windowListeners.hashchange();
const hashChange = snapshot(null);
document.activeElement = outside;
tabs[2].listeners.click();
const click = snapshot(null);

activateView('watch');
document.activeElement = element('pipe-filter');
const ignored = [];
for (const [key, currentTarget, target] of [
  ['ArrowRight', element('pipe-group'), document.activeElement],
  ['ArrowLeft', element('os-group'), element('os-button')],
  ['Tab', nav, tabs[1]],
  ['Enter', nav, tabs[1]],
  [' ', nav, tabs[1]],
]) {
  const event = keyEvent(key, currentTarget, target);
  handleTablistKeydown(event);
  ignored.push(event.prevented);
}
const ignoredState = snapshot(null);

console.log(JSON.stringify({
  initial, sequence, defaultFocus, explicitFocus, invalid, hashChange, click,
  ignored, ignoredState,
}));
"""
    got = _node_json(harness)
    assert got["initial"] == {
        "prevented": 0,
        "selected": ["workspace"],
        "tabbable": ["workspace"],
        "visible": ["workspace"],
        "focus": "outside",
        "hash": "#workspace",
        "current": "workspace",
        "poll": "workspace",
        "guide_enters": 0,
        "updates_runs": 0,
    }
    expected_views = ["auth", "guide", "workspace", "guide", "workspace"]
    expected_guide_enters = [0, 1, 1, 2, 2]
    for state, view, guide_enters in zip(
        got["sequence"], expected_views, expected_guide_enters, strict=True
    ):
        assert state["prevented"] == 1
        assert state["selected"] == [view]
        assert state["tabbable"] == [view]
        assert state["visible"] == [view]
        assert state["focus"] == f"nav-{view}"
        assert state["hash"] == f"#{view}"
        assert state["current"] == view and state["poll"] == view
        assert state["guide_enters"] == guide_enters

    assert got["defaultFocus"]["selected"] == ["auth"]
    assert got["defaultFocus"]["focus"] == "outside"
    assert got["explicitFocus"]["selected"] == ["updates"]
    assert got["explicitFocus"]["focus"] == "nav-updates"
    assert got["explicitFocus"]["updates_runs"] == 1
    assert got["invalid"]["selected"] == ["workspace"]
    assert got["invalid"]["tabbable"] == ["workspace"]
    assert got["invalid"]["visible"] == ["workspace"]
    assert got["invalid"]["focus"] == "outside"
    assert got["hashChange"]["selected"] == ["guide"]
    assert got["hashChange"]["focus"] == "outside"
    assert got["click"]["selected"] == ["auth"]
    assert got["click"]["focus"] == "nav-auth"
    assert got["ignored"] == [0, 0, 0, 0, 0]
    assert got["ignoredState"]["selected"] == ["watch"]
    assert got["ignoredState"]["focus"] == "pipe-filter"


def test_non_tab_pipeline_and_os_controls_are_native_pressed_button_groups():
    """Pipeline and OS choices are filters/actions, not page tabs. They keep native button keyboard
    behavior and must not create a second tablist."""
    _, pipe_group = MARKUP.by_id["pipe_filters"]
    assert pipe_group.get("role") == "group"
    assert pipe_group.get("aria-label")
    pipe_block = HTML.split('id="pipe_filters"', 1)[1].split("</div>", 1)[0]
    pipe_buttons = re.findall(r"<button\b[^>]*class=\"pipe-filter[^\"]*\"[^>]*>", pipe_block)
    assert len(pipe_buttons) == 4
    for button in pipe_buttons:
        assert 'type="button"' in button
        assert 'aria-pressed="' in button
        assert 'role="tab"' not in button and "aria-selected" not in button

    static = HTML.split('id="updates_os_bar"', 1)[1].split("</div>", 2)[0]
    dynamic = JS.split('id="updates_os_bar_live"', 1)[1].split(
        '<div class="upd-ffmpeg-grid"', 1
    )[0]
    for block in (static, dynamic):
        assert 'role="group"' in block
        assert 'aria-label="' in block
        assert 'role="tablist"' not in block and 'role="tab"' not in block
        assert "aria-selected" not in block
        buttons = re.findall(r"<button\b[^>]*class=\"upd-os-tab\"[^>]*>", block)
        assert len(buttons) == 2
        assert [re.search(r'data-os="([^"]+)"', button).group(1) for button in buttons] == [
            "Windows", "macOS",
        ]
        assert all('type="button"' in button and "aria-pressed" in button for button in buttons)


def test_non_tab_os_late_host_detection_rerenders_the_real_ffmpeg_panel():
    """The real applyHostOs -> renderUpdates path must refresh every OS-dependent surface.

    This catches the prior false green where direct syncUpdatesOsTabs calls changed aria-pressed
    while leaving the rendered heading, setup path, action, note, and live detection text stale.
    """
    harness = r"""
function classes(initial = []) {
  const values = new Set(initial);
  return {
    add(...names) { names.forEach((name) => values.add(name)); },
    remove(...names) { names.forEach((name) => values.delete(name)); },
    contains(name) { return values.has(name); },
    toggle(name, force) {
      if (force) values.add(name); else values.delete(name);
    },
  };
}
function element(id) {
  return {
    id, hidden: false, textContent: '', innerHTML: '', title: '',
    dataset: {}, attrs: {}, classList: classes(), offsetParent: {},
    disabled: false,
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return this.attrs[name] || null; },
    focus() { document.activeElement = this; },
  };
}
function osButton(id, os, visible, pressed) {
  const item = element(id);
  item.dataset.os = os;
  item.offsetParent = visible ? {} : null;
  item.classList = classes(['upd-os-tab']);
  item.setAttribute('aria-pressed', pressed ? 'true' : 'false');
  return item;
}
const ids = new Map();
const staticButtons = [
  osButton('static-windows', 'Windows', false, false),
  osButton('static-macos', 'macOS', false, false),
];
let dynamicButtons = [];
let dynamicAction = null;
const documentBody = element('document-body');
global.document = {
  body: documentBody,
  activeElement: documentBody,
  querySelectorAll(selector) {
    if (selector === '.upd-os-tab') return staticButtons.concat(dynamicButtons);
    if (selector === '.quit-hint' || selector === '.upd-act') return [];
    return [];
  },
};

let bodyWrites = 0;
const updatesBody = element('updates_body');
updatesBody._html = '';
Object.defineProperty(updatesBody, 'innerHTML', {
  get() { return this._html; },
  set(value) {
    bodyWrites += 1;
    if (dynamicButtons.includes(document.activeElement) || document.activeElement === dynamicAction) {
      document.activeElement = document.body;
    }
    this._html = String(value);
    dynamicButtons = [];
    dynamicAction = null;
    ids.delete('updates_os_detect_live');
    if (!this._html.includes('id="updates_os_bar_live"')) return;
    for (const os of ['Windows', 'macOS']) {
      const pressed = this._html.includes(`data-os="${os}" aria-pressed="true"`);
      dynamicButtons.push(osButton(`dynamic-${os}-${bodyWrites}`, os, true, pressed));
    }
    if (this._html.includes('data-system-tool="ffmpeg"')) {
      dynamicAction = element(`dynamic-action-${bodyWrites}`);
      dynamicAction.classList = classes(['upd-act']);
    }
    ids.set('updates_os_detect_live', element('updates_os_detect_live'));
  },
});
ids.set('updates_body', updatesBody);
for (const id of [
  'cookiefile_browse', 'probe_pop_reveal', 'host_os_badge', 'host_os_gauge',
  'updates_os_bar', 'updates_os_detect', 'updates_host_title', 'updates_host_sub',
  'updates_host_meta', 'updates_host_platform', 'updates_host_ffmpeg_loc',
]) {
  ids.set(id, element(id));
}
global.$ = (id) => ids.get(id) || null;

let HOST_OS = null;
let HOST_PLATFORM = null;
let HOST_MANAGED_LOCATIONS = null;
let _updatesReport = null;
let _updatesOsTab = null;
let _updatesRenderedPresentation = null;
let refreshRuns = 0;
function refreshUpdates() { refreshRuns += 1; }
function hostDeviceLabel() { return HOST_OS || 'computer'; }
function quitHint(host) { return `quit-${host || 'unknown'}`; }
function esc(value) { return String(value == null ? '' : value); }
function toolRowHtml() { throw new Error('unexpected optional row'); }
function applyUpdatesControlState() {}
function playUpdatesEnter() {}
"""
    for name in (
        "canTakeFocus",
        "hostOsFromReport",
        "effectiveUpdatesOs",
        "syncUpdatesOsTabs",
        "ffmpegSetupPresentation",
        "ffmpegActionForOs",
        "toolStatusLabel",
        "applyHostOs",
        "renderUpdates",
    ):
        harness += _function_source(name)
    harness += r"""
function reportWithoutHost() {
  return {
    host_os: null,
    host_platform: null,
    managed_locations: { Windows: 'C:/managed' },
    pip: [],
    info: [{
      name: 'ffmpeg', installed: null, latest: '7.1',
      path: 'C:/system/ffmpeg.exe', managed: false,
      updatable: true, outdated: true,
    }],
    summary: {},
    warnings: [],
  };
}
function reportForWindows(platform = 'win32', location = 'C:/managed') {
  const report = reportWithoutHost();
  report.host_os = 'Windows';
  report.host_platform = platform;
  report.managed_locations = { Windows: location };
  return report;
}
function actionSnapshot(html) {
  const actions = html.match(/<button type="button" class="[^"]*"[^>]*>[^<]*<\/button>/g) || [];
  const markup = actions.find(
    (item) => item.includes('upd-act-disabled') || item.includes('data-system-tool="ffmpeg"'),
  ) || '';
  const label = markup.match(/>([^<]*)<\/button>/);
  const title = markup.match(/ title="([^"]*)"/);
  return {
    enabled: markup.includes('data-system-tool="ffmpeg"'),
    disabled: /\sdisabled(?:\s|>)/.test(markup),
    label: label ? label[1] : null,
    title: title ? title[1] : null,
  };
}
function panelSnapshot() {
  const html = updatesBody.innerHTML;
  const heading = html.match(/<h4>(Managed install \(Windows\)|Manual setup \(macOS\))<\/h4>/);
  const setup = html.match(/<p>(Private folder|Setup): <code>([^<]*)<\/code><\/p>/);
  const note = setup
    ? html.slice(setup.index + setup[0].length).match(/<p class="upd-note"[^>]*>([^<]*)<\/p>/)
    : null;
  const live = ids.get('updates_os_detect_live');
  return {
    heading: heading ? heading[1] : null,
    locationLabel: setup ? setup[1] : null,
    location: setup ? setup[2] : null,
    note: note ? note[1] : null,
    action: actionSnapshot(html),
    pressed: dynamicButtons.map((item) => item.getAttribute('aria-pressed') === 'true'),
    live: live ? live.innerHTML : null,
    focusedOs: document.activeElement && document.activeElement.dataset.os || null,
  };
}
function reset(explicitSelection) {
  HOST_OS = null;
  HOST_PLATFORM = null;
  HOST_MANAGED_LOCATIONS = null;
  _updatesReport = null;
  _updatesOsTab = explicitSelection;
  _updatesRenderedPresentation = null;
  refreshRuns = 0;
  bodyWrites = 0;
  dynamicButtons = [];
  dynamicAction = null;
  updatesBody._html = '';
  ids.delete('updates_os_detect_live');
  document.activeElement = document.body;
}
function runLateDetection(explicitSelection, detectedHost) {
  reset(explicitSelection);
  renderUpdates(reportWithoutHost(), { animate: false });
  const focusedOs = explicitSelection || 'Windows';
  dynamicButtons.find((item) => item.dataset.os === focusedOs).focus();
  const before = panelSnapshot();
  applyHostOs({
    host_os: detectedHost,
    host_platform: detectedHost === 'macOS' ? 'darwin' : 'win32',
    managed_locations: { Windows: 'C:/managed' },
  });
  return {
    before,
    after: panelSnapshot(),
    explicit: _updatesOsTab,
    refreshRuns,
    bodyWrites,
  };
}
function runIdenticalWindowsHost() {
  reset(null);
  renderUpdates(reportForWindows(), { animate: false });
  dynamicAction.focus();
  const focusedBefore = document.activeElement;
  bodyWrites = 0;
  applyHostOs({
    host_os: 'Windows',
    host_platform: 'win32',
    managed_locations: { Windows: 'C:/managed' },
  });
  return {
    bodyWrites,
    preservedExactFocus: document.activeElement === focusedBefore,
    focusedId: document.activeElement && document.activeElement.id,
    refreshRuns,
  };
}
function runChangedWindowsPresentation({ platform = 'win32', location = 'C:/managed' }) {
  reset(null);
  renderUpdates(reportForWindows(), { animate: false });
  dynamicButtons.find((item) => item.dataset.os === 'Windows').focus();
  bodyWrites = 0;
  applyHostOs({
    host_os: 'Windows',
    host_platform: platform,
    managed_locations: { Windows: location },
  });
  return {
    bodyWrites,
    focusedOs: document.activeElement && document.activeElement.dataset.os || null,
    platform: ids.get('updates_host_platform').textContent,
    location: panelSnapshot().location,
    refreshRuns,
  };
}
console.log(JSON.stringify({
  implicitWindows: runLateDetection(null, 'Windows'),
  implicitMac: runLateDetection(null, 'macOS'),
  explicitMacOnWindows: runLateDetection('macOS', 'Windows'),
  identicalWindows: runIdenticalWindowsHost(),
  changedPlatform: runChangedWindowsPresentation({ platform: 'win32-arm64' }),
  changedLocation: runChangedWindowsPresentation({ location: 'D:/managed' }),
}));
"""
    got = _node_json(harness)

    pending_windows = {
        "heading": "Managed install (Windows)",
        "locationLabel": "Private folder",
        "location": "C:/managed",
        "note": (
            "Host detection is pending. Managed FFmpeg remains unavailable "
            "until Windows is confirmed."
        ),
        "action": {
            "enabled": False,
            "disabled": True,
            "label": "Unavailable on this host",
            "title": "Windows managed FFmpeg cannot be installed until the host is confirmed",
        },
        "pressed": [True, False],
        "live": "Host: <strong>detecting…</strong>",
        "focusedOs": "Windows",
    }
    assert got["implicitWindows"] == {
        "before": pending_windows,
        "after": {
            "heading": "Managed install (Windows)",
            "locationLabel": "Private folder",
            "location": "C:/managed",
            "note": "This matches your host. The button installs only into the private folder.",
            "action": {
                "enabled": True,
                "disabled": False,
                "label": "Install for Windows",
                "title": (
                    "Install private managed FFmpeg for Windows "
                    "(does not replace system FFmpeg)"
                ),
            },
            "pressed": [True, False],
            "live": "You are on <strong>Windows</strong> · install enabled",
            "focusedOs": "Windows",
        },
        "explicit": None,
        "refreshRuns": 0,
        "bodyWrites": 2,
    }
    assert got["implicitMac"] == {
        "before": pending_windows,
        "after": {
            "heading": "Manual setup (macOS)",
            "locationLabel": "Setup",
            "location": "Manual / system installation",
            "note": (
                "This matches your host. Managed FFmpeg is Windows-only; "
                "use a manual/system installation."
            ),
            "action": {
                "enabled": False,
                "disabled": True,
                "label": "Manual FFmpeg",
                "title": "macOS setup is manual-only; managed FFmpeg installs only for Windows",
            },
            "pressed": [False, True],
            "live": "You are on <strong>macOS</strong> · manual/Homebrew only",
            "focusedOs": "Windows",
        },
        "explicit": None,
        "refreshRuns": 0,
        "bodyWrites": 2,
    }
    assert got["explicitMacOnWindows"] == {
        "before": {
            "heading": "Manual setup (macOS)",
            "locationLabel": "Setup",
            "location": "Manual / system installation",
            "note": "Host detection is pending. macOS setup remains manual.",
            "action": {
                "enabled": False,
                "disabled": True,
                "label": "Manual FFmpeg",
                "title": "macOS setup is manual-only; managed FFmpeg installs only for Windows",
            },
            "pressed": [False, True],
            "live": "Host: <strong>detecting…</strong>",
            "focusedOs": "macOS",
        },
        "after": {
            "heading": "Manual setup (macOS)",
            "locationLabel": "Setup",
            "location": "Manual / system installation",
            "note": (
                "You are on Windows. This macOS preview is manual-only; "
                "managed FFmpeg installs only for the Windows target."
            ),
            "action": {
                "enabled": False,
                "disabled": True,
                "label": "Manual FFmpeg",
                "title": "macOS setup is manual-only; managed FFmpeg installs only for Windows",
            },
            "pressed": [False, True],
            "live": "You are on <strong>Windows</strong> · install enabled",
            "focusedOs": "macOS",
        },
        "explicit": "macOS",
        "refreshRuns": 0,
        "bodyWrites": 2,
    }
    assert got["identicalWindows"] == {
        "bodyWrites": 0,
        "preservedExactFocus": True,
        "focusedId": "dynamic-action-1",
        "refreshRuns": 0,
    }
    assert got["changedPlatform"] == {
        "bodyWrites": 1,
        "focusedOs": "Windows",
        "platform": "win32-arm64",
        "location": "C:/managed",
        "refreshRuns": 0,
    }
    assert got["changedLocation"] == {
        "bodyWrites": 1,
        "focusedOs": "Windows",
        "platform": "win32",
        "location": "D:/managed",
        "refreshRuns": 0,
    }


def test_non_tab_dynamic_os_rerender_restores_focus_to_replacement_button():
    """Clicking a dynamic selector replaces updates_body.innerHTML. Focus must move to the new
    matching native button rather than falling back to body."""
    harness = r"""
function classes() {
  const values = new Set();
  return {
    toggle(name, force) {
      if (force) values.add(name); else values.delete(name);
    },
  };
}
let buttons = [];
function button(id, os, visible) {
  return {
    id, dataset: { os }, attrs: {}, classList: classes(),
    offsetParent: visible ? {} : null, disabled: false,
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return this.attrs[name] || null; },
    focus() { document.activeElement = this; },
  };
}
const staticButtons = [
  button('static-windows', 'Windows', false),
  button('static-macos', 'macOS', false),
];
buttons = staticButtons.concat([
  button('dynamic-windows-old', 'Windows', true),
  button('dynamic-macos-old', 'macOS', true),
]);
const ids = new Map();
for (const id of [
  'updates_os_bar', 'updates_os_detect', 'updates_host_title', 'updates_host_sub',
  'updates_host_meta', 'updates_host_platform', 'updates_host_ffmpeg_loc',
]) {
  ids.set(id, {
    id, hidden: false, textContent: '', innerHTML: '',
    classList: classes(), setAttribute() {},
  });
}
global.document = {
  activeElement: buttons[3],
  querySelectorAll(selector) { return selector === '.upd-os-tab' ? buttons : []; },
};
global.$ = (id) => ids.get(id) || null;
let _updatesOsTab = 'macOS';
let _updatesReport = { host_os: 'macOS', managed_locations: {} };
let HOST_OS = 'macOS';
let HOST_PLATFORM = 'darwin';
let HOST_MANAGED_LOCATIONS = {};
let renders = 0;
function hostOsFromReport(report) { return report && report.host_os || HOST_OS || null; }
function esc(value) { return String(value == null ? '' : value); }
function canTakeFocus(element) {
  return !!element && !element.disabled && element.offsetParent !== null;
}
"""
    harness += _function_source("effectiveUpdatesOs")
    harness += _function_source("syncUpdatesOsTabs")
    harness += r"""
function renderUpdates(report) {
  renders += 1;
  buttons = staticButtons.concat([
    button('dynamic-windows-new', 'Windows', true),
    button('dynamic-macos-new', 'macOS', true),
  ]);
  syncUpdatesOsTabs(report);
}
"""
    harness += _function_source("selectUpdatesOs")
    harness += r"""
selectUpdatesOs('Windows', { focus: true });
const afterValid = {
  selected: _updatesOsTab,
  focused: document.activeElement && document.activeElement.id,
  renders,
  pressed: buttons.map((item) => item.getAttribute('aria-pressed') === 'true'),
};
selectUpdatesOs('Plan9', { focus: true });
console.log(JSON.stringify({
  afterValid,
  afterInvalid: {
    selected: _updatesOsTab,
    focused: document.activeElement && document.activeElement.id,
    renders,
  },
}));
"""
    got = _node_json(harness)
    assert got["afterValid"] == {
        "selected": "Windows",
        "focused": "dynamic-windows-new",
        "renders": 1,
        "pressed": [True, False, True, False],
    }
    assert got["afterInvalid"] == {
        "selected": "Windows",
        "focused": "dynamic-windows-new",
        "renders": 1,
    }


def test_main_tab_and_non_tab_focus_rings_follow_active_state_and_motion_preferences():
    """Active-state rules must not paint over keyboard focus, and the new controls retain the
    project's reduced-motion contract."""
    selectors = [
        (".nav-tab.is-active{", ".nav-tab:focus-visible{"),
        (".pipe-filter.is-active{", ".pipe-filter:focus-visible{"),
        ('.upd-os-tab[aria-pressed="true"]{', ".upd-os-tab:focus-visible{"),
    ]
    for active, focused in selectors:
        assert active in CSS and focused in CSS
        assert CSS.index(active) < CSS.index(focused), f"{focused} must follow {active}"
        block = CSS.split(focused, 1)[1].split("}", 1)[0]
        compact_block = block.replace(" ", "")
        assert "outline:none" not in compact_block
        assert "outline:" in compact_block
    assert "@media (prefers-reduced-motion: reduce)" in CSS
