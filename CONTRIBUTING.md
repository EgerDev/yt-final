# Contributing to yt_final

Focused fixes that improve correctness, editor workflows, documentation, tests,
security, or supported-platform reliability are welcome.

## Development environment

Use Python 3.11 or newer on Windows or macOS. Linux can be useful for
development, but it is not a supported 1.0.0 runtime target and cannot replace
the required Windows/macOS evidence.

Create an isolated environment and install the exact hash-locked development
set. Call the environment's interpreter by path rather than activating it: the
lock pins `pip` and `setuptools`, so running it with whichever `python` is on
PATH rewrites that interpreter's own packaging tools.

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/dev.lock
npm ci --ignore-scripts
```

macOS:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/dev.lock
npm ci --ignore-scripts
```

`npm ci --ignore-scripts` is what CI installs with, so use it here too: it
blocks package install scripts, which is supply-chain hardening. (Only
`fsevents` declares one in this tree.) It is not what keeps Playwright from
fetching browsers: npm never installs browser engines either way. The browser
gate installs those explicitly, and has its own section below.

Every `python` below means that interpreter — `.\.venv\Scripts\python.exe` or
`.venv/bin/python` — not a bare `python` from PATH.

FFmpeg and `ffprobe` are required for media integration tests. Hardware,
network, and credential-dependent tests must state their prerequisites and must
not silently stand in for offline unit coverage.

The full suite takes roughly five minutes. To iterate faster, run one module
(`python -m pytest Final/test_auth_webserver.py`) or one topic
(`python -m pytest Final -k cookie`).

## Authoritative checks

Run commands from the repository root:

Ruff, mypy, the npm/ESLint check, pytest, and the release validators are all
required gates; none is advisory.

```bash
python -m ruff check Final scripts
python -m mypy
python -m pip_audit --strict --require-hashes -r requirements/release.lock
python -m compileall -q Final scripts
python Final/_syntax_check.py
npm run lint
python -m pytest
```

`python -m pytest` is the authoritative test command. Add a focused regression
that fails for a confirmed defect before changing its implementation, then run
the affected and adjacent suites. Do not make a gate green by deleting tests,
weakening assertions, adding broad suppressions, or excluding supported source.

Release and workflow changes must also pass the release packaging, artifact,
workflow-policy, clean-install, and reproducibility tests.

CI adds two gates that are not in the list above: pinned, digest-verified
actionlint over `.github/workflows/`, and gitleaks over the whole checkout. A
workflow change or a secret-shaped string can therefore fail CI with every
local gate green.

## Authenticated browser gate

Browser-facing changes — the web UI, the server's routes, or the session
layer — require authenticated Chromium coverage, plus WebKit evidence when you
are on macOS. The gate drives a real Playwright browser against a real
authenticated loopback server, so `python -m pytest` does not cover it and it
has its own commands.

Set `DEV_PYTHON` to the absolute path of the environment's interpreter. The
harness starts the server from a directory outside the checkout, so a relative
path resolves somewhere else. Setting it is on you: with `DEV_PYTHON` unset the
config falls back to `PYTHON` and then to bare `python`, so the gate will run
against whichever interpreter is on PATH and report a perfectly green result for
an environment you did not mean to test.

npm never installs browser engines, so install the ones you need explicitly:

Windows PowerShell:

```powershell
$env:DEV_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
npm exec -- playwright install chromium
npm run test:browser
```

macOS:

```bash
export DEV_PYTHON="$PWD/.venv/bin/python"
npm exec -- playwright install chromium webkit
npm run test:browser
YT_FINAL_BROWSER_NAME=webkit npm run test:browser
```

`YT_FINAL_BROWSER_NAME` picks the engine and accepts only `chromium` (the
default) or `webkit`. The loopback port and the external state directory are
provisioned per run by `scripts/run_browser_tests.mjs`, and calling Playwright
directly is refused rather than half-configured, so always go through
`npm run test:browser`. CI runs this gate on the Windows Python 3.11 cell with
Chromium and on the macOS arm64 Python 3.11 cell with both engines.

Playwright is a contributor test dependency only. The tool itself never needs
it: its managed login drives the Chrome, Edge, or Brave already installed on
the machine. Never attach a password, session token, cookie, or raw browser
profile to an issue or a pull request.

## Change scope

- Preserve the established CLI and direct-script behavior unless it is proven
  incorrect or unsafe.
- Keep public interfaces backward compatible for 1.0.0.
- Prefer small root-cause fixes over unrelated rewrites.
- Do not add arbitrary download/install sources or mutable `latest` URLs.
- Update active documentation and tests with behavior changes.
- Keep generated artifacts, credentials, cookies, logs, downloads, caches, and
  machine-local settings out of Git.

## Pull requests

Describe the problem, root cause, user impact, regression risk, and exact
verification commands. Separate pre-existing failures from regressions. Include
platform details for Windows/macOS behavior and disclose any test that could not
run.

For release-impacting changes, all checks must run from the final source state;
earlier green runs are not evidence after another edit.

## Licensing of contributions

This project is released under the [Unlicense](LICENSE), which is a dedication
of work to the public domain rather than a licence grant. GitHub's default
"inbound = outbound" rule fits that badly: without an explicit term, each merged
contribution would keep its author's copyright while `LICENSE` claims the whole
work is public domain — making that claim untrue for those lines, and unreliable
for anyone downstream who takes it at face value.

So, explicitly: **by submitting a pull request you dedicate your contribution to
the public domain under the terms of the Unlicense.** You confirm you wrote it,
or that you have the right to dedicate it, and that you are not knowingly
including anyone else's copyrighted or patented material.

If you cannot make that dedication — because an employer owns your work, or the
change carries code under another licence — say so in the pull request before
review rather than after.

## Security

Follow [SECURITY.md](SECURITY.md). Never put vulnerability details, real
credentials, cookies, session tokens, or unsanitized browser traces in a public
issue or pull request.
