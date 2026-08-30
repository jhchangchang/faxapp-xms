"""
진단 라우터 - 문제가 어디서 발생하는지 계층별로 진단.
우리 서버(앱/DB/팩스서버/게이트웨이) vs 상대방(리모트) vs 회선 구분.
"""
import time
from fastapi import APIRouter, Depends

from ..core import db, faxclient, errors
from ..deps import get_current_user

router = APIRouter(prefix='/api/diag', tags=['diagnostics'])


def _check(fn):
    """단일 점검 실행 + 소요시간 측정."""
    t0 = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, str(e)[:80]
    ms = round((time.perf_counter() - t0) * 1000)
    return {'ok': ok, 'detail': detail, 'ms': ms}


@router.get('/health')
def diag_health(user=Depends(get_current_user)):
    """우리 서버 계층별 상태 점검 (앱→DB→팩스서버→게이트웨이).
    각 계층이 정상인지, 어디서 끊겼는지 즉시 확인."""
    layers = []

    # 1. 앱 (여기까지 응답이 왔으면 앱은 정상)
    layers.append({'layer': '앱 서버', 'key': 'app', 'scope': 'local',
                   'ok': True, 'detail': '정상 응답', 'ms': 0})

    # 2. 데이터베이스
    def _db():
        r = db.query_one('SELECT 1 AS ok')
        return (bool(r and r.get('ok') == 1), '연결됨' if r else '응답 없음')
    r = _check(_db)
    layers.append({'layer': '데이터베이스', 'key': 'db', 'scope': 'local', **r,
                   'detail': r['detail'] if r['ok'] else 'DB 연결 실패'})

    # 3. 팩스 서버
    def _fs():
        h = faxclient.health()
        return (bool(h and h.get('ok', False)), '정상' if h and h.get('ok') else '응답 없음')
    r = _check(_fs)
    layers.append({'layer': '팩스 서버', 'key': 'faxserver', 'scope': 'local', **r,
                   'detail': r['detail'] if r['ok'] else '팩스 서버(:8090) 응답 없음'})

    # 4. 게이트웨이 (팩스 서버 상태에 포함될 수 있음)
    def _gw():
        h = faxclient.health()
        if not h:
            return (False, '팩스 서버 경유 확인 불가')
        gw = h.get('gateway') or h.get('gw')
        if gw is None:
            return (None, '게이트웨이 상태 미제공')
        # 팩스 서버가 준 상세 상태 문자열 활용
        name = h.get('gateway_name', '')
        state = h.get('gateway_state', '')
        if gw:
            return (True, f'{name} 등록됨' if name else '등록됨')
        return (False, f'{name} {state}'.strip() or '미등록')
    r = _check(_gw)
    # 게이트웨이는 None(미확인) 가능
    gw_ok = r['ok']
    layers.append({'layer': '게이트웨이', 'key': 'gateway', 'scope': 'local',
                   'ok': gw_ok, 'detail': r['detail'], 'ms': r['ms']})

    # 종합 판정: 첫 실패 계층
    first_fail = next((l for l in layers if l['ok'] is False), None)
    overall = 'ok' if first_fail is None else 'degraded'
    return {
        'overall': overall,
        'layers': layers,
        'first_fail': first_fail['key'] if first_fail else None,
        'summary': (f"{first_fail['layer']}에서 문제 감지" if first_fail
                    else '모든 계층 정상'),
    }


@router.get('/fault-analysis')
def fault_analysis(days: int = 7, user=Depends(get_current_user)):
    """최근 실패의 책임 소재 분석 - 우리 서버/상대방/회선 중 어디가 문제인가.
    발송 실패의 error_code를 fault로 분류해 집계."""
    # error_log에서 최근 실패 코드별 집계
    rows = db.query(
        "SELECT code, COUNT(*) AS cnt FROM error_log "
        "WHERE created_at >= now() - (%s || ' days')::interval "
        "GROUP BY code ORDER BY cnt DESC", (str(days),))

    # 각 코드를 fault로 분류
    fault_buckets = {'local': 0, 'remote': 0, 'link': 0, 'user': 0, 'unknown': 0}
    by_code = []
    for row in rows:
        code = row['code']
        cnt = row['cnt']
        # 코드로 예외 클래스 찾아 fault 판정
        fault = _fault_of_code(code)
        fault_buckets[fault] = fault_buckets.get(fault, 0) + cnt
        by_code.append({'code': code, 'count': cnt, 'fault': fault})

    total = sum(fault_buckets.values())
    # 가장 큰 책임 소재
    dominant = max(fault_buckets, key=fault_buckets.get) if total else None
    fault_kr = {'local': '우리 서버', 'remote': '상대방', 'link': '회선',
                'user': '사용자 입력', 'unknown': '미상'}

    return {
        'days': days,
        'total_failures': total,
        'by_fault': [
            {'fault': k, 'label': fault_kr[k], 'count': v,
             'pct': round(100 * v / total, 1) if total else 0}
            for k, v in sorted(fault_buckets.items(), key=lambda x: -x[1]) if v > 0
        ],
        'by_code': by_code[:15],
        'dominant': dominant,
        'dominant_label': fault_kr.get(dominant) if dominant else None,
        'summary': (f"최근 {days}일 실패의 주 원인은 '{fault_kr.get(dominant)}' 쪽입니다"
                    if dominant and total else f"최근 {days}일 실패 없음"),
    }


# 코드 → fault 캐시 (예외 클래스에서 추출)
_CODE_FAULT = {}


def _fault_of_code(code):
    """error_code로 책임 소재 판정."""
    if not _CODE_FAULT:
        # errors 모듈의 모든 예외 클래스에서 code→fault 맵 구축
        for name in dir(errors):
            obj = getattr(errors, name)
            if isinstance(obj, type) and issubclass(obj, errors.FaxAppError):
                try:
                    _CODE_FAULT[obj.code] = obj.fault
                except Exception:
                    pass
    return _CODE_FAULT.get(code, 'unknown')


@router.get('/fault-trend')
def fault_trend(days: int = 14, user=Depends(get_current_user)):
    """일별 실패 추이 - 날짜별로 fault(책임소재)별 실패 건수.
    '어느 날 어떤 문제가 늘었나'를 시간 흐름으로 파악 (일일 운영 모니터링).
    """
    # days 범위 제한 (1~90)
    try:
        days = max(1, min(int(days), 90))
    except (TypeError, ValueError):
        days = 14

    # error_log에서 날짜별·코드별 집계
    rows = db.query(
        "SELECT created_at::date AS day, code, COUNT(*) AS cnt FROM error_log "
        "WHERE created_at >= CURRENT_DATE - (%s || ' days')::interval "
        "GROUP BY created_at::date, code ORDER BY day", (str(days),))
    rows = rows or []

    # 날짜별로 fault 집계
    by_day = {}
    for r in rows:
        day = str(r['day'])
        fault = _fault_of_code(r['code'])
        cnt = r['cnt']
        if day not in by_day:
            by_day[day] = {'day': day, 'total': 0,
                           'local': 0, 'remote': 0, 'link': 0,
                           'user': 0, 'unknown': 0}
        by_day[day][fault] = by_day[day].get(fault, 0) + cnt
        by_day[day]['total'] += cnt

    # 데이터 없는 날도 0으로 채워 연속된 추이 만들기
    from datetime import date, timedelta
    today = date.today()
    trend = []
    for i in range(days - 1, -1, -1):
        d = str(today - timedelta(days=i))
        trend.append(by_day.get(d, {'day': d, 'total': 0,
                     'local': 0, 'remote': 0, 'link': 0,
                     'user': 0, 'unknown': 0}))

    # 기간 합계 (요약용)
    totals = {'local': 0, 'remote': 0, 'link': 0, 'user': 0, 'unknown': 0}
    for t in trend:
        for k in totals:
            totals[k] += t.get(k, 0)
    grand = sum(totals.values())

    fault_kr = {'local': '우리 서버', 'remote': '상대방', 'link': '회선',
                'user': '사용자 입력', 'unknown': '미상'}
    return {
        'days': days,
        'trend': trend,           # 날짜별 fault 건수 (그래프용)
        'totals': totals,         # 기간 합계
        'total_failures': grand,
        'fault_labels': fault_kr,
    }


@router.get('/line')
def line_diagnostics(user=Depends(get_current_user)):
    """회선·게이트웨이 종합 진단 - 하드웨어 입고 당일 물리 연결 점검용.
    E1 링크 → 게이트웨이 등록 → T.38 설정을 순서대로 확인하고
    문제 지점과 조치 방법을 함께 제공."""
    return faxclient.line_diag()
