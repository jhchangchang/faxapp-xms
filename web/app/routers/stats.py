"""
통계·관리 라우터 - 송수신 통계, 부서별/사용자별 집계, 기간별 리포트, 감사 로그.
관리자·매니저 권한.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from ..core import db
from ..deps import get_current_user, require_admin, require_manager

router = APIRouter(prefix='/api/stats', tags=['stats'])


@router.get('/summary')
def summary(user=Depends(get_current_user)):
    """대시보드 요약 - 오늘/전체 송수신 건수, 성공률."""
    # 발송 통계
    sent = db.query_one(
        "SELECT COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE status='sent') AS sent_ok, "
        "COUNT(*) FILTER (WHERE status='failed') AS sent_fail, "
        "COUNT(*) FILTER (WHERE status IN ('pending','queued','sending')) AS in_progress "
        "FROM fax_jobs")
    # 수신 통계
    recv = db.query_one(
        "SELECT COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE is_read=false) AS unread "
        "FROM fax_received")
    # 오늘 발송
    today = db.query_one(
        "SELECT COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE status='sent') AS ok "
        "FROM fax_jobs WHERE created_at >= CURRENT_DATE")
    sent = sent or {}; recv = recv or {}; today = today or {}
    sent_total = sent.get('total', 0) or 0
    sent_ok = sent.get('sent_ok', 0) or 0
    success_rate = round(100 * sent_ok / sent_total, 1) if sent_total else 100.0
    return {
        'sent': {'total': sent_total, 'ok': sent_ok,
                 'fail': sent.get('sent_fail', 0) or 0,
                 'in_progress': sent.get('in_progress', 0) or 0},
        'received': {'total': recv.get('total', 0) or 0,
                     'unread': recv.get('unread', 0) or 0},
        'today': {'sent': today.get('total', 0) or 0, 'ok': today.get('ok', 0) or 0},
        'success_rate': success_rate,
    }


@router.get('/daily')
def daily_stats(days: int = Query(default=14, le=90), user=Depends(get_current_user)):
    """최근 N일 일별 송수신 추이 (차트용)."""
    sent = db.query(
        "SELECT DATE(created_at) AS day, COUNT(*) AS cnt, "
        "COUNT(*) FILTER (WHERE status='sent') AS ok "
        "FROM fax_jobs WHERE created_at >= CURRENT_DATE - %s::int "
        "GROUP BY DATE(created_at) ORDER BY day", (days,))
    recv = db.query(
        "SELECT DATE(received_at) AS day, COUNT(*) AS cnt "
        "FROM fax_received WHERE received_at >= CURRENT_DATE - %s::int "
        "GROUP BY DATE(received_at) ORDER BY day", (days,))
    return {'sent': sent, 'received': recv}


@router.get('/by-department')
def by_department(user=Depends(require_manager)):
    """부서별 송수신 집계."""
    sent = db.query(
        "SELECT d.id, d.name, COUNT(j.id) AS sent_count, "
        "COUNT(j.id) FILTER (WHERE j.status='sent') AS sent_ok "
        "FROM departments d LEFT JOIN fax_jobs j ON j.dept_id=d.id "
        "GROUP BY d.id, d.name ORDER BY d.name")
    recv = db.query(
        "SELECT d.id, d.name, COUNT(r.id) AS recv_count "
        "FROM departments d LEFT JOIN fax_received r ON r.dept_id=d.id "
        "GROUP BY d.id, d.name ORDER BY d.name")
    # 병합
    recv_map = {r['id']: r['recv_count'] for r in recv}
    for s in sent:
        s['recv_count'] = recv_map.get(s['id'], 0)
    return sent


@router.get('/by-user')
def by_user(limit: int = 20, user=Depends(require_manager)):
    """사용자별 발송 집계 (상위 N)."""
    return db.query(
        "SELECT u.id, u.username, u.display_name, "
        "COUNT(j.id) AS sent_count, "
        "COUNT(j.id) FILTER (WHERE j.status='sent') AS sent_ok "
        "FROM users u LEFT JOIN fax_jobs j ON j.user_id=u.id "
        "GROUP BY u.id, u.username, u.display_name "
        "ORDER BY sent_count DESC LIMIT %s", (min(limit, 100),))


@router.get('/history')
def history(
    direction: str = Query(default='all', pattern='^(all|send|receive)$'),
    status: Optional[str] = None,
    number: Optional[str] = None,
    limit: int = 100,
    user=Depends(get_current_user),
):
    """통합 송수신 이력 검색 (필터: 방향/상태/번호)."""
    limit = min(limit, 500)
    results = []
    if direction in ('all', 'send'):
        cond, params = ['user_id=%s'] if user['role'] not in ('admin', 'manager') else [], []
        if user['role'] not in ('admin', 'manager'):
            params.append(user['user_id'])
        if status:
            cond.append('status=%s'); params.append(status)
        if number:
            cond.append('to_number LIKE %s'); params.append(f'%{number}%')
        sql = ("SELECT id, 'send' AS direction, to_number AS number, status, "
               "pages, created_at AS ts FROM fax_jobs")
        if cond:
            sql += ' WHERE ' + ' AND '.join(cond)
        sql += ' ORDER BY id DESC LIMIT %s'
        params.append(limit)
        results += db.query(sql, tuple(params))
    if direction in ('all', 'receive'):
        cond, params = [], []
        if number:
            cond.append('from_number LIKE %s'); params.append(f'%{number}%')
        sql = ("SELECT id, 'receive' AS direction, from_number AS number, "
               "CASE WHEN is_read THEN 'read' ELSE 'unread' END AS status, "
               "pages, received_at AS ts FROM fax_received")
        if cond:
            sql += ' WHERE ' + ' AND '.join(cond)
        sql += ' ORDER BY id DESC LIMIT %s'
        params.append(limit)
        results += db.query(sql, tuple(params))
    # 시간순 정렬
    results.sort(key=lambda x: str(x.get('ts', '')), reverse=True)
    return results[:limit]


@router.get('/audit')
def audit_log(limit: int = 100, user=Depends(require_admin)):
    """감사 로그 조회 (관리자)."""
    return db.query(
        "SELECT a.id, a.user_id, u.username, a.action, a.detail, a.ip, a.created_at "
        "FROM audit_log a LEFT JOIN users u ON a.user_id=u.id "
        "ORDER BY a.id DESC LIMIT %s", (min(limit, 500),))
