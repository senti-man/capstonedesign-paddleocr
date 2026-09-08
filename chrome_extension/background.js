console.log('Background service worker is running!');

// 테스트용으로 Gmail 탭이 열릴 때 메시지를 콘솔에 출력
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (tab.url && tab.url.includes('mail.google.com')) {
        console.log('Gmail tab detected:', tab.url);
    }
});

chrome.runtime.onMessage.addListener((message, sender) => {
    if (!sender.tab) return;

    if (message.type === 'PII_RISK_UPDATE') {
        chrome.action.setBadgeText({ text: String(message.count), tabId: sender.tab.id });
        chrome.action.setBadgeBackgroundColor({ color: '#d93025', tabId: sender.tab.id });
    } else if (message.type === 'PII_RISK_CLEAR') {
        chrome.action.setBadgeText({ text: '', tabId: sender.tab.id });
    } else if (message.type === 'STAT_EVENT') {
        recordStat(message.event);
    }
});

// 팝업(popup.js)에서 읽는 누적 통계를 chrome.storage.local에 저장
function recordStat(event) {
    if (!['detected', 'masked', 'blockedSend'].includes(event)) return;

    chrome.storage.local.get({ stats: { detected: 0, masked: 0, blockedSend: 0 } }, (result) => {
        const stats = result.stats;
        stats[event] = (stats[event] || 0) + 1;
        chrome.storage.local.set({ stats });
    });
}
