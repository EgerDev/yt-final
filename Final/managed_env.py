"""Fail-closed Python dependency policy for the 1.0.0 release.

The application can report the Python packages visible to its trusted current
interpreter. It cannot install or activate app-writable Python code in 1.0.0:
that requires a reviewed whole-runtime receipt and activation design. Python
dependency changes therefore ship only as a future reviewed application
release.
"""

from __future__ import annotations

try:
    from tool_manifest import UnsupportedManagedInstall
except ImportError:  # pragma: no cover - package import style
    from Final.tool_manifest import UnsupportedManagedInstall  # type: ignore


CERTIFIED_RUNTIME_UNAVAILABLE_CODE = "certified-runtime-unavailable"
CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE = (
    "Certified Python runtime activation is unavailable in 1.0.0; "
    "install a future reviewed release."
)


def require_certified_python_runtime_activation() -> None:
    """Reject Python dependency mutation before any filesystem or process I/O."""
    raise UnsupportedManagedInstall(CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE)


def inject_managed_site_packages() -> None:
    """Never activate code from an app-writable Python environment."""
    return None
