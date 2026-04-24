import json
import os
import re
import sys
import queue
import time
import threading
import argparse
import subprocess
import http.server
from typing import Callable, Optional, Union
from urllib.parse import urlparse

import anthropic

# Suppress noisy thread exception tracebacks on Ctrl+C.
threading.excepthook = lambda args: None

import sounddevice as sd
from dotenv import load_dotenv
from websockets import ConnectionClosedOK
from websockets.sync.client import connect


# ── STT constants ─────────────────────────────────────────────────────────────

SONIOX_WEBSOCKET_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
SAMPLE_RATE = 16000
CHUNK_FRAMES = 1600  # 100ms at 16kHz


# ── Claude constants ──────────────────────────────────────────────────────────

DEFAULT_MODEL = "claude-sonnet-4-6"
CACHE_MIN_TOKENS = 1024
KEEPALIVE_IDLE_SECONDS = 270  # 4m30s; stay under the 5-minute ephemeral TTL
KEEPALIVE_POLL_SECONDS = 10


# ── Shared language constants ─────────────────────────────────────────────────

LANG_NAMES = {"ko": "Korean", "en": "English", "es": "Spanish"}

# Languages Soniox should hint for each --source. Strict hints.
SOURCE_LANGS = {
    "ko":    ["ko", "en"],
    "en":    ["en"],
    "es":    ["es", "en"],
    "multi": ["ko", "en", "es"],
}

# Fallback tag when render_tokens emits text with no [xx] language prefix
# (edge case: language_identification is enabled, so this rarely fires).
PRIMARY_SRC = {"ko": "ko", "en": "en", "es": "es", "multi": "en"}

# Matches [xx] tags render_tokens emits; stripped before Claude sees the text
# so embedded tags can't masquerade as the desired output prefix.
_LANG_TAG_RE = re.compile(r"\[[a-z]{2}\]\s*")


# ── Prompt pieces (Claude zone) ───────────────────────────────────────────────

FILLER_CLAUSE_BY_LANG = {
    "ko": "Korean hesitation fillers (아, 어)",
    "en": "English hesitation fillers (uh, um, like, you know, so, I mean)",
    "es": "Spanish hesitation fillers (eh, este, pues, o sea, bueno)",
}

BIBLE_BY_TARGET = {
    "en": "English Standard Version (ESV)",
    "ko": "New Korean Revised Version (개역개정)",
    "es": "Reina-Valera 1960 (RVR1960)",
}

REGISTER_BY_TARGET = {
    "ko": " Use natural, formal polite speech (합쇼체/해요체) as is standard for sermon translation.",
    "en": "",
    "es": "",
}

# Proper-noun / address preferences, keyed by (source, target).
# Religious nouns (하나님, Dios, etc.) live in the Soniox terms list; Claude
# translates them naturally without hints. This table is for proper nouns and
# address-form overrides specific to a direction pair.
TERM_PREFS_BY_PAIR = {
    ("ko", "en"):    "여러분 → everyone; 정목사 → Pastor Chung.",
    ("ko", "es"):    "여러분 → todos; 정목사 → Pastor Chung.",
    ("en", "ko"):    "",
    ("en", "es"):    "",
    ("es", "en"):    "",
    ("es", "ko"):    "",
    # multi → any: use ko-specific prefs since 정목사 only appears in Korean speech.
    ("multi", "en"): "여러분 → everyone; 정목사 → Pastor Chung.",
    ("multi", "es"): "여러분 → todos; 정목사 → Pastor Chung.",
    ("multi", "ko"): "",
}

SOURCE_COMPOSITION = {
    "ko":    "Korean (with occasional English)",
    "en":    "English",
    "es":    "Spanish (with occasional English)",
    "multi": "mixed Korean, English, and Spanish",
}

OUTLINE_WRAPPER = (
    "\n\n--- SERMON OUTLINE (CONTEXT ONLY) ---\n"
    "The following outline is provided for logical flow and topical context only. "
    "Do NOT use it to infer, complete, or reshape what the speaker actually says. "
    "If the spoken phrase contradicts, diverges from, or rhetorically opposes the "
    "outline, translate what is said literally. The outline is background knowledge, "
    "not a script.\n"
    "--- OUTLINE BEGINS ---\n"
    "{outline}\n"
    "--- OUTLINE ENDS ---"
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def load_outline(path: str) -> str:
    """Read a UTF-8 sermon outline file. Fail loudly on any issue."""
    if not os.path.exists(path):
        raise RuntimeError(f"Outline file not found: {path}")
    if os.path.isdir(path):
        raise RuntimeError(f"Outline path is a directory, expected a file: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError as e:
        raise RuntimeError(f"Outline file is not valid UTF-8: {path} ({e})")
    except OSError as e:
        raise RuntimeError(f"Could not read outline file: {path} ({e})")
    if not text.strip():
        raise RuntimeError(f"Outline file is empty: {path}")
    return text.strip()


def build_system_blocks(base_prompt: str, outline: Optional[str],
                        cache: bool) -> Union[str, list[dict]]:
    """Assemble the system parameter for Anthropic.

    No outline  -> plain string (preserves existing API shape).
    With outline -> single TextBlockParam with optional cache_control.
    """
    if outline is None:
        return base_prompt
    combined = base_prompt + OUTLINE_WRAPPER.format(outline=outline)
    block: dict = {"type": "text", "text": combined}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def build_prompt(source: str, target: str) -> str:
    """Assemble the live-translation system prompt for a (source, target) pair.

    Composed from piece dicts above — no per-combo hardcoded strings.
    """
    langs_present = SOURCE_LANGS[source]
    fillers = " and ".join(FILLER_CLAUSE_BY_LANG[l] for l in langs_present)
    tname = LANG_NAMES[target]
    same_lang_clause = (
        f"For segments already in {tname}, keep them unchanged. "
        f"Translate segments in other languages into {tname}, even if they repeat "
        "or paraphrase already-translated content — always include both."
    )
    prefs = TERM_PREFS_BY_PAIR[(source, target)]
    prefs_clause = f"Preferred terms: {prefs} " if prefs else ""
    return (
        f"You are a live translation assistant for a {SOURCE_COMPOSITION[source]} church sermon. "
        "You receive a rolling context window of recent phrases; prior translations are provided as context. "
        f"Translate ONLY the latest phrase into {tname}. "
        f"Drop hesitation fillers like {fillers}. "
        f"{same_lang_clause} "
        f"{prefs_clause}"
        "Output ONLY the translation — no commentary, notes, or language code prefix. "
        "Phrases may arrive as incomplete clauses. Translate only the words present — "
        "never infer or complete missing verbs or conclusions. "
        "If the fragment is too incomplete or garbled, output exactly: [SKIP] "
        "Short fragments that lack a verb or predicate and cannot stand alone as a meaningful sentence "
        "should be [SKIP]ped — they will be prepended to the next phrase automatically. "
        f"When quoting or referencing Bible passages, use the {BIBLE_BY_TARGET[target]} for {tname}."
        f"{REGISTER_BY_TARGET[target]}"
    )


# ── Web State ─────────────────────────────────────────────────────────────────

_web_state = {"lines": [], "updated": 0}
_web_lock = threading.Lock()
_default_target_lang = "en"  # set in main() from the first --target; injected into HTML


def _update_web_state(kind: str, lang: str, text: str):
    """kind='transcription' or 'translation', lang='en'/'ko'/'es'/…"""
    with _web_lock:
        _web_state["lines"].append({"kind": kind, "lang": lang, "text": text})
        _web_state["updated"] = time.time()


def _get_web_state_json() -> bytes:
    with _web_lock:
        return json.dumps(_web_state).encode()


def _push_to_web(kind: str, text: str, fallback_lang: str = "en"):
    """Parse [lang] prefix from text and push to web state."""
    m = re.match(r"\[([a-z]{2})\]\s*", text)
    if m:
        lang = m.group(1)
        raw_text = text[m.end():]
    else:
        lang = fallback_lang
        raw_text = text
    if raw_text.strip():
        _update_web_state(kind, lang, raw_text.strip())


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

  // Content filtering.
  // Default target lang is injected server-side from the first --target.
  // Default mode is transcription.
  // For transcription mode, missing `lang` means "no filter — show all langs".
  // For translation mode, missing `lang` falls back to the default target.
  const DEFAULT_TARGET_LANG = "__DEFAULT_TARGET_LANG__";
  const mode = params.get('mode') || 'transcription';
  const explicitLang = params.get('lang');
  const lang = explicitLang || (mode === 'translation' ? DEFAULT_TARGET_LANG : null);
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
  const isPublic = location.hostname === 'live.rctranslation.org';
  const bgColor  = params.get('bgColor') || (isPublic ? '#000' : 'transparent');
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

  const FAST_MS = 150;
  const MAX_MS  = 1000;
  const GROWTH  = 1.5;
  let pollDelay = FAST_MS;

  const statusEl = document.createElement('div');
  statusEl.textContent = 'Waiting for transcription…';
  statusEl.style.cssText = 'position:fixed;bottom:16px;right:20px;font-size:14px;opacity:0;transition:opacity 0.4s;pointer-events:none;color:#999;';
  if (isPublic) document.body.appendChild(statusEl);
  let failCount = 0;
  const FAIL_THRESHOLD = 3;

  async function poll() {
    try {
      const resp = await fetch('/api/latest');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      pollDelay = FAST_MS;
      failCount = 0;
      statusEl.style.opacity = '0';
      if (data.updated === lastUpdated) return;
      lastUpdated = data.updated;

      // Filter and append only new lines
      const allLines = data.lines;
      const newLines = allLines.slice(lastCount);
      lastCount = allLines.length;

      for (const line of newLines) {
        if (line.kind !== mode) continue;
        if (lang !== null && line.lang !== lang) continue;

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
    } catch (e) {
      pollDelay = Math.min(pollDelay * GROWTH, MAX_MS);
      failCount++;
      if (isPublic && failCount >= FAIL_THRESHOLD) statusEl.style.opacity = '1';
    } finally {
      setTimeout(poll, pollDelay);
    }
  }

  poll();
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
                safe_lang = _default_target_lang if _default_target_lang in ("ko", "en", "es") else "en"
                html = CAPTION_HTML.replace("__DEFAULT_TARGET_LANG__", safe_lang).encode()
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


# ── Soniox Config ─────────────────────────────────────────────────────────────

TERMS_KO = ["하나님", "예수님", "성령", "아멘", "목사님", "집사님", "장로님", "권사님", "전도사님"]
TERMS_EN = ["God", "Jesus", "Holy Spirit", "amen", "Pastor"]
TERMS_ES = ["Dios", "Jesús", "Cristo", "Espíritu Santo", "amén", "Pastor", "hermano", "hermana", "iglesia"]

SOURCE_TERMS = {
    "ko":    TERMS_KO + TERMS_EN,
    "en":    TERMS_EN,
    "es":    TERMS_ES + TERMS_EN,
    "multi": TERMS_KO + TERMS_EN + TERMS_ES,
}

SOURCE_CONTEXT = {
    "ko":    ("Korean church sermon",
              "Live Korean church sermon with occasional English, with a pastor preaching to the congregation."),
    "en":    ("English church sermon",
              "Live English church sermon with a pastor preaching to the congregation."),
    "es":    ("Spanish church sermon",
              "Live Spanish church sermon with occasional English, with a pastor preaching to the congregation."),
    "multi": ("Multilingual church sermon",
              "Live multilingual church sermon in Korean, English, and Spanish, with a pastor preaching to the congregation."),
}


def build_soniox_config(source: str, api_key: str) -> dict:
    """Build the initial-frame JSON for the Soniox STT websocket.

    `translation.target_language` is fixed at `zh` across all sources — Soniox
    translation tokens are used only as a phrase-boundary gating signal; the
    translated text is discarded. Pivoting to an unused language keeps this
    config source-agnostic.
    """
    topic, text = SOURCE_CONTEXT[source]
    return {
        "api_key": api_key,
        "model": "stt-rt-v4",
        "language_hints": SOURCE_LANGS[source],
        "language_hints_strict": True,
        "enable_language_identification": True,
        "enable_endpoint_detection": True,
        "audio_format": "pcm_s16le",
        "sample_rate": SAMPLE_RATE,
        "num_channels": 1,
        "translation": {
            "type": "one_way",
            "target_language": "zh",
        },
        "context": {
            "general": [
                {"key": "domain", "value": "Religion"},
                {"key": "topic", "value": topic},
            ],
            "text": text,
            "terms": SOURCE_TERMS[source],
        },
    }


# ── Audio ─────────────────────────────────────────────────────────────────────


def select_audio_device():
    """List available input devices and prompt the user to select one."""
    devices = sd.query_devices()
    input_devices = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            input_devices.append((i, dev))

    if not input_devices:
        sys.exit("Error: No audio input devices found")

    print("Available audio input devices:")
    print("─" * 60)
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


def stream_audio(device_index: int, ws, stop_event: threading.Event) -> None:
    """Stream microphone audio to the Soniox websocket."""
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
                ws.send(chunk)
    except Exception:
        pass

    # Empty string signals end-of-audio to the server.
    try:
        ws.send("")
    except Exception:
        pass


# ── Token Rendering ───────────────────────────────────────────────────────────


def render_tokens(final_tokens: list[dict]) -> str:
    """Convert Soniox tokens into a readable transcript, interleaving [xx] tags
    on language changes."""
    text_parts: list[str] = []
    current_speaker: Optional[str] = None
    current_language: Optional[str] = None

    for token in final_tokens:
        text = token["text"]
        if text == "<end>":
            continue
        speaker = token.get("speaker")
        language = token.get("language")
        is_translation = token.get("translation_status") == "translation"

        if language is not None and language != current_language:
            if text_parts and not text_parts[-1].endswith(" "):
                text_parts.append(" ")
            current_language = language
            prefix = "[Translation] " if is_translation else ""
            text_parts.append(f"{prefix}[{current_language}] ")
            text = text.lstrip()

        text_parts.append(text)

    return "".join(text_parts)


# ── Claude Caching ────────────────────────────────────────────────────────────


def count_system_tokens(client: anthropic.Anthropic,
                        system: Union[str, list[dict]],
                        model: str) -> int:
    """Exact token count for the system parameter by differencing against a
    baseline call with no system. Uses count_tokens (free of charge)."""
    dummy_msg = [{"role": "user", "content": "x"}]
    baseline = client.messages.count_tokens(model=model, messages=dummy_msg)
    full = client.messages.count_tokens(model=model, system=system, messages=dummy_msg)
    return full.input_tokens - baseline.input_tokens


def check_cache_eligibility(client: anthropic.Anthropic,
                            base_prompt: str, outline: str, model: str,
                            label: str = "") -> tuple[Union[str, list[dict]], bool]:
    """Return (system_blocks, cache_enabled). Warns and strips cache_control
    if the combined system tokens fall below CACHE_MIN_TOKENS. The optional
    `label` is included in log output to distinguish per-target workers."""
    candidate = build_system_blocks(base_prompt, outline, cache=True)
    tokens = count_system_tokens(client, candidate, model)
    tag = f" [{label}]" if label else ""
    if tokens >= CACHE_MIN_TOKENS:
        print(f"Prompt caching enabled{tag} ({tokens} system tokens).")
        return candidate, True
    print(
        f"Warning: system prompt + outline is {tokens} tokens{tag}, below the "
        f"{CACHE_MIN_TOKENS}-token caching threshold. Running without cache.",
        file=sys.stderr,
    )
    return build_system_blocks(base_prompt, outline, cache=False), False


def warm_cache(client: anthropic.Anthropic,
               system: Union[str, list[dict]], model: str,
               label: str = "") -> None:
    """One blocking call to populate the ephemeral cache. Exits on failure so
    an invalid API key or model name surfaces before the session starts."""
    try:
        response = client.messages.create(
            model=model,
            max_tokens=1,
            system=system,
            messages=[{"role": "user", "content": "ready"}],
        )
    except Exception as e:
        sys.exit(f"Cache warmup failed: {e}")
    written = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    tag = f" [{label}]" if label else ""
    print(f"Cache warmed{tag} ({written} tokens written).")


# ── Translation Worker ────────────────────────────────────────────────────────


class TranslationWorker:
    """Owns one target-language translation stream.

    State per worker: Claude client handle (shared), system prompt (own),
    cache flag (own), rolling context window (own), `[SKIP]` pending-text
    buffer (own), input queue (own), and (if cached) a keepalive thread (own).
    The only external seam is the `on_translation(target, text)` callback
    passed at construction — the callee decides how to surface the output
    (e.g. push to web state). This keeps the class language-neutral and
    trivially movable to a future `translate_claude.py` module.
    """

    def __init__(self, client: anthropic.Anthropic, source: str, target: str,
                 system_blocks: Union[str, list[dict]], cache_enabled: bool,
                 model: str, stop_event: threading.Event,
                 on_translation: Callable[[str, str], None]):
        self.client = client
        self.source = source
        self.target = target
        self.system = system_blocks
        self.cache_enabled = cache_enabled
        self.model = model
        self.stop_event = stop_event
        self.on_translation = on_translation
        self.inbox: queue.Queue[str] = queue.Queue()
        self.context: list[tuple[str, str]] = []   # last 5 (source, translation)
        self.pending_text: str = ""
        self._last_activity = time.monotonic()
        self._activity_lock = threading.Lock()
        self._run_thread: Optional[threading.Thread] = None
        self._keepalive_thread: Optional[threading.Thread] = None

    def warm(self) -> None:
        if self.cache_enabled:
            warm_cache(self.client, self.system, self.model, label=self.target)

    def start(self) -> None:
        self._run_thread = threading.Thread(target=self._run, daemon=True)
        self._run_thread.start()
        if self.cache_enabled:
            self._keepalive_thread = threading.Thread(target=self._keepalive, daemon=True)
            self._keepalive_thread.start()

    def enqueue(self, source_text: str) -> None:
        self.inbox.put(source_text)

    def _mark(self) -> None:
        with self._activity_lock:
            self._last_activity = time.monotonic()

    def _idle_seconds(self) -> float:
        with self._activity_lock:
            return time.monotonic() - self._last_activity

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                src = self.inbox.get(timeout=0.25)
            except queue.Empty:
                continue
            clean_src = _LANG_TAG_RE.sub("", src).strip()
            if not clean_src:
                continue
            combined = (self.pending_text + " " + clean_src).strip() if self.pending_text else clean_src
            try:
                out = self._call(combined)
            except Exception as e:
                print(f"[{self.target} translation error: {e}]", file=sys.stderr)
                self._mark()
                continue
            self._mark()
            if "[SKIP]" in out:
                self.pending_text = combined
                continue
            self.pending_text = ""
            self.context.append((combined, out))
            if len(self.context) > 5:
                self.context.pop(0)
            prefixed = f"[{self.target}] {out}"
            print(f"[Translation:{self.target}] {prefixed}")
            self.on_translation(self.target, prefixed)

    def _call(self, text: str) -> str:
        messages: list[dict] = []
        for s, t in self.context:
            messages.append({"role": "user", "content": s})
            messages.append({"role": "assistant", "content": t})
        messages.append({"role": "user", "content": text})
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=self.system,
            messages=messages,
        )
        u = resp.usage
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        print(f"[usage {self.target}: in={u.input_tokens} cache_read={cr} "
              f"cache_write={cw} out={u.output_tokens}]", file=sys.stderr)
        return resp.content[0].text.strip()

    def _keepalive(self) -> None:
        while not self.stop_event.wait(KEEPALIVE_POLL_SECONDS):
            try:
                if self._idle_seconds() < KEEPALIVE_IDLE_SECONDS:
                    continue
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=1,
                    system=self.system,
                    messages=[{"role": "user", "content": "ready"}],
                )
                self._mark()
                u = response.usage
                cr = getattr(u, "cache_read_input_tokens", 0) or 0
                cw = getattr(u, "cache_creation_input_tokens", 0) or 0
                print(f"[keepalive {self.target}: cache_read={cr} cache_write={cw}]",
                      file=sys.stderr)
            except Exception as e:
                print(f"[keepalive {self.target} error: {e}]", file=sys.stderr)


# ── Orchestration ─────────────────────────────────────────────────────────────


def _build_workers(client: anthropic.Anthropic, source: str, targets: list[str],
                   outline: Optional[str], stop_event: threading.Event
                   ) -> list[TranslationWorker]:
    """Construct one TranslationWorker per target. Per-worker cache eligibility
    is decided independently (each has its own system prompt)."""
    workers: list[TranslationWorker] = []
    for t in targets:
        prompt = build_prompt(source, t)
        if outline is None:
            system: Union[str, list[dict]] = prompt
            cache_enabled = False
        else:
            system, cache_enabled = check_cache_eligibility(
                client, prompt, outline, DEFAULT_MODEL, label=t
            )
        w = TranslationWorker(
            client=client,
            source=source,
            target=t,
            system_blocks=system,
            cache_enabled=cache_enabled,
            model=DEFAULT_MODEL,
            stop_event=stop_event,
            on_translation=lambda tgt, txt: _push_to_web(
                "translation", txt, fallback_lang=tgt
            ),
        )
        workers.append(w)
    return workers


def run_session(api_key: str, device_index: int, anthropic_api_key: str,
                source: str, targets: list[str],
                outline: Optional[str] = None) -> None:
    config = build_soniox_config(source, api_key)
    client = anthropic.Anthropic(api_key=anthropic_api_key)
    stop_event = threading.Event()

    workers = _build_workers(client, source, targets, outline, stop_event)

    # Warm each cached worker's ephemeral cache before opening the mic.
    for w in workers:
        w.warm()
    for w in workers:
        w.start()

    transcription_fallback = PRIMARY_SRC[source]

    print("Connecting to Soniox...")
    with connect(SONIOX_WEBSOCKET_URL) as ws:
        ws.send(json.dumps(config))

        audio_thread = threading.Thread(
            target=stream_audio,
            args=(device_index, ws, stop_event),
            daemon=True,
        )
        audio_thread.start()

        print("Session started. Speak into your microphone. Press Ctrl+C to stop.")

        final_tokens: list[dict] = []
        final_translation_tokens: list[dict] = []
        prev_final_count = 0
        prev_translation_count = 0

        try:
            while True:
                message = ws.recv()
                res = json.loads(message)

                if res.get("error_code") is not None:
                    print(f"Error: {res['error_code']} - {res['error_message']}")
                    break

                for token in res.get("tokens", []):
                    if token.get("text"):
                        # Soniox translation tokens are the phrase-boundary gate.
                        # Their content is discarded (target is zh, a dummy pivot).
                        if token.get("translation_status") == "translation":
                            if token.get("is_final"):
                                final_translation_tokens.append(token)
                            continue
                        if token.get("is_final"):
                            final_tokens.append(token)

                # Flush the buffered transcription when a gating translation
                # token arrives — that marks the end of the latest phrase.
                if len(final_translation_tokens) == prev_translation_count:
                    continue

                new_tokens = final_tokens[prev_final_count:]
                prev_final_count = len(final_tokens)
                prev_translation_count = len(final_translation_tokens)
                text = render_tokens(new_tokens)

                banner = f"[Transcription] {text}"
                print(banner)

                _push_to_web("transcription", text, fallback_lang=transcription_fallback)

                # Fan-out: enqueue the raw source phrase to every target worker.
                # Each worker applies its own [SKIP] logic and rolling context.
                for w in workers:
                    w.enqueue(text)

                if res.get("finished"):
                    print("Session finished.")

        except ConnectionClosedOK:
            pass
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        except Exception as e:
            print(f"Error: {e}")
        finally:
            stop_event.set()
            audio_thread.join(timeout=2)


def _parse_and_validate_targets(source: str, target_arg: Optional[str]) -> list[str]:
    """Resolve --target against --source. Returns the validated target list in
    the order specified by the user (first target becomes the web default)."""
    ALL = {"ko", "en", "es"}
    DEFAULTS = {"ko": "en", "multi": "ko,en,es"}

    if target_arg is None:
        if source not in DEFAULTS:
            sys.exit(f"--target is required for --source {source}")
        target_arg = DEFAULTS[source]

    targets = [t.strip() for t in target_arg.split(",") if t.strip()]

    if source == "multi":
        if set(targets) != ALL or len(targets) != 3:
            sys.exit("--source multi requires --target ko,en,es (all three)")
        return targets

    allowed = ALL - {source}
    if source in targets:
        sys.exit(f"--target cannot include --source ({source})")
    if not targets or not set(targets).issubset(allowed):
        sys.exit(f"--target must be a non-empty subset of {sorted(allowed)}")
    return targets


def main():
    parser = argparse.ArgumentParser(
        description="Soniox real-time sermon translation from microphone"
    )
    parser.add_argument(
        "--source", choices=["ko", "en", "es", "multi"], default="ko",
        help="Source language: ko (Korean + English), en (English only), "
             "es (Spanish + English), multi (Korean + English + Spanish). Default: ko.",
    )
    parser.add_argument(
        "--target", type=str, default=None,
        help="Comma-separated translation targets (e.g. 'en' or 'ko,es'). "
             "Defaults to 'en' when --source ko, and 'ko,en,es' when --source multi. "
             "Required for --source en or --source es.",
    )
    parser.add_argument("--device", type=int, default=None,
                        help="Audio input device index (skip interactive selection)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Web caption server port (default: 8080, 0 to disable)")
    parser.add_argument("--tunnel", type=str, default="church-live",
                        help="Cloudflare tunnel name (default: church-live). "
                             "Use --no-tunnel to skip.")
    parser.add_argument("--no-tunnel", action="store_true",
                        help="Skip starting the Cloudflare tunnel.")
    parser.add_argument("--outline", type=str, default=None,
                        help="Path to a UTF-8 .txt sermon outline for context. "
                             "Enables per-target prompt caching when the combined "
                             "system prompt exceeds 1024 tokens.")
    args = parser.parse_args()

    targets = _parse_and_validate_targets(args.source, args.target)

    outline_text: Optional[str] = None
    if args.outline is not None:
        outline_text = load_outline(args.outline)
        print(f"Loaded outline: {args.outline} ({len(outline_text)} chars)")

    global _default_target_lang
    _default_target_lang = targets[0]

    load_dotenv(override=True)
    api_key = os.environ.get("SONIOX_API_KEY")
    if api_key is None:
        raise RuntimeError("Missing SONIOX_API_KEY. Set it in .env or environment.")

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_api_key is None:
        raise RuntimeError("Missing ANTHROPIC_API_KEY. Set it in .env or environment.")

    if args.device is not None:
        device_index = args.device
        dev = sd.query_devices(device_index)
        print(f"Using device [{device_index}]: {dev['name']}")
    else:
        device_index, device_name = select_audio_device()
        print(f"Using device [{device_index}]: {device_name}")

    if args.port > 0:
        start_caption_server(args.port)
        print(f"Web captions: http://localhost:{args.port}")

    tunnel_proc = None
    if args.tunnel and not args.no_tunnel:
        tunnel_proc = start_cloudflare_tunnel(args.tunnel, args.port)
        print(f"Cloudflare tunnel '{args.tunnel}' started → https://live.rctranslation.org")

    try:
        print(f"Translation mode: {args.source} → {', '.join(targets)}")
        run_session(api_key, device_index, anthropic_api_key,
                    source=args.source, targets=targets, outline=outline_text)
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()


if __name__ == "__main__":
    main()
