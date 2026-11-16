import os
import datetime
import pytz
import requests
from google import genai
from google.genai import types

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "9112315353").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MANUAL_SLOT = os.environ.get("MANUAL_SLOT", "").strip()


def detect_current_slot(now_kst: datetime.datetime) -> str:
    if MANUAL_SLOT:
        return MANUAL_SLOT
    weekday = now_kst.weekday()  # 0=월 ... 4=금, 5=토, 6=일
    hour = now_kst.hour
    is_weekend = weekday in (5, 6)

    if 5 <= hour <= 9:
        return "07_nba_nhl"
    if not is_weekend and 15 <= hour <= 18:
        return "17_domestic"
    if is_weekend and 11 <= hour <= 14:
        return "13_weekend_domestic"
    if not is_weekend and (hour <= 3 or hour == 23):
        return "01_overseas_soccer"
    if is_weekend and 18 <= hour <= 22:
        return "20_weekend_soccer"
    return "07_nba_nhl"


def build_prompt(slot: str, date_str: str) -> str:
    common_style_rules = (
        "반드시 눈이 피로한 빨간색(🔴, 🟥) 이모지나 원색 강조를 절대 사용하지 말고, "
        "눈이 편안한 스타일(🌿 주력 추천픽, 💧 언오버/서브 추천픽, ⭐ 고배당 밸류픽, 굵은 글씨 강조)로만 작성해 줘. "
        "단순 일반승 나열이 아니라 핸디캡(마핸/프핸 기준점)과 언오버(기준점)를 반드시 포함해 가장 밸류 높은 라인과 최적 2~3폴더 조합을 제시해 줘."
    )

    if slot == "07_nba_nhl":
        return (
            f"[{date_str} 아침 07:00 NBA · NHL 고퀄리티 예약분석]\n"
            f"오늘 실제로 열리는 NBA 및 NHL 전 경기의 공식 일정, 홈/원정 대진, 부상자·결장 변수, 백투백 체력 지표를 검색해 검증한 뒤, "
            f"피나클(Pinnacle) 로우비그 배당 흐름과 국내 베트맨 프로토 환산 배당을 비교하여 고퀄리티 분석 리포트를 현덕 형님께 존댓말로 작성해 줘. "
            f"{common_style_rules}"
        )
    elif slot in ("17_domestic", "13_weekend_domestic"):
        time_label = "평일 낮 17:00" if slot == "17_domestic" else "주말 낮 13:00"
        return (
            f"[{date_str} {time_label} 국내경기 & 아시아축구 예약분석]\n"
            f"오늘 실제로 열리는 국내 프로스포츠(KBL, WKBL, V-리그, KBO 등) 전 경기에 더해, "
            f"당일 일본축구(J리그/컵대회), 국내축구(K리그/코리아컵), AFC 챔피언스리그(ACL) 경기가 있을 경우 모두 포함하여 분석해 줘. "
            f"배당과 기준점은 반드시 국내 공식 '베트맨(Betman) 프로토 승부식' 배당과 기준점을 최우선 기준으로 삼고, "
            f"해외 피나클 배당 흐름과 비교해 베트맨에서 밸류가 높은 구간을 짚어 줘. "
            f"{common_style_rules}"
        )
    elif slot == "01_overseas_soccer":
        return (
            f"[{date_str} 평일 새벽 01:00 ~ 05:50 해외축구 전 경기 예약분석]\n"
            f"오늘 새벽 1시부터 새벽 5시 50분(KST)까지 킥오프하는 해외축구 전 경기의 공식 일정, 부상자 변수, 최근 기세를 검증한 뒤, "
            f"피나클(Pinnacle) 배당 흐름과 국내 베트맨 프로토 배당을 비교하여 아시안/유러피언 핸디캡 및 언오버(2.5/3.0 등) 핵심 픽을 현덕 형님께 보고해 줘. "
            f"{common_style_rules}"
        )
    else:
        return (
            f"[{date_str} 주말 밤 20:00 ~ 새벽 05:30 해외축구리그 통합 예약분석]\n"
            f"오늘 밤 8시(20:00)부터 내일 새벽 5시 30분(05:30 KST)까지 열리는 주말 해외축구리그(EPL, 라리가, 분데스리가, 세리에A, 리그1 등) "
            f"전 경기의 공식 일정, 결장자 변수, 피나클 배당 흐름 및 베트맨 프로토 배당을 비교하여 핸디캡·언오버 핵심 픽과 최적 조합을 현덕 형님께 보고해 줘. "
            f"{common_style_rules}"
        )


def generate_analysis_text(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # 429 에러(할당량 초과) 방지를 위해 가벼운 모델부터 순차 시도
    models_to_try = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
    last_exception = None

    for model_name in models_to_try:
        try:
            print(f"Trying model: {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.3,
                ),
            )
            if response and response.text:
                return response.text
        except Exception as e:
            print(f"Model {model_name} failed: {e}")
            last_exception = e

    if last_exception:
        raise last_exception
    return "분석 결과를 생성하지 못했습니다."


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다.")
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 3900
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
        }
        resp = requests.post(url, json=payload, timeout=30)
        if not resp.ok:
            print(f"Telegram API Error: {resp.text}")
        resp.raise_for_status()


def main():
    kst = pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(kst)
    date_str = now_kst.strftime("%Y-%m-%d (%a)")
    
    print(f"[{date_str}] Starting analysis workflow...")
    slot = detect_current_slot(now_kst)
    print(f"Selected slot: {slot}")
    
    prompt = build_prompt(slot, date_str)
    report_text = generate_analysis_text(prompt)
    
    print("Sending analysis report to Telegram...")
    send_telegram_message(report_text)
    print(f"Successfully sent slot '{slot}' to Telegram chat {TELEGRAM_CHAT_ID}.")


if __name__ == "__main__":
    main()
