---
name: grdl
description: Download video or music from any URL (YouTube, SoundCloud, Bandcamp, Instagram, TikTok, X/Twitter, Bilibili, Vimeo, 1800+ sites) with the `grdl` CLI. Use when the user asks to download, save, grab, rip, or fetch a video, song, playlist, reel, clip, or audio from a link, when batch-downloading URLs, or when inspecting media metadata (title, duration, formats) before downloading.
---

# grdl — video & music downloader

Agent-first yt-dlp wrapper: NDJSON output when stdout is not a TTY, deterministic
exit codes, never prompts. Single file, stdlib only.

## Check installation

```bash
grdl doctor --json    # expect "ok": true
```

If missing:

```bash
curl -fsSL https://raw.githubusercontent.com/yass-gr/grDownload/main/install.sh | bash -s -- --deps
```

## Common tasks

```bash
grdl dl URL                       # best-quality video → ~/Downloads/grDownload
grdl dl -j 4 URL1 URL2 URL3       # parallel
grdl dl -d /path/to/dir URL       # explicit output dir
grdl audio URL                    # mp3 + cover art (--audio-format m4a|flac|opus)
grdl info --json URL              # metadata only, jq-able
cat urls.txt | grdl dl -          # batch from stdin, one URL per line
grdl dl --single PLAYLIST_URL     # force just one video from a playlist URL
```

## Reading results

stdout = NDJSON, one object per line, each with `event`:
`start`, `meta`, `progress`, `post`, `file`, `done`, `summary`, `error`.

- Absolute file paths: `done.files` or `summary.files`.
- Success: exit 0 and `summary.failed == 0`.
- Failure: exit 1 — read `done.errors` / `error.message` before retrying.
- Progress events are advisory (with aria2c you may get only one per file).

## Flags worth knowing

`--quality 1080|720|...` · `--compat` (h264 mp4) · `--single` · `--subs [langs]` ·
`--cookies-from-browser chrome` (user's own session, for gated content) ·
`--proxy URL` · `--no-aria2` (finer progress) · `--turbo` · `--dry-run` (print the
exact yt-dlp command) · `--force` (overwrite).

## Rules

- Never expect prompts; the CLI is non-interactive by design.
- YouTube bot checks are handled automatically with a no-cookie fallback ladder
  (`fallback` events); don't add cookie or proxy workarounds unless the user asks.
- Don't retry a failed URL unchanged: 403/410 → `grdl update`; age/members gate →
  ask the user before using `--cookies-from-browser`; region block → `--proxy`.
- Don't bypass DRM or authentication.
- Prefer `--dir` when the destination matters.
