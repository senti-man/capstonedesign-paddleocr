"""
기존 경로(pdf2image + Clova OCR) vs 신규 경로(PyMuPDF 직접 추출)의
처리 시간·용량을 비교하는 벤치마크 스크립트.

사용법:
    python benchmark_hybrid_pdf.py [PDF경로] [반복횟수]

기존 경로 측정은 poppler(pdftoppm)와 인터넷 연결, server.py에 설정된
Clova OCR 키가 필요하다. 둘 중 하나라도 없으면 해당 항목은 건너뛰고
신규 경로 결과만 출력한다.
"""
import os
import sys
import time
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "uploads", "test.pdf")
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 3


def avg(nums):
    return sum(nums) / len(nums) if nums else None


def bench_new_extraction(pdf_path, trials):
    from pdf_text_extractor import extract_pdf_ocr_format

    times = []
    result = None
    for _ in range(trials):
        t0 = time.perf_counter()
        result = extract_pdf_ocr_format(pdf_path)
        times.append(time.perf_counter() - t0)

    field_count = sum(len(img["fields"]) for img in result["images"])
    return times, field_count


def bench_old_ocr(pdf_path, trials):
    from config import POPPLER_PATH

    if POPPLER_PATH is None and shutil.which("pdftoppm") is None:
        return None, None, "poppler(pdftoppm) 미설치 - 실제 개발 환경(poppler/Clova 키 설정된 PC)에서 실행하세요."

    try:
        from pdf2image import convert_from_path
        from server import SECRET_KEY, API_URL
        from clova_ocr import call_clova_ocr
    except Exception as e:
        return None, None, f"기존 경로 의존성 로드 실패: {e}"

    tmp_dir = os.path.join(BASE_DIR, "uploads", "_bench_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    times = []
    field_count = 0
    try:
        for _ in range(trials):
            t0 = time.perf_counter()
            pages = convert_from_path(pdf_path, dpi=100, poppler_path=POPPLER_PATH)
            image_paths = []
            for i, page in enumerate(pages):
                p = os.path.join(tmp_dir, f"pdf{i}.png")
                page.save(p, "PNG")
                image_paths.append(p)

            field_count = 0
            for p in image_paths:
                res = call_clova_ocr(p, SECRET_KEY, API_URL)
                if res:
                    field_count += sum(len(img.get("fields", [])) for img in res.get("images", []))
            times.append(time.perf_counter() - t0)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return times, field_count, None


def bench_masking_size(pdf_path):
    from pdf_text_extractor import extract_pdf_ocr_format
    from clova_ocr import extract_text_from_ocr_result
    from detector import detect_sensitive_items
    from mask_pdf import mask_text_pdf_from_ocr

    ocr_json = extract_pdf_ocr_format(pdf_path)
    texts = extract_text_from_ocr_result(ocr_json)

    detected = {}
    for t in texts:
        for k, v in detect_sensitive_items(t).items():
            detected.setdefault(k, set()).update(v)
    selected_items = [v for values in detected.values() for v in values]

    processed_dir = os.path.join(BASE_DIR, "processed")
    os.makedirs(processed_dir, exist_ok=True)
    out_path = os.path.join(processed_dir, "_bench_masked.pdf")

    mask_text_pdf_from_ocr(ocr_json, pdf_path, out_path, selected_items=selected_items)
    size = os.path.getsize(out_path)
    os.remove(out_path)
    return size, len(selected_items)


def main():
    if not os.path.exists(PDF_PATH):
        print(f"PDF 파일을 찾을 수 없습니다: {PDF_PATH}")
        return

    original_size = os.path.getsize(PDF_PATH)
    print(f"대상 PDF: {PDF_PATH} ({original_size / 1024:.1f} KB) / 반복 횟수: {TRIALS}\n")

    new_times, new_fields = bench_new_extraction(PDF_PATH, TRIALS)
    old_times, old_fields, old_skip_reason = bench_old_ocr(PDF_PATH, TRIALS)

    print("| 구분 | 평균 처리시간(초) | 추출 필드 수 | 비고 |")
    print("|---|---|---|---|")
    print(f"| 신규: PyMuPDF 직접 추출 | {avg(new_times):.4f} | {new_fields} | OCR 미호출 |")

    if old_times:
        old_avg = avg(old_times)
        new_avg = avg(new_times)
        print(f"| 기존: pdf2image + Clova OCR | {old_avg:.4f} | {old_fields} | DPI=100 |")
        print(f"\n처리 속도 향상: 약 {old_avg / new_avg:.1f}배 ({old_avg:.2f}초 → {new_avg:.4f}초)")
    else:
        print(f"| 기존: pdf2image + Clova OCR | 실행 불가 | - | {old_skip_reason} |")

    try:
        masked_size, item_count = bench_masking_size(PDF_PATH)
        print(f"\n탐지된 개인정보 {item_count}건 마스킹 후 파일 크기(벡터 리댁션): "
              f"{original_size / 1024:.1f} KB → {masked_size / 1024:.1f} KB")
    except Exception as e:
        print(f"\n마스킹 용량 비교 실패: {e}")


if __name__ == "__main__":
    main()
