# interview-trailer

An agent skill that turns a long interview, podcast, webinar or video-call recording into a
finished, subtitled mini-trailer from one prompt.

> "Make a 5–10 minute trailer from `call.mp4`. Include: what the company does, its history,
> the 2% cashback, treasury accounts, opening an account without a US company. Don't mention
> the target customer."

You get `call-trailer-v1.mp4` + `call-trailer-v1.srt`, usually ready on the first pass.

## Install

```bash
npx skills add pestanadiego/interview-trailer-skill
```

Works with Claude Code, Cursor, Codex and the other agents supported by
[skills.sh](https://skills.sh). The only prerequisite is Python 3.8+ on the machine; the skill
installs everything else by itself into `~/.cache/interview-trailer/` (nothing system-wide).

## What it does

1. **Transcribes** with word timestamps (Whisper large-v3-turbo; MLX on Apple Silicon,
   faster-whisper elsewhere) and flags hallucinated stretches.
2. **Picks the clips** that cover your bullets, respects exclusions, keeps a story arc and
   lands just under your duration limit.
3. **Cuts in real pauses** measured on the audio (Whisper's word times drift ~0.5 s), so no
   half-words leak in.
4. **Knows who is talking** by voice (Resemblyzer), not by guessing.
5. **Follows the speaker** in Zoom/Meet/Teams recordings: crops to their tile, shows full
   screen shares, repairs the black/half-drawn frames left by layout switches.
6. **Blurs private data** on screen shares (balances, amounts, percentages, card/account
   numbers, emails, names) using OCR.
7. **Subtitles** in the team style (Inter SemiBold, one color per speaker, no outline,
   translucent plate), then renders, normalises loudness to −16 LUFS, fades out, and QA-checks
   every cut before handing off.

## Defaults and overrides

Team defaults live in [`skills/interview-trailer/defaults.json`](skills/interview-trailer/defaults.json).
Override per project (`edl.json → "style"`), per folder (`style.json`) or per user
(`~/.config/interview-trailer/style.json`). Just ask the agent: "use white subtitles",
"no fade", "make it 90 seconds".

## Requirements and privacy

- Python 3.8+. First run downloads ~2–3 GB (Whisper model, PyTorch, OCR models, ffmpeg if
  missing, the Inter font).
- Everything runs locally; the video never leaves the machine.
- A 90-minute recording takes roughly 10–25 min to transcribe (hardware-dependent) and a few
  minutes to render.

## License

MIT
