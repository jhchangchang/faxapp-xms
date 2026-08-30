#!/usr/bin/env python3
"""
FaxApp 발신 테스트 스크립트
애플리케이션(:8100)의 발송 API를 통해 실제 발신을 테스트.
여러 페이지 문서 생성 → 로그인 → 발송 → 결과 확인.

사용법:
  python3 test_send_app.py                    # 기본: 3페이지 문서 1건
  python3 test_send_app.py --pages 5          # 5페이지 문서
  python3 test_send_app.py --count 10         # 10건 연속 발송
  python3 test_send_app.py --number 1002      # 수신번호 지정 (루프백)
  python3 test_send_app.py --broadcast 5      # 5개 번호로 동보
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import http.cookiejar

APP_URL = os.environ.get('APP_URL', 'http://127.0.0.1:8100')

# 쿠키 유지용 opener (세션 로그인)
_cj = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))


def _req(method, path, data=None, is_json=True, headers=None):
    url = APP_URL.rstrip('/') + path
    body = None
    h = headers or {}
    if data is not None:
        if is_json:
            body = json.dumps(data).encode()
            h['Content-Type'] = 'application/json'
        else:
            body = data  # bytes (multipart)
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with _opener.open(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {'detail': raw[:200]}
    except Exception as e:
        return 0, {'detail': str(e)}


def make_multipage_pdf(pages, path):
    """여러 페이지 텍스트 PDF 생성 (변환 테스트용)."""
    # 간단히: 텍스트 파일 여러 줄 → 실제로는 앱이 변환함
    # 여기선 페이지 구분을 위해 폼피드(\f) 사용
    lines = []
    for p in range(1, pages + 1):
        lines.append(f'=== 테스트 문서 {p} 페이지 / 전체 {pages}페이지 ===')
        lines.append(f'발신 테스트 - 페이지 {p}')
        lines.append('이것은 FaxApp 애플리케이션 발신 테스트입니다.')
        lines.append('여러 페이지 전송이 정상 동작하는지 확인합니다.')
        lines.append('가나다라마바사 ABCDEFG 1234567890')
        if p < pages:
            lines.append('\f')  # 페이지 구분
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return path


def multipart_send(number, to_name, file_path, cover=False, title='', memo=''):
    """multipart/form-data로 파일 발송."""
    boundary = '----FaxAppTestBoundary'
    parts = []
    def add_field(name, value):
        parts.append(f'--{boundary}'.encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b'')
        parts.append(str(value).encode())
    add_field('number', number)
    add_field('to_name', to_name)
    if cover:
        add_field('cover', 'true'); add_field('title', title); add_field('memo', memo)
    # 파일
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
    ap.add_argument('--password', default=None, help='로그인 비밀번호')
    ap.add_argument('--pages', type=int, default=3, help='문서 페이지 수')
    ap.add_argument('--count', type=int, default=1, help='발송 건수')
    ap.add_argument('--number', default='1002', help='수신번호 (루프백은 1002)')
    ap.add_argument('--cover', action='store_true', help='표지 첨부')
    ap.add_argument('--broadcast', type=int, default=0, help='N개 번호로 동보')
    args = ap.parse_args()

    if not args.password:
        args.password = input('비밀번호: ')

    print(f'=== FaxApp 발신 테스트 ===')
    print(f'대상: {APP_URL}')

    # 1. 헬스체크
    st, health = _req('GET', '/health')
    print(f'\n[1] 상태확인: app={health.get("app")} db={health.get("database")} fax_server={health.get("fax_server")}')
    if not health.get('database'):
        print('  ⚠ DB 연결 안됨 - PostgreSQL 확인 필요'); return
    if not health.get('fax_server'):
        print('  ⚠ 팩스 서버(:8090) 연결 안됨 - 발송은 failed로 기록됨 (계속 진행)')

    # 2. 로그인
    st, r = _req('POST', '/api/auth/login', {'username': args.user, 'password': args.password})
    if st != 200:
        print(f'\n[2] 로그인 실패: {r.get("detail")}'); return
    print(f'[2] 로그인 성공: {r["user"]["display_name"]} ({r["user"]["role"]})')

    # 3. 테스트 문서 생성
    doc = '/tmp/faxapp_test_doc.txt'
    make_multipage_pdf(args.pages, doc)
    print(f'[3] 테스트 문서 생성: {args.pages}페이지')

    # 4. 발송
    if args.broadcast > 0:
        numbers = ','.join(str(1002) for _ in range(args.broadcast))
        print(f'\n[4] 동보전송: {args.broadcast}건')
        # 동보는 multipart - 간단히 단건 반복으로 대체 표시
        print('  (동보는 웹 UI에서 테스트 권장, 여기선 단건 반복)')
        args.count = args.broadcast

    print(f'\n[4] 발송 시작: {args.count}건, 각 {args.pages}페이지, 수신번호={args.number}')
    ok, fail = 0, 0
    for i in range(1, args.count + 1):
        st, r = multipart_send(
            args.number, f'테스트{i}', doc,
            cover=args.cover, title=f'테스트 문서 {i}', memo='발신 테스트입니다')
        if st == 200 and r.get('ok'):
            ok += 1
            print(f'  [{i}/{args.count}] ✓ job_id={r.get("job_id")} '
                  f'status={r.get("status")} pages={r.get("pages")}'
                  f'{" (표지)" if r.get("cover_used") else ""}')
        else:
            fail += 1
            print(f'  [{i}/{args.count}] ✗ {r.get("detail", r)}')
        time.sleep(0.3)

    print(f'\n[5] 발송 결과: 성공 {ok} / 실패 {fail}')

    # 6. 이력 확인
    st, jobs = _req('GET', '/api/fax/jobs?limit=5')
    if st == 200 and jobs:
        print(f'\n[6] 최근 발송 이력 (상위 5건):')
        for j in jobs[:5]:
            print(f'  #{j["id"]} → {j["to_number"]} | {j["status"]} | {j.get("pages",0)}p | {j.get("file_name","")}')

    print(f'\n=== 완료 ===')
    print(f'웹에서 확인: {APP_URL}/ → 발송 이력')
    if health.get('fax_server'):
        print(f'팩스서버 대시보드: http://<서버IP>:8090/ → 최근 전송')


if __name__ == '__main__':
    main()

