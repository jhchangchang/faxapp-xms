"""수신함 페이지네이션 + 검색 테스트."""
import pytest
from unittest.mock import patch

class FakeDB:
    def __init__(self,total=120):
        self.total=total;self.last_sql=''
    def query_one(self,sql,params=None):
        self.last_sql=sql
        if 'dept_id FROM users' in sql:return {'dept_id':1}
        return {'c':self.total}
    def query(self,sql,params=None):
        self.last_sql=sql
        return [{'id':i} for i in range(50)]

class TestInboxPage:
    def test_returns_items_total(self):
        from app.routers import receive
        db=FakeDB(120)
        with patch.object(receive,'db',db):
            r=receive.inbox(user={'user_id':1,'role':'admin'})
        assert r['total']==120 and 'items' in r
    def test_offset(self):
        from app.routers import receive
        db=FakeDB()
        with patch.object(receive,'db',db):
            r=receive.inbox(offset=50,user={'user_id':1,'role':'admin'})
        assert r['offset']==50 and 'OFFSET' in db.last_sql
    def test_from_search(self):
        from app.routers import receive
        db=FakeDB()
        with patch.object(receive,'db',db):
            receive.inbox(from_number='021234',user={'user_id':1,'role':'admin'})
        assert 'from_number' in db.last_sql
    def test_manager_dept_scope(self):
        from app.routers import receive
        db=FakeDB()
        with patch.object(receive,'db',db):
            receive.inbox(user={'user_id':2,'role':'manager'})
        assert 'dept_id' in db.last_sql
