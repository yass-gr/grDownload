# grDownload (`grdl`)

**Agent-first video & music downloader.** Fast, JSON-native, zero-config CLI built on yt-dlp — for scripts, agents, and humans who just want the file.

[![CI](https://github.com/yass-gr/grDownload/actions/workflows/ci.yml/badge.svg)](https://github.com/yass-gr/grDownload/actions/workflows/ci.yml)

```bash
grdl https://youtu.be/VIDEO            # best-quality video
grdl audio https://youtu.be/VIDEO      # music → mp3 with cover art
grdl info https://youtu.be/VIDEO       # metadata as JSON (no download)
cat urls.txt | grdl dl - -j 4          # batch: 4 downloads in parallel
```

## Why it exists

- **Agent-first.** NDJSON events on stdout, deterministic exit codes, zero prompts. stdin is only read when you pipe it. Force modes with `--json` / `--human`.
- **Fast by default.** aria2c 16-connection downloads when installed, 16 concurrent fragments, 20 retries, re-extraction on throttling (`--turbo`), no artificial sleeps, no rate limiting.
- **Hard to block.** A cookie-free fallback ladder for YouTube bot checks: alternate player clients → TLS impersonation (curl_cffi) → plain retry. It never touches your browser cookies unless you explicitly ask.
- **Just works.** No config file, no state. `grdl setup` installs missing dependencies, `grdl update` keeps yt-dlp current, 1800+ sites (YouTube, SoundCloud, Bandcamp, Instagram, TikTok, X/Twitter, Bilibili, Vimeo, ...).
- **Small.** One Python file, stdlib only. Dependencies are the tools you already trust: yt-dlp + ffmpeg, plus optional aria2.

## Install

```bash
# Option 1: one-liner (installs to ~/.local/bin, checks dependencies)
curl -fsSL https://raw.githubusercontent.com/yass-gr/grDownload/main/install.sh | bash
# add --deps to also install missing yt-dlp/ffmpeg/aria2:
curl -fsSL https://raw.githubusercontent.com/yass-gr/grDownload/main/install.sh | bash -s -- --deps

# Option 2: pipx / uv from GitHub
pipx install git+https://github.com/yass-gr/grDownload
uv tool install git+https://github.com/yass-gr/grDownload

# Option 3: manual (single file, no packaging)
curl -fsSL https://raw.githubusercontent.com/yass-gr/grDownload/main/grdl.py -o ~/.local/bin/grdl
chmod +x ~/.local/bin/grdl
```

Requirements: Python 3.9+, yt-dlp, ffmpeg. aria2 is optional but recommended.
`grdl doctor` reports what is missing; `grdl setup` installs it.

## The agent contract

stdout is NDJSON when it is not a TTY (or with `--json`). One JSON object per line:

| event | meaning | key fields |
|---|---|---|
| `start` | a URL begins | `url`, `index`, `total`, `cmd`, `dir` |
| `meta` | media identified | `url`, `id`, `title`, `extractor`, `duration` |
| `progress` | advisory progress | `percent`, `downloaded`, `total`, `speed`, `eta` |
| `post` | ffmpeg / postprocess step | `processor`, `status` |
| `file` | a file is complete on disk | `path`, `size`, `cached` |
| `fallback` | bot check detected; retrying | `attempt`, `max_attempts`, `strategy`, `reason` |
| `done` | a URL finished | `ok`, `partial`, `files`, `errors`, `attempts`, `elapsed` |
| `summary` | everything finished | `ok`, `failed`, `partial`, `files`, `failed_urls` |
| `error` | something failed | `message`, `url` |
| `doctor` / `setup` / `update` | tooling commands | see `grdl doctor --json` |

Exit codes: `0` success · `1` failures or partial downloads · `2` usage error · `130` interrupted.

Rules of thumb for agents:

1. Run `grdl doctor --json` first; expect `"ok": true`.
2. After a download, read `summary.files` — those are absolute paths.
3. Need title/duration before downloading? `grdl info --json URL`.
4. It never prompts and never blocks on input; `--dry-run` prints the exact yt-dlp command.

```bash
grdl dl -j 4 URL1 URL2 URL3 | jq -c 'select(.event=="done") | {ok, files}'
```

## Commands

| command | aliases | purpose |
|---|---|---|
| `grdl download URL...` | `dl`, `get` | best-quality video |
| `grdl audio URL...` | `music`, `mp3` | audio + tagging (default `mp3`) |
| `grdl info URL...` | `meta` | metadata as JSON, no download |
| `grdl formats URL...` | — | available formats table/JSON |
| `grdl doctor` | — | dependency + config check |
| `grdl setup` | — | install missing dependencies |
| `grdl update` | — | update yt-dlp |

`grdl <url>` (bare URL) routes to `download`.

## Options

| flag | default | notes |
|---|---|---|
| `--quality Q` | `best` | `best`, `2160`, `1440`, `1080`, `720`, `480`, `360`, `worst` (aliases: `4k`, `hd`, `sd`) |
| `-f, --format SEL` | — | raw yt-dlp format selector, overrides `--quality` |
| `--compat` | off | force h264+aac mp4 for maximum player compatibility |
| `--audio-format FMT` | `mp3` | `mp3`, `m4a`, `opus`, `flac`, `wav`, `best` |
| `--audio-quality Q` | `0` | `0` best .. `9`, or bitrate e.g. `320K` |
| `--no-cover` | off | don't embed cover art |
| `--single` | off | never expand playlists; only the video in the URL |
| `--subs [LANGS]` | off | download + embed subtitles (default `en.*`) |
| `--dir / -d PATH` | `~/Downloads/grDownload` | where files land |
| `-j, --jobs N` | `3` | URLs downloaded in parallel |
| `-N, --fragments N` | `16` | concurrent fragments per download |
| `--turbo` | off | 32 fragments + abort-and-reconnect on throttling |
| `--no-aria2` | off | use yt-dlp's built-in downloader (finer progress events) |
| `--limit-rate RATE` | unlimited | e.g. `5M` — the only rate limit there is |
| `--cookies FILE` | — | Netscape cookies file |
| `--cookies-from-browser B` | — | `chrome`, `firefox`, `brave`, `edge`, ... |
| `--proxy URL` | — | proxy for all requests |
| `--force` | off | re-download, overwrite existing |
| `--no-fallback` | off | single attempt per URL — disables the bot-check ladder |
| `--impersonate T` | auto | TLS impersonation target (`chrome`, ...); needs curl_cffi |
| `--extractor-args A` | — | raw yt-dlp extractor args (repeatable) |
| `--urls-from FILE` | — | one URL per line, `-` for stdin |
| `--dry-run` | off | print exact yt-dlp commands, don't download |
| `--json` / `--human` | auto | force output mode |
| `-q, --quiet` | off | suppress human chatter |
| `-v, --verbose` | off | stream raw yt-dlp output |

Output template default: `%(title)s [%(id)s].%(ext)s`. Override with `-o`.

## Examples

```bash
# 1080p, and treat a playlist URL as a playlist (default behavior)
grdl dl --quality 1080 'https://www.youtube.com/playlist?list=...'

# Music: whole album, m4a (lossless copy for m4a sources, no re-encode)
grdl audio --audio-format m4a 'https://music.youtube.com/playlist?list=...'

# Parallel batch, custom dir, then report failures only
cat urls.txt | grdl dl -j 6 -d ~/Videos | jq -c 'select(.event=="summary") | {failed, partial, files}'

# Members-only video using the local Chrome session
grdl dl --cookies-from-browser chrome 'https://...'

# Max speed
grdl dl --turbo -j 4 URL1 URL2

# Show the exact command (for debugging / trusting)
grdl dl --dry-run URL
```

## Speed & limits

Default posture is "as fast as the source allows":

- aria2c with 16 connections per file (when installed), 16 concurrent fragments otherwise
- 3 URLs in parallel (`-j`), 20 retries per download, 15s socket timeout
- `--turbo` adds 32 fragments and `--throttled-rate 100K`: segments below 100KB/s are
  aborted and re-requested with fresh URLs, which is the standard fix for YouTube's
  mid-download throttling
- `--ignore-config` is always passed: a stray `~/.config/yt-dlp/config` cannot
  silently slow you down or inject limits

What this is **not**: a DRM or auth bypass. It downloads what yt-dlp can legally
fetch with your permissions — including your own logged-in sessions via
`--cookies-from-browser`. Site-side limits and ToS still apply.

## YouTube bot checks (no cookies required)

YouTube sometimes answers an IP with *"Sign in to confirm you're not a bot"*.
grdl handles that automatically per URL with a cookie-free ladder:

1. **default** — normal extraction
2. **alt-player-clients** — `youtube:player_client=default,tv,mweb`
3. **tls-impersonate** — a real Chrome TLS fingerprint via curl_cffi (`grdl setup --impersonate`)
4. **retry** — short pause + plain retry (many bot checks are transient)

Watch `fallback` events to see which rung fired; `done.attempts` shows how many were
needed. grdl never reads your browser cookies on its own — `--cookies-from-browser`
only runs if *you* pass it. Turn the ladder off with `--no-fallback`.
`grdl doctor` reports impersonation availability and whether a PO-token plugin is
installed — the last resort for hard-blocked IPs is
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider).

## Environment variables

| variable | meaning |
|---|---|
| `GRDL_DIR` | download directory |
| `GRDL_JSON` | `1`/`0` — force JSON output |
| `GRDL_JOBS` | parallel URLs |
| `GRDL_FRAGMENTS` | fragments per download |
| `GRDL_NO_ARIA2` | `1` to disable aria2c |
| `GRDL_NO_FALLBACK` | `1` to disable the bot-check ladder |
| `GRDL_IMPERSONATE` | default TLS impersonation target |
| `GRDL_QUALITY` | default quality |
| `GRDL_AUDIO_FORMAT` | default audio format |
| `GRDL_COOKIES` | cookies file path |
| `GRDL_COOKIES_FROM_BROWSER` | browser name |
| `GRDL_PROXY` | proxy URL |
| `GRDL_YTDLP` | custom yt-dlp command (e.g. `/opt/yt-dlp`) |
| `GRDL_OUTPUT` | default output template |

## Troubleshooting

| symptom | fix |
|---|---|
| `✘ yt-dlp is not installed` | `grdl setup` |
| *"Sign in to confirm you're not a bot"* | handled automatically by the fallback ladder (see below); if it persists run `grdl update` and `grdl setup --impersonate` |
| `HTTP Error 403` / extractor errors | `grdl update` (yt-dlp moves fast) |
| age-restricted / members-only | `grdl dl --cookies-from-browser chrome URL` |
| merged file plays nowhere | re-download with `--compat` (h264+aac mp4) |
| want finer progress events | `--no-aria2` |
| slow despite everything | `--turbo`, fewer `-j` (sites throttle parallel sessions) |

## License

MIT — see [LICENSE](LICENSE).
