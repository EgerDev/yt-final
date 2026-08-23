"""The web console must actually BOOT. Nothing else in the suite checks this.

Earlier today a one-line change shipped a completely dead page: activateView() runs at module level to
apply the #hash, it called a helper that read three `let` bindings declared ~3800 lines further down,
and that hit the temporal dead zone. The ReferenceError aborted the whole script, so every later
declaration stayed uninitialized and the console rendered nothing at all.

What did NOT catch it:
  * `node --check` — the file is perfectly valid syntax; TDZ is a runtime error.
  * every source-string test in this suite — all the tokens they look for were present.
  * the full pytest run — green.
Only loading the page in a real browser caught it, and only by accident.

So: execute app.js's whole module body in node against a DOM stub built from the real index.html ids.
Any throw during boot fails here. This is deliberately a smoke test — it asserts the script survives
and wires listeners, not what any particular handler does; the behavioural truth tables live in
test_workspace_truth/parity/a11y/perf.py.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WEBUI = HERE / "webui"

# Browser globals app.js legitimately uses that node does not provide. Stubbed rather than removed:
# the point is to run the REAL module body, so a missing stub must look like a harness gap (fix it
# here), never like a bug in app.js.
HARNESS = r"""
const fs = require('fs');
const HTML = fs.readFileSync(process.argv[2], 'utf8');
const SRC = fs.readFileSync(process.argv[3], 'utf8');
const QUERY = process.argv[4] || '';
const HASH = process.argv[5] || '';
const ACTION = process.argv[6] || '';

const ids = new Set();
for (const m of HTML.matchAll(/\bid=["']([^"']+)["']/g)) ids.add(m[1]);

const listeners = [];
const elementEvents = [];
function mkEl(id) {
  return {
    id, tagName: 'DIV', type: 'text', value: '', textContent: '', innerHTML: '',
    checked: false, hidden: false, disabled: false, className: '', title: '', open: false,
    options: [], selectedOptions: [{ text: '' }], dataset: {}, style: {}, files: [],
    children: [], parentNode: null, offsetWidth: 100,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(t, f) { listeners.push([id, t, f]); },
    removeEventListener() {}, appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, insertBefore(c) { return c; }, querySelector: () => null,
    querySelectorAll: () => [], closest: () => null,
    focus() { elementEvents.push({ type: 'focus', id }); }, blur() {}, click() {},
    scrollIntoView(options) { elementEvents.push({ type: 'scrollIntoView', id, options }); },
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    hasAttribute: () => false, dispatchEvent: () => true, remove() {}, insertAdjacentHTML() {},
    cloneNode() { return mkEl(id); },
    getBoundingClientRect: () => ({ x: 0, y: 0, width: 0, height: 0, top: 0, left: 0 }),
  };
}
const store = new Map();
const getEl = (id) => { if (!store.has(id)) store.set(id, mkEl(id)); return store.get(id); };
const storageOps = [];
const fetchCalls = [];
const replacedUrls = [];
const fakeLocation = {
  href: 'http://127.0.0.1:8765/' + QUERY + HASH,
  hash: HASH,
  origin: 'http://127.0.0.1:8765',
  search: QUERY,
  pathname: '/',
  reload() {},
  replace() {},
};
const fakeHistory = {
  replaceState(_state, _title, next) {
    replacedUrls.push(String(next));
    const parsed = new URL(String(next), fakeLocation.origin);
    fakeLocation.href = parsed.href;
    fakeLocation.pathname = parsed.pathname;
    fakeLocation.search = parsed.search;
    fakeLocation.hash = parsed.hash;
  },
};
const fakeStorage = {
  getItem: () => null,
  setItem(key, value) { storageOps.push(['set', String(key), String(value)]); },
  removeItem(key) { storageOps.push(['remove', String(key)]); },
};

global.document = {
  readyState: 'complete',
  getElementById: (id) => (ids.has(id) ? getEl(id) : null),
  querySelector: () => null, querySelectorAll: () => [],
  createElement: (t) => mkEl('created:' + t), createDocumentFragment: () => mkEl('frag'),
  addEventListener(t, f) { listeners.push(['document', t, f]); }, removeEventListener() {},
  body: mkEl('body'), documentElement: mkEl('html'), hidden: false, title: '',
};
global.window = {
  addEventListener(t, f) { listeners.push(['window', t, f]); }, removeEventListener() {},
  location: fakeLocation,
  history: fakeHistory,
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  navigator: { userAgent: 'node', platform: 'Win32', clipboard: { writeText: () => Promise.resolve() } },
  localStorage: fakeStorage,
  setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
  fetch: (path, options = {}) => {
    fetchCalls.push({ path: String(path), headers: options.headers || {} });
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}),
                             text: () => Promise.resolve('') });
  },
  scrollTo() {}, requestAnimationFrame: (f) => f(), alert() {}, confirm: () => false, prompt: () => null,
  EventSource: function () { this.close = () => {}; this.addEventListener = () => {}; },
};
global.localStorage = window.localStorage;
global.navigator = window.navigator;
global.location = window.location;
global.history = window.history;
global.fetch = window.fetch;
global.alert = window.alert;
global.confirm = window.confirm;
global.EventSource = window.EventSource;
global.MutationObserver = class { observe() {} disconnect() {} takeRecords() { return []; } };
global.IntersectionObserver = global.MutationObserver;
global.ResizeObserver = global.MutationObserver;
global.AbortController = class { constructor() { this.signal = {}; } abort() {} };
global.FormData = class { append() {} };
global.Event = class { constructor(t) { this.type = t; } };
global.CustomEvent = global.Event;
global.self = global;

try {
  // Indirect eval so the body runs in GLOBAL scope exactly like a <script> tag: `let`/`const`
  // hoisting and the temporal dead zone then behave the same way they do in the browser. Wrapping
  // it in a function or module scope would change that and defeat the whole test.
  (0, eval)(SRC);
  if (ACTION === 'activate-guide') {
    global.activateView('guide', { focus: true, syncHash: false });
  }
  console.log(JSON.stringify({
    ok: true,
    listeners: listeners.length,
    elementEvents,
    storageOps,
    fetchCalls,
    replacedUrls,
    location: {
      pathname: fakeLocation.pathname,
      search: fakeLocation.search,
      hash: fakeLocation.hash,
    },
  }));
  process.exit(0);
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: e.name + ': ' + e.message,
                              stack: String(e.stack).split('\n').slice(0, 5) }));
  process.exit(1);
}
"""


def test_app_js_module_body_survives_boot(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI")
    h = tmp_path / "boot.js"
    h.write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(h), str(WEBUI / "index.html"), str(WEBUI / "app.js")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=60)
    out = (r.stdout or "").strip().splitlines()
    got = json.loads(out[-1]) if out else {"ok": False, "error": "no output: " + (r.stderr or "")[-300:]}
    assert got.get("ok"), (
        "app.js threw during boot — the shipped page would be DEAD (blank console, no polling).\n"
        + str(got.get("error")) + "\n" + "\n".join(got.get("stack") or [])
        + "\n\nIf this is a browser global node lacks, add a stub to HARNESS in this file."
    )
    # A boot that binds nothing means the stub silently swallowed the wiring and this test would
    # pass for a page that does nothing. 40 is well under the real count (61 at the time of writing)
    # and well above zero.
    assert got.get("listeners", 0) > 40, got


def test_the_boot_harness_actually_detects_a_dead_page(tmp_path):
    """A smoke test that cannot fail is worse than no test. Inject the exact bug class that shipped a
    dead page — a module-level read of a `let` before its declaration — and require the harness to
    catch it. Without this, a harness that quietly swallowed every error would look like a pass."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI")
    src = (WEBUI / "app.js").read_text(encoding="utf-8")
    assert "let jobsTimer" in src, "anchor moved; keep this test honest by updating it"
    broken = src.replace("let jobsTimer", "void _tdzCanary;\nlet _tdzCanary = 1;\nlet jobsTimer", 1)
    assert broken != src

    h = tmp_path / "boot.js"
    h.write_text(HARNESS, encoding="utf-8")
    bad = tmp_path / "app.broken.js"
    bad.write_text(broken, encoding="utf-8")
    r = subprocess.run([node, str(h), str(WEBUI / "index.html"), str(bad)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=60)
    assert r.returncode != 0, "the harness is blind to a module-level TDZ — it proves nothing"
    got = json.loads((r.stdout or "").strip().splitlines()[-1])
    assert got["ok"] is False and "before initialization" in got["error"], got


def test_keyboard_tab_activation_scrolls_the_focused_tab_into_view(tmp_path):
    """A horizontally scrolling phone tablist must reveal the tab selected by End/arrow keys."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI")
    h = tmp_path / "boot.js"
    h.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        [
            node,
            str(h),
            str(WEBUI / "index.html"),
            str(WEBUI / "app.js"),
            "",
            "",
            "activate-guide",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    output = (result.stdout or "").strip().splitlines()
    got = json.loads(output[-1]) if output else {
        "ok": False,
        "error": "no output: " + (result.stderr or "")[-300:],
    }
    assert got.get("ok"), got
    assert got["elementEvents"][-2:] == [
        {"type": "focus", "id": "nav-guide"},
        {
            "type": "scrollIntoView",
            "id": "nav-guide",
            "options": {"block": "nearest", "inline": "nearest"},
        },
    ]


def test_query_token_is_stripped_without_persistence_or_request_header(tmp_path):
    """An old bookmarked ?token must be discarded, never promoted into browser credentials."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the UI")
    h = tmp_path / "boot.js"
    h.write_text(HARNESS, encoding="utf-8")
    query_secret = "legacy-query-secret"
    r = subprocess.run(
        [
            node,
            str(h),
            str(WEBUI / "index.html"),
            str(WEBUI / "app.js"),
            f"?keep=1&token={query_secret}",
            "#watch",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    out = (r.stdout or "").strip().splitlines()
    got = json.loads(out[-1]) if out else {"ok": False, "error": (r.stderr or "")[-300:]}
    assert got.get("ok"), got
    assert query_secret not in json.dumps(got["storageOps"])
    assert ["remove", "yt_final_web_token"] in got["storageOps"]
    request_headers = [call.get("headers", {}) for call in got["fetchCalls"]]
    assert query_secret not in json.dumps(request_headers)
    assert got["location"] == {"pathname": "/", "search": "?keep=1", "hash": "#watch"}
    assert "/?keep=1#watch" in got["replacedUrls"]
