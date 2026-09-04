"""이어보내기(부분 재전송) 테스트.
50장 중 48장 성공 후 실패 시, 49~50장만 재전송하는지 검증."""
import pytest
from unittest.mock import patch, MagicMock
from contextlib import ExitStack


class TestResumePlan:
    """이어보내기 판단 정책."""

    def test_short_doc_full_resend(self):
        # 5장 이하는 이어보내기 안 함 (전체 재전송)
        from app.core import retry_worker
        job = {'id': 1, 'pages': 3, 'pages_sent': 2}
        do_resume, start, total, sent = retry_worker._resume_plan(job)
        assert do_resume is False

    def test_large_doc_high_ratio_resume(self):
        # 50장 중 48장(96%) → 이어보내기, 49페이지부터
        from app.core import retry_worker
        job = {'id': 1, 'pages': 50, 'pages_sent': 48}
        do_resume, start, total, sent = retry_worker._resume_plan(job)
        assert do_resume is True
        assert start == 49       # sent+1
        assert total == 50
        assert sent == 48

    def test_large_doc_low_ratio_full(self):
        # 50장 중 20장(40%)만 → 앞부분 많이 유실, 전체 재전송이 안전
        from app.core import retry_worker
        job = {'id': 1, 'pages': 50, 'pages_sent': 20}
        do_resume, start, total, sent = retry_worker._resume_plan(job)
        assert do_resume is False

    def test_no_pages_info_full(self):
        # 페이지 정보 없으면 전체
        from app.core import retry_worker
        job = {'id': 1, 'pages': 0, 'pages_sent': 0}
        do_resume, start, total, sent = retry_worker._resume_plan(job)
        assert do_resume is False

    def test_all_sent_no_resume(self):
        # 이미 다 보냄 (부분전송 아님)
        from app.core import retry_worker
        job = {'id': 1, 'pages': 30, 'pages_sent': 30}
        do_resume, start, total, sent = retry_worker._resume_plan(job)
        assert do_resume is False

    def test_boundary_10_pages_80pct(self):
        # 경계: 정확히 10장 중 8장(80%) → 이어보내기
        from app.core import retry_worker
        job = {'id': 1, 'pages': 10, 'pages_sent': 8}
        do_resume, start, total, sent = retry_worker._resume_plan(job)
        assert do_resume is True
        assert start == 9


class TestSliceTiff:
    """TIFF 페이지 슬라이싱 (실제 파일)."""

    def test_slice_from_middle(self, tmp_path):
        from app.core import convert
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip('Pillow 없음')
        # 10페이지 생성
        pages = []
        for i in range(10):
            img = Image.new('1', (1728, 2200), 1)
            pages.append(img)
        src = str(tmp_path / 'src.tif')
        pages[0].save(src, format='TIFF', compression='group4',
                      save_all=True, append_images=pages[1:])
        # 7페이지부터 슬라이싱 → 4페이지(7,8,9,10)
        out = str(tmp_path / 'out.tif')
        try:
            convert.slice_tiff_pages(src, 7, out)
            n = convert.count_tiff_pages(out)
            assert n == 4
        except convert.ConvertError:
            pytest.skip('tiffcp/convert 없음')
