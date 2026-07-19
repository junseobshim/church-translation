# Caption viewer: fix the top-line position shift (and the descender residue it surfaces)

> Two linked issues in `main.py`'s `CAPTION_HTML` viewer. The **top-line shift**
> is fully diagnosed and has a verified fix (below) — it just needs re-applying.
> Applying it surfaces a secondary **descender-residue** artifact that is *not*
> yet fixed and needs work on a real screen. Nothing here is committed; the fix
> was reverted at the end of the session so `main.py` holds only the unrelated
> `hideStatus` change (see `commit-message.txt`).

## Issue 1 — first line sits a few px lower than every later top line

### Symptom
With font size / weight / `lineSpacing` tuned so the screen fits exactly N lines
(user's case: N = 8), the moment the Nth line fills the screen the **top line
jumps up very slightly**. Ever after, the top line of the screen rests a few px
higher than where the very first line of the session sat. The first line is the
only one seen a few px too low.

### Root cause (confirmed in code + real Blink layout)
- `#container` is a plain top-aligned `overflow-y:auto` scroll box — no flex /
  `justify-content` (`main.py` ~L243).
- On every new phrase, while pinned, it runs `scrollTop = scrollHeight`
  (`scrollToBottom`, ~L634, called ~L931).
- While content ≤ viewport there's nothing to scroll, so line 1 rests at
  `y = padding`. The instant content exceeds the viewport by a fractional
  overflow **ε** (because the viewport height is not an exact multiple of the
  line-height), `scrollToBottom` sets `scrollTop = ε` and the whole block —
  line 1 included — jumps up by ε.
- Steady state (pinned) then rests every top line at `y = padding − ε`. Line 1,
  seen only pre-overflow, was at `y = padding`, i.e. exactly ε lower. ε **is**
  the amount line N overflows.

`maxLines` does **not** fix this — it only caps retained DOM lines before
trimming (~L443); it never touches the top-aligned → bottom-pinned transition.

### Verified fix (line-height snap) — was applied, tested, then reverted
Insert right after the `linesDiv.style.cssText = [ … ].join(';')` block
(currently ~L482), before `let lastCount = 0;`:

```js
  // Snap the line-height so a whole number of lines exactly fills the viewport.
  // Then overflow is always a whole line and the top line never moves. Skipped
  // in multi-language mode (blocks carry margins; rows aren't uniform height).
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
    const avail = container.clientHeight - 2 * pad;
    if (avail <= 0) return;
    const n = Math.max(1, Math.round(avail / naturalLineHeight));
    linesDiv.style.lineHeight = (avail / n) + 'px';
  }

  snapLinesToViewport();

  window.addEventListener('resize', function() {
    snapLinesToViewport();
    if (pinnedToBottom) scrollToBottom();
  });
```

**Verification result** (real headed + headless Chrome, `display=line`, viewport
800×575, fontSize 48, lineSpacing 1.4, padding 20 → natural lh 67.2, avail 535):
- Pre-fix: top line y = 20px at 7 lines → 17px at 8 lines (`scrollTop` 3px) = **−3px shift**.
- Fixed (snapped lh 66.875px): top line y = 20px at both 7 and 8 lines, `scrollTop` 0 = **0px shift**.

Snap answers to design questions:
- **Always a whole number of lines**, at any font size: `n = round(avail / (fontSize×lineSpacing))`.
- Bigger font → larger natural lh → smaller `n` (fewer lines), but always an exact
  integer count filling the viewport, recomputed on resize. Never a partial line.

## Issue 2 — descenders (y, g, p, j) leave residue after scrolling up

### Symptom (appears only *after* the Issue-1 fix is applied)
Tall-descender glyphs leave faint leftover pixels ("residue") after their line
scrolls up and off the top.

### What was verified vs. not
- Fed descender-heavy lines through many scroll cycles and diffed each scrolled
  frame against a forced full-repaint of the same frame (fade/opacity
  transitions disabled so they couldn't pollute the diff), in **both headless
  and headed Chrome**: **0 differing pixels** every time.
- ⇒ The **layout is correct** (descenders positioned/clipped right — not a
  geometry bug). This is an **on-screen GPU-compositor paint artifact**.
  `page.screenshot` re-rasterizes, so it paints clean and **cannot reproduce the
  residue from a Puppeteer harness**. Needs verification on a real display.

### Leading hypothesis (not confirmed — could not reproduce)
Two things combine:
1. The snapped line-height is a **fractional pixel** value (e.g. 66.875px), so
   lines and each auto-scroll step sit off the device-pixel grid. Chrome's
   accelerated "blit" scroll shifts existing pixels by whole pixels and repaints
   only newly exposed rows — a fractional shift can leave anti-aliased fragments
   of ink that overflows the line box (descenders) at the repaint seam.
2. The Issue-1 fix removed the ~3px overflow "cushion" that used to put the top
   clip edge inside the empty gap between lines. Exact-line-boundary scrolling
   now puts the seam right through the descenders of the line leaving the top —
   which is why it only shows up *after* the fix.

Independent thing to rule out: **is `bgColor` transparent** (OBS overlay)?
Blit-scrolling over transparency is a separate classic cause of scroll trails and
would explain descender residue on its own. **Check this first next session.**

Increasing font size is **not** a reliable fix — it just changes `n` and the
leftover fraction unpredictably, and costs lines on screen.

### Proposed fix to try (unverified — verify on a real screen)
Snap line-height to a **whole pixel** and shrink the container to an exact
multiple, so the top line still never moves *and* lines + scroll steps land on
the device-pixel grid:

```js
  const n  = Math.max(1, Math.round(avail / naturalLineHeight));
  const lh = Math.floor(avail / n);                    // integer px, grid-aligned
  linesDiv.style.lineHeight = lh + 'px';
  container.style.height = (n * lh + 2 * pad) + 'px';  // exact multiple → whole-px scroll
```
Notes / gotchas for this variant:
- Base `avail` on the **viewport** (`document.documentElement.clientHeight − 2·pad`),
  **not** `container.clientHeight` — once you set an explicit `container.height`,
  `clientHeight` no longer equals the viewport, so a resize recompute would drift.
- Cost: a `< one line` (≈7px in the 8-line case) gap at the very bottom edge —
  the leftover that no longer stretches the lines. Invisible on full-screen / OBS.
- This keeps the Issue-1 no-shift guarantee. Whether it kills the residue is the
  open question — **must be checked on the real display**, since screenshots can't
  show it.

If integer-px alignment does **not** kill it, next candidates: opaque background
(if currently transparent), or forcing a full repaint on each scroll to defeat
the blit optimization (costs some smoothness).

## How to re-run the geometry test
Recipe + gotchas live in the `caption-viewer-browser-test-recipe` memory. For
this specific work the harness was: a Python feeder (`import main;
main.start_caption_server(port)`, push phrases via
`main._update_web_state("translation", lang, text)`, read JSON commands from
stdin) driven by a `puppeteer-core` + system-Chrome script that sets an exact
viewport and measures `document.querySelector('#lines .line-item')`'s
`getBoundingClientRect().top` and `container.scrollTop` before vs. after the Nth
line fills the screen. Run the feeder with the project venv
(`venv/bin/python` — needs `sounddevice`). The residue test additionally disables
`*{animation/transition}` and forces `.line-item{opacity:1}` before diffing, so
the fade transitions don't swamp the diff — but note the residue itself will read
as clean under Puppeteer regardless (see above).
