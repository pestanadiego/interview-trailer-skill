"""Snap every clip boundary to a real pause in the audio, then to the frame grid.

Whisper word timestamps drift up to ~0.5 s around pauses, so cutting on them leaks
half-words ("pero" -> "bro"). This finds the nearest silence gap (>= min_gap) to each
requested boundary and cuts just before speech resumes / just after it stops.

usage: $PY refine.py --project P
reads: edl.json (clips: start/end in seconds or H:MM:SS; optional "exact": true), work/audio.wav
writes: work/clips.json  + prints a boundary report (words just inside/outside every cut)
"""
import argparse

import numpy as np
import soundfile as sf

from common import W, edl, fmt_t, frame_align, load_config, parse_t, probe, save_json, work_dir, words

HOP = 0.01


def envelope(wav_path):
    x, sr = sf.read(str(wav_path), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    h = int(sr * HOP)
    n = len(x) // h
    rms = np.sqrt((x[: n * h].reshape(n, h) ** 2).mean(axis=1))
    return rms


def gaps(rms, min_gap):
    thr = 0.12 * float(np.percentile(rms, 95))
    silent = rms < thr
    out, start = [], None
    for i, s in enumerate(np.append(silent, False)):
        if s and start is None:
            start = i
        elif not s and start is not None:
            if (i - start) * HOP >= min_gap:
                out.append((start * HOP, i * HOP))
            start = None
    return out


def snap_start(t, gl, ws, lead):
    """Cut in the pause between the previous word and the first wanted word.
    Whisper misplaces words next to pauses in BOTH directions, so anchor on words, not on t:
    a usable gap ends after the previous word and starts before the first word is half over."""
    first = next((i for i, w in enumerate(ws) if w["start"] >= t - 0.03), None)
    if first is None:
        return t, None
    F = ws[first]
    P_end = ws[first - 1]["end"] if first > 0 else -1.0
    cands = [g for g in gl if g[1] > P_end - 0.1 and g[0] < F["start"] + 0.5 * (F["end"] - F["start"])]
    if not cands:
        return t, None
    g = max(cands, key=lambda g: g[1])          # the pause closest to the wanted word
    return max(g[0], g[1] - lead), g


def snap_end(t, gl, ws, tail):
    """Cut in the pause after the last wanted word, only if it starts before the next word does."""
    last = max((i for i, w in enumerate(ws) if w["end"] <= t + 0.03), default=None)
    if last is None:
        return t, None
    L = ws[last]
    N_start = ws[last + 1]["start"] if last + 1 < len(ws) else float("inf")
    cands = [g for g in gl if L["start"] + 0.5 * (L["end"] - L["start"]) < g[0] < N_start + 0.03]
    if not cands:
        return t, None
    g = min(cands, key=lambda g: g[0])          # the first pause after the last wanted word
    return min(g[1], g[0] + tail), g


def keep_words(ws, a, b, g_start, g_end):
    """Indices of transcript words that are really audible inside [a, b].
    At a boundary snapped to a pause, trust the pause; otherwise require most of the word inside."""
    keep = []
    for i, w in enumerate(ws):
        if w["end"] <= a or w["start"] >= b:
            continue
        dur = max(0.05, w["end"] - w["start"])
        if w["start"] < a:
            if g_start is not None:
                if w["end"] <= a + 0.12:
                    continue
            elif (w["end"] - a) / dur < 0.5:
                continue
        if w["end"] > b - 0.02 or (g_end is not None and w["start"] >= g_end[0] - 0.1):
            if g_end is not None:
                if w["start"] >= g_end[0] - 0.1:
                    continue
            elif (b - w["start"]) / dur < 0.5:
                continue
        keep.append(i)
    return keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    a = ap.parse_args()
    work = work_dir(a.project)
    full_cfg = load_config(work)
    cfg = full_cfg["cuts"]
    fade = float(full_cfg["style"].get("end_fade_seconds") or 0)
    e = edl(work)
    fps = probe(e["source"])["fps"]
    rms = envelope(W(work, "audio.wav"))
    gl = gaps(rms, cfg["min_gap"])
    ws = words(work)
    out, total = [], 0.0
    for i, c in enumerate(e["clips"]):
        s0, e0 = parse_t(c["start"]), parse_t(c["end"])
        if c.get("exact"):
            s1, e1, g1, g2 = s0, e0, None, None
        else:
            s1, g1 = snap_start(s0, gl, ws, cfg["lead_in"])
            e1, g2 = snap_end(e0, gl, ws, cfg["tail_out"])
        if i == len(e["clips"]) - 1 and fade > 0 and e1 < e0:
            # the ending fades out: keep the requested tail of silence so the fade never eats the last word
            nxt = next((w["start"] for w in ws if w["start"] > e1 + 0.05), float("inf"))
            e1 = min(e0, nxt - 0.15)
        ok1, ok2 = g1 is not None or c.get("exact"), g2 is not None or c.get("exact")
        s1, e1 = frame_align(s1, e1, fps)
        kept = keep_words(ws, s1, e1, g1, g2)
        inside = [ws[k] for k in kept]
        before = [ws[k] for k in range(max(0, (kept[0] if kept else 0) - 2), kept[0] if kept else 0)]
        after = [ws[k] for k in range((kept[-1] + 1) if kept else 0, ((kept[-1] + 3) if kept else 0))]
        out.append({"i": i, "a": round(s1, 3), "b": round(e1, 3), "note": c.get("note", ""),
                    "speaker": c.get("speaker"), "words": kept})
        total += e1 - s1
        flag = "" if (ok1 and ok2) else "  (no pause found near " + ("start" if not ok1 else "end") + ")"
        print(f"#{i:02d} {fmt_t(s1)}-{fmt_t(e1)} ({e1 - s1:5.1f}s) "
              f"[{' '.join(w['word'] for w in before)}] | {' '.join(w['word'] for w in inside[:4])} ... "
              f"{' '.join(w['word'] for w in inside[-4:])} | [{' '.join(w['word'] for w in after)}]"
              f"  start {s1 - s0:+.2f}s end {e1 - e0:+.2f}s{flag}")
    save_json(W(work, "clips.json"), {"fps": fps, "total": round(total, 3), "clips": out})
    print(f"TOTAL {total:.1f}s ({int(total // 60)}:{total % 60:04.1f}) in {len(out)} clips -> work/clips.json")


if __name__ == "__main__":
    main()
