# Command-line reference

`yt_final` is one `argparse` parser with eighteen subcommands. This page lists
every command and every option it accepts, taken from the parser in
`Final/yt_final.py` and checked against `--help` output for 1.0.0.

It leads with argument *placement* rather than with the option list, because
that is the rule the parser enforces most rudely: several options belong to the
top-level parser, and typing one after the command name is a usage error, not a
silently ignored flag.

## Invoking

| Form | Where it comes from |
| --- | --- |
| `yt-final ...` | the console script an installed wheel puts on `PATH` (`[project.scripts]` in `pyproject.toml`) |
| `python -m Final ...` | the installed package's `__main__` |
| `python Final/yt_final.py ...` | a source checkout or the portable ZIP |

All three call the same `main()` and propagate the same exit status. Examples
below use `yt-final`; substitute whichever form you installed.

## Top-level flags must precede the command

Options registered on the root parser are not visible to a subparser. Put one
after the command name and argparse stops with `error: unrecognized arguments`
and exits 2 — the whole run is refused, nothing is downloaded:

```console
$ yt-final download "https://youtu.be/..." --output-dir ./out
yt-final: error: unrecognized arguments: --output-dir ./out        # exit 2
```

```console
$ yt-final --output-dir ./out download "https://youtu.be/..."       # correct
```

| Top-level flag | Argument | Default | What it does |
| --- | --- | --- | --- |
| `-h`, `--help` | — | — | Print the top-level help and exit |
| `--version` | — | — | Print the version and exit |
| `--output-dir` | directory | `./downloads` | Where finished media is written |
| `--pot-server` | URL | unset (`http://127.0.0.1:4416` when reachable) | Base URL of a bgutil PO-token provider. A reachable provider unlocks the fast fragmented web clients |
| `--allow-hooks` | — | off | Honor an `on_complete` command that came out of a batch manifest or config file. Off by default because those files get shared |
| `--pot-mode` | `auto`, `http`, `script`, `off`, `lazy` | `auto` | PO-token backend. `lazy` starts tokenless on `android_vr` and escalates only on age-gate, made-for-kids, or no-format failures |
| `--no-update-check` | — | off | Skip the throttled "newer yt-dlp/plugin available" notice at launch |
| `--config` | TOML file | unset | Config file with `[defaults]` and `[preset.NAME]` tables (see [CONFIGURATION.md](CONFIGURATION.md)) |
| `--preset` | preset name | unset | Named preset to apply from `--config`, or from `~/.yt_final/config.toml` when `--config` is omitted |

One command deliberately re-uses a top-level name: `redo` defines its own
`--output-dir`, so `yt-final redo last --output-dir ./out` is valid and
overrides the redirected job's destination. Everywhere else, `--output-dir`
belongs before the command.

## Commands missing from `yt-final --help`

`download`, `encode`, `gpu-info`, `list-profiles` and `troubleshoot` are
registered without a help string, and argparse only lists a subcommand that has
one. They are fully supported and `yt-final download --help` prints their
options; they simply do not appear in the top-level command list. The other
thirteen commands do.

## `download`

```
yt-final download URL [URL ...] [options]
```

Positional: one or more URLs (video, playlist, or channel).

Selection and fetching:

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--quality` | `best`, `4k`, `1440p`, `1080p`, `720p`, `480p`, `audio` | `1080p` | Target resolution, or audio-only extraction |
| `--codec` | `h264`, `vp9`, `av1`, `any` | unset, which selects H.264 | Preferred video codec for selection. H.264 is smaller, edit-friendly, and faster |
| `--encode-mode` | `none`, `copy`, `reencode` | `copy` | Remux only, or run the selected profile |
| `--profile` | profile id | `auto_h264` | Encoder profile; `yt-final list-profiles` prints the ids |
| `--format-id` | yt-dlp format id | unset | Exact format, e.g. `137+140`. Overrides `--quality` and `--codec` |
| `--section` | range | unset | Download a time range only, e.g. `0:00-2:00` or `90-210` |
| `--parallel` | — | off | Download the given URLs concurrently |
| `--workers` | integer | auto | Cap on parallel workers |
| `--speed-priority` | — | off | Accept quality fallbacks in exchange for speed |
| `--simulate` | — | off | Resolve and print the plan (format, profile, container, estimated size, sidecars, output path), then exit without downloading |
| `--download-engine` | `auto`, `builtin`, `hybrid`, `aria2`, `turbo` | `auto` | Byte-fetch path after formats are picked. `auto` behaves as `builtin` |
| `--concurrent-fragments` | integer | auto | Static override for yt-dlp's parallel fragment count (`-N`) |
| `--download-archive` | file | unset | Skip ids already recorded in this file. The main tool for channel and playlist sync |
| `--no-shorts` | — | off | Skip Shorts (60 s or shorter, or `/shorts/` URLs) |

Network and identity:

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--cookies` | `cookies.txt` | auto-discovered | Netscape cookie file. The reliable option |
| `--cookies-from-browser` | `edge`, `chrome`, `firefox`, ... | unset | Best-effort extraction from an installed browser. Close the browser first or it usually fails |
| `--proxy` | URL | unset | HTTP or SOCKS proxy for downloads |
| `--source-address` | local IP | unset | Bind to a specific NIC, useful when recovering from a soft ban |
| `--slow-requests` | — | off | Sleep 2 s between extraction requests to recover from an IP soft ban |

Passing neither `--cookies` nor `--cookies-from-browser` enables cookie
auto-discovery; the search order is documented in
[CONFIGURATION.md](CONFIGURATION.md#cookie-discovery). Passing either flag —
including from a config file or preset — disables it.

Audio:

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--audio-format` | `mp3`, `aac`, `opus`, `flac`, `wav`, `m4a`, `alac`, `best` | unset | With `--quality audio`, the target format. `best` remuxes the native stream with no transcode |
| `--audio-quality` | bitrate or VBR 0-10 | unset | Bitrate (e.g. `320`) or VBR level for the audio-only extractor |
| `--audio-codec` | `copy`, `aac`, `pcm_s24le`, `flac`, `libopus` | profile default | Override the audio codec of a *video* profile |
| `--audio-bitrate` | e.g. `320k` | profile default | Override the audio bitrate of a video profile |
| `--audio-map` | `stereo`, `dual-mono`, `split` | unset | Split a hard-panned stereo interview into two discrete mono tracks. MOV/MKV only; forces a re-mux or re-encode |

Timeline metadata and sidecars:

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--no-chapters` | — | chapters are written | Do not write YouTube chapters as container chapter markers |
| `--no-metadata` | — | metadata is written | Do not tag title, uploader, date, and URL into the container |
| `--embed-thumbnail` | — | off | Embed the thumbnail as cover art |
| `--sub-langs` | comma list | unset | Subtitle languages to fetch, e.g. `en,es` |
| `--sub-mode` | `sidecar`, `embed`, `burn` | unset (behaves as `sidecar`) | What to do with the fetched subtitles |
| `--sub-auto` | — | off | Also fetch auto-generated (ASR) captions |
| `--sub-format` | `srt`, `vtt`, `both` | `srt` | Sidecar format. `vtt` suits Jellyfin, Plex, and browsers |
| `--write-sidecar` | — | off | Write the full `info.json` next to the media |
| `--write-sidecar-text` | — | off | Write `<stem>.description.txt` and `<stem>.chapters.vtt` |
| `--export-markers` | `edl`, `csv`, `fcpxml`, `all` (comma list) | unset | Export chapters as NLE markers. EDL for Resolve, CSV for Premiere, FCPXML for Final Cut and Premiere |
| `--sponsorblock-markers` | `edl`, `csv`, `fcpxml`, `all` | unset | Emit SponsorBlock category markers as `.sponsorblock.*` sidecars. Requires `--sponsorblock mark` |
| `--write-nfo` | — | off | Write a Kodi/Jellyfin/Plex `.nfo` sidecar |
| `--nfo-kind` | `auto`, `movie`, `episode` | `auto` | NFO schema. `auto` picks episode for playlist or series videos |
| `--write-artwork` | — | off | Write sibling `poster.jpg`, `fanart.jpg`, and `folder.jpg` from the thumbnail |
| `--notify-url` | URL | unset | POST a JSON job-complete payload to a Discord, ntfy, or Home Assistant webhook |
| `--notify-template` | `discord`, `slack`, `ntfy`, `raw` | `raw` | Shape of the `--notify-url` body |

Picture and sound processing:

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--sponsorblock` | `mark`, `remove` | unset | Mark SponsorBlock segments as chapters, or cut them out |
| `--loudnorm` | LUFS float | unset | Normalize integrated loudness, e.g. `-14` for web or `-16` for podcast |
| `--no-hdr` | — | HDR is carried | Do not carry HDR10/HLG color metadata through a re-encode |
| `--color-tag` | `auto`, `off` | `auto` | Stamp Rec.709 on untagged SDR streams during re-encode. Copy and remux are never tagged |
| `--proxy-pair` | — | off | Also produce a half-res H.264 editing proxy beside the master |
| `--burn-in` | — | off | Burn a drawtext slate (title, uploader, date, URL) and source timecode into the picture. Forces a re-encode; skipped on stream copy and on CUDA-surface profiles |
| `--verify-strict` | — | warn only | ffprobe the output and fail the job when duration, streams, or size look wrong |

Output and automation:

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--output-template` | yt-dlp template | unset | Override the output template |
| `--organize-by` | `uploader`, `date` | unset | Place each file in a subfolder |
| `--on-complete` | shell command | unset | Run per finished file; `{}` or `{path}` expands to the file path |

`--on-complete` typed on the command line always runs. The same key read out of
a manifest or config file is dropped unless the top-level `--allow-hooks` is
also given, and the drop is logged.

## `encode`

```
yt-final encode INPUT [INPUT ...] [options]
```

Re-encodes or remuxes files that already exist. It downloads nothing, but like
`download` it still runs the throttled "newer version available" notice at
launch unless you pass the top-level `--no-update-check`.

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--profile` | profile id | `auto_h264` | Encoder profile |
| `--mode` | `copy`, `reencode` | `reencode` | Note the different name and default from `download --encode-mode` |
| `--audio-codec` | `copy`, `aac`, `pcm_s24le`, `flac`, `libopus` | profile default | Override the audio codec |
| `--audio-bitrate` | e.g. `320k` | profile default | Override the audio bitrate |
| `--loudnorm` | LUFS float | unset | Normalize integrated loudness after encoding |

## `gpu-info`

```
yt-final gpu-info
```

No options. Prints the detected GPU capability record, then runs a short
benchmark encode with the recommended encoder. `list-profiles` is the cheaper
command when all you want is the profile list.

## `list-profiles`

```
yt-final list-profiles
```

No options. Prints every `--profile` id with a one-line description, including
the ProRes, DNxHR, and AV1 profiles.

## `history`

```
yt-final history [--limit N] [--history-file FILE] [--json]
```

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--limit` | integer | `50` | How many of the most recent rows to show |
| `--history-file` | file | `history.jsonl` in the state directory | Read a different ledger |
| `--json` | — | off | Emit the raw JSONL rows instead of the table |

## `troubleshoot`

```
yt-final troubleshoot
```

No options. Prints the FFmpeg path, the GPU and working encoders, and the
current speed advice. Use `diagnose` when you want a pass/fail verdict and an
exit status.

## `export-cookies`

```
yt-final export-cookies
```

No options. Prints the guided walkthrough for exporting a `cookies.txt` from a
browser, and the exact paths auto-discovery searches on this machine. Read-only
— it prints and exits.

## `auth`

```
yt-final auth ACTION [args]
```

Manages the cookies used for authenticated downloads. The action is required;
running `yt-final auth` with no action, or with an unrecognized one, prints the
accepted list and exits 2.

| Action | Arguments | What it does |
| --- | --- | --- |
| `status` | — | Print `enabled`, `source`, `valid`, `last_validated_at`, and the available tiers |
| `login` | — | One-time interactive login in a native Chrome, Edge, or Brave window you control, saved into a managed profile |
| `import` | `path` (required) | Adopt an existing `cookies.txt` into the cookie store |
| `refresh` | — | Re-acquire and validate now, ignoring the TTL |
| `test` | — | Validate the current cookies against a gated probe video |
| `clear` | — | Delete the stored cookies and profile state |

`login`, `import`, `refresh`, `test`, and `clear` return 0 on success and 1 on
failure, so they compose in a script. `status` only reports, and always returns
0; read its `valid` line rather than its exit code.

## `diagnose`

```
yt-final diagnose [--check-pypi]
```

Live toolchain doctor. Prints a pass/warn/FAIL table and exits non-zero on a
hard failure, which makes it the right command for a setup gate in a script.

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--check-pypi` | — | off | Also probe PyPI for yt-dlp staleness. Needs network; staleness is a warning, never a hard failure |

## `check-updates`

```
yt-final check-updates
```

No options. Reports installed Python packages and optional tool status. Strictly
read-only: it never installs anything.

## `update`

```
yt-final update [--all] [--yes]
```

Certified Python runtime activation is unavailable in 1.0.0; updating means
installing a future reviewed release. Both flags are compatibility aliases kept
so existing scripts keep parsing — neither prompts nor mutates the Python
environment.

## `batch`

```
yt-final batch MANIFEST [--dry-run] [--queue] [--report-json FILE]
```

Runs a declarative manifest of job specs. The manifest shape is documented in
[CONFIGURATION.md](CONFIGURATION.md#batch-manifests).

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--dry-run` | — | off | Parse and print the plan, download nothing. Also shows any `on_complete` hook that would be honored |
| `--queue` | — | off | Enqueue into the persistent journal instead of running inline |
| `--report-json` | file | unset | Write a URL-to-status report, plus a re-runnable `<file>.failures.json` manifest |

A batch that lost an item exits 1, so `batch jobs.json && next_step` does not
march on after a partial failure.

## `queue`

```
yt-final queue ACTION [URL ...] [options]
```

| Action | Needs | What it does |
| --- | --- | --- |
| `add` | URLs, or `--json` | Append a job to the journal |
| `list` | — | Print every job with id, status, priority, and label, plus counts |
| `run` | — | Drain the queue under a single-writer run lock |
| `retry-failed` | — | Reset failed jobs to queued |
| `clear` | — | Remove finished jobs |
| `bump` | `--id` | Raise a job's priority (to `--priority`, or 100) |
| `pause` | — | Stop `run` from draining |
| `resume` | — | Undo `pause` |
| `cancel` | `--id` | Cancel a queued or running job |

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--priority` | integer | `0` | Priority for `add` and `bump`; higher runs first |
| `--id` | job id | unset | Target job for `bump` and `cancel` |
| `--json` | JSON string | unset | A full job spec for `add`, which is how you reach fields that have no `queue` flag |
| `--quality` | see `download` | `1080p` | Shorthand for `add` |
| `--encode-mode` | `none`, `copy`, `reencode` | `copy` | Shorthand for `add` |
| `--profile` | profile id | `auto_h264` | Shorthand for `add` |
| `--queue-file` | file | `queue.json` in the state directory | Use a different journal |
| `--report-json` | file | unset | For `run`: write a URL-to-status report plus `<file>.failures.json` |

`queue run` exits 1 when a job failed. A cancelled job is an outcome, not a
failure, and does not change the exit status.

## `redo`

```
yt-final redo REF [options]
```

Re-runs a prior job from its stored spec. `REF` is a queue job id (`j_...`), a
history index (`3`, or `-1` for the newest), or `last`.

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--profile` | profile id | from the stored job | Override on the way through |
| `--encode-mode` | see `download` | from the stored job | Override. NOTE: `redo` registers this without `choices=`, so an invalid value is accepted here and rejected by `download` |
| `--quality` | see `download` | from the stored job | Override |
| `--codec` | see `download` | from the stored job | Override. Same missing `choices=` as `--encode-mode` above |
| `--output-dir` | directory | from the stored job | Override. This is the per-command flag, not the top-level one |
| `--queue` | — | off | Enqueue instead of running inline |
| `--queue-file` | file | `queue.json` in the state directory | Journal to resolve a `j_...` reference from, and to enqueue into |
| `--history-file` | file | `history.jsonl` in the state directory | Ledger to resolve an index or `last` from |

An unresolvable reference, or a stored spec with no URLs, exits 1.

## `serve`

```
yt-final serve [--host HOST] [--port PORT] [--queue-file FILE] [--no-browser]
```

Starts the local web console and opens it in a browser.

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--host` | loopback host | `127.0.0.1` | Bind address. Non-loopback values are refused |
| `--port` | integer | `8765` | TCP port |
| `--queue-file` | file | `queue.json` in the state directory | Journal the console operates on |
| `--no-browser` | — | off | Do not auto-open a browser |

The server is loopback-only by construction: `--host` is resolved and rejected
unless every address it maps to is a loopback address. There is no LAN bind, no
built-in TLS, and no reverse-proxy trust. A second copy on the same endpoint is
refused rather than silently sharing the port. The unlock password is random per
launch unless `YT_FINAL_WEBUI_PASSWORD` is set — see
[CONFIGURATION.md](CONFIGURATION.md#environment-variables).

## `normalize`

```
yt-final normalize INPUT [INPUT ...] [--lufs LUFS] [--tp TP]
```

Two-pass EBU R128 loudness normalization of files that already exist. Each
output is written beside its input with an `_ln` suffix; existing names are never
overwritten.

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--lufs` | float | `-14.0` | Target integrated loudness |
| `--tp` | float | `-1.5` | Maximum true peak, dBTP |

Exits 1 when no input produced an output.

## `list-formats`

```
yt-final list-formats URL [--json] [--format-id ID]
```

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--json` | — | off | Emit machine-readable JSON instead of the yt-dlp table |
| `--format-id` | format id | unset | Validate that one id exists and print only its row |

## `watch`

```
yt-final watch CHANNEL [options]
```

Subscribes to a channel's RSS feed — no API key — and grabs new uploads.
`CHANNEL` is a channel URL, an `@handle`, or a `UC...` channel id.

| Flag | Argument | Default | Meaning |
| --- | --- | --- | --- |
| `--archive` | file | `~/.yt_final/watch_<channelid>.txt` | Seen-video archive |
| `--interval` | seconds | `0` | Poll every N seconds. `0` runs one pass and exits |
| `--enqueue` | — | off | Add new videos to the persistent queue instead of downloading inline |
| `--max` | integer | `0` | Cap on new videos taken per poll. `0` takes everything in the feed |
| `--quality` | see `download` | `1080p` | Applied to each new video |
| `--encode-mode` | `none`, `copy`, `reencode` | `copy` | Applied to each new video |
| `--profile` | profile id | `auto_h264` | Applied to each new video |
| `--no-shorts` | — | off | Skip Shorts |
| `--filter-title` | regex | unset | Only take uploads whose title matches, case-insensitively |
| `--min-duration` | seconds | unset | Skip uploads shorter than this |
| `--max-duration` | seconds | unset | Skip uploads longer than this |
| `--after` | `YYYYMMDD` | unset | Only take uploads published on or after this date |
| `--queue-file` | file | `queue.json` in the state directory | Journal used by `--enqueue` |

Title and date filters are free because the RSS feed already carries them.
Duration is probed lazily and only when `--min-duration` or `--max-duration` is
set, so the unfiltered path makes no extra network calls. The default archive
path is built from `~` and is *not* redirected by `YT_FINAL_HOME`; pass
`--archive` explicitly if you have moved your state directory.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. Also the code when every URL was skipped by `--download-archive`, which is "nothing new", not a failure |
| `1` | The work failed: no file produced, a strict verification failure, an unresolvable `redo` reference, or at least one failed item in `batch` / `queue run` |
| `2` | Usage error: an unknown command, a top-level flag typed after the command, `auth` with no or an unknown action, or a bare invocation with no terminal attached |

Running `yt-final` with no command in an interactive terminal opens the
interactive menu. With stdin redirected — a shortcut, a scheduled task, a CI
step — there is no terminal to read from, so it prints the help, says so on
stderr, and exits 2 rather than dying on an `EOFError`.

## See also

- [CONFIGURATION.md](CONFIGURATION.md) — `config.toml` tables, job keys, batch
  manifests, and every environment variable.
- [../Final/README.md](../Final/README.md) — the product guide, with worked
  workflows and encoder profile notes.
