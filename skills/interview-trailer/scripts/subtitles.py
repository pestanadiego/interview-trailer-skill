"""Build subtitle cues on the trailer timeline.

- words from the transcript, only those that belong to each (refined) clip
- speaker per word from work/speakers.json (cue breaks on every speaker change)
- automatic cleanup: fillers, comma-per-pause runs, number glue ("2 .5" -> "2.5"), clip-final
  punctuation, capitalised clip starts, straight quotes
- edl.json text fixes, applied phrase-level and re-timed by alignment:
    "text_fixes": [["find", "replace"], ["re:regex", "replace"], ["find", "replace", clip_index]]
    "word_fixes": {"<word start sec>": "new text or empty to drop"}
    "replace":    {"Jon": "John"}              (global, every clip)

usage: $PY subtitles.py --project P
writes: work/cues.json, work/cues.srt, work/review/cues.txt (numbered, with speaker + time)
"""
import argparse
import difflib
import re

from common import W, clip_words, edl, fmt_t, load_config, load_json, save_json, work_dir, words

END = re.compile(r"[.?!…]['\"»”]?$")


def spk_at(runs, t):
    if not runs:
        return None
    for a, b, k in runs:
        if a <= t < b:
            return k
    return runs[0][2] if t < runs[0][0] else runs[-1][2]


def realign(toks, new_text):
    """Map edited text back onto word timings."""
    old = [t[0] for t in toks]
    new = [x for x in new_text.split(" ") if x]
    out = []
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out += toks[i1:i2]
        elif tag == "replace":
            s, e = toks[i1][1], toks[i2 - 1][2]
            n = j2 - j1
            for k, word in enumerate(new[j1:j2]):
                out.append((word, s + (e - s) * k / n, s + (e - s) * (k + 1) / n, toks[i1][3]))
        elif tag == "insert":
            ref = toks[i1 - 1] if i1 > 0 else toks[min(i1, len(toks) - 1)]
            for word in new[j1:j2]:
                out.append((word, ref[2], ref[2], ref[3]))
    return out


def clean_clip(toks, ci, e, cfg):
    fillers = {f.lower() for f in cfg["transcription"]["fillers"]}
    wf = {round(float(k), 2): v for k, v in e.get("word_fixes", {}).items()}
    # 1) word-level fixes + fillers
    tmp = []
    for t in toks:
        key = round(t[4], 2)
        txt = wf.get(key, t[0])
        if not txt or re.sub(r"[^\w]", "", txt.lower()) in fillers:
            continue
        tmp.append((txt, t[1], t[2], t[3]))
    toks = tmp
    if not toks:
        return toks
    # 2) Whisper sometimes puts a comma at every pause: keep only commas before real pauses
    commas = sum(1 for t in toks if t[0].endswith(","))
    if commas > 0.3 * len(toks):
        toks = [(t[0][:-1] if t[0].endswith(",") and i + 1 < len(toks) and toks[i + 1][1] - t[2] < 0.45 else t[0],
                 t[1], t[2], t[3]) for i, t in enumerate(toks)]
    # 3) glue number fragments
    glued = []
    for t in toks:
        if glued and re.match(r"^[.,]?\d*%\W*$|^[.,]\d", t[0]) and re.search(r"\d$", glued[-1][0]):
            p = glued[-1]
            glued[-1] = (p[0] + t[0], p[1], t[2], p[3])
        else:
            glued.append(t)
    toks = glued
    # 4) text fixes (phrase level) + global replacements
    text = " ".join(t[0] for t in toks)
    new = text
    for fx in e.get("text_fixes", []):
        find, rep = fx[0], fx[1]
        if len(fx) > 2 and int(fx[2]) != ci:
            continue
        new = re.sub(find[3:], rep, new) if find.startswith("re:") else new.replace(find, rep)
    for k, v in e.get("replace", {}).items():
        new = new.replace(k, v)
    if cfg["style"].get("quotes") == "straight":
        new = re.sub(r"[«»“”]", '"', new)
    new = re.sub(r"\s+", " ", new).strip()
    if new != text:
        toks = realign(toks, new)
    if not toks:
        return toks
    # 5) capitalise the clip start, close the clip with punctuation
    first = toks[0]
    m = re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", first[0])
    if m:
        i = m.start()
        toks[0] = (first[0][:i] + first[0][i].upper() + first[0][i + 1:], *first[1:])
    last = toks[-1]
    if last[0].endswith((",", ";", ":")):
        toks[-1] = (last[0][:-1] + ".", *last[1:])
    elif re.search(r"\w$", last[0]):
        toks[-1] = (last[0] + ".", *last[1:])
    return toks


def chunk(toks, cend, max_chars):
    cues, cur = [], []
    for i, t in enumerate(toks):
        cur.append(t)
        text = " ".join(x[0] for x in cur)
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        gap = (nxt[1] - t[2]) if nxt else 99
        nxt_len = len(text) + 1 + len(nxt[0]) if nxt else 0
        end_sent = END.search(t[0]) is not None
        spk_change = nxt is not None and nxt[3] != t[3]
        if nxt and nxt_len > max_chars and not (end_sent or gap > 0.9 or spk_change):
            cut = None
            for j in range(len(cur) - 1, 0, -1):
                pre = " ".join(x[0] for x in cur[:j + 1])
                if re.search(r"[,.;:?!»\"]$", cur[j][0]) and len(pre) >= 30:
                    cut = j
                    break
            if cut is not None and cut < len(cur) - 1:
                head, cur = cur[:cut + 1], cur[cut + 1:]
                cues.append([head[0][1], head[-1][2], " ".join(x[0] for x in head), cend, head[0][3]])
                continue
        if not nxt or gap > 0.9 or spk_change or nxt_len > max_chars or (end_sent and len(text) > 28) \
                or (t[0].endswith(",") and len(text) > 60):
            cues.append([cur[0][1], t[2], text, cend, cur[0][3]])
            cur = []
    return cues


def wrap(text, line_max, lines):
    if len(text) <= line_max or lines < 2:
        return [text]
    words_ = text.split(" ")
    best = None
    for i in range(1, len(words_)):
        l1, l2 = " ".join(words_[:i]), " ".join(words_[i:])
        score = max(len(l1), len(l2)) - (5 if l1[-1] in ",.;:?!" else 0) + (100 if max(len(l1), len(l2)) > line_max + 4 else 0)
        if best is None or score < best[0]:
            best = (score, [l1, l2])
    return best[1]


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    a = ap.parse_args()
    work = work_dir(a.project)
    cfg = load_config(work)
    e = edl(work)
    st = cfg["style"]
    max_chars = st["max_line_chars"] * st["max_lines"] - 6
    clips = load_json(W(work, "clips.json"))["clips"]
    runs = load_json(W(work, "speakers.json"))
    ws = words(work)
    cues, off = [], 0.0
    for c in clips:
        a_, b_ = c["a"], c["b"]
        rr = runs.get(str(c["i"]), [[a_, b_, None]])
        toks = [(w["word"], max(w["start"], a_) - a_ + off, min(w["end"], b_) - a_ + off,
                 spk_at(rr, (max(w["start"], a_) + min(w["end"], b_)) / 2), w["start"])
                for w in ([ws[k] for k in c["words"]] if "words" in c else clip_words(ws, a_, b_))]
        toks = clean_clip(toks, c["i"], e, cfg)
        cues += chunk(toks, off + (b_ - a_), max_chars)
        off += b_ - a_
    merged = []
    for c in cues:  # fold tiny leftovers ("pues.") into the previous cue of the same clip/speaker
        if merged and len(c[2]) <= 8 and merged[-1][3] == c[3] and merged[-1][4] == c[4] \
                and len(merged[-1][2]) + len(c[2]) < max_chars + 10:
            merged[-1][1], merged[-1][2] = c[1], merged[-1][2] + " " + c[2]
        else:
            merged.append(c)
    for i, c in enumerate(merged):
        nxt = merged[i + 1][0] if i + 1 < len(merged) else c[3]
        c[1] = min(max(c[1] + 0.35, c[0] + 1.0), nxt, c[3])
    out = [{"start": round(c[0], 3), "end": round(c[1], 3), "lines": wrap(c[2], st["max_line_chars"], st["max_lines"]),
            "spk": c[4]} for c in merged if c[1] > c[0]]
    save_json(W(work, "cues.json"), {"total": round(off, 3), "cues": out})
    with open(W(work, "cues.srt"), "w", encoding="utf-8") as f:
        for i, c in enumerate(out, 1):
            f.write(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n" + "\n".join(c["lines"]) + "\n\n")
    rv = W(work, "review")
    rv.mkdir(parents=True, exist_ok=True)
    names = {k: v.get("name", k) for k, v in e.get("speakers", {}).items()}
    with open(rv / "cues.txt", "w", encoding="utf-8") as f:
        for i, c in enumerate(out, 1):
            f.write(f"{i:03d} [{fmt_t(c['start'])}] {names.get(c['spk'], c['spk'])}: {' / '.join(c['lines'])}\n")
    print(f"{len(out)} cues, total {fmt_t(off)} -> work/cues.json, work/cues.srt, work/review/cues.txt")


if __name__ == "__main__":
    main()
