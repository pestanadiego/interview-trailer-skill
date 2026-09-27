"""Render the trailer: speaker-following crops, redaction blur, per-speaker subtitles,
normalised audio, fade-out ending. Every run writes a NEW version (never overwrites).

usage: $PY render.py --project P [--name my-trailer]
reads: edl.json, work/clips.json, work/segments.json, work/redact.json, work/cues.json
writes: <project>/<name>-vN.mp4 and <project>/<name>-vN.srt
"""
import argparse
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common import W, edl, env, ff, hex_rgba, load_config, load_json, slug, work_dir


def font(style):
    fd = Path(env().get("font_dir", ""))
    name = style.get("font", "Inter-SemiBold")
    for cand in (Path(name), fd / f"{name}.ttf", fd / "Inter-SemiBold.ttf"):
        if cand.suffix == ".ttf" and cand.exists():
            return ImageFont.truetype(str(cand), style["font_size"])
    for sysf in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(sysf).exists():
            return ImageFont.truetype(sysf, style["font_size"])
    return ImageFont.load_default()


def speaker_colors(e, style):
    pal = style["speaker_colors"]
    out = {}
    for i, (k, s) in enumerate(e.get("speakers", {}).items()):
        out[k] = hex_rgba(s.get("color") or pal[i % len(pal)])
    return out


def render_cue(lines, color, style, fnt, path):
    Wd, Ht = style["width"], style["height"]
    img = Image.new("RGBA", (Wd, Ht), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    boxes = [d.textbbox((0, 0), l, font=fnt) for l in lines]
    asc, desc = fnt.getmetrics()
    lh, gap = asc + desc, 12
    y0 = Ht - style["bottom_margin"] - (len(lines) * lh + (len(lines) - 1) * gap)
    if style.get("plate", True):
        plate = Image.new("RGBA", (Wd, Ht), (0, 0, 0, 0))
        pd = ImageDraw.Draw(plate)
        for i, b in enumerate(boxes):
            tw = b[2] - b[0]
            x, y = (Wd - tw) // 2, y0 + i * (lh + gap)
            pd.rounded_rectangle([x - 20, y - 6, x + tw + 20, y + lh + 6], radius=12, fill=tuple(style["plate_rgba"]))
        img = Image.alpha_composite(img, plate)
        d = ImageDraw.Draw(img)
    for i, (l, b) in enumerate(zip(lines, boxes)):
        tw = b[2] - b[0]
        kw = {"stroke_width": 2, "stroke_fill": (0, 0, 0, 255)} if style.get("outline") else {}
        d.text(((Wd - tw) // 2 - b[0], y0 + i * (lh + gap)), l, font=fnt, fill=color, **kw)
    img.save(path, optimize=True)


def next_version(project, name):
    n = 1
    for p in project.glob(f"{name}-v*.mp4"):
        m = re.search(r"-v(\d+)\.mp4$", p.name)
        if m:
            n = max(n, int(m.group(1)) + 1)
    return project / f"{name}-v{n}.mp4"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--name")
    a = ap.parse_args()
    work = work_dir(a.project)
    project = Path(a.project).expanduser().resolve()
    cfg = load_config(work)
    st = cfg["style"]
    e = edl(work)
    src = e["source"]
    clips = load_json(W(work, "clips.json"))
    segs = load_json(W(work, "segments.json"))["segments"]
    red = load_json(W(work, "redact.json"), default={}) if W(work, "redact.json").exists() else {}
    cues = load_json(W(work, "cues.json"))
    fps = clips["fps"]
    total = sum(c["b"] - c["a"] for c in clips["clips"])
    Wd, Ht = st["width"], st["height"]
    name = a.name or e.get("output_name") or f"{slug(Path(src).stem)}-trailer"
    out = next_version(project, name)
    tmp = W(work, f"render_{out.stem}")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "subs").mkdir(parents=True)
    (tmp / "segs").mkdir(parents=True)

    # 1) subtitle overlays
    fnt = font(st)
    colors = speaker_colors(e, st)
    fallback = hex_rgba(st["speaker_colors"][0])
    blank = tmp / "subs" / "blank.png"
    Image.new("RGBA", (Wd, Ht), (0, 0, 0, 0)).save(blank)
    entries, t = [], 0.0
    for i, c in enumerate(cues["cues"]):
        if c["start"] > t + 0.001:
            entries.append((blank, c["start"] - t))
        p = tmp / "subs" / f"c{i:04d}.png"
        render_cue(c["lines"], colors.get(c["spk"], fallback), st, fnt, p)
        entries.append((p, c["end"] - c["start"]))
        t = c["end"]
    if total > t:
        entries.append((blank, total - t))
    with open(tmp / "subs.txt", "w") as f:
        f.write("ffconcat version 1.0\n")
        for p, dur in entries:
            f.write(f"file '{p.as_posix()}'\nduration {dur:.3f}\n")
        f.write(f"file '{blank.as_posix()}'\n")

    # 2) one video file per segment (crop, fit, blur mask, cloned glitch frames)
    blur = cfg["redact"]

    def seg_job(args):
        i, s = args
        x, y, w, h = s["crop"]
        n = round((s["t1"] - s["t0"]) * fps)
        lead, trail = s.get("clone_lead", 0), s.get("clone_trail", 0)
        good = max(1, n - lead - trail)
        start = s["t0"] + lead / fps
        if s["kind"] == "share":
            fit = f"scale={Wd}:{Ht}:force_original_aspect_ratio=decrease:flags=lanczos,pad={Wd}:{Ht}:(ow-iw)/2:(oh-ih)/2:black"
        elif s["kind"] == "full" and cfg["framing"].get("mode") == "full-frame":
            fit = f"scale={Wd}:{Ht}:force_original_aspect_ratio=decrease:flags=lanczos,pad={Wd}:{Ht}:(ow-iw)/2:(oh-ih)/2:black"
        else:
            fit = f"scale={Wd}:{Ht}:force_original_aspect_ratio=increase:flags=lanczos,crop={Wd}:{Ht},unsharp=5:5:0.5:5:5:0"
        tail = (f"{fit},setsar=1,fps={fps},trim=end_frame={good},"
                f"tpad=start_mode=clone:start={lead}:stop_mode=clone:stop={trail},format=yuv420p")
        rinfo = red.get(str(i))
        if rinfo:
            mw, mh = rinfo["size"]
            inputs = ["-ss", f"{start:.3f}", "-i", src, "-loop", "1", "-i", rinfo["mask"]]
            # full-range RGB so the mask is a true 0/255 switch (no ghost of the original text)
            graph = (f"[0:v]crop={w}:{h}:{x}:{y},format=gbrp,split[a][b];"
                     f"[b]scale=iw/{blur['blur_downscale']}:ih/{blur['blur_downscale']}:flags=area,"
                     f"scale={w}:{h}:flags=bicubic,gblur=sigma={blur['blur_sigma']}[bl];"
                     f"[1:v]format=gray,format=gbrp,pad={max(w, mw)}:{max(h, mh)},crop={w}:{h}:0:0[m];"
                     f"[a][bl][m]maskedmerge,{tail}")
            vf = ["-filter_complex", graph]
        else:
            inputs = ["-ss", f"{start:.3f}", "-i", src]
            vf = ["-vf", f"crop={w}:{h}:{x}:{y},{tail}"]
        p = tmp / "segs" / f"s{i:04d}.mp4"
        ff([*inputs, *vf, "-frames:v", n, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
            "-g", int(fps), p])
        return p, n

    with ThreadPoolExecutor(max(2, (os.cpu_count() or 4) // 2)) as ex:
        results = list(ex.map(seg_job, enumerate(segs)))
    frames = sum(n for _, n in results)
    expected = round(total * fps)
    if abs(frames - expected) > 1:
        print(f"WARNING: video frames {frames} != expected {expected}")
    with open(tmp / "segs.txt", "w") as f:
        f.write("ffconcat version 1.0\n")
        for p, _ in results:
            f.write(f"file '{p.as_posix()}'\n")

    # 3) audio per clip + final mux
    args = ["-f", "concat", "-safe", "0", "-i", tmp / "segs.txt"]
    for c in clips["clips"]:
        args += ["-ss", f"{c['a']:.3f}", "-t", f"{c['b'] - c['a']:.3f}", "-vn", "-i", src]
    n = len(clips["clips"])
    args += ["-f", "concat", "-safe", "0", "-i", tmp / "subs.txt"]
    fc = []
    for i, c in enumerate(clips["clips"]):
        d = c["b"] - c["a"]
        fc.append(f"[{i + 1}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={d:.3f},"
                  f"asetpts=PTS-STARTPTS,afade=t=in:d=0.03,afade=t=out:st={max(d - 0.05, 0):.3f}:d=0.05[a{i}]")
    fc.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[ca]")
    fc.append(f"[{n + 1}:v]fps={fps},format=rgba[sub]")
    fade = float(st.get("end_fade_seconds") or 0)
    vfade = f",fade=t=out:st={total - fade:.3f}:d={fade}" if fade else ""
    afade = f",afade=t=out:st={total - fade:.3f}:d={fade}" if fade else ""
    fc.append(f"[0:v][sub]overlay=0:0:eof_action=pass:format=auto{vfade},format=yuv420p[outv]")
    fc.append(f"[ca]loudnorm=I={st['loudness_lufs']}:TP=-1.5:LRA=11,aresample=48000{afade}[outa]")
    args += ["-filter_complex", ";".join(fc), "-map", "[outv]", "-map", "[outa]",
             "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", out]
    ff(args)
    shutil.copy(W(work, "cues.srt"), out.with_suffix(".srt"))
    shutil.rmtree(tmp / "segs", ignore_errors=True)
    print(f"OUTPUT {out}")
    print(f"SUBTITLES {out.with_suffix('.srt')}")


if __name__ == "__main__":
    main()
