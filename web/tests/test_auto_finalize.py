"""오래된 queued 자동 완료 안전장치 테스트.
통지(webhook)가 유실돼도 오래 멈춘 queued를 자동으로 완료 처리하는지."""
import pytest
from unittest.mock import patch, MagicMock
from contextlib import ExitStack


class FakeDB:
    def __init__(self, stale_ids):
        self.stale_ids = stale_ids
        self.updated = []
    def query(self, sql, params=None):
        if "status='queued'" in sql.lower() and 'interval' in sql.lower():
            return [{'id': i} for i in self.stale_ids]
        return []
    def execute(self, sql, params=None, returning=False):
        if "status='sent'" in sql.lower():
            self.updated.append(params[-1])  # job_id
        return 1


class TestAutoFinalize:
    def test_finalizes_stale_queued(self):
        from app.core import retry_worker
        db = FakeDB(stale_ids=[10, 11, 12])
        with patch.object(retry_worker, 'db', db):
            n = retry_worker._auto_finalize_stale()
        # 3건 자동 완료
        assert n == 3
        assert set(db.updated) == {10, 11, 12}

    def test_no_stale_no_action(self):
        from app.core import retry_worker
        db = FakeDB(stale_ids=[])
        with patch.object(retry_worker, 'db', db):
            n = retry_worker._auto_finalize_stale()
        assert n == 0
        assert db.updated == []

    def test_error_no_crash(self):
        from app.core import retry_worker
        db = MagicMock()
        db.query.side_effect = Exception('db error')
        with patch.object(retry_worker, 'db', db):
            n = retry_worker._auto_finalize_stale()
        # 에러나도 크래시 없이 0 반환
        assert n == 0
