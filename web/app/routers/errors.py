"""
오류 로그 라우터 - 관리자가 시스템 실패를 조회·분석.
중앙 예외 모듈이 기록한 error_log를 보여줌 → 가시성의 접점.
"""
from fastapi import APIRouter, Depends, Query

from ..core import db
from ..deps import require_admin

router = APIRouter(prefix='/api/errors', tags=['errors'])


@router.get('')
def list_errors(
    category: str = None,
    code: str = None,
    resolved: bool = None,
    limit: int = Query(default=100, le=500),
    user=Depends(require_admin)):
    """오류 로그 조회 (필터: 성격·코드·처리여부)."""
    cond, params = [], []
    if category:
        cond.append('category=%s'); params.append(category)
    if code:
        cond.append('code=%s'); params.append(code)
    if resolved is not None:
        cond.append('resolved=%s'); params.append(resolved)
    sql = ('SELECT e.id, e.code, e.category, e.detail, e.context, e.path, '
           'e.user_id, u.username, e.job_id, e.resolved, e.created_at '
           'FROM error_log e LEFT JOIN users u ON e.user_id=u.id')
    if cond:
        sql += ' WHERE ' + ' AND '.join(cond)
    sql += ' ORDER BY e.id DESC LIMIT %s'
    params.append(limit)
    return db.query(sql, tuple(params))


@router.get('/summary')
def error_summary(days: int = Query(default=7, le=90), user=Depends(require_admin)):
    """오류 요약 - 성격별·코드별 건수, 최근 추이."""
    by_category = db.query(
        "SELECT category, COUNT(*) AS cnt FROM error_log "
        "WHERE created_at >= CURRENT_DATE - %s::int "
        "GROUP BY category ORDER BY cnt DESC", (days,))
    by_code = db.query(
        "SELECT code, category, COUNT(*) AS cnt FROM error_log "
        "WHERE created_at >= CURRENT_DATE - %s::int "
        "GROUP BY code, category ORDER BY cnt DESC LIMIT 15", (days,))
    unresolved = db.query_one(
        "SELECT COUNT(*) AS cnt FROM error_log WHERE resolved=false")
    total = db.query_one(
        "SELECT COUNT(*) AS cnt FROM error_log "
        "WHERE created_at >= CURRENT_DATE - %s::int", (days,))
    return {
        'total': total['cnt'] if total else 0,
        'unresolved': unresolved['cnt'] if unresolved else 0,
        'by_category': by_category,
        'by_code': by_code,
    }


@router.post('/{error_id}/resolve')
def resolve_error(error_id: int, user=Depends(require_admin)):
    """오류를 처리됨으로 표시."""
    db.execute('UPDATE error_log SET resolved=true WHERE id=%s', (error_id,))
    return {'ok': True}


@router.post('/resolve-all')
def resolve_all(category: str = None, user=Depends(require_admin)):
    """오류 일괄 처리 표시 (선택: 특정 성격만)."""
    if category:
        db.execute('UPDATE error_log SET resolved=true WHERE category=%s', (category,))
    else:
        db.execute('UPDATE error_log SET resolved=true WHERE resolved=false')
    return {'ok': True}


@router.delete('/cleanup')
def cleanup_errors(days: int = 30, user=Depends(require_admin)):
    """오래된 오류 로그 정리."""
    db.execute("DELETE FROM error_log WHERE created_at < CURRENT_DATE - %s::int "
               "AND resolved=true", (days,))
    return {'ok': True, 'message': f'{days}일 이전 처리된 오류 정리됨'}


@router.delete('/clear-all')
def clear_all_errors(confirm: bool = Query(default=False),
                     user=Depends(require_admin)):
    """오류/알람 로그 전체 초기화 (모두 삭제).
    실수 방지를 위해 confirm=true 필수. 관리자만.
    """
    if not confirm:
        return {'ok': False, 'message': 'confirm=true 파라미터가 필요합니다 (전체 삭제 확인)'}
    # 삭제 전 건수 파악
    cnt = db.query_one("SELECT COUNT(*) AS c FROM error_log")
    total = (cnt or {}).get('c', 0)
    db.execute("DELETE FROM error_log")
    return {'ok': True, 'deleted': total, 'message': f'오류/알람 로그 {total}건 전체 삭제됨'}
