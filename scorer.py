"""
종목 종합 점수 산출 모듈
각 조건 충족 시 점수 부여, 최대 18점
"""
import logging

import numpy as np
import pandas as pd

from filters.technical import (
    check_ma_uptrend,
    check_ma_full_alignment,
    check_base_formation,
    check_bollinger_contraction,
    check_volume_breakout,
    check_rsi_not_overbought,
)
from filters.fundamental import (
    check_eps_growth,
    check_revenue_growth,
    check_operating_profit_growth,
)
from config import (
    SMART_MONEY_MIN_DAYS,
    SMART_MONEY_MIN_NET_BUY,
    MA_LONG,
    BOLLINGER_PERIOD,
    BOLLINGER_STD,
    VOLUME_AVG_PERIOD,
    VOLUME_SURGE_RATIO,
    MA_SLOPE_LOOKBACK,
    BOLLINGER_CONTRACTION_LOOKBACK,
    VOLUME_BREAKOUT_LOOKBACK,
)

logger = logging.getLogger(__name__)

# 점수 배점 정의
SCORE_TABLE = {
    "ma_uptrend":              2,  # 200일선 우상향
    "ma_full_alignment":       1,  # 강세 배열 (현재가 > MA50 > MA150 > MA200)
    "base_formation":          1,  # 베이스 형성
    "bollinger_contraction":   2,  # 볼린저 수축
    "volume_breakout":         3,  # 거래량 돌파 발산
    "rsi_not_overbought":      1,  # RSI 과매수 아님 (< 75)
    "eps_growth":              2,  # EPS 연속 증가 10% 이상
    "revenue_growth":          1,  # 매출 증가
    "operating_profit_growth": 1,  # 영업이익 흑자 + 증가
    "institution_net_buy":     2,  # 기관 순매수 10일+, 50억+
    "foreign_net_buy":         2,  # 외국인 순매수 10일+, 50억+
}

MAX_SCORE = sum(SCORE_TABLE.values())  # 18점

TECH_ONLY_KEYS = [
    "ma_uptrend", "ma_full_alignment", "base_formation",
    "bollinger_contraction", "volume_breakout", "rsi_not_overbought",
]
MAX_SCORE_TECH = sum(SCORE_TABLE[k] for k in TECH_ONLY_KEYS)  # 10점


def _calc_sub_score(
    df: pd.DataFrame,
    financials: pd.DataFrame | None,
    investor_trend: pd.DataFrame | None,
) -> float:
    """
    조건 충족 강도를 0.0~1.0 연속값으로 계산.
    이진 총점이 동일한 종목 간 분별력 확보용 타이브레이커.
    데이터 미비 항목은 평균 계산에서 제외.
    """
    parts: list[float] = []

    # 1. 200일선 기울기 강도 (MA_SLOPE_LOOKBACK일 상승폭 / 5% 기준)
    if len(df) >= MA_LONG + MA_SLOPE_LOOKBACK:
        ma200 = df["close"].rolling(MA_LONG).mean()
        prev = ma200.iloc[-MA_SLOPE_LOOKBACK - 1]
        curr = ma200.iloc[-1]
        if not pd.isna(prev) and not pd.isna(curr) and prev > 0:
            slope_pct = (curr - prev) / prev
            parts.append(float(np.clip(slope_pct / 0.05, 0.0, 1.0)))

    # 2. 볼린저 수축 강도 (현재 밴드폭 백분위 역순: 수축할수록 1.0)
    mid = df["close"].rolling(BOLLINGER_PERIOD).mean()
    std_dev = df["close"].rolling(BOLLINGER_PERIOD).std()
    band_width = (mid + BOLLINGER_STD * std_dev) - (mid - BOLLINGER_STD * std_dev)
    recent_widths = band_width.dropna().iloc[-BOLLINGER_CONTRACTION_LOOKBACK:]
    if len(recent_widths) >= BOLLINGER_PERIOD:
        current_w = float(recent_widths.iloc[-1])
        pct_rank = float((recent_widths < current_w).mean())  # 0=가장 수축
        parts.append(1.0 - pct_rank)

    # 3. 거래량 서지 피크 강도 (최근 최대 배수 / (VOLUME_SURGE_RATIO * 3) 기준)
    vol_avg = df["volume"].rolling(VOLUME_AVG_PERIOD).mean()
    recent_vols = df["volume"].iloc[-VOLUME_BREAKOUT_LOOKBACK:]
    recent_avgs = vol_avg.iloc[-VOLUME_BREAKOUT_LOOKBACK:]
    mask = recent_avgs > 0
    if mask.any():
        ratios = recent_vols[mask].values / recent_avgs[mask].values
        max_ratio = float(ratios.max())
        parts.append(float(np.clip((max_ratio - 1.0) / (VOLUME_SURGE_RATIO * 3.0), 0.0, 1.0)))

    # 4. EPS 성장률 (최근 YoY / 50% 기준 정규화, 양의 베이스만)
    if financials is not None and len(financials) >= 2:
        eps = financials["eps"].iloc[:2].tolist()
        if all(v is not None and not pd.isna(v) for v in eps) and eps[1] > 0:
            rate = (eps[0] - eps[1]) / eps[1]
            parts.append(float(np.clip(rate / 0.5, 0.0, 1.0)))

    # 5. 매출 성장률 (최근 YoY / 30% 기준 정규화)
    if financials is not None and len(financials) >= 2:
        rev = financials["revenue"].iloc[:2].tolist()
        if all(v is not None and not pd.isna(v) for v in rev) and rev[1] > 0:
            rate = (rev[0] - rev[1]) / rev[1]
            parts.append(float(np.clip(rate / 0.3, 0.0, 1.0)))

    # 6. 기관 순매수 비율 (순매수일 / 전체 기간)
    if investor_trend is not None and not investor_trend.empty and "institution_net" in investor_trend.columns:
        total = len(investor_trend)
        buy_days = int((investor_trend["institution_net"] > 0).sum())
        parts.append(buy_days / total)

    # 7. 외국인 순매수 비율 (순매수일 / 전체 기간)
    if investor_trend is not None and not investor_trend.empty and "foreign_net" in investor_trend.columns:
        total = len(investor_trend)
        buy_days = int((investor_trend["foreign_net"] > 0).sum())
        parts.append(buy_days / total)

    return round(sum(parts) / len(parts), 4) if parts else 0.0


def score_stock(
    ticker: str,
    df: pd.DataFrame,
    financials: pd.DataFrame | None,
    investor_trend: pd.DataFrame | None,
    pre_conditions: dict[str, bool] | None = None,
) -> tuple[int, dict[str, bool], float]:
    """
    종목 점수 계산.
    반환: (총점, {조건명: bool} 딕셔너리, sub_score)
    sub_score: 조건 충족 강도 연속값(0.0~1.0), 동점 타이브레이커용
    pre_conditions: 호출 전에 이미 계산된 조건 결과 (재계산 생략용)
    """
    conditions: dict[str, bool] = {}
    _pre = pre_conditions or {}

    # --- 기술적 조건 ---
    try:
        conditions["ma_uptrend"] = _pre.get("ma_uptrend", check_ma_uptrend(df))
    except Exception as e:
        logger.warning(f"{ticker} MA 우상향 판단 오류: {e}")
        conditions["ma_uptrend"] = False

    try:
        conditions["ma_full_alignment"] = _pre.get("ma_full_alignment", check_ma_full_alignment(df))
    except Exception as e:
        logger.warning(f"{ticker} MA 완전 정렬 판단 오류: {e}")
        conditions["ma_full_alignment"] = False

    try:
        conditions["base_formation"] = check_base_formation(df)
    except Exception as e:
        logger.warning(f"{ticker} 베이스 형성 판단 오류: {e}")
        conditions["base_formation"] = False

    try:
        conditions["bollinger_contraction"] = check_bollinger_contraction(df)
    except Exception as e:
        logger.warning(f"{ticker} 볼린저 수축 판단 오류: {e}")
        conditions["bollinger_contraction"] = False

    try:
        conditions["volume_breakout"] = check_volume_breakout(df)
    except Exception as e:
        logger.warning(f"{ticker} 거래량 돌파 판단 오류: {e}")
        conditions["volume_breakout"] = False

    try:
        conditions["rsi_not_overbought"] = check_rsi_not_overbought(df)
    except Exception as e:
        logger.warning(f"{ticker} RSI 판단 오류: {e}")
        conditions["rsi_not_overbought"] = False

    # --- 재무 조건 ---
    try:
        conditions["eps_growth"] = check_eps_growth(financials)
    except Exception as e:
        logger.warning(f"{ticker} EPS 성장 판단 오류: {e}")
        conditions["eps_growth"] = False

    try:
        conditions["revenue_growth"] = check_revenue_growth(financials)
    except Exception as e:
        logger.warning(f"{ticker} 매출 성장 판단 오류: {e}")
        conditions["revenue_growth"] = False

    try:
        conditions["operating_profit_growth"] = check_operating_profit_growth(financials)
    except Exception as e:
        logger.warning(f"{ticker} 영업이익 성장 판단 오류: {e}")
        conditions["operating_profit_growth"] = False

    # --- 수급 조건 ---
    conditions["institution_net_buy"] = False
    conditions["foreign_net_buy"] = False

    if investor_trend is not None and not investor_trend.empty:
        try:
            if "institution_net" in investor_trend.columns:
                inst_series = investor_trend["institution_net"]
                inst_buy_days = (inst_series > 0).sum()
                inst_net_total = inst_series.sum()
                conditions["institution_net_buy"] = (
                    inst_buy_days >= SMART_MONEY_MIN_DAYS
                    and inst_net_total >= SMART_MONEY_MIN_NET_BUY
                )

            if "foreign_net" in investor_trend.columns:
                foreign_series = investor_trend["foreign_net"]
                foreign_buy_days = (foreign_series > 0).sum()
                foreign_net_total = foreign_series.sum()
                conditions["foreign_net_buy"] = (
                    foreign_buy_days >= SMART_MONEY_MIN_DAYS
                    and foreign_net_total >= SMART_MONEY_MIN_NET_BUY
                )
        except Exception as e:
            logger.warning(f"{ticker} 수급 판단 오류: {e}")

    # --- 점수 합산 ---
    total_score = sum(
        SCORE_TABLE[cond] for cond, met in conditions.items() if met
    )

    sub_score = _calc_sub_score(df, financials, investor_trend)

    logger.debug(f"{ticker} 점수: {total_score}/{MAX_SCORE} sub={sub_score:.4f} | {conditions}")
    return total_score, conditions, sub_score
