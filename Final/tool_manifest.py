"""Immutable provenance for managed third-party artifact downloads."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import platform as host_platform
from pathlib import Path
import re
import stat
import sys
from urllib.parse import unquote, urlsplit


class ChecksumError(RuntimeError):
    """A staged artifact does not match its approved SHA-256 digest."""


class UnsupportedManagedInstall(RuntimeError):
    """No valid immutable artifact is approved for the requested host."""


@dataclass(frozen=True)
class ToolArtifact:
    name: str
    version: str
    platform: str
    architecture: str
    url: str
    sha256: str
    archive_kind: str
    binary_sha256: str = ""
    companion_binary_sha256: str | None = None


ARTIFACTS = (
    ToolArtifact(
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
    ToolArtifact(
        "node",
        "24.18.0",
        "win32",
        "x86_64",
        "https://nodejs.org/dist/v24.18.0/node-v24.18.0-win-x64.zip",
        "0ae68406b42d7725661da979b1403ec9926da205c6770827f33aac9d8f26e821",
        "zip",
        "9a4eb5f1c29c6a2e93852ead46b999e284a6a5ca8bab4d4e241d587d025a52de",
    ),
    ToolArtifact(
        "node",
        "24.18.0",
        "darwin",
        "arm64",
        "https://nodejs.org/dist/v24.18.0/node-v24.18.0-darwin-arm64.tar.gz",
        "e1a97e14c99c803e96c7339403282ea05a499c32f8d83defe9ef5ec66f979ed1",
        "tar.gz",
        "ee6fb0e015284d83a91e8ec5213f43a157f8a392b58555301682892ba928c04a",
    ),
    ToolArtifact(
        "deno",
        "2.9.4",
        "win32",
        "x86_64",
        "https://dl.deno.land/release/v2.9.4/"
        "deno-x86_64-pc-windows-msvc.zip",
        "68ed08b05c56cf887e9aa509947dc3f468f7e12f47a13e5c1abd51d46d1453ef",
        "zip",
        "4a2757fe99afc2c62c46500c8221cfa0189ac4bfb7064141875ad9c0f04b60ef",
    ),
    ToolArtifact(
        "deno",
        "2.9.4",
        "darwin",
        "arm64",
        "https://dl.deno.land/release/v2.9.4/deno-aarch64-apple-darwin.zip",
        "6d17647fdbf9c587a581dba205054c4ccf732dae0a196cc1e9b44c07589db412",
        "zip",
        "433088c827fa0e39ff162ab0e475f1fd4c7690eaedec500cf678edc3865e9287",
    ),
)


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"\d+(?:\.\d+)*\Z")
_MUTABLE_URL_PARTS = (
    "/latest/",
    "/releases/latest",
    "/archive/main",
    "/archive/master",
    "/refs/heads/",
    "/heads/main",
    "/heads/master",
    "/main/",
    "/master/",
    "/download/node.zip",
)
_MUTABLE_URL_TOKEN = re.compile(r"(?:^|[/_.-])latest(?:$|[/_.-])", re.IGNORECASE)
_IMMUTABLE_COMMIT_PATH_TOKEN = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{40}(?:[0-9a-f]{24})?(?![0-9a-f])",
    re.IGNORECASE,
)
_ARCHITECTURE_ALIASES = {
    "amd64": "x86_64",
    "x64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def _normalize_architecture(value: str) -> str:
    normalized = _ARCHITECTURE_ALIASES.get(str(value or "").strip().lower())
    if normalized is None:
        raise UnsupportedManagedInstall(
            f"Managed installation is unsupported for architecture {value!r}"
        )
    return normalized


def _canonical_url_path(path: str) -> str:
    """Decode bounded nested escapes so mutable tokens cannot hide behind `%25`."""
    canonical = path
    for _ in range(5):
        if re.search(r"%(?![0-9a-fA-F]{2})", canonical):
            raise ValueError("invalid percent escape")
        decoded = unquote(canonical, errors="strict")
        if decoded == canonical:
            return canonical
        canonical = decoded
    raise ValueError("excessively nested percent encoding")


def _validate_artifact(artifact: ToolArtifact) -> None:
    if not isinstance(artifact, ToolArtifact):
        raise UnsupportedManagedInstall("Managed artifact provenance is malformed")
    if not all(
        isinstance(value, str)
        for value in (
            artifact.name,
            artifact.version,
            artifact.platform,
            artifact.architecture,
            artifact.url,
            artifact.sha256,
            artifact.archive_kind,
            artifact.binary_sha256,
        )
    ):
        raise UnsupportedManagedInstall("Managed artifact provenance is malformed")
    if not artifact.name or not _VERSION.fullmatch(artifact.version):
        raise UnsupportedManagedInstall("Managed artifact identity is malformed")
    if artifact.platform not in {"win32", "darwin"}:
        raise UnsupportedManagedInstall("Managed artifact platform is unsupported")
    if artifact.architecture not in {"x86_64", "arm64"}:
        raise UnsupportedManagedInstall("Managed artifact architecture is malformed")
    if artifact.archive_kind not in {"zip", "tar.gz"}:
        raise UnsupportedManagedInstall("Managed artifact archive kind is unsupported")
    if not _SHA256.fullmatch(artifact.sha256):
        raise UnsupportedManagedInstall("Managed artifact SHA-256 is missing or malformed")
    if not _SHA256.fullmatch(artifact.binary_sha256):
        raise UnsupportedManagedInstall(
            "Managed executable SHA-256 is missing or malformed"
        )
    if artifact.name == "ffmpeg":
        if (
            not isinstance(artifact.companion_binary_sha256, str)
            or not _SHA256.fullmatch(artifact.companion_binary_sha256)
        ):
            raise UnsupportedManagedInstall(
                "Managed companion executable SHA-256 is missing or malformed"
            )
    elif artifact.companion_binary_sha256 is not None:
        raise UnsupportedManagedInstall(
            "Managed artifact has an unexpected companion executable digest"
        )

    try:
        parsed = urlsplit(artifact.url)
        parsed.port
        canonical_path = _canonical_url_path(parsed.path)
    except ValueError as exc:
        raise UnsupportedManagedInstall(
            "Managed artifact URL is malformed"
        ) from exc
    except UnicodeDecodeError as exc:
        raise UnsupportedManagedInstall(
            "Managed artifact URL is malformed"
        ) from exc
    lowered_path = canonical_path.lower()
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or "\\" in canonical_path
        or any(part in lowered_path for part in _MUTABLE_URL_PARTS)
        or _MUTABLE_URL_TOKEN.search(lowered_path) is not None
    ):
        raise UnsupportedManagedInstall("Managed artifact URL is not immutable HTTPS")

    version_in_path = re.search(
        rf"(?<![0-9]){re.escape(artifact.version)}(?![0-9])",
        canonical_path,
    )
    if (
        version_in_path is None
        and _IMMUTABLE_COMMIT_PATH_TOKEN.search(canonical_path) is None
    ):
        raise UnsupportedManagedInstall(
            "Managed artifact URL lacks an immutable version or commit identity"
        )


def _validated_catalog() -> tuple[ToolArtifact, ...]:
    catalog = tuple(ARTIFACTS)
    keys: set[tuple[str, str, str]] = set()
    for artifact in catalog:
        _validate_artifact(artifact)
        key = (artifact.name, artifact.platform, artifact.architecture)
        if key in keys:
            raise UnsupportedManagedInstall("Managed artifact provenance is ambiguous")
        keys.add(key)
    return catalog


def artifact_for(
    name: str,
    platform: str | None = None,
    architecture: str | None = None,
) -> ToolArtifact:
    """Return the sole approved artifact for a host, or fail before any I/O."""
    selected_platform = str(platform if platform is not None else sys.platform)
    selected_architecture = _normalize_architecture(
        architecture if architecture is not None else host_platform.machine()
    )
    matches = [
        artifact
        for artifact in _validated_catalog()
        if artifact.name == name
        and artifact.platform == selected_platform
        and artifact.architecture == selected_architecture
    ]
    if len(matches) != 1:
        raise UnsupportedManagedInstall(
            f"No certified managed {name} artifact is available for "
            f"{selected_platform}/{selected_architecture}"
        )
    return matches[0]


def verify_sha256(source: bytes | bytearray | memoryview | Path, expected: str) -> bool:
    """Require non-empty bytes from a regular file or in-memory staged payload."""
    if not _SHA256.fullmatch(str(expected or "")):
        raise ChecksumError("Approved SHA-256 is missing or malformed")

    digest = hashlib.sha256()
    size = 0
    if isinstance(source, (bytes, bytearray, memoryview)):
        payload = bytes(source)
        size = len(payload)
        digest.update(payload)
    else:
        path = Path(source)
        try:
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise ChecksumError("Downloaded artifact is not a regular file")
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
        except ChecksumError:
            raise
        except OSError as exc:
            raise ChecksumError("Downloaded artifact could not be verified") from exc

    if size == 0 or digest.hexdigest() != expected:
        raise ChecksumError("Downloaded artifact checksum mismatch")
    return True


def verify_downloaded_artifact(
    artifact: ToolArtifact,
    source: bytes | bytearray | memoryview | Path,
    *,
    final_url: str,
    redirected: bool = False,
) -> bool:
    """Validate provenance and bytes before extraction, probing, or activation."""
    _validate_artifact(artifact)
    approved = [
        item
        for item in _validated_catalog()
        if (
            item.name,
            item.platform,
            item.architecture,
        )
        == (
            artifact.name,
            artifact.platform,
            artifact.architecture,
        )
    ]
    if len(approved) != 1 or approved[0] != artifact:
        raise UnsupportedManagedInstall("Artifact provenance is not approved")
    if redirected or final_url != artifact.url:
        raise UnsupportedManagedInstall(
            "Downloaded artifact final URL does not match its approved direct URL"
        )
    # SUBSUMED, deliberately kept. The exact-equality check above already requires final_url ==
    # artifact.url, so by the time control reaches here the scheme/host/port can only match and this
    # block can never be the rejector. It stays as defence in depth: if that equality is ever relaxed
    # (e.g. to tolerate a signed query string), this becomes load-bearing again without anyone having
    # to remember to re-add it. The real control on this path is the SHA-256 pin at the end.
    parsed = urlsplit(final_url)
    approved_origin = urlsplit(artifact.url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != approved_origin.hostname
        or parsed.port != approved_origin.port
    ):
        raise UnsupportedManagedInstall("Downloaded artifact origin is not approved")
    return verify_sha256(source, artifact.sha256)


def approved_python_lock() -> Path:
    """Return the packaged certified Python lock, failing closed if unavailable."""
    lock = Path(__file__).with_name("approved-python.lock")
    try:
        if not lock.is_file():
            raise UnsupportedManagedInstall(
                "Certified Python dependency lock is unavailable"
            )
    except OSError as exc:
        raise UnsupportedManagedInstall(
            "Certified Python dependency lock is unavailable"
        ) from exc
    return lock


_MODULE = sys.modules[__name__]
if __name__ == "tool_manifest":
    sys.modules.setdefault("Final.tool_manifest", _MODULE)
elif __name__ == "Final.tool_manifest":
    sys.modules.setdefault("tool_manifest", _MODULE)
