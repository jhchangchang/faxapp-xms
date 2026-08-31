"""
발송이력 일괄 삭제/취소 테스트.
선택한 job들을 안전하게 처리하는지 (진행 중인 건 보호).
"""
import pytest
from unittest.mock import patch
from contextlib import ExitStack


class FakeDB:
    def __init__(self, jobs):
        self.jobs = jobs  # id → status dict
        self.executed = []
    def query(self, sql, params=None):
        low = sql.lower()
        # bulk-delete: 삭제 가능한(완료계열) id 조회
        if "status in ('sent','failed','cancelled')" in low:
            return [{'id': i} for i, s in self.jobs.items()
                    if s in ('sent','failed','cancelled')]
        # bulk-cancel: 취소 가능한(대기계열) id 조회
        if "status in ('pending','queued','retry_wait')" in low:
            return [{'id': i} for i, s in self.jobs.items()
                    if s in ('pending','queued','retry_wait')]
        return []
    def query_one(self, sql, params=None):
        return None
    def execute(self, sql, params=None, returning=False):
        self.executed.append((sql, params))
        return 1


def _admin():
    return {'user_id': 1, 'role': 'admin'}


class TestBulkDelete:
    def test_deletes_only_completed(self):
        from app.routers import send
        # 5,6=완료계열 / 7=queued(진행중)
        db = FakeDB({5:'sent', 6:'failed', 7:'queued'})
        with ExitStack() as st:
            st.enter_context(patch.object(send, 'db', db))
            st.enter_context(patch.object(send, '_audit', lambda *a,**k: None))
            r = send.bulk_delete_jobs(send.BulkIdsBody(ids=[5,6,7]), _admin())
        # 완료계열 2건 삭제, 진행중 1건 건너뜀
        assert r['deleted'] == 2
        assert r['skipped'] == 1

    def test_empty_ids_safe(self):
        from app.routers import send
        db = FakeDB({})
        with patch.object(send, 'db', db):
            r = send.bulk_delete_jobs(send.BulkIdsBody(ids=[]), _admin())
        assert r['deleted'] == 0

    def test_progress_job_protected(self):
        from app.routers import send
        # 전부 진행 중 → 하나도 안 삭제
        db = FakeDB({10:'queued', 11:'sending', 12:'pending'})
        with ExitStack() as st:
            st.enter_context(patch.object(send, 'db', db))
            st.enter_context(patch.object(send, '_audit', lambda *a,**k: None))
            r = send.bulk_delete_jobs(send.BulkIdsBody(ids=[10,11,12]), _admin())
        assert r['deleted'] == 0
        assert r['skipped'] == 3


class TestBulkCancel:
    def test_cancels_only_pending(self):
        from app.routers import send
        # 20=queued(취소가능) / 21=sent(이미완료)
        db = FakeDB({20:'queued', 21:'sent'})
        with ExitStack() as st:
            st.enter_context(patch.object(send, 'db', db))
            st.enter_context(patch.object(send, '_audit', lambda *a,**k: None))
            r = send.bulk_cancel_jobs(send.BulkIdsBody(ids=[20,21]), _admin())
        assert r['cancelled'] == 1  # queued만
        assert r['skipped'] == 1    # sent는 제외

    def test_sent_cannot_cancel(self):
        from app.routers import send
        db = FakeDB({30:'sent', 31:'sent'})
        with ExitStack() as st:
            st.enter_context(patch.object(send, 'db', db))
            st.enter_context(patch.object(send, '_audit', lambda *a,**k: None))
            r = send.bulk_cancel_jobs(send.BulkIdsBody(ids=[30,31]), _admin())
        assert r['cancelled'] == 0
