#!/bin/bash
# Development launcher (double-click on a Mac): the app window if pywebview is installed, else the browser.
# Testers get Retake.app from the .dmg instead (see BETA.md).
cd "$(dirname "$0")" || exit 1
if .venv/bin/python -c "import webview" 2>/dev/null; then
  exec .venv/bin/python retake/desktop.py
fi
exec .venv/bin/python retake/app.py
