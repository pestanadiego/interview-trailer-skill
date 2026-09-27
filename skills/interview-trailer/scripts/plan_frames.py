"""Plan what is on screen for every moment of the trailer.

follow-speaker mode (video calls): crop to the tile of whoever is talking; screen shares
show the whole shared screen (never the side strip of participants); a pinned full-screen
camera is used as-is. Frames where the call software switches layouts (black / half-drawn)
are detected at full frame rate and replaced by clones of the nearest good frame.
full-frame mode (single camera, podcasts): the whole frame, black borders trimmed.

usage: $PY plan_frames.py --project P
reads: edl.json (source, framing, speakers.{ID}.tile_ref = {"t": sec, "rect": [x,y,w,h]} in source px),
       work/clips.json, work/speakers.json
writes: work/segments.json, work/review/segments.jpg (one labelled frame per segment)
"""
import argparse
import statistics
import subprocess

import cv2
import numpy as np
from PIL import Image, ImageDraw

from common import W, edl, env, fmt_t, load_config, load_json, parse_t, probe, save_json, work_dir


# ------------------------------------------------------------------ helpers
def read_frames(src, t0, dur, width, height, fps=None, crop=None):
    vf = []
    if crop:
        x, y, w, h = crop
        vf.append(f"crop={w}:{h}:{x}:{y}")
    if fps:
        vf.append(f"fps={fps}")
    vf.append(f"scale={width}:{height}")
    cmd = [env()["ffmpeg"], "-hide_banner", "-loglevel", "error", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
           "-i", str(src), "-vf", ",".join(vf), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3)


def components(img):
    m = (img.max(axis=2) > 14).astype(np.uint8)
    _, _, st, _ = cv2.connectedComponentsWithStats(m, 8)
    comps = [tuple(int(v) for v in s[:4]) for s in st[1:] if s[4] > 0.002 * img.shape[0] * img.shape[1]]
    return sorted(comps, key=lambda r: -r[2] * r[3])


def longest_run(flags):
    best, cur = (0, 0), None
    for i, v in enumerate(list(flags) + [False]):
        if v and cur is None:
            cur = i
        elif not v and cur is not None:
            if i - cur > best[1] - best[0]:
                best = (cur, i)
            cur = None
    return best


class Analyzer:
    def __init__(self, refs, sw, sh):
        self.refs, self.sw, self.sh = refs, sw, sh
        self.cache = {}   # (bbox, speaker) -> tile width that matched last time (layouts are stable)

    def match(self, img, k, tw):
        ref = self.refs[k]
        th = int(round(tw * ref.shape[0] / ref.shape[1]))
        if tw < 40 or th >= img.shape[0] or tw >= img.shape[1]:
            return -1.0, None
        t = cv2.resize(ref, (tw, th), interpolation=cv2.INTER_AREA)
        r = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        return float(mx), (loc[0], loc[1], tw, th)

    def share_rect(self, img, box):
        x, y, w, h = box
        reg = img[y:y + h, x:x + w]
        sat = cv2.cvtColor(reg, cv2.COLOR_BGR2HSV)[:, :, 1].mean(axis=0)
        val = reg.max(axis=2).mean(axis=0)
        c0, c1 = longest_run([(s < 45 and v > 60) for s, v in zip(sat, val)])
        return (x + c0, y, max(1, c1 - c0), h)

    def classify(self, img, full=True):
        if img.max() < 30:
            return {"layout": "black"}
        comps = components(img)
        if not comps:
            return {"layout": "black"}
        x, y, w, h = comps[0]
        if float(img[y:y + h, x:x + w].mean()) > 150:
            return {"layout": "share", "rect": self.share_rect(img, comps[0]) if full else None}
        big = [c for c in comps if c[2] * c[3] > 0.02 * self.sw * self.sh]
        one_block = len(big) == 1 and w > 0.6 * self.sw
        if one_block:
            if not self.refs:
                return {"layout": "single", "rect": (x, y, w, h)}
            best = max(((self.match(img, k, w - 2)[0], k) for k in self.refs), default=(-1, None))
            if best[0] > 0.45:
                return {"layout": f"full:{best[1]}", "rect": (x, y, w, h)}
            # call software draws gallery tiles edge to edge, so a gallery is ONE block too
        res = {"layout": "gallery", "bbox": (x, y, w, h)}
        if full:
            key = (x // 8, w // 8)
            for k in self.refs:
                best, best_n = (-1.0, None), None
                n0 = self.cache.get((key, k))          # grid column count that matched before
                order = ([n0] if n0 else []) + [n for n in range(2, 7) if n != n0]
                for n in order:
                    for tw in (w // n - 1, w // n, w // n + 1):
                        m = self.match(img, k, tw)
                        if m[0] > best[0]:
                            best, best_n = m, n
                    if n == n0 and best[0] >= 0.5:
                        break                          # same layout as before: no need to search
                if best_n and best[0] >= 0.5:
                    self.cache[(key, k)] = best_n
                res[k] = best
            if one_block and all(res[k][0] < 0.45 for k in self.refs):
                return {"layout": "single", "rect": (x, y, w, h)}
        return res


def kind_of(layout):
    if layout == "share":
        return "share"
    if layout.startswith("full:") or layout == "single":
        return "full"
    return layout  # gallery / black


def trim_black(src, t, crop):
    x, y, w, h = crop
    a = read_frames(src, t, 0.2, w, h, crop=crop)[0].astype(float).mean(axis=2)
    rows, cols = a.mean(axis=1), a.mean(axis=0)
    top, bot, left, right = 0, h, 0, w
    while bot - top > 10 and rows[bot - 1] < 40:
        bot -= 1
    while bot - top > 10 and rows[top] < 40:
        top += 1
    while right - left > 10 and cols[right - 1] < 40:
        right -= 1
    while right - left > 10 and cols[left] < 40:
        left += 1
    nx, ny = x + left + (2 if left else 0), y + top + (2 if top else 0)
    nw = (right - left - (2 if left else 0) - (2 if right < w else 0)) // 2 * 2
    nh = (bot - top - (2 if top else 0) - (2 if bot < h else 0)) // 2 * 2
    return [nx, ny, nw, nh]


def shrink(rect, s, sw_scale, W_, H_):
    x, y, w, h = [v * sw_scale for v in rect]
    dx, dy = w * s, h * s
    x, y, w, h = x + dx, y + dy, w - 2 * dx, h - 2 * dy
    x, y = max(0, x), max(0, y)
    w, h = min(w, W_ - x), min(h, H_ - y)
    return [int(x) // 2 * 2, int(y) // 2 * 2, int(w) // 2 * 2, int(h) // 2 * 2]


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    a = ap.parse_args()
    work = work_dir(a.project)
    cfg = load_config(work)["framing"]
    e = edl(work)
    src = e["source"]
    info = probe(src)
    SW_ = cfg["sample_width"]
    scale = info["width"] / SW_
    SH_ = int(round(info["height"] / scale)) // 2 * 2
    fps = info["fps"]
    clips = load_json(W(work, "clips.json"))["clips"]
    spk_runs = load_json(W(work, "speakers.json"))
    mode = e.get("framing", {}).get("mode", cfg["mode"]) if isinstance(e.get("framing"), dict) else cfg["mode"]

    refs = {}
    for k, s in e.get("speakers", {}).items():
        tr = s.get("tile_ref")
        if not tr:
            continue
        rx, ry, rw, rh = tr["rect"]
        fr = read_frames(src, parse_t(tr["t"]), 0.2, info["width"], info["height"])[0]
        crop = fr[ry:ry + rh, rx:rx + rw]
        refs[k] = cv2.resize(crop, (max(8, int(rw / scale)), max(8, int(rh / scale))), interpolation=cv2.INTER_AREA)
    if mode == "follow-speaker" and not refs:
        print("no speakers.*.tile_ref given -> falling back to full-frame framing")
        mode = "full-frame"
    an = Analyzer(refs, SW_, SH_)

    segments, limits = [], []
    for c in clips:
        ci, ca, cb = c["i"], c["a"], c["b"]
        runs = spk_runs.get(str(ci), [[ca, cb, None]])
        if mode == "full-frame":
            crop = trim_black(src, (ca + cb) / 2, [0, 0, info["width"], info["height"]])
            for r0, r1, k in runs:
                segments.append({"clip": ci, "t0": max(ca, r0), "t1": min(cb, r1), "crop": crop, "kind": "full",
                                 "spk": k, "shown": k})
            continue
        frames = read_frames(src, ca, cb - ca, SW_, SH_, fps=cfg["sample_fps"])
        step = 1.0 / cfg["sample_fps"]
        rows = []
        for n, img in enumerate(frames):
            r = an.classify(img)
            r["t"] = ca + n * step
            rows.append(r)
        # layout groups (by layout type + gallery geometry)
        def lkey(r):
            if r["layout"] == "gallery":
                x, y, w, h = r["bbox"]
                return ("gallery", round(x / 12), round(w / 12))
            return (r["layout"],)
        groups = []
        for r in rows:
            if groups and groups[-1]["key"] == lkey(r):
                groups[-1]["rows"].append(r)
            else:
                groups.append({"key": lkey(r), "rows": [r]})
        clean = []
        for g in groups:  # 1-sample blips are detector noise
            if clean and len(g["rows"]) == 1:
                clean[-1]["rows"] += g["rows"]
            else:
                clean.append(g)
        for gi, g in enumerate(clean):
            g0 = ca if gi == 0 else g["rows"][0]["t"]
            g1 = cb if gi == len(clean) - 1 else clean[gi + 1]["rows"][0]["t"]
            lay = g["rows"][0]["layout"]
            for r0, r1, k in runs:
                t0, t1 = max(g0, r0), min(g1, r1)
                if t1 - t0 < 0.02:
                    continue
                rs = [r for r in g["rows"] if t0 - step <= r["t"] <= t1] or g["rows"]
                seg = {"clip": ci, "t0": t0, "t1": t1, "spk": k}
                if lay == "share":
                    rect = tuple(int(statistics.median(r["rect"][j] for r in rs if r.get("rect"))) for j in range(4))
                    seg.update(kind="share", crop=trim_black(src, (t0 + t1) / 2, shrink(rect, 0, scale, info["width"], info["height"])),
                               shown="screen")
                elif lay.startswith("full:") or lay == "single":
                    who = lay.split(":", 1)[1] if ":" in lay else "?"
                    seg.update(kind="full", crop=shrink(rs[0]["rect"], cfg["full_shrink"], scale, info["width"], info["height"]),
                               shown=who)
                    if who != k:
                        limits.append(f"{fmt_t(t0)}-{fmt_t(t1)}: {k} is talking but the recording only shows {who}")
                elif lay == "gallery":
                    want = k if k in refs else None
                    good = [r[want][1] for r in rs if want and r.get(want) and r[want][0] > 0.5]
                    shown = want
                    if len(good) < max(1, 0.3 * len(rs)):
                        # speaker not visible in this layout: show the best-visible speaker instead
                        scores = {kk: statistics.median([r[kk][0] for r in rs if r.get(kk)] or [-1]) for kk in refs}
                        shown = max(scores, key=scores.get)
                        good = [r[shown][1] for r in rs if r.get(shown) and r[shown][0] > 0.4] or \
                               [r[shown][1] for r in rs if r.get(shown) and r[shown][1]]
                        limits.append(f"{fmt_t(t0)}-{fmt_t(t1)}: {k} not visible; showing {shown}")
                    rect = tuple(int(statistics.median(v[j] for v in good)) for j in range(4))
                    seg.update(kind="tile", crop=shrink(rect, cfg["tile_shrink"], scale, info["width"], info["height"]),
                               shown=shown)
                else:  # black for the whole group: reuse the previous segment's picture
                    prev = segments[-1] if segments else None
                    seg.update(kind=prev["kind"] if prev else "full", crop=prev["crop"] if prev else [0, 0, info["width"], info["height"]],
                               shown=prev.get("shown") if prev else "?")
                    limits.append(f"{fmt_t(t0)}-{fmt_t(t1)}: source is black here")
                segments.append(seg)

    # frame-grid snap, merge identical neighbours
    out = []
    for s in segments:
        ca = next(c["a"] for c in clips if c["i"] == s["clip"])
        s["t0"] = round(ca + round((s["t0"] - ca) * fps) / fps, 4)
        s["t1"] = round(ca + round((s["t1"] - ca) * fps) / fps, 4)
        if s["t1"] - s["t0"] < 0.5 / fps:
            continue
        if out and out[-1]["clip"] == s["clip"] and out[-1]["crop"] == s["crop"] and out[-1]["spk"] == s["spk"] \
                and abs(out[-1]["t1"] - s["t0"]) < 1e-3:
            out[-1]["t1"] = s["t1"]
        else:
            out.append(s)

    # full-frame-rate check for layout-switch glitches at segment edges
    for s in out:
        n = round((s["t1"] - s["t0"]) * fps)
        s["frames"] = n
        s["clone_lead"] = s["clone_trail"] = 0
        if mode == "full-frame" or n < 3:
            continue
        fr = read_frames(src, s["t0"], n / fps, SW_, SH_)[:n]
        want = s["kind"]
        # glitches live at layout switches: check every frame near the edges, every 5th inside
        edge = int(1.5 * fps)
        idx = [i for i in range(len(fr)) if i < edge or i >= len(fr) - edge or i % 5 == 0]
        lab = {i: kind_of(an.classify(fr[i], full=False)["layout"]) for i in idx}
        labels = [lab.get(i, want) for i in range(len(fr))]
        ok = [(l == want) or (want == "tile" and l == "gallery") for l in labels]
        # a camera crop that suddenly looks nothing like the segment is a transition frame
        x, y, w, h = [int(v / scale) for v in s["crop"]]
        if want in ("tile", "full") and w > 8 and h > 8:
            cr = np.stack([cv2.resize(f[y:y + h, x:x + w], (96, 54)) for f in fr]).astype(np.float32)
            med = np.median(cr, axis=0)
            d = np.abs(cr - med).mean(axis=(1, 2, 3))
            thr = max(25.0, float(np.median(d)) * 3)
            ok = [o and dd <= thr and f.max() >= 30 for o, dd, f in zip(ok, d, cr)]
        lead = next((i for i, v in enumerate(ok) if v), n)
        trail = next((i for i, v in enumerate(reversed(ok)) if v), n)
        if lead >= n:
            continue
        s["clone_lead"] = lead + 1 if lead else 0
        s["clone_trail"] = trail + 1 if trail else 0
        mid_bad = sum(1 for v in ok[lead:n - trail] if not v)
        if mid_bad:
            limits.append(f"{fmt_t(s['t0'])}-{fmt_t(s['t1'])}: {mid_bad} odd frames mid-segment (check QA strips)")

    save_json(W(work, "segments.json"), {"fps": fps, "mode": mode, "segments": out, "limitations": sorted(set(limits))})

    # review sheet
    rv = W(work, "review")
    rv.mkdir(parents=True, exist_ok=True)
    thumbs = []
    for i, s in enumerate(out):
        f = read_frames(src, (s["t0"] + s["t1"]) / 2, 0.2, 480, 270, crop=s["crop"])[0]
        im = Image.fromarray(f[:, :, ::-1].copy())
        d = ImageDraw.Draw(im)
        label = f"{i} #{s['clip']} talk:{s['spk']} show:{s['shown']} {s['kind']}"
        if s["clone_lead"] or s["clone_trail"]:
            label += f" fix{s['clone_lead']}/{s['clone_trail']}"
        d.rectangle([0, 0, 8 + 6 * len(label), 16], fill=(200, 0, 0))
        d.text((4, 3), label, fill=(255, 255, 255))
        thumbs.append(im)
    cols = 5
    sheet = Image.new("RGB", (480 * cols, 270 * ((len(thumbs) + cols - 1) // cols)))
    for i, im in enumerate(thumbs):
        sheet.paste(im, ((i % cols) * 480, (i // cols) * 270))
    sheet.save(rv / "segments.jpg", quality=82)
    for s in out:
        print(f"#{s['clip']:02d} {fmt_t(s['t0'])}-{fmt_t(s['t1'])} talk:{s['spk']} show:{s['shown']:6s} {s['kind']:5s} "
              f"crop={s['crop']}" + (f" fix {s['clone_lead']}/{s['clone_trail']}" if s['clone_lead'] or s['clone_trail'] else ""))
    print(f"{len(out)} segments -> work/segments.json ; review sheet -> work/review/segments.jpg")
    for l in sorted(set(limits)):
        print("  LIMITATION:", l)


if __name__ == "__main__":
    main()
