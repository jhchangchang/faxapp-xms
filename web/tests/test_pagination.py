"""발송 이력 페이지네이션 + 검색 테스트."""
import pytest
from unittest.mock import patch


class FakeDB:
    def __init__(self, total=237):
        self.total=total
        self.last_sql=''
        self.last_params=None
    def query_one(self, sql, params=None):
        self.last_sql=sql
        return {'c': self.total}
    def query(self, sql, params=None):
        self.last_sql=sql; self.last_params=params
        return [{'id':i} for i in range(min(50,self.total))]


def _user(role='admin'):
    return {'user_id':1,'role':role}


class TestPagination:
    def test_returns_items_and_total(self):
        from app.routers import send
        db=FakeDB(total=237)
        with patch.object(send,'db',db):
            r=send.list_jobs_filtered(user=_user())
        assert 'items' in r and 'total' in r
        assert r['total']==237
        assert r['limit']==50
        assert r['offset']==0

    def test_offset_applied(self):
        from app.routers import send
        db=FakeDB(total=237)
        with patch.object(send,'db',db):
            r=send.list_jobs_filtered(offset=100,user=_user())
        assert r['offset']==100
        assert 'OFFSET' in db.last_sql

    def test_limit_capped(self):
        from app.routers import send
        db=FakeDB()
        with patch.object(send,'db',db):
            r=send.list_jobs_filtered(limit=999,user=_user())
        assert r['limit']==200  # 최대 200

    def test_date_filter(self):
        from app.routers import send
        db=FakeDB()
        with patch.object(send,'db',db):
            r=send.list_jobs_filtered(date_from='2026-09-01',date_to='2026-09-03',user=_user())
        assert 'created_at' in db.last_sql

    def test_name_search(self):
        from app.routers import send
        db=FakeDB()
        with patch.object(send,'db',db):
            r=send.list_jobs_filtered(name='홍길동',user=_user())
        assert 'to_name' in db.last_sql

    def test_user_sees_own_only(self):
        from app.routers import send
        db=FakeDB()
        with patch.object(send,'db',db):
            r=send.list_jobs_filtered(user=_user(role='user'))
        assert 'user_id' in db.last_sql
