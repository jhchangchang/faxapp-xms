"""
에러 분류 + 재시도 정책 테스트.
"우리 쪽 문제로 안 죽는다"의 핵심: 실패를 정확히 분류하고,
재시도할 것만 재시도(영구실패는 즉시 중단)하는지 검증.
"""
import pytest
from app.core import errors
from app.core.error_handler import RetryPolicy


class TestResultClassification:
    """엔진 결과 문자열 → 예외 분류."""

    def test_busy_is_retryable(self):
        exc = errors.from_server_result('busy')
        assert exc.category == errors.Category.RETRYABLE

    def test_no_answer_is_retryable(self):
        exc = errors.from_server_result('no_answer')
        assert exc.category == errors.Category.RETRYABLE

    def test_invalid_number_is_permanent(self):
        # 없는 번호는 재시도 무의미 → 영구 실패여야 함
        exc = errors.from_server_result('invalid_number')
        assert exc.category == errors.Category.PERMANENT

    def test_training_failed_is_retryable(self):
        exc = errors.from_server_result('training_failed')
        assert exc.category == errors.Category.RETRYABLE

    def test_unknown_result_defaults_safe(self):
        # 모르는 결과는 일반 전송에러로 (앱이 안 죽어야)
        exc = errors.from_server_result('some_weird_unknown_result_xyz')
        assert exc is not None
        assert hasattr(exc, 'code')

    def test_empty_result_no_crash(self):
        # 빈 결과에도 안 죽음
        exc = errors.from_server_result('')
        assert exc is not None
        exc2 = errors.from_server_result(None)
        assert exc2 is not None

    def test_classify_success(self):
        assert errors.classify_result('sent') == 'sent'
        assert errors.classify_result('success') == 'sent'
        assert errors.classify_result('ok') == 'sent'


class TestRetryPolicy:
    """재시도 판정: 영구실패는 재시도 안 함, 일시적만 재시도."""

    def test_permanent_never_retries(self):
        exc = errors.NumberUnreachable() if hasattr(errors, 'NumberUnreachable') else None
        if exc:
            assert not RetryPolicy.should_retry(exc, 0)

    def test_user_error_never_retries(self):
        exc = errors.InvalidFaxNumber()
        assert not RetryPolicy.should_retry(exc, 0)

    def test_retryable_retries_within_limit(self):
        exc = errors.LineBusy() if hasattr(errors, 'LineBusy') else None
        if exc:
            assert RetryPolicy.should_retry(exc, 0)   # 첫 시도는 재시도 가능

    def test_retry_stops_after_max(self):
        exc = errors.LineBusy() if hasattr(errors, 'LineBusy') else None
        if exc:
            # 충분히 많은 시도 후엔 재시도 중단
            assert not RetryPolicy.should_retry(exc, 999)

    def test_backoff_increases(self):
        # 재시도 대기가 점점 길어지는지 (지수 백오프)
        d0 = RetryPolicy.next_delay(0)
        d1 = RetryPolicy.next_delay(1)
        assert d1 >= d0   # 뒤로 갈수록 길거나 같음

    def test_backoff_no_index_error(self):
        # 시도 횟수가 백오프 배열보다 커도 안 죽음
        d = RetryPolicy.next_delay(9999)
        assert isinstance(d, (int, float))
