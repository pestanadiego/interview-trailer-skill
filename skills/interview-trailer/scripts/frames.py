"""Grab frames to look at: a coordinate-gridded frame (to read tile rects) or a contact sheet.

usage:
  $PY frames.py --project P --src V --grid 600          # work/frames/grid_600.jpg with source-pixel grid
  $PY frames.py --project P --src V --sheet 60,600,1800  # work/frames/sheet.jpg (one frame per time)
  $PY frames.py --project P --src V --every 120         # contact sheet every N seconds of the source
"""
import argparse

from PIL import Image, ImageDraw, ImageFont

from common import W, ff, parse_t, probe, work_dir


def grab(src, t, path, width=None):
    vf = ["-vf", f"scale={width}:-2"] if width else []
    ff(["-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", *vf, path])
    return Image.open(path).convert("RGB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--grid")
    ap.add_argument("--sheet")
    ap.add_argument("--every", type=float)
    a = ap.parse_args()
    work = work_dir(a.project)
    out = W(work, "frames")
    out.mkdir(parents=True, exist_ok=True)
    info = probe(a.src)
    if a.grid:
        t = parse_t(a.grid)
        im = grab(a.src, t, out / f"full_{int(t)}.png")
        d = ImageDraw.Draw(im)
        step = 100 if info["width"] <= 3000 else 200
        font = ImageFont.load_default()
        for x in range(0, im.width, step):
            d.line([(x, 0), (x, im.height)], fill=(255, 0, 0) if x % (step * 5) == 0 else (255, 120, 120), width=1)
            d.text((x + 3, 3), str(x), fill=(255, 255, 0), font=font)
        for y in range(0, im.height, step):
            d.line([(0, y), (im.width, y)], fill=(255, 0, 0) if y % (step * 5) == 0 else (255, 120, 120), width=1)
            d.text((3, y + 3), str(y), fill=(255, 255, 0), font=font)
        p = out / f"grid_{int(t)}.jpg"
        im.save(p, quality=88)
        print(p, f"(source {info['width']}x{info['height']}; full frame: {out / f'full_{int(t)}.png'})")
        return
    if a.every:
        times, t = [], a.every / 2
        while t < info["duration"]:
            times.append(t)
            t += a.every
    else:
        times = [parse_t(x) for x in a.sheet.split(",")]
    thumbs = []
    for t in times:
        im = grab(a.src, t, out / f"t_{int(t)}.jpg", width=480)
        ImageDraw.Draw(im).rectangle([0, 0, 92, 16], fill=(200, 0, 0))
        ImageDraw.Draw(im).text((3, 3), f"{int(t // 60)}:{t % 60:04.1f}", fill=(255, 255, 255))
        thumbs.append(im)
    cols = 4
    w, h = thumbs[0].size
    sheet = Image.new("RGB", (w * cols, h * ((len(thumbs) + cols - 1) // cols)))
    for i, im in enumerate(thumbs):
        sheet.paste(im, ((i % cols) * w, (i // cols) * h))
    p = out / "sheet.jpg"
    sheet.save(p, quality=85)
    print(p)


if __name__ == "__main__":
    main()
