"""오래된 파일 자동 정리 테스트 (디스크 보호)."""
import pytest, os, time
from unittest.mock import patch


class TestFileCleanup:
    def test_removes_old_files(self, tmp_path, monkeypatch):
        from app.core import retry_worker, config
        # 오래된 파일 + 최신 파일
        old = tmp_path / 'old.tif'; old.write_text('x')
        new = tmp_path / 'new.tif'; new.write_text('y')
        # old를 100일 전으로
        old_time = time.time() - 100*86400
        os.utime(str(old), (old_time, old_time))
        # config 경로를 tmp로
        monkeypatch.setattr(config, 'UPLOAD_DIR', str(tmp_path), raising=False)
        monkeypatch.setattr(config, 'RECV_DIR', '', raising=False)
        monkeypatch.setattr(config, 'FAX_DIR', '', raising=False)
        # db는 빈 결과
        class FakeDB:
            def query(self, sql, params=None): return []
        with patch.object(retry_worker, 'db', FakeDB()):
            with patch.object(retry_worker, 'FILE_RETAIN_DAYS', 90):
                n = retry_worker._cleanup_old_files()
        # 오래된 것만 삭제, 최신은 유지
        assert not old.exists()
        assert new.exists()

    def test_preserves_in_use(self, tmp_path, monkeypatch):
        from app.core import retry_worker, config
        old = tmp_path / 'inuse.tif'; old.write_text('x')
        old_time = time.time() - 100*86400
        os.utime(str(old), (old_time, old_time))
        monkeypatch.setattr(config, 'UPLOAD_DIR', str(tmp_path), raising=False)
        monkeypatch.setattr(config, 'RECV_DIR', '', raising=False)
        monkeypatch.setattr(config, 'FAX_DIR', '', raising=False)
        # DB가 이 파일을 아직 참조 (재전송 대기)
        class FakeDB:
            def query(self, sql, params=None):
                return [{'file_path': str(old)}]
        with patch.object(retry_worker, 'db', FakeDB()):
            with patch.object(retry_worker, 'FILE_RETAIN_DAYS', 90):
                retry_worker._cleanup_old_files()
        # DB가 쓰는 파일은 오래돼도 보존
        assert old.exists()

    def test_no_crash_missing_dir(self, monkeypatch):
        from app.core import retry_worker, config
        monkeypatch.setattr(config, 'UPLOAD_DIR', '/nonexistent/xyz', raising=False)
        monkeypatch.setattr(config, 'RECV_DIR', '', raising=False)
        monkeypatch.setattr(config, 'FAX_DIR', '', raising=False)
        class FakeDB:
            def query(self, sql, params=None): return []
        with patch.object(retry_worker, 'db', FakeDB()):
            n = retry_worker._cleanup_old_files()
        assert n == 0
