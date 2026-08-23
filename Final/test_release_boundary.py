"""Release-boundary checks for archived non-product material.

`archive/` holds material that must never reach a release artifact: historical
prototypes, legacy one-off scripts, and planning documents. These checks prove
it stays quarantined.

A published checkout does not carry `archive/` at all -- it is stripped by
`scripts/make_public_tree.py`. The quarantine checks then have no subject, so
this module measures whether the tree it is running against actually has one
instead of assuming it does. That keeps this file byte-identical in the private
and published trees, which is what stops the two from drifting apart. The
invariant that still matters everywhere -- that shipping documents never point
at `archive/` -- is enforced in `test_release_documentation.py`, which runs in
both.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]

ARCHIVE_DIR = ROOT / "archive"

pytestmark = pytest.mark.skipif(
    not ARCHIVE_DIR.is_dir(),
    reason=(
        "no archive/ in this tree: a published checkout strips it, so the "
        "quarantine checks have nothing to quarantine"
    ),
)

def _load_manifest():
    """Load `archive/boundary_manifest.py` by path, or return empty tuples.

    The inventory of quarantined filenames is disclosive on its own -- it names
    the prototype and the internal planning documents being withheld -- so it
    lives inside `archive/` and is stripped with it. Loading it by path rather
    than importing `archive.boundary_manifest` keeps this module free of any
    package-level dependency on a directory that will not be there, so the file
    stays byte-identical in the development and published trees.

    When `archive/` is absent every list is empty, every parametrised case
    collapses to nothing, and `pytestmark` skips the module anyway.
    """
    manifest_path = ARCHIVE_DIR / "boundary_manifest.py"
    if not manifest_path.is_file():
        return SimpleNamespace(
            LEGACY_FILENAMES=(),
            LINUX_PROTOTYPE_FILENAMES=(),
            LAUNCH_PLAN_FILENAMES=(),
            RESEARCH_FILENAME="",
            PRE_QUARANTINE_PREFIXES=(),
            ARCHIVED_PLAN_DIR="",
        )
    spec = importlib.util.spec_from_file_location(
        "_yt_final_archive_boundary_manifest", manifest_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MANIFEST = _load_manifest()

LEGACY_FILENAMES = _MANIFEST.LEGACY_FILENAMES
LINUX_PROTOTYPE_FILENAMES = _MANIFEST.LINUX_PROTOTYPE_FILENAMES
LAUNCH_PLAN_FILENAMES = _MANIFEST.LAUNCH_PLAN_FILENAMES
RESEARCH_FILENAME = _MANIFEST.RESEARCH_FILENAME
PRE_QUARANTINE_PREFIXES = _MANIFEST.PRE_QUARANTINE_PREFIXES
ARCHIVED_PLAN_DIR = getattr(_MANIFEST, "ARCHIVED_PLAN_DIR", "")

_PROTOTYPE_PREFIX, _LAUNCH_PLAN_PREFIX = (
    PRE_QUARANTINE_PREFIXES if PRE_QUARANTINE_PREFIXES else ("", "")
)

ARCHIVED_MOVED_FILES = (
    *(
        (
            Path(_PROTOTYPE_PREFIX) / filename,
            Path("archive") / _PROTOTYPE_PREFIX / filename,
        )
        for filename in LINUX_PROTOTYPE_FILENAMES
    ),
    *(
        (
            Path(_LAUNCH_PLAN_PREFIX) / filename,
            Path(ARCHIVED_PLAN_DIR) / filename,
        )
        for filename in LAUNCH_PLAN_FILENAMES
    ),
    *(
        (
            Path(filename),
            Path("archive/legacy-tools") / filename,
        )
        for filename in LEGACY_FILENAMES
    ),
    (
        Path(RESEARCH_FILENAME),
        Path("archive/research") / RESEARCH_FILENAME,
    ),
)

EXPECTED_ARCHIVE_FILES = frozenset(
    (
        Path("archive/README.md"),
        # The inventory of what is quarantined lives inside the quarantine, so
        # that publishing the tree does not publish an index of the withheld
        # material. See archive/boundary_manifest.py.
        Path("archive/boundary_manifest.py"),
        *(destination for _source, destination in ARCHIVED_MOVED_FILES),
    )
)


@pytest.mark.parametrize(
    ("source", "destination"),
    ARCHIVED_MOVED_FILES,
    ids=lambda value: str(value).replace("\\", "/"),
)
def test_every_moved_file_has_exact_archive_destination(
    source: Path,
    destination: Path,
):
    assert not os.path.lexists(ROOT / source)
    assert (ROOT / destination).is_file()


def test_archive_inventory_has_every_reviewed_file_and_no_extras():
    assert len(LAUNCH_PLAN_FILENAMES) == 15
    assert len(LINUX_PROTOTYPE_FILENAMES) == 12
    assert len(LEGACY_FILENAMES) == 21
    assert len(ARCHIVED_MOVED_FILES) == 49
    assert len(EXPECTED_ARCHIVE_FILES) == 51

    actual = frozenset(
        path.relative_to(ROOT)
        for path in (ROOT / "archive").rglob("*")
        # Importing the manifest by path leaves a __pycache__ beside it; that is
        # a build artifact of running this suite, not archived material.
        if path.is_file() and "__pycache__" not in path.parts
    )
    missing = sorted(EXPECTED_ARCHIVE_FILES - actual)
    unexpected = sorted(actual - EXPECTED_ARCHIVE_FILES)
    assert not missing, f"missing archived files: {missing}"
    assert not unexpected, f"unexpected archived files: {unexpected}"


def test_archive_notice_states_all_release_boundary_obligations():
    notice = (ROOT / "archive" / "README.md").read_text(
        encoding="utf-8"
    ).lower()
    required_statements = (
        "preserved for historical and reference use",
        "unsupported and outside `yt_final 1.0.0`",
        "outside all 1.0.0 security claims",
        "excluded from release artifacts",
        "excluded from the authoritative test suite",
        "never imported, executed, or depended upon by `final/`",
    )
    for statement in required_statements:
        assert statement in notice


def requires_git_checkout():
    """Skip when ROOT is not a git working tree.

    These tests assert on REPOSITORY state, so they can only run inside a checkout. The source also
    ships as an sdist and as a portable zip, where `git` returns 128 ("not a git repository") and the
    assertions below fail for a reason that has nothing to do with the contract they protect.

    This is the second layer of the same defect. The first was asserting that a gitignored,
    machine-local file must EXIST, which reddened the suite on every clean checkout. Removing that
    left the git shell-out behind — and the comment written at the time claimed environment
    independence on the strength of `--no-index`, which only makes the FILE optional, not the
    repository. Verified: `git check-ignore -q --no-index <path>` in a non-repo directory exits 128.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip("not a git checkout — repository-state assertions do not apply here")


def test_local_settings_has_exact_literal_ignore_rule():
    requires_git_checkout()
    ignore_lines = (ROOT / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines()
    assert ".claude/settings.local.json" in ignore_lines
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "-q",
            "--no-index",
            ".claude/settings.local.json",
        ],
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0
    # Deliberately NOT asserting the file exists. It is machine-local and gitignored, so it is absent
    # on a fresh clone, in CI, and on any machine whose tooling has not written one yet — asserting
    # presence made the whole suite red for exactly the environments this test is supposed to protect.
    # `--no-index` is what makes the FILE optional; the repository is still required, which is why
    # this test is gated by requires_git_checkout() above.


def test_local_settings_is_untracked():
    requires_git_checkout()
    tracked = subprocess.run(
        ["git", "ls-files", "--", ".claude/settings.local.json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout == ""


def test_historical_launch_plan_stays_quarantined_in_the_archive():
    """The launch plan lives in archive/, and its pre-quarantine path stays dead.

    This used to additionally REQUIRE README.md and CHANGELOG.md to link to the
    plan. That forced the two most public-facing documents to point at material
    that is stripped before publication, so every published tree had to edit
    them back out and the trees could never be byte-identical. Shipping docs are
    now required NOT to reference archive/ at all
    (`test_release_documentation.py::test_shipping_docs_never_reference_stripped_material`).
    """
    plan_root = ROOT / ARCHIVED_PLAN_DIR
    assert all((plan_root / name).is_file() for name in LAUNCH_PLAN_FILENAMES)
    for document in ("README.md", "CHANGELOG.md"):
        text = (ROOT / document).read_text(encoding="utf-8")
        assert f"{_LAUNCH_PLAN_PREFIX}/" not in text


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_archived_reference(value: str) -> bool:
    normalized = value.strip().replace("\\", "/").lower()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    module_name = normalized.replace("/", ".")
    archived_source_prefixes = PRE_QUARANTINE_PREFIXES
    if (
        normalized == "archive"
        or normalized.startswith("archive/")
        or "/archive/" in normalized
        or normalized.endswith("/archive")
        or module_name == "archive"
        or module_name.startswith("archive.")
        or any(
            normalized == prefix
            or normalized.startswith(prefix + "/")
            or f"/{prefix}/" in normalized
            or normalized.endswith("/" + prefix)
            for prefix in archived_source_prefixes
        )
        or normalized == RESEARCH_FILENAME.lower()
        or normalized.endswith("/" + RESEARCH_FILENAME.lower())
    ):
        return True
    legacy_filenames = {filename.lower() for filename in LEGACY_FILENAMES}
    if normalized.rsplit("/", 1)[-1] in legacy_filenames:
        return True
    legacy_modules = {Path(filename).stem.lower() for filename in LEGACY_FILENAMES}
    return any(
        module_name == stem or module_name.startswith(stem + ".")
        for stem in legacy_modules
    )


_LITERAL_PATH_BUILDERS = {
    "Path",
    "PurePath",
    "PurePosixPath",
    "PureWindowsPath",
}


def _simple_literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and _call_name(node.func) in _LITERAL_PATH_BUILDERS
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def test_final_runtime_has_no_static_or_literal_dynamic_archive_imports():
    dynamic_loaders = {
        "__import__",
        "import_module",
        "resolve_name",
        "run_module",
        "run_path",
        "spec_from_file_location",
        "SourceFileLoader",
        "SourcelessFileLoader",
    }
    violations: list[str] = []
    for path in sorted((ROOT / "Final").rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_archived_reference(alias.name):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: "
                            f"import {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module and _is_archived_reference(node.module):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: "
                        f"from {node.module}"
                    )
                elif node.module is None:
                    for alias in node.names:
                        if _is_archived_reference(alias.name):
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno}: "
                                f"relative import {alias.name}"
                            )
            elif (
                isinstance(node, ast.Call)
                and _call_name(node.func) in dynamic_loaders
            ):
                literal_arguments = [
                    value
                    for argument in (
                        *node.args,
                        *(keyword.value for keyword in node.keywords),
                    )
                    if (value := _simple_literal_string(argument)) is not None
                ]
                for value in literal_arguments:
                    if _is_archived_reference(value):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: "
                            f"literal dynamic load {value!r}"
                        )
    assert violations == []


def _sample_archived_references() -> tuple[str, ...]:
    """Eight sample references, or nothing at all when the manifest is absent.

    Built from the manifest rather than written out here: spelling them
    literally would restore the very inventory `archive/boundary_manifest.py`
    exists to keep out of a published tree.

    All-or-nothing on purpose. An earlier version filtered per item with
    `if reference`, which looked equivalent but was not: with an empty manifest
    `f"archive/research/{RESEARCH_FILENAME}"` is `"archive/research/"`, which is
    truthy, so the tuple came back with one element and the eight-way unpack
    below raised `ValueError` during COLLECTION -- aborting the module in the
    published tree, where `pytestmark` was about to skip it anyway. It could not
    reproduce in the development tree, where the manifest is present.
    """
    if not (LINUX_PROTOTYPE_FILENAMES and LAUNCH_PLAN_FILENAMES
            and LEGACY_FILENAMES and RESEARCH_FILENAME):
        return ()
    prototype_sample = LINUX_PROTOTYPE_FILENAMES[8]
    plan_sample = LAUNCH_PLAN_FILENAMES[13]
    legacy_sample = LEGACY_FILENAMES[7]
    return (
        f"{_PROTOTYPE_PREFIX}/{prototype_sample}",
        f"{_LAUNCH_PLAN_PREFIX}/{plan_sample}",
        RESEARCH_FILENAME,
        f"archive/{_PROTOTYPE_PREFIX}/{prototype_sample}",
        f"{ARCHIVED_PLAN_DIR}/{plan_sample}",
        f"archive/research/{RESEARCH_FILENAME}",
        legacy_sample,
        f"archive/legacy-tools/{legacy_sample}",
    )


_SAMPLE_ARCHIVED_REFERENCES = _sample_archived_references()


@pytest.mark.parametrize("reference", _SAMPLE_ARCHIVED_REFERENCES)
def test_archive_reference_classifier_covers_old_and_new_exact_paths(reference):
    assert _is_archived_reference(reference)


def _run_runtime_guard_on_snippet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    final = tmp_path / "Final"
    final.mkdir()
    (final / "runtime_probe.py").write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    test_final_runtime_has_no_static_or_literal_dynamic_archive_imports()


#: Loader snippets are BUILT from the manifest for the same reason the
#: classifier cases above are: a literal loader call naming a withheld file
#: shipping module republishes the inventory this whole arrangement exists to
#: withhold. `_SAMPLE_ARCHIVED_REFERENCES` supplies the paths; these are the
#: syntactic shapes the guard must catch for each of them.
def _loader_snippets() -> tuple[str, ...]:
    if not _SAMPLE_ARCHIVED_REFERENCES:
        return ()
    pre_prototype, pre_plan, research, prototype, plan, archived_research, \
        legacy, archived_legacy = _SAMPLE_ARCHIVED_REFERENCES
    windows_legacy = archived_legacy.replace("/", "\\")
    return (
        f"import runpy\nrunpy.run_path({pre_prototype!r})\n",
        f"import runpy\nrunpy.run_path({pre_plan!r})\n",
        f"import runpy\nrunpy.run_path({research!r})\n",
        f"import runpy\nrunpy.run_path({prototype!r})\n",
        f"import runpy\nrunpy.run_path({plan!r})\n",
        f"import runpy\nrunpy.run_path({archived_research!r})\n",
        f"import runpy\nrunpy.run_path({archived_legacy!r})\n",
        (
            "from pathlib import Path\n"
            "from runpy import run_path\n"
            f"run_path(Path({prototype!r}))\n"
        ),
        (
            "import runpy\n"
            "from pathlib import PurePath\n"
            f"runpy.run_path(path_name=PurePath({plan!r}))\n"
        ),
        (
            "import runpy\n"
            "from pathlib import PurePosixPath\n"
            f"runpy.run_path(PurePosixPath({archived_research!r}))\n"
        ),
        (
            "from importlib.machinery import SourceFileLoader\n"
            "from pathlib import PureWindowsPath\n"
            "SourceFileLoader('archived', PureWindowsPath("
            f"{windows_legacy!r}))\n"
        ),
    )


@pytest.mark.parametrize("source", _loader_snippets())
def test_archive_guard_rejects_literal_dynamic_loader_snippets(
    tmp_path,
    monkeypatch,
    source,
):
    with pytest.raises(AssertionError) as caught:
        _run_runtime_guard_on_snippet(tmp_path, monkeypatch, source)
    assert "literal dynamic load" in str(caught.value)


def test_archive_guard_ignores_comments_and_unloaded_prose(tmp_path, monkeypatch):
    if not _SAMPLE_ARCHIVED_REFERENCES:
        pytest.skip("no archive manifest in this tree")
    pre_prototype, pre_plan, research, _prototype, _plan, _archived_research, \
        _legacy, archived_legacy = _SAMPLE_ARCHIVED_REFERENCES
    source = (
        f"\n# runpy.run_path({pre_prototype!r})\n"
        f"planning_note = {pre_plan!r}\n"
        f"research_note = {research!r}\n"
        f"archive_note = {archived_legacy!r}\n"
    )
    _run_runtime_guard_on_snippet(tmp_path, monkeypatch, source)
