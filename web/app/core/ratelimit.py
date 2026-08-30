"""
경량 Rate Limiter - 표준 라이브러리만 사용.
폐쇄망 RPM 배포를 위해 외부 의존성(SlowAPI/Redis) 없이 동작.

용도: 팩스 발송 남용 방지 (계정 탈취 시 스팸/과금 폭탄 차단).
방식: 슬라이딩 윈도우. 사용자별/IP별 카운트.

주의: 단일 프로세스 메모리 기반. 다중 워커면 워커별로 카운트됨
      (그만큼 실제 한도는 워커수 배수). 다중 서버 확장 시 Redis 백엔드로
      교체할 수 있게 인터페이스를 단순하게 유지.
"""
import time
import threading
from collections import defaultdict, deque

_lock = threading.Lock()
# key → deque[timestamp]
_hits = defaultdict(deque)


class RateLimitExceeded(Exception):
    def __init__(self, retry_after, limit, window):
        self.retry_after = retry_after
        self.limit = limit
        self.window = window
        super().__init__(f'요청 한도 초과 ({limit}/{window}초). {retry_after}초 후 재시도.')


def check(key, limit, window_sec):
    """key에 대해 window_sec 초 동안 limit 회까지 허용.
    초과 시 RateLimitExceeded 발생. 통과 시 카운트 기록 후 반환.
    """
    now = time.time()
    cutoff = now - window_sec
    with _lock:
        dq = _hits[key]
        # 윈도우 밖의 오래된 기록 제거
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            # 가장 오래된 요청이 윈도우를 벗어날 때까지 대기 필요
            retry_after = int(dq[0] + window_sec - now) + 1
            raise RateLimitExceeded(retry_after, limit, window_sec)
        dq.append(now)
        # 메모리 관리: 키가 너무 많아지면 오래된 것 정리
        if len(_hits) > 10000:
            _cleanup(cutoff)


def _cleanup(cutoff):
    """빈 deque 제거 (메모리 누수 방지). _lock 안에서 호출."""
    empty = [k for k, dq in _hits.items() if not dq or dq[-1] < cutoff]
    for k in empty[:5000]:
        del _hits[k]


def reset(key=None):
    """테스트/관리용. key 지정 시 해당 키만, 없으면 전체 초기화."""
    with _lock:
        if key is None:
            _hits.clear()
        else:
            _hits.pop(key, None)
