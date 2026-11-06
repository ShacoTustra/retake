"""Process-wide setup shared by the desktop app, the dev server and the self-test:
version, where bundled files live, bundled ffmpeg, the log file, single instance."""
import faulthandler
import os
import platform
import sys
import time
from pathlib import Path

VERSION = "0.1.0-beta.2"
# Where beta testers send feedback (a chat, a form, GitHub issues). Shown in the app when set.
FEEDBACK_URL = ""

FROZEN = bool(getattr(sys, "frozen", False))
LOG_MAX = 5_000_000


def resource_dir() -> Path:
    """Folder with ui/ and bin/ (inside the .app when frozen)."""
    return Path(getattr(sys, "_MEIPASS")) if FROZEN else Path(__file__).resolve().parent


def setup_tools() -> None:
    """Put the bundled ffmpeg/ffprobe first on PATH: all code (and mlx-whisper) calls them by name."""
    # running from the repository: setup_mac.sh puts them in packaging/bin
    dirs = [resource_dir() / "bin"] if FROZEN else [resource_dir().parent / "packaging" / "bin"]
    for b in dirs:
        if b.is_dir():
            os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


class _Tee:
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
                st.flush()
            except (OSError, ValueError):
                pass
        return len(s)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except (OSError, ValueError):
                pass

    def isatty(self):
        return False


def log_path() -> Path:
    import config
    d = config.app_dir() / "logs"
    d.mkdir(exist_ok=True)
    return d / "retake.log"


def setup_logging() -> Path:
    """Everything printed (progress, tracebacks of failed jobs) also goes to the log file.

    A windowed .app has no terminal, so without this an error would leave no trace."""
    p = log_path()
    try:
        if p.exists() and p.stat().st_size > LOG_MAX:
            p.replace(p.with_suffix(".log.1"))
    except OSError:
        pass
    f = open(p, "a", buffering=1, encoding="utf-8", errors="replace")
    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} Retake {VERSION} · {platform_info()} =====\n")
    # keep the terminal too: run from Terminal (or CI) the app still prints; from Finder it goes nowhere
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = _Tee(sys.stderr, f)
    faulthandler.enable(f)  # crashes inside native code (Metal, ffmpeg bindings) still leave a stack
    return p


def platform_info() -> str:
    if sys.platform == "darwin":
        osv = "macOS " + platform.mac_ver()[0]
    else:
        osv = platform.platform()
    return f"{osv}, {platform.machine()}, Python {platform.python_version()}" + (", app" if FROZEN else "")


_lock_file = None


def single_instance() -> bool:
    """False if another Retake already runs with the same data folder (two job queues would clash)."""
    global _lock_file
    try:
        import fcntl
    except ImportError:
        return True
    import config
    _lock_file = open(config.app_dir() / "retake.lock", "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
