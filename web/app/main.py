"""
FaxApp - 팩스 애플리케이션 계층 (FastAPI)
팩스 서버(faxserver_v6.py)와 REST API로 연동.

실행:
  pip install fastapi uvicorn "psycopg[binary,pool]"
  uvicorn app.main:app --host 0.0.0.0 --port 8100
"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse

from .core import config, db, faxclient


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 시작 시: DB 풀 초기화 (실패해도 앱은 뜨되 경고)
    try:
        db.init_pool()
        app.state.db_ok = True
    except Exception as e:
        app.state.db_ok = False
        app.state.db_error = str(e)
    # 재시도 워커 시작 (DB 정상일 때만)
    if getattr(app.state, 'db_ok', False):
        try:
            from .core import retry_worker
            retry_worker.start()
        except Exception as e:
            import logging
            logging.getLogger('faxapp').warning(f'재시도 워커 시작 실패: {e}')
    yield
    # 종료 시
    try:
        from .core import retry_worker
        retry_worker.stop()
    except Exception:
        pass
    try:
        db.close_pool()
    except Exception:
        pass


app = FastAPI(title="FaxApp", description="팩스 애플리케이션 계층", version="0.1.0",
              lifespan=lifespan)


_STATIC_DIR = os.path.join(os.path.dirname(__file__), '..', 'static')

@app.get("/")
def root():
    """프론트엔드 UI 서빙."""
    index = os.path.join(_STATIC_DIR, 'index.html')
    if os.path.isfile(index):
        return FileResponse(index)
    return JSONResponse({"app": "FaxApp", "version": "0.1.0", "status": "running",
                         "note": "static/index.html 없음 - 프론트엔드 미배치"})

@app.get("/api/info")
def api_info():
    return {"app": "FaxApp", "version": "0.1.0", "status": "running"}


@app.get("/test")
def field_test():
    """현장 테스트 페이지 서빙 (실제 발송·수신 진단용)."""
    page = os.path.join(_STATIC_DIR, 'fieldtest.html')
    if os.path.isfile(page):
        return FileResponse(page)
    return JSONResponse({"error": "fieldtest.html 없음"}, status_code=404)


@app.get("/health")
def health():
    """앱·DB·팩스서버 3계층 상태 확인."""
    db_ok = getattr(app.state, 'db_ok', False)
    fax_ok = faxclient.server_available()
    return {
        "app": True,
        "database": db_ok,
        "fax_server": fax_ok,
        "overall": db_ok and fax_ok,
    }


@app.get("/api/fax-server/status")
def fax_server_status():
    """팩스 서버 상태를 앱을 통해 조회 (프록시)."""
    try:
        return faxclient.get_status()
    except faxclient.FaxServerError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.post("/api/admin/init-db")
def init_db():
    """DB 스키마 초기화 (최초 1회 또는 관리용)."""
    try:
        db.init_schema()
        return {"ok": True, "message": "스키마 초기화 완료"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# 라우터 등록
# 중앙 예외 핸들러 등록 (라우터보다 먼저)
from .core import error_handler
error_handler.register_handlers(app)

# 라우터 자동 등록 - routers/ 폴더의 모든 라우터를 스캔해서 등록
# 새 라우터 추가 시 이 파일을 수정할 필요 없음 (routers/에 파일만 넣으면 됨)
from .routers import collect_routers
for _router in collect_routers():
    app.include_router(_router)
