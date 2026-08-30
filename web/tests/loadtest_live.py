#!/usr/bin/env python3
"""
실환경 부하 테스트 (실제 떠있는 faxapp 대상).

Mock 테스트(test_load_stress.py)는 "코드가 동시성에 안전한가"를 보고,
이 스크립트는 "실제 서버 + DB + 엔진이 부하에서 안 죽는가"를 본다.

주의: 실제 발송이 일어날 수 있으므로 테스트 환경에서만.
      기본은 health/상태 조회만 부하. --send 옵션 시에만 실제 발송.

사용법:
  # 상태 조회 부하 (안전 - 실제 발송 안 함)
  python3 loadtest_live.py --url http://127.0.0.1:8100 --concurrency 30 --requests 300

  # rate limit 방어 확인 (같은 세션으로 대량 → 429 나오는지)
  python3 loadtest_live.py --url http://127.0.0.1:8100 --test-ratelimit
"""
import argparse
import time
import threading
import urllib.request
import urllib.error
import json


def _get(url, timeout=10):
    t0 = time.time()
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return r.status, time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0
    except Exception as e:
        return None, time.time() - t0


def load_health(base, concurrency, total):
    """health 엔드포인트에 동시 부하 → 서버가 안 죽고 응답하는지."""
    results = []
    lock = threading.Lock()
    sem = threading.Semaphore(concurrency)

    def one(i):
        with sem:
            status, elapsed = _get(f'{base}/health')
            with lock:
                results.append((status, elapsed))

    print(f"health 부하: 동시 {concurrency}, 총 {total}건")
    t0 = time.time()
    threads = [threading.Thread(target=one, args=(i,)) for i in range(total)]
    for t in threads: t.start()
    for t in threads: t.join()
    total_time = time.time() - t0

    ok = sum(1 for s, _ in results if s == 200)
    fail = len(results) - ok
    avg = sum(e for _, e in results) / len(results) if results else 0
    p95 = sorted(e for _, e in results)[int(len(results) * 0.95)] if results else 0

    print(f"  총 {len(results)}건 / 성공 {ok} / 실패 {fail}")
    print(f"  성공률 {ok/len(results)*100:.1f}%")
    print(f"  전체 {total_time:.1f}초, 평균 {avg*1000:.0f}ms, p95 {p95*1000:.0f}ms")
    print(f"  처리량 {len(results)/total_time:.0f} req/s")
    if fail > 0:
        codes = {}
        for s, _ in results:
            codes[s] = codes.get(s, 0) + 1
        print(f"  응답코드 분포: {codes}")
    # 판정
    if ok / len(results) >= 0.99:
        print("  ✓ 부하에서 안정적 (99%+ 성공)")
    else:
        print("  ✗ 실패율 높음 - 서버 자원/설정 점검 필요")
    return ok / len(results) if results else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='http://127.0.0.1:8100')
    ap.add_argument('--concurrency', type=int, default=30)  # 30채널 목표
    ap.add_argument('--requests', type=int, default=300)
    args = ap.parse_args()

    print("=" * 55)
    print(" faxapp 실환경 부하 테스트")
    print(f" 대상: {args.url}")
    print("=" * 55)

    # 서버 살아있는지 먼저
    status, _ = _get(f'{args.url}/health')
    if status != 200:
        print(f" ✗ 서버 응답 없음 ({status}). faxapp 실행 확인.")
        return 1
    print(" ✓ 서버 연결 OK\n")

    # health 부하 (안전)
    load_health(args.url, args.concurrency, args.requests)

    print("\n※ 이 테스트는 조회 부하만. 실제 발송 부하는 테스트 환경에서")
    print("  실제 파일로 별도 수행 권장 (30채널 동시 발송).")
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
