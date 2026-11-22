"""Per-session SRT transcript of the source-language speech.

Plain spoken text only — no language tags, no translations. One file per
session in transcripts/, named after the session's start time, so cue times
are offsets from that moment. To line the file up with a recording (e.g. the
YouTube stream, which starts at a different moment), shift every cue by a
constant offset in any subtitle editor.
"""

import os
import time
import unicodedata
from datetime import datetime
from typing import Optional

TRANSCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcripts")

# Cue boundaries follow the sentence, not the transcriber's phrases: Soniox's
# phrase gate often fires mid-sentence, and ending a cue there leaves sub-second
# fragments ("안 되느냐입니다.") too brief to read. A cue ends only:
#   - after sentence-final punctuation,
#   - when the next word would push it past MAX_CUE_CHARS or MAX_CUE_MS, so a
#     run-on sentence still splits into readable cues. The split goes after the
#     cue's last comma when one falls past the first third of the cue, so the
#     overflow doesn't strand a word or two as its own flash-by cue; otherwise
#     before the overflowing word.
#   - before a pause longer than MAX_GAP_MS (so a cue doesn't sit on screen
#     through a long silence waiting for the sentence to finish).
# A cue shorter than MIN_CUE_MS is held on screen until MIN_CUE_MS or the next
# cue's start, whichever is sooner, so short utterances stay readable.
SENTENCE_END = (".", "?", "!", "。", "？", "！")
CLAUSE_END = (",", "，", "、")
MAX_CUE_CHARS = 80
MAX_CUE_MS = 7000
MAX_GAP_MS = 2000
MIN_CUE_MS = 1500


def _is_punct(piece: str) -> bool:
    """A token that is only punctuation, e.g. a sentence's closing "."."""
    piece = piece.strip()
    return bool(piece) and all(unicodedata.category(c).startswith("P") for c in piece)


def _srt_time(ms: int) -> str:
    ms = max(0, ms)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


class SrtTranscript:
    """Appends cues to transcripts/<start datetime>.srt as speech is finalized.

    The file is created on the first cue, so a session with no speech leaves
    nothing behind. A closed cue is written once the next word arrives (its
    end may stretch toward that word — see MIN_CUE_MS), and an unfinished
    sentence waits for the next phrase; close() writes both. A crash loses at
    most those two cues. Not thread-safe — call from one thread.
    """

    def __init__(self, directory: str = TRANSCRIPTS_DIR):
        self.t0_ms = time.time() * 1000
        name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".srt"
        self.path = os.path.join(directory, name)
        self._f = None
        self._n = 0
        # The cue being built, as (text, start, end) tokens (epoch ms).
        # Survives across add_tokens calls so a sentence can span phrases.
        self._cue: list[tuple[str, float, float]] = []
        # The last closed cue as (text, start, end), held until the next word's
        # start is known so its end can be extended up to MIN_CUE_MS.
        self._held: Optional[tuple[str, float, float]] = None

    def _cue_text(self, cue=None) -> str:
        return "".join(t[0] for t in (self._cue if cue is None else cue))

    def add_tokens(self, tokens: list[dict]) -> None:
        """tokens: one phrase's Soniox spoken tokens, carrying wall-clock
        `abs_start_ms` / `abs_end_ms` (epoch ms) added by the transcriber,
        because Soniox's own start_ms/end_ms restart at 0 on every reconnect."""
        first = True
        for tok in tokens:
            piece = tok["text"]
            if piece == "<end>":
                continue
            # Soniox can timestamp punctuation well after the word it closes
            # (seen: a "." >2s after "이게", at the end of the pause), which
            # tripped the pause rule and made a cue of just ".". Punctuation
            # always attaches to the text before it, and its time is ignored
            # so it can't stretch a cue across the pause either.
            if _is_punct(piece) and (self._cue or self._held):
                first = False
                if self._cue:
                    _, s0, e0 = self._cue[-1]
                    self._cue.append((piece, s0, e0))
                    if self._cue_text().rstrip().endswith(SENTENCE_END):
                        self._close_cue()
                else:
                    text, s0, e0 = self._held
                    self._held = (text + piece.rstrip(), s0, e0)
                continue
            # Soniox marks word starts with a leading space. Defensive: if a
            # phrase's first token joins an open cue without one, add it, since
            # gluing two words together is worse than a stray space.
            if first and self._cue and not piece[:1].isspace() \
                    and not self._cue_text()[-1:].isspace():
                piece = " " + piece
            first = False
            start, end = tok["abs_start_ms"], tok["abs_end_ms"]
            self._write_held(next_start=start)

            if self._cue:
                if start - self._cue[-1][2] > MAX_GAP_MS:
                    self._close_cue()
                elif piece[:1].isspace() and (
                        len((self._cue_text() + piece).strip()) > MAX_CUE_CHARS
                        or end - self._cue[0][1] > MAX_CUE_MS):
                    self._split_cue()

            self._cue.append((piece, start, end))
            if self._cue_text().rstrip().endswith(SENTENCE_END):
                self._close_cue()

    def _split_cue(self) -> None:
        """Close the open cue because it's full: after its last comma past the
        first third (the rest starts the next cue), else entirely."""
        cut = len(self._cue)
        min_head = len(self._cue_text().strip()) / 3
        for i in range(len(self._cue) - 1, 0, -1):
            if len(self._cue_text(self._cue[:i]).strip()) < min_head:
                break
            if self._cue[i - 1][0].rstrip().endswith(CLAUSE_END) \
                    and self._cue[i][0][:1].isspace():
                cut = i
                break
        rest = self._cue[cut:]
        self._cue = self._cue[:cut]
        self._close_cue()
        self._cue = rest

    def _close_cue(self) -> None:
        cue, self._cue = self._cue, []
        text = self._cue_text(cue).strip()
        if not text:
            return
        self._write_held(next_start=cue[0][1])  # normally already written
        self._held = (text, cue[0][1], cue[-1][2])

    def _write_held(self, next_start: Optional[float]) -> None:
        if self._held is None:
            return
        text, start, end = self._held
        self._held = None
        # An open cue (e.g. the remainder of a comma split) is the next cue,
        # and it started before whatever word the caller is looking at.
        if self._cue:
            next_start = self._cue[0][1]
        # Stretch a short cue to MIN_CUE_MS, but never into the next cue and
        # never shorter than the speech itself.
        target = start + MIN_CUE_MS
        if next_start is not None:
            target = min(target, next_start)
        self._emit(text, start, max(end, target))

    def _emit(self, text: str, start_abs: float, end_abs: float) -> None:
        start = int(start_abs - self.t0_ms)
        end = int(end_abs - self.t0_ms)
        end = max(end, start + 1)  # SRT players reject zero-length cues
        if self._f is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._f = open(self.path, "a", encoding="utf-8")
        self._n += 1
        self._f.write(f"{self._n}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n\n")
        self._f.flush()

    def close(self) -> None:
        # Runs in run_session's shutdown path: never let a write error here
        # skip the teardown after it (the tunnel).
        try:
            self._close_cue()  # an unfinished sentence at shutdown
            self._write_held(next_start=None)
            if self._f is not None:
                self._f.close()
        except OSError as e:
            print(f"[Transcript] close error: {e}")
        self._f = None
