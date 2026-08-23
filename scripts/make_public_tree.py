"""Produce the published source tree from this repository.

The published tree is this repository MINUS a fixed set of directories. It is a
pure subtraction: every file that exists in both trees is byte-identical, and
nothing is rewritten, reformatted, or regenerated on the way out. That property
is the whole point. A published tree that needed per-file edits would diverge
from the private one the moment either side was touched, and keeping two
near-copies honest by hand does not work.

The excluded directories hold material that must not be published:

  archive/               historical prototypes, legacy one-off scripts, and
                         planning documents, including an anti-bot-detection
                         prototype that must never ship
  mcps/                  third-party MCP tool schemas used during development
  docs/superpowers/      internal plans and design specs
  Final/docs/superpowers/  the same, colocated with the product

`MANIFEST.in` already prunes `archive`, `mcps`, and `Final/docs` from the source
distribution, so this set is the existing release boundary, restated for the
repository itself rather than for the built artifact.

Nothing else may differ. Two guards hold that line:

  * `test_release_documentation.py::test_shipping_docs_never_reference_stripped_material`
    keeps documentation from pointing at anything stripped here, which is what
    previously forced the published README and CHANGELOG to be edited by hand.
  * `test_release_boundary.py` measures whether `archive/` is present rather
    than assuming it, so that module is identical in both trees too.

Usage:

    python scripts/make_public_tree.py <destination> [--ref main] [--force]

The destination must not already exist unless `--force` is given. Passing
`--git-init` starts the published tree on a fresh, unrelated history, which is
the point of publishing this way: the excluded material was never in it, so
there is no history to scrub.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directory prefixes removed from the published tree. Repository-relative,
#: POSIX-separated, each ending in "/" so a prefix match cannot catch a
#: same-named file (``docs/superpowers.md`` is not ``docs/superpowers/``).
PUBLIC_TREE_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "archive/",
    "mcps/",
    "docs/superpowers/",
    "Final/docs/superpowers/",
)


def _clear_readonly_and_retry(func, path, _excinfo) -> None:
    """rmtree error handler that clears the read-only bit and retries once.

    A previously published tree contains a `.git` directory, and git marks
    every loose object read-only. On Windows `os.unlink` then fails with
    `PermissionError: [WinError 5]`, so a plain `shutil.rmtree` cannot replace
    a destination that was published before -- and it fails PART WAY THROUGH,
    leaving a half-deleted tree behind.

    Works as both `onerror` (<=3.11) and `onexc` (3.12+): each passes
    (function, path, third argument).
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def executable_paths(ref: str, *, repo_root: Path = REPO_ROOT) -> list[str]:
    """Paths git records as mode 100755 at `ref`."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "-r", ref],
        capture_output=True,
        text=True,
        check=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        if not line.startswith("100755"):
            continue
        # "<mode> <type> <sha>\t<path>" -- split on the tab, because paths
        # contain spaces.
        _, _, path = line.partition("\t")
        if path.strip():
            paths.append(path)
    return paths


def restore_exec_bits(destination: Path, ref: str, *, repo_root: Path = REPO_ROOT) -> int:
    """Re-apply the executable bit inside `destination`'s freshly staged index.

    Windows has no execute permission bit, so a tar extraction cannot carry one
    and `git add` in the new repository records 100644 for everything. That
    silently downgrades `Final/Start yt_final.command`, whose whole purpose is
    to be double-clicked on macOS -- a user who clones the published tree gets
    "you do not have appropriate access privileges" instead of a launcher.

    Requires the files to be staged already.
    """
    restored = 0
    for path in executable_paths(ref, repo_root=repo_root):
        subprocess.run(
            ["git", "-C", str(destination), "update-index", "--chmod=+x", "--", path],
            check=True,
        )
        restored += 1
    return restored


def remove_tree(path: Path) -> None:
    """Delete `path` even when it holds read-only git objects."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(path, onerror=_clear_readonly_and_retry)


def is_excluded(repo_relative_path: str) -> bool:
    """Whether a repository-relative path is kept out of the published tree.

    Accepts directory entries as well as file paths. A tar archive carries an
    entry for the directory itself ("archive/"), and it must be matched too --
    extracting it creates an empty `archive/`, which is enough to make
    `test_release_boundary.py` believe the archive is present and run its whole
    quarantine suite against a tree that has nothing to quarantine.
    """
    normalized = repo_relative_path.replace("\\", "/").strip("/")
    for prefix in PUBLIC_TREE_EXCLUDED_PREFIXES:
        stem = prefix.rstrip("/")
        # `== stem` catches the directory itself; `stem + "/"` catches its
        # contents. Neither matches a same-named file such as "archive.md".
        if normalized == stem or normalized.startswith(stem + "/"):
            return True
    return False


def tracked_paths(ref: str, *, repo_root: Path = REPO_ROOT) -> list[str]:
    """Every path tracked at `ref`, in git's order."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", ref],
        capture_output=True,
        text=True,
        check=True,
    )
    # splitlines(), not split(): several tracked paths contain spaces.
    return [line for line in result.stdout.splitlines() if line.strip()]


def partition(ref: str, *, repo_root: Path = REPO_ROOT) -> tuple[list[str], list[str]]:
    """Return (published, excluded) paths for `ref`."""
    published: list[str] = []
    excluded: list[str] = []
    for path in tracked_paths(ref, repo_root=repo_root):
        (excluded if is_excluded(path) else published).append(path)
    return published, excluded


def export_tree(ref: str, destination: Path, *, repo_root: Path = REPO_ROOT) -> None:
    """Write `ref` to `destination`, minus the excluded prefixes.

    Exports through `git archive` so the bytes come from the commit rather than
    the working tree: an uncommitted edit, a stray build output, or an ignored
    file cannot leak into a published tree this way.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        bundle = Path(staging) / "tree.tar"
        with bundle.open("wb") as handle:
            subprocess.run(
                ["git", "-C", str(repo_root), "archive", "--format=tar", ref],
                stdout=handle,
                check=True,
            )
        with tarfile.open(bundle) as archive:
            members = [m for m in archive.getmembers() if not is_excluded(m.name)]
            if hasattr(tarfile, "data_filter"):
                archive.extractall(destination, members=members, filter="data")
            else:
                # `filter=` arrived in 3.12 and was backported to 3.11.4, but
                # requires-python is ">=3.11", so 3.11.0-3.11.3 would raise
                # TypeError on the call above. The bytes come from `git archive`
                # over our own commit rather than an untrusted download, so
                # extracting without the sanitiser is acceptable here -- but
                # only here, and only because of that provenance.
                archive.extractall(destination, members=members)  # noqa: S202 -- bytes come from our own git archive, not a download.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("destination", type=Path)
    parser.add_argument("--ref", default="main", help="git ref to publish (default: main)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace destination if it already exists",
    )
    parser.add_argument(
        "--git-init",
        action="store_true",
        help="initialise a fresh, unrelated git history in the destination",
    )
    args = parser.parse_args(argv)

    destination: Path = args.destination.resolve()
    # --force calls rmtree on the destination, so a path that overlaps this
    # repository would delete part or all of it. Reject the repository itself,
    # anything inside it (`--force ./Final` would erase the product), and any
    # ancestor of it (`--force ..` would erase the repository outright).
    if destination == REPO_ROOT:
        parser.error("destination must not be the repository itself")
    if REPO_ROOT in destination.parents:
        parser.error(f"destination must not be inside the repository: {destination}")
    if destination in REPO_ROOT.parents:
        parser.error(f"destination must not contain the repository: {destination}")
    if destination.exists():
        if not args.force:
            parser.error(f"{destination} already exists (pass --force to replace)")
        remove_tree(destination)

    published, excluded = partition(args.ref)
    export_tree(args.ref, destination)

    written = sum(1 for path in destination.rglob("*") if path.is_file())
    print(f"published {written} files to {destination}")
    print(f"  tracked at {args.ref}: {len(published) + len(excluded)}")
    print(f"  excluded:              {len(excluded)}")
    for prefix in PUBLIC_TREE_EXCLUDED_PREFIXES:
        count = sum(1 for path in excluded if path.startswith(prefix))
        print(f"    {prefix:<26} {count}")

    if written != len(published):
        print(
            f"ERROR: wrote {written} files but expected {len(published)}",
            file=sys.stderr,
        )
        return 1

    # An excluded directory must not survive even empty. `test_release_boundary`
    # decides whether to run by asking whether `archive/` exists, so an empty
    # one is not cosmetic: it turns the whole quarantine suite red.
    survivors = [
        prefix
        for prefix in PUBLIC_TREE_EXCLUDED_PREFIXES
        if (destination / prefix.rstrip("/")).exists()
    ]
    if survivors:
        print(
            "ERROR: excluded directories survived extraction: "
            + ", ".join(survivors),
            file=sys.stderr,
        )
        return 1

    if args.git_init:
        subprocess.run(["git", "init", "-b", "main", "-q"], cwd=destination, check=True)
        # Stage here rather than leaving it to the caller: the executable bits
        # can only be re-applied to an index that already has the entries, and
        # forgetting that step is invisible until a macOS user cannot launch
        # the app.
        subprocess.run(["git", "add", "-A"], cwd=destination, check=True)
        restored = restore_exec_bits(destination, args.ref)
        print(f"  initialised a fresh history in {destination}")
        print(f"  staged {written} files, restored {restored} executable bit(s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
