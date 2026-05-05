"""
DART OpenAPI를 활용한 재무 데이터 수집 모듈
API 문서: https://opendart.fss.or.kr
"""
import io
import json
import time
import threading
import zipfile
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import requests
import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import DART_API_KEY

logger = logging.getLogger(__name__)

_API_DELAY = 0.3
_BASE_URL = "https://opendart.fss.or.kr/api"
_TIMEOUT = 10

# ── 재무 데이터 분기별 로컬 캐시 ────────────────────────────────────────────────
# 재무데이터는 분기마다 한 번만 바뀌므로 같은 분기 내 재조회를 모두 캐시로 처리.
# _mem_cache 구조: {corp_code: {"quarter": "2026Q2", "records": [...]}}
_CACHE_FILE = Path(__file__).parent / "financials_cache.json"
_cache_lock = threading.Lock()
_mem_cache: dict = {}
_cache_initialized = False


def _current_quarter() -> str:
    """현재 분기 문자열 반환. 예: '2026Q2'"""
    today = datetime.today()
    q = (today.month - 1) // 3 + 1
    return f"{today.year}Q{q}"


def _init_cache() -> None:
    """디스크 캐시를 메모리로 로드 (최초 1회만). 이후 호출은 즉시 반환."""
    global _cache_initialized
    if _cache_initialized:
        return
    with _cache_lock:
        if _cache_initialized:
            return
        if _CACHE_FILE.exists():
            try:
                loaded = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
                _mem_cache.update(loaded)
                logger.info(f"재무 캐시 로드: {len(_mem_cache)}개 종목")
            except Exception as e:
                logger.warning(f"재무 캐시 로드 실패 (빈 캐시로 시작): {e}")
        _cache_initialized = True


def _flush_cache() -> None:
    """메모리 캐시를 디스크에 기록. _cache_lock 보유 상태에서 호출해야 함."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps(_mem_cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"재무 캐시 저장 실패: {e}")


def preload_financials_cache() -> None:
    """메인 스레드에서 캐시를 선제 로드. 이번 분기 캐시 현황을 로그로 출력."""
    _init_cache()
    quarter = _current_quarter()
    cached = sum(1 for v in _mem_cache.values() if v.get("quarter") == quarter)
    logger.info(f"재무 캐시: 이번 분기({quarter}) {cached}/{len(_mem_cache)}개 종목 유효")


def _request(endpoint: str, params: dict, attempt_max: int = 3) -> dict | None:
    """DART API 공통 요청 함수. 실패 시 최대 attempt_max회 재시도."""
    params = {**params, "crtfc_key": DART_API_KEY}  # 호출자 dict 변이 방지
    url = f"{_BASE_URL}/{endpoint}"

    for attempt in range(attempt_max):
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "000":
                return data
            else:
                logger.warning(
                    f"DART API 오류 [{endpoint}] status={data.get('status')} "
                    f"msg={data.get('message', '')}"
                )
                return None
        except requests.RequestException as e:
            logger.warning(f"DART 요청 실패 ({attempt+1}/{attempt_max}): {e}")
            if attempt < attempt_max - 1:
                time.sleep(1.0)

    return None


@lru_cache(maxsize=1)
def _load_corp_code_map() -> dict[str, str]:
    """
    DART corpCode.xml을 다운로드하여 {종목코드: corp_code} 딕셔너리 반환.
    lru_cache로 세션 내 1회만 다운로드.
    """
    url = f"{_BASE_URL}/corpCode.xml"
    params = {"crtfc_key": DART_API_KEY}

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()

            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                xml_filename = zf.namelist()[0]
                with zf.open(xml_filename) as f:
                    tree = ET.parse(f)

            root = tree.getroot()
            corp_map: dict[str, str] = {}
            for item in root.iter("list"):
                stock_code = item.findtext("stock_code", "").strip()
                corp_code = item.findtext("corp_code", "").strip()
                if stock_code:
                    corp_map[stock_code] = corp_code

            logger.info(f"DART corp_code 맵 로드 완료: {len(corp_map)}개")
            return corp_map

        except Exception as e:
            logger.warning(f"corpCode.xml 로드 실패 ({attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(1.0)

    logger.error("corpCode.xml 최종 로드 실패, 빈 맵 반환")
    return {}


def preload_corp_code_map() -> None:
    """메인 스레드에서 1회 선제 호출해 lru_cache를 채운다. 이후 워커 스레드는 캐시만 읽는다."""
    _load_corp_code_map()


def get_corp_code(ticker: str) -> str | None:
    """
    종목 티커 → DART 고유번호(corp_code) 변환.
    ticker: 6자리 종목코드 (예: "005930")
    """
    corp_map = _load_corp_code_map()
    result = corp_map.get(ticker)
    if not result:
        logger.warning(f"{ticker} corp_code 매핑 없음")
    return result


def get_recent_financials(corp_code: str) -> pd.DataFrame | None:
    """
    DART API로 최근 4개년 연간 재무 데이터 수집.
    같은 분기 내 재조회 시 로컬 캐시를 즉시 반환 (API 호출 없음).
    반환: 연도별 매출액, 영업이익, 당기순이익, EPS DataFrame.
    컬럼: year, revenue, operating_profit, net_income, eps
    실패 시 None 반환.
    """
    _init_cache()
    quarter = _current_quarter()

    # ── 캐시 히트: 이번 분기 데이터가 이미 있으면 즉시 반환 ──────────────────────
    entry = _mem_cache.get(corp_code)
    if entry and entry.get("quarter") == quarter:
        records = entry.get("records", [])
        logger.debug(f"{corp_code} 재무 캐시 히트 ({quarter})")
        return pd.DataFrame(records) if records else None

    # ── 캐시 미스: DART API 호출 ─────────────────────────────────────────────────
    # 사업보고서(연간)만 사용 — 분기보고서는 YTD 누적값이라 연간값과 혼용 시 비교 왜곡 발생.
    # 5개년을 시도해 최대 4개년 연간 데이터 확보 (당해 연도 보고서는 미제출일 수 있음).
    current_year = datetime.today().year
    years_to_try = [str(current_year - i) for i in range(0, 5)]

    records = []
    for year in years_to_try:
        params = {
            "corp_code": corp_code,
            "bsns_year": year,
            "reprt_code": "11011",  # 사업보고서(연간)만
            "fs_div": "CFS",
        }
        data = _request("fnlttSinglAcntAll.json", params)
        time.sleep(_API_DELAY)

        if not data:
            params["fs_div"] = "OFS"
            data = _request("fnlttSinglAcntAll.json", params)
            time.sleep(_API_DELAY)

        if not data or "list" not in data:
            continue

        row = _parse_financial_row(data["list"], year, "사업보고서")
        if row:
            records.append(row)

        if len(records) >= 4:
            break

    # ── 결과를 캐시에 저장 (데이터 없는 종목도 저장해 재조회 방지) ─────────────────
    with _cache_lock:
        _mem_cache[corp_code] = {"quarter": quarter, "records": records}
        _flush_cache()

    if not records:
        logger.warning(f"{corp_code} 재무데이터 없음")
        return None

    df = pd.DataFrame(records)
    df = df.sort_values("year", ascending=False).reset_index(drop=True)
    return df


def _parse_financial_row(items: list[dict], year: str, reprt_name: str) -> dict | None:
    """DART API 응답 list에서 주요 재무 항목 추출."""
    TARGET_ACCOUNTS = {
        "revenue": ["ifrs-full_Revenue", "dart_Revenue", "us-gaap_Revenues"],
        "operating_profit": [
            "ifrs-full_ProfitLossFromOperatingActivities",
            "dart_OperatingIncomeLoss",
        ],
        "net_income": [
            "ifrs-full_ProfitLoss",
            "dart_ProfitLoss",
            "us-gaap_NetIncomeLoss",
        ],
        "eps": [
            "ifrs-full_BasicEarningsLossPerShare",
            "dart_BasicEarningsLossPerShare",
        ],
    }

    result: dict = {"year": int(year), "reprt": reprt_name}

    for field, account_ids in TARGET_ACCOUNTS.items():
        value = None
        for item in items:
            acnt_id = item.get("account_id", "")
            if acnt_id in account_ids:
                raw = item.get("thstrm_amount", "").replace(",", "").replace(" ", "")
                if raw and raw not in ("-", ""):
                    try:
                        value = float(raw)
                        break
                    except ValueError:
                        pass
        result[field] = value

    if result.get("revenue") is None:
        return None

    return result
