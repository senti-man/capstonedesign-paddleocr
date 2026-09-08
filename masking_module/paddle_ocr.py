"""
PaddleOCR 기반 로컬 OCR 모듈.
Clova OCR API를 대체하여 네트워크 호출/API 키 없이 완전히 로컬에서 동작한다.
clova_ocr.call_clova_ocr()의 반환 형식({"images": [{"fields": [...]}]})과
동일하게 맞춰서 detector.py / utils/ocr_converter.py 등 하위 로직을 그대로 재사용한다.
"""
import threading

_ocr_instance = None
_lock = threading.Lock()


def _get_ocr():
    """PaddleOCR 인스턴스를 지연 생성 + 재사용 (모델 로딩 비용이 크므로 프로세스당 1회만)."""
    global _ocr_instance
    if _ocr_instance is None:
        with _lock:
            if _ocr_instance is None:
                from paddleocr import PaddleOCR
                _ocr_instance = PaddleOCR(
                    lang="korean",
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_recognition_model_name="korean_PP-OCRv5_mobile_rec",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    # 이 환경의 paddlepaddle(CPU) 빌드에서 oneDNN 실행기가 텍스트 검출
                    # 단계에서 NotImplementedError를 던지는 문제가 있어 비활성화한다.
                    enable_mkldnn=False,
                )
    return _ocr_instance


def call_paddle_ocr(image_file_path):
    """
    PaddleOCR로 이미지에서 텍스트와 좌표를 추출하여 Clova OCR 응답과 동일한 포맷으로 반환한다.
    PaddleOCR 인스턴스는 프로세스 내에서 재사용하지만 동시 호출에 대한 스레드 안전성이
    보장되지 않으므로, 호출 측(server.py)에서는 병렬이 아닌 순차 호출을 사용해야 한다.
    """
    ocr = _get_ocr()
    result = ocr.predict(image_file_path)

    fields = []
    for page in result:
        texts = page.get("rec_texts", [])
        polys = page.get("rec_polys", [])
        for text, poly in zip(texts, polys):
            fields.append({
                "inferText": text,
                "boundingPoly": {
                    "vertices": [{"x": float(x), "y": float(y)} for x, y in poly]
                }
            })

    return {"images": [{"fields": fields}]}
