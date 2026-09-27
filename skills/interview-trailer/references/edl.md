# edl.json: the edit decision list

One file drives the whole pipeline. It lives at `<project>/edl.json`. Times are seconds
(float) or `"H:MM:SS.ss"` strings, always in the SOURCE video's timeline.

```jsonc
{
  "source": "/abs/path/interview.mp4",
  "output_name": "acme-ceo-trailer",          // optional; default "<video-stem>-trailer"
  "language": "es",                           // optional; detected by transcribe.py

  "speakers": {                               // short IDs; order = color order (defaults.json)
    "H": {
      "name": "Host",
      "voice_refs": [[534, 577], [2663, 2712], [3683, 3720]],   // solo speech, >= ~30 s total
      "tile_ref": {"t": 600, "rect": [1052, 10, 752, 422]},       // their camera tile, source px
      "color": "#CEF58E"                                          // optional override
    },
    "G": {
      "name": "Guest",
      "voice_refs": [[668, 692], [772, 802], [3101, 3147]],
      "tile_ref": {"t": 600, "rect": [300, 10, 752, 422]}
    }
  },

  "framing": {"mode": "follow-speaker"},      // or "full-frame" (single camera / podcast)

  "clips": [                                  // in TRAILER order (not source order)
    {"start": 534.70, "end": 577.75, "note": "what the company does"},
    {"start": 582.50, "end": 593.60, "note": "headline metric"},
    {"start": 5349.05, "end": 5360.60, "note": "closing line (fade)"},
    {"start": 100.0, "end": 104.2, "note": "mid-phrase cut on purpose", "exact": true},
    {"start": 200.0, "end": 210.0, "note": "force one speaker", "speaker": "G"}
  ],

  "speaker_overrides": [[3157.6, 3163.7, "G"]],   // optional, source times

  // ---- subtitle text ----
  "text_fixes": [                             // applied to each clip's joined text, then re-timed
    ["para quien no conocen", "para quien no conoce"],
    ["re:\\bestoy uno de\\b", "soy uno de"],   // "re:" prefix = regex
    ["and I told:", "and I told him:", 8]          // optional 3rd item = only in clip index 8
  ],
  "word_fixes": {"544.42": "conoce", "673.04": ""},   // by word START time (see words.py); "" drops
  "replace": {"Jon": "John"},            // global, every clip

  // ---- screen-share redaction ----
  "redact": {
    "keep": ["^Limit", "Weekly", "^\\$2,000(\\.00)?$"],   // regexes that must stay readable (demo inputs)
    "names": ["Jane Doe"],              // extra names to blur wherever OCR finds them
    "extra": {"27": [[1400, 250, 220, 660]]}  // segment index -> extra [x,y,w,h] boxes (crop px)
  },

  // ---- style overrides (anything in defaults.json "style") ----
  "style": {"end_fade_seconds": 1.5}
}
```

## Field rules

- `clips[].start` = the first wanted word's start, `end` = the last wanted word's end
  (read them with `words.py`). `refine.py` then moves each cut into the real pause; Whisper
  timestamps drift up to ~0.5 s around pauses in both directions.
- `exact: true` skips the pause snapping for that clip (rare; use when cutting mid-phrase on purpose).
- `speaker` on a clip forces one speaker for the whole clip.
- `voice_refs`: 3+ ranges where ONLY that person talks (long answers, the intro monologue).
  Avoid ranges with crosstalk, laughter or screen-share audio.
- `tile_ref`: pick a gallery moment with `frames.py --grid <t>`; the rect is that person's whole
  camera tile in source pixels. Needed only for `follow-speaker`.
- Fix precedence inside a clip: `word_fixes` -> automatic cleanup -> `text_fixes` -> `replace` -> quote style.
