"""QA a rendered trailer before handing it off.

Checks: stream lengths match, integrated loudness, black frames (outside the end fade),
and produces images to LOOK at: one frame per segment, and frame-by-frame strips around every cut.

usage: $PY qa.py --project P [--video <file>]   (default: newest *-vN.mp4 in the project)
writes: work/review/qa_<version>_segments.jpg, work/review/qa_<version>_cuts_<k>.jpg
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from common import W, env, fmt_t, load_config, load_json, work_dir


def newest(project):
    vids = sorted(project.glob("*-v*.mp4"), key=lambda p: int(re.search(r"-v(\d+)\.mp4$", p.name).group(1)))
    return vids[-1] if vids else None


def grab(video, t, w=384):
    p = subprocess.run([env()["ffmpeg"], "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, t):.3f}", "-i", str(video),
                        "-frames:v", "1", "-vf", f"scale={w}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True)
    from io import BytesIO
    return Image.open(BytesIO(p.stdout)).convert("RGB") if p.stdout else Image.new("RGB", (w, w * 9 // 16))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--video")
    a = ap.parse_args()
    work = work_dir(a.project)
    project = Path(a.project).expanduser().resolve()
    video = Path(a.video) if a.video else newest(project)
    if not video:
        raise SystemExit("no rendered video found")
    cfg = load_config(work)
    clips = load_json(W(work, "clips.json"))
    segs = load_json(W(work, "segments.json"))["segments"]
    fps = clips["fps"]
    rv = W(work, "review")
    rv.mkdir(parents=True, exist_ok=True)
    tag = video.stem

    # streams + loudness + black frames
    pr = json.loads(subprocess.run([env()["ffprobe"], "-v", "error", "-show_entries", "stream=codec_type,duration",
                                    "-show_entries", "format=duration", "-of", "json", str(video)],
                                   capture_output=True, text=True).stdout)
    durs = {s["codec_type"]: float(s.get("duration", 0)) for s in pr["streams"]}
    total = float(pr["format"]["duration"])
    lo = subprocess.run([env()["ffmpeg"], "-hide_banner", "-i", str(video), "-af", "ebur128", "-f", "null", "-"],
                        capture_output=True, text=True).stderr
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", lo)
    lufs = float(m[-1]) if m else None
    bd = subprocess.run([env()["ffmpeg"], "-hide_banner", "-i", str(video), "-vf", "blackdetect=d=0.03:pix_th=0.08",
                         "-an", "-f", "null", "-"], capture_output=True, text=True).stderr
    fade = float(cfg["style"].get("end_fade_seconds") or 0)
    blacks = [(float(s), float(e_)) for s, e_ in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", bd)
              if float(s) < total - fade - 0.1]

    # output-timeline position of every segment
    off, clip_off = 0.0, {}
    for c in clips["clips"]:
        clip_off[c["i"]] = off - c["a"]
        off += c["b"] - c["a"]
    starts = [(s["t0"] + clip_off[s["clip"]], s["t1"] + clip_off[s["clip"]], s) for s in segs]

    thumbs = []
    for i, (t0, t1, s) in enumerate(starts):
        im = grab(video, (t0 + t1) / 2)
        d = ImageDraw.Draw(im)
        lab = f"{i} {fmt_t(t0)[2:]} {s['kind']} talk:{s['spk']} show:{s.get('shown')}"
        d.rectangle([0, 0, 6 * len(lab) + 8, 15], fill=(200, 0, 0))
        d.text((4, 2), lab, fill=(255, 255, 255))
        thumbs.append(im)
    cols = 5
    w, h = thumbs[0].size
    sheet = Image.new("RGB", (w * cols, h * ((len(thumbs) + cols - 1) // cols)))
    for i, im in enumerate(thumbs):
        sheet.paste(im, ((i % cols) * w, (i // cols) * h))
    sheet.save(rv / f"qa_{tag}_segments.jpg", quality=82)

    # frame strips around every cut: 3 frames before, 4 after
    cuts = [t0 for t0, _, _ in starts[1:]]
    per, k, files = 10, 0, []
    for b in range(0, len(cuts), per):
        rows = []
        for t in cuts[b:b + per]:
            row = [grab(video, t + j / fps, 200) for j in range(-3, 4)]
            rw, rh = row[0].size
            strip = Image.new("RGB", (rw * len(row), rh + 14), "black")
            for j, im in enumerate(row):
                strip.paste(im, (j * rw, 14))
            ImageDraw.Draw(strip).text((4, 1), f"cut @ {fmt_t(t)}  (4th frame = first after cut)", fill=(255, 255, 0))
            rows.append(strip)
        k += 1
        sh = Image.new("RGB", (rows[0].width, sum(r.height for r in rows)))
        y = 0
        for r in rows:
            sh.paste(r, (0, y))
            y += r.height
        p = rv / f"qa_{tag}_cuts_{k}.jpg"
        sh.save(p, quality=80)
        files.append(p.name)

    ok = abs(durs.get("video", 0) - durs.get("audio", 0)) < 0.1
    print(f"VIDEO {video.name}  length {fmt_t(total)}  video {durs.get('video', 0):.2f}s audio {durs.get('audio', 0):.2f}s "
          f"{'OK' if ok else 'MISMATCH'}")
    print(f"LOUDNESS {lufs} LUFS (target {cfg['style']['loudness_lufs']})")
    print("BLACK FRAMES " + ("none" if not blacks else ", ".join(f"{fmt_t(s)}-{fmt_t(e_)}" for s, e_ in blacks)))
    print(f"LOOK AT: work/review/qa_{tag}_segments.jpg and " + ", ".join(f"work/review/{f}" for f in files))


if __name__ == "__main__":
    main()
