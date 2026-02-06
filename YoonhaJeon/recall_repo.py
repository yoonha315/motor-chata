# recall_repo.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Tuple

import os
import mysql.connector
from dotenv import load_dotenv

# ============================================================
# 모듈 목적
# - Streamlit UI에서 사용하는 '리콜 조회/필터/통계' 기능을 DB 레이어로 분리하여 관리한다.
# - UI는 이 모듈을 호출해 데이터만 받아 렌더링하며, SQL/DB 연결 세부 구현은 숨긴다.
# - 필터 조건을 공통 WHERE 빌더로 표준화하여 유지보수성과 일관성을 확보한다.
# ============================================================

# ============================================================
# 1) DB CONFIG
# - .env에 저장된 로그인 정보로 DB 연결
# ============================================================

load_dotenv()

HOST = os.getenv("HOST")
PORT = os.getenv("PORT")
USER = os.getenv("USER")
PASSWORD = os.getenv("PASSWORD")
DATABASE = os.getenv("DATABASE")

# 판단 근거:
# - 보안 리스크 감소를 위해 민감정보(접속정보)를 코드에 직접 코딩하지 않고 환경변수로 분리

DB_CONFIG = {
    "host": HOST,
    "port": PORT,
    "user": USER,
    "password": PASSWORD,
    "database": DATABASE,
}


# ============================================================
# 2) DTO (Data Transfer Object)
# ============================================================

@dataclass
class RecallView:
    """
    리콜 목록 화면(카드/리스트)에 필요한 데이터를 담는 DTO(Data Transfer Object).

    필드 정의
    - scope: 국내/해외 구분(제조사 region_at)
    - maker: 제조사명
    - car_name: 모델명
    - start_date/end_date: 제조기간(또는 해당 모델 생산기간)
    - target_units: 리콜 대상 대수
    - defect_text: 결함 설명
    - fix_text: 조치 방법
    - contact_text: 리콜 센터/문의처
    """
    scope: str
    maker: str
    car_name: str
    start_date: datetime
    end_date: datetime
    target_units: int
    defect_text: str
    fix_text: str
    contact_text: str


# ============================================================
# 3) 공통 WHERE 빌더
# - Streamlit에서 전달받은 필터를 SQL WHERE로 변환
# ============================================================

def _build_where(
        scope: str,
        maker: str,
        manufacture_year: Optional[int],
        search_text: str,
) -> Tuple[str, List]:
    """
    Streamlit 필터 입력값을 기반으로 공통 WHERE 절과 파라미터 리스트를 생성한다.

    Args
    - scope: "전체" 또는 "국내/해외" 구분값
    - maker: "전체" 또는 제조사명
    - manufacture_year: 특정 연도 필터(없으면 None)
    - search_text: 제조사/차명 검색어(없으면 "")

    Returns
    - (where_sql, params)
      - where_sql: "WHERE ..." 형태의 문자열 (조건 없으면 "")
      - params: mysql-connector의 parameterized query에 바인딩할 값 리스트

    판단 근거
    - SQL 문자열에 값을 직접 이어붙이지 않고 파라미터 바인딩을 사용:
      (1) SQL 인젝션 방지 (2) 쿼리 캐시/실행 안정성
    - 필터 조건 조립을 한 곳에서 처리:
      (1) fetch_* 함수들 간 조건 일관성 보장 (2) 변경 시 단일 지점 수정 (3) 디버깅 단순화
    """
    where = []
    params: List = []

    # scope: "전체"면 미적용, 아니면 제조사 테이블의 region_at으로 필터
    if scope != "전체":
        where.append("mf.region_at = %s")
        params.append(scope)

    # maker: "전체"면 미적용, 아니면 제조사명 필터
    if maker != "전체":
        where.append("mf.maker_name = %s")
        params.append(maker)

    # 제조연도: 기간 겹침 포함 (start_date <= 12/31 AND end_date >= 01/01)
    # - 예: 2020~2025 생산 모델은 2022/2023 필터에서도 조회되어야 함(요구사항 부합)
    if manufacture_year is not None:
        y_start = date(manufacture_year, 1, 1)
        y_end = date(manufacture_year, 12, 31)
        where.append("md.start_date <= %s AND md.end_date >= %s")
        params.extend([y_end, y_start])

    # 검색: 제조사/차명
    # - UI에서 단일 검색창으로 제조사/차명을 동시에 탐색하는 UX 요구를 반영
    # - LIKE는 부분일치 검색을 제공(정확 일치보다 사용성 우선)
    # - CONCAT('%', %s, '%') 형태로 바인딩하여 escaping/인젝션 위험을 낮춤
    s = (search_text or "").strip()
    if s:
        where.append(
            "(mf.maker_name LIKE CONCAT('%', %s, '%') OR md.model_name LIKE CONCAT('%', %s, '%'))"
        )
        params.extend([s, s])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return where_sql, params


# ============================================================
# 4) 리콜 목록 조회 (카드 리스트용)
# ============================================================

def fetch_recalls(
        scope: str = "전체",
        maker: str = "전체",
        manufacture_year: Optional[int] = None,
        search_text: str = "",
        limit: int = 500,
) -> List[RecallView]:
    """
    Streamlit '리콜 목록' 화면에서 사용할 리스트 데이터를 조회한다.

    Args
    - scope/maker/manufacture_year/search_text: UI 필터 값
    - limit: 과도한 조회로 인한 UI 렌더링/DB 부하 방지를 위한 상한

    Returns
    - List[RecallView]: 카드 UI에 바로 사용할 DTO 리스트

    판단 근거
    - ORDER BY md.end_date DESC:
      최신 생산기간(또는 최신 모델/기간)의 정보를 상단에 보여주는 것이 사용자 의사결정(최신 이슈 확인)에 유리
    - LIMIT 기본 500:
      Streamlit에서 카드 리스트가 지나치게 길면 UX/성능 저하가 발생하므로 적정 상한을 둠
    - COALESCE 사용:
      NULL이 UI로 그대로 노출되면 레이아웃 깨짐/표현 불명확이 발생 → 빈 문자열/0으로 표준화
    """
    where_sql, params = _build_where(scope, maker, manufacture_year, search_text)

    sql = f"""
        SELECT
            COALESCE(mf.region_at, '')        AS scope,
            COALESCE(mf.maker_name, '')       AS maker,
            COALESCE(md.model_name, '')       AS car_name,
            md.start_date                     AS start_date,
            md.end_date                       AS end_date,
            COALESCE(rc.recall_quantity, 0)   AS target_units,
            COALESCE(rc.defect_desc, '')      AS defect_text,
            COALESCE(rc.fix_method, '')       AS fix_text,
            COALESCE(rc.recall_center, '')    AS contact_text
        FROM tbl_recall rc
        JOIN tbl_model md
          ON rc.model_id = md.model_id
        JOIN tbl_manufacturer mf
          ON md.maker_id = mf.maker_id
        {where_sql}
        ORDER BY md.end_date DESC
        LIMIT %s
    """

    params.append(int(limit))

    out: List[RecallView] = []
    try:
        with mysql.connector.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                for row in cursor.fetchall():
                    out.append(RecallView(*row))
    # - DB 레이어에서 예외를 RuntimeError로 래핑하면, 에러 핸들링이 쉬워짐
    except mysql.connector.Error as err:
        raise RuntimeError(f"DB 오류(fetch_recalls): {err}")

    return out


# ============================================================
# 5) 옵션 데이터: 제작사 목록
# ============================================================

def fetch_makers(scope: str = "전체") -> List[str]:
    """
    Streamlit 제조사 드롭다운 옵션(제작사 목록)을 조회한다.

    Args
    - scope: "전체" 또는 국내/해외

    Returns
    - List[str]: maker_name 리스트

    판단 근거
    - DISTINCT + ORDER BY:
      드롭다운에서 중복 제거 및 알파벳/가나다 순 정렬로 선택 UX 개선
    - scope가 "전체"일 때는 필터 미적용:
      전체 스코프에서는 국내/해외 제조사를 합쳐서 보여주는 것이 자연스러운 UX
    """
    where_sql = ""
    params: List = []

    if scope != "전체":
        where_sql = "WHERE region_at = %s"
        params.append(scope)

    sql = f"""
        SELECT DISTINCT maker_name
        FROM tbl_manufacturer
        {where_sql}
        ORDER BY maker_name
    """

    out: List[str] = []
    try:
        with mysql.connector.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                for (maker_name,) in cursor.fetchall():
                    if maker_name:
                        out.append(maker_name)
    except mysql.connector.Error as err:
        raise RuntimeError(f"DB 오류(fetch_makers): {err}")

    return out


# ============================================================
# 6) 옵션 데이터: 제조 연도 범위(최소~최대)
# ============================================================

def fetch_year_range() -> Tuple[int, int]:
    """
    제조연도 드롭다운(또는 슬라이더)의 최소/최대 연도를 계산한다.

    Returns
    - (min_year, max_year)

    판단 근거
    - start_date/end_date 기준으로 범위를 계산:
      데이터가 추가되어도 UI 옵션 범위가 자동으로 확장되도록 함
    - NULL 데이터 방어:
      데이터 적재 누락/초기 세팅에서 MIN/MAX가 NULL일 수 있으므로 fallback 제공
    """
    sql = """
          SELECT MIN(YEAR (start_date)) AS min_year,
                 MAX(YEAR (end_date))   AS max_year
          FROM tbl_model
          WHERE start_date IS NOT NULL
            AND end_date IS NOT NULL \
          """

    try:
        with mysql.connector.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
                # - UI가 연도 옵션을 렌더링하지 못하면 화면이 깨질 수 있어 안전한 기본값 제공
                if not row or row[0] is None or row[1] is None:
                    return 2000, datetime.now().year
                return int(row[0]), int(row[1])
    except mysql.connector.Error as err:
        raise RuntimeError(f"DB 오류(fetch_year_range): {err}")


# ============================================================
# 7) 통계: KPI (건수/대수)
# ============================================================

def fetch_kpi(scope: str, maker: str, year: int) -> Tuple[int, int]:
    """
    대시보드 KPI(리콜 건수, 리콜 대상 대수)를 조회한다.

    Args
    - scope/maker/year: KPI에 적용할 필터

    Returns
    - (recall_cnt, total_units)

    판단 근거
    - SUM(recall_quantity)는 NULL 가능성이 있어 COALESCE로 0 처리:
      KPI 카드에 NULL 표시 방지 및 계산 안정성 확보
    - search_text는 KPI에서 제외:
      KPI는 '필터 기반 요약'이 목적이므로, 검색어는 목록 탐색에만 적용(해석 혼선 방지)
    """
    where_sql, params = _build_where(scope, maker, year, search_text="")

    sql = f"""
        SELECT
            COUNT(*) AS recall_cnt,
            COALESCE(SUM(COALESCE(rc.recall_quantity, 0)), 0) AS total_units
        FROM tbl_recall rc
        JOIN tbl_model md
          ON rc.model_id = md.model_id
        JOIN tbl_manufacturer mf
          ON md.maker_id = mf.maker_id
        {where_sql}
    """

    try:
        with mysql.connector.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                cnt, units = cursor.fetchone()
                return int(cnt or 0), int(units or 0)
    except mysql.connector.Error as err:
        raise RuntimeError(f"DB 오류(fetch_kpi): {err}")


# ============================================================
# 8) 통계: 제조사별 리콜 건수 TOP N
# ============================================================

def fetch_maker_ranking(scope: str, maker: str, year: int, top_n: int = 20):
    """
    제조사별 리콜 건수 랭킹을 조회한다.

    Args
    - scope/maker/year: 필터
    - top_n: 상위 N개 반환

    Returns
    - List[Tuple[maker, recall_cnt]]

    판단 근거
    - LIMIT top_n:
      막대그래프/테이블에서 과도한 항목은 가독성을 떨어뜨리므로 상위 N개로 컷오프
    """
    where_sql, params = _build_where(scope, maker, year, search_text="")

    sql = f"""
        SELECT
            mf.maker_name AS maker,
            COUNT(*)      AS recall_cnt
        FROM tbl_recall rc
        JOIN tbl_model md
          ON rc.model_id = md.model_id
        JOIN tbl_manufacturer mf
          ON md.maker_id = mf.maker_id
        {where_sql}
        GROUP BY mf.maker_name
        ORDER BY recall_cnt DESC
        LIMIT %s
    """
    params.append(int(top_n))

    rows = []
    try:
        with mysql.connector.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
    except mysql.connector.Error as err:
        raise RuntimeError(f"DB 오류(fetch_maker_ranking): {err}")

    return rows


# ============================================================
# 9) 통계: 연도별 추이
# ============================================================

def fetch_year_trend(scope: str, maker: str, min_year: int, max_year: int):
    """
    연도별 리콜 건수 추이를 리스트 형태로 반환한다.

    Args
    - scope/maker: 필터
    - min_year/max_year: 집계 연도 범위

    Returns
    - List[Tuple[year, recall_cnt]]
    """
    trend = []
    for y in range(min_year, max_year + 1):
        cnt, _ = fetch_kpi(scope, maker, y)
        trend.append((y, cnt))
    return trend


# ============================================================
# 10) 통계: 모델별 리콜 순위 TOP N
# ============================================================

def fetch_model_ranking(scope: str, maker: str, year: int, top_n: int = 20):
    """
    모델(차명) 단위의 리콜 건수 랭킹을 조회한다.

    Args
    - scope/maker/year: 필터
    - top_n: 상위 N개 반환

    Returns
    - List[Tuple[car_name, recall_cnt]]

    판단 근거
    - GROUP BY md.model_name:
      사용자 관점에서 '차종/모델별 리콜 빈도'가 핵심 비교 포인트이므로 모델명 기준 집계
    - ORDER BY recall_cnt DESC:
      리콜이 많은 모델을 상단에 노출해 리스크 기반 탐색을 지원
    """
    where_sql, params = _build_where(scope, maker, year, search_text="")

    sql = f"""
        SELECT
            md.model_name AS car_name,
            COUNT(*)      AS recall_cnt
        FROM tbl_recall rc
        JOIN tbl_model md
          ON rc.model_id = md.model_id
        JOIN tbl_manufacturer mf
          ON md.maker_id = mf.maker_id
        {where_sql}
        GROUP BY md.model_name
        ORDER BY recall_cnt DESC
        LIMIT %s
    """
    params.append(int(top_n))

    rows = []
    try:
        with mysql.connector.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
    except mysql.connector.Error as err:
        raise RuntimeError(f"DB 오류(fetch_model_ranking): {err}")

    return rows
