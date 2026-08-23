"""Write a stable SHA256SUMS manifest for top-level release artifacts."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.build_portable import (
        _DirectoryAnchor,
        _digest_descriptor,
        _object_identity,
        _open_publication_guard,
        _portable_component_key,
        _unlink_owned_temporary,
        _validate_portable_component,
        _verified_atomic_replace,
    )
else:
    try:
        from .build_portable import (
            _DirectoryAnchor,
            _digest_descriptor,
            _object_identity,
            _open_publication_guard,
            _portable_component_key,
            _unlink_owned_temporary,
            _validate_portable_component,
            _verified_atomic_replace,
        )
    except ImportError:  # Direct execution: python scripts/write_checksums.py
        from build_portable import (
            _DirectoryAnchor,
            _digest_descriptor,
            _object_identity,
            _open_publication_guard,
            _portable_component_key,
            _unlink_owned_temporary,
            _validate_portable_component,
            _verified_atomic_replace,
        )


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_CHUNK_SIZE = 1024 * 1024


def _link_like(path: Path, metadata=None) -> bool:
    metadata = metadata if metadata is not None else path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        return True
    if getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT:
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _regular_metadata(path: Path):
    metadata = path.lstat()
    if _link_like(path, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"checksum source is not a real regular file: {path}")
    return metadata


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 of one stable regular file."""
    source = Path(path)
    before = _regular_metadata(source)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"checksum source changed type: {source}")
        if (
            getattr(before, "st_ino", None)
            and getattr(opened, "st_ino", None)
            and (before.st_dev, before.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError(f"checksum source changed during open: {source}")
        while True:
            chunk = os.read(descriptor, _CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size != opened.st_size
            or getattr(after, "st_mtime_ns", None)
            != getattr(opened, "st_mtime_ns", None)
        ):
            raise ValueError(f"checksum source changed during read: {source}")
        named_after = _regular_metadata(source)
        if (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_size,
            getattr(named_after, "st_mtime_ns", None),
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            getattr(opened, "st_mtime_ns", None),
        ):
            raise ValueError(
                f"checksum source identity changed after read: {source}"
            )
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _checksum_identity(metadata) -> tuple[int, int, int, int | None]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        getattr(metadata, "st_mtime_ns", None),
    )


@dataclass
class _RetainedChecksumSource:
    path: Path
    name: str
    identity: tuple[int, int, int, int | None]
    digest: str
    descriptor: int
    anchor: _DirectoryAnchor

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = -1
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            self.anchor.close()


def _validate_retained_source(source: _RetainedChecksumSource) -> None:
    opened = os.fstat(source.descriptor)
    if _checksum_identity(opened) != source.identity:
        raise ValueError(
            f"checksum artifact descriptor changed: {source.path}"
        )
    source.anchor.verify_file_descriptor(
        source.descriptor,
        PurePosixPath(source.name),
    )
    named = _regular_metadata(source.path)
    if _checksum_identity(named) != source.identity:
        raise ValueError(
            f"checksum artifact identity changed: {source.path}"
        )
    if _digest_descriptor(source.descriptor) != source.digest:
        raise ValueError(
            f"checksum artifact content changed: {source.path}"
        )
    source.anchor.verify_file_descriptor(
        source.descriptor,
        PurePosixPath(source.name),
    )
    if (
        _checksum_identity(os.fstat(source.descriptor)) != source.identity
        or _checksum_identity(_regular_metadata(source.path))
        != source.identity
    ):
        raise ValueError(
            f"checksum artifact changed during validation: {source.path}"
        )


def _retain_checksum_source(
        source: Path,
        name: str,
) -> _RetainedChecksumSource:
    anchor = _DirectoryAnchor(
        source.parent,
        label="checksum source directory",
    )
    descriptor = -1
    retained_result = False
    try:
        reviewed = _regular_metadata(source)
        identity = _checksum_identity(reviewed)
        digest = sha256_file(source)
        descriptor = _open_publication_guard(
            source,
            share_delete=False,
            share_write=False,
        )
        retained = _RetainedChecksumSource(
            source,
            name,
            identity,
            digest,
            descriptor,
            anchor,
        )
        _validate_retained_source(retained)
        descriptor = -1
        retained_result = True
        return retained
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not retained_result:
            anchor.close()


def _display_name(path: Path) -> str:
    name = path.name
    try:
        _validate_portable_component(name)
    except ValueError as exc:
        raise ValueError(
            f"invalid checksum portable name {name!r}: {exc}"
        ) from exc
    return name


def _same_file_or_destination(source: Path, destination: Path) -> bool:
    source_absolute = os.path.normcase(os.path.abspath(os.fspath(source)))
    destination_absolute = os.path.normcase(
        os.path.abspath(os.fspath(destination))
    )
    if source_absolute == destination_absolute:
        return True
    if source.exists() and destination.exists():
        try:
            return os.path.samefile(source, destination)
        except OSError:
            return False
    return False


def write_sha256sums(
        paths: Sequence[Path],
        destination: Path,
) -> Path:
    """Hash artifacts and atomically write a sorted LF-only manifest."""
    sources = [Path(path) for path in paths]
    if not sources:
        raise ValueError("no artifacts supplied for SHA256SUMS")
    target = Path(destination)
    target_name = _display_name(target)
    target_key = _portable_component_key(target_name)
    named_sources = []
    seen_names: dict[str, str] = {}
    for source in sources:
        name = _display_name(source)
        if _same_file_or_destination(source, target):
            raise ValueError("SHA256SUMS cannot include its destination")
        folded = _portable_component_key(name)
        if folded == target_key:
            raise ValueError(
                "artifact portable name collides with SHA256SUMS "
                f"destination: {name!r}"
            )
        previous = seen_names.get(folded)
        if previous is not None:
            raise ValueError(
                f"duplicate or case-fold-colliding artifact names: "
                f"{previous!r} and {name!r}"
            )
        seen_names[folded] = name
        named_sources.append((source, name))
    with ExitStack() as retained_stack:
        retained_sources = []
        for source, name in named_sources:
            retained = _retain_checksum_source(source, name)
            retained_stack.callback(retained.close)
            retained_sources.append(retained)
        rows = sorted(
            (source.name, source.digest)
            for source in retained_sources
        )
        payload = "".join(
            f"{digest}  {name}\n"
            for name, digest in rows
        ).encode("utf-8")

        def validate_sources() -> None:
            for retained_source in retained_sources:
                _validate_retained_source(retained_source)

        target.parent.mkdir(parents=True, exist_ok=True)
        with _DirectoryAnchor(
                target.parent,
                label="checksum output directory",
        ) as directory_anchor:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            temporary_identity = _object_identity(os.fstat(descriptor))
            try:
                directory_anchor.verify_file_descriptor(
                    descriptor,
                    PurePosixPath(temporary.name),
                )
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                _verified_atomic_replace(
                    temporary,
                    temporary_identity,
                    target,
                    hashlib.sha256(payload).hexdigest(),
                    directory_anchor,
                    boundary_validator=validate_sources,
                )
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                _unlink_owned_temporary(
                    temporary,
                    temporary_identity,
                )
    return target


def _top_level_artifacts(directory: Path) -> list[Path]:
    root = Path(directory)
    metadata = root.lstat()
    if _link_like(root, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"artifact input is not a real directory: {root}")
    artifacts = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name):
        if candidate.name == "SHA256SUMS":
            continue
        candidate_metadata = candidate.lstat()
        if (
            not _link_like(candidate, candidate_metadata)
            and stat.S_ISREG(candidate_metadata.st_mode)
        ):
            artifacts.append(candidate)
    if not artifacts:
        raise ValueError(f"artifact directory contains no regular files: {root}")
    return artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a deterministic SHA256SUMS for one artifact directory.",
    )
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args(argv)
    destination = arguments.directory / "SHA256SUMS"
    result = write_sha256sums(
        _top_level_artifacts(arguments.directory),
        destination,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
