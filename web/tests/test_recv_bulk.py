"""수신 팩스 일괄 삭제 테스트 (권한 범위 내만 삭제)."""
import pytest
from unittest.mock import patch
from contextlib import ExitStack


class FakeDB:
    def __init__(self, rows, dept=None):
        self.rows = rows  # 권한 있는 id 리스트
        self.dept = dept
        self.executed = []
    def query_one(self, sql, params=None):
        if 'dept_id from users' in sql.lower():
            return {'dept_id': self.dept}
        return None
    def query(self, sql, params=None):
        # 권한 필터된 id 반환
        return [{'id': i} for i in self.rows]
    def execute(self, sql, params=None, returning=False):
        self.executed.append((sql, params))
        return 1


class TestRecvBulkDelete:
    def test_admin_deletes_all(self):
        from app.routers import receive
        db = FakeDB(rows=[1,2,3])
        with ExitStack() as st:
            st.enter_context(patch.object(receive, 'db', db))
            r = receive.bulk_delete_received(
                receive.RecvBulkBody(ids=[1,2,3]),
                {'user_id':1,'role':'admin'})
        assert r['deleted'] == 3

    def test_user_only_authorized(self):
        from app.routers import receive
        # 3개 요청했지만 권한 있는 건 2개만
        db = FakeDB(rows=[1,2], dept=5)
        with ExitStack() as st:
            st.enter_context(patch.object(receive, 'db', db))
            r = receive.bulk_delete_received(
                receive.RecvBulkBody(ids=[1,2,3]),
                {'user_id':10,'role':'user'})
        assert r['deleted'] == 2
        assert r['skipped'] == 1  # 권한 없는 1건

    def test_empty_safe(self):
        from app.routers import receive
        db = FakeDB(rows=[])
        with patch.object(receive, 'db', db):
            r = receive.bulk_delete_received(
                receive.RecvBulkBody(ids=[]), {'user_id':1,'role':'admin'})
        assert r['deleted'] == 0
