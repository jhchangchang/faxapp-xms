"""
주소록 라우터 - 연락처 CRUD + 그룹(동보전송용).
개인 주소록(owner_id=본인) + 공유 주소록(is_shared) 지원.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional, List

from ..core import db, errors
from ..deps import get_current_user

router = APIRouter(prefix='/api/contacts', tags=['contacts'])


class ContactBody(BaseModel):
    name: str
    fax_number: str
    company: Optional[str] = None
    memo: Optional[str] = None
    is_shared: bool = False


class GroupBody(BaseModel):
    name: str


class GroupMembersBody(BaseModel):
    contact_ids: List[int]


# ---------- 연락처 ----------
@router.get('')
def list_contacts(user=Depends(get_current_user)):
    """내 연락처 + 공유 연락처."""
    return db.query(
        'SELECT id, name, fax_number, company, memo, is_shared, owner_id '
        'FROM contacts WHERE owner_id=%s OR is_shared=true '
        'ORDER BY name', (user['user_id'],))


@router.post('')
def create_contact(body: ContactBody, user=Depends(get_current_user)):
    """연락처 추가."""
    if not body.name.strip() or not body.fax_number.strip():
        raise errors.ValidationError(detail='이름과 팩스번호는 필수입니다')
    row = db.execute(
        'INSERT INTO contacts(owner_id, name, fax_number, company, memo, is_shared) '
        'VALUES(%s,%s,%s,%s,%s,%s) RETURNING id',
        (user['user_id'], body.name.strip(), body.fax_number.strip(),
         body.company, body.memo, body.is_shared), returning=True)
    return {'ok': True, 'id': row['id']}


@router.put('/{contact_id}')
def update_contact(contact_id: int, body: ContactBody, user=Depends(get_current_user)):
    """연락처 수정 (본인 것만)."""
    row = db.query_one('SELECT owner_id FROM contacts WHERE id=%s', (contact_id,))
    if not row:
        raise errors.NotFound('연락처')
    if row['owner_id'] != user['user_id'] and user['role'] != 'admin':
        raise errors.PermissionDenied(detail='본인 연락처만 수정할 수 있습니다')
    db.execute(
        'UPDATE contacts SET name=%s, fax_number=%s, company=%s, memo=%s, is_shared=%s '
        'WHERE id=%s',
        (body.name.strip(), body.fax_number.strip(), body.company,
         body.memo, body.is_shared, contact_id))
    return {'ok': True}


@router.delete('/{contact_id}')
def delete_contact(contact_id: int, user=Depends(get_current_user)):
    """연락처 삭제 (본인 것만)."""
    row = db.query_one('SELECT owner_id FROM contacts WHERE id=%s', (contact_id,))
    if not row:
        raise errors.NotFound('연락처')
    if row['owner_id'] != user['user_id'] and user['role'] != 'admin':
        raise errors.PermissionDenied(detail='본인 연락처만 삭제할 수 있습니다')
    db.execute('DELETE FROM contacts WHERE id=%s', (contact_id,))
    return {'ok': True}


# ---------- 그룹 (동보전송용) ----------
@router.get('/groups')
def list_groups(user=Depends(get_current_user)):
    """내 그룹 목록 + 각 그룹 인원수."""
    return db.query(
        'SELECT g.id, g.name, '
        '(SELECT COUNT(*) FROM contact_group_members m WHERE m.group_id=g.id) AS member_count '
        'FROM contact_groups g WHERE g.owner_id=%s ORDER BY g.name',
        (user['user_id'],))


@router.post('/groups')
def create_group(body: GroupBody, user=Depends(get_current_user)):
    """그룹 생성."""
    if not body.name.strip():
        raise errors.ValidationError(detail='그룹명은 필수입니다')
    row = db.execute(
        'INSERT INTO contact_groups(owner_id, name) VALUES(%s,%s) RETURNING id',
        (user['user_id'], body.name.strip()), returning=True)
    return {'ok': True, 'id': row['id']}


@router.get('/groups/{group_id}/members')
def group_members(group_id: int, user=Depends(get_current_user)):
    """그룹 멤버 목록."""
    g = db.query_one('SELECT owner_id FROM contact_groups WHERE id=%s', (group_id,))
    if not g or g['owner_id'] != user['user_id']:
        raise errors.NotFound('그룹')
    return db.query(
        'SELECT c.id, c.name, c.fax_number, c.company '
        'FROM contact_group_members m JOIN contacts c ON m.contact_id=c.id '
        'WHERE m.group_id=%s ORDER BY c.name', (group_id,))


@router.post('/groups/{group_id}/members')
def set_group_members(group_id: int, body: GroupMembersBody, user=Depends(get_current_user)):
    """그룹 멤버 설정 (기존 교체)."""
    g = db.query_one('SELECT owner_id FROM contact_groups WHERE id=%s', (group_id,))
    if not g or g['owner_id'] != user['user_id']:
        raise errors.NotFound('그룹')
    db.execute('DELETE FROM contact_group_members WHERE group_id=%s', (group_id,))
    if body.contact_ids:
        db.execute_many(
            'INSERT INTO contact_group_members(group_id, contact_id) VALUES(%s,%s) '
            'ON CONFLICT DO NOTHING',
            [(group_id, cid) for cid in body.contact_ids])
    return {'ok': True, 'count': len(body.contact_ids)}


@router.delete('/groups/{group_id}')
def delete_group(group_id: int, user=Depends(get_current_user)):
    """그룹 삭제."""
    g = db.query_one('SELECT owner_id FROM contact_groups WHERE id=%s', (group_id,))
    if not g or g['owner_id'] != user['user_id']:
        raise errors.NotFound('그룹')
    db.execute('DELETE FROM contact_groups WHERE id=%s', (group_id,))
    return {'ok': True}
