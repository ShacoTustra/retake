#!/bin/bash
# Build Retake.app and Retake-<version>.dmg on an Apple Silicon Mac. Also what CI runs.
#
#   packaging/build_mac.sh
#
# Optional environment:
#   PYTHON=python3.12        interpreter to build with (3.12+)
#   SKIP_SELFTEST=1          do not run the built app's self-test
#   CODESIGN_IDENTITY="Developer ID Application: …"   sign for distribution (default: ad-hoc)
#   NOTARY_PROFILE=name      notarize the dmg with `xcrun notarytool` keychain profile (needs CODESIGN_IDENTITY)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
PY=${PYTHON:-python3.12}
BUILD=$ROOT/build
DIST=$ROOT/dist

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || { echo "Нужен Mac на Apple Silicon"; exit 1; }
VERSION=$("$PY" -c "import sys; sys.path.insert(0, 'retake'); import runtime; print(runtime.VERSION)")
echo "== Retake $VERSION"
rm -rf "$BUILD/pyi" "$BUILD/dmg" "$DIST"
mkdir -p "$BUILD" "$DIST" packaging/bin

echo "== Python environment"
"$PY" -m venv "$BUILD/venv"
# shellcheck disable=SC1091
. "$BUILD/venv/bin/activate"
python -m pip install -q --upgrade pip
PYV=$(python -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
# MLX ships separate wheels per macOS version; take the macOS 14 ones so the app runs on 14+
# even when it is built on a newer macOS (pip would pick the newest compatible wheel)
rm -rf "$BUILD/wheels"
python -m pip download -q --only-binary=:all: --no-deps --platform macosx_14_0_arm64 \
  --python-version "$PYV" -d "$BUILD/wheels" mlx==0.32.4 mlx-metal==0.32.4
python -m pip install -q --no-deps "$BUILD"/wheels/*.whl
python -m pip install -q -r packaging/requirements-mac.txt
python -m pip install -q --no-deps mlx-whisper==0.4.3   # its torch dependency is only for weight conversion

echo "== ffmpeg / ffprobe (static arm64 build, checksums pinned)"
FF_BASE=https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2
fetch() {
  local name=$1 sha=$2
  if [ -x "packaging/bin/$name" ] && [ -f "packaging/bin/.$name.$sha" ]; then return; fi
  curl -fL --retry 3 -o "$BUILD/$name.zip" "$FF_BASE/$name.zip"
  echo "$sha  $BUILD/$name.zip" | shasum -a 256 -c -
  unzip -o -q "$BUILD/$name.zip" -d packaging/bin
  chmod +x "packaging/bin/$name"
  xattr -c "packaging/bin/$name" 2>/dev/null || true
  touch "packaging/bin/.$name.$sha"
}
fetch ffmpeg c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924
fetch ffprobe fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6
packaging/bin/ffmpeg -hide_banner -version | head -1

echo "== Icon"
ICONSET="$BUILD/Retake.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
python packaging/make_icon.py "$BUILD/icon.png"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns -o "$BUILD/Retake.icns" "$ICONSET"

echo "== PyInstaller"
python -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "$DIST" --workpath "$BUILD/pyi" packaging/retake.spec
APP="$DIST/Retake.app"

echo "== Signing"
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  codesign --force --deep --options runtime --timestamp --entitlements packaging/entitlements.plist \
    --sign "$CODESIGN_IDENTITY" "$APP"
else
  codesign --force --deep --sign - "$APP"   # ad-hoc: testers confirm the first launch in System Settings
fi
codesign --verify --deep --strict "$APP"
du -sh "$APP"

if [ "${SKIP_SELFTEST:-0}" != 1 ]; then
  echo "== Self-test of the built app"
  "$APP/Contents/MacOS/Retake" --selftest
  echo "== Launch test: the real window opens and the page loads"
  if ! "$APP/Contents/MacOS/Retake" --smoke; then
    echo "LAUNCH TEST FAILED; app log:"
    tail -80 "$HOME/Library/Application Support/Retake/logs/retake.log" || true
    exit 1
  fi
fi

echo "== Disk image"
STAGE="$BUILD/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp packaging/dmg-readme.txt "$STAGE/Как установить.txt"
DMG="$DIST/Retake-$VERSION.dmg"
hdiutil create -quiet -volname "Retake $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  codesign --sign "$CODESIGN_IDENTITY" --timestamp "$DMG"
  if [ -n "${NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
  fi
fi
ls -la "$DMG"
echo "== Done: $DMG"
