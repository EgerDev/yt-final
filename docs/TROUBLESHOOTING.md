# Troubleshooting

Start here:

```bash
yt-final diagnose
```

It checks Python, FFmpeg, `ffprobe`, every runtime dependency, the HTTP
transport, the JavaScript runtime, the PO-token provider, and your hardware
encoders, and prints PASS or FAIL for each. Most problems below are one of those
lines. If you are about to open an issue, paste that output — it answers the
first three questions anyone would ask.

For a slow *download* rather than a broken one, `yt-final troubleshoot` adds
throughput-specific advice on top.

---

## Downloads fail

### "Sign in to confirm you're not a bot"

YouTube is challenging your IP. It usually clears on its own, but the reliable
fix is to give the tool a signed-in session:

```bash
yt-final auth login          # one-time browser login, handled by the app
yt-final auth import ~/cookies.txt   # or import a Netscape cookies.txt you exported
yt-final auth status         # confirm what is stored
```

Use a **dedicated account**. YouTube can rate-limit or suspend accounts whose
cookies are used by download tools.

You can also hand cookies over per-run with `--cookies ~/cookies.txt`. Keep that
file **outside** the working directory — it is a live login session, not a
config file.

### You signed in, but the app says no cookies were found

If the Login window opened, you signed into YouTube, closed it, and the app still
reports no login — the sign-in probably worked and the *read-back* failed.

The app refuses to read a browser profile whose directory can be modified by
anyone other than you. That is deliberate: a profile another account can write
to is a profile another account can plant cookies in. On Windows the check fails
with:

```
PermissionError: profile source DACL grants mutation rights to a foreign SID
```

It usually means `~/.yt_final` carries an inherited or leftover permission entry —
often from a sandbox, a migration, or an account that no longer exists. Inspect
it:

```powershell
icacls "$env:USERPROFILE\.yt_final"
```

Anything granting `(M)` or `(F)` besides **you**, `SYSTEM`, and `Administrators`
is what trips it. To reset the directory to just your own access:

```powershell
icacls "$env:USERPROFILE\.yt_final" /inheritance:r /grant "$env:USERNAME:(OI)(CI)F" /T
```

Then run Login again. Importing a `cookies.txt` works regardless, and is the
quickest way past this if you would rather not touch permissions.

### "This video is age-restricted"

Same answer: age-gated videos require a signed-in session, so set up auth as
above. Nothing else lifts it.

### HTTP 403 or 429, or downloads that suddenly stop working

Your IP is being rate-limited or soft-blocked. The tool already backs off and
retries. If it persists:

- wait, or change network;
- `--slow-requests` paces extraction requests to cool down a soft-blocked IP;
- set up a PO-token provider (see below), which is what unlocks the fast
  client path.

### "No matching format available"

The quality or codec you asked for does not exist for that video. Try
`--quality best`, or inspect what is actually offered:

```bash
yt-final list-formats "https://youtu.be/..."
```

### DNS, timeouts, connection reset

`yt-final` distinguishes these and prints a specific remedy. A resolver failure
is usually your DNS or VPN; a refused or intercepted connection is usually a
proxy or a TLS-inspecting antivirus.

---

## Downloads are slow

### Every download is capped around 1 MiB/s

You almost certainly have no JavaScript runtime. YouTube's `nsig` challenge has
to be solved, and without Node or Deno every download is throttled. This is the
single most common cause of slow downloads.

```bash
yt-final diagnose      # look for the "JS runtime" line
```

Install Node or Deno — on Windows x86-64 and Apple Silicon the Web UI's
**Updates** tab can do it for you; on Intel Macs install it yourself
(`brew install node`).

### The format id looks like `hls-...` instead of a number

You fell back to the slow HLS path, which means the fast client was not
available — normally a PO-token or geo issue. Check the provider:

```bash
yt-final --pot-mode http download "https://youtu.be/..."
```

`--pot-mode` and `--pot-server` are **top-level flags**: they go before the
subcommand, not after it.

### Your ISP is throttling video

The Web UI's **Speed test** measures it and, if throttling is detected, can turn
on a local bypass that obscures the video hostname during the TLS handshake. It
is opt-in and off by default. Settings → *Bypass ISP throttling*.

---

## Encoding problems

### "No hardware encoder" or everything falls back to CPU

```bash
yt-final gpu-info
```

This probes the encoders your machine can actually use, rather than the ones it
claims. If nothing is listed, the CPU profiles still work — they are just
slower. A profile whose encoder is unusable is skipped automatically, so you do
not need to pick by hand: the default `auto_h264` resolves to the fastest
working H.264 encoder on your machine.

### FFmpeg not found

Everything that encodes, remuxes, or merges video with audio needs FFmpeg and
`ffprobe` on `PATH`. On Windows x86-64 the Web UI's **Updates** tab installs a
reviewed build; on macOS use `brew install ffmpeg`.

---

## Web UI problems

### The password does not work

The password is generated per launch and printed in the window that started the
server. It is also written to `webui.txt` in `~/.yt_final/`.

If you started the app twice, the **first** window owns the session — a second
copy now refuses to start rather than binding the same port, and tells you so.
Close the extras and use the original window's password, or start another
instance on a different port with `--port`.

### "Address already in use", or the app will not start

Another copy is running, or another program holds the port:

```bash
yt-final serve --port 8766
```

### I closed the window and lost the URL

`~/.yt_final/webui.url` is a bookmarkable shortcut, and `webui.txt` next to it
holds the current password.

---

## Where things are

| what | where |
| --- | --- |
| downloads | `./downloads` relative to where you ran it — pass `--output-dir` to control it, and `serve` prints the absolute path at startup |
| log | `%LOCALAPPDATA%\yt_final\final_yt.log` (Windows), `~/Library/Application Support/yt_final/` (macOS). Override with `YT_FINAL_LOG` |
| cookies, queue, history, settings | `~/.yt_final/` |

The log redacts credentials, cookies, proxy details, and unnecessary local
paths, so it is safe to read — but skim it before pasting it in public anyway.

---

## Still stuck

The Web UI has a **Field guide** tab with the same material in context, once the
server is running.

Before opening an issue, run `yt-final diagnose` and include its output, your
platform, and the exact command you ran. Do not include cookies, passwords,
session tokens, or unredacted browser traces — see
[SECURITY.md](../SECURITY.md), and report suspected vulnerabilities privately
rather than in a public issue.
