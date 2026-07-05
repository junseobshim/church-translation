import json
import threading
from typing import Callable, Optional

from websockets import ConnectionClosedOK
from websockets.sync.client import connect

from main import SAMPLE_RATE, CHUNK_FRAMES, SOURCE_LANGS, iter_audio_chunks


# ── Soniox constants ──────────────────────────────────────────────────────────

SONIOX_WEBSOCKET_URL = "wss://stt-rt.soniox.com/transcribe-websocket"


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
              "Live multilingual church sermon in Korean, English, and Spanish, with occasional Chinese, with a pastor preaching to the congregation."),
}


def build_soniox_config(source: str, api_key: str) -> dict:
    """Build the initial-frame JSON for the Soniox STT websocket.

    `translation.target_language` is fixed at `ja` across all sources — Soniox
    translation tokens are used only as a phrase-boundary gating signal; the
    translated text is discarded. The pivot must be a language no speaker will
    use: a source segment already in the target language produces no translation
    tokens, so it would never trip the gate. `multi` now includes Chinese, so
    `zh` can no longer serve as the pivot; `ja` is unused across all sources.
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
            "target_language": "ja",
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


# ── Token Rendering ───────────────────────────────────────────────────────────


def render_tokens(final_tokens: list[dict]) -> str:
    """Convert Soniox tokens into a readable transcript, interleaving [xx] tags
    on language changes."""
    text_parts: list[str] = []
    current_language: Optional[str] = None

    for token in final_tokens:
        text = token["text"]
        if text == "<end>":
            continue
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


# ── Transcriber ───────────────────────────────────────────────────────────────


class Transcriber:
    """Soniox real-time transcription backend.

    Owns the websocket session, audio pump, and recv/gating loop. Each
    finalized phrase is emitted via the on_phrase(text) callback; the
    [lang] prefix from render_tokens is preserved so the orchestrator can
    fan out the source text to every translation worker unchanged.
    """

    def __init__(self, source: str, api_key: str):
        self.source = source
        self.api_key = api_key

    def run(self, device_index: int, on_phrase: Callable[[str], None],
            stop_event: threading.Event) -> None:
        config = build_soniox_config(self.source, self.api_key)

        # Accumulated token state — preserved across reconnects so transcription
        # context is not lost if the WebSocket drops and reconnects.
        final_tokens: list[dict] = []
        final_translation_tokens: list[dict] = []
        prev_final_count = 0
        prev_translation_count = 0

        RECONNECT_DELAY = 2   # seconds to wait before reconnecting
        MAX_RECONNECTS = 10   # give up after this many consecutive failures

        consecutive_failures = 0

        while not stop_event.is_set():
            try:
                print(f"Connecting to Soniox{'(reconnecting)' if consecutive_failures else ''}...")
                with connect(SONIOX_WEBSOCKET_URL) as ws:
                    ws.send(json.dumps(config))
                    consecutive_failures = 0  # reset on successful connect

                    def audio_pump():
                        try:
                            for chunk in iter_audio_chunks(device_index, SAMPLE_RATE,
                                                           CHUNK_FRAMES, stop_event):
                                ws.send(chunk)
                        except Exception:
                            pass
                        try:
                            ws.send("")
                        except Exception:
                            pass

                    audio_thread = threading.Thread(target=audio_pump, daemon=True)
                    audio_thread.start()

                    print("Session started. Speak into your microphone. Press Ctrl+C to stop.")

                    try:
                        while True:
                            message = ws.recv()
                            res = json.loads(message)

                            if res.get("error_code") is not None:
                                print(f"Error: {res['error_code']} - {res['error_message']}")
                                break

                            for token in res.get("tokens", []):
                                if token.get("text"):
                                    if token.get("translation_status") == "translation":
                                        if token.get("is_final"):
                                            final_translation_tokens.append(token)
                                        continue
                                    if token.get("is_final"):
                                        final_tokens.append(token)

                            if len(final_translation_tokens) == prev_translation_count:
                                continue

                            new_tokens = final_tokens[prev_final_count:]
                            prev_final_count = len(final_tokens)
                            prev_translation_count = len(final_translation_tokens)
                            text = render_tokens(new_tokens)

                            print(f"[Transcription] {text}")
                            on_phrase(text)

                            if res.get("finished"):
                                print("Session finished.")
                                stop_event.set()
                                break

                    except ConnectionClosedOK:
                        if stop_event.is_set():
                            break
                        print("[Soniox] Connection closed — reconnecting…")
                    except KeyboardInterrupt:
                        print("\nInterrupted by user.")
                        stop_event.set()
                        break
                    except Exception as e:
                        if stop_event.is_set():
                            break
                        print(f"[Soniox] Session error: {e} — reconnecting…")
                    finally:
                        audio_thread.join(timeout=2)

            except KeyboardInterrupt:
                stop_event.set()
                break
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures >= MAX_RECONNECTS:
                    print(f"[Soniox] Failed to connect after {MAX_RECONNECTS} attempts: {e}")
                    stop_event.set()
                    break
                print(f"[Soniox] Connection error ({consecutive_failures}/{MAX_RECONNECTS}): {e}")

            if not stop_event.is_set():
                print(f"[Soniox] Waiting {RECONNECT_DELAY}s before reconnect…")
                stop_event.wait(RECONNECT_DELAY)
