#!/bin/bash
# Run Retake from the repository (double-click works on a Mac).
#   ./Retake.command             the app window; the browser if there is no window library
#   ./Retake.command --browser   in the default browser
# Testers get Retake.app from the .dmg instead (see BETA.md).
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  ./setup_mac.sh || { read -r -p "Установка не удалась. Нажмите Enter, чтобы закрыть окно."; exit 1; }
fi
if [ "${1:-}" != "--browser" ] && .venv/bin/python -c "import webview" 2>/dev/null; then
  exec .venv/bin/python retake/desktop.py "$@"
fi
exec .venv/bin/python retake/app.py
