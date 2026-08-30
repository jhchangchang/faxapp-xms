"""
보안 유틸 - 비밀번호 해싱(bcrypt) + 세션 관리.
세션은 서버 메모리에 저장(단일 서버 기준). 다중 서버로 확장 시 Redis 등으로 이전.
"""
import bcrypt
import secrets
import time
import threading
from . import config

# ---------- 비밀번호 해싱 (bcrypt) ----------
def hash_password(plain: str) -> str:
    """평문 비밀번호를 bcrypt 해시로. 저장용."""
    if not plain:
        raise ValueError('비밀번호가 비어있습니다')
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain.encode('utf-8'), salt).decode('utf-8')


def verify_password(plain: str, hashed: str) -> bool:
    """평문과 해시 비교. 로그인 검증용."""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


# ---------- 세션 관리 ----------
# {token: {'user_id', 'username', 'role', 'expires'}}
_sessions = {}
_sess_lock = threading.Lock()


def create_session(user_id: int, username: str, role: str) -> str:
    """세션 생성 후 토큰 반환."""
    token = secrets.token_urlsafe(32)
    expires = time.time() + config.SESSION_HOURS * 3600
    with _sess_lock:
        _sessions[token] = {
            'user_id': user_id, 'username': username,
            'role': role, 'expires': expires,
        }
    return token


def get_session(token: str):
    """토큰으로 세션 조회. 만료 시 None."""
    if not token:
        return None
    with _sess_lock:
        sess = _sessions.get(token)
        if not sess:
            return None
        if sess['expires'] < time.time():
            del _sessions[token]
            return None
        return dict(sess)


def destroy_session(token: str):
    """로그아웃 - 세션 삭제."""
    with _sess_lock:
        _sessions.pop(token, None)


def cleanup_expired():
    """만료 세션 정리 (주기 호출용)."""
    now = time.time()
    with _sess_lock:
        expired = [t for t, s in _sessions.items() if s['expires'] < now]
        for t in expired:
            del _sessions[t]
    return len(expired)


def session_count():
    with _sess_lock:
        return len(_sessions)
