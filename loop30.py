#!/usr/bin/env python3
"""
loop30.py - faxapp 30채널 동시 발송 루프 테스트

실시간 채널 신호등이 어떻게 동작하는지 보기 위한 부하 테스트.
30개(또는 지정 수)를 동시에 발송해서 대시보드 채널이 켜지는 걸 확인.

사용법:
  python3 loop30.py --password admin비번
  python3 loop30.py --password 비번 --channels 30 --pages 5 --number 1002 --rounds 3

옵션:
  --channels N : 동시 발송 수 (기본 30)
  --pages N    : 각 팩스 페이지 수 (기본 5 - 길어야 채널이 오래 켜짐)
  --number     : 수신번호 (루프백 1002)
  --rounds N   : 반복 횟수 (기본 1, 계속 보려면 늘림)
  --interval   : 라운드 간 대기초 (기본 10)
  --host       : faxapp 주소 (기본 http://127.0.0.1:8100)

stdlib만 사용 (추가 설치 불필요).
"""
import argparse, sys, os, time, io, uuid, threading
import http.cookiejar, urllib.request, urllib.error, json

ap = argparse.ArgumentParser()
ap.add_argument('--host', default='http://127.0.0.1:8100')
ap.add_argument('--user', default='admin')
ap.add_argument('--password', required=True, help='로그인 비밀번호')
ap.add_argument('--channels', type=int, default=30, help='동시 발송 수')
ap.add_argument('--pages', type=int, default=5, help='팩스 페이지 수')
ap.add_argument('--number', default='1002', help='수신번호')
ap.add_argument('--rounds', type=int, default=1, help='반복 횟수')
ap.add_argument('--interval', type=int, default=10, help='라운드 간 대기(초)')
ap.add_argument('--gap', type=float, default=0.3, help='각 발송 간격(초) - rate limit 회피')
args = ap.parse_args()

BASE = args.host.rstrip('/')
_cj = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))

def _login():
    data = json.dumps({'username': args.user, 'password': args.password}).encode()
    req = urllib.request.Request(BASE+'/api/auth/login', data=data,
                                 method='POST', headers={'Content-Type':'application/json'})
    try:
        _opener.open(req, timeout=10).read()
        return True
    except Exception as e:
        print(f"✗ 로그인 실패: {e}")
        return False

def _make_tiff(pages):
    """간단한 팩스 TIFF를 메모리에 생성 (Pillow 있으면 사용, 없으면 최소 TIFF)."""
    try:
        from PIL import Image, ImageDraw
        imgs=[]
        for p in range(pages):
            im=Image.new('1',(1728,2200),1); d=ImageDraw.Draw(im)
            d.text((100,100),f'LOOP TEST p{p+1}/{pages}',fill=0)
            imgs.append(im)
        buf=io.BytesIO()
        imgs[0].save(buf,format='TIFF',compression='group4',save_all=True,append_images=imgs[1:])
        return buf.getvalue()
    except ImportError:
        # Pillow 없으면 로컬 파일 사용 시도
        for cand in ('/tmp/load10.tif','/tmp/color_test.tif'):
            if os.path.isfile(cand):
                return open(cand,'rb').read()
        print("✗ Pillow 없고 /tmp에 테스트 TIFF도 없음. pip install Pillow --break-system-packages");sys.exit(1)

def _send(idx, tiff_bytes, results):
    """1건 발송 (multipart)."""
    boundary='----loop'+uuid.uuid4().hex
    parts=[]
    def field(name,val):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    def filefield(name,fn,content):
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fn}"\r\n'
                      f'Content-Type: image/tiff\r\n\r\n').encode()+content+b'\r\n')
    field('number', args.number)
    field('to_name', f'loop{idx}')
    filefield('file', f'loop{idx}.tif', tiff_bytes)
    parts.append(f'--{boundary}--\r\n'.encode())
    body=b''.join(parts)
    req=urllib.request.Request(BASE+'/api/fax/send', data=body, method='POST',
                               headers={'Content-Type':f'multipart/form-data; boundary={boundary}'})
    t0=time.time()
    try:
        resp=_opener.open(req, timeout=30).read()
        results[idx]=('ok', time.time()-t0)
    except urllib.error.HTTPError as e:
        if e.code==429:
            results[idx]=('rate_limit', time.time()-t0)
        else:
            results[idx]=(f'HTTP {e.code}', time.time()-t0)
    except Exception as e:
        results[idx]=(f'err {str(e)[:30]}', time.time()-t0)

def _dashboard_channels():
    """현재 채널 상태 조회 (신호등 확인용)."""
    try:
        r=_opener.open(BASE+'/api/stats/dashboard', timeout=5).read()
        d=json.loads(r)
        ch=d.get('channels',{})
        return ch.get('busy',0), ch.get('total',0)
    except Exception:
        return None,None

def main():
    print("="*56)
    print(f" faxapp 30채널 루프 테스트")
    print(f"  대상: {BASE}")
    print(f"  동시발송: {args.channels}채널 × {args.pages}페이지")
    print(f"  수신번호: {args.number} / 라운드: {args.rounds}")
    print("="*56)

    if not _login():
        return 1
    print("✓ 로그인 성공\n")

    print("테스트 문서 생성 중...")
    tiff=_make_tiff(args.pages)
    print(f"✓ {args.pages}페이지 TIFF 생성 ({len(tiff)//1024}KB)\n")

    for rnd in range(1, args.rounds+1):
        print(f"━━━ 라운드 {rnd}/{args.rounds} ━━━")
        print(f"  {args.channels}개 동시 발송 시작... (대시보드에서 채널 불빛 확인!)")
        results={}
        threads=[]
        t0=time.time()
        for i in range(args.channels):
            th=threading.Thread(target=_send, args=(i, tiff, results))
            th.start(); threads.append(th)
            time.sleep(args.gap)  # rate limit 회피용 간격

        # 발송 진행 중 채널 상태 모니터링
        for _ in range(6):
            time.sleep(1)
            busy,total=_dashboard_channels()
            if busy is not None:
                bar='█'*busy+'░'*(total-busy) if total else ''
                print(f"  채널 사용: {busy}/{total}  [{bar}]")

        for th in threads: th.join()
        elapsed=time.time()-t0

        ok=sum(1 for v in results.values() if v[0]=='ok')
        fail=args.channels-ok
        print(f"\n  결과: 성공 {ok} / 실패 {fail} (소요 {elapsed:.1f}초)")
        if fail:
            errs={}
            for v in results.values():
                if v[0]!='ok': errs[v[0]]=errs.get(v[0],0)+1
            for e,c in errs.items(): print(f"    - {e}: {c}건")

        if rnd<args.rounds:
            print(f"\n  {args.interval}초 후 다음 라운드...\n")
            time.sleep(args.interval)

    print("\n"+"="*56)
    print(" 테스트 완료. 대시보드 '실시간 채널 상태'에서 확인하세요.")
    print("="*56)
    return 0

if __name__=='__main__':
    sys.exit(main())
