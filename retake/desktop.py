"""Retake as a Mac app: the same local server, shown in a native window (WKWebView via pywebview).

  python retake/desktop.py [--debug]     # what Retake.app runs
  python retake/desktop.py --selftest    # end-to-end check of a build, no window
  python retake/desktop.py --smoke       # open the real window, check the page loads, quit (CI)
"""
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import runtime  # noqa: E402

VIDEO_TYPES = ("Видео (*.mp4;*.mov;*.m4v;*.mkv;*.webm;*.avi;*.MP4;*.MOV)",)

RU = {
    "global.quitConfirmation": "Идёт обработка урока. Закрыть Retake? Задача прервётся, её можно будет запустить заново.",
    "global.ok": "OK",
    "global.quit": "Закрыть",
    "global.cancel": "Отмена",
    "global.saveFile": "Сохранить файл",
    "cocoa.menu.about": "О программе Retake",
    "cocoa.menu.services": "Службы",
    "cocoa.menu.view": "Вид",
    "cocoa.menu.edit": "Правка",
    "cocoa.menu.hide": "Скрыть Retake",
    "cocoa.menu.hideOthers": "Скрыть остальные",
    "cocoa.menu.showAll": "Показать все",
    "cocoa.menu.quit": "Завершить Retake",
    "cocoa.menu.fullscreen": "Во весь экран",
    "cocoa.menu.cut": "Вырезать",
    "cocoa.menu.copy": "Скопировать",
    "cocoa.menu.paste": "Вставить",
    "cocoa.menu.selectAll": "Выбрать всё",
}


class Bridge:
    """Exposed to the page as window.pywebview.api.* (names starting with _ stay private)."""

    def __init__(self):
        self._window = None

    def pick_file(self):
        import webview
        r = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=VIDEO_TYPES)
        return r[0] if r else None


def on_loaded(window):
    """Dropped files carry their full path only through a Python-side drop handler."""
    try:
        from webview.dom import DOMEventHandler

        def on_drop(e):
            files = (e.get("dataTransfer") or {}).get("files") or []
            paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
            if paths:
                window.evaluate_js(f"window.retakeDropped && window.retakeDropped({json.dumps(paths)})")

        window.dom.document.events.drop += DOMEventHandler(on_drop, prevent_default=True)
    except Exception as e:  # noqa: BLE001 -- drag-and-drop is a convenience, the button still works
        print(f"drag-and-drop unavailable: {e}", file=sys.stderr)


def message(text: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display dialog {json.dumps(text)} buttons {{"OK"}} '
                                           f'default button 1 with title "Retake"'], capture_output=True)
    else:
        print(text, file=sys.stderr)


def main():
    multiprocessing.freeze_support()
    runtime.setup_logging()
    runtime.setup_tools()
    if "--selftest" in sys.argv:
        import selftest
        sys.exit(selftest.main([a for a in sys.argv[1:] if a != "--selftest"]))
    if not runtime.single_instance():
        message("Retake уже открыт.")
        return

    import webview

    import app

    app.DESKTOP = True
    srv, url = app.start_server(port=0)
    print(f"Retake {runtime.VERSION}: {url}", flush=True)

    bridge = Bridge()
    win = webview.create_window("Retake", url, js_api=bridge, width=1440, height=920, min_size=(980, 640),
                                text_select=True, background_color="#f6f5f2")
    bridge._window = win
    win.events.loaded += on_loaded

    def on_closing(window):
        # the localized "really quit?" question is shown only while a lesson is being processed
        window.confirm_close = app.jobs_running()

    win.events.closing += on_closing
    result: dict = {}
    smoke = "--smoke" in sys.argv
    if smoke:  # never hang a CI job
        threading.Thread(target=lambda: (time.sleep(120), print("SMOKE: timeout", flush=True), os._exit(3)),
                         daemon=True).start()
    print("starting window", flush=True)
    webview.start(func=smoke_check if smoke else None, args=(win, result) if smoke else None,
                  localization=RU, private_mode=False, debug="--debug" in sys.argv,
                  storage_path=str(config.app_dir() / "webview"))
    srv.shutdown()
    if smoke:
        print("SMOKE " + ("PASSED" if result.get("ok") else "FAILED"), flush=True)
        sys.exit(0 if result.get("ok") else 1)


def smoke_check(win, result: dict) -> None:
    """Runs beside the GUI loop: the page must load and talk to the server and to Python."""
    try:
        if not win.events.loaded.wait(60):
            print("SMOKE: the page did not load in 60 s", flush=True)
            return
        for _ in range(40):  # the page fetches /api/state, then renders the version
            r = win.evaluate_js("JSON.stringify({ver: (document.querySelector('#ver') || {}).textContent || '',"
                                " api: !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_file)})")
            info = json.loads(r) if isinstance(r, str) else (r or {})
            if info.get("ver") and info.get("api"):
                break
            time.sleep(0.5)
        print(f"SMOKE page: {info}", flush=True)
        result["ok"] = bool(info.get("ver")) and bool(info.get("api"))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        win.destroy()


def run():
    """Entry point: an error at start-up is shown in a dialog, not a silent exit."""
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        traceback.print_exc()
        try:
            log = runtime.log_path()
        except Exception:  # noqa: BLE001
            log = "~/Library/Application Support/Retake/logs/retake.log"
        message(f"Retake не смог запуститься:\n{e.__class__.__name__}: {e}\n\n"
                f"Подробности в журнале:\n{log}\nПришлите этот файл разработчику.")
        sys.exit(1)


if __name__ == "__main__":
    run()
