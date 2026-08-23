from __future__ import annotations

import importlib
import sys
from pathlib import Path


def main() -> int | None:
    package_dir = str(Path(__file__).resolve().parent)
    if not sys.path or sys.path[0] != package_dir:
        sys.path.insert(0, package_dir)
    module = importlib.import_module("yt_final")
    return module.main()
