import fitz  # PyMuPDF

MIN_CHARS_PER_PAGE = 20


def has_extractable_text(pdf_path, min_chars_per_page=MIN_CHARS_PER_PAGE):
    """
    PDF에 실제 텍스트 레이어가 있는지 판별.
    스캔본(이미지만 있는 PDF)은 get_text()가 거의 빈 문자열을 반환하므로
    페이지당 평균 문자 수로 텍스트 PDF 여부를 구분한다.
    """
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count == 0:
            return False
        total_chars = sum(len(page.get_text().strip()) for page in doc)
        return (total_chars / doc.page_count) >= min_chars_per_page
    finally:
        doc.close()


def extract_pdf_ocr_format(pdf_path):
    """
    PDF 텍스트 레이어에서 줄 단위 텍스트와 좌표를 직접 추출하여
    기존 Clova OCR 응답과 동일한 {"images": [{"fields": [...]}]} 형식으로 반환한다.
    OCR 호출 없이 detector.py / ocr_converter.py를 그대로 재사용하기 위함이며,
    좌표는 원본 PDF 페이지 좌표계를 그대로 사용하므로 래스터화 과정에서 생기는
    좌표 불일치 문제가 발생하지 않는다.
    """
    doc = fitz.open(pdf_path)
    images = []

    try:
        for page in doc:
            fields = []
            page_dict = page.get_text("dict")

            for block in page_dict.get("blocks", []):
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    line_text = "".join(span.get("text", "") for span in spans).strip()
                    if not line_text:
                        continue

                    x0, y0, x1, y1 = line["bbox"]
                    fields.append({
                        "inferText": line_text,
                        "boundingPoly": {
                            "vertices": [
                                {"x": x0, "y": y0},
                                {"x": x1, "y": y0},
                                {"x": x1, "y": y1},
                                {"x": x0, "y": y1},
                            ]
                        }
                    })

            images.append({"fields": fields})
    finally:
        doc.close()

    return {"images": images, "source": "pdf_text"}


def extract_embedded_images(pdf_path, min_size=40):
    """
    텍스트 레이어가 있는 PDF라도 그 안에 얹힌 이미지(신분증 사진, 스크린샷, 스캔
    페이지 등)의 내용은 실제 텍스트가 아니라서 get_text()로는 전혀 잡히지 않는다.
    각 페이지에 삽입된 이미지를 찾아 픽셀 데이터 + 페이지 상 배치 좌표(rect)를
    함께 반환한다 — 호출 측에서 이 이미지들에 OCR을 돌려 놓치는 개인정보를 보완한다.

    min_size: 로고/아이콘/장식 무늬처럼 개인정보가 있을 가능성이 낮은 작은 이미지는
    OCR 대상에서 제외하기 위한 최소 픽셀 크기(가로·세로 모두 이 값 이상이어야 포함).
    """
    doc = fitz.open(pdf_path)
    results = []

    try:
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                base_image = doc.extract_image(xref)
                if base_image["width"] < min_size or base_image["height"] < min_size:
                    continue

                rects = page.get_image_rects(xref)
                if not rects:
                    continue

                results.append({
                    "page": page.number,
                    "rect": list(rects[0]),
                    "image_bytes": base_image["image"],
                    "image_ext": base_image["ext"],
                    "pixel_width": base_image["width"],
                    "pixel_height": base_image["height"],
                })
    finally:
        doc.close()

    return results
