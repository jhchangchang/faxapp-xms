"""번호 차단 관리 - 스팸/특정 번호 발송·수신 차단."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from ..core import db
from ..deps import require_admin, get_current_user

router = APIRouter(prefix='/api/blocklist', tags=['blocklist'])


class BlockBody(BaseModel):
    number: str
    direction: str = 'both'   # send / recv / both
    reason: Optional[str] = None


def is_blocked(number, direction):
    """해당 번호가 차단됐는지. direction: 'send' 또는 'recv'."""
    try:
        row = db.query_one(
            "SELECT id FROM block_list WHERE number=%s "
            "AND direction IN (%s, 'both') LIMIT 1", (number, direction))
        return row is not None
    except Exception:
        return False   # 테이블 없거나 오류 시 차단 안 함 (발송 막지 않음)


@router.get('')
def list_blocks(limit: int = 100, offset: int = 0, user=Depends(require_admin)):
    """차단 목록 (페이지네이션)."""
    limit = min(max(int(limit), 1), 200); offset = max(int(offset), 0)
    total_row = db.query_one("SELECT COUNT(*) AS c FROM block_list")
    total = total_row['c'] if total_row else 0
    items = db.query(
        "SELECT b.id, b.number, b.direction, b.reason, u.username AS created_by_name, "
        "b.created_at FROM block_list b LEFT JOIN users u ON b.created_by=u.id "
        "ORDER BY b.id DESC LIMIT %s OFFSET %s", (limit, offset))
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


@router.post('')
def add_block(body: BlockBody, user=Depends(require_admin)):
    """번호 차단 추가."""
    num = (body.number or '').strip()
    if not num:
        return {'ok': False, 'message': '번호를 입력하세요'}
    d = body.direction if body.direction in ('send', 'recv', 'both') else 'both'
    try:
        db.execute(
            "INSERT INTO block_list(number, direction, reason, created_by) "
            "VALUES(%s,%s,%s,%s) ON CONFLICT (number, direction) DO UPDATE SET reason=EXCLUDED.reason",
            (num, d, body.reason, user['user_id']))
        return {'ok': True, 'message': f'{num} 차단 추가됨'}
    except Exception as e:
        return {'ok': False, 'message': f'추가 실패: {e}'}


@router.delete('/{block_id}')
def remove_block(block_id: int, user=Depends(require_admin)):
    """차단 해제."""
    db.execute("DELETE FROM block_list WHERE id=%s", (block_id,))
    return {'ok': True}
