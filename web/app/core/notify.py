"""
알림 코어 로직 - notifications 테이블 CRUD.
라우터(routers/notify.py)가 호출하는 실제 구현.

주의: 이 파일은 '코어 로직'이어야 함 (라우터 아님).
      과거 라우터 코드가 잘못 복사된 적 있어 재작성됨.
"""
from . import db


def create(title, body='', level='info', category=None,
           user_id=None, ref_type=None, ref_id=None):
    """알림 생성. user_id=None이면 전체(관리자) 대상.
    실패해도 예외를 올리지 않음 (알림은 부가기능, 본 기능 방해 금지)."""
    try:
        db.execute(
            'INSERT INTO notifications(user_id, level, title, body, category, '
            'ref_type, ref_id) VALUES(%s,%s,%s,%s,%s,%s,%s)',
            (user_id, level, title[:120], body, category, ref_type, ref_id))
    except Exception:
        # 알림 저장 실패가 본 기능(발송 등)을 막으면 안 됨
        pass


def list_for_user(user, limit=50, unread_only=False):
    """사용자의 알림 목록. user_id 매칭 + 전체대상(user_id NULL) 포함.
    admin은 전체 대상 알림도 봄."""
    limit = min(int(limit or 50), 200)
    uid = user.get('user_id') if isinstance(user, dict) else user
    role = user.get('role') if isinstance(user, dict) else None

    cond = []
    params = []
    if role == 'admin':
        # 관리자: 자기 것 + 전체대상(NULL)
        cond.append('(user_id = %s OR user_id IS NULL)')
        params.append(uid)
    else:
        cond.append('user_id = %s')
        params.append(uid)
    if unread_only:
        cond.append('is_read = FALSE')

    where = ' AND '.join(cond)
    params.append(limit)
    rows = db.query(
        f'SELECT id, level, title, body, category, ref_type, ref_id, '
        f'is_read, created_at FROM notifications WHERE {where} '
        f'ORDER BY created_at DESC LIMIT %s',
        tuple(params))
    return rows or []


def unread_count(user):
    """안 읽은 알림 수 (종 아이콘 뱃지용)."""
    uid = user.get('user_id') if isinstance(user, dict) else user
    role = user.get('role') if isinstance(user, dict) else None
    if role == 'admin':
        row = db.query_one(
            'SELECT COUNT(*) AS n FROM notifications '
            'WHERE (user_id = %s OR user_id IS NULL) AND is_read = FALSE', (uid,))
    else:
        row = db.query_one(
            'SELECT COUNT(*) AS n FROM notifications '
            'WHERE user_id = %s AND is_read = FALSE', (uid,))
    return row['n'] if row else 0


def mark_read(notif_id, user):
    """알림 하나 읽음 처리 (본인 것만)."""
    uid = user.get('user_id') if isinstance(user, dict) else user
    role = user.get('role') if isinstance(user, dict) else None
    if role == 'admin':
        db.execute('UPDATE notifications SET is_read=TRUE WHERE id=%s '
                   'AND (user_id=%s OR user_id IS NULL)', (notif_id, uid))
    else:
        db.execute('UPDATE notifications SET is_read=TRUE WHERE id=%s AND user_id=%s',
                   (notif_id, uid))


def mark_all_read(user):
    """내 알림 전부 읽음 처리."""
    uid = user.get('user_id') if isinstance(user, dict) else user
    role = user.get('role') if isinstance(user, dict) else None
    if role == 'admin':
        db.execute('UPDATE notifications SET is_read=TRUE '
                   'WHERE (user_id=%s OR user_id IS NULL) AND is_read=FALSE', (uid,))
    else:
        db.execute('UPDATE notifications SET is_read=TRUE '
                   'WHERE user_id=%s AND is_read=FALSE', (uid,))
