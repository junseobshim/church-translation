#!/bin/zsh

# ─────────────────────────────────────────────────────────────
# RC Church Translation Launcher
# ─────────────────────────────────────────────────────────────
# Intended for:
# - Automator Application
# - macOS launcher app
#
# This script:
# - Starts the control server
# - Opens the control panel in Chrome
# - Cleans up processes when closed
# ─────────────────────────────────────────────────────────────

# Project location (same across volunteer installs)
PROJECT_DIR="$HOME/Documents/church-translation"

CONTROL_PORT="${CONTROL_PORT:-9090}"
CAPTION_PORT="${CAPTION_PORT:-8080}"

CONTROL_URL="http://localhost:${CONTROL_PORT}"

# Per-user log path. /tmp is shared across macOS accounts and survives logout,
# so a log created by one user (mode 644) is unwritable by another — the `>`
# redirect below would then fail and the control server would never start.
LOG_FILE="/tmp/rc_translation.${USER:-$(id -un)}.log"

# ─────────────────────────────────────────────────────────────
# Cleanup on exit
# ─────────────────────────────────────────────────────────────

cleanup() {
    # Only tear down what this instance started. If the server was already
    # running (double-launch / reattach), we only opened Chrome — killing the
    # ports and tunnel here would destroy the live session.
    if [ -z "$SERVER_PID" ]; then
        return 0
    fi

    echo "[Launcher] Shutting down servers…"

    lsof -ti :"$CONTROL_PORT" | xargs kill -9 2>/dev/null
    lsof -ti :"$CAPTION_PORT" | xargs kill -9 2>/dev/null

    # cloudflared holds no local listening port — it makes outbound-only
    # connections to Cloudflare's edge — so the port-based kills above never catch
    # it. Reap it by name so quitting the app mid-session doesn't leave the tunnel
    # registered and competing for the shared named tunnel.
    pkill -f "cloudflared tunnel run.*church-live" 2>/dev/null

    echo "[Launcher] Done."
}

trap cleanup EXIT TERM INT

# ─────────────────────────────────────────────────────────────
# Enter project directory
# ─────────────────────────────────────────────────────────────

cd "$PROJECT_DIR" || {
    echo "[Launcher] Could not enter project directory."
    exit 1
}

# ─────────────────────────────────────────────────────────────
# Activate virtual environment
# ─────────────────────────────────────────────────────────────

if [ ! -d "venv" ]; then
    echo "[Launcher] Missing venv directory."
    exit 1
fi

# GUI-launched apps (Automator) inherit a minimal PATH from launchd that omits
# Homebrew — so tools like cloudflared aren't found. Add the common locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

source venv/bin/activate

# ─────────────────────────────────────────────────────────────
# Start control server
# ─────────────────────────────────────────────────────────────

if lsof -i :"$CONTROL_PORT" >/dev/null 2>&1; then
    echo "[Launcher] Control server already running."
else
    # Self-heal: if a previous session died ungracefully (force-quit, logout,
    # power loss), it may have orphaned a cloudflared tunnel that is still
    # competing for the shared named tunnel. We are starting fresh — no control
    # server is running on this device — so any leftover cloudflared here is
    # stale. Clear it before we begin. (Guarded by the `else`: if a session were
    # already live, we would not want to kill its tunnel.)
    pkill -f "cloudflared tunnel run.*church-live" 2>/dev/null

    # Same for a stale main.py still holding the caption port — a new session
    # would otherwise fail to bind 8080 and die at Start.
    lsof -ti :"$CAPTION_PORT" | xargs kill -9 2>/dev/null

    venv/bin/python3 control_server.py --port "$CONTROL_PORT" \
        > "$LOG_FILE" 2>&1 &

    SERVER_PID=$!

    echo "[Launcher] Control server started (PID $SERVER_PID)"
fi

# Wait for server startup
sleep 4

# Surface a failed startup (port conflict, broken venv) instead of opening
# Chrome on a dead port.
if [ -n "$SERVER_PID" ] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
    osascript -e "display alert \"Translation control server failed to start\" message \"Check ${LOG_FILE} for details.\""
    exit 1
fi

# ─────────────────────────────────────────────────────────────
# Open control panel
# ─────────────────────────────────────────────────────────────

open -a "Google Chrome" "$CONTROL_URL"

# Bring Chrome forward
osascript <<'EOF'
tell application "Google Chrome" to activate
EOF

# ─────────────────────────────────────────────────────────────
# Wait for control server exit
# ─────────────────────────────────────────────────────────────

if [ -n "$SERVER_PID" ]; then
    wait "$SERVER_PID"

    echo "[Launcher] Control server exited."

    lsof -ti :"$CAPTION_PORT" \
        | xargs kill -9 2>/dev/null
fi
