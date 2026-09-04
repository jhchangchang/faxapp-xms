"""
파일 변환 유틸 - 다양한 문서를 팩스용 TIFF(G3)로 변환.
- Office(doc/docx/xls/xlsx/ppt/pptx), 한글(hwp), 텍스트 → LibreOffice로 PDF → TIFF
- 이미지(jpg/png/gif/bmp) → 직접 TIFF
- PDF → 직접 TIFF
변환 도구: libreoffice(soffice), ghostscript(gs) 또는 ImageMagick(convert)
표준 라이브러리 + 외부 CLI 호출.
"""
import os
import shutil
import subprocess
import tempfile
import uuid

# 팩스 표준 해상도 (204x98 = Fine, 204x196 = Superfine)
FAX_RES = '204x196'

# 형식 분류
OFFICE_EXT = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
              '.hwp', '.hwpx', '.txt', '.rtf', '.odt', '.ods', '.odp'}
IMAGE_EXT  = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif'}
PDF_EXT    = {'.pdf'}


class ConvertError(Exception):
    pass


def _which(name):
    return shutil.which(name)


def _run(cmd, timeout=120):
    """CLI 실행. 실패 시 ConvertError."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise ConvertError(f'{cmd[0]} 실패: {r.stderr[:200]}')
        return r
    except subprocess.TimeoutExpired:
        raise ConvertError(f'{cmd[0]} 시간초과({timeout}s)')
    except FileNotFoundError:
        raise ConvertError(f'{cmd[0]} 미설치')


def hwp_to_pdf(src, out_dir):
    """한글(hwp) → PDF 변환.
    구형 hwp는 LibreOffice가 직접 못 여는 경우가 많으므로,
    hwp5odt(pyhwp)로 ODT 변환 후 LibreOffice로 PDF 생성.
    실패 시 LibreOffice 직접 변환 시도(fallback)."""
    ext = os.path.splitext(src)[1].lower()
    base = os.path.splitext(os.path.basename(src))[0]

    # hwpx(신형 XML)는 LibreOffice가 직접 처리 가능하므로 바로 시도
    if ext == '.hwpx':
        try:
            return office_to_pdf(src, out_dir)
        except ConvertError:
            pass  # 실패하면 아래 hwp5 경로로

    # 구형 hwp: hwp5odt로 ODT 변환 → LibreOffice로 PDF
    hwp5odt = _which('hwp5odt')
    if hwp5odt:
        try:
            odt = os.path.join(out_dir, base + '.odt')
            _run([hwp5odt, '--embed-image', '--output', odt, src], timeout=120)
            if os.path.isfile(odt):
                # ODT → PDF (LibreOffice)
                return office_to_pdf(odt, out_dir)
        except ConvertError:
            pass  # fallback으로

    # fallback: LibreOffice 직접 (h2orestart 확장이 있으면 동작)
    try:
        return office_to_pdf(src, out_dir)
    except ConvertError as e:
        raise ConvertError(
            f'한글(hwp) 변환 실패. pyhwp(hwp5odt) 설치 또는 '
            f'LibreOffice h2orestart 확장이 필요합니다: {e}')


def office_to_pdf(src, out_dir):
    """LibreOffice로 Office/한글/텍스트 → PDF."""
    soffice = _which('libreoffice') or _which('soffice')
    if not soffice:
        raise ConvertError('libreoffice 미설치 - Office 변환 불가')
    _run([soffice, '--headless', '--convert-to', 'pdf',
          '--outdir', out_dir, src], timeout=120)
    base = os.path.splitext(os.path.basename(src))[0]
    pdf = os.path.join(out_dir, base + '.pdf')
    if not os.path.isfile(pdf):
        raise ConvertError('PDF 변환 결과 없음')
    return pdf


def _convert_to_tiff(convert_bin, pre_args, src, out_tiff):
    """ImageMagick 변환. 압축 방식 폴백(Group3→Group4→none)."""
    for comp in ('Group4', 'Group3', 'LZW', 'None'):
        cmd = [convert_bin] + pre_args + [src, '-resize', '1728x',
               '-monochrome', '-compress', comp, out_tiff]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and os.path.isfile(out_tiff) and os.path.getsize(out_tiff) > 0:
                return out_tiff
        except subprocess.TimeoutExpired:
            raise ConvertError('convert 시간초과')
    raise ConvertError('TIFF 변환 실패 (모든 압축 방식)')


def pdf_to_tiff(src, out_tiff):
    """PDF → 팩스 TIFF (G3/G4). ghostscript 우선, 없으면 ImageMagick.
    보안: 악의적 PDF의 RCE 방어를 위해 -dSAFER 등 샌드박스 옵션 필수.
    """
    gs = _which('gs') or _which('ghostscript')
    if gs:
        # -dSAFER: 파일 접근 제한 (RCE 방어 핵심)
        # -dPARANOIDSAFER: 더 엄격한 제한
        # -dNOSAFER 절대 금지. 외부 PDF는 신뢰 불가.
        _run([gs, '-q', '-dNOPAUSE', '-dBATCH', '-dSAFER', '-dPARANOIDSAFER',
              '-dNOOUTERSAVE', '-sDEVICE=tiffg3',
              f'-r{FAX_RES}', f'-sOutputFile={out_tiff}', src], timeout=120)
        if os.path.isfile(out_tiff) and os.path.getsize(out_tiff) > 0:
            return out_tiff
    convert = _which('convert')
    if not convert:
        raise ConvertError('gs/convert 둘 다 미설치 - TIFF 변환 불가')
    return _convert_to_tiff(convert, ['-density', '204'], src, out_tiff)


def image_to_tiff(src, out_tiff):
    """이미지 → 팩스 TIFF."""
    convert = _which('convert')
    if not convert:
        raise ConvertError('ImageMagick(convert) 미설치 - 이미지 변환 불가')
    return _convert_to_tiff(convert, [], src, out_tiff)


def to_fax_tiff(src_path, out_dir):
    """
    입력 파일을 팩스용 TIFF로 변환. 형식 자동 판별.
    반환: (tiff_path, page_estimate)
    """
    if not os.path.isfile(src_path):
        raise ConvertError(f'파일 없음: {src_path}')
    ext = os.path.splitext(src_path)[1].lower()
    os.makedirs(out_dir, exist_ok=True)
    out_tiff = os.path.join(out_dir, f'fax_{uuid.uuid4().hex}.tif')

    if ext in IMAGE_EXT and ext not in ('.tif', '.tiff'):
        image_to_tiff(src_path, out_tiff)
    elif ext in ('.tif', '.tiff'):
        shutil.copy2(src_path, out_tiff)   # 이미 TIFF
    elif ext in PDF_EXT:
        pdf_to_tiff(src_path, out_tiff)
    elif ext in OFFICE_EXT:
        with tempfile.TemporaryDirectory() as tmp:
            # 한글(hwp/hwpx)은 전용 변환 파이프라인 사용
            if ext in ('.hwp', '.hwpx'):
                pdf = hwp_to_pdf(src_path, tmp)
            else:
                pdf = office_to_pdf(src_path, tmp)
            pdf_to_tiff(pdf, out_tiff)
    else:
        raise ConvertError(f'지원하지 않는 형식: {ext}')

    return out_tiff, count_tiff_pages(out_tiff)


def count_tiff_pages(tiff_path):
    """TIFF 페이지 수 추정 (identify 사용, 없으면 1)."""
    identify = _which('identify')
    if identify:
        try:
            r = subprocess.run([identify, tiff_path],
                               capture_output=True, text=True, timeout=20)
            return max(1, len(r.stdout.strip().split('\n')))
        except Exception:
            pass
    return 1


def slice_tiff_pages(src_tiff, start_page, out_tiff, end_page=None):
    """TIFF에서 start_page~end_page 페이지만 추출해 새 TIFF 생성 (이어보내기용).

    페이지 번호는 1-기반. start_page=49면 49페이지부터 끝까지.
    실패 시점 이후 페이지만 재전송하여 통신료·시간·수신측 토너 절약.

    우선순위: tiffcp(가장 정확) → ImageMagick convert → 실패 시 예외.
    반환: out_tiff 경로. 추출 실패 시 ConvertError.
    """
    import os
    total = count_tiff_pages(src_tiff)
    start = max(1, int(start_page))
    end = int(end_page) if end_page else total
    if start > total:
        raise ConvertError(f'시작 페이지({start})가 전체({total})보다 큼')

    # 방법 1: tiffcp (libtiff) - 0-기반 페이지 지정, 팩스 TIFF에 최적
    tiffcp = _which('tiffcp')
    if tiffcp:
        # tiffcp는 src,N 형식으로 특정 페이지 지정 (0-기반)
        pages = [f'{src_tiff},{i}' for i in range(start - 1, end)]
        try:
            _run([tiffcp] + pages + [out_tiff], timeout=60)
            if os.path.isfile(out_tiff) and os.path.getsize(out_tiff) > 0:
                return out_tiff
        except Exception:
            pass  # 다음 방법 시도

    # 방법 2: ImageMagick convert - [start-1] ~ [end-1] (0-기반 범위)
    convert = _which('convert')
    if convert:
        rng = f'[{start-1}-{end-1}]' if end > start else f'[{start-1}]'
        try:
            _run([convert, src_tiff + rng, '-compress', 'Group4', out_tiff], timeout=60)
            if os.path.isfile(out_tiff) and os.path.getsize(out_tiff) > 0:
                return out_tiff
        except Exception:
            pass

    raise ConvertError('페이지 슬라이싱 실패 (tiffcp/convert 필요)')


def slice_pdf_pages(src_pdf, start_page, out_tiff, end_page=None):
    """PDF에서 start_page~end_page만 추출해 팩스 TIFF 생성 (gs).
    원본이 PDF로 보관된 경우의 이어보내기용. 페이지 1-기반."""
    import os
    gs = _which('gs') or _which('ghostscript')
    if not gs:
        raise ConvertError('gs 미설치 - PDF 페이지 추출 불가')
    args = [gs, '-q', '-dNOPAUSE', '-dBATCH', '-dSAFER',
            '-sDEVICE=tiffg4', f'-r{FAX_RES}',
            f'-dFirstPage={int(start_page)}']
    if end_page:
        args.append(f'-dLastPage={int(end_page)}')
    args += [f'-sOutputFile={out_tiff}', src_pdf]
    _run(args, timeout=120)
    if os.path.isfile(out_tiff) and os.path.getsize(out_tiff) > 0:
        return out_tiff
    raise ConvertError('PDF 페이지 추출 실패')


def parse_page_spec(spec, total):
    """페이지 지정 문자열 → 정렬된 페이지 번호 리스트 (1-기반, 중복제거).

    예: "2,8" → [2,8]  /  "3-5" → [3,4,5]  /  "1,3-5,8" → [1,3,4,5,8]
    total 범위를 벗어나는 건 제외. 잘못된 입력은 무시.
    """
    if not spec:
        return []
    pages = set()
    for part in str(spec).replace(' ', '').split(','):
        if not part:
            continue
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                a, b = int(a), int(b)
                for p in range(min(a, b), max(a, b) + 1):
                    if 1 <= p <= total:
                        pages.add(p)
            except ValueError:
                continue
        else:
            try:
                p = int(part)
                if 1 <= p <= total:
                    pages.add(p)
            except ValueError:
                continue
    return sorted(pages)


def slice_selected_pages(src, page_list, out_tiff):
    """지정한 페이지들만 추출해 새 TIFF 생성 (연속 아니어도 됨).

    page_list: 1-기반 페이지 번호 리스트 (예: [2, 8] 또는 [3,4,5]).
    특정 페이지만 재전송(누락 페이지 대응)용.
    TIFF/PDF 원본 모두 지원.
    """
    import os
    if not page_list:
        raise ConvertError('추출할 페이지가 없습니다')
    low = src.lower()

    if low.endswith('.pdf'):
        # PDF: gs로 페이지 목록 추출 (-sPageList)
        gs = _which('gs') or _which('ghostscript')
        if gs:
            plist = ','.join(str(p) for p in page_list)
            _run([gs, '-q', '-dNOPAUSE', '-dBATCH', '-dSAFER',
                  '-sDEVICE=tiffg4', f'-r{FAX_RES}',
                  f'-sPageList={plist}',
                  f'-sOutputFile={out_tiff}', src], timeout=120)
            if os.path.isfile(out_tiff) and os.path.getsize(out_tiff) > 0:
                return out_tiff
        raise ConvertError('PDF 선택 페이지 추출 실패')

    # TIFF: tiffcp (0-기반) 또는 ImageMagick
    tiffcp = _which('tiffcp')
    if tiffcp:
        pages = [f'{src},{p-1}' for p in page_list]  # 0-기반
        try:
            _run([tiffcp] + pages + [out_tiff], timeout=60)
            if os.path.isfile(out_tiff) and os.path.getsize(out_tiff) > 0:
                return out_tiff
        except Exception:
            pass
    convert = _which('convert')
    if convert:
        # convert src[1,7] 처럼 0-기반 인덱스 나열
        idx = ','.join(str(p-1) for p in page_list)
        try:
            _run([convert, f'{src}[{idx}]', '-compress', 'Group4', out_tiff], timeout=60)
            if os.path.isfile(out_tiff) and os.path.getsize(out_tiff) > 0:
                return out_tiff
        except Exception:
            pass
    raise ConvertError('선택 페이지 슬라이싱 실패 (tiffcp/convert 필요)')


def supported_formats():
    """지원 형식 목록 반환."""
    return {
        'office': sorted(OFFICE_EXT),
        'image': sorted(IMAGE_EXT),
        'pdf': sorted(PDF_EXT),
    }


# ---------- 미리보기용 변환 ----------
def to_preview_png(src_path, out_dir, page=0, max_width=1000):
    """
    TIFF/PDF/이미지를 미리보기용 PNG로 변환 (브라우저 표시용).
    page: 0-기반 페이지 번호. 반환: PNG 경로.
    """
    if not os.path.isfile(src_path):
        raise ConvertError(f'파일 없음: {src_path}')
    os.makedirs(out_dir, exist_ok=True)
    out_png = os.path.join(out_dir, f'pv_{uuid.uuid4().hex}.png')
    convert = _which('convert')
    if not convert:
        raise ConvertError('ImageMagick(convert) 미설치 - 미리보기 불가')
    # [page] 로 특정 페이지 선택, 흰 배경 위에 합성(투명 방지)
    src_page = f'{src_path}[{page}]'
    try:
        r = subprocess.run(
            [convert, '-density', '150', src_page, '-resize', f'{max_width}x',
             '-background', 'white', '-flatten', out_png],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not os.path.isfile(out_png):
            raise ConvertError(f'미리보기 변환 실패: {r.stderr[:150]}')
    except subprocess.TimeoutExpired:
        raise ConvertError('미리보기 변환 시간초과')
    return out_png


def to_preview_pdf(src_path, out_dir):
    """TIFF를 미리보기용 PDF로 변환 (여러 페이지 지원)."""
    if not os.path.isfile(src_path):
        raise ConvertError(f'파일 없음: {src_path}')
    ext = os.path.splitext(src_path)[1].lower()
    if ext == '.pdf':
        return src_path  # 이미 PDF
    os.makedirs(out_dir, exist_ok=True)
    out_pdf = os.path.join(out_dir, f'pv_{uuid.uuid4().hex}.pdf')
    convert = _which('convert')
    if not convert:
        raise ConvertError('ImageMagick(convert) 미설치')
    try:
        r = subprocess.run([convert, src_path, out_pdf],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not os.path.isfile(out_pdf):
            raise ConvertError(f'PDF 변환 실패: {r.stderr[:150]}')
    except subprocess.TimeoutExpired:
        raise ConvertError('PDF 변환 시간초과')
    return out_pdf
