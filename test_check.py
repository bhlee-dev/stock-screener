from dotenv import load_dotenv
import os
import requests

load_dotenv()

# 1. 환경변수 확인
print("=== 환경변수 확인 ===")
print("KRX_ID:", os.getenv("KRX_ID"))
print("KRX_PW:", os.getenv("KRX_PW"))
print("TELEGRAM_TOKEN:", os.getenv("TELEGRAM_BOT_TOKEN"))
print("TELEGRAM_CHAT_ID:", os.getenv("TELEGRAM_CHAT_ID"))
print("DART_API_KEY:", os.getenv("DART_API_KEY"))

# 2. pykrx 데이터 수신 확인
print("\n=== pykrx 데이터 확인 ===")
try:
    from pykrx import stock
    df = stock.get_market_cap_by_ticker("20260421", market="KOSPI")
    if df is not None and len(df) > 0:
        print("pykrx 정상 - 종목 수:", len(df))
    else:
        print("pykrx 데이터 없음")
except Exception as e:
    print("pykrx 오류:", e)

# 3. 텔레그램 봇 확인
print("\n=== 텔레그램 확인 ===")
token = os.getenv("TELEGRAM_BOT_TOKEN")
try:
    r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
    data = r.json()
    if data.get("ok"):
        print("텔레그램 정상 - 봇 이름:", data["result"]["username"])
    else:
        print("텔레그램 오류:", data)
except Exception as e:
    print("텔레그램 연결 실패:", e)

# 4. DART API 확인
print("\n=== DART API 확인 ===")
dart_key = os.getenv("DART_API_KEY")
try:
    r = requests.get(
        f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={dart_key}",
        timeout=5
    )
    if r.status_code == 200 and len(r.content) > 1000:
        print("DART API 정상")
    else:
        print("DART API 오류 - 상태코드:", r.status_code)
except Exception as e:
    print("DART 연결 실패:", e)