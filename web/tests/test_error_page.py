"""오류로그 페이지네이션 + 검색 테스트."""
import pytest
from unittest.mock import patch

class FakeDB:
    def __init__(self,total=180):
        self.total=total;self.last_sql=''
    def query_one(self,sql,params=None):
        self.last_sql=sql;return {'c':self.total}
    def query(self,sql,params=None):
        self.last_sql=sql;return [{'id':i} for i in range(50)]

def _admin():return {'user_id':1,'role':'admin'}

class TestErrorPage:
    def test_items_total(self):
        from app.routers import errors
        db=FakeDB(180)
        with patch.object(errors,'db',db):
            r=errors.list_errors(user=_admin())
        assert r['total']==180 and 'items' in r
    def test_offset(self):
        from app.routers import errors
        db=FakeDB()
        with patch.object(errors,'db',db):
            r=errors.list_errors(offset=50,user=_admin())
        assert r['offset']==50 and 'OFFSET' in db.last_sql
    def test_code_filter(self):
        from app.routers import errors
        db=FakeDB()
        with patch.object(errors,'db',db):
            errors.list_errors(code='BUSY',user=_admin())
        assert 'e.code' in db.last_sql
    def test_date_filter(self):
        from app.routers import errors
        db=FakeDB()
        with patch.object(errors,'db',db):
            errors.list_errors(date_from='2026-09-01',user=_admin())
        assert 'created_at' in db.last_sql
