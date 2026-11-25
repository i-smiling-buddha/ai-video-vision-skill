#!/usr/bin/env python3
"""ai-video-vision - one CLI that lets Claude watch any video.

  vv.py setup   [--install] [--set KEY=VALUE] [--json]
  vv.py info    <url|file>
  vv.py analyze <url|file> [--filters scenes,silence,...] [--start T --end T]
  vv.py watch   <url|file> [--question Q] [--engine local|gemini|hybrid] [--mode ...] [...]
  vv.py detail  <url|file|work_dir> --start T --end T [--fps F] [--resolution W] [--view-sample N]
  vv.py search  <work_dir> "words"              (when was it said / shown?)
  vv.py zoom    <url|file|work_dir> --at T [--region top-right] [--ocr]
  vv.py clip    <url|file|work_dir> --start T --end T [--vertical] [--gif]
  vv.py report  <url|file> [--prompt P]        (Gemini structured, anti-hallucination report)

Standard library only; needs ffmpeg/ffprobe (+ yt-dlp for URLs).
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (CACHE_DIR, CONFIG_FILE, UNTRUSTED_NOTE, configure_stdio, fmt, have, log,  # noqa: E402
                    parse_time, probe, run, scaffold_config, setting, short, write_setting)

VERSION = "1.2.0"

# mode -> (sampler, frames_with_transcript, frames_vision_only, width_with, width_vision_only)
MODES = {
    "transcript":   (None,        0,    0,    0,   0),
    "fast":         ("keyframe",  15,   30,   384, 480),
    "balanced":     ("scene",     30,   60,   512, 512),
    "accurate":     ("scene",     60,   100,  768, 768),
    "token-burner": ("scene",     None, None, 768, 1024),
}


def _time(v):
    try:
        return parse_time(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def _work_dir(out_dir) -> Path:
    if out_dir:
        base = Path(out_dir).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="vv_", dir=base))
    return Path(tempfile.mkdtemp(prefix="vv_"))


# =================================================================== setup
def cmd_setup(a) -> int:
    from transcribe import WHISPER_MODELS, MODEL_DIR, install_hint, whisper_bin
    scaffold_config()
    for pair in a.set or []:
        k, _, v = pair.partition("=")
        write_setting(k.strip(), v.strip())
        print(f"set {k.strip()} in {CONFIG_FILE}" + (" (value hidden)" if "KEY" in k.upper() else f" = {v.strip()}"))
    if a.install:
        _install()
    if a.clear_cache:
        import shutil
        from source import MEDIA_CACHE
        size = sum(f.stat().st_size for f in MEDIA_CACHE.rglob("*") if f.is_file()) if MEDIA_CACHE.exists() else 0
        shutil.rmtree(MEDIA_CACHE, ignore_errors=True)
        print(f"cleared download cache ({size / 1e6:.0f} MB); whisper models kept")
    tools = {}
    for t in ("ffmpeg", "ffprobe", "yt-dlp"):
        path = have(t)
        ver = ""
        if path:
            out = run([t, "-version" if t != "yt-dlp" else "--version"]).stdout.splitlines()
            ver = out[0][:60] if out else ""
        tools[t] = {"path": path, "version": ver}
    wb = whisper_bin()
    models = sorted(p.stem.replace("ggml-", "") for p in MODEL_DIR.glob("ggml-*.bin")) if MODEL_DIR.exists() else []
    status = {
        "version": VERSION,
        "os": platform.system(),
        "python": platform.python_version(),
        "config_file": str(CONFIG_FILE),
        "tools": tools,
        "whisper_cpp": {"path": wb, "models_cached": models, "install": None if wb else install_hint()},
        "download_cache_mb": round(sum(f.stat().st_size for f in (CACHE_DIR / "media").rglob("*")
                                       if f.is_file()) / 1e6, 1) if (CACHE_DIR / "media").exists() else 0,
        "tesseract_ocr": have("tesseract"),
        "keys_present": {k: bool(setting(k)) for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY")},
        "settings": {k: setting(k, d) for k, d in (("VV_ENGINE", "local"), ("VV_MODE", "auto"),
                                                   ("VV_TRANSCRIBER", "auto"), ("VV_WHISPER_MODEL", "base"),
                                                   ("VV_GEMINI_MODEL", "gemini-flash-latest"))},
    }
    status["can_watch_local_files"] = bool(tools["ffmpeg"]["path"] and tools["ffprobe"]["path"])
    status["can_watch_urls"] = status["can_watch_local_files"] and bool(tools["yt-dlp"]["path"])
    status["can_use_gemini"] = status["keys_present"]["GEMINI_API_KEY"]
    status["speech_fallback"] = [n for n, ok in (("whisper-cpp", wb), ("groq", status["keys_present"]["GROQ_API_KEY"]),
                                                 ("openai", status["keys_present"]["OPENAI_API_KEY"]),
                                                 ("gemini", status["keys_present"]["GEMINI_API_KEY"])) if ok]
    if a.json:
        print(json.dumps(status, indent=2))
    else:
        print(f"ai-video-vision {VERSION} on {status['os']} / Python {status['python']}")
        for t, info in tools.items():
            print(f"  {t:8} {'OK  ' + info['version'] if info['path'] else 'MISSING'}")
        print(f"  whisper  {'OK  ' + wb if wb else 'not installed -> ' + install_hint()}")
        print(f"  keys     " + ", ".join(f"{k.split('_')[0]}={'yes' if v else 'no'}" for k, v in status['keys_present'].items()))
        print(f"  config   {CONFIG_FILE}")
        print(f"  local files: {'yes' if status['can_watch_local_files'] else 'NO'} | URLs: "
              f"{'yes' if status['can_watch_urls'] else 'NO'} | Gemini: {'yes' if status['can_use_gemini'] else 'no key'}"
              f" | speech fallback: {', '.join(status['speech_fallback']) or 'captions only'}")
    return 0 if status["can_watch_local_files"] else 2


def _install() -> None:
    """Best-effort, user-level installs. Never uses sudo on its own."""
    from transcribe import LOCAL_BIN_DIR, whisper_bin
    sysname = platform.system()
    steps = []
    if not have("yt-dlp"):
        steps.append([sys.executable, "-m", "pip", "install", "--user", "-U", "yt-dlp"])
    if sysname == "Darwin" and have("brew"):
        for pkg, tool in (("ffmpeg", "ffmpeg"), ("whisper-cpp", None)):
            if (tool and not have(tool)) or (pkg == "whisper-cpp" and not whisper_bin()):
                steps.append(["brew", "install", pkg])
    elif sysname == "Windows" and have("winget"):
        if not have("ffmpeg"):
            steps.append(["winget", "install", "-e", "--id", "Gyan.FFmpeg", "--accept-source-agreements"])
    for s in steps:
        log("running: " + " ".join(map(str, s)))
        subprocess.run(s)
    if not have("ffmpeg"):
        print("ffmpeg still missing. Install it: macOS `brew install ffmpeg`, Windows `winget install Gyan.FFmpeg`, "
              "Debian/Ubuntu `sudo apt install ffmpeg` (then reopen the terminal).")
    if sysname == "Linux" and not whisper_bin():
        if have("git") and have("cmake") and (have("g++") or have("c++")):
            src = CACHE_DIR / "whisper.cpp"
            log("building whisper.cpp from source (one-time, a few minutes)...")
            if not src.exists():
                subprocess.run(["git", "clone", "--depth", "1", "https://github.com/ggml-org/whisper.cpp", str(src)])
            subprocess.run(["cmake", "-B", "build", "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF"], cwd=src)
            subprocess.run(["cmake", "--build", "build", "-j", "--config", "Release", "--target", "whisper-cli"], cwd=src)
            built = src / "build" / "bin" / "whisper-cli"
            if built.exists():
                LOCAL_BIN_DIR.mkdir(parents=True, exist_ok=True)
                (LOCAL_BIN_DIR / "whisper-cli").write_bytes(built.read_bytes())
                (LOCAL_BIN_DIR / "whisper-cli").chmod(0o755)
        else:
            print("whisper.cpp needs git + cmake + g++ to build (e.g. `sudo apt install git cmake g++`).")
    if sysname == "Windows" and not whisper_bin():
        print(f"whisper.cpp (optional): unzip whisper-bin-x64.zip from https://github.com/ggml-org/whisper.cpp/releases "
              f"into {LOCAL_BIN_DIR}")


# ==================================================================== info
def cmd_info(a) -> int:
    from source import check_url, is_url, is_youtube, url_info, _cookie_args
    if is_url(a.source):
        check_url(a.source)
    if not is_url(a.source):
        p = Path(a.source).expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"Video not found: {a.source}")
        print(json.dumps({"source": str(p), "title": p.stem, **probe(p)}, indent=2))
        return 0
    info = url_info(a.source, _cookie_args(a.cookies, a.cookies_from_browser))
    out = {"source": a.source, "is_youtube": is_youtube(a.source), "title": info.get("title"),
           "uploader": info.get("uploader") or info.get("channel"), "duration": info.get("duration"),
           "upload_date": info.get("upload_date"), "language": info.get("language"),
           "width": info.get("width"), "height": info.get("height"), "fps": info.get("fps"),
           "chapters": [{"t": fmt(c.get("start_time", 0)), "title": c.get("title")} for c in info.get("chapters") or []],
           "manual_captions": sorted((info.get("subtitles") or {}).keys()),
           "auto_captions_available": bool(info.get("automatic_captions"))}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


# ================================================================= analyze
def cmd_analyze(a) -> int:
    from analyze import ALL, analyze, render
    from source import resolve
    work = _work_dir(a.out_dir)
    src = resolve(a.source, work, need_media=True, want_captions=False,
                  cookies=a.cookies, browser=a.cookies_from_browser)
    filters = ALL if a.filters in (None, "all") else [f.strip() for f in a.filters.split(",")]
    bad = set(filters) - set(ALL)
    if bad:
        raise SystemExit(f"unknown filters {sorted(bad)}; choose from {', '.join(ALL)}")
    res = analyze(str(src.path), filters=filters, start=a.start, end=a.end, scene_threshold=a.scene_threshold,
                  has_audio=src.meta.get("has_audio", True), duration=src.duration)
    (work / "analysis.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    _write_manifest(work, src, extra={"analysis": "analysis.json"})
    print(f"# Analysis: {src.title}\n- Duration: {fmt(src.duration)}\n- Work dir: `{work}`\n")
    print(render(res))
    print("\nUse these times to plan: `watch --timestamps ...` or `detail <work_dir> --start .. --end ..`.")
    return 0


# =================================================================== watch
def _write_manifest(work: Path, src, frames=None, extra=None) -> None:
    mpath = work / "manifest.json"
    m = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
    m.update({"source": src.source, "title": src.title, "duration": src.duration,
              "video_path": str(src.path) if src.path else None, "work_dir": str(work), "updated": time.time()})
    if frames is not None:
        m["frames"] = [{"t": f["t"], "path": f["path"], "kind": f["kind"]} for f in frames]
    m.update(extra or {})
    mpath.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


def _gemini_section(target, a, src_title="") -> list[str]:
    import gemini
    res = gemini.ask(target, a.question, start=a.start, end=a.end, model=a.gemini_model,
                     low_res=a.low_res, fps=a.fps if a.engine == "gemini" else None)
    L = [f"## Answer (from Gemini - {res['model']})",
         "_These are Gemini's observations, not Claude's own viewing. Relay them with their timestamps._", "",
         res["text"], ""]
    if res.get("tokens"):
        L.append(f"- Gemini tokens used: {res['tokens']}")
    if res.get("cleanup_note"):
        L.append(f"- Note: {res['cleanup_note']}")
    return L


def cmd_watch(a) -> int:
    from source import download_media, is_url, is_youtube, resolve
    engine = a.engine or setting("VV_ENGINE", "local")
    if engine == "auto":
        engine = "gemini" if setting("GEMINI_API_KEY") else "local"
    if a.private and engine != "local":
        log("--private: forcing local engine (nothing is uploaded)")
        engine = "local"
    mode = a.mode or setting("VV_MODE", "auto")
    if mode not in MODES and mode != "auto":
        raise SystemExit(f"unknown mode {mode!r}; choose {', '.join(MODES)}")
    tb = "none" if a.no_transcribe else (a.transcriber or setting("VV_TRANSCRIBER", "auto"))
    if a.private and tb in ("groq", "openai", "gemini", "auto"):
        tb = "whisper-cpp" if tb != "auto" else "auto-local"
    wmodel = a.whisper_model or setting("VV_WHISPER_MODEL", "base")
    work = _work_dir(a.out_dir)
    L: list[str] = []

    # ---------------- Gemini only: YouTube needs no download at all
    if engine == "gemini":
        if is_youtube(a.source):
            target, title = a.source, a.source
        else:
            src = resolve(a.source, work, need_media=True, want_captions=False,
                          cookies=a.cookies, browser=a.cookies_from_browser)
            target, title = str(src.path), src.title
        L += [f"# Video watched by Gemini: {title}", f"- Source: `{a.source}`",
              f"- Question: {a.question or '(none - full summary)'}"]
        if a.start is not None or a.end is not None:
            L.append(f"- Range: {fmt(a.start or 0)} - {fmt(a.end) if a.end is not None else 'end'}")
        L += [f"- Work dir: `{work}`", ""] + _gemini_section(target, a)
        L += ["", f"> {UNTRUSTED_NOTE}", "", "## Cleanup", f"When finished: delete `{work}`"]
        print("\n".join(L))
        return 0

    # ---------------- local / hybrid
    auto_info = None
    sampler, fc, fnc, wc, wnc = MODES["balanced" if mode == "auto" else mode]
    cue_times = [parse_time(x) for x in a.timestamps.split(",")] if a.timestamps else []
    src = resolve(a.source, work, need_media=False, sub_lang=a.sub_lang, cookies=a.cookies,
                  browser=a.cookies_from_browser, use_cache=not a.no_cache)

    # transcript: captions first
    from captions import clip, format_transcript, parse_subtitles, visual_cues
    segs, t_source, t_note, gaps, tags = [], None, None, [], []
    if src.captions:
        try:
            segs = clip(parse_subtitles(src.captions), a.start, a.end)
            t_source = (f"platform captions ({src.caption_kind}, {src.caption_lang})" if src.caption_lang
                        else f"sidecar subtitle file {Path(src.captions).name}")
            if not segs:
                t_note = "captions exist but are empty for this range"
        except Exception as e:  # noqa: BLE001
            t_note = f"caption parse failed: {e}"
    need_media = bool(mode != "transcript" or cue_times or (not segs and tb != "none") or a.analyze
                      or a.ocr or (engine == "hybrid" and not is_youtube(a.source)))
    if need_media and src.path is None:
        try:
            from source import _cookie_args
            src.path = download_media(a.source, src.media_dir or work,
                                      _cookie_args(a.cookies, a.cookies_from_browser), a.max_height)
            src.meta = probe(src.path)
            src.duration = src.duration or src.meta["duration"]
        except (RuntimeError, SystemExit) as e:
            if not segs:
                raise SystemExit(f"download failed: {e}")
            src.notes.append(f"media download failed, transcript only: {short(str(e), 300)}")
    duration = src.duration or (src.meta or {}).get("duration") or 0.0
    has_video = bool((src.meta or {}).get("has_video", src.path is not None))

    if not segs and src.path is not None:
        backend = tb
        if tb == "auto-local":
            from transcribe import whisper_bin
            backend = "whisper-cpp" if whisper_bin() else "none"
            if backend == "none":
                t_note = "private mode and local whisper.cpp is not installed - no speech fallback"
        from transcribe import transcribe
        tr = transcribe(str(src.path), work, backend=backend, start=a.start, end=a.end,
                        whisper_model_name=wmodel, language=a.language)
        segs, gaps, tags = tr["segments"], tr["gaps"], tr["audio_tags"]
        t_source = tr["source"] or t_source
        t_note = t_note if backend == "none" and t_note else (tr["note"] or t_note)
    has_tr = bool(segs)

    # auto: classify visual density first (gAmUssA/watch-skill idea) and size the frame budget to it
    cuts_cache = None
    if mode == "auto" and src.path is not None and has_video:
        import classify as C
        import frames as F0
        span = (a.end if a.end is not None else duration) - (a.start or 0.0)
        cuts_cache = F0.detect_cuts(str(src.path), start=a.start, end=a.end)
        cpm = len(cuts_cache) / max(span / 60.0, 1e-6)
        auto_info = C.classify(str(src.path), span, cpm, a.start, a.end)
        budget = {"talking": (12, 30, 384, 512, "keyframe"), "mixed": (30, 60, 512, 512, "scene"),
                  "dense": (60, 90, 768, 768, "scene")}[auto_info["kind"]]
        fc, fnc, wc, wnc, sampler = budget
        if 0 < span <= 60:  # Shorts / Reels: never starve, the hook matters
            fc, fnc = max(fc, 24), max(fnc, 30)

    # frames
    import frames as F
    frames, engine_used, dropped = [], "none", 0
    fdir = work / "frames"
    width = a.resolution or (wc if has_tr else wnc) or 768
    if src.path is not None and has_video and (sampler or cue_times):
        max_frames = a.max_frames if a.max_frames is not None else (fc if has_tr else fnc)
        if sampler and mode != "transcript":
            pick = a.sampler or sampler
            kw = dict(start=a.start, end=a.end, duration=duration, max_frames=max_frames, width=width)
            if a.fps:
                frames, engine_used = F.uniform(str(src.path), fdir, fps=a.fps, **kw), f"uniform @ {a.fps} fps"
            elif pick == "uniform":
                frames, engine_used = F.uniform(str(src.path), fdir, **kw), "uniform"
            elif pick == "keyframe":
                frames, engine_used = F.keyframes(str(src.path), fdir, **kw)
            else:
                frames, engine_used = F.scene(str(src.path), fdir, threshold=a.scene_threshold_frames,
                                              cuts=cuts_cache, **kw)
            if not a.no_dedup:
                frames, dropped = F.dedupe(frames)
        dense = bool(auto_info and auto_info["kind"] == "dense")
        auto_cues = visual_cues(segs) if (a.auto_cues or dense or mode in ("accurate", "token-burner")) and has_tr else []
        all_cues = sorted(set(cue_times + [c["t"] + 1.0 for c in auto_cues]))
        if a.start is not None:
            all_cues = [t for t in all_cues if t >= a.start]
        if a.end is not None:
            all_cues = [t for t in all_cues if t <= a.end]
        if all_cues:
            frames = sorted(frames + F.at_times(str(src.path), fdir, all_cues, max(width, 768)),
                            key=lambda f: f["t"])
    first_look = F.view_sample(frames, a.view_sample) if frames else []
    first_paths = {f["path"] for f in first_look}

    # hook microscope: the first 10 s at 2 fps in one sheet (gAmUssA/watch-skill idea)
    hook_sheet = None
    if (a.hook or (mode in ("accurate", "token-burner") and duration >= 20)) and src.path is not None \
            and has_video and (a.start or 0) < 10:
        hf = F.uniform(str(src.path), work / "hook", start=a.start or 0.0, end=min(duration, (a.start or 0.0) + 10),
                       duration=duration, max_frames=20, width=480, fps=2.0, prefix="h")
        hs = F.contact_sheets(hf, work / "hook" / "sheet", cols=5, rows=4, tile_w=360)
        hook_sheet = hs[0] if hs else None

    # contact sheets: 9 moments per image, ~1 frame's token cost
    sheets = []
    if frames and not a.no_sheets and (a.sheets or len(frames) > 12):
        sheets = F.contact_sheets(frames, work / "sheets")

    # OCR on the first-look frames (optional, needs tesseract)
    ocr = []
    if a.ocr and frames:
        from ocr import ocr_frames
        ocr = ocr_frames(first_look if len(frames) > 30 else frames, lang=a.ocr_lang)
        (work / "ocr.json").write_text(json.dumps(ocr, indent=2, ensure_ascii=False), encoding="utf-8")

    # analysis (optional)
    analysis_md = None
    if (a.analyze or mode in ("accurate", "token-burner") and duration > 30) and src.path is not None and has_video:
        from analyze import analyze as run_an, render
        an = run_an(str(src.path), filters=("scenes", "black", "freeze", "silence", "motion"),
                    start=a.start, end=a.end, has_audio=(src.meta or {}).get("has_audio", True), duration=duration)
        (work / "analysis.json").write_text(json.dumps(an, indent=2), encoding="utf-8")
        analysis_md = render(an)

    # --------------- report
    title = src.title or Path(a.source).name
    L += [f"# Video ready: {title}", "", "## Metadata", f"- Source: `{src.source}`"]
    if src.uploader:
        L.append(f"- Uploader: {src.uploader}")
    L.append(f"- Duration: {fmt(duration)} ({duration:.1f}s)")
    if src.meta:
        m = src.meta
        if m.get("has_video"):
            L.append(f"- Video: {m.get('width')}x{m.get('height')} @ {m.get('fps')} fps, "
                     f"audio: {'yes' if m.get('has_audio') else 'NO audio track'}")
        else:
            L.append("- Audio-only file (no video stream) - answer from the transcript only")
    if a.start is not None or a.end is not None:
        L.append(f"- Focus range: {fmt(a.start or 0)} - {fmt(a.end) if a.end is not None else 'end'}")
    L.append(f"- Engine: {engine} | mode: {mode} | frame sampler: {engine_used}")
    if auto_info:
        import classify as C
        L.append(f"- Auto mode: **{auto_info['label']}** (info-density {auto_info['info_density']}, "
                 f"{auto_info['cuts_per_minute']} cuts/min)")
        mm = C.motion_map(auto_info.get("motion") or [])
        if mm:
            L.append(f"- Motion map {fmt(a.start or 0)}→{fmt(a.end if a.end is not None else duration)}: `{mm}` "
                     "(▁ still … █ fast; re-watch fast parts with `detail` before claiming what happens there)")
    if cuts_cache is not None:
        import pacing
        pc = pacing.compute(cuts_cache, a.start or 0.0, a.end if a.end is not None else duration)
        L.append(f"- Pacing: {pc['shot_count']} shots, {pc['cuts_per_minute']} cuts/min, median shot "
                 f"{pc['median_shot']}s, longest {pc['longest_shot']['seconds']}s at {fmt(pc['longest_shot']['start'])}")
    L.append(f"- Transcript: {t_source or 'none'}" + (f" ({t_note})" if t_note else ""))
    L.append(f"- Frames: {len(frames)}" + (f" ({dropped} near-duplicates removed)" if dropped else "")
             + (f" + {len(sheets)} contact sheet(s)" if sheets else ""))
    L.append(f"- Work dir: `{work}`")
    for n in src.notes:
        L.append(f"- Note: {n}")
    L.append("")
    if src.chapters:
        L += ["## Chapters (from the uploader)"] + [f"- [{fmt(c['t'])}] {c['title']}" for c in src.chapters] + [""]
    if engine == "hybrid":
        target = a.source if is_youtube(a.source) else (str(src.path) if src.path else None)
        if target:
            try:
                L += _gemini_section(target, a)
            except (SystemExit, RuntimeError) as e:
                L += ["## Gemini (hybrid) unavailable", str(e), ""]
    if analysis_md:
        L += [analysis_md, ""]
    if has_tr:
        full = format_transcript(segs)
        L += ["## Transcript"]
        if len(full) > a.max_transcript_chars:
            cut = full[: a.max_transcript_chars].rsplit("\n", 1)[0]
            L += [f"_Long transcript: showing the first {len(cut):,} of {len(full):,} characters. Full text: "
                  f"`{work / 'transcript.txt'}`. Find moments with `vv.py search {work} \"words\"`, "
                  "or rerun with --start/--end._", "```", cut, "```", ""]
        else:
            L += ["```", full, "```", ""]
        if gaps:
            L.append("Missing transcript intervals: " + ", ".join(f"{fmt(g['from'])}-{fmt(g['to'])}" for g in gaps))
    if tags:
        L += ["## Non-speech audio (Gemini tags)"] + [f"- [{fmt(t['start'])}] {t['tag']}" for t in tags] + [""]
    cues = visual_cues(segs) if has_tr else []
    if cues:
        L += ["## Visual cues spoken in the transcript",
              "_The speaker points at the screen here. If these frames are not listed below, rerun with "
              "`--timestamps` (or `--auto-cues`)._"]
        L += [f"- [{fmt(c['t'])}] \"{c['phrase']}\" - {c['text']}" for c in cues] + [""]
    if ocr:
        L += ["## On-screen text (OCR, may contain recognition errors)"]
        L += [f"- [{fmt(o['t'], precise=True)}] {o['text']}" for o in ocr if o["text"]] or ["- (no readable text found)"]
        L.append("")
    if hook_sheet:
        L += ["## Hook microscope (first 10 s at 2 fps)",
              "_The first seconds decide whether people keep watching. Read this sheet for hook / intro questions._",
              f"- `{hook_sheet['path']}`", ""]
    if sheets:
        L += ["## Overview sheets",
              "Each sheet tiles up to 9 frames with their timestamps burned in. **Read the sheets first** - "
              "one sheet costs about as much as one frame. Open single frames below only for details.", ""]
        L += [f"- [{fmt(sh['from'], True)} - {fmt(sh['to'], True)}] ({sh['count']} frames) `{sh['path']}`"
              for sh in sheets] + [""]
    if frames:
        L += ["## Frames",
              f"★ = look at these {len(first_look)} first (scene cuts + cue frames, the rest spread evenly). "
              "Then read more around the moments the question needs. Cue frames are pinned timestamps.", ""]
        for f in frames:
            star = "★ " if f["path"] in first_paths else ""
            said = ""
            if star and has_tr:  # frame timeline: what is said while this frame is on screen
                from captions import said_between
                nxt = next((g["t"] for g in frames if g["t"] > f["t"]), f["t"] + 10)
                txt = said_between(segs, f["t"], nxt)
                said = f" — said: \"{txt}\"" if txt else " — (no speech)"
            L.append(f"- {star}[{fmt(f['t'], precise=True)}] ({f['kind']}) `{f['path']}`{said}")
        L.append("")
    if not has_tr and not frames:
        L += ["## Note", "No transcript and no frames were produced - nothing to answer from.", ""]
    elif not has_tr:
        L += ["## Note", f"No transcript ({t_note or 'unavailable'}). Answer from frames only and do NOT invent "
              "dialogue or narration.", ""]
    L += [f"> {UNTRUSTED_NOTE}", "", "## Cleanup",
          f"When finished with this video, delete only this work dir: `{work}`"]
    _write_manifest(work, src, frames, {"transcript_source": t_source, "mode": mode, "engine": engine,
                                         "sheets": sheets})
    (work / "transcript.txt").write_text(format_transcript(segs), encoding="utf-8")
    (work / "transcript.json").write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    print("\n".join(L))
    return 0


# ================================================================== search
def cmd_search(a) -> int:
    """Find when words were said (transcript) or shown (OCR) in an earlier run."""
    import re as _re
    w = Path(a.work_dir).expanduser()
    tj = w / "transcript.json"
    if not tj.exists():
        raise SystemExit(f"no transcript.json in {w} - run `watch` first and pass its work dir")
    segs = json.loads(tj.read_text(encoding="utf-8"))
    ocr = json.loads((w / "ocr.json").read_text(encoding="utf-8")) if (w / "ocr.json").exists() else []
    words = [x for x in _re.split(r"\s+", a.query.lower().strip()) if x]
    if not words:
        raise SystemExit("empty query")

    def score(text: str) -> int:
        t = text.lower()
        return sum(1 for x in words if x in t)
    need = len(words) if a.all_words else 1
    hits = [i for i, s in enumerate(segs) if score(s["text"]) >= need]
    hits.sort(key=lambda i: -score(segs[i]["text"]))
    hits = sorted(hits[: a.limit])
    print(f"# \"{a.query}\" - {len(hits)} transcript match(es)" + (" (best first, shown in time order)" if hits else ""))
    for i in hits:
        lo, hi = max(0, i - a.context), min(len(segs), i + a.context + 1)
        print(f"\n## [{fmt(segs[i]['start'])}]")
        for j in range(lo, hi):
            mark = "▶ " if j == i else "  "
            print(f"{mark}[{fmt(segs[j]['start'])}] {segs[j]['text']}")
    o_hits = [o for o in ocr if score(o.get("text", "")) >= need]
    if o_hits:
        print(f"\n# On-screen (OCR) matches: {len(o_hits)}")
        for o in o_hits:
            print(f"- [{fmt(o['t'], True)}] {o['text'][:160]}  `{o['path']}`")
    if not hits and not o_hits:
        print("No matches. Try fewer or different words (captions may spell names differently).")
    return 0


# ==================================================================== zoom
REGIONS = {"full": (0, 0, 1, 1), "top": (0, 0, 1, .5), "bottom": (0, .5, 1, .5), "left": (0, 0, .5, 1),
           "right": (.5, 0, .5, 1), "center": (.25, .25, .5, .5), "top-left": (0, 0, .5, .5),
           "top-right": (.5, 0, .5, .5), "bottom-left": (0, .5, .5, .5), "bottom-right": (.5, .5, .5, .5),
           "lower-third": (0, .66, 1, .34)}


def _region(r: str):
    if r in REGIONS:
        return REGIONS[r]
    try:
        x, y, w, h = (float(v) for v in r.split(","))
    except ValueError:
        raise SystemExit(f"region {r!r}: use {', '.join(REGIONS)} or x,y,w,h fractions like 0.5,0,0.5,0.3")
    return x, y, min(w, 1 - x), min(h, 1 - y)


def cmd_zoom(a) -> int:
    """Full-resolution crop of one region at given times - for small text, prices, code, UI labels.
    (zoom/region idea from subh05sus/video-understanding-skill, MIT)"""
    from source import resolve
    p = Path(a.source).expanduser()
    if p.is_dir() and (p / "manifest.json").exists():
        man = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
        video, work = man.get("video_path"), p.resolve()
        if not video:
            raise SystemExit("that work dir has no downloaded video; pass the URL instead")
    else:
        work = _work_dir(a.out_dir)
        src = resolve(a.source, work, need_media=True, want_captions=False,
                      cookies=a.cookies, browser=a.cookies_from_browser)
        video = str(src.path)
        _write_manifest(work, src)
    x, y, w, h = _region(a.region)
    out_dir = work / "zoom"
    out_dir.mkdir(parents=True, exist_ok=True)
    times = [parse_time(t) for t in a.at.split(",")]
    shots = []
    for t in times:
        out = out_dir / f"zoom_{int(t * 1000):09d}_{a.region.replace(',', '-')}.png"
        vf = (f"crop=trunc(iw*{w}/2)*2:trunc(ih*{h}/2)*2:trunc(iw*{x}):trunc(ih*{y}),"
              f"scale='if(lt(iw,{a.width}),{a.width},iw)':-2:flags=lanczos,"
              f"scale=w=-2:h='min(1998,ih)':flags=lanczos")
        res = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", video,
                   "-frames:v", "1", "-vf", vf, str(out)])
        if res.returncode != 0 or not out.exists():
            raise SystemExit(f"zoom failed at {fmt(t)}: {short(res.stderr)}")
        shots.append({"t": t, "path": str(out)})
    text = {}
    if a.ocr:
        from ocr import ocr_frames
        text = {o["path"]: o["text"] for o in ocr_frames(shots, lang=a.ocr_lang, min_conf=60)}
    print(f"# Zoom: region {a.region} ({x:.2f},{y:.2f},{w:.2f},{h:.2f})\n")
    for sh in shots:
        line = f"- [{fmt(sh['t'], True)}] `{sh['path']}`"
        if a.ocr:
            line += f" — OCR: {text.get(sh['path']) or '(nothing readable)'}"
        print(line)
    print("\nRead the image(s). Quote numbers, names, prices and URLs only after reading them here.")
    print(f"\n> {UNTRUSTED_NOTE}")
    return 0


# ==================================================================== clip
def cmd_clip(a) -> int:
    """Cut a clip (mp4, vertical 9:16 Short, or GIF) from a source or a work dir."""
    from source import resolve
    p = Path(a.source).expanduser()
    if p.is_dir() and (p / "manifest.json").exists():
        video = json.loads((p / "manifest.json").read_text(encoding="utf-8")).get("video_path")
        if not video:
            raise SystemExit("that work dir has no downloaded video; pass the URL instead")
    else:
        src = resolve(a.source, _work_dir(a.out_dir), need_media=True, want_captions=False,
                      cookies=a.cookies, browser=a.cookies_from_browser)
        video = str(src.path)
    if a.end <= a.start:
        raise SystemExit("--end must be after --start")
    span = a.end - a.start
    ext = "gif" if a.gif else "mp4"
    out = Path(a.output or f"clip_{fmt(a.start).replace(':', '-')}_{fmt(a.end).replace(':', '-')}"
               f"{'_vertical' if a.vertical else ''}.{ext}").expanduser().resolve()
    vf = []
    if a.vertical:
        vf.append("crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920:flags=lanczos")
    if a.gif:
        w = a.width or 480
        chain = ",".join(vf + [f"fps={a.gif_fps}", f"scale={w}:-2:flags=lanczos"])
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a.start:.3f}", "-i", video,
               "-t", f"{span:.3f}", "-filter_complex",
               f"[0:v]{chain},split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a",
               "-loop", "0", str(out)]
    else:
        if a.width and not a.vertical:
            vf.append(f"scale={a.width}:-2:flags=lanczos")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a.start:.3f}", "-i", video,
               "-t", f"{span:.3f}", *(["-vf", ",".join(vf)] if vf else []),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    res = run(cmd, timeout=3600)
    if res.returncode != 0 or not out.exists():
        raise SystemExit(f"ffmpeg clip failed: {short(res.stderr)}")
    print(f"Clip saved: `{out}` ({span:.1f}s, {out.stat().st_size / 1e6:.1f} MB)"
          + (" - vertical 1080x1920 center crop" if a.vertical else ""))
    print("Only publish clips you have the rights to (your own videos, or with permission).")
    return 0


# ================================================================== detail
def cmd_detail(a) -> int:
    import frames as F
    from source import resolve
    p = Path(a.source).expanduser()
    if p.is_dir() and (p / "manifest.json").exists():
        man = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
        if not man.get("video_path"):
            raise SystemExit("that work dir has no downloaded video (caption-only run); pass the URL instead")
        work, video, duration = p.resolve(), man["video_path"], man.get("duration") or 0
    else:
        work = _work_dir(a.out_dir)
        src = resolve(a.source, work, need_media=True, want_captions=False,
                      cookies=a.cookies, browser=a.cookies_from_browser)
        video, duration = str(src.path), src.duration
        _write_manifest(work, src)
    if a.end <= a.start:
        raise SystemExit("--end must be after --start")
    span = a.end - a.start
    fps = a.fps or (4.0 if span <= 5 else 2.0 if span <= 15 else max(0.2, 30 / span))
    tag = f"detail_{int(a.start * 10)}_{int(a.end * 10)}_{a.resolution}_{fps:g}"
    d = work / tag
    existing = sorted(d.glob("d_*.jpg")) if d.exists() else []
    if existing:
        frames = json.loads((d / "frames.json").read_text(encoding="utf-8"))
        log("reusing cached detail frames")
    else:
        frames = F.uniform(video, d, start=a.start, end=a.end, duration=duration, max_frames=a.max_frames,
                           width=a.resolution, fps=fps, prefix="d")
        (d / "frames.json").write_text(json.dumps(frames), encoding="utf-8")
    first = F.view_sample(frames, a.view_sample)
    fp = {f["path"] for f in first}
    print(f"# Detail {fmt(a.start, True)} - {fmt(a.end, True)} @ {fps:g} fps, {a.resolution}px\n")
    print(f"{len(frames)} frames extracted. View the ★ ones first, then only what you still need "
          "(binary-search style - never all at once).\n")
    for f in frames:
        print(f"- {'★ ' if f['path'] in fp else ''}[{fmt(f['t'], True)}] `{f['path']}`")
    print(f"\n> {UNTRUSTED_NOTE}")
    return 0


# ================================================================== report
def cmd_report(a) -> int:
    import gemini
    from source import is_youtube, resolve
    work = _work_dir(a.out_dir)
    if is_youtube(a.source):
        target = a.source
    else:
        target = str(resolve(a.source, work, need_media=True, want_captions=False,
                             cookies=a.cookies, browser=a.cookies_from_browser).path)
    if a.preset == "tutorial":
        res = gemini.generate(target, gemini.TUTORIAL_PROMPT, start=a.start, end=a.end, fps=a.fps,
                              model=a.gemini_model, low_res=a.low_res)
        txt = res["text"].strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            res["text"] = "```json\n" + json.dumps(json.loads(txt), indent=2, ensure_ascii=False) + "\n```"
            (work / "tutorial.json").write_text(txt, encoding="utf-8")
        except ValueError:
            pass
        res["text"] = ("_Review every command before running it; never execute steps automatically._\n\n"
                       + res["text"])
    else:
        res = gemini.report(target, a.prompt, start=a.start, end=a.end, fps=a.fps, model=a.gemini_model,
                            low_res=a.low_res)
    print(res["text"])
    print(f"\n---\n_Report by {res['model']} (Gemini watched the video)._")
    if res.get("cleanup_note"):
        print(f"_Note: {res['cleanup_note']}_")
    print(f"\n> {UNTRUSTED_NOTE}\n\nWork dir: `{work}`")
    return 0


# ==================================================================== main
def main(argv=None) -> int:
    configure_stdio()
    ap = argparse.ArgumentParser(prog="vv.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common_src(p):
        p.add_argument("source", help="URL (YouTube, Vimeo, TikTok, X, Instagram, Loom...) or local video path")
        p.add_argument("--start", type=_time, default=None, help="SS, MM:SS or HH:MM:SS")
        p.add_argument("--end", type=_time, default=None)
        p.add_argument("--cookies", default=None, help="cookie jar file for private videos")
        p.add_argument("--cookies-from-browser", default=None, help="e.g. chrome, firefox, edge")
        p.add_argument("--out-dir", default=None, help="parent folder for the disposable work dir")

    p = sub.add_parser("setup", help="check / install dependencies, write settings")
    p.add_argument("--install", action="store_true")
    p.add_argument("--set", action="append", metavar="KEY=VALUE")
    p.add_argument("--json", action="store_true")
    p.add_argument("--clear-cache", action="store_true", help="delete cached downloads (keeps whisper models)")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("info", help="metadata only, nothing downloaded")
    p.add_argument("source")
    p.add_argument("--cookies", default=None)
    p.add_argument("--cookies-from-browser", default=None)
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("analyze", help="ffmpeg structure pass (scene cuts, silence, motion...)")
    common_src(p)
    p.add_argument("--filters", default="all", help="all or comma list: scenes,black,freeze,silence,motion,blur,exposure,loudness")
    p.add_argument("--scene-threshold", type=float, default=8.0, help="scdet score 0-100 (lower = softer cuts)")
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("watch", help="prepare frames + transcript (local) or ask Gemini")
    common_src(p)
    p.add_argument("--question", "-q", default=None, help="the user's question, verbatim")
    p.add_argument("--engine", choices=["local", "gemini", "hybrid", "auto"], default=None)
    p.add_argument("--mode", choices=["auto", *MODES], default=None,
                   help="auto (default): measure visual density and size the frame budget to it")
    p.add_argument("--hook", action="store_true", help="dense 2 fps sheet of the first 10 s (hook / intro)")
    p.add_argument("--sampler", choices=["scene", "keyframe", "uniform"], default=None)
    p.add_argument("--max-frames", type=int, default=None)
    p.add_argument("--resolution", type=int, default=None, help="frame width px (1024+ for small text)")
    p.add_argument("--fps", type=float, default=None, help="force uniform sampling at this fps")
    p.add_argument("--timestamps", default=None, help="pin frames: 0:42,3:10,7:55")
    p.add_argument("--auto-cues", action="store_true", help="auto-pin frames where the speaker says 'look at this'")
    p.add_argument("--view-sample", type=int, default=8, help="how many frames to mark ★ for first viewing")
    p.add_argument("--no-dedup", action="store_true")
    p.add_argument("--scene-threshold-frames", type=float, default=0.20, help=argparse.SUPPRESS)
    p.add_argument("--analyze", action="store_true", help="add the ffmpeg structure pass")
    p.add_argument("--transcriber", choices=["auto", "whisper-cpp", "groq", "openai", "gemini", "none"], default=None)
    p.add_argument("--no-transcribe", action="store_true", help="captions only, no speech fallback")
    p.add_argument("--whisper-model", default=None)
    p.add_argument("--language", default=None, help="spoken-language hint for whisper, e.g. hi, en")
    p.add_argument("--sub-lang", default="auto", help="caption language (default: original language)")
    p.add_argument("--private", action="store_true", help="nothing leaves this machine (local engine + local whisper)")
    p.add_argument("--max-height", type=int, default=720, help="download resolution cap")
    p.add_argument("--gemini-model", default=None)
    p.add_argument("--low-res", action="store_true", help="Gemini low media resolution (long videos, fewer tokens)")
    p.add_argument("--sheets", action="store_true", help="always build 3x3 contact sheets")
    p.add_argument("--no-sheets", action="store_true", help="never build contact sheets")
    p.add_argument("--ocr", action="store_true", help="read on-screen text with tesseract (if installed)")
    p.add_argument("--ocr-lang", default="eng", help="tesseract languages, e.g. eng+hin")
    p.add_argument("--max-transcript-chars", type=int, default=40000, help="longer transcripts go to a file")
    p.add_argument("--no-cache", action="store_true", help="don't reuse/keep downloads in ~/.cache/video-vision")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("search", help="find when words were said/shown, in an earlier watch run")
    p.add_argument("work_dir")
    p.add_argument("query")
    p.add_argument("--context", type=int, default=1, help="transcript lines around each hit")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--all-words", action="store_true", help="every word must appear")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("zoom", help="full-resolution crop of a region (small text, prices, code)")
    p.add_argument("source", help="URL, file, or a work dir from watch/detail")
    p.add_argument("--at", required=True, help="time(s): 1:49 or 1:49,2:03")
    p.add_argument("--region", default="full", help="top-right, lower-third, center... or x,y,w,h fractions")
    p.add_argument("--width", type=int, default=1280, help="upscale small crops to at least this width")
    p.add_argument("--ocr", action="store_true")
    p.add_argument("--ocr-lang", default="eng")
    p.add_argument("--cookies", default=None)
    p.add_argument("--cookies-from-browser", default=None)
    p.add_argument("--out-dir", default=None)
    p.set_defaults(fn=cmd_zoom)

    p = sub.add_parser("clip", help="cut an mp4 / vertical Short / GIF from a video")
    common_src(p)
    p.add_argument("-o", "--output", default=None)
    p.add_argument("--vertical", action="store_true", help="9:16 1080x1920 center crop for Shorts/Reels")
    p.add_argument("--gif", action="store_true")
    p.add_argument("--gif-fps", type=int, default=12)
    p.add_argument("--width", type=int, default=None)
    p.set_defaults(fn=cmd_clip)

    p = sub.add_parser("detail", help="drill into a short window at high fps/resolution")
    common_src(p)
    p.add_argument("--fps", type=float, default=None)
    p.add_argument("--resolution", type=int, default=1024)
    p.add_argument("--max-frames", type=int, default=40)
    p.add_argument("--view-sample", type=int, default=3)
    p.set_defaults(fn=cmd_detail)

    p = sub.add_parser("report", help="Gemini structured report with anti-hallucination rules")
    common_src(p)
    p.add_argument("--prompt", default=None, help="custom analysis prompt (accuracy rules are always prepended)")
    p.add_argument("--preset", choices=["report", "tutorial"], default="report",
                   help="tutorial: steps / commands / code / file paths as JSON")
    p.add_argument("--fps", type=float, default=None, help="Gemini sampling fps (raise for fast cuts)")
    p.add_argument("--gemini-model", default=None)
    p.add_argument("--low-res", action="store_true")
    p.set_defaults(fn=cmd_report)

    a = ap.parse_args(argv)
    if a.cmd in ("detail", "clip") and (a.start is None or a.end is None):
        ap.error(f"{a.cmd} needs --start and --end")
    try:
        return a.fn(a)
    except RuntimeError as e:  # Gemini / download errors: one clean line, no traceback
        print(f"ERROR: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
