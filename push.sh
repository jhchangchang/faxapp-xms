#!/bin/bash
# ============================================================
#  faxapp GitHub 업로드 스크립트
#  서버의 변경사항을 GitHub에 올림
#  사용법: bash push.sh "커밋 메시지"
# ============================================================
REPO_DIR="/opt/faxapp"          # git 저장소 위치 (환경에 맞게)
BRANCH="main"

cd "$REPO_DIR" || { echo "✗ $REPO_DIR 없음"; exit 1; }

# git 저장소인지 확인
if [ ! -d .git ]; then
  echo "✗ git 저장소가 아닙니다: $REPO_DIR"
  echo "  최초 1회: git init && git remote add origin <주소>"
  exit 1
fi

# 커밋 메시지 (인자 없으면 날짜)
MSG="${1:-update $(date '+%Y-%m-%d %H:%M')}"

echo "=== 변경사항 확인 ==="
git status --short

echo ""
echo "=== 민감파일 점검 (env/비밀번호) ==="
git status --short | grep -iE "\.env|password|secret" && echo "⚠ 민감파일 있음 - .gitignore 확인!" || echo "✓ 민감파일 없음"

echo ""
read -p "위 내용을 올릴까요? (y/n): " ok
[ "$ok" != "y" ] && { echo "취소됨"; exit 0; }

git add -A
git commit -m "$MSG"
git push origin "$BRANCH"

echo ""
echo "✓ 업로드 완료: $MSG"
