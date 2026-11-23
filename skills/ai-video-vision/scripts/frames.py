"""Frame extraction: uniform, scene-aware, keyframe, pinned timestamps, plus
perceptual de-duplication. Every frame carries its real source timestamp."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from common import fmt, have, log, run, short, vfr_args

MAX_DIM = 1998          # keep images under the 2000px read limit
MAX_UNIFORM_FPS = 2.0
SCENE_THRESHOLD = 0.20  # ffmpeg scene score 0..1
DEDUP_THUMB = 16
DEDUP_THRESHOLD = 2.0   # mean abs RGB diff (0..255) of 16x16 thumbs


def _need_ffmpeg():
    if not have("ffmpeg"):
        raise SystemExit("ffmpeg not found. Run: python scripts/vv.py setup --install")


def scale_filter(width: int) -> str:
    return (f"scale='min({width},iw)':-2:flags=lanczos,"
            f"scale=w=-2:h='min({MAX_DIM},ih)':flags=lanczos")


def _range_args(start: Optional[float], end: Optional[float]) -> tuple[list, list, float]:
    pre, post = [], []
    s = start or 0.0
    if start:
        pre = ["-ss", f"{start:.3f}"]
    if end is not None:
        post = ["-t", f"{max(0.0, end - s):.3f}"]
    return pre, post, s


_PTS = re.compile(r"Parsed_showinfo\S*\s+@\s+\S+\]\s+n:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:\s*(-?[\d.]+)")


def _collect(out_dir: Path, prefix: str, times: list[float], offset: float, kind: str) -> list[dict]:
    files = sorted(out_dir.glob(f"{prefix}_*.jpg"))
    frames = []
    for i, p in enumerate(files):
        t = offset + (times[i] if i < len(times) else 0.0)
        frames.append({"t": round(t, 3), "path": str(p), "kind": kind})
    return frames


def _even_pick(frames: list[dict], n: int) -> list[dict]:
    """Keep n evenly spaced frames (first and last always kept); delete the rest."""
    if n is None or len(frames) <= n:
        return frames
    if n <= 1:
        keep = {0}
    else:
        keep = {round(i * (len(frames) - 1) / (n - 1)) for i in range(n)}
    out = []
    for i, f in enumerate(frames):
        if i in keep:
            out.append(f)
        else:
            Path(f["path"]).unlink(missing_ok=True)
    return out


# ------------------------------------------------------------------ samplers
def uniform(video, out_dir: Path, *, start=None, end=None, duration: float, max_frames: Optional[int],
            width: int, fps: Optional[float] = None, prefix: str = "u") -> list[dict]:
    _need_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    pre, post, s = _range_args(start, end)
    span = max(0.01, (end if end is not None else duration) - s)
    if fps is None:  # spread max_frames evenly, never above 2 fps
        fps = min(MAX_UNIFORM_FPS, (max_frames or span * MAX_UNIFORM_FPS) / span)
    fps = max(0.02, min(fps, 30.0))   # explicit --fps (detail drills) may go up to 30
    vf = f"fps={fps:.6f},{scale_filter(width)},showinfo"
    res = run(["ffmpeg", "-hide_banner", "-nostats", "-y", *pre, "-i", video, *post, "-vf", vf,
               "-q:v", "3", str(out_dir / f"{prefix}_%05d.jpg")])
    if res.returncode != 0:
        raise SystemExit(f"ffmpeg frame extraction failed: {short(res.stderr)}")
    times = [float(m.group(2)) for m in _PTS.finditer(res.stderr)]
    frames = _collect(out_dir, prefix, times, s, "uniform")
    return _even_pick(frames, max_frames) if max_frames else frames


def scene(video, out_dir: Path, *, start=None, end=None, duration: float, max_frames: Optional[int],
          width: int, threshold: float = SCENE_THRESHOLD, prefix: str = "s") -> tuple[list[dict], str]:
    """Scene-change candidates; static clips fall back to uniform."""
    _need_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    pre, post, s = _range_args(start, end)
    vf = f"select='eq(n\\,0)+gt(scene\\,{threshold})',{scale_filter(width)},showinfo"
    res = run(["ffmpeg", "-hide_banner", "-nostats", "-y", *pre, "-i", video, *post, "-vf", vf, *vfr_args(),
               "-q:v", "3", str(out_dir / f"{prefix}_%05d.jpg")])
    if res.returncode != 0:
        raise SystemExit(f"ffmpeg scene extraction failed: {short(res.stderr)}")
    times = [float(m.group(2)) for m in _PTS.finditer(res.stderr)]
    frames = _collect(out_dir, prefix, times, s, "scene")
    span = (end if end is not None else duration) - s
    target = max_frames or min(300, max(8, int(span / 2)))  # uncapped: aim for ~1 frame / 2 s
    if len(frames) < max(3, target // 2):
        # too few cuts to cover the clip (slides, talking head): top up with uniform frames
        log(f"only {len(frames)} scene cuts; topping up with uniform sampling")
        extra = uniform(video, out_dir, start=start, end=end, duration=duration,
                        max_frames=max(1, target - len(frames)), width=width, prefix=prefix + "u")
        if extra and frames:  # drop uniform frames that sit right on a scene frame
            gap = (span / max(1, len(extra))) * 0.5
            extra = [e for e in extra if all(abs(e["t"] - f["t"]) > gap for f in frames)
                     or Path(e["path"]).unlink(missing_ok=True)]
        frames = sorted(frames + extra, key=lambda f: f["t"])
        return (_even_pick(frames, max_frames) if max_frames else frames), "scene+uniform"
    return (_even_pick(frames, max_frames) if max_frames else frames), "scene"


def keyframes(video, out_dir: Path, *, start=None, end=None, duration: float, max_frames: Optional[int],
              width: int, prefix: str = "k") -> tuple[list[dict], str]:
    """Decode only I-frames: near-instant. Sparse keyframes -> uniform fallback."""
    _need_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    pre, post, s = _range_args(start, end)
    res = run(["ffmpeg", "-hide_banner", "-nostats", "-y", "-skip_frame", "nokey", *pre, "-i", video, *post,
               "-vf", f"{scale_filter(width)},showinfo", *vfr_args(), "-q:v", "3",
               str(out_dir / f"{prefix}_%05d.jpg")])
    times = [float(m.group(2)) for m in _PTS.finditer(res.stderr)]
    frames = _collect(out_dir, prefix, times, s, "keyframe") if res.returncode == 0 else []
    span = (end if end is not None else duration) - s
    if len(frames) < 4 or (span > 30 and len(frames) < span / 20):
        for f in frames:
            Path(f["path"]).unlink(missing_ok=True)
        log(f"keyframes too sparse ({len(frames)}); using uniform sampling")
        return uniform(video, out_dir, start=start, end=end, duration=duration, max_frames=max_frames,
                       width=width, prefix=prefix + "u"), "uniform (keyframe fallback)"
    return (_even_pick(frames, max_frames) if max_frames else frames), "keyframe"


def at_times(video, out_dir: Path, times: list[float], width: int, prefix: str = "cue") -> list[dict]:
    """One accurately-seeked frame per requested timestamp."""
    _need_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, t in enumerate(times):
        p = out_dir / f"{prefix}_{i:03d}_{int(t * 1000):09d}.jpg"
        res = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", video,
                   "-frames:v", "1", "-vf", scale_filter(width), "-q:v", "2", str(p)])
        if res.returncode == 0 and p.exists():
            frames.append({"t": round(t, 3), "path": str(p), "kind": "cue"})
        else:
            log(f"could not extract cue frame at {fmt(t)}")
    return frames


# --------------------------------------------------------------- dedupe
def _thumbs(paths: list[str]) -> list[bytes]:
    chunk = DEDUP_THUMB * DEDUP_THUMB * 3
    out = []
    for p in paths:  # per-file: frames may come from several samplers / names
        res = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", p, "-vf",
                   f"scale={DEDUP_THUMB}:{DEDUP_THUMB},format=rgb24", "-f", "rawvideo", "-"], text=False)
        if res.returncode != 0 or len(res.stdout) != chunk:
            return []
        out.append(res.stdout)
    return out


def dedupe(frames: list[dict], threshold: float = DEDUP_THRESHOLD) -> tuple[list[dict], int]:
    """Greedily drop frames nearly identical to the last kept one (cue frames always kept)."""
    if len(frames) < 2:
        return frames, 0
    thumbs = _thumbs([f["path"] for f in frames])
    if len(thumbs) != len(frames):
        return frames, 0
    kept, last, dropped = [frames[0]], thumbs[0], 0
    for f, th in zip(frames[1:], thumbs[1:]):
        diff = sum(abs(a - b) for a, b in zip(th, last)) / len(th)
        if diff <= threshold and f.get("kind") != "cue":
            Path(f["path"]).unlink(missing_ok=True)
            dropped += 1
        else:
            kept.append(f)
            last = th
    return kept, dropped


def view_sample(frames: list[dict], n: int) -> list[dict]:
    """n frames to look at first: every scene-cut / cue frame when they fit, the
    rest filled evenly across the clip (binary-search style viewing)."""
    if n <= 0 or len(frames) <= n:
        return frames
    key = [f for f in frames if f.get("kind") in ("scene", "cue", "keyframe")]
    picked = {id(f): f for f in (key if 0 < len(key) <= n else [])}
    need = n - len(picked)
    rest = [f for f in frames if id(f) not in picked]
    if need > 0 and rest:
        idx = [round(i * (len(rest) - 1) / max(1, need - 1)) for i in range(need)] if need > 1 else [len(rest) // 2]
        for i in idx:
            picked[id(rest[i])] = rest[i]
    if len(picked) < n:  # many key frames: plain even spread
        return [frames[round(i * (len(frames) - 1) / (n - 1))] for i in range(n)]
    return sorted(picked.values(), key=lambda f: f["t"])
