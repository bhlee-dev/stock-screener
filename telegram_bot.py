"""
텔레그램 봇 메시지 전송 모듈
API 키는 로그/메시지에 절대 출력하지 않음
"""
import logging
from datetime import datetime

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from scorer import SCORE_TABLE, MAX_SCORE

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"
_TIMEOUT = 15

# 조건명 → 한국어 레이블 + 점수 표시
_CONDITION_LABELS = {
    "ma_uptrend":               ("200일선 우상향", 2),
    "base_formation":           ("베이스 형성",   1),
    "bollinger_contraction":    ("볼린저 수축",   2),
    "volume_breakout":          ("거래량 돌파 발산", 3),
    "eps_growth":               ("EPS 연속 증가", 2),
    "revenue_growth":           ("매출 증가",     1),
    "operating_profit_growth":  ("영업이익 증가", 1),
    "institution_net_buy":      ("기관 순매수 10일+", 2),
    "foreign_net_buy":          ("외국인 순매수 10일+", 2),
}


def _send_message(text: str) -> bool:
    """텔레그램 메시지 단건 전송. 성공 시 True."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("텔레그램 토큰 또는 채팅 ID 미설정 (환경변수 확인)")
        return False

    url = f"{_TELEGRAM_API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=_TIMEOUT)
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.warning(f"텔레그램 전송 실패 ({attempt+1}/3): {e}")

    logger.error("텔레그램 메시지 최종 전송 실패")
    return False


def _format_stock_message(rank: int, result: dict) -> str:
    """종목 1개에 대한 텔레그램 메시지 포맷 생성."""
    ticker = result["ticker"]
    name = result.get("name", ticker)
    score = result["score"]
    sub_score = result.get("sub_score", 0.0)
    conditions = result["conditions"]
    df = result["df"]

    # 현재가 및 52주 변동폭
    current_price = int(df["close"].iloc[-1])
    lookback_52w = min(252, len(df))
    high_52w = int(df["high"].iloc[-lookback_52w:].max())
    low_52w = int(df["low"].iloc[-lookback_52w:].min())

    # 최근 거래량 / 20일 평균 거래량 비율
    vol_avg = df["volume"].rolling(20).mean().iloc[-1]
    vol_ratio = df["volume"].iloc[-1] / vol_avg if vol_avg else 0

    # EPS 증가율 계산 (재무데이터 있을 경우)
    eps_growth_str = "N/A"
    financials = result.get("financials")
    if financials is not None and len(financials) >= 2:
        eps_now = financials["eps"].iloc[0]
        eps_prev = financials["eps"].iloc[1]
        if eps_prev and eps_prev != 0 and eps_now is not None and eps_prev is not None:
            rate = (eps_now - eps_prev) / abs(eps_prev) * 100
            eps_growth_str = f"{rate:+.1f}%"

    # 충족 조건 목록
    met_conditions = [
        f"- {label} ({pts}점)"
        for cond, (label, pts) in _CONDITION_LABELS.items()
        if conditions.get(cond)
    ]
    met_str = "\n".join(met_conditions) if met_conditions else "- 없음"

    msg = (
        f"🏆 {rank}위. <b>{name}</b> ({ticker})\n"
        f"총점: <b>{score}/{MAX_SCORE}점</b>  강도: {sub_score:.2f}\n"
        f"\n"
        f"✅ 충족 조건:\n{met_str}\n"
        f"\n"
        f"📈 주요 지표:\n"
        f"- 현재가: {current_price:,}원\n"
        f"- 52주 변동폭: {low_52w:,}~{high_52w:,}\n"
        f"- 최근 거래량/평균 거래량: {vol_ratio:.1f}배\n"
        f"- EPS 증가율: {eps_growth_str}\n"
        f"\n"
        f"⚠️ 본 내용은 투자 참고용이며,\n"
        f"최종 판단은 직접 하시기 바랍니다.\n"
        f"──────────────────"
    )
    return msg


def send_screener_result(results: list[dict]) -> None:
    """
    스크리너 결과 전체를 텔레그램으로 전송.
    results: score_stock() 결과를 포함한 dict 리스트 (점수 내림차순 정렬 완료)
    """
    today_str = datetime.today().strftime("%Y년 %m월 %d일")

    # 헤더 메시지
    header = (
        f"📊 <b>주간 종목 스크리너 결과</b>\n"
        f"📅 {today_str} 기준\n"
        f"총 {len(results)}개 종목 발견"
    )
    _send_message(header)

    for rank, result in enumerate(results, start=1):
        msg = _format_stock_message(rank, result)
        _send_message(msg)

    logger.info(f"텔레그램 전송 완료: {len(results)}개 종목")


def send_error_alert(error_msg: str) -> None:
    """
    오류 발생 시 텔레그램 알림 전송.
    API 키 등 민감 정보가 포함되지 않도록 주의.
    """
    # 혹시 토큰이 문자열에 포함됐을 경우 마스킹
    safe_msg = error_msg
    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN in safe_msg:
        safe_msg = safe_msg.replace(TELEGRAM_BOT_TOKEN, "***")

    text = f"🚨 스크리너 오류 발생\n\n{safe_msg}"
    _send_message(text)
