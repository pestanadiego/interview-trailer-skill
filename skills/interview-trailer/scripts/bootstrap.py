#!/usr/bin/env python3
"""Install everything the pipeline needs into a private cache (never touches system packages).

Run with any Python 3.8+:  python3 scripts/bootstrap.py
Prints the interpreter to use for every other script:  PY=/.../venv/bin/python
Idempotent: a second run only verifies.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

CACHE = Path(os.environ.get("INTERVIEW_TRAILER_CACHE", Path.home() / ".cache" / "interview-trailer"))
VENV = CACHE / "venv"
FONTS = CACHE / "fonts"
ENV_FILE = CACHE / "env.json"
IS_WIN = os.name == "nt"
IS_MAC_ARM = sys.platform == "darwin" and platform.machine() == "arm64"
INTER_URL = "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip"
NEEDED_FILTERS = ["maskedmerge", "gblur", "tpad", "loudnorm", "overlay", "concat", "unsharp", "afade"]

BASE_PKGS = ["numpy", "pillow", "opencv-python-headless", "soundfile", "setuptools<81",
             "webrtcvad-wheels", "librosa", "scipy", "typing_extensions", "torch",
             "rapidocr_onnxruntime"]
WHISPER_PKG = "mlx-whisper" if IS_MAC_ARM else "faster-whisper"


def log(msg):
    print(f"[bootstrap] {msg}", flush=True)


def run(cmd, **kw):
    log("$ " + " ".join(map(str, cmd)))
    return subprocess.run(list(map(str, cmd)), check=True, **kw)


def venv_python():
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def find_uv():
    uv = shutil.which("uv")
    if uv:
        return uv
    for cand in (Path.home() / ".local" / "bin" / ("uv.exe" if IS_WIN else "uv"),
                 Path.home() / ".cargo" / "bin" / ("uv.exe" if IS_WIN else "uv")):
        if cand.exists():
            return str(cand)
    return None


def install_uv():
    try:
        if IS_WIN:
            run(["powershell", "-ExecutionPolicy", "ByPass", "-c", "irm https://astral.sh/uv/install.ps1 | iex"])
        else:
            run(["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"])
    except Exception as e:  # noqa: BLE001
        log(f"uv install failed ({e}); falling back to venv+pip")
    return find_uv()


def make_venv():
    if venv_python().exists():
        return find_uv()
    CACHE.mkdir(parents=True, exist_ok=True)
    uv = find_uv() or install_uv()
    if uv:
        run([uv, "venv", "--python", "3.12", str(VENV)])
    else:
        if not ((3, 10) <= sys.version_info[:2] <= (3, 12)):
            sys.exit("Need uv or a Python 3.10-3.12 to build the environment.")
        run([sys.executable, "-m", "venv", str(VENV)])
        run([venv_python(), "-m", "pip", "install", "-q", "--upgrade", "pip"])
    return uv


def pip_install(uv, pkgs, no_deps=False):
    extra = ["--no-deps"] if no_deps else []
    if uv:
        run([uv, "pip", "install", "-q", "--python", str(venv_python()), *extra, *pkgs])
    else:
        run([venv_python(), "-m", "pip", "install", "-q", *extra, *pkgs])


def importable(mods):
    code = "; ".join(f"import {m}" for m in mods)
    r = subprocess.run([str(venv_python()), "-c", code], capture_output=True, text=True)
    return r.returncode == 0, r.stderr.strip()[-400:]


def ffmpeg_ok(ffmpeg):
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return False
    return all(f" {name} " in out for name in NEEDED_FILTERS)


def resolve_ffmpeg(uv):
    sys_ff, sys_fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if sys_ff and sys_fp and ffmpeg_ok(sys_ff):
        return sys_ff, sys_fp
    log("system ffmpeg missing or lacking filters -> installing static-ffmpeg into the venv")
    pip_install(uv, ["static-ffmpeg"])
    code = ("import static_ffmpeg.run as r; f,p=r.get_or_fetch_platform_executables_else_raise(); "
            "print(f); print(p)")
    out = subprocess.run([str(venv_python()), "-c", code], capture_output=True, text=True, check=True).stdout.split()
    return out[-2], out[-1]


def ensure_font():
    FONTS.mkdir(parents=True, exist_ok=True)
    want = ["Inter-SemiBold.ttf", "Inter-Bold.ttf", "Inter-Regular.ttf"]
    if all((FONTS / f).exists() for f in want):
        return
    tmp = FONTS / "inter.zip"
    log("downloading Inter font")
    try:
        urllib.request.urlretrieve(INTER_URL, tmp)
        with zipfile.ZipFile(tmp) as z:
            for n in z.namelist():
                base = n.rsplit("/", 1)[-1]
                if base in want and "/ttf/" in n:
                    (FONTS / base).write_bytes(z.read(n))
        tmp.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        log(f"could not download Inter ({e}); subtitles will fall back to a system sans-serif")


def main():
    uv = make_venv()
    mods = ["numpy", "PIL", "cv2", "soundfile", "resemblyzer", "rapidocr_onnxruntime",
            "mlx_whisper" if IS_MAC_ARM else "faster_whisper"]
    ok, _ = importable(mods)
    if not ok:
        pip_install(uv, BASE_PKGS + [WHISPER_PKG])
        pip_install(uv, ["resemblyzer"], no_deps=True)   # its pinned webrtcvad has no wheels on many platforms
        ok, err = importable(mods)
        if not ok:
            sys.exit(f"dependency import failed after install:\n{err}")
    ffmpeg, ffprobe = resolve_ffmpeg(uv)
    ensure_font()
    env = {
        "python": str(venv_python()),
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "whisper_backend": "mlx" if IS_MAC_ARM else "faster-whisper",
        "font_dir": str(FONTS),
        "platform": f"{sys.platform}-{platform.machine()}",
    }
    ENV_FILE.write_text(json.dumps(env, indent=1))
    log("ready")
    print(f"PY={venv_python()}")


if __name__ == "__main__":
    main()
