from PIL import Image, ImageDraw, ImageFont
import img2pdf
import fitz  # PyMuPDF
import os
import re
from utils.ocr_converter import convert_ocr_json_to_masking_format, estimate_sub_bbox

# 현재 파일 기준 절대 경로로 uploads 폴더 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

def normalize_text(text):
    # 한글, 영문, 숫자만 남기고 특수문자 제거
    return re.sub(r"[^\w가-힣]", "", text).lower()

def mask_pdf_from_ocr(
    ocr_json,
    input_path,
    output_path,
    selected_items=None,
    mask_style="box"
):
    if selected_items is None:
        selected_items = []

    # OCR 결과 정리
    ocr_result = convert_ocr_json_to_masking_format(ocr_json)
    num_pages = max(item["page"] for item in ocr_result) + 1
    pdf_array = [[] for _ in range(num_pages)]

    for item in ocr_result:
        page = item["page"]
        if 0 <= page < num_pages:
            pdf_array[page].append(item)

    temp_images = []

    for i in range(num_pages):
        image_path = os.path.join(UPLOAD_FOLDER, f"pdf{i}.png")

        if not os.path.exists(image_path):
            print(f"[!] 이미지가 존재하지 않음: {image_path}")
            print("[!] 현재 uploads 폴더에 있는 파일들:")
            for f in os.listdir(UPLOAD_FOLDER):
                print(" -", f)
            raise FileNotFoundError(f"이미지가 존재하지 않음: {image_path}")

        image = Image.open(image_path).convert("RGB")
        draw = ImageDraw.Draw(image)

        for item in pdf_array[i]:
            original_text = item["text"]
            normalized_ocr_text = normalize_text(original_text)
            bbox = item["bbox"]

            for selected_text in selected_items:
                for token in selected_text.split():
                    normalized_token = normalize_text(token)

                    if normalized_token and normalized_token in normalized_ocr_text:
                        target_bbox = estimate_sub_bbox(original_text, bbox, token)
                        print(f"마스킹 되는 text: {original_text} (일치한 토큰: '{token}', 영역: {target_bbox})")
                        if mask_style == "text":
                            font = ImageFont.load_default()
                            draw.text((target_bbox[0], target_bbox[1]), "*" * len(token), fill="black", font=font)
                        else:
                            draw.rectangle(target_bbox, fill="black")
                        break  # 하나라도 매칭되면 중복 마스킹 방지

        # JPEG로 저장하여 용량 최적화
        temp_img_path = f"temp_page_{i}.jpg"
        image.save(temp_img_path, "JPEG", quality=60, optimize=True)
        temp_images.append(temp_img_path)

    # JPEG → PDF 병합
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(temp_images))

    # 임시 이미지 삭제
    for img in temp_images:
        os.remove(img)

    print(f"[완료] PDF 마스킹 완료: {output_path}")


def mask_text_pdf_from_ocr(input_path, output_path, selected_items=None, image_derived_fields=None):
    """
    텍스트 레이어가 있는 PDF를 래스터화하지 않고 벡터 상태로 직접 리댁션 처리한다.

    page.search_for()로 선택된 문자열이 실제로 위치한 정확한 좌표를 찾아 그 부분만
    리댁션한다 — 줄 전체를 통째로 가리는 게 아니라 탐지된 문자열(주민등록번호,
    이메일, 이름 등) 딱 그만큼만 가린다. 벡터 PDF는 실제 문자 위치 정보를 그대로
    가지고 있어서 OCR처럼 좌표를 추정할 필요 없이 정확하게 찾을 수 있다.

    image_derived_fields: PDF에 삽입된 이미지(신분증 사진 등)에서 OCR로 보완 추출한
    필드 목록 [{"page": int, "bbox": [x1,y1,x2,y2], "text": str}, ...]. 이 텍스트는
    실제 PDF 텍스트 레이어에 없어서 search_for로 찾을 수 없으므로, 이미 알고 있는
    좌표(bbox)를 이용해 직접 리댁션한다. add_redact_annot은 텍스트뿐 아니라 그
    영역에 있는 이미지 픽셀도 함께 제거하므로 같은 방식으로 처리할 수 있다.

    add_redact_annot + apply_redactions는 박스 이미지를 덧씌우는 것과 달리
    마스킹 대상 영역의 실제 콘텐츠를 제거하므로 추출 불가능한 진짜 마스킹이 된다.
    mask_style(text/box)은 지원하지 않으며 항상 검정 박스로 리댁션한다 —
    벡터 PDF에서 '*' 텍스트로 치환하려면 폰트/자간을 다시 조판해야 해서 범위 밖으로 둔다.
    """
    if selected_items is None:
        selected_items = []
    if image_derived_fields is None:
        image_derived_fields = []

    doc = fitz.open(input_path)

    try:
        for page in doc:
            for selected_text in selected_items:
                rects = page.search_for(selected_text)
                for rect in rects:
                    print(f"마스킹 되는 영역: '{selected_text}' (페이지 {page.number + 1}, {rect})")
                    page.add_redact_annot(rect, fill=(0, 0, 0))

        for field in image_derived_fields:
            field_text = field["text"]
            for selected_text in selected_items:
                for token in selected_text.split():
                    if token and token in field_text:
                        page = doc[field["page"]]
                        target_bbox = estimate_sub_bbox(field_text, field["bbox"], token)
                        print(f"마스킹 되는 영역(임베디드 이미지): '{token}' (페이지 {field['page'] + 1}, {target_bbox})")
                        page.add_redact_annot(fitz.Rect(*target_bbox), fill=(0, 0, 0))
                        break

        for page in doc:
            page.apply_redactions()

        doc.save(output_path, garbage=4, deflate=True)
    finally:
        doc.close()

    print(f"[완료] PDF 마스킹 완료 (텍스트 레이어 직접 리댁션, 정밀 좌표): {output_path}")
