# Security Policy

## Supported versions

| Version | Security support |
| --- | --- |
| 1.0.0 | Supported on Windows x86-64 and macOS Intel/Apple Silicon |
| Earlier snapshots | Unsupported |

Linux, modified distributions, and any historical or unsupported code are
outside the 1.0.0 security-support boundary.

## Report a vulnerability privately

Do not open a public issue or discussion for a suspected vulnerability.

Use the repository's private
[GitHub Security Advisory form](https://github.com/EgerDev/yt-final/security/advisories/new).
Include the affected version and platform, impact, reproduction steps, relevant
logs with credentials removed, and whether you believe active exploitation is
occurring. If the private form is unavailable, contact the repository owner
through GitHub and request a private channel without disclosing technical
details publicly.

We aim to acknowledge a complete report within seven days, keep the reporter
updated during investigation, and coordinate disclosure after a fix is
available. These targets are not a paid bug-bounty commitment.

## Web UI boundary

The supported server is a single-user local application:

- it binds only to loopback;
- it rejects foreign `Host` headers and cross-origin browser mutations;
- static assets, same-origin login, and minimal liveness health are the only
  public routes;
- every other `/api/*` route requires an `X-Token` session credential;
- query-string credentials are rejected;
- sessions expire after 12 hours; and
- logout revokes the presented session, while a server restart invalidates all
  sessions.

The application does not provide LAN/public binding, TLS termination,
reverse-proxy trust, or multi-user authorization. Operator-managed SSH or
WireGuard tunnels terminate outside this security boundary.

## Local data

Cookie files, native-browser profiles, the Web UI password file, queue and watch
state, history, detailed job specifications, and logs can contain sensitive
data. Secret-bearing files — including the Web UI password file — are created
owner-only from their first byte: on Windows a restrictive DACL naming only the
current user, on macOS mode `0600` enforced through the descriptor that created
the file. They do not inherit their parent directory's permissions, so they are
not exposed to `SYSTEM`, `Administrators`, or other local accounts. Cookie
replacement and durable state writes use locked, flushed, atomic replacement.

Reading a Chromium browser copies the cookie-bearing parts of its profile into a
temporary directory, created owner-only the same way, and deletes it afterwards.
On Windows that deletion frequently cannot succeed: Chrome creates subdirectories
inside the copy with permissions naming only an internal Chrome identity, leaving
the owning user without an entry. The copy is not readable by other accounts, but
it does persist, so the application reports where it was left rather than
discarding a login that worked.

Cookies remain local except when sent to the authenticated video service
selected by the user. Normal logs redact credential-bearing URLs, cookies,
webhook secrets, proxy details, and unnecessary absolute paths. Users remain
responsible for protecting their operating-system account, backups, exported
cookies, browser storage, downloaded media, and explicitly configured webhook
or proxy destinations.

## Supply-chain boundary

Automated installers accept only reviewed immutable manifest entries and verify
SHA-256 before probing or executing downloaded bytes. Python environment
updates use the packaged hash-locked allowlist. Managed FFmpeg is Windows-only;
macOS uses manual or Homebrew FFmpeg. Unreviewed optional providers, external
proxies, browser extensions, Homebrew, and user-supplied FFmpeg builds retain
their own security and license boundaries.
