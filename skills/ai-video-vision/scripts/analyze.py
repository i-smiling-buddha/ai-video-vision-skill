"""Structural analysis with ffmpeg filters - WHEN things happen, before any
frame is looked at. Scene cuts, black/frozen stretches, silence, motion
profile, blur, exposure and loudness."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Optional

from common import fmt, have, log, run

ALL = ("scenes", "black", "freeze", "silence", "motion", "blur", "exposure", "loudness")
DEFAULT_SCDET = 8.0  # scdet score 0-100; >=8 ~ hard cut


def _lavfi_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\\\:")


def _intervals(stderr: str, start_key: str, end_key: str, dur_key: str, offset: float) -> list[dict]:
    starts = [float(x) for x in re.findall(rf"{start_key}:\s*(-?[\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(rf"{end_key}:\s*(-?[\d.]+)", stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        out.append({"start": fmt(s + offset), "end": fmt(e + offset) if e is not None else "end",
                    "seconds": round((e - s), 2) if e is not None else None})
    return out


def _per_second(meta: str, key: str, offset: float) -> dict:
    buckets: dict = {}
    t = None
    for line in meta.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(rf"{re.escape(key)}=(-?\d+(?:\.\d+)?)", line)
        if m and t is not None:
            buckets.setdefault(int(t + offset), []).append(float(m.group(1)))
    return {k: sum(v) / len(v) for k, v in buckets.items()}


def profile(si: Optional[float], ti: Optional[float]) -> str:
    if si is None and ti is None:
        return "unknown"
    s = "high" if (si or 0) > 50 else "moderate" if (si or 0) > 25 else "low"
    t = "high" if (ti or 0) > 30 else "moderate" if (ti or 0) > 10 else "low"
    hint = {("low", "low"): "slides / screen recording / static graphics",
            ("moderate", "low"): "talking head or static mid-detail shots",
            ("high", "low"): "detailed static shots",
            ("moderate", "moderate"): "typical narrative footage",
            ("high", "high"): "busy action", ("low", "high"): "simple fast animation"}.get((s, t), "mixed")
    return f"{s} visual complexity, {t} motion ({hint})"


def analyze(video, *, filters=ALL, start=None, end=None, scene_threshold: float = DEFAULT_SCDET,
            has_audio: bool = True, duration: float = 0.0) -> dict:
    if not have("ffmpeg"):
        raise SystemExit("ffmpeg not found. Run: python scripts/vv.py setup --install")
    filters = set(filters)
    pre = ["-ss", f"{start:.3f}"] if start else []
    post = ["-t", f"{max(0.0, end - (start or 0)):.3f}"] if end is not None else []
    off = start or 0.0
    out: dict = {"filters": sorted(filters)}
    tmp = Path(tempfile.mkdtemp(prefix="vv_an_"))
    meta = tmp / "meta.txt"

    vf = ["scale='min(640,iw)':-2"]  # analysis at 640px: same cuts, much faster
    if "scenes" in filters:
        vf.append(f"scdet=threshold={scene_threshold}")
    if "black" in filters:
        vf.append("blackdetect=d=0.1:pic_th=0.98:pix_th=0.10")
    if "freeze" in filters:
        vf.append("freezedetect=n=-60dB:d=2")
    if "motion" in filters:
        vf.append("siti=print_summary=1")
    if "blur" in filters:
        vf.append("blurdetect")
    if "exposure" in filters:
        vf.append("signalstats")
    vf.append(f"metadata=mode=print:file={_lavfi_path(meta)}")
    af = []
    if has_audio and "silence" in filters:
        af.append("silencedetect=n=-40dB:d=0.5")
    if has_audio and "loudness" in filters:
        af.append("ebur128")
    cmd = ["ffmpeg", "-hide_banner", "-nostats", *pre, "-i", video, *post, "-vf", ",".join(vf)]
    cmd += ["-af", ",".join(af)] if af else ["-an"]
    cmd += ["-f", "null", "-"]
    log("running ffmpeg analysis pass...")
    res = run(cmd, timeout=max(600, duration * 2))
    err = res.stderr
    m = meta.read_text(encoding="utf-8", errors="replace") if meta.exists() else ""

    if "scenes" in filters:
        cuts, t = [], None
        for line in m.splitlines():
            mt = re.search(r"pts_time:([\d.]+)", line)
            if mt:
                t = float(mt.group(1))
            ms = re.search(r"lavfi\.scd\.score=([\d.]+)", line)
            if ms and t is not None and float(ms.group(1)) >= scene_threshold:
                cuts.append({"time": fmt(t + off, precise=True), "score": round(float(ms.group(1)), 1)})
        out["scene_changes"] = cuts
    if "black" in filters:
        out["black_intervals"] = _intervals(err, "black_start", "black_end", "black_duration", off)
    if "freeze" in filters:
        out["freeze_intervals"] = _intervals(m + err, "lavfi.freezedetect.freeze_start",
                                             "lavfi.freezedetect.freeze_end", "x", off) or \
            _intervals(err, "freeze_start", "freeze_end", "freeze_duration", off)
    if "silence" in filters and has_audio:
        out["silence_intervals"] = _intervals(err, "silence_start", "silence_end", "silence_duration", off)
    if "motion" in filters:
        si = re.search(r"Spatial Information:\s*\n\s*Average:\s*([\d.]+)", err)
        ti = re.search(r"Temporal Information:\s*\n\s*Average:\s*([\d.]+)", err)
        siv, tiv = (float(si.group(1)) if si else None), (float(ti.group(1)) if ti else None)
        out["motion"] = {"si_avg": siv, "ti_avg": tiv, "content_profile": profile(siv, tiv)}
    if "blur" in filters:
        b = _per_second(m, "lavfi.blur", off)
        if b:
            worst = sorted(b.items(), key=lambda kv: -kv[1])[:5]
            out["blur"] = {"mean": round(sum(b.values()) / len(b), 2),
                           "blurriest_seconds": [{"time": fmt(k), "blur": round(v, 2)} for k, v in sorted(worst)]}
    if "exposure" in filters:
        y = _per_second(m, "lavfi.signalstats.YAVG", off)
        if y:
            dark = [fmt(k) for k, v in sorted(y.items()) if v < 40]
            bright = [fmt(k) for k, v in sorted(y.items()) if v > 220]
            out["exposure"] = {"mean_brightness_0_255": round(sum(y.values()) / len(y), 1),
                               "dark_seconds": dark[:20], "overexposed_seconds": bright[:20]}
    if "loudness" in filters and has_audio:
        i = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", err)
        lra = re.findall(r"LRA:\s*([\d.]+)\s*LU", err)
        if i:
            out["loudness"] = {"integrated_lufs": float(i[-1]), "range_lu": float(lra[-1]) if lra else None}
    if res.returncode != 0 and not m:
        out["warning"] = "ffmpeg exited with an error; results may be partial"
    for p in tmp.iterdir():
        p.unlink(missing_ok=True)
    tmp.rmdir()
    return out


def render(a: dict) -> str:
    L = ["## Structure analysis (ffmpeg)"]
    if "motion" in a:
        L.append(f"- Content profile: {a['motion']['content_profile']}")
    if "scene_changes" in a:
        sc = a["scene_changes"]
        L.append(f"- Scene cuts: {len(sc)}" + (": " + ", ".join(c["time"] for c in sc[:40]) if sc else ""))
        if len(sc) > 40:
            L.append(f"  (+{len(sc) - 40} more in analysis.json)")
    for k, label in (("black_intervals", "Black stretches"), ("freeze_intervals", "Frozen stretches"),
                     ("silence_intervals", "Silences")):
        if k in a:
            iv = a[k]
            L.append(f"- {label}: {len(iv)}" + (": " + ", ".join(f"{x['start']}-{x['end']}" for x in iv[:20]) if iv else ""))
    if "blur" in a:
        L.append(f"- Blur mean {a['blur']['mean']}; blurriest: " + ", ".join(x["time"] for x in a["blur"]["blurriest_seconds"]))
    if "exposure" in a:
        e = a["exposure"]
        L.append(f"- Brightness mean {e['mean_brightness_0_255']}/255; dark: {', '.join(e['dark_seconds'][:8]) or 'none'}")
    if "loudness" in a:
        L.append(f"- Loudness {a['loudness']['integrated_lufs']} LUFS (range {a['loudness']['range_lu']} LU)")
    if a.get("warning"):
        L.append(f"- Warning: {a['warning']}")
    return "\n".join(L)
