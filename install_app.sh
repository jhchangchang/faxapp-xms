#!/bin/bash
# ============================================================
# install_app.sh - faxapp 애플리케이션 + DB 설치 (XMS 연동 VM)
# 새 Rocky Linux VM에서 실행. FreeSWITCH 없이 faxapp+DB만.
#
# 사용법:
#   tar xzf faxapp-xms-bundle.tar.gz
#   sudo ./install_app.sh
# ============================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT="/opt/faxapp"
APP_DIR="$INSTALL_ROOT/web"
DB_NAME="faxapp"
DB_USER="faxapp"
DB_PASS="faxapp"                      # ★ 운영 시 반드시 변경

log()  { echo -e "\n[$1] $2"; }
ok()   { echo "  ✓ $1"; }
warn() { echo "  ⚠ $1"; }
# postgres 계정 실행은 /tmp에서 (현재 디렉토리 접근 경고 방지)
pg_run() { (cd /tmp && sudo -u postgres "$@"); }

echo "============================================"
echo " faxapp XMS 연동 VM 설치 시작"
echo " (faxapp + PostgreSQL, FreeSWITCH 없음)"
echo "============================================"

# ── 1. OS 패키지 설치 (오프라인 우선, 실패 시 온라인) ──
log "1/7" "OS 패키지 설치"
if [ -d "$HERE/rpm" ] && [ "$(ls "$HERE/rpm" 2>/dev/null | wc -l)" -gt 0 ]; then
  dnf install -y --disablerepo=* "$HERE"/rpm/*.rpm 2>&1 | tail -3 && \
    ok "오프라인 RPM 설치" || warn "오프라인 RPM 일부 실패 - 온라인 시도"
fi
# 핵심 패키지 보장 (온라인 가능하면)
dnf install -y python3.12 python3.12-pip postgresql-server postgresql-contrib 2>&1 | tail -2 || \
  warn "온라인 패키지 설치 건너뜀 (이미 설치됨 가정)"

# ── 2. faxapp OS 계정 ──
log "2/7" "faxapp 계정"
if ! id faxapp >/dev/null 2>&1; then
  useradd -r -s /sbin/nologin -d "$INSTALL_ROOT" faxapp 2>/dev/null && \
    ok "faxapp 계정 생성" || warn "faxapp 계정 생성 실패 (무시 가능)"
else
  ok "faxapp 계정 이미 존재"
fi

# ── 3. faxapp 애플리케이션 복원 ──
log "3/7" "faxapp 애플리케이션 복원"
mkdir -p "$INSTALL_ROOT"
if [ -f "$HERE/app/faxapp-web.tar.gz" ]; then
  tar xzf "$HERE/app/faxapp-web.tar.gz" -C "$INSTALL_ROOT"
  mkdir -p "$APP_DIR/uploads"
  ok "faxapp 복원 → $APP_DIR"
else
  echo "  ✗ app/faxapp-web.tar.gz 없음"; exit 1
fi

# ── 4. Python 가상환경 + 패키지 (오프라인 우선) ──
log "4/7" "Python 패키지 설치"
cd "$APP_DIR"
python3.12 -m venv venv 2>/dev/null || python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip 2>&1 | tail -1 || true
if [ -d "$HERE/python" ] && [ "$(ls "$HERE/python" 2>/dev/null | wc -l)" -gt 0 ]; then
  # 오프라인: 수집된 wheel로 설치
  pip install --no-index --find-links="$HERE/python" -r "$HERE/requirements.txt" 2>&1 | tail -3 || \
    warn "일부 패키지 오프라인 설치 실패"
  # psycopg 바이너리 명시적 (소스빌드 회피)
  pip install --no-index --find-links="$HERE/python" psycopg-binary psycopg-pool 2>&1 | tail -2 || true
else
  pip install -r "$HERE/requirements.txt" 2>&1 | tail -3 || warn "온라인 pip 설치"
fi
# psycopg 임포트 검증
python3 -c "import psycopg; print('  ✓ psycopg', psycopg.__version__)" || \
  { echo "  ✗ psycopg 임포트 실패"; deactivate; exit 1; }
deactivate

# ── 5. PostgreSQL 초기화 + 인증 설정 ──
log "5/7" "PostgreSQL 설정"
PGDATA="/var/lib/pgsql/data"
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  postgresql-setup --initdb 2>&1 | tail -2 || /usr/bin/postgresql-setup --initdb 2>&1 | tail -2
  ok "PostgreSQL initdb"
else
  ok "PostgreSQL 이미 초기화됨"
fi
# pg_hba.conf: host(TCP) 127.0.0.1·::1만 md5로, local(소켓)은 peer 유지
#  → 앱은 127.0.0.1로 접속(md5), sudo -u postgres 관리 접속은 비번없이(peer)
HBA="$PGDATA/pg_hba.conf"
if [ -f "$HBA" ]; then
  sed -i 's#\(^host.*127\.0\.0\.1/32.*\)ident#\1md5#; s#\(^host.*127\.0\.0\.1/32.*\)peer#\1md5#' "$HBA"
  sed -i 's#\(^host.*::1/128.*\)ident#\1md5#; s#\(^host.*::1/128.*\)peer#\1md5#' "$HBA"
  ok "pg_hba.conf 설정 (host=md5, local=peer 유지)"
fi
systemctl enable postgresql 2>/dev/null || true
systemctl restart postgresql
sleep 2

# ── 6. DB·사용자 생성 + 덤프 복원 + 소유권 ──
log "6/7" "DB 복원"
pg_run psql << PSQLEOF 2>&1 | tail -3 || true
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$DB_USER') THEN
    CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';
  END IF;
END \$\$;
SELECT 'DB생성' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='$DB_NAME');
PSQLEOF
pg_run psql -tc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 || \
  pg_run createdb -O "$DB_USER" "$DB_NAME"
ok "DB·사용자 준비"

# 덤프 복원 (/tmp 경유 + 권한, postgres가 읽도록)
if [ -f "$HERE/db/faxapp_dump.sql" ]; then
  cp "$HERE/db/faxapp_dump.sql" /tmp/faxapp_dump.sql
  chmod 644 /tmp/faxapp_dump.sql
  pg_run psql -d "$DB_NAME" -f /tmp/faxapp_dump.sql 2>&1 | tail -3 || warn "덤프 복원 일부 경고"
  # 모든 테이블·시퀀스 소유권을 faxapp으로 (앱이 쓰기 가능하도록)
  pg_run psql -d "$DB_NAME" << OWNEOF 2>&1 | tail -2 || true
DO \$\$ DECLARE r RECORD; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE 'ALTER TABLE public.'||quote_ident(r.tablename)||' OWNER TO $DB_USER';
  END LOOP;
  FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname='public' LOOP
    EXECUTE 'ALTER SEQUENCE public.'||quote_ident(r.sequencename)||' OWNER TO $DB_USER';
  END LOOP;
END \$\$;
GRANT ALL ON ALL TABLES IN SCHEMA public TO $DB_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $DB_USER;
OWNEOF
  rm -f /tmp/faxapp_dump.sql
  ok "덤프 복원 + 소유권 설정"
else
  warn "덤프 없음 - 빈 DB로 시작 (스키마 초기화 필요할 수 있음)"
fi
# faxapp 로그인 검증
PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" >/dev/null 2>&1 && \
  ok "faxapp DB 로그인 확인" || warn "faxapp DB 로그인 실패 - pg_hba 확인 필요"

# ── 7. 환경설정 + systemd 서비스 ──
log "7/7" "서비스 등록"
# .env (XMS 연동 정보는 나중에 추가)
cat > "$APP_DIR/.env" << ENVEOF
FAXAPP_DB_HOST=127.0.0.1
FAXAPP_DB_NAME=$DB_NAME
FAXAPP_DB_USER=$DB_USER
FAXAPP_DB_PASSWORD=$DB_PASS
# XMS 엔진 연동 (faxserver_xms 준비 후 설정)
# FAXAPP_FAX_SERVER=http://127.0.0.1:8090
ENVEOF
chown -R faxapp:faxapp "$INSTALL_ROOT" 2>/dev/null || true

# systemd 서비스
cat > /etc/systemd/system/faxapp.service << SVCEOF
[Unit]
Description=FaxApp - 애플리케이션 서버
After=postgresql.service
[Service]
Type=simple
User=faxapp
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8100
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable faxapp 2>/dev/null || true

# 방화벽 (앱 포트 8100)
firewall-cmd --permanent --add-port=8100/tcp 2>/dev/null || true
firewall-cmd --reload 2>/dev/null || true

systemctl start faxapp
sleep 3
echo ""
echo "============================================"
if systemctl is-active faxapp >/dev/null 2>&1; then
  echo " ✓ faxapp 설치 완료 · 실행 중"
  echo "   접속: http://$(hostname -I | awk '{print $1}'):8100"
else
  echo " ⚠ faxapp 서비스가 시작되지 않음"
  echo "   확인: journalctl -u faxapp -n 30"
fi
echo "============================================"
echo ""
echo "다음 단계:"
echo "  1. selfcheck 실행: cd $APP_DIR && venv/bin/python selfcheck.py"
echo "  2. XMS 연동 엔진(faxserver_xms) 준비 후 .env에 FAXAPP_FAX_SERVER 설정"
