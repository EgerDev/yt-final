"""Shared pytest hooks for the Final test suite.

Lives at the repository root (not under Final/) on purpose: the release-inventory tests enforce
that Final/ contains exactly the reviewed runtime module set, so a conftest.py there would be
flagged as an unclassified shipped module. pytest still loads a rootdir conftest for the
Final/ testpaths."""
import os

import pytest

# Enable the built-in pytester plugin suite-wide (canonical location for pytest_plugins); a couple
# of tests use its sandboxed runpytest to assert on pytest's own reporting behaviour.
pytest_plugins = ["pytester"]

# Several tests monkeypatch os.name to "nt" so a Windows-only code path can be exercised on any
# host (e.g. test_jobstore_msvcrt simulating msvcrt locking). When such a test FAILS, pytest's own
# failure-report formatting evaluates pathlib.Path(os.getcwd()); on a POSIX host that raises
#   NotImplementedError: cannot instantiate 'WindowsPath' on your system
# during pytest_runtest_makereport, which aborts the whole session with INTERNALERROR and SUPPRESSES
# the real "FAILED ..." lines — so a genuine test failure shows up only as a pytest crash with no
# name attached. Pin os.name back to the real platform while the report is built, then restore
# whatever the test had set so the test's own monkeypatch teardown still behaves normally.
_REAL_OS_NAME = os.name


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    stashed = os.name
    if stashed != _REAL_OS_NAME:
        os.name = _REAL_OS_NAME
    try:
        yield
    finally:
        if os.name != stashed:
            os.name = stashed
