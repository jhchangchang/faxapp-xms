#!/usr/bin/env python3
"""
FaxApp 부하 테스트 도구 (loadtest.py)
====================================
앱 계층(FastAPI + PostgreSQL)의 동시 처리 능력을 측정.
표준 라이브러리만 사용 - 실서버에 pip 설치 없이 바로 실행 가능.

측정 항목:
- 처리량 (RPS, requests per second)
- 응답 시간 (평균, 중앙값, p95, p99, 최대)
- 성공/실패 건수, 상태코드 분포

사용법:
  # 기본 (10명 동시, 30초)
  python3 loadtest.py --url http://127.0.0.1:8100 --users 10 --duration 30

  # 로그인 후 실제 API 부하 (권장)
  python3 loadtest.py --url http://127.0.0.1:8100 \\
      --login admin:비밀번호 --users 20 --duration 60

  # 특정 시나리오만
  python3 loadtest.py --url http://127.0.0.1:8100 --scenario dashboard

설계:
- 실제 사용 패턴 반영: 조회 위주(대시보드·이력·수신함), 발송은 소수
- 각 가상 사용자가 스레드로 시나리오를 반복 실행
- 로그인 세션 쿠키를 공유해 인증된 요청 측정
"""
import argparse
import http.cookiejar
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict


# ---------- 시나리오 정의 ----------
# 실제 사용 비율을 반영한 가중치 (조회가 많고 발송은 적음)
SCENARIOS = {
    'dashboard': [
        ('GET', '/api/stats/summary', None),
        ('GET', '/api/stats/daily?days=14', None),
        ('GET', '/api/receive/unread/count', None),
    ],
    'history': [
        ('GET', '/api/fax/jobs-filtered', None),
        ('GET', '/api/fax/jobs/stats-quick', None),
    ],
    'inbox': [
        ('GET', '/api/receive/inbox', None),
        ('GET', '/api/receive/unread/count', None),
    ],
    'contacts': [
        ('GET', '/api/contacts', None),
        ('GET', '/api/contacts/groups', None),
    ],
    'stats': [
        ('GET', '/api/stats/by-department', None),
        ('GET', '/api/stats/by-user', None),
    ],
}
# 혼합 시나리오: 실제 트래픽 비율 (대시보드/이력 조회가 대부분)
MIXED_WEIGHTS = [
    ('dashboard', 40),
    ('history', 25),
    ('inbox', 20),
    ('contacts', 10),
    ('stats', 5),
]


class Stats:
    """스레드 안전 측정 수집기."""
    def __init__(self):
        self.lock = threading.Lock()
        self.latencies = []            # 응답시간(ms)
        self.status_codes = Counter()  # 상태코드 분포
        self.errors = 0
        self.per_endpoint = defaultdict(list)  # 엔드포인트별 응답시간

    def record(self, endpoint, latency_ms, status):
        with self.lock:
            self.latencies.append(latency_ms)
            self.status_codes[status] += 1
            self.per_endpoint[endpoint].append(latency_ms)
            if status >= 400 or status == 0:
                self.errors += 1


def make_opener(base_url, login):
    """쿠키 지원 opener 생성. login이 있으면 로그인 수행."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    if login:
        user, _, pw = login.partition(':')
        data = json.dumps({'username': user, 'password': pw}).encode()
        req = urllib.request.Request(
            base_url + '/api/auth/login', data=data,
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            resp = opener.open(req, timeout=10)
            resp.read()
            if resp.status != 200:
                print(f'⚠ 로그인 실패 (status {resp.status}) - 인증 없이 진행')
        except Exception as e:
            print(f'⚠ 로그인 실패: {e} - 인증 없이 진행')
    return opener


def do_request(opener, base_url, method, path, body, stats):
    """단일 요청 실행 + 측정."""
    url = base_url + path
    data = json.dumps(body).encode() if body else None
    headers = {'Content-Type': 'application/json'} if body else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.perf_counter()
    status = 0
    try:
        resp = opener.open(req, timeout=30)
        resp.read()
        status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        status = 0  # 연결 실패/타임아웃
    latency = (time.perf_counter() - start) * 1000
    stats.record(path.split('?')[0], latency, status)


def pick_scenario():
    """가중치로 시나리오 선택."""
    import random
    total = sum(w for _, w in MIXED_WEIGHTS)
    r = random.uniform(0, total)
    upto = 0
    for name, w in MIXED_WEIGHTS:
        upto += w
        if r <= upto:
            return name
    return MIXED_WEIGHTS[0][0]


def worker(base_url, login, scenario, stats, stop_event, ramp_delay):
    """가상 사용자 한 명 - 시나리오를 반복 실행."""
    time.sleep(ramp_delay)  # 램프업(동시 시작 방지)
    opener = make_opener(base_url, login)
    while not stop_event.is_set():
        sc = scenario if scenario != 'mixed' else pick_scenario()
        steps = SCENARIOS.get(sc, SCENARIOS['dashboard'])
        for method, path, body in steps:
            if stop_event.is_set():
                break
            do_request(opener, base_url, method, path, body, stats)
        time.sleep(0.05)  # 사용자 생각 시간(think time)


def pct(sorted_list, p):
    """백분위수."""
    if not sorted_list:
        return 0
    k = int(len(sorted_list) * p / 100)
    return sorted_list[min(k, len(sorted_list) - 1)]


def main():
    ap = argparse.ArgumentParser(description='FaxApp 부하 테스트')
    ap.add_argument('--url', default='http://127.0.0.1:8100', help='앱 주소')
    ap.add_argument('--users', type=int, default=10, help='동시 가상 사용자 수')
    ap.add_argument('--duration', type=int, default=30, help='테스트 시간(초)')
    ap.add_argument('--login', default=None, help='로그인 (user:password)')
    ap.add_argument('--scenario', default='mixed',
                    help='시나리오: mixed(기본)/dashboard/history/inbox/contacts/stats')
    ap.add_argument('--ramp', type=float, default=2.0, help='램프업 시간(초)')
    args = ap.parse_args()

    print('=' * 56)
    print(f'  FaxApp 부하 테스트')
    print('=' * 56)
    print(f'  대상       : {args.url}')
    print(f'  동시 사용자 : {args.users}명')
    print(f'  시간       : {args.duration}초')
    print(f'  시나리오    : {args.scenario}')
    print(f'  인증       : {"예 (" + args.login.split(":")[0] + ")" if args.login else "아니오"}')
    print('=' * 56)

    # 사전 연결 확인
    try:
        urllib.request.urlopen(args.url + '/health', timeout=5).read()
        print('✓ 서버 연결 확인\n')
    except Exception as e:
        print(f'✗ 서버에 연결할 수 없습니다: {e}')
        print(f'  {args.url} 에서 앱이 실행 중인지 확인하세요.')
        sys.exit(1)

    stats = Stats()
    stop_event = threading.Event()
    threads = []
    for i in range(args.users):
        ramp_delay = (args.ramp / max(1, args.users)) * i
        t = threading.Thread(target=worker,
                             args=(args.url, args.login, args.scenario, stats, stop_event, ramp_delay),
                             daemon=True)
        t.start()
        threads.append(t)

    # 진행 표시
    start = time.time()
    try:
        while time.time() - start < args.duration:
            time.sleep(1)
            elapsed = time.time() - start
            with stats.lock:
                cnt = len(stats.latencies)
            rps = cnt / elapsed if elapsed > 0 else 0
            print(f'\r  진행 {elapsed:4.0f}s / {args.duration}s  |  '
                  f'요청 {cnt:6d}  |  현재 {rps:6.0f} req/s', end='', flush=True)
    except KeyboardInterrupt:
        print('\n중단됨')
    stop_event.set()
    time.sleep(0.5)

    total_time = time.time() - start
    print('\n')
    print_report(stats, total_time)


def print_report(stats, total_time):
    lat = sorted(stats.latencies)
    n = len(lat)
    if n == 0:
        print('요청이 기록되지 않았습니다.')
        return

    rps = n / total_time
    ok = sum(c for s, c in stats.status_codes.items() if 200 <= s < 400)
    err = stats.errors

    print('=' * 56)
    print('  결과')
    print('=' * 56)
    print(f'  총 요청       : {n:,}건')
    print(f'  성공          : {ok:,}건 ({100*ok/n:.1f}%)')
    print(f'  실패          : {err:,}건 ({100*err/n:.1f}%)')
    print(f'  처리량 (RPS)  : {rps:.1f} req/s')
    print()
    print('  응답 시간 (ms)')
    print(f'    평균        : {statistics.mean(lat):7.1f}')
    print(f'    중앙값      : {statistics.median(lat):7.1f}')
    print(f'    p95         : {pct(lat, 95):7.1f}   (95%가 이 시간 이내)')
    print(f'    p99         : {pct(lat, 99):7.1f}   (99%가 이 시간 이내)')
    print(f'    최대        : {max(lat):7.1f}')
    print(f'    최소        : {min(lat):7.1f}')
    print()
    print('  상태코드 분포')
    for code in sorted(stats.status_codes):
        label = {200: 'OK', 401: '인증필요', 500: '서버오류', 0: '연결실패'}.get(code, '')
        print(f'    {code if code else "---"} {label:8s}: {stats.status_codes[code]:,}건')
    print()
    print('  엔드포인트별 평균 응답시간 (ms)')
    ep_avg = [(ep, statistics.mean(v), len(v)) for ep, v in stats.per_endpoint.items()]
    for ep, avg, cnt in sorted(ep_avg, key=lambda x: -x[1]):
        print(f'    {avg:7.1f}  ({cnt:,}건)  {ep}')
    print('=' * 56)

    # 간단한 진단
    print('  진단')
    p95 = pct(lat, 95)
    if err > n * 0.05:
        print(f'  ⚠ 실패율 {100*err/n:.1f}% - 서버 한계이거나 인증 문제일 수 있습니다.')
    if p95 > 1000:
        print(f'  ⚠ p95 응답시간 {p95:.0f}ms - 부하에서 느려집니다. 사용자 수를 낮춰 비교해보세요.')
    elif p95 < 200:
        print(f'  ✓ p95 {p95:.0f}ms - 빠릅니다. 사용자 수를 늘려 한계를 찾아볼 수 있습니다.')
    else:
        print(f'  ✓ p95 {p95:.0f}ms - 양호합니다.')
    if err == 0:
        print(f'  ✓ 실패 0건 - 이 부하는 안정적으로 처리됩니다.')
    print('=' * 56)


if __name__ == '__main__':
    main()
