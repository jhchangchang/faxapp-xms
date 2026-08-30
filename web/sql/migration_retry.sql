-- 재시도 자동화용 컬럼 추가 (기존 DB 마이그레이션)
-- 이미 배포된 서버에서 1회 실행:
--   psql -U faxapp -d faxapp -f migration_retry.sql

ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS max_retries INTEGER DEFAULT 3;
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS last_error_code VARCHAR(40);
CREATE INDEX IF NOT EXISTS idx_jobs_retry ON fax_jobs(next_retry_at) WHERE status='retry_wait';

-- error_log 테이블 (Step11에서 추가, 아직 없으면)
CREATE TABLE IF NOT EXISTS error_log (
    id SERIAL PRIMARY KEY,
    code VARCHAR(40) NOT NULL,
    category VARCHAR(20) NOT NULL,
    detail TEXT,
    context JSONB,
    path VARCHAR(200),
    user_id INTEGER,
    job_id INTEGER,
    resolved BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_error_code ON error_log(code);
CREATE INDEX IF NOT EXISTS idx_error_category ON error_log(category);
CREATE INDEX IF NOT EXISTS idx_error_time ON error_log(created_at);
CREATE INDEX IF NOT EXISTS idx_error_job ON error_log(job_id);
