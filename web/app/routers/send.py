"""
발송 라우터 - 파일 업로드 → 변환 → 팩스 서버로 발송.
단건 발송 + 동보전송(다중 수신) + 발송 이력.
"""
import os
import re
import uuid
import shutil
from datetime import datetime
from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
from pydantic import BaseModel
from typing import List, Optional

from ..core import db, config, faxclient, convert, errors, error_handler
from ..core import cover as cover_mod
from ..core import ratelimit
from ..deps import get_current_user, require_admin

router = APIRouter(prefix='/api/fax', tags=['fax-send'])


def _check_send_rate(user):
    """발송 rate limit. 스팸/과금 폭탄 방어.
    한도는 config로 조정 가능 (기본: 사용자당 분당 30건, 시간당 300건).
    admin은 면제 가능하나, 기본은 모두 적용 (탈취 대비)."""
    uid = str(user.get('user_id', 'anon'))
    try:
        ratelimit.check(f'send:min:{uid}', config.RATE_SEND_PER_MIN, 60)
        ratelimit.check(f'send:hour:{uid}', config.RATE_SEND_PER_HOUR, 3600)
    except ratelimit.RateLimitExceeded as e:
        raise errors.RateLimitError(
            retry_after=e.retry_after,
            context={'limit': e.limit, 'window': e.window})


def _valid_fax_number(number: str) -> bool:
    """팩스번호 형식 검증: 숫자·하이픈·괄호·+ 만, 숫자 3자리 이상."""
    if not number:
        return False
    cleaned = re.sub(r'[\s\-()+]', '', number)
    return cleaned.isdigit() and 3 <= len(cleaned) <= 20


def _save_upload(upload: UploadFile) -> str:
    """업로드 파일을 저장하고 경로 반환.
    안정성: 크기 제한(과대 파일 방어) + 디스크 여유 확인(디스크 채움 방어).
    청크 단위로 읽으며 한도 초과 시 중단.
    """
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    # 디스크 여유 확인 (최소 여유 미달이면 거부 - 서비스 마비 방지)
    try:
        st = os.statvfs(config.UPLOAD_DIR)
        free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
        if free_mb < config.MIN_DISK_FREE_MB:
            raise errors.SystemError(
                detail=f'디스크 여유 부족: {free_mb:.0f}MB',
                user_message='일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해주세요.')
    except errors.FaxAppError:
        raise
    except Exception:
        pass  # statvfs 실패는 무시 (일부 환경)

    ext = os.path.splitext(upload.filename or 'file')[1]
    safe = f'up_{uuid.uuid4().hex}{ext}'
    path = os.path.join(config.UPLOAD_DIR, safe)
    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    try:
        with open(path, 'wb') as f:
            while True:
                chunk = upload.file.read(1024 * 1024)  # 1MB씩
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    f.close()
                    os.remove(path)
                    raise errors.FileTooLarge(
                        detail=f'{written} bytes > {max_bytes}',
                        context={'max_mb': config.MAX_UPLOAD_MB})
                f.write(chunk)
    except errors.FaxAppError:
        raise
    except Exception as e:
        # 저장 중 오류 - 부분 파일 정리
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        raise errors.SystemError(detail=f'파일 저장 실패: {e}')
    if written == 0:
        try:
            os.remove(path)
        except Exception:
            pass
        raise errors.EmptyFile()
    return path


def _audit(user_id, action, detail):
    try:
        db.execute('INSERT INTO audit_log(user_id, action, detail) VALUES(%s,%s,%s)',
                   (user_id, action, detail))
    except Exception:
        pass


@router.post('/send')
async def send_fax(
    number: str = Form(...),
    to_name: str = Form(default=''),
    cover: bool = Form(default=False),
    title: str = Form(default=''),
    memo: str = Form(default=''),
    cover_template: str = Form(default='standard'),  # 표지 스타일
    scheduled_at: str = Form(default=''),   # 예약 발송 시각 (ISO8601, 비면 즉시)
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """단건 발송: 파일 업로드 → TIFF 변환 → (표지) → 팩스 서버 발송.
    scheduled_at이 주어지면 그 시각까지 'scheduled' 상태로 대기 후 자동 발송."""
    # 0. Rate Limit (스팸/과금 폭탄 방어) - 사용자별 발송 한도
    _check_send_rate(user)
    # 0. 입력 검증 (사용자 잘못 → 즉시 명확한 안내)
    number = (number or '').strip()
    if not _valid_fax_number(number):
        raise errors.InvalidFaxNumber(context={'number': number})
    # 0-1. 예약 시각 파싱 (있으면)
    sched_dt = None
    if scheduled_at and scheduled_at.strip():
        from datetime import timezone
        try:
            s = scheduled_at.strip().replace('Z', '+00:00')
            sched_dt = datetime.fromisoformat(s)
            # tz 없는 입력은 로컬로 간주 (서버 tz)
            now = datetime.now(sched_dt.tzinfo) if sched_dt.tzinfo else datetime.now()
            if sched_dt <= now:
                raise errors.ValidationError(detail='예약 시각은 현재보다 미래여야 합니다')
        except errors.ValidationError:
            raise
        except (ValueError, TypeError):
            raise errors.ValidationError(detail='예약 시각 형식이 올바르지 않습니다 (예: 2026-08-20T14:30)')
    # 1. 업로드 저장 + 빈 파일 검사
    src = _save_upload(file)
    if not os.path.getsize(src):
        raise errors.EmptyFile(context={'filename': file.filename})
    # 2. TIFF 변환 (변환 예외 → 중앙 예외로 변환)
    try:
        tiff, pages = convert.to_fax_tiff(src, config.UPLOAD_DIR)
    except convert.ConvertError as e:
        msg = str(e)
        if 'timeout' in msg.lower() or '시간초과' in msg:
            raise errors.ConversionTimeout(detail=msg, context={'filename': file.filename})
        if '미설치' in msg or 'not found' in msg.lower():
            raise errors.ToolNotInstalled(detail=msg)
        raise errors.ConversionError(detail=msg, context={'filename': file.filename})
    # 2-1. 표지 첨부 (옵션) - 실패해도 본문 발송(폴백), 단 기록은 남김
    cover_used = False
    if cover:
        try:
            me = db.query_one('SELECT display_name, fax_number FROM users WHERE id=%s',
                              (user['user_id'],))
            info = {
                'to_name': to_name, 'to_number': number,
                'from_name': me['display_name'] if me else '',
                'from_number': me['fax_number'] if me else '',
                'title': title, 'pages': pages, 'memo': memo,
                'template': cover_template or 'standard',
            }
            tiff, pages = cover_mod.attach_cover(tiff, info, config.UPLOAD_DIR)
            cover_used = True
        except Exception as e:
            # 폴백: 표지 없이 본문 발송. 기록만 남김(사용자 흐름은 유지)
            error_handler.log_error(
                errors.ConversionError(detail=f'표지 생성 실패(본문은 발송): {e}',
                                       context={'stage': 'cover'}),
                path='/api/fax/send', user_id=user['user_id'])
    # 3. 작업 레코드 생성
    #    예약이면 scheduled 상태로 저장하고 팩스 서버 발송은 워커에 위임
    if sched_dt is not None:
        job = db.execute(
            'INSERT INTO fax_jobs(user_id, to_number, to_name, file_name, file_path, '
            'pages, cover_used, status, scheduled_at) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (user['user_id'], number, to_name, file.filename, tiff, pages, cover_used,
             'scheduled', sched_dt),
            returning=True)
        _audit(user['user_id'], 'schedule', f'to={number} at={sched_dt} job={job["id"]}')
        return {'ok': True, 'job_id': job['id'], 'status': 'scheduled',
                'scheduled_at': sched_dt.isoformat(), 'pages': pages,
                'cover_used': cover_used,
                'message': f'{sched_dt.strftime("%Y-%m-%d %H:%M")}에 발송 예약되었습니다'}

    job = db.execute(
        'INSERT INTO fax_jobs(user_id, to_number, to_name, file_name, file_path, '
        'pages, cover_used, status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
        (user['user_id'], number, to_name, file.filename, tiff, pages, cover_used, 'pending'),
        returning=True)
    job_id = job['id']
    # 4. 팩스 서버로 발송 요청 (job_id 전달 → 결과통지 매칭)
    try:
        resp = faxclient.send_fax(number, tiff, async_mode=True, job_id=job_id)
        # 팩스 서버가 job_id를 그대로 돌려줌 → server_job_id = 앱 job_id (매칭 확실)
        server_job = resp.get('job_id') or str(job_id)
        status = 'queued' if resp.get('ok', True) or resp.get('queued', True) else 'failed'
        db.execute(
            'UPDATE fax_jobs SET status=%s, server_job_id=%s WHERE id=%s',
            (status, str(server_job), job_id))
        _audit(user['user_id'], 'send', f'to={number} job={job_id}')
        return {'ok': True, 'job_id': job_id, 'status': status, 'pages': pages,
                'cover_used': cover_used}
    except faxclient.FaxServerError as e:
        # 서버 다운 vs 일시적 구분 + 재시도 예약
        server_down = not faxclient.server_available()
        exc = errors.FaxServerDown(detail=str(e), context={'job_id': job_id, 'number': number}) \
            if server_down \
            else errors.FaxServerError(detail=str(e), context={'job_id': job_id, 'number': number})
        # 재시도 가능하면 retry_wait로, 아니면 failed
        if error_handler.RetryPolicy.should_retry(exc, 0):
            from datetime import timedelta
            delay = error_handler.RetryPolicy.next_delay(0, exc)
            next_at = datetime.now() + timedelta(seconds=delay)
            db.execute(
                "UPDATE fax_jobs SET status='retry_wait', next_retry_at=%s, "
                "last_error_code=%s, result_text=%s WHERE id=%s",
                (next_at, exc.code, str(e)[:255], job_id))
            # 예외는 기록하되, 사용자에겐 "재시도 예약됨"으로 응답
            error_handler.log_error(exc, path='/api/fax/send', user_id=user['user_id'], job_id=job_id)
            return {'ok': True, 'job_id': job_id, 'status': 'retry_wait', 'pages': pages,
                    'cover_used': cover_used,
                    'message': f'팩스 서버 지연 - {delay}초 후 자동 재시도됩니다'}
        else:
            db.execute("UPDATE fax_jobs SET status='failed', result_text=%s WHERE id=%s",
                       ('failed', str(e), job_id) if False else (str(e)[:255], job_id))
            raise exc


@router.post('/broadcast')
async def broadcast_fax(
    numbers: str = Form(...),      # 콤마 구분 번호들
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """동보전송: 한 파일을 여러 번호로. numbers는 콤마 구분."""
    num_list = [n.strip() for n in numbers.split(',') if n.strip()]
    if not num_list:
        raise errors.ValidationError(detail='수신 번호가 없습니다')
    if len(num_list) > 500:
        raise errors.ValidationError(detail='한 번에 최대 500건까지 가능합니다')
    # 업로드·변환 (한 번만)
    src = _save_upload(file)
    try:
        tiff, pages = convert.to_fax_tiff(src, config.UPLOAD_DIR)
    except convert.ConvertError as e:
        raise errors.ConversionError(detail=f'파일 변환 실패: {e}')
    batch_id = uuid.uuid4().hex[:16]
    results = []
    for number in num_list:
        job = db.execute(
            'INSERT INTO fax_jobs(user_id, to_number, file_name, file_path, '
            'pages, status, batch_id) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (user['user_id'], number, file.filename, tiff, pages, 'pending', batch_id),
            returning=True)
        job_id = job['id']
        try:
            resp = faxclient.send_fax(number, tiff, async_mode=True)
            server_job = resp.get('job_id') or resp.get('id', '')
            db.execute('UPDATE fax_jobs SET status=%s, server_job_id=%s WHERE id=%s',
                       ('queued', str(server_job), job_id))
            results.append({'number': number, 'job_id': job_id, 'ok': True})
        except faxclient.FaxServerError as e:
            db.execute('UPDATE fax_jobs SET status=%s, result_text=%s WHERE id=%s',
                       ('failed', str(e), job_id))
            results.append({'number': number, 'job_id': job_id, 'ok': False})
    _audit(user['user_id'], 'broadcast', f'batch={batch_id} count={len(num_list)}')
    ok_count = sum(1 for r in results if r['ok'])
    return {'ok': True, 'batch_id': batch_id, 'total': len(num_list),
            'queued': ok_count, 'results': results}


@router.post('/send-to-group')
async def send_to_group(
    group_id: int = Form(...),
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """주소록 그룹으로 동보전송."""
    g = db.query_one('SELECT owner_id FROM contact_groups WHERE id=%s', (group_id,))
    if not g or g['owner_id'] != user['user_id']:
        raise errors.NotFound('그룹')
    members = db.query(
        'SELECT c.fax_number FROM contact_group_members m '
        'JOIN contacts c ON m.contact_id=c.id WHERE m.group_id=%s', (group_id,))
    if not members:
        raise errors.ValidationError(detail='그룹에 멤버가 없습니다')
    numbers = ','.join(m['fax_number'] for m in members)
    # broadcast 재사용
    return await broadcast_fax(numbers=numbers, file=file, user=user)


@router.get('/jobs')
def list_jobs(limit: int = 50, user=Depends(get_current_user)):
    """내 발송 이력."""
    return db.query(
        'SELECT id, to_number, to_name, file_name, pages, status, '
        'server_job_id, batch_id, result_text, created_at, sent_at '
        'FROM fax_jobs WHERE user_id=%s ORDER BY id DESC LIMIT %s',
        (user['user_id'], min(limit, 200)))


@router.get('/jobs/{job_id}')
def job_detail(job_id: int, user=Depends(get_current_user)):
    """발송 작업 상세 + 팩스 서버 상태 동기화."""
    job = db.query_one(
        'SELECT id, user_id, to_number, status, server_job_id, pages, '
        'result_text, created_at FROM fax_jobs WHERE id=%s', (job_id,))
    if not job or (job['user_id'] != user['user_id'] and user['role'] != 'admin'):
        raise errors.NotFound('작업')
    return job


@router.post('/jobs/{job_id}/resend')
def resend_job(job_id: int, user=Depends(get_current_user)):
    """재전송."""
    job = db.query_one(
        'SELECT id, user_id, to_number, file_path FROM fax_jobs WHERE id=%s', (job_id,))
    if not job or (job['user_id'] != user['user_id'] and user['role'] != 'admin'):
        raise errors.NotFound('작업')
    if not job['file_path'] or not os.path.isfile(job['file_path']):
        raise errors.ValidationError(detail='원본 파일이 없어 재전송할 수 없습니다')
    try:
        resp = faxclient.send_fax(job['to_number'], job['file_path'], async_mode=True)
        db.execute(
            'UPDATE fax_jobs SET status=%s, server_job_id=%s, retry_count=retry_count+1 '
            'WHERE id=%s', ('queued', str(resp.get('job_id', '')), job_id))
        return {'ok': True, 'job_id': job_id, 'status': 'queued'}
    except faxclient.FaxServerError as e:
        raise errors.FaxServerDown(detail=f'팩스 서버 연결 실패: {e}')


@router.get('/formats')
def supported_formats(user=Depends(get_current_user)):
    """지원 파일 형식."""
    return convert.supported_formats()


# ===================== 발송 관리 =====================
@router.get('/jobs-filtered')
def list_jobs_filtered(
    status: str = None, number: str = None, limit: int = 100,
    user=Depends(get_current_user)):
    """상태·번호 필터 발송 이력."""
    limit = min(limit, 500)
    cond = ['user_id=%s'] if user['role'] not in ('admin', 'manager') else []
    params = [user['user_id']] if user['role'] not in ('admin', 'manager') else []
    if status:
        cond.append('status=%s'); params.append(status)
    if number:
        cond.append('to_number LIKE %s'); params.append(f'%{number}%')
    sql = ('SELECT id, to_number, to_name, file_name, pages, status, '
           'cover_used, server_job_id, batch_id, result_text, retry_count, '
           'created_at, sent_at FROM fax_jobs')
    if cond:
        sql += ' WHERE ' + ' AND '.join(cond)
    sql += ' ORDER BY id DESC LIMIT %s'
    params.append(limit)
    return db.query(sql, tuple(params))


@router.post('/jobs/{job_id}/cancel')
def cancel_job(job_id: int, user=Depends(get_current_user)):
    """대기 중인 발송 취소."""
    job = db.query_one('SELECT user_id, status FROM fax_jobs WHERE id=%s', (job_id,))
    if not job or (job['user_id'] != user['user_id'] and user['role'] != 'admin'):
        raise errors.NotFound('작업')
    if job['status'] in ('sent',):
        raise errors.ValidationError(detail='이미 전송된 건은 취소할 수 없습니다')
    db.execute("UPDATE fax_jobs SET status='cancelled' WHERE id=%s", (job_id,))
    _audit(user['user_id'], 'cancel', f'job={job_id}')
    return {'ok': True}


@router.post('/jobs/cancel-pending')
def cancel_all_pending(user=Depends(get_current_user)):
    """내 대기(pending/queued) 건 일괄 취소. 관리자는 전체."""
    if user['role'] == 'admin':
        r = db.execute(
            "UPDATE fax_jobs SET status='cancelled' "
            "WHERE status IN ('pending','queued') RETURNING id", returning=False)
        cnt = db.query_one(
            "SELECT COUNT(*) AS c FROM fax_jobs WHERE status='cancelled'")
    else:
        db.execute(
            "UPDATE fax_jobs SET status='cancelled' "
            "WHERE status IN ('pending','queued') AND user_id=%s",
            (user['user_id'],))
    _audit(user['user_id'], 'cancel_all', 'pending')
    return {'ok': True, 'message': '대기 건이 취소되었습니다'}


@router.post('/jobs/sync')
def sync_job_status(user=Depends(get_current_user)):
    """
    팩스 서버에서 발송 최종 상태를 받아 queued→sent/failed 갱신.
    팩스 서버가 개별 job 상태를 지원하면 동기화, 아니면 전체 상태 기반 추정.
    """
    # 내 대기 건들
    where = '' if user['role'] == 'admin' else 'AND user_id=%s'
    params = () if user['role'] == 'admin' else (user['user_id'],)
    pending = db.query(
        f"SELECT id, server_job_id FROM fax_jobs "
        f"WHERE status IN ('queued','pending','sending') {where} "
        f"ORDER BY id DESC LIMIT 200", params)

    updated = {'sent': 0, 'failed': 0, 'unchanged': 0}
    server_up = faxclient.server_available()
    if not server_up:
        return {'ok': False, 'message': '팩스 서버 연결 안됨', 'updated': updated}

    for job in pending:
        sid = job.get('server_job_id')
        new_status = None
        if sid:
            info = faxclient.get_job_status(sid)
            if info:
                s = (info.get('status') or '').lower()
                if s in ('sent', 'success', 'ok', 'delivered'):
                    new_status = 'sent'
                elif s in ('failed', 'error', 'fail'):
                    new_status = 'failed'
        if new_status == 'sent':
            db.execute("UPDATE fax_jobs SET status='sent', sent_at=%s WHERE id=%s",
                       (datetime.now(), job['id']))
            updated['sent'] += 1
        elif new_status == 'failed':
            db.execute("UPDATE fax_jobs SET status='failed' WHERE id=%s", (job['id'],))
            updated['failed'] += 1
        else:
            updated['unchanged'] += 1
    return {'ok': True, 'updated': updated,
            'message': f"동기화 완료: 전송 {updated['sent']}, 실패 {updated['failed']}, 대기유지 {updated['unchanged']}"}


@router.post('/jobs/resend-failed')
def resend_all_failed(user=Depends(get_current_user)):
    """실패한 내 발송 일괄 재전송."""
    where = '' if user['role'] == 'admin' else 'AND user_id=%s'
    params = () if user['role'] == 'admin' else (user['user_id'],)
    failed = db.query(
        f"SELECT id, to_number, file_path FROM fax_jobs "
        f"WHERE status='failed' {where} LIMIT 100", params)
    resent = 0
    for job in failed:
        if not job['file_path'] or not os.path.isfile(job['file_path']):
            continue
        try:
            resp = faxclient.send_fax(job['to_number'], job['file_path'], async_mode=True)
            db.execute(
                "UPDATE fax_jobs SET status='queued', server_job_id=%s, "
                "retry_count=retry_count+1 WHERE id=%s",
                (str(resp.get('job_id', '')), job['id']))
            resent += 1
        except faxclient.FaxServerError:
            continue
    return {'ok': True, 'resent': resent, 'message': f'{resent}건 재전송 요청됨'}


@router.delete('/jobs/cleanup')
def cleanup_old_jobs(days: int = 30, user=Depends(require_admin)):
    """오래된 발송 이력 정리 (관리자). 기본 30일 이전."""
    db.execute(
        "DELETE FROM fax_jobs WHERE created_at < CURRENT_DATE - %s::int "
        "AND status IN ('sent','failed','cancelled')", (days,))
    _audit(user['user_id'], 'cleanup', f'jobs older than {days}d')
    return {'ok': True, 'message': f'{days}일 이전 완료 건 정리됨'}


@router.get('/jobs/stats-quick')
def jobs_quick_stats(user=Depends(get_current_user)):
    """발송 상태별 건수 (관리 화면용)."""
    where = '' if user['role'] in ('admin', 'manager') else 'WHERE user_id=%s'
    params = () if user['role'] in ('admin', 'manager') else (user['user_id'],)
    rows = db.query(
        f"SELECT status, COUNT(*) AS c FROM fax_jobs {where} GROUP BY status", params)
    return {r['status']: r['c'] for r in rows}


@router.get('/retry-queue')
def retry_queue(user=Depends(get_current_user)):
    """재시도 대기 중인 건 조회."""
    where = '' if user['role'] in ('admin', 'manager') else 'AND user_id=%s'
    params = () if user['role'] in ('admin', 'manager') else (user['user_id'],)
    rows = db.query(
        f"SELECT id, to_number, to_name, retry_count, max_retries, "
        f"last_error_code, next_retry_at, result_text FROM fax_jobs "
        f"WHERE status='retry_wait' {where} ORDER BY next_retry_at LIMIT 100", params)
    return rows


@router.post('/retry-now')
def retry_now(user=Depends(require_admin)):
    """재시도 워커를 즉시 한 번 실행 (관리자). 대기 시간 무시하고 처리."""
    from ..core import retry_worker
    # 대기 시간 무시: 모든 retry_wait의 next_retry_at을 now로
    db.execute("UPDATE fax_jobs SET next_retry_at=now() WHERE status='retry_wait'")
    result = retry_worker.run_once()
    return {'ok': True, 'processed': result,
            'message': f"재시도 {result['retried']}건 처리"}


# ==================== 발송 결과 수신 웹훅 ====================
class SendResultBody(BaseModel):
    """팩스 서버가 발송 완료(성공/실패) 시 보내는 결과.
    ※ 내부 통신용 - 운영 시 IP 화이트리스트/토큰 인증 권장."""
    server_job_id: str                      # 발송 시 앱이 받은 server_job_id
    success: bool                           # 전송 성공 여부
    pages_sent: Optional[int] = None        # 실제 전송된 페이지 수
    pages_total: Optional[int] = None       # 전체 페이지 수
    result_text: Optional[str] = None       # 실패 원인 텍스트 (fax_result_text)
    result_code: Optional[str] = None       # spandsp T30_ERR 코드 등
    hangup_cause: Optional[str] = None       # Q.850 끊김 원인
    diag_category: Optional[str] = None      # faxdiag 분류 (T38_NEGOTIATION 등)
    t38_used: Optional[bool] = None
    ecm_used: Optional[bool] = None          # ECM(오류정정) 사용 여부
    bad_rows: Optional[int] = None           # ECM 재전송된 손상 행 수 (품질 지표)
    transfer_rate: Optional[str] = None
    # ── 엔진 호환 확장 필드 (XMS 엔진 등이 추가로 보낼 수 있음) ──
    retryable: Optional[bool] = None         # 엔진이 판단한 재시도 가능 여부 (힌트)
    sip_code: Optional[str] = None           # SIP 응답 코드 (486, 404 등)


# faxdiag 카테고리 → 앱 예외 클래스 매핑 (팩스 서버 분류와 정렬)
_DIAG_CATEGORY_MAP = {
    'T38_NEGOTIATION': errors.NegotiationFailed,
    'E1_LAYER': errors.CarrierLost,          # E1 물리계층 문제 → 회선/반송파 계열
    'DISCONNECT_MID': errors.TransmissionInterrupted,
    'TRAINING': errors.TrainingFailed,
    'INCOMPATIBLE': errors.RemoteIncompatible,
    'PARTIAL': errors.PartialTransmission,
}


def _resolve_failure_exc(body: 'SendResultBody'):
    """실패 원인을 '단순 전송 에러'로 뭉뚱그리지 않고 최대한 구체적으로 분류.
    우선순위: result_code(정확) → result_text(자연어) → diag_category
             → hangup_cause → 최후의 일반 전송 에러.
    """
    ctx = {}
    if body.result_code:
        ctx['result_code'] = body.result_code
    if body.hangup_cause:
        ctx['hangup_cause'] = body.hangup_cause
    if body.diag_category:
        ctx['diag_category'] = body.diag_category

    # 1순위: spandsp 코드(T30_ERR_*)가 있으면 가장 정확
    if body.result_code:
        exc = errors.from_server_result(body.result_code)
        if exc.code != 'TRANSMISSION':   # 일반 전송에러로 안 떨어졌으면 채택
            exc.context.update(ctx)
            return exc

    # 2순위: 자연어 원인 텍스트 (예: "The T.38 negotiation failed")
    if body.result_text:
        exc = errors.from_server_result(body.result_text)
        if exc.code != 'TRANSMISSION':
            exc.context.update(ctx)
            return exc

    # 3순위: faxdiag 카테고리로 분류
    if body.diag_category:
        cls = _DIAG_CATEGORY_MAP.get(body.diag_category.upper())
        if cls:
            exc = cls(context=ctx)
            return exc

    # 4순위: hangup_cause로 분류 (Q.850)
    if body.hangup_cause:
        exc = errors.from_server_result(body.hangup_cause)
        if exc.code != 'TRANSMISSION':
            exc.context.update(ctx)
            return exc

    # 5순위(최후): 그래도 못 잡으면 원인 텍스트를 detail에 담아 일반 전송에러
    #   - 단, detail을 반드시 채워서 "왜 실패했는지" 추적 가능하게
    detail = body.result_text or body.result_code or body.hangup_cause or 'unknown failure'
    return errors.TransmissionError(detail=detail, context=ctx)


def _verify_webhook(request):
    """webhook 인증: IP 화이트리스트 + 선택적 토큰.
    엔진→앱 내부 통신이므로 외부에서 위조 결과를 주입하지 못하게 방어.
    """
    # 1. IP 확인 (기본: localhost만. config로 확장 가능)
    client_ip = request.client.host if request.client else ''
    allowed = [ip.strip() for ip in config.WEBHOOK_ALLOW_IPS.split(',') if ip.strip()]
    if client_ip not in allowed:
        raise errors.ValidationError(
            detail=f'webhook 미허용 IP: {client_ip}',
            context={'client_ip': client_ip})
    # 2. 토큰 확인 (설정된 경우만)
    if config.WEBHOOK_TOKEN:
        token = request.headers.get('X-Fax-Token', '')
        if token != config.WEBHOOK_TOKEN:
            raise errors.ValidationError(detail='webhook 토큰 불일치')


@router.post('/webhook/result')
def send_result_webhook(body: SendResultBody, request: Request):
    """팩스 서버가 발송 완료 후 결과를 알려주는 웹훅.
    성공/실패/부분전송을 구분해 job 상태를 갱신하고, 실패는 정확히 분류·재시도.
    엔진이 direction='receive'로 보내면 수신 통지로 처리 (발송 job 아님).
    """
    # 인증: 위조 결과 주입 방어 (IP + 토큰)
    _verify_webhook(request)

    # 1. server_job_id로 해당 job 조회
    job = db.query_one(
        'SELECT id, user_id, pages, retry_count, max_retries, status '
        'FROM fax_jobs WHERE server_job_id=%s ORDER BY id DESC LIMIT 1',
        (body.server_job_id,))
    if not job:
        # 매칭되는 job이 없어도 웹훅 자체는 정상 응답 (팩스서버 재시도 폭주 방지)
        return {'ok': False, 'reason': 'job_not_found', 'server_job_id': body.server_job_id}

    job_id = job['id']

    # 이미 종료된 job이면 중복 웹훅 무시 (멱등성)
    if job['status'] in ('sent', 'failed', 'cancelled'):
        return {'ok': True, 'job_id': job_id, 'status': job['status'], 'note': 'already_finalized'}

    # 진단 정보는 성공/실패 무관하게 기록
    total = body.pages_total if body.pages_total is not None else job.get('pages')
    _save_diag_fields(job_id, body)

    # 2. 성공 처리 (단, 페이지 누락 확인)
    if body.success:
        sent = body.pages_sent
        if sent is not None and total and int(sent) < int(total):
            # 성공이라지만 일부 페이지만 전송됨 → 부분 전송으로 처리
            exc = errors.PartialTransmission(
                sent_pages=int(sent), total_pages=int(total),
                context={'job_id': job_id})
            return _apply_failure(job, exc, extra_pages=sent)
        # 완전 전송 성공
        db.execute(
            "UPDATE fax_jobs SET status='sent', sent_at=now(), pages_sent=%s WHERE id=%s",
            (sent if sent is not None else total, job_id))
        return {'ok': True, 'job_id': job_id, 'status': 'sent'}

    # 3. 실패 처리 - 원인을 구체적으로 분류
    exc = _resolve_failure_exc(body)
    # 엔진이 '영구실패'로 명시(retryable=False)하면 앱도 재시도 안 함.
    #   엔진은 SIP 404(없는번호)/파일오류 등을 프로토콜 레벨에서 가장 정확히 앎.
    #   단, 엔진이 '재시도 가능'이라 해도 앱 정책이 우선(과도 재시도 방지).
    engine_permanent = (body.retryable is False)
    return _apply_failure(job, exc, extra_pages=body.pages_sent,
                          engine_permanent=engine_permanent)


def _save_diag_fields(job_id, body: 'SendResultBody'):
    """진단 참고 필드를 job에 저장 (실패 분석용)."""
    db.execute(
        "UPDATE fax_jobs SET diag_category=%s, t38_used=%s, transfer_rate=%s, "
        "hangup_cause=%s, ecm_used=%s, bad_rows=%s WHERE id=%s",
        (body.diag_category, body.t38_used, body.transfer_rate,
         (body.hangup_cause or '')[:60] or None, body.ecm_used, body.bad_rows, job_id))


def _apply_failure(job, exc, extra_pages=None, engine_permanent=False):
    """실패 예외를 받아 재시도 예약 또는 실패 확정 + 오류 로그 기록.
    engine_permanent=True면 엔진이 영구실패로 판단한 것 → 재시도 안 함.
    """
    job_id = job['id']
    attempt = job.get('retry_count', 0) or 0
    # 재시도 페이지 수 기록
    if extra_pages is not None:
        db.execute("UPDATE fax_jobs SET pages_sent=%s WHERE id=%s", (extra_pages, job_id))

    # 엔진이 영구실패로 명시하면 앱 정책과 무관하게 재시도 중단
    should_retry = (not engine_permanent) and error_handler.RetryPolicy.should_retry(exc, attempt)
    if should_retry:
        from datetime import timedelta
        delay = error_handler.RetryPolicy.next_delay(attempt, exc)
        next_at = datetime.now() + timedelta(seconds=delay)
        db.execute(
            "UPDATE fax_jobs SET status='retry_wait', next_retry_at=%s, "
            "retry_count=retry_count+1, last_error_code=%s, result_text=%s WHERE id=%s",
            (next_at, exc.code, exc.user_message[:255], job_id))
        error_handler.log_error(exc, path='/api/fax/webhook/result',
                                user_id=job.get('user_id'), job_id=job_id)
        return {'ok': True, 'job_id': job_id, 'status': 'retry_wait',
                'error_code': exc.code, 'category': exc.category.value,
                'retry_in': delay}
    else:
        db.execute(
            "UPDATE fax_jobs SET status='failed', last_error_code=%s, result_text=%s WHERE id=%s",
            (exc.code, exc.user_message[:255], job_id))
        error_handler.log_error(exc, path='/api/fax/webhook/result',
                                user_id=job.get('user_id'), job_id=job_id)
        # 발송 실패 알림 (책임 소재 포함)
        try:
            from ..core import notify
            notify.notify_send_failure(job, exc)
        except Exception:
            pass
        return {'ok': True, 'job_id': job_id, 'status': 'failed',
                'error_code': exc.code, 'category': exc.category.value}


@router.get('/scheduled')
def list_scheduled(user=Depends(get_current_user)):
    """예약된 발송 목록 (아직 발송 안 된 것)."""
    where = '' if user['role'] in ('admin', 'manager') else 'AND user_id=%s'
    params = () if user['role'] in ('admin', 'manager') else (user['user_id'],)
    rows = db.query(
        f"SELECT id, to_number, to_name, file_name, pages, cover_used, "
        f"scheduled_at, created_at FROM fax_jobs "
        f"WHERE status='scheduled' {where} ORDER BY scheduled_at LIMIT 200", params)
    return rows


@router.post('/scheduled/{job_id}/cancel')
def cancel_scheduled(job_id: int, user=Depends(get_current_user)):
    """예약 발송 취소. 본인 것 또는 관리자만."""
    job = db.query_one("SELECT id, user_id, status FROM fax_jobs WHERE id=%s", (job_id,))
    if not job or job['status'] != 'scheduled':
        raise errors.NotFound('예약 발송')
    if user['role'] not in ('admin', 'manager') and job['user_id'] != user['user_id']:
        raise errors.PermissionDenied(detail='본인 예약만 취소할 수 있습니다')
    db.execute("UPDATE fax_jobs SET status='cancelled' WHERE id=%s", (job_id,))
    _audit(user['user_id'], 'schedule_cancel', f'job={job_id}')
    return {'ok': True, 'job_id': job_id, 'status': 'cancelled'}


@router.get('/cover-templates')
def cover_templates(user=Depends(get_current_user)):
    """사용 가능한 표지 템플릿 목록."""
    return cover_mod.TEMPLATE_LIST


@router.post('/mailmerge')
async def mailmerge_fax(
    csv_data: str = Form(...),          # CSV 텍스트: 번호,이름,제목,메모 (헤더 포함)
    cover_template: str = Form(default='standard'),
    file: UploadFile = File(...),       # 공통 본문 문서
    user=Depends(get_current_user),
):
    """대량 맞춤 발송(mail-merge): 공통 본문 + 수신자별 맞춤 표지.
    CSV 각 행이 한 수신자 - 번호별로 다른 이름/제목/메모로 표지 생성.
    CSV 형식(헤더): number,name,title,memo (number 필수, 나머지 선택)."""
    import csv as _csv
    import io as _io

    # 1. CSV 파싱
    rows = []
    try:
        reader = _csv.DictReader(_io.StringIO(csv_data))
        # 헤더 정규화 (한글/영문 허용)
        for r in reader:
            norm = {(k or '').strip().lower(): (v or '').strip() for k, v in r.items()}
            number = (norm.get('number') or norm.get('번호') or norm.get('팩스번호') or '')
            if not number:
                continue
            rows.append({
                'number': number,
                'name': norm.get('name') or norm.get('이름') or norm.get('받는사람') or '',
                'title': norm.get('title') or norm.get('제목') or '',
                'memo': norm.get('memo') or norm.get('메모') or norm.get('내용') or '',
            })
    except Exception as e:
        raise errors.ValidationError(detail=f'CSV 형식 오류: {e}')

    if not rows:
        raise errors.ValidationError(detail='발송할 수신자가 없습니다 (CSV에 number 열 필요)')
    if len(rows) > 500:
        raise errors.ValidationError(detail='한 번에 최대 500건까지 가능합니다')

    # 2. 번호 유효성 사전 검증 (하나라도 틀리면 전체 거부 - 실수 방지)
    bad = [r['number'] for r in rows if not _valid_fax_number(r['number'].strip())]
    if bad:
        raise errors.ValidationError(
            detail=f'잘못된 번호 {len(bad)}건: {", ".join(bad[:5])}' + (' 등' if len(bad) > 5 else ''))

    # 3. 공통 본문 업로드·변환 (한 번만)
    src = _save_upload(file)
    if not os.path.getsize(src):
        raise errors.EmptyFile(context={'filename': file.filename})
    try:
        body_tiff, body_pages = convert.to_fax_tiff(src, config.UPLOAD_DIR)
    except convert.ConvertError as e:
        raise errors.ConversionError(detail=f'파일 변환 실패: {e}')

    # 4. 발신자 정보 (표지용)
    me = db.query_one('SELECT display_name, fax_number FROM users WHERE id=%s', (user['user_id'],))

    # 5. 각 수신자별 맞춤 표지 + 발송
    batch_id = uuid.uuid4().hex[:16]
    results = []
    for r in rows:
        number = r['number'].strip()
        tiff, pages = body_tiff, body_pages
        cover_used = False
        # 맞춤 표지 (수신자별 이름/제목/메모)
        try:
            info = {
                'to_name': r['name'], 'to_number': number,
                'from_name': me['display_name'] if me else '',
                'from_number': me['fax_number'] if me else '',
                'title': r['title'], 'pages': body_pages, 'memo': r['memo'],
                'template': cover_template or 'standard',
            }
            tiff, pages = cover_mod.attach_cover(body_tiff, info, config.UPLOAD_DIR)
            cover_used = True
        except Exception:
            # 표지 실패해도 본문은 발송
            tiff, pages = body_tiff, body_pages

        job = db.execute(
            'INSERT INTO fax_jobs(user_id, to_number, to_name, file_name, file_path, '
            'pages, cover_used, status, batch_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (user['user_id'], number, r['name'], file.filename, tiff, pages,
             cover_used, 'pending', batch_id), returning=True)
        job_id = job['id']
        try:
            resp = faxclient.send_fax(number, tiff, async_mode=True, job_id=job_id)
            server_job = resp.get('job_id') or str(job_id)
            db.execute("UPDATE fax_jobs SET status='queued', server_job_id=%s WHERE id=%s",
                       (str(server_job), job_id))
            results.append({'number': number, 'name': r['name'], 'ok': True})
        except faxclient.FaxServerError as e:
            db.execute("UPDATE fax_jobs SET status='failed', result_text=%s WHERE id=%s",
                       (str(e)[:255], job_id))
            results.append({'number': number, 'name': r['name'], 'ok': False})

    _audit(user['user_id'], 'mailmerge', f'batch={batch_id} count={len(rows)}')
    ok_count = sum(1 for r in results if r['ok'])
    return {'ok': True, 'batch_id': batch_id, 'total': len(rows),
            'queued': ok_count, 'results': results}
