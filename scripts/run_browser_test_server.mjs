import { spawn } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import {
  clearInterval,
  clearTimeout,
  setInterval,
  setTimeout,
} from "node:timers";
import { fileURLToPath } from "node:url";


const repoRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const stateRoot = process.env.YT_FINAL_BROWSER_STATE;
const password = process.env.YT_FINAL_BROWSER_PASSWORD;
const python = process.env.DEV_PYTHON;
const portText = process.env.YT_FINAL_BROWSER_PORT;
const port = Number(portText);
const defaultSessionTtlSeconds = 43_200;

if (!stateRoot || !path.isAbsolute(stateRoot)) {
  throw new Error("YT_FINAL_BROWSER_STATE must be one absolute external path");
}
const relativeState = path.relative(repoRoot, stateRoot);
if (
  relativeState === ""
  || (
    !path.isAbsolute(relativeState)
    && !relativeState.startsWith(`..${path.sep}`)
    && relativeState !== ".."
  )
) {
  throw new Error("browser-test state must stay outside the checkout");
}
if (
  path.basename(path.dirname(stateRoot)) !== "yt-final-browser"
  || !/^\d+-\d+-[0-9a-f]{12}$/.test(path.basename(stateRoot))
) {
  throw new Error("browser-test state path does not match the reviewed shape");
}
if (!password || !python) {
  throw new Error("browser-test password and Python interpreter are required");
}
if (!Number.isInteger(port) || port < 1024 || port > 65535) {
  throw new Error("YT_FINAL_BROWSER_PORT must be a non-privileged TCP port");
}

const serverRoot = path.join(stateRoot, "server");
const restartRequest = path.join(stateRoot, "restart.request");
const restartAck = path.join(stateRoot, "restart.ack");
const restartStopped = path.join(stateRoot, "restart.stopped");
mkdirSync(serverRoot, { recursive: true });
rmSync(restartRequest, { force: true });
rmSync(restartAck, { force: true });
rmSync(restartStopped, { force: true });

let child = null;
let generation = 0;
let stopping = false;
let restarting = false;
let restartDelayMs = 0;
let restartControl = null;
let sessionTtlSeconds = defaultSessionTtlSeconds;
let killTimer = null;
let restartTimer = null;
let finalExitCode = 0;


function browserPythonBootstrap(ttlSeconds) {
  return [
    "import runpy",
    "import sys",
    `sys.path.insert(0, ${JSON.stringify(path.join(repoRoot, "Final"))})`,
    "import websessions as _browser_websessions",
    "_browser_real_session_store = _browser_websessions.SessionStore",
    "class _BrowserSessionStore(_browser_real_session_store):",
    "    def __init__(self, *args, **kwargs):",
    `        kwargs.setdefault("ttl_seconds", ${ttlSeconds})`,
    "        super().__init__(*args, **kwargs)",
    "_browser_websessions.SessionStore = _BrowserSessionStore",
    'runpy.run_module("Final", run_name="__main__", alter_sys=True)',
  ].join("\n");
}


function parseRestartControl(raw) {
  const control = JSON.parse(raw);
  if (!control || typeof control !== "object" || Array.isArray(control)) {
    throw new Error("restart control must be one JSON object");
  }
  const keys = Object.keys(control).sort();
  if (
    keys.join(",")
    !== "delay_ms,request_id,session_ttl_seconds"
  ) {
    throw new Error("restart control has unexpected fields");
  }
  if (
    typeof control.request_id !== "string"
    || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(
      control.request_id,
    )
  ) {
    throw new Error("restart control request_id is invalid");
  }
  if (
    !Number.isInteger(control.delay_ms)
    || control.delay_ms < 0
    || control.delay_ms > 10_000
  ) {
    throw new Error("restart control delay_ms is invalid");
  }
  if (
    !Number.isInteger(control.session_ttl_seconds)
    || control.session_ttl_seconds < 1
    || control.session_ttl_seconds > defaultSessionTtlSeconds
  ) {
    throw new Error("restart control session_ttl_seconds is invalid");
  }
  return control;
}


function writeRestartReceipt(receiptPath, control, instanceGeneration) {
  writeFileSync(
    receiptPath,
    `${JSON.stringify({
      generation: instanceGeneration,
      request_id: control.request_id,
      session_ttl_seconds: control.session_ttl_seconds,
    })}\n`,
    {
      encoding: "utf8",
      mode: 0o600,
    },
  );
}

function cleanupSensitiveState() {
  rmSync(restartRequest, { force: true });
  rmSync(restartAck, { force: true });
  rmSync(restartStopped, { force: true });
  rmSync(serverRoot, { recursive: true, force: true, maxRetries: 3 });
}

function scheduleHardKill(target) {
  if (killTimer) clearTimeout(killTimer);
  killTimer = setTimeout(() => {
    if (child === target) target.kill("SIGKILL");
  }, 5_000);
  killTimer.unref();
}

function stopChild(target, signal) {
  if (!target) return;
  target.kill(signal === "SIGINT" ? "SIGINT" : "SIGTERM");
  scheduleHardKill(target);
}

function startChild() {
  generation += 1;
  const instanceRoot = path.join(serverRoot, `instance-${generation}`);
  const outputDir = path.join(instanceRoot, "downloads");
  const queueFile = path.join(instanceRoot, "queue.json");
  mkdirSync(instanceRoot, { recursive: true });

  const target = spawn(
    python,
    [
      "-B",
      "-c",
      browserPythonBootstrap(sessionTtlSeconds),
      "--output-dir",
      outputDir,
      "serve",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--queue-file",
      queueFile,
      "--no-browser",
    ],
    {
      cwd: instanceRoot,
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONNOUSERSITE: "1",
        PYTHONPATH: repoRoot,
        YT_FINAL_HOME: instanceRoot,
        YT_FINAL_WEBUI_PASSWORD: password,
      },
      // stdout is DISCARDED on purpose, and must stay that way. The ready banner prints the
      // per-launch web UI password (Final/webserver.py::_print_ready_banner), and Playwright
      // relays webServer output straight into the CI log — piping it would publish that password
      // on every run. Discarding is a stronger guarantee than redacting it after the fact, and it
      // also keeps the child from ever blocking on an undrained pipe. Diagnostics are not lost:
      // stderr is inherited, so tracebacks and startup errors still reach the log.
      stdio: ["ignore", "ignore", "inherit"],
      windowsHide: true,
    },
  );
  child = target;

  target.once("error", (error) => {
    process.stderr.write(`browser-test server failed to start: ${error.message}\n`);
    stopping = true;
    finalExitCode = 1;
    clearInterval(restartWatcher);
    cleanupSensitiveState();
    process.exitCode = finalExitCode;
  });
  target.once("exit", (code) => {
    if (killTimer) {
      clearTimeout(killTimer);
      killTimer = null;
    }
    if (child === target) child = null;
    if (stopping) {
      cleanupSensitiveState();
      process.exitCode = finalExitCode;
      return;
    }
    if (!restarting) {
      clearInterval(restartWatcher);
      cleanupSensitiveState();
      finalExitCode = Number.isInteger(code) && code !== 0 ? code : 1;
      process.exitCode = finalExitCode;
      return;
    }
    const completedControl = restartControl;
    writeRestartReceipt(restartStopped, completedControl, generation);
    restarting = false;
    restartTimer = setTimeout(() => {
      restartTimer = null;
      if (stopping) return;
      startChild();
      writeRestartReceipt(restartAck, completedControl, generation);
      restartControl = null;
    }, restartDelayMs);
  });
}

function requestRestart() {
  if (stopping || restarting || !child || !existsSync(restartRequest)) return;
  let control;
  try {
    control = parseRestartControl(readFileSync(restartRequest, "utf8"));
    rmSync(restartRequest, { force: true });
  } catch (error) {
    process.stderr.write(
      `browser-test restart control failed: ${error.message}\n`,
    );
    finalExitCode = 1;
    rmSync(restartRequest, { force: true });
    stop("SIGTERM");
    return;
  }
  restartDelayMs = control.delay_ms;
  restartControl = control;
  sessionTtlSeconds = control.session_ttl_seconds;
  restarting = true;
  stopChild(child, "SIGTERM");
}

function stop(signal) {
  if (stopping) return;
  stopping = true;
  clearInterval(restartWatcher);
  if (restartTimer) {
    clearTimeout(restartTimer);
    restartTimer = null;
  }
  if (child) stopChild(child, signal);
  else {
    cleanupSensitiveState();
    process.exitCode = finalExitCode;
  }
}

const restartWatcher = setInterval(requestRestart, 100);
process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
process.on("exit", cleanupSensitiveState);
startChild();
