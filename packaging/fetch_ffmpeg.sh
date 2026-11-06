#!/bin/bash
# Static ffmpeg and ffprobe for Apple Silicon into packaging/bin, checksums pinned.
# Used by setup_mac.sh and build_mac.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== ffmpeg / ffprobe (static arm64 build, checksums pinned)"
BASE=https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p packaging/bin
fetch() {
  local name=$1 sha=$2
  if [ -x "packaging/bin/$name" ] && [ -f "packaging/bin/.$name.$sha" ]; then return; fi
  curl -fL --retry 3 -o "$TMP/$name.zip" "$BASE/$name.zip"
  echo "$sha  $TMP/$name.zip" | shasum -a 256 -c -
  unzip -o -q "$TMP/$name.zip" -d packaging/bin
  chmod +x "packaging/bin/$name"
  xattr -c "packaging/bin/$name" 2>/dev/null || true
  touch "packaging/bin/.$name.$sha"
}
fetch ffmpeg c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924
fetch ffprobe fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6
packaging/bin/ffmpeg -hide_banner -version | head -1
