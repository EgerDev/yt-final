"""Static guard: no test may reach a platform-exclusive API on a platform that lacks it.

The single largest CI failure category on this repo was "true on the author machine, false
everywhere else": ``ctypes.WinDLL`` / ``ctypes.get_last_error`` evaluated on macOS runners,
``msvcrt`` imported on POSIX, ``os.geteuid`` called on Windows. Each one is an AttributeError,
ImportError or ValueError the moment the test body runs on the wrong host, and none of them can
be discovered by running the suite on a single machine.

This module parses every ``Final/test_*.py`` with :mod:`ast` and fails if a test function can
reach a platform-exclusive API on a host that does not provide it. "Can reach" is resolved
structurally, never textually:

* the reference is found by AST shape -- ``ctypes.WinDLL`` is an ``Attribute`` on a name bound to
  the ``ctypes`` module (aliases resolved), ``msvcrt`` is a ``Name`` bound by an ``import msvcrt``
  in the same file -- so a matching word inside a string, a comment, a
  ``monkeypatch.setattr(mod, "_windows_x", ...)`` target or a subprocess script payload is not a
  reference;
* the guard is found by decorator/AST inspection of *that* function, its class and its module
  (skipif / skipUnless, module- or class-level ``pytestmark``, an autouse fixture that skips, an
  early ``if <wrong platform>: skip/return/raise``, an ``if <right platform>:`` branch, or a
  tolerant ``try/except``), so a ``skipif`` sitting on a *different* function never counts;
* module-level helpers are followed transitively, because a helper is a lexical extension of the
  test that calls it -- ``_windows_sddl()`` running unguarded fails its caller exactly as if the
  body had been pasted inline -- and so are fixtures the test requests by parameter name, which
  the call graph alone cannot see because a fixture is never *called*;
* the MODULE BODY is scanned too. A module-level ``import msvcrt`` on POSIX is a collection
  error, and ``pytestmark`` cannot prevent it -- pytest never gets far enough to read the marker.
  Verified with a real run: ``ModuleNotFoundError ... Interrupted: 1 error during collection``.

Guard *direction* is checked as well: ``skipif(os.name == "nt")`` on a ``ctypes.WinDLL`` test is
not a guard, it is an inversion that guarantees the failure. All the spellings of a real guard
are accepted, so that a correctly written test is never told to fix something that is not broken:
``skipif``/``skipIf``/``skipUnless``, a positional or ``condition=`` argument, a string condition,
and a hoisted predicate (``IS_WINDOWS = os.name == "nt"``) resolved back to its definition.

KNOWN GAPS, none of which correspond to code in this repo today. The guard sees only what the AST
shows, and flagging anything less structural is what would force an allowlist:

* dynamic reach -- ``getattr(os, "geteuid")()``, ``importlib.import_module("fcntl")``, or an API
  named inside a subprocess source string -- is invisible on purpose;
* helpers are followed within one module only: a Windows-only helper imported from another test
  module, or defined as a method on a test class (``self._helper()``), is not traced;
* the marker tables below are a curated list, not the whole platform surface. POSIX-only
  ``signal.SIGKILL``/``os.uname`` and Windows-only ``subprocess.CREATE_NO_WINDOW``/
  ``os.add_dll_directory`` are absent; the live uses of the first two are correctly guarded today,
  but the guard is not what would catch them.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

TEST_DIR = Path(__file__).resolve().parent

WINDOWS = "windows"
POSIX = "posix"

#: ``ctypes`` members that exist only on Windows builds of CPython.
WINDOWS_CTYPES_MEMBERS = frozenset({
    "WinDLL",
    "OleDLL",
    "windll",
    "oledll",
    "WinError",
    "FormatError",
    "get_last_error",
    "set_last_error",
    "wintypes",
})

#: Top-level modules importable only on Windows / only on POSIX.
WINDOWS_MODULES = frozenset({"msvcrt", "winreg", "_winapi"})
POSIX_MODULES = frozenset({"fcntl", "pwd", "grp", "termios", "posix"})

#: Dotted submodules importable only on Windows. ``import ctypes.wintypes as wt`` binds ``wt`` to
#: the submodule, so the ``ctypes.<member>`` attribute rule never sees it -- it needs its own entry.
WINDOWS_SUBMODULES = frozenset({"ctypes.wintypes"})

#: ``os`` members that exist only on Windows / only on POSIX.
WINDOWS_OS_MEMBERS = frozenset({"startfile"})
POSIX_OS_MEMBERS = frozenset({
    "geteuid",
    "getuid",
    "getegid",
    "getgid",
    "getpgid",
    "setpgid",
    "getpgrp",
    "killpg",
    "setsid",
    "fork",
    "forkpty",
    "wait3",
    "wait4",
})

#: Modules whose aliases must be tracked so ``import os as _os`` still resolves.
TRACKED_MODULES = (
    frozenset({"os", "sys", "ctypes", "platform"}) | WINDOWS_MODULES | POSIX_MODULES
)

#: Exception classes a "this API may simply not exist here" handler is allowed to absorb.
TOLERANT_EXCEPTIONS = frozenset({
    "ImportError",
    "ModuleNotFoundError",
    "AttributeError",
    "NotImplementedError",
    "ValueError",
    "OSError",
    "EnvironmentError",
    "Exception",
    "BaseException",
})

#: ``sys.platform`` values that mean Windows.
WINDOWS_PLATFORM_VALUES = frozenset({"win32", "win", "windows", "cygwin", "msys"})

#: No test is allowlisted. A finding here is either a real latent CI failure or a bug in this
#: guard; silencing one with a table entry would defeat the whole point of the file.
ALLOWLIST: frozenset = frozenset()


def windows_helper_names() -> frozenset[str]:
    """Repo-owned Windows-only helpers: module-level ``_windows_*`` defs in secureio.py."""
    tree = ast.parse((TEST_DIR / "secureio.py").read_text(encoding="utf-8"))
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_windows_")
    )


def _other(kind: str) -> str:
    return POSIX if kind == WINDOWS else WINDOWS


# ---------------------------------------------------------------------------------------------
# expression helpers
# ---------------------------------------------------------------------------------------------

def _dotted(node: ast.AST, aliases: dict[str, str] | None = None) -> str | None:
    """Render an attribute chain as a dotted string, resolving the root module alias."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    root = current.id
    if aliases:
        root = aliases.get(root, root)
    parts.append(root)
    return ".".join(reversed(parts))


def _string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _strings(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    values = [_string(item) for item in node.elts]
    return None if any(value is None for value in values) else values  # type: ignore[return-value]


def _equality_meaning(left: str, values: list[str]) -> tuple[str | None, str | None]:
    """Return (kind when equal, kind when NOT equal) for ``<left> == <values>``.

    ``None`` in either slot means "that direction says nothing reliable about the platform".
    """
    if left == "os.name":
        if all(value == "nt" for value in values):
            return WINDOWS, POSIX
        if all(value == "posix" for value in values):
            return POSIX, WINDOWS
        return None, None
    if left == "sys.platform":
        if all(value in WINDOWS_PLATFORM_VALUES for value in values):
            return WINDOWS, POSIX
        if all(value not in WINDOWS_PLATFORM_VALUES for value in values):
            # "== darwin" pins POSIX; "!= darwin" still permits linux *and* win32.
            return POSIX, None
    return None, None


def classify(
    node: ast.AST,
    aliases: dict[str, str] | None = None,
    constants: dict[str, ast.expr] | None = None,
    _seen: frozenset[str] = frozenset(),
) -> str | None:
    """Return WINDOWS if the expression is true exactly on Windows, POSIX if exactly off it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # pytest still accepts a string condition: skipif("os.name != 'nt'", reason=...).
        try:
            parsed = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
        return classify(parsed, aliases, constants, _seen)

    if isinstance(node, ast.Name) and constants and node.id in constants and node.id not in _seen:
        # A hoisted gate -- IS_WINDOWS = os.name == "nt" -- is the same predicate, named once.
        return classify(constants[node.id], aliases, constants, _seen | {node.id})

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {WINDOWS: POSIX, POSIX: WINDOWS}.get(
            classify(node.operand, aliases, constants, _seen))

    if isinstance(node, ast.BoolOp):
        parts = [classify(value, aliases, constants, _seen) for value in node.values]
        if isinstance(node.op, ast.Or):
            # "skip if A or B": one platform-decisive operand already bounds the platform.
            for part in parts:
                if part:
                    return part
            return None
        # "A and B" holds only when both hold, so no single operand bounds the platform.
        return parts[0] if parts and len(set(parts)) == 1 and parts[0] else None

    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        operator = node.ops[0]
        comparator = node.comparators[0]
        left = _dotted(node.left, aliases)
        if (left in {"platform.system", None} and isinstance(node.left, ast.Call)
                and _dotted(node.left.func, aliases) == "platform.system"):
            left = "platform.system"
            values = [_string(comparator)] if _string(comparator) else _strings(comparator)
            if values:
                equal = WINDOWS if all(v == "Windows" for v in values) else (
                    POSIX if all(v != "Windows" for v in values) else None)
                unequal = _other(equal) if equal else None
                return _pick(operator, equal, unequal)
            return None
        if left is None:
            return None
        values = [_string(comparator)] if _string(comparator) is not None else _strings(comparator)
        if not values:
            return None
        equal, unequal = _equality_meaning(left, values)
        return _pick(operator, equal, unequal)

    if isinstance(node, ast.Call):
        target = _dotted(node.func, aliases)
        args = node.args
        if target == "hasattr" and len(args) == 2 and _dotted(args[0], aliases) == "os":
            attribute = _string(args[1])
            if attribute in POSIX_OS_MEMBERS:
                return POSIX
            if attribute in WINDOWS_OS_MEMBERS:
                return WINDOWS
            return None
        if target == "sys.platform.startswith" and len(args) == 1:
            prefix = _string(args[0])
            if prefix:
                return WINDOWS if prefix.lower().startswith("win") else POSIX
    return None


def _pick(operator: ast.cmpop, equal: str | None, unequal: str | None) -> str | None:
    if isinstance(operator, (ast.Eq, ast.In)):
        return equal
    if isinstance(operator, (ast.NotEq, ast.NotIn)):
        return unequal
    return None


# ---------------------------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------------------------

class _Reference:
    __slots__ = ("platform", "label", "line", "via")

    def __init__(self, platform: str, label: str, line: int, via: tuple[str, ...] = ()):
        self.platform = platform
        self.label = label
        self.line = line
        self.via = via

    def through(self, helper: str, line: int) -> "_Reference":
        return _Reference(self.platform, self.label, line, (helper, *self.via))

    def key(self) -> tuple:
        return (self.platform, self.label, self.line, self.via)

    def describe(self) -> str:
        chain = " -> ".join(f"{name}()" for name in self.via)
        return (f"line {self.line}: {self.label} [{self.platform}-only]"
                + (f" reached via {chain}" if chain else ""))


def _fixture_name(node: ast.AST) -> str | None:
    """The name tests request this def by, if it is a pytest fixture; else None."""
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = _dotted(call.func if call else decorator) or ""
        if target.split(".")[-1] != "fixture":
            continue
        if call:
            for keyword in call.keywords:
                if keyword.arg == "name" and _string(keyword.value):
                    return _string(keyword.value)
        return node.name
    return None


class _ModuleIndex:
    """Every name a module binds to a platform-exclusive API, plus its module-level defs."""

    def __init__(self, tree: ast.Module, helpers: frozenset[str]):
        self.helpers = helpers
        self.aliases: dict[str, str] = {}
        self.windows_names: dict[str, str] = {}
        self.posix_names: dict[str, str] = {}
        self.functions: dict[str, ast.AST] = {}
        self.constants: dict[str, ast.expr] = {}
        self.fixtures: dict[str, str] = {}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    bound = alias.asname or (alias.name if "." not in alias.name else root)
                    if alias.name in WINDOWS_SUBMODULES and alias.asname:
                        self.windows_names[bound] = alias.name
                        continue
                    if root in TRACKED_MODULES:
                        self.aliases[bound] = root
                    if root in WINDOWS_MODULES:
                        self.windows_names[bound] = root
                    elif root in POSIX_MODULES:
                        self.posix_names[bound] = root
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if node.module == "ctypes" and alias.name in WINDOWS_CTYPES_MEMBERS:
                        self.windows_names[bound] = f"ctypes.{alias.name}"
                    elif node.module == "os" and alias.name in WINDOWS_OS_MEMBERS:
                        self.windows_names[bound] = f"os.{alias.name}"
                    elif node.module == "os" and alias.name in POSIX_OS_MEMBERS:
                        self.posix_names[bound] = f"os.{alias.name}"
                    elif node.module in WINDOWS_MODULES:
                        self.windows_names[bound] = f"{node.module}.{alias.name}"
                    elif node.module in POSIX_MODULES:
                        self.posix_names[bound] = f"{node.module}.{alias.name}"

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
                fixture = _fixture_name(node)
                if fixture is not None:
                    self.fixtures[fixture] = node.name
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.constants[target.id] = node.value

    def reference(self, node: ast.AST) -> tuple[str, str] | None:
        """Return (platform, label) if this single node touches a platform-exclusive API."""
        if isinstance(node, ast.Attribute):
            base = _dotted(node.value, self.aliases)
            if base == "ctypes" and node.attr in WINDOWS_CTYPES_MEMBERS:
                return WINDOWS, f"ctypes.{node.attr}"
            if base == "os" and node.attr in WINDOWS_OS_MEMBERS:
                return WINDOWS, f"os.{node.attr}"
            if base == "os" and node.attr in POSIX_OS_MEMBERS:
                return POSIX, f"os.{node.attr}"
            if node.attr in self.helpers:
                return WINDOWS, f"secureio.{node.attr}"
            return None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in self.windows_names:
                return WINDOWS, self.windows_names[node.id]
            if node.id in self.posix_names:
                return POSIX, self.posix_names[node.id]
            if node.id in self.helpers:
                return WINDOWS, f"secureio.{node.id}"
            return None
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in WINDOWS_MODULES or alias.name in WINDOWS_SUBMODULES:
                    return WINDOWS, f"import {alias.name}"
                if root in POSIX_MODULES:
                    return POSIX, f"import {alias.name}"
            return None
        if isinstance(node, ast.ImportFrom):
            if node.module == "ctypes":
                for alias in node.names:
                    if alias.name in WINDOWS_CTYPES_MEMBERS:
                        return WINDOWS, f"from ctypes import {alias.name}"
            if node.module in WINDOWS_MODULES:
                return WINDOWS, f"from {node.module} import ..."
            if node.module in POSIX_MODULES:
                return POSIX, f"from {node.module} import ..."
        return None


# ---------------------------------------------------------------------------------------------
# control-flow helpers
# ---------------------------------------------------------------------------------------------

def _exits(statement: ast.stmt) -> bool:
    """Does this statement end the test rather than fall through to the code below it?"""
    if isinstance(statement, (ast.Return, ast.Raise)):
        return True
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        target = _dotted(statement.value.func)
        if target in {"pytest.skip", "pytest.xfail", "pytest.fail", "self.skipTest",
                      "unittest.skip", "sys.exit", "os._exit"}:
            return True
        return False
    if isinstance(statement, ast.If):
        return bool(statement.orelse) and _block_exits(statement.body) and _block_exits(
            statement.orelse)
    if isinstance(statement, ast.Try):
        cleanly = _block_exits(statement.body) or (
            bool(statement.orelse) and _block_exits(statement.orelse))
        handled = all(_block_exits(handler.body) for handler in statement.handlers)
        return (cleanly and handled) or _block_exits(statement.finalbody)
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _block_exits(statement.body)
    return False


def _block_exits(body: list[ast.stmt]) -> bool:
    return any(_exits(statement) for statement in body)


def _tolerant(handlers: list[ast.ExceptHandler]) -> bool:
    """Would this ``try`` absorb the AttributeError/ImportError a missing API raises?"""
    for handler in handlers:
        names: list[str] = []
        if handler.type is None:
            names.append("BaseException")
        elif isinstance(handler.type, ast.Tuple):
            names.extend(name for name in (_dotted(item) for item in handler.type.elts) if name)
        else:
            dotted = _dotted(handler.type)
            if dotted:
                names.append(dotted)
        if not any(name.split(".")[-1] in TOLERANT_EXCEPTIONS for name in names):
            continue
        if any(isinstance(inner, ast.Raise) for inner in handler.body):
            continue
        return True
    return False


# ---------------------------------------------------------------------------------------------
# body scanning
# ---------------------------------------------------------------------------------------------

_NESTED_BODY_STATEMENTS = (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)


class _BodyScanner:
    """Collect unprotected platform references, and unprotected module-local helper calls."""

    def __init__(self, index: _ModuleIndex):
        self.index = index
        self.found: list[_Reference] = []
        self.calls: list[tuple[str, int, frozenset]] = []

    def scan(self, body: list[ast.stmt], allowed: set[str]) -> None:
        """``allowed`` is the set of platforms this region is already restricted to."""
        allowed = set(allowed)
        for statement in body:
            if isinstance(statement, ast.If):
                kind = classify(statement.test, self.index.aliases, self.index.constants)
                self._expr(statement.test, allowed)
                if kind is None:
                    self.scan(statement.body, allowed)
                    self.scan(statement.orelse, allowed)
                    continue
                self.scan(statement.body, allowed | {kind})
                self.scan(statement.orelse, allowed | {_other(kind)})
                if _block_exits(statement.body):
                    # `if <wrong platform>: skip/return/raise` -- the rest is restricted.
                    allowed.add(_other(kind))
                continue
            if isinstance(statement, ast.Try):
                inner = allowed | {WINDOWS, POSIX} if _tolerant(statement.handlers) else allowed
                self.scan(statement.body, inner)
                self.scan(statement.orelse, inner)
                for handler in statement.handlers:
                    self.scan(handler.body, allowed)
                self.scan(statement.finalbody, allowed)
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for decorator in statement.decorator_list:
                    self._expr(decorator, allowed)
                self.scan(statement.body, allowed)
                continue
            if isinstance(statement, _NESTED_BODY_STATEMENTS):
                for field in ("iter", "test", "items"):
                    value = getattr(statement, field, None)
                    for part in (value if isinstance(value, list) else [value]):
                        if isinstance(part, ast.AST):
                            self._expr(part, allowed)
                self.scan(statement.body, allowed)
                self.scan(getattr(statement, "orelse", []), allowed)
                continue
            self._expr(statement, allowed)

    def _expr(self, node: ast.AST, allowed: set[str]) -> None:
        for child in ast.walk(node):
            self._record(child, allowed)

    def _record(self, node: ast.AST, allowed: set[str]) -> None:
        hit = self.index.reference(node)
        if hit is not None and hit[0] not in allowed:
            self.found.append(_Reference(hit[0], hit[1], getattr(node, "lineno", 0)))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in self.index.functions):
            self.calls.append((node.func.id, node.lineno, frozenset(allowed)))


# ---------------------------------------------------------------------------------------------
# guard detection
# ---------------------------------------------------------------------------------------------

#: Every spelling of "skip when this is true": pytest's lowercase ``skipif`` and unittest's
#: camelCase ``skipIf``. Missing ``skipIf`` used to reject a correctly guarded test.
_SKIP_IF_NAMES = frozenset({"skipif", "skipIf"})
_SKIP_UNLESS_NAMES = frozenset({"skipUnless", "skip_unless"})


def _condition_of(call: ast.Call) -> ast.expr | None:
    """The condition a skipif/skipUnless call carries, positionally or as ``condition=``."""
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == "condition":
            return keyword.value
    return None


def _decorator_platforms(
    decorators: list[ast.expr],
    aliases: dict[str, str],
    constants: dict[str, ast.expr] | None = None,
) -> set[str]:
    """Platforms a set of skipif/skipUnless decorators restricts execution to."""
    allowed: set[str] = set()
    for decorator in decorators:
        if not isinstance(decorator, ast.Call):
            continue
        name = (_dotted(decorator.func) or "").split(".")[-1]
        if name not in _SKIP_IF_NAMES | _SKIP_UNLESS_NAMES:
            continue
        condition = _condition_of(decorator)
        if condition is None:
            continue
        kind = classify(condition, aliases, constants)
        if kind is None:
            continue
        if name in _SKIP_IF_NAMES:
            allowed.add(_other(kind))       # skipped when true -> only the other platform runs
        else:
            allowed.add(kind)               # run only when true
    return allowed


def _pytestmark_platforms(
    nodes: list[ast.stmt],
    aliases: dict[str, str],
    constants: dict[str, ast.expr] | None = None,
) -> set[str]:
    """Platforms a module-level or class-level ``pytestmark`` restricts execution to."""
    allowed: set[str] = set()
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        value = node.value
        if value is None:
            continue
        marks = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        allowed |= _decorator_platforms(list(marks), aliases, constants)
    return allowed


def _autouse_fixture_platforms(
    tree: ast.Module,
    aliases: dict[str, str],
    constants: dict[str, ast.expr] | None = None,
) -> set[str]:
    """Platforms an autouse fixture restricts the whole module to."""
    allowed: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        autouse = any(
            isinstance(decorator, ast.Call)
            and (_dotted(decorator.func) or "").split(".")[-1] == "fixture"
            and any(keyword.arg == "autouse"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in decorator.keywords)
            for decorator in node.decorator_list
        )
        if not autouse:
            continue
        for statement in ast.walk(node):
            if isinstance(statement, ast.If) and _block_exits(statement.body):
                kind = classify(statement.test, aliases, constants)
                if kind:
                    allowed.add(_other(kind))
    return allowed


# ---------------------------------------------------------------------------------------------
# per-file analysis
# ---------------------------------------------------------------------------------------------

def _scan_function(node: ast.AST, index: _ModuleIndex, allowed: set[str]) -> _BodyScanner:
    scanner = _BodyScanner(index)
    scanner.scan(
        node.body,
        allowed | _decorator_platforms(node.decorator_list, index.aliases, index.constants),
    )
    return scanner


def _requested_fixtures(node: ast.AST, index: _ModuleIndex) -> list[tuple[str, int]]:
    """Module-level fixtures this def requests by parameter name.

    A fixture is never *called* -- pytest wires it in by name -- so the call graph alone cannot
    see it. A Windows-only handle built in a fixture and consumed by ten tests would otherwise be
    invisible, and a fixture is a natural place to put exactly that setup.
    """
    arguments = node.args
    parameters = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    return [
        (index.fixtures[parameter.arg], node.lineno)
        for parameter in parameters
        if parameter.arg in index.fixtures and index.fixtures[parameter.arg] != node.name
    ]


def _resolve_helpers(index: _ModuleIndex) -> dict[str, list[_Reference]]:
    """Unprotected references reachable from each module-level def, transitively."""
    scanned = {
        name: _scan_function(node, index, set())
        for name, node in index.functions.items()
    }
    edges = {
        name: [*scanner.calls,
               *((callee, line, frozenset())
                 for callee, line in _requested_fixtures(index.functions[name], index))]
        for name, scanner in scanned.items()
    }
    effective = {name: list(scanner.found) for name, scanner in scanned.items()}
    seen = {name: {reference.key() for reference in refs} for name, refs in effective.items()}

    changed = True
    while changed:
        changed = False
        for name in scanned:
            for callee, line, allowed in edges[name]:
                if callee == name or callee not in effective:
                    continue
                for reference in list(effective[callee]):
                    if reference.platform in allowed or name in reference.via:
                        continue
                    promoted = reference.through(callee, line)
                    if promoted.key() in seen[name]:
                        continue
                    seen[name].add(promoted.key())
                    effective[name].append(promoted)
                    changed = True
    return effective


def _test_functions(tree: ast.Module, index: _ModuleIndex):
    """Yield (qualified name, node, platforms the class/module scope already restricts to)."""
    aliases, constants = index.aliases, index.constants
    module_allowed = (_pytestmark_platforms(tree.body, aliases, constants)
                      | _autouse_fixture_platforms(tree, aliases, constants))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                yield node.name, node, module_allowed
        elif isinstance(node, ast.ClassDef):
            class_allowed = (module_allowed
                             | _pytestmark_platforms(node.body, aliases, constants)
                             | _decorator_platforms(node.decorator_list, aliases, constants))
            for child in node.body:
                if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name.startswith("test")):
                    yield f"{node.name}.{child.name}", child, class_allowed


def _collection_time_violations(tree: ast.Module, index: _ModuleIndex, name: str) -> list[str]:
    """Platform references evaluated at IMPORT time, which no skip marker can prevent.

    A module-level ``import msvcrt`` on POSIX is a collection error: pytest never gets as far as
    consulting ``pytestmark``, the whole module errors out, and with ``-x`` the run stops there.
    This is the single worst shape of the WAVE 2 failure and it is invisible to any check that
    only looks inside test bodies. ``if os.name == "nt": import msvcrt`` and a tolerant
    ``try/except ImportError`` are both real guards here and are honoured, because the module
    body is scanned with the same control-flow analysis as a function body.
    """
    body = [
        statement for statement in tree.body
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    scanner = _BodyScanner(index)
    scanner.scan(body, set())
    # Decorators run at import time too, so a platform API in a decorator ARGUMENT -- say a
    # @pytest.mark.parametrize list built from ctypes.WinDLL -- is evaluated on every host no
    # matter what skip marker sits beside it.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                scanner._expr(decorator, set())
    return [
        f"{name}::<module body> {reference.describe()} evaluated at import time, where no "
        "skipif/pytestmark can run -- guard the import with `if os.name == ...:`, a tolerant "
        "try/except ImportError, or pytest.importorskip"
        for reference in sorted({r.key(): r for r in scanner.found}.values(),
                                key=lambda r: (r.line, r.label))
    ]


def analyze(path: Path, helpers: frozenset[str]) -> list[str]:
    """Return one human-readable violation string per unguarded platform reference."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    index = _ModuleIndex(tree, helpers)
    reachable = _resolve_helpers(index)

    violations: list[str] = _collection_time_violations(tree, index, path.name)
    for name, node, scope_allowed in _test_functions(tree, index):
        if (path.name, name) in ALLOWLIST:
            continue
        scanner = _scan_function(node, index, scope_allowed)
        found = list(scanner.found)
        own = scope_allowed | _decorator_platforms(
            node.decorator_list, index.aliases, index.constants)
        for callee, line in _requested_fixtures(node, index):
            for reference in reachable.get(callee, ()):
                if reference.platform not in own:
                    found.append(reference.through(callee, line))
        for callee, line, allowed in scanner.calls:
            for reference in reachable.get(callee, ()):
                if reference.platform not in allowed:
                    found.append(reference.through(callee, line))
        for reference in sorted({r.key(): r for r in found}.values(),
                                key=lambda r: (r.line, r.label)):
            violations.append(f"{path.name}::{name} {reference.describe()}")
    return violations


def scan_directory(directory: Path) -> list[str]:
    helpers = windows_helper_names()
    violations: list[str] = []
    for path in sorted(directory.glob("test_*.py")):
        violations.extend(analyze(path, helpers))
    return violations


# ---------------------------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------------------------

def test_no_test_reaches_a_platform_exclusive_api_unguarded():
    """Every Windows-only / POSIX-only API must be unreachable from a host that lacks it."""
    violations = scan_directory(TEST_DIR)
    assert not violations, (
        "Platform-exclusive API reachable from a test that is not restricted to that platform.\n"
        "Add @pytest.mark.skipif(os.name != 'nt', reason=...) (or the POSIX inverse), a "
        "module-level pytestmark, an autouse fixture that skips, or an in-body platform check:\n"
        "  " + "\n  ".join(violations)
    )


def test_guard_covers_the_whole_suite_and_a_real_helper_set():
    """A sweep that silently inspected nothing would pass vacuously; prove it does not."""
    modules = sorted(TEST_DIR.glob("test_*.py"))
    assert len(modules) > 50, modules
    helpers = windows_helper_names()
    assert "_windows_current_user_sid" in helpers, sorted(helpers)
    assert "_windows_private_descriptor" in helpers, sorted(helpers)
    assert all(name.startswith("_windows_") for name in helpers), sorted(helpers)
    # The corpus really does contain the APIs this guard exists for, so a detector that
    # matched nothing at all could not be mistaken for a clean tree.
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in modules)
    for needle in ("ctypes.WinDLL", "ctypes.get_last_error", "msvcrt", "os.geteuid"):
        assert needle in corpus, needle


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "import ctypes\ndef test_x():\n    ctypes.WinDLL('kernel32')\n",
            ["ctypes.WinDLL"],
            id="bare-windll",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport pytest\n"
            "@pytest.mark.skipif(os.name != 'nt', reason='win')\n"
            "def test_x():\n    ctypes.WinDLL('kernel32')\n",
            [],
            id="skipif-correct-direction",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport pytest\n"
            "@pytest.mark.skipif(os.name == 'nt', reason='inverted')\n"
            "def test_x():\n    ctypes.WinDLL('kernel32')\n",
            ["ctypes.WinDLL"],
            id="skipif-inverted-direction-is-not-a-guard",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport pytest\n"
            "@pytest.mark.skipif(os.name != 'nt', reason='win')\n"
            "def test_guarded():\n    ctypes.WinDLL('kernel32')\n"
            "def test_neighbour():\n    ctypes.WinDLL('kernel32')\n",
            ["ctypes.WinDLL"],
            id="decorator-does-not-leak-to-the-next-function",
        ),
        pytest.param(
            "import ctypes\ndef _helper():\n    ctypes.WinDLL('kernel32')\n"
            "def test_x():\n    _helper()\n",
            ["ctypes.WinDLL"],
            id="module-helper-is-followed",
        ),
        pytest.param(
            "import ctypes\ndef _inner():\n    ctypes.WinDLL('kernel32')\n"
            "def _outer():\n    _inner()\n"
            "def test_x():\n    _outer()\n",
            ["ctypes.WinDLL"],
            id="module-helper-is-followed-transitively",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport pytest\n"
            "def _helper():\n"
            "    if os.name != 'nt':\n        pytest.skip('win only')\n"
            "    ctypes.WinDLL('kernel32')\n"
            "def test_x():\n    _helper()\n",
            [],
            id="helper-skips-itself",
        ),
        pytest.param(
            "import ctypes\nimport os\n"
            "def _helper():\n    ctypes.WinDLL('kernel32')\n"
            "def test_x():\n"
            "    if os.name == 'nt':\n        _helper()\n",
            [],
            id="helper-called-only-inside-a-windows-branch",
        ),
        pytest.param(
            "import os\nimport pytest\n"
            "pytestmark = pytest.mark.skipif(os.name != 'nt', reason='win')\n"
            "def test_x():\n    import msvcrt\n    msvcrt.get_osfhandle(0)\n",
            [],
            id="module-pytestmark",
        ),
        pytest.param(
            # pytestmark CANNOT save a module-level import: on POSIX this is a COLLECTION error
            # ("ModuleNotFoundError ... Interrupted: 1 error during collection"), which happens
            # before any marker is consulted. Verified against a real pytest run.
            "import os\nimport pytest\n"
            "pytestmark = pytest.mark.skipif(os.name != 'nt', reason='win')\n"
            "import msvcrt\ndef test_x():\n    msvcrt.get_osfhandle(0)\n",
            ["import msvcrt"],
            id="module-level-import-is-not-saved-by-pytestmark",
        ),
        pytest.param(
            "import os\nif os.name == 'nt':\n    import msvcrt\n"
            "def test_x():\n    assert os.name\n",
            [],
            id="module-level-import-inside-a-platform-branch",
        ),
        pytest.param(
            "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n"
            "def test_x():\n    assert True\n",
            [],
            id="module-level-import-in-a-tolerant-try",
        ),
        pytest.param(
            "import ctypes.wintypes as wt\ndef test_x():\n    wt.DWORD(0)\n",
            ["import ctypes.wintypes", "ctypes.wintypes"],
            id="aliased-ctypes-submodule-import",
        ),
        pytest.param(
            "import os\nimport pytest\n"
            "class TestGroup:\n"
            "    pytestmark = pytest.mark.skipif(os.name != 'nt', reason='win')\n"
            "    def test_x(self):\n        os.startfile('x')\n",
            [],
            id="class-pytestmark",
        ),
        pytest.param(
            "import os\ndef test_x():\n    os.geteuid()\n",
            ["os.geteuid"],
            id="bare-geteuid",
        ),
        pytest.param(
            "import os as _os\ndef test_x():\n    _os.geteuid()\n",
            ["os.geteuid"],
            id="aliased-os-still-resolves",
        ),
        pytest.param(
            "import os as _os\nimport ctypes\n"
            "def test_x():\n"
            "    if _os.name != 'nt':\n"
            "        try:\n            return True\n        except OSError:\n            return\n"
            "    ctypes.WinDLL('kernel32')\n",
            [],
            id="early-exit-guard-with-nested-returns",
        ),
        pytest.param(
            "import os\nimport sys\ndef test_x():\n"
            "    if sys.platform != 'win32':\n        os.geteuid()\n",
            [],
            id="inline-posix-branch",
        ),
        pytest.param(
            "import os\nimport ctypes\ndef test_x():\n"
            "    if sys.platform != 'darwin':\n        ctypes.WinDLL('kernel32')\n",
            ["ctypes.WinDLL"],
            id="non-windows-platform-value-does-not-imply-windows",
        ),
        pytest.param(
            "def test_x():\n"
            "    try:\n        import fcntl\n    except ImportError:\n        return\n",
            [],
            id="tolerant-try",
        ),
        pytest.param(
            "def test_x():\n"
            "    try:\n        import fcntl\n    except KeyError:\n        return\n",
            ["import fcntl"],
            id="unrelated-except-is-not-a-guard",
        ),
        pytest.param(
            "def test_x(monkeypatch, m):\n"
            "    monkeypatch.setattr(m, '_windows_current_user_sid', lambda: 'S-1')\n",
            [],
            id="string-attribute-name-is-not-a-reference",
        ),
        pytest.param(
            "import secureio\ndef test_x():\n    secureio._windows_current_user_sid()\n",
            ["secureio._windows_current_user_sid"],
            id="repo-windows-helper-is-a-marker",
        ),
        pytest.param(
            "import subprocess, sys\n"
            "def test_x():\n"
            "    subprocess.run([sys.executable, '-c', 'import msvcrt; msvcrt.locking(0,1,1)'])\n",
            [],
            id="subprocess-script-payload-is-not-a-reference",
        ),
        pytest.param(
            "import os\nimport pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def _only_windows():\n"
            "    if os.name != 'nt':\n        pytest.skip('win only')\n"
            "def test_x():\n    import msvcrt\n    msvcrt.get_osfhandle(0)\n",
            [],
            id="autouse-fixture-skip",
        ),
        pytest.param(
            # A fixture is wired in by parameter name, never called, so the call graph is blind
            # to it unless fixture parameters are followed.
            "import ctypes\nimport pytest\n"
            "@pytest.fixture\ndef win_handle():\n    return ctypes.WinDLL('kernel32')\n"
            "def test_x(win_handle):\n    assert win_handle\n",
            ["ctypes.WinDLL"],
            id="requested-fixture-is-followed",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport pytest\n"
            "@pytest.fixture\ndef win_handle():\n"
            "    if os.name != 'nt':\n        pytest.skip('win only')\n"
            "    return ctypes.WinDLL('kernel32')\n"
            "def test_x(win_handle):\n    assert win_handle\n",
            [],
            id="requested-fixture-that-skips-itself",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport pytest\n"
            "IS_WINDOWS = os.name == 'nt'\n"
            "@pytest.mark.skipif(not IS_WINDOWS, reason='win')\n"
            "def test_x():\n    ctypes.WinDLL('kernel32')\n",
            [],
            id="hoisted-constant-is-a-real-guard",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport pytest\n"
            "@pytest.mark.skipif(condition=os.name != 'nt', reason='win')\n"
            "def test_x():\n    ctypes.WinDLL('kernel32')\n",
            [],
            id="skipif-condition-keyword",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport pytest\n"
            '@pytest.mark.skipif("os.name != \'nt\'", reason=\'win\')\n'
            "def test_x():\n    ctypes.WinDLL('kernel32')\n",
            [],
            id="skipif-string-condition",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport unittest\n"
            "@unittest.skipIf(os.name != 'nt', 'win')\n"
            "def test_x():\n    ctypes.WinDLL('kernel32')\n",
            [],
            id="unittest-skipIf-camelcase",
        ),
        pytest.param(
            "import ctypes\nimport os\nimport pytest\n"
            "IS_WINDOWS = os.name == 'nt'\n"
            "@pytest.mark.skipif(IS_WINDOWS, reason='inverted')\n"
            "def test_x():\n    ctypes.WinDLL('kernel32')\n",
            ["ctypes.WinDLL"],
            id="hoisted-constant-inverted-is-still-not-a-guard",
        ),
        pytest.param(
            "import os\nimport unittest\n"
            "@unittest.skipUnless(hasattr(os, 'fork'), 'posix')\n"
            "class TestGroup(unittest.TestCase):\n"
            "    def test_x(self):\n        os.fork()\n",
            [],
            id="unittest-skipunless-on-the-class",
        ),
        pytest.param(
            "import ctypes\ndef test_x():\n"
            "    def _probe():\n        return ctypes.WinDLL('kernel32')\n"
            "    return _probe\n",
            ["ctypes.WinDLL"],
            id="nested-closure-body-still-counts",
        ),
    ],
)
def test_guard_classifies_synthetic_modules(tmp_path, source, expected):
    """Exercise the detector itself on hand-built modules -- guards and anti-guards alike."""
    module = tmp_path / "test_synthetic.py"
    module.write_text(source, encoding="utf-8")
    findings = analyze(module, windows_helper_names())
    labels = [finding.split(": ", 1)[1].split(" [")[0] for finding in findings]
    assert labels == expected, findings


def test_guard_is_not_fooled_by_the_word_skipif_elsewhere_in_the_file(tmp_path):
    """A skipif in prose, or on a neighbouring function, must not protect the offender."""
    module = tmp_path / "test_textual.py"
    module.write_text(
        "import ctypes\nimport os\nimport pytest\n"
        "def test_offender():\n"
        "    '''pytest.mark.skipif(os.name != \"nt\") -- named in prose only.'''\n"
        "    ctypes.WinDLL('kernel32')\n",
        encoding="utf-8",
    )
    findings = analyze(module, windows_helper_names())
    assert len(findings) == 1, findings
    assert "test_offender" in findings[0] and "ctypes.WinDLL" in findings[0], findings


def test_scan_is_pure_static_analysis():
    """The guard must never import the modules it inspects, so it is safe on any host."""
    assert os.name in {"nt", "posix"}
    assert "msvcrt" not in globals() and "fcntl" not in globals()
