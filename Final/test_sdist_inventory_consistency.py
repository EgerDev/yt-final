"""Pin the three hand-maintained sdist inventories to each other and to disk.

Every ``Final/*.py`` file is listed by hand in three separate places:

  * ``Final/test_release_packaging.py``  -> ``SDIST_FINAL_PYTHON_MEMBERS``
  * ``Final/test_verify_release.py``     -> ``SDIST_FINAL_PYTHON``
  * ``scripts/verify_release.py``        -> ``_SDIST_FINAL_PYTHON``

The hand-maintenance is deliberate: the production list in ``verify_release.py``
is the reviewed module boundary a release is verified against, and deriving it
from whatever happens to be on disk would make that check tautological. What was
NOT deliberate is that only the first of the three was ever compared against
disk, and that comparison
(``test_release_sdist_contains_exact_supported_source_boundary``) sits behind the
``release_distributions`` fixture, which builds an sdist. So an ordinary test run
never exercised it, and adding one module to ``Final/`` could fail a single
build-gated test, get "fixed" in a single list, and leave the other two stale
until a release run blew up.

That is not hypothetical: three inventories were found missing the same eight
modules at once.

These tests are deliberately cheap - no fixture, no build, no subprocess - so the
drift is caught by any run of the suite, on any platform, in milliseconds.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import scripts.verify_release as verifier

try:  # pytest's rootdir decides whether Final/ is a package or the rootdir
    from test_release_packaging import SDIST_FINAL_PYTHON_MEMBERS
    from test_verify_release import SDIST_FINAL_PYTHON
except ImportError:  # pragma: no cover - exercised by the other import path
    from Final.test_release_packaging import (  # type: ignore
        SDIST_FINAL_PYTHON_MEMBERS,
    )
    from Final.test_verify_release import SDIST_FINAL_PYTHON  # type: ignore


FINAL_DIR = Path(__file__).resolve().parent

INVENTORIES = (
    (
        "Final/test_release_packaging.py::SDIST_FINAL_PYTHON_MEMBERS",
        frozenset(SDIST_FINAL_PYTHON_MEMBERS),
    ),
    (
        "Final/test_verify_release.py::SDIST_FINAL_PYTHON",
        frozenset(SDIST_FINAL_PYTHON),
    ),
    (
        "scripts/verify_release.py::_SDIST_FINAL_PYTHON",
        frozenset(verifier._SDIST_FINAL_PYTHON),
    ),
)

_IDS = ["release_packaging", "verify_release_test", "verify_release_script"]


def _modules_on_disk() -> frozenset[str]:
    """The same glob the build-gated boundary test uses, so verdicts agree."""
    return frozenset(f"Final/{path.name}" for path in FINAL_DIR.glob("*.py"))


def _remedy(site: str, missing: frozenset[str], extra: frozenset[str]) -> str:
    lines = [f"{site} is out of sync with Final/ on disk."]
    if missing:
        lines.append(
            f"  ADD these {len(missing)} entr(y/ies): "
            + ", ".join(sorted(missing))
        )
    if extra:
        lines.append(
            f"  REMOVE these {len(extra)} stale entr(y/ies): "
            + ", ".join(sorted(extra))
        )
    lines.append(
        "  A new Final/*.py must be added to ALL THREE inventories listed in "
        "this module's docstring - a release fails otherwise."
    )
    return "\n".join(lines)


@pytest.mark.parametrize("site,inventory", INVENTORIES, ids=_IDS)
def test_inventory_matches_final_directory(site: str, inventory: frozenset) -> None:
    """Each inventory lists exactly the modules that exist in Final/."""
    disk = _modules_on_disk()
    missing = disk - inventory
    extra = inventory - disk
    assert not (missing or extra), _remedy(site, missing, extra)


def test_all_three_inventories_agree_with_each_other() -> None:
    """Catch the drift directly, so the failure names both disagreeing sites.

    The per-inventory test above already implies this, but when several lists
    are stale at once its failures read as three unrelated problems. This one
    states the actual invariant.
    """
    reference_site, reference = INVENTORIES[0]
    for site, inventory in INVENTORIES[1:]:
        only_reference = reference - inventory
        only_other = inventory - reference
        assert not (only_reference or only_other), (
            f"{site} disagrees with {reference_site}.\n"
            f"  only in {reference_site}: "
            f"{', '.join(sorted(only_reference)) or '-'}\n"
            f"  only in {site}: {', '.join(sorted(only_other)) or '-'}"
        )


def _imported_scripts_modules(source: str) -> set[str]:
    """Every `scripts.<name>` a module imports, at any nesting depth."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("scripts."):
                    found.add(alias.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("scripts."):
                found.add(node.module.split(".")[1])
    return found


def test_shipped_modules_only_import_scripts_that_also_ship() -> None:
    """A module in the sdist must not import a `scripts/` file the sdist omits.

    `Final/*.py` ships wholesale, but `scripts/` does not: MANIFEST.in names
    four files explicitly, so a new one is repo-only until it is added. A
    shipped test that imports a repo-only script raises ModuleNotFoundError
    during COLLECTION, which aborts the entire module rather than failing one
    test -- and it cannot reproduce in this checkout, where the file is present.

    This is not hypothetical. `test_release_documentation.py` was given a
    module-level `from scripts.make_public_tree import ...` while
    `make_public_tree.py` was repo-only, and every test in that file stopped
    collecting from an extracted sdist.
    """
    shipped_scripts = {
        member.split("/", 1)[1]
        for member in verifier._SDIST_STATIC
        if member.startswith("scripts/")
    }
    offenders = []
    for member in sorted(verifier._SDIST_FINAL_PYTHON):
        path = FINAL_DIR.parent / member
        if not path.is_file():
            continue
        for name in sorted(_imported_scripts_modules(path.read_text(encoding="utf-8"))):
            if f"{name}.py" not in shipped_scripts:
                offenders.append(f"{member} imports scripts.{name}")
    assert not offenders, (
        "these shipped modules import scripts/ files that are not in the "
        "source distribution, so they fail to import from an extracted "
        "sdist:\n  " + "\n  ".join(offenders)
        + "\n\nAdd the script to MANIFEST.in and all three static inventories, "
        "or drop the import."
    )


def test_this_guard_is_itself_inventoried() -> None:
    """The guard must obey the rule it enforces, or it silently breaks releases."""
    own_entry = f"Final/{Path(__file__).name}"
    for site, inventory in INVENTORIES:
        assert own_entry in inventory, (
            f"{own_entry} is absent from {site}. This test module is part of "
            "Final/ and ships in the sdist like every other module."
        )


def test_no_shipped_module_has_an_invalid_string_escape():
    r"""A stray backslash in a docstring is a DeprecationWarning today and a SyntaxError later.

    Python has been warning about unrecognised escapes (`\W`, `\y`, ...) for several releases and
    intends to make them errors. They are easy to introduce without noticing: writing a Windows path
    into a docstring, or losing one level of doubling when a file is edited through a shell heredoc,
    both produce one silently.

    Compiling every shipped module here catches them at the same moment as the inventory checks,
    rather than as a warning nobody reads at the bottom of a green run.
    """
    import warnings

    offenders = []
    for member in sorted(verifier._SDIST_FINAL_PYTHON):
        path = FINAL_DIR.parent / member
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                compile(source, str(path), "exec")
            except SyntaxError as error:            # pragma: no cover - a real syntax error
                offenders.append(f"{member}: {error}")
                continue
            for entry in caught:
                if "invalid escape" in str(entry.message):
                    offenders.append(f"{member}:{entry.lineno}: {entry.message}")
    assert not offenders, "invalid string escapes in shipped modules:\n  " + "\n  ".join(offenders)
