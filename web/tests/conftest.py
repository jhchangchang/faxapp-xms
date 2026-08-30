"""
테스트 공통 설정 + Mock(가짜) 팩스 엔진.

실제 회선/XMS/FreeSWITCH 없이 팩스 엔진의 온갖 응답을 흉내 내서
faxapp이 비정상 상황에서 안 죽는지 검증한다.

pytest만 있으면 폐쇄망에서도 실행 가능 (외부 서비스 불필요).
"""
import sys
import os
import pytest

# app 패키지 임포트 경로
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class MockFaxServer:
    """가짜 팩스 엔진. 시나리오별 응답을 설정할 수 있음.
    faxclient가 호출하는 메서드를 흉내 낸다.
    """
    def __init__(self):
        self.available = True          # server_available() 반환값
        self.send_response = {'ok': True, 'queued': True, 'job_id': None}
        self.send_raises = None        # 설정 시 send_fax가 이 예외를 던짐
        self.job_status = {}           # server_job_id → status dict
        self.sent_calls = []           # 발송 요청 기록 (검증용)

    def send_fax(self, number, file_path, async_mode=True, job_id=None):
        self.sent_calls.append({'number': number, 'file': file_path, 'job_id': job_id})
        if self.send_raises:
            raise self.send_raises
        resp = dict(self.send_response)
        if resp.get('job_id') is None:
            resp['job_id'] = str(job_id) if job_id else 'srv_1'
        return resp

    def server_available(self):
        return self.available

    def get_job_status(self, server_job_id):
        return self.job_status.get(server_job_id)


@pytest.fixture
def mock_server():
    return MockFaxServer()
