"""
기술적 분석 필터 모듈
볼린저 밴드, 이동평균선, 거래량 돌파, 베이스 형성 판단
"""
import logging

import numpy as np
import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import (
    MA_MEDIUM,
    MA_SHORT,
    MA_LONG,
    BOLLINGER_PERIOD,
    BOLLINGER_STD,
    VOLUME_SURGE_RATIO,
    VOLUME_AVG_PERIOD,
    MA_SLOPE_LOOKBACK,
    BOLLINGER_CONTRACTION_LOOKBACK,
    BOLLINGER_CONTRACTION_PERCENTILE,
    VOLUME_BREAKOUT_LOOKBACK,
    HIGH52W_PROXIMITY,
    BASE_FORMATION_DAYS,
    BASE_FORMATION_THRESHOLD,
    BASE_FORMATION_MAX_DECLINE,
    RSI_PERIOD,
    RSI_OVERBOUGHT,
)

logger = logging.getLogger(__name__)


def _bollinger_bands(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저 밴드 (중간선, 상단, 하단) 계산."""
    mid = df["close"].rolling(BOLLINGER_PERIOD).mean()
    std = df["close"].rolling(BOLLINGER_PERIOD).std()
    upper = mid + BOLLINGER_STD * std
    lower = mid - BOLLINGER_STD * std
    return mid, upper, lower


def check_ma_uptrend(df: pd.DataFrame) -> bool:
    """
    200일 이동평균선 우상향 + 현재가가 150일선 위에 있는지 확인.
    기준: 200일선의 최근 MA_SLOPE_LOOKBACK일 기울기가 양수.
    """
    if len(df) < MA_LONG:
        logger.debug("데이터 부족으로 MA 우상향 판단 불가")
        return False

    ma200 = df["close"].rolling(MA_LONG).mean()
    ma150 = df["close"].rolling(MA_SHORT).mean()

    slope_now = ma200.iloc[-1]
    slope_prev = ma200.iloc[-MA_SLOPE_LOOKBACK - 1]

    if pd.isna(slope_now) or pd.isna(slope_prev):
        return False

    is_uptrend = slope_now > slope_prev

    current_price = df["close"].iloc[-1]
    ma150_now = ma150.iloc[-1]
    above_ma150 = (not pd.isna(ma150_now)) and (current_price > ma150_now)

    return is_uptrend and above_ma150


def check_ma_full_alignment(df: pd.DataFrame) -> bool:
    """
    강세 배열 확인: 현재가 > MA50 > MA150 > MA200.
    추세 강도가 완전히 정렬된 종목만 통과.
    """
    if len(df) < MA_LONG:
        return False

    current_price = df["close"].iloc[-1]
    ma50 = df["close"].rolling(MA_MEDIUM).mean().iloc[-1]
    ma150 = df["close"].rolling(MA_SHORT).mean().iloc[-1]
    ma200 = df["close"].rolling(MA_LONG).mean().iloc[-1]

    if any(pd.isna(v) for v in [ma50, ma150, ma200]):
        return False

    return current_price > ma50 > ma150 > ma200


def calc_rsi(df: pd.DataFrame) -> float:
    """RSI(14) 계산. 데이터 부족 시 50.0 반환."""
    if len(df) < RSI_PERIOD + 1:
        return 50.0
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    last_loss = loss.iloc[-1]
    if pd.isna(last_loss) or last_loss == 0:
        return 100.0
    rs = gain.iloc[-1] / last_loss
    return float(100 - (100 / (1 + rs)))


def check_rsi_not_overbought(df: pd.DataFrame) -> bool:
    """RSI가 과매수 기준(RSI_OVERBOUGHT) 미만인지 확인."""
    return calc_rsi(df) < RSI_OVERBOUGHT


def check_bollinger_contraction(df: pd.DataFrame) -> bool:
    """
    볼린저 밴드 폭이 수축 중인지 확인.
    현재 밴드폭이 최근 BOLLINGER_CONTRACTION_LOOKBACK일 대비
    하위 BOLLINGER_CONTRACTION_PERCENTILE% 이내이면 수축으로 판정.
    """
    _, upper, lower = _bollinger_bands(df)
    band_width = upper - lower

    # 유효 데이터만 사용
    recent_widths = band_width.dropna().iloc[-BOLLINGER_CONTRACTION_LOOKBACK:]
    if len(recent_widths) < BOLLINGER_PERIOD:
        return False

    current_width = recent_widths.iloc[-1]
    threshold = np.percentile(recent_widths, BOLLINGER_CONTRACTION_PERCENTILE)

    return current_width <= threshold


def check_volume_breakout(df: pd.DataFrame) -> bool:
    """
    최근 VOLUME_BREAKOUT_LOOKBACK 거래일 내 거래량 급증 + 가격 돌파 동시 발생 여부.
    조건:
      1) 거래량이 20일 평균의 VOLUME_SURGE_RATIO배 이상
      2) 종가가 볼린저 상단 돌파 OR 52주 신고가 5% 이내
    """
    _, upper, _ = _bollinger_bands(df)
    vol_avg = df["volume"].rolling(VOLUME_AVG_PERIOD).mean()

    # 52주(252 거래일) 최고가
    lookback_52w = min(252, len(df))
    high_52w = df["high"].iloc[-lookback_52w:].max()

    recent = df.iloc[-VOLUME_BREAKOUT_LOOKBACK:]
    for idx in range(len(recent)):
        row_idx = len(df) - VOLUME_BREAKOUT_LOOKBACK + idx
        row = df.iloc[row_idx]

        avg_vol = vol_avg.iloc[row_idx]
        upper_val = upper.iloc[row_idx]

        if pd.isna(avg_vol) or pd.isna(upper_val):
            continue

        # 조건 1: 거래량 급증
        vol_surge = row["volume"] >= avg_vol * VOLUME_SURGE_RATIO

        # 조건 2-a: 볼린저 상단 돌파
        bb_breakout = row["close"] >= upper_val

        # 조건 2-b: 52주 신고가 5% 이내
        near_52w_high = row["close"] >= high_52w * (1 - HIGH52W_PROXIMITY)

        if vol_surge and (bb_breakout or near_52w_high):
            return True

    return False


def check_base_formation(df: pd.DataFrame) -> bool:
    """
    베이스(횡보 구간) 형성 여부 확인.
    최근 BASE_FORMATION_DAYS[0]~BASE_FORMATION_DAYS[1]일 구간에서
    (1) 고가-저가 변동폭이 BASE_FORMATION_THRESHOLD 이내이고
    (2) 구간 초반 대비 후반 평균가 하락이 BASE_FORMATION_MAX_DECLINE 이내여야 베이스로 판정.
    """
    min_days, max_days = BASE_FORMATION_DAYS

    if len(df) < min_days:
        return False

    lookback = min(max_days, len(df))
    segment = df.iloc[-lookback:]

    period_high = segment["high"].max()
    period_low = segment["low"].min()

    if period_low == 0:
        return False

    # 조건 1: 변동폭 = (고가 - 저가) / 저가
    fluctuation = (period_high - period_low) / period_low
    if fluctuation > BASE_FORMATION_THRESHOLD:
        return False

    # 조건 2: 방향성 체크 — 구간 초반 20% vs 후반 20% 종가 평균 비교
    # 초반 대비 BASE_FORMATION_MAX_DECLINE% 이상 하락이면 하락 추세이므로 베이스 아님
    fifth = max(1, lookback // 5)
    early_avg = segment["close"].iloc[:fifth].mean()
    late_avg = segment["close"].iloc[-fifth:].mean()
    if early_avg > 0 and (early_avg - late_avg) / early_avg > BASE_FORMATION_MAX_DECLINE:
        return False

    return True
