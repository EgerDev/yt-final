# Releasing yt_final

This procedure creates and verifies the local `yt_final` 1.0.0 release
candidate. It does not publish to PyPI and does not make a GitHub Release
public. Do not publish while a required platform gate is skipped or failing,
or before the license review recorded in `requirements/README.md` is approved.

## Preconditions

- Work from the exact release commit in a clean checkout.
- Use a supported 64-bit CPython and the hash-locked build, development,
  release, and runtime environments described in `requirements/README.md`.
- Complete the Windows x86-64 and macOS Intel/Apple-silicon CI matrix on
  Python 3.11 and 3.14.
- Configure a GitHub environment named exactly `release` with at least one
  required reviewer, prevent self-review, and disable administrator bypass.
  A workflow reference can auto-create an unprotected environment, so the
  release workflow also reads the environment policy and fails unless the
  reviewer and self-review controls are present.
- Enable GitHub release immutability for the repository.
- Configure an active tag ruleset that includes exactly
  `refs/tags/v1.0.0`, has no bypass actors or exclusions, and restricts both
  updates and deletions. The workflow reads the visible ruleset shape and
  fails unless the exact active tag/update/deletion controls are present.
  Keep the rule active until the reviewed draft is published and GitHub's
  immutable-release protection takes over.
- GitHub's built-in GITHUB_TOKEN cannot prove administrator bypass,
  ruleset bypass actors, or release immutability: those repository-admin
  properties are either hidden from its permissions or unavailable through
  the read-only response. An administrator must inspect all three settings
  immediately before dispatch and select both manual confirmations. The
  confirmations are an explicit operator attestation, not an API proof.
- Set `SOURCE_DATE_EPOCH` to the release commit timestamp, not the current
  time:

```powershell
$env:SOURCE_DATE_EPOCH = git show -s --format=%ct HEAD
```

Do not reuse an artifact directory. A valid directory contains exactly the
wheel, source distribution, portable ZIP, and `SHA256SUMS`.

## Build the candidate

The commands below assume the locked tools are already installed into their
separate environments. Replace the interpreter tokens with their canonical
absolute paths.

```powershell
New-Item -ItemType Directory -Path dist -ErrorAction Stop
<build-python> -I -m build --no-isolation --wheel --sdist --outdir dist .
<build-python> scripts/build_portable.py --output-dir dist
<build-python> scripts/write_checksums.py dist
<release-python> -I -m twine check dist/yt_final-1.0.0-py3-none-any.whl dist/yt_final-1.0.0.tar.gz
<release-python> -I scripts/verify_release.py dist `
  --trusted-source-root . `
  --expected-source-epoch $env:SOURCE_DATE_EPOCH
```

The last command prints a JSON report and exits zero only when the directory
contains exactly:

```text
SHA256SUMS
yt_final-1.0.0-portable.zip
yt_final-1.0.0-py3-none-any.whl
yt_final-1.0.0.tar.gz
```

The verifier reads archives without extracting them. It checks canonical
lowercase SHA-256 lines, recomputes every digest, enforces reviewed member
boundaries, rejects unsafe or link-like archive entries, verifies the wheel
`RECORD`, binds every non-generated source-distribution file to the trusted
clean checkout, and cross-checks package metadata, artifact timestamps, plus
every embedded `_version.py`. The `--trusted-source-root` and
`--expected-source-epoch` arguments make those bindings explicit. Treat any
nonzero result as a release blocker; do not edit `SHA256SUMS` by hand.

## Reproducibility and runtime checks

Build a second candidate from the same commit with the same
`SOURCE_DATE_EPOCH` into a different empty directory. Run the verifier on both
directories and compare the SHA-256 of each of the three archives byte for
byte.

Install the verified wheel into a fresh environment outside the checkout.
First install the certified runtime closure from `Final/approved-python.lock`
with hashes enforced, then install the wheel offline with dependency
resolution disabled. Replace the interpreter and environment tokens with
canonical absolute paths:

```powershell
<base-python> -I -B -m venv --without-pip --copies <runtime-environment>
<release-python> -I -B -m pip --isolated --python <runtime-python> install --require-hashes --only-binary=:all: -r Final/approved-python.lock
<release-python> -I -B -m pip --isolated --python <runtime-python> install --no-index --no-deps dist/yt_final-1.0.0-py3-none-any.whl
<release-python> -I -B -m pip --isolated --python <runtime-python> check
<yt-final-console> --help
<yt-final-console> list-profiles
<runtime-python> -I -B -m Final --help
```

Do not replace `--no-deps` with normal wheel dependency resolution: doing so
would bypass the certified hash-locked runtime closure.

Extract the verified portable ZIP into a fresh directory outside the
checkout. Run the direct Python help command and both platform launchers on
their supported operating systems. Also build a wheel from the verified
source distribution and repeat the installed-command checks.

Complete the authenticated loopback server/static-asset smoke and the
frontend browser QA:

```bash
npm ci
npx playwright install chromium
npm run test:browser
```

`scripts/run_browser_tests.mjs` starts an authenticated loopback server and drives it.
It reads `DEV_PYTHON` for the interpreter and `YT_FINAL_BROWSER_NAME` to pick the engine;
run it once with `chromium`, and on macOS again with `webkit`. Never upload raw
authentication traces, passwords, session tokens, cookies, or browser
profiles as CI artifacts.

## Releasing a patch version

There is no 1.0.1 path yet, and this section exists so that fact is written
down rather than discovered. `1.0.0` is a literal in the release machinery, not
a value read from one place, so a patch release is a deliberate edit across
several files rather than a version bump.

`.github/workflows/release.yml` rejects any tag but `v1.0.0` (line 67) and any
tag-commit version but `1.0.0` (line 191). Until that changes, dispatching a
`v1.0.1` fails closed by design.

The surface, measured rather than estimated:

| file | occurrences | role |
| --- | --- | --- |
| `Final/_version.py` | 1 | the source of truth the wheel and the workflow both read |
| `package.json` | 1 | npm package version |
| `.github/workflows/release.yml` | 17 | tag gate, version gate, artifact names |
| `.github/workflows/ci.yml` | 10 | artifact names and version assertions |
| `scripts/verify_release.py` | 1 | expected-version default |
| `Final/test_release_*.py` | many | pinned artifact names and members |

To enumerate them yourself, scoped to product paths:

```bash
git grep -n "1\.0\.0" -- ':!archive' ':!docs/superpowers' ':!Final/docs'
```

Change `Final/_version.py` first, then the two workflows, then the release
tests, and run the release suite before tagging anything. Deliberately not
automated: a version bump that silently rewrote the release gates would defeat
the point of having them.

## Draft release

The protected manual release workflow accepts an existing version tag, reruns
the release gates, and may create a **draft** GitHub Release for reviewer
inspection. It must remain draft-only. There is no PyPI upload or trusted
publishing step for 1.0.0, and making the draft public is outside this
procedure. Dispatch requires the exact tag plus both manual confirmations for
the repository-admin policy checks described above.

The workflow is retry-safe for an interrupted asset upload: it reuses only a
matching workflow-owned draft, rejects unexpected assets, replaces the four
allowlisted assets, and then checks the release metadata, audited commit
binding, each uploaded size, and each SHA-256 digest again. The draft body
records the audited full commit SHA. Tag-scoped concurrency serializes
duplicate dispatches instead of cancelling or racing their uploads. Never
publish a draft from a failed run. Correct the failure and rerun the workflow
until the final reconciliation step passes.
