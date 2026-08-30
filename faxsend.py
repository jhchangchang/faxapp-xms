#!/usr/bin/env python3
# 팩스 발송 API - 표준 라이브러리만 사용 (greenswitch 불필요)
import subprocess, json, os, uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
 
FS_CLI = '/usr/local/freeswitch/bin/fs_cli'
FAX_DIR = '/tmp/fax'
DEST_CTX = 'default'   # 다이얼플랜 컨텍스트
 
def pdf_to_tiff(pdf_path):
    '''PDF를 팩스 규격(G3, 204x98) 흑백 TIFF로 변환'''
    tif = os.path.join(FAX_DIR, f'send_{uuid.uuid4().hex}.tif')
    subprocess.run([
        'gs','-q','-dNOPAUSE','-dBATCH',
        '-sDEVICE=tiffg3','-r204x98',
        f'-sOutputFile={tif}', pdf_path
    ], check=True)
    return tif
 
def send_fax(number, pdf_path):
    '''PDF를 팩스로 발송. fs_cli로 originate+txfax 실행'''
    if not os.path.exists(pdf_path):
        return {'ok': False, 'error': f'file not found: {pdf_path}'}
    try:
        tif = pdf_to_tiff(pdf_path)
    except Exception as e:
        return {'ok': False, 'error': f'convert failed: {e}'}
    # originate 명령 구성 (loopback으로 테스트, 실제로는 sofia 게이트웨이로 변경)
    cmd = ('originate '
           '{origination_caller_id_number=faxsvc,fax_enable_t38=true}'
           f'loopback/{number}/{DEST_CTX} &txfax({tif})')
    r = subprocess.run([FS_CLI,'-x',cmd], capture_output=True, text=True, timeout=120)
    return {'ok': True, 'number': number, 'tiff': tif,
            'fs_response': r.stdout.strip()}
 
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        self.send_response(code)
        self.send_header('Content-Type','application/json')
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())
 
    def do_POST(self):
        if self.path != '/send':
            return self._send(404, {'error':'use POST /send'})
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length))
            number = str(data['number'])
            pdf = data['pdf']
        except Exception as e:
            return self._send(400, {'error': f'bad request: {e}'})
        result = send_fax(number, pdf)
        self._send(200 if result.get('ok') else 500, result)
 
    def log_message(self, *a):  # 기본 접속 로그 억제
        pass
 
if __name__ == '__main__':
    print('Fax send API : http://127.0.0.1:8080/send')
    print('요청 예: {"number":"1002","pdf":"/tmp/fax/sample.pdf"}')
    HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
