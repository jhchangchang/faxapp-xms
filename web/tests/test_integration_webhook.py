"""
통합 테스트: webhook 결과 처리 전 과정.

엔진이 보낸 결과(webhook)를 받아 job 상태를 바꾸고 재시도를 판단하는
전체 흐름을, DB를 Mock으로 대체해 실제 로직으로 검증한다.
"실제로 흐르는가"를 봄: 성공→sent, 실패→분류→재시도or확정, 중복→멱등.
"""
import pytest
import re
from contextlib import ExitStack
from unittest.mock import patch, MagicMock


class FakeDB:
    """가짜 DB. 실제 코드가 status를 SQL 문자열에 직접 박으므로(status='sent') 파싱."""
    def __init__(self, jobs=None):
        self.jobs = jobs or {}
        self.executed = []

    def query_one(self, sql, params=None):
        low = sql.lower()
        if 'server_job_id' in low and params:
            sjid = params[0]
            for job in self.jobs.values():
                if str(job.get('server_job_id')) == str(sjid):
                    return dict(job)
            return None
        # receive.py의 _classify 쿼리 (fax_number 매칭) → 없음(None) 반환
        if 'fax_number' in low or 'from users' in low or 'from departments' in low:
            return None
        return None

    def execute(self, sql, params=None, returning=False):
        self.executed.append((sql, params))
        low = sql.lower()
        if 'update fax_jobs' in low and 'status=' in low:
            m = re.search(r"status='(\w+)'", sql)
            if m and params:
                new_status = m.group(1)
                job_id = params[-1]
                for job in self.jobs.values():
                    if job.get('id') == job_id:
                        job['status'] = new_status
        if returning:
            return {'id': 1}
        return 1

    def query(self, sql, params=None):
        return []


def _body(**kw):
    from app.routers.send import SendResultBody
    d = dict(server_job_id='srv_1', success=True)
    d.update(kw)
    return SendResultBody(**d)


def _req(ip='127.0.0.1'):
    r = MagicMock()
    r.client.host = ip
    r.headers = {}
    return r


def _job(**kw):
    d = {'id': 1, 'server_job_id': 'srv_1', 'status': 'queued',
         'retry_count': 0, 'max_retries': 3, 'pages': 3, 'user_id': 1}
    d.update(kw)
    return d


def _run_webhook(db, body, req=None):
    """send.db와 error_handler.db를 모두 Mock으로 바꾸고 webhook 실행."""
    from app.routers import send
    from app.core import error_handler
    with ExitStack() as stack:
        stack.enter_context(patch.object(send, 'db', db))
        stack.enter_context(patch.object(error_handler, 'db', db))
        return send.send_result_webhook(body, req or _req())


class TestWebhookSuccess:
    def test_success_marks_sent(self):
        db = FakeDB(jobs={'j1': _job(status='queued', pages=3)})
        r = _run_webhook(db, _body(success=True, pages_sent=3, transfer_rate='33600'))
        assert r.get('status') == 'sent'
        assert db.jobs['j1']['status'] == 'sent'

    def test_partial_transmission_detected(self):
        # 성공이라지만 3/5 페이지만 → 부분전송 처리
        db = FakeDB(jobs={'j1': _job(status='queued', pages=5)})
        r = _run_webhook(db, _body(success=True, pages_sent=3, pages_total=5))
        # 부분전송은 실패 계열로 처리 (sent 아님)
        assert db.jobs['j1']['status'] != 'sent'


class TestWebhookFailure:
    def test_permanent_failure_no_retry(self):
        db = FakeDB(jobs={'j1': _job(status='queued')})
        r = _run_webhook(db, _body(success=False, result_text='invalid_number',
                                   retryable=False))
        assert db.jobs['j1']['status'] == 'failed'

    def test_retryable_failure_no_crash(self):
        db = FakeDB(jobs={'j1': _job(status='queued')})
        r = _run_webhook(db, _body(success=False, result_text='busy', retryable=True))
        assert r is not None
        # busy는 failed로 즉시 확정되지 않음 (재시도 대기)
        assert db.jobs['j1']['status'] != 'failed'


class TestWebhookIdempotency:
    def test_already_sent_ignored(self):
        db = FakeDB(jobs={'j1': _job(status='sent')})
        r = _run_webhook(db, _body(success=True, pages_sent=3))
        # 이미 종료된 job은 멱등 처리 (already_finalized)
        assert r.get('note') == 'already_finalized' or db.jobs['j1']['status'] == 'sent'

    def test_duplicate_no_crash(self):
        db = FakeDB(jobs={'j1': _job(status='queued', pages=3)})
        r1 = _run_webhook(db, _body(success=True, pages_sent=3))
        r2 = _run_webhook(db, _body(success=True, pages_sent=3))
        assert r1 is not None and r2 is not None
        assert db.jobs['j1']['status'] == 'sent'


class TestWebhookUnknownJob:
    def test_unknown_job_no_crash(self):
        db = FakeDB(jobs={})
        r = _run_webhook(db, _body(server_job_id='nope', success=True))
        assert r is not None
        assert r.get('reason') == 'job_not_found'


class TestWebhookAuth:
    def test_rejects_external_ip(self):
        from app.core import errors
        db = FakeDB(jobs={})
        with pytest.raises(errors.FaxAppError):
            _run_webhook(db, _body(success=True), req=_req('1.2.3.4'))


class TestReceiveRouter:
    """수신 webhook (receive.py) → fax_received 저장 + 번호 분류."""

    def test_receive_stored_and_classified(self):
        from app.routers import receive
        db = FakeDB(jobs={})
        # _classify가 db.query_one으로 부서/사용자 찾음 (없으면 None,None)
        with ExitStack() as stack:
            stack.enter_context(patch.object(receive, 'db', db))
            body = receive.WebhookBody(
                from_number='0212345678', to_number='0287654321',
                file_path='/tmp/rx/a.pdf', pages=2, rate='33600')
            r = receive.receive_webhook(body, _req())
        assert r.get('ok')
        # fax_received에 INSERT 됐는지
        assert any('fax_received' in sql.lower() for sql, _ in db.executed)
