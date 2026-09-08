function render(stats) {
    document.getElementById('detected').textContent = stats.detected || 0;
    document.getElementById('masked').textContent = stats.masked || 0;
    document.getElementById('blockedSend').textContent = stats.blockedSend || 0;
}

chrome.storage.local.get({ stats: {} }, (result) => render(result.stats));

chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.stats) {
        render(changes.stats.newValue || {});
    }
});

document.getElementById('resetBtn').addEventListener('click', () => {
    chrome.storage.local.set({ stats: { detected: 0, masked: 0, blockedSend: 0 } });
});
