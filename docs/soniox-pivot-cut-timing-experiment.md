# Experiment: how Soniox segments across different `target_language` pivots

> Observe how the choice of `translation.target_language` (the gating pivot in
> `transcribe_soniox.py`) changes **where Soniox cuts phrases** for
> transcription and translation. Run the real live pipeline against one fixed
> audio section, varying only the pivot, and capture the transcript +
> translations to files so runs can be compared side by side.
>
> Line numbers are as of 2026-07-05 and will drift.

## Background — the pivot is a segmentation clock

`transcribe_soniox.py` sets `translation.target_language` but **discards the
translated text**. The pivot's only job is to mark phrase boundaries. The gate
(`Transcriber.run`, ~lines 170–188) is:

```python
if token["translation_status"] == "translation" and token["is_final"]:
    final_translation_tokens.append(token)          # count the pivot's finals
...
if len(final_translation_tokens) == prev_translation_count:
    continue                                         # no new pivot finals → hold
new_tokens = final_tokens[prev_final_count:]         # else emit everything
on_phrase(render_tokens(new_tokens))                 # finalized since last cut
```

So **a phrase is emitted the moment the pivot produces a new *final* translation
token**, and the text handed to Claude is the *original* transcription tokens
finalized since the last emit — never the pivot's own output. Different pivots
can finalize their translations at different points in the same audio, which is
exactly what this experiment measures: **does the pivot language move the cut
points, and if so, how?**

## Why the pivot must be a non-source language

A segment already in the pivot language produces no translation tokens
(`en → en` is a no-op), so it never ticks the clock and would hang un-emitted
until the next foreign phrase flushes it. That is why the pivot must be a
language **none of the configured sources use**. For our sources
(`SOURCE_LANGS`, `main.py:36`) the excluded set is `ko`, `en`, `es` (and `zh` if
`multi` ever re-adds Chinese). Every candidate below is outside that set. See
`chinese-multi-mode-rollback.md` for the same reasoning applied to `zh`.

## Setup — one fixed audio section, live pipeline, one variable

- **Audio:** you play the same YouTube section into the live pipeline via
  virtual audio routing, identically for every run. Start each run from the same
  cue so the runs line up by content.
- **Pipeline:** the normal `main.py` run — no replay harness, no recording step.
- **Hold everything constant except the pivot:** same `--source`, same
  `--target` (the language you actually care about downstream, e.g.
  `--source ko --target en`), same device, same endpoint settings (defaults),
  same outline. The pivot is the *only* thing that changes between runs.

Endpoint detection (`enable_endpoint_detection: true`, defaults) also influences
where segments finalize; leaving it identical across runs keeps the pivot as the
sole variable so any difference in cut points is attributable to it.

## The one addition — capture transcript + translations to files

Both the transcription callback (`on_phrase`, `main.py:1076`) and every
translation callback (`_build_workers`, `main.py:1051`) funnel through
`_push_to_web` (`main.py:214`). That single seam is where to tee to disk, gated
by an env var so production is untouched:

```python
# main.py — near the other module-level state
import time
_CAPTURE_DIR = os.environ.get("CAPTURE_DIR")   # e.g. "runs/af"; unset = disabled
_CAPTURE_T0 = time.monotonic()

def _capture(kind: str, lang: str, text: str) -> None:
    if not _CAPTURE_DIR:
        return
    os.makedirs(_CAPTURE_DIR, exist_ok=True)
    fname = "transcript.txt" if kind == "transcription" else f"translation.{lang}.txt"
    with open(os.path.join(_CAPTURE_DIR, fname), "a", encoding="utf-8") as f:
        f.write(f"{time.monotonic() - _CAPTURE_T0:8.2f}  {text}\n")
```

Then one line inside `_push_to_web`, right after `lang`/`raw_text` are computed:

```python
    if raw_text.strip():
        _capture(kind, lang, text)          # ← add: full phrase, leading tag intact
        _update_web_state(kind, lang, raw_text.strip())
```

Each line in `transcript.txt` is **one Soniox cut** (with its run-relative
timestamp); each `translation.<lang>.txt` line is Claude's output for a cut.
Because every run replays the same section, the files align by content and the
leading offset lets you compare cut cadence.

## Changing the pivot between runs

The pivot is hardcoded (`build_soniox_config`, `transcribe_soniox.py:63`,
currently `"af"`). Two ways to vary it:

- **Zero new code:** edit the `target_language` constant before each run and set
  a matching `CAPTURE_DIR` (`CAPTURE_DIR=runs/af`, then `runs/ja`, …).
- **Optional one-liner** to avoid editing code each time — make it env-driven:
  ```python
  "target_language": os.environ.get("SONIOX_PIVOT", "af"),   # add `import os`
  ```
  then `SONIOX_PIVOT=ja CAPTURE_DIR=runs/ja ./venv/bin/python main.py …`.

Confirm each candidate is a valid Soniox translation target before relying on it
(the supported set is large but not universal — check `/translation/supported-languages`).

## Pivot matrix — spread across word-order families

Pick pivots that span structural families, so the comparison covers the range of
how Soniox might segment rather than four near-identical languages:

| Pivot | Family / word order | Note |
| --- | --- | --- |
| `af` | Germanic, SVO | current pivot; baseline |
| `fr` *or* `de` | Romance / Germanic, SVO | second SVO point (is `af` representative?) |
| `ja` | SOV, verb-final | previous pivot; heavy reordering vs SVO sources |
| `tr` *or* `hi` | SOV, verb-final, distant from ko/en/es | max-reordering point |

Four is a good bound on runtime. If you only have time for two, run `af` (SVO)
vs `ja` (verb-final) — the widest structural contrast.

## What to look at

Diff the run directories (`runs/af/transcript.txt` vs `runs/ja/…`, etc.). Since
the words are identical, only the **boundaries** differ. Useful comparisons:

| Signal | Read from | Tells you |
| --- | --- | --- |
| number of cuts over the section | line count of `transcript.txt` | granularity per pivot |
| words / phrase, distribution | line contents | how much context each emit carries |
| where boundaries land | eyeball | clause/sentence boundaries vs mid-clause |
| cut cadence | leading timestamps | how bursty vs steady the segmentation is |
| downstream coherence | `translation.<lang>.txt` | whether Claude buffered (`[SKIP]`) or translated cleanly per cut |

The translation files are the ground-truth read: whatever pivot lets Claude
produce clean, coherent output per emitted phrase is doing the segmentation job
well, regardless of the intermediate metrics.

## Hypotheses about how the pivot may shift cuts

These are expectations to check against the data, not claims — Soniox does not
document the segmentation policy, so the experiment is the source of truth.

- **Word-order distance (source ↔ pivot) may set how eagerly Soniox commits a
  translation, and thus when the clock ticks.** Streaming MT can emit chunks
  mid-sentence for near-monotonic pairs (little reordering), but must buffer more
  of the clause for pairs that reorder heavily (e.g. SVO source → verb-final
  pivot, which can't place its final verb until later). If cuts are driven by
  this streaming behavior, then for an SVO-leaning source a verb-final pivot
  (`ja`/`tr`) would tend to produce **later, fuller** segments than an SVO pivot
  (`af`), and for a Korean (verb-final) source the direction flips. Because our
  audio is Korean-dominant with embedded English — and `multi` mixes three word
  orders — no single pivot is uniformly near or far from every segment, which is
  why we measure per source rather than reason it out.

- **Cuts may instead be dominated by endpoint detection**, which finalizes
  segments on semantic/pause cues independently of the pivot. If so, the pivot
  will barely move the boundaries and the run directories will look nearly
  identical — itself a useful result: it says segmentation is governed by
  endpoint settings, not the pivot, and points any future tuning there instead.

- On the specific question of using an **English-like pivot to "cut where
  English wants":** the pivot's word order is matched against the *source*, not
  against Claude's target (we throw the pivot text away), so "similar to English"
  only bears on segments that are *spoken* in English. Whether it helps or hurts
  is one of the things the `af` vs `ja`/`tr` contrast will show directly.

## Interpreting results / next steps

- If a pivot clearly produces fuller, cleaner segments for your primary
  `--source`, adopt it (keeping it a non-source language).
- If the run directories are near-identical, segmentation is endpoint-governed;
  revisit the endpoint parameters rather than the pivot.
- Either way, the app owns the emit gate, so the findings can also inform a
  future gate-side segmentation policy (e.g. keying emits off `<end>` or a
  minimum segment size) — worth capturing raw tokens in a later pass if you want
  to see the internal `is_final`/`<end>` triggers behind each cut.
