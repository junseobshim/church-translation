#!/bin/bash
set -euo pipefail

# Wraps the SwiftPM executable into a real, minimal .app bundle (Info.plist +
# Contents/MacOS/Contents/Resources), ad-hoc signed. This exists mainly so
# macOS TCC (microphone permission) has a stable app identity to grant to —
# a bare Mach-O launched via raw exec has no Info.plist / NSMicrophoneUsageDescription
# and no stable identity across rebuilds, so permission grants don't reliably
# stick. See docs/native-macos-app-migration.md §3.3/§3.4.

CONFIG="${1:-debug}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"   # app/
APP_NAME="Church Translation"
BUNDLE="$APP_DIR/build/$APP_NAME.app"
EXECUTABLE="$APP_DIR/.build/$CONFIG/ChurchTranslation"

if [ ! -x "$EXECUTABLE" ]; then
    echo "[make_app_bundle] Building ($CONFIG)..."
    (cd "$APP_DIR" && swift build -c "$CONFIG")
fi

echo "[make_app_bundle] Assembling $BUNDLE..."
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cp "$EXECUTABLE" "$BUNDLE/Contents/MacOS/ChurchTranslation"
cp "$APP_DIR/Info.plist" "$BUNDLE/Contents/Info.plist"

if [ -d "$APP_DIR/Resources/python" ]; then
    echo "[make_app_bundle] Including staged Python runtime..."
    cp -R "$APP_DIR/Resources/python" "$BUNDLE/Contents/Resources/python"
fi
if [ -d "$APP_DIR/Resources/app" ]; then
    echo "[make_app_bundle] Including staged app sources..."
    cp -R "$APP_DIR/Resources/app" "$BUNDLE/Contents/Resources/app"
fi

echo "[make_app_bundle] Ad-hoc signing..."
# Inside-out: leaf Mach-Os first (matters for real Developer ID signing later;
# harmless no-op ordering for ad-hoc). --deep does not reliably descend into
# Resources, per the doc — sign explicitly instead of relying on it.
find "$BUNDLE/Contents/Resources" \( -name "*.so" -o -name "*.dylib" \) -exec \
    codesign --force --sign - {} \; 2>/dev/null || true
[ -f "$BUNDLE/Contents/Resources/python/bin/python3.12" ] && \
    codesign --force --sign - "$BUNDLE/Contents/Resources/python/bin/python3.12"
codesign --force --sign - "$BUNDLE/Contents/MacOS/ChurchTranslation"
codesign --force --sign - "$BUNDLE"

echo "[make_app_bundle] Done: $BUNDLE"
