"""Parse and enforce the runtime dependency floors bundled with Final."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


class RequirementContractError(ValueError):
    """The bundled runtime requirements cannot be used as an unambiguous contract."""


def declared_runtime_floors(path: Path | None = None) -> dict[str, str]:
    """Return canonical distribution names mapped to their sole declared ``>=`` floor."""
    requirements_path = path or Path(__file__).with_name("requirements.txt")
    floors: dict[str, str] = {}
    try:
        lines = requirements_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RequirementContractError(f"cannot read runtime requirements: {requirements_path}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        declaration = raw_line.split("#", 1)[0].strip()
        if not declaration:
            continue
        try:
            requirement = Requirement(declaration)
        except InvalidRequirement as exc:
            raise RequirementContractError(
                f"invalid runtime requirement at line {line_number}: {declaration!r}"
            ) from exc

        specifiers = tuple(requirement.specifier)
        if requirement.extras or requirement.marker or len(specifiers) != 1 or specifiers[0].operator != ">=":
            raise RequirementContractError(
                f"runtime requirement at line {line_number} must contain exactly one >= floor: {declaration!r}"
            )
        minimum = specifiers[0].version
        try:
            Version(minimum)
        except InvalidVersion as exc:
            raise RequirementContractError(
                f"runtime requirement at line {line_number} has an invalid floor: {minimum!r}"
            ) from exc

        name = canonicalize_name(requirement.name)
        if name in floors:
            raise RequirementContractError(f"duplicate runtime requirement for {name!r}")
        floors[name] = minimum

    return floors


def installed_distribution_version(name: str) -> str | None:
    """Return locally installed distribution metadata, without a network lookup."""
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def satisfies_floor(installed: str, minimum: str) -> bool:
    """Return whether an installed PEP 440 version meets a declared lower bound."""
    try:
        return Version(installed) >= Version(minimum)
    except InvalidVersion:
        return False
