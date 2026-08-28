#!/bin/bash
set -euo pipefail

# Stages a self-contained CPython runtime + this project's Python dependencies
# into app/Resources/python/, so the shipped app depends on neither a system
# Python, a venv, nor Homebrew. See docs/native-macos-app-migration.md §3.1/§3.2.
#
# Pinned to one python-build-standalone release/build/checksum for
# reproducibility — bump all three together when upgrading.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"        # app/
REPO_ROOT="$(dirname "$APP_DIR")"         # repo root
RESOURCES_DIR="$APP_DIR/Resources"
PYTHON_DIR="$RESOURCES_DIR/python"
APP_SRC_DIR="$RESOURCES_DIR/app"
CACHE_DIR="$APP_DIR/.build-cache"

PBS_RELEASE="20260825"
PBS_PY_VERSION="3.12.14"
PBS_ASSET="cpython-${PBS_PY_VERSION}+${PBS_RELEASE}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_ASSET}"
PBS_SHA256="8b0f1fa71eab7ca644e482c631807a1116fa848491051cd1c8d9429491de63a6"

if [ "$(uname -m)" != "arm64" ]; then
    echo "[stage_python] This pin is aarch64-apple-darwin only (Apple Silicon)." >&2
    echo "  See docs/native-macos-app-migration.md §3.1 for the Intel story." >&2
    exit 1
fi

mkdir -p "$CACHE_DIR" "$RESOURCES_DIR"

TARBALL="$CACHE_DIR/$PBS_ASSET"
if [ ! -f "$TARBALL" ]; then
    echo "[stage_python] Downloading $PBS_ASSET..."
    curl -fL -o "$TARBALL" "$PBS_URL"
fi

echo "[stage_python] Verifying checksum..."
ACTUAL_SHA256="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
if [ "$ACTUAL_SHA256" != "$PBS_SHA256" ]; then
    echo "[stage_python] Checksum mismatch for $PBS_ASSET" >&2
    echo "  expected: $PBS_SHA256" >&2
    echo "  actual:   $ACTUAL_SHA256" >&2
    rm -f "$TARBALL"
    exit 1
fi

echo "[stage_python] Unpacking to $PYTHON_DIR..."
rm -rf "$PYTHON_DIR"
mkdir -p "$PYTHON_DIR"
tar -xzf "$TARBALL" -C "$PYTHON_DIR" --strip-components=1

PY="$PYTHON_DIR/bin/python3"
"$PY" --version

echo "[stage_python] Installing requirements..."
"$PY" -m pip install --quiet --no-warn-script-location -r "$REPO_ROOT/requirements.txt"

# python-docx: not a main.py/CLI dependency, only used by the app's one-shot
# .docx-outline-to-text extraction (docs/native-macos-app-migration.md §2.3).
"$PY" -m pip install --quiet --no-warn-script-location python-docx

echo "[stage_python] Pruning dead weight..."
PYLIBDIR=$(find "$PYTHON_DIR/lib" -maxdepth 1 -name "python3.*" | head -n1)
rm -rf "$PYLIBDIR/idlelib" "$PYLIBDIR/tkinter" "$PYLIBDIR/test" "$PYLIBDIR/lib2to3" \
       "$PYLIBDIR/ensurepip" "$PYLIBDIR/site-packages/pip" "$PYTHON_DIR"/bin/pip*
rm -f "$PYLIBDIR"/site-packages/pip-*.dist-info 2>/dev/null || true
find "$PYTHON_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$PYTHON_DIR" -name "*.pyc" -delete

echo "[stage_python] Copying app sources..."
mkdir -p "$APP_SRC_DIR"
cp "$REPO_ROOT/main.py" "$REPO_ROOT/transcribe_soniox.py" "$REPO_ROOT/translate_claude.py" "$APP_SRC_DIR/"
rm -rf "$APP_SRC_DIR/static"
cp -r "$REPO_ROOT/static" "$APP_SRC_DIR/"
[ -f "$REPO_ROOT/tunnels.json" ] && cp "$REPO_ROOT/tunnels.json" "$APP_SRC_DIR/"

SIZE=$(du -sh "$RESOURCES_DIR" | cut -f1)
echo "[stage_python] Done. Resources/ is now $SIZE."
