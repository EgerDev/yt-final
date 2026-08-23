"""Immutable managed-artifact provenance contract."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest


APPROVED_ARTIFACTS = (
    (
        "ffmpeg",
        "8.1.2",
        "win32",
        "x86_64",
        "https://www.gyan.dev/ffmpeg/builds/packages/"
        "ffmpeg-8.1.2-essentials_build.zip",
        "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec",
        "zip",
        "1326dde4c84ff1f96fe6b8916c5bed29e163e9b5dccf995f6f3db069d143ec5e",
        "b49ccc7c6547b141ad5a2f6ec69cc04323d7133d7704d70b331b904c63eecb07",
    ),
    (
        "node",
        "24.18.0",
        "win32",
        "x86_64",
        "https://nodejs.org/dist/v24.18.0/node-v24.18.0-win-x64.zip",
        "0ae68406b42d7725661da979b1403ec9926da205c6770827f33aac9d8f26e821",
        "zip",
        "9a4eb5f1c29c6a2e93852ead46b999e284a6a5ca8bab4d4e241d587d025a52de",
        None,
    ),
    (
        "node",
        "24.18.0",
        "darwin",
        "arm64",
        "https://nodejs.org/dist/v24.18.0/node-v24.18.0-darwin-arm64.tar.gz",
        "e1a97e14c99c803e96c7339403282ea05a499c32f8d83defe9ef5ec66f979ed1",
        "tar.gz",
        "ee6fb0e015284d83a91e8ec5213f43a157f8a392b58555301682892ba928c04a",
        None,
    ),
    (
        "deno",
        "2.9.4",
        "win32",
        "x86_64",
        "https://dl.deno.land/release/v2.9.4/"
        "deno-x86_64-pc-windows-msvc.zip",
        "68ed08b05c56cf887e9aa509947dc3f468f7e12f47a13e5c1abd51d46d1453ef",
        "zip",
        "4a2757fe99afc2c62c46500c8221cfa0189ac4bfb7064141875ad9c0f04b60ef",
        None,
    ),
    (
        "deno",
        "2.9.4",
        "darwin",
        "arm64",
        "https://dl.deno.land/release/v2.9.4/deno-aarch64-apple-darwin.zip",
        "6d17647fdbf9c587a581dba205054c4ccf732dae0a196cc1e9b44c07589db412",
        "zip",
        "433088c827fa0e39ff162ab0e475f1fd4c7690eaedec500cf678edc3865e9287",
        None,
    ),
)


def _subject():
    try:
        import tool_manifest
    except ModuleNotFoundError:
        pytest.fail("tool_manifest must provide the immutable artifact contract")
    return tool_manifest


def _approved_tuple(artifact):
    return (
        artifact.name,
        artifact.version,
        artifact.platform,
        artifact.architecture,
        artifact.url,
        artifact.sha256,
        artifact.archive_kind,
        artifact.binary_sha256,
        artifact.companion_binary_sha256,
    )


def test_manifest_contains_only_the_five_independently_approved_artifacts():
    """A changed/extra download target must not silently become managed."""
    manifest = _subject()

    actual = tuple(
        _approved_tuple(manifest.artifact_for(name, platform, architecture))
        for (
            name,
            _version,
            platform,
            architecture,
            _url,
            _sha256,
            _kind,
            _binary_sha256,
            _companion_binary_sha256,
        )
        in APPROVED_ARTIFACTS
    )

    assert actual == APPROVED_ARTIFACTS
    assert tuple(_approved_tuple(item) for item in manifest.ARTIFACTS) == APPROVED_ARTIFACTS


def test_tool_artifacts_are_frozen_values():
    """A caller must not be able to rewrite approved provenance in memory."""
    manifest = _subject()
    artifact = manifest.artifact_for("ffmpeg", "win32", "x86_64")

    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.url = "https://attacker.invalid/ffmpeg.zip"


@pytest.mark.parametrize("alias", ["AMD64", "amd64", "x64", "X64", "x86_64"])
def test_windows_x64_architecture_aliases_select_the_same_artifact(alias):
    """Host spelling differences must not bypass or miss the certified x64 entry."""
    manifest = _subject()

    artifact = manifest.artifact_for("node", "win32", alias)

    assert _approved_tuple(artifact) == APPROVED_ARTIFACTS[1]


@pytest.mark.parametrize("alias", ["ARM64", "arm64", "aarch64", "AARCH64"])
def test_macos_arm_architecture_aliases_select_the_same_artifact(alias):
    """Apple/stdlib architecture spellings must resolve to canonical arm64."""
    manifest = _subject()

    artifact = manifest.artifact_for("deno", "darwin", alias)

    assert _approved_tuple(artifact) == APPROVED_ARTIFACTS[4]


@pytest.mark.parametrize(
    ("name", "platform", "architecture"),
    [
        ("missing", "win32", "x86_64"),
        ("ffmpeg", "linux", "x86_64"),
        ("ffmpeg", "darwin", "x86_64"),
        ("ffmpeg", "darwin", "arm64"),
        ("node", "win32", "arm64"),
        ("deno", "darwin", "x86_64"),
    ],
)
def test_missing_or_uncertified_targets_fail_without_filesystem_io(
    monkeypatch, name, platform, architecture
):
    """Unsupported lookup must stop before staging or network/process work."""
    manifest = _subject()
    monkeypatch.setattr(
        Path,
        "open",
        lambda *_args, **_kwargs: pytest.fail("unsupported lookup performed filesystem I/O"),
    )

    with pytest.raises(manifest.UnsupportedManagedInstall):
        manifest.artifact_for(name, platform, architecture)


@pytest.mark.parametrize(
    "name",
    ["aria2c", "N_m3u8DL-RE", "playwright", "browser", "bgutil-server"],
)
def test_manual_only_tools_have_no_automated_manifest_entry(name):
    """Known uncertified helpers must remain guidance-only."""
    manifest = _subject()

    with pytest.raises(manifest.UnsupportedManagedInstall):
        manifest.artifact_for(name, "win32", "x86_64")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("version", ""),
        ("version", 123),
        ("version", None),
        ("platform", "linux"),
        ("architecture", "x64"),
        ("url", "http://nodejs.org/node.zip"),
        ("url", 123),
        ("url", None),
        ("url", "https://example.test:not-a-port/node.zip"),
        ("url", "https://[malformed/node.zip"),
        ("url", "https://example.test/releases/latest/node.zip"),
        ("url", "https://example.test/archive/refs/heads/main.zip"),
        (
            "url",
            "https://raw.githubusercontent.com/example/project/main/node.zip",
        ),
        (
            "url",
            "https://raw.githubusercontent.com/example/project/master/node.zip",
        ),
        ("url", "https://nodejs.org/dist/node-latest.zip"),
        ("url", "https://nodejs.org/download/node.zip"),
        ("url", "https://dl.deno.land/download/deno.zip"),
        ("url", "https://downloads.example.test/tool.zip"),
        ("url", "https://dl.deno.land/stable/deno.zip"),
        ("url", "https://24.18.0.example.test/tool.zip"),
        ("sha256", ""),
        ("sha256", 123),
        ("sha256", None),
        ("sha256", "a" * 63),
        ("sha256", "g" * 64),
        ("binary_sha256", ""),
        ("binary_sha256", 123),
        ("binary_sha256", None),
        ("binary_sha256", "a" * 63),
        ("binary_sha256", "g" * 64),
        ("archive_kind", "tgz"),
    ],
)
def test_malformed_matching_manifest_entry_fails_closed(monkeypatch, field, value):
    """A malformed matching record must never be treated as an approved download."""
    manifest = _subject()
    valid = manifest.artifact_for("node", "win32", "x86_64")
    malformed = dataclasses.replace(valid, **{field: value})
    monkeypatch.setattr(manifest, "ARTIFACTS", (malformed,))

    with pytest.raises(manifest.UnsupportedManagedInstall):
        manifest.artifact_for("node", "win32", "x86_64")


def test_duplicate_matching_manifest_entries_fail_closed(monkeypatch):
    """Ambiguous provenance must not select whichever duplicate happens to be first."""
    manifest = _subject()
    valid = manifest.artifact_for("node", "win32", "x86_64")
    monkeypatch.setattr(manifest, "ARTIFACTS", (valid, valid))

    with pytest.raises(manifest.UnsupportedManagedInstall):
        manifest.artifact_for("node", "win32", "x86_64")


def test_generic_manifest_url_accepts_immutable_commit_in_path(monkeypatch):
    """A reviewed synthetic artifact may use an immutable commit instead of a version."""
    manifest = _subject()
    valid = manifest.artifact_for("node", "win32", "x86_64")
    commit = "a" * 40
    committed = dataclasses.replace(
        valid,
        url=f"https://downloads.example.test/builds/{commit}/node.zip",
    )
    monkeypatch.setattr(manifest, "ARTIFACTS", (committed,))

    assert (
        manifest.artifact_for("node", "win32", "x86_64")
        == committed
    )


@pytest.mark.parametrize(
    'encoded_mutable_path',
    [
        'releases/%6c%61%74%65%73%74',
        'releases/%256c%2561%2574%2565%2573%2574',
        'archive/%6d%61%69%6e',
        'archive/%256d%2561%2569%256e',
        'refs/%68%65%61%64%73/main',
    ],
)
def test_manifest_rejects_percent_encoded_mutable_path_tokens(
        monkeypatch, encoded_mutable_path):
    """URL grammar is enforced on the canonical decoded path, not raw spelling."""
    manifest = _subject()
    valid = manifest.artifact_for("node", "win32", "x86_64")
    disguised = dataclasses.replace(
        valid,
        url=(
            f"https://downloads.example.test/{encoded_mutable_path}/"
            f"v{valid.version}/node.zip"
        ),
    )
    monkeypatch.setattr(manifest, "ARTIFACTS", (disguised,))

    with pytest.raises(manifest.UnsupportedManagedInstall):
        manifest.artifact_for("node", "win32", "x86_64")


def test_verify_sha256_accepts_known_bytes_and_regular_file(tmp_path):
    """Verification must authenticate both staged bytes and staged regular files."""
    manifest = _subject()
    known_sha256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    staged = tmp_path / "artifact.bin"
    staged.write_bytes(b"abc")

    assert manifest.verify_sha256(b"abc", known_sha256) is True
    assert manifest.verify_sha256(staged, known_sha256) is True


@pytest.mark.parametrize("payload", [b"", b"abd"])
def test_verify_sha256_rejects_empty_or_mismatched_bytes(payload):
    """Empty and altered downloads must be typed checksum failures."""
    manifest = _subject()
    known_sha256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

    with pytest.raises(manifest.ChecksumError):
        manifest.verify_sha256(payload, known_sha256)


@pytest.mark.parametrize(
    ("final_url", "redirected"),
    [
        ("http://nodejs.org/dist/v24.18.0/node-v24.18.0-win-x64.zip", False),
        ("https://nodejs.org/dist/v24.18.0/other.zip", False),
        ("https://cdn.nodejs.org/dist/v24.18.0/node-v24.18.0-win-x64.zip", False),
        (
            "https://nodejs.org/dist/v24.18.0/node-v24.18.0-win-x64.zip",
            True,
        ),
    ],
)
def test_download_verification_rejects_wrong_final_url_or_any_redirect(
    final_url, redirected
):
    """Certified direct URLs may not drift through same- or cross-origin redirects."""
    manifest = _subject()
    artifact = manifest.artifact_for("node", "win32", "x86_64")

    with pytest.raises(manifest.UnsupportedManagedInstall):
        manifest.verify_downloaded_artifact(
            artifact,
            b"untrusted",
            final_url=final_url,
            redirected=redirected,
        )


def test_download_verification_checks_the_approved_digest_after_url_validation():
    """A response at the approved URL is still rejected when its bytes differ."""
    manifest = _subject()
    artifact = manifest.artifact_for("node", "win32", "x86_64")

    with pytest.raises(manifest.ChecksumError):
        manifest.verify_downloaded_artifact(
            artifact,
            b"altered archive",
            final_url=artifact.url,
            redirected=False,
        )
