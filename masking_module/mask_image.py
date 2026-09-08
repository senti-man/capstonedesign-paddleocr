import json
import re
from PIL import Image, ImageDraw, ImageFont
from detector import detect_sensitive_items
from utils.ocr_converter import convert_ocr_json_to_masking_format, estimate_sub_bbox
from clova_ocr import call_clova_ocr


def normalize_text(text):
    """한글, 숫자, 영문만 남기고 제거 (공백 포함 X)"""
    return re.sub(r"[^\w가-힣]", "", text)


def mask_image_from_ocr(
    image_path,
    output_path,
    selected_items=None,
    mask_style="box",
    ocr_json_path=None,
    use_clova=False,
    clova_api_url=None,
    clova_secret_key=None
):
    if selected_items is None:
        selected_items = []

    # OCR 결과 불러오기
    if use_clova:
        ocr_json = call_clova_ocr(image_path, clova_api_url, clova_secret_key)
    else:
        with open(ocr_json_path, "r", encoding="utf-8") as f:
            ocr_json = json.load(f)

    ocr_result = convert_ocr_json_to_masking_format(ocr_json)

    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)

    for item in ocr_result:
        text = normalize_text(item["text"])
        bbox = item["bbox"]

        for selected_text in selected_items:
            for token in selected_text.split():
                normalized_token = normalize_text(token)
                if normalized_token and normalized_token in text:
                    target_bbox = estimate_sub_bbox(item["text"], bbox, token)
                    print(f"마스킹 되는 text: {item['text']} (일치한 토큰: '{token}', 영역: {target_bbox})")
                    if mask_style == "text":
                        font = ImageFont.load_default()
                        draw.text((target_bbox[0], target_bbox[1]), "*" * len(token), fill="black", font=font)
                    else:
                        draw.rectangle(target_bbox, fill="black")
                    break  # 같은 항목에 대해 여러 토큰 중복 마스킹 방지

    image.save(output_path)
    print(f"[완료] 이미지 마스킹 완료: {output_path}")
