"""Resolve a URL or local path: metadata, one best caption track, media.

URLs go through yt-dlp (YouTube, Vimeo, TikTok, X, Instagram, Loom and the
1000+ sites it supports). Captions are fetched without the media when
possible, so transcript-only runs never download video.
"""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import parse_qs, urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import hashlib

from common import CACHE_DIR, have, log, probe, run, short

YOUTUBE = re.compile(
    r"^https?://(?:(?:www\.|m\.|music\.)?youtube\.com/(?:watch\?\S*\bv=|shorts/|live/|embed/)|youtu\.be/)\S+$", re.I)
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus"}
MEDIA_CACHE = CACHE_DIR / "media"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".wmv", ".flv", ".3gp", ".3gpp", ".ts"}


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s or "", re.I))


def is_youtube(s: str) -> bool:
    return bool(YOUTUBE.match(s or ""))


def is_tiktok(s: str) -> bool:
    return bool(re.search(r"(^|\.)tiktok\.com/", s or "", re.I))


def check_url(url: str) -> None:
    """Refuse local/private targets (SSRF guard, from u2n4/video-url-analyzer-mcp, MIT)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise SystemExit(f"Blocked URL scheme {u.scheme!r}: only http/https links are watched")
    host = (u.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise SystemExit(f"Blocked URL host {host!r}: local addresses are not fetched")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            raise SystemExit(f"Blocked internal IP {host}")
    except ValueError:
        pass


def normalize_url(url: str) -> str:
    """Canonical YouTube watch URL (drops playlist/share/tracking params) so the cache hits."""
    if not is_youtube(url):
        return url.strip()
    u = urlparse(url)
    vid = None
    if "youtu.be" in (u.hostname or ""):
        vid = u.path.lstrip("/").split("/")[0]
    elif "/shorts/" in u.path or "/live/" in u.path or "/embed/" in u.path:
        vid = u.path.rstrip("/").split("/")[-1]
    else:
        vid = (parse_qs(u.query).get("v") or [None])[0]
    return f"https://www.youtube.com/watch?v={vid}" if vid else url.strip()


def start_hint(url: str):
    """`?t=90` / `&t=1m30s` in a shared link: the moment the sender pointed at."""
    q = parse_qs(urlparse(url).query).get("t") or parse_qs(urlparse(url).query).get("start")
    if not q:
        return None
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", q[0])
    if not m or not any(m.groups()):
        return None
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


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
    media_dir: Optional[Path] = None          # where URL media/captions live (shared cache)
    chapters: list = field(default_factory=list)


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
    order = [orig, "en", "en-US", "en-GB", "eng-US", "hi"]  # eng-US: TikTok's ASR track
    for lang in order:
        if lang and lang in manual:
            return lang, "manual"
    if manual:
        return sorted(manual)[0], "manual"
    for lang in ([f"{orig}-orig", orig] if orig else []) + ["en-orig", "en", "eng-US", "hi"]:
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
    existing = sorted(work.glob(f"captions.{lang}*.vtt")) or sorted(work.glob(f"captions*{lang}*.vtt"))
    if existing:
        return existing[0], lang, kind
    flag = "--write-subs" if kind == "manual" else "--write-auto-subs"
    try:
        _ytdlp(["--skip-download", flag, "--sub-langs", lang, "--sub-format", "vtt/srt/best",
                "--convert-subs", "vtt", "-o", str(work / "captions.%(ext)s"), *cookie_args, url], timeout=180)
    except RuntimeError as e:
        log(f"caption download failed ({short(str(e), 200)}); will use speech fallback")
        return None, None, None
    files = sorted(work.glob(f"captions.{lang}*.vtt")) or sorted(work.glob("captions*.vtt"))
    return (files[0], lang, kind) if files else (None, None, None)


def download_media(url: str, work: Path, cookie_args: list, max_height: int = 720) -> Path:
    cached = [p for p in work.glob("video.*") if p.suffix.lower() in VIDEO_EXT and p.stat().st_size > 0]
    if cached:
        log("reusing cached download")
        return max(cached, key=lambda p: p.stat().st_size)
    fmt_sel = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b"
    extra = []
    if is_tiktok(url):
        # TikTok sometimes serves ByteDance bvc2 streams ffmpeg can't decode (u2n4/video-url-analyzer-mcp, MIT)
        fmt_sel = ("b[ext=mp4][vcodec^=h264]/b[ext=mp4][vcodec^=avc1]/b[ext=mp4][vcodec^=h265]/"
                   "b[ext=mp4][vcodec^=hvc1]/b[ext=mp4][vcodec^=hevc]/b[ext=mp4][vcodec!*=bvc][vcodec!*=bytevc2]/b")
        extra = ["-S", "+codec:avc:m4a,res,ext:mp4:m4a"]
    _ytdlp(["-f", fmt_sel, *extra, "--merge-output-format", "mp4", "--restrict-filenames",
            "-o", str(work / "video.%(ext)s"), *cookie_args, url], timeout=3600)
    files = [p for p in work.glob("video.*") if p.suffix.lower() in VIDEO_EXT]
    if not files:
        raise RuntimeError("yt-dlp finished but produced no video file")
    return max(files, key=lambda p: p.stat().st_size)


def resolve(src: str, work: Path, *, need_media: bool, sub_lang: str = "auto",
            cookies: Optional[str] = None, browser: Optional[str] = None,
            want_captions: bool = True, max_height: int = 720, use_cache: bool = True) -> Source:
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

    check_url(src)
    hint = start_hint(src)
    src = normalize_url(src)
    ck = _cookie_args(cookies, browser)
    mdir = (MEDIA_CACHE / hashlib.sha1(src.encode()).hexdigest()[:16]) if use_cache else work
    mdir.mkdir(parents=True, exist_ok=True)
    info_file = mdir / "info.json"
    full_info = mdir / "info_full.json"
    if use_cache and full_info.exists():
        info = json.loads(full_info.read_text(encoding="utf-8"))
        log("using cached metadata")
    else:
        info = url_info(src, ck)
        full_info.write_text(json.dumps({k: info.get(k) for k in (
            "id", "title", "uploader", "channel", "duration", "upload_date", "view_count", "description",
            "chapters", "language", "webpage_url", "subtitles", "automatic_captions", "width", "height", "fps")},
            ensure_ascii=False), encoding="utf-8")
    s = Source(source=src, is_local=False, title=info.get("title") or "",
               uploader=info.get("uploader") or info.get("channel") or "",
               duration=float(info.get("duration") or 0), media_dir=mdir)
    if hint is not None:
        s.notes.append(f"the shared link points at {hint // 60:02d}:{hint % 60:02d} (t= parameter) - "
                       "likely the moment the user cares about")
    s.chapters = [{"t": float(c.get("start_time") or 0), "title": c.get("title") or ""}
                  for c in (info.get("chapters") or [])]
    s.available_captions = {"manual": sorted((info.get("subtitles") or {}).keys()),
                            "auto_count": len(info.get("automatic_captions") or {})}
    info_file.write_text(json.dumps(
        {k: info.get(k) for k in ("id", "title", "uploader", "channel", "duration", "upload_date",
                                  "view_count", "description", "chapters", "language", "webpage_url")},
        indent=2, ensure_ascii=False), encoding="utf-8")
    if want_captions:
        s.captions, s.caption_lang, s.caption_kind = fetch_captions(src, mdir, info, sub_lang, ck)
    if need_media:
        try:
            s.path = download_media(src, mdir, ck, max_height)
            s.meta = probe(s.path)
            s.duration = s.duration or s.meta["duration"]
        except RuntimeError as e:
            if s.captions:
                s.notes.append(f"Media download failed, captions kept: {short(str(e), 300)}")
            else:
                raise SystemExit(str(e))
    return s
