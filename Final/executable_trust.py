"""Explicit-PATH-only executable discovery for user-managed tools."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from typing import Iterable


_SUPPORTED_BROWSERS = frozenset({"chrome", "edge", "brave"})
_WINDOWS_KNOWN_FOLDERS = {
    "local_app_data": 0x001C,
    "program_files": 0x0026,
    "program_files_x86": 0x002A,
    "windows": 0x0024,
}
_REPARSE_POINT_FLAG = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x400,
)


def _windows_known_folder(name: str) -> Path | None:
    """Resolve a Windows shell folder without trusting process environment."""
    csidl = _WINDOWS_KNOWN_FOLDERS.get(name)
    if csidl is None or not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        result = ctypes.windll.shell32.SHGetFolderPathW(
            None,
            csidl,
            None,
            0,
            buffer,
        )
        if result != 0 or not buffer.value:
            return None
        path = Path(buffer.value)
        return path if path.is_absolute() else None
    except (AttributeError, OSError, RuntimeError, ValueError):
        return None


def _browser_candidate_specs(
    browser: str,
    platform: str,
) -> tuple[tuple[tuple[Path, ...], Path, bool], ...]:
    """Return fixed authority roots, candidate path, and link policy."""
    if browser not in _SUPPORTED_BROWSERS:
        return ()
    if str(platform).startswith("win"):
        roots = {
            name: _windows_known_folder(name)
            for name in ("program_files", "program_files_x86", "local_app_data")
        }
        relative = {
            "chrome": (
                ("program_files", "Google/Chrome/Application/chrome.exe"),
                ("local_app_data", "Google/Chrome/Application/chrome.exe"),
            ),
            "edge": (
                ("program_files", "Microsoft/Edge/Application/msedge.exe"),
                ("program_files_x86", "Microsoft/Edge/Application/msedge.exe"),
            ),
            "brave": (
                ("local_app_data", "BraveSoftware/Brave-Browser/Application/brave.exe"),
                ("program_files", "BraveSoftware/Brave-Browser/Application/brave.exe"),
                ("program_files_x86", "BraveSoftware/Brave-Browser/Application/brave.exe"),
            ),
        }
        return tuple(
            ((root,), root / suffix, False)
            for root_name, suffix in relative[browser]
            if (root := roots.get(root_name)) is not None
        )
    if platform == "darwin":
        # Do not add ~/Applications here: Path.home() can inherit mutable HOME,
        # which must never become authority for a credential-browser launch.
        system_root = Path("/Applications")
        app_relative = {
            "chrome": (
                "Google Chrome.app/Contents/MacOS/Google Chrome",
                "Chromium.app/Contents/MacOS/Chromium",
            ),
            "edge": ("Microsoft Edge.app/Contents/MacOS/Microsoft Edge",),
            "brave": ("Brave Browser.app/Contents/MacOS/Brave Browser",),
        }
        return tuple(
            ((system_root,), system_root / suffix, False)
            for suffix in app_relative[browser]
        )
    system_roots = (Path("/usr"), Path("/opt"), Path("/snap"))
    candidates = {
        "chrome": (
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/opt/google/chrome/google-chrome",
            "/opt/google/chrome/chrome",
            "/snap/bin/chromium",
        ),
        "edge": (
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/opt/microsoft/msedge/microsoft-edge",
        ),
        "brave": (
            "/usr/bin/brave",
            "/usr/bin/brave-browser",
            "/opt/brave.com/brave/brave-browser",
            "/snap/bin/brave",
        ),
    }
    return tuple(
        (system_roots, Path(candidate), True)
        for candidate in candidates[browser]
    )


def _is_link_like(path: Path, metadata) -> bool:
    if path.is_symlink():
        return True
    if (
        getattr(metadata, "st_file_attributes", 0)
        & _REPARSE_POINT_FLAG
    ):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    return False


def _relative_to_any(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _strict_fixed_executable(
    roots: tuple[Path, ...],
    candidate: Path,
) -> Path | None:
    containing_root = next(
        (
            root
            for root in roots
            if _relative_to_any(candidate, (root,))
        ),
        None,
    )
    if containing_root is None:
        return None
    try:
        relative = candidate.relative_to(containing_root)
        current = containing_root
        for index, component in enumerate((None, *relative.parts)):
            if component is not None:
                current = current / component
            metadata = current.lstat()
            if _is_link_like(current, metadata):
                return None
            is_leaf = index == len(relative.parts)
            if is_leaf:
                if not stat.S_ISREG(metadata.st_mode):
                    return None
            elif not stat.S_ISDIR(metadata.st_mode):
                return None
        resolved_root = containing_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _linked_system_executable(
    roots: tuple[Path, ...],
    candidate: Path,
) -> Path | None:
    if not _relative_to_any(candidate, roots):
        return None
    try:
        resolved_roots = []
        for root in roots:
            try:
                resolved_roots.append(root.resolve(strict=True))
            except (OSError, RuntimeError, ValueError):
                continue
        if not resolved_roots:
            return None
        resolved = candidate.resolve(strict=True)
        if not _relative_to_any(resolved, tuple(resolved_roots)):
            return None
        metadata = resolved.lstat()
        if not stat.S_ISREG(metadata.st_mode) or _is_link_like(
            resolved,
            metadata,
        ):
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def fixed_authority_executable(
    roots: Iterable[Path],
    candidate: Path,
    *,
    allow_internal_links: bool,
    platform: str | None = None,
) -> Path | None:
    """Validate one fixed executable candidate without CWD or PATH search."""
    selected_platform = platform or sys.platform
    absolute_roots = tuple(
        Path(os.path.abspath(os.fspath(root)))
        for root in roots
        if Path(root).is_absolute()
    )
    absolute_candidate = Path(os.path.abspath(os.fspath(candidate)))
    if not absolute_roots or not candidate.is_absolute():
        return None
    validated_target = (
        _linked_system_executable(absolute_roots, absolute_candidate)
        if allow_internal_links
        else _strict_fixed_executable(absolute_roots, absolute_candidate)
    )
    if validated_target is None:
        return None
    if (
        not str(selected_platform).startswith("win")
        and not os.access(validated_target, os.X_OK)
    ):
        return None
    # Preserve the fixed symlink's invocation name on Linux. Snap and some
    # distro launchers dispatch from argv[0]; executing the resolved target
    # directly would change which application they start.
    return absolute_candidate if allow_internal_links else validated_target


def installed_browser_executable(
    browser: str,
    *,
    platform: str | None = None,
) -> Path | None:
    """Return a browser only from fixed OS/vendor install authorities."""
    normalized = str(browser or "").strip().lower()
    if normalized not in _SUPPORTED_BROWSERS:
        return None
    selected_platform = platform or sys.platform
    for roots, candidate, allow_links in _browser_candidate_specs(
        normalized,
        selected_platform,
    ):
        resolved = fixed_authority_executable(
            roots,
            candidate,
            allow_internal_links=allow_links,
            platform=selected_platform,
        )
        if resolved is not None:
            return resolved
    return None


def _system_helper_candidate_specs(
    helper: str,
    platform: str,
) -> tuple[tuple[tuple[Path, ...], Path, bool], ...]:
    if helper != "file-manager":
        return ()
    if str(platform).startswith("win"):
        windows_root = _windows_known_folder("windows")
        return (
            (((windows_root,), windows_root / "explorer.exe", False),)
            if windows_root is not None
            else ()
        )
    if platform == "darwin":
        root = Path("/usr")
        return (((root,), Path("/usr/bin/open"), False),)
    roots = (Path("/usr"), Path("/bin"))
    return (
        (roots, Path("/usr/bin/xdg-open"), True),
        (roots, Path("/bin/xdg-open"), True),
    )


def system_helper_executable(
    helper: str,
    *,
    platform: str | None = None,
) -> Path | None:
    """Return an OS helper only from its fixed system installation authority."""
    normalized = str(helper or "").strip().lower()
    if normalized != "file-manager":
        return None
    selected_platform = platform or sys.platform
    for roots, candidate, allow_links in _system_helper_candidate_specs(
        normalized,
        selected_platform,
    ):
        resolved = fixed_authority_executable(
            roots,
            candidate,
            allow_internal_links=allow_links,
            platform=selected_platform,
        )
        if resolved is not None:
            return resolved
    return None


def _candidate_names(names: Iterable[str], platform: str) -> tuple[str, ...]:
    ordered: list[str] = []
    windows = str(platform).startswith("win")
    extensions = tuple(
        extension.lower()
        for extension in (
            os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        ).split(";")
        if extension
    )
    for value in names:
        name = str(value or "").strip()
        if not name or Path(name).name != name:
            continue
        variants = [name]
        if windows and not Path(name).suffix:
            variants.extend(name + extension for extension in extensions)
        for variant in variants:
            if variant.lower() not in {item.lower() for item in ordered}:
                ordered.append(variant)
    return tuple(ordered)


def explicit_path_executable(
    names: str | Iterable[str],
    *,
    path_value: str | None = None,
    platform: str | None = None,
) -> Path | None:
    """Return an absolute executable named by an explicit absolute PATH entry.

    Empty and relative PATH entries are skipped because both can authorize the
    caller's current directory. This intentionally does not use
    :func:`shutil.which`, whose Windows behavior prepends the current directory.
    """
    selected_platform = platform or sys.platform
    selected_path = os.environ.get("PATH", "") if path_value is None else path_value
    separator = ";" if str(selected_platform).startswith("win") else ":"
    requested = (names,) if isinstance(names, str) else tuple(names)
    candidates = _candidate_names(requested, selected_platform)
    seen: set[str] = set()

    for raw_entry in str(selected_path or "").split(separator):
        entry = raw_entry.strip().strip('"')
        if not entry:
            continue
        directory = Path(os.path.expandvars(entry)).expanduser()
        if not directory.is_absolute():
            continue
        absolute_directory = Path(os.path.abspath(os.fspath(directory)))
        key = os.path.normcase(os.fspath(absolute_directory))
        if key in seen:
            continue
        seen.add(key)
        for name in candidates:
            candidate = absolute_directory / name
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return Path(os.path.abspath(os.fspath(candidate)))
            except OSError:
                continue
    return None


_MODULE = sys.modules[__name__]
if __name__ == "executable_trust":
    sys.modules.setdefault("Final.executable_trust", _MODULE)
elif __name__ == "Final.executable_trust":
    sys.modules.setdefault("executable_trust", _MODULE)
