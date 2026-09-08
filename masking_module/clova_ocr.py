# clova_ocr.py
import time
import uuid
import json
import requests
import os

def call_clova_ocr(image_file_path, secret_key, api_url):
    """
    클로바 OCR API를 호출하여 OCR 결과를 반환하는 함수
    """
    # 요청 JSON 구성
    request_json = {
        'images': [
            {
                'format': 'jpg',
                'name': 'demo'
            }
        ],
        'requestId': str(uuid.uuid4()),
        'version': 'V2',
        'timestamp': int(round(time.time() * 1000))
    }

    # 요청 전송
    files = [('file', open(image_file_path, 'rb'))]
    payload = {'message': json.dumps(request_json).encode('UTF-8')}
    headers = {'X-OCR-SECRET': secret_key}

    try:
        response = requests.post(api_url, headers=headers, data=payload, files=files)
        
        if response.status_code == 200:
            ocr_results = response.json()
            return ocr_results
        else:
            raise Exception(f"OCR 요청 실패: 상태 코드 {response.status_code}, 응답: {response.text}")
    
    except Exception as e:
        print(f"OCR 호출 중 오류 발생: {e}")
        return None

def extract_text_from_ocr_result(ocr_results):
    """
    클로바 OCR 결과에서 텍스트만 추출하여 리스트로 반환하는 함수
    """
    all_texts = []
    for image_result in ocr_results.get('images', []):
        if 'fields' in image_result:
            for field in image_result['fields']:
                text = field.get('inferText', '')
                if text:
                    all_texts.append(text)
    return all_texts
