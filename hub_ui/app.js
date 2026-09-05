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
    <div class="startup-row" id="startup-row" hidden>
      <div>
        <div class="startup-label">Start when I log in</div>
        <div class="startup-sub">Opens automatically with the features you left on.</div>
      </div>
      <button class="switch" id="startup-switch" role="switch" aria-checked="false">
        <span class="knob"></span>
      </button>
    </div>
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
  renderStartupToggle();
}

async function renderStartupToggle() {
  const row = document.getElementById("startup-row");
  const sw = document.getElementById("startup-switch");
  if (!row || !sw) return;
  let state;
  try {
    state = await api().get_launch_at_startup();
  } catch (e) {
    return;                       // not supported here; leave the row hidden
  }
  if (!state || !state.supported) return;
  row.hidden = false;
  const paint = on => {
    sw.classList.toggle("on", !!on);
    sw.setAttribute("aria-checked", on ? "true" : "false");
  };
  paint(state.enabled);
  sw.addEventListener("click", async () => {
    const next = !sw.classList.contains("on");
    paint(next);                  // optimistic, corrected below if it failed
    const res = await api().set_launch_at_startup(next);
    paint(res && res.enabled);
    STATE = await api().get_state();
    renderSidebar();              // surfaces the error notice if it failed
  });
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
      <div class="preview-panel" id="preview-panel"><div class="preview-empty">Preview coming soon</div></div>
    </div>
    <div class="controls-grid" id="title-controls"></div>
    <div id="settings-panel-host"></div>
  `;

  await renderTitleControls(feat, settings, on);
  renderSteps(feat);
  renderPreview(feat);

  const panelHost = document.getElementById("settings-panel-host");
  if (feat.settingsPanel) {
    const panelData = await api().get_panel_data(feat.id);
    if (feat.settingsPanel === "colorblind") renderColorblindPanel(panelHost, feat, settings, panelData);
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


/* ── preview panel (screenshots + screen recordings) ─────────────────── */
// One cycler at a time: leaving the page must stop the old timer, or every
// feature page ever visited keeps ticking in the background.
let previewTimer = null;

async function renderPreview(feat) {
  if (previewTimer) { clearInterval(previewTimer); previewTimer = null; }
  const host = document.getElementById("preview-panel");
  if (!host) return;

  let media;
  try {
    media = await api().get_preview_media(feat.id);
  } catch (e) {
    return;                        // leave the "coming soon" placeholder
  }
  const videos = media.videos || [];
  const images = media.images || [];
  const labels = media.labels || [];
  if (!videos.length && !images.length) return;

  // A recording shows the feature actually running, so it wins the panel.
  if (videos.length) {
    host.innerHTML = `
      <video class="preview-media" src="${escapeHtml(videos[0])}"
             autoplay loop muted playsinline></video>`;
    return;
  }

  if (images.length === 1) {
    host.innerHTML = `<img class="preview-media" src="${escapeHtml(images[0])}" alt="">`;
    return;
  }

  // Several images: cycle them, a second each, with the name of what is on
  // screen. Both frames stay in the DOM and cross-fade, so the panel never
  // flashes empty between them.
  host.innerHTML = `
    <img class="preview-media preview-slide is-on" id="slide-a" alt="">
    <img class="preview-media preview-slide" id="slide-b" alt="">
    <div class="preview-caption" id="preview-caption"></div>`;
  const a = document.getElementById("slide-a");
  const b = document.getElementById("slide-b");
  const cap = document.getElementById("preview-caption");
  let i = 0, showingA = true;
  a.src = images[0];
  if (cap) cap.textContent = labels[0] || "";
  // Preload, so a slide is never blank the first time round.
  images.forEach(src => { const im = new Image(); im.src = src; });

  previewTimer = setInterval(() => {
    if (!document.getElementById("slide-a")) {   // navigated away
      clearInterval(previewTimer); previewTimer = null; return;
    }
    i = (i + 1) % images.length;
    const next = showingA ? b : a;
    const curr = showingA ? a : b;
    next.src = images[i];
    next.classList.add("is-on");
    curr.classList.remove("is-on");
    showingA = !showingA;
    if (cap) cap.textContent = labels[i] || "";
  }, 1000);
}

/* ── utils ───────────────────────────────────────────────────────────── */
function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

if (window.pywebview) boot();
else window.addEventListener("pywebviewready", boot);
