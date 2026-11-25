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
    # select keeps the FIRST real frame of each interval, so showinfo reports its true time
    # (the fps filter keeps the last frame of a bucket and shifts timestamps by up to 1/(2*fps)).
    interval = 1.0 / fps
    vf = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval - 0.001:.4f})',"
          f"{scale_filter(width)},showinfo")
    res = run(["ffmpeg", "-hide_banner", "-nostats", "-y", *pre, "-i", video, *post, "-vf", vf,
               *vfr_args(), "-q:v", "3", str(out_dir / f"{prefix}_%05d.jpg")])
    if res.returncode != 0:
        raise SystemExit(f"ffmpeg frame extraction failed: {short(res.stderr)}")
    times = [float(m.group(2)) for m in _PTS.finditer(res.stderr)]
    frames = _collect(out_dir, prefix, times, s, "uniform")
    return _even_pick(frames, max_frames) if max_frames else frames


def detect_cuts(video, *, start=None, end=None, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Pass 1: every scene-change time (absolute seconds), no JPEGs written."""
    _need_ffmpeg()
    pre, post, s = _range_args(start, end)
    vf = f"scale=320:-2,select='gt(scene\\,{threshold})',showinfo"
    res = run(["ffmpeg", "-hide_banner", "-nostats", *pre, "-i", video, *post, "-vf", vf, "-an",
               *vfr_args(), "-f", "null", "-"], timeout=3600)
    return [round(s + float(m.group(2)), 3) for m in _PTS.finditer(res.stderr)]


def bucket_pick(cuts: list[float], lo: float, hi: float, n: int) -> list[tuple[float, str]]:
    """Even coverage in TIME (idea from gAmUssA/watch-skill, MIT): split the range
    into n equal buckets; take the cut nearest each bucket's centre, or the centre
    itself when the bucket has no cut. A fast-cut intro can no longer eat the whole
    budget and starve the rest of the video."""
    span = max(1e-6, hi - lo)
    width = span / n
    out = []
    for i in range(n):
        a, b = lo + i * width, lo + (i + 1) * width
        c = (a + b) / 2
        inside = [t for t in cuts if a <= t < b]
        out.append((min(inside, key=lambda t: abs(t - c)), "scene") if inside else (round(c, 3), "fill"))
    return out


def scene(video, out_dir: Path, *, start=None, end=None, duration: float, max_frames: Optional[int],
          width: int, threshold: float = SCENE_THRESHOLD, prefix: str = "s",
          cuts: Optional[list] = None) -> tuple[list[dict], str]:
    """Two-pass scene sampling: detect all cuts, then pick for even time coverage.
    Uncapped (max_frames=None) keeps every cut and fills long gaps."""
    lo = start or 0.0
    hi = end if end is not None else duration
    span = max(0.01, hi - lo)
    if cuts is None:
        cuts = detect_cuts(video, start=start, end=end, threshold=threshold)
    times = sorted(set([lo] + [t for t in cuts if lo <= t < hi]))
    if max_frames:
        picks = bucket_pick(times, lo, hi, max_frames) if len(times) > 1 or max_frames > 1 else [(lo, "scene")]
        # the opening frame is always a shot start
        picks[0] = (lo, "scene")
    else:
        n = min(300, max(8, int(span / 2)))
        gap = span / n
        picks = [(t, "scene") for t in times]
        fills = [c for c in (lo + (i + 0.5) * gap for i in range(n))
                 if all(abs(c - t) > gap / 2 for t in times)]
        picks = sorted(picks + [(round(c, 3), "fill") for c in fills])
    seen, uniq = set(), []
    for t, k in picks:
        key = round(t, 2)
        if key not in seen:
            seen.add(key)
            uniq.append((t, k))
    frames = at_times(video, out_dir, [t for t, _ in uniq], width, prefix=prefix)
    kinds = {round(t, 3): k for t, k in uniq}
    for f in frames:
        f["kind"] = kinds.get(round(f["t"], 3), "scene")
    ncut = sum(1 for f in frames if f["kind"] == "scene")
    label = f"scene ({len(cuts)} cuts found, {ncut} used" + (f", {len(frames) - ncut} gap fills)" if
                                                             len(frames) > ncut else ")")
    return frames, label


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
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        i, t = item
        p = out_dir / f"{prefix}_{i:03d}_{int(t * 1000):09d}.jpg"
        res = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", video,
                   "-frames:v", "1", "-vf", scale_filter(width), "-q:v", "2", str(p)])
        if res.returncode == 0 and p.exists():
            return {"t": round(t, 3), "path": str(p), "kind": "cue"}
        log(f"could not extract frame at {fmt(t)}")
        return None
    with ThreadPoolExecutor(max_workers=4) as pool:  # parallel seeks: 4x faster on long lists
        frames = [f for f in pool.map(one, enumerate(times)) if f]
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


# ------------------------------------------------------------ contact sheets
def _label_thumb(src: str, dst: Path, label: str, tile_w: int, tile_h: int) -> bool:
    fit = (f"scale={tile_w}:{tile_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
           f"pad={tile_w}:{tile_h}:(ow-iw)/2:(oh-ih)/2:color=black")
    txt = label.replace(":", "\\:")
    draw = (f",drawtext=text='{txt}':x=6:y=6:fontsize={max(14, tile_w // 18)}:fontcolor=white:"
            "box=1:boxcolor=black@0.65:boxborderw=4")
    for vf in (fit + draw, fit):  # drawtext needs a font; fall back to an unlabeled tile
        res = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vf", vf,
                   "-q:v", "3", str(dst)])
        if res.returncode == 0 and dst.exists():
            return vf is not fit
    return False


def contact_sheets(frames: list[dict], out_dir: Path, cols: int = 3, rows: int = 3,
                   tile_w: int = 480) -> list[dict]:
    """Tile frames into labeled grids: one image shows cols*rows moments for
    roughly the token cost of a single frame. Returns [{path, from, to, count}]."""
    if not frames:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    tile_h = int(tile_w * 9 / 16) // 2 * 2
    per = cols * rows
    sheets = []
    for si in range(0, len(frames), per):
        group = frames[si:si + per]
        tmp = out_dir / f"_tiles_{si // per:03d}"
        tmp.mkdir(exist_ok=True)
        for i, f in enumerate(group):
            _label_thumb(f["path"], tmp / f"t_{i:03d}.jpg", fmt(f["t"], precise=True), tile_w, tile_h)
        n = len(list(tmp.glob("t_*.jpg")))
        if n == 0:
            continue
        r = min(rows, -(-n // cols))
        out = out_dir / f"sheet_{si // per + 1:02d}.jpg"
        res = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1",
                   "-i", str(tmp / "t_%03d.jpg"), "-vf", f"tile={cols}x{r}:padding=4:margin=4:color=0x202020",
                   "-frames:v", "1", "-q:v", "3", str(out)])
        for p in tmp.glob("*"):
            p.unlink(missing_ok=True)
        tmp.rmdir()
        if res.returncode == 0 and out.exists():
            sheets.append({"path": str(out), "from": group[0]["t"], "to": group[-1]["t"], "count": n})
        else:
            log(f"contact sheet failed: {short(res.stderr, 200)}")
    return sheets
