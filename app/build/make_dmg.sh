#!/bin/bash
set -euo pipefail

# Builds an unsigned/ad-hoc DMG for local pilot testing (migration doc §4.1).
# Classic layout: the .app + a symlink to /Applications, so installing is
# drag-and-drop. Not notarized — that requires Developer ID (doc §4.2/Phase 5),
# not done yet. Testers will need the "Open Anyway" Gatekeeper flow once per
# machine, exactly as the doc describes for this phase.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"   # app/
APP_NAME="Church Translation"
APP_BUNDLE="$APP_DIR/build/$APP_NAME.app"
DMG_PATH="$APP_DIR/build/$APP_NAME.dmg"
STAGING_DIR="$APP_DIR/build/.dmg-staging"

if [ ! -d "$APP_BUNDLE" ]; then
    echo "[make_dmg] $APP_BUNDLE not found — building it first..."
    "$SCRIPT_DIR/make_app_bundle.sh"
fi

echo "[make_dmg] Staging DMG contents..."
rm -rf "$STAGING_DIR" "$DMG_PATH"
mkdir -p "$STAGING_DIR"
cp -R "$APP_BUNDLE" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"

echo "[make_dmg] Creating $DMG_PATH..."
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING_DIR" -ov -format UDZO "$DMG_PATH"

rm -rf "$STAGING_DIR"
SIZE=$(du -h "$DMG_PATH" | cut -f1)
echo "[make_dmg] Done: $DMG_PATH ($SIZE)"
