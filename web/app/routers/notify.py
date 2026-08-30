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


@router.get('/config')
def get_config(user=Depends(require_admin)):
    """웹훅 설정 조회 (관리자)."""
    cfg = db.query_one('SELECT webhook_url, notify_on_fail, notify_on_recv FROM notify_config WHERE id=1')
    return cfg or {'webhook_url': None, 'notify_on_fail': True, 'notify_on_recv': False}


@router.post('/config')
def set_config(body: WebhookConfig, user=Depends(require_admin)):
    """웹훅 설정 저장 (관리자)."""
    db.execute(
        'UPDATE notify_config SET webhook_url=%s, notify_on_fail=%s, notify_on_recv=%s WHERE id=1',
        (body.webhook_url, body.notify_on_fail, body.notify_on_recv))
    return {'ok': True}


@router.post('/test')
def test_notification(user=Depends(require_admin)):
    """테스트 알림 발송 (웹훅 설정 확인용)."""
    notify.create('테스트 알림', '알림 설정이 정상 동작합니다.',
                  level='info', category='system', user_id=user['user_id'])
    return {'ok': True, 'message': '테스트 알림을 보냈습니다'}
