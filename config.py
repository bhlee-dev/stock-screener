"""
전역 설정 및 환경변수 로드
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- API 키 ---
DART_API_KEY = os.getenv("DART_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# KRX 데이터포털 자격증명 (pykrx가 os.environ에서 직접 읽음)
# load_dotenv() 이후 os.environ에 자동 반영되므로 별도 노출 없음
KRX_ID = os.getenv("KRX_ID", "")
KRX_PW = os.getenv("KRX_PW", "")

# --- 기술적 분석 파라미터 ---
MA_MEDIUM = 50          # 중기 이평 (50일, 강세 배열 검증용)
MA_SHORT = 150          # 단기 장기이평 (150일)
MA_LONG = 200           # 장기 이평 (200일)
BOLLINGER_PERIOD = 20   # 볼린저 밴드 기간
BOLLINGER_STD = 2       # 볼린저 밴드 표준편차 배수

# --- RSI 파라미터 ---
RSI_PERIOD = 14         # RSI 계산 기간
RSI_OVERBOUGHT = 75     # 과매수 기준 (이 이상이면 페널티)

# --- 거래량 파라미터 ---
VOLUME_SURGE_RATIO = 2.0    # 평균 대비 거래량 급증 기준 배수
VOLUME_AVG_PERIOD = 20      # 거래량 평균 계산 기간 (거래일)

# --- 스크리너 결과 파라미터 ---
MIN_SCORE = 7       # 텔레그램 전송 최소 점수 (조건 강화로 6→7)
TOP_N = 10          # 최대 추천 종목 수

# --- 기타 ---
MIN_MARKET_CAP = 50_000_000_000     # 시가총액 최소값 (500억)
BASE_FORMATION_DAYS = (60, 120)     # 베이스 형성 구간 (거래일)
BASE_FORMATION_THRESHOLD = 0.30     # 베이스 고저 변동폭 허용 비율 (30%)
BASE_FORMATION_MAX_DECLINE = 0.10   # 베이스 구간 내 최대 허용 하락률 (10%, 초반 vs 후반)
BOLLINGER_CONTRACTION_PERCENTILE = 25   # 볼린저 수축 판단 하위 퍼센타일
BOLLINGER_CONTRACTION_LOOKBACK = 60     # 볼린저 수축 비교 기간 (거래일)
MA_SLOPE_LOOKBACK = 20              # 이평선 기울기 판단 기간 (거래일)
VOLUME_BREAKOUT_LOOKBACK = 5        # 거래량 돌파 탐지 기간 (거래일)
HIGH52W_PROXIMITY = 0.05            # 52주 신고가 근접 기준 (5%)
INVESTOR_TREND_PERIOD = 20          # 기관/외국인 순매수 집계 기간 (거래일)
SMART_MONEY_MIN_DAYS = 10               # 스마트머니 판단 최소 순매수 일수
SMART_MONEY_MIN_NET_BUY = 5_000_000_000 # 스마트머니 최소 누적 순매수 금액 (50억)

# --- 재무 파라미터 ---
EPS_MIN_GROWTH_RATE = 0.10          # EPS 최소 성장률 (10%)

# --- 로그 파일 ---
LOG_FILE = "screener_log.txt"

# --- 스케줄 설정 ---
SCHEDULE_DAY_OF_WEEK = "mon"    # 매주 월요일
SCHEDULE_HOUR = 8               # 오전 8시
SCHEDULE_MINUTE = 0
