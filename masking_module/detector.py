import re
from config import MASKING_CONFIG

BASE_EXCLUDED_NAME_WORDS = {
    "최근", "이같은", "이번", "정부", "정보", "이후", "한국",
    "유심", "유출", "정황", "이들이", "이름", "이메일",
    "이하", "최대", "이에는" ,"이에", "한국관", "한국인", "이로",
    "이어질", "정책", "조치", "한다며", "이런", "이러한", "이용",
    "이상", "유출할" , "이", "조사", "홍보", "홍콩"
}

# 자주 쓰이는 조사들 (콤마 누락으로 "할"+"도"가 "할도"로 합쳐져 있던 버그 수정)
JOSA_SUFFIXES = [
    "", "가", "를", "을", "은", "는", "에", "에서", "와", "과", "할",
    "도", "의", "로", "으로", "에게", "보다", "까지", "부터", "된", "이"
]

# 조사 포함한 예외 단어 확장
EXCLUDED_NAME_PATTERNS = set()
for base in BASE_EXCLUDED_NAME_WORDS:
    for suffix in JOSA_SUFFIXES:
        EXCLUDED_NAME_PATTERNS.add(base + suffix)

# 정규식은 "성씨로 시작하는 2~3글자 단어"를 전부 이름 후보로 잡기 때문에, 논문/보고서처럼
# 산문이 많은 문서에서는 "이용해", "정교한", "한다면"처럼 흔한 동사·형용사·명사가 대량으로
# 오탐된다. EXCLUDED_NAME_PATTERNS는 정확히 일치해야만 걸러지므로(활용형까지는 못 잡음),
# 자주 쓰이는 2글자 어간으로 시작하면 통째로 걸러내는 접두어 기반 필터를 추가한다.
# 이 방식도 완벽하지 않다 — 정규식만으로는 "이용해"(동사)와 "이민석"(이름)을 구분할 수
# 없는 것이 근본적 한계이며, 실제 해결은 형태소 분석/개체명 인식(NER)이 필요하다.
# 주의: "이용"처럼 실제 사람 이름에도 자주 쓰이는 접두어(이용운, 이용희, 이용수 등)를
# 통째로 배제하면 오탐은 줄지만 진짜 이름을 놓치는 미탐이 생긴다. 마스킹 도구는 "놓치는
# 것"(미탐)이 "체크박스가 하나 더 뜨는 것"(오탐)보다 위험이 훨씬 크므로, 실제 사람 이름과
# 충돌할 여지가 있는 접두어는 여기 넣지 않는다 — 대신 아래 EXCLUDED_NAME_PATTERNS에
# 관찰된 정확한 활용형만 개별 등록한다(예: "이용해"는 넣지만 "이용"은 넣지 않음).
EXCLUDED_NAME_PREFIXES = {
    "지도",  # "지도교수"를 "지도"+교수 호칭 패턴으로 잘못 쪼개는 것 방지
    "이전", "이후", "이런", "이러", "이렇", "이것", "이번", "이제", "이상",
    "이다", "이며", "이해", "이벤트", "이미지",
    "정보", "정확", "정도", "정리", "정교", "정책", "정황", "정상", "정말",
    "정의", "정에", "정규",
    "조사", "조직", "조성", "조치", "조건", "조율", "조정", "조를",
    "강조", "강화", "강력", "강경", "강제", "강하",
    "유출", "유지", "유의", "유사", "유형", "유무", "유일", "유료", "유리", "유용",
    "임의", "임시", "임박",
    "한계", "한다", "한국", "한번", "한편", "한창", "한글", "한점",
    "홍보", "홍수",
    "최근", "최적", "최초", "최소", "최고",
    "장비", "장점", "장애",
}

# 위 접두어 규칙에서 일부러 뺀 "이용", "최종" 등은 실제 관찰된 활용형만 정확히 등록한다.
EXTRA_EXCLUDED_NAME_EXACT = {
    "이용해", "이용한", "이용된", "이용하여", "이용해서", "이용자", "이용된다",
    "최종적", "최종의", "최종본", "최종안", "최종", "최대한",
}


def _is_excluded_name(matched_text):
    if matched_text in EXCLUDED_NAME_PATTERNS or matched_text in EXTRA_EXCLUDED_NAME_EXACT:
        return True
    return any(matched_text.startswith(prefix) for prefix in EXCLUDED_NAME_PREFIXES)


# 주소의 "OO동" 패턴도 실제 동네 이름과 무관하게 "자동", "수동"처럼 "동"으로 끝나는
# 흔한 단어를 오탐한다. 위와 같은 이유로 정확 일치가 아니라 통째로 배제한다.
EXCLUDED_DONG_WORDS = {
    "자동", "수동", "활동", "운동", "이동", "노동", "감동", "충동",
    "출동", "행동", "언동", "반동", "변동", "진동", "유동", "구동",
    "가동", "부동", "선동", "약동", "연동", "작동", "관련연구",
}

def detect_sensitive_items(text):
    '''
    정규식으로 개인정보를 탐지하여 {유형: [텍스트]} 형태로 반환
    '''
    detected = {}

    for item, config in MASKING_CONFIG.items():
        for pattern in config["patterns"]:
            matches = re.finditer(pattern, text)
            for match in matches:
                matched_text = match.group()

                if item == "이름" and _is_excluded_name(matched_text):
                    continue
                if item == "주소" and matched_text in EXCLUDED_DONG_WORDS:
                    continue

                detected.setdefault(item, set()).add(matched_text)

    return detected

#엑셀에서 사용할 함수
def detect_sensitive_items_fullmatch(text):
    '''
    텍스트 전체에서 항목이 하나라도 감지되면,
    해당 유형으로 셀 전체 텍스트를 반환
    '''
    detected = {}

    for item, config in MASKING_CONFIG.items():
        for pattern in config["patterns"]:
            if re.search(pattern, text):
                if item == "이름" and _is_excluded_name(text):
                    continue
                if item == "주소" and text in EXCLUDED_DONG_WORDS:
                    continue

                detected.setdefault(item, set()).add(text)
                break

    return detected
