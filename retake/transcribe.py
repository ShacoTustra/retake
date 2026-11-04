"""Transcribe an audio/video file into word-level JSON.

Backends:
  - faster-whisper (CUDA / CPU)  -> used for prototyping on a PC
  - mlx-whisper (Apple Silicon)  -> what will run on the iMac M1

Output JSON: {"segments": [{"start", "end", "text", "words": [{"start","end","word","p"}]}]}
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

# progress(fraction or None, message)
Progress = Callable[[float | None, str], None]

# Whisper is trained on "clean" subtitles and tends to drop fillers, false
# starts and repetitions -- exactly what we want to find. A prompt full of
# disfluencies nudges it towards verbatim transcription.
VERBATIM_PROMPT = (
    "Эээ, ну, так... Ммм, вот. Это, это — нет, ещё раз. "
    "Так, значит, э-э, первое предложение. えーと、あの、これは… "
)


def default_backend() -> str:
    return "mlx" if sys.platform == "darwin" else "faster-whisper"


def default_model() -> str:
    if default_backend() == "mlx":
        return "large-v3-turbo"  # a key of models.MLX_MODELS: downloaded into the app folder on first use
    local = Path(__file__).resolve().parent.parent / "models" / "turbo-int8"
    if (local / "model.bin").exists():
        return str(local)
    return "large-v3-turbo"


def read_wav(wav: Path):
    import wave
    import numpy as np

    with wave.open(str(wav)) as wf:  # decoded by ffmpeg already
        return np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float32) / 32768.0


def media_duration(src: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def extract_audio(src: Path, start: float, duration: float | None) -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", str(start)]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)]
    subprocess.run(cmd, check=True)
    return tmp


def _preload_cuda_libs():
    """pip-installed cuBLAS/cuDNN are not on the loader path; load them by hand."""
    import ctypes
    import glob
    import os
    import site

    for sp in site.getsitepackages():
        for pat in ("nvidia/cublas/lib/libcublasLt.so.*", "nvidia/cublas/lib/libcublas.so.*",
                    "nvidia/cudnn/lib/libcudnn*.so.*"):
            for lib in sorted(glob.glob(os.path.join(sp, pat))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


def run_faster_whisper(wav: Path, model_name: str, language: str | None, multilingual: bool,
                       total: float, progress: Progress):
    _preload_cuda_libs()
    from faster_whisper import WhisperModel

    progress(None, "загрузка модели распознавания")
    try:
        # "default" keeps the checkpoint's own precision (int8 model -> int8 kernels)
        model = WhisperModel(model_name, device="cuda", compute_type="default")
    except Exception as e:  # no GPU -> CPU fallback
        print(f"CUDA unavailable ({e}); falling back to CPU", file=sys.stderr)
        model = WhisperModel(model_name, device="cpu", compute_type="int8")

    audio = read_wav(wav)  # skip PyAV
    segments, info = model.transcribe(
        audio,
        language=language,
        multilingual=multilingual,
        beam_size=5,
        word_timestamps=True,
        initial_prompt=VERBATIM_PROMPT,
        condition_on_previous_text=False,  # avoids hallucination loops
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 150},
    )
    out = []
    for s in segments:
        out.append({
            "start": s.start,
            "end": s.end,
            "text": s.text.strip(),
            "lang": getattr(s, "language", None),
            "words": [
                {"start": w.start, "end": w.end, "word": w.word, "p": round(w.probability, 3)}
                for w in (s.words or [])
            ],
        })
        progress(min(1.0, s.end / total) if total else None, "распознавание речи")
    return out, info.language


def mlx_device() -> str:
    """Use the GPU when its kernels load; otherwise the CPU (slower, but the lesson still gets done)."""
    import mlx.core as mx
    try:
        if mx.metal.is_available():
            mx.eval(mx.array([1.0, 2.0]) * 2)  # loads mlx.metallib: fails here if the bundle lacks it
            return "gpu"
        reason = "no Metal device"
    except Exception as e:  # noqa: BLE001
        reason = str(e)
    print(f"MLX: GPU unavailable ({reason}); using the CPU", file=sys.stderr)
    mx.set_default_device(mx.cpu)
    return "cpu"


def run_mlx_whisper(wav: Path, model_name: str, language: str | None, total: float, progress: Progress):
    import mlx.core as mx
    import mlx_whisper

    import models

    mlx_device()

    if model_name in models.MLX_MODELS:
        model_path = str(models.ensure(model_name, progress))
    else:
        model_path = {"large-v3-turbo": "mlx-community/whisper-large-v3-turbo"}.get(model_name, model_name)

    # mlx-whisper reports progress only through a tqdm bar; swap in a shim that forwards it.
    # (the module, not the function: mlx_whisper.transcribe is shadowed by the function of that name)
    mt = sys.modules.get("mlx_whisper.transcribe")

    class _Bar:
        def __init__(self, total=None, **_):
            self.total, self.n = total or 0, 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def update(self, k):
            self.n += k
            if self.total:
                progress(min(1.0, self.n / self.total), "распознавание речи")

    class _Tqdm:
        tqdm = _Bar

    if mt is not None and hasattr(mt, "tqdm"):
        mt.tqdm = _Tqdm

    progress(None, "загрузка модели распознавания в память")
    audio = read_wav(wav)  # an array: mlx-whisper would otherwise call ffmpeg itself
    kw = dict(
        path_or_hf_repo=model_path,
        language=language,
        word_timestamps=True,
        initial_prompt=VERBATIM_PROMPT,
        condition_on_previous_text=False,
        verbose=False,  # makes it use the (patched) progress bar
        # no VAD in mlx-whisper: skip hallucinated text inside long silences (50 s pauses happen)
        hallucination_silence_threshold=2.0,
    )
    try:
        r = mlx_whisper.transcribe(audio, **kw)
    except TypeError:  # older mlx-whisper without that option
        kw.pop("hallucination_silence_threshold")
        r = mlx_whisper.transcribe(audio, **kw)
    finally:
        try:  # free ~2 GB: the model stays cached in mlx-whisper otherwise
            if mt is not None and hasattr(mt, "ModelHolder"):
                mt.ModelHolder.model = None
                mt.ModelHolder.model_path = None
            mx.clear_cache()
        except Exception:  # noqa: BLE001
            pass
    out = []
    for s in r["segments"]:
        out.append({
            "start": s["start"], "end": s["end"], "text": s["text"].strip(), "lang": None,
            "words": [
                {"start": w["start"], "end": w["end"], "word": w["word"], "p": round(w["probability"], 3)}
                for w in s.get("words", [])
            ],
        })
    return out, r.get("language")


def transcribe(src: Path, start: float = 0.0, duration: float | None = None, model: str | None = None,
               language: str | None = "ru", backend: str | None = None, multilingual: bool = False,
               progress: Progress | None = None) -> dict:
    progress = progress or (lambda f, m: None)
    backend = backend or default_backend()
    model = model or default_model()
    total = duration or (media_duration(src) - start)

    progress(None, "извлечение звука")
    wav = extract_audio(src, start, duration)
    t0 = time.time()
    try:
        if backend == "mlx":
            segs, lang = run_mlx_whisper(wav, model, language, total, progress)
        else:
            segs, lang = run_faster_whisper(wav, model, language, multilingual, total, progress)
    finally:
        wav.unlink(missing_ok=True)
    elapsed = time.time() - t0

    # shift timestamps back to source-file time
    for s in segs:
        s["start"] += start
        s["end"] += start
        for w in s["words"]:
            w["start"] += start
            w["end"] += start
    return {
        "source": str(src.resolve()),
        "offset": start,
        "duration": duration,
        "language": lang,
        "backend": backend,
        "model": model,
        "elapsed_sec": round(elapsed, 1),
        "segments": segs,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--start", type=float, default=0.0, help="seconds")
    ap.add_argument("--duration", type=float, default=None, help="seconds")
    ap.add_argument("--model", default=None)
    ap.add_argument("--language", default="ru", help="'auto' for detection")
    ap.add_argument("--multilingual", action="store_true", help="per-segment language detection")
    ap.add_argument("--backend", choices=["faster-whisper", "mlx"], default=None)
    args = ap.parse_args()

    def show(frac, msg):
        print(f"\r{msg} {frac * 100:5.1f}%" if frac is not None else f"\r{msg}…", end="", file=sys.stderr)

    tr = transcribe(args.src, args.start, args.duration, args.model,
                    None if args.language == "auto" else args.language, args.backend, args.multilingual, show)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(tr, ensure_ascii=False, indent=1))
    print(f"\ndone in {tr['elapsed_sec']}s, {len(tr['segments'])} segments -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
