#!/bin/bash
# ============================================================
# collect.sh - [현재 운영 중인 온라인 VM에서 실행]
# 현재 VM의 실제 상태를 통째로 복제해 번들 생성.
# 폐쇄망에서 이 번들로 "지금과 똑같은 환경"을 재현.
#
# 담는 것:
#   1. 앱 전체         (/opt/faxapp)
#   2. FreeSWITCH 전체 (/usr/local/freeswitch)
#   3. FreeSWITCH 시스템 라이브러리 (libspandsp, libsofia-sip-ua 등)
#   4. DB 전체 덤프    (pg_dump)
#   5. RPM·Python 패키지 (psycopg-binary 포함)
# ============================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BUNDLE="$HERE/bundle"
source "$HERE/config.env"

if [ "$(id -u)" -ne 0 ]; then
  echo "⚠ root 권장: sudo ./collect.sh"
fi

echo "============================================"
echo " 현재 VM 상태 통째 복제 시작"
echo "============================================"

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE"/{rpms,python,freeswitch,fslibs,app,db}

# ── 1. 앱 전체 복제 ──
echo ""
echo "[1/7] 앱 전체 복제 중 ($INSTALL_ROOT)..."
if [ -d "$INSTALL_ROOT" ]; then
  # -C 부모디렉토리 후 basename → 풀 때 -C / 로 제자리 복원 (경로 꼬임 방지)
  tar czf "$BUNDLE/app/opt-faxapp.tar.gz" \
    --exclude='__pycache__' --exclude='*.pyc' \
    -C / "opt/$(basename "$INSTALL_ROOT")"
  echo "  → $(du -h "$BUNDLE/app/opt-faxapp.tar.gz" | cut -f1)"
else
  echo "  ✗ $INSTALL_ROOT 없음"
fi

# ── 2. FreeSWITCH 전체 복제 ──
echo ""
echo "[2/7] FreeSWITCH 복제 중 ($FS_PREFIX)..."
if [ -d "$FS_PREFIX" ]; then
  # FS_PREFIX가 /usr/local/freeswitch면 -C / 후 usr/local/freeswitch
  FS_REL="${FS_PREFIX#/}"   # 앞 슬래시 제거 → usr/local/freeswitch
  tar czf "$BUNDLE/freeswitch/freeswitch.tar.gz" \
    --exclude='log/*' --exclude='db/*' --exclude='*.pid' \
    -C / "$FS_REL"
  echo "  → $(du -h "$BUNDLE/freeswitch/freeswitch.tar.gz" | cut -f1)"
else
  echo "  ✗ $FS_PREFIX 없음"
fi

# ── 3. FreeSWITCH 시스템 라이브러리 수집 (핵심!) ──
echo ""
echo "[3/7] FreeSWITCH 시스템 라이브러리 수집 중..."
# freeswitch 바이너리가 의존하는 공유 라이브러리를 ldd로 추적.
# 특히 libspandsp(팩스), libsofia-sip-ua(SIP)는 /usr/local/lib 등에 별도 설치됨.
FSLIB_LIST="$BUNDLE/fslibs/liblist.txt"
: > "$FSLIB_LIST"

collect_libs_of() {
  local bin="$1"
  [ -f "$bin" ] || return
  # ldd 출력에서 실제 파일 경로 추출
  ldd "$bin" 2>/dev/null | awk '/=>/ {print $3} !/=>/ {print $1}' | grep '^/' | sort -u
}

# freeswitch 실행파일 + mod_*.so 들의 의존성 전부 추적
{
  collect_libs_of "$FS_PREFIX/bin/freeswitch"
  for mod in "$FS_PREFIX"/mod/*.so; do
    [ -f "$mod" ] && collect_libs_of "$mod"
  done
} | sort -u > "$FSLIB_LIST.raw"

# 시스템 기본 라이브러리(glibc 등)는 제외, /usr/local·비표준 위치의 것만 담기
# (OS 기본 lib은 RPM으로 설치되므로 중복 방지. 단 spandsp/sofia는 반드시 포함)
> "$FSLIB_LIST"
while read -r lib; do
  [ -z "$lib" ] && continue
  [ -f "$lib" ] || continue
  case "$lib" in
    # 반드시 담을 핵심 (팩스·SIP)
    *libspandsp*|*libsofia-sip-ua*|*libfreeswitch*)
      echo "$lib" >> "$FSLIB_LIST" ;;
    # /usr/local 경로의 라이브러리 (소스 빌드 산물)
    /usr/local/*)
      echo "$lib" >> "$FSLIB_LIST" ;;
    # /opt 경로
    /opt/*)
      echo "$lib" >> "$FSLIB_LIST" ;;
  esac
done < "$FSLIB_LIST.raw"

# 혹시 ldd로 못 잡은 경우 대비: 시스템 전체에서 직접 검색해 추가
for pat in "libspandsp.so*" "libsofia-sip-ua.so*"; do
  find /usr/local/lib /usr/local/lib64 /usr/lib64 /usr/lib -name "$pat" 2>/dev/null >> "$FSLIB_LIST"
done
sort -u "$FSLIB_LIST" -o "$FSLIB_LIST"

# 라이브러리들을 원래 절대경로 구조 그대로 tar에 담기 (-C / 로 풀면 제자리)
if [ -s "$FSLIB_LIST" ]; then
  # 절대경로 리스트를 상대경로로 변환해 -C / 기준 아카이빙
  sed 's|^/||' "$FSLIB_LIST" > "$FSLIB_LIST.rel"
  tar czf "$BUNDLE/fslibs/fslibs.tar.gz" -C / -T "$FSLIB_LIST.rel" 2>/dev/null
  echo "  → 라이브러리 $(wc -l < "$FSLIB_LIST")개 수집"
  echo "     (libspandsp, libsofia-sip-ua 등 포함)"
  # 어떤 핵심 라이브러리가 담겼는지 표시
  grep -E "libspandsp|libsofia" "$FSLIB_LIST" | sed 's|^|     ✓ |' || \
    echo "     ⚠ libspandsp/libsofia를 못 찾음 - 확인 필요!"
else
  echo "  ⚠ 추가 라이브러리 없음 (OS 기본 경로에만 있을 수 있음)"
fi
rm -f "$FSLIB_LIST.raw" "$FSLIB_LIST.rel"

# ── 4. DB 전체 덤프 ──
echo ""
echo "[4/7] 데이터베이스 덤프 중 ($DB_NAME)..."
export PGPASSWORD="$DB_PASS"
if command -v pg_dump >/dev/null 2>&1; then
  # postgres OS 계정으로 덤프 (인증 문제 회피)
  sudo -u postgres pg_dump "$DB_NAME" --no-owner --no-acl > "$BUNDLE/db/faxapp_dump.sql" 2>/dev/null || \
  pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
    --no-owner --no-acl > "$BUNDLE/db/faxapp_dump.sql" 2>/dev/null || \
    echo "  ⚠ pg_dump 실패 - DB 접속 확인"
  if [ -s "$BUNDLE/db/faxapp_dump.sql" ]; then
    echo "  → $(du -h "$BUNDLE/db/faxapp_dump.sql" | cut -f1) 덤프 완료"
  fi
else
  echo "  ⚠ pg_dump 없음"
fi

# ── 5. RPM 패키지 수집 ──
echo ""
echo "[5/7] RPM 패키지 다운로드 중..."
RPM_LIST=(
  postgresql-server postgresql
  python3 python3-pip
  libreoffice-headless ghostscript ImageMagick
  libtool-ltdl speex speexdsp opus libvorbis libogg
  libsndfile libtiff sqlite pcre openssl libcurl libedit
  ldns libuuid unixODBC
)
if command -v dnf >/dev/null 2>&1; then
  dnf download --resolve --alldeps --destdir="$BUNDLE/rpms" "${RPM_LIST[@]}" >/dev/null 2>&1 || \
    echo "  ⚠ 일부 RPM 실패 - 계속"
  echo "  → RPM $(ls "$BUNDLE/rpms" 2>/dev/null | wc -l)개"
fi

# ── 6. Python 패키지 수집 (psycopg 바이너리 명시!) ──
echo ""
echo "[6/7] Python 패키지 다운로드 중..."
if command -v pip3 >/dev/null 2>&1; then
  # psycopg-binary(C 컴파일본)와 psycopg-pool을 명시적으로 다운로드
  # [binary] extra만 믿지 않고 실제 패키지명으로 확실히 담음
  pip3 download -d "$BUNDLE/python" \
    fastapi "uvicorn[standard]" \
    psycopg psycopg-binary psycopg-pool \
    python-multipart bcrypt >/dev/null 2>&1 || \
    echo "  ⚠ 일부 Python 패키지 실패"
  # psycopg-binary가 실제로 담겼는지 검증
  if ls "$BUNDLE/python"/psycopg_binary* >/dev/null 2>&1 || ls "$BUNDLE/python"/psycopg-binary* >/dev/null 2>&1; then
    echo "  ✓ psycopg-binary 포함 확인"
  else
    echo "  ⚠ psycopg-binary가 안 담김 - DB 통신 문제 가능! 수동 확인 필요"
  fi
  echo "  → Python 패키지 $(ls "$BUNDLE/python" 2>/dev/null | wc -l)개"
fi

# ── 7. 매니페스트 + 번들 압축 ──
echo ""
echo "[7/7] 번들 생성 중..."
cat > "$BUNDLE/manifest.txt" << MANIFEST
FaxApp 시스템 복제 번들
생성일: $(date '+%Y-%m-%d %H:%M:%S')
생성 서버: $(hostname) / $(cat /etc/rocky-release 2>/dev/null || echo unknown)
------------------------------------------
앱:              $(du -h "$BUNDLE/app/opt-faxapp.tar.gz" 2>/dev/null | cut -f1 || echo 없음)
FreeSWITCH:      $(du -h "$BUNDLE/freeswitch/freeswitch.tar.gz" 2>/dev/null | cut -f1 || echo 없음)
FS 라이브러리:   $(du -h "$BUNDLE/fslibs/fslibs.tar.gz" 2>/dev/null | cut -f1 || echo 없음)
DB 덤프:         $(du -h "$BUNDLE/db/faxapp_dump.sql" 2>/dev/null | cut -f1 || echo 없음)
RPM:             $(ls "$BUNDLE/rpms" 2>/dev/null | wc -l)개
Python:          $(ls "$BUNDLE/python" 2>/dev/null | wc -l)개
------------------------------------------
포함: FreeSWITCH 설정(vega400.xml), 시스템 라이브러리(spandsp/sofia),
      DB 데이터, psycopg 바이너리
MANIFEST
cat "$BUNDLE/manifest.txt"

# install.sh와 config.env를 번들에 포함 (반드시 이 폴더의 최신본)
# 지난 사고 방지: 담을 install.sh가 FaxApp 최신본인지 검증
if grep -q "FaxApp 시스템 복제 설치" "$HERE/install.sh" 2>/dev/null; then
  echo "  ✓ install.sh 최신본 확인 (FaxApp)"
else
  echo "  ⚠ 경고: $HERE/install.sh 가 최신 FaxApp 버전이 아닐 수 있습니다!"
  echo "     (head -5 $HERE/install.sh 로 확인하세요)"
fi
cp "$HERE/install.sh" "$HERE/config.env" "$BUNDLE/"
# 샘플 생성 헬퍼도 포함 (있으면)
[ -f "$HERE/make_sample_fax.sh" ] && cp "$HERE/make_sample_fax.sh" "$BUNDLE/"

cd "$HERE"
tar czf faxapp-offline-bundle.tar.gz -C "$BUNDLE" .
echo ""
echo "============================================"
echo " 완료: faxapp-offline-bundle.tar.gz ($(du -h faxapp-offline-bundle.tar.gz | cut -f1))"
echo "============================================"
