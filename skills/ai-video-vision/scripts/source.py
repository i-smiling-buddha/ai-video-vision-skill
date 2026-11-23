"""Resolve a URL or local path: metadata, one best caption track, media.

URLs go through yt-dlp (YouTube, Vimeo, TikTok, X, Instagram, Loom and the
1000+ sites it supports). Captions are fetched without the media when
possible, so transcript-only runs never download video.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from common import have, log, probe, run, short

YOUTUBE = re.compile(
    r"^https?://(?:(?:www\.|m\.|music\.)?youtube\.com/(?:watch\?\S*\bv=|shorts/|live/|embed/)|youtu\.be/)\S+$", re.I)
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".wmv", ".flv", ".3gp", ".3gpp", ".ts"}


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s or "", re.I))


def is_youtube(s: str) -> bool:
    return bool(YOUTUBE.match(s or ""))


@dataclass
class Source:
    source: str
    is_local: bool
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    path: Optional[Path] = None            # local media (None for caption-only URL runs)
    captions: Optional[Path] = None
    caption_lang: Optional[str] = None
    caption_kind: Optional[str] = None      # "manual" | "auto"
    available_captions: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)  # ffprobe info once media exists
    notes: list = field(default_factory=list)


def _cookie_args(cookies: Optional[str], browser: Optional[str]) -> list:
    if cookies and browser:
        raise SystemExit("Use either --cookies or --cookies-from-browser, not both")
    if cookies:
        return ["--cookies", str(Path(cookies).expanduser())]
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def _ytdlp(args: list, timeout: float = 900) -> str:
    if not have("yt-dlp"):
        raise SystemExit("yt-dlp not found. Run: python scripts/vv.py setup --install")
    res = run(["yt-dlp", "--no-playlist", "--no-warnings", *args], timeout=timeout)
    if res.returncode != 0:
        err = short(res.stderr, 700)
        hint = ""
        if "403" in err or "Sign in" in err or "confirm" in err.lower():
            hint = (" Hint: update yt-dlp first (pip install -U yt-dlp / brew upgrade yt-dlp) and retry once; "
                    "private or age-gated videos need --cookies-from-browser <browser>.")
        raise RuntimeError(f"yt-dlp failed: {err}{hint}")
    return res.stdout


def url_info(url: str, cookie_args: list) -> dict:
    return json.loads(_ytdlp(["-J", "--skip-download", *cookie_args, url], timeout=180) or "{}")


def _choose_caption(info: dict, want: str) -> tuple[Optional[str], Optional[str]]:
    """Return (lang, kind). want='auto' prefers original-language manual subs."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    orig = (info.get("language") or "").split("-")[0] or None
    if want and want != "auto":
        if want in manual:
            return want, "manual"
        if want in auto:
            return want, "auto"
        return None, None
    order = [orig, "en", "en-US", "en-GB", "hi"]
    for lang in order:
        if lang and lang in manual:
            return lang, "manual"
    if manual:
        return sorted(manual)[0], "manual"
    for lang in ([f"{orig}-orig", orig] if orig else []) + ["en-orig", "en", "hi"]:
        if lang and lang in auto:
            return lang, "auto"
    origs = [k for k in auto if k.endswith("-orig")]
    if origs:
        return origs[0], "auto"
    return None, None


def fetch_captions(url: str, work: Path, info: dict, want: str, cookie_args: list) -> tuple:
    lang, kind = _choose_caption(info, want)
    if not lang:
        return None, None, None
    flag = "--write-subs" if kind == "manual" else "--write-auto-subs"
    try:
        _ytdlp(["--skip-download", flag, "--sub-langs", lang, "--sub-format", "vtt/srt/best",
                "--convert-subs", "vtt", "-o", str(work / "captions.%(ext)s"), *cookie_args, url], timeout=180)
    except RuntimeError as e:
        log(f"caption download failed ({short(str(e), 200)}); will use speech fallback")
        return None, None, None
    files = sorted(work.glob("captions*.vtt"))
    return (files[0], lang, kind) if files else (None, None, None)


def download_media(url: str, work: Path, cookie_args: list, max_height: int = 720) -> Path:
    fmt_sel = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b"
    _ytdlp(["-f", fmt_sel, "--merge-output-format", "mp4", "--restrict-filenames",
            "-o", str(work / "video.%(ext)s"), *cookie_args, url], timeout=3600)
    files = [p for p in work.glob("video.*") if p.suffix.lower() in VIDEO_EXT]
    if not files:
        raise RuntimeError("yt-dlp finished but produced no video file")
    return max(files, key=lambda p: p.stat().st_size)


def resolve(src: str, work: Path, *, need_media: bool, sub_lang: str = "auto",
            cookies: Optional[str] = None, browser: Optional[str] = None,
            want_captions: bool = True, max_height: int = 720) -> Source:
    work.mkdir(parents=True, exist_ok=True)
    if not is_url(src):
        p = Path(src).expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"Video not found: {src}")
        s = Source(source=str(p), is_local=True, title=p.stem, path=p)
        s.meta = probe(p)
        s.duration = s.meta["duration"]
        # sidecar subtitles next to a local file (video.srt / video.en.vtt)
        for side in sorted(p.parent.glob(p.stem + "*")):
            if side.suffix.lower() in (".vtt", ".srt") and side != p:
                s.captions, s.caption_kind, s.caption_lang = side, "sidecar", None
                break
        return s

    ck = _cookie_args(cookies, browser)
    info = url_info(src, ck)
    s = Source(source=src, is_local=False, title=info.get("title") or "",
               uploader=info.get("uploader") or info.get("channel") or "",
               duration=float(info.get("duration") or 0))
    s.available_captions = {"manual": sorted((info.get("subtitles") or {}).keys()),
                            "auto_count": len(info.get("automatic_captions") or {})}
    (work / "info.json").write_text(json.dumps(
        {k: info.get(k) for k in ("id", "title", "uploader", "channel", "duration", "upload_date",
                                  "view_count", "description", "chapters", "language", "webpage_url")},
        indent=2, ensure_ascii=False), encoding="utf-8")
    if want_captions:
        s.captions, s.caption_lang, s.caption_kind = fetch_captions(src, work, info, sub_lang, ck)
    if need_media:
        try:
            s.path = download_media(src, work, ck, max_height)
            s.meta = probe(s.path)
            s.duration = s.duration or s.meta["duration"]
        except RuntimeError as e:
            if s.captions:
                s.notes.append(f"Media download failed, captions kept: {short(str(e), 300)}")
            else:
                raise SystemExit(str(e))
    return s
