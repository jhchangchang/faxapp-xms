-- 번호 차단 목록
CREATE TABLE IF NOT EXISTS block_list (
    id          SERIAL PRIMARY KEY,
    number      VARCHAR(64) NOT NULL,
    direction   VARCHAR(10) NOT NULL DEFAULT 'both',  -- send / recv / both
    reason      VARCHAR(200),
    created_by  INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(number, direction)
);
CREATE INDEX IF NOT EXISTS idx_block_number ON block_list(number);
