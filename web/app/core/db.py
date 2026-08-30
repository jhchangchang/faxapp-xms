"""
DB 접근 계층 - psycopg3 기반. ORM 없이 SQL 직접 사용.
커넥션 풀로 동시성 처리.
"""
import os
from contextlib import contextmanager
from pathlib import Path
from . import config

# psycopg3 (미설치 환경 대비 지연 import)
try:
    import psycopg
    from psycopg_pool import ConnectionPool
    from psycopg.rows import dict_row
    HAS_PG = True
except Exception:
    HAS_PG = False

_pool = None

def init_pool():
    """앱 시작 시 커넥션 풀 생성.
    안정성: 연결 타임아웃, 죽은 커넥션 자동 감지(check), 재연결 설정.
    """
    global _pool
    if not HAS_PG:
        raise RuntimeError('psycopg 미설치: pip install "psycopg[binary,pool]"')
    if _pool is None:
        _pool = ConnectionPool(
            config.dsn(),
            min_size=2, max_size=10,
            # 커넥션 획득 최대 대기(초) - 무한 대기 방지
            timeout=10,
            # 죽은 커넥션 자동 감지 후 재연결 (DB 재시작 대비)
            check=ConnectionPool.check_connection,
            # 커넥션 최대 수명 - 오래된 커넥션 주기적 교체
            max_lifetime=1800,
            # 유휴 커넥션 최대 시간
            max_idle=300,
            kwargs={
                'row_factory': dict_row,
                # 쿼리/연결 타임아웃 (초) - 무한 대기 방지
                'connect_timeout': 10,
            })
    return _pool

def close_pool():
    global _pool
    if _pool:
        _pool.close(); _pool = None

@contextmanager
def get_conn():
    """커넥션 획득 (컨텍스트 매니저).
    안정성: statement_timeout으로 쿼리 무한 대기 방지 (워커 스레드 고갈 차단).
    """
    if _pool is None:
        init_pool()
    with _pool.connection() as conn:
        # 쿼리 최대 실행 시간 30초 (초과 시 에러 - 무한 대기로 스레드 묶임 방지)
        try:
            conn.execute("SET statement_timeout = 30000")
        except Exception:
            pass  # 설정 실패해도 쿼리는 진행
        yield conn

# ---------- 쿼리 헬퍼 ----------
def query(sql, params=None):
    """SELECT - 여러 행 반환 (dict 리스트)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

def query_one(sql, params=None):
    """SELECT - 한 행 반환 (dict 또는 None)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

def execute(sql, params=None, returning=False):
    """INSERT/UPDATE/DELETE. returning=True면 RETURNING 결과 반환."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            result = cur.fetchone() if returning else None
            conn.commit()
            return result

def execute_many(sql, params_list):
    """대량 INSERT (동보전송 등)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, params_list)
            conn.commit()
            return cur.rowcount

def init_schema(schema_path=None):
    """스키마 파일 실행 (테이블 생성)."""
    if schema_path is None:
        # 💡 상대 경로 대신 에러 메시지에 나왔던 절대 경로를 직접 입력합니다.
        schema_path = "/opt/faxapp/web/app/core/sql/schema.sql"
        
    with open(schema_path, encoding='utf-8') as f:
        sql = f.read()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()
    return True
