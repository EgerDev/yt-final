from __future__ import annotations

from pathlib import Path
import re

import pytest

from scripts.make_public_tree import PUBLIC_TREE_EXCLUDED_PREFIXES, is_excluded


ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ROOT / "Final"

#: Documents that ship in the source distribution, so they are read by people
#: who only ever see the published tree.
SHIPPING_DOCS = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "docs/ARCHITECTURE.md",
    "docs/CLI.md",
    "docs/CONFIGURATION.md",
    "docs/RELEASING.md",
    "docs/TROUBLESHOOTING.md",
    "requirements/README.md",
    "Final/README.md",
    "Final/INNOVATION_ROADMAP.md",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_root_readme_states_the_supported_release_contract():
    readme = _text(ROOT / "README.md")
    normalized = " ".join(readme.split())

    for required in (
        "Python 3.11",
        "Windows",
        "macOS",
        "Linux is unsupported for 1.0.0",
        "yt-final",
        "pip install",
        "SHA256SUMS",
        "loopback",
    ):
        assert required in normalized
    assert r".venv\Scripts\python.exe -m pip install" in readme
    assert ".venv/bin/python -m pip install" in readme
    for stale in (
        "No pip package yet",
        "Packaging coming in v1",
        "### VPS / headless server setup",
    ):
        assert stale not in readme


def test_product_readme_uses_the_authoritative_test_and_session_contracts():
    readme = _text(PRODUCT / "README.md")
    normalized = " ".join(readme.split())

    assert "Python 3.11" in normalized
    assert "python -m pytest" in readme
    assert "python test_yt_final.py" not in readme
    assert "12-hour" in normalized
    assert "restart" in normalized
    assert "revok" in normalized.casefold()
    assert "token-gated" not in readme
    assert "loopback-only" in normalized


def test_security_policy_has_a_concrete_private_channel_and_boundaries():
    policy = _text(ROOT / "SECURITY.md")
    normalized = " ".join(policy.split())

    assert "1.0.0" in normalized
    assert (
        "https://github.com/EgerDev/yt-final/security/advisories/new"
        in policy
    )
    for required in (
        "12 hours",
        "server restart",
        "loopback",
        "owner-only",
        "cookies",
        "logs",
    ):
        assert required in normalized
    assert "open an issue asking for a security contact" not in policy


def test_contributor_and_changelog_docs_describe_the_release_gates():
    contributing = _text(ROOT / "CONTRIBUTING.md")
    changelog = _text(ROOT / "CHANGELOG.md")

    assert "Python 3.11" in contributing
    assert "python -m pytest" in contributing
    assert "python test_yt_final.py" not in contributing
    for gate in ("Ruff", "mypy", "npm", "release"):
        assert gate in contributing
    assert "## [1.0.0] - 2026-07-29" in changelog
    for topic in ("loopback", "session", "reproducible", "supply-chain"):
        assert topic in changelog.casefold()


@pytest.mark.parametrize("document", SHIPPING_DOCS)
def test_shipping_docs_never_reference_stripped_material(document: str) -> None:
    """A shipping document must not point at anything the published tree omits.

    `scripts/make_public_tree.py` removes `archive/`, `mcps/`, and the
    `superpowers/` plan directories. A shipping document that links into one of
    them renders as a dead link for every reader who only has the published
    tree -- and worse, it forces the published copy of that document to be
    edited, which is exactly how the two trees start diverging. README.md and
    CHANGELOG.md both carried such a link, because a release-boundary test used
    to REQUIRE it.

    Prose about history is fine. A path is not.
    """
    path = ROOT / document
    if not path.is_file():
        pytest.skip(f"{document} is not present in this tree")
    text = _text(path)
    offenders = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        for prefix in PUBLIC_TREE_EXCLUDED_PREFIXES
        if prefix in line
    ]
    assert not offenders, (
        f"{document} references material stripped from the published tree "
        f"(see scripts/make_public_tree.py):\n  " + "\n  ".join(offenders)
    )


def test_every_certified_distribution_appears_in_the_third_party_notices():
    """The notices file must name every distribution in the runtime lock.

    `Final/THIRD_PARTY_NOTICES` says up front that it covers software the
    project does not bundle -- managed FFmpeg, Node, Deno -- so its own stated
    scope already reaches the pip-installed runtime closure, which it did not
    list. A reviewer working the open GPLv3 question had no licence inventory
    to work from, and the one dependency that matters most for that review
    (bgutil, GPL-3.0) was the one least likely to be noticed.

    Pinned here rather than in prose so adding a distribution to the lock
    without recording its licence fails immediately.
    """
    lock = (ROOT / "Final" / "approved-python.lock").read_text(encoding="utf-8")
    notices = _text(ROOT / "Final" / "THIRD_PARTY_NOTICES").casefold()
    pinned = sorted({
        (match.group(1), match.group(2))
        for match in re.finditer(
            r"(?m)^([A-Za-z][A-Za-z0-9_.-]*)==([0-9][^\s\\;]*)", lock
        )
    })
    assert pinned, "no distributions parsed from approved-python.lock"
    # Require "name version" together, not just the name: every entry also
    # carries an upstream URL containing the project name, so a bare substring
    # check passes even after the entry is deleted -- verified by mutation.
    missing = [
        f"{name}=={version}"
        for name, version in pinned
        if f"{name} {version}".casefold() not in notices
    ]
    assert not missing, (
        "these certified runtime distributions are absent from "
        f"Final/THIRD_PARTY_NOTICES (or their pinned version drifted): "
        f"{', '.join(missing)}"
    )


def test_sponsorblock_attribution_is_recorded():
    """SponsorBlock data is written into files the user keeps and may share.

    Marked segments become chapter titles (`[SponsorBlock]: <category>`) inside
    the output file, so redistributing that file redistributes their data. The
    database carries attribution and non-commercial terms that the project's
    Unlicense does not override.
    """
    notices = _text(ROOT / "Final" / "THIRD_PARTY_NOTICES")
    assert "SponsorBlock" in notices
    assert "sponsor.ajay.app" in notices


def test_macos_launcher_keeps_its_executable_bit_in_git():
    """The double-clickable macOS launcher must be mode 100755 in the index.

    `Final/README.md` tells macOS users to double-click
    `Final/Start yt_final.command`. Without the executable bit a clone gives
    "you do not have appropriate access privileges", or opens it in a text
    editor, with no hint that `chmod +x` is the whole fix. The release
    pipeline sets 0755 when building the portable ZIP, and that IS tested --
    but nothing tested the checked-in mode, and `git clone` is a primary way
    to obtain a public project.

    This regressed once already: Windows has no execute bit, so exporting the
    tree and re-adding it recorded 100644. `scripts/make_public_tree.py`
    restores it now; this test is what notices if that stops working.
    """
    import subprocess

    launcher = "Final/Start yt_final.command"
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip("not a git checkout - repository-state assertions do not apply")

    listing = subprocess.run(
        ["git", "ls-files", "-s", "--", launcher],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not listing:
        pytest.skip(f"{launcher} is not tracked in this checkout")
    mode = listing.split(maxsplit=1)[0]
    assert mode == "100755", (
        f"{launcher} is mode {mode} in the git index, expected 100755. "
        "macOS users are told to double-click it; without the executable bit "
        "a clone cannot launch it."
    )


def test_published_tree_excludes_directory_entries_not_just_their_contents():
    """A bare directory entry must be excluded, not only the files beneath it.

    A tar archive carries an entry for the directory itself, written as
    "archive/". An earlier version of `is_excluded` normalised that through
    `PurePosixPath`, which strips the trailing slash, so "archive/" became
    "archive" and no longer matched the prefix "archive/". Every file inside was
    still excluded, so the published tree looked correct by file count -- but an
    EMPTY `archive/` was created, and `test_release_boundary.py` decides whether
    to run by asking whether that directory exists. Fifty-three tests went red
    against a tree with nothing to quarantine.
    """
    for prefix in PUBLIC_TREE_EXCLUDED_PREFIXES:
        stem = prefix.rstrip("/")
        assert is_excluded(prefix), f"directory entry {prefix!r} must be excluded"
        assert is_excluded(stem), f"bare directory {stem!r} must be excluded"
        assert is_excluded(f"{stem}/nested/file.txt")


@pytest.mark.parametrize(
    "relative",
    ["", "Final", "scripts/out", ".."],
    ids=["repo-itself", "inside-repo", "nested-inside-repo", "repo-parent"],
)
def test_publication_refuses_a_destination_overlapping_the_repository(relative):
    """`--force` rmtree's the destination, so an overlapping path is destructive.

    `--force ./Final` would erase the product; `--force ..` would erase the
    repository outright. Guarding only equality with the repository root is not
    enough.
    """
    from scripts import make_public_tree

    target = make_public_tree.REPO_ROOT if not relative else (
        make_public_tree.REPO_ROOT / relative
    )
    with pytest.raises(SystemExit) as excinfo:
        make_public_tree.main([str(target), "--force"])
    assert excinfo.value.code != 0


def test_publication_can_replace_a_tree_holding_read_only_git_objects(tmp_path):
    """--force must survive the read-only files git leaves in .git/objects.

    A previously published tree has a .git directory, and git marks every loose
    object read-only. A plain shutil.rmtree raises PermissionError on Windows
    PART WAY THROUGH, leaving a half-deleted tree -- which is exactly what
    happened the first time the published tree was regenerated in place.
    """
    import os
    import stat

    from scripts.make_public_tree import remove_tree

    victim = tmp_path / "published"
    (victim / ".git" / "objects" / "ab").mkdir(parents=True)
    loose = victim / ".git" / "objects" / "ab" / "cdef"
    loose.write_bytes(b"object")
    (victim / "README.md").write_text("content", encoding="utf-8")
    os.chmod(loose, stat.S_IREAD)

    remove_tree(victim)

    assert not victim.exists()


def test_published_tree_exclusion_does_not_catch_same_named_siblings():
    """Prefix matching must not swallow a file that merely shares the name."""
    for prefix in PUBLIC_TREE_EXCLUDED_PREFIXES:
        stem = prefix.rstrip("/")
        assert not is_excluded(f"{stem}.md")
        assert not is_excluded(f"{stem}_notes/file.txt")
    assert not is_excluded("Final/yt_final.py")
    assert not is_excluded("README.md")


def test_roadmap_and_browser_copy_cannot_override_the_release_contract():
    roadmap = _text(PRODUCT / "INNOVATION_ROADMAP.md")
    html = _text(PRODUCT / "webui" / "index.html")

    assert roadmap.startswith("# Historical")
    for stale in ("yt-ingest", "(MIT)", "--allow-public", "token-gated"):
        assert stale not in roadmap
    assert "yt_final 1.0.0" in roadmap
    assert "different device on the LAN" not in html
    assert "local application" in html


def test_every_encoder_profile_is_documented():
    """`Final/README.md` must name every profile the CLI can actually select.

    Six were missing, including all four Apple VideoToolbox profiles -- so a macOS reader, on a
    Tier 1 platform, would conclude the project has no hardware encoding for them. The default was
    also documented as `premiere_h264_fast` when it is `auto_h264`, which resolves per machine.

    Parsed from the profile table in the engine rather than a hand-kept list, so a new profile
    fails here instead of silently going undocumented.
    """
    engine = (ROOT / "Final" / "yt_final.py").read_text(encoding="utf-8")
    readme = _text(ROOT / "Final" / "README.md")
    # The profile table is a dict literal keyed by profile name; each entry carries a 'desc'.
    names = sorted(set(re.findall(r'(?m)^\s{8,16}"([a-z][a-z0-9_]{3,})":\s*\{', engine)))
    known = [n for n in names if any(
        n.startswith(prefix) for prefix in
        ("auto_", "premiere_", "nvenc_", "amf_", "qsv_", "vt_", "cpu_", "prores_", "dnxhr_", "copy_")
    )]
    assert len(known) >= 30, f"profile parse looks wrong, found {len(known)}: {known[:5]}"
    missing = [name for name in known if name not in readme]
    assert not missing, (
        f"these encoder profiles are undocumented in Final/README.md: {', '.join(missing)}")
