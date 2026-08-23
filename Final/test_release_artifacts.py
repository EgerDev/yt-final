from __future__ import annotations

import ast
import hashlib
import importlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import time
from zipfile import ZIP_DEFLATED, ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]

# build_portable._DirectoryAnchor.rename_held_child restores the rollback snapshot without ever
# trusting a bare path: Windows renames the open handle, Linux relinks the held descriptor (linkat
# with AT_EMPTY_PATH / /proc/self/fd), and the remaining POSIX platforms -- macOS has neither of
# those primitives -- fall back to renameat() against the anchored directory descriptor, still
# bracketed by verify_file_descriptor() on both sides. Only a POSIX host whose CPython lacks
# renameat-by-descriptor has no restore at all; there rename_held_child raises OSError(ENOTSUP)
# and the tests that exercise a SUCCESSFUL restore cannot run. That is measured, not named, so
# the gate disappears by itself wherever the capability is present.
_HELD_DESCRIPTOR_RESTORE_UNAVAILABLE = (
    os.name != "nt" and os.rename not in os.supports_dir_fd
)
_skip_without_held_descriptor_restore = pytest.mark.skipif(
    _HELD_DESCRIPTOR_RESTORE_UNAVAILABLE,
    reason=(
        "this POSIX host has no renameat-by-descriptor, so build_portable."
        "rename_held_child has no rollback restore primitive at all"
    ),
)
FIXED_EPOCH = 1_700_000_000
FIXED_DOS_TIME = (2023, 11, 14, 22, 13, 20)

EXPECTED_PORTABLE_FILES = {
    "LICENSE",
    "README.md",
    "Final/README.md",
    "Final/Start yt_final.bat",
    "Final/Start yt_final.command",
    "Final/THIRD_PARTY_NOTICES",
    "Final/__init__.py",
    "Final/__main__.py",
    "Final/_cdp_client.py",
    "Final/_version.py",
    "Final/approved-python.lock",
    "Final/assets/slate.ttf",
    "Final/capability.py",
    "Final/cli.py",
    "Final/cookie_providers.py",
    "Final/cookies_auth.py",
    "Final/dependency_contract.py",
    "Final/desync_proxy.py",
    "Final/examples/config.example.toml",
    "Final/examples/jobs.example.json",
    "Final/executable_trust.py",
    "Final/ffmpeg_updater.py",
    "Final/installer_lock.py",
    "Final/jobspec.py",
    "Final/jobstore.py",
    "Final/managed_env.py",
    "Final/markers.py",
    "Final/nfo.py",
    "Final/notifications.py",
    "Final/optional_tools.py",
    "Final/redaction.py",
    "Final/requirements.txt",
    "Final/secureio.py",
    "Final/settings.py",
    "Final/speedtest.py",
    "Final/state_validation.py",
    "Final/tool_manifest.py",
    "Final/url_identity.py",
    "Final/watch.py",
    "Final/watch_filters.py",
    "Final/watchstore.py",
    "Final/webserver.py",
    "Final/websessions.py",
    "Final/webui/app.js",
    "Final/webui/index.html",
    "Final/webui/style.css",
    "Final/yt_final.py",
}


def _portable_module():
    script = ROOT / "scripts" / "build_portable.py"
    assert script.is_file(), "Task 4 portable builder is missing"
    importlib.invalidate_caches()
    module = importlib.import_module("scripts.build_portable")
    assert Path(module.__file__).resolve() == script.resolve()
    return module


def _checksum_module():
    script = ROOT / "scripts" / "write_checksums.py"
    assert script.is_file(), "Task 4 checksum writer is missing"
    importlib.invalidate_caches()
    module = importlib.import_module("scripts.write_checksums")
    assert Path(module.__file__).resolve() == script.resolve()
    return module


def _write_synthetic_repo(
        root: Path,
        allowlist: str,
        files: dict[str, bytes | str] | None = None,
) -> Path:
    (root / "release").mkdir(parents=True)
    (root / "release" / "portable-files.txt").write_text(
        allowlist,
        encoding="utf-8",
        newline="\n",
    )
    version = root / "Final" / "_version.py"
    version.parent.mkdir(parents=True, exist_ok=True)
    version.write_text(
        '__version__ = "1.0.0"\n',
        encoding="utf-8",
        newline="\n",
    )
    for relative, content in (files or {}).items():
        path = root / Path(PurePosixPath(relative))
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")
    return root


def _member_map(archive: Path) -> dict[str, bytes]:
    with ZipFile(archive) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def test_portable_allowlist_expands_to_exact_runtime_product():
    module = _portable_module()

    members = module.portable_members(ROOT)
    destinations = [destination.as_posix() for _source, destination in members]

    assert destinations == sorted(EXPECTED_PORTABLE_FILES)
    assert len(destinations) == 47
    assert "Final/desync_proxy.py" in destinations
    assert not any(
        destination.startswith((
            "archive/",
            "docs/",
            "mcps/",
            ".claude/",
            "requirements/",
        ))
        for destination in destinations
    )
    assert not any(
        PurePosixPath(destination).name.startswith("test_")
        for destination in destinations
    )


def test_portable_runtime_python_inventory_has_no_unclassified_module():
    module = _portable_module()
    destinations = {
        destination.as_posix()
        for _source, destination in module.portable_members(ROOT)
    }
    candidates = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "Final").rglob("*.py")
        if not path.name.startswith("test_")
        and path.name not in {"_syntax_check.py", "runtime_verify.py"}
        and "__pycache__" not in path.parts
    }

    assert candidates <= destinations
    assert "Final/_syntax_check.py" not in destinations
    assert "Final/runtime_verify.py" not in destinations


def test_portable_runtime_import_graph_and_dynamic_paths_are_closed():
    module = _portable_module()
    destinations = {
        destination.as_posix()
        for _source, destination in module.portable_members(ROOT)
    }
    runtime_paths = {
        path.relative_to(ROOT).as_posix(): path
        for path in (ROOT / "Final").rglob("*.py")
        if not path.name.startswith("test_")
        and path.name not in {"_syntax_check.py", "runtime_verify.py"}
        and "__pycache__" not in path.parts
    }

    def module_parts(relative):
        parts = list(PurePosixPath(relative).parts[1:])
        parts[-1] = PurePosixPath(parts[-1]).stem
        if parts[-1] == "__init__":
            parts.pop()
        return tuple(parts)

    modules = {
        module_parts(relative): relative
        for relative in runtime_paths
    }
    graph = {relative: set() for relative in runtime_paths}
    string_literals = set()
    for relative, path in runtime_paths.items():
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        current = module_parts(relative)
        package = (
            current
            if path.name == "__init__.py"
            else current[:-1]
        )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            ):
                string_literals.add(node.value)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = tuple(alias.name.split("."))
                    if target[:1] == ("Final",):
                        target = target[1:]
                    if target in modules:
                        graph[relative].add(modules[target])
            elif isinstance(node, ast.ImportFrom):
                imported = tuple((node.module or "").split("."))
                if imported == ("",):
                    imported = ()
                if node.level:
                    keep = len(package) - (node.level - 1)
                    target = package[:max(0, keep)] + imported
                else:
                    target = imported
                    if target[:1] == ("Final",):
                        target = target[1:]
                if target in modules:
                    graph[relative].add(modules[target])
                for alias in node.names:
                    child = target + (alias.name,)
                    if child in modules:
                        graph[relative].add(modules[child])

    roots = {
        "Final/__init__.py",
        "Final/__main__.py",
        "Final/cli.py",
        "Final/desync_proxy.py",
        "Final/webserver.py",
        "Final/yt_final.py",
    }
    reachable = set(roots)
    pending = list(roots)
    while pending:
        source = pending.pop()
        for imported in graph[source]:
            if imported not in reachable:
                reachable.add(imported)
                pending.append(imported)

    assert reachable == set(runtime_paths)
    assert reachable <= destinations
    assert "desync_proxy.py" in string_literals
    assert {"assets", "slate.ttf"} <= string_literals
    for directory in ("assets", "examples", "webui"):
        actual = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "Final" / directory).rglob("*")
            if path.is_file()
        }
        packaged = {
            destination
            for destination in destinations
            if destination.startswith(f"Final/{directory}/")
        }
        assert packaged == actual


def test_recursive_allowlist_expands_regular_files_in_posix_sort_order(
        tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "# runtime tree\nruntime/**\n",
        {
            "runtime/zeta.txt": "z",
            "runtime/nested/beta.txt": "b",
            "runtime/alpha.txt": "a",
        },
    )

    members = _portable_module().portable_members(repo)

    assert [
        (source.relative_to(repo).as_posix(), destination.as_posix())
        for source, destination in members
    ] == [
        ("runtime/alpha.txt", "runtime/alpha.txt"),
        ("runtime/nested/beta.txt", "runtime/nested/beta.txt"),
        ("runtime/zeta.txt", "runtime/zeta.txt"),
    ]


@pytest.mark.parametrize(
    "entry",
    (
        "/absolute.txt",
        "//server/share.txt",
        "C:/drive.txt",
        "C:drive-relative.txt",
        r"Final\yt_final.py",
        "../outside.txt",
        "Final/../outside.txt",
        "./Final/yt_final.py",
        "Final//yt_final.py",
        "Final/*",
        "Final/**/nested",
        "Final/file?.py",
        "Final/[abc].py",
        "Final/control\x01.py",
    ),
)
def test_allowlist_rejects_ambiguous_or_escaping_paths(tmp_path, entry):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        f"{entry}\n",
        {"Final/yt_final.py": "pass\n"},
    )

    with pytest.raises(ValueError):
        _portable_module().portable_members(repo)


@pytest.mark.parametrize(
    "entry",
    (
        "Final/payload.txt:secret",
        "Final/payload.txt.",
        "Final/payload.txt ",
        "Final/CON",
        "Final/con.txt",
        "Final/AUX.json",
        "Final/NUL",
        "Final/COM1.log",
        "Final/LPT9",
        "Final/bad<name>.txt",
        "Final/bad|name.txt",
        "Final/control\u0085.txt",
        "Final/line\u2028separator.txt",
        "Final/bidi\u202etxt.exe",
    ),
)
def test_allowlist_rejects_windows_aliases_devices_and_unicode_controls(
        entry):
    with pytest.raises(ValueError, match="portable|component|Windows|control"):
        _portable_module()._validate_destination(entry)


def test_allowlist_rejects_empty_effective_membership(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "\n# comments are not members\n   # another comment\n",
    )

    with pytest.raises(ValueError, match="no portable files"):
        _portable_module().portable_members(repo)


@pytest.mark.parametrize(
    ("entry", "files"),
    (
        ("missing.txt", {}),
        ("runtime", {"runtime/child.txt": "child"}),
        ("literal.txt/**", {"literal.txt": "file"}),
    ),
)
def test_allowlist_rejects_missing_and_wrong_kind_sources(
        tmp_path, entry, files):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        f"{entry}\n",
        files,
    )

    with pytest.raises((FileNotFoundError, ValueError)):
        _portable_module().portable_members(repo)


@pytest.mark.parametrize(
    "allowlist",
    (
        "same.txt\nsame.txt\n",
        "runtime/**\nruntime/a.txt\n",
        "Name.txt\nname.txt\n",
    ),
)
def test_allowlist_rejects_duplicates_overlaps_and_case_collisions(
        tmp_path, allowlist):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        allowlist,
        {
            "same.txt": "same",
            "runtime/a.txt": "a",
            "Name.txt": "upper",
            "name.txt": "lower",
        },
    )

    with pytest.raises(ValueError, match="duplicate|overlap|collision"):
        _portable_module().portable_members(repo)


def test_destination_collisions_use_unicode_and_component_prefix_semantics():
    module = _portable_module()

    with pytest.raises(ValueError, match="collision|prefix"):
        module._validate_member_destinations((
            PurePosixPath("Final/caf\u00e9.txt"),
            PurePosixPath("Final/cafe\u0301.txt"),
        ))
    with pytest.raises(ValueError, match="collision|prefix"):
        module._validate_member_destinations((
            PurePosixPath("Final/Node"),
            PurePosixPath("Final/node/child.txt"),
        ))
    with pytest.raises(ValueError, match="collision|prefix"):
        module._validate_member_destinations((
            PurePosixPath("Final/Runtime/one.txt"),
            PurePosixPath("Final/runtime/two.txt"),
        ))


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate streams")
def test_native_ntfs_ads_and_trailing_dot_aliases_are_rejected(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        (
            "Final/_version.py\n"
            "Final/payload.txt\n"
            "Final/payload.txt:secret\n"
        ),
        {"Final/payload.txt": b"base"},
    )
    ads = Path(f"{repo / 'Final' / 'payload.txt'}:secret")
    try:
        ads.write_bytes(b"outside-the-portable-file")
    except OSError as exc:
        pytest.skip(f"NTFS alternate streams unavailable: {exc}")

    with pytest.raises(ValueError, match="portable|component|Windows"):
        _portable_module().build_portable(
            repo,
            tmp_path / "output",
            epoch=FIXED_EPOCH,
        )

    alias_repo = _write_synthetic_repo(
        tmp_path / "alias-repo",
        (
            "Final/_version.py\n"
            "Final/payload.txt\n"
            "Final/payload.txt.\n"
        ),
        {"Final/payload.txt": b"base"},
    )
    with pytest.raises(ValueError, match="portable|component|collision"):
        _portable_module().portable_members(alias_repo)


@pytest.mark.parametrize(
    "forbidden",
    (
        "runtime/test_hidden.py",
        "runtime/__pycache__/hidden.pyc",
        "runtime/downloads/secret.bin",
        "runtime/archive/legacy.py",
        "runtime/debug.log",
        "runtime/logs/debug.txt",
        "runtime/cache/state.bin",
        "runtime/hidden_test.py",
    ),
)
def test_forbidden_members_are_rejected_after_recursive_expansion(
        tmp_path, forbidden):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "runtime/**\n",
        {
            "runtime/good.py": "pass\n",
            forbidden: b"forbidden",
        },
    )

    with pytest.raises(ValueError, match="forbidden"):
        _portable_module().portable_members(repo)


def test_literal_symlink_is_rejected_even_when_target_is_inside_repo(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "alias.txt\n",
        {"target.txt": "target"},
    )
    try:
        os.symlink(repo / "target.txt", repo / "alias.txt")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink|reparse"):
        _portable_module().portable_members(repo)


def test_recursive_symlink_child_is_rejected_before_expansion(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "runtime/**\n",
        {"runtime/good.txt": "good", "outside.txt": "outside"},
    )
    try:
        os.symlink(repo / "outside.txt", repo / "runtime" / "alias.txt")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink|reparse"):
        _portable_module().portable_members(repo)


def test_reparse_or_junction_marker_is_rejected(tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "payload.txt\n",
        {"payload.txt": "payload"},
    )
    original = getattr(Path, "is_junction", lambda _self: False)
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda self: self == repo / "payload.txt" or original(self),
        raising=False,
    )

    with pytest.raises(ValueError, match="symlink|reparse"):
        _portable_module().portable_members(repo)


def test_repository_root_reparse_or_junction_is_rejected(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "payload.txt\n",
        {"payload.txt": "payload"},
    )
    original = getattr(Path, "is_junction", lambda _self: False)
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda self: self == repo or original(self),
        raising=False,
    )

    with pytest.raises(ValueError, match="symlink|reparse"):
        _portable_module().portable_members(repo)


def test_allowlist_version_and_members_all_use_anchored_reader(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\nruntime/**\n",
        {"runtime/payload.txt": "inside\n"},
    )
    module = _portable_module()
    original = module._stable_source_bytes
    destinations = []

    def recording_reader(source, *args, **kwargs):
        destination = kwargs.get("destination")
        destinations.append(
            destination.as_posix()
            if isinstance(destination, PurePosixPath)
            else destination
        )
        return original(source, *args, **kwargs)

    monkeypatch.setattr(module, "_stable_source_bytes", recording_reader)
    module.build_portable(
        repo,
        tmp_path / "output",
        epoch=FIXED_EPOCH,
    )

    assert sorted(destinations) == sorted([
        "Final/_version.py",
        "release/portable-files.txt",
        "runtime/payload.txt",
    ])


def test_repository_root_replacement_after_initial_review_is_rejected(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\nruntime/**\n",
        {"runtime/payload.txt": "reviewed-root\n"},
    )
    parked = tmp_path / "repo-reviewed"
    replacement = _write_synthetic_repo(
        tmp_path / "replacement",
        "Final/_version.py\nruntime/**\n",
        {"runtime/payload.txt": "replacement-root\n"},
    )
    (replacement / "Final" / "_version.py").write_text(
        '__version__ = "9.9.9"\n',
        encoding="utf-8",
        newline="\n",
    )
    module = _portable_module()
    original = module._dos_datetime
    swapped = False

    def replace_root_after_review(epoch):
        nonlocal swapped
        value = original(epoch)
        repo.rename(parked)
        replacement.rename(repo)
        swapped = True
        return value

    monkeypatch.setattr(module, "_dos_datetime", replace_root_after_review)
    try:
        with pytest.raises(ValueError, match="root|identity|anchor|changed"):
            module.build_portable(
                repo,
                tmp_path / "output",
                epoch=FIXED_EPOCH,
            )
    finally:
        if swapped:
            repo.rename(replacement)
            parked.rename(repo)


def test_version_identity_used_for_filename_must_match_archived_member(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    module = _portable_module()
    original = module._expand_allowlist_entries

    def mutate_version_after_expansion(root, declared):
        entries = original(root, declared)
        (repo / "Final" / "_version.py").write_text(
            '__version__ = "9.9.9"\n',
            encoding="utf-8",
            newline="\n",
        )
        return entries

    monkeypatch.setattr(
        module,
        "_expand_allowlist_entries",
        mutate_version_after_expansion,
    )

    with pytest.raises(ValueError, match="version|identity|changed"):
        module.build_portable(
            repo,
            tmp_path / "output",
            epoch=FIXED_EPOCH,
        )


def test_version_filename_and_member_use_the_same_immutable_bytes(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    version_path = repo / "Final" / "_version.py"
    original_bytes = version_path.read_bytes()
    reviewed_metadata = version_path.stat()
    module = _portable_module()
    original_expand = module._expand_allowlist_entries

    def rewrite_version_without_identity_change(root, declared):
        entries = original_expand(root, declared)
        version_path.write_bytes(b'__version__ = "9.9.9"\n')
        os.utime(
            version_path,
            ns=(
                reviewed_metadata.st_atime_ns,
                reviewed_metadata.st_mtime_ns,
            ),
        )
        return entries

    monkeypatch.setattr(
        module,
        "_expand_allowlist_entries",
        rewrite_version_without_identity_change,
    )

    archive = module.build_portable(
        repo,
        tmp_path / "output",
        epoch=FIXED_EPOCH,
    )

    assert archive.name == "yt_final-1.0.0-portable.zip"
    with ZipFile(archive) as built:
        assert (
            built.read("yt_final-1.0.0/Final/_version.py")
            == original_bytes
        )


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction swap")
def test_windows_junction_swap_after_expansion_is_rejected(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\nruntime/**\n",
        {"runtime/payload.txt": "reviewed-inside\n"},
    )
    runtime = repo / "runtime"
    parked = repo / "runtime-reviewed"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.txt").write_text(
        "outside\n",
        encoding="utf-8",
        newline="\n",
    )
    module = _portable_module()
    original = module._normalized_member_bytes
    swapped = False

    def swapping_reader(source, destination, *args, **kwargs):
        nonlocal swapped
        if destination == PurePosixPath("runtime/payload.txt") and not swapped:
            runtime.rename(parked)
            result = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(runtime),
                    str(outside),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                parked.rename(runtime)
                pytest.skip(
                    "junction creation unavailable: "
                    f"{result.stdout}{result.stderr}"
                )
            swapped = True
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(module, "_normalized_member_bytes", swapping_reader)
    try:
        with pytest.raises(
                ValueError,
                match="changed|contain|outside|symlink|reparse|identity"):
            module.build_portable(
                repo,
                tmp_path / "output",
                epoch=FIXED_EPOCH,
            )
    finally:
        if swapped:
            os.rmdir(runtime)
            parked.rename(runtime)


@pytest.mark.skipif(os.name == "nt", reason="native POSIX symlink swap")
def test_posix_symlink_swap_after_expansion_is_rejected(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\nruntime/**\n",
        {"runtime/payload.txt": "reviewed-inside\n"},
    )
    runtime = repo / "runtime"
    parked = repo / "runtime-reviewed"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.txt").write_text(
        "outside\n",
        encoding="utf-8",
        newline="\n",
    )
    module = _portable_module()
    original = module._normalized_member_bytes
    swapped = False

    def swapping_reader(source, destination, *args, **kwargs):
        nonlocal swapped
        if destination == PurePosixPath("runtime/payload.txt") and not swapped:
            runtime.rename(parked)
            os.symlink(outside, runtime, target_is_directory=True)
            swapped = True
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(module, "_normalized_member_bytes", swapping_reader)
    try:
        with pytest.raises(
                (OSError, ValueError),
                match="changed|contain|outside|symlink|reparse|identity"):
            module.build_portable(
                repo,
                tmp_path / "output",
                epoch=FIXED_EPOCH,
            )
    finally:
        if swapped:
            runtime.unlink()
            parked.rename(runtime)


@pytest.fixture(scope="module")
def reproducible_archives(tmp_path_factory):
    if not (ROOT / "scripts" / "build_portable.py").is_file():
        return None
    module = _portable_module()
    base = tmp_path_factory.mktemp("portable-reproducible")
    first = module.build_portable(ROOT, base / "one", epoch=FIXED_EPOCH)
    second = module.build_portable(ROOT, base / "two", epoch=FIXED_EPOCH)
    return first, second


def test_portable_build_is_byte_reproducible(reproducible_archives):
    assert reproducible_archives is not None, "portable builder is missing"
    first, second = reproducible_archives

    assert first.name == "yt_final-1.0.0-portable.zip"
    assert second.name == first.name
    assert first.read_bytes() == second.read_bytes()
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(
        second.read_bytes()
    ).hexdigest()


def test_portable_zipinfo_metadata_is_exact_and_sorted(
        reproducible_archives):
    assert reproducible_archives is not None, "portable builder is missing"
    first, second = reproducible_archives
    expected_names = [
        f"yt_final-1.0.0/{name}"
        for name in sorted(EXPECTED_PORTABLE_FILES)
    ]

    with ZipFile(first) as first_zip, ZipFile(second) as second_zip:
        first_infos = first_zip.infolist()
        second_infos = second_zip.infolist()
        assert first_zip.comment == b""
        assert second_zip.comment == b""
        assert [info.filename for info in first_infos] == expected_names
        assert [
            (
                info.filename,
                info.date_time,
                info.create_system,
                info.compress_type,
                info.external_attr,
                info.internal_attr,
                info.extra,
                info.comment,
            )
            for info in first_infos
        ] == [
            (
                info.filename,
                info.date_time,
                info.create_system,
                info.compress_type,
                info.external_attr,
                info.internal_attr,
                info.extra,
                info.comment,
            )
            for info in second_infos
        ]
        for info in first_infos:
            mode = stat.S_IMODE(info.external_attr >> 16)
            expected_mode = (
                0o755 if info.filename.endswith(".command") else 0o644
            )
            assert info.date_time == FIXED_DOS_TIME
            assert info.create_system == 3
            assert info.create_version == 20
            assert info.extract_version == 20
            assert info.flag_bits == 0
            assert info.volume == 0
            assert info.compress_type == ZIP_DEFLATED
            assert stat.S_IFMT(info.external_attr >> 16) == stat.S_IFREG
            assert mode == expected_mode
            assert info.internal_attr == 0
            assert info.extra == b""
            assert info.comment == b""


def test_text_line_endings_are_exclusive_and_font_bytes_are_untouched(
        tmp_path):
    font = b"\x00\xfffont\r\nbytes\r\x80"
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "\n".join((
            "Final/_version.py",
            "Final/run.bat",
            "Final/run.cmd",
            "Final/run.ps1",
            "Final/run.command",
            "Final/readme.txt",
            "Final/assets/slate.ttf",
            "",
        )),
        {
            "Final/run.bat": b"one\ntwo\r\nthree\rfour\n",
            "Final/run.cmd": b"one\r\ntwo\n",
            "Final/run.ps1": b"one\rtwo",
            "Final/run.command": b"one\r\ntwo\rthree\n",
            "Final/readme.txt": b"one\r\ntwo\rthree\n",
            "Final/assets/slate.ttf": font,
        },
    )

    archive = _portable_module().build_portable(
        repo,
        tmp_path / "output",
        epoch=FIXED_EPOCH,
    )
    members = _member_map(archive)
    prefix = "yt_final-1.0.0/Final/"

    assert members[prefix + "run.bat"] == (
        b"one\r\ntwo\r\nthree\r\nfour\r\n"
    )
    assert members[prefix + "run.cmd"] == b"one\r\ntwo\r\n"
    assert members[prefix + "run.ps1"] == b"one\r\ntwo"
    assert members[prefix + "run.command"] == b"one\ntwo\nthree\n"
    assert members[prefix + "readme.txt"] == b"one\ntwo\nthree\n"
    assert members[prefix + "assets/slate.ttf"] == font
    assert b"\r\r\n" not in members[prefix + "run.bat"]
    assert b"\r" not in members[prefix + "run.command"]


def test_text_members_must_be_strict_utf8(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\nFinal/readme.txt\n",
        {"Final/readme.txt": b"\xffnot-utf8"},
    )

    with pytest.raises(UnicodeDecodeError):
        _portable_module().build_portable(
            repo,
            tmp_path / "output",
            epoch=FIXED_EPOCH,
        )


def test_odd_epoch_seconds_floor_to_dos_resolution(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    module = _portable_module()

    even = module.build_portable(
        repo,
        tmp_path / "even",
        epoch=FIXED_EPOCH,
    )
    odd = module.build_portable(
        repo,
        tmp_path / "odd",
        epoch=FIXED_EPOCH + 1,
    )

    with ZipFile(even) as even_zip, ZipFile(odd) as odd_zip:
        assert even_zip.infolist()[0].date_time == FIXED_DOS_TIME
        assert odd_zip.infolist()[0].date_time == FIXED_DOS_TIME
    assert even.read_bytes() == odd.read_bytes()


@pytest.mark.parametrize(
    "epoch",
    (
        315_532_799,  # one second before 1980-01-01 UTC
        4_354_819_200,  # 2108-01-01 UTC
        True,
        1.5,
    ),
)
def test_zip_epoch_outside_dos_range_or_non_integer_is_rejected(
        tmp_path, epoch):
    repo = _write_synthetic_repo(
        tmp_path / f"repo-{str(epoch).replace('.', '-')}",
        "Final/_version.py\n",
    )

    with pytest.raises((TypeError, ValueError)):
        _portable_module().build_portable(
            repo,
            tmp_path / "output",
            epoch=epoch,
        )


def test_zip_epoch_uses_utc_not_process_timezone(tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    module = _portable_module()
    original_tz = os.environ.get("TZ")
    built = []
    try:
        for zone in ("UTC0", "PST8PDT"):
            monkeypatch.setenv("TZ", zone)
            if hasattr(time, "tzset"):
                time.tzset()
            built.append(module.build_portable(
                repo,
                tmp_path / zone,
                epoch=FIXED_EPOCH,
            ))
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(time, "tzset"):
            time.tzset()

    assert built[0].read_bytes() == built[1].read_bytes()
    with ZipFile(built[0]) as archive:
        assert archive.infolist()[0].date_time == FIXED_DOS_TIME


def test_build_atomically_replaces_existing_archive(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    output = tmp_path / "output"
    output.mkdir()
    destination = output / "yt_final-1.0.0-portable.zip"
    destination.write_bytes(b"stale")

    result = _portable_module().build_portable(
        repo,
        output,
        epoch=FIXED_EPOCH,
    )

    assert result == destination
    assert result.read_bytes().startswith(b"PK")
    assert sorted(path.name for path in output.iterdir()) == [
        "yt_final-1.0.0-portable.zip"
    ]


def test_output_inside_recursive_source_is_rejected_on_every_build(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\nFinal/webui/**\n",
        {"Final/webui/app.js": "console.log('inside');\n"},
    )
    output = repo / "Final" / "webui" / "release-output"
    module = _portable_module()

    for _attempt in range(2):
        with pytest.raises(ValueError, match="output|recursive|overlap"):
            module.build_portable(
                repo,
                output,
                epoch=FIXED_EPOCH,
            )

    assert not output.exists()


def test_canonical_output_alias_into_recursive_source_is_rejected(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\nFinal/webui/**\n",
        {"Final/webui/app.js": "console.log('inside');\n"},
    )
    target = repo / "Final" / "webui"
    alias = tmp_path / "webui-alias"
    if os.name == "nt":
        result = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(alias),
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(
                "junction creation unavailable: "
                f"{result.stdout}{result.stderr}"
            )
    else:
        os.symlink(target, alias, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="output|recursive|overlap"):
            _portable_module().build_portable(
                repo,
                alias / "release-output",
                epoch=FIXED_EPOCH,
            )
    finally:
        if os.name == "nt":
            os.rmdir(alias)
        else:
            alias.unlink()


def test_output_alias_inserted_after_overlap_review_is_rejected(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\nFinal/webui/**\n",
        {"Final/webui/app.js": "console.log('inside');\n"},
    )
    target = repo / "Final" / "webui"
    output = tmp_path / "late-output-alias"
    module = _portable_module()
    original = module._read_version
    inserted = False

    def insert_alias_after_overlap(root, *args, **kwargs):
        nonlocal inserted
        if os.name == "nt":
            result = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(output),
                    str(target),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                pytest.skip(
                    "junction creation unavailable: "
                    f"{result.stdout}{result.stderr}"
                )
        else:
            os.symlink(target, output, target_is_directory=True)
        inserted = True
        return original(root, *args, **kwargs)

    monkeypatch.setattr(module, "_read_version", insert_alias_after_overlap)
    generated = target / "yt_final-1.0.0-portable.zip"
    try:
        with pytest.raises(
                ValueError,
                match="output|recursive|anchor|identity|changed"):
            module.build_portable(
                repo,
                output,
                epoch=FIXED_EPOCH,
            )
    finally:
        if generated.exists():
            generated.unlink()
        if inserted:
            if os.name == "nt":
                os.rmdir(output)
            else:
                output.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths")
def test_windows_extended_length_repository_root_is_accepted(tmp_path):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\nruntime/**\n",
        {"runtime/payload.txt": "portable\n"},
    )
    extended = Path(f"\\\\?\\{repo.resolve()}")

    archive = _portable_module().build_portable(
        extended,
        tmp_path / "output",
        epoch=FIXED_EPOCH,
    )

    with ZipFile(archive) as built:
        assert built.read(
            "yt_final-1.0.0/runtime/payload.txt"
        ) == b"portable\n"


def test_zip_is_written_through_open_descriptor_and_fsynced(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    module = _portable_module()
    real_zipfile = module.ZipFile
    real_fsync = module.os.fsync
    observed = {"file_object": False, "fsync": 0}

    def audited_zipfile(file, *args, **kwargs):
        observed["file_object"] = (
            hasattr(file, "write")
            and not isinstance(file, (str, bytes, os.PathLike))
        )
        return real_zipfile(file, *args, **kwargs)

    def audited_fsync(descriptor):
        observed["fsync"] += 1
        return real_fsync(descriptor)

    monkeypatch.setattr(module, "ZipFile", audited_zipfile)
    monkeypatch.setattr(module.os, "fsync", audited_fsync)

    result = module.build_portable(
        repo,
        tmp_path / "output",
        epoch=FIXED_EPOCH,
    )

    assert result.is_file()
    assert observed == {"file_object": True, "fsync": 1}


def test_temp_path_substitution_never_overwrites_victim(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    output = tmp_path / "output"
    victim = tmp_path / "victim.bin"
    original_victim = b"do-not-overwrite"
    victim.write_bytes(original_victim)
    module = _portable_module()
    real_mkstemp = module.tempfile.mkstemp
    real_zipfile = module.ZipFile
    captured = {}
    attack = {"attempted": False, "succeeded": False}

    def tracked_mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        captured["path"] = Path(name)
        return descriptor, name

    def substituting_zipfile(file, *args, **kwargs):
        attack["attempted"] = True
        temporary = captured["path"]
        try:
            temporary.unlink()
            os.link(victim, temporary)
            attack["succeeded"] = True
        except PermissionError:
            pass
        return real_zipfile(file, *args, **kwargs)

    monkeypatch.setattr(module.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(module, "ZipFile", substituting_zipfile)

    if os.name != "nt":
        with pytest.raises(ValueError, match="temporary|identity|changed"):
            module.build_portable(
                repo,
                output,
                epoch=FIXED_EPOCH,
            )
    else:
        try:
            module.build_portable(
                repo,
                output,
                epoch=FIXED_EPOCH,
            )
        except ValueError:
            pass

    assert attack["attempted"]
    assert victim.read_bytes() == original_victim
    if attack["succeeded"]:
        assert not (output / "yt_final-1.0.0-portable.zip").exists()


@pytest.mark.parametrize(
    "prior_bytes",
    (None, b"previous-valid-archive"),
    ids=("absent", "existing"),
)
@_skip_without_held_descriptor_restore
def test_zip_replace_boundary_substitution_restores_safe_destination(
        tmp_path, monkeypatch, prior_bytes):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    output = tmp_path / "output"
    output.mkdir()
    destination = output / "yt_final-1.0.0-portable.zip"
    if prior_bytes is not None:
        destination.write_bytes(prior_bytes)
    victim = tmp_path / "victim.bin"
    original_victim = b"release-victim"
    victim.write_bytes(original_victim)
    victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
    module = _portable_module()
    real_replace = module.os.replace
    attacked = False

    def substitute_inside_replace(source, target):
        nonlocal attacked
        source_path = Path(source)
        destination_path = Path(target)
        if (
            not attacked
            and source_path.suffix == ".tmp"
            and destination_path.suffix == ".zip"
        ):
            source_path.unlink()
            os.link(victim, source_path)
            attacked = True
        return real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", substitute_inside_replace)

    with pytest.raises(ValueError, match="publish|destination|identity|content"):
        module.build_portable(
            repo,
            output,
            epoch=FIXED_EPOCH,
        )

    assert attacked
    assert victim.read_bytes() == original_victim
    assert (victim.stat().st_dev, victim.stat().st_ino) == victim_identity
    if prior_bytes is None:
        assert not os.path.lexists(destination)
        expected_names = []
    else:
        if os.path.lexists(destination):
            assert destination.read_bytes() == prior_bytes
            expected_names = [destination.name]
        else:
            expected_names = []
    assert sorted(path.name for path in output.iterdir()) == expected_names

    renamed = tmp_path / "renamed-output"
    output.rename(renamed)
    renamed.rename(output)

    monkeypatch.setattr(module.os, "replace", real_replace)
    result = module.build_portable(
        repo,
        output,
        epoch=FIXED_EPOCH,
    )

    assert result == destination
    assert result.read_bytes().startswith(b"PK")
    assert victim.read_bytes() == original_victim


def test_zip_poisoned_backup_restore_fails_closed_and_retries(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    output = tmp_path / "output"
    output.mkdir()
    destination = output / "yt_final-1.0.0-portable.zip"
    prior_bytes = b"previous-valid-archive"
    destination.write_bytes(prior_bytes)
    victim = tmp_path / "publication-victim.bin"
    poison = tmp_path / "restore-poison.bin"
    victim.write_bytes(b"publication-victim")
    poison.write_bytes(b"restore-poison")
    victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
    poison_identity = (poison.stat().st_dev, poison.stat().st_ino)
    module = _portable_module()
    real_replace = module.os.replace
    real_restore = module._DirectoryAnchor.rename_held_child
    publication_attacked = False
    restore_attacked = False

    def substitute_publication(source, target, *args, **kwargs):
        nonlocal publication_attacked
        source_path = Path(source)
        target_path = Path(target)
        if (
            not publication_attacked
            and source_path.suffix == ".tmp"
            and target_path == destination
        ):
            source_path.unlink()
            os.link(victim, source_path)
            publication_attacked = True
        return real_replace(source, target, *args, **kwargs)

    def poison_restore(self, descriptor, source, target):
        nonlocal restore_attacked
        backup_path = self.path / source.name
        target_path = self.path / target.name
        backup_path.unlink()
        os.link(poison, backup_path)
        real_replace(backup_path, target_path)
        restore_attacked = True
        raise ValueError("injected poisoned rollback restore")

    monkeypatch.setattr(module.os, "replace", substitute_publication)
    monkeypatch.setattr(
        module._DirectoryAnchor,
        "rename_held_child",
        poison_restore,
    )

    with pytest.raises(ValueError, match="rollback|restore|publish"):
        module.build_portable(
            repo,
            output,
            epoch=FIXED_EPOCH,
        )

    assert publication_attacked
    assert restore_attacked
    if os.path.lexists(destination):
        assert destination.read_bytes() == prior_bytes
    assert victim.read_bytes() == b"publication-victim"
    assert poison.read_bytes() == b"restore-poison"
    assert (victim.stat().st_dev, victim.stat().st_ino) == victim_identity
    assert (poison.stat().st_dev, poison.stat().st_ino) == poison_identity
    assert not any(
        path.name.endswith((".tmp", ".rollback"))
        for path in output.iterdir()
    )

    renamed = tmp_path / "renamed-output"
    output.rename(renamed)
    renamed.rename(output)

    monkeypatch.setattr(module.os, "replace", real_replace)
    monkeypatch.setattr(
        module._DirectoryAnchor,
        "rename_held_child",
        real_restore,
    )
    result = module.build_portable(
        repo,
        output,
        epoch=FIXED_EPOCH,
    )

    assert result.read_bytes().startswith(b"PK")


@_skip_without_held_descriptor_restore
def test_zip_late_swap_after_backup_restore_fails_closed_and_retries(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    output = tmp_path / "output"
    output.mkdir()
    destination = output / "yt_final-1.0.0-portable.zip"
    prior_bytes = b"previous-valid-archive"
    destination.write_bytes(prior_bytes)
    victim = tmp_path / "publication-victim.bin"
    poison = tmp_path / "late-restore-poison.bin"
    victim.write_bytes(b"publication-victim")
    poison.write_bytes(b"late-restore-poison")
    victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
    poison_identity = (poison.stat().st_dev, poison.stat().st_ino)
    module = _portable_module()
    real_replace = module.os.replace
    real_restore = module._DirectoryAnchor.rename_held_child
    real_digest = module._digest_descriptor
    publication_attacked = False
    restore_returned = False
    restore_descriptor = None
    late_swap_attacked = False

    def substitute_publication(source, target, *args, **kwargs):
        nonlocal publication_attacked
        source_path = Path(source)
        if (
            not publication_attacked
            and source_path.suffix == ".tmp"
            and Path(target) == destination
        ):
            source_path.unlink()
            os.link(victim, source_path)
            publication_attacked = True
        return real_replace(source, target, *args, **kwargs)

    def track_restore(self, descriptor, source, target):
        nonlocal restore_returned, restore_descriptor
        result = real_restore(self, descriptor, source, target)
        restore_descriptor = descriptor
        restore_returned = True
        return result

    def swap_during_post_restore_digest(descriptor):
        nonlocal late_swap_attacked
        digest = real_digest(descriptor)
        if (
            restore_returned
            and descriptor == restore_descriptor
            and not late_swap_attacked
        ):
            destination.unlink()
            os.link(poison, destination)
            late_swap_attacked = True
        return digest

    monkeypatch.setattr(module.os, "replace", substitute_publication)
    monkeypatch.setattr(
        module._DirectoryAnchor,
        "rename_held_child",
        track_restore,
    )
    monkeypatch.setattr(
        module,
        "_digest_descriptor",
        swap_during_post_restore_digest,
    )

    with pytest.raises((OSError, ValueError)):
        module.build_portable(
            repo,
            output,
            epoch=FIXED_EPOCH,
        )

    assert publication_attacked
    assert restore_returned
    assert late_swap_attacked
    if os.path.lexists(destination):
        assert destination.read_bytes() == prior_bytes
    assert victim.read_bytes() == b"publication-victim"
    assert poison.read_bytes() == b"late-restore-poison"
    assert (victim.stat().st_dev, victim.stat().st_ino) == victim_identity
    assert (poison.stat().st_dev, poison.stat().st_ino) == poison_identity
    assert not any(
        path.name.endswith((".tmp", ".rollback"))
        for path in output.iterdir()
    )

    renamed = tmp_path / "renamed-output"
    output.rename(renamed)
    renamed.rename(output)

    monkeypatch.setattr(module.os, "replace", real_replace)
    monkeypatch.setattr(
        module._DirectoryAnchor,
        "rename_held_child",
        real_restore,
    )
    monkeypatch.setattr(module, "_digest_descriptor", real_digest)
    result = module.build_portable(
        repo,
        output,
        epoch=FIXED_EPOCH,
    )

    assert result.read_bytes().startswith(b"PK")


@pytest.mark.skipif(os.name == "nt", reason="native POSIX restore semantics")
def test_posix_restore_without_linkat_puts_the_prior_artifact_back(
        tmp_path, monkeypatch):
    # macOS and the BSDs reach rename_held_child's renameat() fallback because they have
    # neither linkat(AT_EMPTY_PATH) nor /proc/self/fd; forcing the platform name runs that
    # branch on Linux too. Before the fallback existed, _rollback_publication unlinked the
    # destination and then raised ENOTSUP, destroying the previously published archive that
    # the rollback exists to put back.
    if os.rename not in os.supports_dir_fd:
        pytest.skip("renameat with a directory descriptor is unavailable")
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    output = tmp_path / "output"
    output.mkdir()
    destination = output / "yt_final-1.0.0-portable.zip"
    prior_bytes = b"previous-valid-archive"
    destination.write_bytes(prior_bytes)
    victim = tmp_path / "publication-victim.bin"
    victim.write_bytes(b"publication-victim")
    victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
    module = _portable_module()
    real_replace = module.os.replace
    publication_attacked = False

    def substitute_publication(source, target, *args, **kwargs):
        nonlocal publication_attacked
        source_path = Path(source)
        if (
            not publication_attacked
            and source_path.suffix == ".tmp"
            and Path(target) == destination
        ):
            source_path.unlink()
            os.link(victim, source_path)
            publication_attacked = True
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.os, "replace", substitute_publication)

    with pytest.raises(ValueError, match="publish|destination|identity|content"):
        module.build_portable(
            repo,
            output,
            epoch=FIXED_EPOCH,
        )

    assert publication_attacked
    assert destination.read_bytes() == prior_bytes
    assert sorted(path.name for path in output.iterdir()) == [destination.name]
    assert victim.read_bytes() == b"publication-victim"
    assert (victim.stat().st_dev, victim.stat().st_ino) == victim_identity

    monkeypatch.setattr(module.os, "replace", real_replace)
    result = module.build_portable(
        repo,
        output,
        epoch=FIXED_EPOCH,
    )

    assert result == destination
    assert result.read_bytes().startswith(b"PK")


@pytest.mark.skipif(os.name == "nt", reason="native POSIX restore semantics")
def test_posix_restore_uses_held_backup_after_rollback_name_swap(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    output = tmp_path / "output"
    output.mkdir()
    destination = output / "yt_final-1.0.0-portable.zip"
    prior_bytes = b"previous-valid-archive"
    destination.write_bytes(prior_bytes)
    victim = tmp_path / "publication-victim.bin"
    poison = tmp_path / "restore-poison.bin"
    victim.write_bytes(b"publication-victim")
    poison.write_bytes(b"restore-poison")
    module = _portable_module()
    real_replace = module.os.replace
    real_verify = module._DirectoryAnchor.verify_file_descriptor
    publication_attacked = False
    rollback_verifications = 0
    restore_attacked = False

    def substitute_publication(source, target, *args, **kwargs):
        nonlocal publication_attacked
        source_path = Path(source)
        if (
            not publication_attacked
            and source_path.suffix == ".tmp"
            and Path(target) == destination
        ):
            source_path.unlink()
            os.link(victim, source_path)
            publication_attacked = True
        return real_replace(source, target, *args, **kwargs)

    def swap_after_backup_verification(self, descriptor, relative):
        nonlocal rollback_verifications, restore_attacked
        identity = real_verify(self, descriptor, relative)
        if relative.name.endswith(".rollback"):
            rollback_verifications += 1
            if rollback_verifications == 4:
                backup_path = self.path / relative.name
                backup_path.unlink()
                os.link(poison, backup_path)
                restore_attacked = True
        return identity

    monkeypatch.setattr(module.os, "replace", substitute_publication)
    monkeypatch.setattr(
        module._DirectoryAnchor,
        "verify_file_descriptor",
        swap_after_backup_verification,
    )

    with pytest.raises((OSError, ValueError)):
        module.build_portable(
            repo,
            output,
            epoch=FIXED_EPOCH,
        )

    assert publication_attacked
    assert restore_attacked
    if os.path.lexists(destination):
        assert destination.read_bytes() == prior_bytes
    assert victim.read_bytes() == b"publication-victim"
    assert poison.read_bytes() == b"restore-poison"
    assert not any(
        path.name.endswith((".tmp", ".rollback"))
        for path in output.iterdir()
    )

    renamed = tmp_path / "renamed-output"
    output.rename(renamed)
    renamed.rename(output)


def test_zip_final_digest_swap_restores_absent_destination(
        tmp_path, monkeypatch):
    repo = _write_synthetic_repo(
        tmp_path / "repo",
        "Final/_version.py\n",
    )
    output = tmp_path / "output"
    victim = tmp_path / "victim.bin"
    original_victim = b"late-release-victim"
    victim.write_bytes(original_victim)
    victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
    destination = output / "yt_final-1.0.0-portable.zip"
    module = _portable_module()
    real_digest = module._digest_descriptor
    named_digest_calls = 0
    attempted = False

    def swap_during_final_digest(descriptor):
        nonlocal named_digest_calls, attempted
        result = real_digest(descriptor)
        try:
            named = destination.stat()
        except FileNotFoundError:
            return result
        opened = os.fstat(descriptor)
        if (
            (named.st_dev, named.st_ino)
            == (opened.st_dev, opened.st_ino)
        ):
            named_digest_calls += 1
            if named_digest_calls == 2:
                attempted = True
                destination.unlink()
                os.link(victim, destination)
        return result

    monkeypatch.setattr(
        module,
        "_digest_descriptor",
        swap_during_final_digest,
    )

    with pytest.raises((OSError, ValueError)):
        module.build_portable(
            repo,
            output,
            epoch=FIXED_EPOCH,
        )

    assert attempted
    assert not os.path.lexists(destination)
    assert victim.read_bytes() == original_victim
    assert (victim.stat().st_dev, victim.stat().st_ino) == victim_identity
    assert list(output.iterdir()) == []

    renamed = tmp_path / "renamed-output"
    output.rename(renamed)
    renamed.rename(output)

    monkeypatch.setattr(module, "_digest_descriptor", real_digest)
    result = module.build_portable(
        repo,
        output,
        epoch=FIXED_EPOCH,
    )

    assert result.read_bytes().startswith(b"PK")


def test_sha256_file_streams_complete_regular_file(tmp_path):
    payload = (bytes(range(256)) * 8193) + b"tail"
    artifact = tmp_path / "large.zip"
    artifact.write_bytes(payload)

    digest = _checksum_module().sha256_file(artifact)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert len(digest) == 64
    assert digest == digest.lower()


def test_sha256_file_rejects_named_identity_change_after_read(
        tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.zip"
    replacement = tmp_path / "replacement.zip"
    artifact.write_bytes(b"reviewed-artifact")
    replacement.write_bytes(b"replacement-artifact")
    module = _checksum_module()
    original = module._regular_metadata
    calls = 0

    def changed_named_metadata(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            return original(replacement)
        return original(path)

    monkeypatch.setattr(module, "_regular_metadata", changed_named_metadata)

    with pytest.raises(ValueError, match="identity|changed|source"):
        module.sha256_file(artifact)


@pytest.mark.skipif(os.name == "nt", reason="native POSIX rename semantics")
def test_posix_sha256_file_rejects_real_rename_after_read(
        tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.zip"
    parked = tmp_path / "reviewed.zip"
    replacement = tmp_path / "replacement.zip"
    artifact.write_bytes(b"reviewed-artifact")
    replacement.write_bytes(b"replacement-artifact")
    module = _checksum_module()
    original = module._regular_metadata
    calls = 0

    def rename_before_final_identity(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            artifact.rename(parked)
            replacement.rename(artifact)
        return original(path)

    monkeypatch.setattr(
        module,
        "_regular_metadata",
        rename_before_final_identity,
    )

    with pytest.raises(ValueError, match="identity|changed|source"):
        module.sha256_file(artifact)


def test_sha256sums_is_sorted_lf_only_and_input_order_independent(tmp_path):
    first = tmp_path / "zeta.zip"
    second = tmp_path / "alpha.whl"
    first.write_bytes(b"zeta")
    second.write_bytes(b"alpha")
    expected = (
        f"{hashlib.sha256(b'alpha').hexdigest()}  alpha.whl\n"
        f"{hashlib.sha256(b'zeta').hexdigest()}  zeta.zip\n"
    ).encode("utf-8")
    module = _checksum_module()

    one = module.write_sha256sums(
        [first, second],
        tmp_path / "one" / "SHA256SUMS",
    )
    two = module.write_sha256sums(
        [second, first],
        tmp_path / "two" / "SHA256SUMS",
    )

    assert one.read_bytes() == expected
    assert two.read_bytes() == expected
    assert b"\r" not in expected
    assert not expected.startswith(b"\xef\xbb\xbf")
    assert expected.endswith(b"\n")


@pytest.mark.parametrize(
    "prior_bytes",
    (None, b"previous valid checksum manifest\n"),
    ids=("absent", "existing"),
)
@_skip_without_held_descriptor_restore
def test_checksum_replace_boundary_substitution_restores_safe_destination(
        tmp_path, monkeypatch, prior_bytes):
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact")
    victim = tmp_path / "victim.bin"
    original_victim = b"checksum-victim"
    victim.write_bytes(original_victim)
    victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
    destination = tmp_path / "SHA256SUMS"
    if prior_bytes is not None:
        destination.write_bytes(prior_bytes)
    module = _checksum_module()
    real_replace = module.os.replace
    attacked = False

    def substitute_inside_replace(source, target):
        nonlocal attacked
        source_path = Path(source)
        if not attacked and Path(target) == destination:
            source_path.unlink()
            os.link(victim, source_path)
            attacked = True
        return real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", substitute_inside_replace)

    with pytest.raises(ValueError, match="publish|destination|identity|content"):
        module.write_sha256sums([artifact], destination)

    assert attacked
    assert victim.read_bytes() == original_victim
    assert (victim.stat().st_dev, victim.stat().st_ino) == victim_identity
    if prior_bytes is None:
        assert not os.path.lexists(destination)
        expected_names = ["artifact.zip", "victim.bin"]
    else:
        if os.path.lexists(destination):
            assert destination.read_bytes() == prior_bytes
            expected_names = [
                "SHA256SUMS",
                "artifact.zip",
                "victim.bin",
            ]
        else:
            expected_names = ["artifact.zip", "victim.bin"]
    assert sorted(path.name for path in tmp_path.iterdir()) == expected_names

    renamed = tmp_path.parent / f"{tmp_path.name}-renamed"
    tmp_path.rename(renamed)
    renamed.rename(tmp_path)

    monkeypatch.setattr(module.os, "replace", real_replace)
    result = module.write_sha256sums([artifact], destination)
    expected = (
        f"{hashlib.sha256(b'artifact').hexdigest()}  artifact.zip\n"
    ).encode("utf-8")

    assert result == destination
    assert result.read_bytes() == expected
    assert victim.read_bytes() == original_victim


def test_checksum_final_digest_swap_restores_absent_destination(
        tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact")
    victim = tmp_path / "victim.bin"
    original_victim = b"late-checksum-victim"
    victim.write_bytes(original_victim)
    victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
    destination = tmp_path / "SHA256SUMS"
    module = _checksum_module()
    build_module = _portable_module()
    real_digest = build_module._digest_descriptor
    named_digest_calls = 0
    attempted = False

    def swap_during_final_digest(descriptor):
        nonlocal named_digest_calls, attempted
        result = real_digest(descriptor)
        try:
            named = destination.stat()
        except FileNotFoundError:
            return result
        opened = os.fstat(descriptor)
        if (
            (named.st_dev, named.st_ino)
            == (opened.st_dev, opened.st_ino)
        ):
            named_digest_calls += 1
            if named_digest_calls == 2:
                attempted = True
                destination.unlink()
                os.link(victim, destination)
        return result

    monkeypatch.setattr(
        build_module,
        "_digest_descriptor",
        swap_during_final_digest,
    )

    with pytest.raises((OSError, ValueError)):
        module.write_sha256sums([artifact], destination)

    assert attempted
    assert not os.path.lexists(destination)
    assert victim.read_bytes() == original_victim
    assert (victim.stat().st_dev, victim.stat().st_ino) == victim_identity
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "artifact.zip",
        "victim.bin",
    ]

    monkeypatch.setattr(build_module, "_digest_descriptor", real_digest)
    result = module.write_sha256sums([artifact], destination)
    expected = (
        f"{hashlib.sha256(b'artifact').hexdigest()}  artifact.zip\n"
    ).encode("utf-8")

    assert result.read_bytes() == expected


def test_checksum_close_error_does_not_skip_manifest_rollback(
        tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact")
    victim = tmp_path / "victim.bin"
    original_victim = b"close-error-victim"
    victim.write_bytes(original_victim)
    destination = tmp_path / "SHA256SUMS"
    prior_bytes = b"previous close-safe manifest\n"
    destination.write_bytes(prior_bytes)
    payload = (
        f"{hashlib.sha256(b'artifact').hexdigest()}  artifact.zip\n"
    ).encode("utf-8")
    payload_digest = hashlib.sha256(payload).hexdigest()
    module = _checksum_module()
    build_module = _portable_module()
    real_digest = build_module._digest_descriptor
    real_close = build_module.os.close
    named_digest_calls = 0
    final_descriptor = None
    close_error_injected = False

    def swap_during_final_digest(descriptor):
        nonlocal named_digest_calls, final_descriptor
        result = real_digest(descriptor)
        try:
            named = destination.stat()
        except FileNotFoundError:
            return result
        opened = os.fstat(descriptor)
        if (
            result == payload_digest
            and (named.st_dev, named.st_ino)
            == (opened.st_dev, opened.st_ino)
        ):
            named_digest_calls += 1
            if named_digest_calls == 2:
                final_descriptor = descriptor
                destination.unlink()
                os.link(victim, destination)
        return result

    def close_then_report_error(descriptor):
        nonlocal close_error_injected
        if (
            not close_error_injected
            and descriptor == final_descriptor
        ):
            close_error_injected = True
            real_close(descriptor)
            raise OSError("injected final guard close error")
        return real_close(descriptor)

    monkeypatch.setattr(
        build_module,
        "_digest_descriptor",
        swap_during_final_digest,
    )
    monkeypatch.setattr(build_module.os, "close", close_then_report_error)

    with pytest.raises((OSError, ValueError)) as failure:
        module.write_sha256sums([artifact], destination)

    assert final_descriptor is not None
    assert close_error_injected
    if os.path.lexists(destination):
        assert destination.read_bytes() == prior_bytes, repr(failure.value)
    assert victim.read_bytes() == original_victim
    assert not any(
        path.name.endswith((".tmp", ".rollback"))
        for path in tmp_path.iterdir()
    )

    monkeypatch.setattr(build_module, "_digest_descriptor", real_digest)
    monkeypatch.setattr(build_module.os, "close", real_close)
    result = module.write_sha256sums([artifact], destination)

    assert result.read_text(encoding="utf-8").endswith("  artifact.zip\n")


@pytest.mark.parametrize(
    "prior_bytes",
    (None, b"previous valid manifest\n"),
    ids=("absent", "existing"),
)
def test_sha256sums_rejects_artifact_replaced_after_earlier_hash(
        tmp_path, monkeypatch, prior_bytes):
    first = tmp_path / "first.zip"
    second = tmp_path / "second.whl"
    replacement = tmp_path / "replacement.bin"
    parked = tmp_path / "parked-original.bin"
    first.write_bytes(b"reviewed-first")
    second.write_bytes(b"reviewed-second")
    replacement.write_bytes(b"replacement-first")
    destination = tmp_path / "SHA256SUMS"
    if prior_bytes is not None:
        destination.write_bytes(prior_bytes)
    module = _checksum_module()
    real_sha256_file = module.sha256_file
    attacked = False

    def replace_after_first_hash(path):
        nonlocal attacked
        digest = real_sha256_file(path)
        if not attacked and Path(path) == first:
            first.rename(parked)
            replacement.rename(first)
            attacked = True
        return digest

    monkeypatch.setattr(
        module,
        "sha256_file",
        replace_after_first_hash,
    )

    with pytest.raises(ValueError, match="artifact|checksum|source|identity"):
        module.write_sha256sums(
            [first, second],
            destination,
        )

    assert attacked
    assert first.read_bytes() == b"replacement-first"
    assert parked.read_bytes() == b"reviewed-first"
    if prior_bytes is None:
        assert not os.path.lexists(destination)
    else:
        if os.path.lexists(destination):
            assert destination.read_bytes() == prior_bytes
    assert not any(
        path.name.endswith((".tmp", ".rollback"))
        for path in tmp_path.iterdir()
    )

    renamed = tmp_path.parent / f"{tmp_path.name}-renamed"
    tmp_path.rename(renamed)
    renamed.rename(tmp_path)

    monkeypatch.setattr(module, "sha256_file", real_sha256_file)
    result = module.write_sha256sums(
        [first, second],
        destination,
    )
    expected = (
        f"{hashlib.sha256(b'replacement-first').hexdigest()}  first.zip\n"
        f"{hashlib.sha256(b'reviewed-second').hexdigest()}  second.whl\n"
    ).encode("utf-8")

    assert result.read_bytes() == expected


@pytest.mark.parametrize(
    "prior_bytes",
    (None, b"previous late manifest\n"),
    ids=("absent", "existing"),
)
@_skip_without_held_descriptor_restore
def test_sha256sums_rolls_back_artifact_swap_after_manifest_publish(
        tmp_path, monkeypatch, prior_bytes):
    artifact = tmp_path / "artifact.zip"
    replacement = tmp_path / "replacement.bin"
    parked = tmp_path / "parked-original.bin"
    artifact.write_bytes(b"reviewed-artifact")
    replacement.write_bytes(b"replacement-artifact")
    destination = tmp_path / "SHA256SUMS"
    if prior_bytes is not None:
        destination.write_bytes(prior_bytes)
    payload = (
        f"{hashlib.sha256(b'reviewed-artifact').hexdigest()}  artifact.zip\n"
    ).encode("utf-8")
    payload_digest = hashlib.sha256(payload).hexdigest()
    module = _checksum_module()
    build_module = _portable_module()
    real_digest = build_module._digest_descriptor
    published_digest_calls = 0
    attempted = False

    def swap_source_during_final_manifest_digest(descriptor):
        nonlocal published_digest_calls, attempted
        result = real_digest(descriptor)
        try:
            named = destination.stat()
        except FileNotFoundError:
            return result
        opened = os.fstat(descriptor)
        if (
            result == payload_digest
            and (named.st_dev, named.st_ino)
            == (opened.st_dev, opened.st_ino)
        ):
            published_digest_calls += 1
            if published_digest_calls == 2:
                attempted = True
                artifact.rename(parked)
                replacement.rename(artifact)
        return result

    monkeypatch.setattr(
        build_module,
        "_digest_descriptor",
        swap_source_during_final_manifest_digest,
    )

    with pytest.raises((OSError, ValueError)):
        module.write_sha256sums([artifact], destination)

    assert attempted
    if prior_bytes is None:
        assert not os.path.lexists(destination)
    else:
        assert destination.read_bytes() == prior_bytes
    assert not any(
        path.name.endswith((".tmp", ".rollback"))
        for path in tmp_path.iterdir()
    )

    monkeypatch.setattr(build_module, "_digest_descriptor", real_digest)
    current_bytes = artifact.read_bytes()
    result = module.write_sha256sums([artifact], destination)
    expected = (
        f"{hashlib.sha256(current_bytes).hexdigest()}  artifact.zip\n"
    ).encode("utf-8")

    assert result.read_bytes() == expected


def test_sha256sums_rechecks_source_after_last_manifest_digest(
        tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.zip"
    decoy = tmp_path / "decoy.bin"
    artifact.write_bytes(b"reviewed-artifact")
    decoy.write_bytes(b"decoy-artifact")
    destination = tmp_path / "SHA256SUMS"
    payload = (
        f"{hashlib.sha256(b'reviewed-artifact').hexdigest()}  artifact.zip\n"
    ).encode("utf-8")
    payload_digest = hashlib.sha256(payload).hexdigest()
    module = _checksum_module()
    build_module = _portable_module()
    real_digest = build_module._digest_descriptor
    real_metadata = module._regular_metadata
    published_digest_calls = 0
    changed = False

    def change_named_identity_after_last_digest(descriptor):
        nonlocal published_digest_calls, changed
        result = real_digest(descriptor)
        try:
            named = destination.stat()
        except FileNotFoundError:
            return result
        opened = os.fstat(descriptor)
        if (
            result == payload_digest
            and (named.st_dev, named.st_ino)
            == (opened.st_dev, opened.st_ino)
        ):
            published_digest_calls += 1
            if published_digest_calls == 3:
                changed = True
        return result

    def changed_named_metadata(path):
        if changed and Path(path) == artifact:
            return real_metadata(decoy)
        return real_metadata(path)

    monkeypatch.setattr(
        build_module,
        "_digest_descriptor",
        change_named_identity_after_last_digest,
    )
    monkeypatch.setattr(
        module,
        "_regular_metadata",
        changed_named_metadata,
    )

    with pytest.raises(ValueError, match="artifact|source|identity"):
        module.write_sha256sums([artifact], destination)

    assert changed
    assert not os.path.lexists(destination)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "artifact.zip",
        "decoy.bin",
    ]


@pytest.mark.parametrize(
    "kind",
    ("empty", "missing", "directory", "self", "duplicate", "casefold"),
)
def test_sha256sums_rejects_invalid_or_ambiguous_inputs(tmp_path, kind):
    module = _checksum_module()
    destination = tmp_path / "SHA256SUMS"
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact")
    paths = [artifact]
    if kind == "empty":
        paths = []
    elif kind == "missing":
        paths = [tmp_path / "missing.zip"]
    elif kind == "directory":
        directory = tmp_path / "directory.zip"
        directory.mkdir()
        paths = [directory]
    elif kind == "self":
        destination.write_bytes(b"old")
        paths = [destination]
    elif kind == "duplicate":
        other = tmp_path / "other"
        other.mkdir()
        duplicate = other / artifact.name
        duplicate.write_bytes(b"other")
        paths = [artifact, duplicate]
    elif kind == "casefold":
        upper = tmp_path / "Artifact.zip"
        upper.write_bytes(b"upper")
        paths = [artifact, upper]

    with pytest.raises((FileNotFoundError, ValueError)):
        module.write_sha256sums(paths, destination)


def test_sha256sums_rejects_portable_name_collision_with_destination(
        tmp_path):
    source_directory = tmp_path / "artifacts"
    destination_directory = tmp_path / "manifests"
    source_directory.mkdir()
    artifact = source_directory / "sha256sums"
    artifact.write_bytes(b"not-the-manifest")

    with pytest.raises(ValueError, match="destination|collision|SHA256SUMS"):
        _checksum_module().write_sha256sums(
            [artifact],
            destination_directory / "SHA256SUMS",
        )


def test_sha256sums_rejects_control_character_display_name(tmp_path):
    module = _checksum_module()
    bad = tmp_path / "bad\nname.zip"
    try:
        bad.write_bytes(b"bad")
    except OSError as exc:
        pytest.skip(f"control-character filename unavailable: {exc}")

    with pytest.raises(ValueError, match="control"):
        module.write_sha256sums([bad], tmp_path / "SHA256SUMS")


@pytest.mark.parametrize(
    "name",
    (
        "artifact.zip:secret",
        "artifact.zip.",
        "artifact.zip ",
        "CON",
        "nul.zip",
        "bad<name>.zip",
        "control\u0085.zip",
        "line\u2029separator.zip",
        "bidi\u2066name.zip",
    ),
)
def test_checksum_names_share_portable_windows_validation(name):
    with pytest.raises(ValueError, match="portable|component|Windows|control"):
        _checksum_module()._display_name(Path(name))


def test_checksum_duplicate_names_use_unicode_normalization(tmp_path):
    composed = tmp_path / "caf\u00e9.zip"
    decomposed = tmp_path / "cafe\u0301.zip"
    composed.write_bytes(b"composed")
    try:
        decomposed.write_bytes(b"decomposed")
    except OSError as exc:
        pytest.skip(f"normalization-distinct filenames unavailable: {exc}")
    try:
        if os.path.samefile(composed, decomposed):
            pytest.skip("filesystem aliases normalized Unicode filenames")
    except OSError:
        pass

    with pytest.raises(ValueError, match="duplicate|collision"):
        _checksum_module().write_sha256sums(
            [composed, decomposed],
            tmp_path / "SHA256SUMS",
        )


@pytest.mark.skipif(os.name != "nt", reason="NTFS filename semantics")
def test_native_checksum_rejects_ads_alias_and_unicode_control_names(tmp_path):
    module = _checksum_module()
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"artifact")
    ads = Path(f"{artifact}:secret")
    try:
        ads.write_bytes(b"secret")
    except OSError as exc:
        pytest.skip(f"NTFS alternate streams unavailable: {exc}")

    for source in (
        ads,
        Path(f"{artifact}."),
    ):
        with pytest.raises(ValueError, match="portable|component|Windows"):
            module.write_sha256sums(
                [source],
                tmp_path / "SHA256SUMS",
            )

    controlled = tmp_path / "artifact\u0085.zip"
    try:
        controlled.write_bytes(b"controlled")
    except OSError as exc:
        pytest.skip(f"C1 filename unavailable: {exc}")
    with pytest.raises(ValueError, match="portable|component|control"):
        module.write_sha256sums(
            [controlled],
            tmp_path / "SHA256SUMS",
        )


def test_checksum_cli_scans_only_top_level_regular_artifacts(tmp_path):
    directory = tmp_path / "artifacts"
    directory.mkdir()
    artifact = directory / "yt_final-1.0.0-portable.zip"
    artifact.write_bytes(b"portable")
    (directory / "SHA256SUMS").write_text(
        "stale\n",
        encoding="utf-8",
    )
    nested = directory / "nested"
    nested.mkdir()
    (nested / "ignored.whl").write_bytes(b"ignored")

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "write_checksums.py"), directory],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (directory / "SHA256SUMS").read_bytes() == (
        f"{hashlib.sha256(b'portable').hexdigest()}"
        "  yt_final-1.0.0-portable.zip\n"
    ).encode("utf-8")


def requires_git_checkout():
    """Skip when ROOT is not a git working tree.

    These assertions are about REPOSITORY state, so they only apply inside a checkout. The source
    also ships as an sdist and a portable zip, where git exits 128 ("not a git repository") and the
    test fails for a reason unrelated to the contract it protects — the same environment-coupling
    class that already reddened this suite once.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip("not a git checkout — repository-state assertions do not apply here")


def test_generated_artifact_directory_is_ignored_by_git():
    requires_git_checkout()
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".artifacts/probe.zip"],
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 0
