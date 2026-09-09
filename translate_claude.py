import sys
import time
import threading
from typing import Optional, Union

import anthropic

from main import OUTLINE_WRAPPER, build_prompt


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "claude-sonnet-4-6"

# output_config.effort defaults to "high" when omitted. Our output is a single
# constrained sentence with no tools and no thinking, so there is little for a
# high effort level to buy — "low" trims token spend and generation time.
EFFORT = "low"

# Models that accept output_config.effort. Sonnet 4.5 and Haiku 4.5 reject it
# outright with a 400, and CLAUDE_MODEL in .env can still select either, so
# this is a real guard rather than a formality. An unrecognized model omits
# effort entirely, which is the API default and the pre-existing behavior.
EFFORT_MODELS = frozenset({"claude-sonnet-4-6", "claude-sonnet-5"})
# Appended to the user message — never the system prompt — when the worker
# cannot afford a [SKIP]. Keeping it out of `system` leaves the cached prefix
# (prompt + outline) byte-identical, so a forced call still reads from cache.
FORCE_DIRECTIVE = (
    "\n\n(The speaker changes language after this fragment, so it will not be "
    "continued and cannot be completed by a later phrase. Translate the words "
    "present and do not output [SKIP].)"
)

KEEPALIVE_IDLE_SECONDS = 270  # 4m30s; stay under the 5-minute ephemeral TTL
KEEPALIVE_POLL_SECONDS = 10


# ── Client factory ────────────────────────────────────────────────────────────


def effort_kwargs(model: str) -> dict:
    """Extra request kwargs pinning the effort level for `model`.

    Must be applied identically to *every* call sharing a cached prefix —
    warmup, translation and keepalive. Changing effort between requests
    invalidates the messages cache, and on some models the system cache that
    holds the outline too, so setting it on only some of them would quietly
    undo the caching this module exists to set up.
    """
    if model not in EFFORT_MODELS:
        return {}
    return {"output_config": {"effort": EFFORT}}


def make_client(api_key: str) -> anthropic.Anthropic:
    """Construct the shared Anthropic client. Lets the orchestrator stay free
    of any direct `anthropic` symbol reference."""
    return anthropic.Anthropic(api_key=api_key)


# ── System prompt assembly ────────────────────────────────────────────────────


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


# ── Caching helpers ───────────────────────────────────────────────────────────


def warm_cache(client: anthropic.Anthropic,
               system: Union[str, list[dict]], model: str,
               label: str = "") -> tuple[int, int]:
    """One blocking call to populate the ephemeral cache. Returns
    (tokens written, tokens read). Exits on failure so an invalid API key or
    model name surfaces before the session starts.

    Both numbers matter: a *hit* writes nothing and reads everything, so a
    zero write on its own does not mean caching failed. Restarting a session
    within the 5-minute TTL with the same outline is a hit, and a read also
    refreshes the entry's timer.
    """
    try:
        response = client.messages.create(
            model=model,
            max_tokens=1,
            system=system,
            messages=[{"role": "user", "content": "ready"}],
            **effort_kwargs(model),
        )
    except Exception as e:
        sys.exit(f"Cache warmup failed: {e}")
    written = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    tag = f" [{label}]" if label else ""
    if written:
        print(f"Cache warmed{tag} ({written} tokens written).")
    elif read:
        print(f"Cache warmed{tag} ({read} tokens read — entry still live from "
              f"an earlier run).")
    else:
        print(f"Cache not established{tag} (nothing written or read).")
    return written, read


# ── Backend ───────────────────────────────────────────────────────────────────


class Backend:
    """Claude translation backend.

    Owns the system prompt for one (source, target) pair, cache state,
    activity tracking, and (if cached) the keepalive thread. Pure
    translation API wrapper — has no knowledge of TranslationWorker's
    queue/[SKIP]/context shell.
    """

    def __init__(self, client: anthropic.Anthropic, source: str, target: str,
                 system: Union[str, list[dict]], cache_enabled: bool, model: str):
        self.client = client
        self.source = source
        self.target = target
        self.system = system
        self.cache_enabled = cache_enabled
        # cache_enabled is corrected by warmup() from what the API actually
        # did; these two keep the original intent and the measured outcome so
        # main.py can report a cache that was asked for and silently refused.
        self.cache_requested = cache_enabled
        self.cache_written = 0
        self.cache_read = 0
        self.model = model
        self._last_activity = time.monotonic()
        self._activity_lock = threading.Lock()
        self._keepalive_thread: Optional[threading.Thread] = None

    @classmethod
    def from_outline(cls, client: anthropic.Anthropic, source: str, target: str,
                     outline: Optional[str], model: str,
                     forceable: bool = False) -> "Backend":
        prompt = build_prompt(source, target, forceable)
        if outline is None:
            system: Union[str, list[dict]] = prompt
            cache_enabled = False
        else:
            # Always ask for caching when an outline is present. The minimum
            # cacheable prefix is per-model (512-4096 tokens) and not monotonic
            # across generations, so a hardcoded threshold silently goes stale
            # on the next model change — and being wrong in the permissive
            # direction is free here, since the API just ignores cache_control
            # on a short prefix. warmup() reads the real outcome instead.
            system = build_system_blocks(prompt, outline, cache=True)
            cache_enabled = True
        return cls(client=client, source=source, target=target,
                   system=system, cache_enabled=cache_enabled, model=model)

    def warmup(self) -> None:
        if not self.cache_enabled:
            return
        self.cache_written, self.cache_read = warm_cache(
            self.client, self.system, self.model, label=self.target
        )
        if self.cache_written == 0 and self.cache_read == 0:
            # Nothing written AND nothing read: the prefix was under this
            # model's minimum cacheable length, so the API ignored
            # cache_control without raising. (A zero write with a non-zero
            # read is a hit on a still-live entry — caching is working.) Drop
            # cache_enabled so the keepalive thread doesn't spend a request
            # every 4m30s refreshing a cache entry that was never created.
            self.cache_enabled = False
            print(
                f"Warning: prompt caching was requested for [{self.target}] but "
                f"nothing was cached — the system prompt + outline is below "
                f"{self.model}'s minimum cacheable length. Running without cache.",
                file=sys.stderr,
            )

    def mark_activity(self) -> None:
        with self._activity_lock:
            self._last_activity = time.monotonic()

    def _idle_seconds(self) -> float:
        with self._activity_lock:
            return time.monotonic() - self._last_activity

    def translate(self, context: list[tuple[str, str]], latest: str,
                  force: bool = False) -> str:
        """Translate `latest`. With `force`, instruct the model not to [SKIP] —
        see TranslationWorker._flush_pending for when that is needed."""
        messages: list[dict] = []
        for s, t in context:
            messages.append({"role": "user", "content": s})
            messages.append({"role": "assistant", "content": t})
        messages.append({
            "role": "user",
            "content": (latest + FORCE_DIRECTIVE) if force else latest,
        })
        # 4096 is a ceiling, not a target — costs nothing unless generated.
        # Sized so a coalesced catch-up batch after a long stall (the worker
        # drains its whole backlog into one call) still fits without truncation.
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=self.system,
            messages=messages,
            **effort_kwargs(self.model),
        )
        if resp.stop_reason == "max_tokens":
            print(f"[{self.target} translation truncated at max_tokens]",
                  file=sys.stderr)
        u = resp.usage
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        print(f"[usage {self.target}: in={u.input_tokens} cache_read={cr} "
              f"cache_write={cw} out={u.output_tokens}]", file=sys.stderr)
        out = resp.content[0].text.strip()
        self.mark_activity()
        return out

    def start_keepalive(self, stop_event: threading.Event) -> None:
        if not self.cache_enabled:
            return
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, args=(stop_event,), daemon=True
        )
        self._keepalive_thread.start()

    def _keepalive_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(KEEPALIVE_POLL_SECONDS):
            try:
                if self._idle_seconds() < KEEPALIVE_IDLE_SECONDS:
                    continue
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=1,
                    system=self.system,
                    messages=[{"role": "user", "content": "ready"}],
                    **effort_kwargs(self.model),
                )
                self.mark_activity()
                u = response.usage
                cr = getattr(u, "cache_read_input_tokens", 0) or 0
                cw = getattr(u, "cache_creation_input_tokens", 0) or 0
                print(f"[keepalive {self.target}: cache_read={cr} cache_write={cw}]",
                      file=sys.stderr)
            except Exception as e:
                print(f"[keepalive {self.target} error: {e}]", file=sys.stderr)
