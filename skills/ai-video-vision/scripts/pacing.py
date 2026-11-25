"""Editorial pacing: cuts per minute and shot lengths.

Adapted from gAmUssA/watch-skill `scripts/pacing.py` (MIT).
"""
from __future__ import annotations

import statistics


def compute(cut_times: list[float], start: float, end: float) -> dict:
    span = max(0.0, end - start)
    if span <= 0:
        return {"shot_count": 0, "cuts_per_minute": 0.0, "mean_shot": 0.0, "median_shot": 0.0,
                "longest_shot": None, "shortest_shot": None}
    times = []
    for t in sorted(t for t in cut_times if start <= t <= end):
        if not times or t > times[-1] + 1e-3:  # drop duplicate / regressed stamps
            times.append(t)
    if not times or times[0] > start + 0.01:
        times = [start] + times
    shots = [(t, (times[i + 1] if i + 1 < len(times) else end) - t) for i, t in enumerate(times)]
    real = [d for _, d in shots if d >= 0.1] or [d for _, d in shots]
    longest = max(shots, key=lambda s: s[1])
    shortest = min((s for s in shots if s[1] >= 0.1), key=lambda s: s[1], default=longest)
    return {"shot_count": len(shots), "cuts_per_minute": round((len(shots) - 1) / (span / 60.0), 2),
            "mean_shot": round(statistics.mean(real), 2), "median_shot": round(statistics.median(real), 2),
            "longest_shot": {"start": longest[0], "seconds": round(longest[1], 2)},
            "shortest_shot": {"start": shortest[0], "seconds": round(shortest[1], 2)}}
