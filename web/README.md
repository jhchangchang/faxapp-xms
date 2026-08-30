# faxapp 테스트

"우리 쪽 문제로 서비스가 죽으면 안 된다"를 검증하는 테스트.
실제 회선/엔진 없이 Mock으로 비정상 상황을 시뮬레이션.

## 실행

```bash
cd web
pip install pytest
python3 -m pytest tests/ -v
```

## 테스트 구성 (36개)

### test_error_classification.py (13개)
엔진 결과 → 예외 분류, 재시도 정책
- busy/no_answer/training → 재시도함
- invalid_number → 재시도 안 함 (영구실패)
- 알 수 없는/빈 결과에도 안 죽음
- 재시도 무한루프 없음, 백오프 증가

### test_validation_security.py (16개)
입력 검증 + 보안
- 팩스번호 검증 (인젝션 문자 거부)
- Rate Limit (한도 초과 차단, 사용자별 독립)
- 예외 → 일관된 HTTP 응답 (429, 503 등)

### test_resilience.py (7개)
비정상 상황 복원력 (핵심)
- 엔진 다운 → 재시도 가능 분류
- 쓰레기 응답에도 안 죽음
- 중복 webhook 멱등성 (결정적 분류)
- 부분 전송 감지
- 재시도 소진 → failed 확정 (무한루프 방지)
- 동시성 안전 (rate limiter 스레드 안전)

## CI 연동 (선택)

```bash
# 커밋 전 자동 실행
python3 -m pytest tests/ -q || echo "테스트 실패 - 커밋 중단"
```
