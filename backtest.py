"""
Walk-Forward 백테스트 엔진

매주 월요일 기준으로 기술 지표만 사용해 종목을 선정하고,
Top-N 탈락 시 매도하는 전략의 과거 수익률을 검증한다.

실행: python main.py --backtest [--bt-weeks 52] [--bt-top-n 10] [--refresh-cache]
"""
import json
import logging
import time
from datetime import date, datetime, timedelta
from math import floor
from pathlib import Path

import numpy as np
import pandas as pd
from pykrx import stock as pykrx_stock

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent / "data" / "ohlcv_cache"
_CACHE_FILE = _CACHE_DIR / "ohlcv_cache.csv.gz"
_META_FILE = _CACHE_DIR / "cache_meta.json"
_CHECKPOINT_FILE = _CACHE_DIR / "checkpoint.json"
_RESULTS_FILE = Path(__file__).parent / "data" / "backtest_results.json"

_INITIAL_CAPITAL = 10_000_000   # 1,000만원
_TRADE_COST_BUY = 0.00015       # 매수 수수료
_TRADE_COST_SELL = 0.00215      # 매도 수수료 0.015% + 증권거래세 0.2%
_RF_ANNUAL = 0.035              # 무위험이자율 연 3.5%


def _date_str(d: date) -> str:
    return d.strftime("%Y%m%d")


# ── 거래일 유틸 ───────────────────────────────────────────────────────────────

def _get_eval_dates(weeks: int) -> list[date]:
    """
    백테스트 평가 날짜 목록 반환.
    최근 weeks주의 월요일 기준, 비거래일이면 다음 거래일로 보정.
    """
    today = date.today()
    start_cal = today - timedelta(weeks=weeks + 2)

    trading_days: set[date] = set()
    try:
        df_idx = pykrx_stock.get_index_ohlcv_by_date(
            _date_str(start_cal), _date_str(today), "1001"
        )
        time.sleep(0.3)
        if df_idx is not None and not df_idx.empty:
            trading_days = {pd.Timestamp(d).date() for d in df_idx.index}
    except Exception as e:
        logger.warning(f"거래일 조회 실패, 달력 월요일 사용: {e}")

    # 시작 월요일 탐색
    current = today - timedelta(weeks=weeks)
    while current.weekday() != 0:
        current += timedelta(days=1)

    result: list[date] = []
    while current <= today:
        if trading_days:
            adjusted = current
            for delta in range(5):
                candidate = current + timedelta(days=delta)
                if candidate in trading_days:
                    adjusted = candidate
                    break
            result.append(adjusted)
        else:
            result.append(current)
        current += timedelta(weeks=1)

    return sorted(set(result))


# ── OHLCV 캐시 ────────────────────────────────────────────────────────────────

def _cache_valid(weeks: int) -> bool:
    """캐시가 유효(최신이고 범위 충분)한지 확인."""
    from config import BT_CACHE_REFRESH_DAYS
    if not _META_FILE.exists() or not _CACHE_FILE.exists():
        return False
    try:
        meta = json.loads(_META_FILE.read_text())
        created = date.fromisoformat(meta["created_at"])
        if (date.today() - created).days > BT_CACHE_REFRESH_DAYS:
            return False
        needed_start = date.today() - timedelta(days=weeks * 7 + 500)
        cached_start = datetime.strptime(meta["start_date"], "%Y%m%d").date()
        return cached_start <= needed_start
    except Exception:
        return False


def _load_cache() -> pd.DataFrame:
    """캐시 CSV.gz → MultiIndex(ticker, date) DataFrame."""
    logger.info("OHLCV 캐시 로드 중...")
    df = pd.read_csv(
        _CACHE_FILE,
        compression="gzip",
        parse_dates=["date"],
        dtype={
            "ticker": str,
            "open": "int32",
            "high": "int32",
            "low": "int32",
            "close": "int32",
            "volume": "int64",
        },
    )
    df = df.set_index(["ticker", "date"]).sort_index()
    logger.info(
        f"캐시 로드 완료: {len(df):,}행 / {df.index.get_level_values('ticker').nunique()}종목"
    )
    return df


def _write_checkpoint(frames: list[pd.DataFrame], idx: int) -> None:
    if not frames:
        return
    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(_CACHE_FILE, index=False, compression="gzip")
    _CHECKPOINT_FILE.write_text(json.dumps({
        "last_idx": idx,
        "timestamp": datetime.now().isoformat(),
    }))


def _build_cache(tickers: list[str], start_date: date, end_date: date) -> pd.DataFrame:
    """
    전 종목 OHLCV 캐시 빌드.
    20종목마다 체크포인트 저장 → 중단 시 재개 가능.
    """
    from data.krx_fetcher import _throttle

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    start_str = _date_str(start_date)
    end_str = _date_str(end_date)

    # 체크포인트 복구
    start_idx = 0
    accumulated: list[pd.DataFrame] = []

    if _CHECKPOINT_FILE.exists() and _CACHE_FILE.exists():
        try:
            cp = json.loads(_CHECKPOINT_FILE.read_text())
            start_idx = cp.get("last_idx", 0)
            existing = pd.read_csv(
                _CACHE_FILE,
                compression="gzip",
                parse_dates=["date"],
                dtype={
                    "ticker": str, "open": "int32", "high": "int32",
                    "low": "int32", "close": "int32", "volume": "int64",
                },
            )
            accumulated.append(existing)
            logger.info(f"체크포인트 복구: {start_idx}/{len(tickers)}번째부터 재개")
        except Exception:
            start_idx = 0
            accumulated = []

    total = len(tickers)
    logger.info(f"OHLCV 캐시 빌드: {total}종목, {start_date} ~ {end_date}")

    for idx in range(start_idx, total):
        ticker = tickers[idx]
        try:
            df = pykrx_stock.get_market_ohlcv_by_date(start_str, end_str, ticker)
            _throttle()

            if df is None or df.empty:
                continue

            df = df.rename(columns={
                "시가": "open", "고가": "high", "저가": "low",
                "종가": "close", "거래량": "volume",
            })
            df.index.name = "date"
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.insert(0, "ticker", ticker)
            df = df.reset_index()

            for col in ["open", "high", "low", "close"]:
                df[col] = df[col].astype("int32")
            df["volume"] = df["volume"].astype("int64")

            accumulated.append(df)

        except Exception as e:
            logger.warning(f"{ticker} 다운로드 실패 (스킵): {e}")

        if (idx + 1) % 20 == 0:
            _write_checkpoint(accumulated, idx + 1)
            pct = (idx + 1) / total * 100
            logger.info(f"  캐시 진행: {idx+1}/{total} ({pct:.1f}%)")

    if not accumulated:
        logger.error("다운로드된 데이터 없음")
        return pd.DataFrame()

    final_df = pd.concat(accumulated, ignore_index=True)
    final_df.to_csv(_CACHE_FILE, index=False, compression="gzip")
    _META_FILE.write_text(json.dumps({
        "created_at": str(date.today()),
        "start_date": start_str,
        "end_date": end_str,
        "ticker_count": int(final_df["ticker"].nunique()),
        "row_count": len(final_df),
    }))
    if _CHECKPOINT_FILE.exists():
        _CHECKPOINT_FILE.unlink()

    logger.info(f"캐시 빌드 완료: {len(final_df):,}행 / {final_df['ticker'].nunique()}종목")
    return final_df.set_index(["ticker", "date"]).sort_index()


# ── 데이터 조회 유틸 ──────────────────────────────────────────────────────────

def _get_ticker_slice(
    cache_df: pd.DataFrame, ticker: str, as_of: date, periods: int = 300,
) -> pd.DataFrame | None:
    """as_of 날짜 기준 ticker의 최근 periods 거래일 데이터 반환."""
    try:
        ticker_df = cache_df.loc[ticker]
    except KeyError:
        return None
    filtered = ticker_df[ticker_df.index <= pd.Timestamp(as_of)]
    if filtered.empty:
        return None
    return filtered.tail(periods)


def _get_close(cache_df: pd.DataFrame, ticker: str, as_of: date) -> int | None:
    """as_of 날짜 기준 종가 반환 (가장 가까운 이전 거래일)."""
    try:
        ticker_df = cache_df.loc[ticker]
    except KeyError:
        return None
    filtered = ticker_df[ticker_df.index <= pd.Timestamp(as_of)]
    if filtered.empty:
        return None
    return int(filtered["close"].iloc[-1])


def _get_stock_name(ticker: str) -> str:
    try:
        return pykrx_stock.get_market_ticker_name(ticker)
    except Exception:
        return ticker


def _get_kospi_weekly(eval_dates: list[date]) -> dict[str, float]:
    """
    평가 날짜별 KOSPI 누적 수익률(%) 반환.
    첫 평가일의 지수를 기준(0%)으로 계산.
    """
    if not eval_dates:
        return {}
    try:
        df = pykrx_stock.get_index_ohlcv_by_date(
            _date_str(eval_dates[0]), _date_str(eval_dates[-1]), "1001"
        )
        time.sleep(0.3)
        if df is None or df.empty:
            return {}
        df.index = pd.to_datetime(df.index)
        base = float(df["종가"].iloc[0])
        result = {}
        for d in eval_dates:
            ts = pd.Timestamp(d)
            available = df[df.index <= ts]
            if not available.empty:
                val = float(available["종가"].iloc[-1])
                result[str(d)] = round((val - base) / base * 100, 2)
            else:
                result[str(d)] = 0.0
        return result
    except Exception as e:
        logger.warning(f"KOSPI 시계열 조회 실패: {e}")
        return {}


# ── Walk-Forward 시뮬레이션 ───────────────────────────────────────────────────

def _run_walk_forward(
    eval_dates: list[date],
    cache_df: pd.DataFrame,
    tickers: list[str],
    top_n: int,
    min_score: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Walk-Forward 포트폴리오 시뮬레이션.

    매주 eval_date 기준:
    - 기술 지표만으로 전 종목 채점
    - Top-N에 새로 진입하면 매수 (eval_date 종가)
    - Top-N에서 탈락하면 매도 (eval_date 종가)

    반환: (trades, weekly_values, weekly_holdings)
    """
    from scorer import score_stock
    from config import MA_LONG

    cash = float(_INITIAL_CAPITAL)
    portfolio: dict[str, dict] = {}
    trades: list[dict] = []
    weekly_values: list[dict] = []
    weekly_holdings: list[dict] = []
    name_cache: dict[str, str] = {}

    total_weeks = len(eval_dates)

    for w_idx, eval_date in enumerate(eval_dates):
        logger.info(f"  [{w_idx+1:>2}/{total_weeks}] {eval_date} 평가 중...")

        # 1. 전 종목 기술 채점
        weekly_scores: dict[str, tuple] = {}
        for ticker in tickers:
            try:
                df_slice = _get_ticker_slice(cache_df, ticker, eval_date)
                if df_slice is None or len(df_slice) < MA_LONG:
                    continue
                score, conds, sub = score_stock(
                    ticker, df_slice,
                    financials=None,
                    investor_trend=None,
                    pre_conditions=None,
                )
                if score >= min_score:
                    weekly_scores[ticker] = (score, sub, conds)
            except Exception:
                continue

        # 2. 정렬 → Top N
        ranked = sorted(
            weekly_scores,
            key=lambda t: (weekly_scores[t][0], weekly_scores[t][1]),
            reverse=True,
        )
        new_top_n: set[str] = set(ranked[:top_n])
        current_held: set[str] = set(portfolio.keys())
        to_sell = current_held - new_top_n
        to_buy = new_top_n - current_held

        # 3. 매도 (탈락 종목)
        for ticker in to_sell:
            sell_price = _get_close(cache_df, ticker, eval_date) or portfolio[ticker]["buy_price"]
            pos = portfolio.pop(ticker)
            shares = pos["shares"]
            buy_price = pos["buy_price"]

            pnl_pct = (
                sell_price * (1 - _TRADE_COST_SELL)
                - buy_price * (1 + _TRADE_COST_BUY)
            ) / (buy_price * (1 + _TRADE_COST_BUY)) * 100

            holding_days = (eval_date - pos["buy_date"]).days

            if ticker not in name_cache:
                name_cache[ticker] = _get_stock_name(ticker)

            trades.append({
                "ticker": ticker,
                "name": name_cache[ticker],
                "buy_date": str(pos["buy_date"]),
                "sell_date": str(eval_date),
                "buy_price": buy_price,
                "sell_price": sell_price,
                "shares": shares,
                "pnl_pct": round(pnl_pct, 2),
                "holding_weeks": round(holding_days / 7, 1),
                "buy_score": pos["buy_score"],
                "exit_reason": "dropped_from_top10",
            })
            cash += sell_price * shares * (1 - _TRADE_COST_SELL)

        # 4. 매수 (신규 진입)
        if to_buy and cash > 0:
            per_stock = min(_INITIAL_CAPITAL // top_n, cash / len(to_buy))
            for ticker in sorted(to_buy):
                buy_price = _get_close(cache_df, ticker, eval_date)
                if not buy_price:
                    continue

                invest = min(per_stock, cash)
                shares = floor(invest / (buy_price * (1 + _TRADE_COST_BUY)))
                if shares < 1:
                    continue

                cost = buy_price * shares * (1 + _TRADE_COST_BUY)
                if cost > cash:
                    continue

                if ticker not in name_cache:
                    name_cache[ticker] = _get_stock_name(ticker)

                portfolio[ticker] = {
                    "buy_price": buy_price,
                    "shares": shares,
                    "buy_date": eval_date,
                    "buy_score": weekly_scores[ticker][0],
                    "buy_conditions": {k: bool(v) for k, v in weekly_scores[ticker][2].items()},
                }
                cash -= cost

        # 5. 주간 포트폴리오 가치 기록
        port_val = cash
        for ticker, pos in portfolio.items():
            cur = _get_close(cache_df, ticker, eval_date) or pos["buy_price"]
            port_val += cur * pos["shares"]

        weekly_values.append({
            "date": str(eval_date),
            "portfolio_value": round(port_val),
            "holdings_count": len(portfolio),
        })
        weekly_holdings.append({
            "date": str(eval_date),
            "holdings": [
                {
                    "ticker": t,
                    "name": name_cache.get(t, t),
                    "score": p["buy_score"],
                    "conditions": p.get("buy_conditions", {}),
                }
                for t, p in portfolio.items()
            ],
        })

    # 6. 종료 시 미청산 포지션 강제 청산 (마지막 eval_date 종가)
    if eval_dates and portfolio:
        last_date = eval_dates[-1]
        for ticker in list(portfolio.keys()):
            pos = portfolio.pop(ticker)
            sell_price = _get_close(cache_df, ticker, last_date) or pos["buy_price"]
            shares = pos["shares"]
            buy_price = pos["buy_price"]

            pnl_pct = (
                sell_price * (1 - _TRADE_COST_SELL)
                - buy_price * (1 + _TRADE_COST_BUY)
            ) / (buy_price * (1 + _TRADE_COST_BUY)) * 100

            holding_days = (last_date - pos["buy_date"]).days

            if ticker not in name_cache:
                name_cache[ticker] = _get_stock_name(ticker)

            trades.append({
                "ticker": ticker,
                "name": name_cache[ticker],
                "buy_date": str(pos["buy_date"]),
                "sell_date": str(last_date),
                "buy_price": buy_price,
                "sell_price": sell_price,
                "shares": shares,
                "pnl_pct": round(pnl_pct, 2),
                "holding_weeks": round(holding_days / 7, 1),
                "buy_score": pos["buy_score"],
                "exit_reason": "end_of_backtest",
            })

    return trades, weekly_values, weekly_holdings


# ── 성과 지표 계산 ────────────────────────────────────────────────────────────

def _calc_metrics(
    trades: list[dict],
    weekly_values: list[dict],
    kospi_weekly: dict[str, float],
) -> dict:
    """총 수익률, 승률, MDD, 샤프 비율, KOSPI 대비 알파 계산."""
    if not weekly_values:
        return {}

    values = pd.Series([w["portfolio_value"] for w in weekly_values], dtype=float)

    total_ret = (values.iloc[-1] - _INITIAL_CAPITAL) / _INITIAL_CAPITAL * 100

    rolling_max = values.cummax()
    drawdowns = (values - rolling_max) / rolling_max * 100
    mdd = float(drawdowns.min())

    weekly_ret = values.pct_change().dropna()
    rf_weekly = _RF_ANNUAL / 52
    excess = weekly_ret - rf_weekly
    sharpe = (
        float(excess.mean() / excess.std() * np.sqrt(52))
        if len(excess) > 1 and excess.std() > 0
        else 0.0
    )

    closed = [t for t in trades if "pnl_pct" in t]
    win_trades = [t for t in closed if t["pnl_pct"] > 0]
    win_rate = len(win_trades) / len(closed) * 100 if closed else 0.0
    avg_trade_ret = sum(t["pnl_pct"] for t in closed) / len(closed) if closed else 0.0
    avg_hold = sum(t["holding_weeks"] for t in closed) / len(closed) if closed else 0.0

    kospi_ret = list(kospi_weekly.values())[-1] if kospi_weekly else 0.0
    alpha = total_ret - kospi_ret

    return {
        "total_return_pct": round(total_ret, 2),
        "kospi_return_pct": round(kospi_ret, 2),
        "alpha_pct": round(alpha, 2),
        "mdd_pct": round(mdd, 2),
        "win_rate_pct": round(win_rate, 1),
        "avg_trade_return_pct": round(avg_trade_ret, 2),
        "sharpe_ratio": round(sharpe, 3),
        "total_trades": len(closed),
        "avg_holding_weeks": round(avg_hold, 1),
        "initial_capital": _INITIAL_CAPITAL,
        "final_value": int(values.iloc[-1]),
    }


# ── 결과 저장 ─────────────────────────────────────────────────────────────────

def _save_results(
    meta: dict,
    performance: dict,
    trades: list[dict],
    weekly_values: list[dict],
    weekly_holdings: list[dict],
    kospi_weekly: dict[str, float],
) -> None:
    for w in weekly_values:
        w["portfolio_return_pct"] = round(
            (w["portfolio_value"] - _INITIAL_CAPITAL) / _INITIAL_CAPITAL * 100, 2
        )
        w["kospi_return_pct"] = kospi_weekly.get(w["date"], 0.0)

    results = {
        "meta": meta,
        "performance": performance,
        "weekly_series": weekly_values,
        "trades": trades,
        "weekly_holdings": weekly_holdings,
    }
    _RESULTS_FILE.parent.mkdir(exist_ok=True)
    _RESULTS_FILE.write_text(
        json.dumps(results, ensure_ascii=False, default=str, indent=2)
    )
    logger.info(f"백테스트 결과 저장: {_RESULTS_FILE}")


# ── 진입점 ────────────────────────────────────────────────────────────────────

def run_backtest(weeks: int = 52, top_n: int = 10, refresh_cache: bool = False) -> None:
    """
    Walk-Forward 백테스트 실행.
    기술 지표만 사용 (DART 재무·수급 제외 → look-ahead bias 없음).
    보유 전략: Top-N 탈락 시 매도 (다음 스크리닝 주에 자동 처리).
    """
    from config import BT_MIN_SCORE_TECH
    from data.krx_fetcher import get_all_tickers_fast

    logger.info("=" * 60)
    logger.info(f"Walk-Forward 백테스트 시작: {weeks}주 / Top{top_n} / 기술 지표만")
    logger.info("=" * 60)
    start_time = datetime.now()

    # 평가 날짜 생성
    logger.info("평가 날짜 생성 중...")
    eval_dates = _get_eval_dates(weeks)
    if not eval_dates:
        logger.error("평가 날짜 생성 실패")
        return
    logger.info(f"평가 날짜: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}주)")

    # OHLCV 캐시: 첫 평가일 - 300 거래일 버퍼(약 500일)부터 오늘까지
    today = date.today()
    cache_start = eval_dates[0] - timedelta(days=500)
    cache_end = today

    if refresh_cache or not _cache_valid(weeks):
        logger.info("OHLCV 캐시 빌드 중... (최초 실행 시 10~20분 소요, 중단 후 재실행 시 자동 재개)")
        tickers = get_all_tickers_fast()
        cache_df = _build_cache(tickers, cache_start, cache_end)
    else:
        logger.info("기존 OHLCV 캐시 사용")
        cache_df = _load_cache()
        tickers = list(cache_df.index.get_level_values("ticker").unique())

    if cache_df.empty:
        logger.error("캐시 데이터 없음 — 백테스트 중단")
        return

    logger.info(f"유니버스: {len(tickers)}종목")

    # KOSPI 벤치마크 주간 수익률
    logger.info("KOSPI 벤치마크 조회 중...")
    kospi_weekly = _get_kospi_weekly(eval_dates)

    # Walk-Forward 실행
    logger.info("Walk-Forward 시뮬레이션 실행 중...")
    trades, weekly_values, weekly_holdings = _run_walk_forward(
        eval_dates, cache_df, tickers, top_n, BT_MIN_SCORE_TECH
    )

    # 성과 지표
    performance = _calc_metrics(trades, weekly_values, kospi_weekly)

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(
        f"백테스트 완료 | "
        f"총 수익률 {performance.get('total_return_pct', 0):+.1f}% "
        f"(KOSPI {performance.get('kospi_return_pct', 0):+.1f}%, "
        f"알파 {performance.get('alpha_pct', 0):+.1f}%) | "
        f"승률 {performance.get('win_rate_pct', 0):.1f}% | "
        f"{len(trades)}건 거래 | "
        f"소요 {elapsed:.0f}초"
    )

    meta = {
        "generated_at": str(date.today()),
        "backtest_period": {
            "start": str(eval_dates[0]),
            "end": str(eval_dates[-1]),
            "weeks": len(eval_dates),
        },
        "parameters": {
            "top_n": top_n,
            "min_score_tech": BT_MIN_SCORE_TECH,
            "invest_per_stock": _INITIAL_CAPITAL // top_n,
            "initial_capital": _INITIAL_CAPITAL,
            "scoring_mode": "tech_only",
            "trade_cost_buy_pct": round(_TRADE_COST_BUY * 100, 4),
            "trade_cost_sell_pct": round(_TRADE_COST_SELL * 100, 4),
        },
        "elapsed_seconds": round(elapsed),
        "caveats": [
            "생존자 편향: 현재 상장 종목만 대상 (상장폐지 종목 제외 → 성과 소폭 과대평가 가능)",
            "기술 지표만 사용 (DART 재무·수급 제외)",
            "월요일 종가를 매수/매도가로 사용 (실제 체결은 화요일 시가)",
        ],
    }

    _save_results(meta, performance, trades, weekly_values, weekly_holdings, kospi_weekly)
