#!/bin/bash
# ============================================================
# loadtest_direct.sh - V2PRI 30채널 부하 테스트 (IP 직결 방식)
# sofia/external/번호@Vega_IP 직결 → 검증된 방식
#
# 사용법: ./loadtest_direct.sh [채널수] [배치] [간격초]
#   ./loadtest_direct.sh 5 5 0     (먼저 5건=10채널 테스트)
#   ./loadtest_direct.sh 15 15 0   (15건=30채널, E1 풀용량)
# ============================================================
FS_CLI="/usr/local/freeswitch/bin/fs_cli"
SAMPLE_TIF="/tmp/fax/sample_multi.tif"
VEGA_IP="192.168.219.138"       # Vega 게이트웨이 IP (직결)
DEST="01012345678"                     # 수신 다이얼플랜
CHANNELS="${1:-30}"
BATCH="${2:-5}"
GAP="${3:-2}"

echo "=========================================="
echo " V2PRI 부하 테스트 (IP 직결 방식)"
echo " 채널:${CHANNELS} 배치:${BATCH} 간격:${GAP}초"
echo " 경로: FreeSWITCH → ${VEGA_IP}(Vega) → V2PRI → E1루프"
echo "=========================================="

if [ ! -f "$SAMPLE_TIF" ]; then
  echo "✗ 샘플 파일 없음: $SAMPLE_TIF"
  echo "  먼저 생성: bash make_sample_fax.sh"
  exit 1
fi

# 발송 방식 사전 검증 - 1건 직접 originate로 확인
echo "발송 방식 검증 중 (1건)..."
TEST=$($FS_CLI -x "originate {origination_caller_id_number=pretest,fax_enable_t38=true}sofia/external/${DEST}@${VEGA_IP} &txfax('${SAMPLE_TIF}')" 2>&1)
echo "  결과: $TEST"
if echo "$TEST" | grep -q "ERR"; then
  echo "⚠ 직결 발송도 실패. 계속하려면 Enter, 중단은 Ctrl+C"
  read
else
  echo "  ✓ 직결 발송 정상 - 부하 테스트 시작"
  sleep 3   # 검증 호 정리 대기
fi

START=$(date +%s)
sent=0

# 발송 투입 (IP 직결)
while [ "$sent" -lt "$CHANNELS" ]; do
  for j in $(seq 1 "$BATCH"); do
    [ "$sent" -ge "$CHANNELS" ] && break
    sent=$((sent+1))
    $FS_CLI -x "bgapi originate {origination_caller_id_number=load${sent},fax_enable_t38=true,fax_use_ecm=true}sofia/external/${DEST}@${VEGA_IP} &txfax('${SAMPLE_TIF}')" >/dev/null 2>&1 &
  done
  printf "\r  투입: %d/%d" "$sent" "$CHANNELS"
  sleep "$GAP"
done
echo ""
echo "[투입완료] 채널 활성 모니터링..."

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
echo " 투입: ${CHANNELS}건 | 최대 동시 활성: ${MAX_ACTIVE}채널"
echo " 소요: $((END-START))초"
echo "=========================================="
echo ""
echo "※ 루프백은 1건당 2채널(송신+수신) 사용"
echo "   → 15건 발송 시 최대 30채널이면 E1 풀용량 달성"
echo "※ 최대 활성이 30에서 막히면 V2PRI가 E1 30채널 처리 확인"
