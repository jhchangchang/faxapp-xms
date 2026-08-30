#!/usr/bin/env python3
# ============================================================
#  faxdiag.py  -  팩스 진단·네트워크 캡처 모듈 (v5 관측 체계)
#  · 콜별 상세 결과 DB 적재 (hangup_cause, fax_result, T.38, 속도)
#  · 디버그 레벨 동적 설정 (FreeSWITCH sofia/console)
#  · 네트워크 캡처 (tcpdump로 SIP/RTP/T.38 패킷 저장)
#  · 실패 구간 자동 캡처 → 리포트 → DB 기록
#  표준 라이브러리만 사용. (tcpdump는 시스템 설치 필요)
# ============================================================
import os, re, time, json, sqlite3, subprocess, threading, datetime, signal

FS_CLI    = '/usr/local/freeswitch/bin/fs_cli'
DB_PATH   = '/opt/faxapp/fax.db'
CAP_DIR   = '/opt/faxapp/captures'
CAP_IFACE = os.environ.get('FAX_CAP_IFACE', 'any')   # 캡처 인터페이스
CAP_MAX   = 50            # 보관할 최대 캡처 파일 수
CAP_SECS  = 60            # 1건 캡처 최대 시간(초)


# ==================== DB: 상세 진단 테이블 ====================
def diag_db_init():
    """진단 상세 테이블 생성 (기존 fax_log와 별도, call_uuid로 연결)"""
    try:
        con = sqlite3.connect(DB_PATH, timeout=15)
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('''CREATE TABLE IF NOT EXISTS fax_diag(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_uuid TEXT,
            direction TEXT,
            number TEXT,
            hangup_cause TEXT,
            fax_result TEXT,
            fax_success INTEGER,
            t38_used INTEGER,
            negotiate_count INTEGER,
            transfer_rate TEXT,
            ecm_used INTEGER,
            pages_tx INTEGER,
            pages_total INTEGER,
            bad_rows INTEGER,
            capture_file TEXT,
            diag_category TEXT,
            diag_message TEXT,
            raw_vars TEXT,
            created TEXT)''')
        con.commit(); con.close()
        return True
    except Exception as e:
        print(f'diag_db_init 오류: {e}')
        return False


def diag_record(info):
    """진단 정보 1건 DB 기록. info=dict"""
    try:
        con = sqlite3.connect(DB_PATH, timeout=15)
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('''INSERT INTO fax_diag(
            call_uuid,direction,number,hangup_cause,fax_result,fax_success,
            t38_used,negotiate_count,transfer_rate,ecm_used,pages_tx,pages_total,
            bad_rows,capture_file,diag_category,diag_message,raw_vars,created)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            info.get('call_uuid',''), info.get('direction',''), info.get('number',''),
            info.get('hangup_cause',''), info.get('fax_result',''), int(info.get('fax_success',0) or 0),
            int(info.get('t38_used',0) or 0), int(info.get('negotiate_count',0) or 0),
            info.get('transfer_rate',''), int(info.get('ecm_used',0) or 0),
            int(info.get('pages_tx',0) or 0), int(info.get('pages_total',0) or 0),
            int(info.get('bad_rows',0) or 0), info.get('capture_file',''),
            info.get('diag_category',''), info.get('diag_message',''),
            json.dumps(info.get('raw_vars',{}), ensure_ascii=False)[:4000],
            datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        con.commit(); con.close()
        return True
    except Exception as e:
        print(f'diag_record 오류: {e}')
        return False


def diag_recent(limit=50):
    """최근 진단 기록 조회 (대시보드용)"""
    try:
        con = sqlite3.connect(DB_PATH, timeout=15)
        con.execute('PRAGMA journal_mode=WAL')
        cur = con.cursor()
        cur.execute('''SELECT direction,number,hangup_cause,fax_result,fax_success,
            t38_used,transfer_rate,pages_tx,pages_total,capture_file,
            diag_category,diag_message,created FROM fax_diag
            ORDER BY id DESC LIMIT ?''', (limit,))
        keys = ['direction','number','hangup_cause','fax_result','fax_success',
                't38_used','transfer_rate','pages_tx','pages_total','capture_file',
                'diag_category','diag_message','created']
        rows = [dict(zip(keys, r)) for r in cur.fetchall()]
        con.close()
        return rows
    except Exception:
        return []


def diag_stats():
    """실패 원인별 통계 (대시보드 차트용)"""
    try:
        con = sqlite3.connect(DB_PATH, timeout=15)
        con.execute('PRAGMA journal_mode=WAL')
        cur = con.cursor()
        cur.execute('''SELECT diag_category, COUNT(*) FROM fax_diag
            WHERE fax_success=0 GROUP BY diag_category ORDER BY COUNT(*) DESC''')
        by_cat = [{'category': r[0] or '미분류', 'count': r[1]} for r in cur.fetchall()]
        cur.execute('SELECT COUNT(*) FROM fax_diag')
        total = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM fax_diag WHERE fax_success=1')
        ok = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM fax_diag WHERE t38_used=1')
        t38 = cur.fetchone()[0]
        con.close()
        return {'by_category': by_cat, 'total': total, 'success': ok,
                'success_rate': round(100*ok/total,1) if total else 100.0,
                't38_calls': t38}
    except Exception:
        return {'by_category': [], 'total': 0, 'success': 0, 'success_rate': 100.0, 't38_calls': 0}


# ==================== 진단 카테고리 분류 ====================
_CATEGORY_RULES = [
    # (카테고리, [매칭 키워드], 사람이 읽는 메시지)
    ('T38_NEGOTIATION', ['t.38 negotiation failed', 't38 negotiat'],
     'T.38 협상 실패 - 양단 T.38 버전/타이밍/방화벽(UDPTL) 점검'),
    ('NO_CARRIER',      ['no carrier', 'training failed'],
     '캐리어 없음 - 회선 품질/루프 물리 결선'),
    ('ECM_FAIL',        ['ecm', 'error correction'],
     'ECM 오류정정 실패 - 회선 잡음/패킷 손실'),
    ('DISCONNECT_MID',  ['disconnected', 'lost during', 'timeout'],
     '전송 중 끊김 - 네트워크 지터/RTP 손실'),
    ('E1_LAYER',        ['temporary_failure', 'recovery_on_timer', 'out_of_order'],
     'E1 계층 문제 - 링크/클럭/D채널(Q.921)'),
    ('GATEWAY',         ['gateway_down', 'invalid_gateway', 'no_route'],
     '게이트웨이 문제 - 등록/라우팅/네트워크'),
    ('NO_ANSWER',       ['no_answer', 'no_user_response'],
     '응답 없음 - 수신측 다이얼플랜/rxfax'),
    ('BUSY',            ['user_busy'], '상대 통화중'),
    ('REJECTED',        ['call_rejected', 'incompatible'],
     '거절/코덱불일치 - 인증/권한/코덱 협상'),
]

def categorize(hangup_cause='', fax_result=''):
    """원인 문자열을 카테고리+메시지로 분류"""
    blob = f'{hangup_cause} {fax_result}'.lower()
    for cat, keys, msg in _CATEGORY_RULES:
        if any(k in blob for k in keys):
            return cat, msg
    if 'normal_clearing' in blob and fax_result:
        # 정상 종료인데 팩스 결과가 있으면 팩스 자체 문제
        return 'FAX_PARTIAL', f'콜은 정상, 팩스 미완료 - {fax_result[:40]}'
    return 'UNCLASSIFIED', f'미분류 (cause={hangup_cause}, result={fax_result})'


# ==================== FreeSWITCH 디버그 레벨 제어 ====================
def set_debug_level(level='info'):
    """
    FreeSWITCH 로그 레벨 동적 변경.
    level: console(0)~debug(7). 'debug','info','notice','warning' 등
    SIP 트레이스도 함께 제어.
    """
    valid = ['console','alert','crit','err','warning','notice','info','debug']
    if level not in valid:
        return False, f'invalid level: {level}'
    try:
        subprocess.run([FS_CLI, '-x', f'console loglevel {level}'],
                       capture_output=True, text=True, timeout=6)
        # 디버그 레벨이면 SIP 트레이스도 켬
        trace = 'on' if level == 'debug' else 'off'
        subprocess.run([FS_CLI, '-x', f'sofia global siptrace {trace}'],
                       capture_output=True, text=True, timeout=6)
        return True, f'로그레벨={level}, SIP트레이스={trace}'
    except Exception as e:
        return False, str(e)


def get_call_vars(uuid):
    """특정 콜의 채널 변수 조회 (팩스 결과 상세)"""
    vars = {}
    keys = ['fax_success','fax_result_code','fax_result_text',
            'fax_ecm_used','fax_v17_used','fax_t38','fax_document_transferred_pages',
            'fax_document_total_pages','fax_image_resolution','fax_transfer_rate',
            'fax_bad_rows','hangup_cause','sip_hangup_disposition']
    for k in keys:
        try:
            r = subprocess.run([FS_CLI, '-x', f'uuid_getvar {uuid} {k}'],
                               capture_output=True, text=True, timeout=5)
            v = (r.stdout or '').strip()
            if v and v != '_undef_':
                vars[k] = v
        except Exception:
            pass
    return vars


# ==================== 네트워크 캡처 (tcpdump) ====================
_active_captures = {}   # uuid -> Popen
_cap_lock = threading.Lock()

def tcpdump_available():
    try:
        r = subprocess.run(['which', 'tcpdump'], capture_output=True, text=True, timeout=3)
        return bool(r.stdout.strip())
    except Exception:
        return False


def start_capture(tag, peer_ip=None):
    """
    네트워크 캡처 시작. tag=식별자(uuid 등).
    SIP(5060) + RTP/T.38(UDP 대역) 캡처. peer_ip 있으면 그 IP만.
    반환: 캡처 파일 경로 (실패 시 None)
    """
    if not tcpdump_available():
        return None
    os.makedirs(CAP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_tag = re.sub(r'[^0-9A-Za-z_-]', '', str(tag))[:20]
    capfile = os.path.join(CAP_DIR, f'fax_{ts}_{safe_tag}.pcap')

    # 필터: SIP + UDP(RTP/UDPTL). peer_ip 지정 시 해당 호스트만
    bpf = 'udp and (port 5060 or portrange 16384-32768)'
    if peer_ip:
        bpf = f'host {peer_ip} and ' + bpf

    cmd = ['tcpdump', '-i', CAP_IFACE, '-w', capfile, '-s', '0', bpf]
    try:
        # 루트 권한 필요할 수 있음. 실패 시 None 반환.
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        with _cap_lock:
            _active_captures[tag] = proc
        return capfile
    except Exception:
        return None


def stop_capture(tag):
    """캡처 종료"""
    with _cap_lock:
        proc = _active_captures.pop(tag, None)
    if proc:
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
        return True
    return False


def cleanup_captures():
    """오래된 캡처 파일 정리 (최대 CAP_MAX개 유지)"""
    try:
        files = sorted(
            [os.path.join(CAP_DIR, f) for f in os.listdir(CAP_DIR) if f.endswith('.pcap')],
            key=os.path.getmtime, reverse=True)
        for old in files[CAP_MAX:]:
            os.remove(old)
        return len(files)
    except Exception:
        return 0


def capture_report(capfile):
    """
    캡처 파일 요약 리포트 (tcpdump로 재분석).
    패킷 수, SIP 메소드, T.38 여부 등 요약.
    """
    if not capfile or not os.path.isfile(capfile):
        return {'error': 'capture file not found'}
    report = {'file': os.path.basename(capfile), 'size': os.path.getsize(capfile)}
    try:
        # 패킷 수
        r = subprocess.run(['tcpdump', '-r', capfile, '-nn'],
                           capture_output=True, text=True, timeout=20)
        lines = r.stdout.strip().split('\n') if r.stdout.strip() else []
        report['packets'] = len(lines)
        # SIP 메소드 카운트
        sip_methods = {}
        for m in ['INVITE','ACK','BYE','CANCEL','200','486','488','503','480']:
            cnt = sum(1 for l in lines if m in l)
            if cnt:
                sip_methods[m] = cnt
        report['sip'] = sip_methods
        # T.38/UDPTL 흔적
        report['has_t38_ports'] = any('6' in l for l in lines[:5])  # 간이 표시
        report['first'] = lines[0][:80] if lines else ''
        report['last'] = lines[-1][:80] if lines else ''
    except subprocess.TimeoutExpired:
        report['error'] = 'analysis timeout'
    except Exception as e:
        report['error'] = str(e)
    return report


# ==================== 통합: 실패 콜 진단 처리 ====================
def process_call_result(uuid, direction, number, capfile=None):
    """
    콜 종료 후 호출. 변수 수집 → 분류 → (실패면 캡처 리포트) → DB 기록.
    반환: 진단 info dict
    """
    vars = get_call_vars(uuid)
    hangup = vars.get('hangup_cause', '')
    fax_result = vars.get('fax_result_text', '')
    success = 1 if vars.get('fax_success') == '1' else 0
    cat, msg = categorize(hangup, fax_result)

    info = {
        'call_uuid': uuid, 'direction': direction, 'number': number,
        'hangup_cause': hangup, 'fax_result': fax_result, 'fax_success': success,
        't38_used': 1 if vars.get('fax_t38') else 0,
        'transfer_rate': vars.get('fax_transfer_rate', ''),
        'ecm_used': 1 if vars.get('fax_ecm_used') == '1' else 0,
        'pages_tx': vars.get('fax_document_transferred_pages', 0),
        'pages_total': vars.get('fax_document_total_pages', 0),
        'bad_rows': vars.get('fax_bad_rows', 0),
        'capture_file': os.path.basename(capfile) if capfile else '',
        'diag_category': cat, 'diag_message': msg, 'raw_vars': vars,
    }
    diag_record(info)
    return info


if __name__ == '__main__':
    print('=== faxdiag 자체 점검 ===')
    print('DB 초기화:', diag_db_init())
    print('tcpdump 사용가능:', tcpdump_available())
    # 분류 테스트
    print('분류1:', categorize('NORMAL_TEMPORARY_FAILURE'))
    print('분류2:', categorize('NORMAL_CLEARING', 'The T.38 negotiation failed'))
    print('분류3:', categorize('NO_ANSWER'))
    print('분류4:', categorize('NORMAL_CLEARING', 'ECM failed on page 2'))
    # 가짜 진단 기록
    diag_record({'call_uuid':'test-123','direction':'send','number':'8888',
                 'hangup_cause':'NORMAL_TEMPORARY_FAILURE','fax_result':'',
                 'fax_success':0,'diag_category':'E1_LAYER','diag_message':'E1 계층 문제'})
    print('통계:', json.dumps(diag_stats(), ensure_ascii=False))

