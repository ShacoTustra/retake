"""«Сообщить о проблеме»: a zip a beta tester can send back.

Contains the app log, system info and settings (never the API key); for a chosen lesson also its
suggested cuts and the reviewer's decisions — exactly the data needed to improve detection.
Never the video.
"""
import json
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import config
import models
import runtime


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (r.stdout or r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"({e})"


def about() -> str:
    lines = [f"Retake {runtime.VERSION}", runtime.platform_info(), f"frozen: {runtime.FROZEN}",
             f"data: {config.app_dir()}", f"time: {time.strftime('%Y-%m-%d %H:%M:%S %z')}"]
    if sys.platform == "darwin":
        lines.append("cpu: " + _run(["sysctl", "-n", "machdep.cpu.brand_string"]))
        mem = _run(["sysctl", "-n", "hw.memsize"])
        if mem.isdigit():
            lines.append(f"ram: {int(mem) / 2**30:.0f} GB")
    lines.append(f"python: {sys.version.split()[0]} {platform.machine()}")
    ff = shutil.which("ffmpeg")
    lines.append(f"ffmpeg: {ff}")
    lines.append(_run(["ffmpeg", "-hide_banner", "-version"]).splitlines()[0] if ff else "ffmpeg missing")
    enc = _run(["ffmpeg", "-hide_banner", "-encoders"]) if ff else ""
    lines.append("encoders: " + ", ".join(e for e in ("h264_videotoolbox", "libx264", "h264_nvenc") if e in enc))
    try:
        import mlx.core as mx
        lines.append(f"mlx: {mx.__version__}, metal: {mx.metal.is_available()}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"mlx: {e.__class__.__name__}")
    du = shutil.disk_usage(config.app_dir())
    lines.append(f"disk free: {du.free / 1e9:.1f} GB")
    lines.append("models: " + ", ".join(f"{n}={'ready' if models.is_ready(n) else 'missing'}" for n in models.MLX_MODELS))
    lines.append("")
    pub = config.public(config.load())
    pub["llm"].pop("key_hint", None)  # not even a masked key leaves the machine
    lines.append("settings: " + json.dumps(pub, ensure_ascii=False, indent=1))
    return "\n".join(lines)


def build(project=None) -> Path:
    import projects as projects_mod

    dl = Path.home() / "Downloads"
    out_dir = dl if dl.is_dir() else config.app_dir() / "reports"
    out_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    name = f"Retake-отчёт-{stamp}" + (f"-{project.meta.get('name', '')}"[:60] if project else "") + ".zip"
    out = out_dir / name.replace("/", "-")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("about.txt", about())
        for lp in (runtime.log_path(), runtime.log_path().with_suffix(".log.1")):
            if lp.exists():
                z.write(lp, f"logs/{lp.name}")
        z.writestr("projects.json", json.dumps(
            [{k: p.meta.get(k) for k in ("name", "duration", "status", "llm_error")} for p in projects_mod.list_projects()],
            ensure_ascii=False, indent=1))
        if project:
            for f in ("project.json", "edits.json", "decisions.json", "edits.final.json"):
                fp = project.dir / f
                if fp.exists():
                    z.write(fp, f"lesson/{f}")
    return out
