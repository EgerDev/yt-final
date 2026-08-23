"""Executable browser-session contract tests for the shipped vanilla-JS console."""
from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WEBUI = HERE / "webui"
SESSION_KEY = "yt_final_web_session_v2"
LEGACY_KEY = "yt_final_web_token"
FUTURE = 9_999_999_999


HARNESS = r"""
const fs = require('fs');
const HTML = fs.readFileSync(process.argv[2], 'utf8');
const SRC = fs.readFileSync(process.argv[3], 'utf8');
const SCENARIO = JSON.parse(process.argv[4]);
if (Number.isFinite(SCENARIO.now_seconds)) {
  Date.now = () => SCENARIO.now_seconds * 1000;
}

const ids = new Set();
for (const m of HTML.matchAll(/\bid=["']([^"']+)["']/g)) ids.add(m[1]);
function parseLoginMessageMarkup() {
  const match = HTML.match(
    /<([a-z][\w:-]*)\b([^>]*\bid=["']login-msg["'][^>]*)>([\s\S]*?)<\/\1>/i,
  );
  if (!match) throw new Error('missing #login-msg markup');
  const attrs = new Map();
  for (const attr of match[2].matchAll(/([:\w-]+)\s*=\s*(["'])(.*?)\2/g)) {
    attrs.set(attr[1], attr[3]);
  }
  return {
    tagName: match[1].toUpperCase(),
    attrs: [...attrs],
    textContent: match[3].replace(/<[^>]*>/g, '').trim(),
  };
}
const LOGIN_MSG_MARKUP = parseLoginMessageMarkup();
const listeners = [];
const elements = new Map();
function mkClassList(initial = []) {
  const names = new Set(initial);
  return {
    add(...xs) { xs.forEach((x) => names.add(x)); },
    remove(...xs) { xs.forEach((x) => names.delete(x)); },
    toggle(x, force) {
      if (force === true) { names.add(x); return true; }
      if (force === false) { names.delete(x); return false; }
      if (names.has(x)) { names.delete(x); return false; }
      names.add(x); return true;
    },
    contains(x) { return names.has(x); },
    snapshot() { return [...names].sort(); },
  };
}
function mkEl(id) {
  const isLoginMessage = id === 'login-msg';
  const attrs = new Map(isLoginMessage ? LOGIN_MSG_MARKUP.attrs : []);
  const initialClasses = String(attrs.get('class') || '').split(/\s+/).filter(Boolean);
  const el = {
    id, tagName: isLoginMessage ? LOGIN_MSG_MARKUP.tagName : 'DIV',
    type: 'text', value: '',
    textContent: isLoginMessage ? LOGIN_MSG_MARKUP.textContent : '',
    innerHTML: '',
    checked: false, hidden: false, disabled: false,
    className: isLoginMessage ? String(attrs.get('class') || '') : '',
    title: '', open: false,
    options: [], selectedOptions: [{ text: '' }], dataset: {}, style: {}, files: [],
    children: [], parentNode: null, offsetWidth: 100, onclick: null,
    classList: mkClassList(initialClasses),
    addEventListener(type, fn) { listeners.push({ id, type, fn }); },
    removeEventListener() {}, appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, insertBefore(c) { return c; }, querySelector: () => null,
    querySelectorAll: () => [], closest: () => null,
    focus() { events.push('FOCUS ' + id); }, blur() {}, click() {},
    scrollIntoView() {},
    setAttribute(name, value) {
      if (isLoginMessage) attrs.set(String(name), String(value));
    },
    getAttribute(name) {
      name = String(name);
      return isLoginMessage && attrs.has(name) ? attrs.get(name) : null;
    },
    removeAttribute(name) {
      if (isLoginMessage) attrs.delete(String(name));
    },
    hasAttribute(name) {
      return isLoginMessage && attrs.has(String(name));
    },
    dispatchEvent: () => true, remove() {}, insertAdjacentHTML() {},
    cloneNode() { return mkEl(id); },
    getBoundingClientRect: () => ({ x: 0, y: 0, width: 0, height: 0, top: 0, left: 0 }),
  };
  return el;
}
const getEl = (id) => {
  if (!elements.has(id)) elements.set(id, mkEl(id));
  return elements.get(id);
};

const events = [];
let terminalStates = null;
let sessionStates = null;
let loginStates = null;
const storageOps = [];
const storage = new Map(Object.entries(SCENARIO.storage || {}));
const localStorage = {
  getItem(key) { return storage.has(String(key)) ? storage.get(String(key)) : null; },
  setItem(key, value) {
    key = String(key); value = String(value);
    if (SCENARIO.fail_session_storage_write && key === 'yt_final_web_session_v2') {
      storageOps.push(['set-error', key]);
      throw new DOMException('session storage quota unavailable', 'QuotaExceededError');
    }
    storage.set(key, value);
    storageOps.push(['set', key, value]);
  },
  removeItem(key) {
    key = String(key);
    storage.delete(key);
    storageOps.push(['remove', key]);
    if (key === 'yt_final_web_session_v2') events.push('clearSession');
  },
};

const fakeLocation = {
  href: 'http://127.0.0.1:8765/' + (SCENARIO.query || '') + (SCENARIO.hash || ''),
  hash: SCENARIO.hash || '',
  origin: 'http://127.0.0.1:8765',
  search: SCENARIO.query || '',
  pathname: '/',
  reload() {}, replace() {},
};
const replacedUrls = [];
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

const responseQueues = new Map(
  Object.entries(SCENARIO.responses || {}).map(([path, value]) => [
    path, Array.isArray(value) ? value.slice() : [value],
  ]),
);
const fetchCalls = [];
const bodyReads = [];
const deferred = new Map();
const readers = [];
const fileReads = [];
const callbackErrors = [];
function defaultBody(path) {
  if (path === '/api/profiles' || path === '/api/jobs') return [];
  if (path === '/api/watches') return { watches: [] };
  if (path === '/api/gpu') return { gpu_name: 'CPU', working_encoders: [] };
  return {};
}
function makeResponse(path, spec) {
  const status = Number(spec.status == null ? 200 : spec.status);
  let body = spec.body === undefined ? defaultBody(path) : spec.body;
  if (spec.expiry_kind) {
    body = Object.assign({}, body);
    if (spec.expiry_kind === 'nan') body.expires_at = Number.NaN;
    else if (spec.expiry_kind === 'positive_infinity') body.expires_at = Number.POSITIVE_INFINITY;
    else if (spec.expiry_kind === 'negative_infinity') body.expires_at = Number.NEGATIVE_INFINITY;
    else throw new Error('unknown expiry_kind: ' + spec.expiry_kind);
  }
  const headers = Object.assign({ 'content-type': 'application/json' }, spec.headers || {});
  const bodyValue = (kind) => {
    bodyReads.push({ path, kind });
    if (spec.reject_body) return Promise.reject(new Error(String(spec.reject_body)));
    const value = kind === 'text'
      ? (typeof body === 'string' ? body : JSON.stringify(body))
      : body;
    if (!spec.defer_body) return Promise.resolve(value);
    return new Promise((resolve) => {
      deferred.set(String(spec.defer_body), {
        path, spec, resolve: () => resolve(value),
      });
    });
  };
  return {
    ok: spec.ok == null ? status >= 200 && status < 300 : !!spec.ok,
    status,
    statusText: spec.statusText || '',
    headers: {
      get(name) {
        const target = String(name).toLowerCase();
        const key = Object.keys(headers).find((k) => k.toLowerCase() === target);
        return key ? headers[key] : null;
      },
    },
    json: () => bodyValue('json'),
    text: () => bodyValue('text'),
  };
}
function fakeFetch(path, options = {}) {
  path = String(path);
  const method = String(options.method || 'GET').toUpperCase();
  events.push(method + ' ' + path);
  const headers = options.headers instanceof Headers
    ? Object.fromEntries(options.headers.entries())
    : Object.assign({}, options.headers || {});
  const call = { path, method, headers, aborted: !!(options.signal && options.signal.aborted) };
  fetchCalls.push(call);
  const queue = responseQueues.get(path);
  const spec = queue && queue.length ? queue.shift() : {};
  if (options.signal) {
    options.signal.addEventListener('abort', () => { call.aborted = true; }, { once: true });
  }
  if (spec && spec.reject) return Promise.reject(new Error(spec.reject));
  if (spec && spec.defer) {
    return new Promise((resolve, reject) => {
      const item = {
        path, spec, resolve: () => resolve(makeResponse(path, spec)), reject,
      };
      deferred.set(String(spec.defer), item);
      if (options.signal && !spec.ignore_abort) {
        const abort = () => reject(new DOMException('aborted', 'AbortError'));
        if (options.signal.aborted) abort();
        else options.signal.addEventListener('abort', abort, { once: true });
      }
    });
  }
  return Promise.resolve(makeResponse(path, spec || {}));
}
function releaseDeferred(name) {
  const item = deferred.get(String(name));
  if (!item) throw new Error('missing deferred response: ' + name);
  deferred.delete(String(name));
  item.resolve();
}
function rejectDeferred(name, message) {
  const item = deferred.get(String(name));
  if (!item) throw new Error('missing deferred response: ' + name);
  if (typeof item.reject !== 'function') throw new Error('deferred item cannot reject: ' + name);
  deferred.delete(String(name));
  item.reject(new Error(String(message || 'deferred rejection')));
}
function releaseAllDeferred() {
  for (const name of Array.from(deferred.keys())) releaseDeferred(name);
}

let nextTimer = 1;
let nextFrame = 1;
const activeTimeouts = new Map();
const activeIntervals = new Map();
const activeFrames = new Map();
function fakeSetTimeout(fn, delay) {
  const id = nextTimer++;
  activeTimeouts.set(id, { fn, delay: Number(delay || 0) });
  return id;
}
function fakeClearTimeout(id) { activeTimeouts.delete(id); }
function fakeSetInterval(fn, delay) {
  const id = nextTimer++;
  activeIntervals.set(id, { fn, delay: Number(delay || 0) });
  return id;
}
function fakeClearInterval(id) { activeIntervals.delete(id); }
function fakeRequestAnimationFrame(fn) {
  const id = nextFrame++;
  activeFrames.set(id, fn);
  return id;
}
function fakeCancelAnimationFrame(id) { activeFrames.delete(id); }
function takePendingFrames() {
  const callbacks = Array.from(activeFrames.values());
  activeFrames.clear();
  return callbacks;
}
async function runPendingTimeouts(delay = null) {
  const pending = Array.from(activeTimeouts.entries())
    .filter(([, timer]) => delay == null || timer.delay === delay);
  for (const [, timer] of pending) {
    for (const [id, candidate] of activeTimeouts.entries()) {
      if (candidate === timer) activeTimeouts.delete(id);
    }
    if (typeof timer.fn === 'function') await timer.fn();
  }
}

global.document = {
  readyState: 'complete',
  getElementById: (id) => (ids.has(id) ? getEl(id) : null),
  querySelector: () => null,
  querySelectorAll: (selector) => selector === '.upd-act'
    ? Array.from(elements.values()).filter((el) => String(el.className || '').split(/\s+/).includes('upd-act'))
    : [],
  createElement: (t) => mkEl('created:' + t), createDocumentFragment: () => mkEl('frag'),
  addEventListener(type, fn) { listeners.push({ id: 'document', type, fn }); },
  removeEventListener() {},
  body: mkEl('body'), documentElement: mkEl('html'), hidden: false, title: '',
};
global.window = {
  addEventListener(type, fn) { listeners.push({ id: 'window', type, fn }); },
  removeEventListener() {},
  location: fakeLocation, history: fakeHistory,
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  navigator: { userAgent: 'node', platform: 'Win32', clipboard: { writeText: () => Promise.resolve() } },
  localStorage, setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout,
  setInterval: fakeSetInterval, clearInterval: fakeClearInterval, fetch: fakeFetch,
  scrollTo() {}, requestAnimationFrame: fakeRequestAnimationFrame,
  cancelAnimationFrame: fakeCancelAnimationFrame,
  alert() {}, confirm: () => false, prompt: () => null,
  EventSource: function () { this.close = () => {}; this.addEventListener = () => {}; },
};
global.localStorage = localStorage;
global.navigator = window.navigator;
global.location = fakeLocation;
global.history = fakeHistory;
global.fetch = fakeFetch;
global.alert = window.alert;
global.confirm = window.confirm;
global.EventSource = window.EventSource;
global.setTimeout = fakeSetTimeout;
global.clearTimeout = fakeClearTimeout;
global.setInterval = fakeSetInterval;
global.clearInterval = fakeClearInterval;
global.requestAnimationFrame = fakeRequestAnimationFrame;
global.cancelAnimationFrame = fakeCancelAnimationFrame;
global.MutationObserver = class { observe() {} disconnect() {} takeRecords() { return []; } };
global.IntersectionObserver = global.MutationObserver;
global.ResizeObserver = global.MutationObserver;
global.FormData = class { append() {} };
global.Event = class { constructor(type) { this.type = type; } };
global.CustomEvent = global.Event;
global.FileReader = class {
  constructor() {
    this.result = null;
    this.onload = null;
    this.onerror = null;
    this.aborted = false;
    readers.push(this);
  }
  readAsText(file) {
    this.file = file;
    fileReads.push({ reader: 'FileReader', name: file && file.name });
  }
  abort() { this.aborted = true; }
};
global.self = global;

const flush = () => new Promise((resolve) => setImmediate(resolve));
async function captureCallback(callback) {
  try {
    await callback();
  } catch (e) {
    callbackErrors.push((e && e.name ? e.name : 'Error') + ': ' + (e && e.message ? e.message : e));
  }
}
async function runFrameCallbacks(callbacks) {
  callbacks.forEach((fn) => fn(16));
  await flush(); await flush();
}
async function runPendingFrames() {
  await runFrameCallbacks(takePendingFrames());
}
function beginLogin(password = SCENARIO.password || 'password') {
  getEl('login-password').value = password;
  const submit = listeners.find((x) => x.id === 'login-form' && x.type === 'submit');
  return submit.fn({ preventDefault() {} });
}
async function submitLogin(password = SCENARIO.password || 'password') {
  await beginLogin(password);
  await flush(); await flush();
}
(async () => {
  let actionError = null;
  try {
    if (SCENARIO.timeout_mode === 'manual') {
      Object.defineProperty(AbortSignal, 'timeout', {
        configurable: true, writable: true, value: undefined,
      });
    } else if (SCENARIO.timeout_mode === 'fake-native') {
      Object.defineProperty(AbortSignal, 'timeout', {
        configurable: true,
        writable: true,
        value: (delay) => {
          const controller = new AbortController();
          fakeSetTimeout(() => controller.abort(), delay);
          return controller.signal;
        },
      });
    }
    (0, eval)(SRC);
    await flush(); await flush();
    if (SCENARIO.action === 'login') {
      await submitLogin();
    } else if (SCENARIO.action === 'overlapping_login') {
      const older = beginLogin('older-password');
      await flush(); await flush();
      const newer = beginLogin('newer-password');
      await flush(); await flush();
      releaseDeferred('login-newer');
      await newer;
      await flush(); await flush();
      events.push('NEWER_LOGIN_DONE');
      releaseDeferred('login-older');
      await older;
      await flush(); await flush();
      events.push('OLDER_LOGIN_DONE');
    } else if (SCENARIO.action === 'overlapping_login_revoke_timeout') {
      const older = beginLogin('older-password');
      await flush(); await flush();
      const newer = beginLogin('newer-password');
      await flush(); await flush();
      releaseDeferred('login-newer');
      await newer;
      await flush(); await flush();
      releaseDeferred('login-older');
      await flush(); await flush();
      loginStates = {
        while_revoke_pending_disabled: getEl('login-submit').disabled,
      };
      await runPendingTimeouts(5_000);
      await older;
      await flush(); await flush();
    } else if (SCENARIO.action === 'login_body_timeout') {
      const login = beginLogin();
      await flush(); await flush();
      loginStates = {
        while_body_pending_disabled: getEl('login-submit').disabled,
        while_body_pending_feedback: getEl('login-msg').textContent,
      };
      await runPendingTimeouts(30_000);
      await flush(); await flush();
      loginStates.after_deadline_disabled = getEl('login-submit').disabled;
      loginStates.after_deadline_feedback = getEl('login-msg').textContent;
      releaseDeferred('login-body');
      await login;
      await flush(); await flush();
    } else if (SCENARIO.action === 'pending_login_logout') {
      const login = beginLogin();
      await flush(); await flush();
      loginStates = {
        while_login_pending_disabled: getEl('login-submit').disabled,
      };
      const logout = getEl('logout').onclick();
      await flush(); await flush();
      loginStates.after_logout_started_disabled = getEl('login-submit').disabled;
      loginStates.login_aborted_after_logout_started = fetchCalls
        .filter((call) => call.path === '/api/login').at(-1).aborted;
      if (SCENARIO.login_completion_timing === 'during') {
        releaseDeferred('login-a');
        await login;
        await flush(); await flush();
        loginStates.after_login_completed_during_logout = {
          disabled: getEl('login-submit').disabled,
          stored: storage.has('yt_final_web_session_v2'),
        };
        releaseDeferred('logout-a');
        await logout;
      } else {
        await logout;
        loginStates.after_logout_completed = {
          disabled: getEl('login-submit').disabled,
          stored: storage.has('yt_final_web_session_v2'),
        };
        releaseDeferred('login-a');
        await login;
      }
      await flush(); await flush();
      await runPendingTimeouts(50);
    } else if (SCENARIO.action === 'pending_login_session_lost') {
      const login = beginLogin();
      await flush(); await flush();
      loginStates = {
        while_login_pending_disabled: getEl('login-submit').disabled,
      };
      sessionLost('Forced session teardown', 'alert');
      await flush(); await flush();
      loginStates.after_session_lost_disabled = getEl('login-submit').disabled;
      loginStates.login_aborted_after_session_lost = fetchCalls
        .filter((call) => call.path === '/api/login').at(-1).aborted;
      releaseDeferred('login-a');
      await login;
      await flush(); await flush();
      await runPendingTimeouts(50);
    } else if (SCENARIO.action === 'restore_while_login_pending') {
      const login = beginLogin('pending-password');
      await flush(); await flush();
      loginStates = {
        before_restore: {
          disabled: getEl('login-submit').disabled,
          feedback: getEl('login-msg').textContent,
          password: getEl('login-password').value,
        },
      };
      const eventsBeforeRestore = events.length;
      releaseDeferred('restore-a');
      await flush(); await flush(); await flush();
      await runPendingTimeouts(50);
      await flush(); await flush();
      const storedAfterRestore = storage.get('yt_final_web_session_v2');
      loginStates.after_restore = {
        disabled: getEl('login-submit').disabled,
        feedback: getEl('login-msg').textContent,
        feedback_role: getEl('login-msg').getAttribute('role'),
        password: getEl('login-password').value,
        app_started: getEl('login-gate').hidden === true
          && getEl('app-root').hidden === false,
        stored: storedAfterRestore ? JSON.parse(storedAfterRestore) : null,
        focus_events: events.slice(eventsBeforeRestore)
          .filter((event) => event === 'FOCUS login-password'),
      };
      releaseDeferred('login-a');
      await login;
      await flush(); await flush();
    } else if (SCENARIO.action === 'set_session') {
      let expiry = SCENARIO.session_expiry;
      if (SCENARIO.session_expiry_kind === 'nan') expiry = Number.NaN;
      else if (SCENARIO.session_expiry_kind === 'positive_infinity') {
        expiry = Number.POSITIVE_INFINITY;
      } else if (SCENARIO.session_expiry_kind === 'negative_infinity') {
        expiry = Number.NEGATIVE_INFINITY;
      }
      events.push('SET_SESSION_RESULT ' + String(setSession('fresh', expiry)));
    } else if (SCENARIO.action === 'api') {
      try { await api(SCENARIO.path || '/api/action'); }
      catch (e) { actionError = e && e.message; }
      await flush();
    } else if (SCENARIO.action === 'logout') {
      try { await getEl('logout').onclick(); }
      catch (e) { actionError = e && e.message; }
      await flush();
    } else if (SCENARIO.action === 'updates_bootstrap') {
      await runPendingFrames();
      events.push('PRE_RESTORE_FRAMES_DONE');
      releaseDeferred('restore-a');
      await flush(); await flush(); await flush();
      events.push('RESTORE_DONE');
      await runPendingFrames();
    } else if (SCENARIO.action === 'stale_animation_after_relogin') {
      activateView('updates');
      const oldFrames = takePendingFrames();
      await submitLogin();
      events.push('LOGIN_B_DONE');
      await runFrameCallbacks(oldFrames);
      events.push('OLD_FRAME_DONE');
      await runPendingFrames();
    } else if (SCENARIO.action === 'stale_auth_file_reader') {
      const change = listeners.find((x) => x.id === 'auth-file' && x.type === 'change');
      getEl('auth_file_btn').onclick();
      getEl('auth-file').files = [{ name: 'old-load.txt' }];
      change.fn();
      const loadReader = readers[readers.length - 1];
      getEl('auth_file_btn').onclick();
      getEl('auth-file').files = [{ name: 'old-error.txt' }];
      change.fn();
      const errorReader = readers[readers.length - 1];
      await submitLogin();
      getEl('auth-paste').value = 'session-b-paste';
      getEl('auth-file-name').textContent = 'session-b-name';
      getEl('auth_hint').textContent = 'session-b-auth-clean';
      loadReader.result = 'session-a-secret';
      if (loadReader.onload) loadReader.onload();
      if (errorReader.onerror) errorReader.onerror();
      await flush(); await flush();
    } else if (SCENARIO.action === 'stale_native_picker_change_after_relogin'
               || SCENARIO.action === 'native_picker_change_without_session') {
      const authChange = listeners.find((x) => x.id === 'auth-file' && x.type === 'change');
      const uploadOpen = listeners.find(
        (x) => x.id === 'cookiefile_upload_btn' && x.type === 'click',
      );
      const uploadChange = listeners.find(
        (x) => x.id === 'cookiefile_upload' && x.type === 'change',
      );
      getEl('auth_file_btn').onclick();
      uploadOpen.fn({ target: getEl('cookiefile_upload_btn'), preventDefault() {} });
      await getEl('logout').onclick();
      if (SCENARIO.action === 'stale_native_picker_change_after_relogin') {
        await submitLogin();
        events.push('LOGIN_B_DONE');
        getEl('auth-file-name').textContent = 'session-b-auth-name';
        getEl('auth-paste').value = 'session-b-auth-paste';
        getEl('hint').textContent = 'session-b-upload-clean';
      }
      getEl('auth-file').files = [{ name: 'session-a-auth.txt' }];
      getEl('cookiefile_upload').files = [{
        name: 'session-a-upload.txt',
        text() {
          fileReads.push({ reader: 'File.text', name: this.name });
          return Promise.resolve('session-a-cookie-secret');
        },
      }];
      await captureCallback(() => authChange.fn());
      await captureCallback(() => uploadChange.fn());
      await flush(); await flush();
    } else if (SCENARIO.action === 'stale_login_focus') {
      await getEl('logout').onclick();
      await submitLogin();
      events.push('LOGIN_B_DONE');
      await runPendingTimeouts(50);
    } else if (SCENARIO.action === 'stale_restore_after_login') {
      await submitLogin();
      releaseDeferred('restore-a');
      await flush(); await flush();
    } else if (SCENARIO.action === 'stale_restore_body_after_login') {
      await flush();
      releaseDeferred('restore-body-a');
      // Queue the successful-login session replacement behind sessionJson's final body-read check,
      // but ahead of tryExistingSession's continuation. This exercises the microtask gap between
      // the transport helper returning and its caller applying the restore.
      queueMicrotask(() => {
        setSession('session-b', 9999999999);
        startApp();
        events.push('LOGIN_B_DONE');
      });
      await flush(); await flush();
    } else if (SCENARIO.action === 'restore_storage_failure') {
      const before = currentSessionContext();
      releaseDeferred('restore-body-a');
      await flush(); await flush(); await flush();
      const after = currentSessionContext();
      sessionStates = {
        before_generation: before.generation,
        before_token: before.token,
        prior_aborted: !!(before.signal && before.signal.aborted),
        after_generation: after.generation,
        after_token: after.token,
        same_signal: after.signal === before.signal,
      };
    } else if (SCENARIO.action === 'stale_restore_rejection_after_login') {
      await submitLogin();
      events.push('LOGIN_B_DONE');
      rejectDeferred('restore-a', 'old restore connection failed');
      await flush(); await flush(); await flush();
    } else if (SCENARIO.action === 'stale_401_after_relogin') {
      const oldAction = api('/api/action').catch((e) => { actionError = e && e.message; });
      await flush();
      await submitLogin();
      releaseDeferred('action-a');
      await oldAction;
      await flush();
    } else if (SCENARIO.action === 'stale_logout_after_relogin') {
      const oldLogout = getEl('logout').onclick();
      await flush();
      await submitLogin();
      releaseDeferred('logout-a');
      await oldLogout;
      await flush();
    } else if (SCENARIO.action === 'stale_action_ui_after_relogin') {
      const oldAction = postAction('/api/action', 'job-a');
      await flush();
      await submitLogin();
      getEl('hint').textContent = 'session-b-clean';
      releaseDeferred('action-ui-a');
      await oldAction;
      await flush();
    } else if (SCENARIO.action === 'stale_pipeline_body_after_relogin') {
      const work = [
        getEl('retry').onclick(),
        getEl('clear').onclick(),
        getEl('pause').onclick(),
      ];
      await flush();
      await submitLogin();
      events.push('LOGIN_B_DONE');
      getEl('hint').textContent = 'session-b-pipeline-clean';
      releaseDeferred('retry-body-a');
      releaseDeferred('clear-body-a');
      releaseDeferred('pause-body-a');
      await Promise.all(work);
      await flush(); await flush();
    } else if (SCENARIO.action === 'stale_watch_body_after_relogin') {
      const rowButtons = [mkEl('watch-action-a'), mkEl('watch-action-b')];
      const row = mkEl('watch-row-a');
      row.querySelectorAll = (selector) => selector === 'button[data-act]' ? rowButtons
        : selector === '.pending-vid' ? [mkEl('pending-a')] : [];
      const btn = rowButtons[0];
      btn.dataset = { id: 'watch-a', act: 'poll' };
      btn.closest = (selector) => selector === 'button[data-act]' ? btn
        : selector === '.watch-row' ? row : null;
      const click = listeners.find((x) => x.id === 'watches' && x.type === 'click');
      const oldWatch = click.fn({ target: btn });
      await flush();
      await submitLogin();
      events.push('LOGIN_B_DONE');
      getEl('watch_hint').textContent = 'session-b-watch-clean';
      rowButtons.forEach((b) => { b.disabled = false; });
      releaseDeferred('watch-body-a');
      await oldWatch;
      await flush(); await flush();
    } else if (SCENARIO.action === 'stale_network_body_after_relogin') {
      const oldNetwork = saveNetworkSettings();
      await flush();
      await submitLogin();
      events.push('LOGIN_B_DONE');
      getEl('network_status').textContent = 'session-b-network-clean';
      releaseDeferred('network-body-a');
      await oldNetwork;
      await flush(); await flush();
    } else if (SCENARIO.action === 'speedtest_logout_relogin') {
      getEl('st_run').textContent = 'Run speed test';
      const oldSpeed = runSpeedTest();
      await flush();
      await getEl('logout').onclick();
      await submitLogin();
      events.push('LOGIN_B_DONE');
      const afterRelogin = {
        disabled: getEl('st_run').disabled,
        text: getEl('st_run').textContent,
      };
      releaseDeferred('speed-body-a');
      await oldSpeed;
      await flush(); await flush();
      events.push('SPEED_AFTER_RELOGIN ' + JSON.stringify(afterRelogin));
    } else if (SCENARIO.action === 'reset_authenticated_controls') {
      global.confirm = window.confirm = () => true;
      const defaults = {
        probe_res: 'Check formats', browse: 'Browse…', notify_test: 'Send test',
        st_run: 'Run speed test', submit: 'Queue job', watch_add: 'Start watching',
        'auth-from-browser-btn': 'Import from browser', 'auth-login-btn': 'Login with Chrome',
        auth_test: 'Test', 'auth-import-btn': 'Import cookies',
        cookiefile_browse: 'Browse…', download_archive_browse: 'Browse…',
        'st-rec': 'Enable', 'optional-tool': 'Install', 'ffmpeg-tool': 'Install',
        'watch-row-a': 'Check for new', 'watch-row-b': 'Remove',
      };
      Object.entries(defaults).forEach(([id, label]) => { getEl(id).textContent = label; });
      getEl('urls').value = 'https://example.test/video';
      getEl('notify_url').value = 'https://hooks.example.test/notify';
      getEl('watch_channel').value = 'https://youtube.example/@channel';
      getEl('auth-paste').value = 'cookie-data';
      getEl('optional-tool').className = 'jact upd-act';
      getEl('optional-tool').dataset = { systemTool: 'deno' };
      getEl('ffmpeg-tool').className = 'jact upd-act';
      getEl('ffmpeg-tool').dataset = {};

      const rowButtons = [getEl('watch-row-a'), getEl('watch-row-b')];
      const row = mkEl('watch-row');
      row.querySelectorAll = (selector) => selector === 'button[data-act]' ? rowButtons
        : selector === '.pending-vid' ? [] : [];
      rowButtons[0].dataset = { id: 'watch-a', act: 'poll' };
      rowButtons[0].closest = (selector) => selector === 'button[data-act]' ? rowButtons[0]
        : selector === '.watch-row' ? row : null;
      const watchClick = listeners.find((x) => x.id === 'watches' && x.type === 'click');

      const work = [
        getEl('probe_res').onclick(),
        getEl('browse').onclick(),
        getEl('notify_test').onclick(),
        runSpeedTest(),
        getEl('submit').onclick(),
        getEl('watch_add').onclick(),
        authFromBrowser(),
        authLogin(),
        authAction('test', getEl('auth_test')),
        getEl('auth-import-btn').onclick(),
        getEl('cookiefile_browse').onclick(),
        getEl('download_archive_browse').onclick(),
        stDoAction({
          action: { kind: 'enable_bypass', endpoint: '/api/settings/action', payload: {} },
        }, getEl('st-rec')),
        runOptionalToolInstall(getEl('optional-tool')),
        runFFmpegAction(getEl('ffmpeg-tool')),
        watchClick.fn({ target: rowButtons[0] }),
      ].map((p) => Promise.resolve(p).catch(() => null));
      await flush(); await flush();
      await getEl('logout').onclick();
      await submitLogin();
      events.push('LOGIN_B_DONE');
      const restoredControls = Object.fromEntries(
        Object.entries(defaults).map(([id]) => [
          id, { disabled: getEl(id).disabled, text: getEl(id).textContent },
        ]),
      );
      releaseAllDeferred();
      await Promise.all(work);
      await flush(); await flush();
      events.push('RESTORED_CONTROLS ' + JSON.stringify(restoredControls));
    } else if (SCENARIO.action === 'replacement_control_owner') {
      getEl('browse').textContent = 'Browse…';
      const oldBrowse = getEl('browse').onclick();
      await flush();
      await getEl('logout').onclick();
      await submitLogin();
      const newBrowse = getEl('browse').onclick();
      await flush();
      releaseDeferred('browse-a');
      await oldBrowse;
      const whileB = {
        disabled: getEl('browse').disabled,
        text: getEl('browse').textContent,
      };
      releaseDeferred('browse-b');
      await newBrowse;
      events.push('BROWSE_WHILE_B ' + JSON.stringify(whileB));
    } else if (SCENARIO.action === 'terminal_recommendation_relogin') {
      const spec = SCENARIO.terminal_action;
      const btn = getEl('terminal-rec');
      btn.textContent = spec.default_text;
      btn.disabled = false;
      btn.title = spec.default_title;
      btn.className = 'st-rec-btn';
      await stDoAction({
        action: {
          kind: spec.kind,
          label: spec.default_text,
          endpoint: spec.endpoint,
          payload: spec.payload || {},
        },
      }, btn);
      await flush(); await flush();
      const sessionATerminal = {
        disabled: btn.disabled,
        text: btn.textContent,
        done_class: btn.classList.contains('is-done'),
        title: btn.title,
      };
      await getEl('logout').onclick();
      await submitLogin();
      terminalStates = {
        session_a_terminal: sessionATerminal,
        session_b_default: {
          disabled: btn.disabled,
          text: btn.textContent,
          done_class: btn.classList.contains('is-done'),
          title: btn.title,
        },
      };
    } else if (SCENARIO.action === 'terminal_control_commit_lifecycle') {
      const btn = getEl('terminal-commit');
      btn.textContent = 'Default action';
      btn.disabled = false;
      const actionA = beginAuthenticatedAction();
      const controlA = actionA.ownControl(btn, { text: 'Working…' });
      controlA.commit(() => {
        btn.disabled = true;
        btn.textContent = 'Committed ✓';
      });
      controlA.release();
      const afterCurrentFinally = { disabled: btn.disabled, text: btn.textContent };
      await getEl('logout').onclick();
      await submitLogin();
      const afterTransition = { disabled: btn.disabled, text: btn.textContent };
      const actionB = beginAuthenticatedAction();
      const controlB = actionB.ownControl(btn, { text: 'B working…' });
      controlA.release();
      const afterStaleRelease = { disabled: btn.disabled, text: btn.textContent };
      controlB.release();
      terminalStates = {
        after_current_finally: afterCurrentFinally,
        after_transition: afterTransition,
        after_stale_release: afterStaleRelease,
      };
    } else if (SCENARIO.action === 'cancel_authenticated_work') {
      await refreshUpdates({ quiet: true });
      const work = [
        loadGpuStatus({ attempts: 1, delayMs: 1 }),
        refresh({ force: true }),
        refreshWatches(),
        fetchUpdatesReport({ force: true, pypi: true }),
        api('/api/action'),
        api('/api/speedtest', { method: 'POST', body: '{}' }),
        loadNetworkSettings(),
      ].map((p) => Promise.resolve(p).catch(() => null));
      await flush();
      await getEl('logout').onclick();
      await submitLogin();
      events.push('LOGIN_B_DONE');
      await runPendingTimeouts(40);
      releaseAllDeferred();
      await flush(); await flush();
      await Promise.all(work);
    } else if (SCENARIO.action === 'transport_inventory') {
      global.confirm = window.confirm = () => true;
      await loadGpuStatus({ attempts: 1, delayMs: 1 });
      await refresh({ force: true });
      await refreshWatches();
      await fetchUpdatesReport({ force: true, pypi: true });
      await api('/api/action');
      await runOptionalToolInstall(Object.assign(mkEl('tool-btn'), {
        textContent: 'Install', dataset: { systemTool: 'deno' },
      }));
      await runFFmpegAction(Object.assign(mkEl('ffmpeg-btn'), {
        textContent: 'Install', dataset: {},
      }));
      await sessionFetch('/api/headers-instance', {
        headers: new Headers([['x-token', 'stale-a'], ['X-Custom', 'keep-a']]),
      });
      await sessionFetch('/api/headers-mixed', {
        headers: { 'x-ToKeN': 'stale-b', 'X-Custom': 'keep-b' },
      });
      await flush();
    }
    const gate = getEl('login-gate');
    const root = getEl('app-root');
    const loginMessage = getEl('login-msg');
    console.log(JSON.stringify({
      ok: true,
      app_started: gate.hidden === true && root.hidden === false,
      login_visible: gate.hidden === false && root.hidden === true,
      action_error: actionError,
      hint_text: getEl('hint').textContent,
      watch_hint_text: getEl('watch_hint').textContent,
      auth_hint_text: getEl('auth_hint').textContent,
      login_hint_text: loginMessage.textContent,
      login_feedback: {
        text: loginMessage.textContent,
        role: loginMessage.getAttribute('role'),
        live: loginMessage.getAttribute('aria-live'),
        classes: loginMessage.classList.snapshot(),
      },
      login_submit_disabled: getEl('login-submit').disabled,
      auth_paste_text: getEl('auth-paste').value,
      auth_file_name: getEl('auth-file-name').textContent,
      network_status_text: getEl('network_status').textContent,
      speed_button: { disabled: getEl('st_run').disabled, text: getEl('st_run').textContent },
      events, storageOps, storage: Object.fromEntries(storage), fetchCalls, body_reads: bodyReads,
      replacedUrls,
      file_reads: fileReads, callback_errors: callbackErrors,
      terminal_states: terminalStates, session_states: sessionStates, login_states: loginStates,
      location: { pathname: fakeLocation.pathname, search: fakeLocation.search, hash: fakeLocation.hash },
      timers: {
        timeouts: activeTimeouts.size,
        timeout_delays: Array.from(activeTimeouts.values()).map((x) => x.delay),
        intervals: activeIntervals.size,
        frames: activeFrames.size,
      },
    }));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: e.name + ': ' + e.message, stack: e.stack }));
    process.exitCode = 1;
  }
})();
"""


class JsRunner:
    def __init__(self, tmp_path: Path):
        self.node = shutil.which("node")
        if not self.node:
            pytest.skip("node not available to execute the UI")
        self.harness = tmp_path / "webui-session.js"
        self.harness.write_text(HARNESS, encoding="utf-8")

    def run(
        self,
        *,
        action: str = "restore",
        stored: object | None = None,
        stored_raw: str | None = None,
        legacy: str | None = None,
        response: dict | None = None,
        responses: dict | None = None,
        query: str = "",
        hash_: str = "",
        path: str = "/api/action",
        terminal_action: dict | None = None,
        now_seconds: int | None = None,
        fail_session_storage_write: bool = False,
        session_expiry: object | None = None,
        session_expiry_kind: str | None = None,
        timeout_mode: str | None = None,
        login_completion_timing: str | None = None,
    ) -> dict:
        storage: dict[str, str] = {}
        if stored_raw is not None:
            storage[SESSION_KEY] = stored_raw
        elif stored is not None:
            storage[SESSION_KEY] = json.dumps(stored)
        if legacy is not None:
            storage[LEGACY_KEY] = legacy
        configured = dict(responses or {})
        if response is not None:
            configured["/api/session"] = response
        scenario = {
            "action": action,
            "storage": storage,
            "responses": configured,
            "query": query,
            "hash": hash_,
            "path": path,
            "terminal_action": terminal_action,
            "now_seconds": now_seconds,
            "fail_session_storage_write": fail_session_storage_write,
            "session_expiry": session_expiry,
            "session_expiry_kind": session_expiry_kind,
            "timeout_mode": timeout_mode,
            "login_completion_timing": login_completion_timing,
        }
        result = subprocess.run(
            [self.node, str(self.harness), str(WEBUI / "index.html"), str(WEBUI / "app.js"),
             json.dumps(scenario)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        lines = (result.stdout or "").strip().splitlines()
        got = json.loads(lines[-1]) if lines else {"ok": False, "error": result.stderr or ""}
        assert result.returncode == 0 and got.get("ok"), got
        return got

    def run_session_restore(self, *, stored: object, response: dict) -> dict:
        return self.run(stored=stored, response=response)

    def run_logout(self, *, reject: str | None = None) -> dict:
        logout = {"reject": reject} if reject else {"status": 200, "body": {"ok": True}}
        got = self.run(
            action="logout",
            stored={"token": "abc", "expires_at": FUTURE},
            response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
            responses={"/api/logout": logout},
        )
        return got


@pytest.fixture
def js_runner(tmp_path):
    return JsRunner(tmp_path)


def _header(call: dict, name: str):
    return next(
        (value for key, value in call["headers"].items() if key.lower() == name.lower()),
        None,
    )


def _assert_login_feedback(result: dict, text: str, severity: str) -> None:
    assert severity in {"status", "alert"}
    expected_classes = ["bad", "hint", "login-hint"] if severity == "alert" else [
        "hint",
        "login-hint",
    ]
    assert result["login_feedback"] == {
        "text": text,
        "role": severity,
        "live": "assertive" if severity == "alert" else "polite",
        "classes": expected_classes,
    }
    assert result["login_hint_text"] == text


def _assert_locked_without_polling(
    result: dict,
    feedback: str,
    request_paths: list[str] | None = None,
) -> None:
    assert result["app_started"] is False
    assert result["login_visible"] is True
    _assert_login_feedback(result, feedback, "alert")
    assert [call["path"] for call in result["fetchCalls"]] == (
        request_paths if request_paths is not None else ["/api/session"]
    )
    assert result["timers"]["intervals"] == 0
    assert result["timers"]["frames"] == 0
    assert 1_200 not in result["timers"]["timeout_delays"]


@pytest.mark.parametrize(
    "status",
    [201, 202, 299],
)
def test_session_bootstrap_requires_exact_http_200_before_reading_body(js_runner, status):
    result = js_runner.run_session_restore(
        stored={"token": "abc", "expires_at": FUTURE},
        response={"status": status, "body": {"ok": True, "expires_at": FUTURE}},
    )
    _assert_locked_without_polling(
        result,
        "Session temporarily unavailable — try again",
    )
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "abc",
        "expires_at": FUTURE,
    }
    assert result["body_reads"] == []


@pytest.mark.parametrize(
    ("status", "feedback", "preserved"),
    [
        (401, "Session expired — sign in again", False),
        (403, "Session restore blocked by server policy — sign in again", True),
        (404, "Session temporarily unavailable — try again", True),
        (500, "Session temporarily unavailable — try again", True),
    ],
)
def test_session_bootstrap_classifies_http_status_before_reading_body(
    js_runner,
    status,
    feedback,
    preserved,
):
    result = js_runner.run_session_restore(
        stored={"token": "abc", "expires_at": FUTURE},
        response={
            "status": status,
            "body": {"ok": True, "expires_at": FUTURE},
            "defer_body": "body-must-not-be-read",
        },
    )
    _assert_locked_without_polling(result, feedback)
    assert (SESSION_KEY in result["storage"]) is preserved
    assert result["body_reads"] == []


def test_session_bootstrap_rejects_explicitly_unsuccessful_200_without_reading_body(js_runner):
    result = js_runner.run_session_restore(
        stored={"token": "abc", "expires_at": FUTURE},
        response={
            "status": 200,
            "ok": False,
            "body": {"ok": True, "expires_at": FUTURE},
            "defer_body": "body-must-not-be-read",
        },
    )
    _assert_locked_without_polling(
        result,
        "Session temporarily unavailable — try again",
    )
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "abc",
        "expires_at": FUTURE,
    }
    assert result["body_reads"] == []


@pytest.mark.parametrize(
    ("body", "expiry_kind", "now_seconds"),
    [
        (None, None, None),
        ([], None, None),
        ("not an object", None, None),
        (True, None, None),
        (False, None, None),
        ({}, None, None),
        ({"ok": False, "expires_at": FUTURE}, None, None),
        ({"ok": True}, None, None),
        ({"ok": True, "expires_at": "9999999999"}, None, None),
        ({"ok": True, "expires_at": True}, None, None),
        ({"ok": True, "expires_at": 1}, None, None),
        ({"ok": True, "expires_at": 2_000_000_000}, None, 2_000_000_000),
        ({"ok": True, "expires_at": 10_000_000_000}, None, None),
        ({"ok": True, "expires_at": 10_000_000_001}, None, None),
        ({"ok": True, "expires_at": FUTURE * 1_000}, None, None),
        ({"ok": True, "expires_at": FUTURE}, "nan", None),
        ({"ok": True, "expires_at": FUTURE}, "positive_infinity", None),
        ({"ok": True, "expires_at": FUTURE}, "negative_infinity", None),
    ],
    ids=[
        "null",
        "array",
        "string",
        "true",
        "false",
        "empty-object",
        "ok-false",
        "missing-expiry",
        "string-expiry",
        "boolean-expiry",
        "past-expiry",
        "equal-now-expiry",
        "ten-billion-boundary",
        "above-ten-billion",
        "millisecond-like-expiry",
        "nan-expiry",
        "positive-infinity-expiry",
        "negative-infinity-expiry",
    ],
)
def test_session_bootstrap_malformed_200_preserves_retry_context(
    js_runner,
    body,
    expiry_kind,
    now_seconds,
):
    response = {"status": 200, "body": body}
    if expiry_kind is not None:
        response["expiry_kind"] = expiry_kind
    result = js_runner.run(
        stored={"token": "abc", "expires_at": FUTURE},
        response=response,
        now_seconds=now_seconds,
    )
    _assert_locked_without_polling(
        result,
        "Session temporarily unavailable — try again",
    )
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "abc",
        "expires_at": FUTURE,
    }
    assert result["body_reads"] == [{"path": "/api/session", "kind": "json"}]


@pytest.mark.parametrize(
    "response",
    [
        {"reject": "connection refused"},
        {"status": 200, "reject_body": "invalid JSON"},
    ],
    ids=["network-rejection", "json-rejection"],
)
def test_session_bootstrap_transport_or_json_failure_is_retryable(js_runner, response):
    result = js_runner.run_session_restore(
        stored={"token": "abc", "expires_at": FUTURE},
        response=response,
    )
    _assert_locked_without_polling(
        result,
        "Session temporarily unavailable — try again",
    )
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "abc",
        "expires_at": FUTURE,
    }


def test_session_bootstrap_without_storage_stays_locked_and_makes_no_request(js_runner):
    result = js_runner.run()
    assert result["app_started"] is False
    assert result["login_visible"] is True
    _assert_login_feedback(result, "", "status")
    assert result["fetchCalls"] == []
    assert result["timers"]["intervals"] == 0
    assert result["timers"]["frames"] == 0
    assert 1_200 not in result["timers"]["timeout_delays"]


@pytest.mark.parametrize(
    ("stored", "stored_raw"),
    [
        (None, "{not json"),
        (["abc", FUTURE], None),
        ({"expires_at": FUTURE}, None),
        ({"token": "", "expires_at": FUTURE}, None),
        ({"token": "abc", "expires_at": "9999999999"}, None),
        ({"token": "abc", "expires_at": 1}, None),
        ({"token": "abc", "expires_at": FUTURE, "role": "admin"}, None),
        ({"token": "abc", "expires_at": FUTURE, "__proto__": {"token": "evil"}}, None),
    ],
)
def test_malformed_or_expired_storage_is_cleared_before_any_request(js_runner, stored, stored_raw):
    result = js_runner.run(stored=stored, stored_raw=stored_raw)
    assert SESSION_KEY not in result["storage"]
    assert result["fetchCalls"] == []
    assert result["login_visible"] is True
    _assert_login_feedback(result, "", "status")


@pytest.mark.parametrize(
    "expiry",
    [10_000_000_000, 10_000_000_001, FUTURE * 1_000],
    ids=["ten-billion-boundary", "above-ten-billion", "millisecond-like"],
)
def test_out_of_range_stored_expiry_is_cleared_before_any_authenticated_work(
    js_runner, expiry
):
    result = js_runner.run(stored={"token": "abc", "expires_at": expiry})

    assert SESSION_KEY not in result["storage"]
    assert result["fetchCalls"] == []
    assert result["app_started"] is False
    assert result["login_visible"] is True
    _assert_login_feedback(result, "", "status")
    assert result["timers"]["intervals"] == 0
    assert result["timers"]["frames"] == 0
    assert 1_200 not in result["timers"]["timeout_delays"]


def test_set_session_rejects_ten_billion_before_persistence(js_runner):
    result = js_runner.run(
        action="set_session",
        session_expiry=10_000_000_000,
    )

    assert "SET_SESSION_RESULT false" in result["events"]
    assert SESSION_KEY not in result["storage"]
    assert result["app_started"] is False
    assert result["fetchCalls"] == []
    assert result["timers"]["intervals"] == 0
    assert result["timers"]["frames"] == 0


def test_session_bootstrap_valid_200_refreshes_expiry_and_starts_every_request_once(js_runner):
    result = js_runner.run_session_restore(
        stored={"token": "abc", "expires_at": FUTURE - 10},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
    )
    assert result["app_started"] is True
    assert result["login_visible"] is False
    _assert_login_feedback(result, "", "status")
    expected_paths = [
        "/api/session",
        "/api/profiles",
        "/api/jobs",
        "/api/watches",
        "/api/auth",
        "/api/settings",
        "/api/gpu",
        "/api/health",
    ]
    actual_paths = [call["path"] for call in result["fetchCalls"]]
    assert Counter(actual_paths) == Counter({path: 1 for path in expected_paths})
    assert actual_paths == expected_paths
    assert all(_header(call, "X-Token") == "abc" for call in result["fetchCalls"])
    assert json.loads(result["storage"][SESSION_KEY]) == {"token": "abc", "expires_at": FUTURE}


def test_session_bootstrap_storage_write_failure_is_transactional_and_retryable(js_runner):
    result = js_runner.run(
        action="restore_storage_failure",
        stored={"token": "abc", "expires_at": FUTURE},
        response={
            "status": 200,
            "body": {"ok": True, "expires_at": FUTURE - 1},
            "defer_body": "restore-body-a",
        },
        fail_session_storage_write=True,
    )
    _assert_locked_without_polling(
        result,
        "Browser storage temporarily unavailable — try again",
    )
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "abc",
        "expires_at": FUTURE,
    }
    assert result["session_states"] == {
        "before_generation": 1,
        "before_token": "abc",
        "prior_aborted": False,
        "after_generation": 1,
        "after_token": "abc",
        "same_signal": True,
    }
    assert ["set-error", SESSION_KEY] in result["storageOps"]
    assert result["callback_errors"] == []


def test_login_storage_write_failure_stays_locked_without_authenticated_work(js_runner):
    result = js_runner.run(
        action="login",
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "fresh", "expires_at": FUTURE},
            },
        },
        fail_session_storage_write=True,
    )
    _assert_locked_without_polling(
        result,
        "Browser storage temporarily unavailable — try again",
        ["/api/login", "/api/logout"],
    )
    assert SESSION_KEY not in result["storage"]
    assert _header(result["fetchCalls"][0], "X-Token") is None
    assert _header(result["fetchCalls"][1], "X-Token") == "fresh"
    assert ["set-error", SESSION_KEY] in result["storageOps"]
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_older_success_cannot_replace_newer_login_and_is_revoked(js_runner):
    result = js_runner.run(
        action="overlapping_login",
        responses={
            "/api/login": [
                {
                    "defer": "login-older",
                    "ignore_abort": True,
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "older-token",
                        "expires_at": FUTURE,
                    },
                },
                {
                    "defer": "login-newer",
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "newer-token",
                        "expires_at": FUTURE,
                    },
                },
            ],
            "/api/logout": {"status": 200, "body": {"ok": True}},
        },
    )

    assert result["app_started"] is True
    assert result["login_visible"] is False
    _assert_login_feedback(result, "", "status")
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "newer-token",
        "expires_at": FUTURE,
    }
    logout_calls = [
        call for call in result["fetchCalls"] if call["path"] == "/api/logout"
    ]
    assert len(logout_calls) == 1
    assert _header(logout_calls[0], "X-Token") == "older-token"
    login_calls = [
        call for call in result["fetchCalls"] if call["path"] == "/api/login"
    ]
    assert [call["aborted"] for call in login_calls] == [True, False]
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_older_success_cannot_override_newer_failure_and_is_revoked(js_runner):
    result = js_runner.run(
        action="overlapping_login",
        responses={
            "/api/login": [
                {
                    "defer": "login-older",
                    "ignore_abort": True,
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "older-token",
                        "expires_at": FUTURE,
                    },
                },
                {
                    "defer": "login-newer",
                    "status": 401,
                    "body": {"ok": False, "error": "newer password rejected"},
                },
            ],
            "/api/logout": {"status": 200, "body": {"ok": True}},
        },
    )

    assert result["app_started"] is False
    assert result["login_visible"] is True
    _assert_login_feedback(result, "newer password rejected", "alert")
    assert SESSION_KEY not in result["storage"]
    logout_calls = [
        call for call in result["fetchCalls"] if call["path"] == "/api/logout"
    ]
    assert len(logout_calls) == 1
    assert _header(logout_calls[0], "X-Token") == "older-token"
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_older_rejection_cannot_overwrite_newer_success(js_runner):
    result = js_runner.run(
        action="overlapping_login",
        responses={
            "/api/login": [
                {
                    "defer": "login-older",
                    "ignore_abort": True,
                    "status": 401,
                    "body": {"ok": False, "error": "older password rejected"},
                },
                {
                    "defer": "login-newer",
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "newer-token",
                        "expires_at": FUTURE,
                    },
                },
            ],
        },
    )

    assert result["app_started"] is True
    assert result["login_visible"] is False
    _assert_login_feedback(result, "", "status")
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "newer-token",
        "expires_at": FUTURE,
    }
    assert not [
        call for call in result["fetchCalls"] if call["path"] == "/api/logout"
    ]
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_stale_success_equal_to_current_token_is_not_revoked(js_runner):
    result = js_runner.run(
        action="overlapping_login",
        responses={
            "/api/login": [
                {
                    "defer": "login-older",
                    "ignore_abort": True,
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "shared-current-token",
                        "expires_at": FUTURE,
                    },
                },
                {
                    "defer": "login-newer",
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "shared-current-token",
                        "expires_at": FUTURE,
                    },
                },
            ],
        },
    )

    assert result["app_started"] is True
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "shared-current-token",
        "expires_at": FUTURE,
    }
    assert not [
        call for call in result["fetchCalls"] if call["path"] == "/api/logout"
    ]
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_stale_success_remains_inert_when_revocation_rejects(js_runner):
    result = js_runner.run(
        action="overlapping_login",
        responses={
            "/api/login": [
                {
                    "defer": "login-older",
                    "ignore_abort": True,
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "orphaned-older-token",
                        "expires_at": FUTURE,
                    },
                },
                {
                    "defer": "login-newer",
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "current-newer-token",
                        "expires_at": FUTURE,
                    },
                },
            ],
            "/api/logout": {"reject": "revocation transport failed"},
        },
    )

    assert result["app_started"] is True
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "current-newer-token",
        "expires_at": FUTURE,
    }
    logout_calls = [
        call for call in result["fetchCalls"] if call["path"] == "/api/logout"
    ]
    assert len(logout_calls) == 1
    assert _header(logout_calls[0], "X-Token") == "orphaned-older-token"
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_stale_success_revocation_has_a_deterministic_deadline(js_runner):
    result = js_runner.run(
        action="overlapping_login_revoke_timeout",
        timeout_mode="manual",
        responses={
            "/api/login": [
                {
                    "defer": "login-older",
                    "ignore_abort": True,
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "orphaned-older-token",
                        "expires_at": FUTURE,
                    },
                },
                {
                    "defer": "login-newer",
                    "status": 200,
                    "body": {
                        "ok": True,
                        "token": "current-newer-token",
                        "expires_at": FUTURE,
                    },
                },
            ],
            "/api/logout": {
                "defer": "revoke-a",
                "status": 200,
                "body": {"ok": True},
            },
        },
    )

    assert result["app_started"] is True
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "current-newer-token",
        "expires_at": FUTURE,
    }
    logout_calls = [
        call for call in result["fetchCalls"] if call["path"] == "/api/logout"
    ]
    assert len(logout_calls) == 1
    assert _header(logout_calls[0], "X-Token") == "orphaned-older-token"
    assert logout_calls[0]["aborted"] is True
    assert result["login_states"]["while_revoke_pending_disabled"] is False
    assert 5_000 not in result["timers"]["timeout_delays"]
    assert result["callback_errors"] == []


@pytest.mark.parametrize("timeout_mode", ["manual", "fake-native"])
def test_login_deadline_covers_a_stalled_response_body(js_runner, timeout_mode):
    result = js_runner.run(
        action="login_body_timeout",
        timeout_mode=timeout_mode,
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "too-late", "expires_at": FUTURE},
                "defer_body": "login-body",
            },
        },
    )

    assert result["login_states"] == {
        "while_body_pending_disabled": True,
        "while_body_pending_feedback": "Signing in…",
        "after_deadline_disabled": False,
        "after_deadline_feedback": "Cannot reach the app — is it running?",
    }
    assert result["app_started"] is False
    assert result["login_visible"] is True
    _assert_login_feedback(
        result,
        "Cannot reach the app — is it running?",
        "alert",
    )
    assert SESSION_KEY not in result["storage"]
    login_call = next(call for call in result["fetchCalls"] if call["path"] == "/api/login")
    assert login_call["aborted"] is True
    assert 30_000 not in result["timers"]["timeout_delays"]
    assert result["callback_errors"] == []


def test_login_malformed_json_is_invalid_not_a_transport_failure(js_runner):
    result = js_runner.run(
        action="login",
        responses={
            "/api/login": {
                "status": 200,
                "reject_body": "malformed JSON",
            },
        },
    )

    _assert_locked_without_polling(
        result,
        "Invalid session response",
        ["/api/login"],
    )
    assert SESSION_KEY not in result["storage"]
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_login_accepts_ten_billion_minus_one_and_stores_only_the_versioned_record(
    js_runner,
):
    result = js_runner.run(
        action="login",
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "fresh", "expires_at": FUTURE},
            },
        },
    )
    assert result["app_started"] is True
    _assert_login_feedback(result, "", "status")
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "fresh",
        "expires_at": FUTURE,
    }
    assert LEGACY_KEY not in result["storage"]


@pytest.mark.parametrize(
    ("expiry", "expiry_kind"),
    [
        (10_000_000_000, None),
        (10_000_000_001, None),
        (FUTURE * 1_000, None),
        ("9999999999", None),
        (True, None),
        (FUTURE, "nan"),
        (FUTURE, "positive_infinity"),
        (FUTURE, "negative_infinity"),
    ],
    ids=[
        "ten-billion-boundary",
        "above-ten-billion",
        "millisecond-like",
        "string",
        "boolean",
        "nan",
        "positive-infinity",
        "negative-infinity",
    ],
)
def test_login_rejects_invalid_expiry_before_authenticated_work(
    js_runner, expiry, expiry_kind
):
    response = {
        "status": 200,
        "body": {"ok": True, "token": "fresh", "expires_at": expiry},
    }
    if expiry_kind is not None:
        response["expiry_kind"] = expiry_kind
    result = js_runner.run(
        action="login",
        responses={"/api/login": response},
    )

    _assert_locked_without_polling(
        result,
        "Invalid session response",
        ["/api/login", "/api/logout"],
    )
    assert SESSION_KEY not in result["storage"]
    assert _header(result["fetchCalls"][0], "X-Token") is None
    assert _header(result["fetchCalls"][1], "X-Token") == "fresh"
    assert result["callback_errors"] == []


@pytest.mark.parametrize("body", [None, [], "not-an-object", 7])
def test_login_rejects_non_object_success_body_as_invalid_response(js_runner, body):
    result = js_runner.run(
        action="login",
        responses={"/api/login": {"status": 200, "body": body}},
    )

    _assert_locked_without_polling(
        result,
        "Invalid session response",
        ["/api/login"],
    )
    assert SESSION_KEY not in result["storage"]
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_legacy_storage_and_query_tokens_are_discarded_not_promoted(js_runner):
    secret = "retired-secret"
    result = js_runner.run(legacy=secret, query=f"?keep=1&token={secret}", hash_="#watch")
    assert LEGACY_KEY not in result["storage"]
    assert secret not in json.dumps(result["storageOps"])
    assert secret not in json.dumps([call["headers"] for call in result["fetchCalls"]])
    assert result["location"] == {"pathname": "/", "search": "?keep=1", "hash": "#watch"}


def test_401_clears_session_stops_timers_and_locks_the_app(js_runner):
    result = js_runner.run(
        action="api",
        path="/api/action",
        stored={"token": "abc", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={"/api/action": {"status": 401, "body": {"error": "unauthorized"}}},
    )
    assert result["login_visible"] is True
    _assert_login_feedback(result, "Session expired — sign in again", "alert")
    assert SESSION_KEY not in result["storage"]
    assert result["timers"]["intervals"] == 0
    assert 1_200 not in result["timers"]["timeout_delays"]
    assert "Session expired" in result["action_error"]


def test_403_surfaces_policy_error_without_logging_out(js_runner):
    result = js_runner.run(
        action="api",
        path="/api/action",
        stored={"token": "abc", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={"/api/action": {"status": 403, "body": {"error": "policy denied"}}},
    )
    assert result["app_started"] is True
    assert json.loads(result["storage"][SESSION_KEY]) == {"token": "abc", "expires_at": FUTURE}
    assert result["action_error"] == "policy denied"


def test_logout_revokes_before_local_clear(js_runner):
    result = js_runner.run_logout()
    assert result["events"].index("POST /api/logout") < result["events"].index("clearSession")
    assert result["login_visible"] is True
    _assert_login_feedback(result, "Signed out", "status")
    assert SESSION_KEY not in result["storage"]
    assert result["timers"]["intervals"] == 0
    assert 1_200 not in result["timers"]["timeout_delays"]


def test_logout_still_tears_down_locally_when_server_is_gone(js_runner):
    result = js_runner.run_logout(reject="connection refused")
    assert "POST /api/logout" in result["events"]
    assert result["login_visible"] is True
    _assert_login_feedback(result, "Signed out", "status")
    assert SESSION_KEY not in result["storage"]
    assert result["timers"]["intervals"] == 0
    assert 1_200 not in result["timers"]["timeout_delays"]


@pytest.mark.parametrize("completion_timing", ["during", "after"])
def test_logout_immediately_cancels_pending_login_and_revokes_stale_success(
    js_runner, completion_timing
):
    explicit_logout = {
        "status": 200,
        "body": {"ok": True},
    }
    if completion_timing == "during":
        explicit_logout.update({"defer": "logout-a", "ignore_abort": True})
    result = js_runner.run(
        action="pending_login_logout",
        login_completion_timing=completion_timing,
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/login": {
                "defer": "login-a",
                "ignore_abort": True,
                "status": 200,
                "body": {
                    "ok": True,
                    "token": "issued-after-logout",
                    "expires_at": FUTURE,
                },
            },
            "/api/logout": [
                explicit_logout,
                {"status": 200, "body": {"ok": True}},
            ],
        },
    )

    assert result["login_states"]["while_login_pending_disabled"] is True
    assert result["login_states"]["after_logout_started_disabled"] is False
    assert result["login_states"]["login_aborted_after_logout_started"] is True
    if completion_timing == "during":
        assert result["login_states"]["after_login_completed_during_logout"] == {
            "disabled": False,
            "stored": True,
        }
    else:
        assert result["login_states"]["after_logout_completed"] == {
            "disabled": False,
            "stored": False,
        }
    assert result["app_started"] is False
    assert result["login_visible"] is True
    _assert_login_feedback(result, "Signed out", "status")
    assert SESSION_KEY not in result["storage"]
    logout_calls = [
        call for call in result["fetchCalls"] if call["path"] == "/api/logout"
    ]
    assert [_header(call, "X-Token") for call in logout_calls] == [
        "session-a",
        "issued-after-logout",
    ]
    assert result["events"][-1] == "FOCUS login-password"
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_session_teardown_immediately_cancels_pending_login(js_runner):
    result = js_runner.run(
        action="pending_login_session_lost",
        responses={
            "/api/login": {
                "defer": "login-a",
                "ignore_abort": True,
                "status": 200,
                "body": {
                    "ok": True,
                    "token": "issued-after-teardown",
                    "expires_at": FUTURE,
                },
            },
            "/api/logout": {"status": 200, "body": {"ok": True}},
        },
    )

    assert result["login_states"] == {
        "while_login_pending_disabled": True,
        "after_session_lost_disabled": False,
        "login_aborted_after_session_lost": True,
    }
    assert result["app_started"] is False
    assert result["login_visible"] is True
    _assert_login_feedback(result, "Forced session teardown", "alert")
    assert SESSION_KEY not in result["storage"]
    logout_calls = [
        call for call in result["fetchCalls"] if call["path"] == "/api/logout"
    ]
    assert len(logout_calls) == 1
    assert _header(logout_calls[0], "X-Token") == "issued-after-teardown"
    assert result["events"][-1] == "FOCUS login-password"
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_session_bootstrap_starts_no_authenticated_work_before_restore_succeeds(js_runner):
    result = js_runner.run(
        stored={"token": "session-a", "expires_at": FUTURE},
        response={
            "status": 200,
            "body": {"ok": True, "expires_at": FUTURE},
            "defer": "restore-a",
            "ignore_abort": True,
        },
    )
    assert [call["path"] for call in result["fetchCalls"]] == ["/api/session"]


def test_updates_bootstrap_waits_for_restore_then_refreshes_selected_view(js_runner):
    result = js_runner.run(
        action="updates_bootstrap",
        stored={"token": "session-a", "expires_at": FUTURE},
        hash_="#updates",
        response={
            "status": 200,
            "body": {"ok": True, "expires_at": FUTURE},
            "defer": "restore-a",
            "ignore_abort": True,
        },
    )
    before_restore = result["events"][: result["events"].index("PRE_RESTORE_FRAMES_DONE")]
    assert before_restore == ["GET /api/session"]
    after_restore = result["events"][result["events"].index("RESTORE_DONE") + 1 :]
    assert "POST /api/updates" in after_restore
    update = next(call for call in result["fetchCalls"] if call["path"] == "/api/updates")
    assert _header(update, "X-Token") == "session-a"


def test_stale_queued_animation_frame_cannot_launch_session_b_work(js_runner):
    result = js_runner.run(
        action="stale_animation_after_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    old_frame_window = result["events"][
        result["events"].index("LOGIN_B_DONE") + 1 : result["events"].index("OLD_FRAME_DONE")
    ]
    assert "POST /api/updates" not in old_frame_window
    after_old = result["events"][result["events"].index("OLD_FRAME_DONE") + 1 :]
    assert "POST /api/updates" in after_old
    b_updates = [
        call for call in result["fetchCalls"]
        if call["path"] == "/api/updates" and _header(call, "X-Token") == "session-b"
    ]
    assert len(b_updates) == 1


def test_stale_auth_file_reader_callbacks_cannot_paint_new_session(js_runner):
    result = js_runner.run(
        action="stale_auth_file_reader",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["auth_paste_text"] == "session-b-paste"
    assert result["auth_file_name"] == "session-b-name"
    assert result["auth_hint_text"] == "session-b-auth-clean"


def test_native_picker_changes_opened_by_a_are_inert_after_login_b(js_runner):
    result = js_runner.run(
        action="stale_native_picker_change_after_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/logout": {"status": 200, "body": {"ok": True}},
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
            "/api/upload-cookiefile": {
                "status": 200,
                "body": {"ok": True, "path": "C:/session-a-cookies.txt"},
            },
        },
    )
    upload_calls = [
        call for call in result["fetchCalls"]
        if call["path"] == "/api/upload-cookiefile"
    ]
    assert {
        "auth_file_name": result["auth_file_name"],
        "auth_paste_text": result["auth_paste_text"],
        "hint_text": result["hint_text"],
        "file_reads": result["file_reads"],
        "callback_errors": result["callback_errors"],
        "upload_calls": upload_calls,
    } == {
        "auth_file_name": "session-b-auth-name",
        "auth_paste_text": "session-b-auth-paste",
        "hint_text": "session-b-upload-clean",
        "file_reads": [],
        "callback_errors": [],
        "upload_calls": [],
    }


def test_native_picker_changes_are_inert_after_session_ends(js_runner):
    result = js_runner.run(
        action="native_picker_change_without_session",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={"/api/logout": {"status": 200, "body": {"ok": True}}},
    )
    upload_calls = [
        call for call in result["fetchCalls"]
        if call["path"] == "/api/upload-cookiefile"
    ]
    assert {
        "auth_file_name": result["auth_file_name"],
        "auth_paste_text": result["auth_paste_text"],
        "hint_text": result["hint_text"],
        "file_reads": result["file_reads"],
        "callback_errors": result["callback_errors"],
        "upload_calls": upload_calls,
    } == {
        "auth_file_name": "",
        "auth_paste_text": "",
        "hint_text": "",
        "file_reads": [],
        "callback_errors": [],
        "upload_calls": [],
    }


def test_login_gate_focus_callback_is_cancelled_after_relogin(js_runner):
    result = js_runner.run(
        action="stale_login_focus",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/logout": {"status": 200, "body": {"ok": True}},
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    after_login = result["events"][result["events"].index("LOGIN_B_DONE") + 1 :]
    assert "FOCUS login-password" not in after_login


@pytest.mark.parametrize(
    "restore_status",
    [200, 500, 401],
    ids=["success", "server-failure", "unauthorized"],
)
def test_pending_login_owns_feedback_focus_and_button_against_older_restore(
    js_runner, restore_status
):
    result = js_runner.run(
        action="restore_while_login_pending",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={
            "status": restore_status,
            "body": {"ok": True, "expires_at": FUTURE - 100},
            "defer": "restore-a",
            "ignore_abort": True,
        },
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
                "defer": "login-a",
                "ignore_abort": True,
            },
        },
    )

    assert result["login_states"]["before_restore"] == {
        "disabled": True,
        "feedback": "Signing in…",
        "password": "pending-password",
    }
    assert result["login_states"]["after_restore"] == {
        "disabled": True,
        "feedback": "Signing in…",
        "feedback_role": "status",
        "password": "pending-password",
        "app_started": False,
        "stored": {"token": "session-a", "expires_at": FUTURE},
        "focus_events": [],
    }
    assert result["app_started"] is True
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "session-b",
        "expires_at": FUTURE,
    }
    assert result["login_submit_disabled"] is False
    assert result["callback_errors"] == []


def test_stale_session_bootstrap_response_cannot_overwrite_newer_login(js_runner):
    result = js_runner.run(
        action="stale_restore_after_login",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={
            "status": 200,
            "body": {"ok": True, "expires_at": FUTURE - 100},
            "defer": "restore-a",
            "ignore_abort": True,
        },
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["app_started"] is True
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "session-b",
        "expires_at": FUTURE,
    }
    assert result["login_visible"] is False
    assert result["login_hint_text"] == ""


def test_stale_session_bootstrap_body_cannot_overwrite_newer_login(js_runner):
    result = js_runner.run(
        action="stale_restore_body_after_login",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={
            "status": 200,
            "body": {"ok": True, "expires_at": FUTURE - 100},
            "defer_body": "restore-body-a",
        },
    )
    assert result["app_started"] is True
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "session-b",
        "expires_at": FUTURE,
    }
    assert result["login_visible"] is False
    assert result["login_hint_text"] == ""


def test_stale_session_bootstrap_generic_rejection_cannot_relock_newer_login(js_runner):
    result = js_runner.run(
        action="stale_restore_rejection_after_login",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={
            "status": 200,
            "body": {"ok": True, "expires_at": FUTURE - 100},
            "defer": "restore-a",
            "ignore_abort": True,
        },
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["app_started"] is True
    assert result["login_visible"] is False
    assert result["login_hint_text"] == ""
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "session-b",
        "expires_at": FUTURE,
    }


def test_stale_401_cannot_clear_newer_login(js_runner):
    result = js_runner.run(
        action="stale_401_after_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/action": {
                "status": 401,
                "body": {"error": "old session expired"},
                "defer": "action-a",
                "ignore_abort": True,
            },
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["app_started"] is True
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "session-b",
        "expires_at": FUTURE,
    }


def test_stale_logout_completion_cannot_clear_newer_login(js_runner):
    result = js_runner.run(
        action="stale_logout_after_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/logout": {
                "status": 200,
                "body": {"ok": True},
                "defer": "logout-a",
                "ignore_abort": True,
            },
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["app_started"] is True
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "session-b",
        "expires_at": FUTURE,
    }


def test_stale_action_callback_cannot_paint_over_newer_login(js_runner):
    result = js_runner.run(
        action="stale_action_ui_after_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/action": {
                "status": 200,
                "body": {"ok": True},
                "defer": "action-ui-a",
                "ignore_abort": True,
            },
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["app_started"] is True
    assert result["hint_text"] == "session-b-clean"


def test_stale_pipeline_body_completions_are_silent_and_do_not_refetch(js_runner):
    result = js_runner.run(
        action="stale_pipeline_body_after_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/retry-failed": {
                "status": 200, "body": {"reset": 1}, "defer_body": "retry-body-a",
            },
            "/api/clear": {
                "status": 200, "body": {"removed": 1}, "defer_body": "clear-body-a",
            },
            "/api/pause": {
                "status": 200, "body": {"ok": True}, "defer_body": "pause-body-a",
            },
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["hint_text"] == "session-b-pipeline-clean"
    after_login = result["events"][result["events"].index("LOGIN_B_DONE") + 1 :]
    assert "GET /api/jobs" not in after_login


def test_stale_watch_catch_and_finally_are_silent_and_do_not_refetch(js_runner):
    result = js_runner.run(
        action="stale_watch_body_after_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/watches/poll": {
                "status": 200, "body": {"new": 1}, "defer_body": "watch-body-a",
            },
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["watch_hint_text"] == "session-b-watch-clean"
    after_login = result["events"][result["events"].index("LOGIN_B_DONE") + 1 :]
    assert "GET /api/watches" not in after_login
    assert "GET /api/jobs" not in after_login


def test_stale_network_callback_cannot_paint_new_session(js_runner):
    result = js_runner.run(
        action="stale_network_body_after_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/settings": [
                {"status": 200, "body": {"network": {}}},
                {
                    "status": 200,
                    "body": {"network": {"enabled": True}},
                    "defer_body": "network-body-a",
                },
            ],
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["network_status_text"] == "session-b-network-clean"


def test_logout_relogin_resets_speed_test_control_and_stale_finally_is_inert(js_runner):
    result = js_runner.run(
        action="speedtest_logout_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/speedtest": {
                "status": 200,
                "body": {"verdict": {}, "bars": [], "recommendations": []},
                "defer_body": "speed-body-a",
            },
            "/api/logout": {"status": 200, "body": {"ok": True}},
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    marker = next(x for x in result["events"] if x.startswith("SPEED_AFTER_RELOGIN "))
    after_relogin = json.loads(marker.split(" ", 1)[1])
    assert after_relogin == {"disabled": False, "text": "Run speed test"}
    assert result["speed_button"] == {"disabled": False, "text": "Run speed test"}


def test_logout_relogin_restores_every_audited_authenticated_control(js_runner):
    def deferred(name, body):
        return {
            "status": 200, "body": body, "defer": name, "ignore_abort": True,
        }

    result = js_runner.run(
        action="reset_authenticated_controls",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
            "/api/logout": {"status": 200, "body": {"ok": True}},
            "/api/probe": deferred("probe-a", {"ok": True}),
            "/api/pick-folder": deferred("browse-a", {"path": "C:/picked"}),
            "/api/notify-test": deferred("notify-a", {"ok": True}),
            "/api/speedtest": deferred("speed-a", {"verdict": {}, "bars": [], "recommendations": []}),
            "/api/jobs": [
                {"status": 200, "body": []},
                deferred("submit-a", {"id": "job-a"}),
            ],
            "/api/watches": [
                {"status": 200, "body": {"watches": []}},
                deferred("watch-add-a", {"label": "Channel", "pending": [], "auto_queue": False}),
            ],
            "/api/auth": [
                {"status": 200, "body": {}},
                deferred("auth-browser-a", {"ok": True, "status": {}}),
                deferred("auth-login-a", {"ok": True, "status": {}}),
                deferred("auth-test-a", {"ok": True, "status": {}}),
                deferred("auth-import-a", {"ok": True, "status": {}}),
            ],
            "/api/pick-file": [
                deferred("cookie-pick-a", {"path": "C:/cookies.txt"}),
                deferred("archive-pick-a", {"path": "C:/archive.txt"}),
            ],
            "/api/settings/action": deferred("recommendation-a", {"network": {}}),
            "/api/updates/tools": deferred("optional-a", {"ok": True}),
            "/api/updates/ffmpeg": deferred("ffmpeg-a", {"ok": True}),
            "/api/watches/poll": deferred("watch-action-a", {"new": 0}),
        },
    )
    marker = next(x for x in result["events"] if x.startswith("RESTORED_CONTROLS "))
    controls = json.loads(marker.split(" ", 1)[1])
    expected_labels = {
        "probe_res": "Check formats",
        "browse": "Browse…",
        "notify_test": "Send test",
        "st_run": "Run speed test",
        "submit": "Queue job",
        "watch_add": "Start watching",
        "auth-from-browser-btn": "Import from browser",
        "auth-login-btn": "Login with Chrome",
        "auth_test": "Test",
        "auth-import-btn": "Import cookies",
        "cookiefile_browse": "Browse…",
        "download_archive_browse": "Browse…",
        "st-rec": "Enable",
        "optional-tool": "Install",
        "ffmpeg-tool": "Install",
        "watch-row-a": "Check for new",
        "watch-row-b": "Remove",
    }
    assert controls == {
        key: {"disabled": False, "text": label}
        for key, label in expected_labels.items()
    }


def test_stale_a_finally_cannot_release_control_currently_owned_by_b(js_runner):
    result = js_runner.run(
        action="replacement_control_owner",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/logout": {"status": 200, "body": {"ok": True}},
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
            "/api/pick-folder": [
                {
                    "status": 200, "body": {"path": "C:/old"},
                    "defer": "browse-a", "ignore_abort": True,
                },
                {
                    "status": 200, "body": {"path": "C:/new"},
                    "defer": "browse-b", "ignore_abort": True,
                },
            ],
        },
    )
    marker = next(x for x in result["events"] if x.startswith("BROWSE_WHILE_B "))
    assert json.loads(marker.split(" ", 1)[1]) == {
        "disabled": True,
        "text": "opening…",
    }


@pytest.mark.parametrize(
    ("terminal_action", "terminal_text"),
    [
        (
            {
                "kind": "enable_bypass",
                "default_text": "Enable Bypass",
                "default_title": "Enable bypass help",
                "endpoint": "/api/terminal-enable",
            },
            "Bypass enabled ✓",
        ),
        (
            {
                "kind": "set_proxy",
                "default_text": "Save this proxy",
                "default_title": "Save proxy help",
                "endpoint": "/api/terminal-set-proxy",
            },
            "Saved as your proxy ✓",
        ),
        (
            {
                "kind": "use_proxy",
                "default_text": "Use this proxy",
                "default_title": "Use proxy help",
                "endpoint": "/api/terminal-use-proxy",
            },
            "Saved as your proxy ✓",
        ),
        (
            {
                "kind": "install_tool",
                "default_text": "Install node",
                "default_title": "Install node help",
                "endpoint": "/api/terminal-install",
                "payload": {"tool": "node"},
            },
            "Installed ✓",
        ),
        (
            {
                "kind": "future_terminal_action",
                "default_text": "Acknowledge",
                "default_title": "Acknowledge help",
            },
            "OK",
        ),
    ],
)
def test_terminal_recommendation_success_resets_for_login_b(
    js_runner, terminal_action, terminal_text,
):
    endpoint = terminal_action.get("endpoint")
    responses = {
        "/api/logout": {"status": 200, "body": {"ok": True}},
        "/api/login": {
            "status": 200,
            "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
        },
    }
    if endpoint:
        responses[endpoint] = {
            "status": 200,
            "body": (
                {"ok": True, "result": {}, "report": {"info": []}}
                if terminal_action["kind"] == "install_tool"
                else {"network": {}}
            ),
        }
    result = js_runner.run(
        action="terminal_recommendation_relogin",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses=responses,
        terminal_action=terminal_action,
    )
    assert result["terminal_states"] == {
        "session_a_terminal": {
            "disabled": True,
            "text": terminal_text,
            "done_class": True,
            "title": (
                "node ready"
                if terminal_action["kind"] == "install_tool"
                else terminal_action["default_title"]
            ),
        },
        "session_b_default": {
            "disabled": False,
            "text": terminal_action["default_text"],
            "done_class": False,
            "title": terminal_action["default_title"],
        },
    }


def test_terminal_control_commit_survives_finally_and_resets_only_its_owner(js_runner):
    result = js_runner.run(
        action="terminal_control_commit_lifecycle",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/logout": {"status": 200, "body": {"ok": True}},
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
        },
    )
    assert result["terminal_states"] == {
        "after_current_finally": {"disabled": True, "text": "Committed ✓"},
        "after_transition": {"disabled": False, "text": "Default action"},
        "after_stale_release": {"disabled": True, "text": "B working…"},
    }


def test_logout_aborts_session_work_and_cancels_old_delayed_callbacks(js_runner):
    immediate = {"status": 200, "body": {}}
    result = js_runner.run(
        action="cancel_authenticated_work",
        stored={"token": "session-a", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
        responses={
            "/api/login": {
                "status": 200,
                "body": {"ok": True, "token": "session-b", "expires_at": FUTURE},
            },
            "/api/logout": {"status": 200, "body": {"ok": True}},
            "/api/gpu": [
                {"status": 200, "body": {"gpu_name": "CPU", "working_encoders": []}},
                {"status": 200, "body": {}, "defer": "gpu-old"},
            ],
            "/api/jobs": [
                {"status": 200, "body": []},
                {"status": 200, "body": [], "defer": "jobs-old"},
            ],
            "/api/watches": [
                {"status": 200, "body": {"watches": []}},
                {"status": 200, "body": {"watches": []}, "defer": "watches-old"},
            ],
            "/api/updates": [
                immediate,
                {"status": 200, "body": {}, "defer": "updates-old"},
            ],
            "/api/action": {"status": 200, "body": {}, "defer": "action-old"},
            "/api/speedtest": {"status": 200, "body": {}, "defer": "speed-old"},
            "/api/settings": [
                immediate,
                {"status": 200, "body": {}, "defer": "settings-old"},
            ],
        },
    )
    for path in (
        "/api/gpu", "/api/jobs", "/api/watches", "/api/updates",
        "/api/action", "/api/speedtest", "/api/settings",
    ):
        calls = [
            call for call in result["fetchCalls"]
            if call["path"] == path and _header(call, "X-Token") == "session-a"
        ]
        assert calls and calls[-1]["aborted"] is True, (path, calls)
    after_login = result["events"][result["events"].index("LOGIN_B_DONE") + 1 :]
    assert "POST /api/updates" not in after_login, after_login
    assert json.loads(result["storage"][SESSION_KEY]) == {
        "token": "session-b",
        "expires_at": FUTURE,
    }


def test_bespoke_authenticated_transports_use_current_session_and_safe_headers(js_runner):
    result = js_runner.run(
        action="transport_inventory",
        stored={"token": "abc", "expires_at": FUTURE},
        response={"status": 200, "body": {"ok": True, "expires_at": FUTURE}},
    )
    paths = {call["path"] for call in result["fetchCalls"]}
    assert {
        "/api/session", "/api/gpu", "/api/jobs", "/api/watches", "/api/updates",
        "/api/updates/tools", "/api/updates/ffmpeg", "/api/action",
        "/api/headers-instance", "/api/headers-mixed",
    } <= paths
    protected = [call for call in result["fetchCalls"] if call["path"] != "/api/login"]
    assert protected
    assert all(_header(call, "X-Token") == "abc" for call in protected), protected

    instance = next(call for call in protected if call["path"] == "/api/headers-instance")
    mixed = next(call for call in protected if call["path"] == "/api/headers-mixed")
    assert _header(instance, "X-Custom") == "keep-a"
    assert _header(mixed, "X-Custom") == "keep-b"
    assert sum(key.lower() == "x-token" for key in instance["headers"]) == 1
    assert sum(key.lower() == "x-token" for key in mixed["headers"]) == 1
    assert "stale-a" not in instance["headers"].values()
    assert "stale-b" not in mixed["headers"].values()
