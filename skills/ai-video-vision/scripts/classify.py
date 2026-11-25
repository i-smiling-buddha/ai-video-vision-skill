"""Auto mode: decide how many frames a video deserves, before extracting any.

Adapted from gAmUssA/watch-skill `scripts/classify.py` (MIT). The insight
there: motion is not the signal, *visual information density* is. A slide or
screencast sits still but is full of edges (text, UI, diagrams); a talking head
moves constantly but carries little detail. So:

    info_density = edge_peak / (motion_median + EPS)

High -> frames carry information the transcript never states (full budget).
Low  -> the words carry the content (a thin set of confirmation frames).

Two cheap ffmpeg probes, no numpy/opencv. Also returns a per-second motion
series used for the report's motion map (idea from subh05sus/video-understanding-skill).
"""
from __future__ import annotations

import math
import statistics
from typing import Optional

from common import have, run

EPS = 0.05
DENSE_DENSITY_MIN = 1.10    # >= dense (screencast / slides / B-roll with text)
STATIC_DENSITY_MAX = 0.70   # <= talking head
DENSE_CUTS_PER_MIN = 12.0   # fast montage needs frames even with modest detail
PROBE_FRAMES = 90
SPARK = "▁▂▃▄▅▆▇█"


def _yavg_series(video, vf: str, start=None, end=None, limit: int = PROBE_FRAMES) -> list[float]:
    pre = ["-ss", f"{start:.3f}"] if start else []
    post = ["-t", f"{max(0.0, end - (start or 0)):.3f}"] if end is not None else []
    res = run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", *pre, "-i", video, *post,
               "-vf", vf, "-frames:v", str(limit), "-an", "-f", "null", "-"], timeout=900)
    vals = []
    for line in res.stderr.splitlines():
        if "signalstats.YAVG=" in line:
            try:
                vals.append(float(line.split("signalstats.YAVG=", 1)[1].split()[0]) / 255.0)
            except (ValueError, IndexError):
                pass
    return vals


def probe(video, span: float, start=None, end=None) -> dict:
    """Motion (luma delta between samples) and edge density, spread over the whole span."""
    if not have("ffmpeg") or span <= 0:
        return {"motion": [], "edges": []}
    rate = min(1.0, PROBE_FRAMES / span)
    motion = _yavg_series(video, f"fps={rate:.5f},scale=64:-2,format=gray,tblend=all_mode=difference,"
                                 "signalstats=stat=tout,metadata=print", start, end)
    if motion and motion[0] == 0.0:
        motion = motion[1:]
    erate = min(0.5, 40 / span)
    edges = _yavg_series(video, f"fps={erate:.5f},scale=320:-2,format=gray,edgedetect=low=0.1:high=0.3,"
                                "signalstats=stat=tout,metadata=print", start, end, limit=40)
    return {"motion": motion, "edges": edges, "rate": rate}


def classify(video, span: float, cuts_per_min: float = 0.0, start=None, end=None) -> dict:
    p = probe(video, span, start, end)
    motion, edges = p["motion"], p["edges"]
    motion_median = round(statistics.median(motion), 4) if motion else 0.0
    edge_peak = round(max(edges), 4) if edges else 0.0
    density = round(edge_peak / (motion_median + EPS), 3) if (motion or edges) else 0.0
    if density >= DENSE_DENSITY_MIN or cuts_per_min >= DENSE_CUTS_PER_MIN:
        kind, label = "dense", "visually dense (screencast / slides / diagrams / on-screen text / fast B-roll)"
    elif density <= STATIC_DENSITY_MAX:
        kind, label = "talking", "talking head / near-static shot - the transcript carries the content"
    else:
        kind, label = "mixed", "mixed / ambiguous - medium frame budget"
    return {"kind": kind, "label": label, "info_density": density, "motion_median": motion_median,
            "edge_peak": edge_peak, "cuts_per_minute": round(cuts_per_min, 2), "motion": motion,
            "probe_rate": p.get("rate")}


def motion_map(values: list[float], width: int = 40) -> str:
    """Log-scaled sparkline so both a slow pan and a whip pan register."""
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [max(values[int(i * step):max(int((i + 1) * step), int(i * step) + 1)]) for i in range(width)]
    out = []
    for v in values:
        x = (math.log10(max(v, 1e-4)) + 3.0) / 2.3  # -3 .. -0.7 -> 0..1
        out.append(SPARK[min(len(SPARK) - 1, max(0, int(x * len(SPARK))))])
    return "".join(out)
