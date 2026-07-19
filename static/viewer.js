(function () {
  "use strict";

  // ── Constants ────────────────────────────────────────────────────────────

  const STORAGE_KEY = "rctranslation.viewerSettings.v1";
  const SETTINGS_VERSION = 1;

  const LANG_NAMES = { ko: "Korean", en: "English", es: "Spanish" };
  const LANG_ORDER = ["ko", "en", "es"]; // fixed display order for multi-lang grouping

  const FONT_MIN = 16, FONT_MAX = 120, FONT_STEP = 4;

  // Theme presets: light/dark map onto the same bgColor/color/textShadow
  // levers the old query-param API used, just resolved from one stored
  // value instead of three independent params.
  const THEME_PRESETS = {
    dark: { bgColor: "#000", color: "white", textShadow: "none" },
    light: { bgColor: "#fff", color: "#111", textShadow: "none" },
  };

  const DEFAULT_SETTINGS = {
    version: SETTINGS_VERSION,
    view: "transcription", // "transcription" | ISO 639-1 target language code
    fontFamily: "system-ui, sans-serif",
    googleFont: null,
    fontSize: 48,
    theme: "dark",
  };

  const DOM_CAP = 200;
  const FAST_MS = 150;
  const MAX_MS = 1000;
  const GROWTH = 1.5;
  const GROUP_WINDOW_MS = 6000;

  // ── URL params ───────────────────────────────────────────────────────────
  // Session-only overrides, never written to localStorage. Precedence is
  // URL > stored settings > defaults, matching "explicit value always wins"
  // from the original query-param docs. This keeps existing ProPresenter /
  // web-fill links working unchanged while everyday visitors get the new
  // settings-panel + localStorage experience.

  const params = new URLSearchParams(window.location.search);
  const hideStatus = params.get("hideStatus") === "1";

  // Legacy multi-language overlay (?langs=ko,en,es): shows several languages
  // side by side in one caption block. Doesn't map onto a single "view"
  // choice, so when present it takes full control and the View dropdown is
  // disabled rather than force-fit into the new single-select model.
  const legacyLangsParam = params.get("langs");
  const legacyMultiLang = !!legacyLangsParam;
  const legacyLangs = legacyMultiLang
    ? legacyLangsParam.split(",").map((s) => s.trim()).filter(Boolean)
    : [];

  const legacyMode = params.get("mode");
  const legacyLang = params.get("lang");

  const HISTORY_MS =
    Math.max(1, parseInt(params.get("historyMinutes") || "3", 10)) * 60 * 1000;
  const maxLines = Math.min(
    params.get("maxLines") ? parseInt(params.get("maxLines"), 10) : 0,
    200
  );
  // display (line/paragraph) is URL-only, not exposed in the settings panel —
  // out of scope for the interactive page per the HLD; ProPresenter-style
  // deployments set it directly in the link.
  const display = params.get("display") || "line";

  // Remaining typography/layout knobs stay URL-only power-user overrides,
  // exactly matching the ticket's scoped control list (view, font family,
  // font size, theme — nothing else gets a panel control).
  const fontWeight = params.get("fontWeight") || "normal";
  const lineSpacing = params.get("lineSpacing") || "1.4";
  const textAlign = params.get("textAlign") || "left";
  const padding = params.get("padding") || "20";
  const rawColorOverride = params.get("color");
  const rawBgOverride = params.get("bgColor");
  const rawTextShadowOverride = params.get("textShadow");

  // ── Settings store ───────────────────────────────────────────────────────

  function loadSettings() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return Object.assign({}, DEFAULT_SETTINGS);
      const parsed = JSON.parse(raw);
      if (parsed.version !== SETTINGS_VERSION) return Object.assign({}, DEFAULT_SETTINGS);
      return Object.assign({}, DEFAULT_SETTINGS, parsed);
    } catch (e) {
      // Corrupt/inaccessible localStorage — treat as first visit.
      return Object.assign({}, DEFAULT_SETTINGS);
    }
  }

  function saveSettings() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    } catch (e) {
      // Private browsing / quota exceeded — settings just won't survive
      // a reload this session. Not worth surfacing to the visitor.
    }
  }

  const settings = loadSettings();

  // Legacy single lang/mode params seed the initial view only; they don't
  // get persisted unless the visitor subsequently touches the panel.
  if (!legacyMultiLang) {
    if (legacyLang) {
      settings.view = legacyLang;
    } else if (legacyMode === "transcription") {
      settings.view = "transcription";
    }
  }
  if (params.get("fontSize")) settings.fontSize = parseInt(params.get("fontSize"), 10);
  if (params.get("fontFamily")) settings.fontFamily = params.get("fontFamily");
  if (params.get("googleFont")) settings.googleFont = params.get("googleFont");

  // ── DOM refs ─────────────────────────────────────────────────────────────

  const container = document.getElementById("container");
  const linesDiv = document.getElementById("lines");
  const overlay = document.getElementById("waiting-overlay");
  const backToLiveBtn = document.getElementById("back-to-live");
  const settingsTrigger = document.getElementById("settings-trigger");
  const settingsPanel = document.getElementById("settings-panel");
  const settingsClose = document.getElementById("settings-close");
  const viewSelect = document.getElementById("view-select");
  const fontFamilySelect = document.getElementById("font-family-select");
  const fontSizeValue = document.getElementById("font-size-value");
  const fontDecreaseBtn = document.getElementById("font-decrease");
  const fontIncreaseBtn = document.getElementById("font-increase");
  const themeToggle = document.getElementById("theme-toggle");

  if (hideStatus) {
    settingsTrigger.classList.add("hidden");
    settingsPanel.classList.add("hidden");
    overlay.classList.add("force-hidden");
  }

  container.style.padding = padding + "px";

  // ── Google Font loading (ported) ────────────────────────────────────────

  function loadGoogleFont(name) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href =
      "https://fonts.googleapis.com/css2?family=" +
      encodeURIComponent(name) +
      "&display=swap";
    document.head.appendChild(link);
  }

  if (settings.googleFont) loadGoogleFont(settings.googleFont);

  // ── Apply settings to the DOM ────────────────────────────────────────────

  function applyTypography() {
    const resolvedFamily = settings.googleFont
      ? '"' + settings.googleFont.replace(/\+/g, " ") + '", ' + settings.fontFamily
      : settings.fontFamily;

    // The theme setting styles the caption VIEW only (background/text of the
    // actual transcription/translation stream) — not the settings panel or
    // gear button, which stay a fixed dark style on purpose (see the note
    // at the top of viewer.css). rawBgOverride/rawColorOverride/
    // rawTextShadowOverride are the raw ?bgColor=/?color=/?textShadow= URL
    // params, which still take precedence over the theme preset.
    const theme = THEME_PRESETS[settings.theme] || THEME_PRESETS.dark;
    const bgColor = rawBgOverride || theme.bgColor;
    const color = rawColorOverride || theme.color;
    const textShadow = rawTextShadowOverride || theme.textShadow;

    document.body.style.background = bgColor;
    document.body.dataset.theme = settings.theme;

    linesDiv.style.cssText = [
      "font-size:" + settings.fontSize + "px",
      "font-family:" + resolvedFamily,
      "font-weight:" + fontWeight,
      "color:" + color,
      "line-height:" + lineSpacing,
      "text-align:" + textAlign,
      "text-shadow:" + textShadow,
      "white-space:pre-wrap",
    ].join(";");
  }

  function reflectPanelState() {
    fontSizeValue.textContent = settings.fontSize + "px";
    fontFamilySelect.value = settings.fontFamily;
    if (!legacyMultiLang && viewSelect.querySelector('option[value="' + settings.view + '"]')) {
      viewSelect.value = settings.view;
    }
    themeToggle.querySelectorAll(".theme-toggle-option").forEach((el) => {
      el.classList.toggle("active", el.dataset.themeValue === settings.theme);
    });
  }

  // ── View (mode + language) resolution ───────────────────────────────────
  // settings.view is the single source of truth: "transcription" (no
  // filter) or a target language code. This is what used to be two
  // independent query params (mode + lang) collapsed into one control.

  let mode = "transcription";
  let langs = [];

  function applyView() {
    if (legacyMultiLang) {
      mode = "translation";
      langs = legacyLangs;
      return;
    }
    if (settings.view === "transcription") {
      mode = "transcription";
      langs = [];
    } else {
      mode = "translation";
      langs = [settings.view];
    }
  }

  function multiLangMode() {
    return langs.length > 1;
  }

  function getExpectedLangs() {
    return langs.length > 0 ? LANG_ORDER.filter((l) => langs.includes(l)) : LANG_ORDER;
  }

  function langAllowed(lang) {
    if (langs.length === 0) return true;
    return langs.includes(lang);
  }

  // ── /api/config: populate the View dropdown, validate the saved view ────

  function buildViewOptions(targets) {
    viewSelect.innerHTML = "";

    const transcriptionOpt = document.createElement("option");
    transcriptionOpt.value = "transcription";
    transcriptionOpt.textContent = "Transcription (all languages)";
    viewSelect.appendChild(transcriptionOpt);

    targets.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = LANG_NAMES[t] || t;
      viewSelect.appendChild(opt);
    });

    if (legacyMultiLang) {
      viewSelect.disabled = true;
      const opt = document.createElement("option");
      opt.value = "__legacy__";
      opt.textContent = "Multiple (set by URL)";
      viewSelect.appendChild(opt);
      viewSelect.value = "__legacy__";
    }
  }

  async function initConfig() {
    // Default to "no known targets" rather than "all languages" — offering
    // every language as if it were live would defeat the validation below
    // the instant /api/config is unreachable, which is exactly the failure
    // mode this check exists to catch. An unreachable config means we don't
    // know what's real, so the only safe option to offer is Transcription.
    let targets = [];
    let configReachable = false;
    try {
      const resp = await fetch("/api/config");
      if (!resp.ok) throw new Error("bad status " + resp.status);
      const config = await resp.json();
      targets = Array.isArray(config.targets) ? config.targets : [];
      configReachable = true;
    } catch (e) {
      // /api/config unreachable (network hiccup, server restarting). Retry
      // shortly rather than leaving the visitor stuck on a stale/invalid
      // view until their next reload.
      setTimeout(initConfig, 3000);
    }

    buildViewOptions(targets);

    // The one piece of required behavior: a saved (or URL-seeded) language
    // that isn't among the operator's current targets falls back to
    // transcription-with-no-filter, not a broken/empty translation stream.
    // Only enforced once we've actually confirmed the target list — an
    // unreachable config retries above instead of wrongly evicting a view
    // that might still be perfectly valid.
    if (configReachable && !legacyMultiLang &&
        settings.view !== "transcription" && !targets.includes(settings.view)) {
      settings.view = "transcription";
      saveSettings();
      applyView();
      resetRenderedCaptions();
    }

    reflectPanelState();
  }

  // ── Settings panel wiring ────────────────────────────────────────────────

  function openPanel() {
    settingsPanel.classList.remove("hidden");
  }
  function closePanel() {
    settingsPanel.classList.add("hidden");
  }
  function isPanelOpen() {
    return !settingsPanel.classList.contains("hidden");
  }

  settingsTrigger.addEventListener("click", openPanel);
  settingsClose.addEventListener("click", closePanel);

  // Dismiss on outside click. Uses the document-level click's bubble phase,
  // so a click that opens the panel (on settingsTrigger, whose own listener
  // above already ran) doesn't immediately close it again — e.target is
  // still inside settingsTrigger at that point. Same reasoning covers every
  // control inside the panel itself, which is why interacting with the view
  // dropdown, font size buttons, etc. never closes it prematurely.
  document.addEventListener("click", (e) => {
    if (!isPanelOpen()) return;
    if (settingsPanel.contains(e.target) || settingsTrigger.contains(e.target)) return;
    closePanel();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isPanelOpen()) closePanel();
  });

  viewSelect.addEventListener("change", () => {
    if (legacyMultiLang) return;
    settings.view = viewSelect.value;
    saveSettings();
    applyView();
    resetRenderedCaptions(); // re-hydrate history under the new filter
  });

  fontFamilySelect.addEventListener("change", () => {
    const opt = fontFamilySelect.selectedOptions[0];
    settings.fontFamily = fontFamilySelect.value;
    settings.googleFont = (opt && opt.dataset.googleFont) || null;
    if (settings.googleFont) loadGoogleFont(settings.googleFont);
    saveSettings();
    applyTypography();
  });

  function setFontSize(px) {
    settings.fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, px));
    saveSettings();
    applyTypography();
    reflectPanelState();
  }
  fontIncreaseBtn.addEventListener("click", () => setFontSize(settings.fontSize + FONT_STEP));
  fontDecreaseBtn.addEventListener("click", () => setFontSize(settings.fontSize - FONT_STEP));

  themeToggle.addEventListener("click", (e) => {
    const opt = e.target.closest(".theme-toggle-option");
    if (!opt) return;
    settings.theme = opt.dataset.themeValue;
    saveSettings();
    applyTypography();
    reflectPanelState();
  });

  // ── Caption rendering ────────────────────────────────────────────────────
  // Everything below is ported from the original CAPTION_HTML poll/append/
  // trim/scroll logic essentially unchanged — this is the one piece the
  // rewrite must not alter behaviorally. The only addition is
  // resetRenderedCaptions(), needed because view can now change live
  // without a page reload (the old version only ever read query params
  // once at load).

  let lastCount = 0;
  let lastUpdated = 0;
  let overlayDismissed = false;
  let hasReceivedData = false;
  let pollDelay = FAST_MS;
  let pinnedToBottom = true;
  const recentGroups = [];

  function resetRenderedCaptions() {
    linesDiv.innerHTML = "";
    recentGroups.length = 0;
    lastCount = 0;
    lastUpdated = 0;
    hasReceivedData = false;
    if (!overlayDismissed && !hideStatus) overlay.classList.remove("hidden");
  }

  function unpin() {
    if (!pinnedToBottom) return;
    pinnedToBottom = false;
    backToLiveBtn.classList.add("visible");
  }
  container.addEventListener("wheel", unpin, { passive: true });
  container.addEventListener("touchmove", unpin, { passive: true });
  container.addEventListener("scroll", function () {
    if (pinnedToBottom) return;
    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    if (distanceFromBottom < 24) {
      pinnedToBottom = true;
      backToLiveBtn.classList.remove("visible");
    }
  });
  backToLiveBtn.addEventListener("click", function () {
    pinnedToBottom = true;
    backToLiveBtn.classList.remove("visible");
    container.scrollTo({ top: container.scrollHeight, behavior: "instant" });
  });

  window.dismissOverlay = function () {
    overlayDismissed = true;
    overlay.classList.add("hidden");
  };
  overlay.addEventListener("click", function (e) {
    if (e.target === overlay) window.dismissOverlay();
  });

  function scrollToBottom() {
    requestAnimationFrame(function () {
      container.scrollTop = container.scrollHeight;
    });
  }

  function makeLangLine(lang, text) {
    const div = document.createElement("div");
    div.className = "lang-line";
    div.dataset.lang = lang;
    div.textContent = text;
    return div;
  }

  function flushGroup(group) {
    if (group.flushed) return;
    group.flushed = true;
    clearTimeout(group.timer);
    getExpectedLangs().forEach((lang) => {
      if (group.buffer[lang] !== undefined) {
        group.el.appendChild(makeLangLine(lang, group.buffer[lang]));
      }
    });
  }

  function appendMultiLanguageLine(line) {
    const now = Date.now();
    const expected = getExpectedLangs();

    let group = null;
    for (let i = recentGroups.length - 1; i >= 0; i--) {
      const g = recentGroups[i];
      if (!g.flushed && now - g.ts <= GROUP_WINDOW_MS && g.buffer[line.lang] === undefined) {
        group = g;
        break;
      }
    }

    if (!group) {
      const wrapper = document.createElement("div");
      wrapper.className = "multi-line-block";
      wrapper.dataset.ts = String(now);
      linesDiv.appendChild(wrapper);
      group = { ts: now, el: wrapper, buffer: {}, flushed: false, timer: null };
      recentGroups.push(group);
      while (recentGroups.length > 50) recentGroups.shift();
    }

    group.buffer[line.lang] = line.text;

    const have = expected.filter((l) => group.buffer[l] !== undefined);
    if (have.length >= expected.length) {
      flushGroup(group);
      return;
    }

    clearTimeout(group.timer);
    group.timer = setTimeout(() => flushGroup(group), GROUP_WINDOW_MS);
  }

  function appendSingleLine(text) {
    const ts = String(Date.now());
    if (display === "paragraph") {
      const span = document.createElement("span");
      span.className = "span-item";
      span.dataset.ts = ts;
      span.textContent = text + " ";
      linesDiv.appendChild(span);
    } else {
      const div = document.createElement("div");
      div.className = "line-item";
      div.dataset.ts = ts;
      div.textContent = text;
      linesDiv.appendChild(div);
    }
  }

  function trimDom() {
    const selector = multiLangMode()
      ? ".multi-line-block"
      : display === "paragraph"
      ? ".span-item"
      : ".line-item";

    // Absolute ceiling regardless of scroll position — a pure memory safety
    // valve for a viewer left scrolled away for a very long time.
    const HARD_CAP = Math.max(DOM_CAP, maxLines) * 5;
    let items = linesDiv.querySelectorAll(selector);
    const hardExcess = items.length - HARD_CAP;
    for (let i = 0; i < hardExcess; i++) items[i].remove();

    // Everything below only runs while pinned to the live edge, so we never
    // yank content out from under someone who has scrolled back to read it.
    if (!pinnedToBottom) return;

    items = linesDiv.querySelectorAll(selector);
    const limit = maxLines > 0 ? maxLines : DOM_CAP;
    const toRemove = items.length - limit;
    for (let i = 0; i < toRemove; i++) items[i].remove();

    const cutoff = Date.now() - HISTORY_MS;
    const remaining = linesDiv.querySelectorAll(selector);
    for (const el of remaining) {
      if (parseInt(el.dataset.ts, 10) < cutoff) el.remove();
      else break; // elements are appended in chronological order
    }
  }

  async function poll() {
    try {
      const resp = await fetch("/api/latest");
      if (!resp.ok) throw new Error("HTTP " + resp.status);

      const data = await resp.json();
      pollDelay = FAST_MS;

      if (data.updated === lastUpdated) return;
      lastUpdated = data.updated;

      // The server keeps only a recent window of lines: start is the absolute
      // index of the window's first line, total counts every line ever pushed.
      const start = data.start || 0;
      const total = data.total || data.lines.length;
      if (total < lastCount) {
        // Server restarted mid-service — its counter is behind ours. Resync
        // to the window start so the fresh backlog still renders.
        lastCount = start;
      }
      const newLines = data.lines.slice(Math.max(0, lastCount - start));
      lastCount = total;

      let appended = false;

      for (const line of newLines) {
        if (line.kind !== mode) continue;
        if (!langAllowed(line.lang)) continue;

        if (!overlayDismissed && !hasReceivedData) {
          hasReceivedData = true;
          window.dismissOverlay();
        }

        if (multiLangMode()) appendMultiLanguageLine(line);
        else appendSingleLine(line.text);

        appended = true;
      }

      trimDom();

      if (appended && pinnedToBottom) scrollToBottom();
    } catch (e) {
      pollDelay = Math.min(pollDelay * GROWTH, MAX_MS);
    } finally {
      setTimeout(poll, pollDelay);
    }
  }

  // ── Boot ─────────────────────────────────────────────────────────────────
  // Hydrate + apply immediately so there's no flash of default styling;
  // start polling right away rather than waiting on /api/config (which only
  // affects the language-fallback check and, on the rare case it fires,
  // triggers its own re-render via resetRenderedCaptions()).

  applyView();
  applyTypography();
  reflectPanelState();
  poll();
  initConfig();
})();
