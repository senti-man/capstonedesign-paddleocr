// ============================================================
// 개인정보 마스킹 Gmail 확장 - content script
//
// 상태 모델:
//   riskyAttachments : WeakMap<dialogElement, Set<fileName>>
//     - 특정 작성창(dialog)에서 "개인정보가 탐지됐지만 마스킹하지 않은" 파일 이름 집합
//     - 이 집합이 비어있지 않은 동안에는 해당 작성창의 "보내기" 클릭을 가로채 경고한다
//     - 파일마다 1:1로 처리하므로(마스킹 성공 시 원본을 masked_로 치환) 파일이
//       중간에 통째로 사라지는 일이 없다 (기존 버그: 마스킹을 취소하면 첨부 자체가 사라짐)
// ============================================================

const riskyAttachments = new WeakMap(); // dialog -> Set<fileName>
const riskyBody = new WeakMap();        // dialog -> string[] (감지된 본문 항목 텍스트)

function getDialog(el) {
    return el.closest('[role="dialog"]') || document.body;
}

function getRiskySet(dialog) {
    if (!riskyAttachments.has(dialog)) {
        riskyAttachments.set(dialog, new Set());
    }
    return riskyAttachments.get(dialog);
}

function markRisky(dialog, fileName) {
    getRiskySet(dialog).add(fileName);
    notifyBadge(dialog);
}

function clearRisky(dialog, fileName) {
    getRiskySet(dialog).delete(fileName);
    notifyBadge(dialog);
}

function totalRiskCount(dialog) {
    const attachmentCount = getRiskySet(dialog).size;
    const bodyCount = (riskyBody.get(dialog) || []).length > 0 ? 1 : 0;
    return attachmentCount + bodyCount;
}

function notifyBadge(dialog) {
    const count = totalRiskCount(dialog);
    if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.sendMessage) {
        chrome.runtime.sendMessage({ type: count > 0 ? 'PII_RISK_UPDATE' : 'PII_RISK_CLEAR', count });
    }
}

// 팝업 통계용 이벤트 전송 (chrome.storage에 누적 집계됨, background.js 참고)
function sendStat(event) {
    if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.sendMessage) {
        chrome.runtime.sendMessage({ type: 'STAT_EVENT', event });
    }
}

// ------------------------------------------------------------
// 1) 파일 선택(change) / 드래그앤드롭(drop) 공통 처리
// ------------------------------------------------------------

// 버블 단계에서 감지한다 (v1.4에서 캡처 단계 + stopImmediatePropagation으로 바꿔
// 원본 중복 첨부 버그를 고치려 했으나, Gmail 자체도 캡처 단계에서 더 먼저 이벤트를
// 가로채는 것으로 보여 감지 자체가 실행되지 않는 심각한 회귀가 발생했다. 감지가
// 안정적으로 되는 것이 우선이므로 되돌렸다. 원본 중복 첨부 문제는 이후 다른 방식
// (마스킹 완료 후 원본 첨부 칩을 찾아 제거)으로 별도 대응 예정.
document.body.addEventListener('change', async (event) => {
    const input = event.target;
    if (input.type !== 'file' || input.files.length === 0) return;

    // 처리 완료 후 결과 파일을 넣어주기 위해 우리가 스스로 dispatch한 change
    // 이벤트는 다시 처리하지 않는다 (안 그러면 무한 재처리 루프에 빠진다).
    if (input.__piiSyntheticUpdate) {
        input.__piiSyntheticUpdate = false;
        return;
    }

    await handleFiles(Array.from(input.files), input, getDialog(input));
});

// 드래그앤드롭은 Gmail의 기본 첨부 처리가 먼저 원본 파일을 그대로 붙이지 못하도록
// 캡처 단계에서 가로챈다. 이후 같은 작성창 안에 있는(숨겨져 있을 수 있는) 실제
// <input type="file">을 찾아 그 input의 change 이벤트로 재활용한다.
// 주의: Gmail이 내부적으로 첨부 버튼용 file input을 유지한다는 전제에 의존하는
// best-effort 구현이다 — Gmail DOM 구조가 바뀌면 동작하지 않을 수 있다.
document.body.addEventListener('drop', async (event) => {
    if (!event.dataTransfer || !event.dataTransfer.files || event.dataTransfer.files.length === 0) {
        return;
    }
    const dialog = getDialog(event.target);
    if (dialog === document.body) return; // Gmail 작성창 밖의 드롭은 무시

    event.preventDefault();
    event.stopPropagation();

    const hiddenInput = dialog.querySelector('input[type="file"]');
    if (!hiddenInput) {
        showToast('드래그앤드롭 첨부를 처리할 입력 요소를 찾지 못했습니다. 첨부 버튼으로 다시 시도해주세요.');
        return;
    }

    await handleFiles(Array.from(event.dataTransfer.files), hiddenInput, dialog);
}, true);

async function handleFiles(files, input, dialog) {
    const resultFiles = [];

    for (const file of files) {
        const outcome = await processOneFile(file, dialog);
        resultFiles.push(outcome.file);
    }

    await replaceInputFiles(input, resultFiles);
}

// ------------------------------------------------------------
// 2) 파일 1개 처리: 탐지 -> (필요 시) 마스킹 선택 -> 결과 파일 반환
//    반환하는 file은 항상 존재한다 (마스킹본 또는 원본) — 파일이 사라지지 않는다.
// ------------------------------------------------------------

async function processOneFile(file, dialog) {
    let detectedInfo = {};
    let uploadedFilename = '';

    try {
        showLoadingMessage(`📄 ${file.name} 개인정보 항목 감지 중...`);
        const formData = new FormData();
        formData.append('file', file);

        const response = await fetch('http://127.0.0.1:5000/upload', { method: 'POST', body: formData });
        if (!response.ok) throw new Error('파일 업로드 중 오류');

        const result = await response.json();
        detectedInfo = result.detected_info || {};
        uploadedFilename = result.filename;
    } catch (error) {
        console.error(error);
        showToast(`${file.name}: 업로드/탐지에 실패했습니다. 원본 그대로 첨부합니다.`);
        return { file, masked: false };
    } finally {
        hideLoadingMessage();
    }

    if (!detectedInfo || Object.keys(detectedInfo).length === 0) {
        showToast(`${file.name}: 개인정보 항목이 감지되지 않았습니다.`);
        clearRisky(dialog, file.name);
        return { file, masked: false };
    }

    sendStat('detected');

    const proceed = await showConfirmBanner(
        `"${file.name}"에서 개인정보가 감지되었습니다. 마스킹할 항목을 선택하시겠습니까?`,
        { confirmText: '항목 선택하기', cancelText: '마스킹 안 함' }
    );

    if (!proceed) {
        showToast(`${file.name}: 마스킹하지 않고 원본으로 첨부됩니다. (전송 시 다시 확인합니다)`);
        markRisky(dialog, file.name);
        return { file, masked: false };
    }

    const selectedMasking = await showMaskingOptions(file.name, detectedInfo);
    if (selectedMasking.length === 0) {
        showToast(`${file.name}: 선택된 항목이 없어 원본으로 첨부됩니다. (전송 시 다시 확인합니다)`);
        markRisky(dialog, file.name);
        return { file, masked: false };
    }

    try {
        showLoadingMessage(`🔒 ${file.name} 마스킹 처리 중...`);
        const maskResponse = await fetch('http://127.0.0.1:5000/mask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filename: uploadedFilename,
                mask_list: selectedMasking,
                maskStyle: 'box',
            }),
        });

        if (maskResponse.status !== 202) throw new Error('마스킹 요청 실패');

        const maskedBlob = await waitForMaskingComplete(uploadedFilename, (elapsedSec) => {
            showLoadingMessage(`🔒 ${file.name} 마스킹 처리 중... (${elapsedSec}초 경과, 페이지가 많으면 오래 걸릴 수 있어요)`);
        });
        const newFilename = `masked_${file.name}`;
        const maskedFile = new File([maskedBlob], newFilename, { type: file.type });

        sendStat('masked');
        clearRisky(dialog, file.name);
        await showDeleteOriginalReminder(file.name, newFilename);
        return { file: maskedFile, masked: true };
    } catch (error) {
        console.error(error);
        showToast(`${file.name}: 마스킹 처리에 실패했습니다. 원본으로 첨부됩니다.`);
        markRisky(dialog, file.name);
        return { file, masked: false };
    } finally {
        hideLoadingMessage();
    }
}

// 페이지 수가 많은 PDF는 마스킹(재래스터화 + 압축)에 시간이 꽤 걸릴 수 있어
// 넉넉하게 최대 10분(200회 × 3초)까지 기다린다. 예전엔 최대 40초로 고정돼 있어서
// 페이지가 많은 PDF는 실제로는 서버에서 계속 처리 중인데도 클라이언트가 먼저
// "시간 초과"로 포기해버리는 문제가 있었다.
async function waitForMaskingComplete(filename, onProgress) {
    const maxTries = 200;
    const delay = 3000;

    for (let i = 0; i < maxTries; i++) {
        const res = await fetch(`http://127.0.0.1:5000/result?filename=${encodeURIComponent(filename)}`);
        if (res.status === 200) {
            return await res.blob();
        } else if (res.status === 202) {
            if (onProgress) onProgress(Math.round((i + 1) * delay / 1000));
            await new Promise((resolve) => setTimeout(resolve, delay));
        } else {
            throw new Error('마스킹 결과 확인 실패');
        }
    }

    throw new Error('마스킹 대기 시간 초과');
}

async function replaceInputFiles(input, files) {
    const dataTransfer = new DataTransfer();
    files.forEach((file) => dataTransfer.items.add(file));
    input.__piiSyntheticUpdate = true;
    input.files = dataTransfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

// ------------------------------------------------------------
// 3) 전송 시점 강제 차단
//    캡처 단계에서 "보내기" 클릭을 감시하다가, 해당 작성창에 마스킹하지 않은
//    위험 파일이 남아있으면 클릭을 막고 경고 배너를 띄운다.
//    위험 파일이 없으면 아무 것도 하지 않고 그대로 통과시킨다.
// ------------------------------------------------------------

function isSendButton(el) {
    if (!el) return false;
    const label = (el.getAttribute('aria-label') || el.getAttribute('data-tooltip') || el.textContent || '').trim();
    return /^보내기|^send\b/i.test(label);
}

document.addEventListener('click', async (event) => {
    const target = event.target.closest('[role="button"], button');
    if (!isSendButton(target)) return;

    const dialog = getDialog(target);
    const risky = getRiskySet(dialog);
    const bodyItems = riskyBody.get(dialog) || [];
    if (risky.size === 0 && bodyItems.length === 0) return; // 위험 요소 없음 -> 원래 동작 그대로 통과

    event.preventDefault();
    event.stopImmediatePropagation();
    sendStat('blockedSend');

    const lines = [];
    if (risky.size > 0) {
        lines.push('[첨부파일]');
        Array.from(risky).forEach((name) => lines.push(`· ${name}`));
    }
    if (bodyItems.length > 0) {
        lines.push('[본문]');
        bodyItems.forEach((text) => lines.push(`· ${text}`));
    }

    const forceSend = await showConfirmBanner(
        `다음 항목에 개인정보가 포함되어 있을 수 있지만 마스킹/수정되지 않았습니다:\n${lines.join('\n')}\n\n그래도 보내시겠습니까?`,
        { confirmText: '그래도 보내기', cancelText: '취소', danger: true }
    );

    if (forceSend) {
        riskyAttachments.set(dialog, new Set()); // 강제 전송 승인 -> 다음 클릭은 그대로 통과
        riskyBody.set(dialog, []);
        hideBodyWarning(dialog);
        target.click();
    }
}, true);

// ------------------------------------------------------------
// 3.5) 이메일 본문 텍스트 검사
//    작성 중인 본문(contenteditable)에 입력을 멈추면 서버에 물어 개인정보
//    패턴이 있는지 확인한다. 첨부파일과 달리 매 키 입력마다 다이얼로그를
//    띄우면 방해가 되므로, 작성창 상단에 조용한 배너로만 표시하고
//    실질적 차단은 "보내기" 클릭 시점(3번 섹션)에만 수행한다.
// ------------------------------------------------------------

const bodyScanTimers = new WeakMap(); // bodyElement -> timeoutId
const BODY_SCAN_DEBOUNCE_MS = 900;

function findComposeBody(dialog) {
    return dialog.querySelector(
        '[contenteditable="true"][role="textbox"], [contenteditable="true"][aria-label*="본문"], [contenteditable="true"][g_editable="true"]'
    );
}

document.addEventListener('input', (event) => {
    const el = event.target;
    if (!el.isContentEditable) return;

    const dialog = getDialog(el);
    if (dialog === document.body) return;
    if (findComposeBody(dialog) !== el) return; // 본문 편집 영역이 아니면 무시

    clearTimeout(bodyScanTimers.get(el));
    bodyScanTimers.set(el, setTimeout(() => scanBodyText(el, dialog), BODY_SCAN_DEBOUNCE_MS));
}, true);

async function scanBodyText(bodyEl, dialog) {
    const text = bodyEl.innerText || '';
    if (!text.trim()) {
        riskyBody.set(dialog, []);
        hideBodyWarning(dialog);
        notifyBadge(dialog);
        return;
    }

    try {
        const res = await fetch('http://127.0.0.1:5000/detect_text', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        if (!res.ok) return;

        const data = await res.json();
        const detected = data.detected_info || {};
        const items = Object.values(detected).flat();

        riskyBody.set(dialog, items);
        notifyBadge(dialog);

        if (items.length > 0) {
            sendStat('detected');
            showBodyWarning(dialog, items);
        } else {
            hideBodyWarning(dialog);
        }
    } catch (error) {
        console.error('본문 검사 실패:', error);
    }
}

function showBodyWarning(dialog, items) {
    let banner = dialog.querySelector('.pii-body-warning');
    if (!banner) {
        banner = document.createElement('div');
        banner.className = 'pii-body-warning';
        banner.style.background = '#fef7e0';
        banner.style.color = '#7a5900';
        banner.style.border = '1px solid #f2c94c';
        banner.style.borderRadius = '6px';
        banner.style.padding = '8px 12px';
        banner.style.margin = '8px 0';
        banner.style.fontSize = '12px';
        banner.style.fontFamily = 'Roboto, Arial, sans-serif';
        dialog.prepend(banner);
    }
    banner.textContent = `⚠️ 본문에 개인정보로 보이는 텍스트가 있습니다: ${items.join(', ')} (보내기 전 다시 확인합니다)`;
}

function hideBodyWarning(dialog) {
    const banner = dialog.querySelector('.pii-body-warning');
    if (banner) banner.remove();
}

// ------------------------------------------------------------
// 3.6) 받은 메일 첨부파일 마스킹 (best-effort)
//    Gmail이 수신 메일의 첨부파일 다운로드 버튼에 어떤 속성을 쓰는지는
//    공식 문서가 없어 확실하지 않다. aria-label에 "다운로드/download"가
//    포함된 요소를 클릭했을 때, 그 요소(또는 조상)에서 실제 다운로드
//    가능한 href를 동기적으로 먼저 찾을 수 있을 때만 가로챈다 — 못 찾으면
//    아무 것도 하지 않고 Gmail 기본 동작을 그대로 둔다 (오작동으로 정상
//    다운로드를 막는 것을 방지).
// ------------------------------------------------------------

function isDownloadControl(el) {
    if (!el) return false;
    const label = (el.getAttribute('aria-label') || el.getAttribute('data-tooltip') || '').trim();
    return /다운로드|download/i.test(label);
}

function findDownloadUrl(el) {
    const withHref = el.closest('a[href], [data-url], [data-download-url]');
    if (!withHref) return null;
    return withHref.getAttribute('href') || withHref.getAttribute('data-url') || withHref.getAttribute('data-download-url');
}

function guessAttachmentName(el) {
    const chip = el.closest('[aria-label]');
    const label = chip ? chip.getAttribute('aria-label') : '';
    return (label && label.replace(/다운로드|download/gi, '').trim()) || 'attachment';
}

document.addEventListener('click', async (event) => {
    const target = event.target.closest('[role="button"], button, a, span');
    if (!isDownloadControl(target)) return;
    if (getDialog(target) !== document.body) return; // 작성창 안(내 첨부)에는 적용하지 않음

    const url = findDownloadUrl(target);
    if (!url) return; // 다운로드 링크를 못 찾으면 손대지 않고 기본 동작에 맡긴다

    event.preventDefault();
    event.stopImmediatePropagation();

    const fileName = guessAttachmentName(target);

    try {
        showLoadingMessage(`📥 ${fileName} 다운로드 및 개인정보 검사 중...`);
        const fileRes = await fetch(url, { credentials: 'include' });
        if (!fileRes.ok) throw new Error('첨부파일 다운로드 실패');
        const blob = await fileRes.blob();
        const file = new File([blob], fileName, { type: blob.type });

        const formData = new FormData();
        formData.append('file', file);
        const uploadRes = await fetch('http://127.0.0.1:5000/upload', { method: 'POST', body: formData });
        if (!uploadRes.ok) throw new Error('업로드 실패');
        const uploadData = await uploadRes.json();
        const detectedInfo = uploadData.detected_info || {};

        if (!detectedInfo || Object.keys(detectedInfo).length === 0) {
            hideLoadingMessage();
            triggerDownload(blob, fileName);
            return;
        }

        hideLoadingMessage();
        sendStat('detected');
        const selected = await showMaskingOptions(fileName, detectedInfo);

        if (selected.length === 0) {
            showToast(`${fileName}: 마스킹 없이 원본으로 다운로드합니다.`);
            triggerDownload(blob, fileName);
            return;
        }

        showLoadingMessage(`🔒 ${fileName} 마스킹 처리 중...`);
        const maskRes = await fetch('http://127.0.0.1:5000/mask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: uploadData.filename, mask_list: selected, maskStyle: 'box' }),
        });
        if (maskRes.status !== 202) throw new Error('마스킹 요청 실패');

        const maskedBlob = await waitForMaskingComplete(uploadData.filename, (elapsedSec) => {
            showLoadingMessage(`🔒 ${fileName} 마스킹 처리 중... (${elapsedSec}초 경과)`);
        });
        sendStat('masked');
        triggerDownload(maskedBlob, `masked_${fileName}`);
        showToast(`${fileName}: 마스킹 후 다운로드 완료!`);
    } catch (error) {
        console.error(error);
        showToast(`${fileName}: 처리에 실패했습니다. Gmail에서 직접 다운로드해주세요.`);
    } finally {
        hideLoadingMessage();
    }
}, true);

function triggerDownload(blob, fileName) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

// ------------------------------------------------------------
// 3.7) 받은 메일 피싱 의심도 검사 (best-effort)
//    Gmail의 비공식 CSS 클래스명 대신 공식 접근성 속성인 role="listitem"
//    (메일 목록/본문 항목에 쓰이는 ARIA 역할)에 기대어 구현했다. 사용자가
//    메일 목록의 항목을 클릭해 열람하면 잠시(700ms) 후 해당 영역의 텍스트와
//    링크를 모아 서버의 규칙 기반 피싱 스코어러(phishing_detector.py)로 보낸다.
//    "주의"/"위험"으로 판정되면 해당 항목 상단에 배너로 근거를 표시한다.
//    한 항목을 반복 스캔하지 않도록 WeakSet으로 추적한다.
// ------------------------------------------------------------

const phishingScanned = new WeakSet();

document.addEventListener('click', (event) => {
    const item = event.target.closest('[role="listitem"]');
    if (!item) return;
    if (getDialog(item) !== document.body) return; // 작성창 내부 클릭은 무시

    setTimeout(() => scanForPhishing(item), 700); // 본문 렌더링 대기
}, true);

async function scanForPhishing(container) {
    const text = container.innerText || '';
    if (!text.trim() || text.length < 20) return; // 내용이 거의 없으면(리스트 행 자체) 스킵
    if (phishingScanned.has(container)) return;
    phishingScanned.add(container);

    const links = Array.from(container.querySelectorAll('a[href]')).map((a) => ({
        text: a.textContent,
        href: a.href,
    }));

    try {
        const res = await fetch('http://127.0.0.1:5000/detect_phishing', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, links }),
        });
        if (!res.ok) return;

        const result = await res.json();
        if (result.level === '위험' || result.level === '주의') {
            showPhishingWarning(container, result);
            sendStat('detected');
        }
    } catch (error) {
        console.error('피싱 검사 실패:', error);
    }
}

function showPhishingWarning(container, result) {
    let banner = container.querySelector('.pii-phishing-warning');
    if (!banner) {
        banner = document.createElement('div');
        banner.className = 'pii-phishing-warning';
        banner.style.borderRadius = '6px';
        banner.style.padding = '10px 14px';
        banner.style.margin = '8px 0';
        banner.style.fontSize = '13px';
        banner.style.fontFamily = 'Roboto, Arial, sans-serif';
        banner.style.whiteSpace = 'pre-line';
        container.prepend(banner);
    }

    const isDanger = result.level === '위험';
    banner.style.background = isDanger ? '#fce8e6' : '#fef7e0';
    banner.style.color = isDanger ? '#a50e0e' : '#7a5900';
    banner.style.border = `1px solid ${isDanger ? '#d93025' : '#f2c94c'}`;

    const icon = isDanger ? '🚨' : '⚠️';
    const details = result.matched_rules.map((r) => `· ${r.rule} (${r.detail})`).join('\n');
    banner.textContent = `${icon} 피싱 의심 이메일 (${result.level}, 위험도 점수 ${result.percentage}%)\n${details}`;
}

// ------------------------------------------------------------
// 4) UI 헬퍼: 마스킹 항목 선택 다이얼로그
// ------------------------------------------------------------

function showMaskingOptions(fileName, detectedInfo) {
    return new Promise((resolve) => {
        const dialog = document.createElement('dialog');
        applyDialogStyle(dialog);

        const title = document.createElement('h3');
        title.textContent = `${fileName}에서 마스킹할 항목 선택`;
        title.style.margin = '0 0 12px 0';
        dialog.appendChild(title);

        const globalRow = document.createElement('div');
        globalRow.style.display = 'flex';
        globalRow.style.gap = '8px';
        globalRow.style.marginBottom = '8px';

        const selectAllBtn = document.createElement('button');
        selectAllBtn.type = 'button';
        selectAllBtn.textContent = '전체 선택';
        applySecondaryButtonStyle(selectAllBtn);

        const deselectAllBtn = document.createElement('button');
        deselectAllBtn.type = 'button';
        deselectAllBtn.textContent = '전체 해제';
        applySecondaryButtonStyle(deselectAllBtn);

        globalRow.appendChild(selectAllBtn);
        globalRow.appendChild(deselectAllBtn);
        dialog.appendChild(globalRow);

        const form = document.createElement('form');
        form.method = 'dialog';

        const checkboxes = [];

        Object.entries(detectedInfo).forEach(([type, texts]) => {
            if (!Array.isArray(texts) || texts.length === 0) return;

            const header = document.createElement('div');
            header.style.display = 'flex';
            header.style.justifyContent = 'space-between';
            header.style.alignItems = 'center';
            header.style.marginTop = '12px';

            const headerLabel = document.createElement('span');
            headerLabel.textContent = `📌 ${type}`;
            headerLabel.style.fontWeight = 'bold';
            header.appendChild(headerLabel);

            const categoryCheckboxes = [];

            const categoryToggle = document.createElement('button');
            categoryToggle.type = 'button';
            categoryToggle.textContent = '선택/해제';
            categoryToggle.style.fontSize = '11px';
            categoryToggle.style.padding = '2px 8px';
            categoryToggle.style.border = '1px solid #dadce0';
            categoryToggle.style.borderRadius = '4px';
            categoryToggle.style.background = '#fff';
            categoryToggle.style.cursor = 'pointer';
            categoryToggle.addEventListener('click', () => {
                const shouldCheck = categoryCheckboxes.some((cb) => !cb.checked);
                categoryCheckboxes.forEach((cb) => { cb.checked = shouldCheck; });
            });
            header.appendChild(categoryToggle);

            form.appendChild(header);

            texts.forEach((text) => {
                const label = document.createElement('label');
                label.style.display = 'block';
                label.style.margin = '4px 0';

                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.checked = true;
                checkbox.value = JSON.stringify({ type, text });

                label.appendChild(checkbox);
                label.appendChild(document.createTextNode(` ${text}`));
                form.appendChild(label);

                checkboxes.push(checkbox);
                categoryCheckboxes.push(checkbox);
            });
        });

        selectAllBtn.addEventListener('click', () => {
            checkboxes.forEach((cb) => { cb.checked = true; });
        });
        deselectAllBtn.addEventListener('click', () => {
            checkboxes.forEach((cb) => { cb.checked = false; });
        });

        const submitButton = document.createElement('button');
        submitButton.textContent = '마스킹 적용 (선택 안 하면 마스킹 없이 첨부)';
        submitButton.type = 'submit';
        submitButton.style.marginTop = '12px';
        applyPrimaryButtonStyle(submitButton);
        form.appendChild(submitButton);

        dialog.appendChild(form);
        document.body.appendChild(dialog);
        dialog.showModal();

        form.addEventListener('submit', () => {
            const selected = checkboxes
                .filter((cb) => cb.checked)
                .map((cb) => JSON.parse(cb.value));
            dialog.close();
            dialog.remove();
            resolve(selected);
        });
    });
}

// 확인/취소 배너 (confirm() 대체) - Promise<boolean>
function showConfirmBanner(message, { confirmText = '확인', cancelText = '취소', danger = false } = {}) {
    return new Promise((resolve) => {
        const dialog = document.createElement('dialog');
        applyDialogStyle(dialog);
        if (danger) dialog.style.borderTop = '4px solid #d93025';

        const text = document.createElement('p');
        text.style.whiteSpace = 'pre-line';
        text.style.margin = '0 0 16px 0';
        text.textContent = message;
        dialog.appendChild(text);

        const buttonRow = document.createElement('div');
        buttonRow.style.display = 'flex';
        buttonRow.style.justifyContent = 'flex-end';
        buttonRow.style.gap = '8px';

        const cancelBtn = document.createElement('button');
        cancelBtn.textContent = cancelText;
        applySecondaryButtonStyle(cancelBtn);

        const confirmBtn = document.createElement('button');
        confirmBtn.textContent = confirmText;
        applyPrimaryButtonStyle(confirmBtn, danger);

        cancelBtn.addEventListener('click', () => {
            dialog.close();
            dialog.remove();
            resolve(false);
        });
        confirmBtn.addEventListener('click', () => {
            dialog.close();
            dialog.remove();
            resolve(true);
        });

        buttonRow.appendChild(cancelBtn);
        buttonRow.appendChild(confirmBtn);
        dialog.appendChild(buttonRow);

        document.body.appendChild(dialog);
        dialog.showModal();
    });
}

function applyDialogStyle(dialog) {
    dialog.style.padding = '20px';
    dialog.style.maxWidth = '420px';
    dialog.style.maxHeight = '70vh';
    dialog.style.overflowY = 'auto';
    dialog.style.borderRadius = '12px';
    dialog.style.border = 'none';
    dialog.style.boxShadow = '0 4px 24px rgba(0,0,0,0.2)';
    dialog.style.fontFamily = 'Roboto, Arial, sans-serif';
    dialog.style.fontSize = '14px';
    dialog.style.color = '#202124';
}

function applyPrimaryButtonStyle(btn, danger = false) {
    btn.style.padding = '8px 16px';
    btn.style.border = 'none';
    btn.style.borderRadius = '4px';
    btn.style.background = danger ? '#d93025' : '#1a73e8';
    btn.style.color = '#fff';
    btn.style.cursor = 'pointer';
    btn.style.fontWeight = '500';
}

function applySecondaryButtonStyle(btn) {
    btn.style.padding = '8px 16px';
    btn.style.border = '1px solid #dadce0';
    btn.style.borderRadius = '4px';
    btn.style.background = '#fff';
    btn.style.color = '#3c4043';
    btn.style.cursor = 'pointer';
}

// 가벼운 토스트 알림 (alert() 대체, 자동 사라짐)
// 마스킹 성공 시 "원본 파일을 직접 지워달라"고 안내하는 알림창.
// 알려진 한계: 이 확장은 Gmail이 이미 첨부한 원본을 자동으로 제거하지 못한다
// (원인·시도했다가 되돌린 내용은 README "확장 프로그램 동작" 참고). 자동으로
// 사라지는 토스트로는 놓치기 쉬워서, 사용자가 직접 닫아야 하는 배너로 안내한다.
function showDeleteOriginalReminder(originalName, maskedName) {
    return new Promise((resolve) => {
        const dialog = document.createElement('dialog');
        applyDialogStyle(dialog);
        dialog.style.borderTop = '4px solid #1a73e8';

        const text = document.createElement('p');
        text.style.whiteSpace = 'pre-line';
        text.style.margin = '0 0 16px 0';
        text.textContent = `✅ "${maskedName}" 마스킹 완료!\n\n첨부 목록에 원본 "${originalName}"이 함께 남아있을 수 있습니다. 전송 전에 첨부 목록에서 원본을 직접 삭제해주세요.`;
        dialog.appendChild(text);

        const okBtn = document.createElement('button');
        okBtn.textContent = '확인했습니다';
        applyPrimaryButtonStyle(okBtn);
        okBtn.style.display = 'block';
        okBtn.style.marginLeft = 'auto';
        okBtn.addEventListener('click', () => {
            dialog.close();
            dialog.remove();
            resolve();
        });
        dialog.appendChild(okBtn);

        document.body.appendChild(dialog);
        dialog.showModal();
    });
}

function showToast(message) {
    const toast = document.createElement('div');
    toast.textContent = message;
    toast.style.position = 'fixed';
    toast.style.bottom = '24px';
    toast.style.left = '50%';
    toast.style.transform = 'translateX(-50%)';
    toast.style.background = '#323232';
    toast.style.color = '#fff';
    toast.style.padding = '12px 20px';
    toast.style.borderRadius = '8px';
    toast.style.fontSize = '13px';
    toast.style.fontFamily = 'Roboto, Arial, sans-serif';
    toast.style.zIndex = 10001;
    toast.style.boxShadow = '0 2px 12px rgba(0,0,0,0.3)';
    toast.style.maxWidth = '360px';
    toast.style.whiteSpace = 'pre-line';
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

function showLoadingMessage(msg) {
    hideLoadingMessage();
    const overlay = document.createElement('div');
    overlay.id = 'pii-mask-loading';
    overlay.style.position = 'fixed';
    overlay.style.top = 0;
    overlay.style.left = 0;
    overlay.style.width = '100%';
    overlay.style.height = '100%';
    overlay.style.backgroundColor = 'rgba(0,0,0,0.5)';
    overlay.style.color = 'white';
    overlay.style.display = 'flex';
    overlay.style.justifyContent = 'center';
    overlay.style.alignItems = 'center';
    overlay.style.zIndex = 10000;
    overlay.style.fontSize = '20px';
    overlay.textContent = msg;
    document.body.appendChild(overlay);
}

function hideLoadingMessage() {
    const overlay = document.getElementById('pii-mask-loading');
    if (overlay) overlay.remove();
}
