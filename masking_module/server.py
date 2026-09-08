from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import json
import os
import logging
import threading
from concurrent.futures import ProcessPoolExecutor
from pdf2image import convert_from_path
import time
from config import POPPLER_PATH

# 로그 설정
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
# Gmail(https://mail.google.com, 공개 사이트)에서 이 로컬 서버(http://127.0.0.1, 사설
# 네트워크)로 보내는 요청은 크롬의 Private Network Access 정책에 걸려 프리플라이트
# 단계에서 기본 차단된다. allow_private_network=True로 이를 허용한다고 명시적으로 응답한다.
CORS(app, allow_private_network=True)

# 기본 디렉토리 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
PROCESSED_FOLDER = os.path.join(BASE_DIR, "processed")

# 업로드/처리된 파일 디렉토리 생성
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

# 마스킹 상태 저장 딕셔너리
masking_status = {}

# 프로세스 풀 워커에서 실행되는 함수라 모듈 최상위에 있어야 한다(pickle 가능해야 함).
def _ocr_worker(image_path):
    from paddle_ocr import call_paddle_ocr
    return call_paddle_ocr(image_path)

# ✅ 로컬 OCR 병렬 처리 함수 (페이지 순서 유지)
# PaddleOCR 인스턴스는 스레드 세이프하지 않아 스레드로는 병렬화할 수 없다. 대신 완전히
# 독립된 메모리를 쓰는 별도 프로세스로 나눠 돌린다 — 프로세스마다 PaddleOCR 모델을
# 새로 로드해야 하므로, 페이지가 1장뿐이면 그 초기화 비용이 더 커서 순차 처리한다.
# 기본 워커 수는 4로 제한한다(코어가 많아도 워커마다 모델 사본을 메모리에 올리므로
# 무작정 늘리면 메모리 사용량이 커진다) — 필요하면 max_workers를 조정한다.
def run_paddle_ocr_parallel(image_paths, max_workers=4):
    from paddle_ocr import call_paddle_ocr

    if len(image_paths) <= 1:
        return [call_paddle_ocr(p) for p in image_paths]

    workers = min(len(image_paths), os.cpu_count() or max_workers, max_workers)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(_ocr_worker, image_paths))

# ✅ 파일 업로드 및 OCR 처리 라우터
@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400

    file = request.files['file']
    filename = file.filename
    input_path = os.path.join(UPLOAD_FOLDER, filename)

    ocr_result = {}

    try:
        file.save(input_path)
        logging.info(f"파일 저장 완료: {input_path}")

        ext = os.path.splitext(filename)[1].lower()

        from clova_ocr import extract_text_from_ocr_result
        from paddle_ocr import call_paddle_ocr
        from detector import detect_sensitive_items

        if ext == ".pdf":
            from pdf_text_extractor import has_extractable_text, extract_pdf_ocr_format

            start_time = time.time()

            if has_extractable_text(input_path):
                # 텍스트 레이어가 있는 PDF: OCR 없이 직접 추출
                logging.info("텍스트 레이어 감지 → OCR 없이 직접 추출")
                ocr_result = extract_pdf_ocr_format(input_path)

                # 텍스트 레이어가 있어도 그 안에 삽입된 이미지(신분증 사진, 스크린샷 등)의
                # 내용은 실제 텍스트가 아니라서 위 추출로는 전혀 잡히지 않는다. 이미지를
                # 찾아 OCR로 보완하고, 그 결과를 페이지 좌표계로 변환해 병합한다.
                from pdf_text_extractor import extract_embedded_images
                embedded_images = extract_embedded_images(input_path)

                if embedded_images:
                    logging.info(f"임베디드 이미지 {len(embedded_images)}개 발견 → 보완 OCR 수행")
                    temp_paths = []
                    for idx, img in enumerate(embedded_images):
                        temp_path = os.path.join(UPLOAD_FOLDER, f"_embedded_{idx}.{img['image_ext']}")
                        with open(temp_path, 'wb') as tf:
                            tf.write(img['image_bytes'])
                        temp_paths.append(temp_path)

                    img_ocr_results = run_paddle_ocr_parallel(temp_paths)

                    for img_meta, img_ocr in zip(embedded_images, img_ocr_results):
                        if not img_ocr:
                            continue
                        page_idx = img_meta['page']
                        rx0, ry0, rx1, ry1 = img_meta['rect']
                        rw, rh = rx1 - rx0, ry1 - ry0
                        pw, ph = img_meta['pixel_width'], img_meta['pixel_height']

                        for page_result in img_ocr.get('images', []):
                            for field in page_result.get('fields', []):
                                verts = field['boundingPoly']['vertices']
                                # 이미지 픽셀 좌표 → 페이지(포인트) 좌표계로 변환
                                page_verts = [
                                    {"x": rx0 + (v['x'] / pw) * rw, "y": ry0 + (v['y'] / ph) * rh}
                                    for v in verts
                                ]
                                ocr_result['images'][page_idx]['fields'].append({
                                    "inferText": field['inferText'],
                                    "boundingPoly": {"vertices": page_verts},
                                    "_from_image": True,  # 마스킹 시 search_for 대신 좌표 기반 처리
                                })

                    for p in temp_paths:
                        os.remove(p)
            else:
                # 스캔 이미지 PDF: pdf2image로 변환 후 로컬 PaddleOCR 파이프라인
                logging.info("텍스트 레이어 없음 → PaddleOCR 파이프라인 사용")
                DPI = 100
                pages = convert_from_path(input_path, dpi=DPI, poppler_path=POPPLER_PATH)
                image_paths = []

                for page_num, page_image in enumerate(pages):
                    temp_img_path = os.path.join(UPLOAD_FOLDER, f"pdf{page_num}.png")
                    page_image.save(temp_img_path, "PNG")
                    image_paths.append(temp_img_path)

                logging.info(f"OCR 병렬 처리 시작: {len(image_paths)}페이지")
                ocr_results = run_paddle_ocr_parallel(image_paths)

                if not ocr_results:
                    return jsonify({"error": "OCR 결과를 처리할 수 없습니다."}), 500

                base_result = ocr_results[0]
                for res in ocr_results[1:]:
                    base_result["images"].extend(res["images"])
                ocr_result = base_result
                ocr_result["source"] = "paddle_ocr"

            extracted_text = extract_text_from_ocr_result(ocr_result)
            logging.info(f"[INFO] 인식된 텍스트 수: {len(extracted_text)}")

            detected_info = {}
            for text in extracted_text:
                result = detect_sensitive_items(text)
                for item, values in result.items():
                    detected_info.setdefault(item, set()).update(values)
            detected_info = {k: list(v) for k, v in detected_info.items()}

            ocr_json_path = os.path.join(BASE_DIR, f"{filename}_ocr.json")
            with open(ocr_json_path, 'w', encoding='utf-8') as f:
                json.dump(ocr_result, f, ensure_ascii=False, indent=4)

            end_time = time.time()
            print(f"PDF 개인정보 탐지 처리 시간 ({ocr_result.get('source', 'paddle_ocr')}): {end_time - start_time:.2f}초")

            return jsonify({
                "detected_info": detected_info,
                "filename": filename,
                "pdf_mode": ocr_result.get("source", "paddle_ocr")
            }), 200

        elif ext == ".xlsx":
            from mask_excel import extract_excel_sensitive_info
            extracted_text, detected_info = extract_excel_sensitive_info(input_path)
            return jsonify({"detected_info": detected_info, "filename": filename}), 200

        else:
            ocr_result = call_paddle_ocr(input_path)
            if not ocr_result:
                return jsonify({"error": "OCR 결과를 처리할 수 없습니다."}), 500

            extracted_text = extract_text_from_ocr_result(ocr_result)

            detected_info = {}
            for text in extracted_text:
                result = detect_sensitive_items(text)
                for item, values in result.items():
                    detected_info.setdefault(item, set()).update(values)
            detected_info = {k: list(v) for k, v in detected_info.items()}

            ocr_json_path = os.path.join(BASE_DIR, f"{filename}_ocr.json")
            with open(ocr_json_path, 'w', encoding='utf-8') as f:
                json.dump(ocr_result, f, ensure_ascii=False, indent=4)

            return jsonify({"detected_info": detected_info, "filename": filename}), 200

    except Exception as e:
        logging.error(f"업로드 오류: {e}")
        return jsonify({"error": str(e)}), 500

# ✅ 이메일 본문 텍스트(파일 아님) 개인정보 탐지 라우터
@app.route('/detect_text', methods=['POST'])
def detect_text():
    from detector import detect_sensitive_items

    data = request.get_json(silent=True) or {}
    text = data.get('text', '')

    if not text.strip():
        return jsonify({"detected_info": {}}), 200

    detected_info = {}
    result = detect_sensitive_items(text)
    for item, values in result.items():
        detected_info.setdefault(item, set()).update(values)
    detected_info = {k: list(v) for k, v in detected_info.items()}

    return jsonify({"detected_info": detected_info}), 200

# ✅ 받은 메일 피싱 의심도 탐지 라우터
@app.route('/detect_phishing', methods=['POST'])
def detect_phishing_route():
    from phishing_detector import detect_phishing

    data = request.get_json(silent=True) or {}
    text = data.get('text', '')
    links = data.get('links', [])

    result = detect_phishing(text, links)
    return jsonify(result), 200

# ✅ 비동기 마스킹 처리 함수
def process_masking(filename, mask_list, mask_style):
    start_time = time.time()
    logging.info(f"[마스킹 시작] {filename} (선택 항목 {len(mask_list)}건)")
    try:
        input_path = os.path.join(UPLOAD_FOLDER, filename)
        ocr_json_path = os.path.join(BASE_DIR, f"{filename}_ocr.json")
        output_filename = f"masked_{filename}"
        output_path = os.path.join(PROCESSED_FOLDER, output_filename)

        selected_texts = [item["text"] for item in mask_list]
        ext = os.path.splitext(filename)[1].lower()

        if ext == ".pdf":
            with open(ocr_json_path, 'r', encoding='utf-8') as f:
                ocr_json = json.load(f)

            if ocr_json.get("source") == "pdf_text":
                from mask_pdf import mask_text_pdf_from_ocr

                # 업로드 단계에서 임베디드 이미지 OCR로 보완한 필드는 "_from_image"로
                # 표시돼 있다 — 실제 PDF 텍스트 레이어에 없어서 search_for로 못 찾으므로
                # 좌표를 이미 아는 상태로 따로 넘겨준다.
                image_derived_fields = []
                for page_idx, page_data in enumerate(ocr_json.get("images", [])):
                    for field in page_data.get("fields", []):
                        if field.get("_from_image"):
                            verts = field["boundingPoly"]["vertices"]
                            bbox = [
                                min(v["x"] for v in verts), min(v["y"] for v in verts),
                                max(v["x"] for v in verts), max(v["y"] for v in verts),
                            ]
                            image_derived_fields.append({
                                "page": page_idx,
                                "bbox": bbox,
                                "text": field["inferText"],
                            })

                mask_text_pdf_from_ocr(
                    input_path=input_path,
                    output_path=output_path,
                    selected_items=selected_texts,
                    image_derived_fields=image_derived_fields
                )
            else:
                from mask_pdf import mask_pdf_from_ocr
                mask_pdf_from_ocr(
                    ocr_json=ocr_json,
                    input_path=input_path,
                    output_path=output_path,
                    selected_items=selected_texts,
                    mask_style=mask_style
                )

        elif ext == ".xlsx":
            from mask_excel import mask_excel_file
            mask_excel_file(input_path, output_path, selected_items=selected_texts)

        else:
            from mask_image import mask_image_from_ocr
            mask_image_from_ocr(
                image_path=input_path,
                output_path=output_path,
                selected_items=selected_texts,
                ocr_json_path=ocr_json_path,
                mask_style=mask_style
            )

        masking_status[filename] = {"done": True, "result_path": output_path}
        logging.info(f"[마스킹 완료] {filename} ({time.time() - start_time:.2f}초 소요): {output_path}")

    except Exception as e:
        masking_status[filename] = {"done": False, "error": str(e)}
        # str(e)만 남기면 실제 원인(어느 줄에서 왜 터졌는지)을 알 수 없어 스택 트레이스까지 남긴다.
        logging.exception(f"[마스킹 오류] {filename} ({time.time() - start_time:.2f}초 경과 후 실패)")

# ✅ 마스킹 요청 라우터
@app.route('/mask', methods=['POST'])
def mask_file():
    try:
        data = request.get_json()
        filename = data.get("filename")
        mask_list = data.get("mask_list", [])
        mask_style = data.get("maskStyle", "box")

        if not filename or not mask_list:
            return jsonify({"error": "필수 정보가 누락되었습니다."}), 400

        logging.info(f"[/mask 요청 수신] {filename}")
        masking_status[filename] = {"done": False}
        threading.Thread(target=process_masking, args=(filename, mask_list, mask_style)).start()
        return jsonify({"status": "processing", "filename": filename}), 202

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ✅ 마스킹 결과 다운로드
@app.route('/result', methods=['GET'])
def get_result():
    filename = request.args.get("filename")
    status = masking_status.get(filename)

    if not status:
        # 파일명이 서로 다르게 전달돼(인코딩 불일치 등) /mask 때 쓴 키와 안 맞으면
        # 여기서 계속 404가 나서 클라이언트는 "폴링만 반복하다 시간 초과"로 보인다.
        logging.warning(f"[/result 요청] 상태 없음: filename={filename!r}, 현재 키 목록={list(masking_status.keys())}")
        return jsonify({"error": "처리 중인 파일이 없습니다."}), 404

    if not status["done"]:
        return jsonify({"status": "processing"}), 202

    if status.get("error"):
        return jsonify({"error": status["error"]}), 500

    return send_file(status["result_path"], as_attachment=True)

if __name__ == '__main__':
    # threaded=True: 기본값(False)이면 서버가 한 번에 요청을 하나씩만 처리한다.
    # 확장이 마스킹 완료 폴링(/result) 외에도 본문 검사(/detect_text), 받은 메일
    # 피싱 검사(/detect_phishing) 등을 같은 서버에 동시에 요청할 수 있는데, 싱글
    # 스레드에서는 이 요청들이 순서대로 밀리면서 실제로는 서버가 이미 처리를 끝냈는데도
    # 클라이언트가 응답을 한참 늦게 받는 상황이 생길 수 있다.
    # use_reloader=False: 자동 재시작 감시자가 별도 프로세스를 띄우는 구조라 디버깅을
    # 복잡하게 만들 수 있어 꺼둔다 (코드 수정 후에는 직접 재시작하면 된다).
    app.run(debug=True, threaded=True, use_reloader=False)
