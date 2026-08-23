// yt_final ingest console — vanilla, no build step.
const $ = (id) => document.getElementById(id);

// Password login creates a server-revocable, expiring session. The browser stores only the token
// and its Unix expiry in a versioned localStorage record. Users never copy credentials into URLs;
// authenticated requests send the current token as X-Token.
// Poller / lifecycle state. Declared UP HERE on purpose: activateView() runs at module level during
// boot (to apply the #hash) and calls syncWatchPolling(), which reads appStarted/watchTimer. With
// the declaration further down those were in the temporal dead zone, so that call threw a
// ReferenceError and ABORTED the whole script — the page loaded dead.
let jobsTimer = null, watchTimer = null, clockTimer = null, appStarted = false;
let _currentView = 'workspace';
// Which view is showing; the Watch poller only runs while its own panel is visible. Declared here
// for the same TDZ reason as the timers above — activateView() reads it during module-level boot.
const SESSION_KEY = 'yt_final_web_session_v2';
const LEGACY_TOKEN_KEY = 'yt_final_web_token';
const SESSION_STORAGE_RETRY_MESSAGE = 'Browser storage temporarily unavailable — try again';
let TOKEN = '';
let SESSION_EXPIRES_AT = 0;
let SESSION_GENERATION = 0;
let SESSION_CONTROLLER = null;
let LOGIN_FOCUS_TIMER = null;
let LOGIN_ATTEMPT_GENERATION = 0;
let LOGIN_ATTEMPT_CONTROLLER = null;
const AUTH_TIMEOUTS = new Set();
const AUTH_INTERVALS = new Set();
const AUTH_FRAMES = new Set();
const AUTH_CONTROL_OWNERS = new Map();
const AUTH_NATIVE_FILE_PICKERS = new Map();
const SESSION_RESPONSE_CONTEXT = new WeakMap();

function cancelAuthenticatedWork() {
  if (SESSION_CONTROLLER) {
    try { SESSION_CONTROLLER.abort(); } catch (e) { /* ignore */ }
    SESSION_CONTROLLER = null;
  }
  AUTH_TIMEOUTS.forEach((id) => clearTimeout(id));
  AUTH_INTERVALS.forEach((id) => clearInterval(id));
  AUTH_FRAMES.forEach((id) => cancelAnimationFrame(id));
  AUTH_TIMEOUTS.clear();
  AUTH_INTERVALS.clear();
  AUTH_FRAMES.clear();
  AUTH_NATIVE_FILE_PICKERS.clear();
}

function beginSessionWork(token, expiresAt) {
  cancelAuthenticatedWork();
  SESSION_GENERATION += 1;
  SESSION_CONTROLLER = new AbortController();
  TOKEN = token;
  SESSION_EXPIRES_AT = expiresAt;
}

function currentSessionContext() {
  return {
    generation: SESSION_GENERATION,
    token: TOKEN,
    signal: SESSION_CONTROLLER && SESSION_CONTROLLER.signal,
  };
}

function sessionContextIsCurrent(context) {
  return !!context && !!TOKEN && !!SESSION_CONTROLLER
    && context.generation === SESSION_GENERATION
    && context.token === TOKEN
    && !SESSION_CONTROLLER.signal.aborted;
}

function staleSessionError() {
  const error = new Error('stale session work');
  error.name = 'AbortError';
  return error;
}

function assertCurrentSession(context) {
  if (!sessionContextIsCurrent(context)) throw staleSessionError();
}

/**
 * Own every continuation spawned by one authenticated UI action.
 *
 * A transport-level check is not sufficient: response-body parsing resolves in its own microtask,
 * and logout/re-login can replace the session before a handler's success/catch/finally continuation
 * runs. Capture this once before the first await, guard before every effect or follow-up request,
 * and keep stale/aborted failures silent.
 */
function beginAuthenticatedAction() {
  const context = currentSessionContext();
  assertCurrentSession(context);
  return {
    context,
    isCurrent: () => sessionContextIsCurrent(context),
    guard: () => assertCurrentSession(context),
    isSilentError: (error) => !sessionContextIsCurrent(context)
      || !!(error && error.name === 'AbortError'),
    effect(callback) {
      if (!sessionContextIsCurrent(context)) return false;
      callback();
      return true;
    },
    ownControl(element, options = {}) {
      return ownAuthenticatedControl(context, element, options);
    },
  };
}

function openAuthenticatedFilePicker(input, openPicker) {
  const action = beginAuthenticatedAction();
  AUTH_NATIVE_FILE_PICKERS.set(input, action);
  input.value = '';
  try {
    openPicker();
  } catch (error) {
    if (AUTH_NATIVE_FILE_PICKERS.get(input) === action) {
      AUTH_NATIVE_FILE_PICKERS.delete(input);
    }
    throw error;
  }
}

function takeAuthenticatedFilePickerAction(input) {
  const action = AUTH_NATIVE_FILE_PICKERS.get(input);
  AUTH_NATIVE_FILE_PICKERS.delete(input);
  if (!action || !action.isCurrent()) {
    input.value = '';
    return null;
  }
  return action;
}

function ownAuthenticatedControl(context, element, options = {}) {
  if (!element) return { release: () => false, commit: () => false };
  assertCurrentSession(context);
  const previousOwner = AUTH_CONTROL_OWNERS.get(element);
  const owner = {
    context,
    element,
    disabled: previousOwner ? previousOwner.disabled : !!element.disabled,
    text: previousOwner ? previousOwner.text : element.textContent,
    doneClass: previousOwner
      ? previousOwner.doneClass
      : !!(element.classList && element.classList.contains('is-done')),
    title: previousOwner ? previousOwner.title : element.title,
    committed: false,
  };
  AUTH_CONTROL_OWNERS.set(element, owner);
  if (options.disable !== false) element.disabled = true;
  if (Object.prototype.hasOwnProperty.call(options, 'text')) element.textContent = options.text;
  const matches = () => AUTH_CONTROL_OWNERS.get(element) === owner;
  return {
    release() {
      if (!matches() || owner.committed || !sessionContextIsCurrent(context)) return false;
      AUTH_CONTROL_OWNERS.delete(element);
      restoreAuthenticatedControl(owner);
      return true;
    },
    commit(callback) {
      if (!matches() || owner.committed || !sessionContextIsCurrent(context)) return false;
      callback();
      owner.committed = true;
      return true;
    },
  };
}

function restoreAuthenticatedControl(owner) {
  const element = owner.element;
  element.disabled = owner.disabled;
  element.textContent = owner.text;
  if (element.classList) element.classList.toggle('is-done', owner.doneClass);
  element.title = owner.title;
}

function resetAuthenticatedControls() {
  AUTH_CONTROL_OWNERS.forEach((owner) => restoreAuthenticatedControl(owner));
  AUTH_CONTROL_OWNERS.clear();
}

function authenticatedTimeout(callback, delayMs) {
  const context = currentSessionContext();
  if (!sessionContextIsCurrent(context)) return null;
  const id = setTimeout(() => {
    AUTH_TIMEOUTS.delete(id);
    if (!sessionContextIsCurrent(context)) return;
    callback();
  }, delayMs);
  AUTH_TIMEOUTS.add(id);
  return id;
}

function clearAuthenticatedTimeout(id) {
  if (id == null) return;
  clearTimeout(id);
  AUTH_TIMEOUTS.delete(id);
}

function authenticatedInterval(callback, delayMs) {
  const context = currentSessionContext();
  if (!sessionContextIsCurrent(context)) return null;
  const id = setInterval(() => {
    if (sessionContextIsCurrent(context)) callback();
  }, delayMs);
  AUTH_INTERVALS.add(id);
  return id;
}

function clearAuthenticatedInterval(id) {
  if (id == null) return;
  clearInterval(id);
  AUTH_INTERVALS.delete(id);
}

function authenticatedAnimationFrame(callback) {
  const context = currentSessionContext();
  if (!appStarted || !sessionContextIsCurrent(context)) return null;
  const id = requestAnimationFrame(() => {
    AUTH_FRAMES.delete(id);
    if (appStarted && sessionContextIsCurrent(context)) callback();
  });
  AUTH_FRAMES.add(id);
  return id;
}

function authenticatedDelay(delayMs, context) {
  return new Promise((resolve, reject) => {
    if (!sessionContextIsCurrent(context)) { reject(staleSessionError()); return; }
    const id = authenticatedTimeout(() => {
      if (sessionContextIsCurrent(context)) resolve();
      else reject(staleSessionError());
    }, delayMs);
    context.signal.addEventListener('abort', () => {
      clearAuthenticatedTimeout(id);
      reject(staleSessionError());
    }, { once: true });
  });
}

function combinedAbortSignal(...signals) {
  const usable = signals.filter((signal) => signal && typeof signal.addEventListener === 'function');
  if (!usable.length) return signals.find(Boolean);
  if (usable.length === 1) return usable[0];
  if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.any === 'function') {
    return AbortSignal.any(usable);
  }
  const controller = new AbortController();
  const subscriptions = [];
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    subscriptions.splice(0).forEach((signal) => {
      try { signal.removeEventListener('abort', abort); } catch (e) { /* best effort */ }
    });
    MANUAL_ABORT_SIGNAL_CLEANUPS.delete(controller.signal);
  };
  const abort = () => {
    try {
      if (!controller.signal.aborted) controller.abort();
    } finally {
      cleanup();
    }
  };
  MANUAL_ABORT_SIGNAL_CLEANUPS.set(controller.signal, cleanup);
  for (const signal of usable) {
    if (controller.signal.aborted) break;
    if (signal.aborted) {
      abort();
      break;
    }
    subscriptions.push(signal);
    try {
      signal.addEventListener('abort', abort, { once: true });
    } catch (error) {
      cleanup();
      throw error;
    }
    if (cleaned) {
      try { signal.removeEventListener('abort', abort); } catch (e) { /* best effort */ }
      break;
    }
    if (signal.aborted) {
      abort();
      break;
    }
  }
  return controller.signal;
}

const MANUAL_ABORT_SIGNAL_CLEANUPS = new WeakMap();

function cleanupCombinedAbortSignal(signal) {
  const cleanup = signal && MANUAL_ABORT_SIGNAL_CLEANUPS.get(signal);
  if (cleanup) cleanup();
}

function abortReason(signal) {
  const reason = signal && signal.reason;
  return reason instanceof Error
    ? reason
    : new DOMException('The operation was aborted', 'AbortError');
}

/**
 * Keep one deadline alive for every await performed by an operation.
 *
 * External cancellation is delivered through operationSignal and keeps the transport's normal
 * abort semantics. The deadline also rejects independently, so a non-conforming response body
 * cannot leave the operation pending forever after its headers arrived.
 */
async function runWithDeadline(operation, options = {}, timeoutMs = 30_000) {
  let fallbackTimer = null;
  let deadlineSignal;
  if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
    deadlineSignal = AbortSignal.timeout(timeoutMs);
  } else {
    const deadlineController = new AbortController();
    deadlineSignal = deadlineController.signal;
    fallbackTimer = setTimeout(() => deadlineController.abort(), timeoutMs);
  }
  const operationSignal = combinedAbortSignal(options.signal, deadlineSignal);
  let rejectDeadline = null;
  const deadlineFailure = new Promise((resolve, reject) => {
    rejectDeadline = () => reject(abortReason(deadlineSignal));
    if (deadlineSignal.aborted) rejectDeadline();
    else deadlineSignal.addEventListener('abort', rejectDeadline, { once: true });
  });
  try {
    let operationPromise;
    try {
      operationPromise = Promise.resolve(operation(operationSignal));
    } catch (error) {
      operationPromise = Promise.reject(error);
    }
    return await Promise.race([
      operationPromise,
      deadlineFailure,
    ]);
  } finally {
    if (rejectDeadline) {
      try { deadlineSignal.removeEventListener('abort', rejectDeadline); } catch (e) { /* ignore */ }
    }
    cleanupCombinedAbortSignal(operationSignal);
    if (fallbackTimer != null) clearTimeout(fallbackTimer);
  }
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 30_000) {
  return runWithDeadline(
    (signal) => fetch(url, Object.assign({}, options, { signal })),
    { signal: options.signal },
    timeoutMs,
  );
}

function invalidateLoginAttempt() {
  LOGIN_ATTEMPT_GENERATION += 1;
  const controller = LOGIN_ATTEMPT_CONTROLLER;
  LOGIN_ATTEMPT_CONTROLLER = null;
  if (controller) {
    try { controller.abort(); } catch (e) { /* ignore */ }
  }
  const submit = $('login-submit');
  if (submit) submit.disabled = false;
}

/** Remove both the current session and the retired permanent-token key. */
function clearSession() {
  invalidateLoginAttempt();
  cancelAuthenticatedWork();
  SESSION_GENERATION += 1;
  TOKEN = '';
  SESSION_EXPIRES_AT = 0;
  try {
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(LEGACY_TOKEN_KEY);
  } catch (e) { /* storage may be unavailable */ }
}

function sessionExpiryIsValid(expiresAt) {
  return typeof expiresAt === 'number'
    && Number.isFinite(expiresAt)
    && expiresAt > Date.now() / 1000
    && expiresAt < 10_000_000_000;
}

/**
 * Load and validate the versioned session record. localStorage is the authority at request time:
 * malformed, incomplete, or expired values are deleted before they can reach a request header.
 */
function loadSession() {
  try { localStorage.removeItem(LEGACY_TOKEN_KEY); } catch (e) { /* ignore */ }
  let raw = null;
  try { raw = localStorage.getItem(SESSION_KEY); } catch (e) {
    clearSession();
    return null;
  }
  if (!raw) {
    TOKEN = '';
    SESSION_EXPIRES_AT = 0;
    return null;
  }
  try {
    const record = JSON.parse(raw);
    const keys = record && typeof record === 'object' && !Array.isArray(record)
      ? Object.keys(record) : [];
    const valid = keys.length === 2
      && Object.prototype.hasOwnProperty.call(record, 'token')
      && Object.prototype.hasOwnProperty.call(record, 'expires_at')
      && keys.includes('token') && keys.includes('expires_at')
      && typeof record.token === 'string' && record.token.length > 0
      && sessionExpiryIsValid(record.expires_at);
    if (!valid) {
      clearSession();
      return null;
    }
    if (!SESSION_CONTROLLER || TOKEN !== record.token || SESSION_EXPIRES_AT !== record.expires_at) {
      beginSessionWork(record.token, record.expires_at);
    }
    return { token: TOKEN, expires_at: SESSION_EXPIRES_AT };
  } catch (e) {
    clearSession();
    return null;
  }
}

/** Persist exactly the server-issued token and numeric Unix expiry. */
function setSession(token, expiresAt) {
  const expiry = expiresAt;
  if (typeof token !== 'string' || token.length === 0
      || !sessionExpiryIsValid(expiry)) {
    return false;
  }
  const record = JSON.stringify({ token, expires_at: expiry });
  try {
    localStorage.setItem(SESSION_KEY, record);
  } catch (e) {
    return false;
  }
  try { localStorage.removeItem(LEGACY_TOKEN_KEY); } catch (e) { /* best effort */ }
  beginSessionWork(token, expiry);
  return true;
}

loadSession();
// Retired permanent credentials may remain in old bookmarks. Strip them without ever promoting
// them into request headers or browser storage; preserve unrelated query state and the view hash.
(function discardUrlToken() {
  const params = new URLSearchParams(location.search);
  if (!params.has('token')) return;
  params.delete('token');
  const query = params.toString();
  try {
    history.replaceState(
      null,
      '',
      location.pathname + (query ? '?' + query : '') + (location.hash || '') || '/',
    );
  } catch (e) { /* ignore */ }
})();

function showApp() {
  if (LOGIN_FOCUS_TIMER != null) {
    clearTimeout(LOGIN_FOCUS_TIMER);
    LOGIN_FOCUS_TIMER = null;
  }
  document.body.classList.remove('is-locked');
  const gate = $('login-gate');
  const root = $('app-root');
  if (gate) gate.hidden = true;
  if (root) root.hidden = false;
}

function setLoginFeedback(message = '', severity = 'status') {
  const node = $('login-msg');
  if (!node) return false;
  const nextSeverity = severity === 'alert' ? 'alert' : 'status';
  const nextMessage = String(message == null ? '' : message);
  const severityChanged = node.getAttribute('role') !== nextSeverity;
  const messageChanged = node.textContent !== nextMessage;
  if (!severityChanged && !messageChanged) return false;

  // Assistive technology must learn the urgency before the new text is announced.
  if (severityChanged) {
    node.setAttribute('role', nextSeverity);
    node.setAttribute('aria-live', nextSeverity === 'alert' ? 'assertive' : 'polite');
    node.classList.toggle('bad', nextSeverity === 'alert');
    node.classList.remove('ok');
  }
  if (messageChanged) node.textContent = nextMessage;
  return true;
}

function showLogin(message = '', severity = 'status') {
  document.body.classList.add('is-locked');
  const gate = $('login-gate');
  const root = $('app-root');
  if (gate) gate.hidden = false;
  if (root) root.hidden = true;
  setLoginFeedback(message, severity);
  const pw = $('login-password');
  if (pw) {
    pw.value = '';
    if (LOGIN_FOCUS_TIMER != null) clearTimeout(LOGIN_FOCUS_TIMER);
    LOGIN_FOCUS_TIMER = setTimeout(() => {
      LOGIN_FOCUS_TIMER = null;
      if (document.body.classList.contains('is-locked')) pw.focus();
    }, 50);
  }
}

/**
 * quitHint — how to fully quit the browser whose cookies are being imported.
 *
 * This was derived from the VIEWER's user agent, which is the wrong machine. The browser being quit
 * runs on the HOST, and this console is routinely opened from another device on the LAN — so a
 * Windows host viewed from a Mac told the user "Cmd+Q", while the wording that actually matters on
 * Windows is the one about a lingering tray/background instance, which is precisely what breaks the
 * cookie import there. The mis-targeted hint steered users into the exact failure it exists to prevent.
 *
 * Pure function of the os string (HOST_OS, server-reported) so the branches can be executed. Falsy os
 * yields neutral wording rather than a guess — same never-wrong-only-vaguer discipline as
 * hostDeviceLabel(). Function statements, not consts: these are read from code that runs during boot,
 * and a temporal-dead-zone error at module level takes the whole page down.
 */
function quitHint(os) {
  const s = String(os || '');
  if (!s) return 'quit it fully (close every window, and any background instance)';
  if (/mac|darwin|ios/i.test(s)) return 'Cmd+Q';
  return 'close all its windows, and quit any tray/background instance';
}
/** Qualified form for prose that previously named the platform explicitly. */
function quitHintQualified(os) {
  const s = String(os || '');
  if (/mac|darwin|ios/i.test(s)) return 'macOS: Cmd+Q';
  return quitHint(s);
}

/**
 * Tear the app back down to the login gate after the session is gone.
 *
 * appStarted MUST be cleared: startApp() begins with `if (appStarted) return;`, so without this a
 * successful re-login stored the new token and then never called showApp() — the gate stayed up,
 * the password looked accepted, and only a page reload recovered. The pollers must stop too, or
 * they keep hammering /api/jobs with an empty token behind the gate.
 * Declared as a function so it is hoisted above its callers (api(), handlePollError).
 */
function sessionLost(message = 'Session expired — sign in again', severity = 'alert') {
  try { stopPolling(); } catch (e) { /* not started yet */ }
  try { stopClock(); } catch (e) { /* not started yet */ }
  try { resetAuthenticatedUiState(); } catch (e) { /* not started yet */ }
  appStarted = false;
  clearSession();
  showLogin(message, severity);
}

async function sessionFetch(path, options = {}, beforeSessionLoss = null) {
  const session = loadSession();
  if (!session) {
    sessionLost('Session expired — sign in again');
    const err = new Error('Session expired — sign in again');
    err.status = 401;
    throw err;
  }
  const context = currentSessionContext();
  const fetchOptions = Object.assign({}, options);
  const timeoutMs = fetchOptions.timeoutMs;
  delete fetchOptions.timeoutMs;
  const headers = new Headers(fetchOptions.headers || {});
  headers.set('X-Token', context.token);
  const signal = combinedAbortSignal(context.signal, fetchOptions.signal);
  const requestOptions = Object.assign({}, fetchOptions, { headers, signal });
  let res;
  try {
    res = timeoutMs != null
      ? await fetchWithTimeout(path, requestOptions, timeoutMs)
      : await fetch(path, requestOptions);
  } finally {
    cleanupCombinedAbortSignal(signal);
  }
  assertCurrentSession(context);
  if (res.status === 401) {
    if (typeof beforeSessionLoss === 'function') beforeSessionLoss();
    sessionLost('Session expired — sign in again');
    const error = new Error('Session expired — sign in again');
    error.status = 401;
    throw error;
  }
  SESSION_RESPONSE_CONTEXT.set(res, context);
  return res;
}

async function sessionJson(response) {
  const context = SESSION_RESPONSE_CONTEXT.get(response);
  assertCurrentSession(context);
  const value = await response.json();
  assertCurrentSession(context);
  return value;
}

async function sessionText(response) {
  const context = SESSION_RESPONSE_CONTEXT.get(response);
  assertCurrentSession(context);
  const value = await response.text();
  assertCurrentSession(context);
  return value;
}

async function api(path, opts = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  const res = await sessionFetch(path, Object.assign({}, opts, { headers }));
  const ct = res.headers.get('content-type') || '';
  let body = null;
  try {
    body = ct.includes('json') ? await sessionJson(res) : await sessionText(res);
  } catch (e) {
    if (!res.ok) throw new Error(res.statusText || ('HTTP ' + res.status));
    throw e;
  }
  if (!res.ok) {
    if (res.status === 401) throw new Error('Session expired — sign in again');
    const msg = (body && typeof body === 'object' && body.error) ? body.error
      : (typeof body === 'string' && body) ? body
      : (res.statusText || ('HTTP ' + res.status));
    throw new Error(msg);
  }
  return body;
}

// ---- boot: GPU + profiles ----
function applyGpuReport(gpu) {
  const gEl = $('gpu');
  const eEl = $('encoders');
  if (!gEl || !eEl) return;
  if (!gpu || gpu.status === 'initialising') {
    gEl.textContent = 'probing…';
    eEl.textContent = '—';
    gEl.title = 'Detecting graphics and hardware encoders…';
    return false;
  }
  const name = gpu.gpu_name || 'cpu only';
  const encs = (gpu.working_encoders || []).filter(Boolean);
  gEl.textContent = name;
  gEl.title = name;
  eEl.textContent = encs.length ? encs.join(' · ') : 'software / cpu only';
  eEl.title = encs.length ? encs.join(', ') : 'No hardware encoders detected — re-encode will use CPU';
  annotateProfiles(new Set(encs));
  return true;
}

/** Grey out re-encode profiles whose HW encoder isn't in this machine's working set. Auto + software
 *  (libx264) profiles stay enabled. Runs after the (token-gated) GPU probe reports working_encoders. */
function annotateProfiles(working) {
  const HW = ['nvenc', '_amf', '_qsv', 'videotoolbox'];
  ['profile', 'watch_profile'].forEach((selId) => {
    const sel = $(selId);
    if (!sel) return;
    Array.from(sel.options).forEach((opt) => {
      const vc = opt.getAttribute('data-vc') || '';
      const isHw = HW.some((t) => vc.includes(t));
      const usable = !isHw || working.has(vc);
      const base = opt.getAttribute('data-base') || opt.textContent;
      opt.setAttribute('data-base', base);
      opt.disabled = !usable;
      opt.textContent = usable ? base : base + ' (unavailable on this machine)';
    });
    // If the current selection just became unavailable, fall back to auto_h264 when present.
    const cur = sel.selectedOptions[0];
    if (cur && cur.disabled && Array.from(sel.options).some((o) => o.value === 'auto_h264' && !o.disabled)) {
      sel.value = 'auto_h264';
    }
  });
}

/** Poll /api/gpu until the background engine warm-up finishes (or give up). */
async function loadGpuStatus({ attempts = 24, delayMs = 400 } = {}) {
  const action = beginAuthenticatedAction();
  const context = action.context;
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await sessionFetch('/api/gpu');
      let body = null;
      try { body = await sessionJson(res); } catch (e) {
        if (!action.isCurrent()) return;
        body = null;
      }
      action.guard();
      if (res.status === 202 || (body && body.status === 'initialising')) {
        applyGpuReport({ status: 'initialising' });
        await authenticatedDelay(delayMs, context);
        continue;
      }
      if (!res.ok) {
        if (res.status === 401) {
          if ($('gpu')) $('gpu').textContent = 'sign in to probe';
          if ($('encoders')) $('encoders').textContent = '—';
          return;
        }
        throw new Error((body && body.error) || res.statusText || 'gpu probe failed');
      }
      if (applyGpuReport(body)) return;
    } catch (e) {
      if (action.isSilentError(e)) return;
      if (i === attempts - 1) {
        if ($('gpu')) $('gpu').textContent = 'n/a';
        if ($('encoders')) $('encoders').textContent = '—';
      }
      await authenticatedDelay(delayMs, context);
    }
  }
  if ($('gpu') && ($('gpu').textContent === 'probing…')) {
    $('gpu').textContent = 'probe timed out';
    if ($('encoders')) $('encoders').textContent = '—';
  }
}

async function boot() {
  const action = beginAuthenticatedAction();
  let profileIds = new Set();
  try {
    const profs = await api('/api/profiles');
    action.guard();
    $('profile').innerHTML = profs.map(p => `<option value="${p.id}" data-vc="${p.video_codec || ''}">${p.id}</option>`).join('');
    profileIds = new Set((profs || []).map((p) => p.id));
  } catch (e) {
    if (action.isSilentError(e)) return;
    /* offline */
  }
  if (!action.isCurrent()) return;
  // Fire-and-forget GPU poll so profiles/health are not blocked on the ~few-second probe.
  loadGpuStatus();
  try {
    const h = await api('/api/health');
    action.guard();
    applyHostOs(h);
    applyTransport(h);
    // auto_h264 (the new semantic default) picks the fastest working H.264 per machine, so the UI no
    // longer needs to guess from HOST_OS. Fall back to concrete profiles only against an older server.
    const ids = profileIds;
    let def = 'auto_h264';
    const isMac = HOST_OS === 'macOS' || HOST_OS === 'darwin' || /mac/i.test(String(HOST_OS || ''));
    if (!ids.has(def)) {
      if (isMac && ids.has('vt_h264')) def = 'vt_h264';
      else if (ids.has('premiere_h264_fast')) def = 'premiere_h264_fast';
      else if (ids.has('vt_h264')) def = 'vt_h264';
      else if (ids.has('premiere_h264_cpu')) def = 'premiere_h264_cpu';
    }
    if ($('profile') && ids.has(def)) $('profile').value = def;
    if (h.output_dir) {
      $('output_dir').placeholder = h.output_dir;
      // Pre-fill the default folder so it's the visible starting value; it stays unless YOU change it.
      if (!$('output_dir').value) $('output_dir').value = h.output_dir;
    }
  } catch (e) {
    if (action.isSilentError(e)) return;
    applyHostOs({ host_os: null });
  }
}

// ---- view navigation (Workspace primary; Watch / Auth / Guide secondary) ----
function activateView(viewName, { syncHash = true, focus = false } = {}) {
  const views = ['workspace', 'watch', 'auth', 'speedtest', 'updates', 'guide'];
  const name = views.includes(viewName) ? viewName : 'workspace';
  let selectedTab = null;
  views.forEach((v) => {
    const panel = $('view-' + v);
    const tab = $('nav-' + v);
    if (!panel || !tab) return;
    const on = v === name;
    panel.hidden = !on;
    panel.classList.toggle('is-active', on);
    tab.classList.toggle('is-active', on);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
    tab.tabIndex = on ? 0 : -1;
    if (on) selectedTab = tab;
  });
  if (focus && selectedTab) {
    selectedTab.focus();
    selectedTab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  // Keep any remaining non-credential query state intact — only swap the fragment.
  if (syncHash) {
    try {
      const next = location.pathname + location.search + '#' + name;
      if (location.pathname + location.search + location.hash !== next) {
        history.replaceState(null, '', next);
      }
    } catch (e) { /* ignore */ }
  }
  runAuthenticatedViewEffects(name);
  // Watch polling is only useful while the Watch panel can be seen. It used to run every 5s for the
  // whole session regardless of view — the same waste the visibilitychange handler already avoids for
  // a hidden TAB, just applied to in-app view changes. Switching TO Watch refreshes immediately so
  // the panel is never stale.
  if (typeof syncWatchPolling === 'function') syncWatchPolling(name);
}

function handleTablistKeydown(event) {
  const tablist = event && event.currentTarget;
  if (!tablist || typeof tablist.matches !== 'function' || !tablist.matches('.app-nav')) return;
  const target = event.target && typeof event.target.closest === 'function'
    ? event.target.closest('.nav-tab[data-view]')
    : null;
  if (!target || !tablist.contains(target)) return;
  const tabs = Array.from(tablist.querySelectorAll('.nav-tab[data-view]'));
  const index = tabs.indexOf(target);
  if (index < 0 || !tabs.length) return;
  let nextIndex = null;
  if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
  else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
  else if (event.key === 'Home') nextIndex = 0;
  else if (event.key === 'End') nextIndex = tabs.length - 1;
  if (nextIndex == null) return;
  event.preventDefault();
  const next = tabs[nextIndex];
  activateView(next.dataset.view, { focus: true });
}

function runAuthenticatedViewEffects(name) {
  if (!appStarted) return;
  if (name === 'updates' && typeof refreshUpdates === 'function') {
    // Defer one frame so the view paints before the (possibly network) scan starts.
    authenticatedAnimationFrame(() => refreshUpdates({ quiet: true }));
  }
  if (name === 'guide') playGuideEnter();
}

function playGuideEnter() {
  const sc = $('guide_scroll');
  if (!sc) return;
  sc.classList.remove('is-ready');
  // Restart CSS staggered reveal
  void sc.offsetWidth;
  authenticatedAnimationFrame(() => sc.classList.add('is-ready'));
}

/**
 * reduceMotion — does this user ask for less animation?
 *
 * The stylesheet already honours prefers-reduced-motion, but a JS-supplied `behavior: 'smooth'`
 * overrides CSS entirely, so the two remaining programmatic scrolls ignored that preference. Function
 * statement (hoisted) and defensive: matchMedia is absent in some embedded webviews, and throwing here
 * during boot would take the whole script down.
 */
function reduceMotion() {
  try { return !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches); }
  catch (_) { return false; }
}

// Field guide TOC: scroll inside guide_scroll; open matching Advanced panel if needed
function scrollGuideTo(id) {
  const sc = $('guide_scroll');
  const el = id ? document.getElementById(id) : null;
  if (!sc || !el) return;
  // Open <details> if the target is a collapsed panel (or inside one)
  let d = el.closest('details.guide-panel');
  if (!d && el.matches && el.matches('details.guide-panel')) d = el;
  if (d) d.open = true;
  const top = el.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop - 8;
  sc.scrollTo({ top: Math.max(0, top), behavior: reduceMotion() ? 'auto' : 'smooth' });
  // Active chip highlight
  sc.querySelectorAll('.guide-toc-link').forEach((a) => {
    a.classList.toggle('is-active', a.getAttribute('href') === '#' + id);
  });
}
document.querySelectorAll('.guide-toc-link[href^="#"]').forEach((a) => {
  a.addEventListener('click', (e) => {
    const href = a.getAttribute('href') || '';
    const id = href.replace(/^#/, '');
    if (!id || !document.getElementById(id)) return;
    e.preventDefault();
    scrollGuideTo(id);
  });
});
document.querySelectorAll('.nav-tab[data-view]').forEach((btn) => {
  btn.addEventListener('click', () => activateView(btn.dataset.view, { focus: true }));
});
const appNav = document.querySelector('.app-nav');
if (appNav) appNav.addEventListener('keydown', handleTablistKeydown);
// deep-link + browser back/forward on #watch | #auth | #updates | #guide | #workspace
function viewFromHash() {
  const h = (location.hash || '').replace(/^#/, '').toLowerCase();
  return ['workspace', 'watch', 'auth', 'speedtest', 'updates', 'guide'].includes(h) ? h : 'workspace';
}
window.addEventListener('hashchange', () => activateView(viewFromHash(), { syncHash: false }));
activateView(viewFromHash(), { syncHash: true });

// ---- compose ----
function setAdv(open) {
  const a = $('adv'); if (!a) return;
  a.hidden = !open;
  $('advToggle').textContent = (open ? '▾' : '▸') + ' Advanced · cookies · clip · channels · audio · editors · speed';
  $('advToggle').setAttribute('aria-expanded', open ? 'true' : 'false');
}
$('advToggle').onclick = () => setAdv(!!($('adv') && $('adv').hidden));
// Esc closes expanded panels (render-only: advanced drawer first, then any open <details>).
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || e.defaultPrevented) return;
  // Innermost first: the probe popover is a dialog, so Esc must close IT rather than yanking the
  // whole Advanced drawer shut from underneath the user (closeProbePop also restores focus).
  if (probePopOpen()) { closeProbePop(); return; }
  const a = $('adv');
  if (a && !a.hidden) {
    // Only collapse the drawer when focus is not inside it. Collapsing a container that owns focus
    // destroys the focus position (it lands on <body>), which made Esc hostile while typing in an
    // advanced field. If focus IS inside, step it out to the toggle and leave the drawer open.
    const active = document.activeElement;
    if (active && a.contains(active)) {
      const t = $('advToggle');
      if (t) { try { t.focus({ preventScroll: true }); } catch (_) { /* ignore */ } }
      return;
    }
    setAdv(false);
    return;
  }
  // Scope to the VISIBLE view: document.querySelector picked the first open <details> in document
  // order, which is usually off-screen in a hidden view (the Field guide has several), so Esc
  // appeared to do nothing while silently collapsing something the user could not see. And if focus
  // is inside the one we close, step it to that section's <summary> first — collapsing a container
  // that owns focus drops focus to <body>.
  const view = document.querySelector('.view.is-active') || document;
  const openDetails = view.querySelector('details[open]');
  if (openDetails) {
    const active = document.activeElement;
    if (active && openDetails.contains(active)) {
      const summary = openDetails.querySelector('summary');
      if (canTakeFocus(summary)) { try { summary.focus({ preventScroll: true }); } catch (_) { /* ignore */ } }
    }
    openDetails.open = false;
  }
});
function toggleProfile() {
  const wrap = $('profile_wrap');
  const re = $('encode_mode').value === 'reencode';
  if (!wrap) return;
  wrap.hidden = !re;
  const sel = $('profile');
  if (sel) sel.disabled = !re;
  updateComposeSummary();
}
/** Parse MM:SS / HH:MM:SS / seconds (mirrors server _time_to_seconds loosely). */
function timeToSeconds(t) {
  const s = String(t || '').trim();
  if (!s) return null;
  const parts = s.split(':');
  if (parts.length > 3) return null;
  const nums = parts.map((p) => Number(p));
  if (nums.some((n) => !Number.isFinite(n) || n < 0)) return null;
  if (parts.length >= 2 && nums[nums.length - 1] >= 60) return null;
  if (parts.length === 3 && nums[1] >= 60) return null;
  return nums.reduce((acc, n) => acc * 60 + n, 0);
}

function formatClipSec(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/**
 * Sync Start/End → hidden #section, validate, update status + summary.
 * Returns { ok, section, error } for submit gating.
 */
function syncClipRange() {
  const startEl = $('section_start');
  const endEl = $('section_end');
  const hidden = $('section');
  const status = $('clip_status');
  const hint = $('clip_summary_hint');
  const startRaw = startEl ? startEl.value.trim() : '';
  const endRaw = endEl ? endEl.value.trim() : '';
  let section = '';
  let ok = true;
  let msg = 'Full video (no clip)';
  let state = 'idle';

  if (!startRaw && !endRaw) {
    section = '';
  } else if (!endRaw) {
    ok = false;
    msg = 'Set an End time (or clear Start)';
    state = 'bad';
  } else {
    const startSec = startRaw ? timeToSeconds(startRaw) : 0;
    const endSec = timeToSeconds(endRaw);
    if (startRaw && startSec == null) {
      ok = false; msg = 'Start must be MM:SS or seconds'; state = 'bad';
    } else if (endSec == null) {
      ok = false; msg = 'End must be MM:SS or seconds'; state = 'bad';
    } else if (endSec <= (startSec || 0)) {
      ok = false; msg = 'End must be after Start'; state = 'bad';
    } else {
      const a = startRaw || '0:00';
      section = `${a}-${endRaw}`;
      const dur = Math.round(endSec - (startSec || 0));
      msg = `Clip ${formatClipSec(startSec || 0)} → ${formatClipSec(endSec)} (${dur}s)`;
      if ($('section_precise') && $('section_precise').checked) msg += ' · precise';
      state = 'ok';
    }
  }

  if (hidden) hidden.value = section;
  if (status) {
    status.textContent = msg;
    status.className = 'clip-status is-' + state;
  }
  if (hint) {
    hint.classList.remove('is-ok', 'is-bad');
    if (state === 'ok') {
      hint.textContent = section;
      hint.classList.add('is-ok');
    } else if (state === 'bad') {
      hint.textContent = 'invalid range';
      hint.classList.add('is-bad');
    } else {
      hint.textContent = 'optional · short clips only';
    }
  }
  return { ok, section, error: ok ? null : msg };
}

/**
 * Warn when the requested height is unreachable with the chosen source codec.
 *
 * YouTube's avc1 (H.264) ladder tops out at ~1080p. The engine's selector asks for
 * `bestvideo[height<=2160][vcodec^=avc1]` first, and a 1080p avc1 stream SATISFIES that clause — so
 * it never falls through to the 4K VP9/AV1 alternative. Asking for 4k while Preferred stream is
 * H.264 therefore yields 1080p, and the summary used to assert "4k" anyway.
 * Returns a warning string, or '' when the combination is honest.
 */
function h264CapWarning(quality, codec) {
  const capped = quality === '4k' || quality === '1440p' || quality === 'best';
  if (!capped || codec !== 'h264') return '';
  return 'H.264 tops out near 1080p — switch Preferred stream to AV1, VP9 or Any for a real '
    + (quality === 'best' ? 'max-resolution' : quality) + ' pull';
}

/**
 * Name the editor extras that Delivery = "Download only" will silently drop.
 *
 * encode_mode='none' returns the raw download and never calls _finalize — the stage that writes
 * markers, .nfo, artwork, sidecars, chapters and container metadata and runs the loudness pass. Two
 * of those (chapters, embed_metadata) are CHECKED BY DEFAULT, so the quiet no-op was the common case.
 * Returns '' for the other delivery modes.
 */
function extrasSkippedNote(mode) {
  if (mode !== 'none') return '';
  const on = (id) => { const n = $(id); return !!(n && (n.type === 'checkbox' ? n.checked : n.value)); };
  const named = [];
  if (on('export_markers')) named.push('markers');
  if (on('chapters')) named.push('chapters');
  if (on('embed_metadata')) named.push('metadata');
  if (on('write_nfo')) named.push('.nfo');
  if (on('loudnorm')) named.push('loudness');
  if (on('proxy_pair')) named.push('proxy');
  if (on('sub_langs')) named.push('subtitles');
  // verify_strict belongs here for the same reason as everything above it: _run_body returns early
  // for encode_mode 'none' (yt_final.py:5683) and _verify_output is only ever reached from
  // _finalize, so the box is a SILENT no-op in this mode — not a lenient warning, no check at all.
  if (on('verify_strict')) named.push('verification');
  return named.length
    ? ' · skips ' + named.join(', ')
    : ' · editor extras skipped';
}

/**
 * Resolve download_archive for a job AND the truth about what it will actually do.
 *
 * PURE — every input is an argument and the filename literal lives inside, so the truth table can be
 * EXECUTED in node (source strings cannot verify branching; this project learned that three times).
 *
 * Three verified engine facts drive it:
 *  - applied ONLY when there is no clip: `if download_archive and not section` (yt_final.py:3294),
 *    because a clip must not mark the whole video id as done. Archive + clip is a silent no-op.
 *  - _turbo_compatible refuses it outright ('download_archive requires built-in', yt_final.py:4194),
 *    so pinning Turbo quietly demotes the fetch path. aria2/hybrid/auto have no such guard.
 *  - yt-dlp opens download_archive CWD-relative and DOES expand $VAR, while the engine opens
 *    output_dir CWD-relative and does NOT (webserver.py:613 vs 620). Deriving from "Save to folder"
 *    puts the archive inside the job's own output folder under BOTH rules — which is also what keeps
 *    the public-server path guard satisfied whenever output_dir itself passes — except when the
 *    folder contains a '$', where the two resolve to different places. Refuse to guess there.
 * Returns { path, note, hint, level }; level 'bad' must block Queue job.
 */
function archivePlan(enabled, explicit, folder, section, engine) {
  if (!enabled) {
    return { path: null, level: 'idle', hint: 'archive · folder layout',
             note: 'Off — repeat runs download everything again' };
  }
  let path = (explicit || '').trim();
  if (!path) {
    const base = (folder || '').trim() || './downloads';   // matches the #output_dir placeholder
    if (base.indexOf('$') >= 0) {
      return { path: null, level: 'bad', hint: 'archive unusable',
               note: 'Pick an archive file — a “Save to folder” with $VAR in it would put the archive somewhere else than the videos' };
    }
    // Keep the folder's own separator so a Windows path does not come back half-POSIX.
    const sep = (base.indexOf('\\') >= 0 && base.indexOf('/') < 0) ? '\\' : '/';
    path = base.replace(/[\\/]+$/, '') + sep + 'yt-final-archive.txt';
  }
  // The path is still SENT for a clip job: the engine's `and not section` stays the single decision
  // point (same as sending chapters for 'Download only' and merely saying they are skipped).
  if (section) {
    return { path: path, level: 'warn', hint: 'archive ignored',
             note: 'Ignored for this job — a clip must not mark the whole video as done' };
  }
  if (engine === 'turbo') {
    return { path: path, level: 'warn', hint: 'archive on · built-in',
             note: 'On, but Turbo cannot use an archive — this job falls back to the built-in downloader' };
  }
  return { path: path, level: 'ok', hint: 'archive on',
           note: 'On — already-downloaded videos are skipped · ' + path };
}

/** Read the archive controls out of the form and hand them to the pure planner. */
function archivePlanFromForm() {
  return archivePlan(
    !!($('download_archive_on') && $('download_archive_on').checked),
    ($('download_archive') && $('download_archive').value) || '',
    ($('output_dir') && $('output_dir').value) || '',
    ($('section') && $('section').value) || '',
    ($('download_engine') && $('download_engine').value) || 'auto');
}

/** Paint the archive state line + the collapsed-section hint. Mirrors syncClipRange's painting. */
function syncArchiveNote() {
  const plan = archivePlanFromForm();
  const note = $('archive_note');
  if (note) {
    note.textContent = plan.note;
    note.className = 'archive-note is-' + plan.level;
  }
  const hint = $('batch_summary_hint');   // the only archive feedback visible while the section is shut
  if (hint) {
    hint.classList.remove('is-ok', 'is-warn', 'is-bad');
    hint.textContent = plan.hint;
    if (plan.level !== 'idle') hint.classList.add('is-' + plan.level);
  }
  return plan;
}

/**
 * ripBitrateNote — the truth about the audio-only rip bitrate: the three states in which the engine
 * IGNORES the value the user just picked.
 *
 * PURE (every input is an argument), so the truth table can be EXECUTED in node — a source-string
 * assertion cannot verify branching, and this project has learned that more than once.
 *
 * Verified engine facts, in the order this reads them:
 *  - audio_format / audio_quality are read ONLY inside `if audio_only:` (yt_final.py:3262), where
 *    audio_only == (quality == 'audio') (yt_final.py:4532, 4581). On a video job both are dead.
 *  - `if afmt and afmt != 'best':` (yt_final.py:3265) — 'best' installs NO FFmpegExtractAudio
 *    postprocessor at all and just keeps bestaudio, so there is nothing for a bitrate to apply to.
 *  - a BLANK File format does not mean "no conversion", it means mp3:
 *    `afmt = (getattr(spec, 'audio_format', None) or 'mp3')` (yt_final.py:3264).
 *  - a blank bitrate means 192: `str(getattr(spec, 'audio_quality', None) or '192')`
 *    (yt_final.py:3270). The value is handed to yt-dlp as `preferredquality`.
 *  - flac and wav are lossless, so the `-b:a Nk` yt-dlp derives from the number has no effect. This
 *    is a claim about the CODEC, not about the engine — the engine does pass the flag.
 *  - for a lossy target, yt-dlp copies the stream instead of re-encoding when the source already IS
 *    that codec (`elif target_format == 'best' or target_format == filecodec:` -> acodec 'copy',
 *    and `if acodec != 'copy': more_opts = self._quality_args(acodec)`), so the bitrate is skipped.
 *    The browser cannot know YouTube's source codec, so the note states the CONDITION.
 * Returns { level, note }. Never 'bad': every state here still produces a usable file.
 */
function ripBitrateNote(quality, audioFormat, audioQuality) {
  const fmt = String(audioFormat || '').trim().toLowerCase();
  const kbps = String(audioQuality || '').trim();
  if (quality !== 'audio') {
    return kbps
      ? { level: 'warn', note: 'Rip bitrate ignored — it only applies when Max resolution is “audio”' }
      : { level: 'idle', note: 'Default audio — the profile decides' };
  }
  if (fmt === 'best') {
    return kbps
      ? { level: 'warn', note: 'Rip bitrate ignored — “best” keeps YouTube’s own audio stream untouched' }
      : { level: 'idle', note: 'Keeping YouTube’s own audio stream (no re-encode)' };
  }
  if (fmt === 'flac' || fmt === 'wav') {
    return kbps
      ? { level: 'warn', note: 'Rip bitrate ignored — ' + fmt + ' is lossless' }
      : { level: 'ok', note: 'Ripping ' + fmt + ' (lossless — bitrate does not apply)' };
  }
  const named = fmt || 'mp3';
  const blankBit = fmt === '' ? ' (blank File format means mp3)' : '';
  if (!kbps) {
    return { level: 'ok', note: 'Ripping ' + named + ' at the 192 kbps default' + blankBit };
  }
  if (named === 'mp3') {
    return { level: 'ok', note: 'Ripping mp3 at ' + kbps + ' kbps' + blankBit };
  }
  return { level: 'ok', note: 'Ripping ' + named + ' at ' + kbps + ' kbps — skipped when YouTube '
                             + 'already serves ' + named + ' (the stream is kept as-is)' };
}

/** Read the rip controls out of the form and hand them to the pure planner. */
function ripBitratePlanFromForm() {
  return ripBitrateNote(
    ($('quality') && $('quality').value) || '1080p',
    ($('audio_format') && $('audio_format').value) || '',
    ($('audio_quality') && $('audio_quality').value) || '');
}

/** Paint the audio state line. Same shape as syncArchiveNote(): read form -> pure planner -> line. */
function syncAudioNote() {
  const plan = ripBitratePlanFromForm();
  const note = $('audio_note');
  if (note) {
    note.textContent = plan.note;
    note.className = 'audio-note is-' + plan.level;
  }
  return plan;
}

function updateComposeSummary() {
  const el = $('compose_summary');
  if (!el) return;
  const clip = syncClipRange();
  const q = ($('quality') && $('quality').value) || '1080p';
  const codecVal = ($('codec') && $('codec').value) || 'h264';
  const c = ($('codec') && $('codec').selectedOptions[0] && $('codec').selectedOptions[0].text) || 'H.264';
  const mode = ($('encode_mode') && $('encode_mode').value) || 'copy';
  const modeLabel = mode === 'copy' ? 'remux (fast)'
    : mode === 'reencode' ? ('re-encode · ' + (($('profile') && $('profile').value) || 'profile'))
    : 'download only';
  const clipBit = !clip.section ? ' · full video'
    : clip.ok ? (' · clip ' + clip.section)
    : ' · clip invalid';
  el.textContent = 'Will: ' + modeLabel + ' · ' + q + ' · ' + c.split('—')[0].trim()
    + clipBit + extrasSkippedNote(mode);
  // One repaint pass for the compose form. Must run HERE, after syncClipRange() at the top of this
  // function, because archivePlanFromForm reads the #section value that call populates.
  syncArchiveNote();
  // Second pure planner on the same repaint pass: the rip bitrate has three states the engine
  // silently ignores. Reads no field this function writes, so it goes after the archive note.
  syncAudioNote();
  // The cap warning is a separate, louder line: it says the job will NOT do what was asked.
  const warn = $('compose_warn');
  if (warn) {
    const msg = h264CapWarning(q, codecVal);
    warn.textContent = msg;
    warn.hidden = !msg;
  }
}
$('encode_mode').onchange = toggleProfile; toggleProfile();
// The extras are included so the "skips …" note (Download only) updates as they are toggled —
// otherwise the summary would name a stale set.
['quality', 'codec', 'profile', 'section_start', 'section_end', 'section_precise',
 'encode_mode', 'export_markers', 'chapters', 'embed_metadata', 'write_nfo', 'loudnorm',
 'proxy_pair', 'sub_langs', 'verify_strict',
 // The archive note is repainted by updateComposeSummary and depends on these four — without them it
 // keeps claiming a stale path/engine after the user changes one. (#section is already covered via
 // section_start/section_end above.)
 'download_archive_on', 'download_archive', 'output_dir', 'download_engine',
 // ripBitrateNote reads quality (registered above), audio_format and audio_quality. Drop either of
 // the two below and the audio note keeps asserting a stale truth, e.g. still promising a bitrate
 // after File format was switched to flac, which ignores it entirely.
 'audio_format', 'audio_quality'].forEach((id) => {
  const n = $(id);
  if (n) n.addEventListener('change', updateComposeSummary);
  if (n) n.addEventListener('input', updateComposeSummary);
});
updateComposeSummary();

// ---- Format probe pop-out (Detect max resolution) ----
let _lastProbe = null;

function heightToQualityOption(h) {
  const n = Number(h);
  if (!Number.isFinite(n) || n <= 0) return null;
  if (n >= 2160) return '4k';
  if (n >= 1440) return '1440p';
  if (n >= 1080) return '1080p';
  if (n >= 720) return '720p';
  // Below 720 the lowest cap the select offers is 480p. Returning 'best' here (as this used to)
  // meant a '360p' chip in the probe popover set Max resolution to the MAXIMUM — the exact
  // opposite of the click — because the chips use this mapping for their action.
  return '480p';
}

/** Normalize probe quality strings to a <select id="quality"> option value. */
function normalizeQualityOption(q) {
  if (q == null || q === '') return null;
  let s = String(q).trim().toLowerCase();
  if (s === '2160' || s === '2160p' || s === '4k' || s === 'uhd') return '4k';
  if (s === '1440' || s === '1440p' || s === '2k' || s === 'qhd') return '1440p';
  if (s === '1080' || s === '1080p' || s === 'fhd') return '1080p';
  if (s === '720' || s === '720p' || s === 'hd') return '720p';
  if (s === '480' || s === '480p') return '480p';
  if (s === 'best' || s === 'max') return 'best';
  if (s === 'audio') return 'audio';
  // bare number
  const n = Number(s.replace(/p$/i, ''));
  if (Number.isFinite(n) && n > 0) return heightToQualityOption(n);
  return s;
}

function setQualityFromProbe(q) {
  const el = $('quality');
  if (!el || q == null || q === '') return false;
  const target = normalizeQualityOption(q);
  if (!target) return false;
  for (let i = 0; i < el.options.length; i++) {
    const o = el.options[i];
    const ov = (o.value || o.textContent || '').trim();
    const ot = (o.textContent || '').trim();
    if (ov === target || ot === target
        || ov.toLowerCase() === target || ot.toLowerCase() === target) {
      el.selectedIndex = i;
      try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch (_) { /* ignore */ }
      updateComposeSummary();
      return true;
    }
  }
  return false;
}

function setCodecFromProbe(c) {
  const el = $('codec');
  if (!el || !c) return false;
  const v = String(c).toLowerCase();
  const map = { h264: 'h264', avc: 'h264', av1: 'av1', vp9: 'vp9', any: 'any' };
  const want = map[v] || v;
  for (let i = 0; i < el.options.length; i++) {
    if (el.options[i].value === want) {
      el.selectedIndex = i;
      try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch (_) { /* ignore */ }
      updateComposeSummary();
      return true;
    }
  }
  return false;
}

function formatDuration(sec) {
  if (sec == null || !Number.isFinite(Number(sec))) return null;
  sec = Math.max(0, Math.round(Number(sec)));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function probePopOpen() {
  const pop = $('probe_pop');
  return !!(pop && !pop.hidden);
}
/** Can this element actually take focus right now? A disabled or unrendered target silently drops
 *  focus to <body>, which is worse than leaving it where it was. */
function canTakeFocus(el) {
  return !!(el && !el.disabled && el.offsetParent !== null);
}
function closeProbePop() {
  const pop = $('probe_pop');
  const wasOpen = !!(pop && !pop.hidden);
  if (pop) pop.hidden = true;
  if (!wasOpen) return;                  // never opened: not ours to move
  // Return focus where the user left it — but only to a target that can HOLD it. closeProbePop is
  // also called defensively right after `probe_res.disabled = true`, and focusing a disabled button
  // drops focus to <body>, i.e. exactly the loss this is meant to prevent. If nothing suitable is
  // available, leave focus untouched.
  const back = $('probe_res');
  if (canTakeFocus(back)) {
    try { back.focus({ preventScroll: true }); } catch (e) { /* gone */ }
  }
}

async function revealPath(path) {
  try { await api('/api/reveal', { method: 'POST', body: JSON.stringify({ path }) }); }
  catch (e) { /* best-effort; nothing to surface */ }
}
// "Already got this?" — fill the popup banner from res.existing; returns a short label or ''.
function renderProbeExisting(res) {
  const box = $('probe_pop_existing');
  const rev = $('probe_pop_reveal');
  const ex = res && res.existing;
  if (!box) return '';
  if (!ex || !ex.found) { box.hidden = true; if (rev) rev.hidden = true; return ''; }
  const bits = ['✓ Already downloaded'];
  if (ex.when) { const d = new Date(ex.when * 1000); if (!isNaN(d.getTime())) bits.push(d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })); }
  const c = ex.codec === 'h264' ? 'H.264' : (ex.codec || '');
  const qc = [ex.quality, c].filter(Boolean).join(' ');
  if (qc) bits.push(qc);
  if (ex.size) bits.push(fmtBytes(ex.size));
  const label = bits.join(' · ');
  const txt = $('probe_pop_existing_txt');
  if (txt) txt.textContent = label;
  box.hidden = false;
  if (rev) {
    if (ex.path) { rev.hidden = false; rev.onclick = () => revealPath(ex.path); }
    else rev.hidden = true;
  }
  return label;
}
function openProbePop(res) {
  _lastProbe = res || null;
  const pop = $('probe_pop');
  if (!pop || !res) return;
  // Is this a real OPEN, or just a re-render of a popover that is already showing? applyProbeSuggested
  // and applyProbeMax call back in here to refresh the chip states — and those are triggered by
  // buttons INSIDE the popover, so moving focus on a refresh yanks it off the button the user just
  // pressed. Only take focus when the popover was actually closed.
  const wasOpen = !pop.hidden;
  renderProbeExisting(res);
  const title = res.title ? String(res.title) : (res.id ? 'Video ' + res.id : 'Probe result');
  const titleEl = $('probe_pop_title');
  if (titleEl) titleEl.textContent = title;

  const metaBits = [];
  if (res.uploader) metaBits.push(String(res.uploader));
  const dur = formatDuration(res.duration);
  if (dur) metaBits.push(dur);
  if (res.id) metaBits.push(res.id);
  const metaEl = $('probe_pop_meta');
  if (metaEl) metaEl.textContent = metaBits.join(' · ') || '—';

  const maxLabel = (res.max_height ? res.max_height + 'p' : (res.resolution_label || 'unknown'))
    + (res.max_width && res.max_height ? ` · ${res.max_width}×${res.max_height}` : '');
  if ($('probe_pop_max')) $('probe_pop_max').textContent = maxLabel;
  if ($('probe_pop_suggested')) {
    // Edit + speed primary suggestion (not max archive)
    if (res.suggested_label) {
      $('probe_pop_suggested').textContent = res.suggested_label;
    } else {
      const cq = res.suggested_quality || '—';
      const cc = res.suggested_codec === 'h264' ? 'H.264' : (res.suggested_codec || '');
      $('probe_pop_suggested').textContent = cc ? `${cq} · ${cc}` : cq;
    }
  }
  if ($('probe_pop_h264')) {
    // Highest H.264 height YouTube lists (almost always ≤1080p). Not "is h264 available".
    if (res.h264_max_height) {
      $('probe_pop_h264').textContent = res.h264_max_height + 'p';
    } else if (res.codecs && res.codecs.includes('h264')) {
      $('probe_pop_h264').textContent = '≤1080p?';
    } else {
      $('probe_pop_h264').textContent = 'none';
    }
  }
  if ($('probe_pop_fps')) {
    const fps = res.fps_values || [];
    $('probe_pop_fps').textContent = fps.length
      ? fps.map((f) => f + ' fps').join(', ')
      : 'not reported';
  }

  const hWrap = $('probe_pop_heights');
  if (hWrap) {
    hWrap.innerHTML = '';
    const heights = res.available_heights || [];
    const curQ = ($('quality') && $('quality').value) || '';
    heights.forEach((h) => {
      const q = heightToQualityOption(h);
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'probe-chip'
        + (h === res.max_height ? ' is-max' : '')
        + (q && q === curQ ? ' is-active' : '');
      b.setAttribute('role', 'listitem');
      b.textContent = h + 'p';
      b.title = q ? 'Set Max resolution → ' + q : String(h);
      b.onclick = () => {
        if (q) setQualityFromProbe(q);
        hWrap.querySelectorAll('.probe-chip').forEach((c) => c.classList.remove('is-active'));
        b.classList.add('is-active');
      };
      hWrap.appendChild(b);
    });
    if (!heights.length) {
      hWrap.innerHTML = '<span class="muted" style="font-size:var(--fs-xs)">No heights reported</span>';
    }
  }

  const cWrap = $('probe_pop_codecs');
  if (cWrap) {
    cWrap.innerHTML = '';
    const codecs = res.codecs || [];
    const curC = ($('codec') && $('codec').value) || '';
    const order = ['h264', 'av1', 'vp9'];
    const list = [...new Set([...order.filter((c) => codecs.includes(c)), ...codecs, 'any'])];
    list.forEach((c) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'probe-chip' + (c === curC ? ' is-active' : '');
      b.setAttribute('role', 'listitem');
      b.textContent = c === 'h264' ? 'H.264' : c === 'any' ? 'Any' : c.toUpperCase();
      b.title = 'Set Preferred stream → ' + c;
      b.onclick = () => {
        setCodecFromProbe(c);
        cWrap.querySelectorAll('.probe-chip').forEach((x) => x.classList.remove('is-active'));
        b.classList.add('is-active');
      };
      cWrap.appendChild(b);
    });
  }

  const tip = $('probe_pop_tip');
  if (tip) {
    if (res.stream_tip) {
      tip.hidden = false;
      tip.textContent = res.stream_tip;
    } else {
      tip.hidden = true;
      tip.textContent = '';
    }
  }

  const openBtn = $('probe_open_url');
  if (openBtn) {
    const href = res.webpage_url || (res.id ? ('https://www.youtube.com/watch?v=' + res.id) : '');
    if (href) {
      openBtn.hidden = false;
      openBtn.onclick = () => { try { window.open(href, '_blank', 'noopener'); } catch (_) { /* ignore */ } };
    } else {
      openBtn.hidden = true;
    }
  }

  pop.hidden = false;
  try { pop.scrollIntoView({ block: 'nearest', behavior: reduceMotion() ? 'auto' : 'smooth' }); } catch (_) { /* ignore */ }
  // Move focus INTO the dialog so a keyboard user lands on it instead of having to hunt for it. It
  // is deliberately aria-modal="false" and focus is NOT trapped — tabbing out is allowed — so this
  // only places the starting point; Esc closes and closeProbePop() restores focus.
  // Skipped on a REFRESH (wasOpen): the apply buttons live inside this popover and call back in here,
  // so taking focus then would pull it off the control the user just activated.
  if (!wasOpen) {
    try {
      const first = $('probe_pop_close') || pop.querySelector('button, [tabindex]');
      if (canTakeFocus(first)) first.focus({ preventScroll: true });
    } catch (_) { /* ignore */ }
  }
}

function applyProbeSuggested() {
  if (!_lastProbe) return;
  // Edit + speed: prefer 1080p H.264 when the probe computed it
  const q = _lastProbe.suggested_quality || '1080p';
  const c = _lastProbe.suggested_codec || 'h264';
  const okQ = setQualityFromProbe(q);
  const okC = setCodecFromProbe(c);
  if (!okQ && _lastProbe.h264_max_height) {
    setQualityFromProbe(heightToQualityOption(_lastProbe.h264_max_height) || '1080p');
  }
  if (!okC) setCodecFromProbe('h264');
  openProbePop(_lastProbe); // refresh chip active states
  const hint = $('hint');
  if (hint) {
    hint.className = 'hint ok';
    hint.textContent = '✓ Edit + speed: '
      + (($('quality') && $('quality').value) || q)
      + ' · '
      + (($('codec') && $('codec').value) || c);
  }
}

function applyProbeMax() {
  if (!_lastProbe) return;
  // Tallest stream (archive). Prefer server-provided quality_max / codec_max.
  const maxH = _lastProbe.max_height;
  const q = _lastProbe.quality_max
    || heightToQualityOption(maxH)
    || _lastProbe.suggested_quality
    || 'best';
  let c = _lastProbe.codec_max || 'any';
  if (maxH && maxH > 1080 && (c === 'h264' || !c)) {
    // Don't leave H.264 selected for true 4K — it will silently cap at 1080
    c = _lastProbe.codec_max && _lastProbe.codec_max !== 'h264'
      ? _lastProbe.codec_max
      : ((_lastProbe.codecs || []).includes('av1') ? 'av1'
        : ((_lastProbe.codecs || []).includes('vp9') ? 'vp9' : 'any'));
  }
  const okQ = setQualityFromProbe(q);
  const okC = setCodecFromProbe(c);
  // Fallback: if mapping failed, try 'best' + 'any'
  if (!okQ) setQualityFromProbe('best');
  if (!okC) setCodecFromProbe('any');
  openProbePop(_lastProbe);
  const hint = $('hint');
  if (hint) {
    hint.className = 'hint ok';
    hint.textContent = '✓ Tallest stream: '
      + (($('quality') && $('quality').value) || q)
      + ' · '
      + (($('codec') && $('codec').value) || c)
      + (maxH ? ` (${maxH}p)` : '');
  }
}

if ($('probe_pop_close')) $('probe_pop_close').onclick = closeProbePop;
if ($('probe_apply_suggested')) $('probe_apply_suggested').onclick = applyProbeSuggested;
if ($('probe_apply_max')) $('probe_apply_max').onclick = applyProbeMax;
if ($('probe_result')) {
  $('probe_result').onclick = () => {
    if (_lastProbe && $('probe_pop') && $('probe_pop').hidden) openProbePop(_lastProbe);
  };
}

// Probe max resolution for the first URL (no download).
if ($('probe_res')) {
  $('probe_res').onclick = async () => {
    const action = beginAuthenticatedAction();
    const btn = $('probe_res');
    const out = $('probe_result');
    const urls = ($('urls') && $('urls').value || '').split('\n').map((s) => s.trim()).filter(Boolean);
    if (!urls.length) {
      if (out) { out.className = 'probe-result bad'; out.textContent = 'Paste a YouTube URL first'; }
      closeProbePop();
      return;
    }
    const control = action.ownControl(btn, { text: 'Detecting…' });
    if (out) { out.className = 'probe-result'; out.textContent = 'Asking YouTube for available formats…'; }
    closeProbePop();
    try {
      const body = {
        url: urls[0],
        cookiefile: ($('cookiefile') && $('cookiefile').value.trim()) || null,
        // Browser cookies: Auth tab only (Compose no longer exposes cookies_from_browser).
        cookies_from_browser: null,
        use_auth: jobWantsAuth(),
        output_dir: ($('output_dir') && $('output_dir').value.trim()) || null,
      };
      const res = await api('/api/probe', { method: 'POST', body: JSON.stringify(body) });
      action.guard();
      if (!res.ok && res.error) {
        if (out) { out.className = 'probe-result bad'; out.textContent = '✗ ' + res.error; }
        return;
      }
      const maxH = res.max_height;
      const short = 'Tallest '
        + (maxH ? maxH + 'p' : (res.resolution_label || '?'))
        + ' · edit+speed '
        + (res.suggested_quality || '1080p')
        + ' · open details';
      const haveIt = res.existing && res.existing.found;
      if (out) {
        out.className = 'probe-result ok is-link' + (haveIt ? ' have-it' : '');
        out.textContent = (haveIt ? '✓ already downloaded — ' : '') + short;
        out.title = haveIt ? 'You already have this — open details to reveal it' : 'Click to re-open format probe';
      }
      openProbePop(res);
      // Deliberately does NOT apply anything: this is a read-only query ("Check available formats").
      // It used to call applyProbeSuggested() here, which silently reset a user's own 4k/AV1 choice
      // to 1080p/H.264 just for looking. The popover offers both explicit actions
      // (#probe_apply_suggested / #probe_apply_max) for anyone who wants them.
    } catch (e) {
      if (action.isSilentError(e)) return;
      if (out) { out.className = 'probe-result bad'; out.textContent = '✗ ' + (e.message || 'probe failed'); }
    } finally {
      control.release();
    }
  };
}

// Native folder picker — the server pops an OS dialog and returns the absolute path.
$('browse').onclick = async () => {
  const action = beginAuthenticatedAction();
  const btn = $('browse'), label = btn.textContent;
  const control = action.ownControl(btn, { text: 'opening…' });
  try {
    const cur = $('output_dir').value.trim() || $('output_dir').placeholder || '';
    const res = await api('/api/pick-folder', { method: 'POST', body: JSON.stringify({ current: cur }) });
    action.guard();
    if (res.path) {
      $('output_dir').value = res.path;
      $('hint').className = 'hint ok'; $('hint').textContent = '✓ ' + res.path;
    } else if (res.error) {
      $('hint').className = 'hint bad'; $('hint').textContent = '✗ ' + res.error + ' — type the path instead';
    }
  } catch (e) {
    if (action.isSilentError(e)) return;
    $('hint').className = 'hint bad'; $('hint').textContent = '✗ ' + e.message;
  } finally {
    control.release();
  }
};

function buildSpec() {
  const urls = $('urls').value.split('\n').map(s => s.trim()).filter(Boolean);
  const num = (v) => v === '' || v == null ? null : Number(v);
  const str = (v) => v ? v : null;
  return {
    urls,
    output_dir: str($('output_dir').value.trim()),
    cookiefile: str($('cookiefile') && $('cookiefile').value.trim()),
    // Browser pull lives on Auth tab; per-job override is path-only.
    cookies_from_browser: null,
    // Per-job Auth: Yes uses Auth session when cookiefile empty; No = anonymous unless override.
    use_auth: !($('use_auth_no') && $('use_auth_no').checked),
    export_markers: str($('export_markers').value),
    // Channel/playlist workflow. Both are applied at DOWNLOAD time (yt_final.py:3294 / 3323-3326),
    // not in _finalize, so they are sent for every delivery mode — including 'Download only'.
    // archivePlan() owns the path (see its docstring for why it derives from Save to folder).
    download_archive: archivePlanFromForm().path,
    organize_by: str($('organize_by') && $('organize_by').value),
    no_shorts: $('no_shorts').checked,
    write_nfo: $('write_nfo').checked,
    proxy_pair: $('proxy_pair').checked,
    notify_url: str($('notify_url') && $('notify_url').value.trim()),
    notify_template: ($('notify_template') && $('notify_template').value) || 'auto',
    quality: $('quality').value,
    codec: $('codec').value,
    encode_mode: $('encode_mode').value,
    profile: $('profile').value,
    section: str(($('section') && $('section').value.trim()) || ''),
    section_precise: !!( $('section_precise') && $('section_precise').checked ),
    download_engine: ($('download_engine') && $('download_engine').value) || 'auto',
    parallel: $('parallel').checked,
    workers: (function(){ const w = num($('parallel_workers') ? $('parallel_workers').value : ''); return w == null ? null : Math.min(16, Math.max(1, Math.round(w))); })(),
    proxy: str($('proxy') && $('proxy').value.trim()),
    audio_codec: str($('audio_codec').value),
    audio_format: str($('audio_format').value),
    // Audio-only rip bitrate. Read ONLY under `if audio_only:` (yt_final.py:3262) and defaulted to
    // 192 (yt_final.py:3270), so send null rather than '' when the user left it alone.
    audio_quality: str($('audio_quality') && $('audio_quality').value),
    loudnorm: num($('loudnorm').value),
    sponsorblock: str($('sponsorblock').value),
    sub_langs: $('sub_langs').value.trim() || null,
    // The engine only requests writeautomaticsub when sub_auto is set, and most YouTube videos carry
    // ONLY auto-generated captions — so without this a user who asked for 'en' silently got no file.
    sub_auto: !!($('sub_auto') && $('sub_auto').checked),
    sub_mode: str($('sub_mode').value),
    sub_format: str($('sub_format').value) || 'srt',
    chapters: $('chapters').checked,
    embed_metadata: $('embed_metadata').checked,
    embed_thumbnail: $('embed_thumbnail').checked,
    write_sidecar: $('write_sidecar').checked,
    write_sidecar_text: $('write_sidecar_text').checked,
    preserve_hdr: $('preserve_hdr').checked,
    // Strict integrity check: _verify_output ffprobes the delivered file and a failure RAISES
    // VerificationError -> job 'failed' (yt_final.py:6160-6161). Fails CLOSED when ffprobe is
    // missing or errors (yt_final.py:6279-6280). Off by default.
    verify_strict: !!($('verify_strict') && $('verify_strict').checked),
  };
}

if ($('notify_test')) {
  $('notify_test').onclick = async () => {
    const action = beginAuthenticatedAction();
    const btn = $('notify_test');
    const hint = $('notify_hint') || $('hint');
    const url = ($('notify_url') && $('notify_url').value.trim()) || '';
    const template = ($('notify_template') && $('notify_template').value) || 'auto';
    if (!url) {
      if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ paste a webhook URL first'; }
      return;
    }
    const control = action.ownControl(btn, { text: 'Sending…' });
    try {
      const res = await api('/api/notify-test', {
        method: 'POST',
        body: JSON.stringify({ url, template }),
      });
      action.guard();
      if (hint) {
        hint.className = 'hint ' + (res.ok ? 'ok' : 'bad');
        hint.textContent = (res.ok ? '✓ ' : '✗ ') + (res.message || res.error || 'done');
      }
    } catch (e) {
      if (action.isSilentError(e)) return;
      if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message; }
    } finally {
      control.release();
    }
  };
}

// ---- Settings: opt-in default proxy (anti-throttle) ----
function renderDesyncStatus(n) {
  const el = $('network_desync_status');
  if (!el) return;
  if (!n || !n.desync) { el.textContent = ''; el.className = 'net-status'; return; }
  const h = n.desync_health;
  if (h === 'running') { el.textContent = '· active ✓'; el.className = 'net-status ok'; }
  else if (h === 'unreachable') { el.textContent = '· not running — downloads fall back'; el.className = 'net-status bad'; }
  else { el.textContent = n.desync_running ? '· active ✓' : '· starting…'; el.className = 'net-status'; }
}
// Proxy is stored as a URL (scheme://host:port) but edited as scheme dropdown + IP:PORT.
function splitProxy(url) {
  const s = (url || '').trim();
  const m = s.match(/^([a-z0-9]+):\/\/(.*)$/i);
  return m ? { scheme: m[1].toLowerCase(), hostport: m[2] } : { scheme: 'socks5', hostport: s };
}
function buildProxy() {
  const scheme = ($('network_proxy_scheme') && $('network_proxy_scheme').value) || 'socks5';
  const hp = ($('network_proxy_hostport') && $('network_proxy_hostport').value.trim()) || '';
  if (!hp) return '';
  return /:\/\//.test(hp) ? hp : scheme + '://' + hp;   // tolerate a pasted full URL
}
function setNetStatus(msg, kind) {
  const st = $('network_status');
  if (!st) return;
  st.textContent = msg || '';
  st.className = 'net-status' + (kind ? ' ' + kind : '');
}
async function loadNetworkSettings() {
  const action = beginAuthenticatedAction();
  try {
    const d = await api('/api/settings');
    action.guard();
    const n = (d && d.network) || {};
    if ($('network_enabled')) $('network_enabled').checked = !!n.enabled;
    const p = splitProxy(n.proxy);
    if ($('network_proxy_scheme')) $('network_proxy_scheme').value = p.scheme;
    if ($('network_proxy_hostport')) $('network_proxy_hostport').value = p.hostport;
    if ($('network_desync')) $('network_desync').checked = !!n.desync;
    if ($('network_doh')) $('network_doh').checked = !!n.doh;
    // Prefill the Speed Test proxy box from the saved proxy so the tab tests the user's ACTUAL
    // configured egress without them retyping it (only when they haven't typed something else).
    if ($('st_proxy_scheme') && $('st_proxy_hostport') && !$('st_proxy_hostport').value && n.proxy) {
      $('st_proxy_scheme').value = p.scheme;
      $('st_proxy_hostport').value = p.hostport;
    }
    renderDesyncStatus(n);
  } catch (e) {
    if (action.isSilentError(e)) return;
    /* offline / not signed in */
  }
}
async function saveNetworkSettings() {
  const action = beginAuthenticatedAction();
  const body = {
    enabled: $('network_enabled').checked,
    proxy: buildProxy(),
    desync: !!($('network_desync') && $('network_desync').checked),
    doh: !!($('network_doh') && $('network_doh').checked),
  };
  try {
    const r = await api('/api/settings', { method: 'POST', body: JSON.stringify(body) });
    action.guard();
    const n = (r && r.network) || {};
    setNetStatus((n.desync || n.enabled) ? 'saved · active' : 'saved · off', 'ok');
    renderDesyncStatus(n);
  } catch (e) {
    if (action.isSilentError(e)) return;
    setNetStatus(e.message || 'save failed', 'bad');
  }
}
async function testNetworkProxy() {
  const action = beginAuthenticatedAction();
  setNetStatus('testing…');
  try {
    const r = await api('/api/settings/test-proxy', {
      method: 'POST',
      body: JSON.stringify({ proxy: buildProxy() }),
    });
    action.guard();
    if (r && r.ok) setNetStatus(`proxy ${r.proxy_mibs} vs direct ${r.direct_mibs} MiB/s`, 'ok');
    else setNetStatus((r && r.error) || 'proxy test failed', 'bad');
  } catch (e) {
    if (action.isSilentError(e)) return;
    setNetStatus(e.message || 'proxy test failed', 'bad');
  }
}
if ($('network_save')) $('network_save').addEventListener('click', saveNetworkSettings);
if ($('network_test')) $('network_test').addEventListener('click', testNetworkProxy);
// The bypass toggle applies immediately and sends ONLY {desync} so it never commits
// half-typed remote-proxy edits (the server leaves absent keys unchanged).
async function toggleDesync() {
  const action = beginAuthenticatedAction();
  try {
    const r = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ desync: !!($('network_desync') && $('network_desync').checked) }),
    });
    action.guard();
    renderDesyncStatus((r && r.network) || {});
  } catch (e) {
    if (action.isSilentError(e)) return;
    setNetStatus(e.message || 'toggle failed', 'bad');
  }
}
if ($('network_desync')) $('network_desync').addEventListener('change', async () => {
  const action = beginAuthenticatedAction();
  await toggleDesync();
  if (!action.isCurrent()) return;
  authenticatedTimeout(loadNetworkSettings, 800);
});
// DoH persists immediately (server leaves absent keys unchanged). It applies when the bypass
// (re)starts, so no child restart is forced here — matches how desync_mode is applied.
if ($('network_doh')) $('network_doh').addEventListener('change', async () => {
  const action = beginAuthenticatedAction();
  try {
    await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ doh: !!$('network_doh').checked }),
    });
    action.guard();
  } catch (e) {
    if (action.isSilentError(e)) return;
    setNetStatus(e.message || 'toggle failed', 'bad');
  }
});
// ---- Speed test: diagnose why downloads are slow, then one-click the fix ----
function stBuildProxy() {
  const scheme = ($('st_proxy_scheme') && $('st_proxy_scheme').value) || 'socks5';
  const hp = ($('st_proxy_hostport') && $('st_proxy_hostport').value.trim()) || '';
  if (!hp) return '';
  return /:\/\//.test(hp) ? hp : scheme + '://' + hp;   // tolerate a pasted full URL
}
let stRunning = false;
let stPhaseTimer = null;
/**
 * stPhaseView — which phase the elapsed seconds land in, and whether it CHANGED.
 * Pure, so the boundary behaviour can be executed rather than argued about: `announce` is what stops
 * a live region from re-reading the same sentence every second for two minutes.
 */
function stPhaseView(secs, phaseCount, lastIndex) {
  const n = Math.max(1, phaseCount | 0);
  const s = Math.max(0, Number(secs) || 0);
  const index = Math.min(Math.floor(s / 7), n - 1);
  return { index, announce: index !== lastIndex };
}

function stStartPhases(withProxy) {
  const box = $('st_phases');
  if (!box) return;
  const phases = [
    'Measuring your raw connection…',
    'Checking a non-YouTube Google server…',
    'Fetching a real YouTube video…',
    'Sweeping the bypass modes…',
  ];
  if (withProxy) phases.push('Testing your proxy / VPN…');
  phases.push('Working out the verdict…');
  box.hidden = false;
  const started = Date.now();
  // Built ONCE, not rebuilt every second. #st_phases is a live region, and the old paint() cleared it
  // and recreated both children each tick with the seconds counter baked into the same text node — so
  // a screen reader re-announced the entire phase sentence once per second for the whole 1–2 minutes.
  //
  // Split by what the user needs to HEAR: the phase sentence is announced and changes only when the
  // phase does (~7s). The spinner and the ticking counter are aria-hidden, so they stay visible — a
  // slow, throttled line must read as "still working" rather than stuck — without being spoken.
  box.textContent = '';
  const spin = document.createElement('span');
  spin.className = 'st-spin'; spin.textContent = '⟳'; spin.setAttribute('aria-hidden', 'true');
  const txt = document.createElement('span'); txt.className = 'st-phase-txt';
  const meta = document.createElement('span');
  meta.className = 'st-phase-meta'; meta.setAttribute('aria-hidden', 'true');
  box.appendChild(spin); box.appendChild(txt); box.appendChild(meta);

  let lastIndex = -1;
  const paint = () => {
    const secs = Math.round((Date.now() - started) / 1000);
    const view = stPhaseView(secs, phases.length, lastIndex);
    meta.textContent = '  ·  ' + secs + 's (can take 1–2 min)';
    if (view.announce) {
      lastIndex = view.index;
      txt.textContent = phases[view.index];
    }
  };
  paint();
  stPhaseTimer = authenticatedInterval(paint, 1000);
}
function stStopPhases() {
  if (stPhaseTimer) { clearAuthenticatedInterval(stPhaseTimer); stPhaseTimer = null; }
  const box = $('st_phases'); if (box) { box.hidden = true; box.textContent = ''; }
}
const ST_STATE_CLASS = {
  clean: 'ok', throttled_fixable: 'warn', throttled_hard: 'bad', slow_link: 'warn',
  degraded: 'warn', no_network: 'bad', error: 'bad',
  // states the engine also returns — must be coloured too, or they fall back to neutral
  vpn_ok: 'ok', bypass_active: 'ok', vpn_slow: 'warn', partial: 'warn', measured_direct_only: 'info',
};
function stRenderBars(bars) {
  const wrap = document.createElement('div'); wrap.className = 'st-bars';
  const max = Math.max(1, ...bars.map((b) => b.mibs || 0));
  bars.forEach((b) => {
    const row = document.createElement('div'); row.className = 'st-bar-row st-bar-' + (b.kind || 'x');
    const lab = document.createElement('span'); lab.className = 'st-bar-lab'; lab.textContent = b.label;
    const track = document.createElement('span'); track.className = 'st-bar-track';
    const fill = document.createElement('span'); fill.className = 'st-bar-fill';
    fill.style.width = Math.max(3, Math.round(((b.mibs || 0) / max) * 100)) + '%';
    track.appendChild(fill);
    const val = document.createElement('span'); val.className = 'st-bar-val';
    val.textContent = (b.mibs > 0) ? (b.mibs.toFixed(1) + ' MiB/s') : '—';
    if (b.mibs > 0 && b.lo > 0 && b.hi > 0 && b.hi > b.lo) {
      // Jittery flow: show the measured spread so a lone median can't mislead.
      val.textContent += ` (${b.lo.toFixed(0)}–${b.hi.toFixed(0)})`;
    }
    row.appendChild(lab); row.appendChild(track); row.appendChild(val);
    wrap.appendChild(row);
  });
  return wrap;
}
/**
 * PURE (the response is the argument) so the truth table can be EXECUTED in node.
 * Success is {ok, result, report}; message/outdated live on `result`.
 */
function installOutcome(body, tool) {
  const b = body || {};
  const res = b.result || {};
  const name = tool || 'tool';
  if (!b.ok) {
    return { level: 'bad', label: 'Open Updates →', detail: b.error || (name + ' install failed') };
  }
  // A certified install can still be behind the approved manifest version. Keep
  // that distinct from both verification failure and ready-now success.
  const rowBehind = !!(b.report && (b.report.info || []).some(
    (row) => row && row.name === tool && row.outdated));
  if (res.outdated || rowBehind) {
    return { level: 'warn', label: 'Installed (older)',
             detail: res.message || (name + ' installed, but not at the latest version') };
  }
  return { level: 'ok', label: 'Installed ✓', detail: res.message || (name + ' ready') };
}

async function stDoAction(rec, btn) {
  const action = beginAuthenticatedAction();
  const a = rec.action || {};
  let control = null;
  let updatesInstallOwner = null;
  const done = (label) => {
    if (!control) control = action.ownControl(btn, { disable: false });
    control.commit(() => {
      btn.textContent = label;
      btn.disabled = true;
      btn.classList.add('is-done');
    });
  };
  try {
    if (a.kind === 'enable_bypass') {
      // The server supplies the exact payload (desync + swept desync_mode).
      control = action.ownControl(btn, { text: 'Enabling…' });
      await api(a.endpoint || '/api/settings', { method: 'POST', body: JSON.stringify(a.payload || { desync: true }) });
      action.guard();
      loadNetworkSettings();
      done('Bypass enabled ✓');
    } else if (a.kind === 'set_proxy' || a.kind === 'use_proxy') {
      // The server carries the MEASURED winning proxy in the payload, plus desync:false so the
      // saved proxy actually takes effect (the local bypass would otherwise shadow it).
      control = action.ownControl(btn, { text: 'Saving…' });
      await api(a.endpoint || '/api/settings', { method: 'POST', body: JSON.stringify(a.payload || {}) });
      action.guard();
      loadNetworkSettings();
      done('Saved as your proxy ✓');
    } else if (a.kind === 'install_tool') {
      updatesInstallOwner = beginUpdatesInstall();
      if (!updatesInstallOwner) {
        setSpeedInstallControlBusy(btn, true);
        return;
      }
      control = action.ownControl(btn, { text: 'Installing…' });
      const toolName = (a.payload || {}).tool;
      let outcome;
      try {
        const r = await api(a.endpoint || '/api/updates/tools', { method: 'POST', body: JSON.stringify(a.payload || {}) });
        action.guard();
        outcome = installOutcome(r, toolName);
      } catch (e) {
        if (action.isSilentError(e)) return;
        outcome = installOutcome(null, toolName);
      }
      done(outcome.label);
      // Preserve the server's verification/outdated detail on the control
      // without stealing the current view.
      btn.title = outcome.detail;
      if (outcome.level === 'bad') activateView('updates', { focus: true });   // failures land on Updates (api() throws on non-2xx)
    } else if (a.kind === 'open_auth') {
      activateView('auth', { focus: true });
    } else if (a.kind === 'test_proxy') {
      const box = $('st_proxy_box'); if (box) box.open = true;
      const inp = $('st_proxy_hostport'); if (inp) { inp.focus(); inp.scrollIntoView({ block: 'center' }); }
    } else if (a.kind === 'retest') {
      runSpeedTest();
    } else {
      done('OK');
    }
  } catch (e) {
    if (action.isSilentError(e)) return;
    if (control) control.release();
    btn.disabled = false; btn.textContent = (a.label || 'Try again');
    if (btn.parentNode) {
      const old = btn.parentNode.querySelector('.st-rec-err');   // don't stack duplicates on retry
      if (old) old.remove();
      const err = document.createElement('span'); err.className = 'st-rec-err';
      err.textContent = ' · ' + (e.message || 'failed');
      btn.parentNode.appendChild(err);
    }
  } finally {
    // The recommendation control may be committed by done(); releasing only the shared Updates
    // installer owner leaves that committed label/disabled state intact.
    if (updatesInstallOwner) {
      action.effect(() => { finishUpdatesInstall(updatesInstallOwner); });
    }
  }
}
function stRenderRec(rec) {
  const card = document.createElement('div'); card.className = 'st-rec st-rec-' + (rec.severity || 'info');
  const icon = document.createElement('span'); icon.className = 'st-rec-icon'; icon.textContent = rec.icon || '•';
  const body = document.createElement('div'); body.className = 'st-rec-body';
  const head = document.createElement('div'); head.className = 'st-rec-head';
  const title = document.createElement('span'); title.className = 'st-rec-title'; title.textContent = rec.title || '';
  head.appendChild(title);
  if (rec.expected_gain) {
    const gain = document.createElement('span'); gain.className = 'st-rec-gain'; gain.textContent = rec.expected_gain;
    head.appendChild(gain);
  }
  const detail = document.createElement('p'); detail.className = 'st-rec-detail'; detail.textContent = rec.detail || '';
  body.appendChild(head); body.appendChild(detail);
  const a = rec.action || {};
  if (a.kind && a.kind !== 'none' && a.kind !== 'guidance') {
    const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'st-rec-btn';
    btn.textContent = a.label || 'Do it';
    if (a.kind === 'install_tool') {
      btn.dataset.updateInstall = '1';
      setSpeedInstallControlBusy(btn, _updatesBusy);
    }
    btn.addEventListener('click', () => stDoAction(rec, btn));
    body.appendChild(btn);
  }
  card.appendChild(icon); card.appendChild(body);
  return card;
}
// --- run history (localStorage): supports the "turn VPN off and re-run to compare" workflow ---
const ST_HIST_KEY = 'yt_speedtest_history';
function stGetHistory() { try { return JSON.parse(localStorage.getItem(ST_HIST_KEY) || '[]'); } catch (e) { return []; } }
function stPushHistory(res) {
  try {
    const m = (res && res.measurements) || {};
    const entry = {
      t: Date.now(), state: ((res && res.verdict) || {}).state || '',
      org: (m.egress || {}).org || '',
      ceiling: m.ceiling_mibs, direct: m.youtube_direct_mibs,
      bypass: m.youtube_bypass_mibs, proxy: m.youtube_proxy_mibs,
      // The spread and the confidence grade the backend already computed. Without them the compare
      // line prints one confident decimal for a run the backend itself flagged as jittery — and
      // googlevideo pacing makes back-to-back runs on the SAME box read 14 vs 62 MiB/s, so an
      // unqualified single number invites exactly the false comparison the verdict warns against.
      lo: m.youtube_direct_lo, hi: m.youtube_direct_hi, conf: m.confidence,
    };
    const h = stGetHistory(); h.unshift(entry);
    localStorage.setItem(ST_HIST_KEY, JSON.stringify(h.slice(0, 6)));
  } catch (e) { /* storage full / disabled — history is best-effort */ }
}
function stAgo(ms) {
  const s = Math.max(1, Math.round((Date.now() - ms) / 1000));
  if (s < 90) return s + 's ago';
  const mnt = Math.round(s / 60); if (mnt < 90) return mnt + 'm ago';
  return Math.round(mnt / 60) + 'h ago';
}
/**
 * stCompareText — the previous run's line, hedged when that run was jittery.
 *
 * PURE (the elapsed wording is passed in) so the legacy path can be executed: history entries saved
 * before lo/hi/conf existed must render exactly as they did, not "undefined–undefined".
 */
function stCompareText(prior, agoText) {
  if (!prior || !(prior.direct > 0)) return null;
  const org = prior.org ? (' via ' + prior.org.split(' ')[0]) : '';
  const lo = Number(prior.lo), hi = Number(prior.hi);
  const spread = isFinite(lo) && isFinite(hi) && lo > 0 && hi > lo;
  // A range is more honest than a median the backend flagged as unreliable.
  const speed = spread
    ? `${lo.toFixed(0)}–${hi.toFixed(0)} MiB/s`
    : `${prior.direct.toFixed(1)} MiB/s`;
  const hedge = (prior.conf === 'low' && !spread) ? ' (that run was jittery)' : '';
  return `Last test ${agoText}${org}: YouTube was ${speed}`
    + (prior.bypass > 0 ? ` (bypass ${prior.bypass.toFixed(1)})` : '')
    + hedge;
}

function stCompareLine(prior) {
  // Show the previous run's YouTube-direct number + egress so a VPN on/off A/B is visible.
  const txt = stCompareText(prior, stAgo(prior && prior.t));
  if (!txt) return null;
  const el = document.createElement('p'); el.className = 'st-compare';
  el.textContent = txt;
  return el;
}
function stResultText(res) {
  const v = (res && res.verdict) || {};
  const bars = (res && res.bars) || [];
  const lines = ['yt_final speed test — ' + (v.headline || '')];
  bars.forEach((b) => {
    let s = `  ${b.label}: ${(b.mibs > 0) ? b.mibs.toFixed(1) + ' MiB/s' : '—'}`;
    if (b.mibs > 0 && b.lo > 0 && b.hi > 0 && b.hi > b.lo) s += ` (range ${b.lo.toFixed(0)}–${b.hi.toFixed(0)})`;
    lines.push(s);
  });
  if (res && res.variability && res.variability.note) lines.push('  ' + res.variability.note);
  return lines.join('\n');
}
function renderSpeedTest(res, opts = {}) {
  const root = $('st_results'); if (!root) return;
  root.textContent = '';
  // The server can return HTTP 200 with an embedded error (state:'error', no measurements). Treat
  // that as an error too, so it never gets recorded into the compare-history ring as a null run.
  const isError = !!opts.error || !res || !res.measurements || ((res.verdict || {}).state === 'error');
  opts = Object.assign({}, opts, { error: isError });
  const v = (res && res.verdict) || {};
  const verdict = document.createElement('div');
  verdict.className = 'st-verdict st-' + (ST_STATE_CLASS[v.state] || 'info');
  const vh = document.createElement('p'); vh.className = 'st-verdict-headline';
  vh.textContent = v.headline || 'Test complete.';
  verdict.appendChild(vh);
  // Compare against the previous run BEFORE recording this one.
  const cmp = opts.error ? null : stCompareLine(stGetHistory()[0]);
  if (cmp) verdict.appendChild(cmp);
  root.appendChild(verdict);

  if (res && res.bars && res.bars.length) root.appendChild(stRenderBars(res.bars));

  // Variability note: when the direct-YouTube samples disagreed a lot (jittery CDN pacing), tell the
  // user the number is a rough middle and not to compare single readings — prevents the exact
  // "Mac 25 vs Windows 17" confusion where both are the same variable line at different moments.
  if (res && res.variability && res.variability.note) {
    const note = document.createElement('p'); note.className = 'st-variability hint';
    note.textContent = '↕ ' + res.variability.note;
    root.appendChild(note);
  }

  const recs = (res && res.recommendations) || [];
  if (recs.length) {
    const list = document.createElement('div'); list.className = 'st-recs';
    recs.forEach((r) => list.appendChild(stRenderRec(r)));
    root.appendChild(list);
  }
  // Foot: re-test + copy result.
  const foot = document.createElement('div'); foot.className = 'st-foot';
  const again = document.createElement('button'); again.type = 'button'; again.className = 'ghost';
  again.textContent = 'Run again'; again.addEventListener('click', runSpeedTest);
  foot.appendChild(again);
  if (!opts.error && res && res.bars && res.bars.length) {
    const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'ghost';
    copy.textContent = 'Copy result';
    copy.addEventListener('click', () => copyText(stResultText(res), copy));
    foot.appendChild(copy);
  }
  root.appendChild(foot);

  if (!opts.error) stPushHistory(res);       // record AFTER rendering the compare line
}
function stProxyError() {
  // Empty is fine (no proxy leg). If the box has text, it must be a valid passwordless host:port,
  // else a typo would 400 on the server and wipe the whole run — validate here and block instead.
  const inp = $('st_proxy_hostport');
  const raw = (inp && inp.value.trim()) || '';
  if (!raw) return '';
  const url = stBuildProxy();
  if (/@/.test(url)) return 'Use a passwordless proxy (no user:pass@).';
  const m = url.match(/^(socks5|socks5h|socks4|http):\/\/[^\s/@]+:(\d{1,5})$/);
  if (!m) return 'Enter host:port, e.g. 127.0.0.1:1080.';
  const port = parseInt(m[2], 10);
  // Match the server's range (1-65535) so a port it would reject can't slip past and 400 the run.
  if (port < 1 || port > 65535) return 'Port must be between 1 and 65535.';
  return '';
}
async function runSpeedTest() {
  if (stRunning) return;
  const btn = $('st_run'); const results = $('st_results');
  const errEl = $('st_proxy_err');
  const perr = stProxyError();
  if (perr) {                                  // bad optional proxy -> fix it, don't destroy the run
    if (errEl) { errEl.hidden = false; errEl.textContent = perr; }
    const box = $('st_proxy_box'); if (box) box.open = true;
    const inp = $('st_proxy_hostport'); if (inp) inp.focus();
    return;
  }
  if (errEl) errEl.hidden = true;
  const proxy = stBuildProxy();
  const action = beginAuthenticatedAction();
  stRunning = true;
  const control = action.ownControl(btn, { text: 'Testing…' });
  if (results) results.textContent = '';
  stStartPhases(!!proxy);
  try {
    const body = proxy ? { proxy } : {};
    const res = await api('/api/speedtest', { method: 'POST', body: JSON.stringify(body) });
    action.effect(() => renderSpeedTest(res));
  } catch (e) {
    if (action.isSilentError(e)) return;
    renderSpeedTest({ verdict: { headline: (e.message || 'Speed test failed.'), state: 'error' },
                      bars: [], recommendations: [] }, { error: true });
  } finally {
    action.effect(() => {
      stStopPhases();
      stRunning = false;
    });
    control.release();
  }
}
if ($('st_run')) $('st_run').addEventListener('click', runSpeedTest);

// ---- job presets (client-side, saved in this browser) ----
const PRESET_KEY = 'yt_presets';
const PRESET_FIELDS = ['quality', 'codec', 'encode_mode', 'profile'];
function getPresets() { try { return JSON.parse(localStorage.getItem(PRESET_KEY) || '{}'); } catch (e) { return {}; } }
function savePresets(p) { try { localStorage.setItem(PRESET_KEY, JSON.stringify(p)); } catch (e) {} }
function renderPresets(sel) {
  const el = $('preset_select'); if (!el) return;
  const p = getPresets();
  el.textContent = '';
  const def = document.createElement('option');
  def.value = ''; def.textContent = Object.keys(p).length ? 'Presets…' : 'No presets yet';
  el.appendChild(def);
  Object.keys(p).sort().forEach((n) => {
    const o = document.createElement('option'); o.value = n; o.textContent = n; el.appendChild(o);
  });
  if (sel) el.value = sel;
}
function applyPreset(name) {
  const p = getPresets()[name]; if (!p) return;
  PRESET_FIELDS.forEach((k) => {
    const el = $(k);
    if (el && p[k] != null) { el.value = p[k]; el.dispatchEvent(new Event('change', { bubbles: true })); }
  });
}
function savePresetPrompt() {
  const name = (prompt('Preset name (e.g. "1080p for Premiere")') || '').trim();
  if (!name) return;
  const p = getPresets();
  p[name] = {};
  PRESET_FIELDS.forEach((k) => { const el = $(k); if (el) p[name][k] = el.value; });
  savePresets(p); renderPresets(name);
}
function deletePreset() {
  const el = $('preset_select'); const name = el && el.value; if (!name) return;
  const p = getPresets(); delete p[name]; savePresets(p); renderPresets('');
}
if ($('preset_select')) $('preset_select').addEventListener('change', (e) => { if (e.target.value) applyPreset(e.target.value); });
if ($('preset_save')) $('preset_save').addEventListener('click', savePresetPrompt);
if ($('preset_del')) $('preset_del').addEventListener('click', deletePreset);
renderPresets();

/**
 * Preflight the Save-to-folder box against the SERVER's filesystem (the box names a path on the
 * machine running the app, which may not be the machine showing this page).
 *
 * Without this an unusable path — an unmounted E:\, a typo, a read-only dir — reached the engine,
 * which logged a warning and quietly saved to the previous folder while the card still said 'done'.
 * Returns {ok, error}. A transport failure resolves ok:true: a broken check must never be able to
 * block queueing (the engine still records a fallback if it happens).
 */
async function checkOutputFolder(path) {
  if (!path) return { ok: true };
  const action = beginAuthenticatedAction();
  try {
    const res = await api('/api/check-folder', { method: 'POST', body: JSON.stringify({ path }) });
    action.guard();
    return res && res.ok ? { ok: true } : { ok: false, error: (res && res.error) || 'unusable folder' };
  } catch (e) {
    if (action.isSilentError(e)) throw staleSessionError();
    return { ok: true };                      // fail OPEN — never block on a broken check
  }
}
// Live feedback as soon as the user leaves the box, so the problem surfaces before Queue job.
if ($('output_dir')) {
  $('output_dir').addEventListener('blur', async () => {
    const action = beginAuthenticatedAction();
    const hint = $('hint');
    const val = $('output_dir').value.trim();
    if (!val || !hint) return;
    try {
      const r = await checkOutputFolder(val);
      action.guard();
      if (!r.ok) { hint.className = 'hint bad'; hint.textContent = '✗ Save to folder: ' + r.error; }
      else if (hint.textContent.startsWith('✗ Save to folder:')) { hint.className = 'hint'; hint.textContent = ''; }
    } catch (e) {
      if (!action.isSilentError(e)) throw e;
    }
  });
}

$('submit').onclick = async () => {
  const action = beginAuthenticatedAction();
  const clip = syncClipRange();
  const spec = buildSpec();
  const hint = $('hint');
  if (!spec.urls.length) { hint.className = 'hint bad'; hint.textContent = '✗ need at least one URL'; return; }
  if (!clip.ok) {
    hint.className = 'hint bad';
    hint.textContent = '✗ Clip range: ' + (clip.error || 'invalid');
    // Open Advanced + Clip so the user can fix it
    try {
      setAdv(true);
      const det = $('adv_clip');
      if (det) det.open = true;
    } catch (_) { /* ignore */ }
    return;
  }
  // The archive is unresolvable ($VAR in Save to folder) — queueing would hand the engine a null
  // archive while the checkbox says it is on, i.e. a repeat pull that quietly re-downloads
  // everything. Same gate shape as the clip range above, including re-opening the drawer + section.
  const arch = syncArchiveNote();
  if (arch.level === 'bad') {
    hint.className = 'hint bad';
    hint.textContent = '✗ Archive: ' + arch.note;
    try {
      setAdv(true);
      const det = $('adv_batch');
      if (det) det.open = true;
      const box = $('download_archive');
      if (box) box.focus();
    } catch (_) { /* ignore */ }
    return;
  }
  const control = action.ownControl($('submit'));
  try {
    // Block on an unusable destination rather than letting the engine silently save elsewhere.
    const folder = await checkOutputFolder((spec.output_dir || '').trim());
    action.guard();
    if (!folder.ok) {
      hint.className = 'hint bad';
      hint.textContent = '✗ Save to folder: ' + folder.error;
      const box = $('output_dir');
      if (box) { box.focus(); box.select && box.select(); }
      return;
    }
    const rec = await api('/api/jobs', { method: 'POST', body: JSON.stringify(spec) });
    action.guard();
    hint.className = 'hint ok'; hint.textContent = `✓ queued ${rec.id}`;
    $('urls').value = '';
    refresh();
  } catch (e) {
    if (action.isSilentError(e)) return;
    hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message;
  } finally { control.release(); }
};

function pipelineHint(ok, msg) {
  // Reuse the compose hint strip so pipeline actions have visible feedback (was silent on failure).
  const hint = $('hint');
  if (!hint) return;
  hint.className = 'hint ' + (ok ? 'ok' : 'bad');
  hint.textContent = (ok ? '✓ ' : '✗ ') + msg;
}
$('retry').onclick = async () => {
  const action = beginAuthenticatedAction();
  try {
    // Use the server's count: it returns {reset: n}. Reporting a flat success when n === 0 taught
    // users that the button "works" while nothing happened.
    const res = await api('/api/retry-failed', { method: 'POST', body: '{}' });
    action.guard();
    const n = res && typeof res.reset === 'number' ? res.reset : null;
    pipelineHint(true, pipelineActionMsg('retry', n));
    refresh({force:true});
  }
  catch (e) {
    if (action.isSilentError(e)) return;
    pipelineHint(false, e.message || 'retry failed');
  }
};
$('clear').onclick = async () => {
  const action = beginAuthenticatedAction();
  try {
    const res = await api('/api/clear', { method: 'POST', body: '{}' });   // {removed: n}
    action.guard();
    const n = res && typeof res.removed === 'number' ? res.removed : null;
    pipelineHint(true, pipelineActionMsg('clear', n));
    refresh({force:true});
  }
  catch (e) {
    if (action.isSilentError(e)) return;
    pipelineHint(false, e.message || 'clear failed');
  }
};
$('pause').onclick = async () => {
  const action = beginAuthenticatedAction();
  const paused = $('pause').dataset.on === '1';
  try {
    await api(paused ? '/api/resume' : '/api/pause', { method: 'POST', body: '{}' });
    action.guard();
    pipelineHint(true, paused ? 'queue resumed' : 'queue paused');
    refresh();
  } catch (e) {
    if (action.isSilentError(e)) return;
    pipelineHint(false, e.message || 'pause/resume failed');
  }
};
async function postAction(path, id) {
  const action = beginAuthenticatedAction();
  try {
    await api(path, { method: 'POST', body: JSON.stringify({ id }) });
    action.guard();
    refresh({force:true});
  } catch (e) {
    if (action.isSilentError(e)) return;
    pipelineHint(false, e.message || 'action failed');
  }
}

// ---- pipeline render (keyed, no full re-render -> no flicker) ----
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const cards = new Map();   // job id -> DOM element (reused across polls)
let jobFilter = 'all';
try { jobFilter = sessionStorage.getItem('yt_pipe_filter') || 'all'; } catch (e) {}

function fmtBytes(b) {
  if (!b) return '0';
  const u = ['B', 'KiB', 'MiB', 'GiB']; let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v < 10 && i > 0 ? 1 : 0) + u[i];
}
function fmtTime(s) {
  if (s == null || s < 0 || !isFinite(s)) return '--:--';
  s = Math.round(s); const m = Math.floor(s / 60), ss = String(s % 60).padStart(2, '0');
  if (m >= 60) { const h = Math.floor(m / 60); return `${h}:${String(m % 60).padStart(2, '0')}:${ss}`; }
  return `${m}:${ss}`;
}
function fmtSpeed(bps) { return bps ? (bps / 1048576).toFixed(1) + ' MiB/s' : '—'; }
function fmtAgo(ts) {
  if (ts == null || ts === '') return '';
  const t = Number(ts);
  if (!isFinite(t) || t <= 0) return '';
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 45) return 'just now';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
function basename(p) {
  const s = String(p || '');
  const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
  return i >= 0 ? s.slice(i + 1) : s;
}
function jobSourceUrl(j) {
  const urls = (j.spec && j.spec.urls) || [];
  if (urls.length) return urls[0];
  const m = String(j.label || '').match(/https?:\/\/\S+/);
  return m ? m[0].replace(/[),.;]+$/, '') : null;
}
function shortJobLabel(j) {
  const url = jobSourceUrl(j);
  const raw = j.label || j.id;
  if (!url) return { title: raw, sub: '' };
  // Prefer readable bits from the durable label (quality/codec/section) under a short id/host line.
  let id = '';
  try {
    const u = new URL(url);
    id = u.searchParams.get('v') || (u.pathname.match(/\/([A-Za-z0-9_-]{6,})\/*$/) || [])[1] || '';
  } catch (e) {}
  const rest = String(raw).replace(url, '').replace(/^\s*[@·\-—]+\s*/, '').trim();
  const title = id ? `youtu.be/${id}` : url.replace(/^https?:\/\/(www\.)?/, '');
  return { title, sub: rest || (url !== raw ? raw : '') };
}
function matchesJobFilter(status, filter) {
  if (filter === 'all') return true;
  if (filter === 'active') return status === 'queued' || status === 'running';
  return status === filter;
}
async function copyText(text, btn) {
  const t = String(text || '');
  if (!t) return;
  const action = beginAuthenticatedAction();
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(t);
    else {
      const ta = document.createElement('textarea');
      ta.value = t; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
    }
    action.guard();
    if (btn) {
      // Re-entrant on purpose: replace any pending restore and its temporary ownership so repeated
      // clicks always settle back to the original label without letting an older session restore it.
      if (btn._copyRestore) clearAuthenticatedTimeout(btn._copyRestore);
      if (btn._copyControl) btn._copyControl.release();
      const control = action.ownControl(btn, { disable: false, text: 'copied' });
      btn._copyControl = control;
      btn._copyRestore = authenticatedTimeout(() => {
        control.release();
        btn._copyControl = null;
        btn._copyRestore = null;
      }, 900);
    }
  } catch (e) {
    if (action.isSilentError(e)) return;
    pipelineHint(false, 'copy failed');
  }
}

const CARD_HTML = `
  <div class="job-top">
    <div class="job-label"></div>
    <div class="job-top-end">
      <span class="pill"></span>
      <button type="button" class="job-dismiss" title="Remove from list" aria-label="Remove job">×</button>
    </div>
  </div>
  <div class="job-meta"><span class="id" title="Click to copy job id"></span><span class="att"></span><span class="elapsed"></span><span class="updated"></span></div>
  <div class="job-progress"><div class="bar"><div class="fill"></div></div><div class="pstats"></div></div>
  <div class="job-result" hidden></div>
  <!-- Caveats on an otherwise-successful job: URLs that were dropped from a batch, an output folder
       we could not use, or processing that failed so the RAW download was delivered. These used to be
       invisible (log-only), which let a partly-failed job present as a clean green success. -->
  <div class="job-partial" hidden></div>
  <div class="job-out"></div><div class="job-err"></div>
  <div class="job-agegate"></div>
  <div class="job-acts"></div>`;

/**
 * Announce a job reaching a TERMINAL state, once. Progress is deliberately not announced: the bar
 * and stats rewrite ~1/s per running job, so a live region over the list would talk continuously.
 * Pure bookkeeping in a module-level map + one write to #jobs_live.
 */
/**
 * May this poll rewrite the card's class list? PURE, so the truth table is EXECUTABLE — a source
 * assertion cannot tell this guard from `true`, and the failure is invisible for a whole second.
 *
 * `el.className = 'job ' + status` is a full REPLACE that wipes the derived has-result /
 * is-degraded flags, which are re-applied only behind the unchanged-content guard. Rewriting on
 * every poll therefore stripped the amber pill off a partly-failed job. status IS part of
 * contentSig, so a rewrite always coincides with the guard running — the flags come straight back.
 */
function cardClassNeedsRewrite(isNew, prevStatus, status) {
  return !!isNew || (prevStatus || '') !== (status || '');
}

/**
 * The verb to announce for a status transition, or '' for silence. PURE for the same reason.
 *
 * Terminal states only (the progress row rewrites ~1/s per running job — that is why #jobs itself is
 * not a live region), AND only from a KNOWN previous status: prevStatus is the card's own dataset,
 * which is undefined on first paint, so without the !prev guard a page RELOAD read out every
 * finished job still in the queue store.
 */
function jobStatusAnnouncement(prev, next) {
  const verb = next === 'done' ? 'finished'
    : next === 'failed' ? 'failed'
    : next === 'cancelled' ? 'was cancelled' : '';
  if (!verb || !prev || prev === next) return '';
  return verb;
}

function announceJobStatus(j, prevStatus) {
  const el = $('jobs_live');
  if (!el || !j) return;
  const what = jobStatusAnnouncement(prevStatus, j.status);
  if (!what) return;
  const lbl = (typeof shortJobLabel === 'function' ? shortJobLabel(j) : null);
  const label = (lbl && lbl.title) || j.label || j.id;
  el.textContent = `${label} ${what}`;
}

/**
 * Caveats for a job that still counts as delivered. PURE (no DOM) so its truth table can be executed
 * in a test rather than pattern-matched — the rendering that consumes it lives in upsertCard.
 *
 * Three independent truths the UI used to hide, all of which left a green "done" card:
 *   partial_failures    — the engine has always recorded dropped URLs from a --parallel batch;
 *   output_dir_fallback — the requested folder was unusable and we saved elsewhere;
 *   degraded            — processing failed, so the RAW download was delivered instead.
 * Returns {lines, titles}; empty lines means a genuinely clean job.
 */
function jobCaveats(j, r) {
  const lines = [];
  const titles = [];
  const pf = (r && r.partial_failures) || null;
  if (pf && pf.length) {
    const total = (j && j.spec && Array.isArray(j.spec.urls) ? j.spec.urls.length : 0) || null;
    lines.push(total ? `⚠ ${total - pf.length} of ${total} downloaded — ${pf.length} failed`
                     : `⚠ ${pf.length} URL(s) failed`);
    titles.push(pf.map((f) => `${f.url}: ${f.error || 'failed'}`).join('\n'));
  }
  if (r && r.output_dir_fallback) {
    const f = r.output_dir_fallback;
    lines.push(`⚠ saved to ${f.used} — requested folder unavailable`);
    titles.push(`Requested: ${f.requested}\n${f.error || ''}`);
  }
  if (r && r.degraded) {
    lines.push(`⚠ ${r.degraded.delivered || 'delivered a degraded result'} — processing failed`);
    titles.push((r.degraded.detail || ''));
  }
  return { lines, titles };
}

/**
 * Message for a bulk pipeline action, from the count the SERVER returned. Pure, so the zero case is
 * testable: both buttons used to claim success even when they changed nothing.
 */
function pipelineActionMsg(kind, n) {
  if (n === 0) return kind === 'retry' ? 'nothing to retry — no failed jobs'
                                       : 'nothing to clear — no finished jobs';
  if (typeof n !== 'number') return kind === 'retry' ? 'retried failed jobs' : 'cleared finished jobs';
  const s = n === 1 ? '' : 's';
  return kind === 'retry' ? `re-queued ${n} failed job${s}` : `cleared ${n} finished job${s}`;
}

// re-run with tweaks: clone this job's durable spec into a fresh queued job. Shared by the
// per-job "↻ re-run" button and the age-gate banner's "Retry" button (same action, two entry points).
const rerunsInFlight = new Set();
const rerunRequestIds = new Map();
const rerunOwners = new Map();

function newRequestId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  if (typeof crypto === 'undefined' || typeof crypto.getRandomValues !== 'function') {
    throw new Error('Secure request identifiers are unavailable in this browser');
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-`
    + `${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function setRerunControlsDisabled(sourceId, disabled) {
  const source = sourceId == null ? null : String(sourceId);
  document.querySelectorAll('[data-rerun-source]').forEach((control) => {
    if (source == null || control.dataset.rerunSource === source) {
      control.disabled = !!disabled;
    }
  });
}

function markRerunControl(control, sourceId) {
  const source = String(sourceId);
  control.dataset.rerunSource = source;
  control.disabled = rerunsInFlight.has(source);
  return control;
}

function resetRerunState() {
  rerunsInFlight.clear();
  rerunRequestIds.clear();
  rerunOwners.clear();
  setRerunControlsDisabled(null, false);
}

async function rerunJob(id) {
  const sourceId = String(id);
  if (rerunsInFlight.has(sourceId)) return;
  const action = beginAuthenticatedAction();
  let requestId = rerunRequestIds.get(sourceId);
  if (!requestId) {
    requestId = newRequestId();
    rerunRequestIds.set(sourceId, requestId);
  }
  const owner = {};
  rerunsInFlight.add(sourceId);
  rerunOwners.set(sourceId, owner);
  setRerunControlsDisabled(sourceId, true);
  try {
    const response = await api('/api/jobs/rerun', {
      method: 'POST',
      body: JSON.stringify({ id, request_id: requestId }),
    });
    action.guard();
    if (!response || typeof response !== 'object' || Array.isArray(response)
        || typeof response.id !== 'string' || !response.id.trim()
        || response.request_id !== requestId) {
      throw new Error('Rerun acknowledgement was incomplete — retry to confirm');
    }
    if (rerunOwners.get(sourceId) !== owner) return;
    rerunRequestIds.delete(sourceId);
    refresh();
  } catch (e) {
    if (action.isSilentError(e)) return;
    pipelineHint(false, e.message || 'rerun failed');
  } finally {
    if (rerunOwners.get(sourceId) === owner) {
      rerunOwners.delete(sourceId);
      rerunsInFlight.delete(sourceId);
      if (action.isCurrent()) setRerunControlsDisabled(sourceId, false);
    }
  }
}

async function bumpJob(id) {
  const action = beginAuthenticatedAction();
  try {
    await api('/api/priority', { method: 'POST', body: JSON.stringify({ id, priority: 100 }) });
    action.guard();
    refresh();
  } catch (e) {
    if (action.isSilentError(e)) return;
    pipelineHint(false, e.message || 'bump failed');
  }
}

/**
 * openFolderErrorNote — what to tell the user when "open folder" fails.
 *
 * PURE (the error is the argument) so the branches can be EXECUTED in a truth table.
 * Three distinct causes, which used to collapse into two:
 *  - the request hit its deadline: the server thread is stuck in a blocking shell call (a hung
 *    Explorer, or a path on an unreachable drive). The button IS usable again, and saying so matters
 *    because the disable is real now that cards are not rebuilt every poll.
 *  - HTTP 404: an older serve process without this route. Name the LAUNCHERS, not one platform's
 *    interpreter.
 *  - anything else: pass the message through.
 */
function openFolderErrorNote(err) {
  const name = (err && err.name) || '';
  const msg = String((err && err.message) || '');
  if (name === 'TimeoutError' || name === 'AbortError') {
    return 'no answer after 30s — the file manager may be stuck, or the folder is on a drive that is '
      + 'not responding. The button works again; the file itself is fine.';
  }
  if (/not found/i.test(msg)) {
    return 'open-folder needs a newer app — quit and restart it '
      + '(Start yt_final.bat on Windows, Start yt_final.command on macOS)';
  }
  return msg || 'could not open folder';
}

/** Corner × — remove job from the pipeline list (aborts if still running). */
async function dismissJob(id, el) {
  const action = beginAuthenticatedAction();
  try {
    await api('/api/remove', { method: 'POST', body: JSON.stringify({ id }) });
    action.guard();
    if (el && el.parentNode) el.remove();
    cards.delete(id);
    refresh();
  } catch (e) {
    if (action.isSilentError(e)) return;
    pipelineHint(false, e.message || 'remove failed');
  }
}

function upsertCard(j) {
  let el = cards.get(j.id);
  const isNew = !el;
  if (isNew) {
    el = document.createElement('div');
    el.dataset.id = j.id;
    el.innerHTML = CARD_HTML;
    el.style.animation = 'rise .4s cubic-bezier(.2,.7,.2,1) both';  // entrance ONLY on first insert
    cards.set(j.id, el);
  }
  const wasHidden = el.hidden;
  const prevStatus = el.dataset.status;
  // Only on a real status change (see cardClassNeedsRewrite). This assignment is a full REPLACE, so
  // it WIPES the derived has-result / is-degraded flags — and re-adding those now lives behind the
  // unchanged-content guard below, so rewriting it on every 1.2s poll silently stripped the amber
  // pill off a partly-failed job one poll after it appeared. j.status is inside contentSig, so
  // whenever this does rewrite, the guard below also runs and re-applies the flags.
  if (cardClassNeedsRewrite(isNew, prevStatus, j.status)) el.className = 'job ' + j.status;  // no re-animate
  el.dataset.status = j.status || '';
  // Announce done/failed/cancelled once, using the transition we just observed. Must sit here, above
  // the unchanged-content early return, or a terminal transition could go unannounced.
  announceJobStatus(j, prevStatus);
  const newHidden = !matchesJobFilter(j.status, jobFilter);
  el.hidden = newHidden;

  // Cards hidden by the active pipe filter that haven't changed status don't need any further
  // work until they might become visible again — cuts per-poll cost for large/filtered job lists.
  if (!isNew && wasHidden && newHidden && prevStatus === j.status) {
    return el;
  }

  const { title, sub } = shortJobLabel(j);
  const src = jobSourceUrl(j);

  // Signature for the expensive label/output/error/agegate/actions subtrees. These depend only on
  // job identity/result fields, not the fast-changing progress payload — skipping their rebuild
  // when unchanged also preserves transient UI state (e.g. the error 'more/less' expand toggle),
  // which was previously reset every ~1.2s poll even while nothing about the job had changed.
  const contentSig = JSON.stringify([
    j.status, j.last_error, j.last_error_kind, j.output_paths, j.attempt_count, j.priority, j.label, title, sub, src,
    j.cancel_requested || false, j.result || null,
  ]);
  const contentUnchanged = el._lastContentSig === contentSig;
  el._lastContentSig = contentSig;

  if (!contentUnchanged) {
    const labelEl = el.querySelector('.job-label');
    labelEl.innerHTML = '';
    const main = document.createElement(src ? 'a' : 'span');
    if (src) { main.href = src; main.target = '_blank'; main.rel = 'noopener noreferrer'; }
    main.textContent = title;
    main.title = j.label || title;
    labelEl.appendChild(main);
    if (sub) {
      const subEl = document.createElement('span');
      subEl.className = 'job-sub';
      subEl.textContent = sub;
      subEl.title = j.label || sub;
      labelEl.appendChild(subEl);
    }
  }

  // Guarded: every write here derives ONLY from fields already in contentSig (status, id,
  // attempt_count, priority), so on an unchanged card they all rewrite the same values. They used to
  // run on every visible card on every 1.2s poll — including re-assigning two event handlers.
  if (!contentUnchanged) {
    const pill = el.querySelector('.pill'); pill.className = 'pill ' + j.status; pill.textContent = j.status;
    const dismiss = el.querySelector('.job-dismiss');
    if (dismiss) {
      dismiss.title = j.status === 'running'
        ? 'Stop and remove this job from the list'
        : 'Remove from list';
      dismiss.onclick = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        dismissJob(j.id, el);
      };
    }
    const idEl = el.querySelector('.id');
    idEl.textContent = j.id;
    idEl.onclick = () => copyText(j.id, idEl);
    const att = j.attempt_count ? `attempt ${j.attempt_count}` : '';
    const prio = j.priority ? `p${j.priority}` : '';
    el.querySelector('.att').textContent = [att, prio].filter(Boolean).join(' · ');
  }

  const p = j.progress || null;
  const r = j.result || null;
  el.dataset.start = (p && p.started) ? p.started : (j.updated_at || '');
  const fill = el.querySelector('.fill');
  const stats = el.querySelector('.pstats');
  const resultEl = el.querySelector('.job-result');
  const updated = el.querySelector('.updated');
  // Guarded for the same reason: the result row and the caveat row derive only from j.result, which
  // IS in contentSig. The progress block below is NOT guarded — j.progress and j.updated_at are
  // deliberately absent from the signature, so live progress and the ageing "N ago" text must keep
  // rendering while the durable content is unchanged.
  if (!contentUnchanged) {
  // Dedicated always-visible result row (not the collapsible progress strip).
  const hasResult = !!(r && (r.engine || r.avg_speed || r.peak_speed));
  el.classList.toggle('has-result', hasResult);
  if (resultEl) {
    if (hasResult) {
      const bits = [];
      if (r.engine) bits.push('engine ' + r.engine);
      if (r.avg_speed) bits.push('avg ' + fmtSpeed(r.avg_speed));
      if (r.peak_speed) bits.push('peak ' + fmtSpeed(r.peak_speed));
      if (r.network_hint) bits.push(String(r.network_hint).split(' · ')[0]);
      if (r.throttle_suspected) bits.push('⚠ possible throttle');
      resultEl.hidden = false;
      resultEl.textContent = bits.join('  ·  ');
      resultEl.title = [r.engine_reason, r.throttle_hint, r.network_hint].filter(Boolean).join(' · ') || '';
    } else if (j.status === 'cancelled') {
      resultEl.hidden = false;
      resultEl.textContent = 'cancelled';
      resultEl.title = '';
    } else if (j.status === 'done') {
      // Older jobs finished before stats were persisted — still show a line so the card isn't empty.
      resultEl.hidden = false;
      resultEl.textContent = 'engine / speed not recorded — re-run to capture';
      resultEl.title = 'This job finished before download stats were saved. Use re-run for a fresh capture.';
    } else {
      resultEl.hidden = true;
      resultEl.textContent = '';
      resultEl.title = '';
    }
  }
  // --- caveats on an otherwise-"done" job (see .job-partial in CARD_HTML) --------------------
  const partEl = el.querySelector('.job-partial');
  if (partEl) {
    const cav = jobCaveats(j, r);
    partEl.hidden = !cav.lines.length;
    partEl.textContent = cav.lines.join('\n');
    partEl.title = cav.titles.filter(Boolean).join('\n\n');
    el.classList.toggle('is-degraded', !!cav.lines.length);
  }
  }   // end of the content-derived guard
  if (j.status === 'running') {
    const pct = p && p.percent != null ? p.percent : null;
    fill.classList.toggle('indeterminate', pct == null);
    fill.style.width = (pct != null ? pct : 100) + '%';
    const engLabel = (p && p.engine) ? String(p.engine) : '';
    const engReason = (p && p.engine_reason) ? String(p.engine_reason) : '';
    if (j.cancel_requested) {
      stats.textContent = 'cancelling…';
      stats.title = engReason || 'cancel requested — aborting after current fragment';
    } else if (p && p.status === 'downloading') {
      const bits = [
        pct != null ? pct.toFixed(1) + '%' : '--%',
        fmtSpeed(p.speed),
        'ETA ' + fmtTime(p.eta),
        fmtBytes(p.downloaded) + ' / ' + fmtBytes(p.total),
      ];
      if (engLabel) bits.push('via ' + engLabel);
      if (p.network_hint) bits.push(String(p.network_hint).split(' · ')[0]); // short: cdn=mobile
      stats.textContent = bits.join('  ·  ');
      const tips = [engReason, p.network_hint].filter(Boolean);
      stats.title = tips.join(' · ') || (engLabel ? ('download engine: ' + engLabel) : '');
    } else if (p && p.status === 'finishing') {
      stats.textContent = engLabel
        ? ('finalizing — encode / remux…  ·  via ' + engLabel)
        : 'finalizing — encode / remux…';
      stats.title = engReason || '';
      fill.classList.remove('indeterminate'); fill.style.width = '100%';
    } else {
      stats.textContent = engLabel
        ? ('starting — resolving formats…  ·  via ' + engLabel)
        : 'starting — resolving formats…';
      stats.title = engReason || '';
    }
    if (updated) updated.textContent = '';
  } else {
    fill.classList.remove('indeterminate');
    fill.style.width = j.status === 'done' ? '100%' : '0';
    stats.textContent = '';
    stats.title = '';
    // Stopped jobs must not keep a stale running timer from a previous poll.
    const elapsed = el.querySelector('.elapsed');
    if (elapsed) elapsed.textContent = '';
    if (updated) {
      const ago = fmtAgo(j.updated_at);
      updated.textContent = ago ? ago : '';
      // The relative text above MUST be rewritten every poll — that is the whole point of "3m ago",
      // and it is why j.updated_at is deliberately absent from contentSig. The absolute timestamp
      // below is different: it is a pure function of j.updated_at, yet toLocaleString() is Intl-backed
      // and this ran for every visible card on every 1.2 s poll. So memoise it on its own key rather
      // than moving it behind the content guard, which would freeze it.
      if (el._lastUpdatedTs !== j.updated_at) {
        el._lastUpdatedTs = j.updated_at;
        updated.title = j.updated_at ? new Date(Number(j.updated_at) * 1000).toLocaleString() : '';
      }
    }
  }

  // GATE. Everything BELOW this line is skipped on the ~99% of polls where the job's content has not
  // changed — the output rows, the error block, the age-gate banner and the action buttons. That is
  // deliberate: it also preserves transient state (the error more/less toggle, keyboard focus, an
  // in-progress text selection of a long path) that a per-poll rebuild destroyed.
  //
  // So: anything that must run on EVERY poll belongs ABOVE this line, next to the progress and
  // relative-time writes. Code appended after the last block below will run on first paint and on
  // content changes, and then silently never again — with no error to notice.
  if (contentUnchanged) return el;

  const out = el.querySelector('.job-out');
  out.innerHTML = '';
  (j.output_paths || []).forEach((path, outIdx) => {
    const row = document.createElement('div');
    row.className = 'job-out-row';
    const name = document.createElement('span');
    name.className = 'job-out-name';
    name.textContent = '▸ ' + basename(path);
    name.title = path;
    const copy = document.createElement('button');
    copy.type = 'button'; copy.className = 'jact'; copy.textContent = 'copy path';
    copy.onclick = () => copyText(path, copy);
    const folder = document.createElement('button');
    folder.type = 'button'; folder.className = 'jact'; folder.textContent = 'open folder';
    folder.title = 'Reveal in File Explorer / Finder';
    folder.onclick = async () => {
      const action = beginAuthenticatedAction();
      const control = action.ownControl(folder);
      try {
        // Send job_id so the server can re-resolve from the journal if the path string is stale.
        const res = await api('/api/open-folder', {
          method: 'POST',
          body: JSON.stringify({ path, job_id: j.id, index: outIdx }),
          // The server handler ends in a BLOCKING ShellExecuteW / os.startfile on the request thread,
          // so a stuck Explorer or a path on an unreachable drive never settles. Without a deadline
          // the `finally` below never runs and this button stays disabled for the rest of the
          // session — and the disable is real now that the card is no longer rebuilt every 1.2 s.
          // 30 s rather than something tight: the failure being prevented is "forever", and a slow
          // network share genuinely can take a while to reveal.
          timeoutMs: 30000,
        });
        action.guard();
        if (!res.ok) pipelineHint(false, res.error || 'could not open folder');
        else pipelineHint(true, 'opened ' + (res.path || 'folder'));
      } catch (e) {
        if (!action.isCurrent()) return;
        pipelineHint(false, openFolderErrorNote(e));
      } finally { control.release(); }
    };
    row.append(name, copy, folder);
    out.appendChild(row);
  });

  const err = el.querySelector('.job-err');
  err.innerHTML = '';
  err.classList.remove('is-open');
  if (j.status === 'failed' && j.last_error) {
    const short = document.createElement('span');
    short.className = 'job-err-short';
    const fullText = String(j.last_error);
    const clipped = fullText.length > 140 ? fullText.slice(0, 140) + '…' : fullText;
    short.textContent = '✗ ' + clipped;
    err.appendChild(short);
    if (fullText.length > 140) {
      const full = document.createElement('span');
      full.className = 'job-err-full';
      full.textContent = '✗ ' + fullText;
      const tog = document.createElement('button');
      tog.type = 'button'; tog.className = 'job-err-toggle'; tog.textContent = 'more';
      tog.onclick = () => {
        err.classList.toggle('is-open');
        tog.textContent = err.classList.contains('is-open') ? 'less' : 'more';
      };
      err.append(full, tog);
    }
  }

  const gate = el.querySelector('.job-agegate');
  gate.innerHTML = '';
  if (j.status === 'failed' && j.last_error_kind === 'agegate') {
    const banner = document.createElement('div'); banner.className = 'agegate-banner';
    const msg = document.createElement('span');
    msg.textContent = '🔒 Sign-in required — open the Auth tab (or run `auth login` / import cookies on the host), then Retry.';
    const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'jact primary';
    retry.textContent = 'Retry';
    markRerunControl(retry, j.id);
    retry.onclick = () => rerunJob(j.id);
    banner.append(msg, retry);
    gate.append(banner);
  }

  const acts = el.querySelector('.job-acts');
  acts.innerHTML = '';
  if (src) {
    const open = document.createElement('button'); open.type = 'button'; open.className = 'jact'; open.textContent = 'open';
    open.onclick = () => window.open(src, '_blank', 'noopener,noreferrer');
    acts.append(open);
  }
  if (j.status === 'queued' || j.status === 'failed') {
    const bump = document.createElement('button'); bump.type = 'button'; bump.className = 'jact'; bump.textContent = 'bump';
    bump.title = 'Raise priority so this job runs sooner';
    bump.onclick = () => bumpJob(j.id);
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'jact'; cancel.textContent = 'cancel';
    cancel.onclick = () => postAction('/api/cancel', j.id);
    acts.append(bump, cancel);
  }
  // Running jobs: cooperative cancel (next progress tick aborts the download).
  if (j.status === 'running') {
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'jact danger';
    cancel.textContent = j.cancel_requested ? 'cancelling…' : 'cancel';
    cancel.disabled = !!j.cancel_requested;
    cancel.title = j.cancel_requested
      ? 'Cancel already requested — waiting for the download to abort'
      : 'Stop this download (aborts after the current fragment)';
    cancel.onclick = () => postAction('/api/cancel', j.id);
    acts.append(cancel);
  }
  // re-run with tweaks: clone this job's durable spec into a fresh queued job (any status except
  // running — re-queueing a running job is rarely intended and the server clones, never touches it).
  if (j.status !== 'running') {
    const rerun = document.createElement('button');
    rerun.type = 'button';
    rerun.className = 'jact' + (j.status === 'failed' ? ' primary' : '');
    rerun.textContent = j.status === 'failed' ? '↻ retry' : '↻ re-run';
    markRerunControl(rerun, j.id);
    rerun.onclick = () => rerunJob(j.id);
    acts.append(rerun);
  }
  if (j.status === 'failed' && j.last_error) {
    const copyErr = document.createElement('button');
    copyErr.type = 'button'; copyErr.className = 'jact'; copyErr.textContent = 'copy error';
    copyErr.onclick = () => copyText(j.last_error, copyErr);
    acts.append(copyErr);
  }
  return el;
}

function setPipeFilter(name) {
  jobFilter = name || 'all';
  try { sessionStorage.setItem('yt_pipe_filter', jobFilter); } catch (e) {}
  document.querySelectorAll('#pipe_filters .pipe-filter').forEach((btn) => {
    const on = btn.dataset.filter === jobFilter;
    btn.classList.toggle('is-active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  let visible = 0;
  for (const [, el] of cards) {
    const show = matchesJobFilter(el.dataset.status, jobFilter);
    el.hidden = !show;
    if (show) visible++;
  }
  const box = $('jobs');
  if (box && cards.size && !visible) {
    let empty = box.querySelector('.empty-filter');
    if (!empty) {
      empty = document.createElement('div');
      empty.className = 'empty empty-filter';
      box.appendChild(empty);
    }
    empty.textContent = jobFilter === 'failed' ? 'no failed jobs'
      : jobFilter === 'done' ? 'no finished jobs'
      : jobFilter === 'active' ? 'nothing active — queue is idle'
      : 'no jobs in this filter';
  } else if (box) {
    const empty = box.querySelector('.empty-filter');
    if (empty) empty.remove();
  }
}

function updatePipeCounts(counts, jobs) {
  const c = counts || {};
  const active = (c.queued || 0) + (c.running || 0);
  const map = {
    all: jobs ? jobs.length : ((c.queued || 0) + (c.running || 0) + (c.done || 0) + (c.failed || 0) + (c.cancelled || 0)),
    active,
    failed: c.failed || 0,
    done: c.done || 0,
  };
  document.querySelectorAll('#pipe_filters .pipe-n').forEach((n) => {
    const key = n.dataset.n;
    if (key in map) n.textContent = String(map[key]);
  });
}

function renderJobs(data) {
  const jobs = (data.jobs || []).slice().reverse();   // newest first
  $('qcount').textContent = (data.counts.queued || 0) + (data.counts.running || 0);
  $('led').className = 'led' + (data.counts.running ? ' busy' : (data.counts.failed ? ' err' : ''));
  const pauseBtn = $('pause');
  if (pauseBtn) {
    pauseBtn.dataset.on = data.paused ? '1' : '0';
    pauseBtn.textContent = data.paused ? 'resume' : 'pause';
    pauseBtn.classList.toggle('on', !!data.paused);
  }
  const banner = $('pipe_banner');
  if (banner) {
    if (data.paused) {
      banner.hidden = false;
      banner.className = 'pipe-banner is-paused';
      banner.textContent = '⏸ Queue paused — running job finishes; nothing new starts until you resume.';
    } else {
      banner.hidden = true;
      banner.textContent = '';
    }
  }
  updatePipeCounts(data.counts || {}, jobs);
  const box = $('jobs');
  const empty = box.querySelector('.empty:not(.empty-filter)'); if (empty) empty.remove();
  if (!jobs.length) {
    box.innerHTML = '<div class="empty">'
      + '<div class="empty-art" aria-hidden="true">▶</div>'
      + '<p class="empty-title">No jobs yet</p>'
      + '<p class="empty-sub">Paste a YouTube URL in <strong>Compose</strong> and hit <strong>Queue job</strong> — live progress lands here.</p>'
      + '</div>';
    cards.clear();
    setPipeFilter(jobFilter);
    return;
  }
  const seen = new Set();
  jobs.forEach((j, i) => {
    const el = upsertCard(j); seen.add(j.id);
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);  // keep order, reuse nodes
  });
  for (const [id, el] of cards) { if (!seen.has(id)) { el.remove(); cards.delete(id); } }
  // drop stray filter-empty nodes that may sit after cards
  box.querySelectorAll('.empty-filter').forEach((n) => n.remove());
  setPipeFilter(jobFilter);
  tickElapsed();
}

function tickElapsed() {
  const now = Date.now() / 1000;
  for (const [, el] of cards) {
    if (!el.classList.contains('running')) continue;
    const start = parseFloat(el.dataset.start || '0');
    const span = el.querySelector('.elapsed');
    if (start && span) span.textContent = '⏱ ' + fmtTime(now - start);
  }
}

// filter chips
(function wirePipeFilters() {
  const root = $('pipe_filters');
  if (!root) return;
  root.addEventListener('click', (e) => {
    const btn = e.target.closest('.pipe-filter');
    if (!btn) return;
    setPipeFilter(btn.dataset.filter || 'all');
  });
  // restore active chip styling before first poll paints
  setPipeFilter(jobFilter);
})();

let jobsAbort = null;
// Shared handler for background pollers: ignore aborts, but surface a revoked/expired session
// (401) as a re-login prompt instead of silently doing nothing.
let _pollAuthLost = false;
let _pollFailStreak = 0;
/** Show/clear the "we lost the server" state. The elapsed-time clock runs off its OWN timer and
 *  keeps counting with no server involvement, so without this a killed server (closing the launcher
 *  console — the normal way to quit on Windows) left a climbing timer, a pulsing progress bar and a
 *  busy LED all asserting the download was still progressing. */
let _bannerBeforeStale = null;   // pipe banner overwritten by the lost-contact message, if any
function setStaleContact(on) {
  const jobs = $('jobs');
  if (jobs) jobs.classList.toggle('is-stale', !!on);
  const led = $('led');
  if (led) {
    led.classList.toggle('stale', !!on);
    // The LED is the only always-visible health indicator; give it the reason, not just the colour.
    led.title = on ? 'lost contact with the app' : 'queue status';
  }
  // Shell-level notice. Everything below writes into #view-workspace, which is hidden on the other
  // five views — so a server that died while the user was on Watch, Auth, Speed test, Updates or the
  // Guide showed nothing at all. This element lives in the header, outside every view. Note this
  // function runs with `false` on every healthy poll, so the writes stay guarded and cheap.
  const shell = $('conn_banner');
  if (shell) {
    const stxt = '⚠ Lost contact with the app — it may have been closed or crashed. '
      + 'Everything on screen is frozen until it answers again.';
    if (on) {
      if (shell.textContent !== stxt) shell.textContent = stxt;   // don't replay the entrance anim
      shell.hidden = false;
    } else if (!shell.hidden) {
      shell.hidden = true;
      shell.textContent = '';
    }
  }
  // The queue gauge is server-derived, so a frozen count reads as current fact. Blanked while stale
  // and restored by the next successful render. The clock is deliberately left alone: it shows local
  // time, which stays true whatever the server is doing.
  const qc = $('qcount');
  if (qc && on) qc.textContent = '—';
  const banner = $('pipe_banner');
  if (banner && on) {
    const txt = '⚠ Lost contact with the app — numbers below are frozen. Is it still running?';
    if (banner.textContent !== txt) {
      // Remember what we are overwriting (e.g. '⏸ Queue paused …'). renderJobs only repaints on a
      // 200, and a paused idle queue keeps a stable ETag, so a 304-restored connection used to
      // leave the paused banner gone with only the button label still saying so.
      _bannerBeforeStale = { text: banner.textContent, className: banner.className,
                             hidden: banner.hidden };
      banner.textContent = txt;   // don't replay the entrance anim when it's already this text
    }
    banner.hidden = false;
  } else if (banner && banner.textContent.startsWith('⚠ Lost contact')) {
    if (_bannerBeforeStale && _bannerBeforeStale.text && !_bannerBeforeStale.hidden) {
      banner.textContent = _bannerBeforeStale.text;      // put the pre-stale banner back
      banner.className = _bannerBeforeStale.className;
      banner.hidden = false;
      _bannerBeforeStale = null;
      return;
    }
    _bannerBeforeStale = null;
    banner.hidden = true;
    banner.textContent = '';
  }
}
function handlePollError(e) {
  if (!e || e.name === 'AbortError') return;
  if (/\b401\b|session expired/i.test(e.message || '')) {
    if (_pollAuthLost) return;
    _pollAuthLost = true;
    sessionLost('Session expired — sign in again');
    return;
  }
  // Transport failure (server quit/crashed, machine slept). Tolerate a single blip, then say so.
  _pollFailStreak += 1;
  if (_pollFailStreak >= 3) setStaleContact(true);
}
let jobsEtag = null;
let jobsInFlight = false;
/**
 * Poll the job list.
 *
 * `force` (user-initiated: after queue/cancel/rerun) may pre-empt an in-flight poll. The TIMER path
 * must not: aborting unconditionally meant that any /api/jobs slower than the fixed 1.2s interval
 * was killed by the next tick before it could render — forever, with the AbortError swallowed. The
 * pipeline froze on stale progress with no error. Very reachable on Windows, where an AV scan of
 * queue.json contends with the store's file lock.
 */
async function refresh(opts = {}) {
  if (jobsInFlight && !opts.force) return;          // let the slow poll finish instead of killing it
  if (opts.force && jobsAbort) jobsAbort.abort();
  jobsAbort = new AbortController();
  jobsInFlight = true;
  // Ownership token: a pre-empted poll's `finally` runs a microtask LATER than the forced poll
  // that replaced it, and action.effect() only filters auth-session changes — the pre-empted poll
  // is still "current", so it cleared the successor's guard. That let the next timer tick start a
  // duplicate concurrent /api/jobs whose response could render out of order. The controller
  // identity IS the token: a newer poll installs its own, so only the owner may clear the flag.
  const myAbort = jobsAbort;
  const action = beginAuthenticatedAction();
  try {
    // Conditional GET: the server 304s when nothing changed, so idle polls skip the JSON parse
    // AND the whole job-list re-render. (Raw fetch, not api(), because api() throws on a 304.)
    const headers = {};
    if (jobsEtag) headers['If-None-Match'] = jobsEtag;
    const res = await sessionFetch('/api/jobs', { headers, signal: jobsAbort.signal });
    action.guard();
    if (res.status === 304) {                                    // unchanged — nothing to do
      _pollAuthLost = false; _pollFailStreak = 0; setStaleContact(false);
      return;
    }
    if (!res.ok) { handlePollError(new Error('HTTP ' + res.status)); return; }
    jobsEtag = res.headers.get('ETag') || jobsEtag;
    const body = await sessionJson(res);
    action.guard();
    renderJobs(body);
    _pollAuthLost = false; _pollFailStreak = 0; setStaleContact(false);
  } catch (e) {
    if (!action.isSilentError(e)) handlePollError(e);
  } finally {
    action.effect(() => { if (jobsAbort === myAbort) jobsInFlight = false; });
  }
}

// ---- watch channels (auto-detect new uploads) ----
function agoStr(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  return Math.round(s / 3600) + 'h ago';
}

// ---- watch render (keyed, in-place — never replaces rows/buttons mid-action) ----
const watchCards = new Map();        // watch id -> row element (reused across polls)
const watchActionsInFlight = new Set(); // watch ids with an awaited action running

const WATCH_ROW_HTML = `
  <div class="watch-top">
    <span class="watch-label"></span>
    <span class="wp-auto"></span>
    <span class="watch-meta"></span>
    <div class="watch-acts">
      <button type="button" class="ghost" data-act="poll">Check for new</button>
      <button type="button" class="ghost" data-act="toggle"></button>
      <button type="button" class="ghost" data-act="remove">Stop watching</button>
    </div>
  </div>
  <div class="watch-err" hidden></div>
  <div class="watch-body"></div>`;

/**
 * watchRowSig — everything the row's rebuilt markup is derived from, and nothing else.
 *
 * PURE, so the inclusion AND exclusion lists can be executed as a truth table. That matters more here
 * than usual: getting this wrong does not throw, it silently freezes a row — a newly-pending video
 * would never appear. The mutations in the test prove both directions.
 *
 * IN: auto_queue (drives the pill, the toggle button and which body branch renders), label (the title
 * plus every button's title/aria text), and the pending list as [id, title] pairs (the body markup,
 * including one Download button per video — a retitled video must re-render).
 * OUT, deliberately: last_checked, because "Last checked 4m ago" AGES on its own and is written
 * outside the guard as the row's visible heartbeat; last_error, which is cheap and independent; and
 * watchActionsInFlight, which is runtime state rather than server data.
 */
function watchRowSig(w) {
  return JSON.stringify([
    !!(w && w.auto_queue),
    (w && w.label) || '',
    (((w && w.pending) || [])).map((v) => [(v && v.id) || '', (v && v.title) || '']),
  ]);
}

function upsertWatchRow(w) {
  const auto = w.auto_queue;
  const pend = w.pending || [];
  let el = watchCards.get(w.id);
  if (!el) {
    el = document.createElement('div');
    el.className = 'watch-row';
    el.dataset.id = w.id;
    el.innerHTML = WATCH_ROW_HTML;
    watchCards.set(w.id, el);
  }

  // ALWAYS: this ages on its own, so it must be rewritten every 5s poll regardless of the guard
  // below — and it doubles as the row's visible heartbeat, so a frozen body cannot hide next to a
  // stopped clock.
  // textContent already escapes; build meta from parts to keep XSS parity with esc()
  el.querySelector('.watch-meta').textContent = `Last checked ${agoStr(w.last_checked)}`;

  // ALWAYS: last_error changes independently of anything the signature covers, and it is two cheap
  // writes. Inside the guard it would freeze — an error that appeared or cleared would not show.
  const errEl = el.querySelector('.watch-err');
  if (w.last_error) {
    errEl.hidden = false;
    errEl.textContent = w.last_error;
  } else {
    errEl.hidden = true;
    errEl.textContent = '';
  }

  // Everything from here to the end of the body rebuild derives ONLY from what watchRowSig covers, so
  // it is skipped when nothing relevant changed. The rebuild destroyed and recreated every button in
  // the row on every 5s poll, which dropped keyboard focus to <body> mid-tab and cancelled any
  // in-progress text selection of a video title.
  const sig = watchRowSig(w);
  const sigUnchanged = el._lastWatchSig === sig;
  el._lastWatchSig = sig;
  if (!sigUnchanged) {

  // top line ------------------------------------------------------------
  el.querySelector('.watch-label').textContent = w.label;
  const autoPill = el.querySelector('.wp-auto');
  autoPill.className = 'wp-auto ' + (auto ? 'on' : 'off');
  autoPill.textContent = auto ? 'Auto-download' : 'Ask me first';

  // per-row buttons carry the watch id + toggle target ------------------
  const btnPoll = el.querySelector('button[data-act="poll"]');
  const btnToggle = el.querySelector('button[data-act="toggle"]');
  const btnRemove = el.querySelector('button[data-act="remove"]');
  btnPoll.dataset.id = w.id;
  btnRemove.dataset.id = w.id;
  btnToggle.dataset.id = w.id;
  btnToggle.dataset.auto = auto ? 0 : 1;
  btnToggle.textContent = auto ? 'Turn off auto-download' : 'Turn on auto-download';
  const labelSafe = w.label || 'channel';
  btnPoll.title = `Check ${labelSafe} for new uploads`;
  btnPoll.setAttribute('aria-label', `Check ${labelSafe} for new uploads`);
  btnToggle.title = auto
    ? `Switch ${labelSafe} to ask me first (list new videos instead of downloading)`
    : `Turn on auto-download for ${labelSafe}`;
  btnToggle.setAttribute('aria-label', btnToggle.title);
  btnRemove.title = `Stop watching ${labelSafe}`;
  btnRemove.setAttribute('aria-label', `Stop watching ${labelSafe}`);

  // body: auto note, or pending block ----------------------------------
  const body = el.querySelector('.watch-body');
  if (auto) {
    body.innerHTML = '<div class="watch-pending auto">New videos download automatically when they appear.</div>';
  } else if (pend.length) {
    // Server keeps newest-first; reverse only if an old store still has oldest-first
    // (detect: if first item has no published and list is long, still trust server order).
    const list = pend;
    const n = list.length;
    body.innerHTML = `<div class="watch-pending">
        <div class="pending-head">${n} new video${n === 1 ? '' : 's'} waiting — not downloading yet
          <button type="button" class="ghost" data-act="queueall" data-id="${w.id}" aria-label="Download all ${n} waiting videos for ${esc(labelSafe)}">Download all</button>
          <button type="button" class="ghost" data-act="dismiss" data-id="${w.id}" title="Mark all as seen — won’t ask again" aria-label="Skip all waiting videos for ${esc(labelSafe)}">Skip all</button>
        </div>
        <div class="pending-helper">Uses this channel’s download settings. Skip all = ignore forever (won’t reappear).</div>
        ${list.map(v => {
          const t = v.title || v.id || 'video';
          return `<div class="pending-vid"><span class="pv-title">▸ ${esc(t)}</span>
          <button type="button" class="ghost" data-act="queue1" data-id="${w.id}" data-vid="${esc(v.id)}" aria-label="Download ${esc(t)}">Download</button></div>`;
        }).join('')}
      </div>`;
  } else {
    body.innerHTML = '<div class="watch-pending auto">Up to date — nothing waiting.</div>';
  }

  }  // end of the signature guard

  // ALWAYS: watchActionsInFlight is runtime state, not server data, so it is not in the signature — a
  // poll landing mid-action must not leave this row's buttons clickable. When the body WAS rebuilt the
  // buttons are fresh and enabled; when it was skipped they keep whatever state they had. Assigning
  // unconditionally is correct either way.
  const locked = watchActionsInFlight.has(w.id);
  el.querySelectorAll('button[data-act]').forEach(b => { b.disabled = locked; });
  return el;
}

function renderWatches(watches) {
  const box = $('watches');
  if (!watches.length) {
    box.innerHTML = '<div class="empty">'
      + '<div class="empty-art" aria-hidden="true">📡</div>'
      + '<p class="empty-title">No channels watched yet</p>'
      + '<p class="empty-sub">Paste a channel link above and <strong>Start watching</strong> — new uploads will queue up here for you to approve.</p>'
      + '</div>';
    watchCards.clear();
    return;
  }
  const empty = box.querySelector('.empty'); if (empty) empty.remove();
  const seen = new Set();
  watches.forEach((w, i) => {
    const el = upsertWatchRow(w); seen.add(w.id);
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);  // keep order, reuse nodes
  });
  for (const [id, el] of watchCards) { if (!seen.has(id)) { el.remove(); watchCards.delete(id); } }
}

/** Normalize published-after input to YYYYMMDD digits when user types YYYY-MM-DD. */
function normalizeWatchAfter(raw) {
  const s = (raw || '').trim();
  if (!s) return undefined;
  // A YYYY-M-D style date may omit zero-padding on month/day; pad it to a real
  // 8-digit YYYYMMDD so the server's lexicographic date compare stays correct.
  const parts = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (parts) return parts[1] + parts[2].padStart(2, '0') + parts[3].padStart(2, '0');
  const digits = s.replace(/\D/g, '');
  if (digits.length >= 8) return digits.slice(0, 8);
  return s;
}

let watchesAbort = null;
let watchesInFlight = false;
/**
 * Poll the watch list. Only a user-initiated (`force`) call may pre-empt an in-flight poll — the
 * 5s interval must NOT. Aborting unconditionally on every tick meant any /api/watches slower than
 * the interval was killed by the next one before it could render, forever, with the AbortError
 * swallowed: stale rows and no error. That is the identical failure refresh() documents for
 * /api/jobs (Windows AV contending with the store's file lock is this project's own precedent).
 */
async function refreshWatches(opts = {}) {
  const force = !!(opts && opts.force);
  if (watchesInFlight && !force) return;      // let the slow poll finish instead of killing it
  if (force && watchesAbort) watchesAbort.abort();
  watchesAbort = new AbortController();
  watchesInFlight = true;
  const myAbort = watchesAbort;               // ownership token (see refresh())
  const action = beginAuthenticatedAction();
  try {
    const body = await api('/api/watches', { signal: watchesAbort.signal });
    action.guard();
    renderWatches(body.watches || []);
  }
  catch (e) {
    if (action.isSilentError(e)) return;
    handlePollError(e);
    // handlePollError only marks the shared stale-contact state. If the list never loaded there are no
    // rows to look stale, so the static "No channels watched yet" empty state stands — telling a user
    // who HAS watches that they have none. Say it could not be loaded instead. Aborts are ours (a
    // newer refresh superseded this one) and must stay silent.
    if (!(e && (e.name === 'AbortError' || e.name === 'TimeoutError'))) {
      const hint = $('watch_hint');
      if (hint && !$('watches').querySelector('.watch-row')) {
        hint.className = 'hint bad';
        hint.textContent = '✗ could not load your watched channels — ' + ((e && e.message) || 'the app is not answering');
      }
    }
  } finally {
    action.effect(() => { if (watchesAbort === myAbort) watchesInFlight = false; });
  }
}

$('watch_add').onclick = async () => {
  const action = beginAuthenticatedAction();
  const url = $('watch_channel').value.trim();
  const hint = $('watch_hint');
  if (!url) { hint.className = 'hint bad'; hint.textContent = 'Enter a channel link or @handle'; return; }
  const control = action.ownControl($('watch_add'));
  hint.className = 'hint'; hint.textContent = 'Looking up channel and checking for recent videos…';
  try {
    // omit empty selects/inputs (don't send '' — the server uses `payload.get(x) or default`,
    // but explicit undefined keeps the body clean and lets server defaults apply).
    // Profile only applies when re-encoding; empty select = remux (encode_mode copy).
    // Without encode_mode the server always stores copy and ProRes/DNxHR/H.264 profiles are ignored.
    const profile = $('watch_profile').value || '';
    const body = {
      url, auto_queue: $('watch_autoqueue').checked,
      quality: $('watch_quality').value, no_shorts: $('watch_noshorts').checked,
      encode_mode: profile ? 'reencode' : 'copy',
      profile: profile || undefined,
      write_nfo: $('watch_nfo').checked,
      loudnorm: $('watch_loudnorm').checked ? -14.0 : undefined,
      export_markers: $('watch_markers').value || undefined,
      organize_by: $('watch_organize').value || undefined,
      filter_title: $('watch_filter_title').value.trim() || undefined,
      after: normalizeWatchAfter($('watch_after').value),
      min_duration: $('watch_min_dur').value !== '' ? Number($('watch_min_dur').value) : undefined,
      max_duration: $('watch_max_dur').value !== '' ? Number($('watch_max_dur').value) : undefined,
    };
    const w = await api('/api/watches', { method: 'POST', body: JSON.stringify(body) });
    action.guard();
    hint.className = 'hint ok';
    // First poll is fire-and-forget + baseline-first (n=0, pending usually empty on create).
    // Don't claim "N new ready" when nothing is staged yet.
    const n = (w.pending || []).length;
    if (w.auto_queue) {
      hint.textContent = `Watching ${w.label} — new uploads will download automatically`;
    } else if (n > 0) {
      hint.textContent = `Watching ${w.label} — ${n} new video${n === 1 ? '' : 's'} ready below`;
    } else {
      hint.textContent = `Watching ${w.label} — new uploads will show up here when they appear`;
    }
    $('watch_channel').value = '';
    $('watch_filter_title').value = ''; $('watch_min_dur').value = '';
    $('watch_max_dur').value = ''; $('watch_after').value = '';
    refreshWatches({ force: true }); refresh({ force: true });
  } catch (e) {
    if (action.isSilentError(e)) return;
    hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message;
  }
  finally { control.release(); }
};

// Enter-to-submit on the single-line happy-path inputs (render-only convenience).
if ($('watch_channel')) {
  $('watch_channel').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('watch_add').click(); }
  });
}
if ($('output_dir')) {
  $('output_dir').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('submit').click(); }
  });
}

/**
 * watchActionMsg — turn a watch route's RESPONSE into what the user is told.
 *
 * Every one of these routes already returns the truth and the client used to throw all of it away,
 * so a "Download all" that queued nothing looked exactly like one that queued everything: the rows
 * simply re-rendered. Same class the pipeline fixed with pipelineActionMsg().
 *
 * PURE (response and expected count are arguments) so the truth table can be EXECUTED in node —
 * these are branch decisions, and a source assertion cannot verify a branch.
 *
 * The shapes, read off the routes rather than assumed:
 *   poll     -> {new: n, mode} | {new: 0, mode: null, timeout: true}   (webserver.py:2877-2885)
 *   queue    -> {queued: n}                                            (webserver.py:2922)
 *   dismiss  -> {dismissed: n}                                         (webserver.py:2926)
 *   remove   -> {removed: bool}                                        (webserver.py:2952)
 *   update   -> the watch record; nothing to count, so report the state we asked for.
 * `expected` is how many pending videos the row showed when the button was clicked, which is what
 * makes the partial case ("3 of 5") sayable at all.
 */
function watchActionMsg(act, res, expected) {
  const r = res || {};
  const n = (v) => (typeof v === 'number' && isFinite(v) ? v : null);
  const want = n(expected);
  if (act === 'poll') {
    if (r.timeout) return { level: 'warn', note: 'still checking — this feed is slow to answer' };
    const got = n(r.new);
    if (got === null) return { level: 'ok', note: 'checked for new videos' };
    return got
      ? { level: 'ok', note: 'found ' + got + ' new video' + (got === 1 ? '' : 's') }
      : { level: 'ok', note: 'no new videos' };
  }
  if (act === 'queueall' || act === 'queue1') {
    const got = n(r.queued);
    if (got === null) return { level: 'ok', note: 'queued' };
    if (got === 0) {
      return { level: 'warn', note: act === 'queue1'
        ? 'that video could not be queued — see the log'
        : 'nothing could be queued — see the log' };
    }
    // the partial case is the one that used to vanish silently
    if (act === 'queueall' && want !== null && got < want) {
      return { level: 'warn', note: 'queued ' + got + ' of ' + want + ' — ' + (want - got) + ' could not be queued' };
    }
    return { level: 'ok', note: 'queued ' + got + ' video' + (got === 1 ? '' : 's') };
  }
  if (act === 'dismiss') {
    const got = n(r.dismissed);
    if (got === null) return { level: 'ok', note: 'skipped' };
    return got
      ? { level: 'ok', note: 'skipped ' + got + ' video' + (got === 1 ? '' : 's') + ' — they will not come back' }
      : { level: 'warn', note: 'nothing to skip' };
  }
  if (act === 'remove') {
    return r.removed === false
      ? { level: 'warn', note: 'that watch was already gone' }
      : { level: 'ok', note: 'stopped watching' };
  }
  if (act === 'toggle') {
    return { level: 'ok', note: expected ? 'auto-download on' : 'auto-download off' };
  }
  return { level: 'ok', note: 'done' };
}

$('watches').addEventListener('click', async (e) => {
  const action = beginAuthenticatedAction();
  const btn = e.target.closest('button[data-act]'); if (!btn) return;
  const id = btn.dataset.id, act = btn.dataset.act;
  const post = (p, b) => api(p, { method: 'POST', body: JSON.stringify(b) });
  // Confirm the one irreversible action. It deletes the watch's download settings AND its record of
  // which videos it has already seen, so re-adding the channel is not the same as undoing this.
  // MUST come before watchActionsInFlight.add() and the row disable sweep below — cancelling after
  // those would leave the row's buttons dead until a later poll re-rendered them.
  if (act === 'remove') {
    const labelEl = (btn.closest('.watch-row') || document).querySelector('.watch-label');
    const who = (labelEl && labelEl.textContent) ? '“' + labelEl.textContent + '”' : 'this channel';
    if (!confirm('Stop watching ' + who + '?\n\n'
                 + 'Its download settings and its record of already-seen videos are deleted. '
                 + 'If you re-add the channel later, detection starts over. This cannot be undone.')) {
      return;
    }
  }
  watchActionsInFlight.add(id);   // poll-during-await must not re-enable this row
  // Disable every action in this row (not just the clicked one) so a second conflicting
  // action (e.g. remove while poll is in flight) can't fire against the same watch id.
  const row = btn.closest('.watch-row');
  const rowButtons = Array.from((row || btn).querySelectorAll('button[data-act]'));
  if (!rowButtons.includes(btn)) rowButtons.push(btn);
  const controls = rowButtons.map((button) => action.ownControl(button));
  // How many videos this row was offering BEFORE the action — the denominator that makes a partial
  // "queued 3 of 5" sayable. Counted off the DOM because the handler is delegated and has no `w`.
  const expected = row ? row.querySelectorAll('.pending-vid').length : null;
  const hint = $('watch_hint');
  const say = (level, note) => {
    if (!hint) return;
    hint.className = 'hint ' + (level === 'ok' ? 'ok' : level === 'warn' ? 'warn' : 'bad');
    hint.textContent = (level === 'ok' ? '✓ ' : level === 'warn' ? '⚠ ' : '✗ ') + note;
  };
  try {
    let res = null;
    const wantOn = btn.dataset.auto === '1';
    if (act === 'poll') res = await post('/api/watches/poll', { id });
    else if (act === 'toggle') res = await post('/api/watches/update', { id, auto_queue: wantOn });
    else if (act === 'remove') res = await post('/api/watches/remove', { id });
    else if (act === 'queueall') res = await post('/api/watches/queue', { id, video_ids: 'all' });
    else if (act === 'dismiss') res = await post('/api/watches/dismiss', { id, video_ids: 'all' });
    else if (act === 'queue1') res = await post('/api/watches/queue', { id, video_ids: [btn.dataset.vid] });
    action.guard();
    // Every route above returns a count or a flag; throwing it away is what made a no-op action
    // indistinguishable from a successful one.
    const m = watchActionMsg(act, res, act === 'toggle' ? wantOn : expected);
    say(m.level, m.note);
  } catch (err) {
    // Was an empty block: a 500 from a watches.json permission error, or a dropped connection,
    // produced literally no UI at all. sessionFetch() already tears down a lost (401) session
    // before api() throws, so re-showing its message here is harmless.
    if (action.isSilentError(err)) return;
    say('bad', (err && err.message) || 'that action failed');
  } finally {
    controls.forEach((control) => control.release());
    action.effect(() => {
      watchActionsInFlight.delete(id);
      refreshWatches({ force: true }); refresh({ force: true });
    });
  }
});

// ---- host OS detection (global — header + Updates FFmpeg tabs) ----
/** Server-reported host: 'Windows' | 'macOS' | 'Linux' | null until /api/health. */
let HOST_OS = null;
/** Server sys.platform string, e.g. 'win32' | 'darwin'. */
let HOST_PLATFORM = null;
/**
 * 'this Mac' / 'this PC' / 'this machine' for prose about the SERVER's browsers and cookies.
 * Derived from HOST_OS (server-reported), never from navigator/UA: the browser may be on a phone
 * on the LAN while the host is Windows, so the UA would describe the wrong device. Falls back to
 * the neutral 'this machine' before /api/health lands, so it is never wrong — only vaguer.
 * `caps` gives the sentence-initial form ("This PC: …").
 */
function hostDeviceLabel(caps = false) {
  const os = String(HOST_OS || '');
  const label = /mac|darwin/i.test(os) ? 'this Mac'
    : /win/i.test(os) ? 'this PC'
      : 'this machine';
  return caps ? label.charAt(0).toUpperCase() + label.slice(1) : label;
}
/** Managed FFmpeg location strings keyed by OS label. */
let HOST_MANAGED_LOCATIONS = null;

/** Advanced → Performance: whether yt-dlp's pooled transport is active (from /api/health). Unpooled = slow. */
function applyTransport(info) {
  const el = $('transport');
  if (!el) return;
  if (info && info.pooled_transport === true) {
    el.textContent = 'pooled ✓';
    el.title = 'yt-dlp is using its pooled requests transport (persistent connections) — full speed.';
  } else if (info && info.pooled_transport === false) {
    el.textContent = '⚠ unpooled — update release';
    el.title = 'Missing requests/urllib3 → unpooled urllib transport (~+33–67% slower). Install a future reviewed yt_final release.';
  } else {
    el.textContent = '—';
  }
}

function applyHostOs(info, { refreshUpdatesPanel = true } = {}) {
  if (!info) return;
  // Capture the next presentation state before changing the globals. Recomputing the old state from
  // _updatesReport afterwards would false-green because hostOsFromReport() falls back to HOST_OS.
  const cachedReport = _updatesReport;
  const cachedFfmpeg = (cachedReport && cachedReport.info || []).find(
    (row) => row.name === 'ffmpeg',
  ) || null;
  const nextHostOs = info.host_os
    || HOST_OS
    || (cachedReport && cachedReport.host_os)
    || null;
  const nextHostPlatform = info.host_platform
    || HOST_PLATFORM
    || (cachedReport && cachedReport.host_platform)
    || null;
  const nextLocations = info.managed_locations
    || HOST_MANAGED_LOCATIONS
    || (cachedReport && cachedReport.managed_locations)
    || null;
  const nextSelectedOs = _updatesOsTab === 'Windows' || _updatesOsTab === 'macOS'
    ? _updatesOsTab
    : (nextHostOs === 'macOS' ? 'macOS' : 'Windows');
  const nextSetupLocation = cachedFfmpeg
    ? (nextSelectedOs === 'Windows'
      ? ((nextLocations && nextLocations.Windows) || cachedFfmpeg.managed_location || '')
      : 'Manual / system installation')
    : null;
  const nextHostManagedLocation = nextHostOs === 'Windows'
    ? ((nextLocations && nextLocations.Windows)
      || (cachedReport && cachedReport.managed_location)
      || '—')
    : 'Manual / system installation';
  const nextPresentation = {
    hostOs: nextHostOs == null ? null : String(nextHostOs),
    hostPlatform: nextHostPlatform == null ? null : String(nextHostPlatform),
    selectedOs: nextSelectedOs,
    setupLocation: nextSetupLocation == null ? null : String(nextSetupLocation),
    hostManagedLocation: String(nextHostManagedLocation),
  };
  const rendered = _updatesRenderedPresentation;
  const presentationChanged = !rendered
    || rendered.hostOs !== nextPresentation.hostOs
    || rendered.hostPlatform !== nextPresentation.hostPlatform
    || rendered.selectedOs !== nextPresentation.selectedOs
    || rendered.setupLocation !== nextPresentation.setupLocation
    || rendered.hostManagedLocation !== nextPresentation.hostManagedLocation;
  if (info.host_os) HOST_OS = info.host_os;
  if (info.host_platform) HOST_PLATFORM = info.host_platform;
  if (info.managed_locations) HOST_MANAGED_LOCATIONS = info.managed_locations;
  // Upgrade the device-specific tooltips now that we know the real host. The markup ships neutral
  // wording (it must never claim "this Mac" on a Windows host), and this sharpens it once
  // /api/health lands. Keep the LABELS neutral — only titles get the host-specific phrasing.
  const cfb = $('cookiefile_browse');
  if (cfb) cfb.title = `Pick a cookies.txt on ${hostDeviceLabel()}`;
  // Static quit-hint labels: the browser being quit runs on the HOST, so these must follow HOST_OS,
  // not the viewer's user agent. Stamped here rather than at module level because HOST_OS only exists
  // once /api/health has answered — the markup ships neutral so it is never WRONG, just vaguer.
  document.querySelectorAll('.quit-hint').forEach((el) => { el.textContent = quitHint(HOST_OS); });
  const rev = $('probe_pop_reveal');
  if (rev) rev.title = HOST_OS === 'macOS' ? 'Reveal in Finder' : 'Show in File Explorer';
  const badge = $('host_os_badge');
  const gauge = $('host_os_gauge');
  if (badge) {
    badge.textContent = HOST_OS || '…';
    badge.title = HOST_PLATFORM
      ? `sys.platform=${HOST_PLATFORM}`
      : 'Waiting for /api/health';
  }
  if (gauge) {
    gauge.classList.remove('is-macos', 'is-windows', 'is-linux', 'is-unknown');
    const key = (HOST_OS || '').toLowerCase();
    if (key === 'macos') gauge.classList.add('is-macos');
    else if (key === 'windows') gauge.classList.add('is-windows');
    else if (key === 'linux') gauge.classList.add('is-linux');
    else gauge.classList.add('is-unknown');
  }
  if (!refreshUpdatesPanel) return;
  if (_updatesReport && $('updates_body')) {
    if (!presentationChanged) return;
    const active = document.activeElement;
    const focusedOs = active && active.classList && active.classList.contains('upd-os-tab')
      ? active.dataset.os
      : null;
    const refreshedReport = Object.assign({}, _updatesReport, {
      host_os: nextHostOs,
      host_platform: nextHostPlatform,
      managed_locations: nextLocations,
    });
    // Refresh only the cached presentation. This does not start a scan or make a network request.
    renderUpdates(refreshedReport, { animate: false });
    if (focusedOs) {
      const replacement = Array.from(document.querySelectorAll('.upd-os-tab')).find(
        (candidate) => candidate.dataset.os === focusedOs && canTakeFocus(candidate),
      );
      if (replacement) replacement.focus();
    }
    return;
  }
  syncUpdatesOsTabs({ host_os: HOST_OS, managed_locations: HOST_MANAGED_LOCATIONS });
}

// ---- updates (toolchain scan + install/update) ----
/** Last Updates scan payload. */
let _updatesReport = null;
/** Selected FFmpeg setup target. Automatic managed installation remains Windows-only. */
let _updatesOsTab = null;
/** Semantic host/setup inputs used for the most recently rendered Updates panel. */
let _updatesRenderedPresentation = null;
/** Client-side report cache so reopening Updates feels instant. */
let _updatesReportAt = 0;
/**
 * Is an install/update running right now?
 *
 * setUpdatesBusy() disables the .upd-act buttons that exist AT THAT MOMENT, and renderUpdates() then
 * replaces the whole table via innerHTML — recreating every one of them enabled. So navigating away
 * from Updates and back during a multi-minute install re-armed every Install button, and a second
 * click launched a concurrent managed mutation against the same environment.
 *
 * Declared here with the other Updates module state, well above both functions: a `let` read by code
 * that boot can reach must be declared before it, or the temporal dead zone aborts the whole script
 * and the page loads dead (test_webui_boot.py exists for exactly that).
 */
let _updatesBusy = false;
/** An explicit scan owns the controls independently from an installer. */
let _updatesScanBusy = false;
/** Monotonic identity for scan continuations, including quiet phase two. */
let _updatesScanGeneration = 0;
/** Transport cancellation for the current scan generation. */
let _updatesScanController = null;
/** Opaque owner for the sole managed installer allowed to mutate shared tools. */
let _updatesInstallOwner = null;
let _updatesBgTimer = null;
const UPDATES_CLIENT_TTL_MS = 75_000;

function hostOsFromReport(report) {
  if (report && report.host_os) return report.host_os;
  const ff = (report && report.info || []).find((r) => r.name === 'ffmpeg');
  if (ff && ff.host_os) return ff.host_os;
  // Prefer global health detection over inventing an OS.
  return HOST_OS || null;
}

function effectiveUpdatesOs(report) {
  // _updatesOsTab records only an explicit preview choice. Until one exists, derive the visible
  // target from the latest server report so delayed host detection can still correct the UI.
  if (_updatesOsTab === 'Windows' || _updatesOsTab === 'macOS') return _updatesOsTab;
  return hostOsFromReport(report) === 'macOS' ? 'macOS' : 'Windows';
}

const TOOL_ROLES = {
  'yt-dlp': 'Downloads videos from YouTube and other sites',
  'yt-dlp-ejs': 'Helps solve YouTube nsig (needs Node or Deno)',
  'bgutil-ytdlp-pot-provider': 'PO-token plugin for unthrottled YouTube (mweb DASH)',
  'bgutil-plugin': 'yt-dlp plugin that requests PO tokens (pairs with bgutil-server)',
  'requests': 'Pooled HTTP — keeps TCP warm across chunks (up to ~+68% on fast links)',
  'urllib3': 'HTTP pooling layer under requests (yt-dlp needs 2.0.2+)',
  'ffmpeg': 'Encodes and remuxes video (system or managed copy)',
  'node': 'JavaScript runtime for nsig (Node 22+ recommended)',
  'deno': 'Optional alternate JavaScript runtime for nsig',
  'playwright': 'Optional automation browser (Auth Login no longer requires it)',
  'bgutil-server': 'PO-token helper — manual setup only for this release',
  'aria2c': 'Optional faster progressive HTTP downloads',
  'N_m3u8DL-RE': 'Optional turbo engine for HLS (DASH stays native)',
};

const TOOL_LABELS = {
  'yt-dlp': 'yt-dlp (downloader)',
  'yt-dlp-ejs': 'yt-dlp-ejs (nsig helper)',
  'bgutil-ytdlp-pot-provider': 'bgutil plugin (PO tokens)',
  'bgutil-plugin': 'bgutil plugin (PO tokens)',
  'requests': 'requests (pooled HTTP)',
  'urllib3': 'urllib3 (HTTP pool)',
  'ffmpeg': 'FFmpeg',
  'node': 'Node.js',
  'deno': 'Deno',
  'playwright': 'Playwright (optional legacy browser tooling)',
  'bgutil-server': 'bgutil server',
  'aria2c': 'aria2c',
  'N_m3u8DL-RE': 'N_m3u8DL-RE (turbo HLS)',
};

const OPTIONAL_INSTALL_TOOLS = new Set([
  'deno', 'node',
]);

function syncUpdatesOsTabs(report) {
  const bar = $('updates_os_bar');
  const detect = $('updates_os_detect');
  const host = hostOsFromReport(report);
  const selectedOs = effectiveUpdatesOs(report);
  // Host card (plain language)
  const title = $('updates_host_title');
  const sub = $('updates_host_sub');
  const meta = $('updates_host_meta');
  const platEl = $('updates_host_platform');
  const locEl = $('updates_host_ffmpeg_loc');
  const locations = (report && report.managed_locations)
    || HOST_MANAGED_LOCATIONS
    || {};
  if (title) {
    title.textContent = host
      ? `Running on ${host}`
      : 'Detecting host computer…';
  }
  if (sub) {
    if (!host) {
      sub.textContent = 'Scan or unlock loads the real OS from the server.';
    } else if (host === 'Windows') {
      sub.textContent = 'Python changes require a future reviewed yt_final release. Certified managed FFmpeg is available on this Windows x86_64 host.';
    } else if (host === 'macOS') {
      sub.textContent = 'Managed FFmpeg download is disabled on macOS. Use a manual or Homebrew FFmpeg installation; Python changes require a future reviewed yt_final release.';
    } else {
      sub.textContent = 'Managed FFmpeg is available only on Windows x86_64. Install FFmpeg manually on this host; Python changes require a future reviewed yt_final release.';
    }
  }
  if (meta) {
    meta.hidden = !host;
    if (platEl) platEl.textContent = (report && report.host_platform) || HOST_PLATFORM || '—';
    if (locEl) {
      locEl.textContent = host === 'Windows'
        ? (locations.Windows || (report && report.managed_location) || '—')
        : 'Manual / system installation';
    }
  }
  // Keep the static template bar hidden; live tabs are rendered inside the FFmpeg section.
  if (bar) bar.hidden = true;
  document.querySelectorAll('.upd-os-tab').forEach((btn) => {
    const on = btn.dataset.os === selectedOs;
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.classList.toggle('is-active', on);
  });
  if (detect) {
    if (!host) {
      detect.innerHTML = 'Host: <strong>detecting…</strong>';
    } else if (host === 'Windows') {
      detect.innerHTML = 'You are on <strong>Windows</strong> · managed FFmpeg install enabled';
    } else if (host === 'macOS') {
      detect.innerHTML = 'You are on <strong>macOS</strong> · use manual/Homebrew FFmpeg';
    } else {
      detect.innerHTML = `You are on <strong>${esc(host)}</strong> · managed FFmpeg unavailable`;
    }
  }
}

function selectUpdatesOs(os, { focus = false } = {}) {
  if (os !== 'Windows' && os !== 'macOS') return;
  _updatesOsTab = os;
  if (_updatesReport) renderUpdates(_updatesReport, { animate: false });
  else syncUpdatesOsTabs({ host_os: HOST_OS, managed_locations: HOST_MANAGED_LOCATIONS });
  if (!focus) return;
  const replacement = Array.from(document.querySelectorAll('.upd-os-tab')).find(
    (candidate) => candidate.dataset.os === os && canTakeFocus(candidate),
  );
  if (replacement) replacement.focus();
}

function ffmpegSetupPresentation(selectedOs, hostOs, rowEligible) {
  const managedTarget = selectedOs === 'Windows';
  const heading = managedTarget ? 'Managed install (Windows)' : 'Manual setup (macOS)';
  const locationLabel = managedTarget ? 'Private folder' : 'Setup';
  let note;
  if (!hostOs) {
    note = managedTarget
      ? 'Host detection is pending. Managed FFmpeg remains unavailable until Windows is confirmed.'
      : 'Host detection is pending. macOS setup remains manual.';
  } else if (selectedOs === hostOs) {
    if (!managedTarget) {
      note = 'This matches your host. Managed FFmpeg is Windows-only; use a manual/system installation.';
    } else if (rowEligible) {
      note = 'This matches your host. The button installs only into the private folder.';
    } else {
      note = 'This matches the Windows target, but this scan result is not eligible for managed installation.';
    }
  } else {
    note = managedTarget
      ? `You are on ${hostOs}. Windows managed FFmpeg is unavailable on this host; use a manual/system installation.`
      : `You are on ${hostOs}. This macOS preview is manual-only; managed FFmpeg installs only for the Windows target.`;
  }
  return { heading, locationLabel, note };
}

function ffmpegActionForOs(r, selectedOs, hostOs) {
  const locations = r.managed_locations || HOST_MANAGED_LOCATIONS || {};
  const managedLoc = esc(
    locations[selectedOs] || r.managed_location || ''
  );
  const locAttr = managedLoc ? ` data-managed-location="${managedLoc}"` : '';
  const osAttr = ` data-os-target="${esc(selectedOs)}"`;
  const hostMatch = hostOs === 'Windows';
  const canInstall = selectedOs === 'Windows' && hostMatch && r.updatable;

  if (!canInstall) {
    let label;
    let why;
    if (selectedOs !== 'Windows') {
      label = 'Manual FFmpeg';
      why = 'macOS setup is manual-only; managed FFmpeg installs only for Windows';
    } else if (!hostMatch) {
      label = 'Unavailable on this host';
      why = hostOs
        ? `Windows managed FFmpeg cannot be installed on a ${hostOs} host`
        : 'Windows managed FFmpeg cannot be installed until the host is confirmed';
    } else {
      label = 'Install unavailable';
      why = 'This FFmpeg scan result is not eligible for managed installation';
    }
    return `<button type="button" class="upd-act-disabled" disabled title="${esc(why)}">${esc(label)}</button>`;
  }

  const latest = r.latest == null || r.latest === '' ? '—' : esc(String(r.latest));
  const label = !r.installed
    ? `Install for ${selectedOs}`
    : (r.outdated || !r.managed)
      ? `Install managed (${selectedOs})`
      : `Reinstall (${selectedOs})`;
  const primary = (!r.installed || r.outdated || !r.managed) ? ' primary' : '';
  const title = r.managed && !r.outdated
    ? `Reinstall managed FFmpeg for ${selectedOs}`
    : `Install private managed FFmpeg for ${selectedOs} (does not replace system FFmpeg)`;
  return `<button type="button" class="jact${primary} upd-act upd-act-os" data-system-tool="ffmpeg" data-version="${latest}"${locAttr}${osAttr} title="${esc(title)}">${esc(label)}</button>`;
}

function toolStatusLabel(row) {
  // Keep pill labels short (one word / two short words) so they never wrap in the grid.
  if (row.updatable || row.kind === 'pip') {
    if (!row.installed) return { cls: 'st-miss', text: 'missing' };
    // verified === false means metadata found but import probe failed
    if (row.verified === false) return { cls: 'st-warn', text: 'broken' };
    if (row.outdated) return { cls: 'st-warn', text: 'outdated' };
    if (row.current === true || row.verified === true) return { cls: 'st-ok', text: 'current' };
    return { cls: 'st-ok', text: 'current' };
  }
  if (row.name === 'ffmpeg') {
    if (!row.installed) return { cls: 'st-miss', text: 'missing' };
    if (row.outdated) return { cls: 'st-warn', text: 'outdated' };
    return { cls: 'st-ok', text: row.managed ? 'managed' : 'system' };
  }
  if (!row.installed || row.installed === '(not running)') {
    return { cls: 'st-miss', text: row.installed === '(not running)' ? 'offline' : 'missing' };
  }
  if (row.outdated) return { cls: 'st-warn', text: 'outdated' };
  return { cls: 'st-ok', text: 'ready' };
}

function toolActionHtml(r) {
  // Optional helpers with server-side installers.
  if (r.installable && OPTIONAL_INSTALL_TOOLS.has(r.name)) {
    const missing = !r.installed || r.installed === '(not running)';
    const titles = {
      'deno': 'Install the approved Deno artifact after checksum verification',
      'node': 'Install the approved Node.js artifact after checksum verification',
    };
    if (missing) {
      return `<button type="button" class="jact primary upd-act" data-system-tool="${esc(r.name)}" data-tool-install="1" title="${esc(titles[r.name] || 'Install')}">Install</button>`;
    }
    if (r.outdated) {
      const updLabel = 'Update';
      return `<button type="button" class="jact primary upd-act" data-system-tool="${esc(r.name)}" data-tool-install="1" title="${esc(titles[r.name] || updLabel)}">${esc(updLabel)}</button>`;
    }
    const relabel = 'Reinstall';
    return `<button type="button" class="jact upd-act" data-system-tool="${esc(r.name)}" data-tool-install="1" title="${esc(titles[r.name] || relabel)}">${relabel}</button>`;
  }
  if (r.kind !== 'pip') return '<span class="upd-na">—</span>';
  return '<span class="upd-na">Included in certified set</span>';
}

async function runOptionalToolInstall(btn) {
  const hint = $('updates_hint');
  const log = $('updates_log');
  if (!TOKEN) {
    if (hint) { hint.className = 'hint bad'; hint.textContent = 'Sign in first to install optional tools'; }
    return;
  }
  const tool = btn.dataset.systemTool;
  if (!tool) return;
  const labels = {
    'deno': 'Deno (optional JS runtime for nsig)',
    'node': 'Node.js (JS runtime for nsig)',
  };
  const blurb = {
    'deno': 'Downloads only the approved Deno version and verifies its SHA-256 before activation.\n',
    'node': 'Downloads only the approved Node.js version and verifies its SHA-256 before activation.\n',
  };
  const label = labels[tool] || tool;
  if (!confirm(
    `Install ${label} for this computer?\n\n`
    + (blurb[tool] || '')
    + 'The private managed selection is changed only after the staged binary passes its version probe.'
  )) return;
  const action = beginAuthenticatedAction();
  const control = action.ownControl(btn, { text: 'Installing…' });
  const installOwner = beginUpdatesInstall();
  if (!installOwner) {
    control.release();
    if (hint) {
      hint.className = 'hint warn';
      hint.textContent = '⚠ ' + UPDATES_INSTALL_BUSY_MESSAGE;
    }
    return;
  }
  if (hint) { hint.className = 'hint'; hint.textContent = `Installing ${tool}…`; }
  try {
    const response = await sessionFetch('/api/updates/tools', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool }),
    });
    action.guard();
    const body = await sessionJson(response);
    action.guard();
    if (body.report) renderUpdates(body.report);
    const detail = body.ok
      ? ((body.result && (body.result.message || JSON.stringify(body.result))) || `${tool} ready`)
      : (body.error || `${tool} install failed`);
    if (log) {
      log.hidden = false;
      log.textContent = String(detail).slice(0, 4000);
    }
    if (!response.ok || !body.ok) {
      if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + String(detail).slice(0, 500); }
      return;
    }
    // Three outcomes, not two: a verified activation can still require a new
    // process before the selected runtime becomes usable.
    const outcome = installOutcome(body, tool);
    if (outcome.level !== 'ok') {
      if (hint) { hint.className = 'hint warn'; hint.textContent = '⚠ ' + String(outcome.detail || detail).slice(0, 500); }
    } else {
      if (hint) { hint.className = 'hint ok'; hint.textContent = '✓ ' + String(detail).slice(0, 500); }
    }
  } catch (e) {
    if (action.isSilentError(e)) return;
    if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message; }
  } finally {
    action.effect(() => { finishUpdatesInstall(installOwner); });
    control.release();
  }
}

function shortInstallLabel(r) {
  const raw = r.installed;
  if (raw == null || raw === '') return '—';
  const s = String(raw);
  // Never show multi-line paths in the version column.
  if (s.includes('/') || s.includes('\\')) {
    if (r.path && (s === r.path || s.endsWith(r.path))) return 'installed';
    const base = s.split(/[/\\]/).filter(Boolean).pop();
    return base || 'installed';
  }
  return s;
}

function shortPathLabel(path) {
  if (!path) return '';
  const s = String(path);
  if (s.length <= 42) return s;
  // Keep start + end for readability
  return s.slice(0, 18) + '…' + s.slice(-20);
}

function toolRowHtml(r) {
  const st = toolStatusLabel(r);
  const inst = shortInstallLabel(r);
  const latest = r.latest == null || r.latest === '' ? '—' : esc(String(r.latest));
  const note = esc(r.note || '');
  const pathHint = r.path ? shortPathLabel(r.path) : '';
  const label = TOOL_LABELS[r.name] || r.name;
  const role = TOOL_ROLES[r.name] || '';
  const action = r.name === 'ffmpeg' ? '' : toolActionHtml(r);
  const pathHtml = pathHint
    ? `<span class="upd-path" title="${esc(String(r.path))}">${esc(pathHint)}</span>`
    : '';
  return `<div class="upd-tool ${st.cls}" data-tool="${esc(r.name)}">
    <div class="upd-tool-name">
      <span class="upd-name">${esc(label)}</span>
      ${role ? `<span class="upd-role">${esc(role)}</span>` : ''}
      ${pathHtml}
    </div>
    <div class="upd-kv"><span>Yours</span><strong title="${esc(String(r.installed || ''))}">${esc(inst)}</strong></div>
    <div class="upd-kv"><span>Latest</span><strong>${latest}</strong></div>
    <div class="upd-status"><span class="upd-pill ${st.cls}">${st.text}</span></div>
    <div class="upd-note">${note}</div>
    <div class="upd-actions">${action}</div>
  </div>`;
}

function showUpdatesSkeleton() {
  const body = $('updates_body');
  if (!body) return;
  body.classList.remove('is-ready');
  body.classList.add('is-loading');
  body.innerHTML = `
    <div class="upd-skel" aria-busy="true" aria-live="polite">
      <div class="upd-skel-bar"></div>
      <div class="upd-skel-card"></div>
      <div class="upd-skel-card"></div>
      <div class="upd-skel-card upd-skel-card-short"></div>
      <p class="upd-skel-label">Scanning tools…</p>
    </div>`;
}

function playUpdatesEnter(body) {
  if (!body) return;
  body.classList.remove('is-loading');
  // Force reflow so re-adding is-ready retriggers CSS animation
  void body.offsetWidth;
  body.classList.add('is-ready');
}

function renderUpdates(report, opts = {}) {
  const body = $('updates_body');
  const summary = $('updates_summary');
  const warnBox = $('updates_warnings');
  if (!body) return;
  const animate = opts.animate !== false;
  _updatesReport = report || null;
  // Prefer scan host_os; fall back to global /api/health detection.
  if (report && report.host_os) applyHostOs(report, { refreshUpdatesPanel: false });
  else if (report && HOST_OS) {
    report = Object.assign({}, report, { host_os: HOST_OS });
  }
  syncUpdatesOsTabs(report);
  const hostOs = hostOsFromReport(report);
  const selectedOs = effectiveUpdatesOs(report);
  const locations = (report && report.managed_locations)
    || HOST_MANAGED_LOCATIONS
    || {};
  const hostPlatform = (report && report.host_platform) || HOST_PLATFORM || null;

  const pip = (report.pip || []).map((p) => Object.assign({ kind: 'pip', updatable: true }, p));
  const info = (report.info || []).map((p) => Object.assign({ kind: 'info', updatable: false }, p));
  const ffmpeg = info.find((r) => r.name === 'ffmpeg') || null;
  const optional = info.filter((r) => r.name !== 'ffmpeg');
  const renderedSetupLocation = ffmpeg
    ? (selectedOs === 'Windows'
      ? (locations.Windows || ffmpeg.managed_location || '')
      : 'Manual / system installation')
    : null;
  const renderedHostManagedLocation = hostOs === 'Windows'
    ? (locations.Windows || (report && report.managed_location) || '—')
    : 'Manual / system installation';

  if (!pip.length && !info.length) {
    body.classList.remove('is-loading');
    body.innerHTML = `<div class="empty empty-inline upd-empty-block">
      <div class="empty-art" aria-hidden="true">⟳</div>
      <p class="empty-title">No scan yet</p>
      <p class="empty-sub">Hit <strong>Scan now</strong>. Results cache for about 90 seconds.</p>
    </div>`;
  } else {
    let html = '';

    // 1) Download packages
    html += `<section class="upd-section" aria-labelledby="upd-sec-pip">
      <div class="upd-section-hd">
        <h3 id="upd-sec-pip">1. Download packages</h3>
        <p>Required for YouTube downloads. These rows are report-only in 1.0.0. Install a future reviewed yt_final release to change Python packages.</p>
      </div>
      <div class="upd-section-body">${pip.map(toolRowHtml).join('') || '<div class="upd-note" style="padding:12px 16px">No package rows.</div>'}</div>
    </section>`;

    // 2) FFmpeg
    if (ffmpeg) {
      const st = toolStatusLabel(ffmpeg);
      const inst = ffmpeg.installed == null || ffmpeg.installed === '' ? '—' : esc(String(ffmpeg.installed));
      const latest = ffmpeg.latest == null || ffmpeg.latest === '' ? '—' : esc(String(ffmpeg.latest));
      const locForTab = renderedSetupLocation;
      const note = ffmpeg.note || (
        hostOs === 'Windows'
          ? 'Certified managed FFmpeg is available on this host.'
          : 'Managed FFmpeg is Windows-only; use a manual/system FFmpeg installation on this host.'
      );
      const action = ffmpegActionForOs(
        Object.assign({}, ffmpeg, { managed_locations: locations }),
        selectedOs,
        hostOs,
      );
      const setup = ffmpegSetupPresentation(selectedOs, hostOs, !!ffmpeg.updatable);
      const sourceLabel = ffmpeg.managed
        ? 'Using managed (private) FFmpeg'
        : 'Using system / PATH FFmpeg';
      html += `<section class="upd-section" aria-labelledby="upd-sec-ff">
        <div class="upd-section-hd">
          <h3 id="upd-sec-ff">2. FFmpeg (video engine)</h3>
          <p><strong>System FFmpeg</strong> is already on your computer. <strong>Managed FFmpeg</strong> is an optional private copy for yt_final only - it never replaces the system or PATH copy.</p>
        </div>
        <div class="upd-ffmpeg-panel">
          <div class="upd-os-bar" id="updates_os_bar_live">
            <span class="upd-os-label">Show FFmpeg setup for</span>
            <div class="upd-os-tabs" role="group" aria-label="FFmpeg setup platform">
              <button type="button" class="upd-os-tab" data-os="Windows" aria-pressed="${selectedOs === 'Windows' ? 'true' : 'false'}">Windows</button>
              <button type="button" class="upd-os-tab" data-os="macOS" aria-pressed="${selectedOs === 'macOS' ? 'true' : 'false'}">macOS</button>
            </div>
            <span class="upd-os-detect" id="updates_os_detect_live"></span>
          </div>
          <div class="upd-ffmpeg-grid">
            <div class="upd-ffmpeg-block">
              <h4>What this PC is using now</h4>
              <p>${esc(sourceLabel)}. Path: <code>${esc(ffmpeg.path || '—')}</code></p>
              <div class="upd-ffmpeg-stats">
                <div class="upd-kv"><span>Yours</span><strong>${inst}</strong></div>
                <div class="upd-kv"><span>Listed latest</span><strong>${latest}</strong></div>
                <div class="upd-kv"><span>Status</span><strong><span class="upd-pill ${st.cls}">${st.text}</span></strong></div>
              </div>
              <p class="upd-note">${esc(note)}</p>
            </div>
            <div class="upd-ffmpeg-block">
              <h4>${esc(setup.heading)}</h4>
              <p>${esc(setup.locationLabel)}: <code>${esc(locForTab || '—')}</code></p>
              <p class="upd-note" style="margin-bottom:12px">${esc(setup.note)}</p>
              <div class="upd-actions" style="justify-self:start">${action}</div>
            </div>
          </div>
        </div>
      </section>`;
    }

    // 3) Optional helpers
    if (optional.length) {
      html += `<section class="upd-section" aria-labelledby="upd-sec-opt">
        <div class="upd-section-hd">
          <h3 id="upd-sec-opt">3. Optional helpers</h3>
          <p>Verified managed installation is available only for <strong>Node.js</strong> and <strong>Deno</strong> on certified platforms. Other helpers show status and stable manual guidance without an automatic install button.</p>
        </div>
        <div class="upd-section-body">${optional.map(toolRowHtml).join('')}</div>
      </section>`;
    }

    body.innerHTML = html;
    // The line above mints fresh, ENABLED action buttons. If an install is still running, re-apply the
    // busy state — otherwise leaving the Updates tab and coming back mid-install re-armed every
    // Install button and a second click started a concurrent managed mutation.
    // Every re-render path goes through here: the quiet tab-open, the background phase-2 refresh and
    // an OS-tab click. Note the preview "not on this PC" button uses .upd-act-disabled, not .upd-act,
    // so this sweep cannot stomp it.
    applyUpdatesControlState();
    if (animate) playUpdatesEnter(body);
    else {
      body.classList.remove('is-loading');
      body.classList.add('is-ready');
    }

    const liveDetect = $('updates_os_detect_live');
    if (liveDetect) {
      if (!hostOs) liveDetect.innerHTML = 'Host: <strong>detecting…</strong>';
      else if (hostOs === 'Windows') {
        liveDetect.innerHTML = 'You are on <strong>Windows</strong> · install enabled';
      } else if (hostOs === 'macOS') {
        liveDetect.innerHTML = 'You are on <strong>macOS</strong> · manual/Homebrew only';
      } else {
        liveDetect.innerHTML = `You are on <strong>${esc(hostOs)}</strong> · managed install unavailable`;
      }
    }
  }

  _updatesRenderedPresentation = {
    hostOs: hostOs == null ? null : String(hostOs),
    hostPlatform: hostPlatform == null ? null : String(hostPlatform),
    selectedOs,
    setupLocation: renderedSetupLocation == null ? null : String(renderedSetupLocation),
    hostManagedLocation: String(renderedHostManagedLocation),
  };
  const s = (report && report.summary) || {};
  const when = report.checked_at ? new Date(Number(report.checked_at) * 1000).toLocaleString() : '';
  if (summary) {
    const optOut = s.optional_outdated || [];
    const optMiss = s.optional_missing || [];
    const helperBits = [];
    if (optOut.length) {
      helperBits.push(`helpers outdated: <strong>${esc(optOut.join(', '))}</strong>`);
    }
    if (optMiss.length) {
      helperBits.push(`helpers missing: <strong>${esc(optMiss.join(', '))}</strong>`);
    }
    const helperSuffix = helperBits.length ? ' · ' + helperBits.join(' · ') : '';
    if (s.ok && !helperBits.length) {
      summary.className = 'updates-summary is-ok';
      summary.innerHTML = `All download packages look current and verified${when ? ' · checked ' + esc(when) : ''}.`;
    } else if (s.ok && helperBits.length) {
      // Packages fine, optional helpers need attention — soft warn, not full fail.
      summary.className = 'updates-summary is-warn';
      summary.innerHTML = `Download packages are current${helperSuffix}${when ? ' · ' + esc(when) : ''}.`;
    } else {
      summary.className = 'updates-summary is-warn';
      const parts = [];
      if ((s.missing_pip || []).length) {
        parts.push(`Missing: <strong>${esc((s.missing_pip || []).join(', '))}</strong>`);
      }
      if (s.outdated_count) {
        parts.push(`<strong>${s.outdated_count}</strong> package update(s)`);
      }
      if ((s.broken_pip || []).length) {
        parts.push(`Broken: <strong>${esc((s.broken_pip || []).join(', '))}</strong>`);
      }
      if (s.warning_count) parts.push(`${s.warning_count} warning(s)`);
      summary.innerHTML = (parts.join(' · ') || 'Scan complete') + helperSuffix + (when ? ' · ' + esc(when) : '');
    }
  }
  if (warnBox) {
    const ws = report.warnings || [];
    if (ws.length) {
      warnBox.hidden = false;
      warnBox.innerHTML = ws.map((w) => `<div class="upd-warn">${esc(w)}</div>`).join('');
    } else {
      warnBox.hidden = true;
      warnBox.innerHTML = '';
    }
  }
}

async function fetchUpdatesReport({ force = false, pypi = true, signal } = {}) {
  if (TOKEN) {
    return api('/api/updates', {
      method: 'POST',
      signal,
      body: JSON.stringify({
        action: 'scan',
        force: !!force,
        pypi: pypi ? 1 : 0,
      }),
    });
  }
  const res = await sessionFetch(
    `/api/updates?pypi=${pypi ? '1' : '0'}&force=${force ? '1' : '0'}`,
    { signal },
  );
  const report = await sessionJson(res);
  if (!res.ok) throw new Error(report.error || res.statusText);
  return report;
}

function staleUpdateScanError() {
  const error = new Error('stale update scan');
  error.name = 'AbortError';
  return error;
}

function invalidateUpdateScan() {
  // Advance first: abort listeners can run synchronously, and every one must already observe stale
  // generation state before it schedules a continuation.
  _updatesScanGeneration += 1;
  const controller = _updatesScanController;
  _updatesScanController = null;
  if (controller) {
    try { controller.abort(); } catch (_) { /* best effort */ }
  }
  const timer = _updatesBgTimer;
  _updatesBgTimer = null;
  if (timer != null) {
    try { clearAuthenticatedTimeout(timer); } catch (_) { /* best effort */ }
  }
  return _updatesScanGeneration;
}

function beginUpdateScan() {
  const generation = invalidateUpdateScan();
  _updatesScanController = new AbortController();
  return { generation, signal: _updatesScanController.signal };
}

function isCurrentUpdateScan(generation) {
  return generation === _updatesScanGeneration
    && !!_updatesScanController
    && !_updatesScanController.signal.aborted;
}

function currentUpdateScanCanMutate(action, generation) {
  return action.isCurrent() && isCurrentUpdateScan(generation) && !_updatesBusy;
}

function guardCurrentUpdateScan(action, generation) {
  if (!currentUpdateScanCanMutate(action, generation)) throw staleUpdateScanError();
}

const UPDATES_INSTALL_BUSY_MESSAGE = 'Another managed install is already running. Wait for it to finish.';

function setSpeedInstallControlBusy(button, busy) {
  if (!button || (button.classList && button.classList.contains('is-done'))) return;
  const data = button.dataset || {};
  if (busy) {
    if (!Object.prototype.hasOwnProperty.call(data, 'updatesIdleTitle')) {
      data.updatesIdleTitle = button.title || '';
    }
    button.disabled = true;
    button.title = UPDATES_INSTALL_BUSY_MESSAGE;
    return;
  }
  button.disabled = false;
  if (Object.prototype.hasOwnProperty.call(data, 'updatesIdleTitle')) {
    button.title = data.updatesIdleTitle;
    delete data.updatesIdleTitle;
  }
}

function applyUpdatesControlState() {
  const disabled = _updatesBusy || _updatesScanBusy;
  const scan = $('updates_check');
  if (scan) scan.disabled = disabled;
  document.querySelectorAll('.upd-act').forEach((button) => {
    button.disabled = disabled;
  });
  // Speed recommendations live outside the Updates table. Only installer ownership blocks them:
  // an update scan is safe to supersede, while a second managed mutation is not.
  document.querySelectorAll('.st-rec-btn[data-update-install="1"]').forEach((button) => {
    setSpeedInstallControlBusy(button, _updatesBusy);
  });
}

function setUpdatesBusy(busy) {
  _updatesBusy = !!busy;
  applyUpdatesControlState();
}

function setUpdatesScanBusy(busy) {
  _updatesScanBusy = !!busy;
  applyUpdatesControlState();
}

function beginUpdatesInstall() {
  if (_updatesInstallOwner || _updatesBusy) return null;
  const owner = Symbol('updates install owner');
  // A mutation report is authoritative. Retire every older scan before claiming installer ownership
  // so an abort-ignoring transport cannot repaint or release the controls afterward.
  invalidateUpdateScan();
  setUpdatesScanBusy(false);
  _updatesInstallOwner = owner;
  setUpdatesBusy(true);
  _updatesReportAt = 0;
  return owner;
}

function finishUpdatesInstall(owner) {
  if (!owner || owner !== _updatesInstallOwner) return false;
  _updatesInstallOwner = null;
  setUpdatesBusy(false);
  return true;
}

async function refreshUpdates(opts = {}) {
  // Don't re-probe underneath a running install. The Scan buttons are disabled while busy, but the
  // nav tab is not: opening Updates mid-install triggered a quiet refresh that re-rendered the table
  // (and, before the re-apply above, handed back enabled Install buttons).
  if (_updatesBusy) return;
  const hint = $('updates_hint');
  const quiet = !!(opts && opts.quiet);
  // Quiet tab-open reuses caches. Explicit "Scan now" forces a full re-probe.
  const force = quiet ? false : true;
  const now = Date.now();
  const action = beginAuthenticatedAction();
  // This must precede the client-cache return: reopening the tab also retires an old queued phase
  // two, even though the fresh cached report itself needs no request.
  const scan = beginUpdateScan();
  const generation = scan.generation;
  const current = () => currentUpdateScanCanMutate(action, generation);
  // The new generation owns the previous generation's retirement too. A quiet navigation can
  // supersede an explicit scan, so it must clear that old foreground ownership immediately.
  setUpdatesScanBusy(!quiet);

  const t0 = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
  try {
    // Instant reopen: client already has a fresh report. Beginning this generation above is still
    // required because it invalidates an older phase-two callback before this early return.
    if (quiet && _updatesReport && (now - _updatesReportAt) < UPDATES_CLIENT_TTL_MS) {
      guardCurrentUpdateScan(action, generation);
      renderUpdates(_updatesReport, { animate: false });
      return;
    }

    // Keep previous content visible during quiet refresh when possible.
    if (!_updatesReport) showUpdatesSkeleton();
    else if (!quiet && hint) {
      hint.className = 'hint';
      hint.textContent = 'Scanning tools…';
    }

    if (quiet) {
      // Phase 1: local versions only (no PyPI/GitHub) — feels instant.
      const fast = await fetchUpdatesReport({
        force: false,
        pypi: false,
        signal: scan.signal,
      });
      guardCurrentUpdateScan(action, generation);
      renderUpdates(fast, { animate: !_updatesReportAt });
      guardCurrentUpdateScan(action, generation);
      _updatesReportAt = Date.now();
      // Phase 2: fill "Latest" columns in the background without blanking the UI.
      guardCurrentUpdateScan(action, generation);
      let timerId = null;
      timerId = authenticatedTimeout(async () => {
        // A callback that was already queued can run after another generation installed its timer.
        // Clear the public slot only when this callback still owns that exact slot.
        if (_updatesBgTimer === timerId) _updatesBgTimer = null;
        if (!current()) return;
        try {
          const full = await fetchUpdatesReport({
            force: false,
            pypi: true,
            signal: scan.signal,
          });
          guardCurrentUpdateScan(action, generation);
          renderUpdates(full, { animate: false });
          guardCurrentUpdateScan(action, generation);
          _updatesReportAt = Date.now();
        } catch (error) {
          // Phase one remains useful. Abort/stale work and a current background lookup failure all
          // stay silent; only the foreground scan owns the error live region.
          if (!current() || action.isSilentError(error)
              || (error && error.name === 'AbortError')) return;
        }
      }, 40);
      guardCurrentUpdateScan(action, generation);
      _updatesBgTimer = timerId;
    } else {
      const report = await fetchUpdatesReport({
        force: true,
        pypi: true,
        signal: scan.signal,
      });
      guardCurrentUpdateScan(action, generation);
      renderUpdates(report, { animate: true });
      guardCurrentUpdateScan(action, generation);
      _updatesReportAt = Date.now();
      guardCurrentUpdateScan(action, generation);
      if (hint) {
        const ms = Math.round(((typeof performance !== 'undefined' && performance.now)
          ? performance.now() : Date.now()) - t0);
        const cached = report.cached ? ' (cached)' : '';
        hint.className = 'hint ok';
        hint.textContent = report.ok === false
          ? ('✗ ' + (report.error || 'scan failed'))
          : `✓ scan complete in ${ms}ms${cached}`;
      }
    }
  } catch (e) {
    if (!current() || action.isSilentError(e) || (e && e.name === 'AbortError')) return;
    if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + (e.message || 'scan failed'); }
    const body = $('updates_body');
    if (body && !_updatesReport) {
      body.classList.remove('is-loading');
      body.innerHTML = `<div class="empty empty-inline upd-empty-block">
        <p class="empty-title">Scan failed</p>
        <p class="empty-sub">${esc(e.message || 'Try Scan now again.')}</p>
      </div>`;
    }
  } finally {
    // Only the foreground scan that still owns this generation may release scan ownership. An
    // installer invalidates the generation, clears scan ownership itself, and then claims its own
    // owner; this stale finally must not re-enable those controls.
    if (!quiet && current()) setUpdatesScanBusy(false);
  }
}

async function runFFmpegAction(btn) {
  const hint = $('updates_hint');
  const log = $('updates_log');
  if (!TOKEN) {
    if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ sign in first to manage FFmpeg'; }
    return;
  }
  const target = btn.dataset.version && btn.dataset.version !== '—'
    ? btn.dataset.version : 'latest stable';
  const osTarget = btn.dataset.osTarget || hostOsFromReport(_updatesReport) || 'this OS';
  const managedLoc = (btn.dataset.managedLocation && btn.dataset.managedLocation !== '—')
    ? btn.dataset.managedLocation
    : 'the private app-managed FFmpeg folder';
  if (!confirm(
    `Install FFmpeg ${target} for ${osTarget}?\n\n`
    + `Private managed location:\n${managedLoc}\n\n`
    + `This does not change system PATH or the system FFmpeg.`
  )) return;
  const action = beginAuthenticatedAction();
  const control = action.ownControl(btn, { text: 'Downloading…' });
  const installOwner = beginUpdatesInstall();
  if (!installOwner) {
    control.release();
    if (hint) {
      hint.className = 'hint warn';
      hint.textContent = '⚠ ' + UPDATES_INSTALL_BUSY_MESSAGE;
    }
    return;
  }
  if (hint) { hint.className = 'hint'; hint.textContent = 'Downloading and validating FFmpeg…'; }
  try {
    const response = await sessionFetch('/api/updates/ffmpeg', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    action.guard();
    btn.textContent = 'Installing…';
    const body = await sessionJson(response);
    action.guard();
    if (body.report) renderUpdates(body.report);
    const detail = body.ok
      ? ((body.result && (body.result.message || JSON.stringify(body.result, null, 2))) || 'FFmpeg is ready')
      : (body.error || 'FFmpeg update failed');
    if (log) {
      log.hidden = false;
      log.textContent = String(detail).slice(0, 4000);
    }
    if (!response.ok || !body.ok) {
      if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + String(detail).slice(0, 500); }
      return;
    }
    if (hint) { hint.className = 'hint ok'; hint.textContent = '✓ ' + String(detail).slice(0, 500); }
  } catch (e) {
    if (action.isSilentError(e)) return;
    const detail = (e && e.message) || 'FFmpeg update failed';
    if (log) { log.hidden = false; log.textContent = String(detail).slice(0, 4000); }
    if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + String(detail).slice(0, 500); }
  } finally {
    action.effect(() => { finishUpdatesInstall(installOwner); });
    control.release();
  }
}

if ($('updates_check')) {
  $('updates_check').onclick = () => refreshUpdates({ quiet: false });
}
// Delegated actions remain only for verified Node/Deno artifacts and managed FFmpeg.
const updatesBody = $('updates_body');
if (updatesBody) {
  updatesBody.addEventListener('click', (e) => {
    const osButton = e.target.closest('.upd-os-tab');
    if (osButton) {
      selectUpdatesOs(osButton.dataset.os, { focus: true });
      return;
    }
    const btn = e.target.closest('.upd-act');
    if (!btn) return;
    if (btn.dataset.toolInstall === '1' || btn.dataset.toolInstall === 'true') {
      runOptionalToolInstall(btn);
      return;
    }
    if (btn.dataset.systemTool === 'ffmpeg') {
      runFFmpegAction(btn);
    }
  });
}

// ---- auth (cookie/session status) ----
// Last /api/auth payload — shared by Auth tab + Advanced → Access banner.
let _lastAuthStatus = null;

function renderAuthStatus(st) {
  const el = $('auth_status');
  if (!el) return;
  const paint = (cls, lead, bitsStr) => {
    el.className = 'auth-status ' + cls;
    el.innerHTML = '';
    const txt = document.createElement('div');
    const leadEl = document.createElement('span'); leadEl.className = 'auth-lead';
    leadEl.textContent = lead;
    txt.appendChild(leadEl);
    if (bitsStr) {
      const bitsEl = document.createElement('span'); bitsEl.className = 'auth-bits';
      bitsEl.textContent = bitsStr;
      txt.appendChild(bitsEl);
    }
    el.appendChild(txt);
  };
  if (!st) { paint('is-bad', 'Status unavailable', 'is the app running?'); return; }
  if (st.error) { paint('is-bad', '✗ ' + st.error, ''); return; }
  const hasFile = !!(st.has_file || st.cookie_path);
  const ok = !!(st.enabled && st.valid && hasFile);
  const bits = [
    `enabled: ${!!st.enabled}`,
    `on file: ${hasFile}`,
    `valid: ${!!st.valid}`,
  ];
  if (st.source) bits.push(`source: ${st.source}`);
  bits.push(`tiers: ${(st.tiers_available || []).join(', ') || 'none'}`);
  // Prefer server detail; fall back to correct local lead (never claim "on file" without has_file).
  let lead = st.detail || '';
  if (!lead) {
    if (ok) lead = 'Signed-in cookies active';
    else if (st.enabled && hasFile && !st.valid) lead = 'Cookies on file — click Test to verify';
    else if (st.enabled && !hasFile) lead = 'Auth on, but no cookies file — import cookies.txt';
    else if (!st.enabled && hasFile) lead = 'Cookies file found, auth disabled — import again to enable';
    else lead = 'No cookies on file';
  }
  paint(ok ? 'is-ok' : (hasFile || st.enabled) ? 'is-warn' : 'is-idle', lead, bits.join('  ·  '));
}

function jobWantsAuth() {
  // Yes radio (default) = use Auth session; No = anonymous unless cookiefile override.
  if ($('use_auth_no') && $('use_auth_no').checked) return false;
  return true;
}

/**
 * Keep Advanced → Access in sync with the Auth tab + per-job Yes/No.
 * Backend injects Auth cookies only when use_auth=true and cookiefile is blank.
 */
function syncComposeAccessFromAuth(st) {
  _lastAuthStatus = st || null;
  const box = $('access_auth');
  const leadEl = $('access_auth_lead');
  const subEl = $('access_auth_sub');
  const hint = $('access_summary_hint');
  const overrideWrap = $('access_override');
  if (!box || !leadEl) return;

  const cf = ($('cookiefile') && $('cookiefile').value.trim()) || '';
  const hasOverride = !!cf;
  const wantAuth = jobWantsAuth();

  const hasFile = !!(st && (st.has_file || st.cookie_path));
  const enabled = !!(st && st.enabled);
  const valid = !!(st && st.valid && hasFile);
  const ok = !!(enabled && valid);

  let state = 'idle';
  let lead = 'No Auth session yet';
  let sub = 'Open Auth to Login with Chrome or Import. Or pick No for anonymous downloads.';

  if (hasOverride) {
    state = 'override';
    lead = 'Per-job cookies file override';
    sub = 'This job uses the selected cookies.txt. Auth Yes/No is ignored while a file is set.';
    if (overrideWrap && !overrideWrap.open) overrideWrap.open = true;
  } else if (!wantAuth) {
    state = 'off';
    lead = 'Auth off for this job';
    sub = 'This download runs anonymous (no signed-in cookies). Switch to Yes to use the Auth session.';
  } else if (!st) {
    state = 'unknown';
    lead = 'Auth status unavailable';
    sub = 'Is the app running? With Yes selected, jobs try the Auth session when no file is set.';
  } else if (st.error) {
    state = 'bad';
    lead = 'Auth error';
    sub = String(st.error);
  } else if (ok) {
    state = 'ok';
    lead = 'Auth session active — used for this job';
    const src = st.source ? ` · source: ${st.source}` : '';
    sub = 'Yes is selected: signed-in cookies from the Auth tab apply' + src + '.';
  } else if (enabled && hasFile && !st.valid) {
    state = 'warn';
    lead = 'Auth cookies on file — not verified';
    sub = 'Open Auth and click Test cookies. Or pick No for anonymous.';
  } else if (enabled && !hasFile) {
    state = 'warn';
    lead = 'Auth on, but no cookies file';
    sub = 'Open Auth → Login with Chrome or Import. Or pick No for anonymous.';
  } else if (!enabled && hasFile) {
    state = 'warn';
    lead = 'Cookies found, Auth disabled';
    sub = 'Re-import on the Auth tab to enable the session.';
  }

  box.dataset.state = state;
  leadEl.textContent = lead;
  if (subEl) subEl.textContent = sub;

  if (hint) {
    hint.classList.remove('is-ok', 'is-warn', 'is-override');
    if (state === 'ok') {
      hint.textContent = 'Auth on for this job';
      hint.classList.add('is-ok');
    } else if (state === 'off') {
      hint.textContent = 'Auth off · anonymous';
      hint.classList.add('is-warn');
    } else if (state === 'override') {
      hint.textContent = 'per-job override';
      hint.classList.add('is-override');
    } else if (state === 'warn' || state === 'bad') {
      hint.textContent = 'Auth needs attention';
      hint.classList.add('is-warn');
    } else {
      hint.textContent = 'fix sign-in / throttle';
    }
  }
  if (overrideWrap) overrideWrap.classList.toggle('is-active', hasOverride);
}

function setAuthHelpOpen(open) {
  const panel = $('auth-help-panel');
  const btn = $('auth_help_btn');
  if (!panel) return;
  // Class-driven CSS grid animation (0fr ↔ 1fr). Avoid [hidden]/display:none
  // so open/close can ease instead of popping.
  panel.classList.toggle('is-open', !!open);
  panel.setAttribute('aria-hidden', open ? 'false' : 'true');
  if (btn) {
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    btn.classList.toggle('is-open', !!open);
    btn.textContent = open ? 'Hide guide' : 'How to sign in';
  }
  if (open) {
    // Keep focus path sensible after expand; don't steal focus mid-click.
    try {
      const close = $('auth_help_close');
      if (close) close.focus({ preventScroll: true });
    } catch (_) { /* ignore */ }
  }
}

function syncAuthBrowserControls(st) {
  const note = $('auth-browser-note');
  const hero = $('auth-hero');
  const heroKicker = $('auth-hero-kicker');
  const heroTitle = $('auth-hero-title');
  const sel = $('auth-browser');
  const loginBtn = $('auth-login-btn');
  const fromBtn = $('auth-from-browser-btn');
  const reqLive = $('auth-req-live');
  const detected = (st && st.browsers_detected) || [];
  const hasNative = !!(st && (st.native_login_available || detected.length));
  const hasFile = !!(st && st.has_file);
  const valid = !!(st && st.valid && hasFile);
  const enabled = !!(st && st.enabled);

  if (hero) {
    hero.classList.toggle('is-ok', valid);
    hero.classList.toggle('is-warn', !valid && (hasFile || enabled));
  }
  if (heroKicker) {
    heroKicker.textContent = valid ? 'Signed in' : hasFile ? 'Almost ready' : 'Not signed in';
  }
  if (heroTitle) {
    if (valid) heroTitle.textContent = 'Cookies are active';
    else if (hasFile) heroTitle.textContent = 'Cookies on file - test them';
    else if (!detected.length) heroTitle.textContent = 'Install Chrome to get started';
    else heroTitle.textContent = 'Sign in to unlock age-gated downloads';
  }
  if (note) {
    if (detected.length) {
      note.textContent = valid
        ? `Browsers on ${hostDeviceLabel()}: ${detected.join(', ')}. Re-import anytime if cookies expire.`
        : `Browsers on ${hostDeviceLabel()}: ${detected.join(', ')}. Use Login or Import below.`;
    } else {
      note.textContent = 'No browser found. Install Chrome from google.com/chrome, then refresh.';
    }
  }
  if (reqLive) {
    if (!detected.length) {
      reqLive.textContent = `${hostDeviceLabel(true)}: no browser found. Install Chrome from google.com/chrome`;
      reqLive.className = 'auth-req-live is-warn';
    } else if (valid) {
      reqLive.textContent = `${hostDeviceLabel(true)}: ${detected.join(', ')} · cookies OK`;
      reqLive.className = 'auth-req-live is-ok';
    } else if (hasFile) {
      reqLive.textContent = `${hostDeviceLabel(true)}: ${detected.join(', ')} · cookies on file - click Test cookies`;
      reqLive.className = 'auth-req-live is-warn';
    } else {
      reqLive.textContent = `${hostDeviceLabel(true)}: ${detected.join(', ')} · ready for Login or Import`;
      reqLive.className = 'auth-req-live is-ok';
    }
  }
  if (sel && detected.length) {
    const opts = Array.from(sel.options).map((o) => o.value);
    detected.forEach((name) => {
      if (!opts.includes(name)) {
        const o = document.createElement('option');
        o.value = name;
        o.textContent = name;
        sel.appendChild(o);
      }
    });
  }
  if (loginBtn) {
    loginBtn.disabled = false;
    loginBtn.title = hasNative
      ? `Opens real Chrome with a private yt_final profile. Sign into YouTube, then ${quitHint(HOST_OS)}.`
      : 'Install Google Chrome to use Login, or use Import from browser / cookies.txt.';
    loginBtn.classList.toggle('is-muted', !hasNative);
    // When already valid, demote login slightly; when not, keep primary gold.
    loginBtn.classList.toggle('auth-btn-primary', !valid);
    loginBtn.classList.toggle('auth-btn-secondary', valid);
  }
  if (fromBtn) {
    fromBtn.disabled = false;
    fromBtn.classList.toggle('auth-btn-primary', !valid && !hasNative);
    fromBtn.classList.toggle('auth-btn-secondary', valid || hasNative);
  }
}

/** Paint Auth tab + Advanced → Access from one status payload. */
function applyAuthStatus(st) {
  renderAuthStatus(st);
  syncAuthBrowserControls(st);
  syncComposeAccessFromAuth(st);
}

async function refreshAuth() {
  const action = beginAuthenticatedAction();
  try {
    const st = await api('/api/auth');
    action.guard();
    applyAuthStatus(st);
  } catch (e) {
    if (action.isSilentError(e)) return;
    applyAuthStatus(null);
  }
}

async function authFromBrowser() {
  const action = beginAuthenticatedAction();
  const btn = $('auth-from-browser-btn');
  const hint = $('auth_hint');
  const browser = ($('auth-browser') && $('auth-browser').value) || 'auto';
  if (!confirm(
    `Import YouTube cookies from ${browser === 'auto' ? 'an installed browser' : browser}?\n\n`
    + `Fully quit that browser first (${quitHintQualified(HOST_OS)}) so the cookie DB is not locked.\n`
    + 'Cookies stay on this machine only.'
  )) return;
  const control = action.ownControl(btn, { text: 'Reading browser…' });
  if (hint) { hint.className = 'hint'; hint.textContent = 'Reading browser cookies…'; }
  try {
    const res = await api('/api/auth', {
      method: 'POST',
      body: JSON.stringify({ action: 'from_browser', browser }),
    });
    action.guard();
    if (res.ok) {
      if (hint) {
        hint.className = 'hint ok';
        hint.textContent = '✓ ' + (res.message || 'Imported from browser — click Test');
      }
    } else {
      if (hint) {
        hint.className = 'hint bad';
        hint.textContent = '✗ ' + (res.error || 'Browser import failed');
      }
    }
    if (res.status) applyAuthStatus(res.status);
    else {
      action.guard();
      await refreshAuth();
    }
  } catch (e) {
    if (action.isSilentError(e)) return;
    if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message; }
  } finally {
    control.release();
  }
}

async function authLogin() {
  const action = beginAuthenticatedAction();
  const btn = $('auth-login-btn');
  const hint = $('auth_hint');
  if (!confirm(
    'Open real Chrome for YouTube login?\n\n'
    + 'This launches your installed Chrome with a private yt_final profile -\n'
    + 'NOT Playwright (Google blocks automated browsers).\n\n'
    + '1) Sign into YouTube in that window (2FA OK)\n'
    + `2) When finished, quit it fully (${quitHint(HOST_OS)})\n`
    + '3) This page saves cookies automatically\n\n'
    + 'Already signed into daily Chrome? Prefer Import from browser instead.'
  )) return;
  const control = action.ownControl(btn, { text: 'Waiting for login…' });
  if (hint) {
    hint.className = 'hint warn';
    hint.textContent = `Real Chrome opened - sign into YouTube, then quit that browser `
      + `(${quitHint(HOST_OS)}) so cookies can be saved. This page waits.`;
  }
  try {
    const res = await api('/api/auth', {
      method: 'POST',
      body: JSON.stringify({ action: 'login' }),
    });
    action.guard();
    if (res.ok) {
      if (hint) {
        hint.className = 'hint ok';
        hint.textContent = '✓ ' + (res.message || 'Login saved — click Test');
      }
    } else {
      if (hint) {
        hint.className = 'hint bad';
        hint.textContent = '✗ ' + (res.error || 'Login failed');
      }
    }
    if (res.status) applyAuthStatus(res.status);
    else {
      action.guard();
      await refreshAuth();
    }
  } catch (e) {
    if (action.isSilentError(e)) return;
    if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message; }
  } finally {
    control.release();
  }
}

async function authAction(action, btn) {
  const sessionAction = beginAuthenticatedAction();
  const hint = $('auth_hint');
  if (action === 'clear' && !confirm('Clear local cookie store on this machine?')) return;
  const busyLabel = { test: 'Testing…', refresh: 'Refreshing…', clear: 'Clearing…' }[action] || action;
  const control = sessionAction.ownControl(btn, { text: busyLabel });
  try {
    const res = await api('/api/auth', { method: 'POST', body: JSON.stringify({ action }) });
    sessionAction.guard();
    const msg = res.message || res.error || action;
    hint.className = 'hint ' + (res.ok ? 'ok' : 'bad');
    hint.textContent = (res.ok ? '✓ ' : '✗ ') + String(msg).slice(0, 400);
    const status = res.status || await api('/api/auth');
    sessionAction.guard();
    applyAuthStatus(status);
  } catch (e) {
    if (sessionAction.isSilentError(e)) return;
    hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message;
  } finally {
    control.release();
  }
}
$('auth_refresh').onclick = () => authAction('refresh', $('auth_refresh'));
$('auth_test').onclick = () => authAction('test', $('auth_test'));
$('auth_clear').onclick = () => authAction('clear', $('auth_clear'));
if ($('auth_help_btn')) {
  $('auth_help_btn').onclick = () => {
    const panel = $('auth-help-panel');
    // Panel uses .is-open (not the HTML hidden attribute) for CSS animation.
    const open = !(panel && panel.classList.contains('is-open'));
    setAuthHelpOpen(open);
  };
}
if ($('auth_help_close')) {
  $('auth_help_close').onclick = () => setAuthHelpOpen(false);
}
if ($('auth-from-browser-btn')) {
  $('auth-from-browser-btn').onclick = () => authFromBrowser();
}
if ($('auth-login-btn')) {
  $('auth-login-btn').onclick = () => authLogin();
}

// ---- auth import (file picker + paste box) ----
// The visible control is a real <button> (a <label for> is not focusable and the input is hidden, so
// the picker was mouse-only). It forwards the click to the hidden input, same as #cookiefile_upload_btn.
if ($('auth_file_btn')) {
  $('auth_file_btn').onclick = () => openAuthenticatedFilePicker(
    $('auth-file'),
    () => $('auth-file').click(),
  );
}
$('auth-file').addEventListener('change', () => {
  const input = $('auth-file');
  const action = takeAuthenticatedFilePickerAction(input);
  if (!action) return;
  const file = input.files[0];
  $('auth-file-name').textContent = file ? file.name : '';
  if (!file) return;
  const reader = new FileReader();
  const stopWatching = () => {
    if (action.context.signal) action.context.signal.removeEventListener('abort', abortReader);
  };
  const finish = (callback) => {
    stopWatching();
    action.effect(callback);
  };
  const abortReader = () => {
    reader.onload = null;
    reader.onerror = null;
    try { reader.abort(); } catch (e) { /* already complete */ }
  };
  reader.onload = () => finish(() => { $('auth-paste').value = reader.result; });
  reader.onerror = () => {
    finish(() => {
      const hint = $('auth_hint');
      if (hint) {
        hint.className = 'hint bad';
        hint.textContent = '✗ Could not read file';
      }
    });
  };
  if (action.context.signal) {
    action.context.signal.addEventListener('abort', abortReader, { once: true });
  }
  reader.readAsText(file);
});

$('auth-import-btn').onclick = async () => {
  const action = beginAuthenticatedAction();
  const btn = $('auth-import-btn');
  const hint = $('auth_hint');
  const content = $('auth-paste').value;
  if (!content.trim()) {
    hint.className = 'hint bad';
    hint.textContent = '✗ Nothing to import — choose a file or paste cookies.txt contents';
    return;
  }
  const control = action.ownControl(btn, { text: 'Importing…' });
  try {
    const res = await api('/api/auth', {
      method: 'POST',
      body: JSON.stringify({ action: 'import', content }),
    });
    action.guard();
    if (res.ok) {
      hint.className = 'hint ok';
      hint.textContent = '✓ ' + (res.message || 'Imported — click Test to verify login');
      const status = res.status || await api('/api/auth');
      action.guard();
      applyAuthStatus(status);
      // Keep paste so user can re-import; clear file name only.
      if ($('auth-file-name')) $('auth-file-name').textContent = '';
      if ($('auth-file')) $('auth-file').value = '';
    } else {
      hint.className = 'hint bad';
      hint.textContent = '✗ ' + (res.error || 'import failed');
      if (res.status) applyAuthStatus(res.status);
    }
  } catch (e) {
    if (action.isSilentError(e)) return;
    hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message;
  } finally {
    control.release();
  }
};

// Advanced → Access: open Auth tab + path override (browse / upload / clear)
if ($('access_open_auth')) {
  $('access_open_auth').onclick = () => activateView('auth', { focus: true });
}
// Per-job Yes/No for Auth cookies
['use_auth_yes', 'use_auth_no'].forEach((id) => {
  const el = $(id);
  if (el) el.addEventListener('change', () => syncComposeAccessFromAuth(_lastAuthStatus));
});

function setCookiefilePath(path) {
  const inp = $('cookiefile');
  if (!inp) return;
  inp.value = path || '';
  const clearBtn = $('cookiefile_clear');
  if (clearBtn) clearBtn.hidden = !inp.value.trim();
  syncComposeAccessFromAuth(_lastAuthStatus);
}

if ($('cookiefile')) {
  const resync = () => {
    const clearBtn = $('cookiefile_clear');
    if (clearBtn) clearBtn.hidden = !$('cookiefile').value.trim();
    syncComposeAccessFromAuth(_lastAuthStatus);
  };
  $('cookiefile').addEventListener('input', resync);
  $('cookiefile').addEventListener('change', resync);
  // Initial clear-button visibility
  if ($('cookiefile_clear')) $('cookiefile_clear').hidden = !$('cookiefile').value.trim();
}

if ($('cookiefile_browse')) {
  $('cookiefile_browse').onclick = async () => {
    const action = beginAuthenticatedAction();
    const btn = $('cookiefile_browse');
    const control = action.ownControl(btn, { text: 'opening…' });
    try {
      const cur = ($('cookiefile') && $('cookiefile').value.trim()) || '';
      const res = await api('/api/pick-file', {
        method: 'POST',
        body: JSON.stringify({ current: cur, title: 'Select cookies.txt' }),
      });
      action.guard();
      if (res.path) {
        setCookiefilePath(res.path);
        const hint = $('hint');
        if (hint) { hint.className = 'hint ok'; hint.textContent = '✓ Cookies override: ' + res.path; }
      } else if (res.error) {
        const hint = $('hint');
        if (hint) {
          hint.className = 'hint bad';
          hint.textContent = '✗ ' + res.error + ' — use Upload… or type a path on Auth';
        }
      }
      // cancelled → no message
    } catch (e) {
      if (action.isSilentError(e)) return;
      const hint = $('hint');
      if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message; }
    } finally {
      control.release();
    }
  };
}

// The visible control is a real <button> so it is keyboard-reachable (the old <label for> was not
// focusable and the file input is hidden, so Upload… was mouse-only). Forward the click to the
// input's native picker; the change handler below is unchanged.
if ($('cookiefile_upload_btn') && $('cookiefile_upload')) {
  $('cookiefile_upload_btn').addEventListener('click', () => {
    openAuthenticatedFilePicker(
      $('cookiefile_upload'),
      () => $('cookiefile_upload').click(),
    );
  });
}
// Archive file picker — same native dialog as the cookies override. NOTE /api/pick-file is an OPEN
// dialog (webserver.py:2699, askopenfilename), so it can only pick an archive that already exists —
// the repeat-pull case. A first run leaves the box empty and archivePlan() derives the default inside
// "Save to folder", which is why this input is typeable rather than readonly like #cookiefile.
if ($('download_archive_browse')) {
  $('download_archive_browse').onclick = async () => {
    const action = beginAuthenticatedAction();
    const btn = $('download_archive_browse');
    const control = action.ownControl(btn, { text: 'opening…' });
    try {
      const cur = ($('download_archive') && $('download_archive').value.trim()) || '';
      const res = await api('/api/pick-file', {
        method: 'POST',
        body: JSON.stringify({ current: cur, title: 'Select the download archive (.txt)' }),
      });
      action.guard();
      const hint = $('hint');
      if (res.path) {
        $('download_archive').value = res.path;
        // Picking a file IS the intent to use it — leaving the checkbox off would drop it silently.
        if ($('download_archive_on')) $('download_archive_on').checked = true;
        updateComposeSummary();          // a programmatic .value change fires no input event
        if (hint) { hint.className = 'hint ok'; hint.textContent = '✓ Archive: ' + res.path; }
      } else if (res.error && hint) {
        hint.className = 'hint bad';
        hint.textContent = '✗ ' + res.error + ' — type the path instead';
      }
      // cancelled → no message
    } catch (e) {
      if (action.isSilentError(e)) return;
      const hint = $('hint');
      if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message; }
    } finally {
      control.release();
    }
  };
}

if ($('cookiefile_upload')) {
  $('cookiefile_upload').addEventListener('change', async () => {
    const input = $('cookiefile_upload');
    const action = takeAuthenticatedFilePickerAction(input);
    if (!action) return;
    const file = input.files && input.files[0];
    if (!file) return;
    const hint = $('hint');
    try {
      const content = await file.text();
      action.guard();
      const res = await api('/api/upload-cookiefile', {
        method: 'POST',
        body: JSON.stringify({ content }),
      });
      action.guard();
      if (res.ok && res.path) {
        setCookiefilePath(res.path);
        if (hint) {
          hint.className = 'hint ok';
          hint.textContent = '✓ ' + (res.message || 'Override saved') + ' → ' + res.path;
        }
      } else {
        if (hint) {
          hint.className = 'hint bad';
          hint.textContent = '✗ ' + (res.error || 'Upload failed');
        }
      }
    } catch (e) {
      if (action.isSilentError(e)) return;
      if (hint) { hint.className = 'hint bad'; hint.textContent = '✗ ' + e.message; }
    } finally {
      action.effect(() => { $('cookiefile_upload').value = ''; });
    }
  });
}

if ($('cookiefile_clear')) {
  $('cookiefile_clear').onclick = () => {
    setCookiefilePath('');
    const hint = $('hint');
    if (hint) { hint.className = 'hint ok'; hint.textContent = '✓ Override cleared — using Auth session'; }
  };
}

const JOBS_POLL_MS = 1200;
/** Self-scheduling job poll: each tick is armed only AFTER the previous one settles, so a slow
 *  /api/jobs delays the next poll instead of being aborted by it (see refresh()). */
function scheduleJobsPoll() {
  if (jobsTimer != null) return;
  const action = beginAuthenticatedAction();
  jobsTimer = authenticatedTimeout(async () => {
    jobsTimer = null;
    try {
      await refresh();
    } finally {
      // !document.hidden is load-bearing: hiding the tab mid-poll leaves jobsTimer null, so
      // stopPolling() has nothing to clear and this settle used to put the chain straight back —
      // the poller then ran for the whole hidden period, which is what visibilitychange exists
      // to prevent. isCurrent() only covers auth-session changes, not tab visibility.
      if (appStarted && !document.hidden && action.isCurrent()) scheduleJobsPoll();
    }
  }, JOBS_POLL_MS);
}
function syncWatchPolling(view) {
  if (view) _currentView = view;
  const wantWatch = appStarted && _currentView === 'watch';
  if (wantWatch && watchTimer == null) {
    refreshWatches();                                  // switching TO Watch must not show stale rows
    watchTimer = authenticatedInterval(refreshWatches, 5000);
  } else if (!wantWatch && watchTimer != null) {
    clearAuthenticatedInterval(watchTimer); watchTimer = null;
  }
}
function startPolling() {
  scheduleJobsPoll();
  // The jobs poll always runs (the Pipeline lives on the default view and the header queue/LED
  // reflect it everywhere). Watch is view-scoped — see syncWatchPolling.
  syncWatchPolling();
}
function stopPolling() {
  if (jobsTimer != null) { clearAuthenticatedTimeout(jobsTimer); jobsTimer = null; }
  if (watchTimer != null) { clearAuthenticatedInterval(watchTimer); watchTimer = null; }
  if (jobsAbort) { jobsAbort.abort(); jobsAbort = null; }
  if (watchesAbort) { watchesAbort.abort(); watchesAbort = null; }
  jobsInFlight = false;
  // Must clear here too: nulling watchesAbort above defeats the ownership check that is the
  // in-flight flag's ONLY other clear (`if (watchesAbort === myAbort)`), and on the sessionLost
  // path action.effect is a no-op anyway because the session generation changed. Without this the
  // flag latches true and every non-forced revival — startApp, the view switch, the 5s interval —
  // early-returns, silently killing the Watch panel's auto-refresh for the life of the page.
  watchesInFlight = false;
}
function stopClock() {
  if (clockTimer != null) { clearAuthenticatedInterval(clockTimer); clockTimer = null; }
}
function resetAuthenticatedUiState() {
  resetAuthenticatedControls();
  resetRerunState();
  jobsInFlight = false;
  watchesInFlight = false;      // same reason as in stopPolling(): the ownership clear can't fire
  stRunning = false;
  stStopPhases();
  const speedButton = $('st_run');
  if (speedButton) {
    speedButton.disabled = false;
    speedButton.textContent = 'Run speed test';
  }
  watchActionsInFlight.clear();
  invalidateUpdateScan();
  setUpdatesScanBusy(false);
  _updatesInstallOwner = null;
  setUpdatesBusy(false);
}
document.addEventListener('visibilitychange', () => {
  if (!appStarted) return;
  if (document.hidden) {
    stopPolling();
  } else {
    refresh({ force: true }); refreshWatches({ force: true });
    startPolling();
  }
});

function startApp() {
  if (appStarted) return;
  appStarted = true;
  showApp();
  startPolling();
  if (clockTimer == null) {
    clockTimer = authenticatedInterval(() => {
      if ($('clock')) $('clock').textContent = new Date().toLocaleTimeString();
      tickElapsed();
    }, 1000);
  }
  runAuthenticatedViewEffects(_currentView);
  boot(); refresh(); refreshWatches(); refreshAuth(); loadNetworkSettings();
}

async function tryExistingSession() {
  const stored = loadSession();
  if (!stored) { showLogin(); return false; }
  const action = beginAuthenticatedAction();
  const loginAttemptGeneration = LOGIN_ATTEMPT_GENERATION;
  const guardRestoreOwner = () => {
    action.guard();
    if (loginAttemptGeneration !== LOGIN_ATTEMPT_GENERATION) throw staleSessionError();
  };
  try {
    const res = await sessionFetch('/api/session', {}, guardRestoreOwner);
    guardRestoreOwner();
    if (res.status !== 200 || res.ok !== true) {
      showLogin(
        res.status === 403
          ? 'Session restore blocked by server policy — sign in again'
          : 'Session temporarily unavailable — try again',
        'alert',
      );
      return false;
    }
    const body = await sessionJson(res);
    guardRestoreOwner();
    const expiry = body && typeof body === 'object' && !Array.isArray(body)
      ? body.expires_at : null;
    if (!body || typeof body !== 'object' || Array.isArray(body)
        || body.ok !== true || !sessionExpiryIsValid(expiry)) {
      showLogin('Session temporarily unavailable — try again', 'alert');
      return false;
    }
    if (!setSession(stored.token, expiry)) {
      showLogin(SESSION_STORAGE_RETRY_MESSAGE, 'alert');
      return false;
    }
    setLoginFeedback('', 'status');
    startApp();
    return true;
  } catch (e) {
    if (loginAttemptGeneration !== LOGIN_ATTEMPT_GENERATION
        || action.isSilentError(e)) return false;
    showLogin('Session temporarily unavailable — try again', 'alert');
    return false;
  }
}

/**
 * Revoke a server-issued login token that this page cannot adopt.
 *
 * This intentionally bypasses sessionFetch(): a superseded response or failed
 * localStorage write means the issued token is not, and must never become, the
 * current browser session.
 */
async function revokeIssuedLoginSession(token) {
  if (typeof token !== 'string' || !token || token === TOKEN) return false;
  try {
    const response = await fetchWithTimeout(
      '/api/logout',
      {
        method: 'POST',
        headers: { 'X-Token': token },
      },
      5_000,
    );
    return response.ok;
  } catch (error) {
    return false;
  }
}

const loginForm = $('login-form');
if (loginForm) {
  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const pw = ($('login-password') && $('login-password').value) || '';
    const btn = $('login-submit');
    invalidateLoginAttempt();
    const attemptController = new AbortController();
    LOGIN_ATTEMPT_CONTROLLER = attemptController;
    const attemptGeneration = LOGIN_ATTEMPT_GENERATION;
    const attemptIsCurrent = () => attemptGeneration === LOGIN_ATTEMPT_GENERATION
      && LOGIN_ATTEMPT_CONTROLLER === attemptController
      && !attemptController.signal.aborted;
    if (!pw) {
      setLoginFeedback('Enter the password', 'alert');
      LOGIN_ATTEMPT_CONTROLLER = null;
      if (btn) btn.disabled = false;
      return;
    }
    if (btn) btn.disabled = true;
    setLoginFeedback('Signing in…', 'status');
    try {
      const exchange = await runWithDeadline(
        async (signal) => {
          const response = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: pw }),
            signal,
          });
          let body;
          try {
            body = await response.json();
          } catch (error) {
            if (signal.aborted || (error && (
              error.name === 'AbortError' || error.name === 'TimeoutError'
            ))) {
              throw error;
            }
            body = {};
          }
          return { response, body };
        },
        { signal: attemptController.signal },
        30_000,
      );
      const res = exchange.response;
      const body = exchange.body;
      const bodyIsObject = !!body && typeof body === 'object' && !Array.isArray(body);
      const expiry = bodyIsObject ? body.expires_at : null;
      const issuedToken = res.ok && bodyIsObject && body.ok === true
        && typeof body.token === 'string' && body.token ? body.token : '';
      const validSession = !!issuedToken && sessionExpiryIsValid(expiry);
      if (!attemptIsCurrent()) {
        if (issuedToken) await revokeIssuedLoginSession(issuedToken);
        return;
      }
      if (!validSession) {
        const serverError = bodyIsObject && typeof body.error === 'string'
          ? body.error : '';
        setLoginFeedback(
          serverError || (res.ok ? 'Invalid session response' : 'Wrong password'),
          'alert',
        );
        if (issuedToken) {
          await revokeIssuedLoginSession(issuedToken);
        }
        return;
      }
      if (appStarted) {
        stopPolling();
        stopClock();
        resetAuthenticatedUiState();
        appStarted = false;
      }
      if (!setSession(body.token, expiry)) {
        showLogin(SESSION_STORAGE_RETRY_MESSAGE, 'alert');
        await revokeIssuedLoginSession(issuedToken);
        return;
      }
      setLoginFeedback('', 'status');
      startApp();
    } catch (err) {
      if (attemptIsCurrent()) {
        setLoginFeedback('Cannot reach the app — is it running?', 'alert');
      }
    } finally {
      if (attemptIsCurrent()) {
        LOGIN_ATTEMPT_CONTROLLER = null;
        if (btn) btn.disabled = false;
      }
    }
  });
}

const logoutBtn = $('logout');
if (logoutBtn) {
  logoutBtn.onclick = async () => {
    const context = currentSessionContext();
    invalidateLoginAttempt();
    try {
      await sessionFetch('/api/logout', { method: 'POST' });
    } catch (e) {
      // Local logout must still complete if the server was closed or the session already expired.
    } finally {
      if (sessionContextIsCurrent(context)) sessionLost('Signed out', 'status');
    }
  };
}

// Render-only: replay the toast entrance animation whenever a hint strip's message
// changes. Observes text mutations only (never attributes), so toggling the class
// here cannot re-trigger the observer.
(function wireHintFlash() {
  const ids = ['hint', 'watch_hint', 'auth_hint', 'updates_hint', 'login-msg', 'pipe_banner'];
  const mo = new MutationObserver((muts) => {
    for (const m of muts) {
      const el = m.target;
      if (!el || el.nodeType !== 1) continue;
      el.classList.remove('hint-flash');
      void el.offsetWidth; // restart the CSS animation
      el.classList.add('hint-flash');
    }
  });
  ids.forEach((id) => {
    const el = $(id);
    if (el) mo.observe(el, { childList: true, characterData: true, subtree: true });
  });
})();

// Boot: restore session or show password gate
tryExistingSession();

// The .quit-hint labels are filled from HOST_OS inside applyHostOs(), not here: at module level
// HOST_OS is not known yet (it arrives with /api/health), so stamping here would bake in the neutral
// fallback and never correct it.
