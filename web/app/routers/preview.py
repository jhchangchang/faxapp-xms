"""
미리보기 라우터 - 발송 작업/수신 팩스의 파일을 브라우저에서 볼 수 있게 PNG 변환.
TIFF는 브라우저가 못 여니 PNG로 변환해 제공.
"""
import os
from fastapi import APIRouter, Depends, Query, UploadFile, File
from fastapi.responses import FileResponse

from ..core import db, config, convert, errors
from ..deps import get_current_user

router = APIRouter(prefix='/api/preview', tags=['preview'])

PREVIEW_DIR = os.path.join(config.UPLOAD_DIR, '_preview')


def _preview_png(file_path, page):
    """파일을 미리보기 PNG로 변환해 경로 반환."""
    if not file_path or not os.path.isfile(file_path):
        raise errors.NotFound('파일')
    try:
        return convert.to_preview_png(file_path, PREVIEW_DIR, page=page)
    except convert.ConvertError as e:
        raise errors.ConversionError(detail=f'미리보기 변환 실패: {e}')


@router.get('/job/{job_id}')
def preview_job(job_id: int, page: int = Query(default=0, ge=0), user=Depends(get_current_user)):
    """발송 작업 문서 미리보기 (PNG)."""
    job = db.query_one(
        'SELECT user_id, file_path FROM fax_jobs WHERE id=%s', (job_id,))
    if not job or (job['user_id'] != user['user_id'] and user['role'] != 'admin'):
        raise errors.NotFound('작업')
    png = _preview_png(job['file_path'], page)
    return FileResponse(png, media_type='image/png')


@router.get('/received/{recv_id}')
def preview_received(recv_id: int, page: int = Query(default=0, ge=0), user=Depends(get_current_user)):
    """수신 팩스 미리보기 (PNG). 권한 확인 포함."""
    row = db.query_one(
        'SELECT user_id, dept_id, file_path FROM fax_received WHERE id=%s', (recv_id,))
    if not row:
        raise errors.NotFound('수신 팩스')
    # 접근 권한 (receive.py의 규칙과 동일)
    if user['role'] != 'admin':
        me = db.query_one('SELECT dept_id FROM users WHERE id=%s', (user['user_id'],))
        my_dept = me['dept_id'] if me else None
        allowed = False
        if user['role'] == 'manager' and row.get('dept_id') == my_dept:
            allowed = True
        elif row.get('user_id') == user['user_id']:
            allowed = True
        elif row.get('dept_id') == my_dept and row.get('user_id') is None:
            allowed = True
        if not allowed:
            raise errors.PermissionDenied()
    png = _preview_png(row['file_path'], page)
    # 미리보기 시 읽음 처리
    db.execute('UPDATE fax_received SET is_read=true WHERE id=%s', (recv_id,))
    return FileResponse(png, media_type='image/png')


@router.get('/upload')
def preview_upload(path: str, page: int = Query(default=0, ge=0), user=Depends(get_current_user)):
    """
    업로드 직후(발송 전) 파일 미리보기.
    path는 업로드 디렉토리 내 파일만 허용 (경로 탈출 방지).
    """
    # 보안: UPLOAD_DIR 내부 경로만 허용
    real = os.path.realpath(path)
    base = os.path.realpath(config.UPLOAD_DIR)
    if not real.startswith(base):
        raise errors.PermissionDenied(detail='허용되지 않은 경로입니다')
    png = _preview_png(real, page)
    return FileResponse(png, media_type='image/png')


@router.post('/preflight')
async def preview_preflight(file: UploadFile = File(...), page: int = Query(default=0, ge=0),
                           user=Depends(get_current_user)):
    """발송 전 미리보기 - 업로드한 파일을 임시로 변환해 PNG로 즉시 보여줌.
    실제 발송과 동일한 변환을 거쳐, 팩스로 어떻게 나갈지 미리 확인."""
    import uuid as _uuid
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    # 임시 저장
    ext = os.path.splitext(file.filename or '')[1] or '.bin'
    tmp = os.path.join(PREVIEW_DIR, f'pf_{_uuid.uuid4().hex}{ext}')
    data = await file.read()
    if not data:
        raise errors.EmptyFile(context={'filename': file.filename})
    with open(tmp, 'wb') as f:
        f.write(data)
    try:
        # 실제 발송과 동일하게 팩스 TIFF로 변환 후 미리보기
        tiff, pages = convert.to_fax_tiff(tmp, PREVIEW_DIR)
        png = convert.to_preview_png(tiff, PREVIEW_DIR, page=page)
        return FileResponse(png, media_type='image/png',
                            headers={'X-Total-Pages': str(pages)})
    except convert.ConvertError as e:
        raise errors.ConversionError(detail=f'미리보기 변환 실패: {e}')
    finally:
        # 임시 원본 정리 (변환 결과는 캐시로 남김)
        try:
            os.remove(tmp)
        except Exception:
            pass
