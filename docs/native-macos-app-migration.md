# Native macOS App Migration Plan

Migrating the control panel from the Automator-stub + `launcher.sh` + Chrome + `control_server.py` stack
to a single native SwiftUI app that bundles its own Python runtime and installs from one file.

Decisions already made:
- **UI:** full SwiftUI rebuild of `control.html` (no web view, no Electron).
- **Backend:** the app **replaces `control_server.py` entirely** — Swift spawns and manages `main.py`
  directly. `main.py`, the caption server (port 8080), and the cloudflared tunnel stay in Python, unchanged.
- **Signing:** ad-hoc signed during development/testing (manual Gatekeeper bypass), Apple Developer
  Program (US$99/yr) enrollment before volunteer rollout.
- **Deployment target:** macOS 14 (Sonoma).

---

## 1. Target architecture

```
Church Translation.app/
└── Contents/
    ├── Info.plist                  bundle ID, version, NSMicrophoneUsageDescription
    ├── MacOS/
    │   └── Church Translation      the Swift executable (SwiftUI app)
    ├── Frameworks/
    │   └── Sparkle.framework       auto-updates (Phase 6)
    ├── Resources/
    │   ├── python/                 self-contained CPython (python-build-standalone)
    │   │   ├── bin/python3
    │   │   └── lib/python3.14/site-packages/   anthropic, websockets, sounddevice, dotenv…
    │   ├── app/                    main.py, transcribe_soniox.py, translate_claude.py
    │   └── bin/
    │       └── cloudflared         bundled binary — Homebrew no longer required
    └── _CodeSignature/
```

What each part does:

| Today | After migration |
|---|---|
| Automator stub runs `launcher.sh` | Gone — the .app launches directly |
| Chrome renders `control.html` | SwiftUI views inside the app window |
| `control_server.py` on :9090 spawns `main.py`, heartbeat/goodbye/watchdog detects browser close | Gone — Swift `Process` API spawns `main.py`; quitting the app *is* the shutdown signal |
| `venv/` created per machine via README steps | CPython runtime baked into the bundle at build time |
| `cloudflared` from Homebrew | Bundled in `Resources/bin`, signed with the app |
| `.env` in the repo folder | First-run setup window → API keys in the **Keychain**, exported as env vars to the subprocess |
| Logs truncated at `/tmp/rc_translation.$USER.log` | `~/Library/Logs/Church Translation/` — rotating, survives reboot, per-user by construction |
| Code updates = `git pull` + rebuild Automator app on each machine | Sparkle auto-update: volunteers click "Install Update" |

**Unchanged:** the caption web server on :8080 (ProPresenter/phones consume it), the `church-live`
named tunnel, per-machine tunnel credentials in `~/.cloudflared`, the Cloudflare `worker/`.

---

## 2. The Swift app — responsibilities and how each maps

You'll work in **Xcode** (free, Mac App Store). One project, one target, Swift + SwiftUI.
Rough shape: an `@Observable` `SessionController` class owning the subprocess state, and SwiftUI
views bound to it. Total Swift surface is small — this is a control panel, not a big app.

### 2.1 Process management (replaces control_server.py)

- `Process` (a.k.a. NSTask) launches `Resources/python/bin/python3 Resources/app/main.py --source … --target … --device … --port 8080 [--outline …] [--no-tunnel]`.
  - Set `currentDirectoryURL` to `Resources/app/`, environment to include the Keychain-sourced
    `SONIOX_API_KEY` / `ANTHROPIC_API_KEY` (main.py's `dotenv` load simply finds nothing and env vars win — verify `load_dotenv()` doesn't override; it doesn't by default).
- **Stop** = send SIGINT (`proc.interrupt()`), wait ≤5s, then `terminate()` — the exact logic of
  `_handle_stop()` today.
- **Log streaming:** attach `Pipe`s to stdout/stderr, read via `FileHandle.readabilityHandler` (or
  `AsyncBytes`), append to an in-app log view *and* a file in `~/Library/Logs/Church Translation/`.
  This kills the "copy the log before relaunching" problem — keep the last N session logs.
- **Crash detection:** `terminationHandler` fires if main.py dies unexpectedly → flip UI to error
  state, show the tail of the log. (Today a dead session just looks stuck until someone checks.)
- **App quit:** implement `applicationShouldTerminate` → if a session is running, gracefully stop it
  (and optionally confirm with the volunteer), *then* allow termination. This one delegate method
  replaces the entire heartbeat / goodbye-beacon / watchdog machinery — ~120 lines of the hardest-won
  code in control_server.py simply ceases to exist.
- **Startup self-heal (port from launcher.sh):** on launch, if port 8080 is occupied or an orphaned
  `cloudflared tunnel run …church-live` exists (previous force-quit/power loss), kill them — same
  `lsof`/`pkill` logic, runnable from Swift via `Process` or natively with `libproc`. Keep the guard:
  only when no session of ours is live.

### 2.2 Audio device picker — a subtle trap

`--device` takes a **PortAudio index**. Native CoreAudio/AVFoundation enumeration gives different
identifiers, and matching by name is fragile (duplicate names, Unicode variants). **Don't enumerate
natively.** Instead run a one-shot with the bundled interpreter:

```
python3 -c "import sounddevice, json; print(json.dumps([
  {'index': i, 'name': d['name']}
  for i, d in enumerate(sounddevice.query_devices()) if d['max_input_channels'] > 0]))"
```

— same indices main.py will see, guaranteed. Refresh on window focus / a refresh button.
(Longer term, consider changing main.py to accept a device *name* and resolve it itself; then the
picker and CLI both get stable semantics across replug/reboot.)

### 2.3 UI parity checklist (from control.html)

- Source language picker (`ko / en / es / multi`) and target checkboxes, replicating the validation
  rules: `multi` fixes targets to `ko,en,es`; targets exclude the source (except the allowed
  same-language passthrough); `en`/`es` sources require explicit targets.
- Outline: file picker for `.txt` (read as UTF-8) and `.docx`. The browser currently converts .docx
  client-side; natively, do it with a one-shot bundled-Python call using `python-docx` (add to
  requirements at build time), or paste-into-textbox. Swift-side .docx parsing (unzip + XML) is
  possible but not worth it.
- Start / Stop buttons with running-state, PID, elapsed time.
- Caption preview: poll `http://localhost:8080/api/latest` (the same JSON control.html proxies
  today) and render in a SwiftUI list. No web view needed.
- Links/QRs for the display URLs (localhost + tunnel), copy buttons.
- Log viewer pane (live tail).
- Settings window: API keys (Keychain), ports, tunnel on/off, default language config.
- Menu bar + Dock: native "Quit" does the right thing; optionally a menu-bar extra showing
  session status.

### 2.4 First-run migration

On first launch: if `~/Documents/church-translation/.env` exists, offer to import the keys into the
Keychain. Tunnel credentials in `~/.cloudflared` are already per-machine and keep working untouched.

---

## 3. Embedding Python — the biggest novel piece

### 3.1 Runtime choice: python-build-standalone

Use [python-build-standalone](https://github.com/astral-sh/python-build-standalone) (the
relocatable CPython builds that uv/Rye use). Unlike a venv — which hardcodes absolute paths in
`pyvenv.cfg` and symlinks the system interpreter — PBS builds are self-contained and relocatable:
exactly what an app bundle needs. Pick the `aarch64-apple-darwin` `install_only_stripped` build
matching your current 3.14.

**Architecture decision:** PBS ships separate arm64 and x86_64 builds, not universal2. If every
church/volunteer Mac is Apple Silicon (likely — check with `uname -m` / About This Mac on each),
ship arm64-only and keep life simple. If an Intel Mac must be supported: either lipo-merge the two
runtimes with a script, or build two DMGs (Sparkle appcasts can serve per-arch updates).

### 3.2 Build-time staging script

A `build/stage_python.sh` (run as an Xcode build phase or standalone) that:

1. Downloads + caches the pinned PBS tarball, unpacks to `Resources/python/`.
2. `Resources/python/bin/python3 -m pip install -r requirements.txt` (plus `python-docx`)
   into its own site-packages. The `sounddevice` wheel bundles `libportaudio.dylib` — **no Homebrew
   PortAudio needed** (verified in the current venv: `_sounddevice_data/portaudio-binaries/`).
3. Prunes dead weight: `pip`, `ensurepip`, `idlelib`, `tkinter`, `test(s)/`, `__pycache__`. The
   current venv is 67 MB before pruning; expect a ~100–150 MB runtime folder → a DMG well under
   100 MB compressed. Fine.
4. Copies `main.py`, `transcribe_soniox.py`, `translate_claude.py` into `Resources/app/`.
5. Downloads the pinned `cloudflared` release binary into `Resources/bin/`.

Pin every version (PBS URL + sha256, wheel versions via a lock/constraints file, cloudflared
release) so builds are reproducible.

**Change in main.py:** `_resolve_cloudflared()` should look at
`Path(sys.executable).parent.parent.parent / "bin" / "cloudflared"` (i.e. the bundled copy) first,
then fall back to PATH/Homebrew — a 3-line change that keeps the CLI workflow working from a repo
checkout too.

### 3.3 Codesigning the runtime — where everyone gets burned

Notarization requires **every Mach-O binary** in the bundle (each `.so`, each `.dylib`,
`python3`, `cloudflared`) to be individually signed with your identity, hardened runtime, and a
secure timestamp. `codesign --deep` does **not** reliably descend into Resources — the standard fix
is a post-build script that signs inside-out:

```bash
find "$APP/Contents/Resources" \( -name "*.so" -o -name "*.dylib" \) -exec \
  codesign --force --options runtime --timestamp --sign "$IDENTITY" {} \;
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  "$APP/Contents/Resources/python/bin/python3" \
  "$APP/Contents/Resources/bin/cloudflared"
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  --entitlements app.entitlements "$APP"
```

During the unsigned phase, the same script with `--sign -` (ad-hoc, no `--timestamp`) — the same
trick already used to re-sign the Automator bundle.

**Hardened-runtime entitlements** (needed once you sign for real; harmless to carry from day one):
- `com.apple.security.device.audio-input` — hardened runtime blocks microphone access without it
  (this is in addition to the Info.plist usage string).
- `com.apple.security.cs.allow-unsigned-executable-memory` — commonly required for cffi's
  closure trampolines (`sounddevice` depends on cffi). Test without it first; add if PortAudio
  callbacks crash. (Per Apple's hardened-runtime docs and the experience of py2app/Briefcase
  projects; verify empirically at Phase 5.)

Do **not** App-Sandbox the app. It spawns subprocesses, binds ports, runs cloudflared, and reads
`~/.cloudflared` — all painful-to-impossible under sandbox, and Developer ID distribution doesn't
require it (only the Mac App Store does, which this app shouldn't target).

### 3.4 Microphone permission (TCC)

Add `NSMicrophoneUsageDescription` to Info.plist ("Captures the soundboard feed for live
transcription."). The Python subprocess inherits the app's TCC identity, so macOS shows **one**
permission prompt attributed to "Church Translation" — cleaner than today's prompt attributed to
whatever Automator/Chrome context happened to be responsible. Note: when the signing identity
changes (ad-hoc → Developer ID), macOS treats it as a different app and re-prompts once. Keep the
**bundle ID stable from day one** (e.g. `com.rcchurch.translation`) so everything else carries over.

---

## 4. Packaging & install experience

### 4.1 During development (unsigned)

- Build on your Mac: runs fine locally (no quarantine on locally-built products).
- To test on another Mac/account: AirDrop/USB the zipped app. It arrives quarantined; since macOS
  Sequoia the right-click-Open bypass is gone — the tester launches once, gets blocked, then
  System Settings → Privacy & Security → **Open Anyway**. One time per machine per build identity.
  (Or `xattr -cr` the app from Terminal, as with the current launcher-sync flow.)

### 4.2 For rollout (Developer ID)

1. Enroll in the Apple Developer Program; in Xcode create **Developer ID Application** (and, if you
   choose .pkg, **Developer ID Installer**) certificates. Export and back up the private keys.
2. `xcodebuild archive` → export with Developer ID signing → run the runtime-signing script.
3. Notarize: `xcrun notarytool submit ChurchTranslation.zip --keychain-profile rc-notary --wait`
   then `xcrun stapler staple "Church Translation.app"`. First submission of a bundled-Python app
   sometimes gets rejected listing specific unsigned Mach-Os — fix the signing script and resubmit;
   after it passes once it stays boring.
4. **DMG (recommended):** `create-dmg` with the classic app + `/Applications` symlink layout;
   notarize/staple the DMG too. Familiar to Mac users, easy to build.
   **PKG (alternative):** `productbuild` — true one-click, installs straight to `/Applications`,
   sidesteps users running the app from Downloads. Switch to this if volunteers stumble on drag-install.
5. **App Translocation caveat:** a quarantined app launched from Downloads/DMG runs from a
   randomized read-only path. Everything still works (all paths are bundle-relative), but Sparkle
   can't self-update a translocated app — one more reason to ensure it lands in `/Applications`
   (the DMG symlink layout, or a pkg, both solve this).

---

## 5. Updates & releases

### 5.1 Sparkle 2

Add [Sparkle](https://sparkle-project.org/) via Swift Package Manager.

- One-time: `generate_keys` creates an EdDSA keypair (private key lands in your login Keychain —
  **back it up**; losing it strands every installed app on manual updates). Public key +
  `SUFeedURL` go in Info.plist.
- Per release: `generate_appcast` scans a folder of release DMGs, writes `appcast.xml` with
  EdDSA signatures (and binary-delta updates automatically).
- Host `appcast.xml` + DMGs on **GitHub Releases** (repo is already on GitHub); point `SUFeedURL`
  at a stable raw URL (a `gh-pages` branch or a fixed release asset URL).
- Runtime behavior: app checks the feed on launch (configurable), shows "Version X is available"
  with release notes, installs on relaunch. Updates are verified by EdDSA + code signature and
  don't go through the manual Gatekeeper dance.

Since the Python pipeline ships **inside** the bundle, every change to main.py/prompts/UI is just a
new app version — one update channel replaces today's per-machine `git pull` + Automator rebuild.

**Dev-mode escape hatch (recommended):** a hidden preference (e.g. ⌥-click Settings) that makes the
app run `main.py` from `~/Documents/church-translation` with the repo venv instead of the bundled
copy. You keep your current edit-run loop on your own machine; volunteers only ever see bundled code.
This also solves the "two sources of truth" drift during the transition.

### 5.2 Release checklist (script it as `release.sh` on day one)

1. Bump `CFBundleShortVersionString` (human version, e.g. 2.1.0) and `CFBundleVersion`
   (monotonic build number — Sparkle compares this).
2. Tag git (`v2.1.0`).
3. `xcodebuild archive` + export → sign runtime → notarize → staple.
4. `create-dmg` → notarize/staple DMG.
5. `generate_appcast` → upload DMG + appcast to GitHub Release.
6. Smoke-test the update path on one machine before Sunday.

Later, move steps 3–5 to GitHub Actions (macOS runner; cert + notary credentials in repo secrets).
Do it manually first — you want to understand the pipeline before automating it.

---

## 6. Phased implementation

**Phase 0 — Setup (a day):** Install Xcode; create the SwiftUI app project (macOS 14 target,
bundle ID fixed now); commit an `app/` subfolder (or separate repo — subfolder recommended, one
history). Skim Apple's SwiftUI tutorial enough to read code.

**Phase 1 — Walking skeleton (1–2 days, de-risks the scariest part first):** stage_python.sh
builds `Resources/python`; a window with one Start button runs `main.py --source ko --target en
--no-tunnel --device <hardcoded>` via `Process`, streaming output to a text view. **Success =
captions at localhost:8080, launched from a double-clicked .app on a machine with no venv, no
Homebrew.** If this works, everything else is normal app development.

**Phase 2 — UI parity (the bulk, ~1–2 weeks part-time):** §2.3 checklist. Port control.html's
validation logic; device picker via Python one-shot; outline handling; caption preview; log pane;
Settings + Keychain.

**Phase 3 — Lifecycle hardening (a few days):** graceful quit, crash surfacing, startup self-heal,
first-run .env import, second-macOS-account sanity check (port 9090 is gone; 8080 still collides
across simultaneous accounts exactly as today — acceptable, but now the error is visible in-app).

**Phase 4 — Unsigned packaging + pilot (a couple of days):** ad-hoc signing script, DMG, install on
one other Mac/account via the Open Anyway flow; run a real Sunday in parallel with the old
Automator app as fallback.

**Phase 5 — Developer ID (a few days, mostly waiting/learning):** enroll, certificates,
entitlements, notarization until it passes, stapled DMG.

**Phase 6 — Sparkle + release.sh (2–3 days):** keys, appcast on GitHub Releases, test an
end-to-end update on a volunteer machine.

**Phase 7 — Rollout & decommission:** install on all machines, import keys, confirm mic TCC
prompts, retire the Automator app + launcher.sh + control_server.py + control.html, update README.

---

## 7. Challenge summary (what will actually bite)

1. **Codesigning the embedded runtime** — every `.so`/dylib signed individually; `--deep` lies;
   notarization rejections name the stragglers. Solved by the inside-out signing script (§3.3).
2. **Gatekeeper during the unsigned phase** — Sequoia+ removed right-click-Open; testers use
   System Settings → Open Anyway or `xattr -cr`. Disappears at Phase 5.
3. **PortAudio device indices** — never enumerate devices natively; always via the bundled
   interpreter (§2.2).
4. **Hardened runtime vs. mic and cffi** — needs `audio-input` entitlement; possibly
   `allow-unsigned-executable-memory` for cffi. Test at Phase 5, not before.
5. **App Translocation** — ensure the app lives in /Applications (DMG symlink layout or pkg), or
   Sparkle updates stall.
6. **TCC re-prompt on identity change** — expected once at the ad-hoc → Developer ID switch; keep
   the bundle ID stable.
7. **Intel Macs** — PBS isn't universal2; confirm the fleet is Apple Silicon or plan lipo/dual-DMG.
8. **.docx outlines** — browser-side conversion goes away; use python-docx one-shot.
9. **Two sources of truth during transition** — bundled Python code vs. repo checkout; the dev-mode
   preference (§5.1) keeps your workflow while volunteers get bundled code.
10. **Learning curve** — Swift/SwiftUI/Xcode from zero. The app is small and the patterns
    (Process, Pipe, @Observable, Keychain) are all well-trodden; expect the first week to be
    slow and the rest normal.

## 8. Alternatives considered and rejected

- **py2app / Briefcase (BeeWare):** solve the *packaging* (bundle Python into an .app) but the UI
  stays Python (Toga/tkinter/web) — not native SwiftUI. Their signing/notarization docs are still
  useful prior art for §3.3.
- **WKWebView shell around control.html:** 90% code reuse and no Chrome, but the UI stays web —
  explicitly not the goal.
- **Tauri:** Rust + system webview — still a web UI, plus a new language.
- **Rewriting the pipeline in Swift:** sounddevice→Soniox→Claude could all be done natively
  (AVAudioEngine, URLSession WebSockets), but it's a rewrite of battle-tested code for little user
  benefit. Keep Python for the pipeline; revisit only if the bundled runtime proves painful.

## References

- Gatekeeper change in Sequoia (no more Control-click bypass): [Apple developer note](https://developer.apple.com/news/?id=saqachfa), [MacRumors summary](https://www.macrumors.com/2024/08/06/macos-sequoia-gatekeeper-security-change/)
- Relocatable CPython builds: [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
- Signing/notarizing bundled Python, prior art: [haim.dev walkthrough](https://haim.dev/posts/2020-08-08-python-macos-app), [Apple Developer Forums thread](https://developer.apple.com/forums/thread/744471), [Spyder's signing wiki](https://github.com/spyder-ide/spyder/wiki/Dev:-Codesigning-the-macOS-Standalone-Application)
- Auto-updates: [Sparkle project](https://sparkle-project.org/), [docs](https://sparkle-project.org/documentation/)
