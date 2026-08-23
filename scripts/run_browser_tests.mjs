import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { rename, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";


const repoRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

export async function writeAtomicJson(destination, value) {
  const temporary = (
    `${destination}.${process.pid}-${randomBytes(6).toString("hex")}.tmp`
  );
  try {
    await writeFile(temporary, `${JSON.stringify(value)}\n`, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    });
    await rename(temporary, destination);
  } finally {
    await rm(temporary, { force: true });
  }
}


export function stopChildBounded(target, signal, hardKillAfterMs = 5_000) {
  target.kill(signal === "SIGINT" ? "SIGINT" : "SIGTERM");
  const timer = setTimeout(() => target.kill("SIGKILL"), hardKillAfterMs);
  timer.unref();
  return () => clearTimeout(timer);
}


async function availableLoopbackPort() {
  return new Promise((resolve, reject) => {
    const reservation = createServer();
    reservation.unref();
    reservation.once("error", reject);
    reservation.listen(0, "127.0.0.1", () => {
      const address = reservation.address();
      if (!address || typeof address === "string") {
        reservation.close();
        reject(new Error("could not reserve a loopback browser-test port"));
        return;
      }
      reservation.close((error) => {
        if (error) reject(error);
        else resolve(address.port);
      });
    });
  });
}

async function run() {
  const port = await availableLoopbackPort();
  const temporaryRoot = process.env.RUNNER_TEMP || os.tmpdir();
  const stateRoot = path.join(
    temporaryRoot,
    "yt-final-browser",
    `${process.pid}-${Date.now()}-${randomBytes(6).toString("hex")}`,
  );
  const cli = path.join(
    repoRoot,
    "node_modules",
    "@playwright",
    "test",
    "cli.js",
  );
  if (!existsSync(cli)) {
    throw new Error("Playwright is not installed; run npm ci --ignore-scripts");
  }
  mkdirSync(stateRoot, { recursive: true });

  let child = null;
  let forwardedSignal = null;
  let cancelTermination = null;
  const forward = (signal) => {
    forwardedSignal = signal;
    if (child && !cancelTermination) {
      cancelTermination = stopChildBounded(child, signal);
    }
  };
  process.once("SIGINT", () => forward("SIGINT"));
  process.once("SIGTERM", () => forward("SIGTERM"));

  try {
    const exitCode = await new Promise((resolve, reject) => {
      child = spawn(
        process.execPath,
        [cli, "test", "--config=playwright.config.mjs"],
        {
          cwd: repoRoot,
          env: {
            ...process.env,
            YT_FINAL_BROWSER_PORT: String(port),
            YT_FINAL_BROWSER_STATE: stateRoot,
          },
          stdio: "inherit",
          windowsHide: true,
        },
      );
      child.once("error", reject);
      child.once("exit", (code) => {
        if (cancelTermination) {
          cancelTermination();
          cancelTermination = null;
        }
        child = null;
        resolve(Number.isInteger(code) ? code : 1);
      });
    });
    process.exitCode = forwardedSignal ? 1 : exitCode;
  } finally {
    if (cancelTermination) cancelTermination();
    rmSync(stateRoot, {
      recursive: true,
      force: true,
      maxRetries: 10,
      retryDelay: 100,
    });
  }
}

if (
  process.argv[1]
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  await run();
}
