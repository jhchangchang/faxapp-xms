"""선택 페이지 재전송 테스트 (누락 페이지 대응)."""
import pytest
from app.core import convert


class TestPageSpec:
    """페이지 지정 문자열 파싱."""
    def test_comma(self):
        assert convert.parse_page_spec('2,8', 10) == [2,8]
    def test_range(self):
        assert convert.parse_page_spec('3-5', 10) == [3,4,5]
    def test_mixed(self):
        assert convert.parse_page_spec('1,3-5,8', 10) == [1,3,4,5,8]
    def test_spaces(self):
        assert convert.parse_page_spec('2, 8 , 5', 10) == [2,5,8]
    def test_out_of_range_excluded(self):
        assert convert.parse_page_spec('5,99,3', 10) == [3,5]
    def test_reverse_range(self):
        assert convert.parse_page_spec('5-3', 10) == [3,4,5]
    def test_dedup(self):
        assert convert.parse_page_spec('2,2,3', 10) == [2,3]
    def test_empty(self):
        assert convert.parse_page_spec('', 10) == []
    def test_invalid(self):
        assert convert.parse_page_spec('abc,x', 10) == []


class TestSelectSlice:
    """선택 페이지 슬라이싱 (실제 파일)."""
    def test_non_contiguous(self, tmp_path):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip('Pillow 없음')
        pages=[Image.new('1',(1728,2200),1) for _ in range(10)]
        src=str(tmp_path/'s.tif')
        pages[0].save(src,format='TIFF',compression='group4',save_all=True,append_images=pages[1:])
        out=str(tmp_path/'o.tif')
        try:
            convert.slice_selected_pages(src,[2,8],out)
            assert convert.count_tiff_pages(out)==2   # 2,8 → 2장
        except convert.ConvertError:
            pytest.skip('slice 도구 없음')

    def test_scattered(self, tmp_path):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip('Pillow 없음')
        pages=[Image.new('1',(1728,2200),1) for _ in range(10)]
        src=str(tmp_path/'s.tif')
        pages[0].save(src,format='TIFF',compression='group4',save_all=True,append_images=pages[1:])
        out=str(tmp_path/'o.tif')
        try:
            convert.slice_selected_pages(src,[1,3,4,5,8],out)
            assert convert.count_tiff_pages(out)==5
        except convert.ConvertError:
            pytest.skip('slice 도구 없음')
