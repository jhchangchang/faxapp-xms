#!/bin/bash
# ============================================================
# collect_app.sh - faxapp 애플리케이션 + DB만 수집 (XMS 연동 VM 이전용)
# FreeSWITCH는 제외 (XMS가 엔진 역할을 하므로 불필요)
#
# 현재 운영 중인 Rocky Linux VM에서 실행.
# 결과: faxapp-xms-bundle.tar.gz  (새 VM으로 복사)
# ============================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
STAGE="/tmp/faxapp_xms_collect"
INSTALL_ROOT="/opt/faxapp"           # faxapp 설치 경로
APP_DIR="$INSTALL_ROOT/web"          # 애플리케이션 루트
DB_NAME="faxapp"

echo "============================================"
echo " faxapp 애플리케이션 수집 (XMS 연동 VM 이전용)"
echo " ※ FreeSWITCH 제외 - faxapp + DB만"
echo "============================================"

rm -rf "$STAGE"; mkdir -p "$STAGE"/{app,db,python,rpm}

# ── 1. faxapp 웹 애플리케이션 압축 (/opt/faxapp/web) ──
echo "[1/6] faxapp 애플리케이션 압축..."
if [ -d "$APP_DIR" ]; then
  # web 디렉토리만 (faxserver_v6.py 등 FreeSWITCH 엔진은 제외)
  # --exclude는 반드시 압축대상(web)보다 앞에 위치해야 함
  tar czf "$STAGE/app/faxapp-web.tar.gz" \
      --exclude='web/uploads/*' \
      --exclude='*.log' \
      --exclude='__pycache__' \
      -C "$INSTALL_ROOT" web
  echo "  ✓ $APP_DIR 압축 완료"
else
  echo "  ✗ $APP_DIR 없음 - 경로 확인 필요"; exit 1
fi

# ── 2. PostgreSQL 덤프 (스키마 + 데이터, 소유권 제외) ──
echo "[2/6] DB 덤프..."
sudo -u postgres pg_dump --no-owner --no-acl "$DB_NAME" > "$STAGE/db/faxapp_dump.sql" 2>/dev/null && \
  echo "  ✓ DB 덤프 완료 ($(wc -l < "$STAGE/db/faxapp_dump.sql") 줄)" || \
  { echo "  ✗ DB 덤프 실패"; exit 1; }

# ── 3. Python 패키지 수집 (오프라인 설치용 wheel) ──
echo "[3/6] Python 의존성 수집..."
if [ -f "$APP_DIR/requirements.txt" ]; then
  pip download -r "$APP_DIR/requirements.txt" -d "$STAGE/python" 2>&1 | tail -3 || \
    echo "  ⚠ 일부 패키지 다운로드 경고 (계속)"
  # psycopg 바이너리 명시적 확보 (소스빌드 회피)
  pip download psycopg-binary psycopg-pool -d "$STAGE/python" 2>&1 | tail -2 || true
  echo "  ✓ Python 패키지 수집 ($(ls "$STAGE/python" | wc -l)개)"
else
  echo "  ⚠ requirements.txt 없음 - Python 패키지 건너뜀"
fi

# ── 4. requirements.txt 복사 ──
cp "$APP_DIR/requirements.txt" "$STAGE/" 2>/dev/null && echo "[4/6] requirements.txt 복사" || echo "[4/6] requirements.txt 없음"

# ── 5. RPM 패키지 수집 (python3.12, postgresql, nginx 등 OS 의존성) ──
echo "[5/6] OS RPM 패키지 수집..."
PKGS="python3.12 python3.12-pip postgresql-server postgresql-contrib"
dnf download --resolve --alldeps --destdir="$STAGE/rpm" $PKGS 2>&1 | tail -3 || \
  echo "  ⚠ RPM 다운로드 경고 (온라인 설치 가능하면 무시)"
echo "  ✓ RPM 수집 ($(ls "$STAGE/rpm" 2>/dev/null | wc -l)개)"

# ── 6. 매니페스트 + 번들 압축 ──
echo "[6/6] 번들 생성..."
cat > "$STAGE/MANIFEST.txt" << MANIFEST
faxapp XMS 연동 VM 이전 번들
============================
생성일: $(date '+%Y-%m-%d %H:%M:%S')
원본 서버: $(hostname)
구성: faxapp 애플리케이션 + PostgreSQL DB (FreeSWITCH 제외)

포함:
- app/faxapp-web.tar.gz   : faxapp 웹 애플리케이션 ($(du -h "$STAGE/app/faxapp-web.tar.gz" 2>/dev/null | cut -f1))
- db/faxapp_dump.sql      : DB 덤프 ($(du -h "$STAGE/db/faxapp_dump.sql" 2>/dev/null | cut -f1))
- python/                 : Python 패키지 ($(ls "$STAGE/python" 2>/dev/null | wc -l)개)
- rpm/                    : OS RPM ($(ls "$STAGE/rpm" 2>/dev/null | wc -l)개)

새 VM에서: install_app.sh 실행
MANIFEST

cp "$HERE/install_app.sh" "$STAGE/" 2>/dev/null || echo "  ⚠ install_app.sh를 번들에 못 넣음 (수동 복사 필요)"

BUNDLE="$HERE/faxapp-xms-bundle.tar.gz"
tar czf "$BUNDLE" -C "$STAGE" .
echo ""
echo "============================================"
echo " ✓ 수집 완료: $BUNDLE"
echo "   크기: $(du -h "$BUNDLE" | cut -f1)"
echo "============================================"
echo ""
echo "다음: 이 파일을 새 XMS VM으로 복사 후 압축 풀고 install_app.sh 실행"
echo "  scp $BUNDLE root@새VM:/root/"
