"""Who is talking, when: by voice (Resemblyzer embeddings), not by guesswork or Zoom layout.

usage: $PY speakers.py --project P
reads: edl.json  speakers.{ID}.voice_refs = [[start, end], ...]   (clean solo speech, >= ~30 s total each)
       edl.json  speaker_overrides = [[start, end, ID], ...]      (optional manual fixes, source times)
       work/clips.json, work/audio.wav
writes: work/speakers.json  {clip_index: [[t0, t1, ID], ...]}  (source times)
"""
import argparse
import warnings

import numpy as np
import soundfile as sf

from common import W, edl, fmt_t, load_json, parse_t, save_json, work_dir, words

warnings.filterwarnings("ignore")
WIN, HOP, MIN_RUN = 1.5, 0.25, 1.6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    a = ap.parse_args()
    work = work_dir(a.project)
    e = edl(work)
    clips = load_json(W(work, "clips.json"))["clips"]
    ws = words(work)
    spk = e.get("speakers", {})
    ids = list(spk)
    overrides = [(parse_t(s), parse_t(t), who) for s, t, who in e.get("speaker_overrides", [])]
    result = {}
    if len(ids) <= 1:
        only = ids[0] if ids else "S1"
        for c in clips:
            result[str(c["i"])] = [[c["a"], c["b"], only]]
        save_json(W(work, "speakers.json"), result)
        print(f"single speaker '{only}' for all clips")
        return

    from resemblyzer import VoiceEncoder
    wav, sr = sf.read(str(W(work, "audio.wav")), dtype="float32")
    enc = VoiceEncoder("cpu", verbose=False)

    def emb(s, t):
        return enc.embed_utterance(wav[int(s * sr):int(t * sr)])

    refs = {}
    for k in ids:
        rr = spk[k].get("voice_refs") or []
        if not rr:
            raise SystemExit(f"speaker {k} has no voice_refs in edl.json")
        v = np.mean([emb(parse_t(s), parse_t(t)) for s, t in rr], axis=0)
        refs[k] = v / np.linalg.norm(v)
    sims = {f"{x}-{y}": round(float(refs[x] @ refs[y]), 3) for i, x in enumerate(ids) for y in ids[i + 1:]}
    print("reference similarity:", sims, "(>0.85 means the refs are too alike; pick cleaner ranges)")

    for c in clips:
        s0, s1 = c["a"], c["b"]
        cen, lab = [], []
        t = s0
        while t + 0.5 <= s1:
            v = emb(t, min(t + WIN, s1))
            sc = sorted(((float(v @ refs[k]), k) for k in ids), reverse=True)
            cen.append(t + min(WIN, s1 - t) / 2)
            lab.append(sc[0][1] if sc[0][0] - sc[1][0] > 0.03 else None)
            t += HOP
        # fill unsure windows from neighbours, then majority-smooth
        for i in range(len(lab)):
            if lab[i] is None:
                lab[i] = next((lab[j] for j in range(i - 1, -1, -1) if lab[j]), None) or \
                         next((lab[j] for j in range(i + 1, len(lab)) if lab[j]), ids[0])
        sm = []
        for i in range(len(lab)):
            win = lab[max(0, i - 2):i + 3]
            sm.append(max(set(win), key=win.count))
        runs = []
        for i, k in enumerate(sm):
            if runs and runs[-1][2] == k:
                continue
            tb = s0 if not runs else (cen[i - 1] + cen[i]) / 2
            if runs:
                runs[-1][1] = tb
            runs.append([tb, s1, k])
        if not runs:
            runs = [[s0, s1, c.get("speaker") or ids[0]]]
        # absorb blips shorter than MIN_RUN
        merged = []
        for r in runs:
            if merged and (r[1] - r[0] < MIN_RUN):
                merged[-1][1] = r[1]
            elif merged and merged[-1][2] == r[2]:
                merged[-1][1] = r[1]
            else:
                merged.append(r)
        if len(merged) > 1 and merged[0][1] - merged[0][0] < MIN_RUN:
            merged[1][0] = merged[0][0]
            merged.pop(0)
        # voice windows are ~1.5 s wide and smoothing delays changes: re-place every change at the
        # word break where the voice really switches (short before/after embeddings), sentence ends preferred
        cw = [ws[k] for k in c.get("words", [])]
        breaks = []
        for w1, w2 in zip(cw, cw[1:]):
            bonus = 0.08 if w1["word"].rstrip('"»').endswith((".", "?", "!")) else \
                    0.04 if w1["word"].endswith((",", ";", ":")) else 0.0
            breaks.append(((w1["end"] + w2["start"]) / 2, bonus))
        for j in range(1, len(merged)):
            A, B, tb = merged[j - 1][2], merged[j][2], merged[j][0]
            lo, hi = merged[j - 1][0] + 0.4, merged[j][1] - 0.4
            best = None
            for p, bonus in breaks:
                if not (lo < p < hi and abs(p - tb) <= 2.5):
                    continue
                vb, va = emb(max(s0, p - 1.0), p), emb(p, min(s1, p + 1.0))
                d = float(vb @ refs[A] - vb @ refs[B]) + float(va @ refs[B] - va @ refs[A]) + bonus
                if best is None or d > best[0]:
                    best = (d, p)
            if best:
                merged[j - 1][1] = merged[j][0] = best[1]
        if c.get("speaker"):              # clip-level override
            merged = [[s0, s1, c["speaker"]]]
        for os_, ot, who in overrides:    # range overrides
            new = []
            for r in merged:
                if r[1] <= os_ or r[0] >= ot:
                    new.append(r)
                    continue
                if r[0] < os_:
                    new.append([r[0], os_, r[2]])
                new.append([max(r[0], os_), min(r[1], ot), who])
                if r[1] > ot:
                    new.append([ot, r[1], r[2]])
            merged = new
        merged = [[round(x, 3), round(y, 3), k] for x, y, k in merged if y - x > 0.01]
        result[str(c["i"])] = merged
        desc = " | ".join(f"{k} {fmt_t(x)}-{fmt_t(y)}" for x, y, k in merged)
        print(f"#{c['i']:02d} {c.get('note', '')[:34]:34s} {desc}")
    save_json(W(work, "speakers.json"), result)


if __name__ == "__main__":
    main()
