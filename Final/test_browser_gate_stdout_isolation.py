"""Executable guards for the browser gate's stdout discipline.

scripts/run_browser_test_server.mjs spawns the Python web server with
``stdio: ["ignore", "ignore", "inherit"]``. Both halves of that matter and neither is
provable by reading the file:

* SECURITY - the child's stdout carries the ready banner, and the banner prints the
  per-launch web UI password (Final/webserver.py::_print_ready_banner). Playwright is
  configured with ``webServer.stdout: "pipe"``, which relays whatever the harness writes
  straight into the CI log. If the child's stdout is ever piped back into the harness's
  own stdout again, that password is published on every CI run.
* DIAGNOSABILITY - stderr stays "inherit", so tracebacks and startup errors still reach
  the log. A future "fix" that silences stderr too would blind the gate.

These tests execute the real, unmodified harness on this host and observe both properties
from the outside, so the CI-only condition (Playwright capturing the harness's streams) is
reproduced locally instead of being taken on trust.

The only seam either test patches is ``child_process.spawn`` itself, through a Node
``--import`` preload. The harness source is never touched: the preload records the options
the production code passes and, for the control case, flips exactly the one field the
reverted variant flipped.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "run_browser_test_server.mjs"
# The same predicate scripts/run_browser_tests.mjs uses to decide the gate can run at all.
# The end-to-end boot below guards the gate, so it runs exactly where the gate does -- on the
# CI cells with `matrix.browsers != ''`, whose `npm ci` step precedes the pytest steps. The
# cheap spawn-contract test above it carries no such condition and runs on every cell.
BROWSER_GATE_INSTALLED = (
    ROOT / "node_modules" / "@playwright" / "test" / "cli.js"
).exists()

# Observes (and, on request, flips) the single spawn call the harness makes. Loaded with
# `node --import`, which runs before the harness's own `import { spawn } from
# "node:child_process"` binds its named exports, so the harness receives this wrapper
# without a byte of the harness changing.
SPAWN_SEAM_OBSERVER = """
import { createRequire } from "node:module";
import { existsSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const childProcess = require("node:child_process");
const realSpawn = childProcess.spawn;

const reportPath = process.env.YT_FINAL_SEAM_REPORT || "";
const stopFile = process.env.YT_FINAL_SEAM_STOP_FILE || "";
const forcePipe = process.env.YT_FINAL_SEAM_FORCE_PIPE === "1";
const killImmediately = process.env.YT_FINAL_SEAM_KILL_IMMEDIATELY === "1";
let recorded = false;

childProcess.spawn = function observedSpawn(...args) {
  const options = args[2] || {};
  const declaredStdio = options.stdio === undefined ? null : options.stdio;
  // The control case: reproduce the reverted variant by piping the child's stdout back
  // into the harness, exactly as the short-lived forwarder did.
  if (forcePipe && Array.isArray(options.stdio)) {
    options.stdio = [options.stdio[0], "pipe", options.stdio[2]];
  }
  const child = realSpawn.apply(this, args);
  if (recorded) return child;
  recorded = true;
  if (reportPath) {
    writeFileSync(
      reportPath,
      JSON.stringify({
        command: args[0],
        stdio: declaredStdio,
        parent_holds_child_stdout: Boolean(child.stdout),
        parent_holds_child_stderr: Boolean(child.stderr),
      }),
      "utf8",
    );
  }
  if (forcePipe && child.stdout) {
    child.stdout.on("data", (chunk) => process.stdout.write(chunk));
    child.stdout.on("error", () => {});
  }
  if (killImmediately) {
    setTimeout(() => {
      try { child.kill("SIGKILL"); } catch { /* already gone */ }
    }, 0);
  }
  if (stopFile) {
    const timer = setInterval(() => {
      if (!existsSync(stopFile)) return;
      clearInterval(timer);
      try { child.kill("SIGTERM"); } catch { /* already gone */ }
      setTimeout(() => {
        try { child.kill("SIGKILL"); } catch { /* already gone */ }
      }, 4000).unref();
    }, 100);
  }
  return child;
};
"""

# A deliberately piped spawn. Running the observer against this proves the observer can
# still tell a piped stdout apart from a discarded one on THIS host, so a clean report
# from the real harness means something.
PIPED_CONTROL_TARGET = """
import { spawn } from "node:child_process";

const child = spawn(
  process.env.YT_FINAL_SEAM_PYTHON,
  ["-B", "-c", "pass"],
  { stdio: ["ignore", "pipe", "inherit"], windowsHide: true },
);
child.once("exit", () => {});
"""


def _node():
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the browser-test harness"
    return node


def _dev_python():
    return os.environ.get("DEV_PYTHON") or os.environ.get("PYTHON") or sys.executable


def _free_loopback_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def _reviewed_state_root(tmp_path):
    """Build the external state path shape run_browser_test_server.mjs demands."""
    state = (
        tmp_path
        / "yt-final-browser"
        / f"{os.getpid()}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
    )
    state.mkdir(parents=True)
    return state


def _harness_environment(tmp_path, password, port):
    environment = dict(os.environ)
    environment.update(
        {
            "YT_FINAL_BROWSER_STATE": str(_reviewed_state_root(tmp_path)),
            "YT_FINAL_BROWSER_PASSWORD": password,
            "YT_FINAL_BROWSER_PORT": str(port),
            "DEV_PYTHON": _dev_python(),
        }
    )
    return environment


def _drain(stream, sink):
    for line in iter(stream.readline, b""):
        sink.append(line)
    stream.close()


def _terminate_tree(process):
    if process.poll() is not None:
        return
    process.kill()
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            check=False,
            capture_output=True,
        )
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def test_browser_harness_hands_the_python_server_a_discarded_stdout(tmp_path):
    """The harness must not keep a readable handle on the server's stdout.

    Fails loudly if stdout is switched back to "pipe" (or to "inherit"), which is the
    shape that lets the ready banner's password reach the CI log.
    """
    node = _node()
    observer = tmp_path / "spawn_seam_observer.mjs"
    observer.write_text(SPAWN_SEAM_OBSERVER, encoding="utf-8")

    # Control: the observer against a spawn that really does pipe stdout.
    control_target = tmp_path / "piped_control_target.mjs"
    control_target.write_text(PIPED_CONTROL_TARGET, encoding="utf-8")
    control_report = tmp_path / "control-report.json"
    control = subprocess.run(
        [node, "--import", observer.as_uri(), str(control_target)],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "YT_FINAL_SEAM_REPORT": str(control_report),
            "YT_FINAL_SEAM_PYTHON": _dev_python(),
        },
    )
    assert control.returncode == 0, control.stdout + control.stderr
    control_observation = json.loads(control_report.read_text(encoding="utf-8"))
    assert control_observation["stdio"] == ["ignore", "pipe", "inherit"], (
        "the spawn observer misreported a known-piped stdout; the observation below "
        "would be meaningless"
    )
    assert control_observation["command"] == _dev_python(), (
        "the spawn observer misreported the spawned command; the observation below "
        "would be meaningless"
    )
    assert control_observation["parent_holds_child_stdout"] is True

    # The real thing: the unmodified production harness.
    report = tmp_path / "harness-report.json"
    environment = _harness_environment(
        tmp_path, "seam-observation-fixture", _free_loopback_port()
    )
    environment.update(
        {
            "YT_FINAL_SEAM_REPORT": str(report),
            "YT_FINAL_SEAM_KILL_IMMEDIATELY": "1",
        }
    )
    harness = subprocess.run(
        [node, "--import", observer.as_uri(), str(HARNESS)],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        env=environment,
    )
    assert report.exists(), (
        "the harness never spawned a server process: "
        + harness.stdout
        + harness.stderr
    )
    observation = json.loads(report.read_text(encoding="utf-8"))

    # The observer only records the FIRST spawn. Pin it to the interpreter the harness
    # launches the server with (scripts/run_browser_test_server.mjs spawns
    # `process.env.DEV_PYTHON`), so a future harness that spawns some helper first cannot
    # silently make the assertions below grade the wrong process.
    assert observation["command"] == _dev_python(), (
        "the recorded spawn is not the Python web server; the stdio contract asserted "
        f"below would describe the wrong child process ({observation['command']!r})"
    )
    assert observation["stdio"] == ["ignore", "ignore", "inherit"], (
        "scripts/run_browser_test_server.mjs must spawn the server with stdout "
        'DISCARDED ("ignore"). It passed '
        f"{observation['stdio']!r} instead. The ready banner prints the web UI "
        "password on stdout and Playwright relays the harness's stdout into the CI "
        "log, so anything other than a discarded stdout needs redaction that this "
        "repository deliberately does not have."
    )
    assert observation["parent_holds_child_stdout"] is False, (
        "the harness holds a readable pipe on the server's stdout, so it CAN forward "
        "the banner's password"
    )
    # "inherit" (asserted above) means the server writes STRAIGHT to the harness's own
    # stderr, so the harness holds no pipe of its own to drain or lose.
    assert observation["parent_holds_child_stderr"] is False, (
        "the harness now buffers the server's stderr instead of letting it through"
    )


def _boot_capture_and_stop(tmp_path, label, force_pipe):
    """Boot the real harness + real server, capturing its streams the way Playwright does."""
    node = _node()
    workspace = tmp_path / label
    workspace.mkdir()
    observer = workspace / "spawn_seam_observer.mjs"
    observer.write_text(SPAWN_SEAM_OBSERVER, encoding="utf-8")
    stop_file = workspace / "stop.request"
    password = f"CanaryWebUiPassword-{uuid.uuid4().hex}"
    port = _free_loopback_port()

    environment = _harness_environment(workspace, password, port)
    environment["YT_FINAL_SEAM_STOP_FILE"] = str(stop_file)
    if force_pipe:
        environment["YT_FINAL_SEAM_FORCE_PIPE"] = "1"
    # A healthy boot is silent on stderr (the request logger is muted), so ask CPython
    # itself for import tracing. It is the narrowest available way to make the SERVER --
    # not the harness -- write to stderr, which is what proves stderr is still inherited.
    environment["PYTHONVERBOSE"] = "1"

    process = subprocess.Popen(
        [node, "--import", observer.as_uri(), str(HARNESS)],
        cwd=str(ROOT),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    captured_stdout: list[bytes] = []
    captured_stderr: list[bytes] = []
    readers = [
        threading.Thread(
            target=_drain, args=(process.stdout, captured_stdout), daemon=True
        ),
        threading.Thread(
            target=_drain, args=(process.stderr, captured_stderr), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    status = None
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=2
                ) as response:
                    status = response.status
                    break
            except urllib.error.HTTPError as error:
                status = error.code
                break
            except OSError:
                time.sleep(0.3)
        # Serving a request already means the ready banner has been printed. Hold the
        # capture open a little longer anyway so both runs get the same window in which
        # a leak could show up -- and so the control's forwarded banner is flushed
        # through the pipe well before the harness is torn down.
        if status is not None:
            time.sleep(2.0)
    finally:
        stop_file.write_text("stop", encoding="utf-8")
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            _terminate_tree(process)
        for reader in readers:
            reader.join(30)

    return {
        "password": password,
        "status": status,
        "stdout": b"".join(captured_stdout).decode("utf-8", "replace"),
        "stderr": b"".join(captured_stderr).decode("utf-8", "replace"),
    }


@pytest.mark.skipif(
    not BROWSER_GATE_INSTALLED,
    reason="Playwright is not installed here, so the browser gate this guards never runs",
)
def test_browser_harness_never_relays_the_ready_banner_password(tmp_path):
    """End-to-end: the web UI password never reaches anything Playwright would log.

    Two boots of the real harness and the real server, differing only in the stdio field
    the reverted variant changed:

    * control - stdout piped back into the harness: the password DOES appear in the
      captured stream, proving the secret is on that stream and that this capture sees it;
    * production - the harness as shipped: the password appears nowhere, while the server
      demonstrably booted (so the banner demonstrably ran) and its stderr demonstrably
      still reaches the capture.
    """
    control = _boot_capture_and_stop(tmp_path, "control-piped", force_pipe=True)
    assert control["status"] == 200, (
        "the control boot never served a request, so its capture proves nothing; "
        "harness stderr tail: " + control["stderr"][-2000:]
    )
    assert control["password"] in control["stdout"], (
        "CONTROL FAILURE: piping the server's stdout did not surface the ready "
        "banner's password in the captured stream, so the production assertion below "
        "would be vacuous. Check Final/webserver.py::_print_ready_banner still prints "
        "the password on stdout."
    )

    shipped = _boot_capture_and_stop(tmp_path, "production", force_pipe=False)
    assert shipped["status"] == 200, (
        "the shipped harness never served a request, so a clean capture would be "
        "vacuous; harness stderr tail: " + shipped["stderr"][-2000:]
    )
    # The banner ran (the server answered a request only after printing it) and the
    # child's stderr still lands in the capture -- diagnostics were not traded away.
    # `import 'websessions'` is CPython's own -v line for the first module the harness's
    # bootstrap imports; only the SERVER can have written it.
    assert any(
        marker in shipped["stderr"]
        for marker in ("import 'websessions'", "import websessions")
    ), (
        "the server's stderr no longer reaches the harness's stderr; a failed boot "
        "would now be undiagnosable in CI"
    )
    assert shipped["password"] not in shipped["stdout"], (
        "the web UI password reached the harness's stdout, which Playwright relays "
        "into the CI log"
    )
    assert shipped["password"] not in shipped["stderr"], (
        "the web UI password reached the harness's stderr, which Playwright relays "
        "into the CI log"
    )
    assert "Password:" not in shipped["stdout"], (
        "the ready banner's password line reached the harness's stdout"
    )
