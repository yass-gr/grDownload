# grDownload — notes for AI agents

`grdl` is a JSON-first CLI that downloads video/audio from 1800+ sites via yt-dlp.
Designed to be driven by agents: no prompts, no TTY requirements, no state.

## Operating procedure

1. **Check**: `grdl doctor --json` → expect `"ok": true`. If false, run `grdl setup`
   (only after asking the user — it installs packages).
2. **Inspect** (optional): `grdl info --json URL` → trimmed metadata, safe to read.
   Full yt-dlp dump: `--raw`. Playlist listing: `--flat`.
3. **Download**: `grdl dl [-j N] URL...`, or `--urls-from FILE`, or pipe URLs on stdin
   (one per line). Music: `grdl audio URL`. No playlist expansion: `--single`.
4. **Parse**: stdout is NDJSON when not a TTY (always with `--json`). Each line is one
   JSON object with an `event` key: `start`, `meta`, `progress`, `post`, `file`,
   `done`, `summary`, `error`.
5. **Success**: exit code 0 and `summary.failed == 0`. The absolute file paths are in
   `summary.files` (per-URL: `done.files`).

## Failure handling

- Exit 1 = something failed or is partial (`done.ok == false`, `done.partial == true`).
  Read the `error`/`done.errors` messages before retrying.
- Exit 2 = bad usage (e.g. no URLs). Exit 130 = interrupted.
- YouTube bot checks ("Sign in to confirm you're not a bot") are handled automatically
  by a no-cookie fallback ladder — watch `fallback` events; `done.attempts` shows how
  many attempts were used. grdl never reads browser cookies by itself; don't add
  cookie workarounds unless the user explicitly asks.
- `progress` events are advisory: with aria2c there may be only one per file. Never
  treat missing progress as a hang; use `done`/`summary` as ground truth.

## Flags that matter for agent use

- `--dir PATH` — deterministic output location (default `~/Downloads/grDownload`).
- `--dry-run` — emit the exact yt-dlp command; show it to the user if they want proof.
- `--single` — collapse playlists to one video when the user asked for "this video".
- `--quality 1080|720|...`, `--compat` (universal mp4), `--audio-format mp3|m4a|flac`.
- `--no-fallback` — single attempt per URL (strict one-shot behavior).
- `--impersonate chrome` — TLS fingerprint impersonation (needs curl_cffi).
- `--cookies-from-browser chrome` — for media the user's own browser session can see.
- `--proxy URL` — when direct egress fails.

## Do / Don't

- DO set `--dir` when the user cares where files go.
- DO prefer `grdl info --json` over scraping pages for metadata.
- DON'T loop-retry a failing URL with identical flags; the bot-check ladder already
  retries. Read the error first (403 → `grdl update`; age-gate → ask the user before
  using cookies; region → `--proxy`).
- DON'T attempt to bypass DRM or authentication; use the user's own session cookies.
- DON'T parse `--human` output; use JSON mode (the default when piped).

## Install (if missing)

```bash
curl -fsSL https://raw.githubusercontent.com/yass-gr/grDownload/main/install.sh | bash -s -- --deps
```
