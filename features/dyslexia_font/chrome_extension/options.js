const DEFAULTS = {
  enabled: true,
  fontFamily: '"OpenDyslexic", "Atkinson Hyperlegible", "Comic Sans MS", Arial, sans-serif',
  letterSpacing: '0.03em',
  lineHeight: '1.55',
  fontWeight: 'inherit'
};

const fields = ['enabled', 'fontFamily', 'letterSpacing', 'lineHeight', 'fontWeight'];

function setStatus(text) {
  const status = document.getElementById('status');
  status.textContent = text;
  window.setTimeout(() => {
    if (status.textContent === text) status.textContent = '';
  }, 1800);
}

function load() {
  chrome.storage.sync.get(DEFAULTS, (settings) => {
    for (const field of fields) {
      const el = document.getElementById(field);
      if (el.type === 'checkbox') {
        el.checked = Boolean(settings[field]);
      } else {
        el.value = settings[field];
      }
    }
  });
}

function save() {
  const settings = {};
  for (const field of fields) {
    const el = document.getElementById(field);
    settings[field] = el.type === 'checkbox' ? el.checked : el.value;
  }
  chrome.storage.sync.set(settings, () => setStatus('Saved'));
}

document.getElementById('save').addEventListener('click', save);
load();
