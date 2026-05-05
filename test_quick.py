"""
스크리너 빠른 동작 테스트 — 처음 N개 종목만 실행
사용: python test_quick.py [--n 10]
"""
import argparse
import logging
import sys

import config  # noqa: load_dotenv

from data.krx_fetcher import get_all_tickers_fast, get_ohlcv, get_investor_trend
from data.dart_fetcher import get_corp_code, get_recent_financials
from filters.technical import check_ma_uptrend
from scorer import score_stock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _get_stock_name(ticker: str) -> str:
    try:
        from pykrx import stock
        return stock.get_market_ticker_name(ticker)
    except Exception:
        return ticker


def run_quick(n: int = 10) -> None:
    logger.info(f"=== 빠른 테스트: 처음 {n}개 종목 ===")

    tickers = get_all_tickers_fast()
    sample = tickers[:n]
    logger.info(f"전체 {len(tickers)}개 중 {len(sample)}개 테스트")

    passed, skipped, errors = [], 0, 0

    for idx, ticker in enumerate(sample, 1):
        name = _get_stock_name(ticker)
        logger.info(f"[{idx}/{n}] {ticker} {name}")
        try:
            df = get_ohlcv(ticker, period=300)
            if df is None:
                logger.info(f"  -> OHLCV 없음, 스킵")
                skipped += 1
                continue

            if not check_ma_uptrend(df):
                logger.info(f"  -> MA 우상향 미충족, 스킵")
                skipped += 1
                continue

            corp_code = get_corp_code(ticker)
            financials = get_recent_financials(corp_code) if corp_code else None

            investor_trend = get_investor_trend(ticker, period=config.INVESTOR_TREND_PERIOD)

            score, conditions = score_stock(ticker, df, financials, investor_trend)
            logger.info(f"  -> 점수: {score}점  조건: {conditions}")
            passed.append((ticker, name, score, conditions))

        except Exception as e:
            logger.warning(f"  -> 오류: {e}")
            errors += 1

    logger.info(f"\n=== 결과 ===")
    logger.info(f"통과: {len(passed)}개 | 스킵: {skipped}개 | 오류: {errors}개")
    if passed:
        passed.sort(key=lambda x: x[2], reverse=True)
        for ticker, name, score, conds in passed:
            logger.info(f"  {ticker} {name}: {score}점 — {conds}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10, help="테스트할 종목 수 (기본 10)")
    args = parser.parse_args()
    run_quick(args.n)
