-- ============================================================
--  FaxApp 애플리케이션 DB 스키마 (PostgreSQL)
--  실행: psql -U faxapp -d faxapp -f schema.sql
-- ============================================================

-- ---------- 조직 (부서) ----------
CREATE TABLE IF NOT EXISTS departments (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    parent_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    fax_number  VARCHAR(32),                 -- 부서 대표 팩스번호
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- 사용자 ----------
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(64) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,       -- 해시만 저장 (평문 금지)
    display_name  VARCHAR(100) NOT NULL,
    email         VARCHAR(160),
    dept_id       INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    fax_number    VARCHAR(32),                 -- 사용자 개인 팩스번호(DID)
    role          VARCHAR(20) NOT NULL DEFAULT 'user',  -- admin / manager / user
    is_active     BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_users_dept ON users(dept_id);
CREATE INDEX IF NOT EXISTS idx_users_fax  ON users(fax_number);

-- ---------- 주소록 ----------
CREATE TABLE IF NOT EXISTS contacts (
    id          SERIAL PRIMARY KEY,
    owner_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- NULL이면 공용
    name        VARCHAR(100) NOT NULL,
    fax_number  VARCHAR(32) NOT NULL,
    company     VARCHAR(120),
    memo        VARCHAR(255),
    is_shared   BOOLEAN NOT NULL DEFAULT false,   -- 부서/전사 공유 여부
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(owner_id);

-- 주소록 그룹 (동보전송용)
CREATE TABLE IF NOT EXISTS contact_groups (
    id          SERIAL PRIMARY KEY,
    owner_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(100) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS contact_group_members (
    group_id    INTEGER REFERENCES contact_groups(id) ON DELETE CASCADE,
    contact_id  INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, contact_id)
);

-- ---------- 팩스 작업 (발송) ----------
-- 애플리케이션이 관리하는 발송 작업. 실제 전송은 팩스 서버에 위임.
CREATE TABLE IF NOT EXISTS fax_jobs (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    dept_id       INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    to_number     VARCHAR(32) NOT NULL,
    to_name       VARCHAR(100),
    file_name     VARCHAR(255),                -- 업로드 원본 파일명
    file_path     VARCHAR(500),                -- 변환된 TIFF/PDF 경로
    pages         INTEGER DEFAULT 0,
    cover_used    BOOLEAN DEFAULT false,
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',
                  -- pending / queued / sending / sent / failed / cancelled
    server_job_id VARCHAR(64),                 -- 팩스 서버가 준 job_id
    retry_count   INTEGER DEFAULT 0,
    result_text   VARCHAR(255),                -- 실패 사유 등
    batch_id      VARCHAR(64),                 -- 동보전송 묶음 ID
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_jobs_user   ON fax_jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON fax_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_batch  ON fax_jobs(batch_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON fax_jobs(created_at);

-- ---------- 수신 팩스 ----------
CREATE TABLE IF NOT EXISTS fax_received (
    id            SERIAL PRIMARY KEY,
    from_number   VARCHAR(32),
    to_number     VARCHAR(32),                 -- 수신된 DID (번호별 분류)
    dept_id       INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- 배분된 사용자
    file_path     VARCHAR(500),                -- 수신 PDF 경로
    pages         INTEGER DEFAULT 0,
    rate          VARCHAR(16),
    is_read       BOOLEAN NOT NULL DEFAULT false,
    memo          VARCHAR(500),                -- 수신팩스 메모첨부
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_recv_to    ON fax_received(to_number);
CREATE INDEX IF NOT EXISTS idx_recv_dept  ON fax_received(dept_id);
CREATE INDEX IF NOT EXISTS idx_recv_read  ON fax_received(is_read);

-- ---------- 표지 양식 ----------
CREATE TABLE IF NOT EXISTS cover_templates (
    id          SERIAL PRIMARY KEY,
    owner_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(100) NOT NULL,
    content     TEXT,                          -- 표지 템플릿(HTML/텍스트)
    is_default  BOOLEAN DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- 감사 로그 ----------
CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER,
    action      VARCHAR(40),                   -- login / send / delete 등
    detail      VARCHAR(500),
    ip          VARCHAR(45),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at);
