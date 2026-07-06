# Audit findings — future work

Findings from the July 2026 code audit (done alongside the cloudflared
tunnel-leak fix). Ranked by importance. Line numbers are as of that audit and
will drift.

The two caption-viewer scrolling issues (long-run jitter, paragraph
re-wrapping) are **not** in this file — they live in
`caption-viewer-scrolling-issues.md` at the repo root, to be tackled
immediately.

---

## 1. `launcher.sh`: relaunching while a session is live kills the live session

**What happens:** if the launcher runs while the control server is already up
(port 9090 busy), it takes the "already running" branch, opens Chrome, then
falls off the end of the script — `SERVER_PID` is empty so the `wait` is
skipped. Exiting fires the `trap cleanup EXIT`, which `kill -9`s whatever holds
ports 9090 and 8080 and pkills the tunnel — i.e. it destroys the *live*
session it just attached to, and leaves Chrome showing a dead panel.

The "already running" branch therefore defeats its own purpose. This can
realistically happen when the launcher process died (force-quit, logout) but
the servers survived, and the volunteer relaunches the Dock app to reattach.

**Fix sketch:** only clean up what this instance started. Set a flag when this
launcher instance starts the server and make `cleanup()` a no-op otherwise:

```bash
STARTED_BY_ME=0
# in the else-branch that starts control_server.py:
STARTED_BY_ME=1

cleanup() {
    [ "$STARTED_BY_ME" = "1" ] || return 0
    ...existing kills...
}
```

(Alternative: `trap - EXIT TERM INT` inside the already-running branch, then
either exit or `wait` on the existing server some other way.)

## 2. Control server is exposed to the whole LAN

`control_server.py` binds `HTTPServer(("", 9090), …)` — all interfaces. Anyone
on the church Wi-Fi can hit `/api/start`, `/api/stop`, `/api/shutdown` and
start/stop/kill the translation session. The panel is only ever used from
Chrome on the same Mac (the heartbeat design assumes this), so bind to
`127.0.0.1` instead:

```python
_http_server = http.server.HTTPServer(("127.0.0.1", args.port), ControlHandler)
```

Command construction itself is injection-safe (list-form `Popen`, argparse
validation of source/target/port/device), so this is about denial-of-service /
mischief, not code execution.

**Note:** the *caption* server on 8080 may need to stay on all interfaces if
ProPresenter (or any TV/other machine on the LAN) fetches captions directly
from this Mac by IP. Verify before changing that one; cloudflared itself only
needs localhost.

## 3. Caption viewer, multi-lang mode: timer-path flush never scrolls

In `CAPTION_HTML` (main.py), `flushGroup()` has two callers:

- all-expected-langs-arrived — called inside `poll()`, where `appended` is set
  and `scrollToBottom()` runs. Fine.
- the 6-second `GROUP_WINDOW_MS` deadline — called from a `setTimeout`. Content
  is appended to the DOM but **no scroll is triggered**, so a pinned viewer
  doesn't see the flushed lines until the *next* phrase arrives and re-pins.

**Fix sketch:** at the end of `flushGroup()`, `if (pinnedToBottom)
scrollToBottom();`.

Related cosmetic nit: the empty `.multi-line-block` wrapper is appended to the
DOM at group *creation*, so its `margin-bottom: 0.45em` renders as a small
blank gap for up to 6 s before the group flushes. Appending the wrapper at
flush time instead would fix it (group order can still be preserved by
tracking creation order).

## 4. `control_server._handle_stop`: no escalation past SIGTERM, no reap

After `send_signal(SIGINT)` + `wait(timeout=5)` fails, it calls `terminate()`
and immediately sets `_session_proc = None` without waiting:

- If main.py is truly wedged (stuck in uninterruptible C code — PortAudio,
  etc.), SIGTERM may also not kill it and there is no `kill()` (SIGKILL)
  escalation.
- `/api/status` reports "not running" while the old process may still hold
  port 8080 for a few seconds — a rapid stop→start can fail to bind 8080.
- The child isn't reaped promptly (transient zombie until the interpreter gets
  around to it).

**Fix sketch:**

```python
_session_proc.terminate()
try:
    _session_proc.wait(timeout=3)
except subprocess.TimeoutExpired:
    _session_proc.kill()
    _session_proc.wait(timeout=2)
```

Same pattern applies to the shutdown path in `control_server.main()`'s
`finally`.

## 5. `threading.excepthook = lambda args: None` swallows all thread errors

`main.py:18` silences *every* uncaught exception in *every* thread, forever —
not just the Ctrl+C noise it was added for. A crashed caption-server thread or
keepalive thread dies invisibly with no log line.

**Fix sketch:** filter instead of blanket-silence:

```python
_default_thread_hook = threading.excepthook
def _quiet_interrupts(args):
    if not issubclass(args.exc_type, KeyboardInterrupt):
        _default_thread_hook(args)
threading.excepthook = _quiet_interrupts
```

## 6. Soniox token lists grow without bound

`transcribe_soniox.py` — `final_tokens` / `final_translation_tokens` accumulate
every token for the whole session; only the new slice
(`final_tokens[prev_final_count:]`) is ever read. A multi-hour service holds
tens of thousands of dicts for no benefit. Memory-only, low priority.

**Fix sketch:** periodically drop consumed prefixes and rebase the counters
(e.g. once `prev_final_count` exceeds a few thousand, `del
final_tokens[:prev_final_count]` and reset both counters; same for the
translation list — mind that the reconnect logic relies only on counts, which
rebasing preserves).

## 7. Housekeeping

- **`kill -9` by port in `launcher.sh` cleanup** can kill an unrelated process
  that happens to be squatting 8080/9090 (Docker, another dev server) on a
  volunteer's personal Mac. Filtering the `lsof` output by command name
  (`python3`) before killing would avoid collateral damage.
- **Outline temp files leak** if the control server dies before `/api/stop`
  (written with `delete=False`, unlinked only on stop). Trivial; could clean
  stale `rc_outline_*.txt`-style files at startup if given a recognizable
  prefix.
