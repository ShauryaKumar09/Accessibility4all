const DEFAULTS = { enabled: true };

const enabled = document.getElementById('enabled');
const status = document.getElementById('status');

function showStatus(text) {
  status.textContent = text;
  window.setTimeout(() => {
    if (status.textContent === text) status.textContent = '';
  }, 1600);
}

chrome.storage.sync.get(DEFAULTS, (settings) => {
  enabled.checked = Boolean(settings.enabled);
});

enabled.addEventListener('change', () => {
  chrome.storage.sync.set({ enabled: enabled.checked }, () => showStatus('Saved'));
});

document.getElementById('options').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
});
