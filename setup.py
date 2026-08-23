import ctypes
import gzip
import io
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import tempfile

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


RELEASE_EXCLUDED_MODULES = {
    "_syntax_check",
    "runtime_verify",
}


class ReleaseBuildPy(build_py):
    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        if package == "Final":
            modules = [
                module
                for module in modules
                if not module[1].startswith("test_")
                and module[1] not in RELEASE_EXCLUDED_MODULES
            ]
        return modules


def _source_date_epoch():
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        return None
    if not re.fullmatch(r"0|[1-9][0-9]*", raw):
        raise ValueError(
            "SOURCE_DATE_EPOCH must be a canonical non-negative decimal"
        )
    epoch = int(raw)
    if epoch > 0xFFFFFFFF:
        raise ValueError("SOURCE_DATE_EPOCH exceeds the gzip timestamp range")
    return epoch


def _metadata_identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        # Creation time where the platform exposes it, NOT st_ctime. Python 3.14 changed
        # Windows st_ctime from creation time to metadata-change time, which is mutable: the
        # same file measured through os.stat and then os.fstat reported values ~2ms apart, so
        # every sdist capture raised "source file identity changed before open". An identity
        # tuple may only hold fields that cannot move under it. st_birthtime_ns is the stable
        # replacement on 3.14+ and macOS; where it does not exist (Windows <=3.13, Linux)
        # st_ctime_ns is left in place, since nothing in this flow mutates it there.
        getattr(metadata, "st_birthtime_ns", None)
        if hasattr(metadata, "st_birthtime_ns")
        else metadata.st_ctime_ns,
        getattr(metadata, "st_file_attributes", None),
        getattr(metadata, "st_reparse_tag", None),
    )


def _is_link(path, metadata):
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(
            reparse_point
            and getattr(metadata, "st_file_attributes", 0) & reparse_point
        )
        or (
            getattr(path, "is_junction", None) and path.is_junction()
        )
    )


def _capture_regular_file(path, reviewed_metadata):
    if reviewed_metadata.st_nlink != 1:
        raise ValueError(f"source file link count is not one: {path}")
    if _is_link(path, reviewed_metadata) or not stat.S_ISREG(
        reviewed_metadata.st_mode
    ):
        raise ValueError(f"sdist source is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    primary_error = None
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_nlink != 1
            or not os.path.samestat(reviewed_metadata, opened_metadata)
            or _metadata_identity(reviewed_metadata)
            != _metadata_identity(opened_metadata)
        ):
            raise ValueError(f"source file identity changed before open: {path}")
        chunks = []
        remaining = opened_metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"source file was truncated during read: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"source file grew during read: {path}")
        finished_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(finished_metadata.st_mode)
            or finished_metadata.st_nlink != 1
            or _metadata_identity(finished_metadata)
            != _metadata_identity(opened_metadata)
        ):
            raise ValueError(f"source file identity changed during read: {path}")
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            os.close(descriptor)
        except BaseException as close_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "source close cleanup failure: "
                f"{type(close_error).__name__}: {close_error}"
            )

    current_metadata = path.lstat()
    if (
        _is_link(path, current_metadata)
        or not stat.S_ISREG(current_metadata.st_mode)
        or current_metadata.st_nlink != 1
        or not os.path.samestat(opened_metadata, current_metadata)
        or _metadata_identity(opened_metadata)
        != _metadata_identity(current_metadata)
    ):
        raise ValueError(f"source file identity changed after read: {path}")
    return b"".join(chunks)


def _capture_source_snapshot(source):
    source = Path(source)
    root_metadata = source.lstat()
    if _is_link(source, root_metadata) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise ValueError(f"sdist source is not a regular directory: {source}")
    captured = []

    def capture_directory(directory, relative):
        before = directory.lstat()
        if _is_link(directory, before) or not stat.S_ISDIR(before.st_mode):
            raise ValueError(
                f"sdist contains a non-regular directory: {directory}"
            )
        with os.scandir(directory) as entries:
            names = tuple(sorted(entry.name for entry in entries))
        captured.append(("directory", relative, None))
        for name in names:
            if (
                not name
                or name in {".", ".."}
                or "\x00" in name
                or "/" in name
                or "\\" in name
            ):
                raise ValueError(f"noncanonical sdist source name: {name!r}")
            path = directory / name
            child_relative = (
                f"{relative}/{name}" if relative else name
            )
            metadata = path.lstat()
            if _is_link(path, metadata):
                raise ValueError(f"sdist source links are forbidden: {path}")
            if stat.S_ISDIR(metadata.st_mode):
                capture_directory(path, child_relative)
            elif stat.S_ISREG(metadata.st_mode):
                captured.append(
                    (
                        "file",
                        child_relative,
                        _capture_regular_file(path, metadata),
                    )
                )
            else:
                raise ValueError(
                    f"sdist contains a non-regular source path: {path}"
                )

        with os.scandir(directory) as entries:
            current_names = tuple(sorted(entry.name for entry in entries))
        after = directory.lstat()
        if (
            current_names != names
            or _is_link(directory, after)
            or not stat.S_ISDIR(after.st_mode)
            or not os.path.samestat(before, after)
            or _metadata_identity(before) != _metadata_identity(after)
        ):
            raise ValueError(
                f"source directory inventory changed during capture: {directory}"
            )

    capture_directory(source, "")
    return tuple(captured)


def _canonical_tar_info(name, epoch, *, is_directory, executable=False):
    member = tarfile.TarInfo(name)
    member.mtime = epoch
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    member.pax_headers = {}
    if is_directory:
        member.type = tarfile.DIRTYPE
        member.mode = 0o755
        member.size = 0
    else:
        member.type = tarfile.REGTYPE
        member.mode = 0o755 if executable else 0o644
    return member


def _validate_private_directory(path, reviewed_metadata):
    current = path.lstat()
    if (
        _is_link(path, current)
        or not stat.S_ISDIR(current.st_mode)
        or not os.path.samestat(reviewed_metadata, current)
        or reviewed_metadata.st_mode != current.st_mode
    ):
        raise ValueError("publication directory identity changed")
    if os.name == "posix" and stat.S_IMODE(current.st_mode) != 0o700:
        raise ValueError("publication directory must have mode 0700")


def _review_publication_parent(path):
    metadata = path.lstat()
    if _is_link(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("publication parent must be a regular directory")
    if os.name == "posix":
        if metadata.st_uid != os.geteuid():
            raise ValueError("publication parent must be owned by the process")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ValueError(
                "publication parent must not be group/other writable"
            )
    return metadata


def _validate_publication_parent(path, reviewed_metadata):
    current = _review_publication_parent(path)
    if (
        not os.path.samestat(reviewed_metadata, current)
        or reviewed_metadata.st_mode != current.st_mode
        or reviewed_metadata.st_uid != current.st_uid
    ):
        raise ValueError("publication parent identity changed")


def _validate_publication_file(path, reviewed_metadata):
    current = path.lstat()
    if (
        _is_link(path, current)
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or not os.path.samestat(reviewed_metadata, current)
        or _metadata_identity(reviewed_metadata) != _metadata_identity(current)
        or (
            os.name == "posix"
            and stat.S_IMODE(current.st_mode) != 0o644
        )
        or (
            os.name == "nt"
            and not current.st_mode & stat.S_IWRITE
        )
    ):
        raise ValueError("publication file identity changed")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    primary_error = None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not os.path.samestat(reviewed_metadata, opened)
            or _metadata_identity(reviewed_metadata)
            != _metadata_identity(opened)
        ):
            raise ValueError("publication file identity changed")
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            os.close(descriptor)
        except BaseException as close_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "publication validation close cleanup failure: "
                f"{type(close_error).__name__}: {close_error}"
            )


def _cleanup_publication(
    descriptor,
    temporary,
    publication_directory,
    publication_parent,
    parent_metadata,
):
    errors = []
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except BaseException as error:
            errors.append(("descriptor close", error))
    if temporary is not None:
        try:
            temporary.unlink(missing_ok=True)
        except BaseException as error:
            errors.append(("temporary unlink", error))
    if publication_directory is not None:
        try:
            shutil.rmtree(publication_directory)
        except BaseException as error:
            errors.append(("publication directory removal", error))
    try:
        _validate_publication_parent(publication_parent, parent_metadata)
    except BaseException as error:
        errors.append(("publication parent revalidation", error))
    return errors


def _fsync_parent_directory(directory):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    primary_error = None
    try:
        os.fsync(descriptor)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            os.close(descriptor)
        except BaseException as close_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "parent directory close cleanup failure: "
                f"{type(close_error).__name__}: {close_error}"
            )


def _atomic_replace(source, destination):
    if os.name != "nt":
        os.replace(source, destination)
        return
    move_file = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).MoveFileExW
    move_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    )
    move_file.restype = ctypes.c_int
    flags = 0x1 | 0x8  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
    if not move_file(str(source), str(destination), flags):
        raise ctypes.WinError(ctypes.get_last_error())


def _write_reproducible_gztar(source, destination, archive_root, epoch):
    source = Path(source)
    destination = Path(destination)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", archive_root):
        raise ValueError(f"invalid sdist archive root: {archive_root!r}")
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        raise ValueError("SOURCE_DATE_EPOCH must be an integer")
    if not 0 <= epoch <= 0xFFFFFFFF:
        raise ValueError("SOURCE_DATE_EPOCH exceeds the gzip timestamp range")
    snapshot = _capture_source_snapshot(source)
    executable_member = "Final/Start yt_final.command"

    destination.parent.mkdir(parents=True, exist_ok=True)
    parent_metadata = _review_publication_parent(destination.parent)
    publication_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.publish-",
            dir=destination.parent,
        )
    )
    descriptor = -1
    temporary = None
    published = False
    primary_error = None
    try:
        os.chmod(publication_directory, 0o700)
        publication_metadata = publication_directory.lstat()
        _validate_private_directory(
            publication_directory,
            publication_metadata,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="artifact-",
            suffix=".tmp",
            dir=publication_directory,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w+b") as raw:
            descriptor = -1
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw,
                mtime=epoch,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w|",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    for kind, relative, body in sorted(
                        snapshot,
                        key=lambda item: (
                            archive_root
                            if not item[1]
                            else f"{archive_root}/{item[1]}"
                        ),
                    ):
                        name = (
                            archive_root
                            if not relative
                            else f"{archive_root}/{relative}"
                        )
                        is_directory = kind == "directory"
                        member = _canonical_tar_info(
                            name,
                            epoch,
                            is_directory=is_directory,
                            executable=relative == executable_member,
                        )
                        if is_directory:
                            archive.addfile(member)
                        else:
                            member.size = len(body)
                            archive.addfile(member, io.BytesIO(body))
            if os.name == "posix":
                os.fchmod(raw.fileno(), 0o644)
            else:
                os.chmod(temporary, 0o644)
            raw.flush()
            os.fsync(raw.fileno())
            publication_file_metadata = os.fstat(raw.fileno())
        _validate_private_directory(
            publication_directory,
            publication_metadata,
        )
        _validate_publication_file(
            temporary,
            publication_file_metadata,
        )
        _validate_publication_parent(
            destination.parent,
            parent_metadata,
        )
        _atomic_replace(temporary, destination)
        published = True
        destination_metadata = destination.lstat()
        if (
            _is_link(destination, destination_metadata)
            or not stat.S_ISREG(destination_metadata.st_mode)
            or not os.path.samestat(
                publication_file_metadata,
                destination_metadata,
            )
            or (
                os.name == "posix"
                and stat.S_IMODE(destination_metadata.st_mode) != 0o644
            )
            or (
                os.name == "nt"
                and not destination_metadata.st_mode & stat.S_IWRITE
            )
        ):
            raise ValueError("published sdist identity changed")
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors = _cleanup_publication(
            descriptor,
            temporary,
            publication_directory,
            destination.parent,
            parent_metadata,
        )
        if cleanup_errors:
            if primary_error is not None:
                for label, cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"{label} cleanup failure: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            elif len(cleanup_errors) == 1:
                raise cleanup_errors[0][1]
            else:
                raise ExceptionGroup(
                    "sdist publication cleanup failed",
                    [error for _label, error in cleanup_errors],
                )
    if published and os.name == "posix":
        _fsync_parent_directory(destination.parent)
    return destination


class ReleaseSdist(sdist):
    def make_archive(
        self,
        base_name,
        format,
        root_dir=None,
        base_dir=None,
        owner=None,
        group=None,
    ):
        epoch = _source_date_epoch()
        if epoch is None or format != "gztar":
            return super().make_archive(
                base_name,
                format,
                root_dir=root_dir,
                base_dir=base_dir,
                owner=owner,
                group=group,
            )
        if not base_dir:
            raise ValueError("reproducible sdist requires a base directory")
        source = (
            Path(root_dir) / base_dir
            if root_dir is not None
            else Path(base_dir)
        )
        destination = Path(f"{base_name}.tar.gz")
        return os.fspath(
            _write_reproducible_gztar(
                source,
                destination,
                base_dir,
                epoch,
            )
        )


setup(cmdclass={"build_py": ReleaseBuildPy, "sdist": ReleaseSdist})
