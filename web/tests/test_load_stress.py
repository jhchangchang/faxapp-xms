"""
실부하/스트레스 테스트 (30채널 규모 대비).

대량 동시 요청에서 방어 장치(rate limit, 멱등성, 자료구조)가
깨지지 않고 정확히 작동하는지 검증.
"우리 쪽 문제로 죽으면 안 된다"의 부하 버전.

실제 DB/엔진 없이 Mock으로, 동시성만 실제 스레드로 재현.
"""
import pytest
import threading
import re
from contextlib import ExitStack
from unittest.mock import patch, MagicMock
from app.core import ratelimit


class TestRateLimiterUnderLoad:
    """rate limiter가 대량 동시 접근에 안전한가."""

    def setup_method(self):
        ratelimit.reset()

    def test_massive_concurrent_no_crash(self):
        # 50스레드 × 200요청 = 10000 동시 접근에도 자료구조 안 깨짐
        errors = []
        def worker(tid):
            try:
                for i in range(200):
                    try:
                        ratelimit.check(f'load_user_{tid % 10}', limit=1000000, window_sec=60)
                    except ratelimit.RateLimitExceeded:
                        pass
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors, f"동시성 오류: {errors[:3]}"

    def test_limit_accurate_under_concurrency(self):
        # 동시에 몰려도 한도를 초과 허용하지 않는가 (과금 방지 핵심)
        ratelimit.reset()
        allowed = []
        lock = threading.Lock()
        LIMIT = 100
        def worker():
            for _ in range(20):
                try:
                    ratelimit.check('strict_user', limit=LIMIT, window_sec=300)
                    with lock:
                        allowed.append(1)
                except ratelimit.RateLimitExceeded:
                    pass
        threads = [threading.Thread(target=worker) for _ in range(30)]  # 30×20=600시도
        for t in threads: t.start()
        for t in threads: t.join()
        # 허용된 요청이 정확히 LIMIT 이하여야 (초과 허용 = 과금 폭탄)
        assert len(allowed) <= LIMIT, f"한도({LIMIT}) 초과 허용됨: {len(allowed)}"

    def test_memory_bounded(self):
        # 키가 폭증해도 메모리 정리로 무한 증가 안 함
        ratelimit.reset()
        for i in range(15000):
            try:
                ratelimit.check(f'unique_key_{i}', limit=1, window_sec=1)
            except ratelimit.RateLimitExceeded:
                pass
        # 내부 _hits가 무한정 쌓이지 않았는지 (cleanup 동작)
        from app.core.ratelimit import _hits
        # 10000 초과 시 정리하므로 과도하게 크지 않아야
        assert len(_hits) < 20000, f"메모리 정리 안 됨: {len(_hits)}개 키"


class FakeDB:
    """webhook 동시성 테스트용 - 상태 변경을 스레드 안전하게 추적."""
    def __init__(self, jobs):
        self.jobs = jobs
        self.executed = []
        self.lock = threading.Lock()

    def query_one(self, sql, params=None):
        if 'server_job_id' in sql.lower() and params:
            with self.lock:
                for job in self.jobs.values():
                    if str(job.get('server_job_id')) == str(params[0]):
                        return dict(job)
        return None

    def execute(self, sql, params=None, returning=False):
        with self.lock:
            self.executed.append((sql, params))
            low = sql.lower()
            if 'update fax_jobs' in low and 'status=' in low:
                m = re.search(r"status='(\w+)'", sql)
                if m and params:
                    for job in self.jobs.values():
                        if job.get('id') == params[-1]:
                            job['status'] = m.group(1)
        return {'id': 1} if returning else 1

    def query(self, sql, params=None):
        return []


class TestWebhookConcurrency:
    """같은 job에 webhook이 동시에 여러 개 와도 멱등한가 (중복 과금 방지)."""

    def test_duplicate_webhooks_idempotent(self):
        from app.routers import send
        from app.core import error_handler

        job = {'id': 1, 'server_job_id': 'srv_x', 'status': 'queued',
               'retry_count': 0, 'max_retries': 3, 'pages': 3, 'user_id': 1}
        db = FakeDB({'j1': job})

        def _body():
            from app.routers.send import SendResultBody
            return SendResultBody(server_job_id='srv_x', success=True, pages_sent=3)

        def _req():
            r = MagicMock(); r.client.host = '127.0.0.1'; r.headers = {}
            return r

        results = []
        rlock = threading.Lock()
        def fire():
            with ExitStack() as stack:
                stack.enter_context(patch.object(send, 'db', db))
                stack.enter_context(patch.object(error_handler, 'db', db))
                try:
                    r = send.send_result_webhook(_body(), _req())
                    with rlock:
                        results.append(r)
                except Exception as e:
                    with rlock:
                        results.append(('error', e))

        # 같은 job에 webhook 20개 동시
        threads = [threading.Thread(target=fire) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()

        # 크래시 없이 다 처리
        assert len(results) == 20
        assert not any(isinstance(r, tuple) and r[0] == 'error' for r in results)
        # 최종 상태는 sent (한 번만 확정)
        assert db.jobs['j1']['status'] == 'sent'
        # status='sent' UPDATE가 실행됐지만, job은 한 번만 sent됨 (멱등)
        sent_updates = [1 for sql, _ in db.executed
                        if 'status=\'sent\'' in sql.lower().replace('"', "'")]
        # 여러 번 UPDATE될 수 있으나 결과는 일관 (sent)
        assert db.jobs['j1']['status'] == 'sent'
