import base64
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON = (
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
)
SETUP_NODE = "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"
UPLOAD_ARTIFACT = (
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
)
DOWNLOAD_ARTIFACT = (
    "actions/download-artifact@70fc10c6e5e1ce46ad2ea6f2b72d43f7d47b13c3"
)


def _workflow_lines(path=WORKFLOW):
    assert path.is_file(), f"the required workflow is missing: {path.name}"
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert b"\t" not in raw
    text = raw.decode("utf-8")
    assert text.endswith("\n")
    assert all(line == line.rstrip() for line in text.splitlines())
    return text, text.splitlines()


def _mapping_block(lines, key, indent):
    marker = re.compile(rf"^{' ' * indent}{re.escape(key)}:\s*$")
    matches = [index for index, line in enumerate(lines) if marker.fullmatch(line)]
    assert len(matches) == 1, f"expected one {key!r} mapping at indent {indent}"
    start = matches[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        leading = len(line) - len(line.lstrip(" "))
        if leading <= indent:
            end = index
            break
    return lines[start:end]


def _flat_mapping(lines, indent):
    result = {}
    pattern = re.compile(
        rf"^{' ' * indent}([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$"
    )
    for line in lines:
        match = pattern.fullmatch(line)
        if match:
            key, value = match.groups()
            assert key not in result, f"duplicate mapping key: {key}"
            result[key] = "" if value is None else value
    return result


def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def _flat_sequence_mappings(lines, indent):
    item_pattern = re.compile(
        rf"^{' ' * indent}-\s+([A-Za-z_][A-Za-z0-9_-]*):\s*(.+)$"
    )
    child_pattern = re.compile(
        rf"^{' ' * (indent + 2)}([A-Za-z_][A-Za-z0-9_-]*):\s*(.+)$"
    )
    items = []
    current = None
    for line in lines:
        item = item_pattern.fullmatch(line)
        if item:
            current = {item.group(1): _scalar(item.group(2))}
            items.append(current)
            continue
        child = child_pattern.fullmatch(line)
        if child:
            assert current is not None
            key, value = child.groups()
            assert key not in current
            current[key] = _scalar(value)
            continue
        assert not line.strip(), f"unsupported matrix include syntax: {line!r}"
    return items


def _step_blocks(lines):
    steps = _mapping_block(lines, "steps", 4)
    for line in steps:
        if re.match(r" {6}-\s+", line):
            assert re.fullmatch(r" {6}- name:\s*.+", line), (
                "every CI step must be explicitly named"
            )
    starts = [
        index
        for index, line in enumerate(steps)
        if re.fullmatch(r" {6}- name:\s*.+", line)
    ]
    assert starts, "CI job must have named steps"
    blocks = []
    for offset, start in enumerate(starts):
        end = starts[offset + 1] if offset + 1 < len(starts) else len(steps)
        blocks.append(steps[start:end])
    return blocks


def _step_name(step):
    match = re.fullmatch(r" {6}- name:\s*(.+)", step[0])
    assert match
    return _scalar(match.group(1))


def _step_field(step, key):
    pattern = re.compile(rf"^{' ' * 8}{re.escape(key)}:\s*(.+)$")
    values = [match.group(1) for line in step if (match := pattern.fullmatch(line))]
    assert len(values) == 1, f"{_step_name(step)!r} must define {key!r} once"
    return _scalar(values[0])


def _optional_step_field(step, key):
    pattern = re.compile(rf"^{' ' * 8}{re.escape(key)}:\s*(.+)$")
    values = [match.group(1) for line in step if (match := pattern.fullmatch(line))]
    assert len(values) <= 1
    return None if not values else _scalar(values[0])


def _step_nested_mapping(step, key):
    marker = f"{' ' * 8}{key}:"
    matches = [index for index, line in enumerate(step) if line == marker]
    assert len(matches) == 1, f"{_step_name(step)!r} must define {key!r} once"
    result = {}
    pattern = re.compile(r" {10}([A-Za-z_][A-Za-z0-9_-]*):\s*(.+)")
    for line in step[matches[0] + 1 :]:
        match = pattern.fullmatch(line)
        if match:
            name, value = match.groups()
            assert name not in result, f"duplicate {key!r} input: {name}"
            result[name] = _scalar(value)
    return result


def _step_run(step):
    markers = [index for index, line in enumerate(step) if line == "        run: |"]
    assert len(markers) == 1, f"{_step_name(step)!r} must have one block run script"
    script = []
    for line in step[markers[0] + 1 :]:
        assert not line.strip() or line.startswith("          ")
        script.append(line[10:] if line else "")
    return "\n".join(script).strip() + "\n"


def _steps_by_name(lines):
    steps = {_step_name(step): step for step in _step_blocks(lines)}
    assert len(steps) == len(_step_blocks(lines)), "step names must be unique"
    return steps


def _jobs_by_id(lines):
    jobs = _mapping_block(lines, "jobs", 0)
    pattern = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_-]*):\s*$")
    job_ids = [match.group(1) for line in jobs if (match := pattern.fullmatch(line))]
    assert job_ids, "workflow must define at least one job"
    assert len(job_ids) == len(set(job_ids)), "job identifiers must be unique"
    return {job_id: _mapping_block(jobs, job_id, 2) for job_id in job_ids}


def test_all_workflow_powershell_blocks_parse_without_errors():
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    # Hard-fail where the gate must hold — Windows, and CI on any OS, where the hosted windows and
    # macos runners both ship pwsh. Skip on a non-Windows dev box: stock macOS has no PowerShell, and
    # CONTRIBUTING.md advertises a plain `python -m pytest` without listing it as a prerequisite, so
    # a hard assert turns a fresh macOS clone red for a missing tool it was never told to install.
    if powershell is None and os.name != "nt" and not os.environ.get("CI"):
        pytest.skip("PowerShell not installed on this dev host; the CI gate still enforces this")
    assert powershell is not None, "PowerShell is required to validate workflows"

    scripts = []
    _text, ci_lines = _workflow_lines()
    scripts.extend(
        (f"ci:{_step_name(step)}", _step_run(step))
        for step in _step_blocks(ci_lines)
        if _optional_step_field(step, "uses") is None
    )
    _text, release_lines = _workflow_lines(RELEASE_WORKFLOW)
    release_jobs = _jobs_by_id(release_lines)
    for job_id in ("resolve", "verify", "draft"):
        scripts.extend(
            (f"release:{_step_name(step)}", _step_run(step))
            for step in _step_blocks(release_jobs[job_id])
            if _optional_step_field(step, "uses") is None
        )

    for label, script in scripts:
        rendered = re.sub(
            r"\$\{\{[^{}\r\n]+\}\}",
            "GITHUB_EXPRESSION",
            script,
        )
        encoded = base64.b64encode(rendered.encode("utf-8")).decode("ascii")
        probe = (
            "$source=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{encoded}'));"
            "$tokens=$null;$errors=$null;"
            "[Management.Automation.Language.Parser]::ParseInput("
            "$source,[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count){"
            "$errors|ForEach-Object{$_.ToString()}|Write-Error;exit 1}"
        )
        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                probe,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"{label} has invalid PowerShell syntax:\n"
            f"{result.stdout}{result.stderr}"
        )


def test_ci_has_exact_read_only_triggers_and_powershell_defaults():
    _text, lines = _workflow_lines()
    assert set(_flat_mapping(lines, 0)) == {
        "name",
        "on",
        "permissions",
        "defaults",
        "jobs",
    }
    triggers = _flat_mapping(_mapping_block(lines, "on", 0), 2)
    assert triggers == {
        "push": "",
        "pull_request": "",
        "workflow_call": "",
    }
    workflow_call = _mapping_block(
        _mapping_block(lines, "on", 0),
        "workflow_call",
        2,
    )
    assert set(_flat_mapping(workflow_call, 4)) == {"inputs"}
    inputs = _mapping_block(workflow_call, "inputs", 4)
    assert set(_flat_mapping(inputs, 6)) == {"source_sha"}
    source_sha = _flat_mapping(_mapping_block(inputs, "source_sha", 6), 8)
    assert _scalar(source_sha["required"]) is True
    assert _scalar(source_sha["type"]) == "string"
    assert set(source_sha) == {"description", "required", "type"}
    permissions = _flat_mapping(_mapping_block(lines, "permissions", 0), 2)
    assert permissions == {"contents": "read"}
    defaults = _mapping_block(lines, "defaults", 0)
    run_defaults = _flat_mapping(_mapping_block(defaults, "run", 2), 4)
    assert run_defaults == {"shell": "pwsh"}
    assert set(_flat_mapping(_mapping_block(lines, "jobs", 0), 2)) == {"ci"}


def test_ci_matrix_is_the_exact_six_supported_native_cells():
    _text, lines = _workflow_lines()
    job = _mapping_block(_mapping_block(lines, "jobs", 0), "ci", 2)
    job_mapping = _flat_mapping(job, 4)
    assert set(job_mapping) == {
        "name",
        "runs-on",
        "timeout-minutes",
        "strategy",
        "steps",
    }
    assert _scalar(job_mapping["runs-on"]) == "${{ matrix.runner }}"
    strategy = _mapping_block(job, "strategy", 4)
    strategy_mapping = _flat_mapping(strategy, 6)
    assert set(strategy_mapping) == {"fail-fast", "matrix"}
    assert _scalar(strategy_mapping["fail-fast"]) is False
    matrix = _mapping_block(strategy, "matrix", 6)
    assert set(_flat_mapping(matrix, 8)) == {"include"}
    include = _mapping_block(matrix, "include", 8)
    assert _flat_sequence_mappings(include, 10) == [
        {
            "runner": "windows-2025",
            "architecture": "x64",
            "python": "3.11",
            "native_target": "windows-x86_64-py311",
            "upload": True,
            "browsers": "chromium",
        },
        {
            "runner": "windows-2025",
            "architecture": "x64",
            "python": "3.14",
            "native_target": "windows-x86_64-py314",
            "upload": False,
            "browsers": "",
        },
        {
            "runner": "macos-15-intel",
            "architecture": "x64",
            "python": "3.11",
            "native_target": "macos-x86_64-py311",
            "upload": False,
            "browsers": "",
        },
        {
            "runner": "macos-15-intel",
            "architecture": "x64",
            "python": "3.14",
            "native_target": "macos-x86_64-py314",
            "upload": False,
            "browsers": "",
        },
        {
            "runner": "macos-15",
            "architecture": "arm64",
            "python": "3.11",
            "native_target": "macos-arm64-py311",
            "upload": False,
            "browsers": "chromium webkit",
        },
        {
            "runner": "macos-15",
            "architecture": "arm64",
            "python": "3.14",
            "native_target": "macos-arm64-py314",
            "upload": False,
            "browsers": "",
        },
    ]


def test_ci_uses_only_reviewed_sha_pinned_actions_and_one_guarded_upload():
    _text, lines = _workflow_lines()
    steps = _step_blocks(lines)
    action_steps = [step for step in steps if _optional_step_field(step, "uses")]
    assert [_step_field(step, "uses") for step in action_steps] == [
        CHECKOUT,
        SETUP_PYTHON,
        SETUP_NODE,
        UPLOAD_ARTIFACT,
    ]
    for step in action_steps:
        action = _step_field(step, "uses")
        assert not action.startswith("./")
        assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action)

    checkout, setup_python, setup_node, upload = action_steps
    assert _step_nested_mapping(checkout, "with") == {
        "clean": True,
        "fetch-depth": "0",
        "persist-credentials": False,
        "ref": "${{ inputs.source_sha || github.sha }}",
    }
    assert _step_nested_mapping(setup_python, "with") == {
        "architecture": "${{ matrix.architecture }}",
        "python-version": "${{ matrix.python }}",
    }
    assert _step_field(setup_node, "if") == (
        "${{ matrix.upload || matrix.browsers != '' }}"
    )
    assert _step_nested_mapping(setup_node, "with") == {
        "node-version": "24.18.0",
        "package-manager-cache": False,
    }
    assert _step_field(upload, "if") == "${{ matrix.upload }}"
    assert _step_nested_mapping(upload, "with") == {
        "archive": True,
        "if-no-files-found": "error",
        "include-hidden-files": False,
        "name": "yt-final-1.0.0-${{ inputs.source_sha || github.sha }}",
        "path": "${{ runner.temp }}/release-artifacts/**",
    }


def test_ci_bootstraps_external_locked_environments_and_native_gate():
    _text, lines = _workflow_lines()
    steps = _steps_by_name(lines)
    state = _step_run(steps["Establish external release state"])
    assert "${{ runner.temp }}" in state
    assert "$env:GITHUB_ENV" in state
    for assignment in (
        '$releaseRoot = Join-Path $runnerTemp "yt-final-ci"',
        '$stateRoot = Join-Path $releaseRoot "state"',
        '$ruffCache = Join-Path $stateRoot "ruff-cache"',
    ):
        assert assignment in state
    for name in (
        "BUILD_PYTHON",
        "DEV_PYTHON",
        "RELEASE_PYTHON",
        "PYTHONPYCACHEPREFIX",
        "PIP_CACHE_DIR",
        "RUFF_CACHE_DIR",
    ):
        assert f'Export-CiEnvironment "{name}"' in state
    for name in (
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    ):
        assert re.search(rf"(?m)^\s*{name}\s*=", state)
    assert "Export-CiEnvironment $entry.Key $entry.Value" in state

    install = _step_run(steps["Create isolated lock environments"])
    assert install.count("--without-pip") == 3
    assert install.count("--copies") == 3
    assert install.count(" install `") == 3
    assert install.count("--require-hashes") == 3
    assert install.count("--only-binary=:all:") == 3
    assert install.count("--python") >= 2
    for lock in (
        "requirements/build.lock",
        "requirements/dev.lock",
        "requirements/release.lock",
    ):
        assert lock in install
    assert install.count(" check") >= 3
    assert ".venv" not in install

    native = _step_run(steps["Verify the native lock closure"])
    for setting in (
        "YT_FINAL_VERIFY_NATIVE_LOCK",
        "YT_FINAL_LOCK_PYTHON",
        "YT_FINAL_NATIVE_TARGET",
    ):
        assert setting in native
    assert "${{ matrix.native_target }}" in native
    assert (
        "Final/test_release_packaging.py::"
        "test_dependency_locks_install_natively_with_hashes"
    ) in native
    assert "-p no:cacheprovider" in native
    assert "--basetemp" in native


def test_ci_runs_quality_root_tests_and_safe_source_cli_smokes():
    _text, lines = _workflow_lines()
    steps = _steps_by_name(lines)
    quality = _step_run(steps["Run static quality gates"])
    assert "-m ruff check Final scripts" in quality
    assert "-m compileall -q Final scripts" in quality
    assert "PYTHONPYCACHEPREFIX" in _step_run(
        steps["Establish external release state"]
    )
    assert "Push-Location Final" in quality
    assert "_syntax_check.py" in quality
    assert "Pop-Location" in quality

    focused = _step_run(steps["Run focused release tests"])
    for test_file in (
        "Final/test_release_packaging.py",
        "Final/test_release_boundary.py",
        "Final/test_release_artifacts.py",
        "Final/test_release_workflows.py",
    ):
        assert test_file in focused
    assert "-m pytest -q" in focused
    assert "-p no:cacheprovider" in focused
    assert "--basetemp" in focused
    assert "Push-Location" not in focused

    complete = _step_run(steps["Run the complete root test suite"])
    assert "-m pytest -q -p no:cacheprovider" in complete
    assert "--basetemp" in complete
    assert "Final/" not in complete
    assert "Push-Location" not in complete
    pytest_arguments = complete.split("-m pytest", 1)[1]
    for selector in (
        " -k ",
        " -m ",
        "--deselect",
        "--ignore",
        "--ignore-glob",
    ):
        assert selector not in pytest_arguments

    smokes = _step_run(steps["Smoke-test source entry points"])
    for command in (
        "Final/yt_final.py --help",
        "Final/yt_final.py --version",
        "Final/yt_final.py list-profiles",
        "-m Final --help",
        "-m Final --version",
        "-m Final list-profiles",
    ):
        assert command in smokes
    assert "serve" not in smokes.casefold()
    assert ".bat" not in smokes.casefold()
    assert ".command" not in smokes.casefold()


def test_ci_runs_locked_type_audit_frontend_workflow_and_secret_gates():
    _text, lines = _workflow_lines()
    steps = _steps_by_name(lines)
    quality_step = steps["Run locked repository quality gates"]
    assert _step_field(quality_step, "if") == "${{ matrix.upload }}"
    quality = _step_run(quality_step)
    for contract in (
        "-m mypy",
        "Final/websessions.py",
        "Final/secureio.py",
        "Final/redaction.py",
        "Final/state_validation.py",
        "Final/watch_filters.py",
        "Final/url_identity.py",
        "Final/notifications.py",
        "Final/installer_lock.py",
        "Final/tool_manifest.py",
        "scripts",
        "-m pip_audit",
        "--require-hashes",
        "requirements/release.lock",
        "npm ci --ignore-scripts",
        "npm run lint",
        "actionlint_1.7.12_windows_amd64.zip",
        "actionlint_1.7.12_checksums.txt",
        "gitleaks_8.30.1_windows_x64.zip",
        "gitleaks_8.30.1_checksums.txt",
        "gitleaks_8.30.1_config.toml",
        "raw.githubusercontent.com/gitleaks/gitleaks/v8.30.1/config/gitleaks.toml",
        "actionlint.exe",
        "gitleaks.exe",
        "--source $env:GITHUB_WORKSPACE",
        "--ignore-gitleaks-allow",
        ".github/actionlint.yml",
        ".github/actionlint.yaml",
        ".gitleaks.toml",
        ".gitleaksignore",
        "Push-Location $toolRoot",
    ):
        assert contract in quality
    for digest in (
        "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9",
        "433028cf0ba3c42163ea1a668dedce30fcdbe84fe912b1a5e288c006eab8a4f5",
        "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e",
        "061476c21adaf5441516f96f185c1a4706a83cd6329b9b38762271b3d4a52fae",
        "e163e53b9e7e8a8511e77271e2b323ed057759542a6d988258afe3a1fa329caf",
    ):
        assert digest in quality
    assert quality.count("Assert-Sha256") >= 4
    assert "playwright install" not in quality
    assert "npm run test:browser" not in quality
    assert ".github/workflows/*.yml" not in quality
    assert "Get-ChildItem .github/workflows -File" in quality
    assert '.Extension -in @(".yml", ".yaml")' in quality
    assert "-Filter *.yml" not in quality

    browser_step = steps["Run authenticated cross-browser gate"]
    assert _step_field(browser_step, "if") == "${{ matrix.browsers != '' }}"
    browser = _step_run(browser_step)
    for contract in (
        "${{ matrix.browsers }}",
        "PLAYWRIGHT_BROWSERS_PATH",
        "npm ci --ignore-scripts",
        '@("chromium", "webkit")',
        "YT_FINAL_BROWSER_NAME",
        "npm exec -- playwright install $browserName",
        "npm run test:browser",
    ):
        assert contract in browser
    assert browser.index("npm ci --ignore-scripts") < browser.index(
        "npm exec -- playwright install $browserName"
    )
    assert browser.index("npm exec -- playwright install $browserName") < (
        browser.index("npm run test:browser")
    )


def test_ci_binds_full_sha_epoch_task5_and_clean_artifact_build():
    _text, lines = _workflow_lines()
    steps = _steps_by_name(lines)
    identity_step = steps["Bind the immutable source identity"]
    assert _step_nested_mapping(identity_step, "env") == {
        "REQUESTED_SOURCE_SHA": "${{ inputs.source_sha || github.sha }}"
    }
    identity = _step_run(identity_step)
    for contract in (
        'git rev-parse --verify "HEAD^{commit}"',
        "$env:REQUESTED_SOURCE_SHA",
        "^[0-9a-f]{40}$",
        "git show --no-patch --format=%ct",
        "SOURCE_DATE_EPOCH=",
        "git status --porcelain=v1 --untracked-files=all",
    ):
        assert contract in identity
    assert "${{" not in identity

    task5 = _step_run(steps["Run the Task 5 distribution gate"])
    for setting in (
        "YT_FINAL_VERIFY_DISTRIBUTIONS",
        "YT_FINAL_BUILD_PYTHON",
        "YT_FINAL_RELEASE_PYTHON",
    ):
        assert setting in task5
    assert (
        "Final/test_release_packaging.py::"
        "test_phase_b_clean_distribution_build_and_external_install"
    ) in task5
    assert "-p no:cacheprovider" in task5
    assert "--basetemp" in task5

    build = _step_run(steps["Build release artifacts from a clean Git archive"])
    for contract in (
        "git archive --format=tar",
        "$env:FULL_SHA",
        "$env:CLEAN_SOURCE",
        "$env:ARTIFACT_DIR",
        "-m build --no-isolation --wheel --sdist",
        "scripts/build_portable.py",
        "scripts/write_checksums.py",
        "-m twine check",
    ):
        assert contract in build
    assert "dist/" not in build
    assert "build/" not in build
    build_lines = build.splitlines()
    checksum_path_line = next(
        index
        for index, line in enumerate(build_lines)
        if "scripts/write_checksums.py" in line
    )
    assert (
        build_lines[checksum_path_line - 1].strip()
        == "& $env:BUILD_PYTHON -B `"
    )
    assert "-I -B" not in build_lines[checksum_path_line - 1]

    verifier = _step_run(steps["Run the release artifact verifier"])
    assert "scripts/verify_release.py" in verifier
    assert "$env:ARTIFACT_DIR" in verifier
    assert "--expected-version 1.0.0" in verifier
    assert "--trusted-source-root $env:CLEAN_SOURCE" in verifier
    assert "--expected-source-epoch $env:SOURCE_DATE_EPOCH" in verifier


def test_ci_installs_and_smokes_artifacts_outside_checkout_then_allowlists():
    _text, lines = _workflow_lines()
    steps = _steps_by_name(lines)
    runtime = _step_run(steps["Verify the wheel outside the checkout"])
    for contract in (
        "--without-pip",
        "--python",
        "--require-hashes",
        "--only-binary=:all:",
        "Final/approved-python.lock",
        "--no-index",
        "--no-deps",
        " check",
        "yt-final",
        "--help",
        "--version",
        "list-profiles",
        "-m Final --help",
    ):
        assert contract in runtime
    assert "$env:RUNNER_TEMP" in runtime

    portable = _step_run(steps["Extract and smoke-test the portable archive"])
    assert "yt_final-1.0.0-portable.zip" in portable
    assert "Expand-Archive" in portable
    assert portable.count('Join-Path $portableSource "Final/yt_final.py"') == 3
    for argument in ("--help", "--version", "list-profiles"):
        assert argument in portable
    assert "$env:RUNNER_TEMP" in portable
    assert "serve" not in portable.casefold()

    allowlist = _step_run(steps["Enforce the exact upload allowlist"])
    expected_names = [
        "SHA256SUMS",
        "yt_final-1.0.0-portable.zip",
        "yt_final-1.0.0-py3-none-any.whl",
        "yt_final-1.0.0.tar.gz",
    ]
    expected_block = re.search(
        r'(?ms)^\$expected = @\(\n(?P<body>(?:  "[^"\r\n]+"\n)+)\)$',
        allowlist,
    )
    assert expected_block, "artifact allowlist must be one literal string array"
    assert re.findall(
        r'^  "([^"\r\n]+)"$',
        expected_block.group("body"),
        flags=re.MULTILINE,
    ) == expected_names
    for name in expected_names:
        assert allowlist.count(name) == 1
    assert "Compare-Object" in allowlist
    assert "Get-ChildItem" in allowlist
    assert "$env:ARTIFACT_DIR" in allowlist


def test_ci_forbids_privileged_or_release_mutating_behavior():
    text, lines = _workflow_lines()
    lowered = text.casefold()
    for forbidden in (
        "continue-on-error",
        "pull_request_target",
        "workflow_dispatch",
        "workflow_run",
        "repository_dispatch",
        "schedule:",
        "contents: write",
        "id-token: write",
        "${{ secrets.",
        "actions/attest",
        "gh release",
        "twine upload",
        "pypi",
    ):
        assert forbidden not in lowered

    for step in _step_blocks(lines):
        action = _optional_step_field(step, "uses")
        if action is not None:
            assert action in {
                CHECKOUT,
                SETUP_PYTHON,
                SETUP_NODE,
                UPLOAD_ARTIFACT,
            }
            continue
        script = _step_run(step).casefold()
        for forbidden in (
            " publish",
            " attest",
            " serve",
            ".bat",
            ".command",
            "start yt_final",
        ):
            assert forbidden not in script


def test_release_workflow_is_manual_exact_tag_input_and_read_only_by_default():
    text, lines = _workflow_lines(RELEASE_WORKFLOW)
    assert set(_flat_mapping(lines, 0)) == {
        "name",
        "on",
        "permissions",
        "concurrency",
        "defaults",
        "jobs",
    }
    triggers = _flat_mapping(_mapping_block(lines, "on", 0), 2)
    assert triggers == {"workflow_dispatch": ""}
    dispatch = _mapping_block(_mapping_block(lines, "on", 0), "workflow_dispatch", 2)
    assert set(_flat_mapping(dispatch, 4)) == {"inputs"}
    inputs = _mapping_block(dispatch, "inputs", 4)
    assert set(_flat_mapping(inputs, 6)) == {
        "confirm_immutable_releases",
        "confirm_no_policy_bypass",
        "tag",
    }
    tag = _flat_mapping(_mapping_block(inputs, "tag", 6), 8)
    assert set(tag) == {"description", "required", "type"}
    assert _scalar(tag["required"]) is True
    assert _scalar(tag["type"]) == "string"
    for input_name in (
        "confirm_immutable_releases",
        "confirm_no_policy_bypass",
    ):
        confirmation = _flat_mapping(
            _mapping_block(inputs, input_name, 6),
            8,
        )
        assert set(confirmation) == {
            "default",
            "description",
            "required",
            "type",
        }
        assert _scalar(confirmation["default"]) is False
        assert _scalar(confirmation["required"]) is True
        assert _scalar(confirmation["type"]) == "boolean"
    assert _flat_mapping(_mapping_block(lines, "permissions", 0), 2) == {
        "actions": "read",
        "contents": "read",
    }
    assert _flat_mapping(_mapping_block(lines, "concurrency", 0), 2) == {
        "cancel-in-progress": "false",
        "group": (
            "protected-draft-${{ github.repository }}-${{ inputs.tag }}"
        ),
    }
    run_defaults = _flat_mapping(
        _mapping_block(_mapping_block(lines, "defaults", 0), "run", 2),
        4,
    )
    assert run_defaults == {"shell": "pwsh"}
    assert set(_jobs_by_id(lines)) == {"resolve", "ci", "verify", "draft"}
    assert text.count("contents: write") == 1


def test_release_resolves_exact_v1_tag_commit_and_default_branch_fail_closed():
    _text, lines = _workflow_lines(RELEASE_WORKFLOW)
    jobs = _jobs_by_id(lines)
    resolve = jobs["resolve"]
    resolve_mapping = _flat_mapping(resolve, 4)
    assert set(resolve_mapping) == {
        "name",
        "runs-on",
        "timeout-minutes",
        "outputs",
        "steps",
    }
    outputs = _flat_mapping(_mapping_block(resolve, "outputs", 4), 6)
    assert outputs == {
        "default_branch": "${{ steps.resolve.outputs.default_branch }}",
        "source_sha": "${{ steps.resolve.outputs.source_sha }}",
        "tag": "${{ steps.resolve.outputs.tag }}",
    }
    steps = _steps_by_name(resolve)
    checkout = steps["Check out the dispatch default branch and all tags"]
    assert _step_field(checkout, "uses") == CHECKOUT
    assert _step_nested_mapping(checkout, "with") == {
        "clean": True,
        "fetch-depth": "0",
        "fetch-tags": True,
        "persist-credentials": False,
        "ref": "${{ github.event.repository.default_branch }}",
    }
    resolve_step = steps["Resolve the exact release identity"]
    assert _step_nested_mapping(resolve_step, "env") == {
        "CONFIRM_IMMUTABLE_RELEASES": (
            "${{ inputs.confirm_immutable_releases }}"
        ),
        "CONFIRM_NO_POLICY_BYPASS": (
            "${{ inputs.confirm_no_policy_bypass }}"
        ),
        "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
        "DISPATCH_REF": "${{ github.ref }}",
        "DISPATCH_TAG": "${{ inputs.tag }}",
        "GH_TOKEN": "${{ github.token }}",
        "REPOSITORY": "${{ github.repository }}",
    }
    script = _step_run(resolve_step)
    for contract in (
        "$env:DISPATCH_TAG",
        '"v1.0.0"',
        "$env:CONFIRM_IMMUTABLE_RELEASES",
        "$env:CONFIRM_NO_POLICY_BYPASS",
        '"true"',
        "$env:DEFAULT_BRANCH",
        "$env:DISPATCH_REF",
        '"refs/heads/$defaultBranch"',
        '"refs/tags/$tag^{commit}"',
        '"refs/remotes/origin/$defaultBranch^{commit}"',
        "git merge-base --is-ancestor",
        "git show",
        "Final/_version.py",
        'if ($version -cne "1.0.0")',
        'if ($tag -cne "v$version")',
        "^[0-9a-f]{40}$",
        "$env:GITHUB_OUTPUT",
        "source_sha=$sourceSha",
        "default_branch=$defaultBranch",
        "tag=$tag",
        "environments/release",
        "required_reviewers",
        "prevent_self_review",
        "rulesets?includes_parents=true",
        '"update"',
        '"deletion"',
        "refs/tags/$tag",
    ):
        assert contract in script
    assert "immutable-releases" not in script
    assert "bypass_actors" not in script
    assert "no-bypass tag ruleset" not in script
    assert "${{" not in script


def test_release_separates_read_only_verification_from_draft_write_job():
    text, lines = _workflow_lines(RELEASE_WORKFLOW)
    jobs = _jobs_by_id(lines)
    ci = jobs["ci"]
    ci_mapping = _flat_mapping(ci, 4)
    assert set(ci_mapping) == {"name", "needs", "permissions", "uses", "with"}
    assert _scalar(ci_mapping["needs"]) == "resolve"
    assert _scalar(ci_mapping["uses"]) == "./.github/workflows/ci.yml"
    assert _flat_mapping(_mapping_block(ci, "permissions", 4), 6) == {
        "contents": "read"
    }
    assert _flat_mapping(_mapping_block(ci, "with", 4), 6) == {
        "source_sha": "${{ needs.resolve.outputs.source_sha }}"
    }

    verify = jobs["verify"]
    verify_mapping = _flat_mapping(verify, 4)
    assert set(verify_mapping) == {
        "name",
        "needs",
        "runs-on",
        "timeout-minutes",
        "permissions",
        "steps",
    }
    assert _scalar(verify_mapping["needs"]) == "[resolve, ci]"
    assert _flat_mapping(_mapping_block(verify, "permissions", 4), 6) == {
        "contents": "read"
    }

    draft = jobs["draft"]
    draft_mapping = _flat_mapping(draft, 4)
    assert set(draft_mapping) == {
        "name",
        "needs",
        "runs-on",
        "timeout-minutes",
        "environment",
        "permissions",
        "steps",
    }
    assert _scalar(draft_mapping["needs"]) == "[resolve, verify]"
    assert _scalar(draft_mapping["environment"]) == "release"
    assert _flat_mapping(_mapping_block(draft, "permissions", 4), 6) == {
        "contents": "write"
    }
    assert text.count("environment: release") == 1
    assert "environment:" not in "\n".join(jobs["resolve"])
    assert "environment:" not in "\n".join(jobs["ci"])
    assert "environment:" not in "\n".join(jobs["verify"])


def test_release_write_job_never_checks_out_or_executes_tagged_source():
    _text, lines = _workflow_lines(RELEASE_WORKFLOW)
    jobs = _jobs_by_id(lines)
    verify_steps = _steps_by_name(jobs["verify"])
    assert _step_field(
        verify_steps["Check out the resolved release commit"],
        "uses",
    ) == CHECKOUT
    assert "scripts/verify_release.py" in _step_run(
        verify_steps["Revalidate tag binding and downloaded artifacts"]
    )

    draft_steps = _step_blocks(jobs["draft"])
    action_steps = [
        step for step in draft_steps if _optional_step_field(step, "uses")
    ]
    assert [_step_field(step, "uses") for step in action_steps] == [
        DOWNLOAD_ARTIFACT
    ]
    for step in draft_steps:
        assert _optional_step_field(step, "working-directory") is None
        action = _optional_step_field(step, "uses")
        if action is not None:
            assert action == DOWNLOAD_ARTIFACT
            continue
        script = _step_run(step).casefold()
        for forbidden in (
            "$env:github_workspace",
            "scripts/verify_release.py",
            "expand-archive",
            "invoke-expression",
            "python ",
            "git ",
            "npm ",
            "node ",
        ):
            assert forbidden not in script


def test_release_downloads_verifies_and_drafts_only_explicit_assets():
    text, lines = _workflow_lines(RELEASE_WORKFLOW)
    jobs = _jobs_by_id(lines)
    resolve_steps = _step_blocks(jobs["resolve"])
    verify_steps = _step_blocks(jobs["verify"])
    draft_steps = _step_blocks(jobs["draft"])
    all_action_steps = [
        step
        for step in [*resolve_steps, *verify_steps, *draft_steps]
        if _optional_step_field(step, "uses")
    ]
    assert [_step_field(step, "uses") for step in all_action_steps] == [
        CHECKOUT,
        CHECKOUT,
        SETUP_PYTHON,
        DOWNLOAD_ARTIFACT,
        DOWNLOAD_ARTIFACT,
    ]
    for step in all_action_steps:
        assert re.fullmatch(
            r"[^@\s]+@[0-9a-f]{40}",
            _step_field(step, "uses"),
        )

    steps = _steps_by_name(jobs["verify"])
    checkout = steps["Check out the resolved release commit"]
    assert _step_nested_mapping(checkout, "with") == {
        "clean": True,
        "fetch-depth": "0",
        "fetch-tags": True,
        "persist-credentials": False,
        "ref": "${{ needs.resolve.outputs.source_sha }}",
    }
    setup_python = steps["Select release verification Python"]
    assert _step_nested_mapping(setup_python, "with") == {
        "architecture": "x64",
        "python-version": "3.11",
    }
    download = steps["Download the CI-audited release artifact set"]
    assert _step_field(download, "uses") == DOWNLOAD_ARTIFACT
    assert _step_nested_mapping(download, "with") == {
        "name": "yt-final-1.0.0-${{ needs.resolve.outputs.source_sha }}",
        "path": "${{ runner.temp }}/release-artifacts",
    }

    verify_step = steps["Revalidate tag binding and downloaded artifacts"]
    assert _step_nested_mapping(verify_step, "env") == {
        "ARTIFACT_DIR": "${{ runner.temp }}/release-artifacts",
        "RELEASE_TAG": "${{ needs.resolve.outputs.tag }}",
        "SOURCE_SHA": "${{ needs.resolve.outputs.source_sha }}",
    }
    verify = _step_run(verify_step)
    for contract in (
        '"refs/tags/$tag^{commit}"',
        "$env:SOURCE_SHA",
        "$env:RELEASE_TAG",
        "$env:ARTIFACT_DIR",
        "scripts/verify_release.py",
        "--expected-version 1.0.0",
        "--trusted-source-root $env:GITHUB_WORKSPACE",
        "--expected-source-epoch $sourceEpoch",
        "git show --no-patch --format=%ct",
    ):
        assert contract in verify
    assert "${{" not in verify

    draft_steps = _steps_by_name(jobs["draft"])
    handoff = draft_steps["Download the verified release artifact handoff"]
    assert _step_field(handoff, "uses") == DOWNLOAD_ARTIFACT
    assert _step_nested_mapping(handoff, "with") == {
        "name": "yt-final-1.0.0-${{ needs.resolve.outputs.source_sha }}",
        "path": "${{ runner.temp }}/release-artifacts",
    }
    validate_handoff = _step_run(
        draft_steps["Validate the immutable artifact handoff"]
    )
    for contract in (
        "SHA256SUMS",
        "Get-ChildItem",
        "Get-FileHash",
        "SHA256",
        "[Text.UTF8Encoding]::new($false, $true)",
        "$expectedManifest",
        "$actualManifest",
    ):
        assert contract in validate_handoff
    assert "scripts/" not in validate_handoff
    assert "python " not in validate_handoff.casefold()
    assert "git " not in validate_handoff.casefold()

    create_step = draft_steps["Create the protected draft GitHub Release"]
    assert _step_nested_mapping(create_step, "env") == {
        "ARTIFACT_DIR": "${{ runner.temp }}/release-artifacts",
        "GH_TOKEN": "${{ github.token }}",
        "RELEASE_TAG": "${{ needs.resolve.outputs.tag }}",
        "REPOSITORY": "${{ github.repository }}",
        "SOURCE_SHA": "${{ needs.resolve.outputs.source_sha }}",
    }
    create = _step_run(create_step)
    explicit_assets = (
        "SHA256SUMS",
        "yt_final-1.0.0-portable.zip",
        "yt_final-1.0.0-py3-none-any.whl",
        "yt_final-1.0.0.tar.gz",
    )
    for name in explicit_assets:
        assert create.count(name) == 1
    for contract in (
        "gh release view",
        "--json isDraft,tagName",
        "gh release create --draft --verify-tag",
        "gh release upload",
        "--clobber",
        "repos/$repository/releases/tags/$tag",
        "repos/$repository/commits/$tag",
        "sha256:",
        "Audited commit: $sourceSha",
        "target_commitish",
        "$env:RELEASE_TAG",
        "$env:SOURCE_SHA",
        "$env:REPOSITORY",
    ):
        assert contract in create
    assert "${{" not in create
    assert "*" not in create
    assert text.count("gh release create") == 1
    assert create.count("gh release upload") == 1
    assert create.count("Assert-ReleaseMetadata $release") == 2
    lowered = text.casefold()
    for forbidden in (
        "pypi",
        "twine upload",
        "workflow_run",
        "pull_request_target",
        "release edit",
        "--draft=false",
        "--latest",
    ):
        assert forbidden not in lowered


def test_release_docs_disclose_manual_policy_proof_and_exact_verifier_binding():
    docs = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
    normalized = " ".join(docs.split())

    for required in (
        "built-in GITHUB_TOKEN",
        "cannot prove",
        "administrator bypass",
        "ruleset bypass actors",
        "release immutability",
        "manual confirmations",
        "--trusted-source-root",
        "--expected-source-epoch",
    ):
        assert required in normalized
