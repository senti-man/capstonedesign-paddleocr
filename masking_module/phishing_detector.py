"""
규칙 기반 피싱 이메일 탐지 (정규식 + 차등 가중치 점수제)

설계 근거:
  - Basnet, Sung, Liu, "Rule-Based Phishing Attack Detection", New Mexico Tech.
    URL 구조(IP 기반 주소, 비정상 문자, 과도한 길이/서브도메인)를 규칙화하고
    "규칙 개수 카운트 + 임계값" 방식만으로 임계값=4에서 TPR 91.87% / FPR 1.80%를
    달성함을 보였다 (원 논문은 웹페이지 크롤링·블랙리스트 API 기반 규칙까지 포함하지만,
    이 프로젝트는 외부 API 없이 이메일 본문·링크만으로 판단 가능한 규칙만 차용했다).
    또한 Logistic Regression 계수 분석에서 규칙별 위험도(Odds Ratio)가 크게 다름을
    확인했다 (IP 기반 URL이 가장 강한 지표) — 이를 근거로 이 모듈은 동일 가중치가 아닌
    차등 가중치를 사용한다.
  - Hybrid Heuristic-Machine Learning Framework for Phishing Detection (ETASR)
    등 후속 연구에서 흔히 쓰이는 "고위험=10점 / 중위험=5점 / 저위험=2점" 티어 방식을
    가중치 부여 방식으로 채택했다.

한계: 학습 데이터로 검증한 통계적 정확도가 아니라 규칙 설계 시점의 근거일 뿐이다.
실제 성능(TPR/FPR)은 이 프로젝트의 표본 테스트 결과를 별도로 명시한다.
"""
import re
from urllib.parse import urlparse

HIGH = 10
MEDIUM = 5
LOW = 2

# 국내에서 자주 사칭되는 브랜드/기관과 공식 도메인 (필요 시 확장)
KNOWN_BRANDS = {
    "네이버": ["naver.com"],
    "카카오": ["kakao.com", "kakaocorp.com"],
    "국민은행": ["kbstar.com"],
    "신한은행": ["shinhan.com"],
    "우리은행": ["wooribank.com"],
    "구글": ["google.com", "gmail.com"],
    "애플": ["apple.com", "icloud.com"],
    "쿠팡": ["coupang.com"],
    "택배": ["cjlogistics.com", "epost.go.kr", "hanjin.co.kr"],
}

SHORTENER_DOMAINS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "buff.ly", "url.kr"}

URGENCY_KEYWORDS = [
    "계정이 정지", "계정 정지", "즉시 확인", "24시간 이내", "본인인증", "인증이 필요",
    "당첨되었습니다", "당첨되셨습니다", "긴급", "만료됩니다", "만료 예정", "결제 승인",
    "비정상적인 로그인", "잠금 해제", "확인하지 않으면", "클릭하세요", "지금 확인",
]

SENSITIVE_REQUEST_KEYWORDS = [
    "비밀번호를 입력", "비밀번호를 확인", "카드번호를 입력", "카드 번호를 입력",
    "주민등록번호를 입력", "주민등록번호를 확인", "otp", "보안카드 번호",
    "계좌번호를 입력", "인증번호를 입력",
]

IP_URL_PATTERN = re.compile(r"https?://(\d{1,3}\.){3}\d{1,3}")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")


def _extract_domain(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _check_brand_mismatch(sender_name, sender_domain):
    """발신자 표시 이름에 브랜드명이 있는데 실제 도메인이 그 브랜드 공식 도메인이 아닌 경우."""
    if not sender_name or not sender_domain:
        return None
    for brand, official_domains in KNOWN_BRANDS.items():
        if brand in sender_name:
            if not any(sender_domain == d or sender_domain.endswith("." + d) for d in official_domains):
                return brand
    return None


def _check_lookalike_domain(domain):
    """브랜드명이 도메인에 포함되어 있지만 공식 도메인이 아닌 경우 (유사 도메인)."""
    # 한글 브랜드명 자체가 도메인에 들어가진 않으므로 영문 브랜드 힌트 목록으로 검사
    known_english_hints = ["naver", "kakao", "google", "apple", "coupang", "kbstar", "shinhan"]
    for hint in known_english_hints:
        if hint in domain and not any(domain == d or domain.endswith("." + d) for brands in KNOWN_BRANDS.values() for d in brands):
            return hint
    return None


def detect_phishing(text, links=None):
    """
    text: 이메일 본문 텍스트 (본문에 노출된 URL 문자열도 포함)
    links: [{"text": 화면표시텍스트, "href": 실제링크}, ...] (선택, DOM에서 추출된 경우)
    반환: {"score", "max_score", "percentage", "level", "matched_rules": [...]}
    """
    links = links or []
    text = text or ""
    matched = []

    def add(rule, weight, detail):
        matched.append({"rule": rule, "weight": weight, "detail": detail})

    # --- URL 본문에서 직접 추출 (링크 목록이 없을 때 대비) ---
    body_urls = URL_PATTERN.findall(text)
    all_urls = list({*body_urls, *[l["href"] for l in links if l.get("href")]})

    # R1 (HIGH): IP 주소 기반 URL
    for url in all_urls:
        if IP_URL_PATTERN.match(url):
            add("IP 주소 기반 URL", HIGH, url)
            break

    # R2 (HIGH): 표시 텍스트가 URL처럼 보이는데 실제 도메인과 다름 (링크 위장)
    # 표시 텍스트가 명시적으로 http(s):// 또는 www.로 시작할 때만 "URL을 사칭한 텍스트"로
    # 취급한다. 느슨한 "단어.확장자" 패턴은 "report.pdf" 같은 파일명까지 도메인으로
    # 오인식해 정상 첨부파일 링크를 오탐하므로 사용하지 않는다.
    for link in links:
        display = (link.get("text") or "").strip()
        href = link.get("href") or ""
        display_url_match = re.match(r"(https?://|www\.)[\w.-]+\.[a-zA-Z]{2,}", display, re.IGNORECASE)
        if display_url_match and href:
            display_domain = _extract_domain(display_url_match.group()) or display_url_match.group().lower()
            href_domain = _extract_domain(href)
            if href_domain and display_domain not in href_domain and href_domain not in display_domain:
                add("표시 링크와 실제 링크 도메인 불일치", HIGH, f"{display} -> {href}")
                break

    # R3 (HIGH): 발신자 표시이름 - 도메인 불일치 (브랜드 사칭)
    # (server.py 쪽에서 sender_name/sender_domain을 넘겨줄 때만 유효 — 여기선 text 내 "보낸사람:" 패턴으로 best-effort 시도)
    sender_match = re.search(r"보낸사람\s*[:\-]?\s*(.+?)\s*<([^>]+)>", text)
    if sender_match:
        sender_name = sender_match.group(1)
        sender_domain = _extract_domain("http://" + sender_match.group(2).split("@")[-1])
        brand = _check_brand_mismatch(sender_name, sender_domain)
        if brand:
            add("발신자 표시이름-도메인 불일치", HIGH, f"'{brand}' 사칭 의심 ({sender_domain})")

    # R4 (MEDIUM): 유사 도메인
    for url in all_urls:
        domain = _extract_domain(url)
        hint = _check_lookalike_domain(domain)
        if hint:
            add("유사 도메인 의심", MEDIUM, f"{domain} ('{hint}' 유사)")
            break

    # R5 (MEDIUM): 단축 URL 서비스
    for url in all_urls:
        domain = _extract_domain(url)
        if domain in SHORTENER_DOMAINS:
            add("단축 URL 사용", MEDIUM, domain)
            break

    # R6 (MEDIUM): 민감정보 입력 요구 문구
    lowered = text.lower()
    for kw in SENSITIVE_REQUEST_KEYWORDS:
        if kw.lower() in lowered:
            add("민감정보 입력 요구 문구", MEDIUM, kw)
            break

    # R7 (HIGH): URL에 '@' 포함 (@ 앞부분을 신뢰 도메인처럼 위장하는 기법 —
    # R2(링크 위장)와 공격 구조가 동일하므로 같은 HIGH 등급으로 취급)
    for url in all_urls:
        if "@" in url.split("://", 1)[-1]:
            add("URL 내 '@' 문자 포함 (도메인 위장)", HIGH, url)
            break

    # R8 (LOW): 긴급성/위협 키워드
    for kw in URGENCY_KEYWORDS:
        if kw in text:
            add("긴급성/위협 키워드", LOW, kw)
            break

    # R9 (LOW): URL 난독화 문자 과다 (하이픈 3개 이상 또는 서브도메인 5개 이상)
    for url in all_urls:
        domain = _extract_domain(url)
        if domain.count("-") >= 3 or domain.count(".") >= 5:
            add("URL 구조 난독화 의심", LOW, domain)
            break

    # R10 (LOW): URL 과도하게 긴 경우
    for url in all_urls:
        domain = _extract_domain(url)
        if len(url) > 75 or len(domain) > 30:
            add("비정상적으로 긴 URL", LOW, url[:50] + "...")
            break

    max_score = 4 * HIGH + 3 * MEDIUM + 3 * LOW  # 61 (R7이 HIGH로 이동)
    score = sum(m["weight"] for m in matched)
    percentage = round(score / max_score * 100, 1)

    # 등급 판정은 순수 백분율 임계값이 아니라 규칙 심각도를 함께 본다.
    # 근거: Basnet et al.은 IP 기반 URL(Rule 4)·검색엔진 미노출(Rule 1,2) 같은
    # 단일 강신호 규칙이 그 자체로 97% 이상의 피싱 페이지를 탐지해낸다고 보고했다.
    # 반면 약한 규칙(긴급성 키워드 등)은 단독으로는 오탐이 많아 여러 개가 겹쳐야
    # 의미가 있다 (동일 논문 Table II: 규칙 2개 임계값은 FPR 36.4%로 비실용적,
    # 임계값 4에서 TPR 91.87%/FPR 1.80%로 실용적 균형에 도달).
    has_high = any(m["weight"] == HIGH for m in matched)
    if has_high or len(matched) >= 4:
        level = "위험"
    elif len(matched) >= 1:
        level = "주의"
    else:
        level = "안전"

    return {
        "score": score,
        "max_score": max_score,
        "percentage": percentage,
        "level": level,
        "matched_rules": matched,
    }
