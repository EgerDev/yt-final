"""Static syntax gate for every GitHub Actions workflow in .github/workflows/.

Tier 1 CI once shipped a workflow whose PowerShell setup step wrote

    Export-CiEnvironment "PIP_CONFIG_FILE" ( if ($IsWindows) { "NUL" } else { "/dev/null" } )

PowerShell parses a parenthesised `if` in argument position as a command invocation, so every one
of the six matrix cells died in its first real step with "The term 'if' is not recognized" - a
defect that was committed and never once executed. The remedy is the subexpression operator
`$( ... )`.

This module walks EVERY workflow file, resolves which run: blocks actually execute under PowerShell
(workflow defaults, job defaults, or a per-step shell:), strips the GitHub Actions ${{ }}
expressions the runner interpolates before any shell sees them, and applies two checks with the
real PowerShell language services:

  * Parser::ParseInput must report no parse errors, and
  * no CommandAst may name a PowerShell language KEYWORD.

The second check is not optional decoration. `( if ($x) { "a" } else { "b" } )` is perfectly valid
PowerShell *syntax* - the parentheses open a statement context, so `if` is parsed as the name of a
command to invoke - and ParseInput accepts it without a murmur. The failure only surfaces at run
time as "The term 'if' is not recognized", which is precisely how it survived review and reached
all six CI cells. A parse-only gate would have gone green on the broken file.

The gate proves both checks can still fail on every run: the manifest handed to pwsh carries two
positive controls, one holding the exact historical defect and one holding an unbalanced
expression, and the tests assert each is reported by the check that owns it.
"""
import base64
import functools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")

# The runner interpolates ${{ }} before the shell is started, so the text is not PowerShell and must
# be substituted out first: pwsh reads "${{" as the opening of a braced variable name and reports a
# spurious error. A bare identifier is a valid PowerShell command name, so the placeholder parses
# both inside a string literal and in argument position.
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
EXPRESSION_PLACEHOLDER = "GithubActionsExpression"

# Positive controls. These are handed to pwsh alongside the real workflow scripts so that every
# run of this module re-proves that each check still fires; a guard that cannot fail is worthless.
KEYWORD_CONTROL_ID = "positive-control:if-statement-in-argument-position"
KEYWORD_CONTROL = (
    'Export-CiEnvironment "PIP_CONFIG_FILE" '
    '( if ($IsWindows) { "NUL" } else { "/dev/null" } )\n'
)
PARSE_CONTROL_ID = "positive-control:unbalanced-expression"
PARSE_CONTROL = "Write-Host ( 1 +\n"

# PowerShell keywords that a parenthesised statement context silently demotes to a command name.
#
# KNOWN GAP - `foreach` and `where` are deliberately absent: both are real cmdlet aliases
# (ForEach-Object, Where-Object) and appear as legitimate CommandAst names in pipelines, so
# listing them would flag correct code. They are not the hole they look like: the shapes an author
# would actually write, `( foreach ($x in ...) { } )` and `( for (...;...;...) { } )`, are rejected
# by the ParseInput check instead ("Unexpected token 'in' in expression or statement"). A bare
# `( where { $_ } )` does still evade both checks.
#
# The control-flow and reserved words below were each verified against the real parser: in argument
# position every one of them yields a CommandAst named the keyword with zero parse errors (so it
# fails at run time exactly like the historical `if`), while legitimate statement-level use --
# `throw "x"`, `exit 1`, `return 1`, `break`/`continue` in a loop, `static`/`hidden` inside a
# PowerShell class -- produces no CommandAst at all and therefore cannot false-positive.
COMMAND_POSITION_KEYWORDS = (
    "begin",
    "break",
    "catch",
    "class",
    "continue",
    "data",
    "define",
    "do",
    "dynamicparam",
    "else",
    "elseif",
    "end",
    "enum",
    "exit",
    "filter",
    "finally",
    "from",
    "function",
    "hidden",
    "if",
    "in",
    "inlinescript",
    "param",
    "parallel",
    "process",
    "return",
    "sequence",
    "static",
    "switch",
    "throw",
    "trap",
    "try",
    "until",
    "using",
    "var",
    "while",
    "workflow",
)

# Both `shell: pwsh` (PowerShell 7) and `shell: powershell` (Windows PowerShell 5.1) are checked.
# The parser doing the checking is always the pwsh 7 one on this host: validating a PS7 script with
# the 5.1 grammar would produce false positives, and the 5.1-only constructs that go the other way
# (workflow{}, PSv2 syntax) are not used here.
POWERSHELL_SHELLS = frozenset({"pwsh", "powershell"})
KNOWN_SHELLS = POWERSHELL_SHELLS | frozenset(
    {"bash", "sh", "cmd", "python", "node", "perl", "powershell-core"}
)

KEY = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^\s#"'][^:]*?)\s*:(?:[ ](?P<value>.*))?$""")
BLOCK_SCALAR_HEADER = re.compile(r"^(?P<style>[|>])(?:[-+]?[0-9]?|[0-9][-+]?)$")
# Anti-vacuity counter only (see _workflow_units): a raw-text count of block run: scripts, used to
# prove the reader did not silently drop steps. KNOWN GAP - it reads text, not structure, so a
# PowerShell here-string containing a line that is itself exactly `run: |` would inflate the count
# and fail the whole module with a message that blames the reader. No workflow does that today.
RUN_BLOCK_MARKER = re.compile(r"(?m)^[ ]*run:[ ]*[|>]")


class WorkflowSyntaxError(Exception):
    """A workflow file is not the block-structured YAML subset GitHub Actions uses."""


class BlockScalar:
    """A literal (|) or folded (>) block scalar plus where its first content line lives."""

    def __init__(self, text, style, first_line):
        self.text = text
        self.style = style
        self.first_line = first_line


def _indent_of(text):
    return len(text) - len(text.lstrip(" "))


def _is_ignorable(text):
    stripped = text.strip()
    return not stripped or stripped.startswith("#")


def _significant(lines, index):
    while index < len(lines) and _is_ignorable(lines[index][1]):
        index += 1
    return index


def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _read_block_scalar(lines, index, key_indent, header, key_line):
    style = BLOCK_SCALAR_HEADER.fullmatch(header).group("style")
    collected = []
    while index < len(lines):
        text = lines[index][1]
        if text.strip() and _indent_of(text) <= key_indent:
            break
        collected.append(lines[index])
        index += 1
    body = [text for _number, text in collected if text.strip()]
    if not body:
        return BlockScalar("", style, key_line), index
    block_indent = _indent_of(body[0])
    if block_indent <= key_indent:
        raise WorkflowSyntaxError(f"line {key_line}: block scalar is not indented")
    rendered = []
    for number, text in collected:
        if not text.strip():
            rendered.append("")
            continue
        if _indent_of(text) < block_indent:
            raise WorkflowSyntaxError(f"line {number}: block scalar line is under-indented")
        rendered.append(text[block_indent:])
    while rendered and not rendered[-1]:
        rendered.pop()
    text = "\n".join(rendered)
    if text:
        text += "\n"
    return BlockScalar(text, style, collected[0][0]), index


def _parse_child(lines, index, parent_indent):
    if index >= len(lines):
        return None, index
    text = lines[index][1]
    current = _indent_of(text)
    body = text[current:]
    if current > parent_indent:
        return _parse_block(lines, index, current)
    if current == parent_indent and (body == "-" or body.startswith("- ")):
        return _parse_sequence(lines, index, current)
    return None, index


def _parse_mapping(lines, index, indent):
    mapping = {}
    while True:
        index = _significant(lines, index)
        if index >= len(lines):
            break
        number, text = lines[index]
        current = _indent_of(text)
        if current < indent:
            break
        if current > indent:
            raise WorkflowSyntaxError(f"line {number}: unexpected indentation in a mapping")
        body = text[indent:]
        if body == "-" or body.startswith("- "):
            break
        match = KEY.match(body)
        if match is None:
            raise WorkflowSyntaxError(f"line {number}: not a mapping entry: {body!r}")
        key = _scalar(match.group("key"))
        if key in mapping:
            raise WorkflowSyntaxError(f"line {number}: duplicate mapping key {key!r}")
        raw = (match.group("value") or "").strip()
        if not raw:
            child = _significant(lines, index + 1)
            mapping[key], index = _parse_child(lines, child, indent)
        elif BLOCK_SCALAR_HEADER.fullmatch(raw):
            mapping[key], index = _read_block_scalar(lines, index + 1, indent, raw, number)
        else:
            mapping[key], index = _scalar(raw), index + 1
    return mapping, index


def _parse_sequence(lines, index, indent):
    items = []
    while True:
        index = _significant(lines, index)
        if index >= len(lines):
            break
        number, text = lines[index]
        current = _indent_of(text)
        if current < indent:
            break
        if current > indent:
            raise WorkflowSyntaxError(f"line {number}: unexpected indentation in a sequence")
        body = text[indent:]
        if body != "-" and not body.startswith("- "):
            break
        rest = body[2:] if body.startswith("- ") else ""
        if not rest.strip():
            child = _significant(lines, index + 1)
            item, index = _parse_child(lines, child, indent)
            items.append(item)
            continue
        item_indent = indent + 2
        window = [(number, " " * item_indent + rest)]
        probe = index + 1
        while probe < len(lines):
            candidate = lines[probe][1]
            if candidate.strip() and _indent_of(candidate) < item_indent:
                break
            window.append(lines[probe])
            probe += 1
        if KEY.match(rest) is None:
            if _significant(window, 1) < len(window):
                raise WorkflowSyntaxError(f"line {number}: scalar sequence item has a body")
            items.append(_scalar(rest))
        else:
            item, consumed = _parse_block(window, 0, item_indent)
            if _significant(window, consumed) < len(window):
                raise WorkflowSyntaxError(f"line {number}: sequence item was not fully consumed")
            items.append(item)
        index = probe
    return items, index


def _parse_block(lines, index, indent):
    body = lines[index][1][indent:]
    if body == "-" or body.startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _load_workflow(path):
    """Parse one workflow into plain dicts, lists, strings and BlockScalar values."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise WorkflowSyntaxError("workflow starts with a UTF-8 BOM")
    if b"\t" in raw:
        raise WorkflowSyntaxError("workflow indents with a tab")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    lines = list(enumerate(text.split("\n"), 1))
    for number, value in lines:
        if value.strip() in {"---", "..."}:
            raise WorkflowSyntaxError(f"line {number}: multi-document YAML is not supported")
    index = _significant(lines, 0)
    if index >= len(lines):
        raise WorkflowSyntaxError("workflow has no content")
    if _indent_of(lines[index][1]) != 0:
        raise WorkflowSyntaxError(f"line {lines[index][0]}: document is indented")
    document, index = _parse_block(lines, index, 0)
    index = _significant(lines, index)
    if index < len(lines):
        raise WorkflowSyntaxError(f"line {lines[index][0]}: unparsed trailing content")
    if not isinstance(document, dict):
        raise WorkflowSyntaxError("workflow is not a mapping")
    return document


def _workflow_files():
    assert WORKFLOW_DIR.is_dir(), f"missing workflow directory: {WORKFLOW_DIR}"
    files = sorted(
        path
        for path in WORKFLOW_DIR.iterdir()
        if path.is_file() and path.suffix in WORKFLOW_SUFFIXES
    )
    assert files, "no GitHub Actions workflows were found"
    return files


def _declared_shell(container):
    """Return the shell a defaults: block pins, or None."""
    if not isinstance(container, dict):
        return None
    defaults = container.get("defaults")
    if not isinstance(defaults, dict):
        return None
    run = defaults.get("run")
    if not isinstance(run, dict):
        return None
    shell = run.get("shell")
    return shell if isinstance(shell, str) else None


def _implicit_shell(job):
    """GitHub's implicit shell: pwsh on Windows runners, bash elsewhere."""
    runner = job.get("runs-on")
    if isinstance(runner, list):
        runner = " ".join(str(label) for label in runner)
    if not isinstance(runner, str):
        return None
    lowered = runner.lower()
    if "windows" in lowered:
        return "pwsh"
    if any(family in lowered for family in ("macos", "ubuntu", "linux")):
        return "bash"
    return None


def _workflow_units(path):
    """Every run: block in one workflow, with the shell that will execute it."""
    document = _load_workflow(path)
    jobs = document.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path.name}: workflow declares no jobs"
    workflow_shell = _declared_shell(document)
    units = []
    for job_id, job in jobs.items():
        assert isinstance(job, dict), f"{path.name}: job {job_id!r} is not a mapping"
        steps = job.get("steps")
        if steps is None:
            continue
        assert isinstance(steps, list), f"{path.name}: job {job_id!r} has malformed steps"
        job_shell = _declared_shell(job) or workflow_shell
        for position, step in enumerate(steps, 1):
            assert isinstance(step, dict), (
                f"{path.name}: job {job_id!r} step {position} is not a mapping"
            )
            script = step.get("run")
            if script is None:
                continue
            name = step.get("name") or f"step {position}"
            label = f"{path.name}:{job_id}:{position}:{name}"
            shell = step.get("shell") or job_shell or _implicit_shell(job)
            # Failing loudly beats skipping silently: a block whose shell cannot be resolved is a
            # block this gate would otherwise never check. DELIBERATE CONSEQUENCE - a job that
            # writes `runs-on: ${{ matrix.os }}` with no defaults.run.shell is unresolvable and
            # fails here even though the step itself is fine. Both workflows pin
            # defaults.run.shell, so it does not fire today; the remedy is in the message.
            assert shell in KNOWN_SHELLS, (
                f"{label} runs a script under an undetermined shell {shell!r}; "
                "declare shell: on the step or defaults.run.shell"
            )
            if isinstance(script, BlockScalar):
                assert script.style == "|" or shell not in POWERSHELL_SHELLS, (
                    f"{label} uses a folded (>) run block: folding rewrites newlines into "
                    "spaces and silently corrupts the script"
                )
                text, first_line = script.text, script.first_line
            else:
                text, first_line = f"{script}\n", None
            units.append(
                {
                    "id": label,
                    "workflow": path,
                    "shell": shell,
                    "script": text,
                    "first_line": first_line,
                }
            )
    declared = len(RUN_BLOCK_MARKER.findall(path.read_text(encoding="utf-8")))
    found = sum(1 for unit in units if unit["first_line"] is not None)
    assert found == declared, (
        f"{path.name}: found {found} block run: scripts but the file declares {declared}; "
        "the workflow reader is missing steps"
    )
    return units


@functools.lru_cache(maxsize=1)
def _powershell_units():
    units = []
    for path in _workflow_files():
        units.extend(
            unit for unit in _workflow_units(path) if unit["shell"] in POWERSHELL_SHELLS
        )
    return tuple(units)


DRIVER = """
$ErrorActionPreference = 'Stop'
$manifest = [IO.File]::ReadAllText($env:YT_FINAL_WORKFLOW_MANIFEST) | ConvertFrom-Json
$keywords = @($manifest.keywords)
$findings = [Collections.Generic.List[object]]::new()
foreach ($unit in $manifest.units) {
    $source = [IO.File]::ReadAllText($unit.path)
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput(
        $source, [ref] $tokens, [ref] $errors)
    foreach ($parseError in $errors) {
        $findings.Add([pscustomobject]@{
            id = $unit.id
            kind = 'parse'
            line = $parseError.Extent.StartLineNumber
            column = $parseError.Extent.StartColumnNumber
            message = $parseError.Message
        })
    }
    if ($null -eq $ast) {
        continue
    }
    $commands = $ast.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst]
        },
        $true)
    foreach ($command in $commands) {
        $element = $command.CommandElements[0]
        if ($element -isnot
            [System.Management.Automation.Language.StringConstantExpressionAst]) {
            continue
        }
        if ($element.StringConstantType -ne 'BareWord') {
            continue
        }
        if ($command.InvocationOperator -ne 'Unknown') {
            continue
        }
        if ($keywords -ccontains $element.Value.ToLowerInvariant()) {
            $findings.Add([pscustomobject]@{
                id = $unit.id
                kind = 'keyword'
                line = $command.Extent.StartLineNumber
                column = $command.Extent.StartColumnNumber
                message = (
                    "the PowerShell keyword '" + $element.Value + "' is being invoked as a " +
                    "command; a parenthesised statement needs the subexpression operator " +
                    '$( ... )')
            })
        }
    }
}
$payload = [pscustomobject]@{ findings = @($findings) }
[Console]::Out.Write(($payload | ConvertTo-Json -Depth 6 -Compress))
"""


@functools.lru_cache(maxsize=1)
def _parser_findings():
    """Run the real PowerShell language services once over every unit and both controls."""
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("pwsh is unavailable on this host; the CI matrix still enforces this gate")
    units = list(_powershell_units())
    assert units, "no PowerShell run: blocks were extracted from the workflows"
    with tempfile.TemporaryDirectory(prefix="yt-final-workflow-syntax-") as directory:
        root = Path(directory)
        manifest = []
        scripts = [(unit["id"], unit["script"]) for unit in units]
        scripts.append((KEYWORD_CONTROL_ID, KEYWORD_CONTROL))
        scripts.append((PARSE_CONTROL_ID, PARSE_CONTROL))
        for position, (identifier, script) in enumerate(scripts):
            target = root / f"unit_{position}.ps1"
            target.write_text(
                EXPRESSION.sub(EXPRESSION_PLACEHOLDER, script), encoding="utf-8", newline="\n"
            )
            manifest.append({"id": identifier, "path": str(target)})
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps({"keywords": list(COMMAND_POSITION_KEYWORDS), "units": manifest}),
            encoding="utf-8",
        )
        # -EncodedCommand, not `-Command -`: piping a script into pwsh on stdin runs it the way a
        # prompt would, and a multi-line `foreach { }` block is then silently dropped - the driver
        # exits 0 having produced nothing, which would make this gate pass vacuously.
        encoded = base64.b64encode(DRIVER.encode("utf-16-le")).decode("ascii")
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env={**os.environ, "YT_FINAL_WORKFLOW_MANIFEST": str(manifest_path)},
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    assert completed.returncode == 0, (
        "the PowerShell parser driver failed:\n"
        f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
    )
    payload = json.loads(completed.stdout)
    findings = {}
    for finding in payload["findings"]:
        findings.setdefault(finding["id"], []).append(finding)
    return findings


def _findings_of_kind(unit, kind):
    return [
        finding
        for finding in _parser_findings().get(unit["id"], ())
        if finding["kind"] == kind
    ]


def _render(unit, findings):
    lines = []
    for finding in findings:
        location = f"script line {finding['line']}"
        if unit["first_line"] is not None:
            location = f"{unit['workflow'].name} line {unit['first_line'] + finding['line'] - 1}"
        lines.append(f"  {location} col {finding['column']}: {finding['message']}")
    return "\n".join(lines)


def test_every_workflow_is_structured_yaml_with_jobs():
    for path in _workflow_files():
        try:
            document = _load_workflow(path)
        except WorkflowSyntaxError as error:
            pytest.fail(f"{path.name} is not parseable YAML: {error}")
        assert isinstance(document.get("jobs"), dict) and document["jobs"], (
            f"{path.name} must declare at least one job"
        )
        assert "name" in document, f"{path.name} must declare a workflow name"


def _line_terminated(script):
    """Canonical trailing-newline form, so the two readers are compared on content alone.

    The in-repo reader always hands the shell a newline-terminated script; PyYAML reproduces the
    scalar's own chomping, so a plain `run: echo hi` (and a chomped `run: |-`) comes back with no
    trailing newline. Comparing raw would fail on `run: echo hi`, which is a perfectly ordinary
    step, with a message that says nothing about newlines. What this cross-check is for is
    STRUCTURE - which job, which step, which script text - not chomping.
    """
    return script.rstrip("\n") + "\n" if script.strip() else ""


def test_workflow_reader_agrees_with_a_reference_yaml_parser():
    """Cross-check the in-repo reader against PyYAML wherever PyYAML happens to be installed.

    PyYAML is deliberately NOT in requirements/dev.lock - the release closure is hash-pinned and
    a test-only dependency is not worth widening it - so this is a local fidelity check, never a
    prerequisite for the gate above.

    KNOWN GAP: a folded (`>`) run block would compare unequal, because PyYAML folds newlines into
    spaces and this reader does not. PowerShell blocks may not use `>` at all (folding silently
    corrupts the script - asserted in _workflow_units), and no bash block uses it today.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML is not part of the locked dev closure")
    for path in _workflow_files():
        reference = yaml.safe_load(path.read_text(encoding="utf-8"))
        expected = []
        for job_id, job in reference["jobs"].items():
            for position, step in enumerate(job.get("steps") or [], 1):
                if "run" in step:
                    expected.append((job_id, position, _line_terminated(step["run"])))
        actual = [
            (
                unit["id"].split(":")[1],
                int(unit["id"].split(":")[2]),
                _line_terminated(unit["script"]),
            )
            for unit in _workflow_units(path)
        ]
        assert actual == expected, f"{path.name}: the workflow reader disagrees with PyYAML"


def test_every_powershell_run_block_parses_with_the_real_parser():
    failures = []
    for unit in _powershell_units():
        reported = _findings_of_kind(unit, "parse")
        if reported:
            failures.append(f"{unit['id']} is not valid PowerShell:\n{_render(unit, reported)}")
    assert not failures, "\n".join(failures)


def test_no_powershell_run_block_invokes_a_language_keyword_as_a_command():
    failures = []
    for unit in _powershell_units():
        reported = _findings_of_kind(unit, "keyword")
        if reported:
            failures.append(f"{unit['id']} misuses a keyword:\n{_render(unit, reported)}")
    assert not failures, "\n".join(failures)


def test_the_gate_still_rejects_the_historical_argument_position_if():
    """The keyword check must be able to fail on the defect that killed all six Tier 1 cells."""
    reported = _parser_findings().get(KEYWORD_CONTROL_ID, ())
    assert [finding for finding in reported if finding["kind"] == "keyword"], (
        "the keyword check no longer reports an if-STATEMENT in argument position, so this gate "
        "can no longer detect the defect it exists for"
    )


def test_the_gate_still_rejects_a_genuine_parse_error():
    """The ParseInput check must be able to fail."""
    reported = _parser_findings().get(PARSE_CONTROL_ID, ())
    assert [finding for finding in reported if finding["kind"] == "parse"], (
        "the PowerShell parser accepted an unbalanced expression, so the parse check is inert"
    )


# --------------------------------------------------------------------------------------
# GitHub issue forms and the PR template. These are not workflows, but they fail the same
# way: silently. A malformed `body` element makes the whole form unavailable in the UI with
# no error anywhere, and with `blank_issues_enabled: false` a typo can leave a repository
# with NO working issue path at all. Nothing else in the suite reads them.
# --------------------------------------------------------------------------------------

ISSUE_TEMPLATE_DIR = ROOT / ".github" / "ISSUE_TEMPLATE"

#: Element types GitHub accepts in an issue form body.
_ISSUE_FORM_BODY_TYPES = frozenset({
    "markdown", "input", "textarea", "dropdown", "checkboxes",
})


def _issue_form_files():
    if not ISSUE_TEMPLATE_DIR.is_dir():
        return []
    return sorted(
        path for path in ISSUE_TEMPLATE_DIR.iterdir()
        if path.suffix in WORKFLOW_SUFFIXES and path.name != "config.yml"
    )


def test_issue_forms_parse_and_use_valid_body_elements():
    """Every issue form must be well-formed YAML with a name, description, and valid body.

    PyYAML is deliberately not in the locked dev closure, so this skips rather than widens it -
    same arrangement as the workflow fidelity check above.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML is not part of the locked dev closure")
    forms = _issue_form_files()
    assert forms, "no issue forms found under .github/ISSUE_TEMPLATE"
    for path in forms:
        form = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(form, dict), f"{path.name} is not a mapping"
        for key in ("name", "description", "body"):
            assert form.get(key), f"{path.name} is missing '{key}'"
        for index, element in enumerate(form["body"], 1):
            kind = element.get("type")
            assert kind in _ISSUE_FORM_BODY_TYPES, (
                f"{path.name} body[{index}] has invalid type {kind!r}")
            assert element.get("attributes"), (
                f"{path.name} body[{index}] has no attributes")


def test_issue_template_config_routes_somewhere_that_exists():
    """`blank_issues_enabled: false` means the contact links are the only other route.

    Both point at repository FEATURES that are off by default -- Discussions and private
    vulnerability reporting -- so if neither is enabled a reporter following SECURITY.md lands on a
    page with no form. This test cannot check a GitHub setting; it checks the file is coherent and
    that the repository slug matches the one the rest of the docs use, so a copy-paste from another
    project cannot send reporters somewhere else.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML is not part of the locked dev closure")
    config_path = ISSUE_TEMPLATE_DIR / "config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    links = config.get("contact_links") or []
    assert links, "config.yml disables blank issues but offers no contact links"
    for link in links:
        for key in ("name", "url", "about"):
            assert link.get(key), f"contact link missing '{key}': {link}"
        assert link["url"].startswith("https://github.com/EgerDev/yt-final"), link["url"]


def test_pull_request_template_states_the_licensing_term():
    """A PR template that omits the dedication makes CONTRIBUTING's term unenforceable in practice.

    The Unlicense is a public-domain dedication rather than a licence grant, so inbound=outbound
    does not do the work here; the checkbox is where a contributor actually agrees.
    """
    template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert "public domain" in template.casefold()
    assert "licensing-of-contributions" in template
