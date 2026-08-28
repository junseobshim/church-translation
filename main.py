import json
import os
import re
import shutil
import sys
import queue
import time
import threading
import argparse
import signal
import subprocess
import http.server
import importlib
from collections import deque
from typing import Callable, Optional
from urllib.parse import urlparse

# Static frontend assets for the caption viewer (see _CaptionHandler below).
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Suppress noisy thread exception tracebacks on Ctrl+C.
threading.excepthook = lambda args: None

import sounddevice as sd
from dotenv import load_dotenv


# ── Audio constants ───────────────────────────────────────────────────────────

SAMPLE_RATE = 16000
CHUNK_FRAMES = 1600  # 100ms at 16kHz


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

# Same tags, but capturing, for splitting a phrase into its per-language runs.
_LANG_SPLIT_RE = re.compile(r"\[([a-z]{2})\]")

# Hangul, kana and CJK ideographs pack more content per character than Latin
# script, so a raw character count would call a mostly-Korean phrase "English"
# as soon as it picks up a few Latin words. Counting them double keeps the
# comparison in phrase_lang_weights roughly fair across scripts.
_CJK_RE = re.compile(r"[\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff]")


def _lang_weight(segment: str) -> int:
    return len(segment) + len(_CJK_RE.findall(segment))


def phrase_lang_weights(text: str, fallback: str) -> dict[str, int]:
    """Weight per spoken language in one render_tokens phrase.

    render_tokens interleaves [xx] tags wherever the speaker switches language
    mid-utterance, so a phrase is not necessarily monolingual. Untagged leading
    text is attributed to `fallback`.
    """
    parts = _LANG_SPLIT_RE.split(text)  # [pre, lang, run, lang, run, …]
    weights: dict[str, int] = {}
    lead = parts[0].strip()
    if lead:
        weights[fallback] = _lang_weight(lead)
    for i in range(1, len(parts), 2):
        run = parts[i + 1].strip()
        if run:
            weights[parts[i]] = weights.get(parts[i], 0) + _lang_weight(run)
    return weights


def dominant_lang(weights: dict[str, int], fallback: str) -> str:
    """The language a phrase (or a coalesced batch) is mostly in."""
    if not weights:
        return fallback
    return max(weights.items(), key=lambda kv: kv[1])[0]


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
#
# 그루터기 교회 / Remnant Church is the church's official name in each language,
# not a translation of the other — without the hint Claude renders it literally
# ("Stump Church"). There is no official Spanish name, so es targets are left to
# translate it naturally.
CHURCH_TO_EN = ('그루터기 교회 → Remnant Church (the church\'s official English '
                'name — never render it literally, e.g. "Stump Church")')
CHURCH_TO_KO = "Remnant Church → 그루터기 교회"

TERM_PREFS_BY_PAIR = {
    ("ko", "en"):    f"여러분 → everyone; 정목사 → Pastor Chung; {CHURCH_TO_EN}.",
    ("ko", "es"):    "여러분 → todos; 정목사 → Pastor Chung.",
    ("en", "ko"):    f"{CHURCH_TO_KO}.",
    ("en", "es"):    "",
    ("es", "en"):    "",
    ("es", "ko"):    f"{CHURCH_TO_KO}.",
    # Same-language targets: a bilingual source (ko+en or es+en) may also select
    # its base language as a target, so matching segments pass through unchanged
    # and only overrides for the source's *other* language apply. --source en is
    # pure English and never targets en, so there is no (en, en) entry.
    ("ko", "ko"):    f"{CHURCH_TO_KO}.",
    ("es", "es"):    "",
    # multi → any: use ko-specific prefs since 정목사 only appears in Korean speech.
    ("multi", "en"): f"여러분 → everyone; 정목사 → Pastor Chung; {CHURCH_TO_EN}.",
    ("multi", "es"): "여러분 → todos; 정목사 → Pastor Chung.",
    ("multi", "ko"): f"{CHURCH_TO_KO}.",
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

# Viewers poll /api/latest for the whole service, so the payload must stay
# bounded: keep only a recent window of lines plus a cumulative counter, and
# let clients track their position via the start/total indices in the response.
_WEB_LINES_MAX = 300

_web_state = {"lines": deque(maxlen=_WEB_LINES_MAX), "total": 0, "updated": 0}
_web_lock = threading.Lock()
_default_target_lang = "en"  # set in main() from the first --target
# Operator's current run, exposed read-only via GET /api/config so the viewer
# UI can validate a visitor's saved language against what's actually running.
_current_source = "ko"
_current_targets: list[str] = []


def _encode_web_state() -> bytes:
    """Caller must hold _web_lock (except at import time)."""
    lines = list(_web_state["lines"])
    return json.dumps({
        "lines": lines,
        "start": _web_state["total"] - len(lines),
        "total": _web_state["total"],
        "updated": _web_state["updated"],
    }).encode()


# Pre-encoded /api/latest response, rebuilt once per appended line. Viewer
# polls just grab these bytes instead of re-serializing the state under
# _web_lock on every request, which would block the transcription/translation
# threads pushing new lines.
_web_json_cache = _encode_web_state()


def _update_web_state(kind: str, lang: str, text: str, src: Optional[str] = None):
    """kind='transcription' or 'translation', lang='en'/'ko'/'es'/…

    `src` is the language the phrase was *spoken* in, carried on translation
    lines only (on a transcription line `lang` already is the spoken language).
    The two-slot caption mode needs it to tell a real translation apart from a
    same-language passthrough — see the `slot` param in static/viewer.js.
    """
    global _web_json_cache
    line = {"kind": kind, "lang": lang, "text": text}
    if src:
        line["src"] = src
    with _web_lock:
        _web_state["lines"].append(line)
        _web_state["total"] += 1
        _web_state["updated"] = time.time()
        _web_json_cache = _encode_web_state()


def _get_web_state_json() -> bytes:
    with _web_lock:
        return _web_json_cache


def _get_config_json() -> bytes:
    return json.dumps({
        "source": _current_source,
        "targets": _current_targets,
        "default_target": _default_target_lang,
    }).encode()


def _push_to_web(kind: str, text: str, fallback_lang: str = "en",
                 src: Optional[str] = None):
    """Parse [lang] prefix from text and push to web state."""
    m = re.match(r"\[([a-z]{2})\]\s*", text)
    if m:
        lang = m.group(1)
        raw_text = text[m.end():]
    else:
        lang = fallback_lang
        raw_text = text
    # render_tokens also tags language changes *inside* a phrase, and those
    # inner tags are display noise — strip them so a mid-phrase switch never
    # paints a literal "[en]" on the projection. `lang` above keeps its
    # meaning: the language the phrase started in.
    raw_text = _LANG_TAG_RE.sub("", raw_text)
    if raw_text.strip():
        _update_web_state(kind, lang, raw_text.strip(), src)


# ── HTTP Server ───────────────────────────────────────────────────────────────
#
# The caption viewer's HTML/CSS/JS used to live inline here as a Python
# triple-quoted string (CAPTION_HTML). It has been extracted to
# static/viewer.html, static/viewer.css, and static/viewer.js so it can be
# edited as ordinary web files — no Python knowledge required to work on the
# frontend. _CaptionHandler below just serves those files plus two small JSON
# endpoints; it does no HTML templating of its own anymore.


class _CaptionHandler(http.server.BaseHTTPRequestHandler):
    def _serve_static_file(self, filename: str, content_type: str):
        """Serve a file from STATIC_DIR as-is. `filename` is always one of the
        hardcoded literals below (never derived from the request path), so
        there's no path-traversal surface to worry about here."""
        path = os.path.join(STATIC_DIR, filename)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self, payload: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/latest":
                self._serve_json(_get_web_state_json())
            elif parsed.path == "/api/config":
                self._serve_json(_get_config_json())
            elif parsed.path == "/":
                self._serve_static_file("viewer.html", "text/html; charset=utf-8")
            elif parsed.path == "/viewer.css":
                self._serve_static_file("viewer.css", "text/css; charset=utf-8")
            elif parsed.path == "/viewer.js":
                self._serve_static_file("viewer.js", "application/javascript; charset=utf-8")
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass

    def log_message(self, format, *args):
        pass


def start_caption_server(port: int):
    # Threading: every phone in the congregation polls continuously; with a
    # single-threaded server one slow client stalls everyone else's captions.
    server = http.server.ThreadingHTTPServer(("", port), _CaptionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── Cloudflare Tunnel ─────────────────────────────────────────────────────────


def _resolve_cloudflared() -> str:
    """Locate the cloudflared binary.

    GUI-launched apps (e.g. the Automator control-panel app) inherit a minimal
    PATH from launchd that omits Homebrew's bin directory, so a bare
    "cloudflared" lookup raises FileNotFoundError even when it is installed. Fall
    back to the common Homebrew install locations before giving up.
    """
    found = shutil.which("cloudflared")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        "cloudflared not found on PATH or in /opt/homebrew/bin, /usr/local/bin. "
        "Install it (`brew install cloudflared`) or run with --no-tunnel."
    )


def tunnel_public_url(tunnel_name: str) -> Optional[str]:
    """Public URL served by a named tunnel, or None if it has no mapping.

    The name→URL mapping lives in tunnels.json rather than being hardcoded here
    so main.py, control_server.py and the control panel can't disagree about
    which host a tunnel fronts. An unmapped tunnel is not an error — it just
    means we can't advertise a remote URL for it.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tunnels.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("tunnels", {}).get(tunnel_name)
    except (OSError, ValueError):
        return None


def start_cloudflare_tunnel(tunnel_name: str, port: int):
    """Launch cloudflared as a subprocess for a named tunnel."""
    cloudflared = _resolve_cloudflared()
    proc = subprocess.Popen(
        [cloudflared, "tunnel", "run", "--url", f"http://localhost:{port}", tunnel_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


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


def iter_audio_chunks(device_index: int, sample_rate: int, chunk_frames: int,
                      stop_event: threading.Event):
    """Yield raw int16 PCM chunks from the mic until stop_event fires.

    Pure mic-capture; transport is the caller's responsibility.
    """
    audio_queue: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            print(f"  [Audio] {status}", file=sys.stderr)
        audio_queue.put(bytes(indata))

    stream = sd.RawInputStream(
        samplerate=sample_rate,
        blocksize=chunk_frames,
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
                yield chunk
    except Exception:
        pass


# ── Translation Worker ────────────────────────────────────────────────────────


class TranslationWorker:
    """LLM-agnostic queue/[SKIP]/rolling-context shell for one target language.

    State per worker: rolling context window (own), `[SKIP]` pending-text
    buffer (own), input queue (own), and a backend (Claude/Gemini/etc.) that
    owns the actual translation API call, cache, and keepalive. The only
    external seam is the `on_translation(target, text, src_lang)` callback
    passed at construction — the callee decides how to surface the output
    (e.g. push to web state).
    """

    def __init__(self, backend, source: str, stop_event: threading.Event,
                 on_translation: Callable[[str, str, str], None]):
        self.backend = backend
        self.source = source
        self.stop_event = stop_event
        self.on_translation = on_translation
        self.inbox: queue.Queue[tuple[str, str]] = queue.Queue()  # (spoken lang, phrase)
        self.context: list[tuple[str, str]] = []   # last 5 (source, translation)
        self.pending_text: str = ""
        # Language weights carried by pending_text, so a [SKIP]ped fragment
        # still counts toward the source language of whatever it lands in.
        self.pending_weights: dict[str, int] = {}
        # One phrase pulled off the inbox but held back because it starts a new
        # spoken language — see _run. Only ever touched by the worker thread.
        self._held: Optional[tuple[str, str]] = None
        # Coalescing across a language change would produce one output covering
        # two spoken languages, which the two-slot caption mode cannot route
        # (it drops a line whose target equals the spoken language). Only multi
        # feeds that mode, so only multi pays the extra round trip at switches.
        self.split_on_lang_change = source == "multi"
        self._run_thread: Optional[threading.Thread] = None

    def warm(self) -> None:
        self.backend.warmup()

    def start(self) -> None:
        self._run_thread = threading.Thread(target=self._run, daemon=True)
        self._run_thread.start()
        self.backend.start_keepalive(self.stop_event)

    def enqueue(self, source_lang: str, source_text: str) -> None:
        self.inbox.put((source_lang, source_text))

    def _next_item(self, block: bool) -> tuple[str, str]:
        """Next (spoken lang, phrase), held-back item first. Raises queue.Empty."""
        if self._held is not None:
            item, self._held = self._held, None
            return item
        if block:
            return self.inbox.get(timeout=0.25)
        return self.inbox.get_nowait()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                first = self._next_item(block=True)
            except queue.Empty:
                continue
            # Drain the whole backlog into one call. Phrases arrive about as
            # fast as a single translation round-trip, so translating strictly
            # one-per-call lets the queue (and the on-screen delay) grow
            # without bound; coalescing keeps the delay at worst one in-flight
            # call plus this one. When the worker is keeping up the queue is
            # empty and this is a batch of one, identical to prior behavior.
            batch = [first]
            while True:
                try:
                    nxt = self._next_item(block=False)
                except queue.Empty:
                    break
                if self.split_on_lang_change and nxt[0] != batch[-1][0]:
                    self._held = nxt  # starts a new spoken language — next call
                    break
                batch.append(nxt)
            weights = dict(self.pending_weights)
            parts = []
            for lang, text in batch:
                clean = _LANG_TAG_RE.sub("", text).strip()
                if not clean:
                    continue
                parts.append(clean)
                weights[lang] = weights.get(lang, 0) + _lang_weight(clean)
            clean_src = " ".join(parts)
            if not clean_src:
                continue
            src_lang = dominant_lang(weights, batch[-1][0])
            combined = (self.pending_text + " " + clean_src).strip() if self.pending_text else clean_src
            try:
                out = self.backend.translate(self.context, combined)
            except Exception as e:
                print(f"[{self.backend.target} translation error: {e}]", file=sys.stderr)
                self.backend.mark_activity()
                # Keep the batch for the next call — dropping it would lose a
                # whole backlog of speech on a transient API error.
                self.pending_text = combined
                self.pending_weights = weights
                continue
            if len(batch) > 1:
                print(f"[worker {self.backend.target}: coalesced={len(batch)}]",
                      file=sys.stderr)
            if "[SKIP]" in out:
                self.pending_text = combined
                self.pending_weights = weights
                continue
            self.pending_text = ""
            self.pending_weights = {}
            self.context.append((combined, out))
            if len(self.context) > 5:
                self.context.pop(0)
            prefixed = f"[{self.backend.target}] {out}"
            print(f"[Translation:{self.backend.target}] {prefixed}")
            self.on_translation(self.backend.target, prefixed, src_lang)


# ── Orchestration ─────────────────────────────────────────────────────────────


def _build_workers(client, source: str, targets: list[str],
                   outline: Optional[str], stop_event: threading.Event,
                   model: str, backend_cls) -> list[TranslationWorker]:
    """Construct one TranslationWorker per target. Per-worker cache eligibility
    is decided independently inside backend_cls.from_outline."""
    workers: list[TranslationWorker] = []
    for t in targets:
        backend = backend_cls.from_outline(client, source, t, outline, model)
        w = TranslationWorker(
            backend=backend,
            source=source,
            stop_event=stop_event,
            on_translation=lambda tgt, txt, src: _push_to_web(
                "translation", txt, fallback_lang=tgt, src=src
            ),
        )
        workers.append(w)
    return workers


def run_session(api_key: str, device_index: int, anthropic_api_key: str,
                source: str, targets: list[str], outline: Optional[str],
                transcriber_cls, backend_cls, make_client_fn, model: str) -> None:
    client = make_client_fn(anthropic_api_key)
    stop_event = threading.Event()

    workers = _build_workers(client, source, targets, outline, stop_event,
                             model, backend_cls)

    # Warm each cached worker's ephemeral cache before opening the mic.
    for w in workers:
        w.warm()
    for w in workers:
        w.start()

    transcription_fallback = PRIMARY_SRC[source]

    def on_phrase(text: str) -> None:
        _push_to_web("transcription", text, fallback_lang=transcription_fallback)
        # The phrase's dominant language, computed once and shared by every
        # worker. Deliberately not the same as the transcription line's own
        # `lang` above, which is the *leading* [xx] tag: that one labels where
        # the phrase started, this one labels what it was mostly spoken in,
        # which is what the two-slot caption mode routes on.
        spoken = dominant_lang(
            phrase_lang_weights(text, transcription_fallback),
            transcription_fallback,
        )
        # Fan-out: enqueue the raw source phrase to every target worker.
        # Each worker applies its own [SKIP] logic and rolling context.
        for w in workers:
            w.enqueue(spoken, text)

    transcriber = transcriber_cls(source=source, api_key=api_key)
    try:
        transcriber.run(device_index, on_phrase, stop_event)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        stop_event.set()


def _parse_and_validate_targets(source: str, target_arg: Optional[str]) -> list[str]:
    """Resolve --target against --source. Returns the validated target list in
    the order specified by the user (first target becomes the web default).

    A target may equal a source language when that source is bilingual (ko+en or
    es+en): matching segments pass through untranslated, exactly as --source
    multi already handles ko/en/es. --source en is pure English, so it can never
    target en (English → English does nothing)."""
    ALL = {"ko", "en", "es"}
    DEFAULTS = {"ko": "en", "multi": "ko,en,es"}

    if target_arg is None:
        if source not in DEFAULTS:
            sys.exit(f"--target is required for --source {source}")
        target_arg = DEFAULTS[source]

    targets = [t.strip() for t in target_arg.split(",") if t.strip()]
    tset = set(targets)

    if source == "multi":
        if tset != ALL or len(targets) != 3:
            sys.exit("--source multi requires --target ko,en,es (all three)")
        return targets

    if not targets or not tset.issubset(ALL):
        sys.exit(f"--target must be a non-empty subset of {sorted(ALL)}")
    if source == "en" and "en" in tset:
        sys.exit("--source en cannot target en (English → English does nothing); "
                 "choose ko and/or es")
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
                             "Use church-testing to exercise the mirrored test "
                             "stack without touching live. Use --no-tunnel to skip.")
    parser.add_argument("--no-tunnel", action="store_true",
                        help="Skip starting the Cloudflare tunnel.")
    parser.add_argument("--outline", type=str, default=None,
                        help="Path to a UTF-8 .txt sermon outline for context. "
                             "Enables per-target prompt caching when the combined "
                             "system prompt exceeds 1024 tokens.")
    parser.add_argument("--transcriber", choices=["soniox"], default="soniox",
                        help="Transcription backend (default: soniox). "
                             "Loads transcribe_<name>.py at startup.")
    parser.add_argument("--translator", choices=["claude"], default="claude",
                        help="Translation backend (default: claude). "
                             "Loads translate_<name>.py at startup.")
    args = parser.parse_args()

    try:
        tx_mod = importlib.import_module(f"transcribe_{args.transcriber}")
        tl_mod = importlib.import_module(f"translate_{args.translator}")
    except ModuleNotFoundError as e:
        sys.exit(f"Backend module not found: {e.name}")

    targets = _parse_and_validate_targets(args.source, args.target)

    outline_text: Optional[str] = None
    if args.outline is not None:
        outline_text = load_outline(args.outline)
        print(f"Loaded outline: {args.outline} ({len(outline_text)} chars)")

    global _default_target_lang, _current_source, _current_targets
    _default_target_lang = targets[0]
    _current_source = args.source
    _current_targets = targets

    load_dotenv(override=True)
    api_key = os.environ.get("SONIOX_API_KEY")
    if api_key is None:
        raise RuntimeError("Missing SONIOX_API_KEY. Set it in .env or environment.")

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_api_key is None:
        raise RuntimeError("Missing ANTHROPIC_API_KEY. Set it in .env or environment.")

    # Optional model override from the environment (e.g. CLAUDE_MODEL in .env, loaded
    # above). Falls back to the translation backend's DEFAULT_MODEL. Read here, after
    # load_dotenv, so a value set only in .env is honored.
    model = os.environ.get("CLAUDE_MODEL") or tl_mod.DEFAULT_MODEL

    if args.device is not None:
        device_index = args.device
        dev = sd.query_devices(device_index)
        print(f"Using device [{device_index}]: {dev['name']}")
    else:
        device_index = None
        print("Using system default audio input device.")

    if args.port > 0:
        start_caption_server(args.port)
        print(f"Web captions: http://localhost:{args.port}")

    # The launcher starts control_server.py backgrounded (`… &`), which sets this
    # process tree's SIGINT disposition to SIG_IGN; main.py inherits it through
    # Popen. Without re-installing handlers, control_server's SIGINT-based stop is
    # silently ignored — it then falls back to SIGTERM, whose default action kills
    # this process WITHOUT running the finally below, orphaning the cloudflared
    # tunnel (which keeps competing for the shared named tunnel and breaks routing
    # for other devices). Re-installing a handler for both signals overrides the
    # inherited SIG_IGN and routes either signal through the finally so the tunnel
    # is always torn down. Installed before the tunnel starts so no signal can
    # arrive with a tunnel up but no handler yet.
    def _graceful_shutdown(signum, frame):
        # One-shot: ignore further signals once teardown has begun. A second
        # signal (e.g. control_server's SIGTERM fallback after its 5s SIGINT
        # timeout) would otherwise raise inside the finally below and abort it
        # before the tunnel is terminated.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    tunnel_proc = None
    if args.tunnel and not args.no_tunnel:
        try:
            tunnel_proc = start_cloudflare_tunnel(args.tunnel, args.port)
            public_url = tunnel_public_url(args.tunnel)
            suffix = f" → {public_url}" if public_url else ""
            print(f"Cloudflare tunnel '{args.tunnel}' started{suffix}")
        except (RuntimeError, OSError) as e:
            print(f"Warning: could not start Cloudflare tunnel: {e}\n"
                  f"Continuing with local captions only at http://localhost:{args.port}.",
                  file=sys.stderr)

    try:
        print(f"Translation mode: {args.source} → {', '.join(targets)}")
        run_session(api_key, device_index, anthropic_api_key,
                    source=args.source, targets=targets, outline=outline_text,
                    transcriber_cls=tx_mod.Transcriber, backend_cls=tl_mod.Backend,
                    make_client_fn=tl_mod.make_client, model=model)
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()
            # 3s keeps a normal stop inside control_server's 5s SIGINT window;
            # kill() covers a cloudflared that ignores SIGTERM (the launcher's
            # pkill backstop is also SIGTERM, so it couldn't reap that either).
            try:
                tunnel_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()


if __name__ == "__main__":
    main()
