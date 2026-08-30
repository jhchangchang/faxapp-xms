"""
인증 라우터 - 로그인, 로그아웃, 내 정보.
"""
from fastapi import APIRouter, Response, Depends, Request, Cookie
from pydantic import BaseModel
from datetime import datetime

from ..core import db, security, config, errors
from ..core import ratelimit
from ..deps import get_current_user, COOKIE_NAME

router = APIRouter(prefix='/api/auth', tags=['auth'])


class LoginBody(BaseModel):
    username: str
    password: str


def _check_login_rate(ip, username):
    """로그인 브루트포스 방어. IP당 + 계정당 시도 제한.
    분당 10회, 시간당 30회 초과 시 차단."""
    for key in (f'login:ip:{ip}', f'login:user:{username}'):
        try:
            ratelimit.check(key + ':min', 10, 60)
            ratelimit.check(key + ':hour', 30, 3600)
        except ratelimit.RateLimitExceeded as e:
            raise errors.RateLimitError(
                retry_after=e.retry_after,
                detail='로그인 시도 초과',
                context={'reason': 'too_many_login_attempts'})


def _audit(user_id, action, detail, ip):
    """감사 로그 기록 (실패해도 무시)."""
    try:
        db.execute(
            'INSERT INTO audit_log(user_id, action, detail, ip) VALUES(%s,%s,%s,%s)',
            (user_id, action, detail, ip))
    except Exception:
        pass


@router.post('/login')
def login(body: LoginBody, response: Response, request: Request):
    """로그인 - 성공 시 세션 쿠키 설정."""
    ip = request.client.host if request.client else ''
    # 브루트포스 방어 (비밀번호 검증 전에 시도 횟수 제한)
    _check_login_rate(ip, body.username)
    # 사용자 조회
    row = db.query_one(
        'SELECT id, username, password_hash, display_name, role, is_active '
        'FROM users WHERE username=%s', (body.username,))
    if not row or not row['is_active']:
        _audit(None, 'login_fail', f'unknown/inactive: {body.username}', ip)
        raise errors.AuthError(detail='아이디 또는 비밀번호가 올바르지 않습니다')
    # 비밀번호 검증
    if not security.verify_password(body.password, row['password_hash']):
        _audit(row['id'], 'login_fail', 'bad password', ip)
        raise errors.AuthError(detail='아이디 또는 비밀번호가 올바르지 않습니다')
    # 세션 생성
    token = security.create_session(row['id'], row['username'], row['role'])
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, samesite='lax',
        max_age=config.SESSION_HOURS * 3600, path='/')
    # 최종 로그인 시각 갱신
    try:
        db.execute('UPDATE users SET last_login=%s WHERE id=%s',
                   (datetime.now(), row['id']))
    except Exception:
        pass
    _audit(row['id'], 'login', 'success', ip)
    return {'ok': True, 'user': {'id': row['id'], 'username': row['username'],
            'display_name': row['display_name'], 'role': row['role']}}


@router.post('/logout')
def logout(response: Response, faxapp_session: str = Cookie(default=None)):
    """로그아웃 - 세션 삭제."""
    if faxapp_session:
        security.destroy_session(faxapp_session)
    response.delete_cookie(COOKIE_NAME, path='/')
    return {'ok': True}


@router.get('/me')
def me(user=Depends(get_current_user)):
    """현재 로그인 사용자 정보. (password_hash는 조회하지 않음)"""
    row = db.query_one(
        'SELECT u.id, u.username, u.display_name, u.email, u.role, '
        'u.fax_number, u.dept_id, d.name AS dept_name '
        'FROM users u LEFT JOIN departments d ON u.dept_id=d.id '
        'WHERE u.id=%s', (user['user_id'],))
    if not row:
        raise errors.NotFound('사용자')
    # 안전장치: 혹시라도 민감 필드가 들어오면 제거
    row.pop('password_hash', None)
    return row
