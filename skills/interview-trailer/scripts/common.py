"""Shared helpers for the interview-trailer pipeline (paths, config, ffmpeg, transcript access)."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("INTERVIEW_TRAILER_CACHE", Path.home() / ".cache" / "interview-trailer"))
ENV_FILE = CACHE_DIR / "env.json"


# ---------------------------------------------------------------- json / config
def load_json(p, default=None):
    p = Path(p)
    if not p.exists():
        if default is not None:
            return copy.deepcopy(default)
        raise FileNotFoundError(p)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_json(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(work: Path):
    """defaults.json <- ~/.config/interview-trailer/style.json <- <project>/style.json <- edl.json overrides."""
    cfg = load_json(SKILL_DIR / "defaults.json")
    for p in (Path.home() / ".config" / "interview-trailer" / "style.json", work / "style.json"):
        if p.exists():
            cfg = deep_merge(cfg, load_json(p))
    edl = work / "edl.json"
    if edl.exists():
        e = load_json(edl)
        for key in ("style", "framing", "cuts", "transcription", "redact"):
            if key in e:
                cfg[key] = deep_merge(cfg[key], e[key])
    return cfg


def env():
    if ENV_FILE.exists():
        return load_json(ENV_FILE)
    return {"ffmpeg": shutil.which("ffmpeg") or "ffmpeg", "ffprobe": shutil.which("ffprobe") or "ffprobe"}


# ---------------------------------------------------------------- work dir
def work_dir(arg: str | None) -> Path:
    w = Path(arg).expanduser().resolve()
    (w / "work").mkdir(parents=True, exist_ok=True)
    return w


def W(work: Path, *parts) -> Path:
    """Path inside <project>/work/."""
    p = work / "work"
    for x in parts:
        p = p / x
    return p


def edl(work: Path):
    return load_json(work / "edl.json")


# ---------------------------------------------------------------- ffmpeg
def ff(args, capture=False, check=True):
    cmd = [env()["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    if capture:
        return subprocess.run(cmd, capture_output=True, check=check)
    return subprocess.run(cmd, check=check)


def probe(src):
    out = subprocess.run([env()["ffprobe"], "-v", "error", "-show_entries",
                          "stream=codec_type,width,height,r_frame_rate:format=duration",
                          "-of", "json", str(src)], capture_output=True, check=True, text=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    num, den = v["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    return {"width": int(v["width"]), "height": int(v["height"]),
            "fps": round(fps) if abs(fps - round(fps)) < 0.02 else fps,
            "duration": float(d["format"]["duration"]),
            "has_audio": any(s["codec_type"] == "audio" for s in d["streams"])}


def frame_align(a, b, fps):
    """Snap a clip to a whole number of frames so audio, video and subtitles stay locked."""
    n = max(1, round((b - a) * fps))
    return a, a + n / fps


# ---------------------------------------------------------------- time
def parse_t(x):
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    parts = [float(p) for p in s.split(":")]
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def fmt_t(t):
    t = max(0.0, float(t))
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


# ---------------------------------------------------------------- transcript
def words(work: Path):
    tr = load_json(W(work, "transcript.json"))
    out = []
    for s in tr["segments"]:
        for w in s.get("words", []):
            if w.get("start") is None:
                continue
            out.append({"word": w["word"].strip(), "start": float(w["start"]), "end": float(w["end"])})
    return out


def clip_words(all_words, a, b):
    """Words that belong to [a, b]: mostly inside, and started before the cut out."""
    return [w for w in all_words if w["end"] > a + 0.12 and w["start"] < b - 0.08]


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def hex_rgba(h, a=255):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
