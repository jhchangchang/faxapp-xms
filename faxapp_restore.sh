#!/bin/bash
# ============================================================
#  faxapp 복구 스크립트
#  백업에서 DB + 수신팩스 복원
#  사용법: bash faxapp_restore.sh /opt/faxapp/backups/20260101_030000
# ============================================================
set -e

BACKUP_DIR="$1"
DB_NAME="${FAXAPP_DB_NAME:-faxapp}"
DB_USER="${FAXAPP_DB_USER:-faxapp}"
DB_HOST="${FAXAPP_DB_HOST:-127.0.0.1}"
RECV_DIR="${FAXAPP_RECV_DIR:-/opt/faxapp/received}"

if [ -z "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ]; then
  echo "사용법: bash faxapp_restore.sh <백업폴더경로>"
  echo ""
  echo "사용 가능한 백업:"
  ls -1d ${FAXAPP_BACKUP_DIR:-/opt/faxapp/backups}/20* 2>/dev/null | tail -10
  exit 1
fi

echo "============================================================"
echo " faxapp 복구"
echo " 백업: $BACKUP_DIR"
echo "============================================================"
cat "$BACKUP_DIR/MANIFEST.txt" 2>/dev/null
echo ""
echo "⚠ 주의: 현재 DB와 수신파일이 백업 시점으로 덮어쓰기됩니다."
read -p "계속할까요? (yes 입력): " ok
[ "$ok" != "yes" ] && { echo "취소됨"; exit 0; }

# 서비스 중지 (복구 중 쓰기 방지)
echo ""
echo "서비스 중지..."
sudo systemctl stop faxapp 2>/dev/null || true

# 1. DB 복구
DB_FILE=$(ls "$BACKUP_DIR"/db_*.sql.gz 2>/dev/null | head -1)
if [ -n "$DB_FILE" ]; then
  echo "[1/2] DB 복구 중..."
  # 기존 DB 비우고 복원
  PGPASSWORD="${FAXAPP_DB_PASS:-faxapp}" psql -h "$DB_HOST" -U "$DB_USER" "$DB_NAME" \
    -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" 2>/dev/null
  gunzip -c "$DB_FILE" | PGPASSWORD="${FAXAPP_DB_PASS:-faxapp}" psql -h "$DB_HOST" -U "$DB_USER" "$DB_NAME"
  echo "  ✓ DB 복구 완료"
fi

# 2. 수신 파일 복구
RECV_FILE="$BACKUP_DIR/received.tar.gz"
if [ -f "$RECV_FILE" ]; then
  echo "[2/2] 수신 파일 복구 중..."
  tar -xzf "$RECV_FILE" -C "$(dirname "$RECV_DIR")"
  echo "  ✓ 수신 파일 복구 완료"
fi

# 서비스 재시작
echo ""
echo "서비스 재시작..."
sudo systemctl start faxapp 2>/dev/null || true

echo ""
echo "============================================================"
echo " ✓ 복구 완료"
echo "============================================================"
