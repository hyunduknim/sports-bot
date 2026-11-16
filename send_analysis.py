import os
import re
import html
import time
import urllib.parse
import xml.etree.ElementTree as ET
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
    return "all_sports"


def to_kst_str(date_iso: str) -> str:
    try:
        dt = datetime.datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
        kst_dt = dt.astimezone(pytz.timezone("Asia/Seoul"))
        return kst_dt.strftime("%m/%d %H:%M KST")
    except Exception:
        return date_iso


def fetch_espn_scoreboard(sport_path: str, dates_str: str) -> list:
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard?dates={dates_str}"
    try:
        r = requests.get(url, timeout=(5, 8))
        if r.status_code != 200:
            return []
        data = r.json()
        lines = []
        for ev in data.get("events", []):
            date_kst = to_kst_str(ev.get("date", ""))
            comp = (ev.get("competitions") or [{}])[0]
            odds_list = comp.get("odds") or []
            odds_info = ""
            if odds_list:
                o = odds_list[0]
                details = o.get("details", "")
                ou = o.get("overUnder", "")
                odds_info = f" | 배당/기준: {details}, 언오버 {ou}"
            competitors = comp.get("competitors") or []
            home_str, away_str = "", ""
            for c in competitors:
                t_name = c.get("team", {}).get("displayName", "")
                ha = c.get("homeAway", "")
                recs = c.get("records") or []
                rec = recs[0].get("summary", "") if recs else ""
                if ha == "home":
                    home_str = f"{t_name}(홈, {rec})"
                else:
                    away_str = f"{t_name}(원정, {rec})"
            lines.append(f"- [{date_kst}] {home_str} vs {away_str}{odds_info}")
        return lines
    except Exception:
        return []


def fetch_google_news_rss(query: str, max_items: int = 8, lang: str = "ko") -> str:
    try:
        q = urllib.parse.quote(query)
        if lang == "ko":
            url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
        else:
            url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, timeout=(5, 10))
        if r.status_code != 200:
            return ""
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item")[:max_items]:
            title = item.findtext("title", "")
            pub = item.findtext("pubDate", "")
            items.append(f"- {title} ({pub})")
        return "\n".join(items)
    except Exception:
        return ""


def fetch_wisetoto_text() -> str:
    try:
        r = requests.get("https://www.wisetoto.com/", headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 10))
        r.encoding = r.apparent_encoding or "utf-8"
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        return text[:2500]
    except Exception:
        return ""


def collect_nba_nhl(yesterday_ymd: str, today_ymd: str) -> list:
    out = []
    nba = fetch_espn_scoreboard("basketball/nba", yesterday_ymd) + fetch_espn_scoreboard("basketball/nba", today_ymd)
    nhl = fetch_espn_scoreboard("hockey/nhl", yesterday_ymd) + fetch_espn_scoreboard("hockey/nhl", today_ymd)
    out.append("[NBA 공식 일정/배당]\n" + ("\n".join(nba) if nba else "조회된 경기 없음"))
    out.append("[NHL 공식 일정/배당]\n" + ("\n".join(nhl) if nhl else "조회된 경기 없음"))
    nba_news = fetch_google_news_rss("NBA injury report preview odds today", max_items=6, lang="en")
    nhl_news = fetch_google_news_rss("NHL starting goalies injury preview picks today", max_items=6, lang="en")
    if nba_news:
        out.append("[NBA 주요 부상/프리뷰 소식]\n" + nba_news)
    if nhl_news:
        out.append("[NHL 골리/부상/프리뷰 소식]\n" + nhl_news)
    return out


def collect_domestic(yesterday_ymd: str, today_ymd: str, date_ko: str) -> list:
    out = []
    asia_leagues = [
        ("K리그1", "soccer/kor.1"),
        ("J리그", "soccer/jpn.1"),
        ("AFC챔피언스리그", "soccer/afc.champions"),
    ]
    for label, path in asia_leagues:
        games = fetch_espn_scoreboard(path, yesterday_ymd) + fetch_espn_scoreboard(path, today_ymd)
        if games:
            out.append(f"[{label} 공식 일정]\n" + "\n".join(games))

    news_kbl = fetch_google_news_rss(f"{date_ko} 프로농구 KBL WKBL V리그 경기 일정 프리뷰")
    news_proto = fetch_google_news_rss(f"프로토 승부식 {date_ko} KBL WKBL 배당")
    if news_kbl:
        out.append(f"[{date_ko} 국내경기 뉴스/일정]\n{news_kbl}")
    if news_proto:
        out.append(f"[프로토 관련 소식]\n{news_proto}")

    wt = fetch_wisetoto_text()
    if len(wt) > 200:
        out.append(f"[와이즈토토 프로토 배당판]\n{wt}")
    return out


def collect_overseas_soccer(yesterday_ymd: str, today_ymd: str) -> list:
    out = []
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
            out.append(f"[{label}]\n" + "\n".join(games))
    soccer_news = fetch_google_news_rss("football prediction preview team news odds today", max_items=6, lang="en")
    if soccer_news:
        out.append("[해외축구 프리뷰/팀 뉴스]\n" + soccer_news)
    return out


def collect_live_context(slot: str, now_kst: datetime.datetime) -> str:
    today_ymd = now_kst.strftime("%Y%m%d")
    yesterday_ymd = (now_kst - datetime.timedelta(days=1)).strftime("%Y%m%d")
    date_ko = f"{now_kst.month}월 {now_kst.day}일"

    if slot == "07_nba_nhl":
        sections = collect_nba_nhl(yesterday_ymd, today_ymd)
    elif slot in ("17_domestic", "13_weekend_domestic"):
        sections = collect_domestic(yesterday_ymd, today_ymd, date_ko)
    elif slot in ("01_overseas_soccer", "20_weekend_soccer"):
        sections = collect_overseas_soccer(yesterday_ymd, today_ymd)
    else:
        sections = (
            collect_overseas_soccer(yesterday_ymd, today_ymd)
            + collect_nba_nhl(yesterday_ymd, today_ymd)
            + collect_domestic(yesterday_ymd, today_ymd, date_ko)
        )

    return "\n\n".join(sections) if sections else "수집된 경기 데이터가 없습니다."


def build_prompt(slot: str, date_str: str, live_data: str) -> str:
    common_style_rules = (
        "1) 텔레그램 메시지용이므로 **, *, ###, --- 같은 마크다운 기호는 절대 쓰지 말고 깔끔한 줄바꿈과 이모지(🌿, 🏀, 🏒, ⚽, 📌)로만 작성해 줘.\n"
        "2) 모든 팀명은 반드시 한국어 풀네임으로 적고 (홈) / (원정)을 꼭 표시해 줘. 빨간색(🔴, 🟥) 이모지는 절대 쓰지 마.\n"
        "3) 절대로 1줄 요약으로 대충 넘기지 말고, 경기마다 아래 양식대로 상세한 분석 이유를 꼭 작성해 줘:\n"
        "   [종목 | 한국시간] 홈팀(홈) vs 원정팀(원정) (기준점/배당)\n"
        "   • 전력/기세 분석: 양 팀 시즌 성적, 홈/원정 유불리, 최근 흐름, 부상자·골리·백투백 체력 변수와 배당 흐름을 3~4문장으로 디테일하게 설명\n"
        "   🌿 최종 추천픽: (일반승·핸디캡·언오버를 모두 종합해서 가장 확률과 밸류가 높은 딱 1개만 선정)\n"
        "4) 맨 마지막에는 전체 경기 중 가장 안정적인 2폴더 최적 조합 1~2개만 깔끔하게 정리해 줘.\n"
        "5) 아래 [실시간 수집 데이터]에 없는 경기는 절대 지어내지 마."
    )

    if slot == "07_nba_nhl":
        header = f"[{date_str} 아침 07:00 NBA · NHL 고퀄리티 예약분석]"
        guide = "피나클(Pinnacle) 로우비그 배당 흐름과 프로토 환산 배당을 비교하여 오늘 NBA·NHL 경기를 디테일하게 분석해 줘."
    elif slot in ("17_domestic", "13_weekend_domestic"):
        time_label = "평일 낮 17:00" if slot == "17_domestic" else "주말 낮 13:00"
        header = f"[{date_str} {time_label} 국내경기 & 아시아축구 예약분석]"
        guide = (
            "오늘 열리는 국내 경기(KBL, WKBL, V-리그, KBO) 및 한국축구(K리그)·일본축구(J리그)·AFC챔피언스리그 경기가 있다면 모두 포함해 분석해 줘. "
            "배당과 기준점은 국내 공식 '베트맨 프로토 승부식' 기준을 최우선으로 삼고 밸류 구간을 짚어 줘."
        )
    elif slot == "01_overseas_soccer":
        header = f"[{date_str} 평일 새벽 01:00 ~ 05:50 해외축구 전 경기 예약분석]"
        guide = "새벽 1시~5시 50분(KST) 해외축구 경기를 피나클·베트맨 배당 흐름 기준으로 디테일하게 분석해 줘."
    elif slot == "20_weekend_soccer":
        header = f"[{date_str} 주말 밤 20:00 ~ 새벽 05:30 해외축구리그 통합 예약분석]"
        guide = "밤 8시~새벽 5시 30분(KST) 주말 해외축구리그 경기를 피나클·베트맨 배당 흐름 기준으로 디테일하게 분석해 줘."
    else:
        header = f"[{date_str} 전체 종목 통합 분석 (해외축구 · NBA · NHL · 국내경기)]"
        guide = "해외축구, NBA, NHL, 국내경기 및 아시아축구까지 수집된 모든 종목을 섹션별로 나누어 디테일하게 분석해 줘."

    return f"{header}\n{guide}\n\n{common_style_rules}\n\n[실시간 수집 데이터]\n{live_data}"


def clean_telegram_text(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", text)
    text = text.replace("---", "──────────")
    return text.strip()


def generate_analysis_text(prompt: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=90000))
    models_to_try = ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    last_err = None

    for model_name in models_to_try:
        try:
            print(f"Trying model: {model_name}...", flush=True)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=8192),
            )
            if response and response.text:
                return clean_telegram_text(response.text)
        except Exception as e:
            print(f"Model {model_name} failed: {e}", flush=True)
            last_err = e
            time.sleep(2)

    raise RuntimeError(f"All models failed. Last error: {last_err}")


def resolve_telegram_chat_id() -> str:
    try:
        me = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=(5, 10)).json()
        if me.get("ok"):
            print(f"Bot connected: @{me['result'].get('username')}", flush=True)
        upd = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=(5, 10)).json()
        results = upd.get("result") or []
        if results:
            last_msg = results[-1].get("message") or results[-1].get("edited_message") or {}
            detected_id = last_msg.get("chat", {}).get("id")
            if detected_id:
                print(f"Auto-detected chat_id: {detected_id}", flush=True)
                return str(detected_id)
    except Exception as e:
        print(f"Chat ID check note: {e}", flush=True)
    return TELEGRAM_CHAT_ID


def send_telegram_message(text: str) -> None:
    target_chat_id = resolve_telegram_chat_id()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 3900
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    for idx, chunk in enumerate(chunks, 1):
        print(f"Sending chunk {idx}/{len(chunks)} to {target_chat_id}...", flush=True)
        payload = {"chat_id": target_chat_id, "text": chunk}
        resp = requests.post(url, json=payload, timeout=(5, 15))
        if not resp.ok:
            print(f"Telegram Error Response: {resp.status_code} {resp.text}", flush=True)
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
    print("[3/4] Calling Gemini...", flush=True)
    report_text = generate_analysis_text(prompt)
    print(f"[4/4] Generated length: {len(report_text)}, sending to Telegram...", flush=True)
    send_telegram_message(report_text)
    print("DONE! Telegram message delivered.", flush=True)


if __name__ == "__main__":
    main()
