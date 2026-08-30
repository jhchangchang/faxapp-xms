"""
수신 라우터 - 팩스 서버 웹훅 수신 → 번호별 분류 → 수신함.
미리보기(파일 다운로드), 읽음 표시, 메모 첨부.
"""
import os
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from ..core import db, config, errors, faxclient
from ..deps import get_current_user, require_manager

router = APIRouter(prefix='/api/receive', tags=['fax-receive'])


class WebhookBody(BaseModel):
    from_number: Optional[str] = None
    to_number: Optional[str] = None       # 수신 DID (분류 기준)
    file_path: str                        # 팩스 서버가 저장한 수신 파일 경로
    pages: int = 0
    rate: Optional[str] = None


class MemoBody(BaseModel):
    memo: str


def _classify(to_number):
    """수신 번호(DID)로 부서/사용자 자동 배분.
    반환: (dept_id, user_id) - 못 찾으면 (None, None)"""
    if not to_number:
        return None, None
    # 1. 사용자 개인 팩스번호 매칭
    u = db.query_one('SELECT id, dept_id FROM users WHERE fax_number=%s AND is_active=true',
                     (to_number,))
    if u:
        return u['dept_id'], u['id']
    # 2. 부서 대표 팩스번호 매칭
    d = db.query_one('SELECT id FROM departments WHERE fax_number=%s', (to_number,))
    if d:
        return d['id'], None
    return None, None


@router.post('/webhook')
def receive_webhook(body: WebhookBody, request: Request):
    """
    팩스 서버가 수신 완료 시 호출하는 웹훅.
    ※ 내부 통신용 - 운영 시 IP 화이트리스트/토큰 인증 권장.
    """
    dept_id, user_id = _classify(body.to_number)
    row = db.execute(
        'INSERT INTO fax_received(from_number, to_number, dept_id, user_id, '
        'file_path, pages, rate) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
        (body.from_number, body.to_number, dept_id, user_id,
         body.file_path, body.pages, body.rate), returning=True)
    return {'ok': True, 'id': row['id'], 'classified': {'dept_id': dept_id, 'user_id': user_id}}


@router.get('/inbox')
def inbox(unread_only: bool = False, limit: int = 50, user=Depends(get_current_user)):
    """
    수신함. 권한에 따라 범위 결정:
    - admin: 전체
    - manager: 자기 부서
    - user: 자기 앞으로 온 것 + 부서 공용(미배정)
    """
    limit = min(limit, 200)
    base = ('SELECT id, from_number, to_number, dept_id, user_id, pages, rate, '
            'is_read, memo, received_at FROM fax_received')
    cond, params = [], []

    if user['role'] == 'admin':
        pass
    elif user['role'] == 'manager':
        # 자기 부서 것
        me = db.query_one('SELECT dept_id FROM users WHERE id=%s', (user['user_id'],))
        dept = me['dept_id'] if me else None
        cond.append('dept_id = %s'); params.append(dept)
    else:
        # 본인 앞 + 본인 부서 미배정
        me = db.query_one('SELECT dept_id FROM users WHERE id=%s', (user['user_id'],))
        dept = me['dept_id'] if me else None
        cond.append('(user_id = %s OR (dept_id = %s AND user_id IS NULL))')
        params += [user['user_id'], dept]

    if unread_only:
        cond.append('is_read = false')

    sql = base
    if cond:
        sql += ' WHERE ' + ' AND '.join(cond)
    sql += ' ORDER BY id DESC LIMIT %s'
    params.append(limit)
    return db.query(sql, tuple(params))


@router.get('/{recv_id}')
def receive_detail(recv_id: int, user=Depends(get_current_user)):
    """수신 팩스 상세."""
    row = db.query_one(
        'SELECT id, from_number, to_number, dept_id, user_id, file_path, '
        'pages, rate, is_read, memo, received_at FROM fax_received WHERE id=%s',
        (recv_id,))
    if not row:
        raise errors.NotFound('수신 팩스')
    _check_access(row, user)
    return row


@router.get('/{recv_id}/file')
def receive_file(recv_id: int, user=Depends(get_current_user)):
    """수신 팩스 파일 다운로드/미리보기."""
    row = db.query_one('SELECT user_id, dept_id, file_path FROM fax_received WHERE id=%s',
                       (recv_id,))
    if not row:
        raise errors.NotFound('수신 팩스')
    _check_access(row, user)
    path = row['file_path']
    if not path or not os.path.isfile(path):
        raise errors.NotFound('파일')
    # 읽음 처리
    db.execute('UPDATE fax_received SET is_read=true WHERE id=%s', (recv_id,))
    filename = os.path.basename(path)
    return FileResponse(path, filename=filename)


@router.post('/{recv_id}/read')
def mark_read(recv_id: int, user=Depends(get_current_user)):
    """읽음 표시."""
    row = db.query_one('SELECT user_id, dept_id FROM fax_received WHERE id=%s', (recv_id,))
    if not row:
        raise errors.NotFound('수신 팩스')
    _check_access(row, user)
    db.execute('UPDATE fax_received SET is_read=true WHERE id=%s', (recv_id,))
    return {'ok': True}


@router.post('/{recv_id}/memo')
def set_memo(recv_id: int, body: MemoBody, user=Depends(get_current_user)):
    """수신 팩스에 메모 첨부."""
    row = db.query_one('SELECT user_id, dept_id FROM fax_received WHERE id=%s', (recv_id,))
    if not row:
        raise errors.NotFound('수신 팩스')
    _check_access(row, user)
    db.execute('UPDATE fax_received SET memo=%s WHERE id=%s', (body.memo, recv_id))
    return {'ok': True}


@router.post('/{recv_id}/assign')
def assign_receive(recv_id: int, target_user_id: int, user=Depends(require_manager)):
    """수신 팩스를 특정 사용자에게 배정 (매니저/관리자)."""
    row = db.query_one('SELECT id FROM fax_received WHERE id=%s', (recv_id,))
    if not row:
        raise errors.NotFound('수신 팩스')
    target = db.query_one('SELECT dept_id FROM users WHERE id=%s', (target_user_id,))
    if not target:
        raise errors.NotFound('대상 사용자')
    db.execute('UPDATE fax_received SET user_id=%s, dept_id=%s WHERE id=%s',
               (target_user_id, target['dept_id'], recv_id))
    return {'ok': True}


@router.get('/unread/count')
def unread_count(user=Depends(get_current_user)):
    """안읽은 수신 팩스 개수 (뱃지용)."""
    if user['role'] == 'admin':
        r = db.query_one('SELECT COUNT(*) AS c FROM fax_received WHERE is_read=false')
    else:
        me = db.query_one('SELECT dept_id FROM users WHERE id=%s', (user['user_id'],))
        dept = me['dept_id'] if me else None
        r = db.query_one(
            'SELECT COUNT(*) AS c FROM fax_received WHERE is_read=false AND '
            '(user_id=%s OR (dept_id=%s AND user_id IS NULL))',
            (user['user_id'], dept))
    return {'unread': r['c'] if r else 0}


def _check_access(row, user):
    """수신 팩스 접근 권한 확인."""
    if user['role'] == 'admin':
        return
    me = db.query_one('SELECT dept_id FROM users WHERE id=%s', (user['user_id'],))
    my_dept = me['dept_id'] if me else None
    if user['role'] == 'manager':
        if row.get('dept_id') == my_dept:
            return
    else:
        if row.get('user_id') == user['user_id']:
            return
        if row.get('dept_id') == my_dept and row.get('user_id') is None:
            return
    raise errors.PermissionDenied()


class ForwardBody(BaseModel):
    to_number: str                        # 전달할 팩스 번호
    to_name: Optional[str] = None         # 받는 사람 이름(선택)


@router.post('/{recv_id}/forward')
def forward_receive(recv_id: int, body: ForwardBody, user=Depends(get_current_user)):
    """받은 팩스를 다른 번호로 전달(포워딩).
    수신 파일(TIFF)을 그대로 발송 큐에 넣음. 발송 이력에도 기록됨."""
    # 1. 수신 팩스 조회 + 접근 권한
    row = db.query_one(
        'SELECT id, from_number, to_number, dept_id, user_id, file_path, pages '
        'FROM fax_received WHERE id=%s', (recv_id,))
    if not row:
        raise errors.NotFound('수신 팩스')
    _check_access(row, user)

    # 2. 전달 번호 검증
    import re
    num = (body.to_number or '').strip()
    cleaned = re.sub(r'[\s\-()+]', '', num)
    if not (cleaned.isdigit() and 3 <= len(cleaned) <= 20):
        raise errors.ValidationError(detail='전달할 팩스번호 형식이 올바르지 않습니다')

    # 3. 파일 존재 확인
    fpath = row.get('file_path')
    if not fpath or not os.path.isfile(fpath):
        raise errors.NotFound('수신 팩스 파일')

    # 4. 발송 job 생성 (전달임을 file_name에 표시)
    pages = row.get('pages', 0) or 0
    fwd_name = f'[전달] {row["from_number"] or "수신"} → 원본#{recv_id}'
    job = db.execute(
        'INSERT INTO fax_jobs(user_id, to_number, to_name, file_name, file_path, '
        'pages, cover_used, status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
        (user['user_id'], num, body.to_name or '', fwd_name, fpath, pages, False, 'pending'),
        returning=True)
    job_id = job['id']

    # 5. 팩스 서버로 발송 요청
    try:
        resp = faxclient.send_fax(num, fpath, async_mode=True, job_id=job_id)
        server_job = resp.get('job_id') or str(job_id)
        db.execute("UPDATE fax_jobs SET status='queued', server_job_id=%s WHERE id=%s",
                   (str(server_job), job_id))
        return {'ok': True, 'job_id': job_id, 'status': 'queued',
                'message': f'{num}로 전달 요청되었습니다'}
    except faxclient.FaxServerError as e:
        exc = errors.FaxServerDown(detail=str(e), context={'job_id': job_id})
        db.execute("UPDATE fax_jobs SET status='retry_wait', last_error_code=%s, "
                   "result_text=%s WHERE id=%s", (exc.code, str(e)[:255], job_id))
        raise exc
