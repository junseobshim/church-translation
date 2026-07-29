# Caption viewer: fix the top-line position shift (revised plan)

> Issue re-confirmed in current `main.py` (main @ 9f368bf, 2026-07-12). The
> previous fix (fractional line-height snap) was verified to kill the shift but
> was reverted because it surfaced a descender-residue artifact on the real
> display. This revision goes straight to a **whole-pixel** line-height snap
> with the leftover absorbed at the bottom — same no-shift guarantee, but lines
> and scroll steps stay on the device pixel grid, which is the leading suspect
> for the residue. Residue notes kept at the end as deferred reference.

## The issue — first line sits a few px lower than every later top line

With font size / weight / `lineSpacing` tuned so the screen fits exactly N
lines (user's case: N = 8), the moment the Nth line fills the screen the top
line jumps up slightly. Ever after, the top line rests a few px higher than
where the very first line of the session sat.

Requirement: the first line must appear **at the top** (bottom-anchored /
scroll-up layouts are ruled out), and it must sit at exactly the same y as
every later top line.

### Root cause (confirmed in code + real Blink layout)
- `#container` is a plain top-aligned `overflow-y:auto` scroll box
  (`main.py:243`), padded at runtime (`main.py:467`). No flex/anchoring.
- `linesDiv` gets `line-height: lineSpacing` as a bare multiplier
  (`main.py:478`) — e.g. 48 × 1.4 = 67.2px.
- On every new phrase, while pinned, `scrollToBottom()` (`main.py:634`, called
  from `poll()` at `main.py:931`) sets `scrollTop = scrollHeight`.
- While content ≤ viewport there's nothing to scroll, so line 1 rests at
  `y = padding`. The instant content exceeds the viewport by a fractional
  overflow **ε** (avail = clientHeight − 2·pad is not a multiple of the
  line-height), `scrollTop` becomes ε and the whole block jumps up by ε.
  Steady state then rests every top line at `padding − ε`; line 1, seen only
  pre-overflow, sat at `padding` — exactly ε lower.
- Measured (viewport 800×575, fontSize 48, lineSpacing 1.4, padding 20):
  avail 535, lh 67.2 → 8th line overflows by 2.6px → ~3px shift. Matches the
  headed/headless Chrome measurements from the previous session.

`maxLines` (`main.py:743`) doesn't help — it only trims retained DOM lines,
never the top-aligned → bottom-pinned transition.

## Revised fix — integer-px line-height snap + bottom gap absorption

Make the overflow always a whole number of lines, so `scrollTop` is always an
exact multiple of the line-height and the top line never moves:

1. **Snap the line-height to a whole CSS pixel**, not to `avail / n`:
   `n = round(avail / naturalLh)` keeps the user's tuned line count;
   `lh = floor(avail / n)` is integer px. (Must be `floor`, never `round` —
   rounding up would make n lines overflow by a fraction and reintroduce ε.)
   Integer px is exactly representable in Blink's LayoutUnit and lands on the
   device pixel grid for integer DPRs — this is the change aimed at not
   resurrecting the residue.
2. **Absorb the leftover** `gap = avail − n·lh` (0 … n−1 px) at the bottom so
   n lines + gap exactly fill the content box: zero the container's own
   bottom padding and move it into `#lines`' `padding-bottom = pad + gap`.
   - Why on `#lines` and not the container: a child's padding is
     unambiguously part of scrollHeight in every engine; whether a scroll
     container's *own* block-end padding counts is the classic cross-browser
     quirk. With container bottom padding at 0, the math can't drift on
     phones/Safari.
   - Why not the doc's earlier `container.style.height = n·lh + 2·pad`
     variant: equivalent visually, but it breaks `clientHeight === viewport`
     (resize recompute must then special-case), and the strip below the
     container stops responding to wheel/touch. The padding variant has
     neither problem. Keep the height variant as fallback if the padding one
     misbehaves.
3. Result while pinned: content height above the gap is always `k·lh`, so
   `maxScroll = (k − n)·lh` — a whole multiple. Top line at `y = pad` before
   overflow **and** forever after. Cost: the last line's bottom sits
   `gap` px (≤ ~7px in the 8-line case) higher than before — invisible on
   full-screen / OBS.

### Sketch (insert after the `linesDiv.style.cssText` block, `main.py:482`)

```js
  // Snap line-height to a whole px so an integer number of lines plus a small
  // bottom gap exactly fills the viewport: overflow is then always a whole
  // line and the top line never moves. Integer px keeps lines and scroll
  // steps on the device pixel grid. Skipped in multi-language mode (blocks
  // carry margins; rows aren't uniform height).
  const naturalLineHeight = (function() {
    const cs = getComputedStyle(linesDiv);
    let lh = parseFloat(cs.lineHeight);
    if (!isFinite(lh)) lh = 1.2 * parseFloat(cs.fontSize);   // 'normal'
    else if (lh < 4) lh *= parseFloat(cs.fontSize);          // bare multiplier
    return lh;
  })();

  function snapLinesToViewport() {
    if (multiLangMode || !(naturalLineHeight > 0)) return;
    const pad = parseFloat(padding) || 0;
    const avail = container.clientHeight - 2 * pad;   // clientHeight stays = viewport
    if (avail <= 0) return;
    const n  = Math.max(1, Math.round(avail / naturalLineHeight));
    const lh = Math.floor(avail / n);                 // integer px — never round up
    if (!(lh > 0)) return;
    const gap = avail - n * lh;
    linesDiv.style.lineHeight = lh + 'px';
    container.style.paddingBottom = '0';
    linesDiv.style.paddingBottom = (pad + gap) + 'px';
  }

  snapLinesToViewport();

  window.addEventListener('resize', function() {
    snapLinesToViewport();
    if (pinnedToBottom) scrollToBottom();
  });
```

### Scope / interactions checked
- **Applies to `display=line` and `display=paragraph`** — both are uniform
  line-height flows. A `.line-item` that wraps to 2 rendered lines is still
  2·lh tall (no margins), and paragraph trims cut at rendered-line boundaries
  (`cutAtLineStart`), so scrollHeight always changes by whole lines.
- **Skip `multiLangMode`** — `.multi-line-block` has `margin-bottom: 0.45em`
  and variable per-block heights; the whole-line invariant can't hold there.
- `lineTolerance()` (`main.py:709`) keeps working — it now reads the snapped
  px value directly.
- Explicit px line-height is immune to Google Font swap (wrap points may
  change; line heights don't).
- Fade-in animation is opacity-only — no layout effect.

### Preferred sizing mode for the TV: `lines=N` param (drives font size)
The control panel emits exactly two presets (`control.html` `buildLinks()`):
TV (`fontSize=72&padding=40`, fixed screen) and Phones (`fontSize=28`, remote
base). For the TV, line count is the native tuning unit — so add an optional
`lines=N` param that takes precedence over fontSize-driven snapping:

```js
  // inside snapLinesToViewport(), after computing avail:
  const n = screenLines > 0
    ? screenLines                                   // lines=N drives sizing
    : Math.max(1, Math.round(avail / naturalLineHeight));
  const lh = Math.floor(avail / n);
  if (screenLines > 0) linesDiv.style.fontSize = (lh / parseFloat(lineSpacing)) + 'px';
  linesDiv.style.lineHeight = lh + 'px';
  // …gap absorption as above
```

- With `lines=N` there is **no rounding heuristic and no spacing deviation**:
  n is exact, spacing ratio is exactly `lineSpacing`, no-shift holds by
  construction, and the same URL fits N lines on any screen resolution.
- **Do not eliminate fontSize**: phones need it. Height-driven sizing on a
  portrait phone gives comically large text (lines=8 on ~800px avail → ~71px
  font → ~8 chars/line) and font-size jumps on rotation; a handheld's natural
  unit is font size. Multi-lang mode also can't be line-driven (non-uniform
  blocks). fontSize URLs also stay for backward compat (bookmarks/worker).
- Changes: viewer JS (~10 lines on top of the snap), TV preset in
  `control.html` → `lines=8&padding=40` (drop fontSize there).
- Name it `lines` (or `screenLines`) — distinct from existing `maxLines`,
  which caps retained DOM history, not visible lines.
- Derived fontSize may be fractional px — fine; lh stays integer, which is
  what matters for scroll/grid alignment.

### Resilience to font changes
- **Family / fallback fonts**: line boxes follow the inherited px
  `line-height`, not font metrics — mixed Korean/Latin fallbacks and late
  Google Font loads change wrap points only, never line geometry. And since
  `lineSpacing` is a multiplier of `fontSize`, `naturalLineHeight` is the same
  before and after the font loads.
- **Font size**: URL param → only changes with a reload, and the snap runs on
  every load (plus resize). Bigger font → smaller `n`, always exact fill.
- **No "lots of space" case**: the leftover is folded into the leading, never
  left as a bottom gap. If the natural fit is 7.4 lines, `round` gives 7
  lines *stretched* to fill (lh 76 vs natural 72); at 7.6 it gives 8 lines
  slightly squeezed. Effective spacing deviates from the requested
  `lineSpacing` by at most ±(0.5/n): ~6% at n=8, 12.5% at n=4 — imperceptible
  at caption sizes; only degenerate at n=1–2 (huge fonts). If "never tighter
  than requested" ever matters, switch `Math.round` → `Math.floor` when
  picking n (prefers fewer, looser lines). Exact fill + exact spacing +
  no shift is a pick-two triangle; this plan flexes spacing because the
  alternative is a visible bottom gap of up to one line.
- Known imperfection: fractional `devicePixelRatio` (some Androids) still puts
  integer CSS px off the device grid. Ignore unless residue is seen there.

### Test plan
Use the harness from the `caption-viewer-browser-test-recipe` memory
(`venv/bin/python` feeder pushing via `main._update_web_state`, puppeteer-core
+ system Chrome at exact viewport):
1. `display=line`, 800×575, fontSize 48, lineSpacing 1.4, padding 20: assert
   first `.line-item` `getBoundingClientRect().top === 20` at 7, 8, and 12
   lines, and `container.scrollTop % lh === 0` throughout.
2. Same assertions in `display=paragraph` with wrapping text, across a few
   trim cycles (trims must not disturb the top-line y while pinned).
3. Resize the viewport mid-run: top line returns to `pad` after recompute.
4. Multi-lang mode: assert line-height/padding untouched (snap skipped).
5. Then check the real display for residue (screenshots can't show it — see
   below).

## Deferred: descender residue (reference notes from previous session)

Appeared only after the *fractional* snap was applied; may not occur with the
integer-px plan above — check on the real display after implementing.

- Verified then: layout is correct — frame-diffing scrolled frames against
  forced full repaints (fades disabled) showed **0 differing pixels** in both
  headless and headed Chrome. It's an on-screen GPU-compositor paint artifact;
  `page.screenshot` re-rasterizes clean, so Puppeteer cannot reproduce it.
- Leading hypothesis: fractional-px line-height put scroll steps off the
  device pixel grid, and exact-line-boundary scrolling puts the repaint seam
  through descenders. The integer-px snap addresses the first half directly.
- Independent thing to rule out first if it reappears: **transparent
  `bgColor`** (OBS overlay) — blit-scrolling over transparency is a classic
  standalone cause of scroll trails.
- If it still appears: try opaque background, then forcing a full repaint per
  scroll (costs smoothness).
