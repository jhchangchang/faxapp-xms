"""
외부 연동 API - 다른 사내 시스템이 인증키로 팩스를 보낼 수 있는 REST API.
- 키 관리: 관리자가 발급/폐기 (세션 인증)
- 외부 발송: X-API-Key 헤더로 인증 (세션 불필요)
"""
import os
from fastapi import APIRouter, Depends, UploadFile, File, Form
from pydantic import BaseModel

from ..core import db, apikey, errors, convert, config, faxclient
from ..deps import get_current_user, require_admin, require_api_key

router = APIRouter(prefix='/api/v1', tags=['external-api'])


# ==================== 키 관리 (관리자, 세션 인증) ====================

class KeyCreate(BaseModel):
    name: str


@router.post('/keys')
def create_key(body: KeyCreate, user=Depends(require_admin)):
    """API 키 발급. 평문 키는 이 응답에서만 확인 가능 (이후 조회 불가)."""
    if not body.name or not body.name.strip():
        raise errors.ValidationError(detail='키 이름을 입력해주세요')
    result = apikey.generate_key(body.name.strip(), user['user_id'])
    return {
        'ok': True,
        'id': result['id'],
        'name': result['name'],
        'key': result['key'],   # 평문 - 1회만 노출
        'warning': '이 키는 지금만 확인할 수 있습니다. 안전한 곳에 보관하세요.',
    }


@router.get('/keys')
def list_api_keys(user=Depends(require_admin)):
    """발급된 키 목록 (평문 키 없음, 접두어만)."""
    return apikey.list_keys()


@router.post('/keys/{key_id}/revoke')
def revoke(key_id: int, user=Depends(require_admin)):
    """키 폐기 (비활성화)."""
    apikey.revoke_key(key_id)
    return {'ok': True, 'message': '키가 폐기되었습니다'}


@router.delete('/keys/{key_id}')
def delete(key_id: int, user=Depends(require_admin)):
    """키 완전 삭제."""
    apikey.delete_key(key_id)
    return {'ok': True}


# ==================== 외부 발송 (API 키 인증) ====================

@router.post('/fax/send')
async def api_send_fax(
    number: str = Form(...),
    file: UploadFile = File(...),
    to_name: str = Form(default=''),
    key_info=Depends(require_api_key),
):
    """외부 시스템용 팩스 발송. X-API-Key 헤더로 인증.

    요청 예시:
      POST /api/v1/fax/send
      Header: X-API-Key: fx_xxxxx
      Form: number=0212345678, file=@document.pdf

    반환: {ok, job_id, status}
    """
    # 발송 담당 사용자 = 키 발급자 (이력 추적용)
    owner_id = key_info.get('created_by')

    number = (number or '').strip()
    # 번호 검증
    import re
    cleaned = re.sub(r'[\s\-()+]', '', number)
    if not (cleaned.isdigit() and 3 <= len(cleaned) <= 20):
        raise errors.InvalidFaxNumber(context={'number': number})

    # 파일 저장·변환
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    import uuid as _uuid
    ext = os.path.splitext(file.filename or '')[1] or '.bin'
    src = os.path.join(config.UPLOAD_DIR, f'api_{_uuid.uuid4().hex}{ext}')
    data = await file.read()
    if not data:
        raise errors.EmptyFile(context={'filename': file.filename})
    with open(src, 'wb') as f:
        f.write(data)
    try:
        tiff, pages = convert.to_fax_tiff(src, config.UPLOAD_DIR)
    except convert.ConvertError as e:
        raise errors.ConversionError(detail=f'파일 변환 실패: {e}')

    # 작업 생성 (api_key_id로 어느 키가 보냈는지 추적)
    job = db.execute(
        'INSERT INTO fax_jobs(user_id, to_number, to_name, file_name, file_path, '
        'pages, status, api_key_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
        (owner_id, number, to_name, file.filename, tiff, pages, 'pending', key_info['id']),
        returning=True)
    job_id = job['id']

    # 발송
    try:
        resp = faxclient.send_fax(number, tiff, async_mode=True, job_id=job_id)
        server_job = resp.get('job_id') or str(job_id)
        db.execute("UPDATE fax_jobs SET status='queued', server_job_id=%s WHERE id=%s",
                   (str(server_job), job_id))
        return {'ok': True, 'job_id': job_id, 'status': 'queued',
                'pages': pages, 'to': number}
    except faxclient.FaxServerError as e:
        db.execute("UPDATE fax_jobs SET status='failed', result_text=%s WHERE id=%s",
                   (str(e)[:255], job_id))
        raise errors.FaxServerDown(detail=str(e))


@router.get('/fax/status/{job_id}')
def api_fax_status(job_id: int, key_info=Depends(require_api_key)):
    """발송 상태 조회. 외부 시스템이 발송 결과를 폴링할 수 있음."""
    job = db.query_one(
        'SELECT id, to_number, status, pages, last_error_code, result_text, '
        'created_at, sent_at FROM fax_jobs WHERE id=%s AND api_key_id=%s',
        (job_id, key_info['id']))
    if not job:
        raise errors.NotFound('발송 작업')
    return {
        'job_id': job['id'], 'to': job['to_number'], 'status': job['status'],
        'pages': job['pages'],
        'error_code': job.get('last_error_code'),
        'error': job.get('result_text'),
        'created_at': str(job['created_at']),
        'sent_at': str(job['sent_at']) if job.get('sent_at') else None,
    }
