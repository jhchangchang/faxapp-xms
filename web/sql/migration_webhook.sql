-- 발송 결과 webhook용 컬럼 추가
-- 팩스 서버가 전송 완료 후 실제 전송 페이지 수와 상세 정보를 넘겨줌
-- 기존 배포 서버에서 1회 실행:
--   sudo -u postgres psql -d faxapp -f migration_webhook.sql

-- 실제 전송된 페이지 수 (부분 전송 감지용)
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS pages_sent INTEGER;

-- 진단 카테고리 (팩스 서버 faxdiag가 준 분류: T38_NEGOTIATION 등)
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS diag_category VARCHAR(40);

-- T.38 사용 여부, 전송 속도 (진단 참고용)
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS t38_used BOOLEAN;
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS transfer_rate VARCHAR(20);

-- hangup_cause (Q.850 끊김 원인, 진단 참고용)
ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS hangup_cause VARCHAR(60);
