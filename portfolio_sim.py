"""
주간 추천 종목 가상 포트폴리오 시뮬레이션

- save_weekly_picks(results): 추천 시점 저장 (종목코드, 종목명, 매수가, 수량)
- send_portfolio_report(): 누적 수익률 계산 후 텔레그램 전송
"""
import json
import logging
import time
from datetime import datetime, timedelta
from math import floor
from pathlib import Path

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_HISTORY_FILE = Path(__file__).parent / "data" / "recommendation_history.json"
_INVEST_PER_STOCK = 1_000_000  # 100만원
_TELEGRAM_API = "https://api.telegram.org"
_TIMEOUT = 15


# ── 저장 ──────────────────────────────────────────────────────────────────────

def _load_history() -> list[dict]:
    if not _HISTORY_FILE.exists():
        return []
    try:
        return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"추천 이력 로드 실패: {e}")
        return []


def _save_history(history: list[dict]) -> None:
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_weekly_picks(results: list[dict]) -> None:
    """
    스크리너 결과(top_results)를 받아 추천 이력에 저장.
    results의 각 dict에는 ticker, name, df 키가 있어야 함.
    """
    today_str = datetime.today().strftime("%Y-%m-%d")
    history = _load_history()

    # 같은 날짜 중복 저장 방지
    if any(entry["week"] == today_str for entry in history):
        logger.info(f"오늘({today_str}) 추천 이력 이미 저장됨 — 덮어쓰기")
        history = [e for e in history if e["week"] != today_str]

    stocks = []
    for result in results:
        ticker = result["ticker"]
        name = result.get("name", ticker)
        df = result.get("df")
        if df is None or df.empty:
            logger.warning(f"{ticker} df 없음 — 스킵")
            continue

        buy_price = int(df["close"].iloc[-1])
        if buy_price <= 0:
            logger.warning(f"{ticker} 현재가 0 이하 — 스킵")
            continue

        shares = floor(_INVEST_PER_STOCK / buy_price)
        if shares == 0:
            shares = 1  # 고가 주식은 최소 1주

        stocks.append({
            "ticker": ticker,
            "name": name,
            "buy_price": buy_price,
            "shares": shares,
            "invested": buy_price * shares,
            "score": result.get("score", 0),
            "sub_score": result.get("sub_score", 0.0),
            "conditions": result.get("conditions", {}),
        })

    entry = {"week": today_str, "stocks": stocks}
    history.append(entry)
    _save_history(history)
    logger.info(f"추천 이력 저장 완료: {today_str}, {len(stocks)}개 종목")


# ── 현재가 조회 ───────────────────────────────────────────────────────────────

def _get_current_prices_batch(tickers: list[str]) -> dict[str, int]:
    """
    여러 종목 현재가를 단 2번의 API 호출로 일괄 조회.
    get_market_cap_by_ticker 응답에 종가(종가)가 포함됨.
    """
    from pykrx import stock
    prices: dict[str, int] = {}

    # 최근 유효 거래일 탐색 + KOSPI 데이터 즉시 활용
    trade_date = None
    for days_back in range(7):
        candidate = (datetime.today() - timedelta(days=days_back)).strftime("%Y%m%d")
        try:
            kospi_df = stock.get_market_cap_by_ticker(candidate, market="KOSPI")
            time.sleep(0.3)
            if kospi_df is not None and not kospi_df.empty:
                trade_date = candidate
                if "종가" in kospi_df.columns:
                    for t in tickers:
                        if t in kospi_df.index:
                            val = int(kospi_df.loc[t, "종가"])
                            if val > 0:
                                prices[t] = val
                break
        except Exception:
            pass

    if trade_date is None:
        logger.warning("거래일 탐색 실패 — 현재가 일괄 조회 불가")
        return prices

    # 코스닥 종목 추가
    try:
        kosdaq_df = stock.get_market_cap_by_ticker(trade_date, market="KOSDAQ")
        time.sleep(0.3)
        if kosdaq_df is not None and not kosdaq_df.empty and "종가" in kosdaq_df.columns:
            for t in tickers:
                if t in kosdaq_df.index and t not in prices:
                    val = int(kosdaq_df.loc[t, "종가"])
                    if val > 0:
                        prices[t] = val
    except Exception as e:
        logger.warning(f"코스닥 현재가 일괄 조회 실패: {e}")

    logger.info(f"현재가 일괄 조회: {len(prices)}/{len(tickers)}개 종목 성공")
    return prices


# ── 계산 ──────────────────────────────────────────────────────────────────────

def _calc_status() -> dict | None:
    """
    전체 추천 이력 기반 포트폴리오 현황 계산.
    반환: {
        weeks: int,              # 총 추천 회차 수
        total_invested: int,     # 총 투자금 (원)
        total_current: int,      # 현재 평가금 (원)
        overall_return_pct: float,
        this_week: dict | None,  # 이번 주 수익률 정보
        top3: list[dict],        # 누적 상위 3종목
        bottom3: list[dict],     # 누적 하위 3종목
    }
    """
    history = _load_history()
    if not history:
        logger.info("추천 이력 없음")
        return None

    # 전체 이력 고유 티커를 2번의 API 호출로 일괄 조회 (이력이 쌓여도 API 호출 수 고정)
    all_tickers = list({s["ticker"] for entry in history for s in entry["stocks"]})
    current_prices = _get_current_prices_batch(all_tickers)

    all_stocks: list[dict] = []  # {ticker, name, buy_price, shares, invested, current_price, return_pct}

    for entry in history:
        for s in entry["stocks"]:
            current = current_prices.get(s["ticker"]) or s["buy_price"]  # 조회 실패 시 손익 0
            return_pct = (current - s["buy_price"]) / s["buy_price"] * 100
            all_stocks.append({
                "ticker": s["ticker"],
                "name": s["name"],
                "buy_price": s["buy_price"],
                "shares": s["shares"],
                "invested": s["invested"],
                "current_price": current,
                "current_value": current * s["shares"],
                "return_pct": return_pct,
                "week": entry["week"],
            })

    if not all_stocks:
        return None

    total_invested = sum(s["invested"] for s in all_stocks)
    total_current = sum(s["current_value"] for s in all_stocks)
    overall_return_pct = (total_current - total_invested) / total_invested * 100

    # 이번 주 (최신 회차) 수익률
    latest_week = history[-1]["week"]
    this_week_stocks = [s for s in all_stocks if s["week"] == latest_week]
    this_week = None
    if this_week_stocks:
        tw_invested = sum(s["invested"] for s in this_week_stocks)
        tw_current = sum(s["current_value"] for s in this_week_stocks)
        this_week = {
            "week": latest_week,
            "return_pct": (tw_current - tw_invested) / tw_invested * 100,
        }

    # Top3 / Bottom3 (종목별 수익률 기준)
    sorted_stocks = sorted(all_stocks, key=lambda x: x["return_pct"], reverse=True)
    top3 = sorted_stocks[:3]
    bottom3 = sorted_stocks[-3:][::-1]  # 낮은 순 → 역순으로 worst부터

    return {
        "weeks": len(history),
        "total_invested": total_invested,
        "total_current": total_current,
        "overall_return_pct": overall_return_pct,
        "this_week": this_week,
        "top3": top3,
        "bottom3": bottom3,
    }


# ── 포맷 ──────────────────────────────────────────────────────────────────────

def _format_message(status: dict) -> str:
    weeks = status["weeks"]
    total_inv_man = status["total_invested"] / 10_000
    total_cur_man = status["total_current"] / 10_000
    overall = status["overall_return_pct"]
    sign = "+" if overall >= 0 else ""
    arrow = "📈" if overall >= 0 else "📉"

    lines = [
        "📊 <b>가상 포트폴리오 현황</b>",
        "─────────────────",
        f"💰 총 투자금: {total_inv_man:,.0f}만원 ({weeks}주 × 최대 1,000만원)",
        f"{arrow} 현재 평가금: {total_cur_man:,.0f}만원",
        f"🎯 누적 수익률: <b>{sign}{overall:.1f}%</b>",
    ]

    if status["this_week"]:
        tw = status["this_week"]
        tw_sign = "+" if tw["return_pct"] >= 0 else ""
        lines += [
            "",
            f"📅 이번 주({tw['week']}) 수익률: {tw_sign}{tw['return_pct']:.1f}%",
        ]

    if status["top3"]:
        lines += ["", "🏆 <b>베스트 종목</b>"]
        for i, s in enumerate(status["top3"], 1):
            ret_sign = "+" if s["return_pct"] >= 0 else ""
            lines.append(f"  {i}. {s['name']} {ret_sign}{s['return_pct']:.1f}%")

    if status["bottom3"]:
        lines += ["", "💀 <b>워스트 종목</b>"]
        for i, s in enumerate(status["bottom3"], 1):
            ret_sign = "+" if s["return_pct"] >= 0 else ""
            lines.append(f"  {i}. {s['name']} {ret_sign}{s['return_pct']:.1f}%")

    lines += ["", "⚠️ 실제 투자와 무관한 시뮬레이션입니다."]
    return "\n".join(lines)


# ── 전송 ──────────────────────────────────────────────────────────────────────

def _send_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("텔레그램 토큰 또는 채팅 ID 미설정")
        return False
    url = f"{_TELEGRAM_API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=_TIMEOUT)
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.warning(f"포트폴리오 메시지 전송 실패 ({attempt+1}/3): {e}")
    return False


def send_portfolio_report() -> None:
    """추천 이력 기반 포트폴리오 현황을 텔레그램으로 전송."""
    logger.info("포트폴리오 현황 계산 중...")
    status = _calc_status()
    if status is None:
        logger.info("포트폴리오 데이터 없음 — 전송 생략")
        return
    msg = _format_message(status)
    if _send_message(msg):
        logger.info("포트폴리오 현황 전송 완료")
    else:
        logger.error("포트폴리오 현황 전송 실패")
