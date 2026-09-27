---
name: interview-trailer
description: >-
  Turns a long interview, podcast, webinar or video-call recording (Zoom, Meet, Teams) into a
  finished, subtitled mini-trailer from ONE request: the user gives the video, bullet points of
  what to include (and exclude), and a duration limit; the skill transcribes, picks the clips,
  snaps every cut to a real pause, identifies speakers by voice, crops to whoever is talking,
  blurs sensitive numbers/names on screen shares, renders an MP4 + SRT and QA-checks it.
  Use when the user asks to "make a trailer / teaser / highlight reel / recap video from this
  interview", "cut the best clips about X", "mini-trailer", "hazme un tráiler de esta
  entrevista", "clips con subtítulos", "resumen en video de la llamada", or gives a recording
  plus topics and a length. Installs its own dependencies; works on macOS, Linux and Windows.
---

# interview-trailer

Goal: **one prompt in, a finished video out.** The user should need at most 2–3 rounds of
feedback. Work autonomously end to end: do not ask before installing, do not ask about
style, do not stop to confirm the clip list. Ask only if the video file cannot be found.
Every render is a new version (`-v1`, `-v2`, …); never overwrite a previous one.

## Inputs (infer everything you can)

| input | required | default |
|---|---|---|
| video file | yes | none |
| include bullets (topics, claims, benefits, story points) | yes | none |
| exclude bullets (topics/people/segments that must NOT appear) | no | none |
| duration limit ("5–10 min", "under 3 min") | no | 2–3 min |
| style overrides (font, colors, fade, loudness…) | no | `defaults.json` (team default) |
| language | no | auto-detected |

No intro, titles, music or lower-thirds unless asked. Subtitles are always on.

## Setup (automatic, no questions)

```bash
python3 <skill_dir>/scripts/bootstrap.py      # prints PY=/path/to/python ; idempotent
```
It builds a private environment in `~/.cache/interview-trailer/` (uv or venv), installs
Whisper (MLX on Apple Silicon, faster-whisper elsewhere), OpenCV, Resemblyzer, RapidOCR,
uses the system ffmpeg if it has the needed filters or downloads a static build, and fetches
the Inter font. Use that `PY` for every script below. `S=<skill_dir>/scripts`.

Project folder: `<video dir>/<video-stem>-trailer/` (call it `P`). All intermediates go to
`P/work/`; review images to `P/work/review/`; outputs `P/<name>-vN.mp4` + `.srt`.

Long steps (transcribe, plan_frames, render) can exceed a tool timeout: run them in the
background and wait for completion instead of polling in a loop.

## Pipeline

### 1. Transcribe
```bash
$PY $S/transcribe.py --project P --src VIDEO
```
Writes `work/transcript.txt` (timestamped segments) and `work/hallucinations.json`.
Never use a flagged range for a clip or a subtitle (Whisper invents loops like "era como que ×40").

### 2. Read the whole transcript, then choose clips
Read `work/transcript.txt` completely (in chunks). Identify who is who from the content
(introductions, names said aloud). Then select clips:

- **Coverage:** every include-bullet gets at least one strong clip, in the speakers' own words.
  Also add 1–2 clips the user would obviously want (e.g. who the company is / its history)
  when they fit the brief.
- **Exclusions are semantic:** drop clips that mention an excluded topic even indirectly
  (e.g. "don't mention the target customer" → drop every segment naming a customer
  segment, persona or industry the company sells to).
- **Story arc:** what it is → origin/history → how it works → benefits (one topic per block,
  strongest first) → proof/speed/trust → forward-looking close. The close lets the last
  speaker finish the sentence; the render adds the fade.
- **Budget:** total 85–97 % of the duration limit. First drafts always run long: trim filler
  inside clips (split one clip into two around a digression) before dropping good topics.
- **Clean boundaries:** start on the first wanted word, end on the last wanted word: whole
  sentences, no dangling "y…", no half-words. Use `words.py` for exact word times:
  `$PY $S/words.py --project P 9:05 9:40` or `--find "pricing"`.
- **Flag, don't hide:** offers made only to the live audience ("we'll give you all…"),
  profanity, third-party company names, and on-screen private data. Include offers only if
  they clearly serve the brief, and mention them in the handoff.
- Avoid clips where the speaker is the host introducing themselves or other attendees.

### 3. Speakers and framing references
- `voice_refs`: for each speaker, 3+ ranges (≥30 s total) where only they talk.
- Video calls: find a gallery-view moment (`$PY $S/frames.py --project P --src VIDEO --every 300`
  for an overview, then `--grid <t>`), read each speaker's tile rectangle in source pixels
  from the grid image → `tile_ref`. Single-camera sources: set `"framing": {"mode": "full-frame"}`.

Write `P/edl.json` (schema and example in `references/edl.md`).

### 4. Refine cuts
```bash
$PY $S/refine.py --project P
```
Snaps every boundary to the real pause next to the wanted words and to the frame grid. Read
the report: `[words outside] | first words … last words | [words outside]`. If a wanted word
fell outside or an unwanted one is inside, fix that clip's `start`/`end` and rerun.
Check `TOTAL` against the budget.

### 5. Speakers by voice
```bash
$PY $S/speakers.py --project P
```
Prints who talks when inside each clip. Changes are placed at the word break where the
voice switches, accurate to about one word; crosstalk can blur it. Sanity-check every
mid-clip change against the content (a question vs. the answer). Fix with
`speaker_overrides` or a clip-level `speaker`. If the reference similarity is > 0.85,
pick cleaner `voice_refs`.

### 6. Plan framing (background, ~1 min per trailer minute)
```bash
$PY $S/plan_frames.py --project P
```
Look at `work/review/segments.jpg`. Every frame must show only the speaking person
(or the whole shared screen). Read the LIMITATION lines, e.g. "B is talking but the
recording only shows A" happens when the call recording pinned one person. That footage
does not exist: keep it, and report it; never fake lip-sync with footage from another moment.

### 7. Redact screen shares
```bash
$PY $S/redact.py --project P
```
Look at `work/review/redact.jpg`. Blurred by default: money, percentages, card last-4 and long
account numbers, emails, names in person/customer columns, right-aligned amount columns.
Keep demo inputs readable with `redact.keep`; add `redact.names` / `redact.extra` for misses.
Rerun until nothing private is readable and nothing needed is blurred.

### 8. Subtitles
```bash
$PY $S/subtitles.py --project P
```
Read ALL of `work/review/cues.txt` and fix the text with `text_fixes` / `word_fixes` / `replace`:
punctuation and capitals Whisper dropped, proper names (use the spelling the people/company
use; list your assumptions in the handoff), numbers ("2.5%", "1.600 millones"), light grammar
fixes that keep the speaker's rhythm ("it grew... or more"), no fillers. Quotes follow
`style.quotes` (team default: straight `"`). Rerun until clean.

### 9. Render and QA (background)
```bash
$PY $S/render.py --project P
$PY $S/qa.py --project P
```
Look at every QA image: `qa_<version>_segments.jpg` (one frame per shot: right person? whole
screen? blur covering the data?) and each `qa_<version>_cuts_<k>.jpg` (frame strips at every
cut: no black or half-drawn frames, no participant grid flashes). Confirm lengths match,
loudness ≈ target, no black frames before the ending fade. If anything fails, fix and render
again (it becomes the next version) before handing off.

## Handoff (always this shape)

1. Paths of the MP4 and SRT, final length vs. the limit.
2. The trailer's structure: blocks with output timestamps and what each covers.
3. What changed since the last version (for follow-ups).
4. Limitations: speaker off-camera, footage missing, anything you could not do.
5. Risky content to review before sharing: audience-specific offers, on-screen data you left
   readable on purpose, strong claims, profanity.
6. Assumptions: name spellings, speaker→color mapping.

## Follow-up requests → where to change

| feedback | change |
|---|---|
| wrong word / spelling / punctuation in a subtitle | `text_fixes` (or `word_fixes`), rerun 8→9 |
| a clip starts/ends at the wrong word, a stray word leaks | clip `start`/`end`, rerun 4→9 |
| add/remove/reorder topics, too long/short | `clips`, rerun 4→9 |
| wrong person on screen / wrong color | `speaker_overrides`, rerun 5→9 |
| crop cuts part of the screen / participants visible | `tile_ref`, framing, rerun 6→9 |
| a black/odd frame at some time | check `qa_*_cuts` images; rerun 6→9 (auto-repaired) |
| blur too much/too little | `redact.keep` / `names` / `extra`, rerun 7→9 |
| font, colors, outline, fade, loudness | `style` in `edl.json`, rerun 9 |

## Style (team default, overridable)

`defaults.json` → `style`: Inter SemiBold 46 px, one color per speaker (`#CEF58E`, `#FFA5CB`,
then `#9AD7FF`, `#FFD27A`), no outline, translucent dark plate behind each line, ≤2 lines ×
42 chars, straight quotes, 1.5 s fade-out ending, −16 LUFS, 1920×1080.
Override per project in `edl.json` → `"style": {...}`, per user in
`~/.config/interview-trailer/style.json`, or per folder in `P/style.json`.

## Hard-won rules (do not relearn them)

- Whisper word times drift ~0.5 s next to pauses in both directions → never cut on raw word
  times; `refine.py` cuts in the measured pause and keeps only words that are really audible.
- Keep clips a whole number of frames long or subtitles drift (the scripts do this).
- Identify speakers by voice. Content guesses and the call layout both lie (layouts pin people).
- Call-software galleries have no gaps between tiles: find tiles by matching each speaker's
  reference image, not by detecting shapes. Crop ~4.5 % inside the tile to hide name labels.
- Screen shares: crop to the low-saturation product UI (thumbnails are colorful), trim black
  letterbox rows, never crop to a popup; show the whole screen.
- Layout switches leave black or half-drawn frames at segment edges: they are detected at full
  frame rate and replaced by clones of the neighbouring good frame (length unchanged).
- Blur in full-range RGB with a real downscale-blur: a mask merged in limited-range YUV
  leaves a readable ghost of the text. Keep blur boxes off black borders.
- Subtitles are drawn as PNG overlays (works with every ffmpeg build; many lack libass/drawtext).
- Whisper punctuates well only with a punctuated prompt and context carried between windows
  (`transcribe.py` does both, with a silence-hallucination guard). Still read every cue.
- OCR the same screen across frames counts each on-screen item once; a table needs a real
  header row (4+ titles) before a whole column is blurred, otherwise a form label only blurs its value.
- Let the last speaker finish; the fade starts after the final word, never on it.
