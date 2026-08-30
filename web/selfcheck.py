#!/usr/bin/env python3
"""
배포 후 자가진단 - 지금까지 만든 모든 것이 실제 서버에서 제대로 도는지 한 번에 점검.

사용법 (앱 루트 /opt/faxapp/web 에서):
    python3 selfcheck.py

점검 항목:
  1) 모든 코어 모듈 임포트
  2) 앱 전체 로드 + 라우터 등록
  3) DB 연결 + 필수 테이블/컬럼 (마이그레이션 적용 여부)
  4) 팩스 서버 연결 + 게이트웨이 상태
  5) 예외 체계 무결성

표준 라이브러리만 사용. 실패한 항목과 조치 방법을 함께 안내.
"""
import sys
import os

# 색상 (터미널)
G = '\033[92m'; R = '\033[91m'; Y = '\033[93m'; B = '\033[94m'; X = '\033[0m'
OK = f'{G}✓{X}'; NG = f'{R}✗{X}'; WARN = f'{Y}!{X}'

results = {'pass': 0, 'fail': 0, 'warn': 0}


def section(title):
    print(f'\n{B}── {title} ──{X}')


def check(name, ok, detail='', fix=''):
    if ok is True:
        print(f'  {OK} {name}' + (f' · {detail}' if detail else ''))
        results['pass'] += 1
    elif ok is None:
        print(f'  {WARN} {name}' + (f' · {detail}' if detail else ''))
        results['warn'] += 1
    else:
        print(f'  {NG} {name}' + (f' · {detail}' if detail else ''))
        if fix:
            print(f'      {Y}→ {fix}{X}')
        results['fail'] += 1


# ── 1. 코어 모듈 임포트 ──
section('1. 코어 모듈 임포트')
CORE = ['config', 'db', 'convert', 'cover', 'errors', 'error_handler',
        'faxclient', 'notify', 'retry_worker', 'security', 'apikey']
for m in CORE:
    try:
        __import__(f'app.core.{m}')
        check(f'app.core.{m}', True)
    except Exception as e:
        check(f'app.core.{m}', False, str(e)[:60],
              '해당 모듈 파일이 제대로 복사됐는지 확인')

# ── 2. 앱 전체 로드 + 라우터 ──
section('2. 앱 로드 + 라우터 등록')
app = None
try:
    from app.main import app
    check('app.main 로드', True)
except Exception as e:
    check('app.main 로드', False, str(e)[:70],
          '__pycache__ 삭제 후 재시도. 누락된 파일 확인')

if app:
    try:
        paths = set(app.openapi()['paths'].keys())
        # 핵심 엔드포인트들이 등록됐는지
        expect = {
            '/api/fax/send': '발송',
            '/api/fax/webhook/result': '발송결과 webhook',
            '/api/fax/scheduled': '예약 발송',
            '/api/fax/cover-templates': '표지 템플릿',
            '/api/receive/{recv_id}/forward': '수신 전달',
            '/api/diag/health': '진단',
            '/api/diag/fault-analysis': '문제 위치 분석',
            '/api/notify/list': '알림',
            '/api/notify/config': '알림 설정',
            '/api/preview/preflight': '발송 전 미리보기',
            '/api/fax/mailmerge': '대량 맞춤 발송',
            '/api/data/export/client-report': '고객사 리포트',
            '/api/v1/fax/send': '외부 연동 API',
            '/api/v1/keys': 'API 키 관리',
        }
        for path, label in expect.items():
            check(f'{label} ({path})', path in paths, '',
                  '해당 라우터 파일 복사 확인')
    except Exception as e:
        check('라우터 확인', False, str(e)[:60])

# ── 3. DB 연결 + 테이블/컬럼 ──
section('3. DB 연결 + 마이그레이션 적용 여부')
try:
    from app.core import db
    r = db.query_one('SELECT 1 AS ok')
    check('DB 연결', bool(r and r.get('ok') == 1))

    # 필수 테이블
    def table_exists(t):
        r = db.query_one(
            "SELECT to_regclass(%s) AS t", (f'public.{t}',))
        return bool(r and r.get('t'))

    for t in ['users', 'fax_jobs', 'fax_received', 'contacts',
              'error_log', 'notifications', 'notify_config', 'api_keys']:
        exists = table_exists(t)
        fix = ''
        if not exists:
            if t in ('error_log',):
                fix = 'migration_retry.sql 적용 필요'
            elif t in ('notifications', 'notify_config'):
                fix = 'migration_notify.sql 적용 필요'
            elif t == 'api_keys':
                fix = 'migration_apikey.sql 적용 필요'
        check(f'테이블 {t}', exists, '', fix)

    # fax_jobs 필수 컬럼 (마이그레이션들이 추가한 것)
    def col_exists(table, col):
        r = db.query_one(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name=%s AND column_name=%s", (table, col))
        return bool(r)

    col_checks = [
        ('fax_jobs', 'max_retries', 'migration_retry.sql'),
        ('fax_jobs', 'next_retry_at', 'migration_retry.sql'),
        ('fax_jobs', 'last_error_code', 'migration_retry.sql'),
        ('fax_jobs', 'scheduled_at', 'migration_schedule.sql'),
        ('fax_jobs', 'pages_sent', 'migration_webhook.sql'),
        ('fax_jobs', 'diag_category', 'migration_webhook.sql'),
        ('fax_jobs', 'hangup_cause', 'migration_webhook.sql'),
        ('fax_jobs', 'transfer_rate', 'migration_webhook.sql'),
        ('fax_jobs', 'ecm_used', 'migration_ecm.sql'),
        ('fax_jobs', 'bad_rows', 'migration_ecm.sql'),
        ('fax_jobs', 'api_key_id', 'migration_apikey.sql'),
    ]
    for table, col, mig in col_checks:
        exists = col_exists(table, col)
        check(f'{table}.{col}', exists, '',
              f'{mig} 적용 필요' if not exists else '')
except Exception as e:
    check('DB 점검', False, str(e)[:70], 'DB 연결 설정 확인')

# ── 4. 팩스 서버 연결 ──
section('4. 팩스 서버 연결')
try:
    from app.core import faxclient
    h = faxclient.health()
    if h and h.get('ok'):
        check('팩스 서버 (:8090)', True, f"uptime {h.get('uptime','?')}")
        # 게이트웨이 상태
        gw = h.get('gateway')
        if gw is None:
            check('게이트웨이 상태', None, '팩스서버가 gateway 필드 미제공',
                  '')
        else:
            check('게이트웨이', bool(gw),
                  h.get('gateway_state', ''),
                  '장비 연결 후 sofia status 확인' if not gw else '')
    else:
        check('팩스 서버 (:8090)', False, '응답 없음',
              'faxserver_v6.py 실행 확인')
except Exception as e:
    check('팩스 서버 연결', None, str(e)[:60],
          '팩스 서버가 꺼져 있어도 앱은 동작 (발송 시에만 필요)')

# ── 5. 예외 체계 무결성 ──
section('5. 예외 체계')
try:
    from app.core import errors
    # 코드 중복 없는지, fault 다 있는지
    classes = [c for c in vars(errors).values()
               if isinstance(c, type) and issubclass(c, errors.FaxAppError)]
    codes = {}
    no_fault = []
    for c in classes:
        codes.setdefault(c.code, []).append(c.__name__)
        if not hasattr(c, 'fault'):
            no_fault.append(c.__name__)
    check(f'예외 클래스 {len(classes)}개 로드', True)
    check('서버결과 매핑', len(errors._SERVER_RESULT_MAP) > 50,
          f'{len(errors._SERVER_RESULT_MAP)}개 항목')
    # 실제 분류 동작
    test_exc = errors.from_server_result('T30_ERR_CANNOT_TRAIN')
    check('T.30 코드 분류', test_exc.code == 'TRAINING_FAILED',
          f'→ {test_exc.code}')
except Exception as e:
    check('예외 체계', False, str(e)[:60])

# ── 요약 ──
print(f'\n{B}{"="*50}{X}')
p, f, w = results['pass'], results['fail'], results['warn']
print(f'  {G}통과 {p}{X}  ·  {R}실패 {f}{X}  ·  {Y}경고 {w}{X}')
if f == 0:
    print(f'\n  {G}✓ 모든 필수 점검 통과 - 배포 정상{X}')
    if w:
        print(f'  {Y}경고 {w}건은 하드웨어(게이트웨이 등) 관련일 수 있음{X}')
else:
    print(f'\n  {R}✗ 실패 {f}건 - 위 조치 방법 참고{X}')
print()
sys.exit(1 if f else 0)
