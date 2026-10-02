-- 이메일 알림 설정 추가 (기존 notify_config에 컬럼 확장)
-- 기존 배포 서버에서 1회 실행:
--   sudo -u postgres psql -d faxapp -f migration_email_notify.sql

ALTER TABLE notify_config ADD COLUMN IF NOT EXISTS email_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE notify_config ADD COLUMN IF NOT EXISTS smtp_host     VARCHAR(120);
ALTER TABLE notify_config ADD COLUMN IF NOT EXISTS smtp_port     INTEGER DEFAULT 587;
ALTER TABLE notify_config ADD COLUMN IF NOT EXISTS smtp_user     VARCHAR(120);
ALTER TABLE notify_config ADD COLUMN IF NOT EXISTS smtp_pass     VARCHAR(200);
ALTER TABLE notify_config ADD COLUMN IF NOT EXISTS smtp_tls      BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE notify_config ADD COLUMN IF NOT EXISTS email_from    VARCHAR(120);
ALTER TABLE notify_config ADD COLUMN IF NOT EXISTS email_to      TEXT;  -- 수신자(쉼표구분)
