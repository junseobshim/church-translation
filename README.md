# Live Church Sermon Translation

_Last updated: September 9, 2026_

Real-time sermon translation using [Soniox](https://soniox.com/) real-time STT and [Claude](https://anthropic.com/) for translation, with a built-in web display for ProPresenter or any browser. Supports Korean, English, and Spanish — in any source/target combination, including multilingual (ko+en+es) sermons. Each translation target runs on its own parallel worker, so one Korean phrase can be translated into English and Spanish simultaneously on separate URLs.

## Prerequisites

- macOS with [Homebrew](https://brew.sh/)
- A [Soniox API key](https://soniox.com/) (real-time speech-to-text)
- An [Anthropic API key](https://console.anthropic.com/) (Claude translation)
- An audio input device (e.g. USB interface from church soundboard)



## Setup

```bash
# Install dependencies (skip any you already have)
brew install python git portaudio

# Clone the repo
git clone https://github.com/junseobshim/church-translation.git
cd church-translation

# Create a virtual environment and install Python packages
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure API keys
cp .env.example .env   # then edit .env and fill in SONIOX_API_KEY and ANTHROPIC_API_KEY
```



## Running

The recommended way to run this is via the control panel — see **Application** below. For direct CLI usage (`python main.py` with flags), see [CLI.md](CLI.md).

### Application

The recommended way to run this is via the included Automator app (`launcher.sh`), which:

1. Clears any stale Cloudflare tunnel left behind by a previous ungraceful shutdown, then starts `control_server.py` (the volunteer control panel) in the background
2. Opens `http://localhost:9090` in Chrome
3. Cleanly stops the translation session **and its Cloudflare tunnel** on shutdown — whether the volunteer clicks **Stop & Close Server**, closes the browser tab, or quits the browser entirely

To set it up:

1. Open Automator → New Document → **Application**
2. Add a **Run Shell Script** action (Shell: `/bin/bash`, Pass input: as arguments)
3. Paste the contents of `launcher.sh`
4. Save as an `.app` and pin it to the Dock

> **Maintenance note:** the `.app` embeds a *copy* of `launcher.sh` (inside `Contents/document.wflow`), and it is gitignored so each machine keeps its own. If you edit `launcher.sh`, rebuild the app from the new script — or update the embedded copy and re-sign the bundle — otherwise the running app keeps using the old version.

From the control panel at `http://localhost:9090`, volunteers can select the audio device, source/target languages, optionally upload a sermon outline, and start/stop the translation session.

> **Logs:** When launched via the `.app`, `launcher.sh` sends all output to **`/tmp/rc_translation.<username>.log`** (per-user, so two macOS accounts on the same Mac don't collide) — both the control server and the translation session (`main.py`) write there. Note two things: the file is **truncated on every launch** (each run overwrites the last), and `/tmp` is cleared on reboot. So if a session fails (e.g. a "Failed to fetch" error when clicking Start), copy the log *before* relaunching: `cp /tmp/rc_translation.$USER.log ~/Desktop/`. When you instead run `control_server.py` or `main.py` by hand in a terminal, output goes to that terminal, not the file.

## Web Display

Open in any browser or ProPresenter Web Fill:


| URL                                                                                                                                             | What it shows                                                                                                                |
| ----------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `http://localhost:8080/`                                                                                                                        | All transcription lines, regardless of detected language (Korean, English, Spanish as spoken). No query params needed.       |
| `http://localhost:8080/?mode=translation`                                                                                                       | Translations in the default target (the first `--target`).                                                                   |
| `http://localhost:8080/?mode=translation&lang=en`                                                                                               | English translations only                                                                                                    |
| `http://localhost:8080/?mode=translation&lang=ko`                                                                                               | Korean translations only                                                                                                     |
| `http://localhost:8080/?mode=translation&lang=es`                                                                                               | Spanish translations only                                                                                                    |
| `http://localhost:8080/?display=paragraph`                                                                                                      | Paragraph style (for ProPresenter)                                                                                           |
| `http://localhost:8080/?mode=translation&lang=en&display=paragraph`                                                                             | English translations, paragraph style                                                                                        |
| `http://localhost:8080/?mode=translation&lang=en&display=paragraph&fontSize=96&fontWeight=500&lineSpacing=1.3&bgColor=transparent&hideStatus=1` | English translations default for RCC Sanctuary TV display (ProPresenter web fill — transparent overlay, no status indicator) |
| `http://localhost:8080/?mode=transcription&lang=ko`                                                                                             | Only Korean transcription segments (explicit filter on the transcription stream)                                             |
| `http://localhost:8080/?mode=translation&slot=1&display=paragraph&bgColor=transparent&hideStatus=1`                                             | Slot mode, box 1 — see **Slot mode** below. `slot=2` for the second box (trilingual services only).                          |




### Scroll-back

Any caption viewer supports scrolling up to read previous captions during a live service. Scrolling up detaches the view from auto-follow; a **Back to Live** button appears and snaps back to the current caption when clicked. Caption history is preserved for the last 3 minutes by default (configurable via `?historyMinutes=`), and old lines age out of the DOM automatically — but only while pinned to the live edge, so a viewer scrolled back to read history never has content pruned out from under them.

### Slot mode (experimental)

A trilingual service has three languages, but a projection screen only has room for two caption boxes. `?slot=1` and `?slot=2` fill that gap: each box shows one of the languages that is **not** currently being spoken, so whichever language the speaker switches into, the others are always on screen.

Point one ProPresenter web-fill text box at each. The control panel lists the slot links after Start whenever the targets **mirror** what can be spoken — every language the source can produce is also a target. That is the condition the mode needs: a phrase in a language nobody is translating into has no line to route, so the boxes would blank out or swap languages mid-service.

| Source              | Mirror targets | Slot links offered           |
| ------------------- | -------------- | ---------------------------- |
| Multilingual        | ko + en + es   | Slot 1 and Slot 2            |
| Korean (+ English)  | ko + en        | Slot 1 only (alternates)     |
| Spanish (+ English) | es + en        | Slot 1 only (alternates)     |
| English             | —              | none (English can't target English) |

With two languages in play a phrase leaves only one to display, so the bilingual sources get a single box that alternates: speaking Korean it shows English, speaking English it shows Korean. With three, both boxes fill.

Routing is decided per caption line rather than by tracking a "current speaker language", so the boxes can never disagree or lag each other. In the trilingual case, with the default priority order `ko,en,es`, each box gets a home language and English backfills whichever box's home language is being spoken:

| Language being spoken | Slot 1      | Slot 2      |
| --------------------- | ----------- | ----------- |
| English               | Korean      | Spanish     |
| Korean                | **English** | Spanish     |
| Spanish               | Korean      | **English** |

So only one box ever changes at a time, and only while its own home language is being spoken. In paragraph display the viewer forces a line break wherever the language changes, so a switch always reads as a new line instead of flowing into the previous sentence.

Two things to know before relying on it:

- **Mixed-language phrases.** A phrase is routed by the language it is *mostly* in. If the speaker code-switches inside a single phrase — an English sentence with a Korean quotation in it — that phrase counts as English throughout, so the embedded Korean is not separately surfaced to English readers. Switches *between* phrases, the normal case, are handled exactly.
- **Non-mirroring sessions.** The control panel won't offer slot links unless the targets mirror the source, but a hand-typed `?slot=` URL still resolves against whatever targets are running. During a Korean → English service (English is spoken but not targeted) slot 1 shows English while Korean is spoken and goes blank during English asides. It degrades rather than showing something wrong, but use a fixed `?lang=` box there instead.

### Query Parameters


| Param         | Default                                                                 | Description                                                                                                                                                                           |
| ------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mode`        | `transcription`                                                         | `transcription` or `translation`                                                                                                                                                      |
| `lang`        | first `--target` for translation mode; no filter for transcription mode | ISO 639-1 language filter. In transcription mode, omitting `lang` shows all languages as spoken; in translation mode it defaults to the first `--target`. Explicit value always wins. |
| `langs`       | —                                                                       | Legacy multi-language column view (e.g. `?langs=ko,en,es`). Shows each language in its own column in fixed order (ko → en → es). The View dropdown is disabled when this param is active. |
| `display`     | `line`                                                                  | `line` (block divs) or `paragraph` (inline spans)                                                                                                                                     |
| `fontSize`    | `48`                                                                    | Font size in px                                                                                                                                                                       |
| `fontFamily`  | `system-ui, sans-serif`                                                 | CSS font stack                                                                                                                                                                        |
| `googleFont`  | —                                                                       | Google Fonts name (auto-loaded)                                                                                                                                                       |
| `fontWeight`  | `normal`                                                                | CSS font weight                                                                                                                                                                       |
| `color`       | `white`                                                                 | Text color                                                                                                                                                                            |
| `lineSpacing` | `1.4`                                                                   | CSS line-height                                                                                                                                                                       |
| `textAlign`   | `left`                                                                  | CSS text-align                                                                                                                                                                        |
| `textShadow`  | `none`                                                                  | CSS text-shadow                                                                                                                                                                       |
| `bgColor`     | `#000`                                                                  | Background color. Pass `bgColor=transparent` for ProPresenter web fill.                                                                                                               |
| `hideStatus`  | `0`                                                                     | Set to `1` to suppress the bottom-right "Waiting for transcription…" connection indicator. Use for ProPresenter web fill so the indicator never paints on the projection.             |
| `padding`     | `20`                                                                    | Container padding in px                                                                                                                                                               |
| `maxLines`    | `0` (unlimited)                                                         | Max lines displayed (hard cap 200)                                                                                                                                                    |
| `historyMinutes` | `3`                                                                  | How many minutes of caption history to preserve for scroll-back. Minimum 1. Old lines age out automatically while pinned to the live edge.                                            |
| `slot`        | —                                                                       | `1` or `2`. Slot mode (see above): shows a language not currently being spoken. `slot=2` only fills on a trilingual service. Overrides `mode`/`lang`, and disables the View dropdown.  |
| `priority`    | `ko,en,es`                                                              | Slot priority order — all three codes, comma-separated. Slot 1 takes the first language left after removing the one being spoken, slot 2 the second.                                  |




## Cloudflare Tunnel (Internet Access)

To make the web display accessible over the internet (e.g. at `live.rctranslation.org`):

```bash
# One-time setup (see below for steps regarding tunnel credentials)
brew install cloudflared
cloudflared tunnel login
```

Copy the .json file (`~/.cloudflared/` on an existing installation) associated with the church-live tunnel onto the new device.

Alternatively, regenerate the credentials through the Cloudflare dashboard (Networking -> Tunnels -> Rotate token).

The tunnel starts automatically when you run the script — `--tunnel church-live` is the default. Pass `--no-tunnel` to skip it for local-only work:

```bash
python main.py                        # live tunnel, runs automatically
python main.py --tunnel church-testing  # testing mirror (see below)
python main.py --no-tunnel            # localhost only
```

In the control panel, the same choice is a dropdown under **Advanced options → Cloudflare tunnel**, listing the tunnels this device holds credentials for plus a "No tunnel" option. The phone links shown after Start follow the selected tunnel's hostname.

### Testing tunnel

`church-testing` → `testing.rctranslation.org` is a full mirror of the live stack (own tunnel, own Worker), for exercising Cloudflare and the public internet path without any risk of interfering with `live.rctranslation.org`. Use it for any rehearsal or debugging that would otherwise be done on live.

The name→hostname mapping for both lives in [`tunnels.json`](tunnels.json), which the control panel, `main.py` and the control server all read. Add an entry there whenever you create a new tunnel, or the panel won't know what URL it serves. The **first** entry is the panel's default selection, so keep `church-live` at the top.

Viewers can access:

- `https://live.rctranslation.org/` — all transcription lines, regardless of language, with a solid black background (default)
- `https://live.rctranslation.org/?mode=translation&lang=en` — English translations
- `https://live.rctranslation.org/?mode=translation&lang=ko` — Korean translations
- `https://live.rctranslation.org/?mode=translation&lang=es` — Spanish translations



### Waiting page

When the tunnel has no origin (i.e. no device is running `main.py`), visitors to `live.rctranslation.org` see Cloudflare's default 530 error. To replace that with a branded "Waiting for transcription…" page that auto-refreshes into captions when the tunnel comes back online, deploy the Cloudflare Worker in `worker/`. See [worker/README.md](worker/README.md) for the one-time deploy.

### Troubleshooting: stale tunnels

All devices share one named tunnel per environment (`church-live` for live, `church-testing` for testing). Cloudflare treats multiple running `cloudflared` instances of the *same* tunnel as replicas and load-balances across all of them, with **no awareness of which origin is actually serving captions**. So if a device leaves a `cloudflared` running after its caption server is gone, viewers of `live.rctranslation.org` hit that dead origin for a share of requests — the classic "captions only show up about half the time" symptom.

The control panel now prevents this on its own: it tears the tunnel down on every shutdown path, and clears a stale one on launch (see **Application** above). If you still suspect a leftover tunnel:

```bash
pgrep -fa "cloudflared tunnel run"   # list tunnels running on THIS device
pkill -f "cloudflared tunnel run"    # kill them (all tunnels, live and testing)
```

Because a test session runs a *different* named tunnel, it can never steal live traffic — that isolation is the main reason the testing environment exists.

This is **per-device** — it cannot clear a stale tunnel on a *different* machine. If another device is holding the shared tunnel, that machine must be cleaned (relaunch its control panel, which self-heals, or run `pkill` there). Making one device authoritative regardless of the others is a larger change (design sketches are in `docs/multi-device-streaming.md`).

## CLI Options

See [CLI.md](CLI.md) for the full flag reference.




## Translation pipeline

Three mechanisms sit between a finalized phrase and a caption on screen. Two of them compose cleanly; the third collides with one of the others in exactly one place, which is handled explicitly.

**1. Coalescing** — every target language runs its own worker with its own queue. A worker blocks for one phrase, then drains whatever else has piled up into a single API call. Phrases arrive at roughly the speed of one translation round-trip, so translating strictly one-per-call lets the queue — and the on-screen lag — grow without bound whenever the API is briefly slower than speech. Coalescing caps the lag at one in-flight call plus one. When the worker is keeping up the queue is empty and every batch is a single phrase, which is the common case.

**2. `[SKIP]`** — Soniox finalizes on pauses, so a phrase can arrive as a fragment with no predicate; Korean is verb-final, which makes this routine. Translating such a fragment alone yields nonsense or an invented completion, so the prompt tells the model to answer exactly `[SKIP]`. The worker stashes the text in `pending_text` and prepends it to the next call, so the fragment is translated together with the speech that completes it. A failed API call uses the same buffer for the same reason — the text rides along with the next batch instead of being dropped.

**3. Splitting** — slot mode only. Every output line carries one `src` label, the language it was spoken in, and `?slot=` routes on it. If a batch spans a language change, that single label describes only part of the content, so splitting breaks the backlog at language boundaries — one call per language *run* — keeping each line's label truthful. It costs an extra round trip per boundary inside a backlog, so `slot_mode_available()` turns it on only when the targets mirror what the source can speak, which is exactly when a viewer could be in slot mode. Every other session, including the usual `ko → en`, never splits.

### Where they conflict

Splitting and `[SKIP]` want opposite things at a language boundary:

- Splitting isolates the trailing fragment before a switch — and an isolated fragment is precisely what the prompt tells the model to `[SKIP]`.
- `[SKIP]` then prepends that fragment to the next call, which is in the other language. `pending_text` is spliced in *after* the batch is assembled, so the split never sees it and cannot prevent the re-merge.

The result is one output line covering two spoken languages under one `src`. That cannot be repaired downstream: slot mode has N−1 boxes for N languages, on the premise that the language being spoken is heard rather than read. A merged line breaks the premise — Korean listeners need the English part, English listeners need the Korean part, Spanish listeners need both — so one target's line is dropped and its readers lose that fragment entirely.

### How it is resolved

In slot mode only, a pending fragment about to merge into a batch of a different language is translated on its own first (`TranslationWorker._flush_pending`), with a directive in the **user message** — not the cached system prompt — telling the model not to answer `[SKIP]`. The fragment is about to stop being completable, so deferring is no longer an option. Re-asking without that directive would skip again, since neither the input nor the rolling context has changed since the call that skipped.

The check sits where `pending_text` is *consumed* rather than where it is filled, so it covers the API-error path as well as `[SKIP]`. If the forced call skips anyway, or fails, pending is left alone and the ordinary merge happens — degraded, not broken.

The cost is one extra call, and only when slot mode, a pending fragment, and a language change all coincide. Every other case — including every non-slot session — follows exactly the path it did before.

### The concision clause

Added 2026-09-09, and **isolated so it can be removed cleanly** if it proves to cost translation quality. It is one constant, `CONCISION_CLAUSE` in `main.py`, inserted into every system prompt (all sources, all targets, slot mode or not) directly after the "Output ONLY the translation" line:

> Display space is limited: prefer the shortest rendering that preserves the full meaning and context. Never trade accuracy, context or literalness for brevity.

The intent is a **tie-breaker only**. Accuracy, context-awareness and literalness outrank brevity; brevity decides only among renderings that are already equally faithful. It is phrased as a property of the single output rather than a choice between two, since the model only ever emits one. The second sentence exists to keep the first from being read as a licence to compress, which is the failure mode to watch for in testing — clipped clauses, dropped qualifiers, names or scripture references shortened away.

To remove it: delete the `CONCISION_CLAUSE` constant and the single `f"{CONCISION_CLAUSE}"` line in `build_prompt`. Nothing else references it. Costs ~30 tokens per prompt.

### Knobs, if this needs retuning

| Mechanism | Where | Notes |
| --------- | ----- | ----- |
| Coalescing | always on, no switch | drains whatever is queued at the moment the worker frees up |
| Splitting | `slot_mode_available()` in `main.py` | must stay in step with `SPOKEN_LANGS` in `control.html`, which gates the slot links |
| `[SKIP]` | prompt text in `build_prompt` | dropping it entirely would end the conflict outright, at the cost of nonsense translations on fragments |
| Concision | `CONCISION_CLAUSE` in `main.py` | one constant plus one line in `build_prompt`; see above |
| Forced flush | `TranslationWorker._flush_pending` | gated on `split_on_lang_change`; falls through to the old merge if the forced call still skips |

## Architecture

The codebase splits into a shared shell plus per-backend modules:

- `main.py` — shared infrastructure: audio capture, web caption server, Cloudflare tunnel (torn down on `SIGINT`/`SIGTERM` so it never outlives the session), prompt-building scaffolding, the LLM-agnostic `TranslationWorker` (queue/`[SKIP]`/rolling context), orchestration, and CLI. It loads the requested transcription and translation modules lazily via `importlib`, so a deployment using only e.g. `azure` + `gemini` backends would not pull in `websockets` or `anthropic`.
- `transcribe_soniox.py` — Soniox transcription backend: WebSocket session, audio pump, recv/gating loop, term lists, and the `[Transcription]` print. Imports `websockets`.
- `translate_claude.py` — Claude translation backend: per-target system prompt, cache warmup (which measures what was actually cached rather than predicting it from a token threshold), keepalive thread, and the `messages.create` translation call. Imports `anthropic`.
- `control_server.py` — Volunteer control panel server (`http://localhost:9090`). Serves `control.html`, manages the `main.py` subprocess (stopping it — and its Cloudflare tunnel — cleanly), and shuts itself down when the browser tab closes.
- `control.html` — Volunteer UI: device selection, source/target language picker, optional sermon outline upload (`.txt` or in-browser `.docx` conversion), start/stop controls, and live caption viewer links.
- `launcher.sh` — Automator shell script: reaps any stale Cloudflare tunnel, launches `control_server.py`, opens Chrome, and waits — cleaning up the servers and tunnel when the panel shuts down.

Alternative backends drop in alongside without modifying the main file beyond extending the `--transcriber` / `--translator` choice lists. They must implement these contracts:

- `Transcriber(source, api_key)` with `run(device_index, on_phrase, stop_event)` — blocking; calls `on_phrase(text)` once per finalized phrase and prints `[Transcription] {text}` itself.
- `Backend` with `from_outline(client, source, target, outline, model, forceable=False)` classmethod plus `warmup()`, `translate(context, latest, force=False)`, `mark_activity()`, and `start_keepalive(stop_event)` instance methods. Module-level `make_client(api_key)` factory and `DEFAULT_MODEL` constant.

`websockets` is Soniox-only and `anthropic` is Claude-only at the import level — both are still required for the default Soniox + Claude path. Once optional backends ship, `requirements.txt` may split into extras keyed by backend.

## Viewer settings panel

The web display at `http://localhost:8080/` also ships a gear-icon settings panel (top right) for visitors who don't have a preset query-param link: a **View** dropdown (transcription or a specific target language), font family, font size, and a light/dark theme toggle. Choices persist per-browser via `localStorage` and don't affect the query-param links documented above, which still work unchanged and always take precedence — this is for the plain `http://localhost:8080/` / `live.rctranslation.org/` links people open directly. The panel and gear icon are hidden automatically whenever `hideStatus=1` is set, so ProPresenter web-fill links never show it.

## License

Unlicense