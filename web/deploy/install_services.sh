#!/bin/bash
# FaxApp + FaxServer systemd 서비스 설치 스크립트
# 사용: sudo bash install_services.sh

set -e
echo "=== FaxApp 자동시작 서비스 설치 ==="

# 1. uvicorn 실제 경로 확인
UVICORN=$(which uvicorn 2>/dev/null || echo "")
if [ -z "$UVICORN" ]; then
  echo "⚠ uvicorn을 찾을 수 없습니다. 경로를 확인하세요: pip show uvicorn"
  echo "  서비스 파일의 ExecStart 경로를 수동으로 맞춰야 합니다."
else
  echo "✓ uvicorn 경로: $UVICORN"
fi

# 2. 서비스 파일 복사
echo "서비스 파일을 /etc/systemd/system/ 로 복사..."
cp faxapp.service /etc/systemd/system/
cp faxserver.service /etc/systemd/system/

# 3. uvicorn 경로 자동 반영 (찾았으면)
if [ -n "$UVICORN" ]; then
  sed -i "s|/usr/local/bin/uvicorn|$UVICORN|" /etc/systemd/system/faxapp.service
fi

# 4. systemd 재로딩
systemctl daemon-reload

# 5. 부팅 시 자동시작 등록 + 지금 시작
echo "서비스 등록 및 시작..."
systemctl enable faxapp.service
systemctl start faxapp.service

# faxserver는 FreeSWITCH 설정에 따라 선택적으로
echo ""
echo "=== 설치 완료 ==="
echo "상태 확인:"
echo "  systemctl status faxapp"
echo "  systemctl status faxserver"
echo ""
echo "faxserver도 자동시작하려면:"
echo "  systemctl enable faxserver && systemctl start faxserver"
