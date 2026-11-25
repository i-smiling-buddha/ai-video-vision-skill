"""End-to-end tests on generated clips (no network, no API keys).

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "ai-video-vision" / "scripts"
VV = SCRIPTS / "vv.py"
sys.path.insert(0, str(SCRIPTS))

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def vv(*args, env=None) -> subprocess.CompletedProcess:
    e = {**os.environ, "VV_CONFIG_DIR": TMP["cfg"], "VV_CACHE_DIR": TMP["cache"], **(env or {})}
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        if not (env or {}).get(k):
            e.pop(k, None)
    return subprocess.run([sys.executable, str(VV), *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8", env=e, timeout=600)


TMP: dict = {}


def setUpModule():
    root = Path(tempfile.mkdtemp(prefix="vvtest_"))
    TMP.update(root=root, cfg=str(root / "cfg"), cache=str(root / "cache"))
    if not HAVE_FFMPEG:
        return
    # 35 s: test pattern 0-10, red 10-20, mandelbrot 20-30, black 30-35; tone 0-15 then silence
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=10",
        "-f", "lavfi", "-i", "color=c=red:size=640x360:rate=25:duration=10",
        "-f", "lavfi", "-i", "mandelbrot=size=640x360:rate=25",
        "-f", "lavfi", "-i", "color=c=black:size=640x360:rate=25:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=15",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-filter_complex",
        "[2:v]trim=duration=10,setpts=PTS-STARTPTS[c];[0:v][1:v][c][3:v]concat=n=4:v=1:a=0[v];"
        "[5:a]atrim=duration=20[s];[4:a][s]concat=n=2:v=0:a=1,aresample=44100[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(root / "clip.mp4")], check=True)
    (root / "clip.vtt").write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello and welcome\n\n"
        "00:00:03.000 --> 00:00:06.000\nHello and welcome to the demo\n\n"
        "00:00:12.000 --> 00:00:15.000\nAs you can see the red screen here\n", encoding="utf-8")


def tearDownModule():
    shutil.rmtree(TMP["root"], ignore_errors=True)
    for p in Path(tempfile.gettempdir()).glob("vv_*"):
        shutil.rmtree(p, ignore_errors=True)


class Pure(unittest.TestCase):
    def test_time(self):
        from common import fmt, parse_time
        self.assertEqual(parse_time("1:02:03"), 3723)
        self.assertEqual(parse_time("4:05"), 245)
        self.assertEqual(parse_time("12.5"), 12.5)
        self.assertEqual(fmt(3723), "1:02:03")
        self.assertEqual(fmt(65.4, precise=True), "01:05.4")
        with self.assertRaises(ValueError):
            parse_time("abc")

    def test_rolling_captions(self):
        from captions import dedupe_rolling
        out = dedupe_rolling([{"start": 0, "end": 2, "text": "hello"}, {"start": 1, "end": 3, "text": "hello world"},
                              {"start": 3, "end": 5, "text": "next"}])
        self.assertEqual([s["text"] for s in out], ["hello world", "next"])

    def test_caption_choice(self):
        from source import _choose_caption
        self.assertEqual(_choose_caption({"language": "hi", "subtitles": {"en": [1], "hi": [1]}}, "auto"), ("hi", "manual"))
        self.assertEqual(_choose_caption({"language": "hi", "automatic_captions": {"hi-orig": [1], "en": [1]}}, "auto"),
                         ("hi-orig", "auto"))
        self.assertEqual(_choose_caption({"automatic_captions": {"en": [1]}}, "fr"), (None, None))

    def test_visual_cues(self):
        from captions import visual_cues
        cues = visual_cues([{"start": 5, "end": 6, "text": "as you can see here"},
                            {"start": 9, "end": 10, "text": "यहाँ देखो"}, {"start": 12, "end": 13, "text": "thanks"}])
        self.assertEqual([c["t"] for c in cues], [5, 9])

    def test_url_helpers(self):
        from source import check_url, normalize_url, start_hint
        self.assertEqual(normalize_url("https://youtu.be/abc?si=x&t=90"), "https://www.youtube.com/watch?v=abc")
        self.assertEqual(normalize_url("https://www.youtube.com/shorts/QQ?feature=share"),
                         "https://www.youtube.com/watch?v=QQ")
        self.assertEqual(start_hint("https://www.youtube.com/watch?v=a&t=1m30s"), 90)
        for bad in ("http://127.0.0.1/x.mp4", "http://localhost/x", "file:///etc/passwd", "http://10.0.0.5/v"):
            with self.assertRaises(SystemExit):
                check_url(bad)
        check_url("https://www.tiktok.com/@a/video/1")

    def test_hallucination_filter(self):
        from captions import drop_hallucinations
        kept, dropped = drop_hallucinations([{"start": 0, "end": 1, "text": "Thanks for watching!"},
                                             {"start": 1, "end": 2, "text": "ooooooooooo"},
                                             {"start": 2, "end": 3, "text": "Now deploy the app"}])
        self.assertEqual((len(kept), dropped), (1, 2))

    def test_bucket_and_pacing(self):
        from frames import bucket_pick
        import pacing
        picks = bucket_pick([0.0, 0.5, 1.0, 1.5, 2.0, 50.0], 0, 60, 6)
        self.assertEqual(len(picks), 6)
        self.assertTrue(any(k == "fill" for _, k in picks))       # empty middle buckets get filled
        self.assertEqual(picks[5][0], 50.0)                       # the late cut is kept
        pc = pacing.compute([10, 20, 30], 0, 35)
        self.assertEqual((pc["shot_count"], pc["median_shot"]), (4, 10.0))

    def test_youtube_regex(self):
        from source import is_youtube
        self.assertTrue(is_youtube("https://youtu.be/abc"))
        self.assertTrue(is_youtube("https://www.youtube.com/shorts/abc"))
        self.assertFalse(is_youtube("https://vimeo.com/1"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class Media(unittest.TestCase):
    @property
    def clip(self):
        return TMP["root"] / "clip.mp4"

    def test_uniform_timestamps_are_real(self):
        import frames as F
        fr = F.uniform(str(self.clip), TMP["root"] / "u", duration=35, max_frames=7, width=320)
        self.assertEqual(len(fr), 7)
        self.assertEqual(fr[0]["t"], 0.0)
        self.assertTrue(all(b["t"] > a["t"] for a, b in zip(fr, fr[1:])))

    def test_analyze_finds_structure(self):
        r = vv("analyze", self.clip, "--filters", "scenes,black,silence")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"Scene cuts: 3: 00:10\.0, 00:20\.0, 00:30\.0")
        self.assertIn("Black stretches: 1", r.stdout)
        self.assertRegex(r.stdout, r"Silences: 1: 00:1[45]")

    def test_watch_sidecar_sheets_and_search(self):
        r = vv("watch", TMP["root"] / "clip.mp4", "--mode", "accurate", "--sheets")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sidecar subtitle file clip.vtt", r.stdout)
        self.assertIn("[00:01] Hello and welcome to the demo", r.stdout)
        self.assertIn("## Overview sheets", r.stdout)
        self.assertIn("Scene cuts: 3", r.stdout)
        work = re.search(r"Work dir: `([^`]+)`", r.stdout).group(1)
        for f in re.findall(r"`([^`]+\.jpg)`", r.stdout):
            self.assertTrue(Path(f).exists(), f)
        s = vv("search", work, "red screen")
        self.assertIn("▶ [00:12] As you can see the red screen here", s.stdout)

    def test_silence_is_not_transcribed(self):
        r = vv("watch", self.clip, "--mode", "fast", "--start", "20", "--end", "30", "--transcriber", "whisper-cpp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("audio is silent", r.stdout)
        self.assertIn("do NOT invent", r.stdout)

    def test_detail_and_cache(self):
        r = vv("detail", self.clip, "--start", "9", "--end", "11")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.count("★"), 4)  # 3 starred lines + the explanation line

    def test_clip_vertical(self):
        out = TMP["root"] / "v.mp4"
        r = vv("clip", self.clip, "--start", "8", "--end", "12", "--vertical", "-o", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        dims = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                               str(out)], capture_output=True, text=True).stdout.split()
        self.assertIn("1080,1920", dims)

    def test_auto_mode_reports_density_and_pacing(self):
        r = vv("watch", self.clip, "--mode", "auto", "--hook")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Auto mode:", r.stdout)
        self.assertIn("Motion map", r.stdout)
        self.assertRegex(r.stdout, r"Pacing: 4 shots")
        self.assertIn("## Hook microscope", r.stdout)

    def test_scene_sampler_even_coverage(self):
        import frames as F
        fr, label = F.scene(str(self.clip), TMP["root"] / "sc", duration=35, max_frames=8, width=320)
        ts = [f["t"] for f in fr]
        self.assertEqual(len(fr), 8)
        for cut in (10.0, 20.0, 30.0):
            self.assertIn(cut, ts)
        self.assertGreater(max(ts), 30.0)

    def test_zoom(self):
        r = vv("zoom", self.clip, "--at", "0:12", "--region", "top-left")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(Path(re.search(r"`([^`]+\.png)`", r.stdout).group(1)).exists())

    def test_gemini_without_key_is_clear(self):
        r = vv("watch", self.clip, "--engine", "gemini")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("GEMINI_API_KEY is not set", r.stderr + r.stdout)

    def test_hybrid_without_key_still_answers_locally(self):
        r = vv("watch", self.clip, "--engine", "hybrid", "--mode", "fast")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("## Gemini (hybrid) unavailable", r.stdout)
        self.assertIn("## Frames", r.stdout)


if __name__ == "__main__":
    unittest.main()
