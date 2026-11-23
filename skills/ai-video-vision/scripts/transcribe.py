"""Speech fallbacks for videos without captions.

Chain (VV_TRANSCRIBER=auto): local whisper.cpp -> Groq -> OpenAI -> Gemini,
using only backends that are installed / have a key. A silence pre-check
stops Whisper from hallucinating speech on silent or ambient-only audio.
"""
from __future__ import annotations

import json
import math
import platform
import re
import uuid
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import CACHE_DIR, have, log, probe, redact, run, setting, short

WHISPER_MODELS = ("tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "large-v3-turbo")
MODEL_DIR = CACHE_DIR / "whisper-models"
LOCAL_BIN_DIR = CACHE_DIR / "bin"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{}.bin"

CLOUD = {
    "groq": ("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3", "GROQ_API_KEY"),
    "openai": ("https://api.openai.com/v1/audio/transcriptions", "whisper-1", "OPENAI_API_KEY"),
}
MAX_UPLOAD = 24_000_000
SILENT_DB = -50.0


# ------------------------------------------------------------- audio utils
def _range(start, end) -> tuple[list, list]:
    pre = ["-ss", f"{start:.3f}"] if start else []
    post = ["-t", f"{max(0.0, end - (start or 0)):.3f}"] if end is not None else []
    return pre, post


def audio_level(video, start=None, end=None) -> Optional[float]:
    """Peak volume in dB of the (clipped) audio; None when there is no audio."""
    pre, post = _range(start, end)
    res = run(["ffmpeg", "-hide_banner", "-nostats", *pre, "-i", video, *post, "-vn",
               "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", res.stderr)
    if not m:
        return None
    return -120.0 if m.group(1) == "-inf" else float(m.group(1))


def extract_audio(video, out: Path, start=None, end=None, wav: bool = False) -> Path:
    pre, post = _range(start, end)
    codec = ["-c:a", "pcm_s16le"] if wav else ["-c:a", "libmp3lame", "-b:a", "64k"]
    res = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *pre, "-i", video, *post,
               "-vn", "-ac", "1", "-ar", "16000", *codec, str(out)])
    if res.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"audio extraction failed: {short(res.stderr, 300)}")
    return out


# -------------------------------------------------------------- whisper.cpp
def whisper_bin() -> Optional[str]:
    for name in ("whisper-cli", "whisper-cpp", "whisper-cli.exe"):
        if have(name):
            return have(name)
    for p in (LOCAL_BIN_DIR / "whisper-cli", LOCAL_BIN_DIR / "whisper-cli.exe",
              LOCAL_BIN_DIR / "Release" / "whisper-cli.exe"):
        if p.is_file():
            return str(p)
    return None


def whisper_model(name: str) -> Path:
    if name not in WHISPER_MODELS:
        raise ValueError(f"unknown whisper model {name!r}; choose {', '.join(WHISPER_MODELS)}")
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    target = MODEL_DIR / f"ggml-{name}.bin"
    if target.is_file() and target.stat().st_size > 1_000_000:
        return target
    log(f"downloading whisper model '{name}' once (base ~140 MB)...")
    tmp = target.with_suffix(".partial")
    with urlopen(MODEL_URL.format(name), timeout=60) as r, open(tmp, "wb") as f:
        while True:
            block = r.read(1 << 20)
            if not block:
                break
            f.write(block)
    tmp.replace(target)
    return target


def whisper_cpp(video, work: Path, start=None, end=None, model: str = "base",
                language: Optional[str] = None) -> list[dict]:
    from captions import offset, parse_subtitles
    binary = whisper_bin()
    if not binary:
        raise RuntimeError("whisper.cpp not installed (python scripts/vv.py setup --install)")
    work.mkdir(parents=True, exist_ok=True)
    wav = extract_audio(video, work / "audio.wav", start, end, wav=True)
    try:
        mpath = whisper_model(model)
        out_base = work / "whisper"
        log(f"transcribing locally with whisper.cpp ({model})...")
        res = run([binary, "-m", mpath, "-f", wav, "-ovtt", "-of", out_base, "-l", language or "auto", "-np"])
        vtt = out_base.with_suffix(".vtt")
        if res.returncode != 0 or not vtt.exists():
            raise RuntimeError(f"whisper-cli failed: {short(res.stderr, 300)}")
        return offset(parse_subtitles(vtt), start or 0.0)
    finally:
        wav.unlink(missing_ok=True)


# ------------------------------------------------------------ Groq / OpenAI
def _multipart(fields: dict, file_path: Path) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
                 f"Content-Type: audio/mpeg\r\n\r\n".encode() + file_path.read_bytes() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), b


def _cloud_chunk(provider: str, mp3: Path, key: str, language: Optional[str]) -> list[dict]:
    url, model, _ = CLOUD[provider]
    fields = {"model": model, "response_format": "verbose_json", "temperature": "0"}
    if language:
        fields["language"] = language
    body, boundary = _multipart(fields, mp3)
    req = Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urlopen(req, timeout=600) as r:
            data = json.loads(r.read())
    except HTTPError as e:
        raise RuntimeError(f"{provider} HTTP {e.code}: {redact(short(e.read().decode('utf-8', 'replace'), 300))}")
    except (URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"{provider} network error: {e}")
    segs = []
    for s in data.get("segments") or []:
        # Whisper's own hallucination guard: skip segments it is sure are not speech
        if s.get("no_speech_prob", 0) > 0.8 and s.get("avg_logprob", 0) < -1.0:
            continue
        if (s.get("text") or "").strip():
            segs.append({"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()})
    if not segs and (data.get("text") or "").strip():
        segs = [{"start": 0.0, "end": float(data.get("duration") or 0), "text": data["text"].strip()}]
    return segs


def cloud(provider: str, video, work: Path, start=None, end=None, language=None) -> tuple[list[dict], list]:
    key = setting(CLOUD[provider][2])
    if not key:
        raise RuntimeError(f"{CLOUD[provider][2]} not set")
    work.mkdir(parents=True, exist_ok=True)
    mp3 = extract_audio(video, work / "audio.mp3", start, end)
    total = probe(mp3)["duration"]
    n = max(1, math.ceil(mp3.stat().st_size / MAX_UPLOAD))
    chunk = total / n
    segs, gaps = [], []
    for i in range(n):
        if n == 1:
            part, off = mp3, 0.0
        else:
            off = i * chunk
            part = work / f"chunk_{i:03d}.mp3"
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{off:.3f}", "-i", mp3,
                 "-t", f"{chunk:.3f}", "-c", "copy", part])
        log(f"transcribing with {provider} ({i + 1}/{n})...")
        try:
            for s in _cloud_chunk(provider, part, key, language):
                segs.append({**s, "start": s["start"] + off + (start or 0), "end": s["end"] + off + (start or 0)})
        except RuntimeError as e:
            gaps.append({"from": off + (start or 0), "to": off + chunk + (start or 0), "error": str(e)})
            if n == 1:
                raise
    for p in work.glob("chunk_*.mp3"):
        p.unlink(missing_ok=True)
    mp3.unlink(missing_ok=True)
    return segs, gaps


# --------------------------------------------------------------- dispatcher
def transcribe(video, work: Path, *, backend: str, start=None, end=None,
               whisper_model_name: str = "base", language: Optional[str] = None) -> dict:
    """Returns {segments, source, gaps, audio_tags, note}. Never raises for 'no speech'."""
    result = {"segments": [], "source": None, "gaps": [], "audio_tags": [], "note": None}
    if backend == "none":
        result["note"] = "speech fallback disabled"
        return result
    level = audio_level(video, start, end)
    if level is None:
        result["note"] = "no audio track"
        return result
    if level <= SILENT_DB:
        result["note"] = f"audio is silent (peak {level:.0f} dB) - no speech to transcribe"
        return result

    order = {"auto": ["whisper-cpp", "groq", "openai", "gemini"]}.get(backend, [backend])
    errors = []
    for b in order:
        try:
            if b == "whisper-cpp":
                if backend == "auto" and not whisper_bin():
                    continue
                result["segments"] = whisper_cpp(video, work / "whisper", start, end, whisper_model_name, language)
                result["source"] = f"local whisper.cpp ({whisper_model_name})"
            elif b in CLOUD:
                if backend == "auto" and not setting(CLOUD[b][2]):
                    continue
                result["segments"], result["gaps"] = cloud(b, video, work / b, start, end, language)
                result["source"] = f"{b} cloud Whisper"
            elif b == "gemini":
                if backend == "auto" and not setting("GEMINI_API_KEY"):
                    continue
                import gemini
                data = gemini.transcribe_audio(video, work / "gemini_audio", start, end)
                result["segments"], result["audio_tags"] = data["segments"], data["audio_tags"]
                result["source"] = f"Gemini audio ({data['model']})"
            else:
                raise RuntimeError(f"unknown transcriber {b!r}")
            if not result["segments"]:
                result["note"] = f"{result['source']} found no speech"
            return result
        except Exception as e:  # noqa: BLE001 - every backend failure is reported, never fatal
            errors.append(f"{b}: {short(redact(str(e)), 250)}")
            if backend != "auto":
                break
    result["note"] = ("no speech fallback available - install whisper.cpp or add a GROQ/OPENAI/GEMINI key"
                      if not errors else "speech fallback failed: " + " | ".join(errors))
    return result


def install_hint() -> str:
    sysname = platform.system()
    if sysname == "Darwin":
        return "brew install whisper-cpp"
    if sysname == "Windows":
        return ("download whisper-bin-x64.zip from https://github.com/ggml-org/whisper.cpp/releases and put "
                f"whisper-cli.exe (with its DLLs) in {LOCAL_BIN_DIR}")
    return "python scripts/vv.py setup --install   (builds whisper.cpp from source; needs git, cmake, g++)"
