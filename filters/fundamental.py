"""
재무 분석 필터 모듈
EPS, 매출액, 영업이익 성장 여부 판단
"""
import logging
import sys
import os

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import EPS_MIN_GROWTH_RATE

logger = logging.getLogger(__name__)


def check_eps_growth(financials: pd.DataFrame) -> bool:
    """
    최근 2개 연도 연속 EPS 증가 여부 확인.
    financials: get_recent_financials() 반환 DataFrame (year 내림차순 정렬)
    """
    if financials is None or len(financials) < 3:
        return False

    # year 내림차순: [0]=최근, [1]=전년, [2]=전전년
    eps_values = financials["eps"].iloc[:3].tolist()

    if any(v is None or pd.isna(v) for v in eps_values):
        return False

    # 연속 증가 + 최근 YoY 성장률 최솟값 (EPS_MIN_GROWTH_RATE) 충족
    if not (eps_values[0] > eps_values[1] > eps_values[2]):
        return False

    base = eps_values[1]
    if base == 0:
        return False
    growth_rate = (eps_values[0] - base) / abs(base)
    return growth_rate >= EPS_MIN_GROWTH_RATE


def check_revenue_growth(financials: pd.DataFrame) -> bool:
    """
    전년 대비 매출액 증가 여부 확인.
    최근 연도 매출액 > 전년 매출액
    """
    if financials is None or len(financials) < 2:
        return False

    revenues = financials["revenue"].iloc[:2].tolist()

    if any(v is None or pd.isna(v) for v in revenues):
        return False

    return revenues[0] > revenues[1]


def check_operating_profit_growth(financials: pd.DataFrame) -> bool:
    """
    전년 대비 영업이익 증가 여부 확인.
    최근 연도 영업이익 > 전년 영업이익
    적자 구간(음수)에서 개선되는 것도 증가로 인정.
    """
    if financials is None or len(financials) < 2:
        return False

    op_profits = financials["operating_profit"].iloc[:2].tolist()

    if any(v is None or pd.isna(v) for v in op_profits):
        return False

    # 흑자(양수) + 증가 동시 충족 — 적자 개선만으로는 통과 불가
    return op_profits[0] > 0 and op_profits[0] > op_profits[1]
