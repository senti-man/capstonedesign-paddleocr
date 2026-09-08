def estimate_sub_bbox(line_text, bbox, matched_token):
    '''
    OCR은 줄 단위 좌표만 주기 때문에, 매칭된 문자열만 정밀하게 가리려면 줄 안에서
    그 문자열의 위치를 추정해야 한다. 줄 전체 글자 수 대비 매칭 문자열의 시작/끝
    위치 비율로 가로 폭을 나눈다 — 글자마다 폭이 다른 실제 렌더링과는 오차가 있는
    근사치지만, 줄 전체를 가리는 것보다는 훨씬 정밀하다.
    찾지 못하면 안전하게 원래 bbox 전체를 반환한다(과소 마스킹보다 과다 마스킹이 낫다).
    '''
    if not line_text or not matched_token:
        return bbox

    idx = line_text.find(matched_token)
    if idx == -1:
        return bbox

    x1, y1, x2, y2 = bbox
    width = x2 - x1
    start_ratio = idx / len(line_text)
    end_ratio = min((idx + len(matched_token)) / len(line_text), 1.0)

    return [x1 + width * start_ratio, y1, x1 + width * end_ratio, y2]


def convert_ocr_json_to_masking_format(json_data):
    '''
    OCR의 결과가 저장된 JSON에서 텍스트와 좌표를 추출,
    페이지 정보를 json 구조에서 직접 가져옴
    '''
    ocr_result = []

    for page_index, page_data in enumerate(json_data["images"]):
        fields = page_data.get("fields", [])
        for field in fields:
            text = field["inferText"]
            vertices = field["boundingPoly"]["vertices"]
            x1 = min(v["x"] for v in vertices)
            y1 = min(v["y"] for v in vertices)
            x2 = max(v["x"] for v in vertices)
            y2 = max(v["y"] for v in vertices)

            ocr_result.append({
                "text": text,
                "bbox": [x1, y1, x2, y2],
                "page": page_index
            })

    return ocr_result
