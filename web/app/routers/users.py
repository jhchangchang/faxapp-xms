"""
사용자 라우터 - 목록/생성/수정/삭제 + 비밀번호 변경 + 최초 관리자 생성.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from ..core import db, security, errors
from ..deps import get_current_user, require_admin

router = APIRouter(prefix='/api/users', tags=['users'])

VALID_ROLES = ('admin', 'manager', 'user')


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str
    email: Optional[str] = None
    dept_id: Optional[int] = None
    fax_number: Optional[str] = None
    role: str = 'user'


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    dept_id: Optional[int] = None
    fax_number: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


@router.get('')
def list_users(user=Depends(require_admin)):
    """사용자 목록 (관리자)."""
    return db.query(
        'SELECT u.id, u.username, u.display_name, u.email, u.role, '
        'u.fax_number, u.is_active, u.dept_id, d.name AS dept_name, u.last_login '
        'FROM users u LEFT JOIN departments d ON u.dept_id=d.id '
        'ORDER BY u.username')


@router.post('')
def create_user(body: UserCreate, user=Depends(require_admin)):
    """사용자 생성 (관리자)."""
    if body.role not in VALID_ROLES:
        raise errors.ValidationError(detail=f'role은 {VALID_ROLES} 중 하나여야 합니다')
    if len(body.password) < 4:
        raise errors.ValidationError(detail='비밀번호는 4자 이상이어야 합니다')
    dup = db.query_one('SELECT id FROM users WHERE username=%s', (body.username,))
    if dup:
        raise errors.Conflict('이미 존재하는 아이디입니다')
    pw_hash = security.hash_password(body.password)
    row = db.execute(
        'INSERT INTO users(username, password_hash, display_name, email, '
        'dept_id, fax_number, role) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
        (body.username, pw_hash, body.display_name, body.email,
         body.dept_id, body.fax_number, body.role), returning=True)
    return {'ok': True, 'id': row['id']}


@router.put('/{user_id}')
def update_user(user_id: int, body: UserUpdate, user=Depends(require_admin)):
    """사용자 수정 (관리자)."""
    target = db.query_one('SELECT id FROM users WHERE id=%s', (user_id,))
    if not target:
        raise errors.NotFound('사용자')
    # 동적 UPDATE - 전달된 필드만
    fields, params = [], []
    for col in ('display_name', 'email', 'dept_id', 'fax_number', 'role', 'is_active'):
        val = getattr(body, col)
        if val is not None:
            if col == 'role' and val not in VALID_ROLES:
                raise errors.ValidationError(detail='잘못된 role입니다')
            fields.append(f'{col}=%s'); params.append(val)
    if not fields:
        return {'ok': True, 'message': '변경 없음'}
    params.append(user_id)
    db.execute(f'UPDATE users SET {", ".join(fields)} WHERE id=%s', tuple(params))
    return {'ok': True}


@router.delete('/{user_id}')
def delete_user(user_id: int, user=Depends(require_admin)):
    """사용자 삭제 (관리자). 자기 자신은 삭제 불가."""
    if user_id == user['user_id']:
        raise errors.ValidationError(detail='자기 자신은 삭제할 수 없습니다')
    target = db.query_one('SELECT id FROM users WHERE id=%s', (user_id,))
    if not target:
        raise errors.NotFound('사용자')
    db.execute('DELETE FROM users WHERE id=%s', (user_id,))
    return {'ok': True}


@router.post('/me/password')
def change_my_password(body: PasswordChange, user=Depends(get_current_user)):
    """내 비밀번호 변경 (본인)."""
    row = db.query_one('SELECT password_hash FROM users WHERE id=%s',
                       (user['user_id'],))
    if not row or not security.verify_password(body.old_password, row['password_hash']):
        raise errors.AuthError(detail='현재 비밀번호가 올바르지 않습니다')
    if len(body.new_password) < 4:
        raise errors.ValidationError(detail='새 비밀번호는 4자 이상이어야 합니다')
    new_hash = security.hash_password(body.new_password)
    db.execute('UPDATE users SET password_hash=%s WHERE id=%s',
               (new_hash, user['user_id']))
    return {'ok': True}


@router.post('/bootstrap-admin')
def bootstrap_admin(body: UserCreate):
    """
    최초 관리자 생성. 사용자가 하나도 없을 때만 동작 (인증 불필요).
    시스템 초기 설정용.
    """
    count = db.query_one('SELECT COUNT(*) AS c FROM users')
    if count and count['c'] > 0:
        raise errors.PermissionDenied(detail='이미 사용자가 존재합니다. 이 기능은 최초 1회만 사용 가능합니다')
    pw_hash = security.hash_password(body.password)
    row = db.execute(
        'INSERT INTO users(username, password_hash, display_name, email, role) '
        'VALUES(%s,%s,%s,%s,%s) RETURNING id',
        (body.username, pw_hash, body.display_name, body.email, 'admin'),
        returning=True)
    return {'ok': True, 'id': row['id'], 'message': '최초 관리자 생성 완료'}
