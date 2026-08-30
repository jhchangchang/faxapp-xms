#!/bin/bash
# ============================================================
# install.sh - [폐쇄망 깡통 VM에서 실행]
# collect.sh가 만든 번들로 "원본 VM과 똑같은 환경" 재현.
#
# 사용법:
#   tar xzf faxapp-offline-bundle.tar.gz
#   sudo ./install.sh
# ============================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/config.env"

if [ "$(id -u)" -ne 0 ]; then
  echo "root 권한으로 실행하세요: sudo ./install.sh"; exit 1
fi

log()  { echo -e "\n\033[1;34m[$1]\033[0m $2"; }
ok()   { echo -e "  \033[1;32m✓\033[0m $1"; }
warn() { echo -e "  \033[1;33m!\033[0m $1"; }

# postgres 계정으로 실행 (현재 디렉토리 접근 경고 방지 위해 /tmp에서)
pg_run() { (cd /tmp && sudo -u postgres "$@"); }

echo "============================================"
echo " FaxApp 시스템 복제 설치 시작"
echo "============================================"

# ── 1. RPM 패키지 오프라인 설치 ──
log "1/9" "시스템 패키지 설치 (RPM)"
if ls "$HERE/rpms/"*.rpm >/dev/null 2>&1; then
  dnf install -y --disablerepo="*" "$HERE/rpms/"*.rpm 2>&1 | tail -3 || \
    rpm -Uvh --replacepkgs --nodeps "$HERE/rpms/"*.rpm 2>&1 | tail -5 || \
    warn "일부 패키지는 이미 설치됨 (정상)"
  ok "RPM 설치 완료"
else
  warn "rpms 폴더 비어있음 - 건너뜀"
fi

# ── 2. faxapp OS 계정 생성 (필요 시) ──
log "2/9" "시스템 계정 확인"
# 앱 파일 소유·서비스 실행용 OS 계정. 없으면 생성.
if ! id faxapp >/dev/null 2>&1; then
  useradd -r -s /sbin/nologin -d "$INSTALL_ROOT" faxapp 2>/dev/null && \
    ok "faxapp 시스템 계정 생성" || warn "faxapp 계정 생성 실패 (무시 가능)"
else
  ok "faxapp 계정 이미 존재"
fi

# ── 3. Python 패키지 오프라인 설치 (psycopg 바이너리 강제 명시) ──
log "3/9" "Python 패키지 설치"
if ls "$HERE/python/"* >/dev/null 2>&1; then
  # psycopg-binary(C컴파일본)·psycopg-pool을 명시적으로 설치 (DB 통신 필수)
  PIP_PKGS="fastapi uvicorn psycopg psycopg-binary psycopg-pool python-multipart bcrypt"
  pip3 install --no-index --find-links "$HERE/python" $PIP_PKGS --break-system-packages 2>&1 | tail -3 || \
  pip3 install --no-index --find-links "$HERE/python" $PIP_PKGS 2>&1 | tail -3
  # psycopg 바이너리 설치 검증
  if python3 -c "import psycopg" 2>/dev/null; then
    ok "Python 패키지 설치 완료 (psycopg 정상)"
  else
    warn "psycopg import 실패 - DB 통신 확인 필요"
  fi
else
  warn "python 폴더 비어있음"
fi

# ── 4. 앱 복원 (-C / 로 제자리 복원, 경로 꼬임 방지) ──
log "4/9" "앱 복원"
if [ -f "$HERE/app/opt-faxapp.tar.gz" ]; then
  tar xzf "$HERE/app/opt-faxapp.tar.gz" -C /
  ok "앱 복원 완료 ($INSTALL_ROOT)"
else
  warn "앱 번들 없음!"
fi

# ── 5. FreeSWITCH 복원 + 시스템 라이브러리 + ldconfig (핵심!) ──
log "5/9" "FreeSWITCH 복원"
if [ -f "$HERE/freeswitch/freeswitch.tar.gz" ]; then
  tar xzf "$HERE/freeswitch/freeswitch.tar.gz" -C /
  ok "FreeSWITCH 복원 (vega400.xml·다이얼플랜 포함)"
else
  warn "FreeSWITCH 번들 없음"
fi

# 시스템 라이브러리 복원 (libspandsp, libsofia-sip-ua 등)
if [ -f "$HERE/fslibs/fslibs.tar.gz" ]; then
  tar xzf "$HERE/fslibs/fslibs.tar.gz" -C /
  ok "FreeSWITCH 라이브러리 복원 (spandsp/sofia)"
else
  warn "FS 라이브러리 번들 없음 - 팩스 엔진이 안 켜질 수 있음"
fi

# ★ ldconfig: OS가 /usr/local/lib64 라이브러리를 인식하게 (필수!)
echo "/usr/local/lib"   > /etc/ld.so.conf.d/freeswitch.conf
echo "/usr/local/lib64" >> /etc/ld.so.conf.d/freeswitch.conf
ldconfig
ok "라이브러리 경로 등록 + ldconfig 완료"

# FreeSWITCH systemd 서비스
if [ ! -f /etc/systemd/system/freeswitch.service ]; then
  cat > /etc/systemd/system/freeswitch.service << FSEOF
[Unit]
Description=FreeSWITCH
After=network.target
[Service]
Type=forking
ExecStart=$FS_PREFIX/bin/freeswitch -ncwait -nonat
ExecReload=$FS_PREFIX/bin/fs_cli -x reload
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
FSEOF
  ok "FreeSWITCH 서비스 생성"
fi

# ── 6. PostgreSQL 초기화 + 인증방식 수정 (ident/peer → md5) ──
log "6/9" "PostgreSQL 설정"
# postgres 계정 실행 시 현재 디렉토리 접근 경고 방지 (이후는 절대경로만 사용)
cd /tmp
PGDATA=/var/lib/pgsql/data
if [ ! -d "$PGDATA/base" ]; then
  postgresql-setup --initdb 2>&1 | tail -2 || \
    /usr/bin/postgresql-setup --initdb 2>&1 | tail -2 || \
    warn "initdb 실패 - 이미 초기화됐을 수 있음"
fi

# ★ pg_hba.conf 인증 방식 변경: ident/peer → md5 (비밀번호 접속 허용)
HBA="$PGDATA/pg_hba.conf"
if [ -f "$HBA" ]; then
  cp "$HBA" "$HBA.bak" 2>/dev/null || true
  # 구분자 # 사용 (정규식 내 | 를 OR로 정상 처리)
  # ★ 앱(faxapp)은 127.0.0.1로 접속하므로 host 항목만 md5로.
  #   local(Unix소켓)은 peer로 유지 → sudo -u postgres 관리 접속이 비번 없이 됨.
  # IPv4 로컬 (127.0.0.1/32): ident/peer/scram → md5
  sed -i -E 's#^(host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+)(ident|peer|scram-sha-256)#\1md5#' "$HBA"
  # IPv6 로컬 (::1/128)
  sed -i -E 's#^(host[[:space:]]+all[[:space:]]+all[[:space:]]+::1/128[[:space:]]+)(ident|peer|scram-sha-256)#\1md5#' "$HBA"
  # ※ local(peer) 항목은 그대로 둠 - postgres 관리 계정 접속 보호
  ok "pg_hba.conf 인증 방식 → md5 (비밀번호 접속 허용)"
else
  warn "pg_hba.conf 없음 - 경로 확인"
fi

systemctl enable postgresql 2>/dev/null || true
systemctl restart postgresql
sleep 2
ok "PostgreSQL 기동"

# DB·사용자 생성 (postgres OS 계정으로 - 인증 문제 없음)
sudo -u postgres psql << PSQLEOF 2>&1 | tail -3 || true
SELECT 'CREATE ROLE $DB_USER LOGIN PASSWORD ''$DB_PASS'''
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$DB_USER')\gexec
SELECT 'CREATE DATABASE $DB_NAME OWNER $DB_USER'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='$DB_NAME')\gexec
PSQLEOF
ok "DB·사용자 준비 ($DB_NAME / $DB_USER)"

# ── 7. DB 덤프 복원 (/tmp 경유 + 권한, postgres가 읽기) ──
log "7/9" "데이터베이스 복원"
if [ -f "$HERE/db/faxapp_dump.sql" ]; then
  # ★ /tmp로 복사 + 644 권한: postgres OS 계정이 읽을 수 있게
  #   (홈디렉토리/root 아래 파일은 postgres가 접근 못 하는 문제 방지)
  TMPDUMP=/tmp/faxapp_dump_$$.sql
  cp "$HERE/db/faxapp_dump.sql" "$TMPDUMP"
  chmod 644 "$TMPDUMP"
  # postgres 계정으로 복원 → 대상 DB에 데이터 적재
  sudo -u postgres psql -d "$DB_NAME" -f "$TMPDUMP" >/dev/null 2>&1 && \
    ok "DB 데이터 복원 완료 (원본과 동일)" || \
    warn "DB 복원 일부 경고 - selfcheck로 확인"
  rm -f "$TMPDUMP"

  # ★ 복원된 테이블 소유권·권한을 faxapp에 부여 (권한 문제 원천 차단)
  sudo -u postgres psql -d "$DB_NAME" << GRANTEOF >/dev/null 2>&1 || true
-- 모든 테이블 소유권을 faxapp로
DO \$\$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE 'ALTER TABLE public.' || quote_ident(r.tablename) || ' OWNER TO $DB_USER';
  END LOOP;
  FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname='public' LOOP
    EXECUTE 'ALTER SEQUENCE public.' || quote_ident(r.sequencename) || ' OWNER TO $DB_USER';
  END LOOP;
END\$\$;
GRANT ALL ON ALL TABLES IN SCHEMA public TO $DB_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $DB_USER;
GRANTEOF
  ok "DB 소유권·권한 faxapp로 이전 완료"

  # 접속 검증 (faxapp 계정으로 실제 붙어보기)
  export PGPASSWORD="$DB_PASS"
  if psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" >/dev/null 2>&1; then
    ok "faxapp 계정 DB 접속 검증 성공"
  else
    warn "faxapp 계정 접속 실패 - pg_hba.conf 확인"
  fi
else
  warn "DB 덤프 없음 - 빈 DB로 시작"
fi

# ── 8. 앱 환경설정 ──
log "8/9" "앱 설정"
if [ ! -f "$INSTALL_ROOT/web/.env" ]; then
  if [ -z "${APP_SECRET:-}" ]; then
    APP_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  fi
  cat > "$INSTALL_ROOT/web/.env" << ENVEOF
FAXAPP_DB_HOST=$DB_HOST
FAXAPP_DB_PORT=$DB_PORT
FAXAPP_DB_NAME=$DB_NAME
FAXAPP_DB_USER=$DB_USER
FAXAPP_DB_PASS=$DB_PASS
FAXAPP_SECRET=$APP_SECRET
FAXAPP_PORT=$APP_PORT
FAXAPP_UPLOAD_DIR=$INSTALL_ROOT/uploads
ENVEOF
  ok "환경설정 생성 (.env)"
else
  ok "기존 .env 유지"
fi
mkdir -p "$INSTALL_ROOT/uploads"
# 앱 디렉토리 소유권 (faxapp 계정이 있으면)
id faxapp >/dev/null 2>&1 && chown -R faxapp:faxapp "$INSTALL_ROOT" 2>/dev/null || true

# ── 9. 서비스 등록 + 방화벽 + 시작 ──
log "9/9" "서비스 등록 및 시작"
UVICORN=$(command -v uvicorn || echo /usr/local/bin/uvicorn)
if [ -f "$INSTALL_ROOT/web/deploy/faxapp.service" ]; then
  sed "s|ExecStart=.*uvicorn|ExecStart=$UVICORN|" \
    "$INSTALL_ROOT/web/deploy/faxapp.service" > /etc/systemd/system/faxapp.service
else
  cat > /etc/systemd/system/faxapp.service << APPEOF
[Unit]
Description=FaxApp
After=network.target postgresql.service
Wants=postgresql.service
[Service]
Type=simple
WorkingDirectory=$INSTALL_ROOT/web
EnvironmentFile=$INSTALL_ROOT/web/.env
ExecStart=$UVICORN app.main:app --host 0.0.0.0 --port $APP_PORT
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
APPEOF
fi

cat > /etc/systemd/system/faxserver.service << SRVEOF
[Unit]
Description=FaxServer - 팩스 엔진
After=network.target freeswitch.service
Wants=freeswitch.service
[Service]
Type=simple
WorkingDirectory=$INSTALL_ROOT
ExecStart=/usr/bin/python3 $INSTALL_ROOT/faxserver_v6.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
SRVEOF
ok "서비스 파일 등록"

if systemctl is-active firewalld >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=$APP_PORT/tcp >/dev/null 2>&1 || true
  firewall-cmd --permanent --add-port=$FAXSERVER_PORT/tcp >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  ok "방화벽 포트 개방 ($APP_PORT, $FAXSERVER_PORT)"
fi

systemctl daemon-reload
systemctl enable freeswitch faxserver faxapp >/dev/null 2>&1 || true
systemctl start freeswitch 2>/dev/null || warn "FreeSWITCH - 게이트웨이 연결 후 확인"
sleep 3
systemctl start faxserver 2>/dev/null || warn "faxserver 확인 필요"
systemctl start faxapp
sleep 2
ok "서비스 시작"

# ── 샘플 팩스 TIFF 생성 (loadtest·발송 테스트용) ──
log "+" "샘플 팩스 파일 생성"
FAX_DIR=/tmp/fax
mkdir -p "$FAX_DIR"
if command -v convert >/dev/null 2>&1; then
  TMPD=$(mktemp -d)
  for p in 1 2 3; do
    convert -size 1728x2200 xc:white \
      -fill black -pointsize 70 \
      -annotate +120+200 "FAX TEST DOCUMENT" \
      -pointsize 45 \
      -annotate +120+320 "Page $p of 3 - Sample" \
      -annotate +120+450 "Resolution: 204x196 DPI (Fine)" \
      -annotate +120+520 "Format: TIFF Group 4" \
      -annotate +120+590 "1234567890 ABCDEFGHIJKLMNOPQRSTUVWXYZ" \
      -draw "rectangle 40,40 1688,2160" \
      "$TMPD/page_$p.png" 2>/dev/null
  done
  convert "$TMPD"/page_*.png \
    -density 204x196 -units PixelsPerInch \
    -threshold 50% -type bilevel -compress Group4 \
    "$FAX_DIR/sample_multi.tif" 2>/dev/null && \
    ok "샘플 TIFF 생성 ($FAX_DIR/sample_multi.tif)" || \
    warn "샘플 TIFF 생성 실패 - make_sample_fax.sh 수동 실행"
  rm -rf "$TMPD"
else
  warn "convert(ImageMagick) 없음 - 샘플 TIFF 생성 건너뜀"
fi

# ── 검증 ──
echo ""
echo "============================================"
echo " 설치 완료 - 검증"
echo "============================================"
# FreeSWITCH 라이브러리 로딩 확인
if [ -x "$FS_PREFIX/bin/freeswitch" ]; then
  MISSING=$(ldd "$FS_PREFIX/bin/freeswitch" 2>/dev/null | grep "not found" | head -3)
  if [ -n "$MISSING" ]; then
    warn "FreeSWITCH 라이브러리 누락:"
    echo "$MISSING"
  else
    ok "FreeSWITCH 라이브러리 정상 (누락 없음)"
  fi
fi
if [ -f "$INSTALL_ROOT/web/selfcheck.py" ]; then
  (cd "$INSTALL_ROOT/web" && set -a && [ -f .env ] && source .env; set +a && python3 selfcheck.py) || \
    warn "selfcheck 일부 항목 확인 필요"
fi

echo ""
echo "============================================"
echo " 접속: http://$SERVER_IP:$APP_PORT"
echo " 팩스서버 대시보드: http://$SERVER_IP:$FAXSERVER_PORT"
echo "============================================"
