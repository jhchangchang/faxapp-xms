-- ECM(오류정정) 품질 지표 컬럼
-- 기존 배포 서버에서 1회 실행:
--   sudo -u postgres psql -d faxapp -f migration_ecm.sql

-- ECM 사용 여부 (회선 품질 낮을 때 손상 행 재전송으로 보정)
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS ecm_used BOOLEAN;

-- ECM으로 재전송된 손상 행 수 (0이면 깨끗, 높으면 회선 품질 나쁨)
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS bad_rows INTEGER;
