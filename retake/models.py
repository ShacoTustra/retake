"""Speech-recognition model files for the Mac (MLX) build: downloaded once into the app folder.

Plain HTTPS from huggingface.co (or a mirror) with resume and SHA-256 check, so the progress can be
shown in the app and a dropped connection does not restart a 1.6 GB download from zero.
"""
import hashlib
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import config

# pinned revisions: a model update upstream must not silently change the results
MLX_MODELS = {
    "large-v3-turbo": {
        "repo": "mlx-community/whisper-large-v3-turbo",
        "rev": "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
        "files": {
            "config.json": (268, None),
            "weights.safetensors": (1613977612, "951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6"),
        },
    },
    "tiny": {  # 75 MB, only for the self-test
        "repo": "mlx-community/whisper-tiny",
        "rev": "78c52ab98ca87f570bc57ad852e15ef7060f9f76",
        "files": {
            "config.json": (262, None),
            "weights.npz": (74418182, "d5a3b8671ac7aab11a2c9d0f16e7da94bad5500d785856f438c6bd44c3723944"),
        },
    },
}
DEFAULT = "large-v3-turbo"

_lock = threading.Lock()
# shared with the UI: {"name", "state": idle|downloading|error|ready, "done", "total", "error"}
status: dict = {"state": "idle"}


def model_dir(name: str) -> Path:
    return config.app_dir() / "models" / name


def total_size(name: str) -> int:
    return sum(size for size, _ in MLX_MODELS[name]["files"].values())


def is_ready(name: str = DEFAULT) -> bool:
    d = model_dir(name)
    return all((d / f).is_file() and (d / f).stat().st_size == size
               for f, (size, _) in MLX_MODELS[name]["files"].items())


def _ctx():
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure(name: str = DEFAULT, progress=None, endpoint: str | None = None) -> Path:
    """Path to a ready model folder; downloads what is missing. Thread-safe (one download at a time)."""
    progress = progress or (lambda f, m: None)
    spec = MLX_MODELS[name]
    d = model_dir(name)
    if is_ready(name):
        return d
    with _lock:
        if is_ready(name):
            return d
        d.mkdir(parents=True, exist_ok=True)
        total = total_size(name)
        need = total - sum((d / f).stat().st_size for f in spec["files"] if (d / f).is_file())
        free = shutil.disk_usage(d).free
        if free < need + 300_000_000:
            raise RuntimeError(f"мало места на диске: нужно {need / 1e9:.1f} ГБ, свободно {free / 1e9:.1f} ГБ")
        base = (endpoint or config.load()["whisper"].get("hf_endpoint") or "https://huggingface.co").rstrip("/")
        status.update(name=name, state="downloading", total=total, done=0, error=None)
        try:
            done_before = 0
            for fname, (size, sha) in spec["files"].items():
                dst = d / fname
                if dst.is_file() and dst.stat().st_size == size:
                    done_before += size
                    continue
                url = f"{base}/{spec['repo']}/resolve/{spec['rev']}/{fname}"
                _download(url, dst, size, sha, lambda n: _report(done_before + n, total, progress))
                done_before += size
            status.update(state="ready", done=total)
            return d
        except Exception as e:
            status.update(state="error", error=str(e))
            raise


def _report(done: int, total: int, progress) -> None:
    status["done"] = done
    progress(done / total, f"загрузка модели распознавания (один раз): {done / 1e6:.0f} из {total / 1e6:.0f} МБ")


def _download(url: str, dst: Path, size: int, sha: str | None, on_bytes) -> None:
    part = dst.with_name(dst.name + ".part")
    for attempt in range(6):
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if have < size:
            req = urllib.request.Request(url, headers={"User-Agent": "Retake", **({"Range": f"bytes={have}-"} if have else {})})
            try:
                with urllib.request.urlopen(req, timeout=60, context=_ctx()) as r:
                    if have and r.status != 206:  # server ignored Range: start over
                        have = 0
                    with open(part, "ab" if have else "wb") as f:
                        last = 0.0
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            have += len(chunk)
                            if time.time() - last > 0.3:
                                last = time.time()
                                on_bytes(have)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt == 5:
                    raise RuntimeError(f"не удалось скачать модель: {e}") from None
                time.sleep(2 + 3 * attempt)
                continue
        on_bytes(have)
        if have != size:
            continue  # connection dropped mid-file: resume
        if sha and _sha256(part) != sha:
            part.unlink()
            raise RuntimeError("скачанный файл модели повреждён (не совпала контрольная сумма), попробуйте ещё раз")
        part.replace(dst)
        return
    raise RuntimeError("не удалось скачать модель: соединение постоянно обрывается")
