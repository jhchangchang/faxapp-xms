"""
표지(cover) 생성 모듈.
받는사람/보내는사람/제목/매수 등을 담은 표지 페이지를 만들어
본문 TIFF 앞에 병합.
표지는 HTML → PNG(ImageMagick) 방식, 없으면 텍스트 기반 이미지.
"""
import os
import uuid
import shutil
import subprocess
from datetime import datetime
from . import convert


def _esc(s):
    return (str(s or '')).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def build_cover_html(info, template='standard'):
    """표지 HTML 생성. template으로 스타일 선택.
    info: dict(to_name, to_number, from_name, from_number, title, pages, memo)
    template: standard(기본) / simple(간결) / official(공식)
    """
    builder = _TEMPLATES.get(template, _cover_standard)
    return builder(info)


def _cover_standard(info):
    """기본 - 격식 있는 표 형식 (진한 헤더 + 전달내용 박스)."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    @page {{ size: A4; margin: 0; }}
    body {{ font-family: 'Malgun Gothic','Noto Sans CJK KR',sans-serif; margin:0; padding:60px 70px; color:#1a2433; }}
    .hd {{ border-bottom:3px solid #1a2433; padding-bottom:18px; margin-bottom:34px; }}
    .hd h1 {{ font-size:44px; letter-spacing:8px; margin:0; }}
    .hd .en {{ font-size:14px; color:#888; letter-spacing:3px; margin-top:4px; }}
    table {{ width:100%; border-collapse:collapse; font-size:19px; }}
    td {{ padding:15px 10px; border-bottom:1px solid #ddd; vertical-align:top; }}
    td.k {{ width:130px; font-weight:bold; color:#555; background:#f7f9fc; }}
    .memo {{ margin-top:34px; }}
    .memo .kk {{ font-weight:bold; color:#555; margin-bottom:10px; font-size:17px; }}
    .memo .bx {{ border:1px solid #ddd; border-radius:6px; padding:18px; min-height:150px; font-size:16px; line-height:1.7; }}
    .ft {{ position:fixed; bottom:40px; left:70px; right:70px; font-size:13px; color:#999; border-top:1px solid #eee; padding-top:12px; text-align:center; }}
    </style></head><body>
    <div class="hd"><h1>팩 스 전 송</h1><div class="en">FACSIMILE TRANSMISSION</div></div>
    <table>
      <tr><td class="k">받는 사람</td><td>{_esc(info.get('to_name')) or '-'}</td>
          <td class="k">받는 번호</td><td>{_esc(info.get('to_number')) or '-'}</td></tr>
      <tr><td class="k">보내는 사람</td><td>{_esc(info.get('from_name')) or '-'}</td>
          <td class="k">보내는 번호</td><td>{_esc(info.get('from_number')) or '-'}</td></tr>
      <tr><td class="k">제목</td><td colspan="3">{_esc(info.get('title')) or '-'}</td></tr>
      <tr><td class="k">전송일시</td><td>{now}</td>
          <td class="k">매수</td><td>표지 포함 {int(info.get('pages',0))+1}매</td></tr>
    </table>
    <div class="memo"><div class="kk">전달 내용</div><div class="bx">{_esc(info.get('memo'))}</div></div>
    <div class="ft">본 팩스는 FaxApp 시스템을 통해 전송되었습니다.</div>
    </body></html>'''


def _cover_simple(info):
    """간결 - 최소한의 정보만, 여백 넉넉 (일상 업무용)."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    @page {{ size: A4; margin: 0; }}
    body {{ font-family: 'Malgun Gothic','Noto Sans CJK KR',sans-serif; margin:0; padding:90px 80px; color:#222; }}
    h1 {{ font-size:32px; margin:0 0 8px; font-weight:700; }}
    .sub {{ color:#888; font-size:15px; margin-bottom:50px; }}
    .row {{ display:flex; padding:16px 0; border-bottom:1px solid #eee; font-size:19px; }}
    .row .k {{ width:120px; color:#888; }}
    .row .v {{ flex:1; color:#222; }}
    .memo {{ margin-top:44px; font-size:17px; line-height:1.8; color:#333; white-space:pre-wrap; }}
    </style></head><body>
    <h1>FAX</h1><div class="sub">{now}</div>
    <div class="row"><div class="k">To</div><div class="v">{_esc(info.get('to_name')) or '-'} ({_esc(info.get('to_number')) or '-'})</div></div>
    <div class="row"><div class="k">From</div><div class="v">{_esc(info.get('from_name')) or '-'}</div></div>
    <div class="row"><div class="k">제목</div><div class="v">{_esc(info.get('title')) or '-'}</div></div>
    <div class="memo">{_esc(info.get('memo'))}</div>
    </body></html>'''


def _cover_official(info):
    """공식 - 관공서/계약용 격식체 (테두리 + 직인란)."""
    now = datetime.now().strftime('%Y년 %m월 %d일 %H:%M')
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    @page {{ size: A4; margin: 0; }}
    body {{ font-family: 'Batang','Noto Serif CJK KR',serif; margin:0; padding:50px 60px; color:#000; }}
    .frame {{ border:2px solid #000; padding:0; }}
    .title {{ text-align:center; font-size:34px; font-weight:700; letter-spacing:14px; padding:26px 0; border-bottom:2px solid #000; }}
    table {{ width:100%; border-collapse:collapse; font-size:18px; }}
    td {{ padding:16px 14px; border-bottom:1px solid #000; }}
    td.k {{ width:120px; font-weight:700; text-align:center; background:#f0f0f0; border-right:1px solid #000; }}
    .memo-t {{ padding:16px 14px; font-weight:700; border-bottom:1px solid #000; background:#f0f0f0; }}
    .memo-b {{ padding:22px; min-height:200px; font-size:16px; line-height:1.9; white-space:pre-wrap; }}
    .seal {{ text-align:right; padding:24px 30px; font-size:16px; }}
    .seal .box {{ display:inline-block; width:70px; height:70px; border:1px solid #999; border-radius:50%; margin-left:14px; vertical-align:middle; text-align:center; line-height:70px; color:#ccc; font-size:12px; }}
    </style></head><body>
    <div class="frame">
      <div class="title">팩 스 송 부 서</div>
      <table>
        <tr><td class="k">수 신</td><td>{_esc(info.get('to_name')) or '-'}</td>
            <td class="k">팩스번호</td><td>{_esc(info.get('to_number')) or '-'}</td></tr>
        <tr><td class="k">발 신</td><td>{_esc(info.get('from_name')) or '-'}</td>
            <td class="k">발신번호</td><td>{_esc(info.get('from_number')) or '-'}</td></tr>
        <tr><td class="k">제 목</td><td colspan="3">{_esc(info.get('title')) or '-'}</td></tr>
        <tr><td class="k">발송일시</td><td colspan="3">{now}</td></tr>
      </table>
      <div class="memo-t">내 용</div>
      <div class="memo-b">{_esc(info.get('memo'))}</div>
      <div class="seal">위와 같이 송부합니다. <span class="box">(인)</span></div>
    </div>
    </body></html>'''


_TEMPLATES = {
    'standard': _cover_standard,
    'simple': _cover_simple,
    'official': _cover_official,
}

# 사용 가능한 템플릿 목록 (UI용)
TEMPLATE_LIST = [
    {'id': 'standard', 'name': '기본', 'desc': '격식 있는 표 형식 (일반 업무용)'},
    {'id': 'simple', 'name': '간결', 'desc': '최소 정보, 깔끔한 여백'},
    {'id': 'official', 'name': '공식', 'desc': '관공서·계약용 격식체 (직인란 포함)'},
]


def make_cover_tiff(info, out_dir):
    """표지를 TIFF로 생성. 반환: 표지 TIFF 경로.
    info['template']으로 스타일 선택 (없으면 standard)."""
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, f'cover_{uuid.uuid4().hex}.html')
    template = info.get('template', 'standard')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(build_cover_html(info, template))

    # HTML → PDF (LibreOffice) → TIFF, 실패 시 대체
    try:
        with_pdf = convert.office_to_pdf(html_path, out_dir)  # soffice가 html도 처리
        cover_tiff = os.path.join(out_dir, f'cover_{uuid.uuid4().hex}.tif')
        convert.pdf_to_tiff(with_pdf, cover_tiff)
        return cover_tiff
    except Exception:
        # 대체: wkhtmltoimage 시도
        wk = shutil.which('wkhtmltoimage')
        if wk:
            png = os.path.join(out_dir, f'cover_{uuid.uuid4().hex}.png')
            subprocess.run([wk, '-q', html_path, png], timeout=40,
                           capture_output=True)
            if os.path.isfile(png):
                cover_tiff = os.path.join(out_dir, f'cover_{uuid.uuid4().hex}.tif')
                return convert.image_to_tiff(png, cover_tiff)
        raise


def merge_tiffs(tiff_list, out_dir):
    """여러 TIFF를 하나로 병합 (표지 + 본문). 반환: 병합 TIFF 경로."""
    convert_bin = shutil.which('convert')
    if not convert_bin:
        raise convert.ConvertError('ImageMagick 미설치 - 표지 병합 불가')
    out = os.path.join(out_dir, f'merged_{uuid.uuid4().hex}.tif')
    # tiffcp가 있으면 더 안전 (다중페이지 TIFF 병합)
    tiffcp = shutil.which('tiffcp')
    try:
        if tiffcp:
            subprocess.run([tiffcp] + tiff_list + [out], timeout=60,
                           capture_output=True, check=True)
        else:
            subprocess.run([convert_bin] + tiff_list + [out], timeout=60,
                           capture_output=True, check=True)
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        raise convert.ConvertError('병합 결과 없음')
    except subprocess.CalledProcessError as e:
        raise convert.ConvertError(f'TIFF 병합 실패: {e}')


def attach_cover(body_tiff, info, out_dir):
    """
    본문 TIFF 앞에 표지를 붙인 새 TIFF 생성.
    반환: (merged_tiff_path, total_pages)
    """
    cover = make_cover_tiff(info, out_dir)
    merged = merge_tiffs([cover, body_tiff], out_dir)
    pages = convert.count_tiff_pages(merged)
    return merged, pages
