"""Skip gates must measure a capability, not assert an interpreter identity.

A skip gate keyed on a hardcoded interpreter version or OS name cannot self-heal: it
keeps deleting coverage long after the condition it was written for has gone away, and
nothing ever re-checks it. The concrete case this guard exists for was

    _SDIST_WRITER_UNSUPPORTED = sys.platform == "win32" and sys.version_info[:2] != (3, 11)

which silently skipped 29 cases (including an attacker-model test) on every non-3.11 cell
whether or not the os.stat/os.fstat divergence it named was real -- and the evidence says
it was not. It now probes the actual condition (test_release_packaging.py
`_sdist_identity_guard_self_consistent`), and the coverage came back.

WHAT THIS GUARD ENFORCES
    Every skip gate keyed on `sys.platform` / `sys.version_info` / `platform.system()` and
    friends must appear in `_KNOWN_GATES` below with a written justification. A new one
    fails this test. The point is not to ban the pattern -- it is to make reaching for an
    interpreter-identity equality a conscious, reviewed act instead of the path of least
    resistance when a cell goes red.

    Four shapes count as a gate, because a gate that hides is still a gate:
      * `@pytest.mark.skipif(...)` / `unittest.skipIf` / `skipUnless`, positional or
        `condition=`;
      * `if <cond>: pytest.skip(...)` (either branch), and the ternary spelling
        `pytest.skip(...) if <cond> else None`;
      * a condition hidden behind a module-level constant, which is expanded after " -> ";
      * a helper that FALLS THROUGH to `pytest.skip(...)` with no `if` governing it at all.
        That last one has no condition to read, which is exactly why it needs finding: it is
        the shape of `_current_native_target_name`, which deletes eleven parametrised cases
        on any host that is not one of the six Tier-1 cells.
    Aliases are resolved first, so `import sys as _sys2` and `from sys import platform as
    _plat` are the same gate as `sys.platform` and cannot slip past on spelling.

    Each entry must say either
      * status "LEGITIMATE" -- the API genuinely does not exist there, so there is nothing
        to measure; or
      * status "SUSPECT" -- it gates a behaviour that COULD be probed, plus a `probe`
        field naming the measurement that should replace it.
    A SUSPECT entry with no probe recorded is itself a failure. Recording the probe is
    what turns "CI is red, add a skip" into a tracked debt.

    Stale entries fail too: when a gate is finally replaced by a probe, its condition stops
    matching and the leftover entry is reported, so the allowlist cannot silently rot.

DELIBERATELY OUT OF SCOPE, AND SAY IT PLAINLY: `os.name == "nt"` / `os.name != "nt"`.
    The overwhelming majority of platform-or-identity-keyed skipif/skipUnless decorators in
    this suite are keyed on `os.name`; only a handful are keyed on an interpreter identity.
    So this guard polices the minority by design, and the majority idiom -- which is also the
    path of least resistance when a cell goes red -- is NOT audited here.

    (A precise tally used to sit here and went stale the first time a gate was replaced by a
    probe. A count nothing asserts is a claim that rots; the shape is the point, not the
    number.)

    The reason is that essentially all 64 are the genuine Windows-vs-POSIX API dichotomy
    (ctypes.WinDLL, msvcrt, NTFS reparse points, Windows SIDs/DACLs, Job Objects); covering
    them would need an allowlist bigger than the thing it documents, which is how
    allowlists stop being read. What keeps that from being a hole is that the OTHER guard
    covers it from the opposite side: test_platform_api_guards.py fails when a test can
    REACH a platform-exclusive API without a gate, so a wrong or inverted `os.name` gate is
    caught there by consequence rather than by spelling. Neither guard catches an `os.name`
    gate on a test that touches no platform-exclusive API at all -- e.g. one that skips on
    Windows purely because of a timing or path assumption. That case is still manual.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SELF = Path(__file__).name

# Interpreter-identity expressions. `os.name` is intentionally absent -- see the module docstring.
# Conditions are CANONICALISED before this runs (see _canonical), so `import sys as _sys2` and
# `from sys import platform as _plat` are rewritten back to `sys.platform` rather than needing a
# spelling in the pattern here. Matching raw source with a `_?sys\.` alternative would have covered
# exactly two aliases and missed the third, and this suite really does write `import sys as _sys2`.
_HARDCODED_IDENTITY = re.compile(
    r"(?<!\w)sys\.(?:platform|version_info|winver|implementation)\b"
    r"|(?<!\w)platform\.(?:system|machine|mac_ver|win32_ver|python_version)(?=\s*\()"
    r"|(?<!\w)os\.uname(?=\s*\()"
)

#: Modules whose members spell an interpreter identity; their aliases are resolved.
_IDENTITY_MODULES = frozenset({"sys", "platform", "os"})

_LEGITIMATE = "LEGITIMATE"
_SUSPECT = "SUSPECT"

# Keys are "<file>::<normalised condition>". The condition is the unparsed skip condition
# with every module-level constant it names expanded after " -> ", so replacing a constant
# with a real probe changes the key and drops the gate out of this set entirely.
_KNOWN_GATES = {
    "test_capability_macos.py::not sys.platform.startswith('darwin')": {
        "status": _LEGITIMATE,
        "why": (
            "test_live_macos_detects_videotoolbox drives CapabilityDetector against live "
            "Apple hardware; VideoToolbox exists only on macOS, so there is no condition "
            "to measure off-Darwin. The live half IS probed: the body skips when the host "
            "exposes no working videotoolbox encoder, which is what keeps virtualized "
            "macOS runners from failing it."
        ),
    },
    "test_release_packaging.py::_current_native_target_name() unconditional pytest.skip "
    "keyed on platform.machine, sys.platform, sys.version_info": {
        "status": _SUSPECT,
        "why": (
            "_current_native_target_name (test_release_packaging.py:4116) builds "
            "(sys.platform, normalised machine, sys.version_info[:2]), looks it up in "
            "NATIVE_TARGETS and falls through to pytest.skip when the running interpreter is "
            "not one of the six Tier-1 cells. It is the sole caller's first statement, so it "
            "silently deletes all 11 parametrisations of "
            "test_native_gate_rejects_untrusted_interpreter_identity_before_venv on every "
            "developer machine. Nothing in that test is cell-specific: it asserts that the "
            "native gate rejects a tampered interpreter identity BEFORE creating a venv, which "
            "is an attacker-model claim that would pass anywhere."
        ),
        "probe": (
            "Do not measure the host at all -- monkeypatch NATIVE_TARGETS with a synthetic "
            "entry built from the running interpreter, so the gate logic is exercised on every "
            "host, and keep the real-target lookup for the tests that genuinely consume "
            "reviewed per-cell artifacts."
        ),
    },
    "test_release_packaging.py::sys.version_info[:2] != (3, 11)": {
        "status": _SUSPECT,
        "why": (
            "test_lock_toolchain_uses_an_absolute_real_interpreter asserts that "
            "_verified_lock_toolchain resolves to an absolute real file and launches the "
            "probe with exactly that path. Nothing in that claim is 3.11-specific. It "
            "skips only because the helper defaults to expected_python=(3, 11) while the "
            "stub identity is built from the RUNNING interpreter, so the two disagree off "
            "the py311 cell. This deletes an acceptance test on 4 of the 6 Tier-1 cells."
        ),
        "probe": (
            "No probe is even needed -- pass the running interpreter's own version: "
            "_verified_lock_toolchain(str(interpreter), "
            "expected_python=sys.version_info[:2]), and drop the gate. The 3.11 pin still "
            "belongs on the tests that consume the reviewed lock artifacts, not on this "
            "path-resolution acceptance test."
        ),
    },
}


def _scanned_files():
    files = sorted(p for p in TESTS_DIR.glob("test_*.py") if p.name != SELF)
    for conftest in (TESTS_DIR / "conftest.py", ROOT / "conftest.py"):
        if conftest.is_file():
            files.append(conftest)
    return files


def _dotted(call):
    """Return the dotted name of a Call's callee, e.g. ('pytest', 'mark', 'skipif')."""
    func = call.func
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    parts.reverse()
    return tuple(parts)


def _is_call(node, tail):
    return (
        isinstance(node, ast.Call)
        and _dotted(node)[-len(tail):] == tail
    )


def _skip_names(tree):
    """Every dotted tail that calls pytest.skip in this file, aliases included.

    Hardcoding `("pytest", "skip")` would let `import pytest as _p` hide every gate in a file
    from all four detectors at once, so the skip callable is resolved per file just like the
    identity modules are.
    """
    names = {("pytest", "skip")}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest" and alias.asname:
                    names.add((alias.asname, "skip"))
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest" and not node.level:
            for alias in node.names:
                if alias.name == "skip":
                    names.add((alias.asname or "skip",))
    return frozenset(names)


def _is_skip_call(node, skip_names):
    if not isinstance(node, ast.Call):
        return False
    dotted = _dotted(node)
    return any(dotted[-len(tail):] == tail for tail in skip_names)


def _contains_skip(statements, skip_names):
    """True when a pytest skip is reachable directly in these statements.

    Nested function and class bodies are excluded: a skip in there belongs to the inner
    scope, not to this `if`.
    """
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if _is_skip_call(node, skip_names):
                return True
    return False


def _module_constants(tree):
    """Module-level names bound to an expression, in every ordinary spelling.

    `_G: bool = sys.platform == "win32"` (annotated) and `_A = _B = ...` (chained) are normal
    style, not evasion; missing them would let a gate hide behind a name, which is precisely what
    the constant expansion exists to prevent.
    """
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                constants[node.target.id] = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value
    return constants


class _Canonical(ast.NodeTransformer):
    """Rewrite aliased identity references back to their canonical `sys.`/`platform.` spelling."""

    def __init__(self, modules, members):
        self.modules = modules
        self.members = members

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id in self.members:
            return ast.copy_location(
                ast.parse(self.members[node.id], mode="eval").body, node)
        if node.id in self.modules and self.modules[node.id] != node.id:
            return ast.copy_location(ast.Name(id=self.modules[node.id], ctx=node.ctx), node)
        return node


def _bindings(tree):
    """(alias -> module, alias -> dotted member) for `sys`, `platform` and `os` imports."""
    modules, members = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _IDENTITY_MODULES:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if node.module in _IDENTITY_MODULES:
                for alias in node.names:
                    members[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return modules, members


def _canonical(node, bindings):
    """Unparse `node` with aliases resolved, on a copy so the source tree is untouched."""
    modules, members = bindings
    copied = ast.parse(ast.unparse(node), mode="eval").body
    return " ".join(ast.unparse(_Canonical(modules, members).visit(copied)).split())


def _expanded(condition, constants, bindings):
    """Unparse a skip condition, appending the source of each constant it names."""
    rendered = _canonical(condition, bindings)
    for name, value in constants.items():
        if re.search(r"\b%s\b" % re.escape(name), rendered):
            rendered += " -> " + _canonical(value, bindings)
    return rendered


def _skip_decorator_condition(node):
    """The condition of a skipif/skipIf/skipUnless call, positional or by keyword."""
    for tail in (("mark", "skipif"), ("skipIf",), ("skipUnless",)):
        if not _is_call(node, tail):
            continue
        if node.args:
            return node.args[0]
        for keyword in node.keywords:
            if keyword.arg == "condition":
                return keyword.value
    return None


class _WithoutMessages(ast.NodeTransformer):
    """Blank out string literals and the arguments of ``pytest.skip(...)``.

    The skip MESSAGE routinely names the platform (``pytest.skip(f"no ffmpeg on {sys.platform}")``)
    without the gate being keyed on it. Reading raw source instead was what made an earlier attempt
    at the fall-through check produce false positives on tests that merely mention
    ``sys.version_info`` inside a subprocess payload string.
    """

    def __init__(self, skip_names):
        self.skip_names = skip_names

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node

    def visit_Call(self, node):
        if _is_skip_call(node, self.skip_names):
            return ast.copy_location(
                ast.Call(func=node.func, args=[], keywords=[]), node)
        return self.generic_visit(node)


def _identity_tokens(node, bindings, skip_names):
    """Identity expressions this node reaches, ignoring strings and skip messages."""
    copied = ast.parse(ast.unparse(node))
    modules, members = bindings
    stripped = _WithoutMessages(skip_names).visit(copied)
    rendered = ast.unparse(_Canonical(modules, members).visit(stripped))
    return sorted(set(_HARDCODED_IDENTITY.findall(rendered)))


_BLOCK_FIELDS = ("body", "orelse", "finalbody")


def _unconditional_skip_lines(function, skip_names):
    """``pytest.skip(...)`` calls in this def's own scope that no ``if`` governs.

    A helper that falls through to ``pytest.skip`` is a skip gate with no condition to read -- it
    is the shape of `_current_native_target_name`, which deletes eleven parametrised cases on any
    host that is not a Tier-1 cell. A check that only looked at ``if`` tests could not see it.
    """
    lines = []

    def walk(statements, conditional):
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, ast.If):
                walk(statement.body, True)
                walk(statement.orelse, True)
                continue
            nested = False
            for field in _BLOCK_FIELDS:
                block = getattr(statement, field, None)
                if isinstance(block, list) and block:
                    walk(block, conditional)
                    nested = True
            for handler in getattr(statement, "handlers", None) or ():
                walk(handler.body, conditional)
                nested = True
            if nested:
                continue
            if conditional:
                continue
            for node in ast.walk(statement):
                if _is_skip_call(node, skip_names):
                    lines.append(node.lineno)

    walk(function.body, False)
    return lines


def _expression_skip(node, skip_names):
    """The condition of a ternary skip: ``pytest.skip(...) if <cond> else None``."""
    if not isinstance(node, ast.IfExp):
        return None
    for branch in (node.body, node.orelse):
        for inner in ast.walk(branch):
            if _is_skip_call(inner, skip_names):
                return node.test
    return None


def _identity_keyed_gates():
    """Every skip gate in the suite whose condition asserts an interpreter identity."""
    found = {}
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = _module_constants(tree)
        bindings = _bindings(tree)
        skip_names = _skip_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines = _unconditional_skip_lines(node, skip_names)
                tokens = _identity_tokens(node, bindings, skip_names) if lines else []
                if tokens:
                    key = (f"{path.name}::{node.name}() unconditional pytest.skip keyed on "
                           + ", ".join(tokens))
                    found.setdefault(key, []).extend(lines)
                continue
            condition = _skip_decorator_condition(node) or _expression_skip(node, skip_names)
            if condition is None:
                if isinstance(node, ast.If) and (
                        _contains_skip(node.body, skip_names)
                        or _contains_skip(node.orelse, skip_names)):
                    condition = node.test
                else:
                    continue
            rendered = _expanded(condition, constants, bindings)
            if not _HARDCODED_IDENTITY.search(rendered):
                continue
            key = f"{path.name}::{rendered}"
            found.setdefault(key, []).append(condition.lineno)
    return found


def test_no_unjustified_interpreter_identity_skip_gate():
    """A new version- or platform-string-keyed skip gate must be justified in writing."""
    found = _identity_keyed_gates()
    unknown = {
        key: lines for key, lines in found.items() if key not in _KNOWN_GATES
    }
    assert not unknown, (
        "New skip gate(s) keyed on a hardcoded interpreter version or platform string:\n"
        + "\n".join(
            "  %s  (line%s %s)"
            % (key, "" if len(lines) == 1 else "s", ", ".join(map(str, lines)))
            for key, lines in sorted(unknown.items())
        )
        + "\n\nA hardcoded identity cannot self-heal: it keeps deleting coverage after the "
        "condition it names stops holding. Probe the real condition instead (see "
        "test_release_packaging._sdist_identity_guard_self_consistent for the pattern). "
        "If the API genuinely does not exist there, add an entry to _KNOWN_GATES in "
        + SELF
        + " saying so."
    )


def test_known_interpreter_identity_gates_are_not_stale():
    """A gate replaced by a probe must lose its entry, so the set stays honest and tiny."""
    found = _identity_keyed_gates()
    stale = sorted(set(_KNOWN_GATES) - set(found))
    assert not stale, (
        "_KNOWN_GATES entries no longer match any skip gate in the suite:\n"
        + "\n".join("  " + key for key in stale)
        + "\n\nIf the gate was replaced by a capability probe, delete the entry."
    )


@pytest.mark.parametrize("key", sorted(_KNOWN_GATES))
def test_every_known_gate_records_why_it_cannot_be_probed(key):
    """SUSPECT gates must carry the probe that should replace them, as tracked debt."""
    entry = _KNOWN_GATES[key]
    assert entry.get("status") in (_LEGITIMATE, _SUSPECT), key
    assert entry.get("why", "").strip(), f"{key} has no justification"
    if entry["status"] == _SUSPECT:
        assert entry.get("probe", "").strip(), (
            f"{key} is marked SUSPECT but records no probe. A SUSPECT gate is disabling "
            "coverage that might well pass; name the measurement that would replace it."
        )
