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


def fetch_espn_upcoming(sport_path: str, now_kst: datetime.datetime) -> list:
    dates_to_check = [
        (now_kst - datetime.timedelta(days=1)).strftime("%Y%m%d"),
        now_kst.strftime("%Y%m%d"),
    ]
    seen_ids = set()
    lines = []
    kst_tz = pytz.timezone("Asia/Seoul")

    for d_str in dates_to_check:
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard?dates={d_str}"
        try:
            r = requests.get(url, timeout=(5, 8))
            if r.status_code != 200:
                continue
            data = r.json()
            for ev in data.get("events", []):
                ev_id = ev.get("id", "")
                if ev_id in seen_ids:
                    continue
                seen_ids.add(ev_id)

                state = ev.get("status", {}).get("type", {}).get("state", "")
                if state == "post":
                    continue

                date_iso = ev.get("date", "")
                try:
                    dt = datetime.datetime.fromisoformat(date_iso.replace("Z", "+00:00")).astimezone(kst_tz)
                    diff_hours = (dt - now_kst).total_seconds() / 3600.0
                    if diff_hours < -2 or diff_hours > 24:
                        continue
                    date_kst = dt.strftime("%m/%d %H:%M")
                except Exception:
                    date_kst = date_iso

                comp = (ev.get("competitions") or [{}])[0]
                odds_list = comp.get("odds") or []
                odds_info = ""
                if odds_list:
                    o = odds_list[0]
                    details = o.get("details", "")
                    ou = o.get("overUnder", "")
                    odds_info = f" | 기준점/배당: {details}, 언오버 {ou}"

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
        except Exception:
            continue
    return lines


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
            items.append(f"- {title}")
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


def clean_telegram_text(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", text)
    text = text.replace("---", "──────────")
    return text.strip()


def call_gemini(title: str, guide: str, data_block: str, game_count: int) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=90000))
    models_to_try = [
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-4-hydrogen",
        "gemini-3.6-flash",
        "gemini-3.1-flash-lite",
    ]
    sys_inst = (
        "당신은 한국인 스포츠 베팅 전문 애널리스트입니다. "
        "반드시 100% 한국어로만 작성하고 영어 문장이나 영어 약어 팀명(HOU, MIA 등)을 절대 쓰지 마세요. "
        "마크다운 기호(*, **, #)와 빨간색 이모지(🔴, 🟥)를 절대 사용하지 마세요. "
        "제공된 경기 목록을 단 한 경기도 빠뜨리지 말고 끝까지 전부 분석하세요."
    )
    rules = (
        f"1) 아래 목록에 있는 총 {game_count}경기를 단 한 경기도 빠뜨리지 말고 1번부터 {game_count}번까지 순서대로 전부 분석해 줘 (중간 생략 절대 금지).\n"
        "2) 텔레그램 메시지용이므로 **, *, ###, --- 같은 마크다운 기호는 절대 쓰지 마.\n"
        "3) 모든 팀명은 반드시 한국어 풀네임으로 적고 (홈) / (원정)을 꼭 표시해 줘. 빨간색(🔴, 🟥) 이모지는 절대 쓰지 마.\n"
        "4) 각 경기마다 아래 양식대로 2~3문장의 핵심 분석 근거와 최종 추천픽 딱 1개만 작성해 줘:\n"
        "   [종목 | 한국시간] 홈팀(홈) vs 원정팀(원정) | 기준점\n"
        "   • 분석 근거: 양 팀 시즌 성적, 홈/원정 강약점, 공수 지표 및 기준점 대비 유불리를 2~3문장으로 명확하게 설명\n"
        "   🌿 최종 추천픽: (일반승·핸디캡·언오버를 종합해 가장 확률 높은 딱 1개만 선정)\n"
        "5) 맨 마지막에 가장 확실한 2폴더 추천 조합 1개를 덧붙여 줘."
    )
    prompt = f"{title}\n{guide}\n\n{rules}\n\n[경기 데이터]\n{data_block}"

    for round_idx in range(1, 4):
        for model_name in models_to_try:
            try:
                print(f"[Round {round_idx}] Trying model: {model_name} for {title}...", flush=True)
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=sys_inst,
                        temperature=0.3,
                        max_output_tokens=8192,
                    ),
                )
                if response and response.text:
                    return clean_telegram_text(response.text)
            except Exception as e:
                print(f"Model {model_name} failed: {e}", flush=True)
                time.sleep(8 * round_idx)

    return f"{title}\n(일시적 AI 서버 혼잡으로 실시간 수집된 경기 및 기준점 원본을 먼저 전달합니다)\n\n{data_block}"


def resolve_telegram_chat_id() -> str:
    try:
        upd = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=(5, 10)).json()
        results = upd.get("result") or []
        if results:
            last_msg = results[-1].get("message") or results[-1].get("edited_message") or {}
            detected_id = last_msg.get("chat", {}).get("id")
            if detected_id:
                return str(detected_id)
    except Exception:
        pass
    return TELEGRAM_CHAT_ID


def send_telegram_message(text: str, target_chat_id: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 2 <= 3800:
            current = f"{current}\n\n{p}".strip() if current else p
        else:
            if current:
                chunks.append(current)
            if len(p) <= 3800:
                current = p
            else:
                for i in range(0, len(p), 3800):
                    chunks.append(p[i:i + 3800])
                current = ""
    if current:
        chunks.append(current)

    for idx, chunk in enumerate(chunks, 1):
        print(f"Sending chunk {idx}/{len(chunks)} to {target_chat_id}...", flush=True)
        payload = {"chat_id": target_chat_id, "text": chunk}
        resp = requests.post(url, json=payload, timeout=(5, 15))
        resp.raise_for_status()
        time.sleep(1)


def run_nba_nhl(now_kst: datetime.datetime, date_str: str, chat_id: str) -> None:
    nba_games = fetch_espn_upcoming("basketball/nba", now_kst)
    nhl_games = fetch_espn_upcoming("hockey/nhl", now_kst)

    if nba_games:
        nba_news = fetch_google_news_rss("NBA injury report preview odds today", max_items=5, lang="en")
        block = "[오늘 예정된 NBA 경기 목록]\n" + "\n".join(nba_games)
        if nba_news:
            block += "\n\n[NBA 참고 소식]\n" + nba_news
        text = call_gemini(
            f"🏀 [{date_str} NBA 전 경기(총 {len(nba_games)}경기) 예약분석]",
            "오늘 열리는 NBA 전 경기를 한 경기도 빠짐없이 끝까지 분석해 줘.",
            block,
            len(nba_games),
        )
        send_telegram_message(text, chat_id)

    if nba_games and nhl_games:
        print("Cooling down 12s before NHL analysis...", flush=True)
        time.sleep(12)

    if nhl_games:
        nhl_news = fetch_google_news_rss("NHL starting goalies injury preview picks today", max_items=5, lang="en")
        block = "[오늘 예정된 NHL 경기 목록]\n" + "\n".join(nhl_games)
        if nhl_news:
            block += "\n\n[NHL 참고 소식]\n" + nhl_news
        text = call_gemini(
            f"🏒 [{date_str} NHL 전 경기(총 {len(nhl_games)}경기) 예약분석]",
            "오늘 열리는 NHL 전 경기를 한 경기도 빠짐없이 끝까지 분석해 줘.",
            block,
            len(nhl_games),
        )
        send_telegram_message(text, chat_id)


def run_domestic(now_kst: datetime.datetime, date_str: str, chat_id: str) -> None:
    date_ko = f"{now_kst.month}월 {now_kst.day}일"
    sections = []
    count = 0
    for label, path in [("K리그1", "soccer/kor.1"), ("J리그", "soccer/jpn.1"), ("AFC챔피언스리그", "soccer/afc.champions")]:
        g = fetch_espn_upcoming(path, now_kst)
        if g:
            count += len(g)
            sections.append(f"[{label}]\n" + "\n".join(g))

    news_kbl = fetch_google_news_rss(f"{date_ko} 프로농구 KBL WKBL V리그 경기 일정 프리뷰", max_items=10)
    news_proto = fetch_google_news_rss(f"프로토 승부식 {date_ko} KBL WKBL V리그 배당", max_items=8)
    if news_kbl:
        sections.append(f"[{date_ko} 국내 농구/배구 뉴스 및 일정]\n{news_kbl}")
    if news_proto:
        sections.append(f"[프로토 관련 소식]\n{news_proto}")
    wt = fetch_wisetoto_text()
    if len(wt) > 200:
        sections.append(f"[와이즈토토 프로토 배당판]\n{wt}")

    block = "\n\n".join(sections) if sections else "오늘 예정된 국내 경기 데이터 없음"
    text = call_gemini(
        f"🏐 [{date_str} 국내경기(KBL·WKBL·V리그) & 아시아축구 예약분석]",
        "베트맨 프로토 기준점과 국내외 배당 흐름을 바탕으로 오늘 열리는 국내경기 및 아시아축구를 빠짐없이 분석해 줘.",
        block,
        max(count, 2),
    )
    send_telegram_message(text, chat_id)


def run_overseas_soccer(now_kst: datetime.datetime, date_str: str, chat_id: str) -> None:
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
    sections = []
    total_games = 0
    for label, path in leagues:
        g = fetch_espn_upcoming(path, now_kst)
        if g:
            total_games += len(g)
            sections.append(f"[{label}]\n" + "\n".join(g))

    soccer_news = fetch_google_news_rss("football prediction preview team news odds today", max_items=6, lang="en")
    if soccer_news:
        sections.append("[해외축구 프리뷰 소식]\n" + soccer_news)

    block = "\n\n".join(sections) if sections else "오늘 예정된 해외축구 경기 없음"
    text = call_gemini(
        f"⚽ [{date_str} 해외축구 전 경기(총 {total_games}경기) 예약분석]",
        "오늘 예정된 해외축구 전 경기를 빠짐없이 피나클·프로토 배당 흐름 기준으로 분석해 줘.",
        block,
        max(total_games, 1),
    )
    send_telegram_message(text, chat_id)


def main():
    kst = pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(kst)
    date_str = now_kst.strftime("%Y-%m-%d (%a)")
    slot = detect_current_slot(now_kst)
    chat_id = resolve_telegram_chat_id()
    print(f"Running slot: {slot} -> chat_id: {chat_id}", flush=True)

    if slot == "07_nba_nhl":
        run_nba_nhl(now_kst, date_str, chat_id)
    elif slot in ("17_domestic", "13_weekend_domestic"):
        run_domestic(now_kst, date_str, chat_id)
    elif slot in ("01_overseas_soccer", "20_weekend_soccer"):
        run_overseas_soccer(now_kst, date_str, chat_id)
    else:
        run_overseas_soccer(now_kst, date_str, chat_id)
        time.sleep(10)
        run_nba_nhl(now_kst, date_str, chat_id)
        time.sleep(10)
        run_domestic(now_kst, date_str, chat_id)

    print("ALL DONE!", flush=True)


if __name__ == "__main__":
    main()
