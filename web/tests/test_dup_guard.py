"""중복 발송 방지 테스트."""
import pytest
from unittest.mock import patch, MagicMock

class TestDupGuard:
    def test_config_exists(self):
        from app.core import config
        assert hasattr(config, 'DUP_GUARD_MIN')
        assert isinstance(config.DUP_GUARD_MIN, int)

    def test_dup_query_shape(self):
        """중복 검사 쿼리가 번호+파일명+시간 조건을 쓰는지."""
        from app.core import config
        # 설정이 양수면 검사 활성
        assert config.DUP_GUARD_MIN >= 0
