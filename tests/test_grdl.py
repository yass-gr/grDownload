"""Unit tests for grDownload (grdl). Run: python -m unittest discover -s tests -v"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_grdl():
    spec = importlib.util.spec_from_file_location("grdl", ROOT / "grdl.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["grdl"] = mod
    spec.loader.exec_module(mod)
    return mod


g = load_grdl()


def mk_opts(**over):
    base = dict(
        command="download",
        fragments=16,
        dir=Path(tempfile.gettempdir()),
        output=g.DEFAULT_TEMPLATE,
        verbose=False,
        no_aria2=True,
        turbo=False,
        limit_rate=None,
        cookies=None,
        cookies_from_browser=None,
        proxy=None,
        single=False,
        force=False,
        subs=None,
        compat=False,
        quality="best",
        format_override=None,
        audio_format="mp3",
        audio_quality="0",
        no_cover=False,
        json=False,
        quiet=False,
        dry_run=False,
        urls=None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


class TestNormalizeArgv(unittest.TestCase):
    def test_plain_url_routes_to_download(self):
        self.assertEqual(g.normalize_argv(["https://x.com/a"]),
                         ["download", "https://x.com/a"])

    def test_leading_flag_then_url(self):
        self.assertEqual(g.normalize_argv(["--json", "https://x.com/a"]),
                         ["--json", "download", "https://x.com/a"])

    def test_value_flag_before_url(self):
        self.assertEqual(g.normalize_argv(["--dir", "/tmp/x", "https://x.com/a"]),
                         ["--dir", "/tmp/x", "download", "https://x.com/a"])

    def test_known_command_untouched(self):
        argv = ["dl", "--quality", "720", "https://x.com/a"]
        self.assertEqual(g.normalize_argv(argv), argv)

    def test_empty(self):
        self.assertEqual(g.normalize_argv([]), [])


class TestParsers(unittest.TestCase):
    def test_parse_progress(self):
        p = g.parse_progress("223779 223779 NA 23956.5 NA 2 8")
        self.assertEqual(p["downloaded"], 223779)
        self.assertEqual(p["total"], 223779)
        self.assertIsNone(p["estimate"])
        self.assertAlmostEqual(p["speed"], 23956.5)
        self.assertIsNone(p["eta"])
        self.assertEqual(p["fragment_index"], 2)
        self.assertEqual(p["fragment_count"], 8)

    def test_parse_progress_short(self):
        p = g.parse_progress("5")
        self.assertEqual(p["downloaded"], 5)
        self.assertIsNone(p["total"])

    def test_parse_sep(self):
        raw = '"abc"' + g.SEP + '"Tít le"' + g.SEP + "NA"
        self.assertEqual(g.parse_sep(raw), ["abc", "Tít le", None])

    def test_parse_sep_plain_fallback(self):
        self.assertEqual(g.parse_sep("not json"), ["not json"])


class TestFormatters(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(g.human_size(999), "999 B")
        self.assertEqual(g.human_size(1536), "1.5 KiB")
        self.assertEqual(g.human_size(3 * 1024 ** 2), "3.0 MiB")

    def test_human_time(self):
        self.assertEqual(g.human_time(59), "59s")
        self.assertEqual(g.human_time(125), "2m05s")
        self.assertEqual(g.human_time(3700), "1h01m")

    def test_parse_url_list(self):
        text = "# comment\n\nhttps://a.com/1\nhttps://a.com/1\nhttps://b.com/2 extra note\n"
        self.assertEqual(g.parse_url_list(text),
                         ["https://a.com/1", "https://b.com/2"])


class TestArgBuilders(unittest.TestCase):
    def test_format_spec_best_and_cap(self):
        self.assertEqual(g.format_spec(mk_opts(quality="best")), "bv*+ba/b")
        self.assertIn("height<=1080", g.format_spec(mk_opts(quality="1080")))
        self.assertEqual(g.format_spec(mk_opts(quality="worst")), "wv*+wa/w")

    def test_format_spec_compat(self):
        spec = g.format_spec(mk_opts(quality="720", compat=True))
        self.assertIn("vcodec^=avc", spec)
        self.assertIn("acodec^=mp4a", spec)
        self.assertIn("height<=720", spec)

    def test_common_args_are_agent_safe(self):
        args = g.yt_common_args(mk_opts())
        self.assertIn("--ignore-config", args)
        self.assertIn("--no-simulate", args)
        self.assertIn("-q", args)
        self.assertTrue(any(a.startswith("download:" + g.PROGRESS_PREFIX) for a in args))
        self.assertTrue(any(g.FILE_PREFIX in a and a.startswith("after_move:") for a in args))
        self.assertTrue(any(a.startswith("home:") for a in args))
        self.assertIn(g.DEFAULT_TEMPLATE, args)
        self.assertNotIn("--downloader", args)  # no_aria2=True

    def test_common_args_aria2_when_available(self):
        if not g.ARIA2:
            self.skipTest("aria2c not installed")
        args = g.yt_common_args(mk_opts(no_aria2=False))
        self.assertIn("--downloader", args)
        self.assertIn("aria2c", args)

    def test_build_video_argv(self):
        args = g.build_download_argv(mk_opts(), "https://x.com/v", mode="video")
        self.assertIn("-i", args)
        self.assertIn("--merge-output-format", args)
        self.assertEqual(args[-1], "https://x.com/v")

    def test_build_audio_argv(self):
        args = g.build_download_argv(mk_opts(command="audio", audio_format="mp3"),
                                     "https://x.com/v", mode="audio")
        self.assertIn("-x", args)
        self.assertIn("--audio-format", args)
        self.assertIn("--embed-thumbnail", args)

    def test_format_override_wins(self):
        args = g.build_download_argv(mk_opts(format_override="bestvideo"), "https://x.com/v")
        self.assertIn("bestvideo", args)
        self.assertNotIn("-S", args)


class TestDataHelpers(unittest.TestCase):
    def test_trim_info(self):
        sample = {
            "id": "abc",
            "title": "T",
            "description": "x" * 2000,
            "webpage_url": "https://x.com/v",
            "formats": [{"format_id": "137", "ext": "mp4", "height": 1080,
                         "url": "SECRET", "filesize": 10}],
            "subtitles": {"en": [{"ext": "vtt"}]},
            "entries": None,
        }
        out = g.trim_info(sample)
        self.assertEqual(out["id"], "abc")
        self.assertLessEqual(len(out["description"]), 1001)
        self.assertEqual(out["format_count"], 1)
        self.assertNotIn("url", out["formats"][0])  # format URLs stripped
        self.assertEqual(out["subtitles"], ["en"])

    def test_existing_files(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "Song [vid1].mp3").write_text("x")
            (d / "Song [vid1].f140.m4a").write_text("x")
            (d / "Song [vid1].mp3.part").write_text("x")
            hits = g.existing_files(d, "vid1")
            self.assertEqual(len(hits), 2)
            self.assertTrue(all("part" not in h for h in hits))

    def test_summarize(self):
        results = [
            {"url": "u1", "idx": 1, "ok": True, "partial": False, "files": ["a"], "cached": False},
            {"url": "u2", "idx": 2, "ok": False, "partial": False, "files": [], "cached": False},
            {"url": "u3", "idx": 3, "ok": False, "partial": True, "files": ["b"], "cached": False},
        ]
        s = g.summarize(results)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["ok"], 1)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["partial"], 1)
        self.assertEqual(s["files"], ["a", "b"])
        self.assertEqual(s["failed_urls"], ["u2"])
        self.assertEqual(s["partial_urls"], ["u3"])


class TestCliSurface(unittest.TestCase):
    def test_help_version_usage(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            self.assertEqual(g.main(["--help"]), 0)
            self.assertEqual(g.main(["--version"]), 0)
            self.assertEqual(g.main([]), 2)

    def test_json_flag_forces_json_mode(self):
        ns = types.SimpleNamespace(json=True)
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("GRDL_JSON", "GRDL_QUIET"):
                os.environ.pop(key, None)
            o = g.resolve_options(ns, "download")
        self.assertTrue(o.json)

    def test_human_flag_forces_human_mode(self):
        ns = types.SimpleNamespace(human=True, json=False)
        o = g.resolve_options(ns, "download")
        self.assertFalse(o.json)

    def test_non_tty_defaults_to_json(self):
        ns = types.SimpleNamespace()
        fake = io.StringIO()  # isatty() -> False
        with mock.patch("sys.stdout", fake):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GRDL_JSON", None)
                o = g.resolve_options(ns, "download")
        self.assertTrue(o.json)


if __name__ == "__main__":
    unittest.main()
