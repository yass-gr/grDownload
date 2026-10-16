#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""grDownload (grdl) — agent-first video & music downloader.

Thin, machine-friendly wrapper around yt-dlp: 1800+ sites, playlists,
best-quality video, music extraction, subtitles. NDJSON output for agents,
live progress for humans. Fast defaults: aria2c multi-connection downloads,
16 concurrent fragments, 20 retries, zero artificial sleeps.

Usage:
  grdl <url> [url ...]        # download video (best quality)
  grdl audio <url>            # download music as mp3
  grdl info <url>             # metadata as JSON
  grdl formats <url>          # list available formats
  grdl doctor                 # check dependencies
  grdl setup                  # install missing dependencies
  grdl update                 # update yt-dlp

Exit codes: 0 = success, 1 = one or more downloads failed, 2 = usage error,
130 = interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

__version__ = "1.1.0"
REPO_URL = "https://github.com/yass-gr/grDownload"

# Unit separator: JSON-encoded template values escape control characters, so a
# literal \x1f can never appear inside a value — safe field delimiter.
SEP = "\x1f"

PROGRESS_PREFIX = "grdl-progress"
POST_PREFIX = "grdl-post"
META_PREFIX = "grdl-meta"
FILE_PREFIX = "grdl-file"

URL_RE = re.compile(r"^(?:https?|ftp)://\S+$", re.I)
T0 = time.time()

DEFAULT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"
ARIA2_ARGS = (
    "-x16 -s16 -k1M --show-console-readout=false --file-allocation=none "
    "--console-log-level=warn --summary-interval=0 --max-tries=10 --retry-wait=1"
)

CHILDREN: list = []
CHILD_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# output layer: NDJSON for agents, pretty text for humans
# ---------------------------------------------------------------------------

class Out:
    def __init__(self) -> None:
        self.json = False
        self.quiet = False
        self.lock = threading.Lock()
        self._live = False

    def event(self, kind: str, **fields) -> None:
        with self.lock:
            if not self.json:
                return
            rec = {"event": kind, "t": round(time.time() - T0, 3)}
            rec.update(fields)
            print(json.dumps(rec, ensure_ascii=False, default=str), flush=True)

    def human(self, msg: str, err: bool = False) -> None:
        with self.lock:
            if self.json or self.quiet:
                return
            print(msg, file=sys.stderr if err else sys.stdout, flush=True)

    def progress(self, line: str) -> None:
        with self.lock:
            if self.json or self.quiet:
                return
            sys.stdout.write("\r\033[K" + line)
            sys.stdout.flush()
            self._live = True

    def end_progress(self) -> None:
        with self.lock:
            if self._live:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._live = False


OUT = Out()


def fail(message: str, code: int = 1, **extra):
    OUT.event("error", message=message, **extra)
    OUT.human("✘ " + message, err=True)
    return code


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def which(cmd: str):
    return shutil.which(cmd)


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def human_time(sec) -> str:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "?"
    if sec < 0:
        return "?"
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


def env_bool(name: str):
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_url_list(text: str):
    """One URL per line; '#' comments; extra tokens after a URL are ignored."""
    urls = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        pick = None
        if len(tokens) == 1:
            pick = tokens[0]
        else:
            for tok in tokens:
                if URL_RE.match(tok):
                    pick = tok
                    break
            if pick is None:
                pick = tokens[0]
        if pick and pick not in seen:
            seen.add(pick)
            urls.append(pick)
    return urls


def read_stdin_maybe() -> str:
    """Read stdin only when it looks like a pipe with data (never hang agents)."""
    try:
        if sys.stdin.isatty():
            return ""
    except Exception:
        return ""
    try:
        import select
        ready, _, _ = select.select([sys.stdin], [], [], 0.25)
        if not ready:
            return ""
    except Exception:
        pass
    try:
        return sys.stdin.read()
    except Exception:
        return ""


def normalize_argv(argv):
    """`grdl <url>` and `grdl --json <url>` both route to the download command."""
    commands = {"download", "dl", "get", "audio", "music", "mp3", "info", "meta",
                "formats", "doctor", "setup", "update"}
    value_flags = {"-d", "--dir", "-j", "--jobs", "-N", "--fragments",
                   "--limit-rate", "--cookies", "--cookies-from-browser",
                   "--proxy", "-o", "--output", "-f", "--format", "--quality",
                   "--audio-format", "--audio-quality", "--urls-from", "--subs"}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in commands:
            return argv
        if tok.startswith("-"):
            if "=" in tok:
                i += 1
            elif tok in value_flags:
                i += 2
            else:
                i += 1
            continue
        return argv[:i] + ["download"] + argv[i:]
    return argv


# ---------------------------------------------------------------------------
# dependency discovery
# ---------------------------------------------------------------------------

def resolve_ytdlp():
    override = os.environ.get("GRDL_YTDLP")
    if override:
        return shlex.split(override)
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None


YTDLP = resolve_ytdlp()
FFMPEG = shutil.which("ffmpeg")
ARIA2 = shutil.which("aria2c")


def require_ytdlp():
    if YTDLP:
        return None
    return fail("yt-dlp is not installed. Run: grdl setup", 1, missing=["yt-dlp"])


# ---------------------------------------------------------------------------
# yt-dlp argument construction
# ---------------------------------------------------------------------------

def parse_progress(rest: str) -> dict:
    toks = rest.split(" ")

    def num(t: str):
        if t in ("NA", "null", "None", ""):
            return None
        try:
            v = json.loads(t)
            return v if isinstance(v, (int, float)) else None
        except Exception:
            return None

    while len(toks) < 7:
        toks.append("NA")
    return {
        "downloaded": num(toks[0]),
        "total": num(toks[1]),
        "estimate": num(toks[2]),
        "speed": num(toks[3]),
        "eta": num(toks[4]),
        "fragment_index": num(toks[5]),
        "fragment_count": num(toks[6]),
    }


def parse_sep(rest: str):
    out = []
    for part in rest.split(SEP):
        if part in ("NA", ""):
            out.append(None)
            continue
        try:
            out.append(json.loads(part))
        except Exception:
            out.append(part)
    return out


def format_spec(o) -> str:
    compat = getattr(o, "compat", False)
    quality = str(getattr(o, "quality", "best") or "best")
    if quality == "worst":
        return "wv*+wa/w"
    if compat:
        cap = "" if quality == "best" else f"[height<={quality}]"
        return (f"bv*[vcodec^=avc]{cap}+ba[acodec^=mp4a]/"
                f"b[ext=mp4]{cap}/b{cap}/b")
    if quality == "best":
        return "bv*+ba/b"
    return f"bv*[height<={quality}]+ba/b[height<={quality}]/b"


def sort_spec(o) -> str:
    if getattr(o, "compat", False):
        return "res,br"
    # Prefer h264+aac at the same resolution: those merge into a universally
    # playable mp4. Higher resolutions (AV1/VP9-only) still win on `res`.
    return "res,vcodec:h264,acodec:m4a,br"


# ---------------------------------------------------------------------------
# cookie-free fallback ladder (YouTube bot checks and friends)
# ---------------------------------------------------------------------------

URL_YOUTUBE_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I)
FALLBACK_TRIGGER_RE = re.compile(
    r"sign in to confirm|not a bot|page needs to be reloaded|"
    r"no video formats found|unable to extract|"
    r"failed to extract any player response|http error 403",
    re.I)
YOUTUBE_ALT_CLIENTS = "youtube:player_client=default,tv,mweb"

_IMPERSONATE_CACHE = None


def is_youtube_url(url: str) -> bool:
    return bool(URL_YOUTUBE_RE.match(url or ""))


def impersonate_targets(refresh: bool = False):
    """(client, os, source) impersonation targets supported by the yt-dlp in use."""
    global _IMPERSONATE_CACHE
    if _IMPERSONATE_CACHE is not None and not refresh:
        return _IMPERSONATE_CACHE
    targets = []
    if YTDLP:
        try:
            code, out, _ = run_capture(
                list(YTDLP) + ["--ignore-config", "--list-impersonate-targets"])
            if code == 0:
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and parts[-1] == "curl_cffi":
                        targets.append(tuple(parts[:3]))
        except Exception:
            pass
    _IMPERSONATE_CACHE = targets
    return targets


def impersonate_default_target():
    targets = impersonate_targets()
    for client, _os, _src in targets:
        if client.lower().startswith("chrome"):
            return "chrome"
    return targets[0][0] if targets else None


def build_fallback_ladder(o, url):
    """Ordered (strategy, extra_args, pause_seconds) attempts for one URL.

    Triggered when a YouTube attempt fails in a recognizable bot-check way.
    Deliberately cookie-free: alternate player clients, then TLS impersonation
    (when curl_cffi is available), then one plain retry for transient checks.
    Disable with --no-fallback / GRDL_NO_FALLBACK=1.
    """
    ladder = [("default", [], 0.0)]
    if o.no_fallback or not is_youtube_url(url):
        return ladder
    user_youtube_args = any("youtube:" in str(a) for a in (o.extractor_args or []))
    alt = [] if user_youtube_args else ["--extractor-args", YOUTUBE_ALT_CLIENTS]
    if alt:
        ladder.append(("alt-player-clients", alt, 1.5))
    if not o.impersonate:
        target = impersonate_default_target()
        if target:
            ladder.append(("tls-impersonate", ["--impersonate", target] + alt, 1.5))
    ladder.append(("retry", [], 4.0))
    return ladder


def yt_common_args(o) -> list:
    args = [
        "--ignore-config",
        "--no-warnings",
        "--no-simulate",
        "--progress",
        "--newline",
        "--progress-template",
        f"download:{PROGRESS_PREFIX} "
        "%(progress.downloaded_bytes)j %(progress.total_bytes)j "
        "%(progress.total_bytes_estimate)j %(progress.speed)j "
        "%(progress.eta)j %(progress.fragment_index)j %(progress.fragment_count)j",
        "--progress-template",
        f"postprocess:{POST_PREFIX} %(progress.postprocessor)j %(progress.status)j",
        "--print",
        META_PREFIX + SEP + "%(id)j" + SEP + "%(title)j" + SEP + "%(extractor)j"
        + SEP + "%(webpage_url)j" + SEP + "%(duration|0)j",
        "--print",
        "after_move:" + FILE_PREFIX + SEP + "%(filepath)j",
        "--retries", "20",
        "--fragment-retries", "20",
        "--file-access-retries", "10",
        "--socket-timeout", "15",
        "--concurrent-fragments", str(o.fragments),
        "--trim-filenames", "160",
        "-P", f"home:{o.dir}",
        "-o", o.output,
    ]
    if not o.verbose:
        args.insert(1, "-q")
    if ARIA2 and not o.no_aria2:
        args += ["--downloader", "aria2c", "--downloader-args", "aria2c:" + ARIA2_ARGS]
    else:
        args += ["--http-chunk-size", "10M"]
    if o.turbo:
        args += ["--throttled-rate", "100K"]
    if o.limit_rate:
        args += ["--limit-rate", str(o.limit_rate)]
    if o.cookies_from_browser:
        args += ["--cookies-from-browser", o.cookies_from_browser]
    elif o.cookies:
        args += ["--cookies", o.cookies]
    if o.proxy:
        args += ["--proxy", o.proxy]
    if o.impersonate:
        args += ["--impersonate", o.impersonate]
    for ea in (o.extractor_args or []):
        args += ["--extractor-args", ea]
    return args


def build_download_argv(o, url: str, mode: str = "video", extra_args=None) -> list:
    args = list(YTDLP or []) + yt_common_args(o) + ["-i"]
    if o.single:
        args.append("--no-playlist")
    if o.force:
        args.append("--force-overwrites")
    if mode == "audio":
        args += ["-x", "--audio-format", o.audio_format,
                 "--audio-quality", str(o.audio_quality), "--embed-metadata"]
        if not o.no_cover:
            args.append("--embed-thumbnail")
    else:
        if getattr(o, "format_override", None):
            args += ["-f", o.format_override]
        else:
            args += ["-f", format_spec(o), "-S", sort_spec(o)]
        args += ["--merge-output-format", "mp4/mkv"]
        if o.subs:
            args += ["--write-subs", "--write-auto-subs",
                     "--sub-langs", o.subs, "--embed-subs"]
    if extra_args:
        args += list(extra_args)
    args.append(url)
    return args


# ---------------------------------------------------------------------------
# process plumbing
# ---------------------------------------------------------------------------

def kill_children() -> None:
    with CHILD_LOCK:
        children = list(CHILDREN)
    for proc in children:
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            pass


def run_streaming(args: list, on_line) -> int:
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=hasattr(os, "killpg"),
    )
    with CHILD_LOCK:
        CHILDREN.append(proc)

    def pump(stream, is_err: bool):
        try:
            for line in stream:
                on_line(line.rstrip("\n"), is_err)
        except Exception:
            pass

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, False), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, True), daemon=True),
    ]
    for t in threads:
        t.start()
    rc = proc.wait()
    for t in threads:
        t.join()
    with CHILD_LOCK:
        try:
            CHILDREN.remove(proc)
        except ValueError:
            pass
    return rc


def existing_files(dirp: Path, vid) -> list:
    """Files already on disk for a video id (used when yt-dlp skips a re-run)."""
    if not vid:
        return []
    hits = []
    try:
        for name in os.listdir(dirp):
            if name.endswith((".part", ".ytdl", ".aria2")):
                continue
            if f"[{vid}]" in name:
                hits.append(str(dirp / name))
    except OSError:
        return []
    hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return hits


def filesize(path: str):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# download engine
# ---------------------------------------------------------------------------

def _attempt(o, url, idx, total, allow_live, extra_args, strategy, attempt_no):
    """Run yt-dlp once for one URL; returns the per-attempt state dict."""
    state = {
        "url": url,
        "idx": idx,
        "files": [],
        "seen": set(),
        "errors": [],
        "tail": deque(maxlen=30),
        "meta": None,
        "skipped": False,
        "progress_seen": False,
        "speed": None,
        "label": url if len(url) < 70 else url[:67] + "...",
        "live": allow_live and not OUT.json,
        "strategy": strategy,
        "rc": 0,
        "triggered": False,
    }
    mode = "audio" if o.command == "audio" else "video"
    args = build_download_argv(o, url, mode=mode, extra_args=extra_args)
    OUT.event("start", url=url, index=idx, total=total, attempt=attempt_no,
              strategy=strategy, cmd=shlex.join(args), dir=str(o.dir), mode=o.command)

    def add_file(path, cached=False):
        if not path or path in state["seen"]:
            return
        state["seen"].add(path)
        state["files"].append(path)
        size = filesize(path)
        cached_now = cached or not state["progress_seen"]
        OUT.event("file", url=url, path=path, size=size, cached=cached_now)
        OUT.end_progress()
        OUT.human(("= cached " if cached_now else "✔ ") + path)

    def on_line(line: str, is_err: bool):
        if is_err:
            state["tail"].append(line)
            if line.startswith("ERROR"):
                state["errors"].append(line.strip()[:400])
            if o.verbose:
                if OUT.json:
                    OUT.event("log", url=url, line=line)
                else:
                    OUT.human("! " + line, err=True)
            return

        if line.startswith(PROGRESS_PREFIX + " "):
            state["progress_seen"] = True
            p = parse_progress(line[len(PROGRESS_PREFIX) + 1:])
            total_b = p["total"] or p["estimate"]
            pct = None
            if total_b and p["downloaded"] is not None:
                pct = round(min(100.0, p["downloaded"] * 100.0 / total_b), 1)
            state["speed"] = p["speed"]
            OUT.event("progress", url=url, percent=pct, **p)
            if state["live"] and pct is not None:
                spd = f" @ {human_size(p['speed'])}/s" if p["speed"] else ""
                eta = f" ETA {human_time(p['eta'])}" if p["eta"] else ""
                OUT.progress(f"[{idx}/{total}] {pct:.0f}%{spd}{eta} — {state['label']}")
            return

        if line.startswith(POST_PREFIX + " "):
            parts = parse_sep(line[len(POST_PREFIX) + 1:])
            processor = parts[0] if parts else None
            status = parts[1] if len(parts) > 1 else None
            OUT.event("post", url=url, processor=processor, status=status)
            return

        if line.startswith(META_PREFIX + SEP):
            vals = parse_sep(line[len(META_PREFIX) + 1:])
            vals += [None] * (5 - len(vals))
            meta = dict(zip(("id", "title", "extractor", "webpage_url", "duration"), vals))
            state["meta"] = meta
            if meta.get("title"):
                state["label"] = meta["title"][:60]
            OUT.event("meta", url=url, index=idx, **meta)
            OUT.human(f"[{idx}/{total}] ▶ {state['label']} · {meta.get('extractor') or '?'}")
            return

        if line.startswith(FILE_PREFIX + SEP):
            vals = parse_sep(line[len(FILE_PREFIX) + 1:])
            add_file(vals[0] if vals else None)
            return

        state["tail"].append(line)
        if "already been downloaded" in line:
            state["skipped"] = True
        if o.verbose:
            if OUT.json:
                OUT.event("log", url=url, line=line)
            else:
                OUT.human("· " + line)

    rc = run_streaming(args, on_line)
    state["rc"] = rc
    state["triggered"] = any(FALLBACK_TRIGGER_RE.search(l) for l in state["errors"])

    if not state["files"] and rc == 0 and not state["errors"] and state["meta"]:
        for path in existing_files(o.dir, state["meta"].get("id")):
            add_file(path, cached=True)
    return state


def run_one(o, url: str, idx: int, total: int, allow_live: bool) -> dict:
    started = time.time()
    ladder = build_fallback_ladder(o, url)
    state = None
    attempts_used = 0
    for attempt_no, (strategy, extra, pause) in enumerate(ladder, 1):
        if attempt_no > 1:
            if pause:
                time.sleep(pause)
            reason = ""
            if state:
                pool = state["errors"] or list(state["tail"])
                reason = (pool[-1][:200] if pool else "")
            OUT.event("fallback", url=url, attempt=attempt_no,
                      max_attempts=len(ladder), strategy=strategy, reason=reason)
            OUT.human(f"↻ {state['label'] if state else url} — retrying with "
                      f"{strategy} ({attempt_no}/{len(ladder)})", err=True)
        state = _attempt(o, url, idx, total, allow_live, extra, strategy, attempt_no)
        attempts_used = attempt_no
        if state["files"] or not state["triggered"]:
            break

    rc = state["rc"]
    files = state["files"]
    cached = bool(files) and not state["progress_seen"]
    if not files:
        ok, partial = False, False
    else:
        ok = rc == 0 and not state["errors"]
        partial = not ok

    elapsed = round(time.time() - started, 2)
    result = {
        "url": url,
        "idx": idx,
        "ok": ok,
        "partial": partial,
        "cached": cached,
        "files": files,
        "errors": state["errors"][:3],
        "attempts": attempts_used,
        "elapsed": elapsed,
    }
    if not ok and not files:
        first = state["errors"][0] if state["errors"] else \
            (state["tail"][-1] if state["tail"] else f"yt-dlp exited with code {rc}")
        OUT.event("error", url=url, message=first, exit=rc, attempts=attempts_used)
        OUT.human(f"✘ {url}: {first}", err=True)
    OUT.end_progress()
    OUT.event("done", url=url, ok=ok, partial=partial, cached=cached,
              files=files, errors=state["errors"][:3], exit=rc,
              attempts=attempts_used, elapsed=elapsed)
    return result


def summarize(results) -> dict:
    files = []
    failed_urls = []
    partial_urls = []
    cached = 0
    for r in sorted(results, key=lambda r: r["idx"]):
        files.extend(r["files"])
        if r.get("cached"):
            cached += 1
        if not r["ok"] and not r["partial"]:
            failed_urls.append(r["url"])
        elif r["partial"]:
            partial_urls.append(r["url"])
    ok = sum(1 for r in results if r["ok"])
    return {
        "total": len(results),
        "ok": ok,
        "partial": len(partial_urls),
        "failed": len(failed_urls),
        "cached": cached,
        "files": files,
        "failed_urls": failed_urls,
        "partial_urls": partial_urls,
    }


def cmd_download(o, ns) -> int:
    missing = require_ytdlp()
    if missing is not None:
        return missing
    urls = collect_urls(o, ns)
    if urls is None:
        return 2
    if not urls:
        return fail("no URLs provided (pass URLs as arguments, --urls-from FILE, or pipe them)", 2, usage=True)
    o.urls = urls
    n = len(urls)
    if o.dry_run:
        for i, url in enumerate(urls, 1):
            args = build_download_argv(o, url, mode="audio" if o.command == "audio" else "video")
            OUT.event("start", url=url, index=i, total=n, cmd=shlex.join(args), dir=str(o.dir), dry_run=True)
            OUT.human(f"[{i}/{n}] {shlex.join(args)}")
            OUT.event("done", url=url, ok=True, partial=False, dry_run=True, files=[], errors=[], exit=None, elapsed=0)
        OUT.event("summary", **summarize([{"url": u, "idx": i, "ok": True, "partial": False,
                                           "files": [], "cached": False}
                                          for i, u in enumerate(urls, 1)]),
                  dir=str(o.dir), elapsed=0, dry_run=True)
        return 0

    jobs = max(1, min(o.jobs, n))
    allow_live = (n == 1 and not OUT.json)
    results = []
    OUT.event("batch", total=n, jobs=jobs, dir=str(o.dir))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        future_url = {pool.submit(run_one, o, u, i, n, allow_live): (i, u)
                      for i, u in enumerate(urls, 1)}
        for fut in as_completed(future_url):
            i, u = future_url[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # defensive: one task must not sink the batch
                results.append({"url": u, "idx": i, "ok": False,
                                "partial": False, "files": [], "errors": [str(exc)],
                                "cached": False, "elapsed": 0})
    res = summarize(results)
    OUT.event("summary", **res, dir=str(o.dir), elapsed=round(time.time() - T0, 2))
    if res["failed"] or res["partial"]:
        OUT.human(f"Done in {human_time(time.time() - T0)} — {len(res['files'])} files, "
                  f"{res['failed']} failed, {res['partial']} partial", err=True)
        for u in res["failed_urls"]:
            OUT.human(f"  ✘ {u}", err=True)
        return 1
    OUT.human(f"Done in {human_time(time.time() - T0)} — {len(res['files'])} files")
    OUT.human(f"Saved to {o.dir}")
    return 0


# ---------------------------------------------------------------------------
# info / formats
# ---------------------------------------------------------------------------

def trim_format(f: dict) -> dict:
    keys = ("format_id", "ext", "resolution", "height", "width", "fps",
            "vcodec", "acodec", "abr", "vbr", "tbr", "filesize",
            "filesize_approx", "format_note", "language", "protocol",
            "dynamic_range")
    return {k: f.get(k) for k in keys if f.get(k) is not None}


def trim_entry(e: dict) -> dict:
    keys = ("id", "title", "url", "webpage_url", "duration", "view_count", "uploader")
    return {k: e.get(k) for k in keys if e.get(k) is not None}


def trim_info(d: dict) -> dict:
    keys = ("id", "title", "uploader", "uploader_url", "channel", "channel_url",
            "upload_date", "timestamp", "duration", "view_count", "like_count",
            "comment_count", "webpage_url", "original_url", "extractor",
            "extractor_key", "thumbnail", "age_limit", "is_live", "was_live",
            "availability", "license", "tags", "categories", "chapters")
    out = {k: d.get(k) for k in keys if d.get(k) is not None}
    desc = d.get("description")
    if desc:
        out["description"] = desc if len(desc) <= 1000 else desc[:1000] + "…"
    if d.get("subtitles"):
        out["subtitles"] = sorted(d["subtitles"].keys())
    if d.get("automatic_captions"):
        out["auto_captions"] = sorted(d["automatic_captions"].keys())
    if d.get("formats"):
        out["formats"] = [trim_format(f) for f in d["formats"]]
        out["format_count"] = len(d["formats"])
    if d.get("entries") is not None:
        out["entries"] = [trim_entry(e) for e in d["entries"] if e]
        out["entry_count"] = len(out["entries"])
    return out


def run_capture(args: list):
    proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


def cmd_info(o, ns) -> int:
    missing = require_ytdlp()
    if missing is not None:
        return missing
    urls = collect_urls(o, ns)
    if urls is None:
        return 2
    if not urls:
        return fail("no URLs provided", 2, usage=True)
    rc = 0
    for url in urls:
        args = list(YTDLP) + ["--ignore-config", "--no-warnings", "-J"]
        if o.flat:
            args.append("--flat-playlist")
        if o.cookies_from_browser:
            args += ["--cookies-from-browser", o.cookies_from_browser]
        elif o.cookies:
            args += ["--cookies", o.cookies]
        if o.proxy:
            args += ["--proxy", o.proxy]
        args.append(url)
        code, stdout, stderr = run_capture(args)
        if code != 0 or not stdout.strip():
            rc = 1
            msg = (stderr.strip().splitlines() or ["metadata extraction failed"])[-1]
            OUT.event("error", url=url, message=msg)
            OUT.human(f"✘ {url}: {msg}", err=True)
            continue
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            rc = 1
            OUT.event("error", url=url, message="could not parse yt-dlp JSON output")
            continue
        if o.raw:
            print(json.dumps(data, ensure_ascii=False))
            continue
        trimmed = trim_info(data)
        OUT.event("info", url=url, data=trimmed)
        if not OUT.json:
            OUT.human(f"Title:    {trimmed.get('title')}")
            OUT.human(f"Uploader: {trimmed.get('uploader') or trimmed.get('channel') or '?'}")
            if trimmed.get("duration"):
                OUT.human(f"Duration: {human_time(trimmed['duration'])}")
            if isinstance(trimmed.get("view_count"), (int, float)):
                OUT.human(f"Views:    {trimmed['view_count']:,}")
            if trimmed.get("entries") is not None:
                OUT.human(f"Entries:  {trimmed['entry_count']} (playlist)")
            elif trimmed.get("format_count"):
                OUT.human(f"Formats:  {trimmed['format_count']}")
            OUT.human(f"URL:      {trimmed.get('webpage_url') or url}")
    return rc


def cmd_formats(o, ns) -> int:
    missing = require_ytdlp()
    if missing is not None:
        return missing
    urls = collect_urls(o, ns)
    if urls is None:
        return 2
    if not urls:
        return fail("no URLs provided", 2, usage=True)
    rc = 0
    for url in urls:
        args = list(YTDLP) + ["--ignore-config", "--no-warnings", "-J", url]
        code, stdout, stderr = run_capture(args)
        if code != 0 or not stdout.strip():
            rc = 1
            msg = (stderr.strip().splitlines() or ["format listing failed"])[-1]
            OUT.event("error", url=url, message=msg)
            OUT.human(f"✘ {url}: {msg}", err=True)
            continue
        try:
            formats = [trim_format(f) for f in (json.loads(stdout).get("formats") or [])]
        except json.JSONDecodeError:
            rc = 1
            OUT.event("error", url=url, message="could not parse yt-dlp JSON output")
            continue
        OUT.event("formats", url=url, formats=formats)
        if not OUT.json:
            OUT.human(f"{'id':<12} {'ext':<5} {'resolution':<11} {'fps':<4} "
                      f"{'vcodec':<9} {'acodec':<9} {'tbr':<8} size")
            for f in formats:
                size = f.get("filesize") or f.get("filesize_approx")
                OUT.human(f"{str(f.get('format_id')):<12} {str(f.get('ext')):<5} "
                          f"{str(f.get('resolution') or '-'):<11} "
                          f"{str(f.get('fps') or '-'):<4} "
                          f"{str(f.get('vcodec') or '-'):<9} "
                          f"{str(f.get('acodec') or '-'):<9} "
                          f"{str(f.get('tbr') or '-'):<8} "
                          f"{human_size(size) if size else '-'}")
    return rc


# ---------------------------------------------------------------------------
# doctor / setup / update
# ---------------------------------------------------------------------------

def doctor_report() -> dict:
    rep = {
        "grDownload": __version__,
        "python": platform.python_version(),
        "yt_dlp": {"found": bool(YTDLP), "path": YTDLP[0] if YTDLP else None,
                   "version": None},
        "ffmpeg": {"found": bool(FFMPEG), "path": FFMPEG, "version": None},
        "aria2": {"found": bool(ARIA2), "path": ARIA2, "version": None},
        "dir": None,
        "dir_writable": False,
        "hints": [],
    }
    if YTDLP:
        try:
            code, out, _ = run_capture(list(YTDLP) + ["--ignore-config", "--version"])
            if code == 0:
                rep["yt_dlp"]["version"] = out.strip().splitlines()[0]
        except Exception:
            pass
    if FFMPEG:
        try:
            code, out, _ = run_capture([FFMPEG, "-version"])
            if code == 0:
                first = out.splitlines()[0]
                m = re.search(r"ffmpeg version (\S+)", first)
                rep["ffmpeg"]["version"] = m.group(1) if m else first
        except Exception:
            pass
    if ARIA2:
        try:
            code, out, _ = run_capture([ARIA2, "--version"])
            if code == 0:
                m = re.search(r"aria2 version (\S+)", out)
                rep["aria2"]["version"] = m.group(1) if m else out.splitlines()[0]
        except Exception:
            pass
    dirp = Path(os.environ.get("GRDL_DIR") or (Path.home() / "Downloads" / "grDownload"))
    rep["dir"] = str(dirp.expanduser())
    try:
        dirp.expanduser().mkdir(parents=True, exist_ok=True)
        probe = dirp / ".grdl-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        rep["dir_writable"] = True
    except OSError:
        rep["dir_writable"] = False
    if not rep["yt_dlp"]["found"]:
        rep["hints"].append("install yt-dlp:  grdl setup")
    if not rep["ffmpeg"]["found"]:
        rep["hints"].append("install ffmpeg (needed to merge video+audio and convert music):  grdl setup")
    if not rep["aria2"]["found"]:
        rep["hints"].append("optional: install aria2 for faster multi-connection downloads:  grdl setup")
    if not rep["dir_writable"]:
        rep["hints"].append(f"download dir is not writable: {rep['dir']}")
    imp = impersonate_targets()
    rep["impersonate"] = {"available": bool(imp),
                          "targets": [f"{c} ({os_name})" for c, os_name, _s in imp][:4]}
    rep["pot_provider"] = pot_provider_present()
    if not imp:
        rep["hints"].append("optional: TLS impersonation fallback for stubborn sites — "
                            "run: grdl setup --impersonate")
    rep["ok"] = bool(rep["yt_dlp"]["found"] and rep["ffmpeg"]["found"] and rep["dir_writable"])
    return rep


def pot_provider_present() -> bool:
    for base in (Path.home() / ".config" / "yt-dlp" / "plugins",
                 Path.home() / ".local" / "share" / "yt-dlp" / "plugins",
                 Path("/etc/yt-dlp/plugins")):
        try:
            if base.is_dir() and any("pot" in c.name.lower() for c in base.iterdir()):
                return True
        except OSError:
            continue
    return False


def cmd_doctor(o, ns) -> int:
    rep = doctor_report()
    OUT.event("doctor", **rep)
    if not OUT.json:
        mark = lambda ok: "✔" if ok else "✘"
        OUT.human(f"{mark(rep['yt_dlp']['found'])} yt-dlp   "
                  f"{rep['yt_dlp']['version'] or 'not found'}"
                  f"   {rep['yt_dlp']['path'] or ''}".rstrip())
        OUT.human(f"{mark(rep['ffmpeg']['found'])} ffmpeg   "
                  f"{rep['ffmpeg']['version'] or 'not found'}")
        OUT.human(f"{mark(rep['aria2']['found'] or True)} aria2    "
                  f"{rep['aria2']['version'] or 'not found (optional)'}")
        imp = rep["impersonate"]
        imp_label = imp["targets"][0] if imp["targets"] else "available"
        OUT.human(f"{'✔' if imp['available'] else '·'} fallback  impersonate: "
                  f"{imp_label if imp['available'] else 'unavailable (grdl setup --impersonate)'}"
                  f" · pot-provider: {'yes' if rep['pot_provider'] else 'no'}")
        OUT.human(f"{mark(rep['dir_writable'])} dir      {rep['dir']}")
        for hint in rep["hints"]:
            OUT.human("  → " + hint)
        OUT.human("All good." if rep["ok"] else "Problems found.", err=not rep["ok"])
    if getattr(ns, "strict", False) and not rep["ok"]:
        return 1
    return 0


def installer_candidates(tool: str):
    py = sys.executable
    cands = []
    if tool == "yt-dlp":
        if which("pipx"):
            cands.append((["pipx", "install", "yt-dlp"], False))
        if which("uv"):
            cands.append((["uv", "tool", "install", "yt-dlp"], False))
        cands.append(([py, "-m", "pip", "install", "--user", "--upgrade", "yt-dlp"], False))
    pkg = {"yt-dlp": "yt-dlp", "ffmpeg": "ffmpeg", "aria2": "aria2"}[tool]
    if which("pacman"):
        cands.append((["pacman", "-S", "--noconfirm", pkg], True))
    if which("apt-get"):
        cands.append((["apt-get", "install", "-y", pkg], True))
    if which("dnf"):
        cands.append((["dnf", "install", "-y", pkg], True))
    if which("brew"):
        cands.append((["brew", "install", pkg], False))
    return cands


def sudo_available() -> bool:
    try:
        proc = subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=10)
        return proc.returncode == 0
    except Exception:
        return False


def run_installer(argv, use_sudo: bool):
    if use_sudo:
        if not which("sudo") or not sudo_available():
            return False, "needs a sudo password (run it manually)"
        argv = ["sudo", "-n", *argv]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=900)
        if proc.returncode == 0:
            return True, ""
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, tail[-1][:200] if tail else f"exit {proc.returncode}"
    except Exception as exc:
        return False, str(exc)[:200]


def cmd_setup(o, ns) -> int:
    global YTDLP, FFMPEG, ARIA2
    dry = bool(getattr(ns, "dry_run", False))
    rep = doctor_report()
    needs = []
    if not rep["yt_dlp"]["found"]:
        needs.append("yt-dlp")
    if not rep["ffmpeg"]["found"]:
        needs.append("ffmpeg")
    if not rep["aria2"]["found"]:
        needs.append("aria2")
    want_imp = bool(getattr(ns, "impersonate", False))
    imp_missing = want_imp and not rep["impersonate"]["available"]
    if not needs and not imp_missing:
        OUT.event("setup", status="already-installed", ok=True)
        OUT.human("Everything is already installed. Nothing to do.")
        return 0

    for tool in needs:
        cands = installer_candidates(tool)
        plan = [["sudo", *argv] if use_sudo else argv for argv, use_sudo in cands]
        OUT.event("setup", tool=tool, action="plan", candidates=plan, dry_run=dry)
        if dry:
            OUT.human(f"{tool}: would try {len(plan)} installer(s)")
            for cmd in plan:
                OUT.human("  " + shlex.join(cmd))
            continue
        installed = False
        for argv, use_sudo in cands:
            ok, why = run_installer(argv, use_sudo)
            OUT.event("setup", tool=tool, action="try", cmd=shlex.join(argv),
                      ok=ok, message=why or None)
            if ok:
                OUT.human(f"✔ installed {tool} via {shlex.join(argv)}")
                installed = True
                break
            OUT.human(f"  · {shlex.join(argv)} failed: {why}", err=True)
        if not installed:
            OUT.event("setup", tool=tool, action="failed", ok=False)
            OUT.human(f"✘ could not install {tool}; install it manually", err=True)

    imp_cands = []
    if imp_missing:
        uv = which("uv")
        if uv:
            imp_cands.append(([uv, "pip", "install", "--python", sys.executable,
                               "--break-system-packages", "curl_cffi"], True))
        imp_cands.append(([sys.executable, "-m", "pip", "install",
                           "--break-system-packages", "curl_cffi"], True))

    if dry:
        if imp_missing:
            OUT.human("curl_cffi: would try:")
            for argv, _use_sudo in imp_cands:
                OUT.human("  " + shlex.join(argv))
        OUT.event("setup", status="plan", ok=None)
        return 0

    if imp_missing:
        installed_imp = False
        for argv, use_sudo in imp_cands:
            ok, why = run_installer(argv, use_sudo)
            OUT.event("setup", tool="curl_cffi", action="try",
                      cmd=shlex.join(argv), ok=ok, message=why or None)
            if ok:
                OUT.human("✔ installed curl_cffi (TLS impersonation fallback)")
                installed_imp = True
                break
            OUT.human(f"  · {shlex.join(argv)} failed: {why}", err=True)
        if not installed_imp:
            OUT.human("✘ could not install curl_cffi; impersonation fallback stays off",
                      err=True)
        impersonate_targets(refresh=True)

    # Re-scan PATH for newly installed binaries.
    YTDLP = resolve_ytdlp()
    FFMPEG = shutil.which("ffmpeg")
    ARIA2 = shutil.which("aria2c")
    rep = doctor_report()
    OUT.event("setup", status="done", ok=rep["ok"], report=rep)
    if rep["ok"]:
        OUT.human("Setup complete.")
        return 0
    OUT.human("Some dependencies are still missing. See `grdl doctor`.", err=True)
    return 1


def cmd_update(o, ns) -> int:
    missing = require_ytdlp()
    if missing is not None:
        return missing
    attempts = [list(YTDLP) + ["--ignore-config", "-U"]]
    if which("pipx"):
        attempts.append(["pipx", "upgrade", "yt-dlp"])
    if which("uv"):
        attempts.append(["uv", "tool", "upgrade", "yt-dlp"])
    attempts.append([sys.executable, "-m", "pip", "install", "--user", "--upgrade", "yt-dlp"])
    for argv in attempts:
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=600)
        except Exception as exc:
            OUT.event("update", cmd=shlex.join(argv), ok=False, message=str(exc)[:200])
            continue
        out = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        OUT.event("update", cmd=shlex.join(argv), ok=ok,
                  message=out.strip().splitlines()[-1][:300] if out.strip() else None)
        if not OUT.json:
            for line in out.strip().splitlines()[-4:]:
                OUT.human("  " + line)
        if ok:
            break
    code, out, _ = run_capture(list(YTDLP) + ["--ignore-config", "--version"])
    version = out.strip().splitlines()[0] if code == 0 else None
    OUT.event("update", done=True, version=version)
    OUT.human(f"yt-dlp {version or '(unknown version)'}")
    return 0


# ---------------------------------------------------------------------------
# option resolution / CLI
# ---------------------------------------------------------------------------

QUALITY_ALIASES = {"4k": "2160", "8k": "4320", "hd": "1080", "sd": "480", "fullhd": "1080"}


def resolve_options(ns, command: str):
    o = argparse.Namespace()
    o.command = command

    forced = None
    if getattr(ns, "json", False):
        forced = True
    if getattr(ns, "human", False):
        forced = False
    if forced is None:
        forced = env_bool("GRDL_JSON")
    if forced is None:
        forced = not sys.stdout.isatty()
    o.json = bool(forced)

    o.quiet = bool(getattr(ns, "quiet", False) or env_bool("GRDL_QUIET"))
    o.verbose = bool(getattr(ns, "verbose", False))

    dir_raw = getattr(ns, "dir", None) or os.environ.get("GRDL_DIR")
    o.dir = Path(dir_raw).expanduser() if dir_raw else (Path.home() / "Downloads" / "grDownload")

    o.jobs = max(1, getattr(ns, "jobs", None) or env_int("GRDL_JOBS", 3))
    explicit_fragments = getattr(ns, "fragments", None)
    o.fragments = max(1, explicit_fragments or env_int("GRDL_FRAGMENTS", 16))
    o.no_aria2 = bool(getattr(ns, "no_aria2", False) or env_bool("GRDL_NO_ARIA2"))
    o.turbo = bool(getattr(ns, "turbo", False))
    if o.turbo and not explicit_fragments and not os.environ.get("GRDL_FRAGMENTS"):
        o.fragments = 32
    o.limit_rate = getattr(ns, "limit_rate", None)
    o.cookies = getattr(ns, "cookies", None) or os.environ.get("GRDL_COOKIES")
    o.cookies_from_browser = (getattr(ns, "cookies_from_browser", None)
                              or os.environ.get("GRDL_COOKIES_FROM_BROWSER"))
    o.proxy = getattr(ns, "proxy", None) or os.environ.get("GRDL_PROXY")
    o.no_fallback = bool(getattr(ns, "no_fallback", False) or env_bool("GRDL_NO_FALLBACK"))
    o.impersonate = getattr(ns, "impersonate", None) or os.environ.get("GRDL_IMPERSONATE")
    o.extractor_args = list(getattr(ns, "extractor_args", None) or [])

    o.output = (getattr(ns, "output", None) or os.environ.get("GRDL_OUTPUT")
                or DEFAULT_TEMPLATE)
    quality = str(getattr(ns, "quality", None) or os.environ.get("GRDL_QUALITY") or "best").lower()
    quality = QUALITY_ALIASES.get(quality, quality)
    if quality != "best" and quality != "worst" and not quality.isdigit():
        raise SystemExit(f"invalid --quality: {quality}")
    o.quality = quality
    o.compat = bool(getattr(ns, "compat", False))
    o.format_override = getattr(ns, "format", None)
    o.single = bool(getattr(ns, "single", False))
    o.force = bool(getattr(ns, "force", False))
    subs = getattr(ns, "subs", None)
    o.subs = subs if subs else None
    o.dry_run = bool(getattr(ns, "dry_run", False))
    o.flat = bool(getattr(ns, "flat", False))
    o.raw = bool(getattr(ns, "raw", False))
    o.audio_format = (getattr(ns, "audio_format", None)
                      or os.environ.get("GRDL_AUDIO_FORMAT") or "mp3")
    o.audio_quality = str(getattr(ns, "audio_quality", None) or "0")
    o.no_cover = bool(getattr(ns, "no_cover", False))
    return o


def collect_urls(o, ns):
    if getattr(o, "urls", None):
        return o.urls
    urls = list(getattr(ns, "urls", None) or [])
    src = getattr(ns, "urls_from", None)
    stdin_used = False
    if src:
        try:
            if src == "-":
                text = sys.stdin.read()
                stdin_used = True
            else:
                text = Path(src).expanduser().read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            fail(f"cannot read --urls-from {src}: {exc}", 2, usage=True)
            return None
        urls += parse_url_list(text)
    if "-" in urls:  # `grdl dl -` reads URLs from stdin
        urls = [u for u in urls if u != "-"]
        try:
            urls += parse_url_list(sys.stdin.read())
        except Exception:
            pass
        stdin_used = True
    if not urls and not stdin_used:
        urls += parse_url_list(read_stdin_maybe())
    seen, deduped = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def add_common(p: argparse.ArgumentParser, with_impersonate: bool = True) -> None:
    S = argparse.SUPPRESS
    p.add_argument("--json", action="store_true", default=S,
                   help="force NDJSON output on stdout")
    p.add_argument("--human", action="store_true", default=S,
                   help="force human-readable output")
    p.add_argument("-d", "--dir", default=S, metavar="PATH",
                   help="download directory (default: ~/Downloads/grDownload)")
    p.add_argument("-j", "--jobs", type=int, default=S, metavar="N",
                   help="parallel URLs (default 3)")
    p.add_argument("-N", "--fragments", type=int, default=S, metavar="N",
                   help="concurrent fragments per download (default 16)")
    p.add_argument("--turbo", action="store_true", default=S,
                   help="max speed preset: 32 fragments + throttling escape")
    p.add_argument("--no-aria2", action="store_true", default=S,
                   help="use yt-dlp's built-in downloader instead of aria2c")
    p.add_argument("--limit-rate", default=S, metavar="RATE",
                   help="optional rate limit, e.g. 5M (default: unlimited)")
    p.add_argument("--cookies", default=S, metavar="FILE",
                   help="Netscape cookies file (for age/member-restricted media)")
    p.add_argument("--cookies-from-browser", default=S, metavar="BROWSER",
                   help="read cookies from a browser (chrome, firefox, brave, edge, ...)")
    p.add_argument("--proxy", default=S, metavar="URL",
                   help="proxy to route downloads through")
    p.add_argument("-q", "--quiet", action="store_true", default=S,
                   help="suppress human chatter (errors still shown)")
    p.add_argument("-v", "--verbose", action="store_true", default=S,
                   help="stream raw yt-dlp output")
    p.add_argument("--no-fallback", action="store_true", default=S,
                   help="disable the automatic no-cookie retry ladder (bot checks)")
    if with_impersonate:
        p.add_argument("--impersonate", default=S, metavar="TARGET",
                       help="TLS impersonation target, e.g. chrome (needs curl_cffi)")
    p.add_argument("--extractor-args", action="append", default=S, metavar="ARGS",
                   help="raw yt-dlp extractor args, e.g. 'youtube:player_client=tv' (repeatable)")


def add_urls(p: argparse.ArgumentParser) -> None:
    S = argparse.SUPPRESS
    p.add_argument("urls", nargs="*", help="media URLs")
    p.add_argument("--urls-from", default=S, metavar="FILE",
                   help="read URLs from a file ('-' for stdin), one per line")


def add_download_flags(p: argparse.ArgumentParser) -> None:
    S = argparse.SUPPRESS
    p.add_argument("--quality", default=S, metavar="Q",
                   help="best (default) | 2160 | 1440 | 1080 | 720 | 480 | 360 | worst")
    p.add_argument("-f", "--format", default=S, metavar="SELECTOR",
                   help="raw yt-dlp format selector (overrides --quality)")
    p.add_argument("--compat", action="store_true", default=S,
                   help="force h264+aac mp4 for maximum player compatibility")
    p.add_argument("--single", action="store_true", default=S,
                   help="only the single video, never the whole playlist")
    p.add_argument("--subs", nargs="?", const="en.*", default=S, metavar="LANGS",
                   help="download + embed subtitles (default lang: en.*)")
    p.add_argument("--force", action="store_true", default=S,
                   help="re-download and overwrite existing files")
    p.add_argument("-o", "--output", default=S, metavar="TEMPLATE",
                   help="yt-dlp output template (default: '{title} [{id}].{ext}')")
    p.add_argument("--dry-run", action="store_true", default=S,
                   help="print the yt-dlp commands without downloading")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grdl",
        description="grDownload — agent-first video & music downloader (fast yt-dlp wrapper)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  grdl https://youtu.be/VIDEO              download best-quality video\n"
            "  grdl audio https://youtu.be/VIDEO        extract music as mp3\n"
            "  grdl dl --quality 1080 URL1 URL2         two videos in parallel\n"
            "  cat urls.txt | grdl dl -                 batch from stdin\n"
            "  grdl info URL | jq .data.title           metadata as JSON\n"
            "  grdl dl --dry-run URL                    show the exact yt-dlp command\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"grdl {__version__}")
    add_common(p)
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("download", aliases=["dl", "get"],
                       help="download video at best quality")
    add_common(d)
    add_urls(d)
    add_download_flags(d)

    a = sub.add_parser("audio", aliases=["music", "mp3"],
                       help="download audio and convert to music files")
    add_common(a)
    add_urls(a)
    add_download_flags(a)
    a.add_argument("--audio-format", default=argparse.SUPPRESS, metavar="FMT",
                   help="mp3 (default) | m4a | opus | flac | wav | best")
    a.add_argument("--audio-quality", default=argparse.SUPPRESS, metavar="Q",
                   help="0 (best, default) .. 9 or bitrate like 320K")
    a.add_argument("--no-cover", action="store_true", default=argparse.SUPPRESS,
                   help="do not embed the thumbnail as cover art")

    i = sub.add_parser("info", aliases=["meta"],
                       help="print metadata as JSON (no download)")
    add_common(i)
    add_urls(i)
    i.add_argument("--flat", action="store_true", default=argparse.SUPPRESS,
                   help="playlists: list entries without extracting each one")
    i.add_argument("--raw", action="store_true", default=argparse.SUPPRESS,
                   help="print yt-dlp's full JSON, untrimmed")

    f = sub.add_parser("formats", help="list available formats")
    add_common(f)
    add_urls(f)

    doc = sub.add_parser("doctor", help="check dependencies and configuration")
    add_common(doc)
    doc.add_argument("--strict", action="store_true", default=argparse.SUPPRESS,
                     help="exit 1 when something is missing")

    s = sub.add_parser("setup", help="install missing dependencies")
    add_common(s, with_impersonate=False)
    s.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                   help="show the install commands without running them")
    s.add_argument("--impersonate", action="store_true", default=argparse.SUPPRESS,
                   help="also install curl_cffi (enables the TLS impersonation fallback)")

    u = sub.add_parser("update", help="update yt-dlp to the latest version")
    add_common(u)

    return p


COMMAND_MAP = {
    "download": "download", "dl": "download", "get": "download",
    "audio": "audio", "music": "audio", "mp3": "audio",
    "info": "info", "meta": "info",
    "formats": "formats",
    "doctor": "doctor",
    "setup": "setup",
    "update": "update",
}


def _sigint(sig, frame):
    kill_children()
    raise KeyboardInterrupt


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = normalize_argv(argv)
    parser = build_parser()
    if not argv:
        parser.print_help(sys.stderr)
        return 2

    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    command = COMMAND_MAP.get(getattr(ns, "command", None) or "")
    if not command:
        parser.print_help(sys.stderr)
        return 2

    try:
        o = resolve_options(ns, command)
    except SystemExit as exc:
        return fail(str(exc), 2, usage=True)

    OUT.json = o.json
    OUT.quiet = o.quiet

    previously = None
    try:
        previously = signal.signal(signal.SIGINT, _sigint)
    except ValueError:
        pass  # not on the main thread (tests)

    dispatch = {
        "download": cmd_download,
        "audio": cmd_download,
        "info": cmd_info,
        "formats": cmd_formats,
        "doctor": cmd_doctor,
        "setup": cmd_setup,
        "update": cmd_update,
    }
    try:
        try:
            return dispatch[command](o, ns)
        except KeyboardInterrupt:
            kill_children()
            OUT.event("interrupted")
            OUT.human("interrupted", err=True)
            return 130
        except BrokenPipeError:
            try:
                sys.stdout.close()
            except Exception:
                pass
            return 0
    finally:
        if previously is not None:
            try:
                signal.signal(signal.SIGINT, previously)
            except ValueError:
                pass


if __name__ == "__main__":
    sys.exit(main())
