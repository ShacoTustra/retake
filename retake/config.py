"""Settings: built-in defaults <- settings.json in the app folder <- environment / .env (fills blanks).

LLM settings work with any OpenAI-compatible Chat Completions API:
  proxyapi.ru, OpenAI, OpenRouter, a local Ollama (http://localhost:11434/v1) or LM Studio (http://localhost:1234/v1).

Environment variables (also read from a .env file next to the project or in the current folder):
  LLM_BASE_URL, LLM_MODEL, LLM_API_KEY (falls back to API_KEY, OPENAI_API_KEY), RETAKE_HOME (app folder)
"""
import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    # model "" = automatic; hf_endpoint: where the Mac build downloads the model from (a mirror if
    # huggingface.co is slow or blocked)
    "whisper": {"model": "", "language": "ru", "hf_endpoint": "https://huggingface.co"},
    "detect": {"phrase_gap": 0.5, "retake_window": 25.0, "min_pause": 1.2, "keep_pause": 0.8},
    "llm": {
        "enabled": False,
        "base_url": "",
        "model": "",
        "api_key": "",
        "temperature": 0.0,
        "timeout": 300,
        "max_chars": 40000,  # transcript characters per request; longer lessons are split
        "max_tokens": 16000,  # answer length limit (including the model's thinking)
        "reasoning_effort": "low",  # "", "low", "medium", "high" -- for models that think first
        "auto_apply": "high",  # new cuts found by the model are pre-selected from this confidence up
    },
    "render": {"encoder": ""},  # "" = automatic
}


def app_dir() -> Path:
    if os.environ.get("RETAKE_HOME"):
        d = Path(os.environ["RETAKE_HOME"]).expanduser()
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "Retake"
    else:
        d = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "retake"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    return app_dir() / "settings.json"


def read_dotenv() -> dict:
    env = {}
    for f in (ROOT / ".env", Path.cwd() / ".env"):
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("'\""))
    return env


def _env(*names: str) -> str:
    dot = read_dotenv()
    for n in names:
        v = os.environ.get(n) or dot.get(n)
        if v:
            return v
    return ""


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif k in out or not isinstance(base, dict):
            out[k] = v
    return out


def load_saved() -> dict:
    p = settings_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def load() -> dict:
    """Effective settings. Values from the environment only fill fields left empty."""
    s = _merge(DEFAULTS, load_saved())
    llm = s["llm"]
    llm["base_url"] = llm["base_url"] or _env("LLM_BASE_URL", "OPENAI_BASE_URL")
    llm["model"] = llm["model"] or _env("LLM_MODEL")
    llm["key_from_env"] = not llm["api_key"] and bool(_env("LLM_API_KEY", "API_KEY", "OPENAI_API_KEY"))
    llm["api_key"] = llm["api_key"] or _env("LLM_API_KEY", "API_KEY", "OPENAI_API_KEY")
    return s


def save(patch: dict) -> dict:
    """Merge a partial update into settings.json (the file is private: it may hold an API key)."""
    cur = _merge(DEFAULTS, load_saved())
    new = _merge(cur, patch)
    new["llm"].pop("key_from_env", None)
    p = settings_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    return load()


def mask(key: str) -> str:
    return (key[:5] + "…" + key[-4:]) if len(key) > 12 else ("•" * len(key))


def public(s: dict) -> dict:
    """Settings safe to send to the browser: the API key is never returned."""
    out = copy.deepcopy(s)
    key = out["llm"].pop("api_key", "")
    out["llm"]["has_key"] = bool(key)
    out["llm"]["key_hint"] = mask(key) if key else ""
    return out
