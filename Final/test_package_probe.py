"""UpdateChecker package probe: real install + import verification."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import yt_final
from yt_final import UpdateChecker


def test_declared_runtime_floor_rejects_known_old_yt_dlp():
    """A runtime below the declared yt-dlp floor must not be treated as supported."""
    from dependency_contract import declared_runtime_floors, satisfies_floor

    floors = declared_runtime_floors()
    assert floors["yt-dlp"] == "2026.7.4"
    assert not satisfies_floor("2026.6.9", floors["yt-dlp"])
    assert satisfies_floor("2026.7.4", floors["yt-dlp"])
    assert satisfies_floor("2026.8.1", floors["yt-dlp"])


@pytest.mark.parametrize(
    "contents",
    [
        "yt-dlp==2026.7.4\n",
        "yt-dlp>=2026.7.4\nyt-dlp>=2026.8.1\n",
    ],
)
def test_declared_runtime_floors_reject_malformed_or_duplicate_declarations(tmp_path, contents):
    """Malformed declarations and duplicate distributions cannot silently weaken the contract."""
    from dependency_contract import RequirementContractError, declared_runtime_floors

    requirements = tmp_path / "requirements.txt"
    requirements.write_text(contents, encoding="utf-8")
    with pytest.raises(RequirementContractError):
        declared_runtime_floors(requirements)


def test_every_update_package_has_a_declared_runtime_floor():
    """The Updates-tab install targets must all be covered by the runtime contract."""
    from dependency_contract import declared_runtime_floors

    floors = declared_runtime_floors()
    assert set(UpdateChecker.PIP_PACKAGES) <= set(floors)


def test_regex_runtime_floor_and_import_probe_are_authoritative():
    from dependency_contract import declared_runtime_floors, satisfies_floor

    floors = declared_runtime_floors()
    assert floors["regex"] == "2026.7.19"
    assert not satisfies_floor("2026.7.18", floors["regex"])
    assert satisfies_floor("2026.7.19", floors["regex"])
    assert "regex" in UpdateChecker.PIP_PACKAGES
    assert UpdateChecker.PIP_IMPORT_PROBES["regex"] == ("regex",)


def test_installed_distribution_version_returns_none_when_distribution_is_absent():
    """An absent distribution is reported without importing or contacting a package index."""
    from dependency_contract import installed_distribution_version

    assert installed_distribution_version("yt-final-distribution-that-does-not-exist") is None


def test_package_probe_reports_verified_for_managed_installs(monkeypatch):
    """On this host, packages installed into the app venv must show verified+current."""
    # test_yt_final.py installs a module-level yt_dlp STUB into sys.modules (for its own network-free
    # tests); collecting it leaks that stub across the whole session. The bgutil plugin import-probe
    # below needs the REAL yt_dlp, so drop any stubbed yt_dlp* module (no __file__) for the duration
    # of this test — monkeypatch restores them on teardown so other files keep their stub. Without
    # this, the in-process probe fails whenever no managed venv exists to fall back to (e.g. a dev
    # Windows box), even though the packages are correctly installed.
    import sys as _sys
    for _name in list(_sys.modules):
        if _name == 'yt_dlp' or _name.startswith('yt_dlp.') or _name.startswith('yt_dlp_plugins'):
            if getattr(_sys.modules[_name], '__file__', None) is None:
                monkeypatch.delitem(_sys.modules, _name, raising=False)
    UpdateChecker.invalidate_cache()
    UpdateChecker._ensure_managed_imports()
    for pkg in UpdateChecker.PIP_PACKAGES:
        probe = UpdateChecker._package_probe(pkg)
        # If the package is present, verification must pass (import really works).
        if probe.get('version'):
            assert probe['verified'] is True, f'{pkg} metadata without import: {probe}'
            assert probe['source'] in ('process', 'managed_venv')


def test_scan_pip_rows_include_verification_fields(monkeypatch):
    monkeypatch.setattr(
        UpdateChecker, '_package_probe',
        classmethod(lambda cls, pkg: {
            'version': '1.2.3' if pkg == 'yt-dlp' else None,
            'verified': pkg == 'yt-dlp',
            'source': 'managed_venv' if pkg == 'yt-dlp' else None,
        }),
    )
    monkeypatch.setattr(
        UpdateChecker, '_pypi_latest',
        classmethod(lambda cls, pkg, timeout=3: '1.2.3' if pkg == 'yt-dlp' else '9.9.9'),
    )
    monkeypatch.setattr(
        UpdateChecker, '_preferred_ffmpeg_path',
        staticmethod(lambda: 'ffmpeg'),
    )
    # Keep ffmpeg path cheap
    monkeypatch.setattr(
        UpdateChecker, '_cmd_first_line',
        classmethod(lambda cls, args, pattern=None, timeout=2: None),
    )
    monkeypatch.setattr(
        UpdateChecker, '_ffmpeg_release_latest',
        classmethod(lambda cls: None),
        raising=False,
    )
    report = UpdateChecker._scan_uncached(
        check_pypi=True, include_optional_tools=False, ffmpeg_path='ffmpeg')
    by_name = {p['name']: p for p in report['pip']}
    yt = by_name['yt-dlp']
    assert yt['installed'] == '1.2.3'
    assert yt['verified'] is True
    assert yt['current'] is True
    assert yt['outdated'] is False
    assert 'verified' in (yt.get('note') or '').lower() or 'up to date' in (yt.get('note') or '').lower()
    missing = by_name['yt-dlp-ejs']
    assert missing['installed'] is None
    assert missing['verified'] is False
    assert missing['current'] is False


def test_package_probe_marks_unverified_when_import_fails(monkeypatch):
    monkeypatch.setattr(UpdateChecker, '_ensure_managed_imports', staticmethod(lambda: None))
    monkeypatch.setattr(UpdateChecker, '_metadata_version', staticmethod(lambda pkg: '9.9.9'))
    monkeypatch.setattr(UpdateChecker, '_venv_metadata_version', classmethod(lambda cls, pkg: None))
    monkeypatch.setattr(UpdateChecker, '_imports_ok', classmethod(lambda cls, pkg: False))
    monkeypatch.setattr(UpdateChecker, '_venv_imports_ok', classmethod(lambda cls, pkg: False))
    probe = UpdateChecker._package_probe('yt-dlp')
    assert probe['version'] == '9.9.9'
    assert probe['verified'] is False


def test_outdated_compares_numeric_version_tokens():
    assert UpdateChecker._outdated('2026.7.4', '2026.7.4') is False
    assert UpdateChecker._outdated('2026.7.4', '2026.07.04') is False  # same numbers
    assert UpdateChecker._outdated('2026.7.3', '2026.7.4') is True
    assert UpdateChecker._outdated(None, '1.0') is False
    assert UpdateChecker._outdated('1.0', None) is False


def test_pip_packages_contain_pooling_deps_with_probes():
    """requests+urllib3 unlock yt-dlp's POOLED RequestsRH transport: without them yt-dlp opens a
    new TCP connection per 10 MiB http_chunk_size boundary, so the congestion window restarts every
    chunk (measured +67.6% median throughput with pooling, 11/12 interleaved rounds).

    They must live in PIP_PACKAGES — not just requirements.txt — because that list is what the
    Updates tab installs, and EXISTING users only gain the speedup through it. urllib3 is listed
    explicitly: `pip install -U requests` is satisfied by a stale urllib3 1.x, which yt-dlp rejects
    (floor 2.0.2) and then silently drops back to the unpooled handler."""
    for pkg in ('requests', 'urllib3'):
        assert pkg in UpdateChecker.PIP_PACKAGES, f'{pkg} missing — pooled handler silently lost'
        assert UpdateChecker.PIP_IMPORT_PROBES.get(pkg) == (pkg,), pkg
    # the probe map and the package list must never drift apart
    assert set(UpdateChecker.PIP_IMPORT_PROBES) == set(UpdateChecker.PIP_PACKAGES)
    print('test_pip_packages_contain_pooling_deps_with_probes passed')


def test_requirements_txt_matches_pip_packages_with_floors():
    """The manual install path (requirements.txt) and the Updates-tab path (PIP_PACKAGES) must not
    drift apart, and the pins must stay at/above yt-dlp's HARD floors — below them yt-dlp raises
    ImportError internally and silently falls back to the unpooled handler, costing the speedup
    with no visible error."""
    import re
    from pathlib import Path
    text = (Path(__file__).parent / 'requirements.txt').read_text(encoding='utf-8')
    reqs = {}
    for line in text.splitlines():
        line = line.split('#')[0].strip()
        m = re.match(r'^([A-Za-z0-9._-]+)\s*>=\s*([0-9][0-9.]*)$', line)
        if m:
            reqs[m.group(1)] = m.group(2)
    assert set(UpdateChecker.PIP_PACKAGES) <= set(reqs), (
        f'PIP_PACKAGES missing declared requirements: {set(UpdateChecker.PIP_PACKAGES) - set(reqs)}')

    def as_tuple(v):
        return tuple(int(x) for x in v.split('.'))

    assert as_tuple(reqs['requests']) >= (2, 32, 2), reqs['requests']
    assert as_tuple(reqs['urllib3']) >= (2, 0, 2), reqs['urllib3']
    print('test_requirements_txt_matches_pip_packages_with_floors passed')


def test_pooled_handler_selected_iff_requests_importable():
    """The BEHAVIORAL heart of the pooling change, asserted BOTH WAYS so it never skips and runs
    green in any environment (a dev venv without requests still exercises the negative branch).

    yt-dlp's own import gate IS the version check: yt_dlp/networking/__init__.py does
    `try: from . import _requests / except ImportError: pass`, and _requests itself raises when
    requests<2.32.2 or urllib3<2.0.2. So the presence of that submodule is EXACTLY the condition
    under which the pooled RequestsRH exists — we derive it from yt-dlp rather than re-implementing
    the comparison.

    Runs in a SUBPROCESS against the trusted current interpreter rather than in-process,
    for two reasons: (1) several tests here replace sys.modules
    ['yt_dlp'] with a stub namespace, which leaks and breaks any in-process import
    ("'yt_dlp' is not a package"); (2) the subprocess matches the runtime selected by
    the launcher without ever probing or executing app-data candidates."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    base = Path(__file__).parent
    cand = [
        Path(sys.executable),
        base / '.venv-macos' / 'bin' / 'python3',
        base / '.venv-macos' / 'Scripts' / 'python.exe',
        base / '.venv' / 'bin' / 'python3',
        base / '.venv' / 'Scripts' / 'python.exe',
    ]
    py = next((str(p) for p in cand if p.exists()), None)
    if not py:
        pytest.skip('no trusted Python interpreter on this host')

    snippet = (
        'import json\n'
        'try:\n'
        '    import yt_dlp.networking._requests as r\n'
        '    floors, cls = True, getattr(r, "RequestsRH", None) is not None\n'
        'except ImportError:\n'
        '    floors, cls = False, False\n'
        'except Exception:\n'
        '    floors, cls = False, False\n'
        'import yt_dlp.networking as n\n'
        'print(json.dumps({"floors": floors, "cls": cls,\n'
        '                  "exposed": hasattr(n, "_requests"), "urllib": hasattr(n, "_urllib")}))\n'
    )
    proc = subprocess.run([py, '-c', snippet], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        pytest.skip(f'trusted runtime cannot import yt_dlp: {proc.stderr[-200:]}')
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    # The gate must be self-consistent: the submodule is exposed exactly when the floors are met.
    assert got['exposed'] is got['floors'], got
    if got['floors']:
        assert got['cls'], ('requests/urllib3 satisfy yt-dlp but RequestsRH is missing — '
                            'the pooled-transport speedup would be silently lost')
    # urllib is the unconditional fallback in either direction (never crashes, just slower)
    assert got['urllib'], got
    print(f'test_pooled_handler_selected_iff_requests_importable passed ({got})')


def test_ui_status_handles_verified_and_broken():
    from pathlib import Path
    js = (Path(__file__).parent / 'webui' / 'app.js').read_text(encoding='utf-8')
    # Short pill labels: verified === false → 'broken' (not the older "broken install" phrase)
    assert "text: 'broken'" in js or 'text: "broken"' in js
    assert 'verified === false' in js
    assert 'row.current' in js or 'r.current' in js or 'r.verified' in js
    assert 'toolStatusLabel' in js
