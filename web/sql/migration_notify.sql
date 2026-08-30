-- 알림 기능용 테이블
-- 기존 배포 서버에서 1회 실행:
--   sudo -u postgres psql -d faxapp -f migration_notify.sql

CREATE TABLE IF NOT EXISTS notifications (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- NULL이면 전체 관리자 대상
    level       VARCHAR(10) NOT NULL DEFAULT 'info',   -- info / warn / error
    title       VARCHAR(120) NOT NULL,
    body        TEXT,
    category    VARCHAR(30),        -- send_fail / receive / system / fault 등
    ref_type    VARCHAR(20),        -- job / received / null
    ref_id      INTEGER,
    is_read     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read, created_at DESC);

-- 알림 설정 (웹훅 등) - 단일 행 설정 테이블
CREATE TABLE IF NOT EXISTS notify_config (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    webhook_url     TEXT,                        -- Slack 등 외부 웹훅 (비면 미사용)
    notify_on_fail  BOOLEAN NOT NULL DEFAULT TRUE,   -- 발송 실패 시 알림
    notify_on_recv  BOOLEAN NOT NULL DEFAULT FALSE,  -- 수신 시 알림
    fault_filter    VARCHAR(20) DEFAULT 'local',      -- 웹훅 보낼 최소 심각도(어느 fault부터)
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO notify_config(id) VALUES(1) ON CONFLICT (id) DO NOTHING;
