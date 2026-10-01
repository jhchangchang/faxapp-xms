#!/bin/bash
# ============================================================
#  faxapp 백업 스크립트
#  DB + 수신팩스 + 설정을 날짜별로 백업
#  사용법: bash faxapp_backup.sh
#  자동화: crontab에 0 3 * * * /opt/faxapp/faxapp_backup.sh
# ============================================================
set -e

# ── 설정 (환경에 맞게) ──
DB_NAME="${FAXAPP_DB_NAME:-faxapp}"
DB_USER="${FAXAPP_DB_USER:-faxapp}"
DB_HOST="${FAXAPP_DB_HOST:-127.0.0.1}"
RECV_DIR="${FAXAPP_RECV_DIR:-/opt/faxapp/received}"
CONF="/etc/faxapp/faxapp.env"
BACKUP_ROOT="${FAXAPP_BACKUP_DIR:-/opt/faxapp/backups}"
RETAIN_DAYS="${FAXAPP_BACKUP_RETAIN:-30}"   # 보관 기간

TS=$(date '+%Y%m%d_%H%M%S')
DEST="$BACKUP_ROOT/$TS"
mkdir -p "$DEST"

echo "============================================================"
echo " faxapp 백업 시작: $TS"
echo " 저장 위치: $DEST"
echo "============================================================"

# 1. DB 백업 (pg_dump)
echo "[1/3] DB 백업 중..."
if command -v pg_dump >/dev/null; then
  PGPASSWORD="${FAXAPP_DB_PASS:-faxapp}" pg_dump -h "$DB_HOST" -U "$DB_USER" "$DB_NAME" \
    | gzip > "$DEST/db_${DB_NAME}.sql.gz"
  echo "  ✓ DB: $(du -h "$DEST/db_${DB_NAME}.sql.gz" | cut -f1)"
else
  echo "  ✗ pg_dump 없음 - DB 백업 건너뜀"
fi

# 2. 수신 팩스 파일 백업
echo "[2/3] 수신 파일 백업 중..."
if [ -d "$RECV_DIR" ]; then
  tar -czf "$DEST/received.tar.gz" -C "$(dirname "$RECV_DIR")" "$(basename "$RECV_DIR")" 2>/dev/null
  echo "  ✓ 수신파일: $(du -h "$DEST/received.tar.gz" | cut -f1)"
else
  echo "  - 수신 폴더 없음 (건너뜀)"
fi

# 3. 설정 파일 백업
echo "[3/3] 설정 백업 중..."
[ -f "$CONF" ] && cp "$CONF" "$DEST/faxapp.env.bak" && echo "  ✓ 설정 백업됨"

# 요약 기록
cat > "$DEST/MANIFEST.txt" << MANIFEST
faxapp 백업
일시: $(date)
DB: $DB_NAME @ $DB_HOST
포함: DB(sql.gz), 수신파일(tar.gz), 설정(env)
MANIFEST

# 오래된 백업 정리
echo ""
echo "오래된 백업 정리 (${RETAIN_DAYS}일 초과)..."
find "$BACKUP_ROOT" -maxdepth 1 -type d -name "20*" -mtime +$RETAIN_DAYS -exec rm -rf {} \; 2>/dev/null
REMAIN=$(find "$BACKUP_ROOT" -maxdepth 1 -type d -name "20*" | wc -l)

echo ""
echo "============================================================"
echo " ✓ 백업 완료: $DEST"
echo " 보관 중인 백업: ${REMAIN}개"
echo "============================================================"
