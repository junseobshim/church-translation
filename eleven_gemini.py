import json
import os
import re
import sys
import base64
import queue
import time
import threading
import argparse
import subprocess
import http.server
from urllib.parse import urlparse

# Suppress noisy thread exception tracebacks on Ctrl+C.
threading.excepthook = lambda args: None

import sounddevice as sd
from dotenv import load_dotenv
from websockets import ConnectionClosed, ConnectionClosedOK
from websockets.sync.client import connect

ELEVENLABS_WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"

SAMPLE_RATE = 16000
CHUNK_FRAMES = 1600  # 100ms at 16kHz

SYSTEM_PROMPT = (
    "You are a live translation assistant for a Korean church sermon. "
    "You receive a rolling context window of recent phrases; prior translations are provided as context. "
    "Translate ONLY the latest phrase from Korean to English. "
    "Drop Korean hesitation fillers (아, 어). "
    "Preferred terms: 여러분 → everyone; 정목사 → Pastor Chung. "
    "If input contains both English and Korean, keep the English as-is and translate the Korean portions into English, "
    "even if the Korean repeats or paraphrases the English — always include both. "
    "If input is entirely in English, output it unchanged. "
    "Prefix output with the ISO 639-1 language code in brackets, e.g. [en]. "
    "Output ONLY the translation — no commentary or notes. "
    "Phrases may arrive as incomplete clauses. Translate only the words present — "
    "never infer or complete missing verbs or conclusions. "
    "If the fragment is too incomplete or garbled, output exactly: [SKIP] "
    "Short fragments that lack a verb or predicate and cannot stand alone as a meaningful sentence "
    "should be [SKIP]ped — they will be prepended to the next phrase automatically. "
    "When quoting or referencing Bible passages, use the New Korean Revised Version (개역개정) "
    "for Korean and the English Standard Version (ESV) for English."
)

# ── Web State ─────────────────────────────────────────────────────────────────

_web_state = {"lines": [], "updated": 0}
_web_lock = threading.Lock()


def _update_web_state(kind: str, lang: str, text: str):
    """kind='transcription' or 'translation', lang='en'/'ko'/etc."""
    with _web_lock:
        _web_state["lines"].append({"kind": kind, "lang": lang, "text": text})
        _web_state["updated"] = time.time()


def _get_web_state_json() -> bytes:
    with _web_lock:
        return json.dumps(_web_state).encode()


# ── HTML Template ─────────────────────────────────────────────────────────────

CAPTION_HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body {
    width: 100%; height: 100%;
    background: transparent;
    overflow: hidden;
  }
  #container {
    width: 100%; height: 100%;
    overflow-y: auto;
    scroll-behavior: smooth;
    scrollbar-width: none;
    -ms-overflow-style: none;
  }
  #container::-webkit-scrollbar { display: none; }
  .line-item {
    animation: fadeIn 0.25s ease-out;
  }
  .span-item {
    /* no animation — instant append for paragraph mode */
  }
  @keyframes fadeIn {
    from { opacity: 0; }
    to   { opacity: 1; }
  }
</style>
</head><body>
<div id="container">
  <div id="lines"></div>
</div>
<script>
(function() {
  const params = new URLSearchParams(window.location.search);

  // Content filtering
  const mode    = params.get('mode')    || 'transcription';
  const lang    = params.get('lang')    || 'en';
  const display = params.get('display') || 'line';

  // Typography
  const fontSize   = params.get('fontSize')   || '48';
  const fontFamily = params.get('fontFamily') || 'system-ui, sans-serif';
  const googleFont = params.get('googleFont');
  const fontWeight = params.get('fontWeight') || 'normal';
  const color      = params.get('color')      || 'white';
  const lineSpacing = params.get('lineSpacing') || '1.4';
  const textAlign  = params.get('textAlign')  || 'left';
  const textShadow = params.get('textShadow') || 'none';

  // Layout
  const bgColor  = params.get('bgColor') || 'transparent';
  const padding  = params.get('padding') || '20';
  const maxLines = Math.min(
    params.get('maxLines') ? parseInt(params.get('maxLines')) : 0,
    200
  );

  // Load Google Font if specified
  if (googleFont) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://fonts.googleapis.com/css2?family='
              + encodeURIComponent(googleFont) + '&display=swap';
    document.head.appendChild(link);
  }

  const container = document.getElementById('container');
  const linesDiv  = document.getElementById('lines');

  // Apply styles
  document.body.style.background = bgColor;
  container.style.padding = padding + 'px';

  const resolvedFamily = googleFont
    ? '"' + googleFont.replace(/\+/g, ' ') + '", ' + fontFamily
    : fontFamily;
  linesDiv.style.cssText = [
    'font-size:'    + fontSize + 'px',
    'font-family:'  + resolvedFamily,
    'font-weight:'  + fontWeight,
    'color:'        + color,
    'line-height:'  + lineSpacing,
    'text-align:'   + textAlign,
    'text-shadow:'  + textShadow,
  ].join(';');

  let lastCount = 0;
  let lastUpdated = 0;
  const DOM_CAP = 200;

  async function poll() {
    try {
      const resp = await fetch('/api/latest');
      const data = await resp.json();
      if (data.updated === lastUpdated) return;
      lastUpdated = data.updated;

      // Filter and append only new lines
      const allLines = data.lines;
      const newLines = allLines.slice(lastCount);
      lastCount = allLines.length;

      for (const line of newLines) {
        if (line.kind !== mode) continue;
        if (line.lang !== lang) continue;

        if (display === 'paragraph') {
          const span = document.createElement('span');
          span.className = 'span-item';
          span.textContent = line.text + ' ';
          linesDiv.appendChild(span);
        } else {
          const div = document.createElement('div');
          div.className = 'line-item';
          div.textContent = line.text;
          linesDiv.appendChild(div);
        }
      }

      // Trim DOM
      const selector = display === 'paragraph' ? '.span-item' : '.line-item';
      const items = linesDiv.querySelectorAll(selector);
      const limit = maxLines > 0 ? maxLines : DOM_CAP;
      const toRemove = items.length - limit;
      for (let i = 0; i < toRemove; i++) {
        items[i].remove();
      }

      container.scrollTop = container.scrollHeight;
    } catch (e) {}
  }

  setInterval(poll, 150);
})();
</script>
</body></html>
"""

# ── HTTP Server ───────────────────────────────────────────────────────────────


class _CaptionHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/latest":
                data = _get_web_state_json()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path == "/":
                html = CAPTION_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(html)
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass

    def log_message(self, format, *args):
        pass


def start_caption_server(port: int):
    server = http.server.HTTPServer(("", port), _CaptionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── Cloudflare Tunnel ─────────────────────────────────────────────────────────


def start_cloudflare_tunnel(tunnel_name: str, port: int):
    """Launch cloudflared as a subprocess for a named tunnel."""
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "run", "--url", f"http://localhost:{port}", tunnel_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


# ── Audio Device Selection ────────────────────────────────────────────────────


def select_audio_device():
    devices = sd.query_devices()
    input_devices = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            input_devices.append((i, dev))

    if not input_devices:
        sys.exit("Error: No audio input devices found")

    print("Available audio input devices:")
    print("-" * 60)
    for idx, dev in input_devices:
        sr = dev["default_samplerate"]
        ch = dev["max_input_channels"]
        print(f"  [{idx}]  {dev['name']}  ({ch}ch, {sr:.0f}Hz)")
    print()

    while True:
        try:
            choice = input("Enter device index to use: ").strip()
            idx = int(choice)
            dev = sd.query_devices(idx)
            if dev["max_input_channels"] > 0:
                return idx, dev["name"]
            print("  That device has no input channels. Try again.")
        except (ValueError, sd.PortAudioError):
            print("  Invalid device index. Try again.")


# ── Web Push Helper ───────────────────────────────────────────────────────────


def _push_to_web(kind: str, text: str):
    """Parse [lang] prefix from text and push to web state."""
    m = re.match(r"\[([a-z]{2})\]\s*", text)
    if m:
        lang = m.group(1)
        raw_text = text[m.end():]
    else:
        lang = "en"
        raw_text = text
    if raw_text.strip():
        _update_web_state(kind, lang, raw_text.strip())


# ── ElevenLabs Audio Streaming ────────────────────────────────────────────────


def stream_audio(device_index: int, ws, stop_event: threading.Event) -> None:
    audio_queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            print(f"  [Audio] {status}", file=sys.stderr)
        audio_queue.put(bytes(indata))

    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=CHUNK_FRAMES,
        device=device_index,
        dtype="int16",
        channels=1,
        callback=callback,
    )

    try:
        with stream:
            while not stop_event.is_set():
                try:
                    chunk = audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                ws.send(json.dumps({
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(chunk).decode("ascii"),
                    "sample_rate": SAMPLE_RATE,
                    "commit": False,
                }))
    except Exception as e:
        print(f"[Audio thread error] {e}", file=sys.stderr)

    # Signal end-of-stream
    try:
        ws.send(json.dumps({"message_type": "commit"}))
    except Exception:
        pass


# ── Main Session ──────────────────────────────────────────────────────────────


def run_session(elevenlabs_key: str, device_index: int) -> None:
    url = (
        f"{ELEVENLABS_WS_URL}"
        f"?model_id=scribe_v2_realtime"
        f"&language_code=kor"
        f"&commit_strategy=vad"
    )
    headers = {"xi-api-key": elevenlabs_key}

    print("Connecting to ElevenLabs Scribe v2...")
    with connect(url, additional_headers=headers) as ws:
        # Wait for session_started
        init_msg = json.loads(ws.recv())
        if init_msg.get("message_type") == "session_started":
            print(f"Session started (ID: {init_msg.get('session_id', 'unknown')})")
        else:
            print(f"Unexpected init message: {init_msg}")

        # Start streaming audio in the background
        stop_event = threading.Event()
        audio_thread = threading.Thread(
            target=stream_audio,
            args=(device_index, ws, stop_event),
            daemon=True,
        )
        audio_thread.start()

        print("Listening. Speak into your microphone. Press Ctrl+C to stop.")

        try:
            while True:
                message = ws.recv()
                data = json.loads(message)
                msg_type = data.get("message_type")

                if msg_type == "committed_transcript":
                    text = data.get("text", "")
                    if not text.strip():
                        continue
                    print(f"[Transcription] {text}")
                    _push_to_web("transcription", f"[ko] {text}")

                elif msg_type == "partial_transcript":
                    # Interim results — don't push to web/translation
                    pass

                elif msg_type == "error":
                    print(f"ElevenLabs error: {data.get('error', data)}")
                    break

        except ConnectionClosedOK:
            print("Connection closed normally.")
        except ConnectionClosed as e:
            print(f"Connection closed: code={e.code} reason={e.reason}")
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            stop_event.set()
            audio_thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(
        description="ElevenLabs Scribe v2 real-time Korean transcription"
    )
    parser.add_argument("--device", type=int, default=None,
                        help="Audio input device index (skip interactive selection)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Web caption server port (default: 8080, 0 to disable)")
    parser.add_argument("--tunnel", type=str, default=None,
                        help="Cloudflare tunnel name (e.g. church-live)")
    args = parser.parse_args()

    load_dotenv(override=True)
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY")
    if elevenlabs_key is None:
        raise RuntimeError("Missing ELEVENLABS_API_KEY. Set it in .env or environment.")

    if args.device is not None:
        device_index = args.device
        dev = sd.query_devices(device_index)
        print(f"Using device [{device_index}]: {dev['name']}")
    else:
        device_index, device_name = select_audio_device()
        print(f"Using device [{device_index}]: {device_name}")

    # Start web caption server
    if args.port > 0:
        start_caption_server(args.port)
        print(f"Web captions: http://localhost:{args.port}")

    # Start Cloudflare tunnel
    tunnel_proc = None
    if args.tunnel:
        tunnel_proc = start_cloudflare_tunnel(args.tunnel, args.port)
        print(f"Cloudflare tunnel '{args.tunnel}' started")

    try:
        run_session(elevenlabs_key, device_index)
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()


if __name__ == "__main__":
    main()
