"""대시보드 종합 API 테스트 (실시간 상태 + 조치 필요)."""
import pytest
from unittest.mock import patch
from contextlib import ExitStack


class FakeDB:
    def __init__(self, one_vals=None, query_vals=None):
        self.one_vals = one_vals or {}
        self.query_vals = query_vals or []
        self._call = 0
    def query_one(self, sql, params=None):
        # SQL 내용으로 어떤 값 줄지 결정
        low = sql.lower()
        if 'current_date' in low and 'yesterday' not in low and "status='sent'" in low and 'created_at >= current_date - 1' not in low:
            return {'total': 10, 'ok': 8, 'fail': 2}
        if 'current_date - 1' in low:
            return {'total': 5}
        if 'in_progress' in low or "filter (where status in ('pending'" in low:
            return {'active': 3, 'retry_wait': 1}
        if "status='failed'" in low and 'current_date - 7' in low and 'count(*) as c' in low:
            return {'c': 4}
        if 'interval' in low:
            return {'c': 2}
        if 'is_read=false' in low:
            return {'c': 6}
        if 'error_log' in low:
            return {'c': 1}
        return {'total': 0, 'ok': 0, 'fail': 0, 'c': 0, 'active': 0, 'retry_wait': 0}
    def query(self, sql, params=None):
        if 'group by last_error_code' in sql.lower():
            return [{'code': 'BUSY', 'c': 3}, {'code': 'NO_ANSWER', 'c': 1}]
        return []


class TestDashboard:
    def test_dashboard_structure(self):
        from app.routers import stats
        from app.core import faxclient
        db = FakeDB()
        with ExitStack() as st:
            st.enter_context(patch.object(stats, 'db', db))
            st.enter_context(patch.object(faxclient, 'health',
                lambda: {'ok': True, 'engine': 'xms-ip-fax', 'channels': 2}))
            r = stats.dashboard({'user_id': 1, 'role': 'admin'})
        # 필수 구조 확인
        assert 'engine' in r
        assert 'today' in r
        assert 'processing' in r
        assert 'action_needed' in r
        assert 'fail_reasons' in r

    def test_engine_down_no_crash(self):
        # 엔진이 죽어도 대시보드는 응답 (앱 안 죽음)
        from app.routers import stats
        from app.core import faxclient
        db = FakeDB()
        def boom(): raise Exception('engine down')
        with ExitStack() as st:
            st.enter_context(patch.object(stats, 'db', db))
            st.enter_context(patch.object(faxclient, 'health', boom))
            r = stats.dashboard({'user_id': 1, 'role': 'admin'})
        assert r['engine']['ok'] is False  # 엔진 다운 표시
        assert 'today' in r  # 나머지는 정상

    def test_fail_reasons_listed(self):
        from app.routers import stats
        from app.core import faxclient
        db = FakeDB()
        with ExitStack() as st:
            st.enter_context(patch.object(stats, 'db', db))
            st.enter_context(patch.object(faxclient, 'health', lambda: {'ok': True}))
            r = stats.dashboard({'user_id': 1, 'role': 'admin'})
        assert len(r['fail_reasons']) == 2
        assert r['fail_reasons'][0]['code'] == 'BUSY'
