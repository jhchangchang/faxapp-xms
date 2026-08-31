#!/bin/bash
# ============================================================
# FreeSWITCH → XMS146 게이트웨이 부하테스트
# fs_cli originate를 N개 동시 실행하여 10채널 송신 부하 검증.
#
# 사용법:
#   bash fs_load_xms.sh [동시채널수] [팩스파일]
#   bash fs_load_xms.sh 10 /tmp/load10.tif
#
# 사전조건:
#   - 게이트웨이 xms146 로드됨 (sofia status gateway xms146 → UP)
#   - 146에서 faxserver_xms 수신모드 실행중 (FAX_INBOUND=1)
#   - 발송할 팩스 TIFF 준비
# ============================================================

FS_CLI="/usr/local/freeswitch/bin/fs_cli"
GATEWAY="xms146"
DEST="rest"                          # 146이 받는 대상
CHANNELS="${1:-10}"                  # 동시 채널 수 (기본 10)
FAXFILE="${2:-/tmp/load10.tif}"      # 발송 팩스

echo "======================================================"
echo " FreeSWITCH → XMS146 부하테스트"
echo " 동시 채널: $CHANNELS"
echo " 팩스 파일: $FAXFILE"
echo " 게이트웨이: $GATEWAY → $DEST@146"
echo "======================================================"

# 파일 확인
if [ ! -f "$FAXFILE" ]; then
  echo "✗ 팩스 파일 없음: $FAXFILE"
  echo "  make_load_fax.py로 생성하거나 경로 확인"
  exit 1
fi

# 게이트웨이 상태 확인
GW_STATE=$($FS_CLI -x "sofia status gateway $GATEWAY" 2>/dev/null | grep -i "Status" | awk '{print $2}')
echo " 게이트웨이 상태: $GW_STATE"
echo ""

# 팩스 발송 함수 (백그라운드로 동시 실행)
send_fax() {
  local idx=$1
  local t0=$(date +%s.%N)
  # originate로 게이트웨이 통해 발송 + txfax 실행
  # fax_enable_t38=false → G.711 passthrough (V.34 유지)
  local result=$($FS_CLI -x "originate {origination_caller_id_number=fax$idx,fax_enable_t38=false,fax_use_ecm=true}sofia/gateway/$GATEWAY/$DEST &txfax($FAXFILE)" 2>&1)
  local t1=$(date +%s.%N)
  local elapsed=$(echo "$t1 - $t0" | bc)
  local ts=$(date +%H:%M:%S)
  if echo "$result" | grep -q "+OK"; then
    echo "  [$ts] ✓ #$idx 성공 (${elapsed}s)"
  else
    echo "  [$ts] ✗ #$idx 실패: $(echo $result | head -c 60) (${elapsed}s)"
  fi
}

echo " 발송 시작 ($CHANNELS채널 동시)..."
echo ""
START=$(date +%s.%N)

# N개 동시 발송
for i in $(seq 1 $CHANNELS); do
  send_fax $i &
  sleep 0.2   # 살짝 간격 (순간 폭주 완화)
done

# 모든 백그라운드 작업 완료 대기
wait

END=$(date +%s.%N)
TOTAL=$(echo "$END - $START" | bc)

echo ""
echo "======================================================"
echo " 전체 소요: ${TOTAL}초"
echo ""
echo " ※ 발송 결과(성공/실패)는 위 로그 참고"
echo " ※ 실제 팩스 협상 결과는:"
echo "   - FreeSWITCH 대시보드(:8090) → 최근 전송"
echo "   - 146 수신 엔진 로그 → [수신완료] bit_rate 확인"
echo "   - 146 수신 파일: /tmp/fax/rx/ (또는 설정 경로)"
echo "======================================================"

# 현재 활성 채널 확인
echo ""
echo " 현재 활성 채널:"
$FS_CLI -x "show channels count" 2>/dev/null
