import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { writeAtomicJson } from "../../scripts/run_browser_tests.mjs";


const SESSION_KEY = "yt_final_web_session_v2";
const DEFAULT_SESSION_TTL_SECONDS = 43_200;
// A restart re-launches a real CPython process: interpreter start, the whole Final import graph,
// bind, then the accept loop. On the slowest hosted runner that is far more than the 15s these
// waits used to allow, and the symptom was misleading — the listener exists before the accept
// loop runs, so the next navigation failed with ERR_CONNECTION_TIMED_OUT rather than a clear
// "not ready yet". Both deadlines are bounded; they never turn a dead server into a pass.
const SERVER_READY_TIMEOUT_MS = 45_000;
// The restart receipts only cost one 100ms watcher tick plus the requested delay and the child's
// exit, but that whole path competes with the browser for a loaded two-core runner.
const RESTART_RECEIPT_TIMEOUT_MS = 20_000;
// The client gives a login round trip 30s (runWithDeadline in app.js), but the global expect
// budget is 10s. On a loaded two-core runner the POST competes with Chromium for CPU, so the
// assertion could fire while a perfectly good login was still in flight -- the gate had not
// lifted YET, not failed. That is the test being stricter than the contract it is testing, and
// it showed up as an intermittent red on windows-x86_64-py311 with #app-root still hidden.
// Matched to the client's own deadline so the assertion fails only when the login really did.
const LOGIN_VISIBLE_TIMEOUT_MS = 30_000;
const NETWORK_CONSOLE_NOISE = /Failed to load resource|Load failed|NetworkError|ERR_|offline|Fetch API cannot load/i;

function observeRuntime(page) {
  const runtime = {
    unexpected: [],
    networkNoise: [],
  };
  page.on("console", (message) => {
    if (message.type() !== "error" && message.type() !== "warning") return;
    const issue = `console ${message.type()}: ${message.text()}`;
    if (NETWORK_CONSOLE_NOISE.test(issue)) runtime.networkNoise.push(issue);
    else runtime.unexpected.push(issue);
  });
  page.on("pageerror", (error) => {
    runtime.unexpected.push(`pageerror: ${error.message}`);
  });
  return runtime;
}

function expectNoUnexpectedRuntime(runtime) {
  expect(runtime.unexpected).toEqual([]);
  expect(runtime.networkNoise).toEqual([]);
}


function acceptExpectedNetworkNoise(runtime, maximum) {
  expect(runtime.unexpected).toEqual([]);
  expect(runtime.networkNoise.length <= maximum).toBe(true);
  for (const issue of runtime.networkNoise) {
    expect(NETWORK_CONSOLE_NOISE.test(issue)).toBe(true);
  }
  runtime.networkNoise.length = 0;
}

async function login(page, password) {
  await page.locator("#login-password").fill(password);
  await page.locator("#login-submit").click();
  await expect(page.locator("#app-root")).toBeVisible({
    timeout: LOGIN_VISIBLE_TIMEOUT_MS,
  });
  await expect(page.locator("#login-gate")).toBeHidden({
    timeout: LOGIN_VISIBLE_TIMEOUT_MS,
  });
}

async function readSession(page) {
  return page.evaluate((key) => {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  }, SESSION_KEY);
}

async function apiStatus(request, baseURL, endpoint, token = "") {
  const options = token ? { headers: { "X-Token": token } } : {};
  const response = await request.get(`${baseURL}${endpoint}`, options);
  return response.status();
}

async function fulfillJson(route, body, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}


async function readControlReceipt(receiptPath) {
  try {
    return JSON.parse(await readFile(receiptPath, "utf8"));
  } catch (_error) {
    return null;
  }
}


async function requestServerRestart(
  stateRoot,
  {
    delayMs = 0,
    sessionTtlSeconds = DEFAULT_SESSION_TTL_SECONDS,
  } = {},
) {
  const requestId = randomUUID();
  const requestPath = path.join(stateRoot, "restart.request");
  const stoppedPath = path.join(stateRoot, "restart.stopped");
  const ackPath = path.join(stateRoot, "restart.ack");
  await writeAtomicJson(requestPath, {
    delay_ms: delayMs,
    request_id: requestId,
    session_ttl_seconds: sessionTtlSeconds,
  });
  return {
    ackPath,
    requestId,
    stoppedPath,
  };
}


async function waitForRestartReceipt(receiptPath, requestId, timeout) {
  await expect.poll(async () => {
    const receipt = await readControlReceipt(receiptPath);
    return receipt && receipt.request_id;
  }, { timeout }).toBe(requestId);
  return readControlReceipt(receiptPath);
}


async function waitForServerHealth(
  request,
  baseURL,
  timeout = SERVER_READY_TIMEOUT_MS,
) {
  await expect.poll(async () => {
    try {
      const response = await request.get(`${baseURL}/api/health`);
      return response.status();
    } catch (_error) {
      return 0;
    }
  }, { timeout }).toBe(200);
}


// Every test drives the same shared server, and the restart tests can hand the next test a
// process that is still booting. page.goto does not retry a transport-level failure, so without
// this gate a slow restart surfaced as ERR_CONNECTION_TIMED_OUT in an unrelated test instead of a
// bounded wait here. Fail-closed: an unreachable server still fails the test that needs it.
test.beforeEach(async ({ baseURL, request }) => {
  await waitForServerHealth(request, baseURL);
});


function updateReport({ nodeReady = false, nodeVersion = null } = {}) {
  const nodeIsReady = nodeReady || nodeVersion !== null;
  return {
    ok: true,
    checked_at: 1_700_000_000,
    host_os: "Windows",
    host_platform: "win32-x64",
    managed_locations: {
      Windows: "C:\\browser-fixture\\ffmpeg",
    },
    pip: [
      {
        name: "yt-dlp",
        installed: "2026.07.01",
        latest: "2026.07.01",
        current: true,
        verified: true,
      },
    ],
    info: [
      {
        name: "ffmpeg",
        installed: "7.1",
        latest: "7.1",
        path: "C:\\browser-fixture\\ffmpeg.exe",
        managed: true,
        updatable: true,
        outdated: false,
      },
      {
        name: "node",
        installed: nodeVersion || (nodeReady ? "24.18.0" : null),
        latest: "24.18.0",
        installable: true,
        outdated: !nodeIsReady,
        note: "browser fixture",
      },
    ],
    summary: {
      ok: true,
      missing_pip: [],
      broken_pip: [],
      outdated_count: 0,
      warning_count: 0,
      optional_missing: nodeIsReady ? [] : ["node"],
      optional_outdated: [],
    },
    warnings: [],
  };
}

test("runtime capture rejects network-like errors outside an expected window", async ({
  page,
}) => {
  const runtime = observeRuntime(page);
  await page.goto("/");
  await page.evaluate(() => {
    console.error("NetworkError browser-gate sentinel");
  });
  await expect.poll(() => runtime.networkNoise.length).toBe(1);
  let rejected = false;
  try {
    expectNoUnexpectedRuntime(runtime);
  } catch (_error) {
    rejected = true;
  }
  expect(rejected).toBe(true);
});

test("a late session check does not wipe a password being typed", async ({
  page,
}) => {
  const password = process.env.YT_FINAL_BROWSER_PASSWORD;
  expect(Boolean(password)).toBe(true);
  const runtime = observeRuntime(page);

  // #login-gate is visible from the first paint, so a user can type while the app is still
  // awaiting /api/session for a stored session. When that resolves to "not a live session" the
  // app calls showLogin(), which used to clear the password field -- so the click that followed
  // submitted an EMPTY password and the gate never lifted despite a correct password. The
  // existing LOGIN_ATTEMPT_GENERATION guard does not cover it, because the generation only
  // advances on submit.
  //
  // Reproduced through the real path rather than by poking an internal: seed a stale session so
  // the restore actually runs, and hold /api/session open long enough to type underneath it.
  let releaseSessionCheck;
  const sessionCheckHeld = new Promise((resolve) => {
    releaseSessionCheck = resolve;
  });
  await page.route("**/api/session", async (route) => {
    await sessionCheckHeld;
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, error: "expired" }),
    });
  });

  await page.addInitScript(
    ([key, ttl]) => {
      localStorage.setItem(key, JSON.stringify({
        token: "stale-token-from-a-previous-run",
        expires_at: Math.floor(Date.now() / 1000) + ttl,
      }));
    },
    [SESSION_KEY, DEFAULT_SESSION_TTL_SECONDS],
  );

  await page.goto("/");
  await expect(page.locator("#login-gate")).toBeVisible();

  // Type while the restore is still in flight, then let it fail.
  await page.locator("#login-password").fill(password);
  releaseSessionCheck();

  // The late showLogin() lands here. The typed password must survive it.
  await expect(page.locator("#login-msg")).toContainText(/session|sign in/i);
  await expect(page.locator("#login-password")).toHaveValue(password);

  await page.unroute("**/api/session");
  await page.locator("#login-submit").click();
  await expect(page.locator("#app-root")).toBeVisible();
  await expect(page.locator("#login-gate")).toBeHidden();

  acceptExpectedNetworkNoise(runtime, 1);
});


test("authentication and protected routes stay fail-closed", async ({
  browser,
  page,
  request,
}) => {
  const password = process.env.YT_FINAL_BROWSER_PASSWORD;
  expect(Boolean(password)).toBe(true);
  const runtime = observeRuntime(page);

  await page.goto("/?token=retired-query-credential");
  const baseURL = new URL(page.url()).origin;
  await expect(page.locator("#login-gate")).toBeVisible();
  await expect.poll(() => new URL(page.url()).searchParams.has("token")).toBe(
    false,
  );

  await page.locator("#login-password").fill("Wrong password");
  await page.locator("#login-submit").click();
  await expect(page.locator("#login-msg")).toContainText(/wrong password/i);
  await expect(page.locator("#login-msg")).toHaveAttribute("role", "alert");
  await expect(page.locator("#login-msg")).toHaveAttribute(
    "aria-live",
    "assertive",
  );
  await expect(page.locator("#app-root")).toBeHidden();
  acceptExpectedNetworkNoise(runtime, 1);

  await login(page, password);
  await expect(page.locator("#login-msg")).toHaveAttribute("role", "status");
  await expect(page.locator("#login-msg")).toHaveAttribute(
    "aria-live",
    "polite",
  );
  const firstSession = await readSession(page);
  expect(typeof firstSession.token === "string").toBe(true);
  expect(firstSession.token.length > 20).toBe(true);
  expect(typeof firstSession.expires_at === "number").toBe(true);
  expect(firstSession.expires_at > Date.now() / 1000).toBe(true);

  const protectedEndpoints = [
    "/api/session",
    "/api/profiles",
    "/api/gpu",
    "/api/jobs",
    "/api/watches",
    "/api/auth",
    "/api/settings",
    "/api/updates?pypi=0&force=0",
    "/api/history",
  ];
  const protectedStatuses = [];
  for (const endpoint of protectedEndpoints) {
    protectedStatuses.push(
      await apiStatus(request, baseURL, endpoint, firstSession.token),
    );
  }
  expect(protectedStatuses).toEqual(protectedEndpoints.map(() => 200));
  expect(
    await apiStatus(
      request,
      baseURL,
      "/api/jobs?token=retired-query-credential",
    ),
  ).toBe(401);

  await page.reload();
  await expect(page.locator("#app-root")).toBeVisible();
  const restoredSession = await readSession(page);
  expect(restoredSession.token === firstSession.token).toBe(true);

  const secondContext = await browser.newContext();
  const secondPage = await secondContext.newPage();
  const secondRuntime = observeRuntime(secondPage);
  await secondPage.goto(`${baseURL}/`);
  await login(secondPage, password);
  const secondSession = await readSession(secondPage);
  expect(secondSession.token === firstSession.token).toBe(false);
  expect(
    await apiStatus(request, baseURL, "/api/session", firstSession.token),
  ).toBe(200);
  expect(
    await apiStatus(request, baseURL, "/api/session", secondSession.token),
  ).toBe(200);

  await secondPage.evaluate((key) => {
    const record = JSON.parse(localStorage.getItem(key));
    record.expires_at = 1;
    localStorage.setItem(key, JSON.stringify(record));
  }, SESSION_KEY);
  await secondPage.reload();
  await expect(secondPage.locator("#login-gate")).toBeVisible();
  expect((await readSession(secondPage)) === null).toBe(true);
  const expiredCleanup = await request.post(`${baseURL}/api/logout`, {
    headers: { "X-Token": secondSession.token },
  });
  expect(expiredCleanup.status()).toBe(200);
  expectNoUnexpectedRuntime(secondRuntime);
  await secondContext.close();

  await page.locator("#nav-workspace").focus();
  await page.keyboard.press("End");
  await expect(page.locator("#nav-guide")).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.keyboard.press("Home");
  await expect(page.locator("#nav-workspace")).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#nav-watch")).toHaveAttribute(
    "aria-selected",
    "true",
  );

  await page.locator("#logout").click();
  await expect(page.locator("#login-gate")).toBeVisible();
  expect((await readSession(page)) === null).toBe(true);
  expect(
    await apiStatus(request, baseURL, "/api/session", firstSession.token),
  ).toBe(401);
  expectNoUnexpectedRuntime(runtime);
});

test("server loss is visible and restart invalidates sessions", async ({
  page,
  request,
}) => {
  const password = process.env.YT_FINAL_BROWSER_PASSWORD;
  const stateRoot = process.env.YT_FINAL_BROWSER_STATE;
  expect(Boolean(password && stateRoot)).toBe(true);
  const runtime = observeRuntime(page);

  await page.goto("/");
  const baseURL = new URL(page.url()).origin;
  await login(page, password);
  const session = await readSession(page);
  expectNoUnexpectedRuntime(runtime);
  const restart = await requestServerRestart(stateRoot, { delayMs: 3_000 });

  await waitForRestartReceipt(
    restart.stoppedPath,
    restart.requestId,
    RESTART_RECEIPT_TIMEOUT_MS,
  );
  const serverUnreachable = await page.evaluate(async () => {
    try {
      await fetch("/api/health");
      return false;
    } catch (_error) {
      return true;
    }
  });
  expect(serverUnreachable).toBe(true);
  await page.evaluate(() => {
    window.handlePollError(new TypeError("server unavailable"));
    window.handlePollError(new TypeError("server unavailable"));
    window.handlePollError(new TypeError("server unavailable"));
  });
  await expect(page.locator("#conn_banner")).toBeVisible();
  await expect(page.locator("#conn_banner")).toContainText(
    "Lost contact with the app",
  );
  await waitForRestartReceipt(
    restart.ackPath,
    restart.requestId,
    RESTART_RECEIPT_TIMEOUT_MS,
  );
  await waitForServerHealth(request, baseURL);

  await expect(page.locator("#login-gate")).toBeVisible({ timeout: 10_000 });
  expect((await readSession(page)) === null).toBe(true);
  expect(
    await apiStatus(request, baseURL, "/api/session", session.token),
  ).toBe(401);
  acceptExpectedNetworkNoise(runtime, 20);
  expectNoUnexpectedRuntime(runtime);
});

test("server-issued sessions expire without a client-side rewrite", async ({
  page,
  request,
}) => {
  const password = process.env.YT_FINAL_BROWSER_PASSWORD;
  const stateRoot = process.env.YT_FINAL_BROWSER_STATE;
  expect(Boolean(password && stateRoot)).toBe(true);
  const runtime = observeRuntime(page);

  await page.goto("/");
  const baseURL = new URL(page.url()).origin;
  const shortTtlRestart = await requestServerRestart(stateRoot, {
    sessionTtlSeconds: 2,
  });
  await waitForRestartReceipt(
    shortTtlRestart.stoppedPath,
    shortTtlRestart.requestId,
    RESTART_RECEIPT_TIMEOUT_MS,
  );
  await waitForRestartReceipt(
    shortTtlRestart.ackPath,
    shortTtlRestart.requestId,
    RESTART_RECEIPT_TIMEOUT_MS,
  );
  await waitForServerHealth(request, baseURL);

  try {
    await login(page, password);
    const session = await readSession(page);
    expect(session.expires_at - Date.now() / 1000 <= 3).toBe(true);
    expectNoUnexpectedRuntime(runtime);
    await expect.poll(
      () => apiStatus(request, baseURL, "/api/session", session.token),
      { timeout: 10_000 },
    ).toBe(401);
    await expect(page.locator("#login-gate")).toBeVisible({ timeout: 10_000 });
    expect((await readSession(page)) === null).toBe(true);
    acceptExpectedNetworkNoise(runtime, 10);
  } finally {
    const restore = await requestServerRestart(stateRoot);
    await waitForRestartReceipt(
      restore.stoppedPath,
      restore.requestId,
      RESTART_RECEIPT_TIMEOUT_MS,
    );
    await waitForRestartReceipt(
      restore.ackPath,
      restore.requestId,
      RESTART_RECEIPT_TIMEOUT_MS,
    );
    await waitForServerHealth(request, baseURL);
  }

  expectNoUnexpectedRuntime(runtime);
});

test("critical UI actions, live regions, and responsive panels work", async ({
  page,
}) => {
  const password = process.env.YT_FINAL_BROWSER_PASSWORD;
  const runtime = observeRuntime(page);
  await page.goto("/");
  await login(page, password);

  let jobStatus = "queued";
  const browserJob = () => ({
    id: "browser-job",
    status: jobStatus,
    updated_at: Date.now() / 1000,
    label: "Browser fixture",
    last_error: jobStatus === "failed" ? "fixture failure" : "",
    output_paths: ["C:\\browser-fixture\\video.mp4"],
    attempt_count: 1,
    priority: 0,
    progress: null,
    result: null,
    spec: {
      urls: ["https://www.youtube.com/watch?v=browser1"],
    },
  });
  await page.route("**/api/jobs", async (route) => {
    const counts = {
      queued: jobStatus === "queued" ? 1 : 0,
      running: 0,
      done: 0,
      failed: jobStatus === "failed" ? 1 : 0,
      cancelled: 0,
    };
    await fulfillJson(route, {
      jobs: [browserJob()],
      counts,
      paused: false,
    });
  });

  let rerunRoute = null;
  let rerunRequestId = "";
  let rerunRequests = 0;
  await page.route("**/api/jobs/rerun", async (route) => {
    const body = route.request().postDataJSON();
    rerunRequests += 1;
    rerunRequestId = body.request_id;
    rerunRoute = route;
  });

  const updateRequests = [];
  let quietFullScanRoute = null;
  let explicitScanRoute = null;
  await page.route("**/api/updates", async (route) => {
    const body = route.request().postDataJSON();
    updateRequests.push({
      force: Boolean(body.force),
      pypi: Boolean(body.pypi),
    });
    if (!body.force && body.pypi) {
      quietFullScanRoute = route;
      return;
    }
    if (body.force) {
      explicitScanRoute = route;
      return;
    }
    await fulfillJson(route, updateReport());
  });

  let installRoute = null;
  let installRequests = 0;
  await page.route("**/api/updates/tools", async (route) => {
    installRequests += 1;
    installRoute = route;
  });

  await page.evaluate(() => window.refresh({ force: true }));
  const jobCard = page.locator('[data-id="browser-job"]');
  await expect(jobCard.locator(".pill")).toHaveText("queued");
  jobStatus = "failed";
  await page.evaluate(() => window.refresh({ force: true }));
  await expect(jobCard.locator(".pill")).toHaveText("failed");
  await expect(page.locator("#jobs_live")).toContainText("failed");
  await expect(page.locator("#jobs_live")).toHaveAttribute("role", "status");
  await expect(page.locator("#jobs_live")).toHaveAttribute(
    "aria-live",
    "polite",
  );

  const rerunButton = jobCard.getByRole("button", { name: "↻ retry" });
  await rerunButton.evaluate((button) => {
    button.click();
    button.click();
  });
  await expect.poll(() => rerunRequests).toBe(1);
  await expect(rerunButton).toBeDisabled();
  expect(Boolean(rerunRequestId && rerunRoute)).toBe(true);
  await fulfillJson(rerunRoute, {
    id: "browser-job-rerun",
    request_id: rerunRequestId,
  });
  await expect(rerunButton).toBeEnabled();
  expect(rerunRequests).toBe(1);

  const timeoutPatched = await page.evaluate(() => {
    if (typeof AbortSignal.timeout !== "function") return false;
    const timeoutDescriptor = Object.getOwnPropertyDescriptor(
      AbortSignal,
      "timeout",
    );
    const originalTimeout = AbortSignal.timeout;
    const originalFetch = window.fetch;
    Object.defineProperty(AbortSignal, "timeout", {
      configurable: true,
      value: () => originalTimeout.call(AbortSignal, 300),
    });
    window.fetch = (input, init = {}) => {
      const rawUrl = typeof input === "string" ? input : input.url;
      const requestUrl = new URL(rawUrl, location.href);
      if (requestUrl.pathname !== "/api/open-folder") {
        return originalFetch.call(window, input, init);
      }
      return new Promise((_resolve, reject) => {
        const signal = init.signal;
        const rejectAbort = () => reject(signal.reason);
        if (signal && signal.aborted) rejectAbort();
        else if (signal) {
          signal.addEventListener("abort", rejectAbort, { once: true });
        }
      });
    };
    window.__restoreBrowserTimeoutFixture = () => {
      window.fetch = originalFetch;
      Object.defineProperty(AbortSignal, "timeout", timeoutDescriptor);
      delete window.__restoreBrowserTimeoutFixture;
    };
    return true;
  });
  expect(timeoutPatched).toBe(true);
  const openFolder = jobCard.getByRole("button", { name: "open folder" });
  await openFolder.click();
  await expect(openFolder).toBeDisabled();
  await expect(page.locator("#hint")).toContainText(
    "no answer after 30s",
  );
  await expect(page.locator("#hint")).toContainText(
    "The button works again",
  );
  await expect(openFolder).toBeEnabled();
  await page.evaluate(() => window.__restoreBrowserTimeoutFixture());

  await page.locator("#nav-updates").click();
  await expect.poll(() => updateRequests.length).toBeGreaterThanOrEqual(2);
  expect(updateRequests.slice(0, 2)).toEqual([
    { force: false, pypi: false },
    { force: false, pypi: true },
  ]);
  await expect(page.locator("#updates_body")).toContainText(
    "Download packages",
  );
  await expect(page.locator("#updates_hint")).toHaveAttribute(
    "aria-live",
    "polite",
  );

  expect(Boolean(quietFullScanRoute)).toBe(true);
  await page.locator("#updates_check").click();
  await expect.poll(() => Boolean(explicitScanRoute)).toBe(true);
  await expect(page.locator("#updates_check")).toBeDisabled();
  await fulfillJson(
    explicitScanRoute,
    updateReport({ nodeVersion: "24.99-newer" }),
  );
  explicitScanRoute = null;
  await expect(page.locator("#updates_hint")).toContainText("scan complete");
  await expect(page.locator("#updates_check")).toBeEnabled();
  await expect(page.locator("#updates_body")).toContainText("24.99-newer");
  await fulfillJson(
    quietFullScanRoute,
    updateReport({ nodeVersion: "1.00-stale" }),
  );
  quietFullScanRoute = null;
  await expect(page.locator("#updates_body")).toContainText("24.99-newer");
  await expect(page.locator("#updates_body")).not.toContainText("1.00-stale");

  page.once("dialog", (dialog) => dialog.accept());
  await page.locator('[data-system-tool="node"]').click();
  await expect.poll(() => installRequests).toBe(1);
  await expect(page.locator("#updates_check")).toBeDisabled();
  await expect(page.locator(".upd-act:not(:disabled)")).toHaveCount(0);
  await page.locator("#nav-guide").click();
  await page.locator("#nav-updates").click();
  await expect(page.locator('[data-system-tool="node"]')).toBeDisabled();
  await fulfillJson(installRoute, {
    ok: true,
    result: {
      ok: true,
      outdated: false,
      message: "node ready",
    },
    report: updateReport({ nodeReady: true }),
  });
  await expect(page.locator("#updates_hint")).toContainText("node ready");
  await expect(page.locator("#updates_check")).toBeEnabled();
  await expect(page.locator('[data-system-tool="node"]')).toBeEnabled();
  expect(installRequests).toBe(1);

  const panels = [
    ["workspace", "Compose job"],
    ["watch", "Watch channels"],
    ["auth", "Auth"],
    ["speedtest", "Speed test"],
    ["updates", "Updates"],
    ["guide", "Field guide"],
  ];
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 820, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    for (const [name, heading] of panels) {
      const tab = page.locator(`#nav-${name}`);
      const panel = page.locator(`#view-${name}`);
      await tab.click();
      await expect(tab).toHaveAttribute("aria-selected", "true");
      await expect(panel).toBeVisible();
      await expect(panel.locator("h2").first()).toHaveText(heading);
      const bounds = await page.evaluate((tabId) => {
        const selected = document.querySelector(tabId);
        const nav = document.querySelector(".app-nav");
        const selectedRect = selected.getBoundingClientRect();
        const navRect = nav.getBoundingClientRect();
        return {
          documentWidth: document.documentElement.scrollWidth,
          viewportWidth: window.innerWidth,
          tabInsideNav: (
            selectedRect.left >= navRect.left - 1
            && selectedRect.right <= navRect.right + 1
          ),
        };
      }, `#nav-${name}`);
      expect(bounds.documentWidth <= bounds.viewportWidth + 1).toBe(true);
      expect(bounds.tabInsideNav).toBe(true);
    }
  }

  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator("#nav-workspace").focus();
  await page.evaluate(() => {
    document.querySelector(".app-nav").scrollLeft = 0;
  });
  await page.keyboard.press("End");
  await expect(page.locator("#nav-guide")).toBeFocused();
  await expect(page.locator("#nav-guide")).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.locator("#view-guide")).toBeVisible();
  const keyboardTabVisible = await page.evaluate(() => {
    const nav = document.querySelector(".app-nav").getBoundingClientRect();
    const tab = document.querySelector("#nav-guide").getBoundingClientRect();
    return tab.left >= nav.left - 1 && tab.right <= nav.right + 1;
  });
  expect(keyboardTabVisible).toBe(true);
  await page.keyboard.press("Home");
  await expect(page.locator("#nav-workspace")).toBeFocused();
  await expect(page.locator("#view-workspace")).toBeVisible();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#nav-watch")).toBeFocused();
  await expect(page.locator("#view-watch")).toBeVisible();

  await expect(page.locator("#conn_banner")).toHaveAttribute("role", "status");
  expectNoUnexpectedRuntime(runtime);
  await page.locator("#logout").click();
  await expect(page.locator("#login-gate")).toBeVisible();
});
