/* Black Sakura Translate — Steam-Style Local AI Game Localization Launcher */

const state = {
  view: "game",       // "home" | "game" | "tools"
  subtab: "overview", // "overview" | "live" | "saved" | "settings"
  theme: localStorage.getItem("sakuga_theme") || "mono-ink",
  uiLang: localStorage.getItem("sakuga_ui_lang") === "en" ? "en" : "vi",
  games: [],
  selectedGameId: null,
  filter: "all",
  search: "",
  liveFilter: "",
  liveCategoryFilter: "ALL",
  liveSearchTimer: null,
  dismissedUpdateGameIds: new Set(),
  recentRows: [],
  preset: "BALANCED",
  scope: "FULL_PATCH",
  aiMode: "AUTO_HYBRID",
  sourceLang: "en",
  targetLang: "vi",
  injectSlot: "",
  rankedSlots: [],
  estimates: {},
  jobPaused: false,
  jobRunning: false,
  etaSecondsRemaining: null,
  etaSlowdownStrikes: 0,
  etaTimerHandle: null,
  contextSaveTimer: null,
  fetchedSteamContextIds: new Set(),
  sessionTokens: 0,
  lastTokPerSec: 0,
  supportedLangs: null,
  hardware: null,
  ollamaStatus: null,
  skills: { languages: [], styles: [] },
  skillCatalogLayer: "styles",
  editingSkill: null,
  appVersion: "1.0.0",
  autoCheckUpdates: true,
  appUpdateInfo: null,
  lastJobError: null,
};

const DEFAULT_QWEN_SUPPORTED_LANGS = [
  { code: "vi", label: "Tiếng Việt (Vietnamese)" },
  { code: "en", label: "English" },
  { code: "zh", label: "简体中文 (Simplified Chinese)" },
  { code: "zh-TW", label: "繁體中文 (Traditional Chinese)" },
  { code: "ja", label: "日本語 (Japanese)" },
  { code: "ko", label: "한국어 (Korean)" },
  { code: "fr", label: "Français (French)" },
  { code: "es", label: "Español (Spanish)" },
  { code: "de", label: "Deutsch (German)" },
  { code: "pt", label: "Português (Portuguese)" },
  { code: "pt-BR", label: "Português do Brasil" },
  { code: "it", label: "Italiano (Italian)" },
  { code: "ru", label: "Русский (Russian)" },
  { code: "pl", label: "Polski (Polish)" },
  { code: "th", label: "ไทย (Thai)" },
  { code: "id", label: "Bahasa Indonesia" },
  { code: "tr", label: "Türkçe (Turkish)" },
  { code: "ar", label: "العربية (Arabic)" },
  { code: "nl", label: "Nederlands (Dutch)" },
];

const QWEN_ALIAS_MAP = {
  vi: "vi", "vi-vn": "vi", vietnamese: "vi",
  en: "en", "en-us": "en", "en-gb": "en", english: "en",
  zh: "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh", schinese: "zh", chinese: "zh", chs: "zh",
  "zh-tw": "zh-TW", "zh-hk": "zh-TW", "zh-hant": "zh-TW", tchinese: "zh-TW", cht: "zh-TW",
  ja: "ja", "ja-jp": "ja", jp: "ja", japanese: "ja",
  ko: "ko", "ko-kr": "ko", kr: "ko", korean: "ko", koreana: "ko",
  fr: "fr", "fr-fr": "fr", french: "fr",
  es: "es", "es-es": "es", "es-419": "es", spanish: "es", latam: "es",
  de: "de", "de-de": "de", german: "de",
  pt: "pt", "pt-pt": "pt", portuguese: "pt",
  "pt-br": "pt-BR", ptbr: "pt-BR", brazilian: "pt-BR",
  it: "it", "it-it": "it", italian: "it",
  ru: "ru", "ru-ru": "ru", russian: "ru",
  pl: "pl", "pl-pl": "pl", polish: "pl",
  th: "th", "th-th": "th", thai: "th",
  id: "id", "id-id": "id", indonesian: "id",
  tr: "tr", "tr-tr": "tr", turkish: "tr",
  ar: "ar", "ar-sa": "ar", arabic: "ar",
  nl: "nl", "nl-nl": "nl", dutch: "nl",
};

function normalizeQwenLangCode(code) {
  const raw = String(code || "").trim();
  if (!raw) return "";
  const key = raw.toLowerCase().replace(/_/g, "-");
  if (QWEN_ALIAS_MAP[key]) return QWEN_ALIAS_MAP[key];
  const base = key.split("-")[0];
  if (base !== "zh" && QWEN_ALIAS_MAP[base]) return QWEN_ALIAS_MAP[base];
  return "";
}

function isQwenSupportedLang(code) {
  return Boolean(normalizeQwenLangCode(code));
}

function getSupportedLangs() {
  return Array.isArray(state.supportedLangs) && state.supportedLangs.length
    ? state.supportedLangs
    : DEFAULT_QWEN_SUPPORTED_LANGS;
}

function getLangDisplayLabel(code) {
  const canon = normalizeQwenLangCode(code) || code;
  const hit = getSupportedLangs().find((x) => x.code === canon);
  return hit ? hit.label : String(code || "").toUpperCase();
}

function populateTargetLangSelect() {
  const sel = $("selectTargetLang");
  if (!sel) return;
  const cur = sel.value || state.targetLang || "vi";
  const langs = getSupportedLangs();
  sel.innerHTML = "";
  for (const item of langs) {
    const opt = document.createElement("option");
    opt.value = item.code;
    opt.textContent = item.label;
    sel.appendChild(opt);
  }
  const desired = langs.some((x) => x.code === cur) ? cur : "vi";
  state.targetLang = desired;
  sel.value = desired;
}

const TONE_LABELS = {
  natural: "Tự nhiên",
  wuxia: "Cổ trang / Kiếm hiệp",
  casual: "Đời thường / Hài hước",
  horror: "Kinh dị / U ám",
  scifi: "Sci-Fi / Kỹ thuật",
};

const PRESET_DEFS = {
  FAST: { level: 1, badge: "CẤP 1 • SIÊU TỐC", aiMode: "FLASH", scope: "FULL_PATCH", translateChoices: true, keepProperNouns: true, patchFonts: true },
  BALANCED: { level: 2, badge: "CẤP 2 • CÂN BẰNG", aiMode: "AUTO_HYBRID", scope: "FULL_PATCH", translateChoices: true, keepProperNouns: true, patchFonts: true },
  DEEP_STORY: { level: 3, badge: "CẤP 3 • CỐT TRUYỆN SÂU", aiMode: "DEEP_THINK", scope: "SUB_ONLY", translateChoices: true, keepProperNouns: true, patchFonts: true },
};
const LEVEL_TO_PRESET = { "1": "FAST", "2": "BALANCED", "3": "DEEP_STORY" };
const $ = (id) => document.getElementById(id);

function fireSakugaVfx(el, kind = "tap") {
  const fx = window.SakugaFX;
  if (!fx || !el) return;
  try {
    const r = el.getBoundingClientRect?.();
    if (!r || r.width < 2 || r.height < 2) return;
    const [x, y] = fx.at(el, 0.5, 0.5);
    if (kind === "heavy") {
      fx.burst(x, y, 72, 11);
      fx.ticks(x, y, 32, 6);
      fx.sfx?.("ドン", x, y - 28, { size: 42 });
    } else if (kind === "preset") {
      fx.burst(x, y, 42, 7);
      fx.ticks(x, y, 22, 5);
      fx.sfx?.("カチッ", x + 22, y - 20, { size: 32 });
    } else {
      fx.ticks(x, y, 18, 4);
      fx.puff(x, y, 8);
    }
  } catch (_) {}
}

function applyTheme(themeName, elForBurst = null, persistToServer = false) {
  state.theme = themeName || "mono-ink";
  document.documentElement.setAttribute("data-theme", state.theme);
  localStorage.setItem("sakuga_theme", state.theme);
  document.querySelectorAll(".swatch").forEach((s) => s.classList.toggle("active", s.dataset.themePick === state.theme));
  if (elForBurst) {
    fireSakugaVfx(elForBurst, "preset");
    state.themeDirty = true;
    fetch("/api/ui/theme", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ theme: state.theme }),
    }).catch(() => {});
  } else if (persistToServer) {
    fetch("/api/ui/theme", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ theme: state.theme }),
    }).catch(() => {});
  }
}

function applyUiLanguage(langCode, elForBurst = null, persistToServer = false) {
  const next = langCode === "en" ? "en" : "vi";
  state.uiLang = next;
  if (typeof window.applyUiLanguageToDom === "function") {
    window.applyUiLanguageToDom(next);
  }
  updateStartButtonState();
  syncDualModelUI();
  if ($("activePresetBadge") && typeof window.tUI === "function") {
    $("activePresetBadge").textContent = window.tUI(`preset_badge_${state.preset || "BALANCED"}`);
  }
  if (selectedGame()) renderSelectedGamePage();
  if (state.view === "tools") renderSkillsCatalog();
  renderAppUpdateUI();

  if (elForBurst) {
    fireSakugaVfx(elForBurst, "tap");
    state.uiLangDirty = true;
    fetch("/api/ui/lang", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lang: next }),
    }).catch(() => {});
    refreshHealth();
    showToast(next === "en" ? "Switched UI language to English ✓" : "Đã chuyển giao diện sang Tiếng Việt ✓");
  } else if (persistToServer) {
    fetch("/api/ui/lang", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lang: next }),
    }).catch(() => {});
  }
}

function showToast(msg, kind = "ok") {
  if (kind === "ok" && msg.length <= 44 && $("sakugaToast")?.show) {
    $("sakugaToast").show(msg);
    return;
  }
  const stack = $("toastStack");
  const el = document.createElement("div");
  el.className = `toast ${kind === "error" ? "error" : ""}`;
  el.textContent = msg;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail;
    const msg = typeof detail === "object" ? detail.message || JSON.stringify(detail) : detail || res.statusText;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

function formatEta(secs) {
  if (secs === null || secs === undefined || secs < 0) return "--:--";
  if (secs === 0) return "00:00";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function renderEtaText(secs) {
  const txt = formatEta(secs);
  $("teleEta").textContent = `Còn lại: ${txt}`;
  $("teleEtaMini").textContent = txt;
}

function startLocalEtaCountdown(initialSecs) {
  stopLocalEtaCountdown();
  if (initialSecs === null || initialSecs === undefined) return;
  state.etaSecondsRemaining = Math.max(0, Math.round(initialSecs));
  state.etaSlowdownStrikes = 0;
  state.etaSlowFactor = 1;
  state.etaTickAccum = 0;
  renderEtaText(state.etaSecondsRemaining);
  state.etaTimerHandle = setInterval(() => {
    if (!state.jobRunning || state.jobPaused || state.etaSecondsRemaining === null) return;
    state.etaTickAccum = (state.etaTickAccum || 0) + 1;
    const step = state.etaSlowFactor || 1;
    if (state.etaTickAccum < step) return;
    state.etaTickAccum = 0;
    if (state.etaSecondsRemaining > 1) {
      state.etaSecondsRemaining -= 1;
      renderEtaText(state.etaSecondsRemaining);
    }
  }, 1000);
}

function syncLocalEtaFromServer(serverSecs) {
  if (serverSecs === null || serverSecs === undefined) return;
  const next = Math.max(0, Math.round(serverSecs));
  if (!state.etaTimerHandle || state.etaSecondsRemaining === null) {
    startLocalEtaCountdown(next);
    return;
  }
  const cur = state.etaSecondsRemaining;
  if (next === 0 || next <= cur) {
    // Strictly allow decreasing or exact match; restore normal 1s cadence
    state.etaSecondsRemaining = next;
    state.etaSlowFactor = 1;
    state.etaSlowdownStrikes = 0;
    renderEtaText(next);
    return;
  }
  // Strict monotonic non-increasing UI rule: NEVER jump the visible ETA upward (e.g. 9m -> 18m).
  // Instead, gently slow local countdown ticks while waiting for real progress to catch up.
  state.etaSlowFactor = (next > cur * 1.25 && cur > 15) ? 2 : 1;
}

function stopLocalEtaCountdown() {
  if (state.etaTimerHandle) {
    clearInterval(state.etaTimerHandle);
    state.etaTimerHandle = null;
  }
  state.etaSlowdownStrikes = 0;
  state.etaSlowFactor = 1;
  state.etaTickAccum = 0;
}

function selectedGame() {
  return state.games.find((g) => g.game_id === state.selectedGameId) || state.games[0] || null;
}

// ---------------- View & Subtab Switching ---------------- //

function switchView(viewName) {
  state.view = viewName;
  document.querySelectorAll(".top-link").forEach((b) => b.classList.toggle("active", b.dataset.view === viewName));
  $("viewHome").classList.toggle("active", viewName === "home");
  $("viewGame").classList.toggle("active", viewName === "game");
  $("viewTools").classList.toggle("active", viewName === "tools");
  if (viewName === "tools") loadToolsAndTM();
}

function switchSubtab(subName) {
  const normalized = subName === "shields" ? "saved" : subName;
  state.subtab = normalized;
  document.querySelectorAll(".subtab").forEach((t) => {
    const act = t.dataset.subtab === normalized;
    t.classList.toggle("active", act);
    if (act) fireSakugaVfx(t, "tap");
  });
  $("paneOverview").classList.toggle("active", normalized === "overview");
  $("paneLive").classList.toggle("active", normalized === "live");
  $("paneSaved").classList.toggle("active", normalized === "saved");
  $("paneSettings").classList.toggle("active", normalized === "settings");
  if (subName === "shields") {
    const sh = $("shieldsDetailsCard");
    if (sh) {
      sh.open = true;
      sh.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

// ---------------- Health & Telemetry ---------------- //

function syncDualModelUI(tele = null, opts = {}) {
  const hw = state.hardware || {};
  const ol = state.ollamaStatus || {};
  const mainModel = (tele?.model || hw.main_model || ol.main_model || "qwen3.5:4b");
  const uiFastModel = (tele?.ui_fast_model || hw.ui_fast_model || ol.ui_fast_model || "qwen3:1.7b");
  const dualOn = Boolean(
    opts.dualModelActive !== undefined
      ? opts.dualModelActive
      : (tele?.dual_model_enabled !== undefined ? tele.dual_model_enabled : (hw.dual_model_enabled ?? ol.dual_model_enabled))
  );

  if ($("chkDualModelQuick") && $("chkDualModelQuick").checked !== dualOn) $("chkDualModelQuick").checked = dualOn;
  if ($("chkDualModelSettings") && $("chkDualModelSettings").checked !== dualOn) $("chkDualModelSettings").checked = dualOn;
  if ($("lblUiFastModelName")) $("lblUiFastModelName").textContent = uiFastModel;
  if ($("lblMainStoryModelName")) $("lblMainStoryModelName").textContent = mainModel;
  if ($("dualModeStatusBadge")) {
    const isEn = state.uiLang === "en";
    $("dualModeStatusBadge").textContent = dualOn ? (isEn ? "⚡ ON (UI Boost)" : "⚡ ĐANG BẬT (Tăng tốc UI)") : (isEn ? "OFF (1 Model)" : "TẮT (1 Model)");
    $("dualModeStatusBadge").className = `badge ${dualOn ? "badge-safe" : ""}`;
  }
  if ($("dualModelPairStrip")) $("dualModelPairStrip").classList.toggle("is-off", !dualOn);
  const chipUi = document.querySelector("#dualModelPairStrip .chip-ui");
  if (chipUi) chipUi.classList.toggle("active-on", dualOn);

  const banner = $("liveActiveModelBanner");
  if (banner) {
    const isEn = state.uiLang === "en";
    const role = opts.activeModelRole || tele?.active_runtime_role || (state.jobRunning ? "MAIN_STORY" : "IDLE");
    const activeMdl = opts.activeModel || tele?.active_runtime_model || mainModel;
    banner.className = "live-active-model-banner";
    if (!state.jobRunning || role === "IDLE") {
      banner.textContent = dualOn
        ? (isEn ? `⚡ Dual-Model ready: UI [${uiFastModel}] ➔ Flush VRAM ➔ Story [${mainModel}]` : `⚡ Dual-Model sẵn sàng: UI [${uiFastModel}] ➔ Xả VRAM ➔ Cốt truyện [${mainModel}]`)
        : (isEn ? `🤖 Model ready: ${mainModel} (All UI & Story)` : `🤖 Model sẵn sàng: ${mainModel} (Dịch toàn bộ UI & Cốt truyện)`);
    } else if (role === "UI_FAST") {
      banner.classList.add("role-ui-fast");
      banner.textContent = isEn
        ? `⚡ RUNNING UI MODEL: ${activeMdl} — Fast scanning UI labels (${mainModel} handles Story)`
        : `⚡ ĐANG CHẠY MODEL UI: ${activeMdl} — Quét nhanh nhãn ngắn (Giữ 100% chất lượng thoại cho ${mainModel})`;
    } else if (role === "VRAM_SWAP") {
      banner.classList.add("role-swap");
      banner.textContent = isEn
        ? `🧹 FLUSHING VRAM (keep_alive=0): Unloading ${uiFastModel} ➔ Loading 100% VRAM for ${mainModel}...`
        : `🧹 ĐANG XẢ VRAM (keep_alive=0): Gỡ ${uiFastModel} ➔ Nạp 100% VRAM cho ${mainModel}...`;
    } else if (role === "PRETHINK") {
      banner.classList.add("role-main");
      banner.textContent = isEn
        ? `🧠 RUNNING MAIN MODEL: ${activeMdl} — Phase 0 Glossary & Style Card`
        : `🧠 ĐANG CHẠY MODEL CHÍNH: ${activeMdl} — Pha 0 lập Từ điển & Style Card`;
    } else if (role === "QA_POLISH") {
      banner.classList.add("role-main");
      banner.textContent = isEn
        ? `🛠 RUNNING MAIN MODEL: ${activeMdl} — AI QA Repair & Polish`
        : `🛠 ĐANG CHẠY MODEL CHÍNH: ${activeMdl} — AI QA kiểm lỗi & chuốt văn phong`;
    } else {
      banner.classList.add("role-main");
      banner.textContent = isEn
        ? `📖 RUNNING MAIN MODEL: ${activeMdl} — High-Quality Story & Dialogue`
        : `📖 ĐANG CHẠY MODEL CHÍNH: ${activeMdl} — Dịch Cốt truyện / Hội thoại chất lượng cao`;
    }
  }
}

async function toggleDualModel(enabled) {
  const dual = Boolean(enabled);
  try {
    const res = await api("/api/system/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dual_model_enabled: dual }),
    });
    if (res.hardware) state.hardware = res.hardware;
    if (res.ollama) state.ollamaStatus = res.ollama;
    await refreshHealth();
    syncDualModelUI(res.telemetry || null, { dualModelActive: dual });
    const uiM = state.hardware?.ui_fast_model || "qwen3:1.7b";
    const mainM = state.hardware?.main_model || "qwen3.5:4b";
    showToast(
      dual
        ? `Đã BẬT Dual-Model: ${uiM} (UI) ➔ ${mainM} (Cốt truyện) — Nhanh hơn, chất lượng thoại không đổi!`
        : `Đã TẮT Dual-Model: Chạy toàn bộ bằng ${mainM}.`
    );
  } catch (err) {
    showToast(`Lỗi bật/tắt Dual-Model: ${err.message}`, "error");
    syncDualModelUI();
  }
}

function updateLiveTelemetryUI(tele = null, opts = {}) {
  const workers = tele?.parallel_streams || state.hardware?.parallel_workers || 4;
  if (tele && typeof tele === "object") {
    const used = Number(tele.vram_used_gb || 0).toFixed(1);
    const total = Number(tele.vram_total_gb || 8).toFixed(1);
    const vramTxt = `VRAM: ${used} / ${total} GB`;
    if ($("teleVram")) $("teleVram").textContent = vramTxt;
    if ($("progLiveVram")) $("progLiveVram").textContent = vramTxt;
    if (tele.model) {
      if ($("activeModelShortLabel")) $("activeModelShortLabel").textContent = tele.model;
      if ($("prethinkModelLabel")) $("prethinkModelLabel").textContent = tele.model;
      if ($("settingsActiveModelLabel")) $("settingsActiveModelLabel").textContent = tele.model;
      if ($("footerModelBadge")) $("footerModelBadge").textContent = `BLACK SAKURA TRANSLATE // ${tele.model}`;
    }
  }
  if (typeof opts.tokensDone === "number") state.sessionTokens = Math.max(0, Math.round(opts.tokensDone));
  if (typeof opts.tokPerSec === "number") state.lastTokPerSec = opts.tokPerSec;
  if (typeof opts.tokensDone === "number" || typeof opts.tokPerSec === "number") {
    const tokFmt = Number(state.sessionTokens || 0).toLocaleString("vi-VN");
    const tpsFmt = Number(state.lastTokPerSec || 0).toFixed(1);
    if ($("progLiveTokens")) $("progLiveTokens").textContent = `Token: ${tokFmt} (${tpsFmt} tok/s)`;
    if ($("teleSpeed")) $("teleSpeed").textContent = `${tpsFmt} tok/s • ${tokFmt} tok • ${workers}x Pool`;
  }
  if (typeof opts.running === "boolean" && $("progLiveDot")) {
    $("progLiveDot").className = `dot ${
      opts.running ? "dot-live" : opts.error ? "dot-error" : opts.done ? "dot-green" : "dot-muted"
    }`;
  } else if (opts.error && $("progLiveDot")) {
    $("progLiveDot").className = "dot dot-error";
  } else if (opts.done && $("progLiveDot")) {
    $("progLiveDot").className = "dot dot-green";
  }
  const stateBtn = $("progLiveStateBtn");
  if (stateBtn) {
    if (opts.error) {
      stateBtn.classList.add("has-error");
      stateBtn.setAttribute("role", "button");
      stateBtn.setAttribute("tabindex", "0");
      stateBtn.title = state.uiLang === "en" ? "Click to inspect error details" : "Bấm vào đây để xem bảng chi tiết lỗi";
    } else if (opts.running || opts.done || opts.clearError) {
      stateBtn.classList.remove("has-error");
      stateBtn.removeAttribute("role");
      stateBtn.removeAttribute("tabindex");
      stateBtn.removeAttribute("title");
    }
  }
  if (typeof opts.taskLabel === "string" && $("progLiveTask")) {
    $("progLiveTask").textContent = opts.taskLabel;
  }
  syncDualModelUI(tele, opts);
}

function getErrorHintText(rawMsg) {
  const isEn = state.uiLang === "en";
  const m = String(rawMsg || "").toLowerCase();
  if (m.includes("400") || m.includes("bad request")) {
    return isEn
      ? "💡 Tip: Ollama rejected a request parameter or context chunk. Try updating the app or switching AI Engine to 'Auto-Hybrid'."
      : "💡 Gợi ý: Ollama từ chối tham số hoặc gói text dài. Hãy thử cập nhật bản mới hoặc chuyển Động cơ AI sang 'Auto-Hybrid'.";
  }
  if (m.includes("timeout") || m.includes("timed out") || m.includes("11434") || m.includes("connect")) {
    return isEn
      ? "💡 Tip: Make sure Ollama is running smoothly and not starved of GPU VRAM by other heavy apps/games."
      : "💡 Gợi ý: Kiểm tra lại Ollama đang chạy ổn định và đóng bớt game/ứng dụng nặng VRAM khi dịch.";
  }
  if (m.includes("anticheat")) {
    return isEn
      ? "💡 Tip: Protected by Anti-Cheat Guard to keep your online account safe."
      : "💡 Gợi ý: Game có Anti-Cheat bảo vệ tài khoản Online nên bị chặn mặc định.";
  }
  return isEn
    ? "💡 Tip: Click 'Copy Error' below if you want to report this issue on GitHub."
    : "💡 Gợi ý: Bấm 'Copy lỗi' bên dưới để kiểm tra hoặc gửi báo lỗi lên GitHub.";
}

function recordJobError(message, phaseHint = "") {
  const g = selectedGame();
  const mdl = state.ollamaStatus?.main_model || state.hardware?.main_model || "Ollama";
  state.lastJobError = {
    message: String(message || "Unknown error").trim(),
    phase: phaseHint || $("phaseStatusLabel")?.textContent || "",
    gameTitle: g?.title || "",
    model: mdl,
    time: new Date().toLocaleTimeString(),
  };
  const errLabel = state.uiLang === "en" ? "Error" : "Gặp lỗi";
  updateLiveTelemetryUI(null, { running: false, error: true, taskLabel: errLabel });
}

function openErrorReportModal() {
  if (!state.lastJobError) return;
  const e = state.lastJobError;
  const isEn = state.uiLang === "en";
  if ($("errorReportContext")) {
    const parts = [];
    if (e.gameTitle) parts.push(`${isEn ? "Game" : "Game"}: ${e.gameTitle}`);
    if (e.model) parts.push(`Model: ${e.model}`);
    if (e.phase) parts.push(`${isEn ? "Stage" : "Giai đoạn"}: ${e.phase}`);
    if (e.time) parts.push(`${isEn ? "Time" : "Lúc"}: ${e.time}`);
    $("errorReportContext").textContent = parts.join(" • ");
  }
  if ($("errorReportMessageBox")) {
    $("errorReportMessageBox").textContent = e.message;
  }
  if ($("errorReportHintBox")) {
    $("errorReportHintBox").textContent = getErrorHintText(e.message);
  }
  $("modalErrorReport")?.classList.remove("hidden");
}

async function refreshHealth() {
  try {
    const data = await api("/api/health");
    const ol = data.ollama || {};
    state.ollamaStatus = ol;
    if (data.hardware) state.hardware = data.hardware;

    const workers = ol.parallel_workers || data.hardware?.parallel_workers || 4;
    const modelLbl = ol.main_model || data.hardware?.main_model || "Ollama";
    const uiModelLbl = ol.ui_fast_model || data.hardware?.ui_fast_model || "";
    const dualOn = Boolean(ol.dual_model_enabled ?? data.hardware?.dual_model_enabled);
    const tierLbl = ol.tier_label || data.hardware?.hardware?.tier_label || "Auto";
    const wLbl = state.uiLang === "en" ? "Workers" : "Luồng";
    $("ollamaStatusDot").className = `dot ${ol.online ? (ol.selected_installed === false ? "dot-gold" : "dot-green") : "dot-red"}`;
    $("ollamaStatusText").textContent = ol.online
      ? (dualOn && uiModelLbl && uiModelLbl !== modelLbl
          ? `Dual: ${uiModelLbl} ⚡ + ${modelLbl} 📖 • ${workers} ${wLbl}`
          : `${modelLbl} • ${workers} ${wLbl} (${tierLbl})`)
      : `Ollama Offline • ${modelLbl}`;

    const tl = data.tools || {};
    $("toolsStatusDot").className = `dot ${tl.all_ready ? "dot-green" : "dot-gold"}`;
    $("toolsStatusText").textContent = tl.all_ready ? "Tools Ready" : `Tools ${tl.ready_count || 0}/${tl.total_count || 5}`;

    syncOllamaOnboardingBanner(ol, data.hardware || state.hardware);

    if (data.telemetry) updateLiveTelemetryUI(data.telemetry);
    else syncDualModelUI();
    if (data.tm_stats) $("tmTotalCount").textContent = data.tm_stats.count || 0;
    if (Array.isArray(data.supported_langs) && data.supported_langs.length) {
      state.supportedLangs = data.supported_langs;
      populateTargetLangSelect();
    }
    if (data.ui_theme && !state.themeDirty && data.ui_theme !== state.theme) {
      applyTheme(data.ui_theme, null, false);
    }
    if (data.ui_lang && !state.uiLangDirty && data.ui_lang !== state.uiLang) {
      applyUiLanguage(data.ui_lang, null, false);
    }
    if (typeof data.minimize_to_tray === "boolean" && $("chkMinimizeToTray")) {
      $("chkMinimizeToTray").checked = data.minimize_to_tray;
    }
    if (data.app_version) {
      state.appVersion = data.app_version;
    }
    if (typeof data.auto_check_updates === "boolean") {
      state.autoCheckUpdates = data.auto_check_updates;
      if ($("chkAutoUpdateCheck") && $("chkAutoUpdateCheck").checked !== data.auto_check_updates) {
        $("chkAutoUpdateCheck").checked = data.auto_check_updates;
      }
    }
    renderAppUpdateUI();
  } catch (_) {}
}

function renderAppUpdateUI() {
  const isEn = state.uiLang === "en";
  const curVer = state.appVersion || "1.0.0";
  const info = state.appUpdateInfo;
  const pill = $("appUpdatePill");
  const pillTxt = $("appUpdatePillText");
  const badge = $("appVersionBadge");
  const statusEl = $("appUpdateStatusText");
  const btnView = $("btnOpenUpdateModal");

  if (badge) badge.textContent = `v${curVer}`;

  if (info && info.update_available) {
    const tag = info.latest_tag || `v${info.latest_version || curVer}`;
    if (pill) pill.classList.remove("hidden");
    if (pillTxt) pillTxt.textContent = isEn ? `⬆ ${tag} New` : `⬆ Có ${tag}`;
    if (btnView) btnView.classList.remove("hidden");
    if (statusEl) {
      statusEl.textContent = isEn
        ? `Update available: v${curVer} ➔ ${tag} (click to view release notes)`
        : `Đã có bản mới: v${curVer} ➔ ${tag} (bấm để xem chi tiết & tải về)`;
    }
  } else {
    if (pill) pill.classList.add("hidden");
    if (btnView) btnView.classList.add("hidden");
    if (statusEl) {
      if (info && info.ok && !info.skipped) {
        statusEl.textContent = isEn
          ? `Up to date (v${curVer}) • Latest on GitHub: ${info.latest_tag || "v" + curVer}`
          : `Đang dùng bản mới nhất (v${curVer}) • Bản mới nhất trên GitHub: ${info.latest_tag || "v" + curVer}`;
      } else if (info && info.error) {
        statusEl.textContent = isEn
          ? `Current version: v${curVer} • Offline or unable to reach GitHub`
          : `Phiên bản hiện tại: v${curVer} • Không có mạng hoặc chưa kết nối được GitHub`;
      } else {
        statusEl.textContent = isEn
          ? `Current version: v${curVer}`
          : `Phiên bản hiện tại: v${curVer}`;
      }
    }
  }
}

function openAppUpdateModal() {
  const info = state.appUpdateInfo;
  const curVer = state.appVersion || "1.0.0";
  const isEn = state.uiLang === "en";
  if ($("lblUpdateCurrentVer")) $("lblUpdateCurrentVer").textContent = `v${curVer}`;
  if ($("lblUpdateLatestVer")) $("lblUpdateLatestVer").textContent = info?.latest_tag || `v${info?.latest_version || curVer}`;
  if ($("lblUpdateReleaseDate")) {
    const pub = (info?.published_at || "").slice(0, 10);
    $("lblUpdateReleaseDate").textContent = pub ? (isEn ? `Published: ${pub}` : `Phát hành: ${pub}`) : "";
  }
  if ($("lblUpdateAssetMeta")) {
    const szMb = info?.asset_size ? `${(info.asset_size / (1024 * 1024)).toFixed(1)} MB` : "";
    if (info?.update_mode === "exe_zip" && info?.asset_name) {
      $("lblUpdateAssetMeta").textContent = isEn
        ? `Package: ${info.asset_name}${szMb ? " (" + szMb + ")" : ""} • Auto-update ready`
        : `Gói cài đặt: ${info.asset_name}${szMb ? " (" + szMb + ")" : ""} • Hỗ trợ tự cập nhật 1-Click`;
    } else if (info?.update_mode === "git_pull") {
      $("lblUpdateAssetMeta").textContent = isEn
        ? "Source mode detected • Click Update Now to run git pull"
        : "Đang chạy bản mã nguồn Git • Bấm Cập nhật ngay để chạy git pull";
    } else {
      $("lblUpdateAssetMeta").textContent = "";
    }
  }
  if ($("appUpdateNotesBox")) {
    $("appUpdateNotesBox").textContent = (info?.release_notes || "").trim() || (isEn ? "No release notes provided." : "Không có mô tả chi tiết cho phiên bản này.");
  }
  if ($("appUpdateProgressWrap")) $("appUpdateProgressWrap").classList.add("hidden");
  const applyBtn = $("btnApplyAppUpdate");
  if (applyBtn) {
    applyBtn.disabled = false;
    applyBtn.classList.toggle("hidden", !(info?.can_auto_update || info?.update_mode === "exe_zip" || info?.update_mode === "git_pull"));
  }
  $("modalAppUpdate")?.classList.remove("hidden");
}

async function applyAppUpdateNow() {
  const info = state.appUpdateInfo || {};
  const isEn = state.uiLang === "en";
  const applyBtn = $("btnApplyAppUpdate");
  if (state.jobRunning) {
    showToast(
      isEn
        ? "A translation/repair job is running. Please pause or wait before updating."
        : "Đang có tiến trình Dịch / Fix lỗi chạy ngầm. Hãy tạm dừng hoặc đợi xong trước khi cập nhật.",
      "error"
    );
    return;
  }
  if (applyBtn) applyBtn.disabled = true;
  if ($("appUpdateProgressWrap")) $("appUpdateProgressWrap").classList.remove("hidden");
  if ($("appUpdateProgressBar")) $("appUpdateProgressBar").style.width = "4%";
  if ($("appUpdateProgressText")) {
    $("appUpdateProgressText").textContent = isEn ? "Starting update..." : "Đang bắt đầu tải bản cập nhật...";
  }
  try {
    const res = await api("/api/app/update-apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmed: true, download_url: info.download_url || "" }),
    });
    if (res.mode === "manual") {
      openAppReleasePage();
      if (applyBtn) applyBtn.disabled = false;
      return;
    }
    if (res.mode === "git_pull") {
      if (res.ok) {
        if ($("appUpdateProgressBar")) $("appUpdateProgressBar").style.width = "100%";
        if ($("appUpdateProgressText")) $("appUpdateProgressText").textContent = res.message || (isEn ? "Updated! Please restart app." : "Đã cập nhật! Hãy mở lại app.");
        showToast(res.message || "Đã cập nhật xong ✓");
      } else {
        if ($("appUpdateProgressText")) $("appUpdateProgressText").textContent = res.message || "Update error";
        showToast(res.message || "Lỗi cập nhật", "error");
        if (applyBtn) applyBtn.disabled = false;
      }
    }
  } catch (err) {
    if ($("appUpdateProgressText")) $("appUpdateProgressText").textContent = err.message;
    showToast(err.message, "error");
    if (applyBtn) applyBtn.disabled = false;
  }
}

async function openAppReleasePage() {
  const targetUrl = state.appUpdateInfo?.release_url || "";
  try {
    const q = targetUrl ? `?url=${encodeURIComponent(targetUrl)}` : "";
    await api(`/api/app/open-release${q}`, { method: "POST" });
    showToast(state.uiLang === "en" ? "Opened GitHub Releases in your browser!" : "Đã mở trang GitHub Releases trên trình duyệt!");
  } catch (_) {
    window.open(targetUrl || "https://github.com/hohieu112007-byte/black-sakura-translate/releases", "_blank");
  }
}

async function checkAppUpdate(force = false) {
  const btn = $("btnCheckAppUpdate");
  if (btn && force) btn.disabled = true;
  try {
    const data = await api(`/api/app/update-check?force=${force ? "true" : "false"}`);
    state.appUpdateInfo = data;
    if (data.current_version) state.appVersion = data.current_version;
    renderAppUpdateUI();
    if (force) {
      const isEn = state.uiLang === "en";
      if (data.update_available) {
        openAppUpdateModal();
      } else if (data.ok) {
        showToast(isEn ? `You are on the latest version (v${state.appVersion}) ✓` : `Bạn đang dùng bản mới nhất (v${state.appVersion}) ✓`);
      } else {
        showToast(isEn ? "Could not check GitHub right now (offline/timeout)." : "Chưa kết nối được GitHub để kiểm tra cập nhật.", "error");
      }
    }
  } catch (_) {
    if (force) {
      showToast(state.uiLang === "en" ? "Failed to check for updates." : "Lỗi kiểm tra cập nhật.", "error");
    }
  } finally {
    if (btn && force) btn.disabled = false;
  }
}

async function toggleAutoUpdateCheck(enabled) {
  const val = Boolean(enabled);
  state.autoCheckUpdates = val;
  try {
    await api("/api/app/update-settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: val }),
    });
    showToast(
      val
        ? (state.uiLang === "en" ? "Auto update check enabled ✓" : "Đã bật tự kiểm tra bản cập nhật ✓")
        : (state.uiLang === "en" ? "Auto update check disabled" : "Đã tắt tự kiểm tra bản cập nhật")
    );
  } catch (err) {
    showToast(err.message, "error");
  }
}

function getBestRecommendedModelName(ol, hw) {
  const recs = ol?.recommended_models || hw?.hardware?.recommended_models || [];
  if (recs.length && recs[0]?.name) return recs[0].name;
  return ol?.main_model || hw?.main_model || "qwen3.5:4b";
}

function syncOllamaOnboardingBanner(ol = null, hw = null) {
  const banner = $("ollamaSetupBanner");
  const modalStrip = $("hwModalOfflineStrip");
  const status = ol || state.ollamaStatus || {};
  const online = Boolean(status.online);
  const selectedInstalled = status.selected_installed !== false;
  const installedList = Array.isArray(status.models) ? status.models : [];

  if (modalStrip) modalStrip.classList.toggle("hidden", online);
  if (!banner) return;

  if (online && selectedInstalled) {
    banner.classList.add("hidden");
    banner.classList.remove("is-offline");
    return;
  }

  banner.classList.remove("hidden");
  const btnDl = $("btnDownloadOllama");
  const btnLaunch = $("btnLaunchOllama");
  const btnRecheck = $("btnRecheckOllama");
  const btnPull = $("btnQuickPullRecommended");
  const btnOpenHw = $("btnOpenHwFromBanner");

  if (!online) {
    banner.classList.add("is-offline");
    if ($("ollamaSetupTitle")) $("ollamaSetupTitle").textContent = "⚠ Chưa kết nối được với Ollama AI Local";
    if ($("ollamaSetupDesc")) {
      $("ollamaSetupDesc").textContent =
        "Black Sakura Translate dùng Ollama chạy AI miễn phí ngay trên máy bạn. Nếu chưa cài hãy bấm 'Tải Ollama', nếu đã cài hãy bấm 'Mở Ollama ngay'.";
    }
    btnDl?.classList.remove("hidden");
    btnLaunch?.classList.remove("hidden");
    btnRecheck?.classList.remove("hidden");
    btnPull?.classList.add("hidden");
    if (btnOpenHw) btnOpenHw.textContent = "⚙ Cấu hình AI";
  } else {
    banner.classList.remove("is-offline");
    const recModel = getBestRecommendedModelName(status, hw);
    const activeWant = status.main_model || recModel;
    btnDl?.classList.add("hidden");
    btnLaunch?.classList.add("hidden");
    btnRecheck?.classList.add("hidden");
    btnPull?.classList.remove("hidden");
    if (btnPull) {
      btnPull.dataset.model = recModel;
      btnPull.textContent = `⬇ Tải ngay ${recModel} & Tự kết nối`;
    }
    if (installedList.length > 0) {
      const sample = installedList.slice(0, 3).join(", ");
      if ($("ollamaSetupTitle")) $("ollamaSetupTitle").textContent = `🤖 Model '${activeWant}' chưa tải — nhưng máy bạn đã có sẵn ${installedList.length} model!`;
      if ($("ollamaSetupDesc")) {
        $("ollamaSetupDesc").textContent = `Model có sẵn trên Ollama: ${sample}. Bạn có thể tải '${recModel}' bằng 1 nút hoặc bấm 'Chọn model có sẵn' để dùng ngay.`;
      }
      if (btnOpenHw) btnOpenHw.textContent = "⚙ Chọn model có sẵn";
    } else {
      if ($("ollamaSetupTitle")) $("ollamaSetupTitle").textContent = "🚀 Ollama đã kết nối! Chỉ còn 1 bước: Tải Model AI để dịch";
      if ($("ollamaSetupDesc")) {
        $("ollamaSetupDesc").textContent = `Chưa có model nào trên Ollama. Bấm nút bên phải để tải '${recModel}' ngay trong app — tải xong hệ thống sẽ tự động kết nối và lưu cấu hình!`;
      }
      if (btnOpenHw) btnOpenHw.textContent = "⚙ Chọn model khác";
    }
  }
}

async function openOllamaDownloadPage() {
  try {
    await api("/api/ollama/open-download", { method: "POST" });
    showToast("Đã mở trang tải Ollama (https://ollama.com/download) trên trình duyệt!");
  } catch (_) {
    window.open("https://ollama.com/download", "_blank");
  }
}

async function launchOllamaFromUI(btnEl = null) {
  if (btnEl) btnEl.disabled = true;
  try {
    showToast("Đang tìm và khởi động Ollama trên máy...");
    const res = await api("/api/ollama/launch", { method: "POST" });
    if (res.ollama) state.ollamaStatus = res.ollama;
    await refreshHealth();
    if (!$("modalHardwareSettings")?.classList.contains("hidden")) {
      await openHardwareModal();
    }
    if (res.online) {
      showToast("Đã khởi động & kết nối Ollama thành công ✓");
    } else if (res.installed === false) {
      showToast(res.message || "Chưa cài Ollama trên máy. Hãy bấm 'Tải Ollama (Miễn phí)'.", "error");
    } else {
      showToast(res.message || "Đã mở Ollama, vui lòng đợi vài giây rồi bấm 'Kiểm tra lại'.");
    }
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    if (btnEl) btnEl.disabled = false;
  }
}

// ---------------- Games List & Steam Hero Rendering ---------------- //

async function loadGames(selectId = null) {
  const data = await api("/api/games");
  state.games = data.games || [];
  if (selectId) state.selectedGameId = selectId;
  if (!state.selectedGameId && state.games.length) {
    state.selectedGameId = state.games[0].game_id;
  }
  renderSidebarGames();
  renderPosterGrid();
  renderSelectedGamePage();
}

function getFilteredGames() {
  return state.games.filter((g) => {
    if (state.search && !g.title.toLowerCase().includes(state.search.toLowerCase())) return false;
    if (state.filter === "done") {
      return Boolean(g.is_patched || g.has_saved_translation || Number(g.progress_pct || 0) >= 99.9);
    }
    if (state.filter === "steam") return Boolean(g.steam_appid);
    return true;
  });
}

async function removeGameFromLibrary(game, ev = null) {
  if (ev) {
    ev.preventDefault();
    ev.stopPropagation();
  }
  if (!game?.game_id) return;
  try {
    await api(`/api/games/${game.game_id}`, { method: "DELETE" });
    if (state.selectedGameId === game.game_id) state.selectedGameId = null;
    await loadGames();
    if (!$("modalAddGame").classList.contains("hidden")) {
      await refreshSteamModalList();
    }
    showToast(`Đã bỏ ${game.title} khỏi danh sách`);
  } catch (err) {
    showToast(err.message, "error");
  }
}

function renderSidebarGames() {
  const box = $("sidebarGameList");
  box.innerHTML = "";
  const items = getFilteredGames();
  $("sidebarGameCount").textContent = state.games.length;

  for (const g of items) {
    const row = document.createElement("div");
    row.className = `sb-game-row ${g.game_id === state.selectedGameId ? "selected" : ""}`;
    const safeCov = safeCoverUrl(g.cover_url);
    const thumbStyle = safeCov ? `background-image:url('${safeCov}')` : "";
    const statusText = g.anticheat_status === "BLOCKED"
      ? "🛡 Anti-Cheat"
      : g.is_patched
      ? `✓ ${(g.target_lang || "vi").toUpperCase()} (${g.inject_slot || ""})`
      : g.has_saved_translation
      ? `💾 Đã dịch ${Math.round(g.saved_pct || g.progress_pct || 0)}% (Bản gốc)`
      : `${Math.round(g.progress_pct || 0)}% • ${g.engine}`;

    row.innerHTML = `
      <div class="sb-thumb" style="${thumbStyle}"></div>
      <div class="sb-info">
        <div class="sb-title">${escapeHtml(g.title)}</div>
        <div class="sb-sub">${escapeHtml(statusText)}</div>
      </div>
      <button type="button" class="sb-remove-btn" title="Bỏ game khỏi danh sách (không xóa file game)">✕</button>
    `;
    row.querySelector(".sb-remove-btn").addEventListener("click", (ev) => removeGameFromLibrary(g, ev));
    row.addEventListener("click", () => {
      state.selectedGameId = g.game_id;
      fireSakugaVfx(row, "tap");
      switchView("game");
      renderSidebarGames();
      renderPosterGrid();
      renderSelectedGamePage();
    });
    box.appendChild(row);
  }
}

function renderPosterGrid() {
  const grid = $("posterGrid");
  grid.innerHTML = "";
  for (const g of getFilteredGames()) {
    const card = document.createElement("div");
    card.className = `poster-card ${g.game_id === state.selectedGameId ? "selected" : ""}`;
    const badgeTxt = g.anticheat_status === "BLOCKED"
      ? "🛡 KHÓA AC"
      : g.is_patched
      ? `✓ ${(g.target_lang || "vi").toUpperCase()}`
      : g.has_saved_translation
      ? "💾 ĐÃ DỊCH"
      : "CHƯA DỊCH";
    const media = g.cover_url
      ? `<img class="poster-img" src="${escapeHtml(safeCoverUrl(g.cover_url))}" alt="${escapeHtml(g.title)}" />`
      : `<div class="poster-fallback"><strong>${escapeHtml(g.title)}</strong></div>`;
    card.innerHTML = `
      <span class="poster-status-badge">${escapeHtml(badgeTxt)}</span>
      <button type="button" class="poster-remove-btn" title="Bỏ game khỏi danh sách">✕</button>
      ${media}
      <div class="poster-footer"><div class="poster-title">${escapeHtml(g.title)}</div></div>
    `;
    card.querySelector(".poster-remove-btn").addEventListener("click", (ev) => removeGameFromLibrary(g, ev));
    card.addEventListener("click", () => {
      state.selectedGameId = g.game_id;
      fireSakugaVfx(card, "tap");
      switchView("game");
      renderSidebarGames();
      renderSelectedGamePage();
    });
    grid.appendChild(card);
  }

  const addCard = document.createElement("div");
  addCard.className = "poster-card poster-add-card";
  addCard.innerHTML = `<span style="font-size:28px;font-weight:700;">+</span><strong>${escapeHtml(typeof window.tUI === "function" ? window.tUI("poster_add") : "Thêm Game")}</strong>`;
  addCard.addEventListener("click", openAddGameModal);
  grid.appendChild(addCard);
}

function syncActionPackDot(pct) {
  const dot = $("actionPackDot");
  if (!dot) return;
  const p = Number(pct || 0);
  dot.className = `dot ${p >= 99.5 ? "dot-green" : p > 0 ? "dot-gold" : "dot-muted"}`;
}

async function renderSelectedGamePage() {
  const g = selectedGame();
  if (!g) {
    if ($("navGameDetailBtn")) $("navGameDetailBtn").textContent = typeof window.tUI === "function" ? window.tUI("nav_game") : "Trang Game";
    $("heroTitle").textContent = typeof window.tUI === "function" ? window.tUI("hero_empty_title") : "Chưa có game nào trong thư viện";
    $("heroSubtitle").textContent = typeof window.tUI === "function" ? window.tUI("hero_empty_sub") : "Bấm nút (+) ở góc trên trái hoặc kéo thả thư mục game vào cửa sổ để bắt đầu.";
    $("btnHeroPlay").disabled = true;
    $("btnStartTranslate").disabled = true;
    syncActionPackDot(0);
    return;
  }

  $("btnHeroPlay").disabled = false;
  $("btnFixErrors").disabled = false;
  $("btnQuickRestore").disabled = !g.has_backup;
  if ($("navGameDetailBtn")) $("navGameDetailBtn").textContent = g.title || "Trang Game";
  $("heroTitle").textContent = g.title;
  const safeCov = safeCoverUrl(g.cover_url);
  $("heroBgImage").style.backgroundImage = safeCov ? `url('${safeCov}')` : "none";
  $("ambientGameBackdrop").style.backgroundImage = safeCov ? `url('${safeCov}')` : "none";
  $("heroPosterThumb").style.backgroundImage = safeCov ? `url('${safeCov}')` : "none";

  $("heroLaunchBadge").textContent = g.launch_mode === "steam" && g.steam_appid ? `STEAM • ${g.steam_appid}` : g.launch_mode.toUpperCase();
  $("heroEngineBadge").textContent = g.engine || "Unknown Engine";
  $("studioEngineMethod").textContent = `${g.engine} • ${g.method || "--"}`;
  $("selectLaunchMode").value = g.launch_mode || "direct_exe";

  const pct = Number(g.progress_pct || 0);
  $("heroSubtitle").textContent = `${g.method || "Bảng ngôn ngữ gốc"} • ${g.total_strings || 0} câu duy nhất`;
  $("statProgressMini").textContent = `${pct}%`;
  $("statUniqueCount").textContent = g.total_strings || 0;
  syncActionPackDot(pct);

  const blocked = g.anticheat_status === "BLOCKED";
  $("heroAcBadge").textContent = blocked ? "🛡 ANTI-CHEAT BLOCKED" : "SAFE • NO ANTICHEAT";
  $("heroAcBadge").className = `badge ${blocked ? "badge-danger" : "badge-safe"}`;
  $("studioAcBanner").classList.toggle("hidden", !blocked);
  if (blocked) {
    $("studioAcDesc").textContent = `Phát hiện ${(g.anticheat_vendors || []).join(", ")}. Khóa chỉnh sửa file để tránh Ban tài khoản.`;
  }

  const ij = (!state.jobRunning && g.interrupted_job) ? g.interrupted_job : null;
  $("interruptedJobBanner")?.classList.toggle("hidden", !ij);
  if (ij && $("interruptedJobDesc")) {
    const mName = ij.mode === "REPAIR" ? "Fix lỗi & AI QA" : "Dịch AI";
    $("interruptedJobDesc").textContent = `Phiên ${mName} (${ij.source_lang || g.source_lang} ➔ ${ij.target_lang || g.target_lang}) chưa hoàn tất do tắt app đột ngột. Các câu đã dịch vẫn an toàn trong SQLite — bấm "▶ Tiếp tục phần còn lại" để chạy nối tiếp.`;
  }

  const uw = (!state.jobRunning && g.update_watch?.detected && !state.dismissedUpdateGameIds.has(g.game_id))
    ? g.update_watch
    : null;
  $("gameUpdateBanner")?.classList.toggle("hidden", !uw);
  if (uw) {
    if ($("gameUpdateTitle")) {
      $("gameUpdateTitle").textContent = uw.state === "STEAM_VERIFIED"
        ? "📦 GAME ĐÃ TRỞ VỀ BẢN GỐC (STEAM VERIFY / UPDATE)"
        : "📦 PHÁT HIỆN GAME VỪA CẬP NHẬT BẢN MỚI TRÊN STEAM";
    }
    if ($("gameUpdateDesc")) {
      $("gameUpdateDesc").textContent = uw.message || "Bấm để giữ toàn bộ bản dịch cũ, chỉ trích xuất và dịch thêm câu mới.";
    }
  }

  // Languages (restricted to Qwen 3.5:4B supported non-empty slots) & Ranked Slots
  populateTargetLangSelect();
  const allSlots = g.detected_slots || [];
  const qwenNonEmptySlots = allSlots.filter(
    (s) => Number(s.count || 0) > 0 && isQwenSupportedLang(s.slot_id)
  );
  const srcSlots = qwenNonEmptySlots.length
    ? qwenNonEmptySlots
    : allSlots.filter((s) => Number(s.count || 0) > 0).length
    ? allSlots.filter((s) => Number(s.count || 0) > 0)
    : allSlots;
  const srcSelect = $("selectSourceLang");
  srcSelect.innerHTML = "";
  for (const s of srcSlots) {
    const opt = document.createElement("option");
    opt.value = s.slot_id;
    opt.textContent = `${s.label || getLangDisplayLabel(s.slot_id)} [${s.slot_id}] (${s.count || 0})`;
    srcSelect.appendChild(opt);
  }
  const rawSrc = g.source_lang || (srcSlots[0]?.slot_id || "en");
  state.sourceLang = [...srcSelect.options].some((o) => o.value === rawSrc)
    ? rawSrc
    : (srcSlots[0]?.slot_id || "en");
  const rawTgt = g.target_lang || "vi";
  state.targetLang = isQwenSupportedLang(rawTgt) ? (normalizeQwenLangCode(rawTgt) || rawTgt) : "vi";
  if ([...srcSelect.options].some((o) => o.value === state.sourceLang)) srcSelect.value = state.sourceLang;
  $("selectTargetLang").value = state.targetLang;

  state.rankedSlots = g.ranked_slots || [];
  state.injectSlot = g.inject_slot || state.rankedSlots[0]?.slot_id || state.sourceLang;
  renderSlotGrids();
  renderGameContextCard(g);

  if (g.steam_appid && !(g.game_description || "").trim() && !state.fetchedSteamContextIds.has(g.game_id)) {
    state.fetchedSteamContextIds.add(g.game_id);
    fetchSteamContextForGame(g.game_id, true);
  }

  if (PRESET_DEFS[state.preset]) applyPresetLevel(state.preset, false);
  updateStartButtonState();
  await refreshStudioMonitor(g.game_id);
}

function autoResizeTextarea(el) {
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(180, Math.max(62, el.scrollHeight))}px`;
}

function renderGameContextCard(g) {
  if (!g) return;
  const src = state.sourceLang || g.source_lang || "en";
  const tgt = state.targetLang || g.target_lang || "vi";
  const styles = state.skills?.styles || [];
  const langs = state.skills?.languages || [];
  const activeStyleId = g.style_skill_id || g.tone_preset || "natural_standard";
  const activeStyle =
    styles.find((s) => s.id === activeStyleId || (s.aliases || []).includes(activeStyleId)) ||
    g.active_style_skill ||
    null;

  $("activeToneBadge").textContent = activeStyle
    ? `${activeStyle.icon || "🎭"} ${activeStyle.name}`
    : (TONE_LABELS[activeStyleId] || "Chuẩn Tự Nhiên");

  // Render dynamic Layer 2 Style Pills
  const bar = $("tonePresetBar");
  if (bar) {
    bar.innerHTML = "";
    const pillsData = styles.length
      ? styles
      : Object.entries(TONE_LABELS).map(([k, v]) => ({ id: k, name: v, icon: "🎭", description: v, token_count: 130 }));
    for (const sk of pillsData) {
      const btn = document.createElement("button");
      btn.type = "button";
      const isSelected = sk.id === (activeStyle?.id || activeStyleId);
      btn.className = `tone-pill${isSelected ? " active" : ""}`;
      btn.dataset.skillId = sk.id;
      btn.textContent = `${sk.icon ? sk.icon + " " : ""}${sk.name}`;
      const skTok = sk.tokens ?? sk.token_count ?? 0;
      btn.title = `${sk.description || sk.name} (~${skTok} tok)`;
      btn.addEventListener("mouseenter", () => {
        if ($("activeSkillHoverHint")) {
          $("activeSkillHoverHint").textContent = `${sk.icon || "🎭"} ${sk.name}: ${sk.description || ""} (~${skTok} tokens)`;
        }
      });
      btn.addEventListener("mouseleave", () => {
        if ($("activeSkillHoverHint") && activeStyle) {
          const aTok = activeStyle.tokens ?? activeStyle.token_count ?? 0;
          $("activeSkillHoverHint").textContent = `Đang chọn ${activeStyle.icon || "🎭"} ${activeStyle.name}: ${activeStyle.description || ""} (~${aTok} tokens)`;
        }
      });
      btn.addEventListener("click", () => {
        g.style_skill_id = sk.id;
        g.tone_preset = sk.id;
        g.compiled_style_card = "";
        if ((g.translated_strings || 0) > 0) g.skill_changed = true;
        renderGameContextCard(g);
        fireSakugaVfx(btn, "tap");
        saveGameContextPatch(g.game_id, { style_skill_id: sk.id, tone_preset: sk.id });
      });
      bar.appendChild(btn);
    }
  }
  if ($("activeSkillHoverHint") && activeStyle) {
    const aTok = activeStyle.tokens ?? activeStyle.token_count ?? 0;
    $("activeSkillHoverHint").textContent = `Đang chọn ${activeStyle.icon || "🎭"} ${activeStyle.name}: ${activeStyle.description || ""} (~${aTok} tokens)`;
  }

  // Render Layer 1 Language Bridge selector & badge
  const langOv = g.lang_skill_override || "auto";
  const matchedLang = g.active_lang_skill || langs.find((l) => l.id === langOv) || null;
  if ($("activeLangBridgeLabel")) {
    const lTok = matchedLang ? (matchedLang.tokens ?? matchedLang.token_count ?? 0) : 0;
    $("activeLangBridgeLabel").textContent = matchedLang
      ? `${matchedLang.name} (${lTok} tok)`
      : `Auto (${src.toUpperCase()} ➔ ${tgt.toUpperCase()})`;
  }
  if ($("summaryBridgePill")) {
    $("summaryBridgePill").textContent = `${src.toUpperCase()} ➔ ${tgt.toUpperCase()}`;
    $("summaryBridgePill").title = matchedLang
      ? `Cầu ngôn ngữ: ${matchedLang.name}`
      : `Cầu ngôn ngữ tự động (${src.toUpperCase()} ➔ ${tgt.toUpperCase()})`;
  }
  const bridgeSel = $("selectLangBridgeOverride");
  if (bridgeSel) {
    bridgeSel.innerHTML = "";
    const autoOpt = document.createElement("option");
    autoOpt.value = "auto";
    autoOpt.textContent = matchedLang && langOv === "auto"
      ? `Auto (${matchedLang.name})`
      : `Auto theo cặp (${src} ➔ ${tgt})`;
    bridgeSel.appendChild(autoOpt);
    for (const lsk of langs) {
      const opt = document.createElement("option");
      opt.value = lsk.id;
      opt.textContent = `${lsk.icon || "🌉"} ${lsk.name} (${lsk.language_pair || "any"})`;
      bridgeSel.appendChild(opt);
    }
    bridgeSel.value = langs.some((l) => l.id === langOv) ? langOv : "auto";
  }

  // Show Re-apply Skill banner when editing/switching Skill on an already translated game
  if ($("reapplySkillBanner")) {
    $("reapplySkillBanner").classList.toggle("hidden", !Boolean(g.skill_changed));
  }

  const ta = $("txtGameContext");
  if (ta && document.activeElement !== ta) {
    ta.value = g.game_description || "";
    autoResizeTextarea(ta);
  }
  const btnSteam = $("btnFetchSteamContext");
  if (btnSteam) btnSteam.classList.toggle("hidden", !g.steam_appid);

  const cardText = (g.compiled_style_card || "").trim();
  $("compiledStyleBadge").classList.toggle("hidden", !cardText);
  $("compiledStylePreview").textContent = cardText
    ? cardText
    : "Pha 0 sẽ tự nén thành Style Card ≤ 4 dòng (KV-Cache Prefix) khi dịch.";
}

async function saveGameContextPatch(gameId, patch) {
  try {
    const res = await api(`/api/games/${gameId}/profile`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    const g = state.games.find((x) => x.game_id === gameId);
    if (g && res.game) {
      Object.assign(g, res.game);
      if (selectedGame()?.game_id === gameId) renderGameContextCard(g);
    }
  } catch (err) {
    showToast(`Lưu bối cảnh thất bại: ${err.message}`, "error");
  }
}

async function fetchSteamContextForGame(gameId, silent = false) {
  const btn = $("btnFetchSteamContext");
  if (btn) btn.disabled = true;
  try {
    const res = await api(`/api/games/${gameId}/fetch-context`, { method: "POST" });
    if (res.ok && res.game) {
      const g = state.games.find((x) => x.game_id === gameId);
      if (g) {
        Object.assign(g, res.game);
        if (selectedGame()?.game_id === gameId) renderGameContextCard(g);
      }
      if (!silent) showToast("Đã kéo mô tả & thể loại từ Steam!");
    } else if (!silent) {
      showToast(res.message || "Không kéo được mô tả từ Steam", "error");
    }
  } catch (err) {
    if (!silent) showToast(err.message, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderSlotGrids() {
  const grids = [$("slotCardsGrid"), $("slotCardsGridQuick")].filter(Boolean);
  const list = state.rankedSlots || [];
  const activeItem = list.find((s) => s.slot_id === state.injectSlot) || list[0];
  if (activeItem) {
    state.injectSlot = activeItem.slot_id;
    $("activeSlotSummary").textContent = `${activeItem.display_name || activeItem.slot_id} (${activeItem.slot_id}) • ${activeItem.badge}`;
    $("slotGuideBox").textContent = state.uiLang === "en"
      ? `💡 In-game language guide: ${activeItem.settings_guide || ""}`
      : `💡 Hướng dẫn bật trong game: ${activeItem.settings_guide || ""}`;
  } else {
    $("activeSlotSummary").textContent = state.uiLang === "en" ? "No available slot" : "Chưa có slot khả dụng";
  }

  for (const grid of grids) {
    grid.innerHTML = "";
    for (const s of list) {
      const card = document.createElement("div");
      card.className = `slot-card ${s.slot_id === state.injectSlot ? "active" : ""}`;
      card.innerHTML = `
        <div class="slot-card-top">
          <span class="slot-name">${escapeHtml(s.display_name || s.slot_id.toUpperCase())} (${escapeHtml(s.slot_id)})</span>
          <span class="slot-badge ${s.recommended ? "rec" : "norm"}">${escapeHtml(s.badge || "SLOT")}</span>
        </div>
        <div class="slot-note">${escapeHtml(s.note || "")}</div>
      `;
      card.addEventListener("click", () => {
        state.injectSlot = s.slot_id;
        fireSakugaVfx(card, "tap");
        renderSlotGrids();
        const g = selectedGame();
        if (g) {
          g.inject_slot = s.slot_id;
          api(`/api/games/${g.game_id}/profile`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ inject_slot: s.slot_id }),
          }).catch(() => {});
        }
      });
      grid.appendChild(card);
    }
  }
}

// ---------------- 3 Presets & Slider Sync ---------------- //

function applyPresetLevel(levelKey, triggerVfx = true) {
  const def = PRESET_DEFS[levelKey];
  if (!def) return;
  state.preset = levelKey;
  state.aiMode = def.aiMode;
  state.scope = def.scope;

  document.querySelectorAll(".preset-card").forEach((c) => c.classList.toggle("active", c.dataset.preset === levelKey));
  $("activePresetBadge").textContent = typeof window.tUI === "function" ? window.tUI(`preset_badge_${levelKey}`) : def.badge;

  document.querySelectorAll("#scopeSegmented .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.scope === def.scope));
  $("subOptionsDrawer").classList.toggle("hidden", def.scope !== "SUB_ONLY");
  document.querySelectorAll("#aiModeSegmented .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.aimode === def.aiMode));

  $("chkTranslateChoices").checked = def.translateChoices;
  $("chkKeepProperNouns").checked = def.keepProperNouns;
  $("chkPatchFonts").checked = def.patchFonts;

  if (state.rankedSlots.length) {
    const rec = state.rankedSlots.find((s) => s.recommended) || state.rankedSlots[0];
    state.injectSlot = rec.slot_id;
    renderSlotGrids();
  }

  const est = state.estimates?.[levelKey];
  if (est) {
    const isEn = state.uiLang === "en";
    $("etaCalibratedLabel").textContent = isEn
      ? `Est. for tier: ${est.est_label} (${est.pending_count || 0} lines)`
      : `Ước lượng mức này: ${est.est_label} (${est.pending_count || 0} câu)`;
    if (!state.jobRunning) {
      $("teleEta").textContent = isEn ? `ETA: ${est.est_label}` : `Dự kiến: ${est.est_label}`;
      $("teleEtaMini").textContent = est.est_label;
    }
  }
  if (triggerVfx) fireSakugaVfx($(`presetCard${levelKey}`), "preset");
}

function renderPresetEstimates(estimates) {
  if (!estimates) return;
  state.estimates = estimates;
  for (const key of ["FAST", "BALANCED", "DEEP_STORY"]) {
    const e = estimates[key];
    const el = $(`estTime${key}`);
    if (e && el) el.textContent = `⏱ ${e.est_label}`;
  }
  const act = estimates[state.preset];
  if (act) {
    const isEn = state.uiLang === "en";
    $("etaCalibratedLabel").textContent = isEn
      ? `Est. for tier: ${act.est_label} (${act.pending_count || 0} lines)`
      : `Ước lượng mức đang chọn: ${act.est_label} (${act.pending_count || 0} câu)`;
    if (!state.jobRunning) {
      $("teleEta").textContent = isEn ? `ETA: ${act.est_label}` : `Dự kiến: ${act.est_label}`;
      $("teleEtaMini").textContent = act.est_label;
    }
  }
}

function markCustomPresetIfModified() {
  state.preset = "CUSTOM";
  document.querySelectorAll(".preset-card").forEach((c) => c.classList.remove("active"));
  $("activePresetBadge").textContent = typeof window.tUI === "function" ? window.tUI("preset_badge_CUSTOM") : "TÙY CHỈNH THỦ CÔNG";
}

// ---------------- Monitor & Inline Editor ---------------- //

function setPrethinkContent(text) {
  const clean = String(text || "").trim();
  const out = $("prethinkOutput");
  const det = $("prethinkDetails");
  const hint = $("prethinkSummaryHint");
  if (out) out.textContent = clean || (typeof window.tUI === "function" ? window.tUI("prethink_empty") : "Chưa có nhật ký Pre-Think.");
  if (det) det.open = Boolean(clean);
  if (hint) {
    hint.textContent = clean
      ? (typeof window.tUI === "function" ? window.tUI("prethink_ready_hint") : "• Đã phân tích ✓")
      : (typeof window.tUI === "function" ? window.tUI("prethink_collapsed_hint") : "• Thu gọn (chưa có log)");
  }
}

async function refreshStudioMonitor(gameId) {
  try {
    const mon = await api(`/api/games/${gameId}/monitor`);
    state.recentRows = mon.recent_rows || [];
    renderLiveRows(state.recentRows);
    renderGlossaryTerms(mon.glossary_terms || []);
    setPrethinkContent(mon.prethink || "");
    renderShields(mon.shields || []);
    renderPresetEstimates(mon.estimates);

    if (mon.stats && (mon.stats.raw_count || mon.stats.unique_count)) {
      $("statRawCount").textContent = mon.stats.raw_count || 0;
      $("statUniqueCount").textContent = mon.stats.unique_count || 0;
      const savedPct = Number(mon.stats.dedup_saved_pct || 0);
      $("statDedupPct").textContent = savedPct > 0 ? `(-${savedPct}%)` : "";
      $("statTmHits").textContent = mon.stats.tm_hits || 0;
      const foldedSaved = Number(mon.stats.num_folded_saved || 0);
      if ($("statTemplateFoldChip") && $("statFoldSaved")) {
        $("statFoldSaved").textContent = foldedSaved.toLocaleString("vi-VN");
        $("statTemplateFoldChip").classList.toggle("hidden", foldedSaved <= 0);
      }
    }
    if (mon.progress) {
      const pct = mon.progress.pct || 0;
      const pLbl = state.uiLang === "en" ? "Progress" : "Tiến độ";
      $("teleProgress").textContent = `${pLbl}: ${pct}% (${mon.progress.done || 0}/${mon.progress.total || 0})`;
      $("statProgressMini").textContent = `${pct}%`;
      syncActionPackDot(pct);
      const sp = $("sakugaProgressBar");
      if (sp?.set) (pct <= 0 && sp.value > 0) ? sp.reset() : sp.set(pct / 100.0, true);
    }
    renderSavedTranslations(mon.saved_translations || [], mon.cache_db_path || "", selectedGame());
  } catch (_) {}
}

function renderSavedTranslations(savedList, cacheDbPath, game) {
  const tabBtn = document.querySelector('.subtab[data-subtab="saved"]');
  if (tabBtn) {
    const baseTab = typeof window.tUI === "function" ? window.tUI("tab_saved") : "Kho bản dịch & An toàn";
    tabBtn.textContent = savedList && savedList.length ? `${baseTab} (${savedList.length})` : baseTab;
  }
  const box = $("savedTranslationsList");
  const pathLbl = $("savedCachePathLabel");
  if (pathLbl) {
    pathLbl.textContent = cacheDbPath ? cacheDbPath : `cache/${game?.game_id || "--"}.db`;
  }
  if (!box) return;
  box.innerHTML = "";
  if (!savedList || !savedList.length) {
    box.innerHTML = `<div class="empty-state">${escapeHtml(typeof window.tUI === "function" ? window.tUI("saved_empty") : "Chưa có bản dịch nào được lưu trong SQLite cho game này.")}</div>`;
    return;
  }

  for (const item of savedList) {
    const isActive = Boolean(
      game?.is_patched &&
      game.source_lang === item.source_lang &&
      game.target_lang === item.target_lang
    );
    const row = document.createElement("div");
    row.className = `saved-pack-row ${isActive ? "active-pack" : ""}`;
    const dt = item.updated_at ? new Date(item.updated_at * 1000).toLocaleString("vi-VN") : "Chưa rõ";
    const srcLbl = `${getLangDisplayLabel(item.source_lang)} [${item.source_lang}]`;
    const tgtLbl = `${getLangDisplayLabel(item.target_lang)} [${item.target_lang}]`;
    row.innerHTML = `
      <div class="saved-pack-info">
        <div class="saved-pack-title">
          <span>${escapeHtml(srcLbl)} ➔ ${escapeHtml(tgtLbl)}</span>
          ${isActive ? `<span class="saved-pack-badge">ĐANG CÀI TRONG GAME</span>` : ""}
        </div>
        <div class="saved-pack-meta">${item.done || 0}/${item.total || 0} câu (${item.pct || 0}%) • Cập nhật: ${escapeHtml(dt)}</div>
      </div>
      <div class="saved-pack-actions">
        <button type="button" class="btn btn-sm btn-outline js-export-sakugapack">📤 Xuất .sakugapack</button>
        <button type="button" class="btn btn-sm ${isActive ? "btn-outline" : "btn-outline"} js-apply-saved">
          ${isActive ? "↻ Cài lại vào game" : "✓ Chuyển sang bản này"}
        </button>
        <button type="button" class="btn btn-sm btn-danger-outline js-delete-saved">✕ Xóa</button>
      </div>
    `;
    row.querySelector(".js-export-sakugapack").addEventListener("click", () => {
      exportSakugaPack(game.game_id, item.source_lang, item.target_lang);
    });
    row.querySelector(".js-apply-saved").addEventListener("click", () => {
      applySavedTranslation(game.game_id, item.source_lang, item.target_lang);
    });
    row.querySelector(".js-delete-saved").addEventListener("click", () => {
      deleteSavedTranslation(game.game_id, item.source_lang, item.target_lang);
    });
    box.appendChild(row);
  }
}

async function applySavedTranslation(gameId, sourceLang, targetLang) {
  try {
    showToast(`Đang nạp lại bản dịch ${sourceLang} ➔ ${targetLang} từ kho lưu (không cần AI)...`);
    const res = await api(`/api/games/${gameId}/saved-translations/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_lang: sourceLang,
        target_lang: targetLang,
        inject_slot: state.injectSlot || null,
        patch_fonts: Boolean($("chkPatchFonts")?.checked),
        force_offline_override: Boolean($("chkOfflineOverride")?.checked),
      }),
    });
    await loadGames(gameId);
    const upd = res?.write_result?.updated || 0;
    showToast(`Đã cài lại ${upd} câu vào slot [${res?.game?.inject_slot || "--"}] ✓`);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function deleteSavedTranslation(gameId, sourceLang, targetLang) {
  try {
    const res = await api(
      `/api/games/${gameId}/saved-translations?source_lang=${encodeURIComponent(sourceLang)}&target_lang=${encodeURIComponent(targetLang)}`,
      { method: "DELETE" }
    );
    showToast(`Đã xóa ${res.deleted || 0} câu khỏi kho bản dịch [${sourceLang} ➔ ${targetLang}].`);
    await loadGames(gameId);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function runDeltaUpdate() {
  const g = selectedGame();
  if (!g) return;
  try {
    showToast("Đang đồng bộ bản cập nhật game & nạp lại câu cũ...");
    const res = await api(`/api/games/${g.game_id}/delta-update`, { method: "POST" });
    state.dismissedUpdateGameIds.add(g.game_id);
    $("gameUpdateBanner")?.classList.add("hidden");
    await loadGames(g.game_id);
    if ((res.pending_new || 0) > 0) {
      showToast(`Đã giữ ${res.done || 0} câu cũ & phát hiện ${res.pending_new} câu mới! Bấm ⚡ DỊCH NGAY để dịch nốt.`);
    } else {
      showToast(`Đã áp dụng lại toàn bộ ${res.done || 0} câu dịch vào game mới ✓`);
    }
  } catch (err) {
    showToast(`Lỗi đồng bộ cập nhật: ${err.message}`, "error");
  }
}

async function searchLiveStrings() {
  const g = selectedGame();
  if (!g) return;
  const q = ($("inpFilterLive")?.value ?? state.liveFilter ?? "").trim();
  const filter = $("selectLiveCategoryFilter")?.value || state.liveCategoryFilter || "ALL";
  state.liveFilter = q;
  state.liveCategoryFilter = filter;
  try {
    const res = await api(
      `/api/games/${g.game_id}/strings/search?q=${encodeURIComponent(q)}&filter=${encodeURIComponent(filter)}&limit=200`
    );
    state.recentRows = res.rows || [];
    renderLiveRows(state.recentRows);
  } catch (err) {
    showToast(`Lỗi tìm kiếm DB: ${err.message}`, "error");
  }
}

async function hotApplyCurrentTranslations() {
  const g = selectedGame();
  if (!g) return;
  try {
    showToast("Đang ghi nóng các chỉnh sửa vào file game...");
    const res = await api(`/api/games/${g.game_id}/hot-apply`, { method: "POST" });
    await loadGames(g.game_id);
    showToast(`Đã ghi nóng ${res?.write_result?.updated || 0} dòng vào game ✓`);
  } catch (err) {
    showToast(`Lỗi ghi nóng: ${err.message}`, "error");
  }
}

async function runBulkReplace(hotApply = false) {
  const g = selectedGame();
  if (!g) return;
  const findText = ($("inpBulkFind")?.value || "").trim();
  const replaceText = $("inpBulkReplace")?.value ?? "";
  if (!findText) {
    showToast("Nhập từ cần tìm trước khi thay thế.", "error");
    return;
  }
  try {
    const res = await api(`/api/games/${g.game_id}/strings/bulk-replace`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        find_text: findText,
        replace_text: replaceText,
        case_sensitive: Boolean($("chkBulkCaseSensitive")?.checked),
        category_filter: $("selectLiveCategoryFilter")?.value || "ALL",
        hot_apply: Boolean(hotApply),
      }),
    });
    await searchLiveStrings();
    if (hotApply && res.hot_applied) {
      await loadGames(g.game_id);
      showToast(`Đã thay thế ${res.replaced_count || 0} dòng & ghi nóng vào game ✓`);
    } else {
      showToast(`Đã thay thế ${res.replaced_count || 0} dòng trong DB ✓`);
    }
  } catch (err) {
    showToast(`Lỗi thay thế hàng loạt: ${err.message}`, "error");
  }
}

function exportSakugaPack(gameId, sourceLang, targetLang) {
  const url = `/api/games/${encodeURIComponent(gameId)}/sakugapack/export?source_lang=${encodeURIComponent(sourceLang)}&target_lang=${encodeURIComponent(targetLang)}`;
  window.open(url, "_blank");
  showToast(`Đang tải gói .sakugapack [${sourceLang} ➔ ${targetLang}]...`);
}

async function importSakugaPackFile(file) {
  const g = selectedGame();
  if (!g || !file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      showToast("Đang nạp gói .sakugapack & chèn vào game...");
      const res = await api(`/api/games/${g.game_id}/sakugapack/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: file.name,
          content_b64: String(reader.result || ""),
          auto_apply: true,
          inject_slot: state.injectSlot || null,
        }),
      });
      await loadGames(g.game_id);
      showToast(`Đã nhập ${res.imported_strings || 0} câu & cài xong vào game ✓`);
    } catch (err) {
      showToast(`Lỗi nhập .sakugapack: ${err.message}`, "error");
    }
  };
  reader.readAsDataURL(file);
}

function renderLiveRows(rows) {
  const fullBox = $("liveRowsBody");
  const miniBox = $("miniLivePreview");
  const q = (state.liveFilter || "").trim().toLowerCase();
  const visible = (rows || []).filter((r) => {
    if (!q) return true;
    return (
      (r.key || r.entry_key || "").toLowerCase().includes(q) ||
      (r.source || r.source_text || "").toLowerCase().includes(q) ||
      (r.translated || r.translated_text || "").toLowerCase().includes(q)
    );
  });

  if (!visible.length) {
    const msg = `<div class="empty-state">Chưa có bản dịch. Bấm <strong>⚡ DỊCH NGAY</strong> ở thanh trên để bắt đầu.</div>`;
    fullBox.innerHTML = msg;
    miniBox.innerHTML = msg;
    return;
  }

  miniBox.innerHTML = "";
  for (const r of visible.slice(0, 6)) {
    const el = document.createElement("div");
    el.className = "live-row";
    el.innerHTML = `
      <span class="live-key">${escapeHtml(r.key || r.entry_key || "")}</span>
      <div class="live-pair"><span class="live-src">${escapeHtml(r.source || r.source_text || "")}</span><span class="live-dst">➔ ${escapeHtml(r.translated || r.translated_text || "")}</span></div>
      <span class="live-badge">${escapeHtml(r.badge || r.tag_status || "OK")}</span>
    `;
    miniBox.appendChild(el);
  }

  fullBox.innerHTML = "";
  for (const r of visible.slice(0, 120)) {
    const trText = r.translated || r.translated_text || "";
    const rowEl = document.createElement("div");
    rowEl.className = "live-row";
    rowEl.innerHTML = `
      <span class="live-key">${escapeHtml(r.key || r.entry_key || "")}</span>
      <div class="live-pair">
        <span class="live-src">${escapeHtml(r.source || r.source_text || "")}</span>
        <span class="live-dst">➔ ${escapeHtml(trText)}</span>
      </div>
      <span class="live-badge">${escapeHtml(r.badge || r.tag_status || "OK")}</span>
    `;
    rowEl.addEventListener("dblclick", () => {
      if (!r.str_hash) return;
      const pair = rowEl.querySelector(".live-pair");
      const dst = pair.querySelector(".live-dst");
      if (!dst || pair.querySelector(".live-edit-input")) return;
      const inp = document.createElement("input");
      inp.className = "live-edit-input";
      inp.value = trText;
      dst.replaceWith(inp);
      inp.focus();
      inp.select();
      const save = async () => {
        const val = inp.value.trim();
        const g = selectedGame();
        if (val && val !== trText && g) {
          await api(`/api/games/${g.game_id}/edit-string`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ str_hash: r.str_hash, translated_text: val }),
          });
          r.translated_text = val;
          r.translated = val;
          r.badge = "EDITED";
          fireSakugaVfx(rowEl, "preset");
          showToast("Đã lưu chỉnh sửa");
        }
        renderLiveRows(state.recentRows);
      };
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") save();
        if (e.key === "Escape") renderLiveRows(state.recentRows);
      });
      inp.addEventListener("blur", save);
    });
    fullBox.appendChild(rowEl);
  }
}

function renderGlossaryTerms(terms) {
  const list = $("glossaryList");
  list.innerHTML = "";
  if (!terms.length) {
    list.innerHTML = `<div class="mono-sub">Chưa có thuật ngữ Glossary.</div>`;
    return;
  }
  for (const t of terms) {
    const el = document.createElement("div");
    el.className = "glossary-item";
    el.innerHTML = `<span><strong>${escapeHtml(t.source_term)}</strong> ➔ ${escapeHtml(t.target_term)}</span><button class="btn-icon">✕</button>`;
    el.querySelector("button").addEventListener("click", async () => {
      const g = selectedGame();
      if (!g) return;
      await api(`/api/games/${g.game_id}/glossary/${t.term_id}`, { method: "DELETE" });
      refreshStudioMonitor(g.game_id);
    });
    list.appendChild(el);
  }
}

function renderShields(shields) {
  const box = $("shieldsChecklist");
  box.innerHTML = "";
  for (const sh of shields) {
    const el = document.createElement("div");
    el.className = "shield-item";
    el.innerHTML = `
      <span class="dot ${sh.ok ? "dot-green" : "dot-red"}" style="margin-top:4px;"></span>
      <div><strong>${escapeHtml(sh.name)}</strong><span class="detail">${escapeHtml(sh.detail)}</span></div>
    `;
    box.appendChild(el);
  }
}

function updateStartButtonState() {
  const g = selectedGame();
  const btn = $("btnStartTranslate");
  if (!g) { btn.disabled = true; return; }
  const blockedLocked = g.anticheat_status === "BLOCKED" && !$("chkOfflineOverride").checked;
  btn.disabled = blockedLocked;
  if (blockedLocked) btn.textContent = typeof window.tUI === "function" ? window.tUI("btn_ac_locked") : "🛡 KHÓA ANTI-CHEAT";
  else if (state.jobRunning) btn.textContent = state.jobPaused ? (typeof window.tUI === "function" ? window.tUI("btn_resume") : "⚡ Tiếp tục") : (typeof window.tUI === "function" ? window.tUI("btn_pause") : "⏸ Tạm dừng");
  else btn.textContent = typeof window.tUI === "function" ? window.tUI("btn_translate") : "⚡ DỊCH NGAY";
}

async function runTranslationWithPreset(presetKey = null) {
  const g = selectedGame();
  if (!g) return;
  if (presetKey) applyPresetLevel(presetKey, true);

  // Friendly pre-flight check so first-time users know exactly how to connect Ollama / pull a model
  if (!state.ollamaStatus || !state.ollamaStatus.online || state.ollamaStatus.selected_installed === false) {
    await refreshHealth();
  }
  if (!state.ollamaStatus?.online) {
    showToast("Ollama chưa chạy! Hãy bấm 'Mở Ollama ngay' hoặc 'Tải Ollama' trong cửa sổ vừa mở.", "error");
    openHardwareModal();
    return;
  }
  if (state.ollamaStatus?.selected_installed === false) {
    const rec = getBestRecommendedModelName(state.ollamaStatus, state.hardware);
    showToast(`Model AI chưa được tải trên Ollama! Hãy bấm '⬇ Tải về' (${rec}) hoặc chọn model có sẵn.`, "error");
    openHardwareModal();
    return;
  }

  try {
    state.lastJobError = null;
    state.jobRunning = true;
    state.jobPaused = false;
    state.sessionTokens = 0;
    state.lastTokPerSec = 0;
    updateStartButtonState();
    const activeMdl = state.ollamaStatus?.main_model || state.hardware?.main_model || "AI Local";
    updateLiveTelemetryUI(null, { running: true, taskLabel: `Đang khởi chạy ${activeMdl}...`, tokensDone: 0, tokPerSec: 0 });
    $("phaseStatusLabel").textContent = "Đang khởi chạy...";
    $("sakugaProgressBar")?.reset?.();
    const est = state.estimates?.[state.preset];
    if (est?.est_seconds) startLocalEtaCountdown(est.est_seconds);

    await api("/api/translate/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        game_id: g.game_id,
        source_lang: state.sourceLang,
        target_lang: state.targetLang,
        inject_slot: state.injectSlot || state.sourceLang,
        scope: state.scope,
        ai_mode: state.aiMode,
        translate_choices: $("chkTranslateChoices").checked,
        keep_proper_nouns: $("chkKeepProperNouns").checked,
        patch_fonts: $("chkPatchFonts").checked,
        force_offline_override: $("chkOfflineOverride").checked,
        preset_level: PRESET_DEFS[state.preset] ? state.preset : null,
        style_skill_id: g.style_skill_id || g.tone_preset || "natural_standard",
        lang_skill_override: g.lang_skill_override || "auto",
      }),
    });
    showToast(`Bắt đầu dịch (${state.aiMode})`);
  } catch (err) {
    state.jobRunning = false;
    stopLocalEtaCountdown();
    recordJobError(err.message, state.uiLang === "en" ? "Start Translation" : "Khởi chạy dịch");
    updateStartButtonState();
    showToast(err.message, "error");
  }
}

async function handleStartOrPause() {
  const g = selectedGame();
  if (!g) return;
  if (state.jobRunning) {
    if (state.jobPaused) {
      await api(`/api/translate/resume/${g.game_id}`, { method: "POST" });
      state.jobPaused = false;
      $("phaseStatusLabel").textContent = "Đang tiếp tục...";
    } else {
      await api(`/api/translate/pause/${g.game_id}`, { method: "POST" });
      state.jobPaused = true;
      $("phaseStatusLabel").textContent = "Tạm dừng";
    }
    updateStartButtonState();
    return;
  }
  await runTranslationWithPreset(null);
}

async function rerankSlots() {
  const g = selectedGame();
  if (!g) return;
  const data = await api(`/api/games/${g.game_id}/slots`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_lang: state.sourceLang, target_lang: state.targetLang }),
  });
  state.rankedSlots = data.ranked_slots || [];
  if (state.rankedSlots.length) state.injectSlot = state.rankedSlots[0].slot_id;
  renderSlotGrids();
}

async function scanSelectedGame() {
  const g = selectedGame();
  if (!g) return;
  try {
    showToast("Đang quét lại bảng ngôn ngữ...");
    const res = await api(`/api/games/${g.game_id}/scan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_lang: state.sourceLang,
        target_lang: state.targetLang,
        scope: state.scope,
        translate_choices: $("chkTranslateChoices").checked,
      }),
    });
    renderPresetEstimates(res.scan_stats?.estimates);
    await loadGames(g.game_id);
    showToast("Quét hoàn tất");
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function launchGame(gameId) {
  try {
    showToast("Đang xả VRAM về 0GB...");
    const res = await api(`/api/games/${gameId}/launch`, { method: "POST" });
    $("btnHeroPlay")?.done?.();
    showToast(`Mở game qua ${res.launched_via?.toUpperCase()}`);
    refreshHealth();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function restoreGame(gameId) {
  try {
    const res = await api(`/api/games/${gameId}/restore`, { method: "POST" });
    showToast(`Đã khôi phục ${res.restored_count} file gốc! Bản dịch cũ vẫn được giữ trong Kho bản dịch đã lưu.`);
    await loadGames(gameId);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function repairGameErrors(gameId, fixFonts = true) {
  const btn = $("btnFixErrors");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Đang Fix lỗi...";
  }
  state.lastJobError = null;
  state.jobRunning = true;
  state.jobPaused = false;
  state.sessionTokens = 0;
  state.lastTokPerSec = 0;
  updateStartButtonState();
  updateLiveTelemetryUI(null, { running: true, taskLabel: "Fix lỗi • Đang quét lỗi & chạy AI QA...", tokensDone: 0, tokPerSec: 0 });
  $("phaseStatusLabel").textContent = "Fix lỗi • Đang quét...";
  $("sakugaProgressBar")?.reset?.();
  try {
    showToast("Đang quét lỗi, rà AI QA & đồng bộ catalog.bin...");
    const activeG = selectedGame();
    const res = await api(`/api/games/${gameId}/repair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        retry_with_ai: true,
        fix_fonts: fixFonts,
        force_offline_override: Boolean($("chkOfflineOverride")?.checked),
        style_skill_id: activeG?.style_skill_id || activeG?.tone_preset || "natural_standard",
        lang_skill_override: activeG?.lang_skill_override || "auto",
      }),
    });
    const list = $("repairSummaryList");
    list.innerHTML = "";
    for (const line of res.summary || []) {
      const li = document.createElement("li");
      li.textContent = line;
      list.appendChild(li);
    }
    $("repairReportBanner").classList.remove("hidden");
    fireSakugaVfx($("repairReportBanner"), "preset");
    stopLocalEtaCountdown();
    const qaTok = Number(res?.qa_polish?.tokens_done || state.sessionTokens || 0);
    updateLiveTelemetryUI(null, { running: false, done: true, taskLabel: "Fix lỗi hoàn tất ✓", tokensDone: qaTok });
    $("phaseStatusLabel").textContent = "Fix lỗi xong ✓";
    $("teleEta").textContent = "Còn lại: 00:00";
    $("teleEtaMini").textContent = "Xong ✓";
    $("sakugaProgressBar")?.set?.(1);
    await loadGames(gameId);
    showToast(res.ok ? "Đã kiểm tra & fix lỗi xong!" : "Fix hoàn tất có cảnh báo.");
  } catch (err) {
    stopLocalEtaCountdown();
    recordJobError(err.message, state.uiLang === "en" ? "Fix Errors" : "Fix lỗi");
    showToast(err.message, "error");
  } finally {
    state.jobRunning = false;
    state.jobPaused = false;
    updateStartButtonState();
    if (btn) {
      btn.disabled = false;
      btn.textContent = "🛠 Fix lỗi";
    }
  }
}

// ---------------- Tools, 2-Layer Skill Hub & Hardware Modal ---------------- //

async function loadSkillsCatalog() {
  try {
    const res = await api("/api/skills");
    state.skills = {
      languages: res.languages || [],
      styles: res.styles || [],
    };
    renderSkillsCatalog();
    const g = selectedGame();
    if (g) renderGameContextCard(g);
  } catch (_) {}
}

function estimateTokensClient(text) {
  const s = String(text || "").trim();
  if (!s) return 0;
  return Math.max(1, Math.ceil(s.length / 3.4));
}

function updateSkillTokenMeter() {
  const layer = $("selectSkillLayer")?.value || "styles";
  const limit = layer === "languages" ? 110 : 160;
  const parts = [
    $("txtSkillRules")?.value || "",
    ...[1, 2, 3].map((i) => {
      const s = ($("inpSkillExSrc" + i)?.value || "").trim();
      const t = ($("inpSkillExTgt" + i)?.value || "").trim();
      return s && t ? `${s} -> ${t}` : "";
    }),
  ].join("\n");
  const tok = estimateTokensClient(parts);
  const meter = $("skillTokenMeter");
  if (meter) {
    meter.textContent = `~${tok} / ${limit} tokens${tok > limit ? " (Hơi dài — nên rút gọn)" : " ✓"}`;
    meter.classList.toggle("token-warn", tok > limit);
  }
}

function renderSkillsCatalog() {
  const grid = $("skillsCatalogGrid");
  if (!grid) return;
  const layer = state.skillCatalogLayer || "styles";
  $("skillSubtabStyles")?.classList.toggle("active", layer === "styles");
  $("skillSubtabLangs")?.classList.toggle("active", layer === "languages");
  const list = state.skills?.[layer] || [];
  grid.innerHTML = "";
  if (!list.length) {
    grid.innerHTML = `<div class="mono-sub">Chưa có Skill nào trong lớp này.</div>`;
    return;
  }
  for (const sk of list) {
    const card = document.createElement("div");
    card.className = "skill-card";
    const limit = sk.token_limit || (layer === "languages" ? 110 : 160);
    const tokCnt = sk.tokens ?? sk.token_count ?? 0;
    const tokCls = tokCnt > limit ? "badge-danger" : "";
    const pairLabel = sk.language_pair || `${(sk.source_langs || ["*"]).join(",")}➔${(sk.target_langs || ["*"]).join(",")}`;
    card.innerHTML = `
      <div>
        <div class="skill-card-head">
          <div class="skill-card-title">${escapeHtml(sk.icon || "🎭")} ${escapeHtml(sk.name)}</div>
          <span class="badge ${tokCls}">${tokCnt} tok</span>
        </div>
        <div class="mono-mini" style="margin:3px 0 5px 0;">
          ${sk.is_builtin ? "📦 Mặc định" : "✨ Tùy biến"} • ${escapeHtml(pairLabel)}
        </div>
        <div class="skill-card-desc">${escapeHtml(sk.description || "")}</div>
      </div>
      <div class="skill-card-actions">
        <button type="button" class="btn btn-ghost btn-sm js-sk-edit">${sk.is_builtin ? "📋 Nhân bản & Sửa" : "✏ Sửa"}</button>
        <button type="button" class="btn btn-ghost btn-sm js-sk-exp-md">⬇ .md</button>
        <button type="button" class="btn btn-ghost btn-sm js-sk-exp-zip">📦 .zip</button>
        ${sk.is_builtin ? "" : '<button type="button" class="btn btn-danger-outline btn-sm js-sk-del">Xóa</button>'}
      </div>
    `;
    card.querySelector(".js-sk-edit").addEventListener("click", () => openSkillEditorModal(sk, layer, sk.is_builtin));
    card.querySelector(".js-sk-exp-md").addEventListener("click", () => {
      window.open(`/api/skills/export/${encodeURIComponent(layer)}/${encodeURIComponent(sk.id)}?fmt=md`, "_blank");
    });
    card.querySelector(".js-sk-exp-zip").addEventListener("click", () => {
      const gid = selectedGame()?.game_id || "";
      window.open(`/api/skills/export/${encodeURIComponent(layer)}/${encodeURIComponent(sk.id)}?fmt=zip${gid ? `&game_id=${encodeURIComponent(gid)}` : ""}`, "_blank");
    });
    card.querySelector(".js-sk-del")?.addEventListener("click", async () => {
      if (!confirm(`Xóa Skill "${sk.name}"?`)) return;
      try {
        await api(`/api/skills/${encodeURIComponent(layer)}/${encodeURIComponent(sk.id)}`, { method: "DELETE" });
        showToast(`Đã xóa Skill "${sk.name}"`);
        await loadSkillsCatalog();
      } catch (err) {
        showToast(err.message, "error");
      }
    });
    grid.appendChild(card);
  }
}

function openSkillEditorModal(skillObj = null, layer = "styles", cloneMode = false, focusAi = false) {
  const activeLayer = skillObj?.layer || layer || "styles";
  state.editingSkill = skillObj && !cloneMode ? { id: skillObj.id, layer: activeLayer } : null;
  $("modalSkillEditorTitle").textContent = skillObj
    ? (cloneMode ? `📋 NHÂN BẢN SKILL: ${skillObj.name}` : `✏ SỬA SKILL: ${skillObj.name}`)
    : "🧩 TẠO SKILL MỚI (NO-CODE)";
  $("inpAiSkillIdea").value = "";
  $("selectSkillLayer").value = activeLayer;
  $("inpSkillName").value = skillObj ? (cloneMode ? `${skillObj.name} (Custom)` : skillObj.name) : "";
  $("inpSkillIcon").value = skillObj?.icon || (activeLayer === "languages" ? "🌉" : "🎭");
  $("inpSkillDesc").value = skillObj?.description || "";
  const pairInit = skillObj?.language_pair || (skillObj?.source_langs || skillObj?.target_langs
    ? `${(skillObj.source_langs || ["*"]).join(",")}->${(skillObj.target_langs || ["*"]).join(",")}`
    : (activeLayer === "languages" ? `${state.sourceLang}->${state.targetLang}` : "any"));
  $("inpSkillLangPair").value = pairInit;
  const rulesText = skillObj?.rules_text ?? (Array.isArray(skillObj?.rules) ? skillObj.rules.join("\n") : (skillObj?.rules || ""));
  $("txtSkillRules").value = rulesText;
  const ex = Array.isArray(skillObj?.examples) ? skillObj.examples : [];
  for (let i = 1; i <= 3; i += 1) {
    const pair = ex[i - 1] || {};
    $("inpSkillExSrc" + i).value = pair.source || "";
    $("inpSkillExTgt" + i).value = pair.target || "";
  }
  updateSkillTokenMeter();
  $("modalSkillEditor").classList.remove("hidden");
  setTimeout(() => (focusAi ? $("inpAiSkillIdea") : $("inpSkillName"))?.focus?.(), 40);
}

async function runAiSkillGenerator() {
  const idea = ($("inpAiSkillIdea")?.value || "").trim();
  if (!idea) {
    showToast("Hãy nhập 1 câu mô tả phong cách dịch trước!", "error");
    return;
  }
  const btn = $("btnRunAiGenSkill");
  btn.disabled = true;
  btn.textContent = "⏳ Đang viết...";
  try {
    const layer = $("selectSkillLayer").value || "styles";
    const res = await api("/api/skills/ai-generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea, layer, source_lang: state.sourceLang || "en", target_lang: state.targetLang || "vi", auto_save: false }),
    });
    const sk = res.skill || {};
    if (sk.name) $("inpSkillName").value = sk.name;
    if (sk.icon) $("inpSkillIcon").value = sk.icon;
    if (sk.description) $("inpSkillDesc").value = sk.description;
    if (sk.language_pair) $("inpSkillLangPair").value = sk.language_pair;
    if (sk.rules_text) $("txtSkillRules").value = sk.rules_text;
    else if (Array.isArray(sk.rules)) $("txtSkillRules").value = sk.rules.join("\n");
    const ex = sk.examples || [];
    for (let i = 1; i <= 3; i += 1) {
      $("inpSkillExSrc" + i).value = ex[i - 1]?.source || "";
      $("inpSkillExTgt" + i).value = ex[i - 1]?.target || "";
    }
    updateSkillTokenMeter();
    showToast(res.used_ai ? "AI Local đã viết nháp Skill!" : "Đã dựng khung Skill mẫu (Ollama đang tắt).");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "✨ Nhờ AI viết nháp";
  }
}

async function saveSkillEditorForm() {
  const name = ($("inpSkillName").value || "").trim();
  if (!name) {
    showToast("Vui lòng nhập Tên Skill!", "error");
    return;
  }
  const layer = $("selectSkillLayer").value || "styles";
  const pair = ($("inpSkillLangPair").value || "any").trim() || "any";
  let srcL = "any";
  let tgtL = "any";
  if (pair.includes("->")) {
    const parts = pair.split("->");
    srcL = (parts[0] || "any").trim();
    tgtL = (parts[1] || "any").trim();
  }
  const examples = [];
  for (let i = 1; i <= 3; i += 1) {
    const s = ($("inpSkillExSrc" + i).value || "").trim();
    const t = ($("inpSkillExTgt" + i).value || "").trim();
    if (s && t) examples.push({ source: s, target: t });
  }
  try {
    const payload = {
      id: state.editingSkill?.id || null,
      layer,
      name,
      icon: ($("inpSkillIcon").value || "🎭").trim() || "🎭",
      description: ($("inpSkillDesc").value || "").trim(),
      language_pair: pair,
      source_lang: srcL,
      target_lang: tgtL,
      rules: ($("txtSkillRules").value || "").split("\n").map((x) => x.trim()).filter(Boolean),
      examples,
    };
    const res = await api("/api/skills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("btnSaveSkill")?.done?.();
    $("modalSkillEditor").classList.add("hidden");
    state.skillCatalogLayer = layer;
    await loadSkillsCatalog();
    const g = selectedGame();
    if (g && layer === "styles" && res.skill?.id) {
      g.style_skill_id = res.skill.id;
      g.tone_preset = res.skill.id;
      if ((g.translated_strings || 0) > 0) g.skill_changed = true;
      await saveGameContextPatch(g.game_id, { style_skill_id: res.skill.id, tone_preset: res.skill.id });
    }
    showToast(`Đã lưu Skill "${res.skill?.name || name}"!`);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function reapplyGameSkill(scope) {
  const g = selectedGame();
  if (!g) return;
  try {
    state.lastJobError = null;
    state.jobRunning = true;
    state.jobPaused = false;
    updateStartButtonState();
    const res = await api(`/api/games/${g.game_id}/reapply-skill`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        style_skill_id: g.style_skill_id || g.tone_preset || "natural_standard",
        lang_skill_override: g.lang_skill_override || "auto",
        scope,
        keep_manual_locked: true,
        auto_start: true,
      }),
    });
    g.skill_changed = false;
    $("reapplySkillBanner")?.classList.add("hidden");
    if (res.started) {
      showToast(`Đang áp dụng Skill mới cho ${res.reset_count || 0} câu...`);
    } else {
      state.jobRunning = false;
      updateStartButtonState();
      showToast("Đã cập nhật Skill (không có câu nào cần dịch lại).");
    }
  } catch (err) {
    state.jobRunning = false;
    recordJobError(err.message, state.uiLang === "en" ? "Re-apply Skill" : "Áp dụng lại Skill");
    updateStartButtonState();
    showToast(err.message, "error");
  }
}

async function openHardwareModal() {
  $("modalHardwareSettings").classList.remove("hidden");
  $("hwDetectedSummary").textContent = "Đang kiểm tra GPU VRAM / RAM & danh sách Model Ollama...";
  try {
    const [hwRes, mdlRes] = await Promise.all([api("/api/system/hardware"), api("/api/ollama/models")]);
    const hwEff = hwRes.hardware || {};
    state.hardware = hwEff;
    state.ollamaStatus = mdlRes || {};
    const probe = hwEff.hardware || {};
    $("hwDetectedSummary").innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
        <span>🖥️ <strong>${escapeHtml(probe.gpu_name || "CPU Mode")}</strong> • <strong>${probe.vram_total_gb || 0} GB VRAM</strong> • RAM <strong>${probe.ram_total_gb || 0} GB</strong></span>
        <span class="badge badge-safe">${escapeHtml(probe.tier_label || hwEff.tier)} (${hwEff.parallel_workers || 4} luồng)</span>
      </div>
    `;

    const warnTxt = hwEff.ollama_env_warning || mdlRes.ollama_env_warning || "";
    $("ollamaEnvWarnBox").classList.toggle("hidden", !warnTxt);
    $("ollamaEnvWarnText").textContent = warnTxt;
    syncOllamaOnboardingBanner(mdlRes, hwEff);

    const installed = mdlRes.models_meta || [];
    const mainSel = $("selectMainModel");
    const uiSel = $("selectUiFastModel");
    mainSel.innerHTML = "";
    uiSel.innerHTML = "";

    const seenNames = new Set();
    for (const m of installed) {
      seenNames.add(m.name);
      const label = `${m.name} (${m.size_gb} GB${m.supports_thinking ? " • Thinking" : ""})`;
      mainSel.add(new Option(label, m.name));
      uiSel.add(new Option(label, m.name));
    }
    if (hwEff.main_model && !seenNames.has(hwEff.main_model)) {
      mainSel.add(new Option(`${hwEff.main_model} (Chưa cài local)`, hwEff.main_model));
    }
    mainSel.value = hwEff.main_model || (installed[0]?.name || "qwen3.5:4b");
    if (hwEff.ui_fast_model && !seenNames.has(hwEff.ui_fast_model)) {
      uiSel.add(new Option(hwEff.ui_fast_model, hwEff.ui_fast_model));
    }
    uiSel.value = hwEff.ui_fast_model || (installed[0]?.name || mainSel.value);

    const recSel = $("selectRecommendModel");
    recSel.innerHTML = "";
    for (const r of mdlRes.recommended_models || probe.recommended_models || []) {
      recSel.add(new Option(`${r.name} — ${r.desc || ""} (~${r.size_gb || 0} GB)`, r.name));
    }

    $("chkDualModel").checked = Boolean(hwEff.dual_model_enabled);
    $("dualModelRow").classList.toggle("hidden", !Boolean(hwEff.dual_model_enabled));
    $("chkAutoTuneHw").checked = hwEff.auto_tune !== false;
    if ($("chkQuietModeHw")) $("chkQuietModeHw").checked = Boolean(hwEff.quiet_mode);
    if ($("hwAdvancedDetails")) $("hwAdvancedDetails").open = hwEff.auto_tune === false;
    $("sliderParallelWorkers").value = hwEff.parallel_workers || 4;
    $("valParallelWorkers").textContent = String(hwEff.parallel_workers || 4);
    $("selectNumCtx").value = String(hwEff.num_ctx || 2048);
    $("inpOllamaBaseUrl").value = hwEff.ollama_base_url || "http://localhost:11434";
  } catch (err) {
    $("hwDetectedSummary").textContent = `Không đọc được thông số: ${err.message}`;
  }
}

async function pullModelAndAutoLink(modelName, fromBanner = false) {
  const mdl = (modelName || "").trim();
  if (!mdl) return;
  const btnModal = $("btnPullModel");
  const btnBanner = $("btnQuickPullRecommended");
  if (btnModal) btnModal.disabled = true;
  if (btnBanner) btnBanner.disabled = true;

  if ($("pullProgressWrap")) $("pullProgressWrap").classList.remove("hidden");
  if ($("pullProgressBar")) $("pullProgressBar").style.width = "4%";
  if ($("pullProgressText")) $("pullProgressText").textContent = `Đang bắt đầu tải ${mdl}...`;

  if (fromBanner && $("bannerPullProgressWrap")) {
    $("bannerPullProgressWrap").classList.remove("hidden");
    if ($("bannerPullProgressBar")) $("bannerPullProgressBar").style.width = "4%";
    if ($("bannerPullProgressText")) $("bannerPullProgressText").textContent = `Đang tải ${mdl} về Ollama Local...`;
  }

  try {
    const pullRes = await api("/api/ollama/pull", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: mdl }),
    });

    // Auto-link downloaded model immediately so beginners never forget to click "Lưu cấu hình AI"
    const isSmallUiHelper = /(?:^|:)(?:0\.\d+b|1\.[0-9]b|2b)(?:\b|$)/i.test(mdl);
    const hasExistingMain = Boolean(state.ollamaStatus?.selected_installed);
    const dualChecked = Boolean($("chkDualModel")?.checked || state.hardware?.dual_model_enabled);
    const linkPayload = (isSmallUiHelper && hasExistingMain && dualChecked && !fromBanner)
      ? { ui_fast_model: mdl }
      : { main_model: mdl };

    const saveRes = await api("/api/system/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(linkPayload),
    });
    if (saveRes.hardware) state.hardware = saveRes.hardware;
    if (saveRes.ollama) state.ollamaStatus = saveRes.ollama;
    else if (pullRes.ollama) state.ollamaStatus = pullRes.ollama;

    showToast(
      linkPayload.main_model
        ? `Đã tải xong & tự kết nối Model chính ${mdl} ✓ Sẵn sàng dịch ngay!`
        : `Đã tải xong & gắn Model UI nhanh ${mdl} ✓`
    );
    await refreshHealth();
    if (!$("modalHardwareSettings")?.classList.contains("hidden")) {
      await openHardwareModal();
    }
    const g = selectedGame();
    if (g) refreshStudioMonitor(g.game_id);
  } catch (err) {
    if ($("pullProgressText")) $("pullProgressText").textContent = `Lỗi: ${err.message}`;
    if ($("bannerPullProgressText")) $("bannerPullProgressText").textContent = `Lỗi: ${err.message}`;
    showToast(err.message, "error");
  } finally {
    if (btnModal) btnModal.disabled = false;
    if (btnBanner) btnBanner.disabled = false;
    if ($("bannerPullProgressWrap")) $("bannerPullProgressWrap").classList.add("hidden");
  }
}

async function pullRecommendedModel() {
  const mdl = ($("selectRecommendModel")?.value || "").trim();
  await pullModelAndAutoLink(mdl, false);
}

async function saveHardwareSettings() {
  try {
    const payload = {
      auto_tune: $("chkAutoTuneHw").checked,
      quiet_mode: Boolean($("chkQuietModeHw")?.checked),
      main_model: $("selectMainModel").value,
      dual_model_enabled: $("chkDualModel").checked,
      ui_fast_model: $("selectUiFastModel").value,
      parallel_workers: Number($("sliderParallelWorkers").value || 4),
      num_ctx: Number($("selectNumCtx").value || 2048),
      ollama_base_url: ($("inpOllamaBaseUrl").value || "http://localhost:11434").trim(),
    };
    const res = await api("/api/system/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.hardware = res.hardware;
    state.ollamaStatus = res.ollama;
    $("btnSaveHwSettings")?.done?.();
    $("modalHardwareSettings").classList.add("hidden");
    await refreshHealth();
    const g = selectedGame();
    if (g) refreshStudioMonitor(g.game_id);
    showToast(`Đã lưu cấu hình AI (${payload.main_model} • ${res.hardware?.parallel_workers || payload.parallel_workers} luồng)!`);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function loadToolsAndTM() {
  const tmQ = $("tmSearchInput")?.value || "";
  const [tools, tm] = await Promise.all([
    api("/api/tools/status"),
    api(`/api/tm?q=${encodeURIComponent(tmQ)}`),
    loadSkillsCatalog(),
  ]);
  const list = $("toolsCardsList");
  list.innerHTML = "";
  for (const t of Object.values(tools.tools || {})) {
    const item = document.createElement("div");
    item.className = "tool-item";
    item.innerHTML = `
      <div style="display:flex;justify-content:space-between;"><strong>${escapeHtml(t.name)}</strong><span class="dot ${t.installed ? "dot-green" : "dot-gold"}"></span></div>
      <div class="mono-mini">${escapeHtml(t.repo)} • ${t.installed ? "Đã cài" : "Chưa tải"}</div>
    `;
    list.appendChild(item);
  }
  $("tmTotalCount").textContent = tm.stats?.count || 0;
  const tmList = $("tmEntriesList");
  tmList.innerHTML = "";
  for (const row of tm.entries || []) {
    const rEl = document.createElement("div");
    rEl.className = "tm-row";
    rEl.innerHTML = `
      <span class="mono-mini">${escapeHtml(row.source_lang)}➔${escapeHtml(row.target_lang)}</span>
      <span>${escapeHtml(row.source_text)}</span>
      <span>${escapeHtml(row.translated_text)}</span>
      <button class="btn btn-ghost btn-sm">Xóa</button>
    `;
    rEl.querySelector("button").addEventListener("click", async () => {
      await api(`/api/tm/${row.id}`, { method: "DELETE" });
      loadToolsAndTM();
    });
    tmList.appendChild(rEl);
  }
}

// ---------------- Modals & Drag-Drop ---------------- //

async function openAddGameModal() {
  $("modalAddGame").classList.remove("hidden");
  await refreshSteamModalList();
}

async function refreshSteamModalList() {
  const box = $("steamAppsList");
  box.innerHTML = `<div class="mono-sub">Đang đọc Steam libraryfolders.vdf...</div>`;
  try {
    const data = await api("/api/steam/discover");
    box.innerHTML = "";
    for (const a of data.apps || []) {
      const normDir = String(a.game_dir || "").toLowerCase();
      const existing = state.games.find(
        (g) => String(g.game_dir || "").toLowerCase() === normDir || (a.steam_appid && g.steam_appid === a.steam_appid)
      );
      const added = Boolean(a.already_added || existing);
      const row = document.createElement("div");
      row.className = "steam-app-row";
      row.innerHTML = `
        <div><strong>${escapeHtml(a.title)}</strong><div class="mono-mini">AppID ${a.steam_appid} • ${escapeHtml(a.game_dir)}</div></div>
        <button class="btn ${added ? "btn-danger-outline" : "btn-outline"} btn-sm">${added ? "− Bỏ" : "+ Nạp"}</button>
      `;
      row.querySelector("button").addEventListener("click", () => {
        if (added && existing) removeGameFromLibrary(existing);
        else addGameByPath(a.game_dir);
      });
      box.appendChild(row);
    }
  } catch (err) {
    box.innerHTML = `<div class="mono-sub">${escapeHtml(err.message || "Lỗi không xác định")}</div>`;
  }
}

async function addGameByPath(path) {
  if (!path || !path.trim()) return;
  try {
    showToast("Đang nạp game...");
    const res = await api("/api/games/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path.trim() }),
    });
    $("btnSubmitGamePath")?.done?.();
    $("modalAddGame").classList.add("hidden");
    $("inpGamePath").value = "";
    switchView("game");
    await loadGames(res.game.game_id);
    showToast(`Đã thêm ${res.game.title}`);
  } catch (err) {
    showToast(err.message, "error");
  }
}

window.onSakugaNativeDrop = (paths) => {
  $("globalDropOverlay").classList.add("hidden");
  if (paths && paths.length) addGameByPath(paths[0]);
};

function setupDragDropOverlay() {
  let d = 0;
  window.addEventListener("dragenter", (e) => { e.preventDefault(); d += 1; $("globalDropOverlay").classList.remove("hidden"); });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("dragleave", () => { d = Math.max(0, d - 1); if (d === 0) $("globalDropOverlay").classList.add("hidden"); });
  window.addEventListener("drop", (e) => { e.preventDefault(); d = 0; $("globalDropOverlay").classList.add("hidden"); });
}

function setupSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      const { event, data } = msg;
      const g = selectedGame();
      if ((event === "connected" || event === "ping" || event === "vram_flushed") && data.telemetry) {
        updateLiveTelemetryUI(data.telemetry);
      }
      if (event === "ollama_pull_progress") {
        const pct = Number(data.pct || 0);
        const widthStr = `${Math.min(100, Math.max(2, pct))}%`;
        const msgStr = data.error
          ? `Lỗi: ${data.error}`
          : `${data.model || ""}: ${data.status || ""} (${pct.toFixed(1)}%)`;
        if ($("pullProgressWrap")) $("pullProgressWrap").classList.remove("hidden");
        if ($("pullProgressBar")) $("pullProgressBar").style.width = widthStr;
        if ($("pullProgressText")) $("pullProgressText").textContent = msgStr;
        if ($("bannerPullProgressWrap") && !$("ollamaSetupBanner")?.classList.contains("hidden")) {
          $("bannerPullProgressWrap").classList.remove("hidden");
          if ($("bannerPullProgressBar")) $("bannerPullProgressBar").style.width = widthStr;
          if ($("bannerPullProgressText")) $("bannerPullProgressText").textContent = msgStr;
        }
        return;
      }
      if (event === "tool_status") { refreshHealth(); if (state.view === "tools") loadToolsAndTM(); }
      if (event === "app_update_progress") {
        const wrap = $("appUpdateProgressWrap");
        const bar = $("appUpdateProgressBar");
        const txt = $("appUpdateProgressText");
        const btn = $("btnApplyAppUpdate");
        const pct = Math.max(0, Math.min(100, Number(data.pct || 0)));
        const isEn = state.uiLang === "en";
        if (wrap) wrap.classList.remove("hidden");
        if (bar) bar.style.width = `${pct}%`;
        if (data.stage === "downloading") {
          const dlStr = data.total_mb ? `${data.downloaded_mb}/${data.total_mb} MB` : `${data.downloaded_mb || 0} MB`;
          if (txt) txt.textContent = isEn ? `Downloading release (${pct}% • ${dlStr})...` : `Đang tải bản cập nhật (${pct}% • ${dlStr})...`;
        } else if (data.stage === "extracting") {
          if (txt) txt.textContent = data.message || (isEn ? "Extracting & verifying ZIP..." : "Đang giải nén & kiểm tra gói...");
        } else if (data.stage === "restarting") {
          if (txt) txt.textContent = data.message || (isEn ? "Applying update & restarting app..." : "Đang áp dụng bản mới & khởi động lại...");
          showToast(isEn ? "Update downloaded! Restarting Black Sakura Translate..." : "Đã tải xong bản mới! App đang tự khởi động lại...");
        } else if (data.stage === "done_restart_needed") {
          if (txt) txt.textContent = data.message || (isEn ? "Updated! Restart app to apply." : "Đã cập nhật! Hãy khởi động lại app.");
        } else if (data.stage === "error") {
          if (txt) txt.textContent = `❌ ${data.message || "Update failed"}`;
          showToast(data.message || "Lỗi cập nhật", "error");
          if (btn) btn.disabled = false;
        }
        return;
      }
      if (!g || (data.game_id && data.game_id !== g.game_id)) return;

      if (event === "live_rows" && data.rows) {
        state.recentRows = [...data.rows, ...state.recentRows].slice(0, 140);
        renderLiveRows(state.recentRows);
      }
      if (event === "prethink_done") {
        setPrethinkContent(data.thinking || "");
        renderGlossaryTerms(data.terms || []);
        if (data.style_card) {
          g.compiled_style_card = data.style_card;
          renderGameContextCard(g);
        }
      }
      if (event === "progress") {
        const vp = data.weighted_pct ?? data.pct ?? 0;
        const tps = Number(data.tok_per_sec ?? 0);
        const toks = typeof data.tokens_done === "number" ? data.tokens_done : state.sessionTokens;
        if (data.done !== undefined && data.total !== undefined) {
          $("teleProgress").textContent = `Tiến độ: ${data.pct || 0}% (${data.done || 0}/${data.total || 0})`;
          $("statProgressMini").textContent = `${data.pct || 0}%`;
          syncActionPackDot(data.pct || 0);
        }
        updateLiveTelemetryUI(data.telemetry || null, {
          running: true,
          tokensDone: toks,
          tokPerSec: tps,
          taskLabel: data.phase_label || "Đang xử lý AI...",
          activeModel: data.active_model,
          activeModelRole: data.active_model_role,
          dualModelActive: data.dual_model_active,
        });
        if (data.phase_label) {
          $("phaseStatusLabel").textContent = data.phase_label;
          $("etaCalibratedLabel").textContent = data.qa_total
            ? `AI QA: ${data.qa_done || 0} / ${data.qa_total} gói`
            : data.chars_total
            ? `${(data.chars_done || 0).toLocaleString()} / ${(data.chars_total || 0).toLocaleString()} ký tự`
            : "Đang xử lý...";
        }
        if (data.eta_seconds !== null && data.eta_seconds !== undefined) syncLocalEtaFromServer(data.eta_seconds);
        $("sakugaProgressBar")?.set?.(Math.min(0.99, vp / 100.0));
      }
      if (event === "saved_translation_applied" || event === "game_restored") {
        loadGames(g.game_id);
      }
      if (event === "paused") {
        state.jobPaused = Boolean(data.paused);
        updateStartButtonState();
      }
      if (event === "repair_complete") {
        stopLocalEtaCountdown();
        updateLiveTelemetryUI(data.telemetry || null, {
          running: false,
          done: true,
          tokensDone: typeof data.tokens_done === "number" ? data.tokens_done : state.sessionTokens,
          taskLabel: "Fix lỗi hoàn tất ✓",
        });
        $("phaseStatusLabel").textContent = "Fix lỗi xong ✓";
        $("sakugaProgressBar")?.set?.(1);
      }
      if (event === "complete") {
        state.jobRunning = false;
        state.jobPaused = false;
        stopLocalEtaCountdown();
        $("teleEta").textContent = "Còn lại: 00:00";
        $("teleEtaMini").textContent = "Xong ✓";
        $("phaseStatusLabel").textContent = "Hoàn tất 100%!";
        syncActionPackDot(100);
        updateLiveTelemetryUI(null, { running: false, done: true, taskLabel: "Hoàn tất 100% ✓" });
        $("sakugaProgressBar")?.set?.(1);
        $("btnStartTranslate")?.done?.(0);
        updateStartButtonState();
        showToast("Hoàn tất dịch & chèn vào game!");
        loadGames(g.game_id);
      }
      if (event === "error") {
        state.jobRunning = false;
        stopLocalEtaCountdown();
        recordJobError(data.message || "Lỗi không xác định");
        updateStartButtonState();
        showToast(`Lỗi dịch: ${data.message}`, "error");
      }
    } catch (_) {}
  };
}

function safeCoverUrl(u) {
  const s = String(u || "").trim();
  if (!s) return "";
  if (s.startsWith("/covers/") || /^https?:\/\//i.test(s)) {
    return s.replaceAll("'", "%27").replaceAll('"', "%22").replaceAll(")", "%29");
  }
  return "";
}

function escapeHtml(s) {
  return String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

function bindEvents() {
  document.querySelectorAll(".top-link").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
  $("btnSidebarHome").addEventListener("click", () => switchView("home"));
  document.querySelectorAll(".subtab").forEach((t) => t.addEventListener("click", () => switchSubtab(t.dataset.subtab)));
  document.querySelectorAll(".swatch").forEach((sw) => sw.addEventListener("click", () => applyTheme(sw.dataset.themePick, sw)));
  document.querySelectorAll("#uiLangSwitcher .lang-btn").forEach((btn) => btn.addEventListener("click", () => applyUiLanguage(btn.dataset.langPick, btn)));

  $("globalSearchInput").addEventListener("input", (e) => {
    state.search = e.target.value || "";
    renderSidebarGames();
    renderPosterGrid();
  });
  $("inpFilterLive").addEventListener("input", (e) => {
    state.liveFilter = e.target.value || "";
    clearTimeout(state.liveSearchTimer);
    state.liveSearchTimer = setTimeout(() => searchLiveStrings(), 240);
  });
  $("selectLiveCategoryFilter")?.addEventListener("change", () => searchLiveStrings());
  $("btnToggleBulkReplace")?.addEventListener("click", () => {
    $("bulkReplaceDrawer")?.classList.toggle("hidden");
  });
  $("btnHotApplyLive")?.addEventListener("click", hotApplyCurrentTranslations);
  $("btnRunBulkReplace")?.addEventListener("click", () => runBulkReplace(false));
  $("btnRunBulkReplaceAndApply")?.addEventListener("click", () => runBulkReplace(true));
  $("btnImportSakugaPack")?.addEventListener("click", () => $("inpImportSakugaPackFile")?.click());
  $("inpImportSakugaPackFile")?.addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    if (file) await importSakugaPackFile(file);
    e.target.value = "";
  });
  $("btnRunDeltaUpdate")?.addEventListener("click", runDeltaUpdate);
  $("btnDismissUpdateBanner")?.addEventListener("click", () => {
    const g = selectedGame();
    if (g) state.dismissedUpdateGameIds.add(g.game_id);
    $("gameUpdateBanner")?.classList.add("hidden");
  });

  document.querySelectorAll(".sb-pill").forEach((p) => {
    p.addEventListener("click", () => {
      document.querySelectorAll(".sb-pill").forEach((x) => x.classList.remove("active"));
      p.classList.add("active");
      state.filter = p.dataset.filter;
      renderSidebarGames();
      renderPosterGrid();
    });
  });

  $("btnOpenAddModal").addEventListener("click", openAddGameModal);
  $("btnCloseAddModal").addEventListener("click", () => $("modalAddGame").classList.add("hidden"));
  $("btnSubmitGamePath").addEventListener("click", () => addGameByPath($("inpGamePath").value));
  $("btnRefreshSteamModal").addEventListener("click", refreshSteamModalList);
  $("btnDiscoverSteam").addEventListener("click", openAddGameModal);

  $("btnPickFolder").addEventListener("click", async () => {
    const r = await api("/api/ui/pick-folder", { method: "POST" });
    if (r.path) addGameByPath(r.path);
  });
  $("btnPickFile").addEventListener("click", async () => {
    const r = await api("/api/ui/pick-file", { method: "POST" });
    if (r.path) addGameByPath(r.path);
  });

  $("btnHeroPlay").addEventListener("click", () => selectedGame() && launchGame(selectedGame().game_id));
  $("btnFixErrors").addEventListener("click", () => selectedGame() && repairGameErrors(selectedGame().game_id, Boolean($("chkPatchFonts")?.checked ?? true)));
  $("btnFixFontsQuick").addEventListener("click", () => selectedGame() && repairGameErrors(selectedGame().game_id, true));
  $("btnCloseRepairBanner").addEventListener("click", () => $("repairReportBanner").classList.add("hidden"));
  $("btnResumeInterrupted")?.addEventListener("click", () => {
    const g = selectedGame();
    if (!g || !g.interrupted_job) return;
    $("interruptedJobBanner")?.classList.add("hidden");
    if (g.interrupted_job.mode === "REPAIR") {
      repairGameErrors(g.game_id, Boolean($("chkPatchFonts")?.checked ?? true));
    } else {
      runTranslationWithPreset(null);
    }
  });
  $("btnDismissInterrupted")?.addEventListener("click", async () => {
    const g = selectedGame();
    if (!g) return;
    $("interruptedJobBanner")?.classList.add("hidden");
    g.interrupted_job = null;
    await api(`/api/games/${g.game_id}/interrupted-job`, { method: "DELETE" }).catch(() => {});
  });
  $("btnHideToTray")?.addEventListener("click", () => {
    api("/api/ui/hide-to-tray", { method: "POST" }).catch(() => {});
  });
  $("chkMinimizeToTray")?.addEventListener("change", (e) => {
    api("/api/ui/minimize-to-tray", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: Boolean(e.target.checked) }),
    }).catch(() => {});
  });
  $("btnQuickRestore").addEventListener("click", () => selectedGame() && restoreGame(selectedGame().game_id));
  $("btnStartTranslate").addEventListener("click", handleStartOrPause);

  // Settings navigation shortcuts from Overview / Action strip
  $("btnOpenSavedQuick")?.addEventListener("click", () => switchSubtab("saved"));
  $("btnOpenSettingsDrawer").addEventListener("click", () => switchSubtab(state.subtab === "settings" ? "overview" : "settings"));
  $("btnLinkToFullSettings").addEventListener("click", () => switchSubtab("settings"));
  $("btnViewAllLive").addEventListener("click", () => switchSubtab("live"));
  $("btnToggleSlotDrawer").addEventListener("click", () => $("inlineSlotCollapse").classList.toggle("hidden"));

  // Collapsible Game Context & Style Card events
  $("btnToggleContextDrawer").addEventListener("click", () => {
    const body = $("contextDrawerBody");
    const box = body.closest(".context-style-box");
    const isHidden = body.classList.toggle("hidden");
    box?.classList.toggle("collapsed", isHidden);
    $("contextChevron").textContent = isHidden ? "▸" : "▾";
    if (!isHidden && $("txtGameContext")) {
      autoResizeTextarea($("txtGameContext"));
    }
  });
  $("btnFetchSteamContext").addEventListener("click", (e) => {
    e.stopPropagation();
    const g = selectedGame();
    if (g) fetchSteamContextForGame(g.game_id, false);
  });
  $("selectLangBridgeOverride")?.addEventListener("change", (e) => {
    const g = selectedGame();
    if (!g) return;
    g.lang_skill_override = e.target.value || "auto";
    g.compiled_style_card = "";
    if ((g.translated_strings || 0) > 0) g.skill_changed = true;
    saveGameContextPatch(g.game_id, { lang_skill_override: g.lang_skill_override });
    renderGameContextCard(g);
  });
  $("btnQuickCreateSkill")?.addEventListener("click", () => openSkillEditorModal(null, "styles", false, false));
  $("btnReapplyFlagged")?.addEventListener("click", () => reapplyGameSkill("flagged_only"));
  $("btnReapplySub")?.addEventListener("click", () => reapplyGameSkill("sub_only"));
  $("btnReapplyAll")?.addEventListener("click", () => reapplyGameSkill("all_unlocked"));

  // Topbar AI Model & Hardware Profiler modal triggers + 1-Click Onboarding Banner
  $("ollamaStatusPill")?.addEventListener("click", openHardwareModal);
  $("ollamaStatusPill")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openHardwareModal(); }
  });
  $("btnDownloadOllama")?.addEventListener("click", openOllamaDownloadPage);
  $("btnModalDownloadOllama")?.addEventListener("click", openOllamaDownloadPage);
  $("btnLaunchOllama")?.addEventListener("click", (e) => launchOllamaFromUI(e.currentTarget));
  $("btnModalLaunchOllama")?.addEventListener("click", (e) => launchOllamaFromUI(e.currentTarget));
  $("btnRecheckOllama")?.addEventListener("click", async () => {
    await refreshHealth();
    showToast(state.ollamaStatus?.online ? "Ollama đã kết nối ✓" : "Vẫn chưa thấy Ollama chạy.");
  });
  $("btnQuickPullRecommended")?.addEventListener("click", (e) => {
    const mdl = e.currentTarget?.dataset?.model || getBestRecommendedModelName(state.ollamaStatus, state.hardware);
    pullModelAndAutoLink(mdl, true);
  });
  $("btnOpenHwFromBanner")?.addEventListener("click", openHardwareModal);
  $("btnCloseHwModal")?.addEventListener("click", () => $("modalHardwareSettings").classList.add("hidden"));
  $("btnConfigDualModels")?.addEventListener("click", openHardwareModal);
  $("chkDualModelQuick")?.addEventListener("change", (e) => toggleDualModel(e.target.checked));
  $("chkDualModelSettings")?.addEventListener("change", (e) => toggleDualModel(e.target.checked));
  $("chkDualModel")?.addEventListener("change", (e) => $("dualModelRow")?.classList.toggle("hidden", !e.target.checked));
  $("chkAutoTuneHw")?.addEventListener("change", (e) => {
    if ($("hwAdvancedDetails")) $("hwAdvancedDetails").open = !e.target.checked;
  });
  $("sliderParallelWorkers")?.addEventListener("input", (e) => {
    if ($("valParallelWorkers")) $("valParallelWorkers").textContent = e.target.value;
    if ($("chkAutoTuneHw")) $("chkAutoTuneHw").checked = false;
  });
  $("selectNumCtx")?.addEventListener("change", () => {
    if ($("chkAutoTuneHw")) $("chkAutoTuneHw").checked = false;
  });
  $("btnPullModel")?.addEventListener("click", pullRecommendedModel);
  $("btnSaveHwSettings")?.addEventListener("click", saveHardwareSettings);
  $("btnCopyEnvFix")?.addEventListener("click", () => {
    const workers = state.hardware?.hardware?.parallel_workers || 4;
    const kv = state.hardware?.hardware?.kv_cache_type || "q8_0";
    const cmd = `setx OLLAMA_NUM_PARALLEL ${workers} && setx OLLAMA_KV_CACHE_TYPE ${kv} && setx OLLAMA_FLASH_ATTENTION 1`;
    navigator.clipboard?.writeText(cmd);
    showToast("Đã copy lệnh CMD thiết lập Ollama Env (khởi động lại Ollama sau khi chạy)!");
  });

  // 2-Layer Skill Hub Catalog & No-Code Skill Editor modal triggers
  $("skillSubtabStyles")?.addEventListener("click", () => { state.skillCatalogLayer = "styles"; renderSkillsCatalog(); });
  $("skillSubtabLangs")?.addEventListener("click", () => { state.skillCatalogLayer = "languages"; renderSkillsCatalog(); });
  $("btnNewSkill")?.addEventListener("click", () => openSkillEditorModal(null, state.skillCatalogLayer || "styles", false, false));
  $("btnAiGenSkillOpen")?.addEventListener("click", () => openSkillEditorModal(null, state.skillCatalogLayer || "styles", false, true));
  $("btnCloseSkillModal")?.addEventListener("click", () => $("modalSkillEditor").classList.add("hidden"));
  $("btnRunAiGenSkill")?.addEventListener("click", runAiSkillGenerator);
  $("btnSaveSkill")?.addEventListener("click", saveSkillEditorForm);
  ["selectSkillLayer", "txtSkillRules", "inpSkillExSrc1", "inpSkillExTgt1", "inpSkillExSrc2", "inpSkillExTgt2", "inpSkillExSrc3", "inpSkillExTgt3"].forEach((id) => {
    $(id)?.addEventListener("input", updateSkillTokenMeter);
    $(id)?.addEventListener("change", updateSkillTokenMeter);
  });
  $("btnImportSkill")?.addEventListener("click", () => $("inpImportSkillFile")?.click());
  $("inpImportSkillFile")?.addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const res = await api("/api/skills/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: file.name, content_b64: String(reader.result || "") }),
        });
        state.skillCatalogLayer = res.skill?.layer || "styles";
        await loadSkillsCatalog();
        showToast(`Đã nhập Skill "${res.skill?.name}"!`);
      } catch (err) {
        showToast(err.message, "error");
      }
      e.target.value = "";
    };
    reader.readAsDataURL(file);
  });
  $("btnOpenSkillsFolder")?.addEventListener("click", () => api("/api/skills/open-folder", { method: "POST" }));
  $("txtGameContext").addEventListener("input", (e) => {
    autoResizeTextarea(e.target);
    const g = selectedGame();
    if (!g) return;
    g.game_description = e.target.value;
    clearTimeout(state.contextSaveTimer);
    state.contextSaveTimer = setTimeout(() => {
      saveGameContextPatch(g.game_id, { game_description: e.target.value });
    }, 600);
  });
  $("txtGameContext").addEventListener("blur", (e) => {
    const g = selectedGame();
    if (!g) return;
    clearTimeout(state.contextSaveTimer);
    saveGameContextPatch(g.game_id, { game_description: e.target.value });
  });

  $("heroBgImage").addEventListener("dblclick", () => switchView("home"));
  $("btnHeroEditCover").addEventListener("click", () => {
    const g = selectedGame();
    if (!g) return;
    $("editTitle").value = g.title || "";
    $("editLaunchMode").value = g.launch_mode || "direct_exe";
    $("editAppId").value = g.steam_appid || "";
    $("modalEditProfile").classList.remove("hidden");
  });
  $("btnCloseEditModal").addEventListener("click", () => $("modalEditProfile").classList.add("hidden"));
  $("btnSaveProfile").addEventListener("click", async () => {
    const g = selectedGame();
    if (!g) return;
    await api(`/api/games/${g.game_id}/profile`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("editTitle").value, launch_mode: $("editLaunchMode").value, steam_appid: $("editAppId").value }),
    });
    const f = $("editCoverFile").files?.[0];
    if (f) {
      const fd = new FormData();
      fd.append("file", f);
      await fetch(`/api/games/${g.game_id}/cover`, { method: "POST", body: fd });
    }
    $("btnSaveProfile")?.done?.();
    $("modalEditProfile").classList.add("hidden");
    loadGames(g.game_id);
  });
  $("btnDeleteGame").addEventListener("click", async () => {
    const g = selectedGame();
    if (!g) return;
    await api(`/api/games/${g.game_id}`, { method: "DELETE" });
    state.selectedGameId = null;
    $("modalEditProfile").classList.add("hidden");
    loadGames();
  });

  document.querySelectorAll(".preset-card").forEach((c) => {
    c.addEventListener("click", () => applyPresetLevel(c.dataset.preset, true));
    c.addEventListener("dblclick", () => runTranslationWithPreset(c.dataset.preset));
  });

  $("btnRescanGame").addEventListener("click", scanSelectedGame);
  $("selectLaunchMode").addEventListener("change", async (e) => {
    const g = selectedGame();
    if (g) await api(`/api/games/${g.game_id}/profile`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ launch_mode: e.target.value }) });
  });
  $("selectSourceLang").addEventListener("change", (e) => { state.sourceLang = e.target.value; rerankSlots(); });
  $("selectTargetLang").addEventListener("change", (e) => { state.targetLang = e.target.value; rerankSlots(); });

  document.querySelectorAll("#scopeSegmented .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#scopeSegmented .seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.scope = b.dataset.scope;
      $("subOptionsDrawer").classList.toggle("hidden", state.scope !== "SUB_ONLY");
      markCustomPresetIfModified();
    });
  });
  document.querySelectorAll("#aiModeSegmented .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#aiModeSegmented .seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.aiMode = b.dataset.aimode;
      markCustomPresetIfModified();
    });
  });

  $("chkTranslateChoices").addEventListener("change", markCustomPresetIfModified);
  $("chkKeepProperNouns").addEventListener("change", markCustomPresetIfModified);
  $("chkPatchFonts").addEventListener("change", markCustomPresetIfModified);
  $("chkOfflineOverride").addEventListener("change", updateStartButtonState);
  $("btnOpenBackupFolder").addEventListener("click", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    const g = selectedGame();
    if (g) await api(`/api/games/${g.game_id}/open-backup`, { method: "POST" });
  });

  $("btnAddGlossary").addEventListener("click", async () => {
    const g = selectedGame();
    if (!g) return;
    const src = ($("inpGlossarySrc").value || "").trim();
    const tgt = ($("inpGlossaryTgt").value || "").trim();
    if (!src || !tgt) return;
    await api(`/api/games/${g.game_id}/glossary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_lang: state.sourceLang, target_lang: state.targetLang, source_term: src, target_term: tgt }),
    });
    $("btnAddGlossary")?.done?.();
    $("inpGlossarySrc").value = "";
    $("inpGlossaryTgt").value = "";
    refreshStudioMonitor(g.game_id);
  });

  $("btnUpdateTools").addEventListener("click", async () => {
    await api("/api/tools/bootstrap?force=true", { method: "POST" });
    $("btnUpdateTools")?.done?.();
    showToast("Đang cập nhật Tools...");
  });
  $("appUpdatePill")?.addEventListener("click", openAppUpdateModal);
  $("btnCheckAppUpdate")?.addEventListener("click", () => checkAppUpdate(true));
  $("btnOpenUpdateModal")?.addEventListener("click", openAppUpdateModal);
  $("btnCloseUpdateModal")?.addEventListener("click", () => $("modalAppUpdate")?.classList.add("hidden"));
  $("btnDismissUpdateModal")?.addEventListener("click", () => $("modalAppUpdate")?.classList.add("hidden"));
  $("btnOpenGitHubRelease")?.addEventListener("click", () => {
    openAppReleasePage();
  });
  $("btnApplyAppUpdate")?.addEventListener("click", () => {
    applyAppUpdateNow();
  });
  $("btnCloseErrorModal")?.addEventListener("click", () => $("modalErrorReport")?.classList.add("hidden"));
  $("btnDismissErrorModal")?.addEventListener("click", () => $("modalErrorReport")?.classList.add("hidden"));
  $("btnCopyErrorReport")?.addEventListener("click", () => {
    if (!state.lastJobError) return;
    const e = state.lastJobError;
    const dump = `[Black Sakura Translate Error]\nGame: ${e.gameTitle}\nModel: ${e.model}\nStage: ${e.phase}\nTime: ${e.time}\nMessage: ${e.message}`;
    navigator.clipboard?.writeText(dump);
    showToast(state.uiLang === "en" ? "Copied error details!" : "Đã copy chi tiết lỗi!");
  });
  $("progLiveStateBtn")?.addEventListener("click", () => {
    if (state.lastJobError) openErrorReportModal();
  });
  $("progLiveStateBtn")?.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && state.lastJobError) {
      e.preventDefault();
      openErrorReportModal();
    }
  });
  $("chkAutoUpdateCheck")?.addEventListener("change", (e) => toggleAutoUpdateCheck(e.target.checked));
  $("tmSearchInput").addEventListener("input", () => loadToolsAndTM());
}

document.addEventListener("DOMContentLoaded", async () => {
  applyTheme(state.theme, null);
  applyUiLanguage(state.uiLang, null);
  bindEvents();
  setupDragDropOverlay();
  setupSSE();
  await Promise.all([refreshHealth(), loadGames(), loadSkillsCatalog()]);
  setTimeout(() => checkAppUpdate(false), 1800);
  setInterval(refreshHealth, 8000);
});
