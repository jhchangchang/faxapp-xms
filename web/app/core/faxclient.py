"""
팩스 서버(faxserver_v6.py) 연동 클라이언트.
애플리케이션이 실제 팩스 송수신을 팩스 서버에 위임하는 통로.
표준 라이브러리(urllib)만 사용 - 별도 HTTP 라이브러리 불필요.
"""
import json
import urllib.request
import urllib.error
from . import config


class FaxServerError(Exception):
    pass


def _request(method, path, body=None, timeout=15):
    """팩스 서버에 HTTP 요청."""
    url = config.FAX_SERVER_URL.rstrip('/') + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # 팩스 서버가 에러 응답을 보낸 경우, 본문 파싱 시도
        try:
            return json.loads(e.read().decode())
        except Exception:
            raise FaxServerError(f'팩스 서버 오류 {e.code}: {e.reason}')
    except urllib.error.URLError as e:
        raise FaxServerError(f'팩스 서버 연결 실패: {e.reason}')
    except Exception as e:
        raise FaxServerError(f'팩스 서버 요청 오류: {e}')


# ---------- 팩스 서버 API 래퍼 ----------
def health():
    """팩스 서버 상태 확인."""
    try:
        return _request('GET', '/health', timeout=5)
    except FaxServerError:
        return {'ok': False}


def line_diag():
    """회선·게이트웨이 종합 진단 (입고 당일 물리 연결 점검용)."""
    try:
        return _request('GET', '/line-diag', timeout=10)
    except FaxServerError:
        return {'overall': 'fail', 'checks': [],
                'summary': '팩스 서버에 연결할 수 없습니다'}


def send_fax(number, file_path, async_mode=True, job_id=None):
    """
    팩스 발송 요청. 팩스 서버의 /send 엔드포인트 호출.
    async_mode=True면 큐에 넣고 즉시 반환(202).
    job_id: 앱의 fax_jobs.id - 팩스 서버가 결과통지 시 되돌려줌 (매칭용).
    반환: 팩스 서버 응답 dict (job_id 등)
    """
    body = {'number': number, 'pdf': file_path, 'async': async_mode}
    if job_id is not None:
        body['job_id'] = str(job_id)
    return _request('POST', '/send', body)


def get_status():
    """팩스 서버 전체 상태(/api) - 채널·성공률·큐 등."""
    return _request('GET', '/api')


def get_job_status(server_job_id):
    """특정 발송 작업의 상태 조회. 팩스 서버가 지원하면 사용."""
    try:
        return _request('GET', f'/job/{server_job_id}', timeout=5)
    except FaxServerError:
        return None


def get_diagnostics():
    """진단 로그·통계(/diag)."""
    return _request('GET', '/diag')


def get_alarms():
    """알람 목록(/alarms)."""
    return _request('GET', '/alarms')


def server_available():
    """팩스 서버 가용 여부 (빠른 체크)."""
    h = health()
    return bool(h.get('ok'))
