"""
FaxApp 설정. 환경변수로 오버라이드 가능.
"""
import os

# ---------- DB (PostgreSQL) ----------
DB_HOST = os.environ.get('FAXAPP_DB_HOST', '127.0.0.1')
DB_PORT = int(os.environ.get('FAXAPP_DB_PORT', '5432'))
DB_NAME = os.environ.get('FAXAPP_DB_NAME', 'faxapp')
DB_USER = os.environ.get('FAXAPP_DB_USER', 'faxapp')
DB_PASS = os.environ.get('FAXAPP_DB_PASS', 'faxapp')

def dsn():
    return f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASS}"

# ---------- 팩스 서버 연동 ----------
# 앞서 만든 faxserver_v6.py 의 주소
FAX_SERVER_URL = os.environ.get('FAXAPP_FAX_SERVER', 'http://127.0.0.1:8090')

# ---------- 인증 ----------
SECRET_KEY = os.environ.get('FAXAPP_SECRET', 'change-this-in-production')
SESSION_HOURS = int(os.environ.get('FAXAPP_SESSION_HOURS', '8'))

# ---------- 파일 저장 ----------
UPLOAD_DIR = os.environ.get('FAXAPP_UPLOAD_DIR', '/opt/faxapp/uploads')
RECV_DIR   = os.environ.get('FAXAPP_RECV_DIR', '/opt/faxapp/received')
# 업로드 최대 크기(MB) - 과대 파일로 디스크/메모리 소진 방지
MAX_UPLOAD_MB = int(os.environ.get('FAXAPP_MAX_UPLOAD_MB', '30'))
# 최소 디스크 여유(MB) - 이하면 업로드 거부 (디스크 채움 방지)
MIN_DISK_FREE_MB = int(os.environ.get('FAXAPP_MIN_DISK_FREE_MB', '500'))

# ---------- 앱 ----------
APP_HOST = os.environ.get('FAXAPP_HOST', '0.0.0.0')
APP_PORT = int(os.environ.get('FAXAPP_PORT', '8100'))

# ---------- 보안 ----------
# 발송 Rate Limit (스팸/과금 폭탄 방어). 사용자별.
RATE_SEND_PER_MIN  = int(os.environ.get('FAXAPP_RATE_SEND_MIN', '30'))    # 분당
RATE_SEND_PER_HOUR = int(os.environ.get('FAXAPP_RATE_SEND_HOUR', '300'))  # 시간당

# webhook 인증 토큰 (엔진→앱 결과통지 검증). 비면 localhost만 허용.
#   엔진(faxserver)에도 같은 토큰을 설정하면 X-Fax-Token 헤더로 검증.
WEBHOOK_TOKEN = os.environ.get('FAXAPP_WEBHOOK_TOKEN', '')
# webhook 허용 IP (비면 127.0.0.1/localhost만). 콤마구분.
WEBHOOK_ALLOW_IPS = os.environ.get('FAXAPP_WEBHOOK_IPS', '127.0.0.1,::1')
