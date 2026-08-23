import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]

MYPY_FILES = [
    "Final/websessions.py",
    "Final/secureio.py",
    "Final/redaction.py",
    "Final/state_validation.py",
    "Final/watch_filters.py",
    "Final/url_identity.py",
    "Final/notifications.py",
    "Final/installer_lock.py",
    "Final/tool_manifest.py",
    "scripts/*.py",
]

ESLINT_GLOBALS = {
    name: "readonly"
    for name in (
        "AbortController",
        "AbortSignal",
        "DOMException",
        "Event",
        "FileReader",
        "Headers",
        "MutationObserver",
        "URL",
        "URLSearchParams",
        "cancelAnimationFrame",
        "clearInterval",
        "clearTimeout",
        "confirm",
        "console",
        "crypto",
        "document",
        "fetch",
        "history",
        "localStorage",
        "location",
        "matchMedia",
        "navigator",
        "performance",
        "prompt",
        "requestAnimationFrame",
        "sessionStorage",
        "setInterval",
        "setTimeout",
        "window",
    )
}

ESLINT_NODE_GLOBALS = {
    name: "readonly"
    for name in (
        "AbortSignal",
        "URL",
        "clearTimeout",
        "console",
        "document",
        "fetch",
        "localStorage",
        "location",
        "process",
        "setTimeout",
        "window",
    )
}

ESLINT_RULES = {
    rule: "error"
    for rule in (
        "no-async-promise-executor",
        "no-constant-condition",
        "no-dupe-keys",
        "no-promise-executor-return",
        "no-undef",
        "no-unreachable",
        "no-unsafe-finally",
        "use-isnan",
        "valid-typeof",
    )
}


def _effective_requirement_lines(path):
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _load_eslint_flat_config():
    node = shutil.which("node")
    assert node is not None, "Node.js is required to validate the browser lint policy"
    result = subprocess.run(
        [
            node,
            "-e",
            (
                "const path = require('node:path');"
                "const config = require(path.resolve(process.argv[1]));"
                "process.stdout.write(JSON.stringify(config));"
            ),
            str(ROOT / "eslint.config.js"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_python_quality_scope_and_checks_are_explicit():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["tool"]["mypy"] == {
        "python_version": "3.11",
        "files": MYPY_FILES,
        "check_untyped_defs": True,
        "warn_unused_ignores": True,
        "no_implicit_optional": True,
        "warn_unused_configs": True,
        "follow_imports": "silent",
    }


def test_python_quality_runners_are_exact_direct_dev_inputs():
    lines = _effective_requirement_lines(ROOT / "requirements" / "dev.in")

    assert {
        "mypy==2.3.0",
        "pip-audit==2.10.1",
        "pytest==9.1.1",
        "ruff==0.16.0",
        "types-regex==2026.7.19.20260720",
    } <= lines
    assert not any(
        re.match(r"(mypy|pip-audit|pytest|ruff)\s*[<>=!~]", line)
        and line
        not in {
            "mypy==2.3.0",
            "pip-audit==2.10.1",
            "pytest==9.1.1",
            "ruff==0.16.0",
            "types-regex==2026.7.19.20260720",
        }
        for line in lines
    )


def test_javascript_quality_toolchain_is_exactly_locked():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))

    assert package == {
        "name": "yt-final",
        "version": "1.0.0",
        "private": True,
        "scripts": {
            "lint": (
                "eslint Final/webui/app.js playwright.config.mjs "
                "scripts/run_browser_test_server.mjs "
                "scripts/run_browser_tests.mjs "
                "tests/browser/fail_closed_reporter.mjs "
                "tests/browser/release.spec.mjs"
            ),
            "test:browser": (
                "node scripts/run_browser_tests.mjs"
            ),
        },
        "devDependencies": {
            "@playwright/test": "1.61.0",
            "eslint": "10.8.0",
        },
    }
    assert lock["lockfileVersion"] == 3
    assert lock["name"] == "yt-final"
    assert lock["version"] == "1.0.0"
    assert lock["packages"][""]["name"] == "yt-final"
    assert lock["packages"][""]["version"] == "1.0.0"
    assert lock["packages"][""]["devDependencies"] == {
        "@playwright/test": "1.61.0",
        "eslint": "10.8.0",
    }
    for package_name in (
        "@playwright/test",
        "playwright",
        "playwright-core",
    ):
        entry = lock["packages"][f"node_modules/{package_name}"]
        assert entry["version"] == "1.61.0"
        assert entry["integrity"].startswith("sha512-")
        assert entry["resolved"].startswith("https://registry.npmjs.org/")
    assert lock["packages"]["node_modules/eslint"]["version"] == "10.8.0"
    assert lock["packages"]["node_modules/eslint"]["resolved"] == (
        "https://registry.npmjs.org/eslint/-/eslint-10.8.0.tgz"
    )
    assert lock["packages"]["node_modules/eslint"]["integrity"].startswith("sha512-")
    for path, package_entry in lock["packages"].items():
        if not path or package_entry.get("link"):
            continue
        assert package_entry.get("version"), path
        assert package_entry.get("integrity", "").startswith("sha512-"), path
        assert package_entry.get("resolved", "").startswith(
            "https://registry.npmjs.org/"
        ), path


def test_authenticated_browser_gate_is_isolated_and_capture_free():
    config = (ROOT / "playwright.config.mjs").read_text(encoding="utf-8")
    server = (ROOT / "scripts" / "run_browser_test_server.mjs").read_text(
        encoding="utf-8"
    )
    runner = (ROOT / "scripts" / "run_browser_tests.mjs").read_text(
        encoding="utf-8"
    )
    spec = (ROOT / "tests" / "browser" / "release.spec.mjs").read_text(
        encoding="utf-8"
    )
    reporter = (
        ROOT / "tests" / "browser" / "fail_closed_reporter.mjs"
    ).read_text(encoding="utf-8")

    for required in (
        "reuseExistingServer: false",
        'trace: "off"',
        'screenshot: "off"',
        'video: "off"',
        "YT_FINAL_BROWSER_STATE",
        "YT_FINAL_BROWSER_PASSWORD",
        "YT_FINAL_BROWSER_NAME",
        "fail_closed_reporter.mjs",
    ):
        assert required in config
    assert "39091" not in config
    for required in (
        "DEV_PYTHON",
        "YT_FINAL_HOME",
        "YT_FINAL_WEBUI_PASSWORD",
        "--no-browser",
        "--output-dir",
        "--queue-file",
        "windowsHide: true",
        "restart.request",
        "restart.ack",
        "restart.stopped",
        "PYTHONPATH: repoRoot",
        "cwd: instanceRoot",
        "rmSync(serverRoot",
    ):
        assert required in server
    for required in (
        "availableLoopbackPort",
        "RUNNER_TEMP",
        "YT_FINAL_BROWSER_PORT",
        "YT_FINAL_BROWSER_STATE",
        "randomBytes",
        "rmSync(stateRoot",
        "maxRetries: 10",
    ):
        assert required in runner
    for required in (
        "Wrong password",
        "yt_final_web_session_v2",
        "Lost contact with the app",
        "pageerror",
        "console",
        "#nav-workspace",
        "#nav-updates",
        "390",
        "#logout",
        "/api/session",
        "/api/history",
        "expires_at",
        "restart.request",
        "/api/updates/tools",
        "/api/jobs/rerun",
        "open-folder",
        "aria-live",
        "#view-${name}",
    ):
        assert required in spec
    assert "console.log" not in spec
    assert "test.skip" not in spec
    assert "test.fixme" not in spec
    assert "result.status === \"skipped\"" in reporter
    assert "this.discovered === 0" in reporter
    assert 'return { status: "failed" }' in reporter


def test_browser_reporter_rejects_expected_failures():
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the browser reporter"
    reporter_uri = (
        ROOT / "tests" / "browser" / "fail_closed_reporter.mjs"
    ).as_uri()
    script = f"""
import Reporter from {json.dumps(reporter_uri)};
const testCase = {{
  expectedStatus: "failed",
  titlePath: () => ["expected failure fixture"],
}};
const reporter = new Reporter();
reporter.onBegin({{}}, {{ allTests: () => [testCase] }});
reporter.onTestEnd(testCase, {{ status: "failed" }});
const result = reporter.onEnd({{ status: "passed" }});
process.stdout.write(JSON.stringify(result));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"status": "failed"}
    assert "expected-status browser tests" in result.stderr


def test_browser_runner_atomically_writes_controls_and_bounds_termination(tmp_path):
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the browser runner"
    runner_uri = (ROOT / "scripts" / "run_browser_tests.mjs").as_uri()
    destination = tmp_path / "restart.request"
    script = f"""
import {{
  stopChildBounded,
  writeAtomicJson,
}} from {json.dumps(runner_uri)};
import {{ readdir, readFile }} from "node:fs/promises";

const destination = {json.dumps(str(destination))};
await writeAtomicJson(destination, {{
  delay_ms: 3000,
  request_id: "quality-contract",
  session_ttl_seconds: 43200,
}});
const controls = JSON.parse(await readFile(destination, "utf8"));
const entries = await readdir({json.dumps(str(tmp_path))});

const signals = [];
const child = {{
  kill: (signal) => {{
    signals.push(signal);
    return true;
  }},
}};
const cancel = stopChildBounded(child, "SIGTERM", 10);
await new Promise((resolve) => setTimeout(resolve, 30));
cancel();

process.stdout.write(JSON.stringify({{ controls, entries, signals }}));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "controls": {
            "delay_ms": 3000,
            "request_id": "quality-contract",
            "session_ttl_seconds": 43200,
        },
        "entries": ["restart.request"],
        "signals": ["SIGTERM", "SIGKILL"],
    }


def test_javascript_install_tree_is_excluded_from_release_source_status():
    ignored = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "node_modules/" in ignored


def test_eslint_flat_config_is_browser_and_harness_fail_closed():
    config = _load_eslint_flat_config()

    assert config == [
        {
            "files": ["Final/webui/app.js"],
            "languageOptions": {
                "ecmaVersion": 2022,
                "sourceType": "script",
                "globals": ESLINT_GLOBALS,
            },
            "linterOptions": {
                "noInlineConfig": True,
                "reportUnusedDisableDirectives": "error",
            },
            "rules": ESLINT_RULES,
        },
        {
            "files": [
                "playwright.config.mjs",
                "scripts/run_browser_test_server.mjs",
                "scripts/run_browser_tests.mjs",
                "tests/browser/fail_closed_reporter.mjs",
                "tests/browser/release.spec.mjs",
            ],
            "languageOptions": {
                "ecmaVersion": 2022,
                "sourceType": "module",
                "globals": ESLINT_NODE_GLOBALS,
            },
            "linterOptions": {
                "noInlineConfig": True,
                "reportUnusedDisableDirectives": "error",
            },
            "rules": ESLINT_RULES,
        },
    ]


def test_quality_targets_have_no_blanket_suppressions():
    targets = [
        *(ROOT / path for path in MYPY_FILES[:-1]),
        *(ROOT / "scripts").glob("*.py"),
        ROOT / "Final" / "webui" / "app.js",
        ROOT / "playwright.config.mjs",
        ROOT / "scripts" / "run_browser_test_server.mjs",
        ROOT / "scripts" / "run_browser_tests.mjs",
        ROOT / "tests" / "browser" / "fail_closed_reporter.mjs",
        ROOT / "tests" / "browser" / "release.spec.mjs",
    ]

    for path in targets:
        body = path.read_text(encoding="utf-8")
        assert not re.search(r"#\s*type:\s*ignore(?!\[)", body), path
        assert "eslint-disable" not in body, path


# _windows_private_descriptor() opens with `from ctypes import wintypes`, which raises before the
# faked WinDLL below is ever consulted: ctypes.wintypes does not exist on a POSIX build of CPython.
# The same platform gate the sibling test below carries applies here.
@pytest.mark.skipif(
    os.name != "nt",
    reason="ctypes.wintypes/WinDLL are Windows-only APIs",
)
def test_windows_security_descriptor_rejects_null_native_pointer(monkeypatch):
    from Final import secureio
    import ctypes

    class SuccessfulNullConversion:
        def __call__(self, *_args):
            return True

    class FakeAdvapi:
        ConvertStringSecurityDescriptorToSecurityDescriptorW = (
            SuccessfulNullConversion()
        )

    monkeypatch.setattr(
        secureio,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-test",
    )
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: FakeAdvapi(),
        raising=False,
    )

    with pytest.raises(OSError, match="null security descriptor"):
        secureio._windows_private_descriptor()


# ctypes.get_last_error() (and the use_last_error WinDLL machinery it reports for) exists only on
# Windows, so even the monkeypatch.setattr below raises AttributeError on macOS/Linux. The
# assertion guards a Windows-only code path in secureio, so gate it on the platform capability.
@pytest.mark.skipif(
    os.name != "nt",
    reason="ctypes.get_last_error/WinDLL are Windows-only APIs",
)
def test_windows_sid_conversion_rejects_success_without_a_value(monkeypatch):
    from Final import secureio
    import ctypes

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeAdvapi:
        def __init__(self):
            self.OpenProcessToken = FakeFunction(lambda *_args: True)

            def get_token_information(
                _token,
                _kind,
                buffer,
                _size,
                needed,
            ):
                needed._obj.value = 32
                return buffer is not None

            self.GetTokenInformation = FakeFunction(get_token_information)
            self.ConvertSidToStringSidW = FakeFunction(lambda *_args: True)

    class FakeKernel:
        def __init__(self):
            self.GetCurrentProcess = FakeFunction(lambda: 1)
            self.LocalFree = FakeFunction(lambda *_args: 0)
            self.CloseHandle = FakeFunction(lambda *_args: True)

    advapi = FakeAdvapi()
    kernel = FakeKernel()
    monkeypatch.setattr(secureio, "_WINDOWS_USER_SID", None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 122)
    monkeypatch.setattr(ctypes, "wstring_at", lambda _pointer: None)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **_kwargs: advapi if name == "advapi32" else kernel,
        raising=False,
    )

    with pytest.raises(OSError, match="SID conversion returned no value"):
        secureio._windows_current_user_sid()
