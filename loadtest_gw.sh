#!/bin/bash
# ============================================================
# loadtest_gw.sh - V2PRI 보드 30채널 동시 부하 테스트
# 게이트웨이(vega400) 경유 → 실제 Vega + V2PRI + E1 회선 통과
#
# 사용법: ./loadtest_gw.sh [채널수] [배치크기] [간격초]
#   예: ./loadtest_gw.sh 30 5 2   (30채널, 5개씩 2초 간격)
#       ./loadtest_gw.sh 30 30 0  (30채널 한번에 몰아서)
# ============================================================
FS_CLI="/usr/local/freeswitch/bin/fs_cli"
SAMPLE_TIF="/tmp/fax/sample_multi.tif"
GW="vega400"                    # 게이트웨이 경유 (V2PRI 통과)
DEST="1002"                     # 수신 다이얼플랜 (루프 3번포트→rxfax)
CHANNELS="${1:-30}"
BATCH="${2:-5}"
GAP="${3:-2}"

echo "=========================================="
echo " V2PRI 30채널 부하 테스트 (게이트웨이 경유)"
echo " 채널:${CHANNELS} 배치:${BATCH} 간격:${GAP}초"
echo " 경로: FreeSWITCH → ${GW} → V2PRI → E1루프"
echo "=========================================="

# 샘플 파일 확인
if [ ! -f "$SAMPLE_TIF" ]; then
  echo "✗ 샘플 파일 없음: $SAMPLE_TIF"
  echo "  먼저 생성: bash make_sample_fax.sh"
  exit 1
fi

# 게이트웨이 상태 확인
GW_STATE=$($FS_CLI -x "sofia status gateway $GW" 2>/dev/null | grep -i "Status" | awk '{print $NF}')
echo "게이트웨이 $GW 상태: ${GW_STATE:-불명}"
if [ "$GW_STATE" != "UP" ]; then
  echo "⚠ 게이트웨이가 UP이 아님 - 계속하려면 Enter, 중단하려면 Ctrl+C"
  read
fi

START=$(date +%s)
sent=0

# 발송 투입
while [ "$sent" -lt "$CHANNELS" ]; do
  for j in $(seq 1 "$BATCH"); do
    [ "$sent" -ge "$CHANNELS" ] && break
    sent=$((sent+1))
    # 게이트웨이 경유 발송 (job_id 대신 load 식별자)
    $FS_CLI -x "bgapi originate {origination_caller_id_number=load${sent},fax_enable_t38=true,fax_use_ecm=true}sofia/gateway/${GW}/${DEST} &txfax(${SAMPLE_TIF})" >/dev/null 2>&1 &
  done
  printf "\r  투입: %d/%d" "$sent" "$CHANNELS"
  sleep "$GAP"
done
echo ""
echo "[투입완료] 채널 활성 상태 모니터링..."

# 활성 채널 추적 (최대 성능 지점 관찰)
MAX_ACTIVE=0
for t in $(seq 1 120); do
  A=$($FS_CLI -x "show channels count" 2>/dev/null | grep -oE '[0-9]+ total' | grep -oE '^[0-9]+')
  A=${A:-0}
  [ "$A" -gt "$MAX_ACTIVE" ] && MAX_ACTIVE=$A
  printf "\r  %3ds | 활성:%-4s | 최대:%-4s" "$t" "$A" "$MAX_ACTIVE"
  [ "$A" -le 1 ] && [ "$t" -gt 5 ] && { echo ""; break; }
  sleep 1
done
echo ""

END=$(date +%s)
echo "=========================================="
echo " 투입: ${CHANNELS}채널 | 최대 동시 활성: ${MAX_ACTIVE}채널"
echo " 소요: $((END-START))초"
echo "=========================================="
echo ""
echo "※ 최대 동시 활성 채널이 30에 가까우면 V2PRI 30채널 처리 성공"
echo "※ 그보다 낮으면: E1 채널 한계, 게이트웨이 설정, 또는 동시발신 상한 확인"
