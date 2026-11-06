# PyInstaller spec for Retake.app (Apple Silicon). Run through packaging/build_mac.sh.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "retake"
BUILD = ROOT / "build"
sys.path.insert(0, str(SRC))
from runtime import VERSION  # noqa: E402

datas = [
    (str(SRC / "ui"), "ui"),
    (str(ROOT / "packaging" / "THIRD_PARTY.txt"), "."),
]
binaries = [(str(ROOT / "packaging" / "bin" / t), "bin") for t in ("ffmpeg", "ffprobe")]
# our own modules are imported lazily (selftest, app inside desktop.main): list them explicitly
hiddenimports = ["app", "config", "detect", "llm", "models", "projects", "render", "report", "runtime",
                 "selftest", "transcribe", "webview.platforms.cocoa"]
hiddenimports += collect_submodules("webview.dom")
for pkg in ("mlx", "mlx_whisper"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(SRC / "desktop.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # torch is a declared dependency of mlx-whisper but is never imported for transcription
    excludes=["torch", "faster_whisper", "ctranslate2", "onnxruntime", "av", "tkinter", "matplotlib",
              "IPython", "pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Retake",
    console=False,
    target_arch="arm64",
    codesign_identity=os.environ.get("CODESIGN_IDENTITY") or None,
    entitlements_file=str(ROOT / "packaging" / "entitlements.plist") if os.environ.get("CODESIGN_IDENTITY") else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Retake")

base_version = VERSION.split("-")[0]
folder_text = "Retake читает записи уроков и сохраняет готовые ролики рядом с ними."
app = BUNDLE(
    coll,
    name="Retake.app",
    icon=str(BUILD / "Retake.icns") if (BUILD / "Retake.icns").exists() else None,
    bundle_identifier=os.environ.get("BUNDLE_ID", "app.retake.editor"),
    version=base_version,
    info_plist={
        "CFBundleDisplayName": "Retake",
        "CFBundleShortVersionString": base_version,
        "CFBundleVersion": os.environ.get("BUILD_NUMBER", "1"),
        "RetakeVersion": VERSION,
        "LSMinimumSystemVersion": "14.0",
        "LSApplicationCategoryType": "public.app-category.video",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        # the UI is served by the app itself on 127.0.0.1
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSDesktopFolderUsageDescription": folder_text,
        "NSDocumentsFolderUsageDescription": folder_text,
        "NSDownloadsFolderUsageDescription": folder_text,
        "NSRemovableVolumesUsageDescription": folder_text,
        "NSNetworkVolumesUsageDescription": folder_text,
        "NSHumanReadableCopyright": "Retake beta",
    },
)
