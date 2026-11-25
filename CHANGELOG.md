# Changelog

## 1.2.0 — 2026-11-24

Borrowed (with credit, all MIT) from four more video skills:

- **Auto mode (new default):** measures visual information density (edges vs motion) and cut rate, then sizes the frame budget — talking heads get a thin set, screencasts/slides the full one. Report shows a motion map and pacing. *(gAmUssA/watch-skill)*
- **Even-coverage scene sampling:** detect every cut first, then pick one frame per equal time bucket (cut nearest the centre, or a fill frame). Parallel frame extraction. *(gAmUssA/watch-skill)*
- **Pacing metrics** in `analyze` and auto mode: shots, cuts/min, mean/median/longest shot. *(gAmUssA/watch-skill)*
- **Hook microscope** (`--hook`): first 10 s at 2 fps in one sheet. *(gAmUssA/watch-skill)*
- **Frame timeline:** ★ frames show what is said while they are on screen. *(gAmUssA/watch-skill, mfrashad/video-analyze)*
- **`zoom` command:** full-resolution region crops (`top-right`, `lower-third`, `x,y,w,h`) with optional OCR. *(subh05sus/video-understanding-skill)*
- **Whisper hallucination filter:** drops "thanks for watching", looping syllables and one-letter lines. *(subh05sus/video-understanding-skill)*
- **Evidence-style answers** in SKILL.md: seen / read / heard / inferred, "How I watched", confidence. *(subh05sus/video-understanding-skill)*
- **URL safety** (no localhost/private IPs/non-http), **YouTube URL normalisation** for the cache, `t=` start hints, **TikTok codec-safe** downloads, TikTok `eng-US` captions, **Gemini retry** with backoff, **`report --preset tutorial`**. *(u2n4/video-url-analyzer-mcp, gAmUssA/watch-skill)*
- 19 tests.

## 1.1.0 — 2026-11-24

- **Contact sheets:** 3×3 grids with burned-in timestamps (`--sheets`, automatic above 12 frames). The agent sees 9 moments for about the token cost of one frame.
- **`search` command:** find when words were said (transcript) or shown on screen (OCR) in an earlier run.
- **`clip` command:** cut an MP4, a vertical 1080×1920 Short/Reel, or a GIF from any video or work dir.
- **OCR (`--ocr`):** reads slides, code and burned-in text with tesseract, filtered by confidence (`--ocr-lang eng+hin`).
- **Chapters:** uploader chapters are shown in the report.
- **Long transcripts:** past `--max-transcript-chars` the full text goes to `transcript.txt` with a pointer to `search`.
- **Audio files:** podcasts and `.mp3/.m4a/.wav` work in transcript mode.
- **Download cache:** URLs, captions and metadata are reused across runs (`--no-cache`, `setup --clear-cache`).
- **Fix:** uniform frames now carry their exact source timestamp (the old fps-filter sampling could be off by up to half an interval).
- **Tests + CI:** 13 end-to-end tests on generated clips, run on Linux, macOS and Windows.

## 1.0.0 — 2026-11-24

- First release: fuses claude-vision, claude-video-vision, watch-video and claude-video into one stdlib-only skill.
