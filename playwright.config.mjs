import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "@playwright/test";


const repoRoot = path.dirname(fileURLToPath(import.meta.url));
const browserName = process.env.YT_FINAL_BROWSER_NAME || "chromium";
if (!["chromium", "webkit"].includes(browserName)) {
  throw new Error("YT_FINAL_BROWSER_NAME must be chromium or webkit");
}
const port = Number(process.env.YT_FINAL_BROWSER_PORT);
if (!Number.isInteger(port) || port < 1024 || port > 65535) {
  throw new Error("run browser tests through scripts/run_browser_tests.mjs");
}
const browserPassword = (
  process.env.YT_FINAL_BROWSER_PASSWORD || "yt-final-browser-fixture"
);
process.env.YT_FINAL_BROWSER_PASSWORD = browserPassword;
const stateRoot = process.env.YT_FINAL_BROWSER_STATE;
if (!stateRoot || !path.isAbsolute(stateRoot)) {
  throw new Error("browser-test state must be provisioned by the runner");
}

export default defineConfig({
  testDir: "./tests/browser",
  outputDir: path.join(stateRoot, "results"),
  // The restart tests stop and re-launch a real CPython server mid-test. On the slowest hosted
  // runner (macOS arm64) each of those boots costs tens of seconds, so 45s truncated the test
  // before its own bounded waits could report WHY the server had not come back.
  timeout: 240_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  preserveOutput: "never",
  reporter: [
    ["line"],
    [path.join(repoRoot, "tests", "browser", "fail_closed_reporter.mjs")],
  ],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName,
    headless: true,
    screenshot: "off",
    trace: "off",
    video: "off",
  },
  webServer: {
    command: "node scripts/run_browser_test_server.mjs",
    cwd: repoRoot,
    env: {
      ...process.env,
      DEV_PYTHON: process.env.DEV_PYTHON || process.env.PYTHON || "python",
      YT_FINAL_BROWSER_PASSWORD: browserPassword,
      YT_FINAL_BROWSER_PORT: String(port),
      YT_FINAL_BROWSER_STATE: stateRoot,
    },
    reuseExistingServer: false,
    // These control the HARNESS's own streams, not the Python child's. The harness discards the
    // child's stdout (it carries the ready banner's password — see run_browser_test_server.mjs)
    // and inherits its stderr, so what surfaces here is the harness's own messages plus the
    // server's tracebacks: diagnosable, with no secret on either stream.
    stdout: "pipe",
    stderr: "pipe",
    // Cold start on macOS arm64 is a fresh CPython plus the whole Final import graph; keep a wide
    // margin so a merely slow runner is not reported as an unreachable server.
    timeout: 180_000,
    url: `http://127.0.0.1:${port}/`,
  },
});
