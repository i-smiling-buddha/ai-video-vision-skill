# Contributing to AI Video Vision Skill

Thanks for helping AI agents understand video better! 🎬

## Ways to help

- **Bug reports:** a site that won't download, a wrong timestamp, a transcript that fails. Include your OS, Python, ffmpeg and yt-dlp versions, the command and the output (without private links or paths).
- **Cue phrases:** "look at this" phrases in more languages (`scripts/captions.py`).
- **Analysis filters and samplers:** new ffmpeg filters, better scene thresholds, smarter frame picking.
- **Docs and translations:** improvements to `README.md`, `README.hi.md` or `SKILL.md`.

## Ground rules

- **Standard library only** in `scripts/` — no pip dependencies.
- **No invented evidence.** Anything the agent reports must come from captions, transcripts, frames or Gemini, with timestamps.
- **No videos in the repo.** Don't commit video, audio or other copyrighted media; generate test clips with ffmpeg `lavfi` sources.
- **Keys stay secret.** Never print, log or commit API keys.

## Pull requests

1. Fork the repo and create a branch: `feat/<short-name>` or `fix/<short-name>`.
2. Keep the change focused; describe what it does and how you tested it.
3. Be kind in reviews. Beginners are welcome here.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
