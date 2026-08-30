"""
부서(조직) 라우터 - 목록/생성/수정/삭제. 관리자 권한 필요.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from ..core import db, errors
from ..deps import get_current_user, require_admin

router = APIRouter(prefix='/api/departments', tags=['departments'])


class DeptBody(BaseModel):
    name: str
    parent_id: Optional[int] = None
    fax_number: Optional[str] = None


@router.get('')
def list_departments(user=Depends(get_current_user)):
    """부서 목록 (로그인 사용자 누구나 조회 가능)."""
    return db.query(
        'SELECT d.id, d.name, d.parent_id, d.fax_number, '
        '(SELECT COUNT(*) FROM users u WHERE u.dept_id=d.id) AS user_count '
        'FROM departments d ORDER BY d.name')


@router.post('')
def create_department(body: DeptBody, user=Depends(require_admin)):
    """부서 생성 (관리자)."""
    if not body.name.strip():
        raise errors.ValidationError(detail='부서명은 필수입니다')
    row = db.execute(
        'INSERT INTO departments(name, parent_id, fax_number) '
        'VALUES(%s,%s,%s) RETURNING id',
        (body.name.strip(), body.parent_id, body.fax_number), returning=True)
    return {'ok': True, 'id': row['id']}


@router.put('/{dept_id}')
def update_department(dept_id: int, body: DeptBody, user=Depends(require_admin)):
    """부서 수정 (관리자)."""
    exists = db.query_one('SELECT id FROM departments WHERE id=%s', (dept_id,))
    if not exists:
        raise errors.NotFound('부서')
    db.execute(
        'UPDATE departments SET name=%s, parent_id=%s, fax_number=%s WHERE id=%s',
        (body.name.strip(), body.parent_id, body.fax_number, dept_id))
    return {'ok': True}


@router.delete('/{dept_id}')
def delete_department(dept_id: int, user=Depends(require_admin)):
    """부서 삭제 (관리자). 소속 사용자는 dept_id=NULL로 남음."""
    exists = db.query_one('SELECT id FROM departments WHERE id=%s', (dept_id,))
    if not exists:
        raise errors.NotFound('부서')
    db.execute('DELETE FROM departments WHERE id=%s', (dept_id,))
    return {'ok': True}
