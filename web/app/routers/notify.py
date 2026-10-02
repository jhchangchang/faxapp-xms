"""
알림 라우터 - 알림 조회/읽음 처리 + 웹훅 설정.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from ..core import db, notify, errors
from ..deps import get_current_user, require_admin

router = APIRouter(prefix='/api/notify', tags=['notifications'])


@router.get('/list')
def list_notifications(unread_only: bool = False, limit: int = 50, user=Depends(get_current_user)):
    """내 알림 목록."""
    return notify.list_for_user(user, limit=limit, unread_only=unread_only)


@router.get('/unread-count')
def unread(user=Depends(get_current_user)):
    """안 읽은 알림 수 (종 아이콘 뱃지용)."""
    return {'unread': notify.unread_count(user)}


@router.post('/{notif_id}/read')
def read_one(notif_id: int, user=Depends(get_current_user)):
    notify.mark_read(notif_id, user)
    return {'ok': True}


@router.post('/read-all')
def read_all(user=Depends(get_current_user)):
    notify.mark_all_read(user)
    return {'ok': True}


class WebhookConfig(BaseModel):
    webhook_url: Optional[str] = None
    notify_on_fail: bool = True
    notify_on_recv: bool = False
    # 이메일 알림 설정
    email_enabled: bool = False
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_pass: Optional[str] = None
    smtp_tls: bool = True
    email_from: Optional[str] = None
    email_to: Optional[str] = None


@router.get('/config')
def get_config(user=Depends(require_admin)):
    """알림 설정 조회 (관리자) — 웹훅 + 이메일. 비밀번호는 마스킹."""
    cfg = db.query_one(
        'SELECT webhook_url, notify_on_fail, notify_on_recv, '
        'email_enabled, smtp_host, smtp_port, smtp_user, smtp_tls, email_from, email_to '
        'FROM notify_config WHERE id=1')
    if not cfg:
        return {'webhook_url': None, 'notify_on_fail': True, 'notify_on_recv': False,
                'email_enabled': False}
    cfg['smtp_pass'] = ''   # 비밀번호는 내려주지 않음 (보안)
    return cfg


@router.post('/config')
def set_config(body: WebhookConfig, user=Depends(require_admin)):
    """알림 설정 저장 (관리자). smtp_pass가 비어있으면 기존 비밀번호 유지."""
    if body.smtp_pass:
        db.execute(
            'UPDATE notify_config SET webhook_url=%s, notify_on_fail=%s, notify_on_recv=%s, '
            'email_enabled=%s, smtp_host=%s, smtp_port=%s, smtp_user=%s, smtp_pass=%s, '
            'smtp_tls=%s, email_from=%s, email_to=%s WHERE id=1',
            (body.webhook_url, body.notify_on_fail, body.notify_on_recv,
             body.email_enabled, body.smtp_host, body.smtp_port, body.smtp_user,
             body.smtp_pass, body.smtp_tls, body.email_from, body.email_to))
    else:
        # 비밀번호 제외하고 저장 (기존 유지)
        db.execute(
            'UPDATE notify_config SET webhook_url=%s, notify_on_fail=%s, notify_on_recv=%s, '
            'email_enabled=%s, smtp_host=%s, smtp_port=%s, smtp_user=%s, '
            'smtp_tls=%s, email_from=%s, email_to=%s WHERE id=1',
            (body.webhook_url, body.notify_on_fail, body.notify_on_recv,
             body.email_enabled, body.smtp_host, body.smtp_port, body.smtp_user,
             body.smtp_tls, body.email_from, body.email_to))
    return {'ok': True}


@router.post('/test-email')
def test_email_notify(user=Depends(require_admin)):
    """이메일 설정 테스트 발송 (관리자)."""
    from ..core import email_notify
    ok = email_notify.test_email()
    if ok:
        return {'ok': True, 'message': '테스트 이메일을 발송했습니다. 수신함을 확인하세요.'}
    return {'ok': False, 'message': '이메일 발송 실패 — 설정(SMTP 정보)을 확인하세요.'}


@router.post('/test')
def test_notification(user=Depends(require_admin)):
    """테스트 알림 발송 (웹훅 설정 확인용)."""
    notify.create('테스트 알림', '알림 설정이 정상 동작합니다.',
                  level='info', category='system', user_id=user['user_id'])
    return {'ok': True, 'message': '테스트 알림을 보냈습니다'}
