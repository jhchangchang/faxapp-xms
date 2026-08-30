"""
API 키 관리 - 외부 시스템 연동용 인증 키.
키는 발급 시 1회만 평문 노출, DB에는 해시로 저장 (보안 표준).
"""
import hashlib
import secrets
from . import db


def _hash_key(raw_key):
    """API 키를 SHA-256으로 해시 (평문 저장 방지)."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_key(name, created_by):
    """새 API 키 발급. 평문 키는 이때만 반환되고 이후 조회 불가.
    반환: {'id', 'name', 'key'(평문, 1회만), 'prefix'}"""
    # fx_ 접두어 + 32자 랜덤 (URL-safe)
    raw = 'fx_' + secrets.token_urlsafe(24)
    prefix = raw[:10]      # 식별용 앞부분
    key_hash = _hash_key(raw)
    row = db.execute(
        'INSERT INTO api_keys(name, key_hash, key_prefix, created_by) '
        'VALUES(%s,%s,%s,%s) RETURNING id',
        (name, key_hash, prefix, created_by), returning=True)
    return {'id': row['id'], 'name': name, 'key': raw, 'prefix': prefix}


def verify_key(raw_key):
    """API 키 검증. 유효하면 키 정보 반환, 아니면 None.
    검증 성공 시 사용 기록(last_used_at, call_count) 갱신."""
    if not raw_key or not raw_key.startswith('fx_'):
        return None
    key_hash = _hash_key(raw_key)
    row = db.query_one(
        'SELECT id, name, is_active, created_by FROM api_keys '
        'WHERE key_hash=%s AND is_active=TRUE', (key_hash,))
    if not row:
        return None
    # 사용 기록 갱신
    db.execute(
        'UPDATE api_keys SET last_used_at=now(), call_count=call_count+1 WHERE id=%s',
        (row['id'],))
    return row


def list_keys():
    """발급된 키 목록 (평문 키는 없음, 접두어만)."""
    return db.query(
        'SELECT k.id, k.name, k.key_prefix, k.is_active, k.last_used_at, '
        'k.call_count, k.created_at, u.display_name AS created_by_name '
        'FROM api_keys k LEFT JOIN users u ON u.id=k.created_by '
        'ORDER BY k.created_at DESC')


def revoke_key(key_id):
    """키 폐기 (비활성화). 폐기된 키는 즉시 인증 실패."""
    db.execute('UPDATE api_keys SET is_active=FALSE WHERE id=%s', (key_id,))


def delete_key(key_id):
    """키 완전 삭제."""
    db.execute('DELETE FROM api_keys WHERE id=%s', (key_id,))
