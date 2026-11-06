"""Retake as a person uses it, on a real Mac desktop (macOS only, run by CI):

  python tests/mac_app_check.py --screens DIR -- COMMAND...     # COMMAND starts the app

Real clicks, the system file dialog, files dragged from Finder, keys in the editor, export, the
problem report, quit and start again. It drives the app from outside, through Accessibility and
synthetic input, so the same check runs on a built Retake.app.

Needs the Accessibility permission (CI runners have it) and, on top of what the app itself uses,
pyobjc-framework-ApplicationServices. The app gets its own empty data folder.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="retake-app-")).resolve()
os.environ["RETAKE_HOME"] = str(HOME)
sys.path.insert(0, str(REPO / "retake"))
import AppKit  # noqa: E402
import HIServices  # noqa: E402
import Quartz  # noqa: E402
import runtime  # noqa: E402

runtime.setup_tools()
import projects  # noqa: E402

VIDEOS = HOME / "видео"
READY = VIDEOS / "Готовый урок.mp4"
# й and ё have two Unicode forms and a file name can be stored in either: Finder and Cocoa often
# write the decomposed one, an app like Lesson Studio the composed one
BY_BUTTON = VIDEOS / "Японский по кнопке.mp4"
DRAGGED = [VIDEOS / "Японский ёж, готовая форма.mp4",
           VIDEOS / unicodedata.normalize("NFD", "Японский ёж, разложенная форма.mp4")]
CUT = (8.0, 10.0)
KEY = {"return": 36, "tab": 48, "space": 49, "x": 7, "z": 6, "g": 5, "v": 9, "q": 12}
CMD, SHIFT = Quartz.kCGEventFlagMaskCommand, Quartz.kCGEventFlagMaskShift


def nfc(s) -> str:
    return unicodedata.normalize("NFC", str(s))


# ---- input

def post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def mouse(kind, x: float, y: float) -> None:
    post(Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft))


def click(x: float, y: float) -> None:
    mouse(Quartz.kCGEventMouseMoved, x, y)
    time.sleep(0.15)
    mouse(Quartz.kCGEventLeftMouseDown, x, y)
    time.sleep(0.06)
    mouse(Quartz.kCGEventLeftMouseUp, x, y)
    time.sleep(0.3)


def drag(a: tuple, b: tuple) -> None:
    mouse(Quartz.kCGEventMouseMoved, *a)
    time.sleep(0.4)
    mouse(Quartz.kCGEventLeftMouseDown, *a)
    time.sleep(0.6)
    for i in range(1, 41):
        mouse(Quartz.kCGEventLeftMouseDragged, a[0] + (b[0] - a[0]) * i / 40, a[1] + (b[1] - a[1]) * i / 40)
        time.sleep(0.03)
    time.sleep(0.8)  # the target reports whether it takes the drop
    mouse(Quartz.kCGEventLeftMouseDragged, *b)
    time.sleep(0.4)
    mouse(Quartz.kCGEventLeftMouseUp, *b)
    time.sleep(0.5)


def key(name: str, flags: int = 0) -> None:
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(None, KEY[name], down)
        Quartz.CGEventSetFlags(e, flags)
        post(e)
        time.sleep(0.05)
    time.sleep(0.25)


def paste(text: str) -> None:
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
    key("v", CMD)


# ---- what is on the screen

def ax(el, name: str):
    err, value = HIServices.AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def walk(el, depth: int = 0):
    yield el
    if depth < 40:
        for child in ax(el, "AXChildren") or []:
            yield from walk(child, depth + 1)


def own_text(el) -> str:
    parts = (ax(el, k) for k in ("AXTitle", "AXDescription", "AXValue", "AXFilename"))
    return " ".join(nfc(p) for p in parts if isinstance(p, str))


def text_of(el) -> str:
    return " ".join(own_text(e) for e in walk(el, 37))


def page_text(pid: int) -> str:
    return " ".join(own_text(e) for e in walk(windows(pid)[0]))


def rect(el) -> tuple:
    pos, size = str(ax(el, "AXPosition")), str(ax(el, "AXSize"))
    x, y = re.search(r"x:(-?[\d.]+) y:(-?[\d.]+)", pos).groups()
    w, h = re.search(r"w:([\d.]+) h:([\d.]+)", size).groups()
    return float(x), float(y), float(w), float(h)


def center(el) -> tuple:
    x, y, w, h = rect(el)
    return x + w / 2, y + h / 2


def app_element(pid: int):
    return HIServices.AXUIElementCreateApplication(pid)


def windows(pid: int) -> list:
    return list(ax(app_element(pid), "AXWindows") or [])


def find(pid: int, text: str, role: str = "AXButton", timeout: float = 20):
    """The first element of that role showing that text, in any window of the process."""
    deadline = time.time() + timeout
    while True:
        for win in windows(pid):
            for el in walk(win):
                if ax(el, "AXRole") == role and text in text_of(el):
                    return el
        if time.time() > deadline:
            return None
        time.sleep(0.4)


class App:
    def __init__(self, command: list, screens: Path):
        self.command, self.screens = command, screens
        self.proc = None
        self.url = self.token = ""
        self.output: list = []
        self.shots = 0

    def start(self) -> None:
        self.proc = subprocess.Popen(self.command, env=os.environ, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, errors="replace")
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        deadline = time.time() + 120
        self.url = ""
        while not self.url:
            if self.proc.poll() is not None or time.time() > deadline:
                raise RuntimeError("the app did not start:\n" + "".join(self.output[-40:]))
            time.sleep(0.2)
        with urllib.request.urlopen(self.url, timeout=10) as r:
            self.token = json.loads(re.search(r"window\.RETAKE = (\{.*?\});", r.read().decode())[1])["token"]
        deadline = time.time() + 60
        while not windows(self.proc.pid):
            if time.time() > deadline:
                raise RuntimeError("no window")
            time.sleep(0.3)
        self.activate()

    def _read(self, proc) -> None:
        for line in proc.stdout:
            self.output.append(line)
            if m := re.search(r"Retake \S+: (http://\S+)", line):
                self.url = m[1]

    @property
    def pid(self) -> int:
        return self.proc.pid

    def activate(self) -> None:
        running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid)
        running.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.5)

    def state(self) -> dict:
        req = urllib.request.Request(self.url + "api/state", headers={"X-Retake-Token": self.token})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def sources(self) -> list:
        return [nfc(p["source"]) for p in self.state()["projects"]]

    def shot(self, name: str) -> None:
        self.shots += 1
        self.screens.mkdir(parents=True, exist_ok=True)
        subprocess.run(["screencapture", "-x", str(self.screens / f"{self.shots:02d}-{name}.png")], check=False)

    def press(self, text: str, role: str = "AXButton", timeout: float = 20) -> None:
        el = find(self.pid, text, role, timeout)
        if el is None:
            raise RuntimeError(f"nothing to press: {text}")
        HIServices.AXUIElementPerformAction(el, "AXScrollToVisible")  # a card further down the list
        time.sleep(0.4)
        click(*center(el))

    def wait(self, what: str, fn, timeout: float = 20):
        deadline = time.time() + timeout
        while True:
            value = fn()
            if value:
                return value
            if time.time() > deadline:
                raise RuntimeError(f"not seen in {timeout:g} s: {what}")
            time.sleep(0.3)


# ---- what to prepare

def ffmpeg(*args) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def make_clip(path: Path, seconds: int) -> None:
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=640x360:r=25:d={seconds}", "-f", "lavfi", "-i",
           f"sine=f=440:r=48000:d={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "50",
           "-c:a", "aac", str(path))


def ready_project(src: Path) -> projects.Project:
    p = projects.create(src)
    words = [{"s": t + 0.1, "e": t + 0.8, "t": f" слово{t}"} for t in range(20)]
    phrases = [{"start": w["s"], "end": w["e"], "w0": i, "w1": i + 1} for i, w in enumerate(words)]
    doc = {"source": str(src), "range": [0.0, 20.0], "words": words, "phrases": phrases,
           "edits": [{"id": 1, "kind": "retake", "start": CUT[0], "end": CUT[1], "on": True, "why": "проверка"}]}
    (p.dir / "edits.json").write_text(json.dumps(doc, ensure_ascii=False))
    p.save_meta(status={"state": "done"})
    return p


def prepare() -> projects.Project:
    VIDEOS.mkdir()
    make_clip(READY, 20)
    tmp = VIDEOS / "clip.mp4"
    make_clip(tmp, 3)
    for dst in [BY_BUTTON, *DRAGGED]:
        subprocess.run(["cp", str(tmp), str(dst)], check=True)
    tmp.unlink()
    # imported files stay unprocessed: this check is about getting them in, the self-test covers the rest
    (HOME / "settings.json").write_text(json.dumps({"whisper": {"hf_endpoint": "http://127.0.0.1:9"}}))
    return ready_project(READY)


# ---- the steps

def step_open_editor(a: App, project: projects.Project) -> None:
    a.press("Открыть")
    a.wait("the editor", lambda: find(a.pid, "Экспорт", timeout=1), 30)
    time.sleep(1.5)
    a.shot("editor")
    if find(a.pid, "Понятно", timeout=5) is None:
        raise RuntimeError("the first-start hint is not there")
    a.press("Понятно")


def decision(project: projects.Project):
    return (project.decisions()["overrides"].get("1") or {}).get("on")


def step_keys(a: App, project: projects.Project) -> None:
    a.activate()
    x, y, w, h = rect(windows(a.pid)[0])
    click(x + w / 2, y + 70)  # a neutral spot, so no button holds the keyboard
    key("tab")   # first suggestion
    key("x")     # keep it after all
    a.wait("X undoes the cut in decisions.json", lambda: decision(project) is False, 6)
    a.shot("after-x")
    key("z", CMD)
    a.wait("⌘Z brings the cut back", lambda: decision(project) in (True, None), 6)

    before = page_text(a.pid)  # the time on the page moves only if the video really plays
    key("space")
    time.sleep(1.5)
    after = page_text(a.pid)
    key("space")
    a.shot("played")
    if before == after:
        raise RuntimeError("space: the time on the page did not move")


def step_export(a: App, project: projects.Project) -> None:
    a.activate()
    a.press("Экспорт")
    time.sleep(0.4)
    a.press("Видео MP4")
    out = READY.with_name(f"{READY.stem} (монтаж).mp4")


    def finished():
        status = project.meta.get("status") or {}
        return out.exists() and status.get("job") == "render" and status.get("state") == "done"

    a.wait("the finished video", finished, 90)
    a.shot("exported")
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                       capture_output=True, text=True, check=True)
    length, want = float(r.stdout), 20 - (CUT[1] - CUT[0])
    if abs(length - want) > 0.3:
        raise RuntimeError(f"the video is {length:.2f} s, expected {want:.2f} s")


def step_report(a: App, project: projects.Project) -> None:
    downloads = Path.home() / "Downloads"
    before = set(downloads.glob("Retake-*.zip"))
    a.activate()
    a.press("Экспорт")
    time.sleep(0.4)
    a.press("Сообщить о проблеме")
    time.sleep(1)
    a.shot("report-question")
    key("return")  # the page's confirm(): a native alert in the app window
    a.wait("the report in Downloads", lambda: set(downloads.glob("Retake-*.zip")) - before, 20)
    time.sleep(3)  # Finder comes up with the file
    a.shot("report-done")


def step_back_home(a: App, project: projects.Project) -> None:
    a.activate()
    a.press("Уроки", role="AXLink")
    a.wait("the lesson list", lambda: find(a.pid, "Добавить запись урока", timeout=1), 15)
    time.sleep(0.5)


def step_open_again(a: App, project: projects.Project) -> None:
    a.activate()
    a.press("Открыть")
    a.wait("the editor", lambda: find(a.pid, "Экспорт", timeout=1), 30)
    time.sleep(1)


def ensure_home(a: App) -> None:
    """An earlier failure must not hide whether importing works: start again on the lesson list."""
    if find(a.pid, "Добавить запись урока", timeout=2) is None:
        print("(not on the lesson list: restarting the app)", flush=True)
        step_quit(a, None)
        a.start()
        time.sleep(2)


def step_button(a: App, project: projects.Project) -> None:
    ensure_home(a)
    a.activate()
    count = len(windows(a.pid))
    a.press("Добавить запись урока")
    a.wait("the system file dialog", lambda: len(windows(a.pid)) > count, 10)
    time.sleep(1)
    a.shot("file-dialog")
    key("g", CMD | SHIFT)  # Go to Folder
    time.sleep(1.2)
    paste(str(BY_BUTTON))
    time.sleep(1.2)
    a.shot("go-to-file")
    key("return")
    time.sleep(1.5)
    a.shot("file-chosen")
    key("return")
    a.wait("the lesson chosen in the dialog", lambda: nfc(BY_BUTTON) in a.sources(), 15)


def finder_item(path: Path):
    """Show the file in Finder and return (where it is on screen, the Finder window's rectangle)."""
    subprocess.run(["open", "-R", str(path)], check=True)
    finder = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.finder")[0]
    name = nfc(path.name)
    deadline = time.time() + 20
    while time.time() < deadline:
        for win in windows(finder.processIdentifier()):
            for el in walk(win):
                # the icon or the name of a file, not the toolbar or the status bar
                if ax(el, "AXRole") in ("AXImage", "AXTextField", "AXStaticText", "AXRow", "AXCell") \
                        and name in own_text(el):
                    return center(el), rect(win)
        time.sleep(0.5)
    raise RuntimeError(f"Finder does not show {name}")


def step_drag(a: App, path: Path) -> None:
    ensure_home(a)
    for attempt in range(1, 4):  # repeated: the list refreshes under the pointer at its own moments
        item, (fx, fy, fw, fh) = finder_item(path)
        x, y, w, h = rect(windows(a.pid)[0])
        spots = [(x + w * kx, y + h * ky) for kx in (0.92, 0.5, 0.08) for ky in (0.85, 0.5, 0.15)]
        target = next((s for s in spots if not (fx <= s[0] <= fx + fw and fy <= s[1] <= fy + fh)), None)
        if target is None:
            raise RuntimeError("Finder covers the app window")
        if attempt == 1:
            a.shot("before-drag")
        drag(item, target)
        a.wait(f"the dragged lesson {nfc(path.name)}", lambda: nfc(path) in a.sources(), 15)
        time.sleep(1.5)
        if find(a.pid, "Добавить запись урока", timeout=3) is None:
            raise RuntimeError(f"drag {attempt}: the window opened the file instead of keeping the page")
    a.shot("after-drag")


def step_quit(a: App, project: projects.Project) -> None:
    a.activate()
    key("q", CMD)
    time.sleep(1.5)
    if a.proc.poll() is None:  # lessons are being processed: the app asks first
        a.shot("quit-question")
        key("return")
    a.proc.wait(20)


def step_second_start(a: App, project: projects.Project) -> None:
    first = a.url
    a.start()
    if a.url != first:
        raise RuntimeError(f"another address after a restart, so the window forgets its settings: {first} {a.url}")
    a.press("Открыть")
    a.wait("the editor", lambda: find(a.pid, "Экспорт", timeout=1), 30)
    time.sleep(2)
    a.shot("second-start")
    if find(a.pid, "Понятно", timeout=2) is not None:
        raise RuntimeError("the first-start hint is back: settings were not kept")
    a.activate()
    key("q", CMD)
    time.sleep(1.5)
    if a.proc.poll() is None:
        key("return")
    a.proc.wait(20)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screens", type=Path, default=Path("screens"))
    ap.add_argument("command", nargs="+")
    args = ap.parse_args()

    trusted = HIServices.AXIsProcessTrusted()
    screen = AppKit.NSScreen.mainScreen().frame().size
    print(f"accessibility: {trusted}, screen: {screen.width:g}×{screen.height:g}", flush=True)
    project = prepare()
    a = App(args.command, args.screens)
    a.start()
    time.sleep(2)
    a.shot("home")

    steps = [("open the editor", step_open_editor), ("the link back to the lessons", step_back_home),
             ("open it again", step_open_again), ("keys in the editor", step_keys), ("export", step_export),
             ("problem report", step_report), ("back after the report", step_back_home),
             ("add by button and file dialog", step_button)]
    steps += [(f"drag from Finder: {nfc(p.name)}", lambda a, _, p=p: step_drag(a, p)) for p in DRAGGED]
    steps += [("quit", step_quit), ("second start keeps settings", step_second_start)]

    failed = []
    for name, fn in steps:
        t0 = time.time()
        try:
            fn(a, project)
            print(f"[ OK ] {name} ({time.time() - t0:.1f} s)", flush=True)
        except Exception as e:  # noqa: BLE001 -- the next steps still say something
            failed.append(name)
            print(f"[FAIL] {name}: {e}", flush=True)
            traceback.print_exc(file=sys.stdout)
            a.shot("failed")
    if a.proc.poll() is None:
        a.proc.kill()
    print("---- the app's output ----\n" + "".join(a.output[-60:]), flush=True)
    print("APP CHECK " + (f"FAILED: {', '.join(failed)}" if failed else "PASSED"), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    # a hung dialog must not hold the job until its time limit
    threading.Thread(target=lambda: (time.sleep(420), print("APP CHECK: timeout", flush=True), os._exit(3)),
                     daemon=True).start()
    sys.exit(main())
