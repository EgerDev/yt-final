"""The report-formatting hook in conftest.py must keep a test that fails while os.name is patched
to the other platform from crashing pytest with INTERNALERROR (which would suppress the FAILED
line). Verified via pytester using the symmetric case: on this host we patch os.name to the OTHER
platform, so pathlib's failure is reproduced regardless of which OS runs the suite."""
import os
from pathlib import Path

import pytest

_OTHER_OS_NAME = "posix" if os.name == "nt" else "nt"

# The report-unmask hook lives in the repository-root conftest (see that file for why not Final/).
_CONFTEST = (Path(__file__).resolve().parents[1] / "conftest.py").read_text(encoding="utf-8")

_FAILING_TEST = f"""
import os
def test_fails_while_platform_patched(monkeypatch):
    monkeypatch.setattr(os, "name", {_OTHER_OS_NAME!r})
    assert False, "boom"
"""


def test_hook_turns_internalerror_into_a_normal_failed_line(pytester):
    # With the hook: a test that fails while os.name is patched to the other platform must be
    # reported as an ordinary FAILED, never crash the session with INTERNALERROR. (No negative
    # control here: whether the UN-hooked run crashes with INTERNALERROR vs is handled cleanly
    # varies by CPython/pytest version, so asserting the crash would be flaky.)
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(_FAILING_TEST)
    result = pytester.runpytest_subprocess("-q")
    out = result.stdout.str() + result.stderr.str()
    assert "INTERNALERROR" not in out, out
    assert "test_fails_while_platform_patched" in out
    result.assert_outcomes(failed=1)
