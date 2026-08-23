"""Structural check on the yt_final/capability module split.

`yt_final.py` used to own the FFmpeg/GPU capability probing. That moved into
`capability.py`, and this script asserts the split stayed split: the detector
lives in one module, the engine imports it rather than redefining it, and both
files still parse.

Two things were wrong with the original and are worth stating so they do not
come back:

* It opened ``"yt_final.py"`` relative to the CURRENT DIRECTORY, so the
  invocation `CONTRIBUTING.md` documents -- `python Final/_syntax_check.py`
  from the repository root -- died with `FileNotFoundError`. Only CI's
  `Push-Location Final` worked, so the documented form was never exercised.
  Paths now resolve from this file's own directory and it runs from anywhere.

* Every check was a bare ``print("label:", <bool>)``. The script printed
  ``False`` and exited 0, so as a *gate* it could not fail -- it was a no-op
  sitting in a list `CONTRIBUTING.md` calls "required gates; none is
  advisory". Failures now set the exit status.
"""

from __future__ import annotations

import ast
from pathlib import Path


HERE = Path(__file__).resolve().parent

failures: list[str] = []


def check(label: str, condition: bool) -> bool:
    """Record and report one expectation."""
    print(f"{'ok  ' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)
    return condition


engine_src = (HERE / "yt_final.py").read_text(encoding="utf-8")
capability_src = (HERE / "capability.py").read_text(encoding="utf-8")

print("=== yt_final.py ===")
print(f"      source length: {len(engine_src)}")
check(
    "imports CapabilityDetector from capability",
    "from .capability import CapabilityDetector" in engine_src
    or "from capability import CapabilityDetector" in engine_src,
)
check("does not redefine CapabilityDetector", "class CapabilityDetector" not in engine_src)
check("does not keep a local _find_ffmpeg", "def _find_ffmpeg(self):" not in engine_src)

engine_tree = ast.parse(engine_src)
check("parses as a module", isinstance(engine_tree, ast.Module))

print("\n=== capability.py ===")
print(f"      source length: {len(capability_src)}")
check("defines CapabilityDetector", "class CapabilityDetector" in capability_src)
check("defines _find_ffmpeg", "def _find_ffmpeg(self):" in capability_src)
check("keeps the 320x240 probe sizes", "320x240" in capability_src)

capability_tree = ast.parse(capability_src)
check(
    "_find_ffmpeg is a member of capability.py",
    "_find_ffmpeg" in [
        node.name for node in ast.walk(capability_tree) if hasattr(node, "name")
    ],
)

print("\n=== top-level definitions in yt_final.py ===")
for node in engine_tree.body:
    name = getattr(node, "name", "")
    print(f"      {type(node).__name__:<12} {name[:40]}")

if failures:
    print(f"\nFAILED {len(failures)} check(s): " + "; ".join(failures))
    raise SystemExit(1)

print("\nAll structural checks passed.")
