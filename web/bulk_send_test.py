#!/usr/bin/env python3
"""
엔진 계층 부하 테스트용 대량 발송 도구 (bulk_send_test.py)
=========================================================
실제 팩스 회선이 붙은 후, 큐에 대량 발송을 던져
엔진이 동시에 몇 채널로 처리하는지 측정하기 위한 도구.

표준 라이브러리만 사용. 로그인 후 발송 API를 반복 호출.

사용법:
  # 100건을 번호 1002로 발송 (테스트 문서 필요)
  python3 bulk_send_test.py --url http://127.0.0.1:8100 \\
      --login admin:비밀번호 --count 100 --number 1002 \\
      --file /opt/faxapp/web/test.pdf

측정 후:
  - 팩스 서버 대시보드(:8090)에서 동시 채널 수 확인
  - fs_cli -x "show channels count" 로 활성 채널 모니터
  - 앱 발송 이력에서 완료 시각 분포 확인
"""
import argparse
import http.cookiejar
import json
import os
import threading
import time
import urllib.request
import uuid


def login(opener, base, cred):
    u, _, p = cred.partition(':')
    data = json.dumps({'username': u, 'password': p}).encode()
    req = urllib.request.Request(base + '/api/auth/login', data=data,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    opener.open(req, timeout=10).read()


def send_one(opener, base, number, filepath, idx, results):
    """multipart/form-data로 발송 요청."""
    boundary = uuid.uuid4().hex
    with open(filepath, 'rb') as f:
        content = f.read()
    fname = os.path.basename(filepath)
    parts = []
    for k, v in [('number', number), ('to_name', f'부하테스트{idx}'), ('cover', 'false'), ('title', ''), ('memo', '')]:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                 f'Content-Type: application/pdf\r\n\r\n'.encode())
    parts.append(content)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    body = b''.join(parts)
    req = urllib.request.Request(base + '/api/fax/send', data=body,
                                 headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
                                 method='POST')
    start = time.perf_counter()
    try:
        resp = opener.open(req, timeout=30)
        r = json.loads(resp.read())
        results.append(('ok', (time.perf_counter() - start) * 1000, r.get('status', '?')))
    except Exception as e:
        results.append(('err', (time.perf_counter() - start) * 1000, str(e)[:40]))


def main():
    ap = argparse.ArgumentParser(description='엔진 계층 대량 발송 테스트')
    ap.add_argument('--url', default='http://127.0.0.1:8100')
    ap.add_argument('--login', required=True, help='user:password')
    ap.add_argument('--count', type=int, default=50, help='발송 건수')
    ap.add_argument('--number', default='1002', help='받는 번호(테스트용)')
    ap.add_argument('--file', required=True, help='보낼 테스트 문서 경로')
    ap.add_argument('--concurrency', type=int, default=10, help='동시 요청 수')
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        print(f'✗ 파일 없음: {args.file}')
        return

    print(f'대량 발송 테스트: {args.count}건 → {args.number}')
    print(f'동시 요청: {args.concurrency}, 파일: {args.file}\n')

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    login(opener, args.url, args.login)

    results = []
    sem = threading.Semaphore(args.concurrency)
    threads = []
    start = time.time()

    def task(i):
        with sem:
            send_one(opener, args.url, args.number, args.file, i, results)

    for i in range(args.count):
        t = threading.Thread(target=task, args=(i,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    elapsed = time.time() - start
    ok = sum(1 for r in results if r[0] == 'ok')
    err = len(results) - ok
    lat = [r[1] for r in results]
    print(f'완료: {len(results)}건, {elapsed:.1f}초')
    print(f'  발송 요청 성공: {ok}, 실패: {err}')
    if lat:
        print(f'  요청 응답시간 평균: {sum(lat)/len(lat):.0f}ms')
    print(f'  요청 처리율: {len(results)/elapsed:.1f} 건/초')
    print()
    print('※ 이제 팩스 서버(:8090)에서 실제 동시 전송 채널 수를 확인하세요:')
    print('   /usr/local/freeswitch/bin/fs_cli -x "show channels count"')
    print('※ 앱 발송 이력에서 sent 완료 시각 분포로 실제 처리 속도를 봅니다.')


if __name__ == '__main__':
    main()
