#!/bin/bash
# 팩스 규격 샘플 TIFF 생성 (loadtest용)
# 서버에서 실행: bash make_sample_fax.sh
# ImageMagick(convert) 사용. 팩스 표준: A4 1728px, 흑백, Group4

FAX_DIR="/tmp/fax"
OUT="$FAX_DIR/sample_multi.tif"
mkdir -p "$FAX_DIR"

# 3페이지 각각 생성 후 멀티페이지로 합침
TMPD=$(mktemp -d)
for p in 1 2 3; do
  # 흰 배경 A4에 텍스트 (204x196 DPI 규격)
  convert -size 1728x2200 xc:white \
    -fill black -pointsize 70 \
    -annotate +120+200 "FAX TEST DOCUMENT" \
    -pointsize 45 \
    -annotate +120+320 "Page $p of 3 - Load Test Sample" \
    -annotate +120+450 "Resolution: 204x196 DPI (Fine)" \
    -annotate +120+520 "Format: TIFF Group 4" \
    -annotate +120+590 "1234567890 ABCDEFGHIJKLMNOPQRSTUVWXYZ" \
    -annotate +120+660 "The quick brown fox jumps over the lazy dog" \
    -draw "rectangle 40,40 1688,2160" \
    "$TMPD/page_$p.png"
done

# 멀티페이지 TIFF, Group4 압축, 팩스 해상도로
convert "$TMPD"/page_*.png \
  -density 204x196 -units PixelsPerInch \
  -threshold 50% -type bilevel \
  -compress Group4 \
  "$OUT"

rm -rf "$TMPD"

echo "생성 완료: $OUT"
identify "$OUT" 2>/dev/null | head -3
echo ""
echo "이제 loadtest 실행 가능:"
echo "  ./loadtest.sh 30 5 2"
