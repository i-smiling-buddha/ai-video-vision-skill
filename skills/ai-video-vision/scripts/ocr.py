"""Optional on-screen text reading with tesseract (slides, code, captions burned
into the picture). Skipped cleanly when tesseract is not installed."""
from __future__ import annotations

import re

from common import have, log, run


def ocr_frames(frames: list[dict], lang: str = "eng", min_conf: float = 70.0) -> list[dict]:
    if not have("tesseract"):
        log("tesseract not installed; skipping OCR (macOS: brew install tesseract, "
            "Windows: winget install UB-Mannheim.TesseractOCR, Linux: apt install tesseract-ocr)")
        return []
    out = []
    for f in frames:
        # tsv output carries a confidence per word: drop the noise tesseract guesses on photos/gradients
        res = run(["tesseract", f["path"], "stdout", "-l", lang, "--psm", "11", "tsv"], timeout=120)
        words = []
        for line in (res.stdout.splitlines()[1:] if res.returncode == 0 else []):
            cols = line.split("\t")
            if len(cols) < 12:
                continue
            try:
                conf = float(cols[10])
            except ValueError:
                continue
            w = cols[11].strip()
            if conf >= min_conf and len(w) >= 2 and re.search(r"[A-Za-z0-9\u0900-\u097F]{2,}", w):
                words.append(w)
        out.append({"t": f["t"], "path": f["path"], "text": " ".join(words)[:400]})
    return out
