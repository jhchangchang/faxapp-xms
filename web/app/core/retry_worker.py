"""
재시도 워커 (retry_worker.py)
============================
백그라운드에서 주기적으로 재시도 대상을 찾아 자동 재전송.
중앙 예외 체계(errors.py)의 RetryPolicy와 연동.

동작 흐름:
1. 주기적으로(기본 30초) 재시도 대상 조회
   - status='retry_wait' 이고 next_retry_at <= now() 인 건
2. 각 건에 대해:
   - 팩스 서버에 재전송 요청
   - 성공 → status='sent'
   - 실패 → RetryPolicy로 재시도 여부 판정
     - 재시도 가능 & 횟수 남음 → next_retry_at 갱신(백오프), retry_count++
     - 아니면 → status='failed' 확정
3. 상태 동기화도 겸함: queued 건의 서버 상태 확인

상태 흐름:
  pending → queued → (서버 결과)
                     ├ sent (완료)
                     ├ retry_wait (재시도 대기) → [워커] → queued/sent/failed
                     └ failed (영구 실패)

안전장치:
- 최대 재시도 횟수 초과 시 무조건 failed
- 한 번에 처리하는 건수 제한 (기본 20)
- 워커 예외가 나도 다음 주기에 계속 (죽지 않음)
- 앱 시작 시 lifespan에서 워커 시작, 종료 시 정리
"""
import asyncio
import logging
import os as _os
from datetime import datetime, timedelta

from . import db, faxclient, errors, error_handler, convert, config
from .error_handler import RetryPolicy

logger = logging.getLogger('faxapp.retry')

# 설정
POLL_INTERVAL = 30       # 재시도 대상 조회 주기(초)
# 파일 자동 정리 (디스크 보호)
FILE_RETAIN_DAYS = int(_os.environ.get('FAXAPP_FILE_RETAIN_DAYS', '90'))   # 보관 기간
FILE_CLEANUP_EVERY = int(_os.environ.get('FAXAPP_FILE_CLEANUP_EVERY', '1000'))  # N주기마다
BATCH_SIZE = 20          # 한 주기에 처리할 최대 건수
STUCK_MINUTES = 5        # queued로 이 시간 이상 멈춘 건 동기화 시도
# queued로 이 시간 이상 남으면 자동 완료 처리 (통지 유실 대비 안전장치).
# 팩스는 통상 수 분 내 종료되므로, 이 시간 초과 = 통지만 유실된 것으로 간주.
AUTO_FINALIZE_MINUTES = int(_os.environ.get('FAXAPP_AUTO_FINALIZE_MIN', '3'))
# ── 이어보내기(부분 재전송) 정책 ──
RESUME_MIN_TOTAL = int(_os.environ.get('FAXAPP_RESUME_MIN_TOTAL', '5'))   # 이하는 전체 재전송
RESUME_MIN_PAGES = int(_os.environ.get('FAXAPP_RESUME_MIN_PAGES', '10'))  # 이상일 때 이어보내기 검토
RESUME_MIN_RATIO = float(_os.environ.get('FAXAPP_RESUME_MIN_RATIO', '0.8'))  # 이 비율 이상 전송 시

_worker_task = None
_running = False


def _mark_retry_wait(job_id, exc, attempt):
    """재시도 대기 상태로 전환 + 다음 재시도 시각 계산."""
    delay = RetryPolicy.next_delay(attempt)
    next_at = datetime.now() + timedelta(seconds=delay)
    code = exc.code if isinstance(exc, errors.FaxAppError) else 'UNKNOWN'
    db.execute(
        "UPDATE fax_jobs SET status='retry_wait', next_retry_at=%s, "
        "last_error_code=%s, result_text=%s WHERE id=%s",
        (next_at, code, str(exc)[:255], job_id))
    logger.info(f'job {job_id}: 재시도 예약 (#{attempt+1}, {delay}초 후, 사유 {code})')


def _mark_failed(job_id, reason):
    """영구 실패 확정."""
    db.execute(
        "UPDATE fax_jobs SET status='failed', result_text=%s WHERE id=%s",
        (str(reason)[:255], job_id))
    logger.info(f'job {job_id}: 영구 실패 확정 ({reason})')


def _mark_sent(job_id):
    db.execute("UPDATE fax_jobs SET status='sent', sent_at=%s WHERE id=%s",
               (datetime.now(), job_id))
    logger.info(f'job {job_id}: 재시도 성공 → 완료')


def _send_due_scheduled():
    """예약 시각이 도래한 scheduled 건을 발송 큐로 전환. 처리 건수 반환."""
    import os
    sent = 0
    due = db.query(
        "SELECT id, to_number, file_path, scheduled_at FROM fax_jobs "
        "WHERE status='scheduled' AND scheduled_at IS NOT NULL "
        "AND scheduled_at <= now() ORDER BY scheduled_at LIMIT %s", (BATCH_SIZE,))
    for job in due:
        job_id = job['id']
        # 파일 존재 확인 (예약 사이에 파일이 지워졌을 수 있음)
        if not job.get('file_path') or not os.path.isfile(job['file_path']):
            _mark_failed(job_id, '예약 발송 실패: 변환 파일이 없습니다')
            continue
        try:
            resp = faxclient.send_fax(job['to_number'], job['file_path'],
                                      async_mode=True, job_id=job_id)
            server_job = resp.get('job_id') or str(job_id)
            db.execute(
                "UPDATE fax_jobs SET status='queued', server_job_id=%s WHERE id=%s",
                (str(server_job), job_id))
            logger.info(f'job {job_id}: 예약 발송 실행 (예약 {job.get("scheduled_at")})')
            sent += 1
        except faxclient.FaxServerError as e:
            # 팩스 서버가 안 되면 재시도 대기로 (예약은 이미 도래했으므로)
            exc = errors.FaxServerError(detail=str(e), context={'job_id': job_id})
            if RetryPolicy.should_retry(exc, 0):
                _mark_retry_wait(job_id, exc, 0)
            else:
                _mark_failed(job_id, f'예약 발송 실패: {e}')
            error_handler.log_error(exc, path='retry_worker.scheduled', job_id=job_id)
    return sent


def _resume_plan(job):
    """이어보내기(남은 페이지만 재전송) 여부와 시작 페이지 결정.

    정책 (수신측 안전 + 절감 균형):
      - 전체 5장 이하: 이어보내기 안 함 (처음부터 전체). 짧은 문서는 재전송 부담 적음.
      - 전체 RESUME_MIN_PAGES(10)장 이상 AND 전송률 RESUME_MIN_RATIO(80%) 이상:
        → 이어보내기 (sent_pages+1 부터)
      - 그 외: 전체 재전송 (앞부분 상당량 유실 시 이어보내기는 위험)

    반환: (do_resume: bool, start_page: int, total: int, sent: int)
    """
    total = job.get('pages') or 0
    sent = job.get('pages_sent') or 0
    if total <= 0 or sent <= 0:
        return (False, 1, total, sent)
    if total <= RESUME_MIN_TOTAL:
        return (False, 1, total, sent)          # 짧은 문서는 전체
    if sent >= total:
        return (False, 1, total, sent)          # 이미 다 보냄 (부분전송 아님)
    ratio = sent / total
    if total >= RESUME_MIN_PAGES and ratio >= RESUME_MIN_RATIO:
        return (True, sent + 1, total, sent)    # 이어보내기
    return (False, 1, total, sent)              # 그 외 전체


def _make_resume_file(job, start_page):
    """원본 파일에서 start_page 이후만 추출한 이어보내기용 파일 생성.
    실패하면 None 반환 (호출측이 전체 재전송으로 폴백)."""
    import os, uuid
    src = job.get('file_path')
    if not src or not os.path.isfile(src):
        return None
    out_dir = os.path.dirname(src)
    out = os.path.join(out_dir, f'resume_{job["id"]}_{uuid.uuid4().hex[:8]}.tif')
    try:
        low = src.lower()
        if low.endswith('.pdf'):
            convert.slice_pdf_pages(src, start_page, out)
        else:
            convert.slice_tiff_pages(src, start_page, out)
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    except Exception as e:
        logger.warning(f'job {job["id"]} 이어보내기 파일 생성 실패(전체 재전송으로 폴백): {e}')
    return None


def _retry_one(job):
    """한 건 재전송 시도."""
    job_id = job['id']
    attempt = job.get('retry_count', 0) or 0
    max_r = job.get('max_retries', 3) or 3

    # 파일 존재 확인
    import os
    if not job.get('file_path') or not os.path.isfile(job['file_path']):
        _mark_failed(job_id, '재전송 실패: 변환 파일이 없습니다')
        return

    # 최대 횟수 초과 → 실패 확정
    if attempt >= max_r:
        _mark_failed(job_id, f'최대 재시도({max_r}회) 초과')
        return

    try:
        # ── 이어보내기 판단 ──
        # 부분 전송(예: 50장 중 48장)이었다면 남은 페이지만 재전송하여
        # 통신료·시간·수신측 토너 절약. 조건 미달 시 전체 재전송.
        send_path = job['file_path']
        resume_note = ''
        do_resume, start_page, total, sent = _resume_plan(job)
        if do_resume:
            resume_file = _make_resume_file(job, start_page)
            if resume_file:
                send_path = resume_file
                resume_note = f' [이어보내기 {start_page}~{total}p, {sent}p 완료분 제외]'
                logger.info(f'job {job_id}: 이어보내기 - {start_page}페이지부터 재전송')

        resp = faxclient.send_fax(job['to_number'], send_path, async_mode=True)
        server_job = resp.get('job_id') or resp.get('id', '')
        db.execute(
            "UPDATE fax_jobs SET status='queued', server_job_id=%s, "
            "retry_count=retry_count+1, next_retry_at=NULL WHERE id=%s",
            (str(server_job), job_id))
        logger.info(f'job {job_id}: 재전송 요청됨 (#{attempt+1}){resume_note}')
    except faxclient.FaxServerError as e:
        # 서버가 여전히 안 되면 다시 재시도 예약 (횟수 소진까지)
        exc = errors.FaxServerError(detail=str(e), context={'job_id': job_id})
        if RetryPolicy.should_retry(exc, attempt + 1):
            _mark_retry_wait(job_id, exc, attempt + 1)
        else:
            _mark_failed(job_id, f'재시도 소진: {e}')
        error_handler.log_error(exc, path='retry_worker', job_id=job_id)


def _sync_queued():
    """queued로 오래 멈춘 건의 서버 상태를 확인해 갱신."""
    if not faxclient.server_available():
        return
    stuck = db.query(
        "SELECT id, server_job_id, retry_count, max_retries, pages FROM fax_jobs "
        "WHERE status='queued' AND server_job_id IS NOT NULL "
        "AND created_at < now() - interval '%s minutes' LIMIT %s",
        (STUCK_MINUTES, BATCH_SIZE))
    for job in stuck:
        info = faxclient.get_job_status(job['server_job_id'])
        if not info:
            continue
        result = info.get('status', '')
        verdict = errors.classify_result(result)
        if verdict == 'sent':
            # 성공으로 보고됐어도 전송 페이지 수를 확인 (부분 전송 감지)
            sent_pages = info.get('pages_sent', info.get('transmitted_pages'))
            total_pages = job.get('pages')
            if (sent_pages is not None and total_pages
                    and int(sent_pages) < int(total_pages)):
                # 일부 페이지만 전송됨 → 부분 전송 예외로 처리
                exc = errors.PartialTransmission(
                    sent_pages=int(sent_pages), total_pages=int(total_pages),
                    context={'job_id': job['id']})
                attempt = job.get('retry_count', 0) or 0
                if RetryPolicy.should_retry(exc, attempt):
                    _mark_retry_wait(job['id'], exc, attempt)
                else:
                    _mark_failed(job['id'], exc.user_message)
            else:
                _mark_sent(job['id'])
        elif verdict == 'failed':
            # 영구 실패로 판정된 경우
            exc = errors.from_server_result(result, {'job_id': job['id']})
            if exc.category == errors.Category.PERMANENT:
                _mark_failed(job['id'], exc.user_message)
            else:
                # 일시적이면 재시도 큐로
                attempt = job.get('retry_count', 0) or 0
                if RetryPolicy.should_retry(exc, attempt):
                    _mark_retry_wait(job['id'], exc, attempt)
                else:
                    _mark_failed(job['id'], exc.user_message)


def _auto_finalize_stale():
    """webhook·폴링 모두 실패해도 오래 멈춘 queued를 자동 정리하는 안전장치.

    배경: 일부 엔진(FreeSWITCH 게이트웨이 경유 등)은 팩스 전송에 성공해도
    완료 통지(webhook)가 job_id 유실로 앱에 도달하지 못하는 경우가 있음.
    이때 queued가 영원히 남아 화면이 지저분해짐.

    정책: 팩스는 통상 수 분 내 종료되므로, AUTO_FINALIZE_MINUTES 이상
    queued로 남은 건은 '전송은 됐으나 통지만 유실된 것'으로 간주하여
    완료(sent) 처리한다. (실패로 처리하지 않는 이유: 실제로는 대부분 성공)

    ※ 엔진 조회에 의존하지 않으므로 어떤 엔진에서도 동작.
    """
    try:
        stale = db.query(
            "SELECT id FROM fax_jobs WHERE status='queued' "
            "AND created_at < now() - interval '%s minutes' LIMIT %s",
            (AUTO_FINALIZE_MINUTES, BATCH_SIZE))
        for job in (stale or []):
            db.execute(
                "UPDATE fax_jobs SET status='sent', sent_at=%s, "
                "result_text=COALESCE(result_text,'전송 완료 (자동 확정)') WHERE id=%s",
                (datetime.now(), job['id']))
            logger.info(f"job {job['id']}: queued {AUTO_FINALIZE_MINUTES}분 초과 → 자동 완료 처리")
        return len(stale or [])
    except Exception as e:
        logger.error(f'자동 완료 처리 오류: {e}')
        return 0


def run_once():
    """한 주기 실행 (테스트·수동 호출 가능)."""
    processed = {'retried': 0, 'synced': 0, 'scheduled': 0}
    # 0. 예약 시각이 된 건 발송
    try:
        processed['scheduled'] = _send_due_scheduled()
    except Exception as e:
        logger.error(f'예약 발송 처리 오류: {e}')
    # 1. 재시도 대기 건 처리
    try:
        due = db.query(
            "SELECT id, to_number, file_path, retry_count, max_retries, "
            "pages, pages_sent "
            "FROM fax_jobs WHERE status='retry_wait' "
            "AND (next_retry_at IS NULL OR next_retry_at <= now()) "
            "ORDER BY next_retry_at LIMIT %s", (BATCH_SIZE,))
        for job in due:
            _retry_one(job)
            processed['retried'] += 1
    except Exception as e:
        logger.error(f'재시도 처리 오류: {e}')
    # 2. 멈춘 queued 동기화 (엔진 조회)
    try:
        _sync_queued()
        processed['synced'] += 1
    except Exception as e:
        logger.error(f'큐 동기화 오류: {e}')
    # 3. 오래된 queued 자동 완료 (통지 유실 대비 안전장치)
    try:
        processed['finalized'] = _auto_finalize_stale()
    except Exception as e:
        logger.error(f'자동 완료 오류: {e}')
    return processed


def _cleanup_old_files():
    """오래된 팩스 파일 자동 정리 (디스크 보호).

    장기 운영 시 수신/업로드 파일이 쌓여 디스크가 차는 것을 방지.
    - FILE_RETAIN_DAYS(기본 90일) 초과한 파일 삭제
    - 단, DB에 아직 참조된(재전송 대기 등) 파일은 보존
    - 삭제 실패는 무시 (다음 주기에 재시도)

    반환: 삭제한 파일 수.
    """
    import os, time, glob
    retain_sec = FILE_RETAIN_DAYS * 86400
    now = time.time()
    deleted = 0
    # DB가 아직 참조하는 파일 경로 수집 (재전송 대기 등 - 지우면 안 됨)
    try:
        rows = db.query(
            "SELECT DISTINCT file_path FROM fax_jobs "
            "WHERE status IN ('queued','retry_wait','scheduled','sending','pending') "
            "AND file_path IS NOT NULL")
        in_use = {r['file_path'] for r in (rows or [])}
    except Exception:
        in_use = set()

    for base in (getattr(config, 'UPLOAD_DIR', None),
                 getattr(config, 'RECV_DIR', None),
                 getattr(config, 'FAX_DIR', None)):
        if not base or not os.path.isdir(base):
            continue
        try:
            for path in glob.glob(os.path.join(base, '*')):
                if not os.path.isfile(path):
                    continue
                if path in in_use:
                    continue  # DB가 아직 씀 (재전송 등) → 보존
                try:
                    if now - os.path.getmtime(path) > retain_sec:
                        os.remove(path)
                        deleted += 1
                except Exception:
                    pass  # 개별 파일 실패 무시
        except Exception as e:
            logger.warning(f'파일 정리 오류({base}): {e}')
    if deleted:
        logger.info(f'오래된 파일 {deleted}개 정리 ({FILE_RETAIN_DAYS}일 초과)')
    return deleted


async def _loop():
    """비동기 워커 루프."""
    global _running
    _running = True
    logger.info(f'재시도 워커 시작 (주기 {POLL_INTERVAL}초)')
    _cleanup_tick = 0
    _file_tick = 0
    while _running:
        try:
            # 블로킹 DB 작업을 스레드에서
            await asyncio.to_thread(run_once)
            # 주기적으로 만료 세션 정리 (메모리 누수 방지). 약 10주기마다.
            _cleanup_tick += 1
            if _cleanup_tick >= 10:
                _cleanup_tick = 0
                try:
                    from . import security
                    n = security.cleanup_expired()
                    if n:
                        logger.info(f'만료 세션 {n}개 정리')
                except Exception as e:
                    logger.warning(f'세션 정리 오류(무시): {e}')
            # 파일 정리는 더 드물게 (파일수 많으면 부담) - 약 1000주기마다
            _file_tick += 1
            if _file_tick >= FILE_CLEANUP_EVERY:
                _file_tick = 0
                try:
                    await asyncio.to_thread(_cleanup_old_files)
                except Exception as e:
                    logger.warning(f'파일 정리 오류(무시): {e}')
        except Exception as e:
            logger.error(f'워커 주기 오류(계속 진행): {e}')
        await asyncio.sleep(POLL_INTERVAL)
    logger.info('재시도 워커 종료')


def start():
    """워커 시작 (lifespan에서 호출)."""
    global _worker_task
    if _worker_task is None:
        try:
            loop = asyncio.get_event_loop()
            _worker_task = loop.create_task(_loop())
        except RuntimeError:
            logger.warning('이벤트 루프 없음 - 워커 미시작')


def stop():
    """워커 정지."""
    global _running, _worker_task
    _running = False
    if _worker_task:
        _worker_task.cancel()
        _worker_task = None
