"""
스키마 정합성 테스트.
코드가 참조하는 테이블/컬럼이 실제 schema.sql과 일치하는지 검증.
(테이블명 오타 같은 실수를 자동으로 잡음)
"""
import os
import re
import pytest

SQL_DIR = os.path.join(os.path.dirname(__file__), '..', 'sql')
SEND_PY = os.path.join(os.path.dirname(__file__), '..', 'app', 'routers', 'send.py')


def _load_schema_tables():
    """schema.sql + migration에서 테이블명 추출."""
    tables = set()
    if not os.path.isdir(SQL_DIR):
        pytest.skip('sql 디렉토리 없음')
    for fn in os.listdir(SQL_DIR):
        if fn.endswith('.sql'):
            with open(os.path.join(SQL_DIR, fn), encoding='utf-8') as f:
                content = f.read()
            for m in re.finditer(r'CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)', content, re.I):
                tables.add(m.group(1).lower())
    return tables


class TestSchemaConsistency:
    def test_receive_table_exists(self):
        # 수신 통지가 쓰는 테이블이 스키마에 있는지
        tables = _load_schema_tables()
        assert 'fax_received' in tables, \
            f"fax_received 테이블이 스키마에 없음. 있는 테이블: {tables}"

    def test_send_py_uses_real_tables(self):
        # send.py의 INSERT/UPDATE 대상 테이블이 다 실재하는지
        tables = _load_schema_tables()
        if not tables:
            pytest.skip('스키마 테이블 없음')
        with open(SEND_PY, encoding='utf-8') as f:
            code = f.read()
        # SQL 문자열 안의 테이블만 추출 (import 문 등 제외)
        # 따옴표로 감싼 SQL에서 INSERT INTO / UPDATE 대상만
        referenced = set()
        for m in re.finditer(r'(?:INSERT INTO|UPDATE)\s+(fax_\w+|error_log|audit_log|users|notifications)',
                             code, re.I):
            referenced.add(m.group(1).lower())
        # 코드가 참조하는 테이블이 스키마에 다 있는지
        missing = [t for t in referenced if t not in tables
                   and t not in ('now',)]   # 함수 제외
        assert not missing, f"send.py가 참조하는데 스키마에 없는 테이블: {missing}"

    def test_fax_received_has_expected_columns(self):
        # 수신 통지가 쓰는 컬럼이 실제 있는지
        schema_path = os.path.join(SQL_DIR, 'schema.sql')
        if not os.path.isfile(schema_path):
            pytest.skip('schema.sql 없음')
        with open(schema_path, encoding='utf-8') as f:
            content = f.read()
        m = re.search(r'CREATE TABLE(?:\s+IF NOT EXISTS)?\s+fax_received\s*\((.*?)\);',
                      content, re.I | re.S)
        assert m, 'fax_received 정의를 못 찾음'
        cols = m.group(1).lower()
        for col in ['from_number', 'file_path', 'pages', 'rate', 'received_at']:
            assert col in cols, f"fax_received에 {col} 컬럼 없음"
