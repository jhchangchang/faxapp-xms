#!/usr/bin/env python3
# ============================================================
#  FreeSWITCH Dashboard v6 - 통합 팩스 서버 (실서비스 예외처리 강화판)
#  v4 기능 + faxguard 모듈(디스크감시·TIFF검증·번호정규화·
#  좀비채널정리·진단메시지·프로세스락) 통합
#  표준 라이브러리만 사용 (외부 의존성 없음)
#  ※ faxguard.py 를 같은 폴더에 두세요.
# ============================================================
import subprocess, json, re, os, time, socket, threading, sqlite3, uuid, datetime, queue, logging, signal, sys
import platform, shutil, glob
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# faxguard 예외처리 모듈 (없으면 기능 축소 모드로 계속 동작)
try:
    import faxguard as guard
    HAS_GUARD = True
except Exception:
    HAS_GUARD = False

# faxdiag 진단·네트워크캡처 모듈
try:
    import faxdiag as diag
    HAS_DIAG = True
except Exception:
    HAS_DIAG = False

# ---------------- 설정 ----------------
FS_CLI      = '/usr/local/freeswitch/bin/fs_cli'
FAX_DIR     = '/tmp/fax'
DB_PATH     = '/opt/faxapp/fax.db'
LOG_PATH    = '/opt/faxapp/faxserver.log'
DEST_CTX    = 'default'
ESL_HOST, ESL_PORT, ESL_PASS = '127.0.0.1', 8021, 'ClueCon'
WEB_PORT    = 8090

# 동시성 제어: 한 번에 실제로 발신할 최대 채널 수 (폭주 방지의 핵심)
MAX_CONCURRENT = int(os.environ.get('FAX_MAX_CONCURRENT', '40'))
SEND_RETRIES   = 3          # 발신 실패 시 재시도 횟수
RETRY_BACKOFF  = 3          # 재시도 간 대기(초), 시도마다 배수 증가
FS_TIMEOUT     = 130        # fs_cli 타임아웃(초)
GW_NAME        = os.environ.get('FAX_GATEWAY', '')   # 실환경 게이트웨이명(없으면 loopback)
CLEANUP_HOURS  = 24         # 임시 tif 정리 기준(시간)

# 앱(faxapp) 발송결과 webhook - 발송 완료 시 결과를 앱에 통지
APP_RESULT_WEBHOOK = os.environ.get('FAX_APP_WEBHOOK',
                                    'http://127.0.0.1:8100/api/fax/webhook/result')

SERVER_STARTED = time.time()

# ---------------- 로깅 ----------------
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(),
              logging.FileHandler(LOG_PATH, encoding='utf-8')])
log = logging.getLogger('faxops')

# ---------------- 전역 상태 ----------------
_prev = {'idle': 0, 'total': 0}
_seen = set()
_seen_lock = threading.Lock()
_send_sem = threading.Semaphore(MAX_CONCURRENT)   # 동시 발신 제한
_send_q = queue.Queue()                            # 발송 작업 큐
_stats_lock = threading.Lock()
_gw_status = {'name': GW_NAME or 'loopback', 'state': 'N/A', 'checked': '-'}
_shutdown = threading.Event()

# ---------------- 알람 엔진 ----------------
_alarms = []                    # 알람 리스트 (최신순)
_alarm_lock = threading.Lock()
_alarm_seq = 0
# 임계치 설정
ALARM_SUCCESS_MIN = float(os.environ.get('FAX_ALARM_SUCCESS_MIN', '90'))  # 성공률 %
ALARM_CPU_MAX     = float(os.environ.get('FAX_ALARM_CPU_MAX', '80'))       # CPU %
ALARM_QUEUE_MAX   = int(os.environ.get('FAX_ALARM_QUEUE_MAX', '50'))       # 큐 적체
_alarm_dedup = {}               # 같은 알람 반복 억제 (key -> last_time)

def raise_alarm(level, title, desc, dedup_key=None, cooldown=300):
    """알람 발생. dedup_key로 같은 알람 반복 억제(cooldown 초)."""
    global _alarm_seq
    now = time.time()
    if dedup_key:
        last = _alarm_dedup.get(dedup_key, 0)
        if now - last < cooldown:
            return  # 쿨다운 중 - 중복 억제
        _alarm_dedup[dedup_key] = now
    with _alarm_lock:
        _alarm_seq += 1
        _alarms.insert(0, {
            'id': _alarm_seq, 'level': level, 'title': title, 'desc': desc,
            'time': time.strftime('%H:%M:%S'), 'unread': 1,
            'dedup_key': dedup_key,
        })
        del _alarms[200:]   # 최대 200개 보관
    log.info(f'[알람:{level}] {title} - {desc}')

def alarms_snapshot():
    with _alarm_lock:
        return {'alarms': list(_alarms),
                'unread': sum(1 for a in _alarms if a['unread'])}

def alarms_ack():
    with _alarm_lock:
        for a in _alarms:
            a['unread'] = 0
    return True

def resolve_alarm(dedup_key):
    """복구 시 해당 알람을 해소(제거)하고 dedup도 리셋해 재발생 가능하게."""
    removed = 0
    with _alarm_lock:
        before = len(_alarms)
        _alarms[:] = [a for a in _alarms if a.get('dedup_key') != dedup_key]
        removed = before - len(_alarms)
    _alarm_dedup.pop(dedup_key, None)
    if removed:
        log.info(f'[알람해소] {dedup_key} ({removed}건)')
    return removed

def alarms_clear():
    with _alarm_lock:
        _alarms.clear()
    return True

def alarm_monitor_loop():
    """주기적으로 임계치를 검사해 알람 발생."""
    # 부팅 직후 FreeSWITCH가 완전히 올라올 때까지 유예 (오탐 방지)
    for _ in range(15):
        if _shutdown.is_set():
            return
        if fs_alive():
            break
        time.sleep(2)
    prev_fs_alive = True
    prev_gw = None
    while not _shutdown.is_set():
        try:
            # 게이트웨이 다운 감지
            gw = _gw_status.get('state', '')
            if prev_gw and gw != prev_gw and ('DOWN' in gw.upper() or 'FAIL' in gw.upper() or gw == 'UNKNOWN'):
                raise_alarm('crit', '게이트웨이 상태 이상',
                            f'{_gw_status["name"]} 상태: {gw}', dedup_key='gw_down')
            elif prev_gw and gw != prev_gw and ('REGED' in gw.upper() or 'UP' in gw.upper()):
                resolve_alarm('gw_down')   # 게이트웨이 복구
            prev_gw = gw
            # FreeSWITCH 다운
            alive = fs_alive()
            if prev_fs_alive and not alive:
                raise_alarm('crit', 'FreeSWITCH 응답 없음',
                            '팩스 엔진이 응답하지 않습니다', dedup_key='fs_down')
            elif not prev_fs_alive and alive:
                # 복구됨 - 알람 해소
                resolve_alarm('fs_down')
            prev_fs_alive = alive
            # CPU
            cpu = cpu_percent()
            if cpu > ALARM_CPU_MAX:
                raise_alarm('warn', 'CPU 부하 높음',
                            f'CPU {cpu}% (임계치 {ALARM_CPU_MAX}% 초과)', dedup_key='cpu_high')
            # 큐 적체
            q = _send_q.qsize()
            if q > ALARM_QUEUE_MAX:
                raise_alarm('warn', '발송 큐 적체',
                            f'대기 {q}건 (임계치 {ALARM_QUEUE_MAX} 초과)', dedup_key='queue_high')
            # 성공률 (누적)
            ct = db_counts()
            tot = sum(ct.values()); ok = ct['sent_ok'] + ct['recv_ok']
            if tot >= 10:
                rate = 100 * ok / tot
                if rate < ALARM_SUCCESS_MIN:
                    raise_alarm('warn', '전송 성공률 하락',
                                f'성공률 {round(rate,1)}% (임계치 {ALARM_SUCCESS_MIN}% 미만)',
                                dedup_key='success_low', cooldown=600)
        except Exception as e:
            log.warning(f'알람 모니터 오류: {e}')
        _shutdown.wait(20)

# ---------------- DB (재시도 래퍼 + 마이그레이션) ----------------
def _db_exec(sqls, params_list=None, fetch=None, retries=5):
    """SQLite busy 대비 재시도 래퍼. sqls: 단일 문자열 또는 리스트."""
    if isinstance(sqls, str):
        sqls = [sqls]; params_list = [params_list or ()]
    last = None
    for attempt in range(retries):
        try:
            con = sqlite3.connect(DB_PATH, timeout=15)
            con.execute('PRAGMA journal_mode=WAL')   # 동시 읽기/쓰기 개선
            cur = con.cursor()
            result = None
            for sql, prm in zip(sqls, params_list):
                cur.execute(sql, prm or ())
                if fetch:
                    result = cur.fetchall()
            con.commit(); con.close()
            return result
        except sqlite3.OperationalError as e:
            last = e
            time.sleep(0.2 * (attempt + 1))
        except Exception as e:
            log.error(f'DB 오류: {e}')
            return None
    log.error(f'DB 재시도 초과: {last}')
    return None

def db_init():
    _db_exec('''CREATE TABLE IF NOT EXISTS fax_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        direction TEXT, number TEXT, file TEXT,
        pages INTEGER, success INTEGER, rate TEXT, note TEXT, created TEXT)''')
    # 구버전 호환: 없는 컬럼 자동 추가
    cols = _db_exec("PRAGMA table_info(fax_log)", fetch=True) or []
    names = [r[1] for r in cols]
    for col in ('note', 'rate'):
        if col not in names:
            _db_exec(f'ALTER TABLE fax_log ADD COLUMN {col} TEXT')
    log.info('DB 초기화 완료')

def db_log(direction, number, file, pages, success, rate='', note=''):
    _db_exec('INSERT INTO fax_log(direction,number,file,pages,success,rate,note,created)'
             ' VALUES(?,?,?,?,?,?,?,?)',
             (direction, number, file, int(pages or 0), int(bool(success)), rate, note,
              datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

def db_recent(limit=60):
    rows = _db_exec('SELECT id,direction,number,pages,success,rate,note,created'
                    ' FROM fax_log ORDER BY id DESC LIMIT ?', (limit,), fetch=True) or []
    keys = ['id','direction','number','pages','success','rate','note','created']
    return [dict(zip(keys, r)) for r in rows]

def db_delete_one(rec_id):
    """전송 기록 1건 삭제."""
    _db_exec('DELETE FROM fax_log WHERE id=?', (int(rec_id),))
    return True

def db_delete_many(ids):
    """선택한 전송 기록 삭제."""
    ids = [int(i) for i in (ids or [])][:1000]
    if not ids:
        return 0
    qmarks = ','.join('?' * len(ids))
    _db_exec(f'DELETE FROM fax_log WHERE id IN ({qmarks})', tuple(ids))
    return len(ids)

def db_counts():
    def q(cond):
        r = _db_exec(f"SELECT COUNT(*) FROM fax_log WHERE {cond}", fetch=True)
        return r[0][0] if r else 0
    return {'sent_ok': q("direction='send' AND success=1"),
            'sent_no': q("direction='send' AND success=0"),
            'recv_ok': q("direction='receive' AND success=1"),
            'recv_no': q("direction='receive' AND success=0")}

def db_clear():
    _db_exec('DELETE FROM fax_log')
    with _seen_lock:
        _seen.clear()
    log.info('전송 기록 초기화됨')
    return True

# ---------------- 입력 검증 ----------------
def valid_number(number):
    """번호 형식 검증 - 숫자/일부 기호만 허용 (명령 인젝션 방지)"""
    return bool(re.fullmatch(r'[0-9A-Za-z*#+._-]{1,32}', number or ''))

def valid_path(path):
    """경로 검증 - 존재하는 파일이며 FAX_DIR 또는 허용 경로 내"""
    if not path or not isinstance(path, str):
        return False
    if any(c in path for c in [';', '|', '&', '`', '$', '\n']):
        return False
    return os.path.isfile(path)

# ---------------- FreeSWITCH ----------------
def fs_api(cmd, timeout=6):
    try:
        r = subprocess.run([FS_CLI, '-x', cmd], capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning(f'fs_api 타임아웃: {cmd[:40]}')
        return ''
    except FileNotFoundError:
        log.error('fs_cli를 찾을 수 없음 - FreeSWITCH 설치 경로 확인')
        return ''
    except Exception as e:
        log.warning(f'fs_api 오류: {e}')
        return ''

def fs_alive(retries=2):
    """FreeSWITCH 응답 확인. 일시적 실패(재기동 중 등)에 오탐하지 않도록 재시도.
    status 출력에 'UP'이나 버전 정보가 있으면 정상으로 확실히 판정."""
    for i in range(retries + 1):
        out = fs_api('status')
        if out and ('UP ' in out or 'FreeSWITCH' in out or 'session' in out.lower()):
            return True
        if out:  # 뭔가 응답은 왔음 (비어있지 않으면 살아있다고 봄)
            return True
        if i < retries:
            time.sleep(1)   # 잠깐 기다렸다 재시도 (재기동 중일 수 있음)
    return False

def channel_count():
    out = fs_api('show channels count')
    m = re.search(r'(\d+)\s+total', out)
    return int(m.group(1)) if m else 0

def channel_list():
    out = fs_api('show channels as json')
    try:
        data = json.loads(out) if out else {}
        rows = []
        for r in data.get('rows', [])[:60]:
            # 통화 시간 계산 (created_epoch 기준)
            dur = ''
            try:
                ce = int(r.get('created_epoch', 0))
                if ce:
                    sec = max(0, int(time.time()) - ce)
                    dur = f'{sec//60}:{sec%60:02d}'
            except Exception:
                pass
            rows.append({
                'uuid': (r.get('uuid', '') or '')[:8],
                'state': r.get('callstate') or r.get('state', ''),
                'dir': r.get('direction', ''),
                'cid_num': r.get('cid_num', '') or r.get('cid_name', ''),
                'dest': r.get('dest', ''),
                'application': r.get('application', ''),
                'codec': r.get('read_codec', '') or r.get('write_codec', ''),
                'duration': dur,
                'created': r.get('created', ''),
            })
        return rows
    except Exception:
        return []


def channel_detail_stats():
    """채널 통계 요약 (방향별/상태별)."""
    chs = channel_list()
    return {
        'total': len(chs),
        'inbound': sum(1 for c in chs if c.get('dir') == 'inbound'),
        'outbound': sum(1 for c in chs if c.get('dir') == 'outbound'),
        'active': sum(1 for c in chs if c.get('state') in ('ACTIVE', 'CS_EXECUTE')),
        'channels': chs,
    }


# ==================== 패킷 캡처 ====================
_captures = []            # 캡처 기록 (메모리)
_capture_lock = threading.Lock()
CAPTURE_DIR = os.environ.get('FAX_CAPTURE_DIR', '/tmp/fax/captures')
_active_capture = {'proc': None, 'file': None, 'started': None, 'target': None}


def capture_start(target_ip='', duration=60):
    """SIP 패킷 캡처 시작 (tcpdump). target_ip 지정 시 해당 호스트만.
    반환: (성공, 메시지)."""
    global _active_capture
    if _active_capture.get('proc'):
        return (False, '이미 캡처 진행 중입니다')
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')
    fname = f'sip_{ts}.pcap'
    fpath = os.path.join(CAPTURE_DIR, fname)
    # tcpdump 필터: SIP(5060) + RTP, target 있으면 host 제한
    filt = 'port 5060 or (udp and portrange 16384-32768)'
    if target_ip:
        filt = f'host {target_ip} and ({filt})'
    dur = max(5, min(int(duration), 300))   # 5~300초
    try:
        # 실제 tcpdump 실행 (백그라운드)
        cmd = ['tcpdump', '-i', 'any', '-w', fpath, '-G', str(dur),
               '-W', '1', '-n', filt]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        _active_capture = {'proc': proc, 'file': fname, 'path': fpath,
                          'started': time.strftime('%H:%M:%S'),
                          'target': target_ip or '전체', 'duration': dur}
        # duration 후 자동 종료 스레드
        def _auto_stop():
            proc.wait()
            _finish_capture()
        threading.Thread(target=_auto_stop, daemon=True).start()
        return (True, f'캡처 시작 ({dur}초, {target_ip or "전체"})')
    except FileNotFoundError:
        return (False, 'tcpdump가 설치되어 있지 않습니다')
    except Exception as e:
        return (False, f'캡처 시작 실패: {e}')


def _finish_capture():
    """캡처 완료 처리 - 파일 분석 후 기록."""
    global _active_capture
    ac = _active_capture
    if not ac.get('file'):
        return
    fpath = ac.get('path', '')
    # 패킷 수 / SIP 메시지 분석
    packets = 0
    sip_msgs = []
    try:
        if os.path.isfile(fpath):
            # tcpdump로 읽어서 요약
            r = subprocess.run(['tcpdump', '-r', fpath, '-n'],
                             capture_output=True, text=True, timeout=15)
            lines = r.stdout.strip().split('\n')
            packets = len([l for l in lines if l.strip()])
            # SIP 메서드 추출 (INVITE, 200 OK 등)
            for l in lines:
                for m in ('INVITE', '100 Trying', '180 Ringing', '200 OK',
                          'ACK', 'BYE', '486', '404', '488'):
                    if m in l:
                        sip_msgs.append(m)
                        break
    except Exception:
        pass
    size = os.path.getsize(fpath) if os.path.isfile(fpath) else 0
    with _capture_lock:
        _captures.insert(0, {
            'file': ac['file'], 'started': ac.get('started', ''),
            'target': ac.get('target', ''), 'duration': ac.get('duration', 0),
            'packets': packets, 'size_kb': round(size/1024, 1),
            'sip_flow': sip_msgs[:20],  # SIP 흐름
        })
        del _captures[50:]  # 최근 50개만
    _active_capture = {'proc': None, 'file': None, 'started': None, 'target': None}


def capture_stop():
    """진행 중인 캡처 강제 종료."""
    ac = _active_capture
    if ac.get('proc'):
        try:
            ac['proc'].terminate()
        except Exception:
            pass
        return (True, '캡처 중지됨')
    return (False, '진행 중인 캡처 없음')


def capture_status():
    """캡처 상태 + 기록."""
    with _capture_lock:
        caps = list(_captures)
    ac = _active_capture
    return {
        'active': bool(ac.get('proc')),
        'active_info': {
            'file': ac.get('file', ''), 'target': ac.get('target', ''),
            'started': ac.get('started', ''), 'duration': ac.get('duration', 0),
        } if ac.get('proc') else None,
        'captures': caps,
        'tcpdump_available': shutil.which('tcpdump') is not None,
    }


def capture_delete(fname):
    """캡처 파일 삭제."""
    import re as _re
    if not _re.match(r'^sip_[\d_]+\.pcap$', fname):
        return False
    fpath = os.path.join(CAPTURE_DIR, fname)
    try:
        if os.path.isfile(fpath):
            os.remove(fpath)
        with _capture_lock:
            _captures[:] = [c for c in _captures if c['file'] != fname]
        return True
    except Exception:
        return False


def dial_string(number, tif, job_id=None):
    ep = f'sofia/gateway/{GW_NAME}/{number}' if GW_NAME else f'loopback/{number}/{DEST_CTX}'
    # job_id를 채널변수로 심어 ESL 이벤트에서 되읽음 (앱 결과통지 매칭용)
    jobvar = f',faxapp_job_id={job_id}' if job_id else ''
    # ECM(오류정정) 명시적 활성화 - 회선 품질이 낮아도 손상된 행을 재전송해 보정.
    # fax_use_ecm=true: ECM 사용, fax_enable_t38_request=true: T.38 협상 요청
    faxvars = 'fax_enable_t38=true,fax_use_ecm=true,fax_enable_t38_request=true'
    return f'{{origination_caller_id_number=faxsvc,{faxvars}{jobvar}}}{ep} &txfax({tif})'


def notify_app_result(server_job_id, success, **fields):
    """발송 결과를 앱(faxapp) webhook에 통지. 표준 라이브러리만 사용.
    실패해도 팩스 서버 동작에 영향 없도록 예외를 삼킴."""
    if not server_job_id:
        return
    import urllib.request
    payload = {'server_job_id': str(server_job_id), 'success': bool(success)}
    payload.update({k: v for k, v in fields.items() if v is not None})
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            APP_RESULT_WEBHOOK, data=data,
            headers={'Content-Type': 'application/json'}, method='POST')
        urllib.request.urlopen(req, timeout=5).read()
        log.info(f'앱 결과통지: job={server_job_id} success={success}')
    except Exception as e:
        # 통지 실패는 로그만 - 앱의 폴링(retry_worker)이 백업으로 동작
        log.warning(f'앱 결과통지 실패 (폴링으로 대체됨): {e}')

# ---------------- 게이트웨이 헬스체크 ----------------
def line_diagnostics():
    """회선·게이트웨이 종합 진단 - 입고 당일 물리 연결 점검용.
    E1 링크 → 게이트웨이 등록 → T.38 설정을 순서대로 확인.
    각 항목: {key, label, ok, detail, hint(문제 시 조치)}"""
    checks = []

    # 1. FreeSWITCH 코어
    alive = fs_alive()
    checks.append({
        'key': 'fs', 'label': 'FreeSWITCH 코어', 'ok': alive,
        'detail': '정상 응답' if alive else '응답 없음',
        'hint': '' if alive else 'systemctl status freeswitch 확인',
    })
    if not alive:
        return {'checks': checks, 'overall': 'fail',
                'summary': 'FreeSWITCH가 응답하지 않아 이후 점검 불가'}

    # 2. Sofia(SIP) 프로파일 상태
    sofia = fs_api('sofia status')
    ext_up = 'external' in sofia and ('RUNNING' in sofia or 'running' in sofia)
    checks.append({
        'key': 'sofia', 'label': 'SIP 프로파일 (external)', 'ok': ext_up,
        'detail': 'RUNNING' if ext_up else '프로파일 미기동',
        'hint': '' if ext_up else 'sofia profile external start / vega400.xml 확인',
    })

    # 3. 게이트웨이 등록
    gw_raw = fs_api(f'sofia status gateway {GW_NAME}') if GW_NAME else ''
    gw_state = ''
    for line in gw_raw.splitlines():
        if 'Status' in line or 'State' in line:
            gw_state = line.split()[-1] if line.split() else ''
            break
    gw_ok = 'REGED' in gw_raw.upper() or 'UP' in gw_raw.upper() or 'NOREG' in gw_raw.upper()
    # NOREG는 등록은 안 됐지만 게이트웨이 정의는 됨 (IP 직결 모드일 수 있음)
    reged = 'REGED' in gw_raw.upper()
    checks.append({
        'key': 'gateway', 'label': f'게이트웨이 ({GW_NAME or "미설정"})',
        'ok': gw_ok,
        'detail': (gw_state or ('등록됨' if reged else '정의됨')) if gw_ok else '게이트웨이 없음/응답없음',
        'hint': '' if gw_ok else 'vega400.xml proxy IP 확인 (Vega=192.168.219.138)',
    })

    # 4. T.38 모듈 로드
    mods = fs_api('module_exists mod_spandsp')
    t38_ok = 'true' in mods.lower()
    checks.append({
        'key': 't38', 'label': 'T.38 엔진 (mod_spandsp)', 'ok': t38_ok,
        'detail': '로드됨' if t38_ok else '미로드',
        'hint': '' if t38_ok else 'load mod_spandsp / modules.conf.xml 확인',
    })

    # 5. 활성 채널 (지금 통화 중인지 - 참고용)
    ch = fs_api('show channels count')
    ch_n = 0
    for tok in ch.split():
        if tok.isdigit():
            ch_n = int(tok); break
    checks.append({
        'key': 'channels', 'label': '현재 활성 채널', 'ok': True,
        'detail': f'{ch_n}개 세션', 'hint': '',
    })

    fails = [c for c in checks if not c['ok']]
    return {
        'checks': checks,
        'overall': 'ok' if not fails else 'fail',
        'summary': ('모든 회선 점검 통과 - 발송 테스트 가능'
                    if not fails else f'{fails[0]["label"]}에서 문제 - 조치 필요'),
    }


def gw_healthcheck_loop():
    while not _shutdown.is_set():
        try:
            if GW_NAME:
                out = fs_api(f'sofia status gateway {GW_NAME}')
                m = re.search(r'Status\s+(\w+)', out)
                state = m.group(1) if m else 'UNKNOWN'
            elif fs_alive():
                state = 'UP(loopback)'
            else:
                state = 'FS_DOWN'
            _gw_status['state'] = state
            _gw_status['checked'] = time.strftime('%H:%M:%S')
        except Exception as e:
            _gw_status['state'] = 'ERR'
            log.warning(f'헬스체크 오류: {e}')
        _shutdown.wait(15)

# ---------------- 발송 (변환/재시도/동시성) ----------------
def pdf_to_tiff(pdf_path):
    tif = os.path.join(FAX_DIR, f'send_{uuid.uuid4().hex}.tif')
    r = subprocess.run(['gs','-q','-dNOPAUSE','-dBATCH','-sDEVICE=tiffg3','-r204x98',
                        f'-sOutputFile={tif}', pdf_path],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not os.path.isfile(tif):
        raise RuntimeError(f'gs 변환 실패: {r.stderr[:120]}')
    return tif

def _do_send(number, pdf_path, job_id=None):
    if not valid_number(number):
        notify_app_result(job_id, False, result_text=f'invalid number: {number}',
                          result_code='INVALID_NUMBER')
        return {'ok': False, 'error': f'invalid number: {number}'}
    if not valid_path(pdf_path):
        notify_app_result(job_id, False, result_text=f'invalid or missing file',
                          result_code='BAD_TIFF')
        return {'ok': False, 'error': f'invalid or missing file: {pdf_path}'}
    # 이미 tif면 변환 생략
    try:
        tif = pdf_path if pdf_path.lower().endswith('.tif') else pdf_to_tiff(pdf_path)
    except Exception as e:
        db_log('send', number, pdf_path, 0, 0, note='convert_fail')
        log.error(f'변환 실패 {number}: {e}')
        notify_app_result(job_id, False, result_text=f'convert failed: {e}',
                          result_code='CONVERSION')
        return {'ok': False, 'error': f'convert failed: {e}'}

    # ── faxguard 발신 전 종합 점검 (디스크·TIFF무결성·번호정규화) ──
    if HAS_GUARD:
        ok, num_norm, err = guard.preflight_send(number, tif)
        if not ok:
            db_log('send', number, tif, 0, 0, note='preflight_fail')
            log.error(f'발신 전 점검 실패 {number}: {err}')
            notify_app_result(job_id, False, result_text=str(err),
                              result_code='BAD_TIFF')
            return {'ok': False, 'number': number, 'error': err}
        number = num_norm  # 정규화된 번호 사용

    last = ''
    with _send_sem:   # 동시 발신 수 제한
        for attempt in range(1, SEND_RETRIES + 1):
            if not fs_alive():
                last = 'FreeSWITCH down'
                log.warning('발신 보류 - FreeSWITCH 응답 없음')
                time.sleep(RETRY_BACKOFF)
                continue
            try:
                r = subprocess.run([FS_CLI, '-x', 'originate ' + dial_string(number, tif, job_id)],
                                   capture_output=True, text=True, timeout=FS_TIMEOUT)
                last = (r.stdout or '').strip()
            except subprocess.TimeoutExpired:
                last = 'fs timeout'
            if '+OK' in last:
                db_log('send', number, tif, 1, 1, note=f'try{attempt}')
                # originate 성공 - 실제 전송 완료/페이지수/원인은 ESL 이벤트에서
                # notify_app_result가 호출됨 (여기선 통지하지 않음, 중복 방지)
                return {'ok': True, 'number': number, 'attempts': attempt}
            # 실패 시 faxguard로 원인 진단
            diag = guard.diagnose(last) if HAS_GUARD else ''
            log.warning(f'발신 실패 {number} (시도 {attempt}/{SEND_RETRIES}): {last[:60]} {diag}')
            # faxguard 백오프+지터 (동시 재시도 폭주 방지)
            if HAS_GUARD:
                guard.backoff_wait(attempt)
            else:
                time.sleep(RETRY_BACKOFF * attempt)

    diag = guard.diagnose(last) if HAS_GUARD else ''
    db_log('send', number, tif, 0, 0, note=f'fail:{diag[:40]}' if diag else f'fail_{SEND_RETRIES}try')
    # originate 자체가 최종 실패 - 앱에 통지 (ESL 이벤트가 안 올 것이므로)
    notify_app_result(job_id, False, result_text=(last or 'originate failed')[:200],
                      result_code='FAX_SERVER', diag_category=(diag or None))
    return {'ok': False, 'number': number, 'attempts': SEND_RETRIES, 'error': last, 'diagnosis': diag}

def send_fax(number, pdf_path, job_id=None):
    """동기 발송 (API 직접 호출용)"""
    return _do_send(str(number), pdf_path, job_id)

def send_worker():
    """큐 기반 비동기 발송 워커 (다수 발송을 순서대로 소화)"""
    while not _shutdown.is_set():
        try:
            item = _send_q.get(timeout=1)
        except queue.Empty:
            continue
        try:
            _do_send(item['number'], item['pdf'], item.get('job_id'))
        except Exception as e:
            log.error(f'send_worker 오류: {e}')
        finally:
            _send_q.task_done()

# ---------------- 수신 핸들러 (자동복구) ----------------
def esl_recv_loop():
    backoff = 3
    while not _shutdown.is_set():
        s = None
        try:
            s = socket.create_connection((ESL_HOST, ESL_PORT), timeout=10)
            s.settimeout(None)
            f = s.makefile('rwb')
            _read_event(f)
            f.write(f'auth {ESL_PASS}\n\n'.encode()); f.flush()
            auth = _read_event(f)
            if not auth:
                raise RuntimeError('auth 응답 없음')
            f.write(b'event plain CHANNEL_EXECUTE_COMPLETE CHANNEL_HANGUP_COMPLETE\n\n'); f.flush()
            _read_event(f)
            log.info('수신 핸들러 ESL 연결됨')
            backoff = 3
            while not _shutdown.is_set():
                ev = _read_event(f)
                if ev is None:
                    raise RuntimeError('ESL 연결 종료됨')
                try:
                    _handle_event(ev)
                except Exception as e:
                    log.error(f'수신 이벤트 처리 오류: {e}')   # 개별 이벤트 오류가 루프를 죽이지 않도록
        except Exception as e:
            log.warning(f'수신 핸들러 재연결 {backoff}s 후: {e}')
            _shutdown.wait(backoff)
            backoff = min(backoff * 2, 30)   # 지수 백오프
        finally:
            try:
                if s: s.close()
            except Exception:
                pass

def _read_event(f):
    headers = {}
    while True:
        line = f.readline()
        if not line:
            return None
        line = line.decode(errors='ignore').strip()
        if line == '':
            break
        if ':' in line:
            k, v = line.split(':', 1)
            headers[k.strip()] = v.strip()
    try:
        clen = int(headers.get('Content-Length', 0))
    except ValueError:
        clen = 0
    if clen:
        body = f.read(clen).decode(errors='ignore')
        for bl in body.split('\n'):
            if ':' in bl:
                k, v = bl.split(':', 1)
                headers[k.strip()] = v.strip().replace('%20', ' ')
    return headers

def _handle_event(ev):
    faxfile = ev.get('variable_fax_file')
    success = ev.get('variable_fax_success')
    if success is None or not faxfile:
        return
    uid = ev.get('Unique-ID', faxfile)
    with _seen_lock:
        if uid in _seen:
            return
        _seen.add(uid)
        if len(_seen) > 10000:          # 메모리 누수 방지
            _seen.clear()
    pages = ev.get('variable_fax_document_transferred_pages', '0')
    rate  = ev.get('variable_fax_transfer_rate', '')
    number = ev.get('Caller-Destination-Number', 'unknown')
    ok = (success == '1')
    result = ev.get('variable_fax_result_text', '')[:40]

    # ── 발송 결과를 앱에 통지 (job_id가 심어진 발송 이벤트) ──
    job_id = ev.get('variable_faxapp_job_id')
    # [진단] 완료 이벤트의 job_id 유무·출처 추적 (루프백 job_id 문제 진단용)
    log.info(f'[FAX완료] job_id={job_id} success={success} file={faxfile} '
             f'dest={number} uid={uid[:8]} '
             f'app_var={"O" if ev.get("variable_faxapp_job_id") else "X"}')
    if job_id:
        hangup = ev.get('variable_hangup_cause', '')
        result_full = ev.get('variable_fax_result_text', '')[:200]
        total = ev.get('variable_fax_document_total_pages', '') or None
        cat = None
        if HAS_DIAG:
            try:
                cat, _msg = diag.categorize(hangup, result_full)
            except Exception:
                cat = None
        try:
            notify_app_result(
                job_id, ok,
                pages_sent=int(pages) if str(pages).isdigit() else None,
                pages_total=int(total) if total and str(total).isdigit() else None,
                result_text=result_full or None,
                hangup_cause=hangup or None,
                diag_category=cat,
                t38_used=bool(ev.get('variable_fax_t38')),
                ecm_used=(ev.get('variable_fax_ecm_used') == '1'),
                bad_rows=int(ev.get('variable_fax_bad_rows')) if str(ev.get('variable_fax_bad_rows', '')).isdigit() else None,
                transfer_rate=rate or None)
        except Exception as e:
            log.warning(f'발송 결과통지 오류: {e}')

    if faxfile.startswith(os.path.join(FAX_DIR, 'rx_')) and os.path.isfile(faxfile):
        pdf = faxfile.rsplit('.', 1)[0] + '.pdf'
        try:
            subprocess.run(['tiff2pdf', '-o', pdf, faxfile], timeout=30,
                           capture_output=True)
        except Exception as e:
            log.warning(f'TIFF→PDF 변환 실패: {e}')
        db_log('receive', number, pdf, int(pages or 0), ok, rate, note=result)
        log.info(f'수신 {number} pages={pages} rate={rate} ok={ok}')
        # faxdiag 진단 기록
        if HAS_DIAG:
            try:
                hangup = ev.get('variable_hangup_cause', '')
                cat, msg = diag.categorize(hangup, result)
                diag.diag_record({
                    'call_uuid': uid, 'direction': 'receive', 'number': number,
                    'hangup_cause': hangup, 'fax_result': result, 'fax_success': 1 if ok else 0,
                    't38_used': 1 if ev.get('variable_fax_t38') else 0,
                    'transfer_rate': rate, 'pages_tx': pages,
                    'pages_total': ev.get('variable_fax_document_total_pages', 0),
                    'diag_category': cat, 'diag_message': msg,
                })
                # 수신 실패 시 알람
                if not ok:
                    raise_alarm('warn', f'수신 실패 ({cat})', f'{number}: {msg}',
                                dedup_key=f'rxfail_{cat}')
            except Exception as e:
                log.warning(f'진단 기록 오류: {e}')

# ---------------- 정리 작업 ----------------
def cleanup_loop():
    while not _shutdown.is_set():
        try:
            cutoff = time.time() - CLEANUP_HOURS * 3600
            for fn in os.listdir(FAX_DIR):
                if fn.startswith('send_') and fn.endswith('.tif'):
                    fp = os.path.join(FAX_DIR, fn)
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
            # faxguard 좀비 채널 정리 (부하 후 잔여 세션 제거)
            if HAS_GUARD:
                killed = guard.cleanup_zombie_channels(max_age_sec=600)
                if killed:
                    log.info(f'좀비 채널 {killed}개 정리')
        except Exception as e:
            log.warning(f'정리 작업 오류: {e}')
        _shutdown.wait(3600)   # 1시간마다

# ---------------- 시스템 지표 ----------------
def cpu_percent():
    global _prev
    try:
        with open('/proc/stat') as fp:
            nums = list(map(int, fp.readline().split()[1:]))
        idle = nums[3] + nums[4]; total = sum(nums)
        di = idle - _prev['idle']; dt = total - _prev['total']
        _prev = {'idle': idle, 'total': total}
        return round(100 * (1 - di / dt), 1) if dt > 0 else 0
    except Exception:
        return 0

def mem_percent():
    try:
        info = {}
        with open('/proc/meminfo') as fp:
            for line in fp:
                k, v = line.split(':'); info[k] = int(v.strip().split()[0])
        total = info['MemTotal']; avail = info.get('MemAvailable', info['MemFree'])
        return round(100 * (1 - avail / total), 1)
    except Exception:
        return 0

def load_avg():
    try:
        return round(os.getloadavg()[0], 2)
    except Exception:
        return 0

def uptime_str():
    s = int(time.time() - SERVER_STARTED); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'

# ==================== 쉬운 진단 (초보자용) ====================
# 실패 note/원인을 "무슨 일이 일어났고 어떻게 해결하는지"로 번역.
EASY_DIAG_RULES = [
    # (키워드들, 아이콘, 제목, 쉬운설명, 조치방법, 심각도)
    (['busy', 'BUSY', '486', '통화중'], '📞', '상대방 통화중',
     '상대 팩스가 다른 통화 중이었습니다.', '잠시 후 자동으로 다시 시도됩니다. 계속되면 상대 번호를 확인하세요.', 'warn'),
    (['no_answer', 'NO_ANSWER', '480', '408', 'timeout', '무응답'], '🔕', '상대방 무응답',
     '상대 팩스가 응답하지 않았습니다.', '상대 팩스기가 켜져 있는지, 번호가 맞는지 확인하세요.', 'warn'),
    (['404', 'WRONG', 'invalid number', 'INVALID', '번호'], '❌', '잘못된 번호',
     '입력한 번호로 연결할 수 없습니다.', '팩스 번호가 정확한지 다시 확인하세요. (지역번호 포함)', 'crit'),
    (['training', 'TRAINING', 'negotiation'], '📶', '신호 협상 실패',
     '양측 팩스가 통신 속도를 맞추지 못했습니다.', '회선 품질 문제일 수 있습니다. 다시 시도하거나 회선을 점검하세요.', 'warn'),
    (['T38', 't38', 'T.38'], '🔄', 'T.38 협상 문제',
     '팩스 전용 프로토콜(T.38) 협상에 실패했습니다.', '게이트웨이의 T.38 설정을 확인하세요. G.711 방식으로도 전송 가능합니다.', 'warn'),
    (['E1', 'LAYER', 'link', 'TEMPORARY_FAILURE', 'NORMAL_TEMPORARY'], '🔌', '회선/라우팅 문제',
     'E1 회선이나 게이트웨이 연결에 문제가 있습니다.', 'E1 링크 상태와 게이트웨이 등록(sofia status)을 확인하세요.', 'crit'),
    (['convert', 'CONVERSION', 'BAD_TIFF', 'invalid or missing', '변환'], '📄', '문서 변환 실패',
     '보낼 문서를 팩스 형식으로 바꾸지 못했습니다.', '문서가 손상되지 않았는지, 지원 형식(PDF/TIFF)인지 확인하세요.', 'crit'),
    (['503', 'SERVICE_BUSY', '채널', 'exhausted'], '🚦', '채널 부족',
     '동시 처리 가능한 회선이 모두 사용 중이었습니다.', '동시 발송량을 줄이거나 채널 수를 늘리세요.', 'warn'),
    (['hangup', 'DISCONNECT', 'mid', '중단'], '✂️', '전송 중 끊김',
     '전송 도중 연결이 끊어졌습니다.', '회선이 불안정할 수 있습니다. 다시 시도하세요.', 'warn'),
]

def easy_diagnose(text):
    """실패 텍스트 → (아이콘, 제목, 설명, 조치, 심각도). 매칭 없으면 일반 안내."""
    t = str(text or '')
    for keys, icon, title, desc, fix, sev in EASY_DIAG_RULES:
        if any(k in t for k in keys):
            return {'icon': icon, 'title': title, 'desc': desc, 'fix': fix, 'severity': sev}
    return {'icon': '⚠️', 'title': '전송 실패', 'desc': '팩스 전송에 실패했습니다.',
            'fix': '다시 시도하거나, 상세 로그를 확인하세요.', 'severity': 'warn'}

def easy_diag_summary():
    """최근 실패들을 쉬운 진단으로 요약. 대시보드 '쉬운 진단' 탭용."""
    recent = db_recent(60)
    fails = [r for r in recent if not r.get('success')]
    # 원인별 그룹핑
    groups = {}
    for r in fails:
        d = easy_diagnose(r.get('note') or '')
        key = d['title']
        if key not in groups:
            groups[key] = {'diag': d, 'count': 0, 'numbers': [], 'last': r.get('created', '')}
        groups[key]['count'] += 1
        if r.get('number') and r['number'] not in groups[key]['numbers']:
            groups[key]['numbers'].append(r['number'])
    items = sorted(groups.values(), key=lambda g: -g['count'])
    return {
        'total_fails': len(fails),
        'total_recent': len(recent),
        'groups': [{
            'icon': g['diag']['icon'], 'title': g['diag']['title'],
            'desc': g['diag']['desc'], 'fix': g['diag']['fix'],
            'severity': g['diag']['severity'], 'count': g['count'],
            'numbers': g['numbers'][:5], 'last': g['last'],
        } for g in items]
    }


def snapshot():
    ct = db_counts()
    tot = sum(ct.values()); ok = ct['sent_ok'] + ct['recv_ok']
    return {
        'time': time.strftime('%H:%M:%S'), 'uptime': uptime_str(),
        'channels': channel_count(), 'cores': os.cpu_count() or 1,
        'fs_alive': fs_alive(),
        'gw_name': _gw_status['name'], 'gw_state': _gw_status['state'], 'gw_checked': _gw_status['checked'],
        'queue': _send_q.qsize(),
        **ct,
        'success_rate': round(100*ok/tot, 1) if tot else 100.0, 'total_try': tot,
        'cpu': cpu_percent(), 'mem': mem_percent(), 'load': load_avg(),
        'channel_list': channel_list(), 'recent': db_recent(60),
        'alarm_unread': alarms_snapshot()['unread'],
    }

# ---------------- 웹 ----------------

# ==================== 시스템/엔진 정보 수집 (콘솔용) ====================
def _sh(cmd, timeout=5):
    """셸 명령 실행, 실패 시 빈 문자열."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ''


def _fs_cli(cmd, timeout=5):
    """fs_cli 명령 실행."""
    fs = None
    for p in ('/usr/local/freeswitch/bin/fs_cli', '/usr/bin/fs_cli', 'fs_cli'):
        if os.path.exists(p) or p == 'fs_cli':
            fs = p
            break
    if not fs:
        return ''
    return _sh(f'{fs} -x "{cmd}"', timeout)


# ==================== System ====================
def get_system():
    """시스템 정보 (OS, 커널, uptime, CPU, 메모리, 디스크)."""
    # OS
    osr = _sh("cat /etc/redhat-release 2>/dev/null")
    if not osr:
        osr = _sh("grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d'\"' -f2")
    # 메모리
    mem = _sh("free -m | awk '/Mem:/{print $2\" \"$7}'").split()
    mem_total = mem[0] if len(mem) > 0 else '?'
    mem_avail = mem[1] if len(mem) > 1 else '?'
    # 디스크 (루트)
    disks = []
    df = _sh("df -m -x tmpfs -x devtmpfs 2>/dev/null | tail -n +2")
    for line in df.split('\n'):
        parts = line.split()
        if len(parts) >= 6 and parts[0].startswith('/dev'):
            disks.append({
                'device': parts[0], 'mount': parts[5],
                'total_mb': parts[1], 'used_mb': parts[2],
                'pct': parts[4],
            })
    # 로드
    load = _sh("cat /proc/loadavg | awk '{print $1\", \"$2\", \"$3}'")
    return {
        'os': osr or platform.platform(),
        'kernel': platform.release(),
        'uptime': _sh("uptime -p 2>/dev/null") or _sh("uptime"),
        'hostname': platform.node(),
        'cpu_cores': os.cpu_count() or 1,
        'cpu_load': load,
        'mem_total_mb': mem_total,
        'mem_avail_mb': mem_avail,
        'disks': disks,
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'timezone': _sh("timedatectl show -p Timezone --value 2>/dev/null") or time.tzname[0],
    }


# ==================== Network ====================
def get_network():
    """네트워크 인터페이스 + IP."""
    ifaces = []
    # ip -br addr (간결한 형식)
    out = _sh("ip -br addr 2>/dev/null")
    for line in out.split('\n'):
        parts = line.split()
        if len(parts) >= 2 and parts[0] != 'lo':
            ifaces.append({
                'name': parts[0],
                'state': parts[1] if len(parts) > 1 else '?',
                'addr': ' '.join(parts[2:]) if len(parts) > 2 else '-',
            })
    return {
        'hostname': platform.node(),
        'primary_ip': _sh("hostname -I 2>/dev/null | awk '{print $1}'"),
        'interfaces': ifaces,
        'gateway': _sh("ip route | awk '/default/{print $3; exit}'"),
        'dns': _sh("grep nameserver /etc/resolv.conf 2>/dev/null | awk '{print $2}' | head -2 | tr '\\n' ' '"),
    }


# ==================== SIP Profiles ====================
def get_sip_profiles():
    """FreeSWITCH SIP 프로파일 목록 + 상태."""
    profiles = []
    out = _fs_cli("sofia status")
    # 출력 파싱: Name Type Data State 형식
    for line in out.split('\n'):
        parts = line.split('\t') if '\t' in line else line.split()
        # profile 행 찾기 (Type이 profile인 것)
        if len(parts) >= 4 and 'profile' in line.lower():
            profiles.append({
                'name': parts[0].strip(),
                'type': 'profile',
                'data': parts[2].strip() if len(parts) > 2 else '',
                'state': parts[-1].strip(),
            })
    return profiles


def get_sip_gateways():
    """SIP 게이트웨이 목록 + 상태."""
    gateways = []
    out = _fs_cli("sofia status")
    for line in out.split('\n'):
        if 'gateway' in line.lower():
            parts = line.split('\t') if '\t' in line else line.split()
            if len(parts) >= 2:
                # gateway 이름은 profile::gateway 형식일 수 있음
                name = parts[0].strip()
                if '::' in name:
                    name = name.split('::')[-1]
                gateways.append({
                    'name': name,
                    'state': parts[-1].strip() if len(parts) > 1 else '?',
                    'data': parts[2].strip() if len(parts) > 2 else '',
                })
    return gateways


def get_gateway_detail(name):
    """특정 게이트웨이 상세."""
    out = _fs_cli(f"sofia status gateway {name}")
    detail = {}
    for line in out.split('\n'):
        if '\t' in line:
            k, _, v = line.partition('\t')
            detail[k.strip()] = v.strip()
    return detail


# ==================== Codecs ====================
def get_codecs():
    """FreeSWITCH 로드된 코덱 목록."""
    codecs = []
    out = _fs_cli("show codec")
    for line in out.split('\n')[1:]:  # 헤더 스킵
        parts = line.split(',')
        if len(parts) >= 3:
            codecs.append({
                'type': parts[0].strip(),
                'name': parts[1].strip(),
                'ianacode': parts[2].strip() if len(parts) > 2 else '',
            })
    # 팩스 관련 코덱 강조
    return codecs


# ==================== Fax 설정 ====================
def get_fax_config():
    """팩스 관련 설정 (환경변수 + FreeSWITCH 설정)."""
    return {
        'gateway': os.environ.get('FAX_GATEWAY', 'loopback'),
        'max_concurrent': os.environ.get('FAX_MAX_CONCURRENT', '40'),
        'app_webhook': os.environ.get('FAX_APP_WEBHOOK',
                                      'http://127.0.0.1:8100/api/fax/webhook/result'),
        't38_enabled': _fs_cli("global_getvar fax_enable_t38") or 'default',
        'ecm': 'enabled',  # 기본 ECM 사용
        'fax_v34': 'V.17 (14400) - FreeSWITCH 표준',
    }


# ==================== SIP 프로파일 생성 (등록 기능) ====================
def sip_profile_dir():
    """FreeSWITCH external 프로파일 디렉토리 찾기."""
    candidates = [
        '/usr/local/freeswitch/etc/freeswitch/sip_profiles/external',
        '/usr/local/freeswitch/conf/sip_profiles/external',
        '/etc/freeswitch/sip_profiles/external',
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def create_gateway_xml(name, proxy, register=False, username='', password='',
                       realm='', context='default'):
    """게이트웨이 XML 생성 + 배치 + 로드.
    반환: (성공여부, 메시지)
    """
    # 이름 검증 (파일명/SIP 안전)
    import re
    if not re.match(r'^[a-zA-Z0-9_-]{1,32}$', name):
        return (False, '게이트웨이 이름은 영문/숫자/_-만 (32자 이내)')
    if not re.match(r'^[a-zA-Z0-9._:-]{1,64}$', proxy):
        return (False, 'proxy는 IP 또는 도메인 형식이어야 합니다')

    prof_dir = sip_profile_dir()
    if not prof_dir:
        return (False, 'external 프로파일 디렉토리를 찾을 수 없습니다')

    reg = 'true' if register else 'false'
    xml = f'''<include>
  <gateway name="{name}">
    <param name="proxy" value="{proxy}"/>
    <param name="register" value="{reg}"/>
'''
    if register and username:
        xml += f'    <param name="username" value="{username}"/>\n'
        xml += f'    <param name="password" value="{password}"/>\n'
    if realm:
        xml += f'    <param name="realm" value="{realm}"/>\n'
    xml += f'    <param name="context" value="{context}"/>\n'
    xml += '  </gateway>\n</include>\n'

    path = os.path.join(prof_dir, f'{name}.xml')
    try:
        with open(path, 'w') as f:
            f.write(xml)
    except Exception as e:
        return (False, f'파일 저장 실패: {e}')

    # 로드
    _fs_cli("reloadxml")
    _fs_cli("sofia profile external restart reloadxml")
    time.sleep(2)
    # 확인
    st = _fs_cli(f"sofia status gateway {name}")
    if 'Invalid' in st or not st:
        return (True, f'게이트웨이 {name} 생성됨 (로드 확인 필요)')
    return (True, f'게이트웨이 {name} 생성 및 로드 완료')


def delete_gateway(name):
    """게이트웨이 XML 삭제 + 리로드."""
    import re
    if not re.match(r'^[a-zA-Z0-9_-]{1,32}$', name):
        return (False, '잘못된 이름')
    prof_dir = sip_profile_dir()
    if not prof_dir:
        return (False, '프로파일 디렉토리 없음')
    path = os.path.join(prof_dir, f'{name}.xml')
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception as e:
            return (False, f'삭제 실패: {e}')
    _fs_cli("reloadxml")
    _fs_cli("sofia profile external restart reloadxml")
    return (True, f'게이트웨이 {name} 삭제됨')


# ==================== 종합 ====================
def console_snapshot():
    """XMS 콘솔 스타일 대시보드용 종합 정보."""
    return {
        'system': get_system(),
        'network': get_network(),
        'sip_profiles': get_sip_profiles(),
        'sip_gateways': get_sip_gateways(),
        'codecs': get_codecs(),
        'fax_config': get_fax_config(),
    }



# ==================== 콘솔 데이터 API ====================
def console_data(view):
    """XMS 콘솔 스타일 대시보드용 뷰별 데이터.
    view에 따라 필요한 정보만 수집 (성능)."""
    base = {
        'engine_up': fs_alive(),
        'channels': channel_count(),
        'max_ch': int(os.environ.get('FAX_MAX_CONCURRENT', '40')),
    }
    if view == 'system':
        base['system'] = get_system()
    elif view == 'network':
        base['network'] = get_network()
    elif view == 'channels':
        base['channel_stats'] = channel_detail_stats()
    elif view == 'captures':
        base['capture'] = capture_status()
    elif view == 'sipprofiles':
        base['sip_profiles'] = get_sip_profiles()
    elif view == 'gateways':
        base['sip_gateways'] = get_sip_gateways()
    elif view == 'codecs':
        base['codecs'] = get_codecs()
    elif view == 'faxcfg':
        base['fax_config'] = get_fax_config()
    elif view == 'cdr':
        base['recent'] = db_recent(80)
    elif view == 'easydiag':
        base['easy_diag'] = easy_diag_summary()
    elif view == 'reports':
        counts = db_counts()
        st = counts.get('by_category', []) if isinstance(counts, dict) else []
        recent = db_recent(200)
        send = [r for r in recent if r.get('direction') == 'send']
        recv = [r for r in recent if r.get('direction') == 'receive']
        send_ok = sum(1 for r in send if r.get('success'))
        base['stats'] = {
            'send_total': len(send), 'send_ok': send_ok,
            'send_fail': len(send) - send_ok,
            'recv_total': len(recv),
            'success_rate': round(100 * send_ok / len(send), 1) if send else 100.0,
        }
    return base

PAGE = r'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FreeSWITCH Fax Console</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--navy:#2b5580;--navy2:#3a6699;--line:#dde3ea;--line2:#e8ecf2;--ink:#1a2433;--ink2:#3a4658;--ink3:#8a97a8;--bg:#eef1f5;--panel:#f8fafc;
--ok:#0e9f6e;--ok-bg:#e3f6ee;--no:#dc2626;--no-bg:#fce9e9;--warn:#d97706;--warn-bg:#fcf0dd;--mono:Consolas,'SF Mono',monospace;}
body{font-family:'Segoe UI','Malgun Gothic',sans-serif;font-size:13px;color:var(--ink);background:var(--bg)}
a{color:var(--navy);text-decoration:none}
.top{background:#fff;border-bottom:2px solid var(--navy);padding:12px 22px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.brand{display:flex;align-items:center;gap:11px}
.brand .logo{width:32px;height:32px;background:var(--navy);border-radius:6px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:15px}
.brand .name{font-size:15px;font-weight:700;color:var(--navy)}
.top .title{font-size:19px;color:var(--navy);font-weight:600}
.top .user{font-size:12px;color:#666;text-align:right;line-height:1.5}
.wrap{display:flex;min-height:calc(100vh - 60px)}
.side{width:158px;background:#f7f8fa;border-right:1px solid var(--line);flex-shrink:0}
.side-item{padding:9px 18px;color:var(--ink2);cursor:pointer;border-bottom:1px solid #eef1f5;display:flex;align-items:center;justify-content:space-between}
.side-item:hover{background:#edf1f6}
.side-item.on{background:var(--navy);color:#fff;font-weight:600}
.side-item .cnt{font-size:10px;background:rgba(0,0,0,.08);padding:1px 7px;border-radius:9px}
.side-item.on .cnt{background:rgba(255,255,255,.25)}
.side-item.section{font-size:10.5px;color:var(--ink3);font-weight:700;background:transparent;padding:13px 18px 4px;text-transform:uppercase;letter-spacing:.5px;cursor:default}
.side-item.section:hover{background:transparent}
.main{flex:1;min-width:0}
.tabs{background:#fff;border-bottom:1px solid var(--line);padding:0 8px;display:flex;gap:2px;overflow-x:auto}
.tab{padding:11px 17px;color:#5a6b80;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.tab:hover{color:var(--navy)}
.tab.on{color:var(--navy);font-weight:600;border-bottom-color:var(--navy)}
.content{padding:20px 24px}
.view{display:none}.view.on{display:block}
.h2{font-size:16px;font-weight:700;color:var(--navy);margin-bottom:5px}
.sub{font-size:12.5px;color:var(--ink3);margin-bottom:16px}
.card{background:#fff;border:1px solid #e0e6ee;border-radius:7px;margin-bottom:16px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.card-h{background:var(--navy);color:#fff;font-weight:600;padding:10px 15px;display:flex;justify-content:space-between;align-items:center}
.card-h .tools{display:flex;gap:6px}
.itable{width:100%;border-collapse:collapse}
.itable td{padding:9px 15px;border-bottom:1px solid var(--line2)}
.itable tr:last-child td{border-bottom:none}
.itable th{text-align:left;padding:9px 15px;background:#f0f3f7;color:#5a6b80;font-size:12px;font-weight:700;border-bottom:1px solid var(--line)}
.itable .k{width:32%;color:var(--ink2);font-weight:500;background:var(--panel)}
.itable .v{color:var(--ink)}
.itable tr:hover td{background:#f4f7fa}
.itable td.k:hover{background:var(--panel)}
.st{display:inline-block;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700}
.st.up{background:var(--ok-bg);color:var(--ok)}.st.down{background:var(--no-bg);color:var(--no)}
.st.warn{background:var(--warn-bg);color:var(--warn)}.st.noreg{background:#eef2f8;color:#5a6b80}
.btn{padding:7px 14px;border:1px solid var(--navy);background:var(--navy);color:#fff;border-radius:5px;font-size:12.5px;cursor:pointer}
.btn:hover{background:var(--navy2)}
.btn.ghost{background:#fff;color:var(--navy)}.btn.ghost:hover{background:#eef2f8}
.btn.danger{border-color:var(--no);background:#fff;color:var(--no);padding:4px 11px;font-size:11.5px}
.btn.danger:hover{background:var(--no-bg)}
.btn.sm{padding:4px 11px;font-size:11.5px}
.btn-row{display:flex;gap:8px;margin-bottom:14px;align-items:center;flex-wrap:wrap}
.form-row{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.form-row label{width:150px;color:var(--ink2);font-weight:500}
.form-row input,.form-row select{flex:1;max-width:360px;padding:7px 10px;border:1px solid #cdd5e0;border-radius:5px;font-size:13px;font-family:inherit}
.form-row input.mono{font-family:var(--mono)}
.hint{font-size:11.5px;color:var(--ink3);margin-top:6px;line-height:1.6}
.mono{font-family:var(--mono)}
.empty{text-align:center;padding:36px;color:var(--ink3)}
.empty .ic{font-size:32px;margin-bottom:8px}
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:#fff;border:1px solid #e0e6ee;border-radius:7px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.kpi .lbl{font-size:11.5px;color:var(--ink3);margin-bottom:5px}
.kpi .num{font-size:26px;font-weight:750;color:var(--ink)}
.kpi .num.ok{color:var(--ok)}.kpi .num.no{color:var(--no)}
.toast{position:fixed;bottom:24px;right:24px;background:var(--ink);color:#fff;padding:12px 18px;border-radius:7px;font-size:13px;z-index:100;box-shadow:0 4px 16px rgba(0,0,0,.2);display:none}
.toast.ok{background:var(--ok)}.toast.err{background:var(--no)}
input[type=checkbox]{width:15px;height:15px;cursor:pointer}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.kpi{transition:box-shadow .2s}
.kpi:hover{box-shadow:0 3px 10px rgba(43,85,128,.12)}
.card{transition:box-shadow .2s}
.side-item{transition:background .12s}
.btn{transition:background .15s,transform .1s}
.btn:active{transform:translateY(1px)}
.st{letter-spacing:.2px}
.itable tr{transition:background .1s}
/* 스크롤바 */
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:#c2ccd8;border-radius:5px}
::-webkit-scrollbar-thumb:hover{background:#a5b2c2}
::-webkit-scrollbar-track{background:#f0f3f7}
/* KPI 숫자 그라데이션 느낌 */
.kpi .num{letter-spacing:-.5px}
/* 카드 헤더 미묘한 그라데이션 */
.card-h{background:linear-gradient(135deg,#2b5580,#34648f)}
</style></head><body>
<div class="top">
  <div class="brand"><div class="logo">FS</div><div class="name">FreeSWITCH Fax</div></div>
  <div class="title">Fax Media Console</div>
  <div class="user">user: <b>admin</b><br><span id="hdr-state">—</span></div>
</div>
<div class="wrap">
  <div class="side" id="side"></div>
  <div class="main">
    <div class="tabs" id="tabs"></div>
    <div class="content" id="content"></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
function toast(m,t){const el=$('#toast');el.textContent=m;el.className='toast '+(t||'')+' ';el.style.display='block';setTimeout(()=>el.style.display='none',2600);}
async function api(path,opt){const r=await fetch(path,opt);if(!r.ok){let e='오류';try{e=(await r.json()).error||e}catch(_){}toast(e,'err');throw new Error(e);}return r.json();}

// ===== 메뉴 구조 =====
const MENU=[
  {sec:'모니터'},
  {id:'system',label:'System',tabs:['General','Storage','Time']},
  {id:'network',label:'Network'},
  {id:'channels',label:'Active Channels'},
  {id:'captures',label:'Packet Capture'},
  {sec:'설정'},
  {id:'sipprofiles',label:'SIP Profiles'},
  {id:'gateways',label:'Gateways'},
  {id:'codecs',label:'Codecs'},
  {id:'faxcfg',label:'Fax Settings'},
  {sec:'기록'},
  {id:'cdr',label:'CDR'},
  {id:'easydiag',label:'쉬운 진단'},
  {id:'reports',label:'Reports'},
  {sec:'시스템'},
  {id:'control',label:'Control'},
];
let CUR='system', CURTAB=0, DATA={};

function renderSide(){
  $('#side').innerHTML=MENU.map(m=>{
    if(m.sec)return `<div class="side-item section">${m.sec}</div>`;
    return `<div class="side-item ${m.id===CUR?'on':''}" data-id="${m.id}">${m.label}${m.badge?`<span class="cnt">${m.badge}</span>`:''}</div>`;
  }).join('');
  document.querySelectorAll('.side-item[data-id]').forEach(el=>el.onclick=()=>{CUR=el.dataset.id;CURTAB=0;renderSide();renderTabs();loadView();});
}
function renderTabs(){
  const m=MENU.find(x=>x.id===CUR);
  if(m&&m.tabs){$('#tabs').innerHTML=m.tabs.map((t,i)=>`<div class="tab ${i===CURTAB?'on':''}" data-i="${i}">${t}</div>`).join('');
    document.querySelectorAll('.tab').forEach(el=>el.onclick=()=>{CURTAB=parseInt(el.dataset.i);renderTabs();renderView();});
  }else $('#tabs').innerHTML='';
}

// ===== 뷰 렌더링 =====
function tbl(groups){
  // groups: [{title, rows:[[k,v],...]}]
  return groups.map(g=>`<div class="card"><div class="card-h">${esc(g.title)}${g.tools||''}</div>
    <table class="itable">${g.rows.map(r=>`<tr><td class="k">${esc(r[0])}</td><td class="v">${r[1]}</td></tr>`).join('')}</table></div>`).join('');
}

function renderView(){
  const c=$('#content');
  const d=DATA[CUR]||{};
  if(CUR==='system'){
    const s=d.system||{};
    if(CURTAB===0){ // General
      c.innerHTML=tbl([
        {title:'Fax Engine',rows:[['Engine','FreeSWITCH mod_spandsp'],['State',d.engine_up?'<span class="st up">RUNNING</span>':'<span class="st down">STOPPED</span>'],['Active Channels',`${d.channels||0} / ${d.max_ch||40}`]]},
        {title:'System',rows:[['OS Release',esc(s.os)],['Kernel Version',esc(s.kernel)],['Hostname',esc(s.hostname)],['Uptime',esc(s.uptime)],['CPU Cores',s.cpu_cores],['CPU Load',esc(s.cpu_load)],['Memory',`total: ${s.mem_total_mb} MB, Available: ${s.mem_avail_mb} MB`]]},
      ]);
    }else if(CURTAB===1){ // Storage
      c.innerHTML=tbl([{title:'System Storage',rows:(s.disks||[]).map(dk=>[`${dk.device} (${dk.mount})`,`total: ${dk.total_mb} MB, used: ${dk.used_mb} MB (${dk.pct})`])}]);
    }else{ // Time
      c.innerHTML=tbl([{title:'System Time',rows:[['Time',esc(s.time)],['Zone',esc(s.timezone)]]}]);
    }
  }else if(CUR==='network'){
    const n=d.network||{};
    c.innerHTML=`<div class="h2">Network</div><div class="sub">네트워크 인터페이스 및 연결 정보</div>`+
      tbl([{title:'General',rows:[['Hostname',esc(n.hostname)],['Primary IP',`<span class="mono">${esc(n.primary_ip)}</span>`],['Default Gateway',`<span class="mono">${esc(n.gateway)}</span>`],['DNS',`<span class="mono">${esc(n.dns)}</span>`]]}])+
      `<div class="card"><div class="card-h">Interfaces</div><table class="itable"><tr><th>Interface</th><th>State</th><th>Address</th></tr>${(n.interfaces||[]).map(i=>`<tr><td class="mono">${esc(i.name)}</td><td>${i.state==='UP'?'<span class="st up">UP</span>':'<span class="st down">'+esc(i.state)+'</span>'}</td><td class="mono">${esc(i.addr)}</td></tr>`).join('')||'<tr><td colspan=3 class="empty">인터페이스 정보 없음</td></tr>'}</table></div>`;
  }else if(CUR==='channels'){
    const cs=d.channel_stats||{channels:[],total:0};
    const ch=cs.channels||[];
    c.innerHTML=`<div class="h2">Active Channels</div><div class="sub">실시간 콜/팩스 채널 모니터링 (자동 갱신)</div>`+
      `<div class="kpi-row">
        <div class="kpi"><div class="lbl">활성 채널</div><div class="num">${cs.total||0}<span style="font-size:14px;color:var(--ink3)"> / ${d.max_ch||40}</span></div></div>
        <div class="kpi"><div class="lbl">발신 (Outbound)</div><div class="num">${cs.outbound||0}</div></div>
        <div class="kpi"><div class="lbl">수신 (Inbound)</div><div class="num">${cs.inbound||0}</div></div>
        <div class="kpi"><div class="lbl">채널 사용률</div><div class="num ${(cs.total/(d.max_ch||40)*100)>80?'no':'ok'}">${Math.round(cs.total/(d.max_ch||40)*100)}%</div></div>
      </div>`+
      `<div class="card"><div class="card-h">Channel Details <span style="font-size:11px;font-weight:400;opacity:.8">실시간</span></div>
      <table class="itable"><tr><th>UUID</th><th>방향</th><th>발신번호</th><th>목적지</th><th>App</th><th>Codec</th><th>통화시간</th><th>상태</th></tr>
      ${ch.length?ch.map(x=>`<tr>
        <td class="mono" style="font-size:11px">${esc(x.uuid)}</td>
        <td>${x.dir==='outbound'?'<span class="st" style="background:#e0f2fe;color:#0369a1">발신</span>':x.dir==='inbound'?'<span class="st" style="background:#f0e8ff;color:#7c3aed">수신</span>':esc(x.dir||'-')}</td>
        <td class="mono">${esc(x.cid_num||'-')}</td>
        <td class="mono">${esc(x.dest||'-')}</td>
        <td style="font-size:12px">${esc(x.application||'-')}</td>
        <td class="mono" style="font-size:11px">${esc(x.codec||'-')}</td>
        <td class="mono">${esc(x.duration||'-')}</td>
        <td>${x.state==='ACTIVE'||x.state==='CS_EXECUTE'?'<span class="st up">활성</span>':'<span class="st noreg">'+esc(x.state||'-')+'</span>'}</td>
      </tr>`).join(''):'<tr><td colspan=8 class="empty"><div class="ic">○</div>현재 활성 채널이 없습니다<div style="font-size:12px;margin-top:4px">팩스 송수신 시 여기에 실시간으로 표시됩니다</div></td></tr>'}
      </table></div>`;
  }else if(CUR==='captures'){
    renderCaptures(c,d);
  }else if(CUR==='sipprofiles'){
    const ps=d.sip_profiles||[];
    c.innerHTML=`<div class="h2">SIP Profiles</div><div class="sub">FreeSWITCH SIP 프로파일 상태</div>`+
      `<div class="card"><div class="card-h">Profiles</div><table class="itable"><tr><th>Name</th><th>Data (bind)</th><th>State</th></tr>${ps.length?ps.map(p=>`<tr><td class="mono">${esc(p.name)}</td><td class="mono">${esc(p.data)}</td><td>${p.state==='RUNNING'?'<span class="st up">RUNNING</span>':'<span class="st warn">'+esc(p.state)+'</span>'}</td></tr>`).join(''):'<tr><td colspan=3 class="empty">프로파일 정보 없음 (FreeSWITCH 확인)</td></tr>'}</table></div>`;
  }else if(CUR==='gateways'){
    renderGateways(c,d);
  }else if(CUR==='codecs'){
    const cd=d.codecs||[];
    c.innerHTML=`<div class="h2">Codecs</div><div class="sub">로드된 코덱 목록 (팩스: G.711 / T.38)</div>`+
      `<div class="card"><div class="card-h">Loaded Codecs</div><table class="itable"><tr><th>Type</th><th>Name</th><th>IANA</th></tr>${cd.length?cd.map(x=>`<tr><td>${esc(x.type)}</td><td class="mono">${esc(x.name)}</td><td class="mono">${esc(x.ianacode)}</td></tr>`).join(''):'<tr><td colspan=3 class="empty">코덱 정보 없음</td></tr>'}</table></div>`;
  }else if(CUR==='faxcfg'){
    const f=d.fax_config||{};
    c.innerHTML=`<div class="h2">Fax Settings</div><div class="sub">팩스 엔진 설정</div>`+
      tbl([{title:'Fax Configuration',rows:[['발신 게이트웨이',`<span class="mono">${esc(f.gateway)}</span>`],['최대 동시 발송',esc(f.max_concurrent)],['앱 통지 URL',`<span class="mono" style="font-size:11px">${esc(f.app_webhook)}</span>`],['T.38',esc(f.t38_enabled)],['ECM (오류정정)',esc(f.ecm)],['전송 속도',esc(f.fax_v34)]]}]);
  }else if(CUR==='cdr'){
    renderCDR(c,d);
  }else if(CUR==='easydiag'){
    renderEasyDiag(c,d);
  }else if(CUR==='reports'){
    renderReports(c,d);
  }else if(CUR==='control'){
    c.innerHTML=`<div class="h2">Control</div><div class="sub">엔진 제어 (주의: 서비스에 영향)</div>`+
      `<div class="card"><div class="card-h">FreeSWITCH 제어</div><div style="padding:16px">
        <div class="btn-row"><button class="btn ghost" onclick="fsReload()">SIP 프로파일 재로드</button>
        <span class="hint" style="margin:0">sofia profile external restart reloadxml — 게이트웨이 설정 반영</span></div>
        <div class="btn-row"><button class="btn ghost" onclick="clearLog()">전송 기록 초기화</button>
        <span class="hint" style="margin:0">CDR/진단 로그 전체 삭제</span></div>
      </div></div>`;
  }
}

// ===== Gateways (등록/삭제) =====
function renderGateways(c,d){
  const gw=d.sip_gateways||[];
  c.innerHTML=`<div class="h2">SIP Gateways</div><div class="sub">팩스를 송수신할 외부 SIP 대상(게이트웨이) 관리 — CLI 없이 웹에서 등록</div>
  <div class="card"><div class="card-h">등록된 Gateway <span class="cnt" style="background:rgba(255,255,255,.2);padding:2px 8px;border-radius:9px;font-size:11px">${gw.length}</span></div>
  <table class="itable"><tr><th>Name</th><th>Proxy / Data</th><th>State</th><th></th></tr>
  ${gw.length?gw.map(g=>`<tr><td class="mono">${esc(g.name)}</td><td class="mono">${esc(g.data)}</td>
    <td>${g.state==='UP'||g.state==='NOREG'?'<span class="st up">'+esc(g.state)+'</span>':'<span class="st down">'+esc(g.state)+'</span>'}</td>
    <td><button class="btn danger" onclick="delGw('${esc(g.name)}')">삭제</button></td></tr>`).join(''):'<tr><td colspan=4 class="empty"><div class="ic">○</div>등록된 게이트웨이 없음</td></tr>'}
  </table></div>
  <div class="h2" style="margin-top:22px">새 Gateway 추가</div>
  <div class="card"><div class="card-h">Gateway 등록</div><div style="padding:18px">
    <div class="form-row"><label>이름 *</label><input id="gw-name" class="mono" placeholder="예: xms146, vega_gw"></div>
    <div class="form-row"><label>Proxy (IP/도메인) *</label><input id="gw-proxy" class="mono" placeholder="예: 192.168.219.146"></div>
    <div class="form-row"><label>등록(Register)</label><select id="gw-reg"><option value="false">false (IP 직결 - XMS/Vega)</option><option value="true">true (인증 등록)</option></select></div>
    <div class="form-row" id="gw-auth" style="display:none"><label>Username / Password</label><input id="gw-user" placeholder="username" style="max-width:170px"><input id="gw-pass" placeholder="password" style="max-width:170px"></div>
    <div class="form-row"><label>Context</label><input id="gw-ctx" class="mono" value="default"></div>
    <div class="btn-row" style="margin-top:6px"><button class="btn" onclick="createGw()">Gateway 생성 + 로드</button><button class="btn ghost" onclick="loadView()">초기화</button></div>
    <div class="hint">생성 시 XML 파일이 자동 생성되고 sofia reload까지 자동 처리됩니다. CLI 명령이 필요 없습니다.</div>
  </div></div>`;
  const reg=$('#gw-reg');if(reg)reg.onchange=()=>{$('#gw-auth').style.display=reg.value==='true'?'flex':'none';};
}
async function createGw(){
  const name=$('#gw-name').value.trim(),proxy=$('#gw-proxy').value.trim();
  if(!name||!proxy){toast('이름과 Proxy를 입력하세요','err');return;}
  const body={name,proxy,register:$('#gw-reg').value==='true',
    username:$('#gw-user')?.value||'',password:$('#gw-pass')?.value||'',context:$('#gw-ctx').value||'default'};
  try{const r=await api('/gateway/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast(r.message||'생성됨','ok');loadView();}catch(e){}
}
async function delGw(name){
  try{const r=await api('/gateway/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    toast(r.message||'삭제됨','ok');loadView();}catch(e){}
}

// ===== CDR (통화기록 + 삭제) =====
let _cdrSel=new Set();
function renderCDR(c,d){
  const rec=d.recent||[];
  c.innerHTML=`<div class="h2">CDR — 통화 기록</div><div class="sub">최근 송수신 팩스 기록</div>
  <div class="btn-row"><span id="cdr-selbar" style="display:none"><button class="btn danger sm" onclick="delCdrSel()">선택 삭제 (<span id="cdr-cnt">0</span>)</button></span>
  <button class="btn ghost sm" onclick="clearLog()">전체 초기화</button></div>
  <div class="card"><div class="card-h">Records</div>
  <table class="itable"><tr><th style="width:30px"><input type="checkbox" onchange="cdrAll(this)"></th><th>시각</th><th>구분</th><th>번호</th><th>페이지</th><th>속도</th><th>결과</th><th></th></tr>
  ${rec.length?rec.map(r=>`<tr><td><input type="checkbox" class="cdrchk" data-id="${r.id}" ${_cdrSel.has(r.id)?'checked':''} onchange="cdrTog(${r.id})"></td>
    <td class="mono">${esc((r.created||'').slice(11)||r.created)}</td>
    <td>${r.direction==='send'?'발송':'수신'}</td><td class="mono">${esc(r.number)}</td><td class="mono">${r.pages||0}</td>
    <td class="mono">${esc(r.rate||'—')}</td><td>${r.success?'<span class="st up">성공</span>':'<span class="st down">실패</span>'}</td>
    <td><button class="btn danger" onclick="delCdr(${r.id})">삭제</button></td></tr>`).join(''):'<tr><td colspan=8 class="empty"><div class="ic">▤</div>기록 없음</td></tr>'}
  </table></div>`;
}
function cdrTog(id){_cdrSel.has(id)?_cdrSel.delete(id):_cdrSel.add(id);$('#cdr-selbar').style.display=_cdrSel.size?'inline-flex':'none';$('#cdr-cnt').textContent=_cdrSel.size;}
function cdrAll(cb){document.querySelectorAll('.cdrchk').forEach(c=>{c.checked=cb.checked;const id=parseInt(c.dataset.id);cb.checked?_cdrSel.add(id):_cdrSel.delete(id);});$('#cdr-selbar').style.display=_cdrSel.size?'inline-flex':'none';$('#cdr-cnt').textContent=_cdrSel.size;}
async function delCdr(id){try{await api('/log/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});loadView();}catch(e){}}
async function delCdrSel(){if(!_cdrSel.size)return;try{await api('/log/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[..._cdrSel]})});_cdrSel.clear();loadView();}catch(e){}}
async function clearLog(){try{await api('/reset',{method:'POST'});toast('초기화됨','ok');loadView();}catch(e){}}

// ===== 쉬운 진단 =====
function renderEasyDiag(c,d){
  const ed=d.easy_diag||{groups:[],total_fails:0,total_recent:0};
  const sevC={crit:'var(--no)',warn:'var(--warn)',info:'var(--navy)'},sevB={crit:'var(--no-bg)',warn:'var(--warn-bg)',info:'#eef2f8'};
  c.innerHTML=`<div class="h2">쉬운 진단</div><div class="sub">최근 ${ed.total_recent}건 중 실패 ${ed.total_fails}건 — 원인과 해결방법</div>`+
  (ed.groups&&ed.groups.length?ed.groups.map(g=>`
    <div class="card" style="border-left:4px solid ${sevC[g.severity]||'var(--warn)'}"><div style="padding:16px 18px">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px"><span style="font-size:26px">${g.icon}</span>
        <div style="font-size:15px;font-weight:750">${esc(g.title)}<span style="margin-left:9px;font-size:11px;font-weight:800;padding:2px 9px;border-radius:12px;background:${sevB[g.severity]};color:${sevC[g.severity]}">${g.count}건</span></div></div>
      <div style="display:flex;gap:22px;margin-left:38px">
        <div style="flex:1"><div style="font-size:11px;color:var(--ink3);font-weight:700;margin-bottom:3px">무슨 일인가요?</div><div style="font-size:13px;color:var(--ink2);line-height:1.5">${esc(g.desc)}</div></div>
        <div style="flex:1"><div style="font-size:11px;color:var(--ok);font-weight:700;margin-bottom:3px">어떻게 해결하나요?</div><div style="font-size:13px;line-height:1.5">${esc(g.fix)}</div></div></div>
      ${g.numbers&&g.numbers.length?`<div style="margin-left:38px;margin-top:8px;font-size:11px;color:var(--ink3)">관련 번호: ${g.numbers.map(n=>`<span class="mono" style="background:var(--panel);padding:1px 6px;border-radius:4px;margin-right:4px">${esc(n)}</span>`).join('')}</div>`:''}
    </div></div>`).join('')
    :`<div class="card"><div class="empty"><div class="ic">✓</div><div style="font-weight:600;color:var(--ink)">문제 없음</div><div>최근 실패한 전송이 없습니다.</div></div></div>`);
}

// ===== Packet Capture =====
function renderCaptures(c,d){
  const cap=d.capture||{captures:[],active:false,tcpdump_available:true};
  const caps=cap.captures||[];
  const activeBar=cap.active?`<div class="card" style="border-left:4px solid var(--no)"><div style="padding:14px 16px;display:flex;align-items:center;gap:14px">
    <span style="width:12px;height:12px;border-radius:50%;background:var(--no);animation:pulse 1.5s infinite"></span>
    <div style="flex:1"><b>캡처 진행 중</b> · 대상: ${esc(cap.active_info?.target||'전체')} · ${esc(cap.active_info?.duration||0)}초</div>
    <button class="btn danger sm" onclick="capStop()">중지</button></div></div>`:'';
  c.innerHTML=`<div class="h2">Packet Capture</div><div class="sub">SIP/RTP 패킷을 캡처하여 통신 흐름 분석 (INVITE → 200 OK → BYE)</div>`+
    (!cap.tcpdump_available?`<div class="card"><div style="padding:14px;color:var(--warn)">⚠ tcpdump가 설치되어 있지 않습니다. (yum install tcpdump)</div></div>`:'')+
    activeBar+
    `<div class="card"><div class="card-h">새 캡처 시작</div><div style="padding:16px">
      <div class="form-row"><label>대상 IP (선택)</label><input id="cap-target" class="mono" placeholder="예: 192.168.219.146 (비우면 전체)"></div>
      <div class="form-row"><label>캡처 시간</label><select id="cap-dur"><option value="30">30초</option><option value="60" selected>60초</option><option value="120">120초</option><option value="300">300초</option></select></div>
      <div class="btn-row" style="margin-top:4px"><button class="btn" onclick="capStart()" ${cap.active?'disabled style="opacity:.5"':''}>캡처 시작</button></div>
      <div class="hint">캡처 중 팩스를 송수신하면 SIP 메시지 흐름이 기록됩니다. 지정 시간 후 자동 종료됩니다.</div>
    </div></div>
    <div class="card"><div class="card-h">캡처 기록 <span style="font-size:11px;font-weight:400;opacity:.8">최근 ${caps.length}건</span></div>
    <table class="itable"><tr><th>파일</th><th>시각</th><th>대상</th><th>패킷</th><th>크기</th><th>SIP 흐름</th><th></th></tr>
    ${caps.length?caps.map(cp=>`<tr>
      <td class="mono" style="font-size:11px">${esc(cp.file)}</td>
      <td class="mono">${esc(cp.started)}</td>
      <td class="mono">${esc(cp.target)}</td>
      <td class="mono">${cp.packets}</td>
      <td class="mono">${cp.size_kb} KB</td>
      <td style="font-size:11px">${(cp.sip_flow||[]).length?cp.sip_flow.slice(0,6).map(m=>`<span style="background:var(--panel);padding:1px 5px;border-radius:3px;margin-right:2px;font-family:var(--mono)">${esc(m)}</span>`).join(' '):'<span style="color:var(--ink3)">-</span>'}</td>
      <td><button class="btn danger" onclick="capDel('${esc(cp.file)}')">삭제</button></td>
    </tr>`).join(''):'<tr><td colspan=7 class="empty"><div class="ic">◇</div>캡처 기록이 없습니다<div style="font-size:12px;margin-top:4px">위에서 캡처를 시작해보세요</div></td></tr>'}
    </table></div>`;
}
async function capStart(){
  const target=$('#cap-target')?.value.trim()||'';
  const dur=parseInt($('#cap-dur')?.value||'60');
  try{const r=await api('/capture/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target,duration:dur})});
    toast(r.message||'캡처 시작','ok');loadView();}catch(e){}
}
async function capStop(){try{const r=await api('/capture/stop',{method:'POST'});toast(r.message||'중지','ok');loadView();}catch(e){}}
async function capDel(f){try{await api('/capture/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:f})});loadView();}catch(e){}}

// ===== Reports =====
function renderReports(c,d){
  const st=d.stats||{};
  c.innerHTML=`<div class="h2">Reports</div><div class="sub">송수신 통계</div>
  <div class="kpi-row">
    <div class="kpi"><div class="lbl">총 발송</div><div class="num">${st.send_total||0}</div></div>
    <div class="kpi"><div class="lbl">발송 성공</div><div class="num ok">${st.send_ok||0}</div></div>
    <div class="kpi"><div class="lbl">발송 실패</div><div class="num no">${st.send_fail||0}</div></div>
    <div class="kpi"><div class="lbl">총 수신</div><div class="num">${st.recv_total||0}</div></div>
    <div class="kpi"><div class="lbl">성공률</div><div class="num ${(st.success_rate||0)>=90?'ok':'no'}">${st.success_rate||0}%</div></div>
  </div>`;
}

// ===== 제어 =====
async function fsReload(){try{await api('/fs/reload',{method:'POST'});toast('SIP 프로파일 재로드됨','ok');}catch(e){}}

// ===== 데이터 로딩 =====
async function loadView(){
  try{
    const d=await api('/console/data?view='+CUR);
    DATA[CUR]=d;
    if(d.engine_up!==undefined)$('#hdr-state').innerHTML=d.engine_up?'<span style="color:#0e9f6e">● 정상</span>':'<span style="color:#dc2626">● 엔진 응답없음</span>';
    renderView();
  }catch(e){$('#content').innerHTML='<div class="empty">데이터를 불러올 수 없습니다.</div>';}
}

// 초기화
renderSide();renderTabs();loadView();
// 자동 갱신: 순수 조회 화면만, 그리고 입력 중이면 건너뜀 (폼 리셋 방지)
setInterval(()=>{
  // 폼이 있는 화면(gateways 등)은 자동 갱신 제외 - 입력 중 리셋 방지
  if(!['system','channels','captures'].includes(CUR))return;
  // 입력 필드에 포커스가 있으면 갱신 안 함 (폼 리셋 방지)
  const ae=document.activeElement;
  if(ae&&(ae.tagName==='INPUT'||ae.tagName==='SELECT'||ae.tagName==='TEXTAREA'))return;
  loadView();
},5000);
</script></body></html>'''


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def do_GET(self):
        try:
            if self.path == '/api':
                self._json(200, snapshot())
            elif self.path == '/health':
                # 게이트웨이 상태를 앱 진단이 읽을 수 있게 포함
                gw_raw = _gw_status.get('state', '')
                gw_up = ('REGED' in gw_raw.upper() or 'UP' in gw_raw.upper()
                         or 'NOREG' not in gw_raw.upper() and gw_raw not in ('N/A', 'ERR', 'FS_DOWN', 'UNKNOWN', ''))
                # loopback 모드는 게이트웨이 없이도 정상으로 간주
                if 'loopback' in _gw_status.get('name', '').lower():
                    gw_up = fs_alive()
                self._json(200, {
                    'ok': fs_alive(), 'uptime': uptime_str(),
                    'gateway': gw_up,
                    'gateway_name': _gw_status.get('name', ''),
                    'gateway_state': gw_raw,
                    'gateway_checked': _gw_status.get('checked', '-'),
                })
            elif self.path == '/line-diag':
                return self._json(200, line_diagnostics())
            elif self.path == '/easy-diag':
                return self._json(200, easy_diag_summary())
            elif self.path == '/diag':
                if HAS_DIAG:
                    recent = diag.diag_recent(50)
                    stats = diag.diag_stats()
                    unclassified = sum(x['count'] for x in stats.get('by_category', [])
                                       if x['category'] == 'UNCLASSIFIED')
                    caps = [{'created': r.get('created',''), 'number': r.get('number',''),
                             'file': r.get('capture_file',''), 'packets': '-', 'sip': '-'}
                            for r in recent if r.get('capture_file')]
                    self._json(200, {'recent': recent, 'stats': stats,
                                     'unclassified': unclassified, 'captures': caps})
                else:
                    self._json(200, {'recent': [], 'stats': {'by_category': []},
                                     'unclassified': 0, 'captures': []})
            elif self.path == '/alarms':
                self._json(200, alarms_snapshot())
            elif self.path.startswith('/console/data'):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                view = q.get('view', ['system'])[0]
                return self._json(200, console_data(view))
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers(); self.wfile.write(PAGE.encode())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error(f'GET 오류: {e}')
    def do_POST(self):
        try:
            if self.path == '/reset':
                return self._json(200, {'ok': db_clear()})
            if self.path == '/gateway/create':
                length = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(length)) if length else {}
                ok, msg = create_gateway_xml(
                    data.get('name', ''), data.get('proxy', ''),
                    register=data.get('register', False),
                    username=data.get('username', ''),
                    password=data.get('password', ''),
                    context=data.get('context', 'default'))
                return self._json(200 if ok else 400,
                                  {'ok': ok, 'message': msg} if ok else {'error': msg})
            if self.path == '/gateway/delete':
                length = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(length)) if length else {}
                ok, msg = delete_gateway(data.get('name', ''))
                return self._json(200 if ok else 400,
                                  {'ok': ok, 'message': msg} if ok else {'error': msg})
            if self.path == '/fs/reload':
                _fs_cli("reloadxml")
                _fs_cli("sofia profile external restart reloadxml")
                return self._json(200, {'ok': True})
            if self.path == '/capture/start':
                length = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(length)) if length else {}
                ok, msg = capture_start(data.get('target', ''),
                                        data.get('duration', 60))
                return self._json(200 if ok else 400,
                                  {'ok': ok, 'message': msg} if ok else {'error': msg})
            if self.path == '/capture/stop':
                ok, msg = capture_stop()
                return self._json(200, {'ok': ok, 'message': msg})
            if self.path == '/capture/delete':
                length = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(length)) if length else {}
                ok = capture_delete(data.get('file', ''))
                return self._json(200, {'ok': ok})
            if self.path == '/log/delete':
                # 개별/선택 삭제: {ids:[...]} 또는 {id: N}
                length = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads(self.rfile.read(length)) if length else {}
                if 'ids' in data:
                    n = db_delete_many(data['ids'])
                    return self._json(200, {'ok': True, 'deleted': n})
                elif 'id' in data:
                    db_delete_one(data['id'])
                    return self._json(200, {'ok': True, 'deleted': 1})
                return self._json(400, {'error': 'ids 또는 id 필요'})
            if self.path == '/alarms/ack':
                return self._json(200, {'ok': alarms_ack()})
            if self.path == '/alarms/clear':
                return self._json(200, {'ok': alarms_clear()})
            if self.path.startswith('/debug'):
                # /debug?level=xxx
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                level = (q.get('level', ['info'])[0])
                if HAS_DIAG:
                    ok, msg = diag.set_debug_level(level)
                    return self._json(200, {'ok': ok, 'msg': msg})
                return self._json(200, {'ok': False, 'msg': 'diag module 없음'})
            if self.path == '/captures/clean':
                n = diag.cleanup_captures() if HAS_DIAG else 0
                return self._json(200, {'ok': True, 'remaining': n})
            if self.path == '/send':
                length = int(self.headers.get('Content-Length', 0) or 0)
                if length <= 0 or length > 1_000_000:
                    return self._json(400, {'error': 'invalid content length'})
                data = json.loads(self.rfile.read(length))
                number = str(data.get('number', ''))
                pdf = data.get('pdf', '')
                job_id = data.get('job_id')          # 앱이 준 job_id (결과통지 매칭용)
                async_mode = bool(data.get('async', False))
                if not valid_number(number):
                    return self._json(400, {'error': f'invalid number: {number}'})
                if async_mode:
                    _send_q.put({'number': number, 'pdf': pdf, 'job_id': job_id})
                    # 앱이 매칭할 수 있도록 job_id를 그대로 돌려줌
                    return self._json(202, {'queued': True, 'number': number,
                                            'job_id': job_id, 'queue': _send_q.qsize()})
                result = send_fax(number, pdf, job_id)
                return self._json(200 if result.get('ok') else 500, result)
            self._json(404, {'error': 'not found'})
        except json.JSONDecodeError:
            self._json(400, {'error': 'invalid json'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error(f'POST 오류: {e}')
            self._json(500, {'error': str(e)})
    def log_message(self, *a): pass

def _graceful(signum, frame):
    log.info(f'종료 신호({signum}) 수신 - 정리 중')
    _shutdown.set()
    time.sleep(1)
    sys.exit(0)

def main():
    os.makedirs(FAX_DIR, exist_ok=True)
    # faxguard 프로세스 락 - 중복 실행 방지 (포트 충돌 전에 차단)
    if HAS_GUARD and not guard.acquire_lock('/tmp/faxserver.lock'):
        print('오류: 팩스 서버가 이미 실행 중입니다. (중복 실행 방지)')
        print('  기존 프로세스 종료: sudo pkill -f faxserver')
        sys.exit(1)
    db_init(); cpu_percent()
    if HAS_DIAG:
        diag.diag_db_init()
    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    # 백그라운드 스레드들
    threading.Thread(target=esl_recv_loop, daemon=True).start()
    threading.Thread(target=gw_healthcheck_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    threading.Thread(target=alarm_monitor_loop, daemon=True).start()
    for _ in range(4):   # 발송 워커 4개
        threading.Thread(target=send_worker, daemon=True).start()
    if not fs_alive():
        log.warning('경고: FreeSWITCH 응답 없음 - 실행 상태를 확인하세요')
    log.info(f'FreeSWITCH Dashboard v6 시작 · 대시보드 :{WEB_PORT} · 동시발신 상한 {MAX_CONCURRENT} · GW={_gw_status["name"]} · guard={HAS_GUARD} · diag={HAS_DIAG}')
    print('='*56)
    print(' FreeSWITCH Dashboard v6 · 통합 팩스 서버 (관제·진단·알람·캡처 통합)')
    print(f'  · 대시보드   : http://<서버IP>:{WEB_PORT}/')
    print(f'  · 발송 API   : POST /send  {{"number","pdf","async":true}}')
    print(f'  · 헬스체크   : GET  /health')
    print(f'  · 동시발신   : 최대 {MAX_CONCURRENT} (환경변수 FAX_MAX_CONCURRENT)')
    print(f'  · 게이트웨이 : {_gw_status["name"]} (환경변수 FAX_GATEWAY)')
    print('='*56)
    try:
        ThreadingHTTPServer(('0.0.0.0', WEB_PORT), Handler).serve_forever()
    except OSError as e:
        log.error(f'웹서버 시작 실패(포트 {WEB_PORT} 사용 중?): {e}')
        sys.exit(1)

if __name__ == '__main__':
    main()
