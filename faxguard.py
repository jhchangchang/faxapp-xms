#!/usr/bin/env python3
# ============================================================
#  faxguard.py  -  팩스 서버 실서비스용 예외처리 모듈 (v5 추가분)
#  faxserver_v4.py 와 함께 사용. import 해서 기능 보강.
#  표준 라이브러리만 사용.
#
#  담긴 예외처리:
#   1. 디스크 용량 감시 (팩스 저장 실패 예방)
#   2. 파일 무결성 검증 (TIFF 손상·빈 파일 차단)
#   3. 발신 번호 정규화 (국번/특수문자 정리)
#   4. 좀비 채널 자동 정리 (부하 후 잔여 세션 제거)
#   5. 재시도 백오프 + 지터 (동시 재시도 폭주 방지)
#   6. 결과 코드 → 사람이 읽는 진단 메시지 변환
#   7. 안전한 파일 이동 (부분 쓰기 방지)
#   8. 프로세스 락 (중복 실행 방지)
# ============================================================
import os, re, time, random, shutil, subprocess, tempfile, fcntl, errno

FAX_DIR = '/tmp/fax'
FS_CLI  = '/usr/local/freeswitch/bin/fs_cli'


# ---------- 1. 디스크 용량 감시 ----------
def disk_ok(path=FAX_DIR, min_free_mb=200):
    """저장 공간이 부족하면 False. 팩스 저장 전 호출."""
    try:
        st = os.statvfs(path)
        free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
        return free_mb >= min_free_mb, round(free_mb, 1)
    except Exception:
        return True, -1  # 확인 실패 시 통과(보수적으로 막지 않음)


# ---------- 2. 파일 무결성 검증 ----------
def valid_tiff(path):
    """TIFF 파일이 실제로 유효한지 최소 검증."""
    try:
        if not os.path.isfile(path):
            return False, 'not found'
        size = os.path.getsize(path)
        if size == 0:
            return False, 'empty file'
        if size < 100:
            return False, f'too small ({size}B)'
        # TIFF 매직넘버 확인 (II* 또는 MM*)
        with open(path, 'rb') as f:
            magic = f.read(4)
        if magic[:2] not in (b'II', b'MM'):
            return False, 'not a TIFF (bad magic)'
        return True, 'ok'
    except Exception as e:
        return False, str(e)


# ---------- 3. 발신 번호 정규화 ----------
def normalize_number(number):
    """번호에서 공백·하이픈·괄호 제거, 유효문자만 남김."""
    if not number:
        return ''
    n = re.sub(r'[\s\-()]', '', str(number))
    # 허용: 숫자, * # + 및 라우팅 접두(9 등)
    n = re.sub(r'[^0-9*#+]', '', n)
    return n


# ---------- 4. 좀비 채널 자동 정리 ----------
def cleanup_zombie_channels(max_age_sec=300):
    """오래 남아있는 채널을 강제 종료. 부하 테스트 후 잔여 세션 제거."""
    try:
        r = subprocess.run([FS_CLI, '-x', 'show channels as json'],
                           capture_output=True, text=True, timeout=6)
        import json
        data = json.loads(r.stdout) if r.stdout.strip() else {}
        killed = 0
        now = time.time()
        for row in data.get('rows', []):
            created = row.get('created_epoch')
            uuid = row.get('uuid')
            if not (created and uuid):
                continue
            try:
                age = now - int(created)
            except (ValueError, TypeError):
                continue
            if age > max_age_sec:
                subprocess.run([FS_CLI, '-x', f'uuid_kill {uuid}'],
                               capture_output=True, text=True, timeout=5)
                killed += 1
        return killed
    except Exception:
        return 0


# ---------- 5. 재시도 백오프 + 지터 ----------
def backoff_wait(attempt, base=2, cap=30):
    """지수 백오프에 랜덤 지터를 더해 동시 재시도 폭주 방지."""
    delay = min(base ** attempt, cap)
    jitter = random.uniform(0, delay * 0.3)
    time.sleep(delay + jitter)
    return round(delay + jitter, 1)


# ---------- 6. 결과 코드 → 진단 메시지 ----------
_HANGUP_DIAG = {
    'NORMAL_CLEARING':          '정상 종료',
    'USER_BUSY':                '상대 통화중',
    'NO_ANSWER':                '응답 없음 - 수신측 다이얼플랜/응답 확인',
    'NO_USER_RESPONSE':         '무응답 - 루프백 결선/D채널 확인',
    'CALL_REJECTED':            '거절됨 - 게이트웨이 인증/권한',
    'NORMAL_TEMPORARY_FAILURE': '일시적 실패 - E1 링크/B채널/라우팅 확인',
    'GATEWAY_DOWN':             '게이트웨이 다운 - 등록/전원/네트워크',
    'DESTINATION_OUT_OF_ORDER': '목적지 장애 - E1 포트 상태',
    'INVALID_GATEWAY':          '게이트웨이 미정의 - sofia gateway 설정 확인',
    'RECOVERY_ON_TIMER_EXPIRE': 'D채널 타이머 만료 - Q.921 확인',
    'INCOMPATIBLE_DESTINATION': '코덱/프로토콜 불일치 - T.38/G.711 확인',
}

_FAX_RESULT_DIAG = {
    'The T.38 negotiation failed':      'T.38 협상 실패 - 양단 T.38 설정/타이밍',
    'No carrier':                       '캐리어 없음 - 회선/루프 물리 확인',
    'Disconnected':                     '세션 중 끊김 - 네트워크 지터/타임아웃',
    'ECM':                              'ECM 오류정정 실패 - 회선 품질',
    'Invalid ECM':                      'ECM 데이터 오류',
}

def diagnose(hangup_cause='', fax_result=''):
    """행업 원인·팩스 결과를 사람이 읽는 진단으로."""
    msgs = []
    hc = (hangup_cause or '').upper()
    for key, msg in _HANGUP_DIAG.items():
        if key in hc:
            msgs.append(f'[콜] {msg}')
            break
    fr = fax_result or ''
    for key, msg in _FAX_RESULT_DIAG.items():
        if key.lower() in fr.lower():
            msgs.append(f'[팩스] {msg}')
            break
    return ' / '.join(msgs) if msgs else f'원인 미분류 (cause={hangup_cause}, result={fax_result})'


# ---------- 7. 안전한 파일 이동 ----------
def safe_move(src, dst):
    """임시 경로에 쓴 뒤 원자적 rename. 부분 쓰기로 인한 손상 방지."""
    try:
        d = os.path.dirname(dst)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d)
        os.close(fd)
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)   # 원자적
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


# ---------- 8. 프로세스 락 (중복 실행 방지) ----------
_lock_fh = None
def acquire_lock(lockfile='/tmp/faxserver.lock'):
    """이미 실행 중이면 False. 서버 중복 기동 방지."""
    global _lock_fh
    try:
        _lock_fh = open(lockfile, 'w')
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        return True
    except IOError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            return False  # 이미 다른 프로세스가 잡음
        return True
    except Exception:
        return True


# ---------- 발신 전 종합 점검 ----------
def preflight_send(number, tif_path):
    """
    발신 직전 모든 예외 조건을 한 번에 점검.
    반환: (ok: bool, normalized_number: str, error: str|None)
    """
    # 번호 정규화
    num = normalize_number(number)
    if not num or not re.fullmatch(r'[0-9*#+]{1,32}', num):
        return False, num, f'유효하지 않은 번호: {number}'

    # 디스크
    ok, free = disk_ok()
    if not ok:
        return False, num, f'디스크 부족: {free}MB 남음'

    # TIFF 무결성
    ok, msg = valid_tiff(tif_path)
    if not ok:
        return False, num, f'TIFF 오류: {msg}'

    return True, num, None


if __name__ == '__main__':
    # 자체 테스트
    print('=== faxguard 자체 점검 ===')
    print('디스크:', disk_ok())
    print('번호정규화:', normalize_number(' 031-222-9999 '), '/', normalize_number('9(1002)'))
    print('진단1:', diagnose('NORMAL_TEMPORARY_FAILURE'))
    print('진단2:', diagnose('NORMAL_CLEARING', 'The T.38 negotiation failed'))
    print('락 획득:', acquire_lock('/tmp/faxguard_test.lock'))
    # TIFF 검증 (없는 파일)
    print('TIFF검증(없음):', valid_tiff('/tmp/nope.tif'))

