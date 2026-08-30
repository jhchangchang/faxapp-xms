#!/usr/bin/env python3
"""
FaxApp 연결 진단 도구 (conncheck.py)
===================================
사무실 이전 후, 팩스 시스템 전체 연결을 단계별로 점검.
IP를 바꾼 뒤 "어디까지 정상이고 어디서 끊겼는지" 바로 파악.

표준 라이브러리만 사용 - 서버에 파일만 두면 실행 가능.

점검 순서 (실제 팩스 흐름 따라):
  1. 서버 자기 IP 확인
  2. Vega 게이트웨이 네트워크 도달 (ping)
  3. FreeSWITCH 실행 여부 (ESL 포트)
  4. FreeSWITCH ↔ Vega 게이트웨이 등록 상태 (sofia)
  5. 팩스 서버(:8090) 상태
  6. 앱(:8100) 상태 + DB 연결

사용법:
  python3 conncheck.py                          # 기본 (설정에서 IP 자동 추출)
  python3 conncheck.py --vega 192.168.0.101     # Vega IP 직접 지정
"""
import argparse
import re
import socket
import subprocess
import sys
import urllib.request

# ---------- 색상 (터미널) ----------
def c(text, color):
    codes = {'green': '92', 'red': '91', 'yellow': '93', 'blue': '94', 'gray': '90'}
    return f'\033[{codes.get(color, "0")}m{text}\033[0m'

OK = c('✓', 'green')
NO = c('✗', 'red')
WARN = c('!', 'yellow')

VEGA_XML = '/usr/local/freeswitch/etc/freeswitch/sip_profiles/external/vega400.xml'
FS_CLI = '/usr/local/freeswitch/bin/fs_cli'


def step(n, title):
    print(f'\n{c(f"[{n}]", "blue")} {title}')


def get_own_ip():
    """서버 자기 IP (외부로 나가는 인터페이스 기준)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def extract_vega_ip():
    """vega400.xml에서 proxy IP 추출."""
    try:
        with open(VEGA_XML) as f:
            content = f.read()
        m = re.search(r'proxy"\s+value="([0-9.]+)"', content)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def ping(ip, count=2):
    """ping 도달 확인."""
    try:
        r = subprocess.run(['ping', '-c', str(count), '-W', '2', ip],
                          capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def port_open(host, port, timeout=3):
    """TCP 포트 열림 확인."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        r = s.connect_ex((host, port))
        s.close()
        return r == 0
    except Exception:
        return False


def fs_command(cmd):
    """fs_cli 명령 실행."""
    try:
        r = subprocess.run([FS_CLI, '-x', cmd], capture_output=True,
                          timeout=10, text=True)
        return r.stdout
    except Exception as e:
        return None


def http_get(url, timeout=5):
    """HTTP GET."""
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return resp.status, resp.read().decode('utf-8', 'ignore')
    except Exception as e:
        return None, str(e)


def main():
    ap = argparse.ArgumentParser(description='FaxApp 연결 진단')
    ap.add_argument('--vega', default=None, help='Vega 게이트웨이 IP (미지정시 설정에서 추출)')
    ap.add_argument('--app', default='http://127.0.0.1:8100', help='앱 주소')
    ap.add_argument('--faxserver', default='http://127.0.0.1:8090', help='팩스서버 주소')
    args = ap.parse_args()

    print(c('=' * 56, 'gray'))
    print('  FaxApp 연결 진단')
    print(c('=' * 56, 'gray'))

    results = {}

    # 1. 서버 자기 IP
    step(1, '서버 자기 IP')
    own = get_own_ip()
    if own:
        print(f'   {OK} 서버 IP: {c(own, "green")}')
        print(f'      → 직원 접속주소: http://{own}:8100')
        print(f'      → Vega 설정의 SIP서버 IP를 이 값으로 맞추세요')
        results['own_ip'] = True
    else:
        print(f'   {NO} 자기 IP를 확인할 수 없습니다 (네트워크 미연결?)')
        results['own_ip'] = False

    # 2. Vega 게이트웨이 IP + 도달
    step(2, 'Vega 게이트웨이 연결')
    vega = args.vega or extract_vega_ip()
    if not vega:
        print(f'   {WARN} vega400.xml에서 IP를 못 찾음. --vega 로 지정하세요.')
        print(f'      설정 파일: {VEGA_XML}')
        results['vega_ping'] = None
    else:
        src = '설정파일' if not args.vega else '직접지정'
        print(f'   → Vega IP: {c(vega, "yellow")} ({src})')
        if ping(vega):
            print(f'   {OK} Vega 네트워크 도달 (ping 성공)')
            results['vega_ping'] = True
        else:
            print(f'   {NO} Vega에 ping 실패 - 네트워크 확인 필요')
            print(f'      · Vega 전원/랜선 확인')
            print(f'      · Vega IP가 {vega} 가 맞는지 확인')
            print(f'      · 같은 네트워크 대역인지 확인 (서버 {own})')
            results['vega_ping'] = False

    # 3. FreeSWITCH 실행
    step(3, 'FreeSWITCH 실행 상태')
    if port_open('127.0.0.1', 8021):
        print(f'   {OK} FreeSWITCH ESL 포트(8021) 열림 - 실행 중')
        results['fs_running'] = True
    else:
        print(f'   {NO} FreeSWITCH가 실행 중이 아닙니다 (8021 닫힘)')
        print(f'      systemctl status freeswitch 로 확인')
        results['fs_running'] = False

    # 4. 게이트웨이 등록 상태
    step(4, 'FreeSWITCH ↔ Vega 게이트웨이 등록')
    if results.get('fs_running'):
        out = fs_command('sofia status')
        if out:
            # 게이트웨이 라인 찾기
            gw_lines = [l for l in out.splitlines() if 'gateway' in l.lower() or 'vega' in l.lower()]
            print(f'   sofia status 결과:')
            for l in out.splitlines()[:12]:
                if l.strip():
                    print(f'      {c(l.strip(), "gray")}')
            # 상태 판정
            if 'REGED' in out or 'UP' in out:
                print(f'   {OK} 게이트웨이 등록/연결 상태 확인됨')
                results['gw_reg'] = True
            elif 'NOREG' in out or 'DOWN' in out or 'FAIL' in out:
                print(f'   {NO} 게이트웨이 미등록/실패 상태')
                print(f'      · vega400.xml의 proxy IP가 Vega 새 IP와 맞는지 확인')
                print(f'      · 수정 후: fs_cli -x "sofia profile external restart"')
                results['gw_reg'] = False
            else:
                print(f'   {WARN} 게이트웨이 상태를 명확히 판정 못함 - 위 결과 확인')
                results['gw_reg'] = None
        else:
            print(f'   {WARN} sofia status 실행 실패 (fs_cli 경로 확인: {FS_CLI})')
            results['gw_reg'] = None
    else:
        print(f'   {c("건너뜀", "gray")} (FreeSWITCH 미실행)')
        results['gw_reg'] = None

    # 5. 팩스 서버
    step(5, '팩스 서버 (:8090)')
    status, body = http_get(args.faxserver + '/status' if not args.faxserver.endswith('/') else args.faxserver)
    if status:
        print(f'   {OK} 팩스 서버 응답 (status {status})')
        results['faxserver'] = True
    else:
        # /status 없을 수 있으니 포트만 확인
        host = args.faxserver.split('//')[1].split(':')[0]
        port = int(args.faxserver.split(':')[-1])
        if port_open(host, port):
            print(f'   {OK} 팩스 서버 포트({port}) 열림')
            results['faxserver'] = True
        else:
            print(f'   {NO} 팩스 서버에 연결 안 됨')
            print(f'      팩스 서버(faxserver_v6.py)가 실행 중인지 확인')
            results['faxserver'] = False

    # 6. 앱 + DB
    step(6, '앱 (:8100) + DB')
    status, body = http_get(args.app + '/health')
    if status == 200:
        db_ok = '"database":true' in body.replace(' ', '')
        fax_ok = '"fax_server":true' in body.replace(' ', '')
        print(f'   {OK} 앱 응답 (status 200)')
        print(f'   {OK if db_ok else NO} 데이터베이스: {"연결됨" if db_ok else "연결 안 됨"}')
        print(f'   {OK if fax_ok else WARN} 팩스서버 연동: {"정상" if fax_ok else "확인 필요"}')
        results['app'] = True
        results['db'] = db_ok
    else:
        print(f'   {NO} 앱에 연결 안 됨: {body[:60]}')
        print(f'      systemctl status faxapp 로 확인')
        results['app'] = False

    # ---------- 종합 ----------
    print('\n' + c('=' * 56, 'gray'))
    print('  종합 진단')
    print(c('=' * 56, 'gray'))
    checks = [
        ('서버 네트워크', results.get('own_ip')),
        ('Vega 도달', results.get('vega_ping')),
        ('FreeSWITCH 실행', results.get('fs_running')),
        ('게이트웨이 등록', results.get('gw_reg')),
        ('팩스 서버', results.get('faxserver')),
        ('앱 + DB', results.get('app') and results.get('db')),
    ]
    for name, ok in checks:
        mark = OK if ok else (NO if ok is False else c('—', 'gray'))
        print(f'   {mark} {name}')

    # 첫 실패 지점 안내
    print()
    for name, ok in checks:
        if ok is False:
            print(f'   → 먼저 해결할 것: {c(name, "yellow")}')
            break
    else:
        if all(ok for _, ok in checks if ok is not None):
            print(f'   {c("전체 정상! 실제 팩스 송수신 테스트를 진행하세요.", "green")}')
    print(c('=' * 56, 'gray'))


if __name__ == '__main__':
    main()
