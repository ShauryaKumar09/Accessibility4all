/* Accessibility4all hub — sidebar + page router + modals.
 * All state comes from Python via pywebview's js_api bridge (window.pywebview.api).
 * No build step — plain JS, matching the design-handoff files' own convention.
 */

let FEATURES = [];
let STATE = { enabled: {}, notice: "" };
let CURRENT_PAGE = "home";
let TEXT_SCALE = 1.0;

function api() { return window.pywebview.api; }

function featureById(id) { return FEATURES.find(f => f.id === id); }

async function boot() {
  FEATURES = await api().list_features();
  STATE = await api().get_state();
  TEXT_SCALE = await api().get_text_scale();
  document.documentElement.style.setProperty("--text-scale", TEXT_SCALE);
  renderSidebar();
  renderPage();
  setInterval(pollState, 1000);
}

async function pollState() {
  STATE = await api().poll();
  renderSidebar();
  if (CURRENT_PAGE !== "home") updateTitleRowState();
  else renderPage();
}

/* ── sidebar ─────────────────────────────────────────────────────────── */
function renderSidebar() {
  document.getElementById("nav-home").classList.toggle("active", CURRENT_PAGE === "home");
  const list = document.getElementById("nav-list");
  list.innerHTML = "";
  for (const feat of FEATURES) {
    const on = !!STATE.enabled[feat.id];
    const row = document.createElement("div");
    row.className = "nav-row" + (CURRENT_PAGE === feat.id ? " active" : "") + (on ? " on" : "");
    row.innerHTML = `
      <span class="nav-label">${escapeHtml(feat.name)}</span>
      <span class="switch ${on ? "on" : ""}" data-toggle="${feat.id}"><span class="knob"></span></span>
    `;
    row.addEventListener("click", (e) => {
      if (e.target.closest(".switch")) return;
      navigate(feat.id);
    });
    row.querySelector(".switch").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleFeature(feat.id);
    });
    list.appendChild(row);
  }
  const running = Object.values(STATE.enabled).filter(Boolean).length;
  let hint = `${running} of ${FEATURES.length} features on`;
  if (STATE.notice) hint += `   ·   ${STATE.notice}`;
  document.getElementById("footer-hint").textContent = hint;
}

document.getElementById("nav-home").addEventListener("click", () => navigate("home"));

async function toggleFeature(id) {
  STATE = await api().toggle_feature(id);
  renderSidebar();
  if (CURRENT_PAGE === id) updateTitleRowState();
  else if (CURRENT_PAGE === "home") renderPage();
}

function navigate(page) {
  CURRENT_PAGE = page;
  renderSidebar();
  renderPage();
}

/* ── page router ─────────────────────────────────────────────────────── */
function renderPage() {
  const main = document.getElementById("main");
  if (CURRENT_PAGE === "home") {
    renderHome(main);
  } else {
    const feat = featureById(CURRENT_PAGE);
    if (!feat) { renderHome(main); CURRENT_PAGE = "home"; return; }
    renderFeaturePage(main, feat);
  }
}

function renderHome(main) {
  main.innerHTML = `
    <h1 class="page-title">Turn on the help you need.</h1>
    <p class="page-sub">Click a feature to see how it works, or flip it on right here.</p>
    <div class="home-grid" id="home-grid"></div>
  `;
  const grid = document.getElementById("home-grid");
  for (const feat of FEATURES) {
    const on = !!STATE.enabled[feat.id];
    const card = document.createElement("div");
    card.className = "home-card" + (on ? " on" : "");
    card.innerHTML = `
      <h3>${escapeHtml(feat.name)}</h3>
      <p>${escapeHtml(feat.description)}</p>
      <div class="state">${on ? "On" : "Off"}</div>
    `;
    card.addEventListener("click", () => navigate(feat.id));
    grid.appendChild(card);
  }
}

/* ── feature page ────────────────────────────────────────────────────── */
async function renderFeaturePage(main, feat) {
  const settings = await api().get_settings(feat.id);
  const on = !!STATE.enabled[feat.id];

  main.innerHTML = `
    <div class="title-row">
      <h1 class="page-title" style="margin-bottom:0">${escapeHtml(feat.name)}</h1>
    </div>
    <p class="page-sub">${escapeHtml(feat.description)}</p>
    <div class="body-split">
      <div class="steps" id="steps-col"></div>
      <div class="placeholder-panel">Preview coming soon</div>
    </div>
    <div class="controls-grid" id="title-controls"></div>
    <div id="settings-panel-host"></div>
  `;

  await renderTitleControls(feat, settings, on);
  renderSteps(feat);

  const panelHost = document.getElementById("settings-panel-host");
  if (feat.settingsPanel) {
    const panelData = await api().get_panel_data(feat.id);
    if (feat.settingsPanel === "dyslexia") renderDyslexiaPanel(panelHost, feat, settings, panelData, on);
    else if (feat.settingsPanel === "colorblind") renderColorblindPanel(panelHost, feat, settings, panelData);
    else if (feat.settingsPanel === "focus") renderFocusPanel(panelHost, feat, settings, panelData);
    else if (feat.settingsPanel === "cursor") renderCursorPanel(panelHost, feat, settings, panelData);
  }
}

async function renderTitleControls(feat, settings, on) {
  const host = document.getElementById("title-controls");
  host.innerHTML = "";

  for (const sc of feat.shortcuts) {
    const card = document.createElement("div");
    card.className = "control-card" + (sc.editable ? "" : " static");
    const raw = sc.editable ? (settings.hotkeys || {})[sc.key] || "" : (sc.static || "");
    const current = raw ? await api().pretty_hotkey(raw) : "";
    card.innerHTML = `
      <span class="control-name">${escapeHtml(sc.label)}</span>
      <span class="chip-btn${sc.editable ? "" : " static"}">${escapeHtml(current || "—")}</span>
    `;
    if (sc.editable) card.querySelector(".chip-btn").addEventListener(
      "click", () => openHotkeyModal(feat, sc, settings));
    host.appendChild(card);
  }

  for (const opt of feat.options) {
    const card = document.createElement("div");
    card.className = "control-card";
    const optOn = !!settings[opt.key];
    card.innerHTML = `
      <span class="control-name">
        <button class="info-btn" title="What's this?">i</button>
        ${escapeHtml(opt.label)}
      </span>
      <span class="switch ${optOn ? "on" : ""}"><span class="knob"></span></span>
    `;
    card.querySelector(".info-btn").addEventListener(
      "click", (e) => { e.stopPropagation(); openInfoModal(opt.label, opt.info); });
    const sw = card.querySelector(".switch");
    sw.addEventListener("click", async () => {
      const next = !sw.classList.contains("on");
      sw.classList.toggle("on", next);
      settings[opt.key] = next;
      await api().save_setting(feat.id, opt.key, next);
    });
    host.appendChild(card);
  }
}

function updateTitleRowState() {
  // Re-render just the on/off switches after a poll tick, so a crash reflects
  // without rebuilding the whole page (which would drop focus/scroll).
  const feat = featureById(CURRENT_PAGE);
  if (!feat) return;
  api().get_settings(feat.id).then(settings => {
    renderTitleControls(feat, settings, !!STATE.enabled[feat.id]);
  });
}

function renderSteps(feat) {
  const col = document.getElementById("steps-col");
  col.innerHTML = "";
  feat.instructions.forEach(([title, body], i) => {
    const el = document.createElement("div");
    el.className = "step";
    el.innerHTML = `
      <div class="num">${i + 1}</div>
      <div>
        <div class="step-title">${escapeHtml(title)}</div>
        <div class="step-body">${escapeHtml(body)}</div>
      </div>
    `;
    el.addEventListener("click", () => el.classList.toggle("highlight"));
    col.appendChild(el);
  });
}

/* ── bespoke settings panels ─────────────────────────────────────────── */
function renderDyslexiaPanel(host, feat, settings, panelData, on) {
  const spacing = settings.letter_spacing ?? 0.03;
  const lineHeight = settings.line_height ?? 1.55;
  host.innerHTML = `
    <div class="settings-panel">
      <h4>Pick a font</h4>
      <div class="font-grid" id="font-grid"></div>
      <div id="stepper-spacing"></div>
      <div id="stepper-line"></div>
      <div class="small-note">
        ${on
          ? "Applied automatically — Windows apps and websites (via the Chrome extension) update as soon as you pick a font."
          : "Turn the switch in the sidebar on to apply this font."}
      </div>
    </div>
    <div class="screen-btn">
      <button class="text-btn primary" id="start-screening">Take the screening test</button>
    </div>
  `;
  const grid = document.getElementById("font-grid");
  for (const f of panelData.fontChoices || []) {
    const card = document.createElement("div");
    card.className = "font-card" + (settings.font_family === f.name ? " selected" : "");
    card.innerHTML = `<div class="fname">${escapeHtml(f.name)}</div><div class="fnote">${escapeHtml(f.note)}</div>`;
    card.addEventListener("click", async () => {
      settings.font_family = f.name;
      await api().save_setting(feat.id, "font_family", f.name);
      renderDyslexiaPanel(host, feat, settings, panelData, on);
    });
    grid.appendChild(card);
  }

  renderStepper(document.getElementById("stepper-spacing"), "Space between letters",
    spacing, 0.0, 0.12, 0.01, v => `${v.toFixed(2)} em`,
    async v => { settings.letter_spacing = v; await api().save_setting(feat.id, "letter_spacing", v); });

  renderStepper(document.getElementById("stepper-line"), "Space between lines",
    lineHeight, 1.0, 2.2, 0.05, v => `${v.toFixed(2)}×`,
    async v => { settings.line_height = v; await api().save_setting(feat.id, "line_height", v); });

  document.getElementById("start-screening").addEventListener("click", openScreeningModal);
}

function renderColorblindPanel(host, feat, settings, panelData) {
  host.innerHTML = `<div class="settings-panel"><h4>Filter type</h4><div class="filter-grid" id="filter-grid"></div></div>`;
  const grid = document.getElementById("filter-grid");
  for (const name of panelData.filterTypes || []) {
    const btn = document.createElement("button");
    btn.className = "filter-btn" + (settings.filter_name === name ? " selected" : "");
    btn.textContent = name;
    btn.addEventListener("click", async () => {
      settings.filter_name = name;
      await api().save_setting(feat.id, "filter_name", name);
      renderColorblindPanel(host, feat, settings, panelData);
    });
    grid.appendChild(btn);
  }
}

function renderFocusPanel(host, feat, settings, panelData) {
  const duration = settings.duration_minutes ?? 25;
  host.innerHTML = `
    <div class="settings-panel">
      <h4>Blocked sites (one per line)</h4>
      <textarea class="blocklist-box" id="blocklist">${escapeHtml((settings.blocklist || []).join("\n"))}</textarea>
      <div id="stepper-duration"></div>
      ${panelData.isAdmin === false ? '<div class="small-note">Run the hub as Administrator, or blocking will fail.</div>' : ""}
    </div>
  `;
  document.getElementById("blocklist").addEventListener("blur", async (e) => {
    const domains = e.target.value.split("\n").map(s => s.trim()).filter(Boolean);
    settings.blocklist = domains;
    await api().save_setting(feat.id, "blocklist", domains);
  });
  renderStepper(document.getElementById("stepper-duration"), "Session length",
    duration, 5, 480, 5, v => `${Math.round(v)} min`,
    async v => { settings.duration_minutes = v; await api().save_setting(feat.id, "duration_minutes", v); });
}

function renderCursorPanel(host, feat, settings, panelData) {
  const size = settings.size ?? 1;
  host.innerHTML = `
    <div class="settings-panel">
      <div id="stepper-cursor-size"></div>
      ${panelData.isWindows === false ? '<div class="small-note">Cursor size only works on Windows.</div>' : ""}
    </div>
  `;
  renderStepper(document.getElementById("stepper-cursor-size"), "Pointer size",
    size, 1, 15, 1, v => `${Math.round(v)} / 15`,
    async v => { settings.size = v; await api().save_setting(feat.id, "size", v); });
}

function renderStepper(host, label, value, min, max, step, fmt, onChange) {
  let v = value;
  function draw() {
    host.innerHTML = `
      <div class="stepper-row">
        <div><div class="label">${escapeHtml(label)}</div><div class="value">${fmt(v)}</div></div>
        <div class="stepper-btns">
          <button class="stepper-btn" data-dir="-1">−</button>
          <button class="stepper-btn" data-dir="1">+</button>
        </div>
      </div>
    `;
    host.querySelectorAll(".stepper-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const dir = parseFloat(btn.dataset.dir);
        v = Math.min(max, Math.max(min, Math.round((v + dir * step) * 100) / 100));
        draw();
        await onChange(v);
      });
    });
  }
  draw();
}

/* ── modals ──────────────────────────────────────────────────────────── */
function openModal(html) {
  document.getElementById("modal-card").innerHTML = `<button class="modal-close" id="modal-x">&times;</button>` + html;
  const scrim = document.getElementById("modal");
  scrim.classList.remove("hidden");
  document.getElementById("modal-x").addEventListener("click", closeModal);
}
function closeModal() {
  document.getElementById("modal").classList.add("hidden");
  document.getElementById("modal-card").innerHTML = "";
}
document.getElementById("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

function openInfoModal(title, body) {
  openModal(`<h3>${escapeHtml(title)}</h3><p>${escapeHtml(body || "")}</p>`);
}

function openHotkeyModal(feat, shortcut, settings) {
  openModal(`<h3>${escapeHtml(shortcut.label)}</h3><div class="modal-hint" id="hk-hint">Press a key…</div>`);
  api().capture_hotkey().then(async (res) => {
    const hint = document.getElementById("hk-hint");
    if (!hint) return; // modal closed before capture finished
    if (!res.ok) { hint.textContent = res.error; return; }
    const saved = await api().save_hotkey(feat.id, shortcut.key, res.combo);
    if (!saved.ok) { hint.textContent = saved.error; return; }
    settings.hotkeys = saved.hotkeys;
    hint.textContent = `Set to ${res.label}`;
    setTimeout(() => { closeModal(); renderTitleControls(feat, settings, !!STATE.enabled[feat.id]); }, 700);
  });
}

/* ── screening quiz (pure client-side, same modal) ──────────────────── */
let SCREENING = null;
let quizState = null;

async function openScreeningModal() {
  if (!SCREENING) SCREENING = await api().get_screening();
  quizState = { order: shuffle([...SCREENING.questions.keys()]), idx: 0, flags: 0, readStart: 0 };
  showScreeningIntro();
}

function showScreeningIntro() {
  openModal(`
    <h3>Dyslexia Screening</h3>
    <p style="color:var(--warm-text);margin-bottom:12px">${escapeHtml(SCREENING.disclaimer)}</p>
    <p>${SCREENING.questions.length} quick questions, then a short timed reading task.</p>
    <div class="modal-actions"><button class="text-btn primary" id="q-start">Start</button></div>
  `);
  document.getElementById("q-start").addEventListener("click", showScreeningQuestion);
}

function showScreeningQuestion() {
  if (quizState.idx >= quizState.order.length) { showReadingTask(); return; }
  const q = SCREENING.questions[quizState.order[quizState.idx]];
  const opts = shuffle(q.options.map((text, i) => ({ text, i })));
  const optsHtml = opts.map(o => `<button class="quiz-opt" data-i="${o.i}">${escapeHtml(o.text)}</button>`).join("");
  openModal(`
    <h3>Question ${quizState.idx + 1} of ${quizState.order.length}</h3>
    <p>${escapeHtml(q.prompt)}</p>
    <div class="quiz-card">${optsHtml}</div>
  `);
  document.querySelectorAll(".quiz-opt").forEach(btn => {
    btn.addEventListener("click", () => {
      if (parseInt(btn.dataset.i) !== q.correct) quizState.flags++;
      quizState.idx++;
      showScreeningQuestion();
    });
  });
}

function showReadingTask() {
  openModal(`
    <h3>Reading speed</h3>
    <p>Read the passage, then press Done.</p>
    <div class="quiz-card">${escapeHtml(SCREENING.passage)}</div>
    <div class="modal-actions"><button class="text-btn primary" id="q-done">Done reading</button></div>
  `);
  quizState.readStart = performance.now();
  document.getElementById("q-done").addEventListener("click", () => {
    const elapsed = Math.max(0.5, (performance.now() - quizState.readStart) / 1000);
    const words = SCREENING.passage.split(/\s+/).length;
    const wpm = words / (elapsed / 60);
    if (wpm < SCREENING.typicalWpm * 0.5) quizState.flags++;
    showScreeningResult(wpm);
  });
}

function showScreeningResult(wpm) {
  let msg, color;
  if (quizState.flags <= 1) { msg = "Few patterns showed up in your answers."; color = "var(--on)"; }
  else if (quizState.flags <= 3) { msg = "Some patterns showed up in your answers."; color = "var(--warm-text)"; }
  else { msg = "Several patterns showed up in your answers."; color = "var(--stop-border)"; }
  openModal(`
    <h3>Result</h3>
    <p style="color:${color};font-weight:600">${msg} (reading speed: ${Math.round(wpm)} words/min)</p>
    <p style="color:var(--warm-text);margin-top:10px">${escapeHtml(SCREENING.disclaimer)}</p>
    <div class="modal-actions"><button class="text-btn" id="q-again">Try again</button></div>
  `);
  document.getElementById("q-again").addEventListener("click", openScreeningModal);
}

/* ── utils ───────────────────────────────────────────────────────────── */
function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function shuffle(arr) {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

if (window.pywebview) boot();
else window.addEventListener("pywebviewready", boot);
