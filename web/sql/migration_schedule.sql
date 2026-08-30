-- 발송 예약 기능용 컬럼
-- 기존 배포 서버에서 1회 실행:
--   sudo -u postgres psql -d faxapp -f migration_schedule.sql

-- 예약 발송 시각 (NULL이면 즉시 발송)
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ;

-- 예약 건을 빠르게 찾기 위한 인덱스 (status='scheduled'인 것만)
CREATE INDEX IF NOT EXISTS idx_jobs_scheduled ON fax_jobs(scheduled_at)
    WHERE status='scheduled';

-- status에 'scheduled' 상태가 추가됨 (기존: pending/queued/sending/sent/failed/cancelled/retry_wait)
-- 별도 제약은 없으므로 컬럼 코멘트만 갱신
COMMENT ON COLUMN fax_jobs.status IS
    'pending/scheduled/queued/sending/sent/failed/cancelled/retry_wait';
