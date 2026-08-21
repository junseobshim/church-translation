#!/usr/bin/env python3
"""
control_server.py — Volunteer control panel server for RC Church Translation.

Serves the control UI at http://localhost:9090 and manages the main.py session.

Usage:
    python control_server.py
    python control_server.py --port 9090

Then open http://localhost:9090 in a browser.
"""

import json
import os
import shutil
import sys
import signal
import subprocess
import threading
import tempfile
import argparse
import time
import http.server
from pathlib import Path
from typing import Optional, List, Dict

# ── State ──────────────────────────────────────────────────────────────────────

_session_proc: Optional[subprocess.Popen] = None
_session_lock = threading.Lock()
_outline_temp: Optional[tempfile.NamedTemporaryFile] = None

# Heartbeat — set to current time whenever /api/heartbeat is called.
_last_heartbeat: float = 0.0
# Crash backstop only (force-quit, power loss, tab discard) — intentional
# closes are handled by the /api/goodbye beacon within seconds. Must sit well
# above 60s: Chrome throttles hidden-tab timers to ~1/min after 5 minutes in
# the background, so a backgrounded panel heartbeats that slowly.
_HEARTBEAT_TIMEOUT = 90  # seconds; browser pings every 4s (~1/min when throttled)
_GOODBYE_GRACE = 5  # seconds after /api/goodbye before shutdown; any heartbeat cancels

# Reference to the HTTPServer so handlers can call server.shutdown()
_http_server: Optional[http.server.HTTPServer] = None

# ── Helpers ────────────────────────────────────────────────────────────────────

def _ignore_further_signals() -> None:
    """Make shutdown one-shot, so teardown always runs to completion."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass  # not the main thread; the handler is already installed there


def get_audio_devices() -> List[Dict]:
    """Return list of audio input devices via sounddevice."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        result = []
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                result.append({"index": i, "name": dev["name"]})
        return result
    except Exception as e:
        return [{"index": -1, "name": f"Error loading devices: {e}"}]


def _tunnel_url_map() -> Dict[str, str]:
    """Tunnel name → public URL, from tunnels.json. Empty if unreadable."""
    path = Path(__file__).parent / "tunnels.json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("tunnels", {})
    except (OSError, ValueError):
        return {}


def _local_tunnel_ids() -> set:
    """Tunnel UUIDs this device holds credentials for.

    A tunnel can only be *run* here if its <uuid>.json credentials file exists.
    Note this is a convenience filter, not a security boundary: cert.pem lets
    this device mint credentials for any tunnel on the account via
    `cloudflared tunnel token --cred-file`.
    """
    ids = set()
    for creds in Path.home().joinpath(".cloudflared").glob("*.json"):
        try:
            with open(creds, encoding="utf-8") as f:
                tunnel_id = json.load(f).get("TunnelID")
            if tunnel_id:
                ids.add(tunnel_id)
        except (OSError, ValueError):
            continue
    return ids


def get_tunnels() -> List[Dict]:
    """Named tunnels runnable on this device, each with its public URL.

    `cloudflared tunnel list` is a network call against the Cloudflare API, so it
    lists every tunnel on the account — including ones with no credentials here —
    and fails outright when offline. Intersect with the on-disk credentials and
    fall back to the tunnels.json mapping so the panel still offers choices when
    the API is unreachable.
    """
    url_map = _tunnel_url_map()
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        # GUI launches inherit a launchd PATH without Homebrew (see main.py).
        for candidate in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
            if os.path.exists(candidate):
                cloudflared = candidate
                break

    if cloudflared:
        try:
            out = subprocess.run(
                [cloudflared, "tunnel", "list", "--output", "json"],
                capture_output=True, timeout=6, check=True,
            ).stdout
            local_ids = _local_tunnel_ids()
            found = [
                {"name": t["name"], "id": t["id"], "url": url_map.get(t["name"])}
                for t in json.loads(out)
                if t.get("id") in local_ids
            ]
            # Order by tunnels.json rather than the API's creation-date order, so
            # the panel's default selection (first entry) is ours to control and
            # can't silently change when a tunnel is added. Unmapped tunnels last.
            order = list(url_map)
            found.sort(key=lambda t: order.index(t["name"]) if t["name"] in order else len(order))
            return found
        except (subprocess.SubprocessError, OSError, ValueError, KeyError):
            pass

    # Offline / cloudflared missing: names from the map, URLs included, no IDs.
    return [{"name": name, "id": None, "url": url} for name, url in url_map.items()]


def build_command(payload: dict, outline_path: Optional[str]) -> list[str]:
    # Use the venv Python explicitly so dependencies (sounddevice, etc.) are available
    venv_python = Path(__file__).parent / "venv" / "bin" / "python3"
    python = str(venv_python) if venv_python.exists() else sys.executable
    cmd = [python, "main.py"]
    cmd += ["--source", payload["source"]]
    cmd += ["--target", ",".join(payload["targets"])]
    if payload.get("device") is not None:
        cmd += ["--device", str(payload["device"])]
    cmd += ["--port", str(payload.get("port", 8080))]
    # `tunnel` is a tunnel name, or null/false for no tunnel. Older clients sent
    # a bare boolean, where true meant main.py's default tunnel.
    tunnel = payload.get("tunnel", True)
    if not tunnel:
        cmd.append("--no-tunnel")
    elif isinstance(tunnel, str):
        cmd += ["--tunnel", tunnel]
    if outline_path:
        cmd += ["--outline", outline_path]
    return cmd


def stream_output(proc: subprocess.Popen):
    """Stream subprocess stdout/stderr to this process's terminal."""
    def _stream(pipe):
        for line in iter(pipe.readline, b""):
            try:
                sys.stdout.write(line.decode("utf-8", errors="replace"))
                sys.stdout.flush()
            except Exception:
                pass

    t1 = threading.Thread(target=_stream, args=(proc.stdout,), daemon=True)
    t2 = threading.Thread(target=_stream, args=(proc.stderr,), daemon=True)
    t1.start()
    t2.start()


# ── HTTP Handler ───────────────────────────────────────────────────────────────

class ControlHandler(http.server.BaseHTTPRequestHandler):

    # ── Static UI ──────────────────────────────────────────────────────────────
    def _serve_ui(self):
        ui_path = Path(__file__).parent / "control.html"
        if not ui_path.exists():
            self.send_error(404, "control.html not found next to control_server.py")
            return
        html = ui_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html)

    # ── /api/devices ───────────────────────────────────────────────────────────
    def _serve_devices(self):
        devices = get_audio_devices()
        self._json(200, devices)

    # ── /api/tunnels ───────────────────────────────────────────────────────────
    def _serve_tunnels(self):
        self._json(200, get_tunnels())

    # ── /api/start ─────────────────────────────────────────────────────────────
    def _handle_start(self):
        global _session_proc, _outline_temp

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._json(400, {"error": "Invalid JSON"})
            return

        with _session_lock:
            if _session_proc and _session_proc.poll() is None:
                self._json(409, {"error": "Session already running"})
                return

            # Write outline to temp file if provided
            outline_path = None
            if payload.get("outline"):
                try:
                    _outline_temp = tempfile.NamedTemporaryFile(
                        mode="w", suffix=".txt", encoding="utf-8", delete=False
                    )
                    _outline_temp.write(payload["outline"])
                    _outline_temp.flush()
                    _outline_temp.close()
                    outline_path = _outline_temp.name
                except Exception as e:
                    self._json(500, {"error": f"Could not write outline: {e}"})
                    return

            cmd = build_command(payload, outline_path)
            print(f"\n[ControlServer] Starting: {' '.join(cmd)}\n")

            try:
                _session_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=Path(__file__).parent,
                )
                stream_output(_session_proc)
                self._json(200, {"ok": True, "pid": _session_proc.pid})
            except Exception as e:
                self._json(500, {"error": str(e)})

    # ── /api/stop ──────────────────────────────────────────────────────────────
    def _handle_stop(self, respond: bool = True):
        """Stop the running session. `respond=False` lets _handle_shutdown reuse
        this without writing a second HTTP response onto the same connection."""
        global _session_proc, _outline_temp

        with _session_lock:
            if _session_proc:
                try:
                    _session_proc.send_signal(signal.SIGINT)
                    _session_proc.wait(timeout=5)
                except Exception:
                    try:
                        _session_proc.terminate()
                    except Exception:
                        pass
                _session_proc = None
                print("[ControlServer] Session stopped.")

            # Clean up outline temp file
            if _outline_temp:
                try:
                    os.unlink(_outline_temp.name)
                except Exception:
                    pass
                _outline_temp = None

        if respond:
            self._json(200, {"ok": True})

    # ── /api/status ────────────────────────────────────────────────────────────
    def _serve_status(self):
        with _session_lock:
            running = _session_proc is not None and _session_proc.poll() is None
        self._json(200, {"running": running})

    # ── /api/heartbeat ─────────────────────────────────────────────────────────
    def _handle_heartbeat(self):
        global _last_heartbeat
        _last_heartbeat = time.monotonic()
        self._json(200, {"ok": True})

    # ── /api/goodbye ───────────────────────────────────────────────────────────
    def _handle_goodbye(self):
        """Explicit goodbye from the browser (pagehide beacon on tab close).

        pagehide also fires on reload and navigation, so don't shut down
        immediately: wait a grace period and cancel if a heartbeat arrives —
        a reloaded page (or a second open tab) reconnects within seconds.
        """
        goodbye_time = time.monotonic()
        self._json(200, {"ok": True})
        print(f"[ControlServer] Goodbye received — shutting down in "
              f"{_GOODBYE_GRACE}s unless the panel reconnects.")

        def _pending_shutdown():
            time.sleep(_GOODBYE_GRACE)
            if _last_heartbeat > goodbye_time:
                print("[ControlServer] Panel reconnected — shutdown canceled.")
                return
            print("[ControlServer] Browser closed. Shutting down.")
            if _http_server:
                _http_server.shutdown()

        threading.Thread(target=_pending_shutdown, daemon=True).start()

    def _proxy_latest(self):
        """Proxy /api/latest to the caption server port."""
        import urllib.request
        import urllib.error

        caption_port = 8080  # default; could be stored from last start payload
        try:
            with urllib.request.urlopen(
                f"http://localhost:{caption_port}/api/latest", timeout=1
            ) as r:
                data = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except Exception:
            self._json(503, {"lines": [], "updated": 0})

    # ── JSON helper ────────────────────────────────────────────────────────────
    def _json(self, status: int, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    # ── Routing ────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/" or path == "/index.html":
                self._serve_ui()
            elif path == "/api/devices":
                self._serve_devices()
            elif path == "/api/tunnels":
                self._serve_tunnels()
            elif path == "/api/status":
                self._serve_status()
            elif path == "/api/latest":
                self._proxy_latest()
            elif path == "/api/heartbeat":
                self._handle_heartbeat()
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass

    # ── /api/shutdown ─────────────────────────────────────────────────────────
    def _handle_shutdown(self):
        """Force an immediate teardown: stop the session, then stop the server.

        NOT DEAD CODE, despite having no caller in control.html — the panel's
        "Stop & Close Server" button was removed once closing the tab was shown
        to tear down just as cleanly. This is the manual override:

            curl -X POST http://<host>:9090/api/shutdown

        Reach for it when the normal paths can't work:

        * A stale tab is pinning a session open. /api/goodbye is deliberately
          cancelable — any tab still heartbeating within _GOODBYE_GRACE calls
          the shutdown off. So if the panel is open on an unattended machine
          (screen locked, operator gone), you cannot stop it by opening the
          panel elsewhere and closing it again: the original tab keeps voting
          to stay alive. This endpoint does not consult the heartbeat.
        * You need to stop a session without physical access to the machine.
          Any device on the same network can hit it.

        Prefer closing the panel tab when you can actually reach the browser,
        and Ctrl+C when you can reach the terminal — both run the same cleanup
        with a grace period this skips. `kill`/`pkill` is also safe now (main()
        installs a SIGTERM handler), but needs a shell on the host.

        Unauthenticated and bound to all interfaces, like every other route
        here — anyone on the church network can end a live service with it. See
        the network-exposure item in the audit notes; the fix is to gate the
        whole API, not to single this route out.
        """
        self._handle_stop(respond=False)
        self._json(200, {"ok": True})
        def _do_shutdown():
            time.sleep(0.5)
            print("[ControlServer] Shutdown requested via /api/shutdown.")
            if _http_server:
                _http_server.shutdown()
        threading.Thread(target=_do_shutdown, daemon=True).start()

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/start":
                self._handle_start()
            elif path == "/api/stop":
                self._handle_stop()
            elif path == "/api/shutdown":
                self._handle_shutdown()
            elif path == "/api/goodbye":
                self._handle_goodbye()
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass

    def log_message(self, fmt, *args):
        # Suppress noisy GET /api/latest and /api/heartbeat polling logs
        msg = str(args[0] if args else "")
        if "/api/latest" not in msg and "/api/heartbeat" not in msg:
            super().log_message(fmt, *args)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Volunteer control panel server")
    parser.add_argument("--port", type=int, default=9090,
                        help="Port for the control panel (default: 9090)")
    args = parser.parse_args()

    global _http_server, _outline_temp
    # ThreadingHTTPServer: requests are handled concurrently, so a slow
    # /api/latest proxy or a blocking /api/stop can't queue heartbeats behind
    # it and trip the liveness watchdog mid-session.
    _http_server = http.server.ThreadingHTTPServer(("", args.port), ControlHandler)
    server = _http_server

    # Route both signals through the finally block below. Two separate problems:
    #
    #   SIGTERM has no handler by default, so its default action kills this
    #   process outright without unwinding — the session and its cloudflared
    #   tunnel survive as orphans, still registered against the shared named
    #   tunnel and breaking routing for other devices. That makes `kill` and
    #   `pkill -f control_server.py` actively worse than doing nothing.
    #
    #   SIGINT is ignored rather than fatal: the launcher backgrounds this
    #   process (`… &`), which sets the process tree's SIGINT disposition to
    #   SIG_IGN, and CPython preserves an inherited SIG_IGN instead of
    #   installing its own handler. Ctrl+C in that terminal would do nothing.
    #
    # Installing an explicit handler for both fixes each case. Done before
    # serve_forever so no signal can arrive with a session up but no handler
    # yet. Raising rather than calling server.shutdown() is deliberate —
    # shutdown() blocks until serve_forever returns, which would deadlock when
    # called from the main thread.
    def _graceful_shutdown(signum, frame):
        _ignore_further_signals()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    # Heartbeat watcher — backstop that shuts down if the browser disappears
    # without a goodbye (Chrome force-quit, power loss, tab discarded).
    global _last_heartbeat
    _last_heartbeat = time.monotonic()  # grace period from startup

    def _heartbeat_watcher(srv):
        # Give the browser 20s to open before we start checking
        time.sleep(20)
        while True:
            time.sleep(3)
            if time.monotonic() - _last_heartbeat > _HEARTBEAT_TIMEOUT:
                print("[ControlServer] Heartbeat timeout — browser gone without goodbye. Shutting down.")
                srv.shutdown()  # unblocks serve_forever(); finally block cleans up
                break

    threading.Thread(target=_heartbeat_watcher, args=(server,), daemon=True).start()

    print(f"""
╔══════════════════════════════════════════════╗
║   RC Church · Live Translation               ║
║   Volunteer control panel                    ║
╚══════════════════════════════════════════════╝

  Open this in a browser:
  → http://localhost:{args.port}

  Press Ctrl+C to quit.
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Reached by every shutdown path — Ctrl+C, SIGTERM, the /api/goodbye
        # beacon the panel sends on tab close, /api/shutdown, and the heartbeat
        # watchdog. Tab close is the normal way operators end a session, so this
        # has to clean up as thoroughly as an explicit stop would.
        #
        # Teardown waits up to 5s for the session to exit, and a signal arriving
        # in that window (the launcher's cleanup, an impatient second Ctrl+C)
        # would raise here and abort it partway. _graceful_shutdown already
        # muted signals, but the goodbye/shutdown/watchdog paths reach the
        # finally without passing through it.
        _ignore_further_signals()
        print("\n[ControlServer] Shutting down…")
        with _session_lock:
            if _session_proc and _session_proc.poll() is None:
                try:
                    _session_proc.send_signal(signal.SIGINT)
                    # 5s: main.py allows itself up to 3s to terminate the
                    # cloudflared tunnel, so a shorter wait here would fall
                    # through to terminate() while that teardown is still
                    # running.
                    _session_proc.wait(timeout=5)
                except Exception:
                    try:
                        _session_proc.terminate()
                    except Exception:
                        pass

            # Outlines are written with delete=False, so nothing reclaims them
            # if the process exits without going through /api/stop. /tmp is
            # shared across macOS accounts — don't leave sermon text there.
            if _outline_temp:
                try:
                    os.unlink(_outline_temp.name)
                except Exception:
                    pass
                _outline_temp = None
        server.server_close()


if __name__ == "__main__":
    main()
