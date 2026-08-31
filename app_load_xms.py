#!/usr/bin/env python3
"""
faxapp → FreeSWITCH → XMS146 부하테스트 (실제 서비스 경로).
검증된 test_send_app.py의 발송 방식을 그대로 사용하여 N건 동시 발송.

사용법:
  python3 app_load_xms.py --count 10 --pages 10 --password '비번'
"""
import argparse, json, time, threading, os
import urllib.request, urllib.error
import http.cookiejar

APP = os.environ.get('APP_URL', 'http://127.0.0.1:8100')

# 쿠키 자동관리 opener (로그인 세션 유지)
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def _req(method, path, data=None, is_json=True, headers=None):
    url = APP + path
    h = headers or {}
    body = None
    if data is not None:
        if is_json:
            body = json.dumps(data).encode()
            h['Content-Type'] = 'application/json'
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with _opener.open(req, timeout=30) as r:
            txt = r.read().decode()
            try:
                return r.status, json.loads(txt)
            except Exception:
                return r.status, txt
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return None, str(e)


def make_multipage_txt(pages, path):
    """텍스트 문서 생성 (faxapp이 TIFF로 변환). test_send_app과 동일 방식."""
    with open(path, 'w') as f:
        for p in range(1, pages + 1):
            f.write(f'=== LOAD TEST PAGE {p} / {pages} ===\n\n')
            for i in range(15):
                f.write(f'Line {i+1:02d}: FreeSWITCH to XMS146 fax load test 0123456789\n')
            if p < pages:
                f.write('\f')  # 페이지 구분(form feed)


def multipart_send(number, to_name, file_path):
    """검증된 test_send_app.py와 동일한 multipart 방식."""
    boundary = '----FaxAppTestBoundary'
    parts = []
    def add_field(name, value):
        parts.append(f'--{boundary}'.encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b'')
        parts.append(str(value).encode())
    add_field('number', number)
    add_field('to_name', to_name)
    fname = os.path.basename(file_path)
    with open(file_path, 'rb') as f:
        fdata = f.read()
    parts.append(f'--{boundary}'.encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{fname}"'.encode())
    parts.append(b'Content-Type: text/plain')
    parts.append(b'')
    parts.append(fdata)
    parts.append(f'--{boundary}--'.encode())
    body = b'\r\n'.join(parts)
    headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
    return _req('POST', '/api/fax/send', data=body, is_json=False, headers=headers)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--user', default='admin')
    ap.add_argument('--password', required=True)
    ap.add_argument('--count', type=int, default=10)
    ap.add_argument('--pages', type=int, default=10)
    ap.add_argument('--number', default='rest')
    args = ap.parse_args()

    print('=' * 55)
    print(' faxapp → FreeSWITCH → XMS146 부하테스트')
    print(f' 동시 발송: {args.count}건, 각 {args.pages}페이지, 대상={args.number}')
    print('=' * 55)

    st, health = _req('GET', '/health')
    print(f' 앱 상태: {health}')

    # 로그인 (쿠키는 opener가 자동 저장)
    st, r = _req('POST', '/api/auth/login',
                 {'username': args.user, 'password': args.password})
    if st != 200:
        print(f' ✗ 로그인 실패({st}): {r}')
        return 1
    print(' ✓ 로그인 성공\n')

    # 문서 생성
    doc = '/tmp/app_load_doc.txt'
    make_multipage_txt(args.pages, doc)

    results = []
    lock = threading.Lock()

    def send_one(idx):
        t0 = time.time()
        st, r = multipart_send(args.number, f'load{idx}', doc)
        el = time.time() - t0
        with lock:
            if st in (200, 202) and isinstance(r, dict):
                jid = r.get('job_id')
                results.append(('ok', idx, jid))
                print(f"  ✓ #{idx} job={jid} status={r.get('status')} ({el:.1f}s)")
            else:
                results.append(('fail', idx, r))
                print(f"  ✗ #{idx} ({st}) {str(r)[:60]}")

    print(f' {args.count}건 동시 발송...\n')
    t_start = time.time()
    threads = [threading.Thread(target=send_one, args=(i,)) for i in range(1, args.count + 1)]
    for t in threads:
        t.start()
        time.sleep(0.2)
    for t in threads:
        t.join()
    total = time.time() - t_start

    ok = [r for r in results if r[0] == 'ok']
    fail = [r for r in results if r[0] == 'fail']
    print()
    print('=' * 55)
    print(f' 발송 요청: {len(results)}건')
    print(f' 성공(큐잉): {len(ok)} / 실패: {len(fail)}')
    print(f' 전체 소요: {total:.1f}초')
    print()
    print(' ※ 실제 전송 결과 확인:')
    print('   - faxapp 발송이력: queued → sent')
    print('   - 146 수신엔진 로그: [수신완료] bit_rate')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
