#!/usr/bin/env python3
"""
make_load_fax.py - 부하테스트용 N페이지 팩스 TIFF 생성

각 페이지에 페이지 번호와 내용을 넣어, 수신측에서 페이지 누락을 확인 가능.
사용법: python3 make_load_fax.py --pages 10 --out /tmp/load10.tif
필요: Pillow (pip install Pillow --break-system-packages)
"""
import argparse
try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:
    print("Pillow 필요: pip install Pillow --break-system-packages")
    raise

def make_page(num, total, text, font, font_s):
    W, H = 1728, 2200
    img = Image.new('1', (W, H), 1)
    d = ImageDraw.Draw(img)
    d.rectangle([(40, 40), (W-40, H-40)], outline=0, width=3)
    d.text((100, 120), f"{text}", fill=0, font=font)
    d.line([(100, 240), (1628, 240)], fill=0, width=4)
    # 큰 페이지 번호 (수신 확인용)
    d.text((100, 320), f"PAGE {num} / {total}", fill=0, font=font)
    d.text((100, 480), "V.34 SuperG3 33600bps Load Test", fill=0, font=font_s)
    d.text((100, 560), f"Dialogic PowerMedia XMS", fill=0, font=font_s)
    # 페이지마다 다른 패턴 (내용 무결성 확인)
    y = 700
    for i in range(15):
        d.text((100, y), f"Line {i+1:02d}: p{num} 0123456789 ABCDEFGH", fill=0, font=font_s)
        y += 70
    # 격자 (해상도 확인)
    for gx in range(0, 1500, 50):
        d.line([(120+gx, 1800), (120+gx, 2100)], fill=0, width=1)
    for gy in range(0, 300, 50):
        d.line([(120, 1800+gy), (1620, 1800+gy)], fill=0, width=1)
    return img

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pages', type=int, default=10)
    ap.add_argument('--out', default='/tmp/load10.tif')
    ap.add_argument('--text', default='XMS FAX LOAD TEST')
    args = ap.parse_args()

    try:
        font = ImageFont.truetype("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", 70)
        font_s = ImageFont.truetype("/usr/share/fonts/dejavu/DejaVuSans.ttf", 44)
    except Exception:
        font = font_s = ImageFont.load_default()

    pages = [make_page(i+1, args.pages, args.text, font, font_s) for i in range(args.pages)]
    # 팩스 흑백 해석 보정: 저장 전 반전 → 팩스 장비 해석 후 흰바탕 검은글씨
    pages = [ImageOps.invert(p.convert('L')).convert('1') for p in pages]
    # 멀티페이지 TIFF (Group4)
    pages[0].save(args.out, format='TIFF', compression='group4',
                  dpi=(204, 196), save_all=True, append_images=pages[1:])
    print(f"✓ {args.pages}페이지 팩스 생성: {args.out}")
    check = Image.open(args.out)
    n = 0
    try:
        while True:
            check.seek(n); n += 1
    except EOFError:
        pass
    print(f"  확인: {n}페이지, size={check.size}, mode={check.mode}")

if __name__ == '__main__':
    main()
