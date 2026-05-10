"""
주간 종목 스크리너 메인 실행 파일

실행 방법:
  스케줄 모드 (매주 월요일 08:00): python main.py
  즉시 1회 실행 (GitHub Actions 등): python main.py --now

주의: pykrx가 os.environ에서 KRX_ID/KRX_PW를 읽기 때문에
      반드시 config(load_dotenv)를 먼저 import해야 합니다.
"""
import argparse
import logging
import sys
import time
import threading
import traceback
import concurrent.futures
from datetime import datetime

# ★ pykrx 임포트 전에 반드시 .env 로딩 (KRX_ID/KRX_PW 선행 설정)
import config  # noqa: E402 — load_dotenv() 호출됨

from apscheduler.schedulers.blocking import BlockingScheduler

from data.krx_fetcher import get_all_tickers_fast, get_ohlcv, get_investor_trend
from data.dart_fetcher import get_corp_code, get_recent_financials, preload_corp_code_map, preload_financials_cache
from filters.technical import check_ma_uptrend
from scorer import score_stock
from telegram_bot import send_screener_result, send_error_alert

# --- 로깅 설정 ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ],
)
# Windows 콘솔 인코딩을 UTF-8로 강제 설정
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
logger = logging.getLogger(__name__)


def _validate_credentials() -> bool:
    """
    필수 자격증명 설정 여부 확인.
    미설정 항목 목록을 로그로 출력하고, 모두 설정된 경우 True 반환.
    """
    missing = []
    placeholders = {"여기에", "_입력", "_입력"}

    def _is_missing(val: str, name: str) -> bool:
        if not val:
            return True
        for p in placeholders:
            if p in val:
                return True
        return False

    checks = {
        "KRX_ID": config.KRX_ID,
        "KRX_PW": config.KRX_PW,
        "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": config.TELEGRAM_CHAT_ID,
    }
    for name, val in checks.items():
        if _is_missing(val, name):
            missing.append(name)

    if missing:
        logger.warning(f"미설정 환경변수: {', '.join(missing)}")
        logger.warning(".env 파일에 실제 값을 입력하세요.")
        if "KRX_ID" in missing or "KRX_PW" in missing:
            logger.warning(
                "KRX 자격증명 발급: https://data.krx.co.kr 에서 무료 회원가입 후 이용"
            )
        return False
    return True


def _get_stock_name(ticker: str) -> str:
    """pykrx로 종목명 조회. 실패 시 티커 반환."""
    try:
        from pykrx import stock
        return stock.get_market_ticker_name(ticker)
    except Exception:
        return ticker


def _process_ticker(ticker: str) -> dict | None:
    """단일 종목 처리 (워커 스레드에서 실행). MA 1차 필터 실패 시 즉시 None 반환."""
    try:
        df = get_ohlcv(ticker, period=300)
        if df is None:
            return None

        if not check_ma_uptrend(df):
            return None  # DART/수급 API 호출 생략

        corp_code = get_corp_code(ticker)
        financials = None
        if corp_code:
            financials = get_recent_financials(corp_code)

        investor_trend = get_investor_trend(ticker, period=config.INVESTOR_TREND_PERIOD)

        score, conditions, sub_score = score_stock(
            ticker, df, financials, investor_trend,
            pre_conditions={"ma_uptrend": True},
        )

        if score >= config.MIN_SCORE:
            name = _get_stock_name(ticker)
            logger.info(f"통과: {ticker} ({name}) - {score}점 (sub={sub_score:.4f})")
            return {
                "ticker": ticker,
                "name": name,
                "score": score,
                "sub_score": sub_score,
                "conditions": conditions,
                "df": df,
                "financials": financials,
            }
        return None

    except Exception as e:
        logger.warning(f"{ticker} 처리 중 오류 (스킵): {e}")
        return None


def run_screener(n_workers: int = 10) -> None:
    """스크리너 메인 로직 1회 실행."""
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info(f"스크리너 실행 시작: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    results: list[dict] = []

    try:
        # 1. corp_code 맵 + 재무 캐시 메인 스레드에서 선제 로드
        logger.info("DART corp_code 맵 사전 로드 중...")
        preload_corp_code_map()
        logger.info("재무 데이터 캐시 로드 중...")
        preload_financials_cache()

        # 2. 전체 티커 수집
        logger.info("전체 티커 수집 중...")
        tickers = get_all_tickers_fast()
        total = len(tickers)
        logger.info(f"분석 대상 종목: {total}개 | 워커: {n_workers}개")

        completed = 0
        lock = threading.Lock()
        loop_start = time.monotonic()

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as executor:
            future_to_ticker = {executor.submit(_process_ticker, t): t for t in tickers}

            for future in concurrent.futures.as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    result = future.result()
                except Exception as e:
                    logger.warning(f"{ticker} future 오류 (스킵): {e}")
                    result = None

                with lock:
                    completed += 1
                    if result is not None:
                        results.append(result)

                    if completed % 50 == 0:
                        elapsed_s = time.monotonic() - loop_start
                        speed = completed / elapsed_s if elapsed_s > 0 else 0
                        remaining = (total - completed) / speed if speed > 0 else 0
                        logger.info(
                            f"진행: {completed}/{total} | "
                            f"속도: {speed:.1f}종목/초 | "
                            f"잔여: {remaining:.0f}초"
                        )

        # 3. 점수 내림차순 정렬 → 동점 시 sub_score(강도) 내림차순
        results.sort(key=lambda x: (x["score"], x["sub_score"]), reverse=True)

        # 4. 상위 TOP_N개 텔레그램 전송
        top_results = results[: config.TOP_N]

        if top_results:
            logger.info(f"텔레그램 전송: 상위 {len(top_results)}개 종목")
            send_screener_result(top_results)
            from portfolio_sim import save_weekly_picks, send_portfolio_report
            save_weekly_picks(top_results)
            send_portfolio_report()

        # 5. 정적 대시보드 생성 (실패해도 스크리너 결과에 영향 없음)
        try:
            from generate_dashboard import generate_dashboard
            generate_dashboard()
        except Exception as dash_err:
            logger.warning(f"대시보드 생성 실패 (비치명적): {dash_err}")

        if not top_results:
            logger.info("MIN_SCORE 이상 종목 없음 — 텔레그램 전송 생략")

        # 5. 실행 로그 저장
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"스크리너 완료: {len(results)}개 종목 발견, 소요 시간: {elapsed:.0f}초")

    except Exception as e:
        error_detail = traceback.format_exc()
        logger.error(f"스크리너 실행 중 치명적 오류:\n{error_detail}")
        send_error_alert(f"스크리너 실행 실패\n원인: {str(e)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="주간 종목 스크리너")
    parser.add_argument(
        "--now",
        action="store_true",
        help="스케줄 없이 즉시 1회 실행 (GitHub Actions 등 CI 환경용)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        metavar="N",
        help="병렬 처리 워커 수 (기본값: 10)",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Walk-Forward 백테스트 실행 (기술 지표만, 기본 52주)",
    )
    parser.add_argument(
        "--bt-weeks",
        type=int,
        default=config.BT_WEEKS,
        metavar="W",
        help=f"백테스트 기간 (주, 기본값: {config.BT_WEEKS})",
    )
    parser.add_argument(
        "--bt-top-n",
        type=int,
        default=config.BT_TOP_N,
        metavar="N",
        help=f"백테스트 포트폴리오 종목 수 (기본값: {config.BT_TOP_N})",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="OHLCV 캐시 강제 재다운로드",
    )
    args = parser.parse_args()

    # 자격증명 확인 (경고만, 중단하지 않음 — 일부 기능은 크리덴셜 없이도 동작)
    _validate_credentials()

    if args.backtest:
        logger.info(f"백테스트 모드 (--backtest) | {args.bt_weeks}주 | Top{args.bt_top_n}")
        from backtest import run_backtest
        run_backtest(
            weeks=args.bt_weeks,
            top_n=args.bt_top_n,
            refresh_cache=args.refresh_cache,
        )
        try:
            from generate_dashboard import generate_dashboard
            generate_dashboard()
        except Exception as dash_err:
            logger.warning(f"대시보드 생성 실패 (비치명적): {dash_err}")
    elif args.now:
        # 즉시 1회 실행 모드
        logger.info("즉시 실행 모드 (--now)")
        run_screener(n_workers=args.workers)
    else:
        # APScheduler 스케줄 모드: 매주 월요일 오전 8시
        scheduler = BlockingScheduler(timezone="Asia/Seoul")
        scheduler.add_job(
            lambda: run_screener(n_workers=args.workers),
            trigger="cron",
            day_of_week=config.SCHEDULE_DAY_OF_WEEK,
            hour=config.SCHEDULE_HOUR,
            minute=config.SCHEDULE_MINUTE,
            id="weekly_screener",
        )
        logger.info(
            f"스케줄러 시작: 매주 {config.SCHEDULE_DAY_OF_WEEK.upper()} "
            f"{config.SCHEDULE_HOUR:02d}:{config.SCHEDULE_MINUTE:02d} KST 실행"
        )
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("스케줄러 종료")


if __name__ == "__main__":
    main()
