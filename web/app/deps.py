"""
인증 의존성 - FastAPI Depends로 각 라우터에서 재사용.
세션 쿠키(faxapp_session)에서 현재 사용자를 추출하고 권한을 확인.
외부 연동용 API 키 인증(X-API-Key)도 제공.
"""
from fastapi import Depends, HTTPException, Cookie, status, Header
from .core import security, apikey

COOKIE_NAME = 'faxapp_session'


def get_current_user(faxapp_session: str = Cookie(default=None)):
    """세션 쿠키에서 현재 로그인 사용자 반환. 없으면 401."""
    sess = security.get_session(faxapp_session)
    if not sess:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail='로그인이 필요합니다')
    return sess


def require_api_key(x_api_key: str = Header(default=None)):
    """외부 연동용 API 키 인증. X-API-Key 헤더로 검증.
    유효하면 키 정보 반환(id, name, created_by), 아니면 401."""
    from .core import errors
    key_info = apikey.verify_key(x_api_key)
    if not key_info:
        raise errors.AuthError(detail='유효하지 않은 API 키입니다')
    return key_info


def require_role(*roles):
    """특정 권한만 허용하는 의존성 생성기.
    예: Depends(require_role('admin')) 또는 require_role('admin','manager')"""
    def checker(user=Depends(get_current_user)):
        if user['role'] not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f'권한 부족 (필요: {", ".join(roles)})')
        return user
    return checker


# 자주 쓰는 조합
def require_admin(user=Depends(get_current_user)):
    if user['role'] != 'admin':
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='관리자 권한이 필요합니다')
    return user


def require_manager(user=Depends(get_current_user)):
    """manager 이상(admin 포함)."""
    if user['role'] not in ('admin', 'manager'):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='관리자 또는 매니저 권한이 필요합니다')
    return user
