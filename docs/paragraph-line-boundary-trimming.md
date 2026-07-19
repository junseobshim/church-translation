# Paragraph mode: trim at rendered-line boundaries (remove the chunk breaks)

Planned follow-up to the caption-viewer scrolling fixes. Not started —
implement in a future session.

## Goal

The current paragraph-mode fix groups phrases into `.para-chunk` `<div>`s of
`CHUNK_SPANS` (20) so trims drop whole chunks and never re-wrap visible text.
The cost is a visible paragraph break at every chunk boundary (~every 20
phrases). This change removes chunking entirely: paragraph mode goes back to
**one continuous flowing block**, and trimming instead cuts old content at a
**rendered-line boundary** measured in the viewer's own browser — invisible by
construction, no artificial breaks ever.

## Why cutting at a line start is invisible

Line wrapping is computed greedily from each line's left edge. If the cut
lands exactly where a rendered line already starts, the text below the cut
began at a line start before the cut and still does after it — every
downstream wrap point is unchanged. Content above the viewport just ceases to
exist; nothing the viewer can see moves. (This is the standard virtualized
text-view trick.)

Font size / viewport differences between devices don't matter: each client
measures its **own** rendered layout at trim time. Nothing is precomputed or
shared.

## Implementation plan (all in `main.py`'s `CAPTION_HTML` JS)

1. **Revert paragraph appends to flat spans.** In `appendSingleLine`, drop the
   `.para-chunk` wrapper logic — append `.span-item` spans (with `dataset.ts`)
   directly to `#lines` again. Delete `CHUNK_SPANS`.

2. **Replace `trimParagraphChunks()` with `trimParagraphLines()`.** Same
   policies, new granularity:
   - Pick the first span to keep, `S`, exactly as today: hard cap
     (`HARD_CAP`, runs regardless of pin state), then while pinned the batch
     soft cap (over `limit * 1.5` → back to `limit`) and the `HISTORY_MS`
     age-out (`dataset.ts < cutoff`). If nothing qualifies, return without
     touching layout (no measurement cost on the idle path).
   - Find the **start of the rendered line containing `S`'s first character**
     and cut there instead of at the span boundary. This keeps up to one extra
     line of aged content — semantics stay "at least HISTORY_MS", same as the
     chunk version.
   - Remove all spans wholly before the cut; if the cut falls mid-span, keep
     the span element (preserves `dataset.ts`, class, and its running CSS
     animation) and delete only the leading part of its text node
     (`textNode.splitText(offset)` then remove the first half).

3. **Finding the cut point (geometry).** Within a single block flow, character
   rect tops are monotonically non-decreasing in document order, so binary
   search works:
   - `targetTop` = top of the first character of `S` (via a collapsed-ish
     `Range` on its text node, `range.getBoundingClientRect()`).
   - Binary-search backward (across spans by first-char top, then within the
     boundary span by char offset) for the **first** position whose top is
     `>= targetTop - 0.5` (subpixel tolerance ~0.5–1px). By monotonicity that
     position is a line start: `top(pos-1) < targetTop <= top(pos)`.
   - Use the top-delta predicate, *not* a left-edge check — with
     `textAlign=center/right` a line's first char is not at the block's left
     edge, but "top strictly greater than previous char's top" identifies a
     line start under any alignment (left/center/right/justify).

4. **Cut precisely; never normalize whitespace.** `#lines` is
   `white-space: pre-wrap` and spans carry trailing spaces. The removed part
   must end exactly at the measured offset — do not strip a leading space from
   the kept remainder "for tidiness", since changing content changes wrapping.
   (With single spaces between phrases, a post-wrap line start is a non-space
   char anyway; multi-space runs are the case this rule guards.)

5. **Fail safe.** After computing the cut position, sanity-check the predicate
   (`top(pos) - top(pos-1) > tolerance`). If measurement looks wrong (fonts
   mid-load, zero rects), fall back to cutting at `S`'s span boundary — one
   visible reflow, same as the pre-chunking behavior, but bounded DOM — rather
   than skipping trims forever.

6. **Keep the existing machinery.**
   - `removeWithScrollAnchor()` semantics stay (scrollTop compensation while
     unpinned; browser clamp while pinned) — generalize it to wrap a mutation
     callback, since a text-node split isn't an element list.
   - Batch thresholds stay for CPU thrift: each trim forces layout for the
     measurements, so keep trims occasional even though they're now invisible.
   - Line mode, multi-lang mode, `trimDom()` dispatch, and the delta-protocol
     poll loop are untouched.

## Edge cases to keep in mind

- **Resize / rotation** re-wraps the whole page in any scheme (browser
  reflow); measurements are taken fresh at each trim, so the next trim is
  correct for the new geometry. No special handling.
- **Google Font late load** reflows everything once on font swap; same story.
- **Korean text** wraps mid-word (CJK line breaking) — the cut may land
  mid-word inside a phrase. That's fine: it's exactly where the browser broke
  the line, and that partial word is scrolled off-screen anyway.
- **Mutating a span's text does not restart its CSS `fadeIn`** (the element
  isn't re-inserted) — no flash on split.

## Verification

The Node DOM-stub tests can't check wrap geometry — this needs a real browser:

1. Feed fake phrases through the real server
   (`main.start_caption_server` + `_update_web_state` every ~1–2s, see the
   recipe in the scrolling-fixes session) and watch
   `?display=paragraph&historyMinutes=1`:
   - no paragraph breaks anywhere,
   - existing lines never shift or re-wrap when trims fire,
   - span/DOM count stays bounded (DevTools).
2. Repeat on **iPhone Safari** (primary audience) and desktop Chrome/Safari;
   with `fontSize=48` and `96`; `textAlign=left` and `center`; mixed
   Korean+English phrases; a `googleFont` param (late font swap).
3. Optional: a `?debugTrim=1` flag that console-asserts the cut predicate and
   logs before/after rects of the first kept line, to make manual verification
   cheap.

## Fallback

The chunking implementation (`trimParagraphChunks`, `CHUNK_SPANS`,
`.para-chunk`) lives in git history in the "caption viewer long-run fixes"
commit — revert to it if browser testing surfaces geometry surprises.

---

*Delete this file once the change lands.*
