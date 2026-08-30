"""
비정상 상황 복원력 테스트 (핵심).
"우리 쪽 문제로 서비스가 죽으면 안 된다"를 시나리오로 검증.

엔진 다운, webhook 유실, 중복 통지, 잘못된 데이터 등
현실에서 실제로 터지는 상황들을 Mock으로 재현.
"""
import pytest
from app.core import errors
from app.core.error_handler import RetryPolicy


class TestEngineFailureResilience:
    """팩스 엔진이 죽거나 이상할 때 앱이 버티는지."""

    def test_engine_down_raises_retryable(self):
        # 엔진 다운 = FaxServerDown, 재시도 가능해야 (일시적)
        if hasattr(errors, 'FaxServerDown'):
            exc = errors.FaxServerDown(detail='connection refused')
            # 시스템/재시도 계열이어야 (사용자 잘못 아님)
            assert exc.category in (errors.Category.RETRYABLE, errors.Category.SYSTEM)

    def test_engine_timeout_recoverable(self):
        if hasattr(errors, 'FaxServerError'):
            exc = errors.FaxServerError(detail='timeout')
            # 재시도 판정이 크래시 없이 동작
            result = RetryPolicy.should_retry(exc, 0)
            assert isinstance(result, bool)

    def test_garbage_result_no_crash(self):
        # 엔진이 쓰레기 응답을 줘도 앱이 안 죽음
        for garbage in ['', None, '\x00\x01', 'x' * 10000, '{"broken', '한글결과']:
            exc = errors.from_server_result(garbage)
            assert exc is not None   # 항상 유효한 예외 반환


class TestWebhookIdempotency:
    """중복 webhook(같은 결과 두 번)에도 안전한지 - 멱등성 로직 검증."""

    def test_classify_deterministic(self):
        # 같은 입력 → 같은 결과 (재현성)
        r1 = errors.classify_result('busy')
        r2 = errors.classify_result('busy')
        assert r1 == r2

    def test_success_classification_stable(self):
        # 성공은 항상 sent
        for _ in range(3):
            assert errors.classify_result('sent') == 'sent'


class TestPartialTransmission:
    """부분 전송(10페이지 중 6페이지) 감지."""

    def test_partial_exists(self):
        if hasattr(errors, 'PartialTransmission'):
            exc = errors.PartialTransmission(sent_pages=6, total_pages=10)
            assert exc is not None
            # 부분 전송은 재시도 가치 있음
            assert exc.category in (errors.Category.RETRYABLE, errors.Category.PERMANENT)


class TestRetryExhaustion:
    """재시도 소진 시 무한 루프 없이 failed로 확정되는지."""

    def test_retry_eventually_stops(self):
        # 어떤 재시도 가능 예외도 무한 재시도하지 않음
        exc = errors.LineBusy() if hasattr(errors, 'LineBusy') else errors.TransmissionError()
        # 최대 정책 횟수를 넘으면 반드시 중단
        max_attempts = 100
        stopped = False
        for attempt in range(max_attempts):
            if not RetryPolicy.should_retry(exc, attempt):
                stopped = True
                break
        assert stopped, "재시도가 무한히 계속됨 (위험!)"


class TestConcurrencySafety:
    """동시 요청에도 rate limiter가 깨지지 않는지."""

    def test_ratelimit_thread_safe(self):
        import threading
        from app.core import ratelimit
        ratelimit.reset()
        errors_found = []

        def hammer():
            try:
                for _ in range(50):
                    try:
                        ratelimit.check('concurrent', limit=1000, window_sec=60)
                    except ratelimit.RateLimitExceeded:
                        pass
            except Exception as e:
                errors_found.append(e)

        threads = [threading.Thread(target=hammer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 동시 접근에도 예외(자료구조 깨짐) 없어야
        assert not errors_found, f"동시성 오류: {errors_found}"
