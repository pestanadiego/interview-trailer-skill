"""Transcribe the source with word timestamps; flag Whisper hallucinations.

usage: $PY transcribe.py --project <dir> --src <video> [--lang es] [--model large-v3-turbo]
writes: work/audio.wav, work/transcript.json, work/transcript.txt, work/hallucinations.json
"""
import argparse
import os
import re
import sys
from collections import Counter

from common import W, env, ff, fmt_t, load_config, probe, save_json, work_dir

MLX_REPOS = {"large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
             "large-v3": "mlx-community/whisper-large-v3-mlx",
             "medium": "mlx-community/whisper-medium-mlx"}


def extract_audio(src, wav):
    if not wav.exists():
        ff(["-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])


def detect_language(backend, wav, model):
    """Detect from the first ~60 s (cheap)."""
    probe_wav = wav.parent / "lang_probe.wav"
    ff(["-i", wav, "-t", "60", probe_wav])
    if backend == "mlx":
        import mlx_whisper
        r = mlx_whisper.transcribe(str(probe_wav), path_or_hf_repo=MLX_REPOS.get(model, model), verbose=None)
        return r.get("language", "en")
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="auto", compute_type="auto")
    _, info = m.transcribe(str(probe_wav))
    return info.language


def run_mlx(wav, model, lang, prompt):
    import mlx_whisper
    r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=MLX_REPOS.get(model, model), language=lang,
                               word_timestamps=True, condition_on_previous_text=True,
                               initial_prompt=prompt, hallucination_silence_threshold=2.0, verbose=None)
    segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip(),
             "words": [{"word": w["word"], "start": w["start"], "end": w["end"]} for w in s.get("words", [])]}
            for s in r["segments"]]
    return r.get("language", lang), segs


def run_faster(wav, model, lang, prompt):
    from faster_whisper import WhisperModel
    try:
        import torch  # noqa: F401
        cuda = torch.cuda.is_available()
    except Exception:  # noqa: BLE001
        cuda = False
    m = WhisperModel(model, device="cuda" if cuda else "cpu", compute_type="float16" if cuda else "int8")
    it, info = m.transcribe(str(wav), language=lang, word_timestamps=True, vad_filter=True,
                            condition_on_previous_text=True, initial_prompt=prompt,
                            hallucination_silence_threshold=2.0)
    segs = []
    for s in it:
        segs.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                     "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (s.words or [])]})
        print(f"\r  transcribed to {fmt_t(s.end)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return info.language, segs


def hallucinations(segs):
    """Ranges Whisper made up: n-gram loops, stacks of zero-length words, runaway repeats."""
    flags = []
    for s in segs:
        toks = re.findall(r"\w+", s["text"].lower())
        for n in (1, 2, 3, 4):
            grams = [" ".join(toks[i:i + n]) for i in range(0, max(0, len(toks) - n + 1))]
            if grams:
                g, c = Counter(grams).most_common(1)[0]
                if c >= 6 and c * n >= 0.5 * len(toks):
                    flags.append({"start": s["start"], "end": s["end"], "why": f"'{g}' x{c}"})
                    break
        zero = sum(1 for w in s["words"] if w["end"] - w["start"] < 0.02)
        if zero >= 5:
            flags.append({"start": s["start"], "end": s["end"], "why": f"{zero} zero-length words"})
    merged = []
    for f in sorted(flags, key=lambda x: x["start"]):
        if merged and f["start"] <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(merged[-1]["end"], f["end"])
        else:
            merged.append(dict(f))
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--lang")
    ap.add_argument("--model")
    a = ap.parse_args()
    work = work_dir(a.project)
    cfg = load_config(work)
    model = a.model or cfg["transcription"]["model"]
    backend = os.environ.get("INTERVIEW_TRAILER_WHISPER") or env().get("whisper_backend", "faster-whisper")
    wav = W(work, "audio.wav")
    info = probe(a.src)
    extract_audio(a.src, wav)
    lang = a.lang or detect_language(backend, wav, model)
    prompt = cfg["transcription"]["initial_prompts"].get(lang)
    print(f"backend={backend} model={model} lang={lang} duration={fmt_t(info['duration'])}", file=sys.stderr)
    lang, segs = (run_mlx if backend == "mlx" else run_faster)(wav, model, lang, prompt)
    save_json(W(work, "transcript.json"), {"language": lang, "backend": backend, "model": model,
                                          "source": str(a.src), "segments": segs})
    with open(W(work, "transcript.txt"), "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"[{fmt_t(s['start'])} - {fmt_t(s['end'])}] {s['text']}\n")
    hz = hallucinations(segs)
    save_json(W(work, "hallucinations.json"), hz)
    print(f"{len(segs)} segments -> work/transcript.txt ; {len(hz)} suspicious ranges -> work/hallucinations.json")
    for h in hz:
        print(f"  SKIP {fmt_t(h['start'])}-{fmt_t(h['end'])}: {h['why']}")


if __name__ == "__main__":
    main()
