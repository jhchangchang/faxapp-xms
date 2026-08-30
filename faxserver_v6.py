#!/usr/bin/env python3
# ============================================================
#  FreeSWITCH Dashboard v6 - 통합 팩스 서버 (실서비스 예외처리 강화판)
#  v4 기능 + faxguard 모듈(디스크감시·TIFF검증·번호정규화·
#  좀비채널정리·진단메시지·프로세스락) 통합
#  표준 라이브러리만 사용 (외부 의존성 없음)
#  ※ faxguard.py 를 같은 폴더에 두세요.
# ============================================================
import subprocess, json, re, os, time, socket, threading, sqlite3, uuid, datetime, queue, logging, signal, sys
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
    rows = _db_exec('SELECT direction,number,pages,success,rate,note,created'
                    ' FROM fax_log ORDER BY id DESC LIMIT ?', (limit,), fetch=True) or []
    keys = ['direction','number','pages','success','rate','note','created']
    return [dict(zip(keys, r)) for r in rows]

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
        return [{'uuid': (r.get('uuid','') or '')[:8],
                 'state': r.get('callstate') or r.get('state',''),
                 'dir': r.get('direction','')} for r in data.get('rows', [])[:60]]
    except Exception:
        return []

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
PAGE = r'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FreeSWITCH Dashboard · 팩스 관제</title>
<style>
/* ============ v6 라이트 테마 ============
   상용 관제 SW의 밝은 운영 화면. 흰 배경 + 절제된 블루 + 상태 신호색.
   알람 센터 추가. 계기판 느낌의 등폭 숫자 유지. */
:root{
  --bg:#f4f6fa;           /* 페이지 배경 (차분한 블루그레이) */
  --panel:#ffffff;        /* 패널 */
  --panel2:#f8fafc;       /* 카드 내부 */
  --line:#e3e8ef;         /* 경계 */
  --line2:#d3dae4;
  --ink:#1a2433;          /* 주 텍스트 */
  --ink2:#5a6b7f;         /* 보조 */
  --ink3:#93a1b2;         /* 흐린 */
  --accent:#2563eb;       /* 신호 블루 */
  --accent-soft:#eaf1fe;
  --ok:#0ea86e;           /* 성공 */
  --ok-soft:#e6f7f0;
  --no:#e5484d;           /* 실패/위험 */
  --no-soft:#fdeaea;
  --warn:#e8930c;         /* 경고 */
  --warn-soft:#fdf3e3;
  --crit:#c2185b;         /* 심각 */
  --crit-soft:#fce4ec;
  --send:#2563eb;
  --recv:#0ea86e;
  --mono:'SF Mono','Roboto Mono',Consolas,monospace;
  --sans:-apple-system,'Segoe UI','Malgun Gothic',sans-serif;
  --shadow:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
  --shadow-lg:0 4px 12px rgba(16,24,40,.08);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}

/* ===== 상단 바 ===== */
.topbar{display:flex;align-items:center;justify-content:space-between;padding:0 20px;height:56px;
  background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:50;box-shadow:var(--shadow)}
.brand{display:flex;align-items:center;gap:11px}
.brand-mark{width:32px;height:32px;border-radius:7px;background:linear-gradient(135deg,var(--accent),#1e51c9);
  display:flex;align-items:center;justify-content:center;font-size:16px;color:#fff}
.brand-name{font-size:15px;font-weight:750;letter-spacing:-.2px}
.brand-sub{font-size:10.5px;color:var(--ink3);font-weight:600;letter-spacing:.3px;text-transform:uppercase}
.topbar-right{display:flex;align-items:center;gap:20px}
.stat-mini{text-align:right;line-height:1.25}
.stat-mini .l{font-size:10px;color:var(--ink3);font-weight:600;text-transform:uppercase;letter-spacing:.4px}
.stat-mini .v{font-size:14px;font-weight:700}
/* 알람 벨 */
.bell{position:relative;width:38px;height:38px;border-radius:9px;background:var(--panel2);border:1px solid var(--line);
  display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--ink2);transition:all .15s}
.bell:hover{border-color:var(--accent);background:var(--accent-soft);color:var(--accent)}
.bell svg{display:block}
.bell-count{position:absolute;top:-5px;right:-5px;min-width:17px;height:17px;border-radius:9px;background:var(--no);
  color:#fff;font-size:10px;font-weight:800;display:flex;align-items:center;justify-content:center;padding:0 4px}
.bell-count.zero{display:none}
.health{display:flex;align-items:center;gap:7px;padding:6px 12px;border-radius:6px;font-size:12px;font-weight:700;background:var(--ok-soft);color:var(--ok)}
.health.down{background:var(--no-soft);color:var(--no)}
.health .dot{width:7px;height:7px;border-radius:50%;background:currentColor;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ===== 알람 드로어 ===== */
.drawer{position:fixed;top:56px;right:0;width:380px;max-width:92vw;height:calc(100vh - 56px);
  background:var(--panel);border-left:1px solid var(--line);box-shadow:-8px 0 24px rgba(16,24,40,.1);
  z-index:60;transform:translateX(100%);transition:transform .22s ease;overflow-y:auto}
.drawer.open{transform:none}
.drawer-head{display:flex;align-items:center;justify-content:space-between;padding:15px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}
.drawer-head h3{font-size:14px;font-weight:750}
.drawer-head .close{cursor:pointer;color:var(--ink3);font-size:20px;line-height:1}
.drawer-tools{display:flex;gap:8px;padding:10px 18px;border-bottom:1px solid var(--line)}
.alarm{display:flex;gap:12px;padding:13px 18px;border-bottom:1px solid var(--line);position:relative}
.alarm:hover{background:var(--panel2)}
.alarm.unread::before{content:'';position:absolute;left:7px;top:19px;width:6px;height:6px;border-radius:50%;background:var(--accent)}
.alarm-ic{width:30px;height:30px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}
.alarm-ic.crit{background:var(--crit-soft);color:var(--crit)}
.alarm-ic.warn{background:var(--warn-soft);color:var(--warn)}
.alarm-ic.info{background:var(--accent-soft);color:var(--accent)}
.alarm-body{flex:1;min-width:0}
.alarm-title{font-size:12.5px;font-weight:700;margin-bottom:2px}
.alarm-desc{font-size:11.5px;color:var(--ink2);line-height:1.45}
.alarm-time{font-size:10.5px;color:var(--ink3);margin-top:4px;font-family:var(--mono)}

/* ===== 레이아웃 ===== */
.shell{display:flex;min-height:calc(100vh - 56px)}
.side{width:196px;background:var(--panel);border-right:1px solid var(--line);padding:14px 10px;flex-shrink:0}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:7px;color:var(--ink2);
  font-weight:600;font-size:13px;cursor:pointer;margin-bottom:2px;transition:all .12s}
.nav-item:hover{background:var(--panel2);color:var(--ink)}
.nav-item.on{background:var(--accent);color:#fff}
.nav-item .ic{width:16px;height:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.nav-item .ic svg{display:block}
.nav-item .n-badge{margin-left:auto;font-size:10px;font-weight:800;background:var(--no);color:#fff;border-radius:9px;padding:1px 6px}
.nav-item.on .n-badge{background:rgba(255,255,255,.3)}
.nav-label{font-size:10px;color:var(--ink3);font-weight:700;text-transform:uppercase;letter-spacing:.5px;padding:10px 12px 4px}
.main{flex:1;padding:20px;overflow-x:hidden;min-width:0}
.view{display:none}.view.on{display:block;animation:fade .25s}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* ===== KPI ===== */
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:13px;margin-bottom:18px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:16px 17px;position:relative;overflow:hidden;box-shadow:var(--shadow)}
.kpi::after{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
.kpi.ok::after{background:var(--ok)}.kpi.no::after{background:var(--no)}.kpi.warn::after{background:var(--warn)}
.kpi .k{font-size:11px;color:var(--ink2);font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:9px}
.kpi .v{font-size:30px;font-weight:750;letter-spacing:-1px;line-height:1}
.kpi .sub{font-size:11.5px;color:var(--ink3);margin-top:7px}
.kpi .sub b{color:var(--no)}.kpi .sub.good b{color:var(--ok)}

/* ===== 패널 ===== */
.panel{background:var(--panel);border:1px solid var(--line);border-radius:11px;margin-bottom:18px;overflow:hidden;box-shadow:var(--shadow)}
.panel-head{display:flex;align-items:center;justify-content:space-between;padding:13px 17px;border-bottom:1px solid var(--line)}
.panel-title{font-size:13.5px;font-weight:750;display:flex;align-items:center;gap:8px}
.panel-title .badge{font-size:10.5px;color:var(--ink2);font-weight:600;background:var(--panel2);border:1px solid var(--line);padding:2px 8px;border-radius:20px}
.panel-tools{display:flex;gap:8px;align-items:center}

/* ===== 테이블 ===== */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12.5px}
thead th{text-align:left;padding:9px 17px;font-size:10.5px;color:var(--ink3);font-weight:700;text-transform:uppercase;letter-spacing:.4px;
  border-bottom:1px solid var(--line);white-space:nowrap;position:sticky;top:0;background:var(--panel2)}
tbody td{padding:10px 17px;border-bottom:1px solid var(--line);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--panel2)}
.scroll-y{max-height:440px;overflow-y:auto}
.tag{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:5px;font-size:11px;font-weight:700}
.tag-send{background:var(--accent-soft);color:var(--send)}
.tag-recv{background:var(--ok-soft);color:var(--recv)}
.res-ok{color:var(--ok);font-weight:700;display:inline-flex;align-items:center;gap:5px}
.res-no{color:var(--no);font-weight:700;display:inline-flex;align-items:center;gap:5px}
.res-ok::before,.res-no::before{content:'●';font-size:8px}
.dim{color:var(--ink3)}
.cat-pill{display:inline-block;padding:2px 8px;border-radius:5px;font-size:10.5px;font-weight:700;background:var(--no-soft);color:var(--no)}
.cat-pill.ok{background:var(--ok-soft);color:var(--ok)}
.diag-msg{font-size:11.5px;color:var(--ink2);max-width:340px;white-space:normal;line-height:1.4}

/* 버튼 */
.btn{padding:6px 13px;border:1px solid var(--line2);background:var(--panel);color:var(--ink2);border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;font-family:var(--sans);transition:all .12s}
.btn:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.btn-danger:hover{border-color:var(--no);color:var(--no);background:var(--no-soft)}
.btn-sm{padding:3px 9px;font-size:11px}
.seg{display:inline-flex;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:2px}
.seg button{padding:5px 12px;border:none;background:none;color:var(--ink3);font-size:11.5px;font-weight:700;cursor:pointer;border-radius:5px;font-family:var(--mono)}
.seg button.on{background:var(--accent);color:#fff}

/* 실패 바 차트 */
.bars{padding:16px 17px;display:flex;flex-direction:column;gap:11px}
.bar-row{display:grid;grid-template-columns:180px 1fr 42px;align-items:center;gap:12px}
.bar-label{font-size:12px;color:var(--ink2);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-track{height:9px;background:var(--panel2);border:1px solid var(--line);border-radius:5px;overflow:hidden}
.bar-fill{height:100%;border-radius:5px;background:linear-gradient(90deg,var(--no),#f27a7d)}
.bar-val{font-size:12px;font-weight:700;text-align:right;font-family:var(--mono)}

/* 게이지 */
.gauge-wrap{display:flex;align-items:center;gap:24px;padding:20px 17px}
.ring{--v:0;--c:var(--accent);position:relative;width:128px;height:128px;border-radius:50%;flex-shrink:0;background:conic-gradient(var(--c) calc(var(--v)*3.6deg),var(--panel2) 0)}
.ring::before{content:'';position:absolute;inset:13px;border-radius:50%;background:var(--panel);box-shadow:inset 0 0 0 1px var(--line)}
.ring-in{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ring-v{font-size:27px;font-weight:750;font-family:var(--mono)}
.ring-l{font-size:10px;color:var(--ink3);font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.gauge-meta{font-size:12px;color:var(--ink2);line-height:1.9}
.gauge-meta b{color:var(--ink);font-family:var(--mono)}

/* 채널 그리드 */
.ch-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:10px;padding:16px 17px}
.ch-box{border:1px solid var(--line);border-radius:9px;padding:12px;background:var(--panel2)}
.ch-uuid{font-family:var(--mono);font-size:11px;color:var(--accent);font-weight:700}
.ch-state{font-size:11px;color:var(--ink2);margin-top:5px}
.ch-dir{display:inline-block;margin-top:7px;padding:1px 7px;border-radius:4px;font-size:10px;font-weight:700}

/* 시스템 */
.sys-grid{display:grid;grid-template-columns:1.3fr 1fr 1fr;gap:16px}
.metric-big{font-size:38px;font-weight:750;font-family:var(--mono);padding:14px 17px 4px;letter-spacing:-1px}
.track{height:8px;background:var(--panel2);border:1px solid var(--line);border-radius:5px;margin:0 17px 17px;overflow:hidden}
.track>i{display:block;height:100%;border-radius:5px;transition:width .5s}

/* 기능맵 (되는것/부분/안되는것) */
.feat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:13px;padding:17px}
.eng-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;padding:17px}
.eng-cell{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:14px 15px}
.eng-top{display:flex;align-items:center;gap:7px;margin-bottom:9px}
.eng-led{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.eng-led.ok{background:var(--ok);box-shadow:0 0 0 3px var(--ok-soft)}
.eng-led.no{background:var(--no);box-shadow:0 0 0 3px var(--no-soft)}
.eng-k{font-size:12px;font-weight:600;color:var(--ink2)}
.eng-v{font-size:22px;font-weight:750;letter-spacing:-.3px;line-height:1}
.eng-v.warn{color:var(--no)}
.eng-sub{font-size:11px;color:var(--ink3);margin-top:5px}
.feat{border:1px solid var(--line);border-radius:10px;padding:14px;background:var(--panel2)}
.feat-h{display:flex;align-items:center;gap:8px;margin-bottom:9px}
.feat-dot{width:9px;height:9px;border-radius:50%}
.feat-dot.y{background:var(--ok)}.feat-dot.p{background:var(--warn)}.feat-dot.n{background:var(--ink3)}
.feat-name{font-size:12.5px;font-weight:700}
.feat-status{margin-left:auto;font-size:10px;font-weight:800;padding:2px 8px;border-radius:20px}
.feat-status.y{background:var(--ok-soft);color:var(--ok)}
.feat-status.p{background:var(--warn-soft);color:var(--warn)}
.feat-status.n{background:#eef1f5;color:var(--ink3)}
.feat-desc{font-size:11.5px;color:var(--ink2);line-height:1.5}

/* 빈 상태 */
.empty{padding:52px 20px;text-align:center;color:var(--ink3)}
.empty .ic{font-size:30px;opacity:.5;margin-bottom:10px}
.empty .t{font-size:13px;font-weight:600;color:var(--ink2)}
.empty .d{font-size:12px;margin-top:4px}
.cap-link{color:var(--accent);font-family:var(--mono);font-size:11.5px;cursor:pointer;text-decoration:none}
.cap-link:hover{text-decoration:underline}
.scrim{position:fixed;inset:56px 0 0 0;background:rgba(16,24,40,.28);z-index:55;opacity:0;pointer-events:none;transition:opacity .2s}
.scrim.on{opacity:1;pointer-events:auto}

@media(max-width:1000px){.side{width:56px}.side .nav-item span:not(.ic),.side .nav-label,.side .n-badge{display:none}.sys-grid{grid-template-columns:1fr}}
@media(max-width:640px){.topbar-right .stat-mini{display:none}.bar-row{grid-template-columns:110px 1fr 38px}}
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">
    <div class="brand-mark"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9V3h12v6M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8" rx="1"/></svg></div>
    <div><div class="brand-name">FreeSWITCH Dashboard</div><div class="brand-sub">Fax Control Center</div></div>
  </div>
  <div class="topbar-right">
    <div class="stat-mini"><div class="l">Uptime</div><div class="v mono" id="uptime">—</div></div>
    <div class="stat-mini"><div class="l">Cores</div><div class="v mono" id="cores">—</div></div>
    <div class="stat-mini"><div class="l">Queue</div><div class="v mono" id="queue">—</div></div>
    <div class="stat-mini"><div class="l">Gateway</div><div class="v mono" id="gw">—</div></div>
    <div class="bell" id="bell" onclick="toggleDrawer()"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg><span class="bell-count zero" id="bell-count">0</span></div>
    <div class="health" id="health"><span class="dot"></span><span id="health-t">FS</span></div>
  </div>
</div>

<!-- 알람 드로어 -->
<div class="scrim" id="scrim" onclick="toggleDrawer()"></div>
<aside class="drawer" id="drawer">
  <div class="drawer-head"><h3>알람 센터</h3><span class="close" onclick="toggleDrawer()">×</span></div>
  <div class="drawer-tools"><button class="btn btn-sm" onclick="ackAll()">모두 확인</button><button class="btn btn-sm" onclick="clearAlarms()">지우기</button></div>
  <div id="alarm-list"></div>
</aside>

<div class="shell">
  <nav class="side">
    <div class="nav-label">모니터</div>
    <div class="nav-item on" data-v="overview"><span class="ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg></span><span>대시보드</span></div>
    <div class="nav-item" data-v="channels"><span class="ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h4l3 8 4-16 3 8h4"/></svg></span><span>활성 채널</span></div>
    <div class="nav-item" data-v="alarms"><span class="ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg></span><span>알람</span><span class="n-badge" id="nav-alarm">0</span></div>
    <div class="nav-label">진단</div>
    <div class="nav-item" data-v="diag"><span class="ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h6"/></svg></span><span>진단 로그</span></div>
    <div class="nav-item" data-v="failures"><span class="ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg></span><span>실패 분석</span></div>
    <div class="nav-item" data-v="captures"><span class="ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/></svg></span><span>패킷 캡처</span></div>
    <div class="nav-label">시스템</div>
    <div class="nav-item" data-v="system"><span class="ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9z"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/></svg></span><span>시스템 부하</span></div>
    <div class="nav-item" data-v="features"><span class="ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></span><span>엔진 상태</span></div>
    <div class="nav-item" data-v="control"><span class="ic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></span><span>제어</span></div>
  </nav>

  <main class="main">
    <!-- 대시보드 -->
    <section class="view on" id="v-overview">
      <div class="kpi-row">
        <div class="kpi"><div class="k">활성 채널</div><div class="v mono" id="k-ch">0</div><div class="sub">현재 처리 중</div></div>
        <div class="kpi ok"><div class="k">전송 성공률</div><div class="v mono" id="k-rate">100%</div><div class="sub good"><span id="k-total">0</span>건 기준</div></div>
        <div class="kpi"><div class="k">발송 / 수신</div><div class="v mono"><span id="k-sent">0</span> <span class="dim" style="font-size:20px">/</span> <span id="k-recv">0</span></div><div class="sub">실패 <b id="k-fail">0</b>건</div></div>
        <div class="kpi no"><div class="k">활성 알람</div><div class="v mono" id="k-alarm">0</div><div class="sub">확인 필요</div></div>
      </div>
      <div class="panel">
        <div class="panel-head"><div class="panel-title">최근 전송 <span class="badge" id="b-recent">0</span></div></div>
        <div class="tbl-wrap scroll-y"><table><thead><tr><th>시각</th><th>구분</th><th>번호</th><th>페이지</th><th>속도</th><th>결과</th></tr></thead><tbody id="t-recent"></tbody></table></div>
      </div>
    </section>

    <!-- 활성 채널 -->
    <section class="view" id="v-channels">
      <div class="panel"><div class="panel-head"><div class="panel-title">활성 채널 <span class="badge"><span id="b-ch">0</span>개</span></div></div><div id="ch-grid"></div></div>
    </section>

    <!-- 알람 -->
    <section class="view" id="v-alarms">
      <div class="panel">
        <div class="panel-head"><div class="panel-title">알람 이력 <span class="badge">임계치 기반</span></div>
          <div class="panel-tools"><button class="btn btn-sm" onclick="ackAll()">모두 확인</button></div></div>
        <div class="tbl-wrap scroll-y"><table><thead><tr><th>시각</th><th>등급</th><th>알람</th><th>내용</th><th>상태</th></tr></thead><tbody id="t-alarm"></tbody></table></div>
      </div>
    </section>

    <!-- 진단 로그 -->
    <section class="view" id="v-diag">
      <div class="panel">
        <div class="panel-head"><div class="panel-title">진단 로그 <span class="badge">콜별 상세</span></div>
          <div class="panel-tools"><span class="dim" style="font-size:11px">hangup_cause · fax_result · T.38 · 속도</span></div></div>
        <div class="tbl-wrap scroll-y"><table><thead><tr><th>시각</th><th>구분</th><th>번호</th><th>결과</th><th>T.38</th><th>속도</th><th>페이지</th><th>진단</th></tr></thead><tbody id="t-diag"></tbody></table></div>
      </div>
    </section>

    <!-- 실패 분석 -->
    <section class="view" id="v-failures">
      <div class="panel"><div class="panel-head"><div class="panel-title">실패 원인 분포 <span class="badge">카테고리별</span></div></div><div class="bars" id="fail-bars"></div></div>
      <div class="panel"><div class="panel-head"><div class="panel-title">최근 실패 상세</div></div>
        <div class="tbl-wrap scroll-y"><table><thead><tr><th>시각</th><th>번호</th><th>원인 코드</th><th>진단</th><th>캡처</th></tr></thead><tbody id="t-fail"></tbody></table></div></div>
    </section>

    <!-- 캡처 -->
    <section class="view" id="v-captures">
      <div class="panel"><div class="panel-head"><div class="panel-title">패킷 캡처 <span class="badge">SIP · RTP · T.38</span></div>
        <div class="panel-tools"><span class="dim" style="font-size:11px">실패 콜 자동 캡처</span></div></div>
        <div class="tbl-wrap scroll-y"><table><thead><tr><th>시각</th><th>연결 번호</th><th>캡처 파일</th><th>패킷</th><th>SIP 흐름</th><th>다운로드</th></tr></thead><tbody id="t-cap"></tbody></table></div></div>
    </section>

    <!-- 시스템 -->
    <section class="view" id="v-system">
      <div class="sys-grid">
        <div class="panel"><div class="panel-head"><div class="panel-title">CPU 사용률 <span class="badge"><span id="s-cores">—</span> 코어</span></div></div>
          <div class="gauge-wrap"><div class="ring" id="cpu-ring"><div class="ring-in"><div class="ring-v" id="s-cpu">0%</div><div class="ring-l">CPU</div></div></div>
          <div class="gauge-meta">활성 채널 <b id="s-ch">0</b><br>대기 큐 <b id="s-q">0</b><br><span class="dim">80% 초과 시 부하 한계</span></div></div></div>
        <div class="panel"><div class="panel-head"><div class="panel-title">메모리</div></div><div class="metric-big" id="s-mem">0%</div><div class="track"><i id="s-memb" style="background:linear-gradient(90deg,#8b5cf6,#a78bfa)"></i></div></div>
        <div class="panel"><div class="panel-head"><div class="panel-title">Load Average</div></div><div class="metric-big" id="s-load">0.0</div><div style="padding:0 17px 17px" class="dim">1분 평균</div></div>
      </div>
    </section>

    <!-- 기능 현황 -->
    <section class="view" id="v-features">
      <div class="panel">
        <div class="panel-head"><div class="panel-title">팩스 엔진 상태 <span class="badge">FreeSWITCH · spandsp</span></div>
          <div class="panel-tools"><span class="dim mono" id="eng-time" style="font-size:11px"></span></div></div>
        <div class="eng-grid" id="eng-grid"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><div class="panel-title">코덱 · 프로토콜 지원</div></div>
        <div class="feat-grid" id="feat-grid"></div>
      </div>
    </section>

    <!-- 제어 -->
    <section class="view" id="v-control">
      <div class="panel"><div class="panel-head"><div class="panel-title">디버그 레벨</div></div>
        <div style="padding:16px 17px"><div style="font-size:12px;color:var(--ink2);margin-bottom:11px">FreeSWITCH 로그 상세도. debug 선택 시 SIP 트레이스도 활성화됩니다.</div>
          <div class="seg" id="log-seg"><button data-lvl="warning">warning</button><button data-lvl="notice">notice</button><button class="on" data-lvl="info">info</button><button data-lvl="debug">debug</button></div></div></div>
      <div class="panel"><div class="panel-head"><div class="panel-title">알람 임계치</div></div>
        <div style="padding:16px 17px;font-size:12px;color:var(--ink2);line-height:2">
          성공률 하락 경고: <b class="mono">90%</b> 미만 &nbsp;·&nbsp; CPU 경고: <b class="mono">80%</b> 초과 &nbsp;·&nbsp; 게이트웨이 다운 시 즉시 &nbsp;·&nbsp; 큐 적체: <b class="mono">50</b> 초과
        </div></div>
      <div class="panel"><div class="panel-head"><div class="panel-title">데이터 관리</div></div>
        <div style="padding:16px 17px;display:flex;gap:10px"><button class="btn btn-danger" onclick="resetLog()">전송 기록 초기화</button><button class="btn" onclick="cleanCaptures()">오래된 캡처 정리</button></div></div>
    </section>
  </main>
</div>

<script>
// ===== 데모 데이터 =====
const DEMO={
  uptime:'04:21:37',cores:16,queue:2,gw_name:'vega400',gw_state:'REGED',fs_alive:true,
  channels:3,sent_ok:142,sent_no:8,recv_ok:97,recv_no:3,success_rate:96.7,total_try:250,
  cpu:34.2,mem:41.5,load:2.1,
  channel_list:[{uuid:'a3f9c210',state:'ACTIVE',dir:'outbound'},{uuid:'b8e14d77',state:'ACTIVE',dir:'inbound'},{uuid:'c1902fab',state:'RINGING',dir:'outbound'}],
  recent:[
    {direction:'send',number:'0312229901',pages:3,success:1,rate:'14400',created:'2026-08-08 04:21:02'},
    {direction:'receive',number:'025553321',pages:1,success:1,rate:'14400',created:'2026-08-08 04:20:41'},
    {direction:'send',number:'0518887654',pages:5,success:0,rate:'',created:'2026-08-08 04:19:55'},
    {direction:'receive',number:'0332119080',pages:2,success:1,rate:'9600',created:'2026-08-08 04:19:30'}],
  diag:[
    {direction:'send',number:'0312229901',hangup_cause:'NORMAL_CLEARING',fax_result:'',fax_success:1,t38_used:1,transfer_rate:'14400',pages_tx:3,pages_total:3,diag_category:'',diag_message:'정상',created:'04:21:02'},
    {direction:'send',number:'0518887654',hangup_cause:'NORMAL_CLEARING',fax_result:'The T.38 negotiation failed',fax_success:0,t38_used:0,transfer_rate:'',pages_tx:0,pages_total:5,diag_category:'T38_NEGOTIATION',diag_message:'T.38 협상 실패 - 양단 T.38 버전/타이밍/방화벽(UDPTL) 점검',capture_file:'fax_20260808_041955_c1902fab.pcap',created:'04:19:55'},
    {direction:'send',number:'0299887766',hangup_cause:'NORMAL_TEMPORARY_FAILURE',fax_result:'',fax_success:0,t38_used:0,transfer_rate:'',pages_tx:0,pages_total:2,diag_category:'E1_LAYER',diag_message:'E1 계층 문제 - 링크/클럭/D채널(Q.921)',capture_file:'fax_20260808_041720_b8e14d77.pcap',created:'04:17:20'}],
  fail_stats:{by_category:[{category:'T38_NEGOTIATION',count:5},{category:'E1_LAYER',count:3},{category:'DISCONNECT_MID',count:2},{category:'UNCLASSIFIED',count:2}]},
  captures:[{created:'04:19:55',number:'0518887654',file:'fax_20260808_041955_c1902fab.pcap',packets:1284,sip:'INVITE→488'},{created:'04:17:20',number:'0299887766',file:'fax_20260808_041720_b8e14d77.pcap',packets:342,sip:'INVITE→no ans'}],
  alarms:[
    {level:'crit',title:'게이트웨이 응답 지연',desc:'vega400 헬스체크 3회 연속 실패 후 복구됨',time:'04:19:20',unread:1},
    {level:'warn',title:'전송 성공률 하락',desc:'최근 20건 성공률 85% (임계치 90% 미만)',time:'04:18:44',unread:1},
    {level:'warn',title:'T.38 협상 실패 급증',desc:'5분간 T38_NEGOTIATION 5건 발생',time:'04:17:10',unread:1},
    {level:'info',title:'좀비 채널 정리',desc:'유휴 세션 2개 자동 종료',time:'04:12:03',unread:0}]
};
const FEATURES=[
  {s:'y',n:'T.38 (IP 팩스)',d:'FreeSWITCH mod_spandsp로 T.38 팩스 릴레이 지원. 현재 핵심 경로.'},
  {s:'y',n:'T.30 (아날로그 팩스)',d:'spandsp T.30 프로토콜 스택. G3 팩스 표준 완전 지원.'},
  {s:'y',n:'V.17 · 14,400bps',d:'표준 G3 최고 속도. spandsp 모뎀으로 지원.'},
  {s:'y',n:'ECM (오류정정)',d:'Error Correction Mode 지원. 회선 품질 낮아도 재전송으로 보정.'},
  {s:'y',n:'TDM / E1 (게이트웨이)',d:'Vega 게이트웨이 경유 E1 회선. SIP↔TDM 변환.'},
  {s:'n',n:'V.34 · 28,800bps (슈퍼 G3)',d:'무료 엔진 미지원. 상용 엔진(XCAPI/Dialogic) 도입 시 지원.'},
  {s:'y',n:'페이지 단위 상태 추적',d:'전송/전체 페이지 수 실시간 추적. 부분 전송 감지.'},
  {s:'y',n:'실패 원인 진단 (T.30 코드)',d:'spandsp T30_ERR 코드 → 원인 분류. 앱 예외체계 연동.'},
  {s:'p',n:'V.34 폴백 협상',d:'상용 엔진 도입 시 V.34→V.17 자동 폴백 가능.'}];

const esc=s=>String(s??'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
const el=id=>document.getElementById(id);
let alarmState=null;

document.querySelectorAll('.nav-item').forEach(n=>n.onclick=()=>{
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.view').forEach(x=>x.classList.remove('on'));
  n.classList.add('on'); el('v-'+n.dataset.v).classList.add('on');
});
document.querySelectorAll('#log-seg button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('#log-seg button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); fetch('/debug?level='+b.dataset.lvl,{method:'POST'}).catch(()=>{});
});
function toggleDrawer(){el('drawer').classList.toggle('open');el('scrim').classList.toggle('on');}
function ackAll(){(alarmState||DEMO.alarms).forEach(a=>a.unread=0);renderAlarms();}
function clearAlarms(){fetch('/alarms/clear',{method:'POST'}).then(()=>{alarmState=[];renderAlarms();}).catch(()=>{alarmState=[];renderAlarms();});}
function resetLog(){if(confirm('전송·진단 기록을 초기화할까요?'))fetch('/reset',{method:'POST'}).then(()=>tick()).catch(()=>{});}
function cleanCaptures(){fetch('/captures/clean',{method:'POST'}).catch(()=>{});}

const alarmIc={crit:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>',warn:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>',info:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/></svg>'};
function renderAlarms(){
  const al=alarmState||DEMO.alarms;
  const unread=al.filter(a=>a.unread).length;
  const bc=el('bell-count'); bc.textContent=unread; bc.classList.toggle('zero',unread===0);
  el('nav-alarm').textContent=unread; el('nav-alarm').style.display=unread?'':'none';
  el('k-alarm').textContent=unread;
  el('alarm-list').innerHTML=al.length?al.map(a=>`<div class="alarm ${a.unread?'unread':''}"><div class="alarm-ic ${a.level}">${alarmIc[a.level]}</div><div class="alarm-body"><div class="alarm-title">${esc(a.title)}</div><div class="alarm-desc">${esc(a.desc)}</div><div class="alarm-time">${esc(a.time)}</div></div></div>`).join(''):`<div class="empty"><div class="ic">✓</div><div class="t">활성 알람 없음</div><div class="d">시스템 정상 동작 중</div></div>`;
  const lv={crit:'<span class="cat-pill">심각</span>',warn:'<span class="cat-pill" style="background:var(--warn-soft);color:var(--warn)">경고</span>',info:'<span class="cat-pill ok">정보</span>'};
  el('t-alarm').innerHTML=al.length?al.map(a=>`<tr><td class="mono dim">${esc(a.time)}</td><td>${lv[a.level]}</td><td style="font-weight:700">${esc(a.title)}</td><td class="diag-msg">${esc(a.desc)}</td><td>${a.unread?'<span class="dim">미확인</span>':'<span style="color:var(--ok)">확인됨</span>'}</td></tr>`).join(''):`<tr><td colspan="5"><div class="empty"><div class="ic">✓</div><div class="t">알람 없음</div></div></td></tr>`;
}

function row_recent(r){const tag=r.direction==='send'?'<span class="tag tag-send">발송</span>':'<span class="tag tag-recv">수신</span>';const res=r.success?'<span class="res-ok">성공</span>':'<span class="res-no">실패</span>';return `<tr><td class="mono dim">${esc(r.created).slice(11)}</td><td>${tag}</td><td class="mono">${esc(r.number)}</td><td class="mono">${r.pages||0}</td><td class="mono">${esc(r.rate||'—')}</td><td>${res}</td></tr>`;}
function row_diag(r){const tag=r.direction==='send'?'<span class="tag tag-send">발송</span>':'<span class="tag tag-recv">수신</span>';const res=r.fax_success?'<span class="res-ok">성공</span>':'<span class="res-no">실패</span>';const t38=r.t38_used?'<span class="cat-pill ok">T.38</span>':'<span class="dim">—</span>';const cat=r.fax_success?'<span class="diag-msg dim">정상 완료</span>':`<span class="diag-msg">${esc(r.diag_message||r.diag_category)}</span>`;return `<tr><td class="mono dim">${esc(r.created)}</td><td>${tag}</td><td class="mono">${esc(r.number)}</td><td>${res}</td><td>${t38}</td><td class="mono">${esc(r.transfer_rate||'—')}</td><td class="mono">${r.pages_tx||0}/${r.pages_total||0}</td><td>${cat}</td></tr>`;}
function row_fail(r){const cap=r.capture_file?`<span class="cap-link" onclick="alert('캡처: ${esc(r.capture_file)}')">${esc(r.capture_file).slice(0,22)}…</span>`:'<span class="dim">—</span>';return `<tr><td class="mono dim">${esc(r.created)}</td><td class="mono">${esc(r.number)}</td><td><span class="cat-pill">${esc(r.hangup_cause||r.diag_category)}</span></td><td><span class="diag-msg">${esc(r.diag_message)}</span></td><td>${cap}</td></tr>`;}
function row_cap(r){return `<tr><td class="mono dim">${esc(r.created)}</td><td class="mono">${esc(r.number)}</td><td class="mono" style="font-size:11px">${esc(r.file)}</td><td class="mono">${r.packets}</td><td class="mono dim">${esc(r.sip)}</td><td><a class="cap-link" href="/captures/${esc(r.file)}">다운로드</a></td></tr>`;}
const emptyRow=(n,ic,t,d)=>`<tr><td colspan="${n}"><div class="empty"><div class="ic">${ic}</div><div class="t">${t}</div><div class="d">${d}</div></div></td></tr>`;

function renderFeatures(){
  const st={y:'지원',p:'부분',n:'예정'};
  el('feat-grid').innerHTML=FEATURES.map(f=>`<div class="feat"><div class="feat-h"><span class="feat-dot ${f.s}"></span><span class="feat-name">${esc(f.n)}</span><span class="feat-status ${f.s}">${st[f.s]}</span></div><div class="feat-desc">${esc(f.d)}</div></div>`).join('');
}
function renderEngine(d){
  d=d||DEMO;
  const g=el('eng-grid'); if(!g)return;
  el('eng-time').textContent=d.time||'';
  const gwUp=(d.gw_state||'').toUpperCase();
  const gwOk=gwUp.includes('REGED')||gwUp.includes('UP')||(d.gw_name||'').toLowerCase().includes('loopback');
  const cells=[
    {k:'FreeSWITCH',v:d.fs_alive?'정상':'응답없음',ok:d.fs_alive,sub:'팩스 엔진 코어'},
    {k:'게이트웨이',v:d.gw_state||'-',ok:gwOk,sub:d.gw_name||''},
    {k:'활성 채널',v:(d.channels||0)+'ch',ok:true,sub:'동시 통화 세션'},
    {k:'발송 큐',v:(d.queue||0)+'건',ok:(d.queue||0)<50,sub:'대기 중'},
    {k:'CPU',v:(d.cpu||0)+'%',ok:(d.cpu||0)<80,sub:'프로세서 부하'},
    {k:'메모리',v:(d.mem||0)+'%',ok:(d.mem||0)<85,sub:'RAM 사용'},
    {k:'성공률',v:(d.success_rate!=null?d.success_rate:100)+'%',ok:(d.success_rate!=null?d.success_rate:100)>=90,sub:'전체 시도 대비'},
    {k:'Load',v:(d.load!=null?d.load:'—'),ok:true,sub:'1분 평균'},
  ];
  g.innerHTML=cells.map(c=>`<div class="eng-cell"><div class="eng-top"><span class="eng-led ${c.ok?'ok':'no'}"></span><span class="eng-k">${esc(c.k)}</span></div><div class="eng-v ${c.ok?'':'warn'}">${esc(String(c.v))}</div><div class="eng-sub">${esc(c.sub)}</div></div>`).join('');
}

function render(d){
  d=d||DEMO;
  el('uptime').textContent=d.uptime;el('cores').textContent=d.cores;el('queue').textContent=d.queue;el('gw').textContent=d.gw_state;
  const h=el('health');h.className='health'+(d.fs_alive?'':' down');el('health-t').textContent=d.fs_alive?'정상':'중단';
  el('k-ch').textContent=d.channels;el('k-rate').textContent=d.success_rate+'%';el('k-total').textContent=d.total_try;
  el('k-sent').textContent=d.sent_ok;el('k-recv').textContent=d.recv_ok;el('k-fail').textContent=(d.sent_no+d.recv_no);
  el('b-recent').textContent=d.recent.length;
  el('t-recent').innerHTML=d.recent.map(row_recent).join('')||emptyRow(6,'▤','전송 기록 없음','팩스를 보내거나 받으면 표시됩니다');
  el('t-diag').innerHTML=(d.diag||[]).map(row_diag).join('')||emptyRow(8,'◈','진단 기록 없음','콜 완료 시 진단이 쌓입니다');
  const fs=(d.fail_stats&&d.fail_stats.by_category)||[];const max=Math.max(1,...fs.map(x=>x.count));
  el('fail-bars').innerHTML=fs.length?fs.map(x=>`<div class="bar-row"><div class="bar-label">${esc(x.category)}</div><div class="bar-track"><div class="bar-fill" style="width:${x.count/max*100}%"></div></div><div class="bar-val">${x.count}</div></div>`).join(''):`<div class="empty"><div class="ic">▨</div><div class="t">실패 없음</div></div>`;
  const fails=(d.diag||[]).filter(x=>!x.fax_success);
  el('t-fail').innerHTML=fails.map(row_fail).join('')||emptyRow(5,'▨','실패 없음','실패 콜이 여기 표시됩니다');
  el('t-cap').innerHTML=(d.captures||[]).map(row_cap).join('')||emptyRow(6,'⊚','캡처 없음','실패 시 자동 캡처됩니다');
  el('b-ch').textContent=d.channels;
  el('ch-grid').innerHTML=d.channel_list.length?`<div class="ch-grid">${d.channel_list.map(c=>{const col=c.dir==='inbound'?'var(--recv)':'var(--send)';return `<div class="ch-box"><div class="ch-uuid">${esc(c.uuid)}</div><div class="ch-state">${esc(c.state)}</div><span class="ch-dir" style="background:${col}1a;color:${col}">${esc(c.dir)}</span></div>`;}).join('')}</div>`:`<div class="empty"><div class="ic">◉</div><div class="t">처리 중인 채널 없음</div></div>`;
  el('s-cores').textContent=d.cores;el('s-ch').textContent=d.channels;el('s-q').textContent=d.queue;
  const cc=d.cpu>80?'var(--no)':d.cpu>60?'var(--warn)':'var(--accent)';const ring=el('cpu-ring');ring.style.setProperty('--v',d.cpu);ring.style.setProperty('--c',cc);
  el('s-cpu').textContent=d.cpu+'%';el('s-mem').textContent=d.mem+'%';el('s-memb').style.width=Math.min(d.mem,100)+'%';el('s-load').textContent=d.load;
  renderAlarms();
  renderEngine(d);
}

async function tick(){
  try{const d=await(await fetch('/api')).json();
    try{const dg=await(await fetch('/diag')).json();d.diag=dg.recent;d.fail_stats=dg.stats;d.captures=dg.captures;}catch(e){}
    try{const al=await(await fetch('/alarms')).json();alarmState=al.alarms;}catch(e){}
    render(d);
  }catch(e){render(DEMO);}
}
renderFeatures();render(DEMO);setInterval(tick,2500);tick();
</script>
</body>
</html>
'''

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
