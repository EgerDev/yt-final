"""Security contracts for the frozen Python dependency update surface."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import managed_env
from tool_manifest import UnsupportedManagedInstall


FROZEN_CODE = "certified-runtime-unavailable"
FROZEN_MESSAGE = (
    "Certified Python runtime activation is unavailable in 1.0.0; "
    "install a future reviewed release."
)


def test_certified_python_update_contract_is_stable_and_fail_closed():
    assert managed_env.CERTIFIED_RUNTIME_UNAVAILABLE_CODE == FROZEN_CODE
    assert managed_env.CERTIFIED_RUNTIME_UNAVAILABLE_MESSAGE == FROZEN_MESSAGE

    with pytest.raises(UnsupportedManagedInstall) as raised:
        managed_env.require_certified_python_runtime_activation()

    assert str(raised.value) == FROZEN_MESSAGE


def test_managed_python_mutation_surface_is_absent():
    """1.0.0 must not ship unreachable installer infrastructure."""
    removed = (
        "ensure_managed_venv",
        "managed_pip_python",
        "pip_install_command",
        "_pip_install_command_for_validated_lock",
        "_finalize_pip_install_command",
        "_prepare_managed_venv",
        "_activate_staged_managed_venv",
        "_discard_staged_managed_venv",
        "_PreparedPipCommand",
    )

    assert [name for name in removed if hasattr(managed_env, name)] == []


def test_managed_site_packages_are_never_activated(tmp_path):
    """A hostile .pth below app data is inert and sys.path is unchanged."""
    site_packages = tmp_path / "app-data" / "venv" / "site-packages"
    site_packages.mkdir(parents=True)
    marker = tmp_path / "pth-executed"
    (site_packages / "hostile.pth").write_text(
        f'import pathlib; pathlib.Path({str(marker)!r}).write_text("ran")\n',
        encoding="utf-8",
    )
    before = list(sys.path)

    assert managed_env.inject_managed_site_packages() is None
    assert sys.path == before
    assert not marker.exists()


@pytest.mark.parametrize("module_name", ["yt_final", "webserver"])
def test_entrypoint_import_never_calls_legacy_managed_site_injector(
        tmp_path, module_name):
    """Importing either entry point cannot cross the app-writable code boundary."""
    fake_modules = tmp_path / "fake-modules"
    fake_modules.mkdir()
    marker = tmp_path / f"{module_name}-import-injected"
    (fake_modules / "managed_env.py").write_text(
        "from pathlib import Path\n"
        "def inject_managed_site_packages():\n"
        f'    Path({str(marker)!r}).write_text("executed", encoding="utf-8")\n',
        encoding="utf-8",
    )
    final_dir = Path(__file__).resolve().parent
    script = (
        "import sys\n"
        f"sys.path[:0] = [{str(fake_modules)!r}, {str(final_dir)!r}]\n"
        f"import {module_name}\n"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not marker.exists()


def test_managed_env_source_has_no_python_installer_or_activation_primitive():
    source = Path(managed_env.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "subprocess",
        "site.addsitedir",
        "shutil.rmtree",
        "-m', 'pip",
        '"-m", "pip',
        "-m', 'venv",
        '"-m", "venv',
        "--target",
    ):
        assert forbidden not in source
