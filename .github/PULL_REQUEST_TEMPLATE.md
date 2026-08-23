<!--
Security fixes do not start here. Report privately first:
https://github.com/EgerDev/yt-final/security/advisories/new

Never include real credentials, cookies, session tokens, or unredacted
browser traces in a pull request.
-->

## Problem

What is broken or missing, and how it affects someone using the tool.

## Root cause

Why it happens. For a bug fix, name the specific defect rather than the
symptom.

## Change

What this does, and anything you deliberately did not do.

## Verification

Paste the exact commands you ran and their results. Per `CONTRIBUTING.md`,
these must come from the final state of the branch — an earlier green run is
not evidence after another edit.

```text
python -m ruff check Final scripts
python -m mypy
python -m pip_audit --strict --require-hashes -r requirements/release.lock
python -m compileall -q Final scripts
python Final/_syntax_check.py
npm run lint
python -m pytest
```

Browser-facing changes also need the authenticated browser gate, which
`python -m pytest` does not run — see
[CONTRIBUTING.md](../CONTRIBUTING.md#authenticated-browser-gate). Paste the
`npm run test:browser` result for each engine you drove (Chromium, and WebKit
too on macOS).

- [ ] A regression test fails without this change and passes with it
- [ ] Existing tests were not deleted, weakened, or suppressed to go green
- [ ] Documentation and tests are updated alongside the behavior change
- [ ] I dedicate this contribution to the public domain under the
      [Unlicense](../LICENSE), per
      [CONTRIBUTING.md](../CONTRIBUTING.md#licensing-of-contributions)

**Platforms tested:** <!-- e.g. Windows 11 x86-64 / Python 3.11 -->

**Not run, and why:** <!-- state any gate you could not execute -->

## Risk

Regression risk, and what a reviewer should look at hardest. Separate any
pre-existing failures you observed from ones this change could have caused.
