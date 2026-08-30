"""
라우터 자동 수집
================
routers/ 폴더의 모든 .py를 스캔해서 'router' 객체를 모아 반환.
main.py는 이 함수만 호출하면 됨 → 새 라우터 추가 시 main.py 수정 불필요.

사용 (main.py):
    from .routers import collect_routers
    for r in collect_routers():
        app.include_router(r)

설계:
- 각 모듈에서 'router' 속성(APIRouter)을 찾아 수집
- 하나가 import 실패해도 전체가 죽지 않음 (경고 로그 후 계속)
- 로드 순서를 제어할 수 있게 우선순위 지원 (auth를 먼저 등 필요시)
"""
import importlib
import logging
import pkgutil
from pathlib import Path

logger = logging.getLogger('faxapp')

# 먼저 로드할 라우터 (순서 중요한 경우). 나머지는 알파벳순 자동.
_PRIORITY = ['auth']
# 자동 수집에서 제외할 모듈 (테스트 파일 등)
_EXCLUDE = {'__init__', 'test_send_app'}


def collect_routers():
    """
    routers/ 안의 모든 모듈에서 router 객체를 수집해 리스트로 반환.
    우선순위 모듈 먼저, 나머지는 이름순.
    """
    pkg_dir = Path(__file__).parent
    module_names = []
    for info in pkgutil.iter_modules([str(pkg_dir)]):
        if info.name in _EXCLUDE:
            continue
        module_names.append(info.name)

    def sort_key(name):
        if name in _PRIORITY:
            return (0, _PRIORITY.index(name))
        return (1, name)
    module_names.sort(key=sort_key)

    routers = []
    loaded, failed = [], []
    for name in module_names:
        try:
            mod = importlib.import_module(f'{__name__}.{name}')
            router = getattr(mod, 'router', None)
            if router is None:
                logger.warning(f"라우터 '{name}'에 router 객체가 없어 건너뜀")
                continue
            routers.append(router)
            loaded.append(name)
        except Exception as e:
            failed.append(name)
            logger.error(f"라우터 '{name}' 로드 실패 (건너뜀): {e}")

    logger.info(f"라우터 자동 등록: {len(loaded)}개 로드 {loaded}"
                + (f", {len(failed)}개 실패 {failed}" if failed else ""))
    return routers
