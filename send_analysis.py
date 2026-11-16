import os
import re
import html
import datetime
import pytz
import requests
from google import genai
from google.genai import types

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "9112315353").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
MANUAL_SLOT = os.environ.get("MANUAL_SLOT", "").strip()


def detect_current_slot(now_kst: datetime.datetime) -> str:
    if MANUAL_SLOT:
        return MANUAL_SLOT
    weekday = now_kst.weekday()
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


def fetch_espn_scoreboard(sport_path: str, dates_str: str) -> list:
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard?dates={dates_str}"
    try:
        r = requests.get(url, timeout=(5, 8))
        if r.status_code != 200:
            return []
        data = r.json()
        lines = []
        for ev in data.get("events", []):
            name = ev.get("name", "")
            date_iso = ev.get("date", "")
            comp = (ev.get("competitions") or [{}])[0]
            odds_list = comp.get("odds") or []
            odds_info = ""
            if odds_list:
                o = odds_list[0]
                details = o.get("details", "")
                ou = o.get("overUnder", "")
                odds_info = f" | 배당/기준: {details}, O/U {ou}"
            competitors = comp.get("competitors") or []
            rec_parts = []
            for c in competitors:
                t_name = c.get("team", {}).get("displayName", "")
                ha = c.get("homeAway", "")
                recs = c.get("records") or []
                rec = recs[0].get("summary", "") if recs else ""
                rec_parts.append(f"{t_name}({ha}, {rec})")
            lines.append(f"- {name} ({date_iso}) | {' vs '.join(rec_parts)}{odds_info}")
        return lines
    except Exception:
        return []


def fetch_wisetoto_text() -> str:
    try:
        r = requests.get("https://www.wisetoto.com/", headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 10))
        r.encoding = r.apparent_encoding or "utf-8"
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        return text[:4500]
    except Exception as e:
        return f"와이즈토토 연결 지연(해외서버 제한 가능): {e}"


def collect_live_context(slot: str, now_kst: datetime.datetime) -> str:
    today_ymd = now_kst.strftime("%Y%m%d")
    yesterday_ymd = (now_kst - datetime.timedelta(days=1)).strftime("%Y%m%d")
    sections = []

    if slot == "07_nba_nhl":
        nba = fetch_espn_scoreboard("basketball/nba", yesterday_ymd) + fetch_espn_scoreboard("basketball/nba", today_ymd)
        nhl = fetch_espn_scoreboard("hockey/nhl", yesterday_ymd) + fetch_espn_scoreboard("hockey/nhl", today_ymd)
        sections.append("[NBA 공식 일정/배당]\n" + ("\n".join(nba) if nba else "조회된 경기 없음"))
        sections.append("[NHL 공식 일정/배당]\n" + ("\n".join(nhl) if nhl else "조회된 경기 없음"))
    elif slot in ("17_domestic", "13_weekend_domestic"):
        sections.append("[와이즈토토 프로토 승부식 배당판]\n" + fetch_wisetoto_text())
    else:
        leagues = [
            ("EPL", "soccer/eng.1"),
            ("라리가", "soccer/esp.1"),
            ("분데스리가", "soccer/ger.1"),
            ("세리에A", "soccer/ita.1"),
            ("리그1", "soccer/fra.1"),
            ("챔피언스리그", "soccer/uefa.champions"),
            ("유로파리그", "soccer/uefa.europa"),
            ("네이션스리그", "soccer/uefa.nations"),
        ]
        for label, path in leagues:
            games = fetch_espn_scoreboard(path, yesterday_ymd) + fetch_espn_scoreboard(path, today_ymd)
            if games:
                sections.append(f"[{label}]\n" + "\n".join(games))
        sections.append("[와이즈토토 프로토 참고]\n" + fetch_wisetoto_text()[:2000])

    return "\n\n".join(sections)


def build_prompt(slot: str, date_str: str, live_data: str) -> str:
    common_style_rules = (
        "반드시 눈이 피로한 빨간색(🔴, 🟥) 이모지나 원색 강조를 절대 사용하지 말고, "
        "눈이 편안한 스타일(🌿 주력 추천픽, 💧 언오버/서브 추천픽, ⭐ 고배당 밸류픽)로만 작성해 줘. "
        "단순 일반승 나열이 아니라 핸디캡(마핸/프핸 기준점)과 언오버(기준점)를 반드시 포함해 가장 밸류 높은 라인과 최적 2~3폴더 조합을 제시해 줘."
    )

    if slot == "07_nba_nhl":
        header = f"[{date_str} 아침 07:00 NBA · NHL 고퀄리티 예약분석]"
        guide = "피나클(Pinnacle) 로우비그 배당 흐름과 프로토 환산 배당을 비교하여 오늘 NBA·NHL 경기를 분석해 줘."
    elif slot in ("17_domestic", "13_weekend_domestic"):
        time_label = "평일 낮 17:00" if slot == "17_domestic" else "주말 낮 13:00"
        header = f"[{date_str} {time_label} 국내경기 & 아시아축구 예약분석]"
        guide = (
            "오늘 열리는 국내 경기(KBL, WKBL, V-리그, KBO) 및 한국축구(K리그)·일본축구(J리그)·AFC챔피언스리그 경기가 있다면 모두 포함해 분석해 줘. "
            "배당은 반드시 국내 공식 '베트맨 프로토 승부식' 실제 배당과 기준점을 최우선 기준으로 삼고, 해외 배당 흐름 대비 밸류 구간을 짚어 줘."
        )
    elif slot == "01_overseas_soccer":
        header = f"[{date_str} 평일 새벽 01:00 ~ 05:50 해외축구 전 경기 예약분석]"
        guide = "새벽 1시~5시 50분(KST) 해외축구 경기를 피나클·베트맨 배당 흐름 기준으로 핸디캡·언오버 핵심 픽을 정리해 줘."
    else:
        header = f"[{date_str} 주말 밤 20:00 ~ 새벽 05:30 해외축구리그 통합 예약분석]"
        guide = "밤 8시~새벽 5시 30분(KST) 주말 해외축구리그 경기를 피나클·베트맨 배당 흐름 기준으로 핸디캡·언오버 핵심 픽과 최적 조합을 정리해 줘."

    return f"{header}\n{guide}\n{common_style_rules}\n\n[실시간 수집 데이터]\n{live_data}"


def generate_analysis_text(prompt: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=60000))
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.3),
    )
    return response.text or "분석 결과를 생성하지 못했습니다."


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 3900
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    for idx, chunk in enumerate(chunks, 1):
        print(f"Sending chunk {idx}/{len(chunks)} to Telegram...", flush=True)
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk}
        resp = requests.post(url, json=payload, timeout=(5, 15))
        resp.raise_for_status()


def main():
    kst = pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(kst)
    date_str = now_kst.strftime("%Y-%m-%d (%a)")
    slot = detect_current_slot(now_kst)
    print(f"[1/4] Running slot: {slot}", flush=True)
    live_data = collect_live_context(slot, now_kst)
    print(f"[2/4] Collected data length: {len(live_data)}", flush=True)
    prompt = build_prompt(slot, date_str, live_data)
    print("[3/4] Calling Gemini 3.8 Flash...", flush=True)
    report_text = generate_analysis_text(prompt)
    print(f"[4/4] Generated length: {len(report_text)}, sending to Telegram...", flush=True)
    send_telegram_message(report_text)
    print(f"DONE! Sent slot '{slot}' to Telegram chat {TELEGRAM_CHAT_ID}.", flush=True)


if __name__ == "__main__":
    main()
