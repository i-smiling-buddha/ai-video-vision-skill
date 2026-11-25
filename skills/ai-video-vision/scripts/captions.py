"""Parse WebVTT/SRT into segments, collapse YouTube rolling captions, format
compact `[MM:SS] text` transcripts and spot "look at this" visual cues."""
from __future__ import annotations

import html
import re
import unicodedata
from pathlib import Path
from typing import Optional

from common import fmt

_TS = re.compile(r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})")
_TAG = re.compile(r"<[^>]+>")


def _sec(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def parse_subtitles(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    cues: list[dict] = []
    cur: Optional[dict] = None
    skip_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if cur and cur["lines"]:
                cues.append(cur)
            cur, skip_block = None, False
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")) and cur is None:
            skip_block = True
            continue
        if skip_block or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        m = _TS.search(line)
        if m:
            if cur and cur["lines"]:
                cues.append(cur)
            cur = {"start": _sec(m.group(1)), "end": _sec(m.group(2)), "lines": []}
        elif cur is not None and not line.isdigit():
            cur["lines"].append(line)
    if cur and cur["lines"]:
        cues.append(cur)

    out = []
    for c in cues:
        t = html.unescape(_TAG.sub("", " ".join(c["lines"]))).strip()
        t = re.sub(r"\s+", " ", t)
        if t:
            out.append({"start": c["start"], "end": c["end"], "text": t})
    return dedupe_rolling(out)


def dedupe_rolling(segs: list[dict]) -> list[dict]:
    """YouTube auto-captions repeat each phrase as it scrolls; keep the longest."""
    out: list[dict] = []
    for s in segs:
        if out:
            p = out[-1]
            if s["text"] == p["text"] or p["text"].startswith(s["text"]):
                p["end"] = max(p["end"], s["end"])
                continue
            if s["text"].startswith(p["text"]):
                out[-1] = {"start": p["start"], "end": max(p["end"], s["end"]), "text": s["text"]}
                continue
            # rolling 2-line cue: drop the repeated leading line
            if s["text"].startswith(p["text"].split(" ")[0]) and p["text"] in s["text"]:
                s = {**s, "text": s["text"].replace(p["text"], "", 1).strip()}
                if not s["text"]:
                    p["end"] = max(p["end"], s["end"])
                    continue
        out.append(dict(s))
    return out


def clip(segs: list[dict], start: Optional[float], end: Optional[float]) -> list[dict]:
    if start is not None:
        segs = [s for s in segs if s["end"] >= start]
    if end is not None:
        segs = [s for s in segs if s["start"] <= end]
    return segs


def offset(segs: list[dict], seconds: float) -> list[dict]:
    return [{**s, "start": s["start"] + seconds, "end": s["end"] + seconds} for s in segs]


def format_transcript(segs: list[dict]) -> str:
    return "\n".join(f"[{fmt(s['start'])}] {s['text']}" for s in segs)


# Phrases where a speaker points at the screen. English + Hindi/Hinglish.
_CUE = re.compile(
    r"\b(look at|looking at|as you can see|you can see|notice|see here|right here|over here|"
    r"on (the )?screen|this (slide|chart|graph|diagram|code|line|button|part)|let me show|"
    r"i'?ll show|watch this|check this out|here we have|demo|dekho|yaha|yahan|is tarah)\b"
    r"|देखो|देखिए|यहाँ|यहां|स्क्रीन",
    re.I,
)


def visual_cues(segs: list[dict], limit: int = 12) -> list[dict]:
    hits = []
    for s in segs:
        m = _CUE.search(s["text"])
        if m:
            hits.append({"t": s["start"], "phrase": m.group(0), "text": s["text"][:90]})
    if len(hits) > limit:  # keep an even spread
        step = len(hits) / limit
        hits = [hits[int(i * step)] for i in range(limit)]
    return hits


# Whisper's known inventions over music and silence (from subh05sus/video-understanding-skill, MIT):
# one repeated character, a looping syllable, or stock "credits" lines.
_STOCK = ("thanks for watching", "thank you for watching", "subtitles by the amara", "untertitel im auftrag",
          "untertitelung des zdf", "please subscribe", "like and subscribe", "सब्सक्राइब")


def hallucination(text: str) -> bool:
    letters = [c for c in text.lower() if c.isalpha()]
    if len(set(letters)) <= 2:
        return True
    if re.fullmatch(r"(.{1,6}?)\1{4,}", re.sub(r"\s+", "", text)):
        return True
    t = unicodedata.normalize("NFKC", text.lower()).strip(" .!?")
    return len(t) < 60 and any(x in t for x in _STOCK)


def drop_hallucinations(segs: list[dict]) -> tuple[list[dict], int]:
    kept = [s for s in segs if not hallucination(s["text"])]
    return kept, len(segs) - len(kept)


def said_between(segs: list[dict], a: float, b: float, limit: int = 140) -> str:
    """What is said while a frame is on screen (frame timeline, idea from gAmUssA/watch-skill)."""
    words = " ".join(s["text"] for s in segs if a - 1.5 <= s["start"] < b).split()
    text = " ".join(words)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text
