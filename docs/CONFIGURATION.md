# Configuration reference

Everything `yt_final` reads that is not a command-line flag: the state
directory, the `config.toml` tables, the job keys those tables and the batch
manifests share, and the environment variables. Flags themselves are in
[CLI.md](CLI.md).

Two example files ship with the project and are the fastest way to see the
shapes described here:

- [`Final/examples/config.example.toml`](../Final/examples/config.example.toml)
- [`Final/examples/jobs.example.json`](../Final/examples/jobs.example.json)

## Where state lives

There are two roots, and only one of them is redirectable.

**User state root** — `$YT_FINAL_HOME` if set, otherwise `~/.yt_final`:

| Path | Contents |
| --- | --- |
| `config.toml` | The `[auth]` and `[network]` tables (see below) |
| `queue.json` | The durable download journal, plus its `.lock` and `.flock` sidecars |
| `history.jsonl` | Append-only ledger of finished jobs |
| `watches.json` | Channel subscriptions created from the web console |
| `cookies/` | The managed cookie store, written owner-only |
| `overrides/` | Per-job cookie files uploaded through the web console |
| `webui.url` | An internet shortcut to the running web console |
| `webui.txt` | The current web console password, in clear text, owner-only. Written only by the process that holds the run lock, so a second copy cannot replace a working note with a password that opens nothing |

**Cache and app-data root** — OS-chosen and *not* affected by `YT_FINAL_HOME`:
`%LOCALAPPDATA%\yt_final` on Windows, `~/Library/Caches/yt_final` and
`~/Library/Application Support/yt_final` on macOS. It holds the capability cache
(`caps_cache.json`), the update-check throttle, and managed FFmpeg and Node
installs. Moving your state directory does not move these; they are rebuildable
caches and platform-managed tool installs, not user data.

Two paths are built from `~` directly and stay in `~/.yt_final` even when
`YT_FINAL_HOME` is set:

- the `config.toml` that `--preset` falls back to when `--config` is omitted; and
- the default `watch` archive, `~/.yt_final/watch_<channelid>.txt`.

So if you set `YT_FINAL_HOME`, pass `--config` and `watch --archive` explicitly
rather than relying on their defaults. Note the consequence for `config.toml`:
the `[auth]` and `[network]` tables are read from the redirected directory,
while `[defaults]` and `[preset.NAME]` are read from `~/.yt_final` — the same
file name resolved from two different places.

### Cookie discovery

When a `download` job specifies neither `cookiefile` nor `cookies_from_browser`,
the first existing file wins:

1. `$YT_FINAL_COOKIES`, treated as the full path to the file;
2. `./cookies.txt` in the current working directory;
3. `<output-dir>/cookies.txt`;
4. `<state root>/cookies.txt`, where the state root honors `YT_FINAL_HOME`.

The chosen path is logged. Setting either flag — on the command line, or through
a config file or preset — disables discovery entirely.

The cookie *provider* chain used by `auth` and by `use_auth` jobs searches a
longer list that also includes `<state root>/cookies/active.txt` and
`<state root>/cookies/cookies.txt`; `yt-final export-cookies` prints the exact
paths for the machine you are on.

## `config.toml`

One file, four independent tables. Nothing here is created for you; an install
with no `config.toml` uses the defaults below.

| Table | Read by | Read from |
| --- | --- | --- |
| `[defaults]` | `download`, when `--config` or `--preset` is given | the `--config` path, else `~/.yt_final/config.toml` |
| `[preset.NAME]` | `download --preset NAME` | same as `[defaults]` |
| `[auth]` | the cookie manager | `<state root>/config.toml` |
| `[network]` | the web console and the jobs it runs | `<state root>/config.toml` |

A parse error is never fatal: both loaders log or swallow it and fall back to
defaults. That is deliberate — a typo in an optional file must not stop a
download — but it also means a broken `config.toml` looks like a file that is
simply being ignored.

### `[defaults]` and `[preset.NAME]`

Both tables take the [job keys](#job-keys) below. Precedence is
`[defaults]` < `[preset.NAME]` < explicit command-line flags.

"Explicit" is decided by scanning the raw command line for each option string,
not by comparing values against parser defaults. That distinction matters:
`--quality 1080p` overrides a preset's `quality` even though `1080p` is also the
argparse default, because you typed the flag.

The bare fast path never reads a config file. `download` layers `[defaults]` and
`[preset.NAME]` only when you pass `--config` or `--preset`, so an unrelated
`~/.yt_final/config.toml` cannot change what a plain `yt-final download URL`
does.

`Final/examples/config.example.toml`, without its comment header:

```toml
[defaults]
quality = "1080p"
codec = "h264"
encode_mode = "copy"
chapters = true
embed_metadata = true
preserve_hdr = true

# Editorial master: ProRes HQ, 24-bit PCM audio, subtitles embedded, loudness for broadcast.
[preset.editorial]
encode_mode = "reencode"
profile = "prores_hq"
audio_codec = "pcm_s24le"
sub_langs = ["en"]
sub_mode = "embed"
loudnorm = -16

# Small AV1 archive copy with film-grain synthesis (great for grainy/film content).
[preset.archive]
quality = "4k"
codec = "av1"
encode_mode = "reencode"
profile = "cpu_av1_svt_filmgrain"
embed_thumbnail = true
write_sidecar = true

# Lossless audio pull for music/sound editorial.
[preset.audio]
quality = "audio"
audio_format = "flac"
loudnorm = -14

# Web delivery: capped-size AV1 on the GPU, -14 LUFS, SponsorBlock segments cut out.
[preset.web]
encode_mode = "reencode"
profile = "nvenc_av1_vbr"
loudnorm = -14
sponsorblock = "remove"
```

Apply one with:

```console
$ yt-final --config Final/examples/config.example.toml --preset archive download URL
```

### `[auth]`

Controls optional cookie-based authentication. Keys outside this set are
ignored.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch. While false, computing the cookie paths creates nothing on disk |
| `order` | list of strings | `["file", "profile", "browser"]` | Tier chain, tried in order. `file` is a `cookies.txt`, `profile` is the managed browser profile from `auth login`, `browser` is best-effort extraction from an installed browser |
| `cookiefile` | string | unset | Explicit path for the `file` tier |
| `browser` | string | `"auto"` | Which browser the `browser` tier extracts from |
| `validate_ttl_min` | integer | `45` | Minutes a validated session is trusted before revalidation |
| `probe_video_id` | string | `"HtVdAasjOgU"` | The age-restricted video used to prove a session works |

```toml
[auth]
enabled = true
order = ["file", "profile"]
validate_ttl_min = 60
```

### `[network]`

Written by the web console's network settings; the writer rewrites only this
table and leaves the rest of the file intact.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Whether the default proxy is applied to jobs that do not set their own |
| `proxy` | string | `""` | Default proxy URL |
| `desync` | bool | `false` | Route downloads through the local DPI-desync proxy |
| `desync_mode` | `"oob"`, `"frag"`, `"frag_oob"`, `"tlsrec"` | `"oob"` | Desync strategy. An unrecognized value falls back to `oob` |
| `doh` | bool | `false` | Use DNS-over-HTTPS inside the desync child process |

Proxy URLs are validated before use: scheme must be `http`, `socks4`, `socks5`,
or `socks5h`; a host and a port are both required; credentials are rejected
(passwordless proxies only); and control characters or quotes are refused so a
value cannot inject TOML back into `config.toml`. `https` proxies — the proxy
itself reached over TLS — are rejected on purpose, because the managed yt-dlp
lacks the backend they need and they would fail at download time instead.

Per job, a spec's own `proxy` wins; the literal value `direct` (any case) forces
no proxy even when a default is enabled.

## Job keys

One dataclass, `JobSpec`, is the contract for `[defaults]`, `[preset.NAME]`,
every entry in a batch manifest, `queue add --json`, the durable queue rows, and
the web console form. Unknown keys are ignored, and strings are coerced, so the
same document works from TOML, from JSON, and from an HTML form.

Three friendly aliases are accepted on input: `url` for `urls`, `mode` for
`encode_mode`, and `quality_level` for `quality`.

| Key | Type | Default | CLI flag | Notes |
| --- | --- | --- | --- | --- |
| `urls` | list of strings | `[]` | positional | A string is split on whitespace only, never on commas — a YouTube URL can contain them |
| `output_dir` | string | engine default | *(none)* | Per-job destination. On the command line this is the top-level `--output-dir` |
| `quality` | string | `"1080p"` | `--quality` | |
| `codec` | string | unset (h264) | `--codec` | |
| `encode_mode` | string | `"copy"` | `--encode-mode` | Not validated on input; a typo reaches the engine unchanged |
| `profile` | string | `"auto_h264"` | `--profile` | |
| `section` | string | unset | `--section` | |
| `section_precise` | bool | `false` | *(none)* | Force keyframes at cuts: slower, frame-accurate |
| `format_id` | string | unset | `--format-id` | |
| `parallel` | bool | `false` | `--parallel` | |
| `workers` | integer | auto | `--workers` | |
| `speed_priority` | bool | `false` | `--speed-priority` | |
| `cookies_from_browser` | string | unset | `--cookies-from-browser` | |
| `cookiefile` | string | unset | `--cookies` | |
| `use_auth` | bool | `true` | *(none)* | When true and `cookiefile` is blank, the configured auth tiers may supply cookies. Set false to keep a job anonymous |
| `slow_requests` | bool | `false` | `--slow-requests` | |
| `source_address` | string | unset | `--source-address` | |
| `proxy` | string | unset | `--proxy` | `direct` forces no proxy |
| `concurrent_fragments` | integer | auto | `--concurrent-fragments` | |
| `download_engine` | string | `"auto"` | `--download-engine` | Aliases such as `aria2c`, `native`, or `n-m3u8dl-re` are normalized; anything unrecognized becomes `auto` |
| `download_archive` | string | unset | `--download-archive` | |
| `audio_format` | string | unset | `--audio-format` | |
| `audio_quality` | string | unset | `--audio-quality` | Numbers are coerced to strings |
| `audio_codec` | string | unset | `--audio-codec` | |
| `audio_bitrate` | string | unset | `--audio-bitrate` | |
| `audio_map` | string | unset | `--audio-map` | Only `stereo`, `dual-mono`, `split`; anything else becomes unset |
| `chapters` | bool | `true` | `--no-chapters` inverts | |
| `embed_metadata` | bool | `true` | `--no-metadata` inverts | |
| `embed_thumbnail` | bool | `false` | `--embed-thumbnail` | |
| `sub_langs` | list of strings | unset | `--sub-langs` | A string here *is* split on commas and semicolons |
| `sub_mode` | string | unset (behaves as `sidecar`) | `--sub-mode` | |
| `sub_auto` | bool | `false` | `--sub-auto` | |
| `sub_format` | string | `"srt"` | `--sub-format` | Only `srt`, `vtt`, `both`; anything else becomes `srt` |
| `write_sidecar` | bool | `false` | `--write-sidecar` | |
| `write_sidecar_text` | bool | `false` | `--write-sidecar-text` | |
| `export_markers` | string | unset | `--export-markers` | |
| `sponsorblock_markers` | string | unset | `--sponsorblock-markers` | |
| `no_shorts` | bool | `false` | `--no-shorts` | |
| `write_nfo` | bool | `false` | `--write-nfo` | |
| `burn_in` | bool | `false` | `--burn-in` | |
| `nfo_kind` | string | `"auto"` | `--nfo-kind` | Only `auto`, `movie`, `episode`; anything else becomes `auto` |
| `write_artwork` | bool | `false` | `--write-artwork` | |
| `proxy_pair` | bool | `false` | `--proxy-pair` | |
| `notify_url` | string | unset | `--notify-url` | |
| `notify_template` | string | `"raw"` | `--notify-template` | Only `discord`, `slack`, `ntfy`, `raw`; anything else becomes `raw` |
| `sponsorblock` | string | unset | `--sponsorblock` | |
| `loudnorm` | float | unset | `--loudnorm` | A non-finite number is rejected |
| `preserve_hdr` | bool | `true` | `--no-hdr` inverts | |
| `color_tag` | string | `"auto"` | `--color-tag` | Only `auto`, `off`; anything else becomes `auto` |
| `verify_strict` | bool | `false` | `--verify-strict` | |
| `two_pass` | bool | `false` | *(none)* | Parsed and persisted, but **not implemented**; the job encodes single-pass and says so |
| `bitrate` | string | unset | *(none)* | Parsed and persisted, but **not applied**; the profile's own rate control is used |
| `output_template` | string | unset | `--output-template` | |
| `organize_by` | string | unset | `--organize-by` | |
| `on_complete` | string | unset | `--on-complete` | Runs a program. Ignored when it comes from a file unless `--allow-hooks` is passed |

Booleans accept the usual spellings on input: `1`, `true`, `yes`, `y`, `on` and
their negatives, as well as real TOML and JSON booleans.

`two_pass` and `bitrate` are the two keys accepted by the contract that no
encoder consumes yet. They are surfaced as warnings rather than dropped silently
so a request never looks like it worked.

## Batch manifests

`yt-final batch MANIFEST` reads a `.json` or `.toml` file — the suffix decides
the parser. Three shapes are accepted:

```json
["https://youtu.be/AAA", "https://youtu.be/BBB"]
```

```json
{ "jobs": [ { "urls": ["https://youtu.be/AAA"], "quality": "4k" } ] }
```

```json
{
  "defaults": { "quality": "1080p", "encode_mode": "copy" },
  "jobs": [ { "urls": ["https://youtu.be/AAA"], "embed_thumbnail": true } ]
}
```

A bare string inside `jobs` is expanded to `{"urls": [that string]}`. Each job
is `defaults` merged with the job's own keys, job keys winning, and the result
is a [job spec](#job-keys). Keys the contract does not know are ignored.

`Final/examples/jobs.example.json`, without its comment keys:

```json
{
  "_comment": "Run with:  python yt_final.py batch examples/jobs.example.json   (add --dry-run to preview, --queue to enqueue)",
  "defaults": {
    "quality": "1080p",
    "codec": "h264",
    "encode_mode": "copy",
    "chapters": true,
    "embed_metadata": true
  },
  "jobs": [
    {
      "urls": ["https://www.youtube.com/watch?v=4YrzJ9RZ9qY"],
      "section": "0:00-0:30",
      "embed_thumbnail": true
    },
    {
      "urls": ["https://www.youtube.com/watch?v=4YrzJ9RZ9qY"],
      "quality": "audio",
      "audio_format": "flac",
      "loudnorm": -14
    },
    {
      "urls": ["https://www.youtube.com/watch?v=4YrzJ9RZ9qY"],
      "encode_mode": "reencode",
      "profile": "prores_hq",
      "audio_codec": "pcm_s24le",
      "sub_langs": ["en"],
      "sub_mode": "embed"
    }
  ],
  "_security": "An 'on_complete' key in a manifest runs a program when the job finishes. It is IGNORED unless you also pass the top-level --allow-hooks flag. Never run a manifest you did not write without reading it first: --dry-run lists any hook that would be honored."
}
```

Keys beginning with `_` are not job keys; they are ignored like any other
unknown key, which is what makes them usable as comments in JSON.

### The `on_complete` trust boundary

`on_complete` runs a program when a job finishes. Typing `--on-complete`
yourself is fine — you typed it. Reading it out of a manifest or a `config.toml`
is a different thing: those files get shared, pasted into issues, copied from
gists, and `batch --queue` persists the hook into `queue.json` where the runner
will honor it later.

So a hook that came from a file is stripped unless the top-level `--allow-hooks`
flag is also given. Stripped hooks are logged, honored hooks are logged, and
`batch --dry-run` prints any hook that is in effect, verbatim and untruncated.
The web console applies the same rule and never lets a client supply a hook at
all.

## Environment variables

### Read at runtime

| Variable | Default | Effect |
| --- | --- | --- |
| `YT_FINAL_HOME` | `~/.yt_final` | Moves the user state root: queue journal, history ledger, watch store, cookie store, and the `[auth]` / `[network]` `config.toml`. The only supported way to keep state off the home drive. Does not move the OS cache and app-data root, the `--preset` fallback config path, or the default `watch` archive |
| `YT_FINAL_COOKIES` | unset | Full path to a `cookies.txt`, tried first by cookie auto-discovery and by the `file` provider tier |
| `YT_FINAL_WEBUI_PASSWORD` | a random per-launch password | Overrides the web console unlock password. Set it when you need a stable password across restarts; leave it unset to keep the per-launch random one, which is the safer default. The value is compared in constant time |
| `YT_FINAL_DESYNC_PORT` | `8799` | Loopback port for the local DPI-desync proxy child process |
| `YT_FINAL_WATCH_INTERVAL` | `900` | Seconds between automatic channel-watch polls in the web console |
| `YT_FINAL_NO_CAPS_CACHE` | unset | Any non-empty value disables both reading and writing the GPU capability cache, forcing a fresh probe every run. Useful right after a driver or FFmpeg change |

`YT_FINAL_CANCEL` looks like a seventh variable if you grep for the prefix. It
is not one: it is the sentinel text carried inside the `DownloadCancelled`
exception so every layer can recognize a cancellation. Setting it in the
environment does nothing.

### Read only by the test suite and CI

These configure the release, packaging, and browser gates. The shipped
application never reads them, and setting them changes nothing about a download.

| Variable | Used by |
| --- | --- |
| `YT_FINAL_BUILD_PYTHON` | packaging tests: the canonical absolute interpreter used to build |
| `YT_FINAL_RELEASE_PYTHON` | packaging tests: the interpreter the release is verified against |
| `YT_FINAL_LOCK_PYTHON` | packaging tests: the interpreter that regenerates dependency locks |
| `YT_FINAL_NATIVE_TARGET` | packaging tests: which native platform target is being checked |
| `YT_FINAL_VERIFY_DISTRIBUTIONS` | packaging tests: opt in to distribution verification |
| `YT_FINAL_VERIFY_LOCK_REGEN` | packaging tests: opt in to lock regeneration checks |
| `YT_FINAL_VERIFY_LOCK_WHEELS` | packaging tests: opt in to lock wheel checks |
| `YT_FINAL_VERIFY_NATIVE_LOCK` | packaging tests: opt in to native lock checks |
| `YT_FINAL_BROWSER_NAME` | the Playwright browser gate: which engine to run |
| `YT_FINAL_BROWSER_PORT` | the browser gate: the loopback port the test server binds |
| `YT_FINAL_BROWSER_STATE` | the browser gate: the isolated state root for one run |
| `YT_FINAL_BROWSER_PASSWORD` | the browser gate: the password the test server is started with |
| `YT_FINAL_SEAM_PYTHON` | the stdout-isolation seam test: the interpreter to launch |
| `YT_FINAL_SEAM_REPORT` | the seam test: where the child writes its report |
| `YT_FINAL_SEAM_STOP_FILE` | the seam test: the file whose appearance stops the child |
| `YT_FINAL_SEAM_FORCE_PIPE` | the seam test: force the piped-stdout path |
| `YT_FINAL_SEAM_KILL_IMMEDIATELY` | the seam test: exercise abrupt termination |
| `YT_FINAL_WORKFLOW_MANIFEST` | the CI workflow syntax test: the manifest handed to PowerShell |

### Other variables the application reads

Not `YT_FINAL_*`, but they change behavior on a supported platform.

| Variable | Effect |
| --- | --- |
| `BGUTIL_POT_BASE_URL` | Fallback base URL for the bgutil PO-token provider when `--pot-server` is not passed. Defaults to `http://127.0.0.1:4416` |
| `BGUTIL_POT_MODE` | Fallback PO-token mode when `--pot-mode` is not passed |
| `BGUTIL_POT_SERVER_HOME` | Directory of the bgutil provider's server checkout, used by `--pot-mode script`. Defaults to `~/bgutil-ytdlp-pot-provider/server` |
| `LOCALAPPDATA` | On Windows, the parent of the cache and app-data root. Without it the code falls back to `~/AppData/Local` |
| `PLAYWRIGHT_BROWSERS_PATH` | An extra root searched when detecting installed Playwright browsers |

On platforms outside the 1.0.0 support contract the cache and app-data roots
fall back to `XDG_CACHE_HOME` and `XDG_DATA_HOME` instead of `LOCALAPPDATA`.

## See also

- [CLI.md](CLI.md) — every command and flag, and which flags are top-level.
- [../Final/README.md](../Final/README.md) — the product guide.
- [../SECURITY.md](../SECURITY.md) — the local-storage and web console
  security model.
