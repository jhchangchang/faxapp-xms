"""
예외 핸들러·로거 (error_handler.py)
==================================
중앙 예외 모듈(errors.py)과 짝을 이루는 처리 계층.

역할:
1. log_error()    : 예외를 error_log 테이블 + 파이썬 로거에 기록 (가시성)
2. FastAPI 예외 핸들러 등록 : 모든 FaxAppError를 일관된 JSON 응답으로 (사용자 안내)
3. 재시도 판정 헬퍼 (재시도)

이 모듈이 "세 가지 균형"의 접착제:
- 가시성: 모든 예외가 여기를 거쳐 DB+로그에 남음
- 사용자 안내: user_message가 일관된 형식으로 응답됨
- 재시도: category 기반으로 재시도 여부 판정
"""
import logging
import json

from fastapi import Request
from fastapi.responses import JSONResponse

from . import db
from .errors import FaxAppError, Category

logger = logging.getLogger('faxapp')


def log_error(exc, *, path=None, user_id=None, job_id=None):
    """
    예외를 중앙 기록. DB 실패해도 파이썬 로그에는 남김.
    반환: error_log id (실패 시 None)
    """
    # 앱 예외가 아니면 감싸서 처리
    if isinstance(exc, FaxAppError):
        code = exc.code
        category = exc.category.value
        detail = exc.detail
        context = exc.context
    else:
        code = 'UNHANDLED'
        category = Category.SYSTEM.value
        detail = f'{type(exc).__name__}: {exc}'
        context = {}

    # 파이썬 로거 (항상)
    log_line = f'[{code}/{category}] {detail}'
    if category in (Category.SYSTEM.value, 'UNHANDLED'):
        logger.error(log_line, extra={'context': context})
    else:
        logger.warning(log_line)

    # DB 기록 (실패해도 앱은 계속)
    try:
        row = db.execute(
            'INSERT INTO error_log(code, category, detail, context, path, user_id, job_id) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (code, category, detail[:2000], json.dumps(context, ensure_ascii=False),
             path, user_id, job_id),
            returning=True)
        return row['id'] if row else None
    except Exception as e:
        logger.error(f'error_log 기록 실패: {e}')
        return None


def register_handlers(app):
    """FastAPI에 중앙 예외 핸들러 등록."""

    @app.exception_handler(FaxAppError)
    async def faxapp_error_handler(request: Request, exc: FaxAppError):
        # 사용자 정보 추출 (있으면)
        user_id = None
        try:
            from ..deps import COOKIE_NAME
            from .security import get_session
            token = request.cookies.get(COOKIE_NAME)
            if token:
                sess = get_session(token)
                user_id = sess.get('user_id') if sess else None
        except Exception:
            pass

        # 기록
        log_error(exc, path=str(request.url.path), user_id=user_id,
                  job_id=exc.context.get('job_id'))

        # 일관된 응답
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        # 예상 못한 예외 - 기록하고 일반 메시지
        log_error(exc, path=str(request.url.path))
        return JSONResponse(
            status_code=500,
            content={'error': True, 'code': 'INTERNAL',
                     'message': '처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.',
                     'retryable': True})


# ==================== 재시도 정책 ====================
class RetryPolicy:
    """
    재시도 정책. category와 시도 횟수로 재시도 여부·대기시간 결정.
    특정 예외 코드는 CODE_POLICY로 세밀하게 오버라이드.
    """
    MAX_RETRIES = {
        Category.RETRYABLE: 3,
        Category.USER: 0,        # 사용자 잘못은 재시도 안 함
        Category.PERMANENT: 0,   # 영구 실패는 재시도 안 함
        Category.SYSTEM: 1,      # 시스템은 1회만 (일시적일 수 있으니)
        Category.AUTH: 0,
    }
    # 재시도 대기(초) - 지수 백오프
    BACKOFF = [30, 120, 300]  # 30초, 2분, 5분

    # 코드별 세밀 정책 (실패 유형에 맞게 조정)
    #   {'max': 최대 재시도, 'backoff': [대기초 리스트]}
    CODE_POLICY = {
        # 통화중 - 상대가 곧 끊을 수 있으니 길게 여러 번
        'LINE_BUSY': {'max': 5, 'backoff': [60, 180, 300, 600, 900]},
        # 무응답 - 부재중일 가능성, 적당히
        'NO_ANSWER': {'max': 3, 'backoff': [120, 300, 600]},
        # 협상/트레이닝 실패 - 회선 순간 문제, 짧게 여러 번
        'NEGOTIATION_FAILED': {'max': 4, 'backoff': [20, 60, 120, 300]},
        'TRAINING_FAILED': {'max': 4, 'backoff': [20, 60, 120, 300]},
        # 반송파 상실 - 짧게 재시도
        'CARRIER_LOST': {'max': 3, 'backoff': [30, 90, 180]},
        # 부분 전송 - 곧바로 재시도 가치, 짧게
        'PARTIAL_TX': {'max': 3, 'backoff': [30, 90, 180]},
        # 팩스서버 일시 장애 - 조금 더 기다렸다 재시도
        'FAX_SERVER_TIMEOUT': {'max': 3, 'backoff': [60, 180, 420]},
        'FAX_SERVER_DOWN': {'max': 3, 'backoff': [60, 180, 420]},
    }

    @classmethod
    def _policy_for(cls, exc):
        """예외에 맞는 (max, backoff) 반환. 코드별 정책 우선."""
        code = getattr(exc, 'code', None)
        if code and code in cls.CODE_POLICY:
            p = cls.CODE_POLICY[code]
            return p['max'], p['backoff']
        # 기본: category 기반
        if isinstance(exc, FaxAppError):
            return cls.MAX_RETRIES.get(exc.category, 0), cls.BACKOFF
        return 1, cls.BACKOFF

    @classmethod
    def should_retry(cls, exc, attempt):
        """재시도해야 하나? exc: 예외, attempt: 현재까지 시도 횟수."""
        if not isinstance(exc, FaxAppError):
            return attempt < 1
        max_r, _ = cls._policy_for(exc)
        return attempt < max_r

    @classmethod
    def next_delay(cls, attempt, exc=None):
        """다음 재시도까지 대기(초). exc 주면 코드별 백오프 적용."""
        backoff = cls.BACKOFF
        if exc is not None:
            _, backoff = cls._policy_for(exc)
        idx = min(attempt, len(backoff) - 1)
        return backoff[idx]
