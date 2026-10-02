"""일시적 오류(RETRYABLE)는 관리자 화면(error_log)에 안 뜨는지 테스트."""
import pytest
from unittest.mock import patch, MagicMock

class TestErrorFilter:
    def test_retryable_not_logged_to_db(self):
        """재시도 중인 일시적 오류는 DB 기록 안 함."""
        from app.core import error_handler, errors
        db=MagicMock()
        exc=errors.FaxServerTimeout(detail='timeout')  # RETRYABLE
        with patch.object(error_handler,'db',db):
            r=error_handler.log_error(exc)
        # DB execute(INSERT) 호출 안 됨
        assert r is None
        db.execute.assert_not_called()

    def test_exhausted_retryable_logged(self):
        """재시도 소진한 일시적 오류는 DB 기록 함 (최종 실패)."""
        from app.core import error_handler, errors
        db=MagicMock()
        db.execute.return_value={'id':1}
        exc=errors.FaxServerTimeout(detail='timeout')
        exc.exhausted=True  # 소진 표시
        with patch.object(error_handler,'db',db):
            error_handler.log_error(exc)
        db.execute.assert_called()  # 기록됨

    def test_permanent_always_logged(self):
        """영구 실패는 항상 기록."""
        from app.core import error_handler, errors
        db=MagicMock()
        db.execute.return_value={'id':1}
        exc=errors.WrongNumber(detail='bad') if hasattr(errors,'WrongNumber') else errors.ValidationError(detail='bad')
        with patch.object(error_handler,'db',db):
            error_handler.log_error(exc)
        db.execute.assert_called()

    def test_system_always_logged(self):
        """시스템 오류는 항상 기록."""
        from app.core import error_handler
        db=MagicMock()
        db.execute.return_value={'id':1}
        with patch.object(error_handler,'db',db):
            error_handler.log_error(RuntimeError('boom'))  # UNHANDLED
        db.execute.assert_called()
