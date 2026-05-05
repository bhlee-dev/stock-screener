"""
pykrx를 활용한 KRX 주가/수급 데이터 수집 모듈
"""
import time
import threading
import logging
from datetime import datetime, timedelta

import pandas as pd
from pykrx import stock

logger = logging.getLogger(__name__)

# 전역 rate limiter ─ 워커 수에 관계없이 pykrx API 호출 빈도를 제한.
# 10개 워커가 동시에 호출하면 잠재적으로 33회/초 → IP 차단 위험.
# _MIN_CALL_INTERVAL 기준으로 직렬화하여 전체 합산 최대 ~6회/초로 제한.
_rate_lock = threading.Lock()
_last_call_time: float = 0.0
_MIN_CALL_INTERVAL: float = 0.15  # 초 (≒ 6–7회/초)
_API_DELAY = 0.3  # 메인 스레드 단독 호출 시 사용


def _throttle() -> None:
    """전역 rate limiter: 모든 워커 합산 pykrx API 호출 빈도를 제한한다."""
    global _last_call_time
    with _rate_lock:
        now = time.monotonic()
        wait = _last_call_time + _MIN_CALL_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.monotonic()


def _today_str() -> str:
    return datetime.today().strftime("%Y%m%d")


def _date_before(days: int) -> str:
    return (datetime.today() - timedelta(days=days)).strftime("%Y%m%d")


def get_all_tickers(market: str = "ALL") -> list[str]:
    """
    코스피 + 코스닥 전 종목 티커 반환.
    시가총액 500억 미만, 관리종목, 거래정지 종목 제외.
    market: "KOSPI" | "KOSDAQ" | "ALL"
    """
    today = _today_str()
    tickers: list[str] = []

    markets = ["KOSPI", "KOSDAQ"] if market == "ALL" else [market]

    for mkt in markets:
        try:
            mkt_tickers = stock.get_market_ticker_list(today, market=mkt)
            tickers.extend(mkt_tickers)
            time.sleep(_API_DELAY)
        except Exception as e:
            logger.error(f"{mkt} 티커 목록 수집 실패: {e}")

    # 시가총액 일괄 조회 (루프 밖에서 한 번만 — 기존 루프 내 반복 조회 버그 수정)
    from config import MIN_MARKET_CAP
    try:
        markets_for_cap = ["KOSPI", "KOSDAQ"] if market == "ALL" else [market]
        cap_frames = []
        for mkt in markets_for_cap:
            df_cap = stock.get_market_cap_by_ticker(today, market=mkt)
            time.sleep(_API_DELAY)
            if df_cap is not None and not df_cap.empty:
                cap_frames.append(df_cap)
        cap_df = pd.concat(cap_frames) if cap_frames else pd.DataFrame()
    except Exception as e:
        logger.error(f"시가총액 일괄 조회 실패, 전체 티커 반환: {e}")
        return tickers

    # cap_df에 없는 종목은 조회 실패로 간주해 포함 처리
    filtered = [
        t for t in tickers
        if t not in cap_df.index or cap_df.loc[t, "시가총액"] >= MIN_MARKET_CAP
    ]

    logger.info(f"총 {len(tickers)}개 티커 중 {len(filtered)}개 필터 통과")
    return filtered


def _last_trading_day() -> str:
    """
    KRX 데이터가 실제로 존재하는 가장 최근 거래일 반환.
    당일 데이터가 없으면(장 마감 전 실행 등) 최대 7일 전까지 역으로 탐색.
    """
    for days_back in range(7):
        candidate = (datetime.today() - timedelta(days=days_back)).strftime("%Y%m%d")
        try:
            df = stock.get_market_cap_by_ticker(candidate, market="KOSPI")
            time.sleep(_API_DELAY)
            if df is not None and not df.empty and df["시가총액"].sum() > 0:
                if days_back > 0:
                    logger.info(f"당일 KRX 데이터 없음 — {days_back}일 전({candidate}) 데이터 사용")
                return candidate
        except Exception:
            pass
    return _date_before(1)  # 최후 fallback


def get_all_tickers_fast() -> list[str]:
    """
    시가총액 일괄 조회로 속도 최적화된 버전.
    전 종목 시가총액을 한 번에 가져와 필터링.
    """
    from config import MIN_MARKET_CAP
    trade_date = _last_trading_day()

    try:
        # 코스피 + 코스닥 시가총액 일괄 조회
        cap_kospi = stock.get_market_cap_by_ticker(trade_date, market="KOSPI")
        time.sleep(_API_DELAY)
        cap_kosdaq = stock.get_market_cap_by_ticker(trade_date, market="KOSDAQ")
        time.sleep(_API_DELAY)

        cap_all = pd.concat([cap_kospi, cap_kosdaq])
        # 시가총액 기준 필터
        filtered = cap_all[cap_all["시가총액"] >= MIN_MARKET_CAP].index.tolist()

        logger.info(
            f"전체 {len(cap_all)}개 중 시총 500억 이상 {len(filtered)}개 선별"
        )
        return filtered
    except Exception as e:
        logger.error(f"빠른 티커 수집 실패, 기본 방식으로 전환: {e}")
        return get_all_tickers()


# 200일선 계산에 필요한 최소 거래일
MA_LONG_MIN = 200


def get_ohlcv(ticker: str, period: int = 300) -> pd.DataFrame | None:
    """
    최근 period 거래일 OHLCV 데이터 반환.
    컬럼: date(index), open, high, low, close, volume
    실패 시 None 반환.
    """
    # 거래일 기준으로 충분한 달력 일수 확보 (거래일 × 1.5 대략)
    start = _date_before(int(period * 1.5))
    end = _today_str()

    for attempt in range(3):
        try:
            df = stock.get_market_ohlcv_by_date(start, end, ticker)
            _throttle()

            if df is None or df.empty:
                logger.warning(f"{ticker} OHLCV 데이터 없음")
                return None

            # 컬럼명 영문 통일
            df = df.rename(columns={
                "시가": "open",
                "고가": "high",
                "저가": "low",
                "종가": "close",
                "거래량": "volume",
            })
            df.index.name = "date"

            # 필요한 거래일만 최근 period일로 자르기
            df = df.tail(period)

            if len(df) < MA_LONG_MIN:
                logger.warning(f"{ticker} 데이터 부족 ({len(df)}일, 최소 {MA_LONG_MIN}일 필요)")
                return None

            return df[["open", "high", "low", "close", "volume"]]

        except Exception as e:
            logger.warning(f"{ticker} OHLCV 수집 실패 ({attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(1.0)

    return None


def get_investor_trend(ticker: str, period: int = 20) -> pd.DataFrame | None:
    """
    최근 period 거래일 기관/외국인 순매수 데이터 반환.
    pykrx get_market_trading_value_by_date 활용.
    컬럼: date(index), institution_net, foreign_net
    실패 시 None 반환.
    """
    start = _date_before(int(period * 1.5))
    end = _today_str()

    for attempt in range(3):
        try:
            df = stock.get_market_trading_value_by_date(start, end, ticker)
            _throttle()

            if df is None or df.empty:
                logger.warning(f"{ticker} 수급 데이터 없음")
                return None

            # 기관 순매수 컬럼: "기관합계", 외국인: "외국인합계"
            col_map = {}
            for col in df.columns:
                if "기관" in col:
                    col_map[col] = "institution_net"
                elif "외국인" in col:
                    col_map[col] = "foreign_net"

            df = df.rename(columns=col_map)
            keep = [c for c in ["institution_net", "foreign_net"] if c in df.columns]
            if not keep:
                logger.warning(f"{ticker} 수급 컬럼 매핑 실패, 컬럼: {df.columns.tolist()}")
                return None

            return df[keep].tail(period)

        except Exception as e:
            logger.warning(f"{ticker} 수급 수집 실패 ({attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(1.0)

    return None
