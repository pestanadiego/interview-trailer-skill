"""Find sensitive text on screen-share segments and build blur masks.

Blurs: money, percentages, card last-4 / long account numbers, emails, listed names, and
whole table columns whose header looks like a person/customer column or that hold
right-aligned amounts. Keeps anything matching edl.json redact.keep (e.g. demo inputs).

usage: $PY redact.py --project P
reads: edl.json (redact.keep: [regex], redact.names: [str], redact.extra: {seg_index: [[x,y,w,h]]}),
       work/segments.json
writes: work/masks/m<seg>.png, work/redact.json, work/review/redact.jpg
"""
import argparse
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from common import W, edl, load_config, load_json, save_json, work_dir
from plan_frames import read_frames

MONEY = re.compile(r"[$€£¥]\s?-?[\d.,]*\d|\d[\d.,]*\s?(USD|EUR|COP|MXN|ARS|BRL|CLP|PEN)\b|\(\$[\d.,]+\)", re.I)
PCT = re.compile(r"\d[\d.,]*\s?%|%\s?\d")
LAST4 = re.compile(r"[•·*\-–]\s*\d{4}\b|\*{2,}\s?\d{4}|x{2,}\d{4}", re.I)
LONGNUM = re.compile(r"\d[\d\s-]{6,}\d")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
DATE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|ene|abr|ago|dic)\w*\s?\d", re.I)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def ocr_engine():
    from rapidocr_onnxruntime import RapidOCR
    eng = RapidOCR()

    def run(img):
        res, _ = eng(img)
        out = []
        for box, text, conf in res or []:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            out.append({"text": text, "box": [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]})
        return out
    return run


def sensitive(text, cfg, keep, names):
    if any(k.search(text) for k in keep):
        return False
    if names and any(n in norm(text) for n in names):
        return True
    if cfg["money"] and MONEY.search(text):
        return True
    if cfg["percentages"] and PCT.search(text):
        return True
    if cfg["card_last4"] and LAST4.search(text):
        return True
    if cfg["emails"] and EMAIL.search(text):
        return True
    if cfg["long_numbers"] and LONGNUM.search(text) and not DATE.search(text):
        return True
    return False


def merge(boxes, pad):
    boxes = [[x - pad, y - pad, x + w + pad, y + h + pad] for x, y, w, h in boxes]
    changed = True
    while changed:
        changed, out = False, []
        for b in boxes:
            for o in out:
                if not (b[2] < o[0] or b[0] > o[2] or b[3] < o[1] or b[1] > o[3]):
                    o[:] = [min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3])]
                    changed = True
                    break
            else:
                out.append(list(b))
        boxes = out
    return boxes


def unique(items):
    """The same text seen in many frames is ONE thing on screen (dedupe by text + position)."""
    seen, out = set(), []
    for r in items:
        x, y, bw, bh = r["box"]
        key = (norm(r["text"]), round(x / 12), round(y / 12))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def distinct_rows(boxes):
    ys = sorted(b[1] for b in boxes)
    rows = []
    for y in ys:
        if not rows or y - rows[-1] > 8:
            rows.append(y)
    return len(rows)


def column_boxes(items, w, h, cfg, keep):
    """Whole-column blurs: person/customer columns by header, and right-aligned amount columns."""
    items = [r for r in unique(items) if not any(k.search(r["text"]) for k in keep)]
    out = []
    headers = [r for r in items if norm(r["text"]) in {norm(x) for x in cfg["name_column_headers"]}]
    for hd in headers:
        hx, hy, hw, hh = hd["box"]
        top = hy + hh * 0.6
        # next item to the right on the same row bounds the column
        row = sorted([r for r in items if abs(r["box"][1] - hy) < hh and r["box"][0] > hx + hw],
                     key=lambda r: r["box"][0])
        right = (row[0]["box"][0] if row else hx + max(hw * 3, 0.15 * w)) - 10
        below = sorted([r for r in items if r["box"][1] > top and hx - 15 <= r["box"][0] < right],
                       key=lambda r: r["box"][1])
        chain, last = [], hy + hh
        for r in below:                      # contiguous rows under the header
            if r["box"][1] - last > 3.5 * hh:
                break
            chain.append(r)
            last = r["box"][1] + r["box"][3]
        header_row = [r for r in items if abs(r["box"][1] - hy) < hh * 0.7]
        if len(chain) >= 3 and len(header_row) >= 4:   # a table: blur the whole column
            bottom = min(h - 4, last + 10)
            out.append([hx - 12, top + hh * 0.4, right - (hx - 12), bottom - top])
        else:                                          # a form label: blur its value (below or beside)
            vals = chain[:1] or row[:1]
            for v in list(vals):                       # values can wrap onto a second line
                vx, vy, vw, vh = v["box"]
                vals += [r for r in items if r is not v and abs(r["box"][0] - vx) < 5 * vh
                         and -0.2 * vh < r["box"][1] - (vy + vh) < 1.6 * vh]
            for r in vals:
                bx, by, bw, bh = r["box"]
                out.append([bx - 10, by - 6, bw + 20, bh + 12])
    # right-aligned money columns (3+ amounts sharing a right edge)
    money = [r["box"] for r in items if MONEY.search(r["text"])]
    for b in money:
        col = [m for m in money if abs((m[0] + m[2]) - (b[0] + b[2])) < 25]
        if distinct_rows(col) >= 3:
            x0 = min(m[0] for m in col) - 10
            y0 = min(m[1] for m in col) - 10
            x1 = max(m[0] + m[2] for m in col) + 10
            y1 = min(h - 4, max(m[1] + m[3] for m in col) + 10)
            out.append([x0, y0, x1 - x0, y1 - y0])
    # percentage columns
    pct = [r["box"] for r in items if PCT.search(r["text"])]
    for b in pct:
        col = [m for m in pct if abs(m[0] - b[0]) < 40]
        if distinct_rows(col) >= 4:
            x0 = min(m[0] for m in col) - 10
            y0 = min(m[1] for m in col) - 10
            x1 = max(m[0] + m[2] for m in col) + 10
            y1 = min(h - 4, max(m[1] + m[3] for m in col) + 10)
            out.append([x0, y0, x1 - x0, y1 - y0])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    a = ap.parse_args()
    work = work_dir(a.project)
    cfg = load_config(work)["redact"]
    e = edl(work)
    ecfg = e.get("redact", {})
    segs = load_json(W(work, "segments.json"))["segments"]
    if not cfg.get("enabled", True):
        save_json(W(work, "redact.json"), {})
        print("redaction disabled")
        return
    keep = [re.compile(k, re.I) for k in ecfg.get("keep", [])]
    names = [norm(n) for n in ecfg.get("names", [])]
    extra = {str(k): v for k, v in ecfg.get("extra", {}).items()}
    run = None
    masks_dir = W(work, "masks")
    masks_dir.mkdir(parents=True, exist_ok=True)
    plan, previews = {}, []
    for si, s in enumerate(segs):
        if s["kind"] != "share":
            continue
        run = run or ocr_engine()
        x, y, w, h = s["crop"]
        frames = read_frames(e["source"], s["t0"], s["t1"] - s["t0"], w, h, fps=2, crop=s["crop"])
        items, prev_small, prev_items = [], None, []
        for f in frames:
            small = np.asarray(Image.fromarray(f).convert("L").resize((160, 90)), dtype=np.float32)
            if prev_small is not None and np.abs(small - prev_small).mean() < 1.5:
                items += prev_items          # unchanged screen: reuse OCR
                continue
            prev_small, prev_items = small, run(f[:, :, ::-1].copy())
            items += prev_items
        hits = [r["box"] for r in unique(items) if sensitive(r["text"], cfg, keep, names)]
        boxes = merge(hits, pad=max(6, int(h * 0.006)))
        boxes += [[bx, by, bx + bw, by + bh] for bx, by, bw, bh in column_boxes(items, w, h, cfg, keep)]
        boxes += [[bx, by, bx + bw, by + bh] for bx, by, bw, bh in extra.get(str(si), [])]
        m = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(m)
        for b in boxes:
            d.rectangle([max(0, b[0]), max(0, b[1]), min(w - 3, b[2]), min(h - 3, b[3])], fill=255)
        mp = masks_dir / f"m{si:03d}.png"
        m.save(mp)
        texts = sorted({r["text"] for r in items if sensitive(r["text"], cfg, keep, names)})
        plan[str(si)] = {"mask": str(mp), "size": [w, h], "boxes": len(boxes), "texts": texts[:80]}
        print(f"seg {si} (clip #{s['clip']}): {len(boxes)} blur boxes; e.g. {texts[:8]}")
        im = Image.fromarray(frames[len(frames) // 2][:, :, ::-1].copy()) if len(frames) else None
        if im is not None:
            im = Image.composite(im.filter(ImageFilter.GaussianBlur(24)), im, m)
            im.thumbnail((960, 540))
            previews.append(im)
    save_json(W(work, "redact.json"), plan)
    if previews:
        rv = W(work, "review")
        rv.mkdir(parents=True, exist_ok=True)
        cols = 2
        pw, ph = max(p.width for p in previews), max(p.height for p in previews)
        sheet = Image.new("RGB", (pw * cols, ph * ((len(previews) + cols - 1) // cols)), "black")
        for i, p in enumerate(previews):
            sheet.paste(p, ((i % cols) * pw, (i // cols) * ph))
        sheet.save(rv / "redact.jpg", quality=85)
        print("review sheet -> work/review/redact.jpg")
    else:
        print("no screen-share segments: nothing to redact")


if __name__ == "__main__":
    main()
