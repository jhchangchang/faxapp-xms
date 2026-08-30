#!/bin/bash
FS_CLI="/usr/local/freeswitch/bin/fs_cli"
SAMPLE_TIF="/tmp/fax/sample_multi.tif"
FAX_DIR="/tmp/fax"
DEST="1002"
CHANNELS="${1:-30}"
BATCH="${2:-5}"        # 한 번에 던질 개수
GAP="${3:-2}"          # 배치 사이 간격(초)

echo "=== 부하 테스트: ${CHANNELS}채널 (${BATCH}개씩 ${GAP}초 간격) ==="
BEFORE=$(ls -1 "$FAX_DIR"/rx_*.tif 2>/dev/null | wc -l)
START=$(date +%s)

sent=0
while [ "$sent" -lt "$CHANNELS" ]; do
  for j in $(seq 1 "$BATCH"); do
    [ "$sent" -ge "$CHANNELS" ] && break
    sent=$((sent+1))
    $FS_CLI -x "bgapi originate {origination_caller_id_number=load${sent},fax_enable_t38=true}loopback/${DEST} &txfax(${SAMPLE_TIF})" >/dev/null 2>&1 &
  done
  printf "\r  투입: %d/%d" "$sent" "$CHANNELS"
  sleep "$GAP"
done
echo ""
echo "[투입완료] 완료 대기 중..."

for t in $(seq 1 300); do
  A=$($FS_CLI -x "show channels count" 2>/dev/null | grep -oE '[0-9]+ total' | grep -oE '^[0-9]+')
  A=${A:-0}
  printf "\r  %3ds | 활성:%-4s" "$t" "$A"
  [ "$A" -le 1 ] && [ "$t" -gt 5 ] && { echo ""; break; }
  sleep 1
done
echo ""

END=$(date +%s)
AFTER=$(ls -1 "$FAX_DIR"/rx_*.tif 2>/dev/null | wc -l)
NEW=$((AFTER-BEFORE))
echo "======================================"
echo " 요청: ${CHANNELS} | 성공: ${NEW} | 성공률: $((NEW*100/CHANNELS))%"
echo " 소요: $((END-START))초"
echo "======================================"
