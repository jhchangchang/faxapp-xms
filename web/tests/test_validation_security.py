"""
입력 검증 + 보안 테스트.
잘못된 입력/악의적 요청에 앱이 안 죽고 정확히 거부하는지.
"""
import pytest
from app.core import errors, ratelimit


class TestFaxNumberValidation:
    """팩스번호 검증 - 잘못된 입력 거부."""

    def setup_method(self):
        from app.routers.send import _valid_fax_number
        self.valid = _valid_fax_number

    def test_valid_numbers(self):
        assert self.valid('0212345678')
        assert self.valid('02-1234-5678')
        assert self.valid('+82212345678')
        assert self.valid('(02)1234-5678')

    def test_rejects_empty(self):
        assert not self.valid('')
        assert not self.valid(None)

    def test_rejects_too_short(self):
        assert not self.valid('12')

    def test_rejects_letters(self):
        assert not self.valid('call-me')
        assert not self.valid('abcdefgh')

    def test_rejects_injection_chars(self):
        # 명령 인젝션 시도 문자 거부
        assert not self.valid('010; rm -rf /')
        assert not self.valid('010`whoami`')
        assert not self.valid('010$(cat /etc/passwd)')

    def test_rejects_too_long(self):
        assert not self.valid('0' * 100)


class TestRateLimit:
    """발송 rate limit - 스팸/과금폭탄 방어."""

    def setup_method(self):
        ratelimit.reset()   # 각 테스트 전 초기화

    def test_allows_within_limit(self):
        for i in range(5):
            ratelimit.check('user1', limit=5, window_sec=60)   # 5회까지 OK

    def test_blocks_over_limit(self):
        for i in range(5):
            ratelimit.check('user2', limit=5, window_sec=60)
        with pytest.raises(ratelimit.RateLimitExceeded):
            ratelimit.check('user2', limit=5, window_sec=60)   # 6회째 차단

    def test_separate_users_independent(self):
        # 사용자별 독립 카운트
        for i in range(5):
            ratelimit.check('userA', limit=5, window_sec=60)
        # userB는 영향 없음
        ratelimit.check('userB', limit=5, window_sec=60)

    def test_retry_after_provided(self):
        for i in range(3):
            ratelimit.check('user3', limit=3, window_sec=60)
        try:
            ratelimit.check('user3', limit=3, window_sec=60)
            assert False, "차단됐어야 함"
        except ratelimit.RateLimitExceeded as e:
            assert e.retry_after > 0   # 재시도 시각 안내

    def test_reset_clears(self):
        for i in range(5):
            ratelimit.check('user4', limit=5, window_sec=60)
        ratelimit.reset('user4')
        # 리셋 후 다시 가능
        ratelimit.check('user4', limit=5, window_sec=60)


class TestExceptionResponses:
    """예외가 일관된 응답으로 변환되는지 (앱이 안 죽고 사용자 안내)."""

    def test_all_errors_have_dict(self):
        # 주요 예외가 to_dict()로 응답 가능한지
        for exc in [errors.InvalidFaxNumber(), errors.RateLimitError(retry_after=30),
                    errors.SystemError(detail='x'), errors.FileTooLarge()]:
            d = exc.to_dict()
            assert 'code' in d
            assert 'message' in d or 'error' in d

    def test_system_error_is_503(self):
        assert errors.SystemError().http_status == 503

    def test_rate_limit_is_429(self):
        assert errors.RateLimitError().http_status == 429

    def test_user_errors_are_4xx(self):
        assert 400 <= errors.InvalidFaxNumber().http_status < 500
