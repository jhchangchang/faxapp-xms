"""오류 로그 삭제 API 테스트 (개별/선택)."""
import pytest
from unittest.mock import patch
from contextlib import ExitStack


class FakeDB:
    def __init__(self):
        self.executed = []
    def execute(self, sql, params=None, returning=False):
        self.executed.append((sql, params))
        return 1
    def query_one(self, sql, params=None):
        return {'c': 5}
    def query(self, sql, params=None):
        return []


def _admin():
    return {'user_id': 1, 'role': 'admin'}


class TestErrorDelete:
    def test_bulk_delete(self):
        from app.routers import errors
        db = FakeDB()
        with patch.object(errors, 'db', db):
            r = errors.bulk_delete_errors(errors.ErrorBulkBody(ids=[1,2,3]), _admin())
        assert r['deleted'] == 3
        assert any('error_log' in sql.lower() and 'any' in sql.lower() for sql,_ in db.executed)

    def test_bulk_empty(self):
        from app.routers import errors
        db = FakeDB()
        with patch.object(errors, 'db', db):
            r = errors.bulk_delete_errors(errors.ErrorBulkBody(ids=[]), _admin())
        assert r['deleted'] == 0

    def test_delete_one(self):
        from app.routers import errors
        db = FakeDB()
        with patch.object(errors, 'db', db):
            r = errors.delete_error(42, _admin())
        assert r['ok'] is True
        assert any('42' in str(params) for _,params in db.executed)

    def test_clear_all_needs_confirm(self):
        from app.routers import errors
        db = FakeDB()
        with patch.object(errors, 'db', db):
            r = errors.clear_all_errors(confirm=False, user=_admin())
        # confirm 없으면 삭제 안 함
        assert r['ok'] is False
