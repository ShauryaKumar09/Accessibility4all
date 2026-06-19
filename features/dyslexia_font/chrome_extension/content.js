const DEFAULTS = {
  enabled: true,
  fontFamily: '"OpenDyslexic", "Atkinson Hyperlegible", "Comic Sans MS", Arial, sans-serif',
  letterSpacing: '0.03em',
  lineHeight: '1.55',
  fontWeight: 'inherit'
};

function applySettings(settings) {
  const merged = { ...DEFAULTS, ...(settings || {}) };
  const root = document.documentElement;

  root.classList.toggle('a4a-dyslexia-font-enabled', Boolean(merged.enabled));
  root.style.setProperty('--a4a-dyslexia-font-family', merged.fontFamily);
  root.style.setProperty('--a4a-dyslexia-letter-spacing', merged.letterSpacing);
  root.style.setProperty('--a4a-dyslexia-line-height', merged.lineHeight);
  root.style.setProperty('--a4a-dyslexia-font-weight', merged.fontWeight);
}

function loadAndApply() {
  chrome.storage.sync.get(DEFAULTS, applySettings);
}

loadAndApply();

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== 'sync') return;
  const next = {};
  for (const [key, change] of Object.entries(changes)) {
    next[key] = change.newValue;
  }
  chrome.storage.sync.get(DEFAULTS, (current) => applySettings({ ...current, ...next }));
});
