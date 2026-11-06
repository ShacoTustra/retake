#!/bin/bash
# Prepare this repository to run on an Apple Silicon Mac, without building the app:
#
#   ./setup_mac.sh && ./Retake.command
#
# Safe to run again. SKIP_SELFTEST=1 skips the check at the end.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo "Этот скрипт для Mac на Apple Silicon. На Linux и WSL смотрите раздел о разработке в README." >&2
  exit 1
fi

# the pinned packages have builds for these versions of Python
good_python() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] in ((3, 12), (3, 13)) else 1)' 2>/dev/null
}
UV=$(command -v uv || true)
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV=$HOME/.local/bin/uv

if [ -x .venv/bin/python ] && good_python .venv/bin/python; then
  echo "== Python environment: already there"
else
  rm -rf .venv
  PY=
  for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null && good_python "$candidate"; then PY=$candidate; break; fi
  done
  if [ -n "$PY" ]; then
    echo "== Python environment: $("$PY" --version)"
    "$PY" -m venv .venv
  elif [ -n "$UV" ]; then
    echo "== Python environment: Python 3.12 through uv"
    "$UV" venv --seed --python 3.12 .venv
  else
    cat >&2 <<'MSG'
Нужен Python 3.12. Проще всего поставить uv, он скачает Python сам:

  curl -LsSf https://astral.sh/uv/install.sh | sh

Или через Homebrew: brew install python@3.12
Затем запустите ./setup_mac.sh ещё раз.
MSG
    exit 1
  fi
fi

echo "== Python packages"
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q mlx==0.32.4 mlx-metal==0.32.4 -r packaging/requirements-mac.txt
.venv/bin/python -m pip install -q --no-deps mlx-whisper==0.4.3   # torch is only for converting weights

packaging/fetch_ffmpeg.sh

if [ "${SKIP_SELFTEST:-0}" != 1 ]; then
  echo "== Self-test: speech recognition, detection and render on a short clip"
  .venv/bin/python retake/desktop.py --selftest
fi

echo
echo "Готово. Запуск: ./Retake.command"
