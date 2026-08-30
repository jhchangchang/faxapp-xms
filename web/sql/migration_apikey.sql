-- 외부 연동 API 키
-- 기존 배포 서버에서 1회 실행:
--   sudo -u postgres psql -d faxapp -f migration_apikey.sql

CREATE TABLE IF NOT EXISTS api_keys (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,        -- 키 용도 (예: "ERP 연동", "그룹웨어")
    key_hash    VARCHAR(128) NOT NULL UNIQUE,  -- 키의 해시 (평문 저장 안 함)
    key_prefix  VARCHAR(12) NOT NULL,          -- 키 앞부분 (식별용, 예: fx_a1b2c3)
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    call_count  INTEGER NOT NULL DEFAULT 0,    -- 사용 횟수
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_apikey_hash ON api_keys(key_hash) WHERE is_active;

-- fax_jobs에 API 발송 표시 (어느 키로 보냈는지 추적)
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS api_key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL;
