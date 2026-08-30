#!/usr/bin/env python3
# ============================================================
#  faxtest8888.py - 8888 게이트웨이 발신 팩스 테스트 도구
#  예외처리·진단 강화판. 게이트웨이 루프백 팩스 송수신 검증용.
#
#  실행: python3 faxtest8888.py [보낼PDF경로] [반복횟수]
#  예:   python3 faxtest8888.py /tmp/fax/sample.pdf 1
#
#  표준 라이브러리만 사용.
# ============================================================
import subprocess, os, sys, time, uuid, re, json, shutil

# ---------------- 설정 ----------------
FS_CLI      = '/usr/local/freeswitch/bin/fs_cli'
FAX_DIR     = '/tmp/fax'
GW_NAME     = 'vega400'          # Vega 게이트웨이 SIP 프로파일명
SEND_NUMBER = '8888'             # 발신 번호 (게이트웨이 루프로)
GS_BIN      = 'gs'
ORIG_TIMEOUT= 130                # originate 타임아웃(초)
SETUP_WAIT  = 2                  # 발신 간 간격(초)


# ---------------- 공통 유틸 ----------------
def cerr(msg):  print(f'  [오류] {msg}')
def cok(msg):   print(f'  [정상] {msg}')
def cinfo(msg): print(f'  [정보] {msg}')


def which_or_die(binname):
    """필수 실행파일 존재 확인"""
    path = shutil.which(binname) or (binname if os.path.isfile(binname) else None)
    if not path:
        cerr(f'{binname} 를 찾을 수 없습니다. 설치/경로를 확인하세요.')
        return None
    return path


def preflight():
    """발신 전 환경 점검 - 예외를 미리 잡음"""
    print('── 사전 점검 ──────────────────────────')
    ok = True

    # 1) fs_cli 존재
    if not os.path.isfile(FS_CLI):
        cerr(f'fs_cli 없음: {FS_CLI}'); ok = False
    else:
        cok('fs_cli 확인')

    # 2) FreeSWITCH 응답
    try:
        r = subprocess.run([FS_CLI, '-x', 'status'],
                           capture_output=True, text=True, timeout=6)
        if r.returncode == 0 and r.stdout.strip():
            cok('FreeSWITCH 응답 정상')
        else:
            cerr('FreeSWITCH 무응답 - 실행 상태 확인'); ok = False
    except Exception as e:
        cerr(f'FreeSWITCH 확인 실패: {e}'); ok = False

    # 3) 게이트웨이 등록 상태
    try:
        r = subprocess.run([FS_CLI, '-x', f'sofia status gateway {GW_NAME}'],
                           capture_output=True, text=True, timeout=6)
        out = r.stdout
        m = re.search(r'Status\s+(\S+)', out)
        state = m.group(1) if m else 'UNKNOWN'
        if 'REGED' in out.upper() or 'UP' in state.upper() or 'NOREG' in out.upper():
            cok(f'게이트웨이 {GW_NAME} 상태: {state}')
        else:
            cerr(f'게이트웨이 {GW_NAME} 등록 이상: {state} (수신 경로면 무시 가능)')
            # 등록 안 돼도 발신 시도는 함 (peer 모드 가능성)
    except Exception as e:
        cerr(f'게이트웨이 상태 확인 실패: {e}')

    # 4) gs (PDF→TIFF 변환기)
    if which_or_die(GS_BIN):
        cok('ghostscript 확인')
    else:
        ok = False

    # 5) 팩스 디렉토리
    try:
        os.makedirs(FAX_DIR, exist_ok=True)
        testf = os.path.join(FAX_DIR, '.wtest')
        with open(testf, 'w') as f: f.write('x')
        os.remove(testf)
        cok(f'{FAX_DIR} 쓰기 가능')
    except Exception as e:
        cerr(f'{FAX_DIR} 쓰기 불가: {e}'); ok = False

    print('───────────────────────────────────────')
    return ok


def prepare_tiff(pdf_path):
    """PDF를 팩스 규격 TIFF로 변환. 이미 tif면 그대로 사용."""
    # 입력 검증
    if not pdf_path:
        raise ValueError('입력 파일 경로가 비었습니다')
    if any(ch in pdf_path for ch in [';', '|', '&', '`', '$', '\n']):
        raise ValueError(f'허용되지 않는 문자가 경로에 있음: {pdf_path}')
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f'파일 없음: {pdf_path}')

    if pdf_path.lower().endswith('.tif') or pdf_path.lower().endswith('.tiff'):
        return pdf_path

    tif = os.path.join(FAX_DIR, f'send_{uuid.uuid4().hex}.tif')
    try:
        r = subprocess.run(
            [GS_BIN, '-q', '-dNOPAUSE', '-dBATCH',
             '-sDEVICE=tiffg3', '-r204x98',
             f'-sOutputFile={tif}', pdf_path],
            capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError('gs 변환 타임아웃(60s) - 파일이 너무 크거나 손상')
    if r.returncode != 0 or not os.path.isfile(tif):
        raise RuntimeError(f'gs 변환 실패: {r.stderr[:200]}')
    if os.path.getsize(tif) == 0:
        raise RuntimeError('변환된 TIFF 크기가 0 - 원본 PDF 확인')
    return tif


def count_rx_before():
    try:
        return len([x for x in os.listdir(FAX_DIR)
                    if x.startswith('rx_') and x.endswith('.tif')])
    except Exception:
        return 0


def send_once(tif, seq):
    """8888로 1회 발신. 상세 예외처리 + 결과 판정."""
    print(f'\n── 발신 #{seq} ────────────────────────')

    # 발신 직전 FreeSWITCH 재확인 (죽어있으면 originate가 멈춤)
    try:
        alive = subprocess.run([FS_CLI, '-x', 'status'],
                               capture_output=True, text=True, timeout=5)
        if not alive.stdout.strip():
            cerr('발신 취소 - FreeSWITCH 무응답')
            return False
    except Exception as e:
        cerr(f'발신 취소 - 상태 확인 실패: {e}')
        return False

    rx_before = count_rx_before()

    # originate 문자열 구성
    #  - 게이트웨이 발신, T.38 활성, 실패 원인 캡처
    origin = (
        '{origination_caller_id_number=faxtest,'
        'fax_enable_t38=true,'
        'fax_enable_t38_request=true,'
        'absolute_codec_string=PCMA,PCMU,'
        'ignore_early_media=true,'
        f'originate_timeout={ORIG_TIMEOUT}}}'
        f'sofia/external/{SEND_NUMBER}@192.168.219.138 '
        f'&txfax({tif})'
    )

    cinfo(f'발신: {SEND_NUMBER} via {GW_NAME}')
    cinfo(f'파일: {tif}')

    try:
        r = subprocess.run([FS_CLI, '-x', 'originate ' + origin],
                           capture_output=True, text=True, timeout=ORIG_TIMEOUT + 10)
        out = (r.stdout or '').strip()
    except subprocess.TimeoutExpired:
        cerr(f'originate 타임아웃({ORIG_TIMEOUT}s) - 게이트웨이/루프 응답 없음')
        return False
    except Exception as e:
        cerr(f'originate 실행 오류: {e}')
        return False

    # 결과 판정
    if '+OK' in out:
        cok(f'발신 성공 응답: {out[:60]}')
    else:
        # 실패 원인별 안내
        cerr(f'발신 실패 응답: {out[:80]}')
        _diagnose_fail(out)
        return False

    # 수신 파일 증가 확인 (루프백이면 되돌아와 rxfax가 저장)
    cinfo('수신 대기 (최대 20초)...')
    for _ in range(20):
        time.sleep(1)
        if count_rx_before() > rx_before:
            cok('수신 파일 생성 확인 - 루프백 송수신 성공!')
            return True
    cerr('발신은 됐으나 수신 파일이 안 생김')
    cinfo('  → 루프백 케이블, 규칙B(E1→1002/8888), rxfax 다이얼플랜 확인')
    return False


def _diagnose_fail(out):
    """originate 실패 응답으로 원인 추정"""
    o = out.upper()
    table = [
        ('GATEWAY_DOWN',        '게이트웨이 다운 - Vega 등록/전원/네트워크 확인'),
        ('NO_ROUTE',            '라우팅 없음 - Vega 다이얼플랜 규칙A(SIP→E1) 확인'),
        ('UNALLOCATED',         '번호 미할당 - 8888 라우팅 규칙 확인'),
        ('CALL_REJECTED',       '거절됨 - Vega 인증/권한'),
        ('NORMAL_TEMPORARY',    'E1 링크 문제 - 링크 UP, 클럭(Internal) 확인'),
        ('NO_USER_RESPONSE',    '응답 없음 - 루프백 결선, D채널 상태'),
        ('RECOVERY_ON_TIMER',   'E1 계층 문제 - Q.921 확인'),
        ('DESTINATION_OUT_OF_ORDER', 'E1 포트 다운 - 링크 상태 확인'),
    ]
    for key, msg in table:
        if key in o:
            cinfo(f'  진단: {msg}')
            return
    cinfo('  진단: fs_cli 콘솔에서 console loglevel debug 로 상세 확인')


def main():
    print('=' * 48)
    print(' 8888 게이트웨이 발신 팩스 테스트')
    print('=' * 48)

    pdf = sys.argv[1] if len(sys.argv) > 1 else os.path.join(FAX_DIR, 'sample.pdf')
    repeat = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    # 사전 점검
    if not preflight():
        cerr('사전 점검 실패 - 위 항목을 해결 후 재시도')
        sys.exit(1)

    # TIFF 준비
    try:
        tif = prepare_tiff(pdf)
        cok(f'발신 파일 준비: {tif}')
    except Exception as e:
        cerr(f'파일 준비 실패: {e}')
        sys.exit(1)

    # 반복 발신
    success = 0
    for i in range(1, repeat + 1):
        if send_once(tif, i):
            success += 1
        if i < repeat:
            time.sleep(SETUP_WAIT)

    # 요약
    print('\n' + '=' * 48)
    print(f' 결과: {success}/{repeat} 성공', end='')
    print(f'  ({round(100*success/repeat)}%)' if repeat else '')
    print('=' * 48)
    if success < repeat:
        print(' 실패 건이 있으면 위 [진단] 메시지와')
        print(' fs_cli 의 console loglevel debug 로그를 확인하세요.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n중단됨')
    except Exception as e:
        print(f'\n예기치 못한 오류: {e}')
        sys.exit(1)

