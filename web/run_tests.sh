#!/bin/bash
# 테스트 실행 (폐쇄망 대비: 필요 패키지 확인)
cd "$(dirname "$0")"
echo "=== faxapp 테스트 실행 ==="
python3 -m pytest tests/ -v --tb=short
