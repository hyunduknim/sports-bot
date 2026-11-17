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
    if MANUAL_SLOT and MANUAL_SLOT != "all_sports":
        return MANUAL_SLOT
    weekday = now_kst.weekday()
    hour = now_kst.hour
    is_weekend = weekday in (5, 6)

    if 5 <= hour <= 10:
        return "07_nba_nhl"
    if not is_weekend and 15 <= hour <= 19:
        return "17_domestic"
    if is_weekend and 11 <= hour <= 15:
        return "13_weekend_domestic"
    if not is_weekend and (hour <= 4 or hour == 23):
        return "01_overseas_soccer"
    if is_weekend and 18 <= hour <= 22:
        return "20_weekend_soccer"
    return "07_nba_nhl"


def fetch_espn_window(sport_path: str, start_kst: datetime.datetime, end_kst: datetime.datetime) -> list:
    dates_to_check = [
        (start_kst - datetime.timedelta(days=1)).strftime("%Y%m%d"),
        start_kst.strftime("%Y%m%d"),
        end_kst.strftime("%Y%m%d"),
    ]
    seen_ids = set()
    lines = []
    kst_tz = pytz.timezone("Asia/Seoul")

    for d_str in dict.fromkeys(dates_to_check):
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

                date_iso = ev.get("date", "")
                try:
                    dt = datetime.datetime.fromisoformat(date_iso.replace("Z", "+00:00")).astimezone(kst_tz)
                except Exception:
                    continue

                if not (start_kst <= dt <= end_kst):
                    continue

                seen_ids.add(ev_id)
                date_kst = dt.strftime("%m/%d %H:%M")

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


def fetch_google_news_rss(query: str, max_items: int = 10, lang: str = "ko") -> str:
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
            desc = re.sub(r"<[^>]+>", " ", item.findtext("description", ""))
            desc = html.unescape(re.sub(r"\s+", " ", desc)).strip()
            items.append(f"- {title} | {desc}")
        return "\n".join(items)
    except Exception:
        return ""


def fetch_via_jina(target_url: str, max_chars: int = 3500) -> str:
    try:
        r = requests.get(
            f"https://r.jina.ai/{target_url}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=(5, 12),
        )
        if r.status_code == 200 and len(r.text) > 100:
            return r.text[:max_chars]
    except Exception:
        pass
    return ""


def fetch_domestic_schedule_and_odds(now_kst: datetime.datetime) -> str:
    day_num = now_kst.day
    month_num = now_kst.month
    parts = []

    # 1. 연합뉴스 '오늘의 경기(N일)' 검색 및 본문 추출
    yna_rss = fetch_google_news_rss(f"\"오늘의 경기({day_num}일)\" 연합뉴스 프로농구 프로배구", max_items=5)
    if yna_rss:
        parts.append(f"[연합뉴스 오늘의 경기 공식 일정]\n{yna_rss}")

    # 2. 종목별 오늘 경기 프리뷰 기사 수집
    kbl_rss = fetch_google_news_rss(f"{month_num}월 {day_num}일 프로농구 OR 여자농구 경기 일정 맞대결", max_items=8)
    v_rss = fetch_google_news_rss(f"{month_num}월 {day_num}일 프로배구 V리그 남자부 여자부 경기 일정", max_items=8)
    if kbl_rss:
        parts.append(f"[오늘 국내 농구 프리뷰/일정]\n{kbl_rss}")
    if v_rss:
        parts.append(f"[오늘 국내 배구 프리뷰/일정]\n{v_rss}")

    # 3. 와이즈토토 프로토 배당판 (해외서버 우회 프록시 포함)
    wt_text = ""
    try:
        r = requests.get("https://www.wisetoto.com/", headers={"User-Agent": "Mozilla/5.0"}, timeout=(4, 8))
        r.encoding = r.apparent_encoding or "utf-8"
        t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S | re.I)
        t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))).strip()
        if len(t) > 300:
            wt_text = t[:3500]
    except Exception:
        pass

    if not wt_text:
        wt_text = fetch_via_jina("https://www.wisetoto.com/", max_chars=3500)

    if wt_text:
        parts.append(f"[프로토 승부식 배당판]\n{wt_text}")

    return "\n\n".join(parts)


def clean_telegram_text(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", text)
    text = text.replace("---", "──────────")
    return text.strip()


def call_gemini(title: str, guide: str, data_block: str) -> str:
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
        "절대로 '데이터가 부족하다'거나 '수행이 불가능하다'는 안내문을 출력하지 말고, 제공된 뉴스·일정·배당 데이터에서 오늘 열리는 대진을 파악해 전문적인 전력 분석과 최종 추천픽을 반드시 작성하세요."
    )
    rules = (
        "1) 확인된 모든 오늘 경기를 단 한 경기도 빠뜨리지 말고 순서대로 전부 분석해 줘 (중간 생략 및 분석 거부 절대 금지).\n"
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
    start_w = now_kst.replace(hour=6, minute=0, second=0, microsecond=0)
    end_w = now_kst.replace(hour=16, minute=0, second=0, microsecond=0)

    nba_games = fetch_espn_window("basketball/nba", start_w, end_w)
    nhl_games = fetch_espn_window("hockey/nhl", start_w, end_w)

    if nba_games:
        nba_news = fetch_google_news_rss("NBA injury report preview odds today", max_items=5, lang="en")
        block = f"[오늘({date_str}) NBA 경기 목록 - 총 {len(nba_games)}경기]\n" + "\n".join(nba_games)
        if nba_news:
            block += "\n\n[NBA 참고 소식]\n" + nba_news
        text = call_gemini(
            f"🏀 [{date_str} 오늘의 NBA 전 경기(총 {len(nba_games)}경기) 예약분석]",
            "오늘 열리는 NBA 경기만 한 경기도 빠짐없이 끝까지 분석해 줘.",
            block,
        )
        send_telegram_message(text, chat_id)

    if nba_games and nhl_games:
        time.sleep(12)

    if nhl_games:
        nhl_news = fetch_google_news_rss("NHL starting goalies injury preview picks today", max_items=5, lang="en")
        block = f"[오늘({date_str}) NHL 경기 목록 - 총 {len(nhl_games)}경기]\n" + "\n".join(nhl_games)
        if nhl_news:
            block += "\n\n[NHL 참고 소식]\n" + nhl_news
        text = call_gemini(
            f"🏒 [{date_str} 오늘의 NHL 전 경기(총 {len(nhl_games)}경기) 예약분석]",
            "오늘 열리는 NHL 경기만 한 경기도 빠짐없이 끝까지 분석해 줘.",
            block,
        )
        send_telegram_message(text, chat_id)


def run_domestic(now_kst: datetime.datetime, date_str: str, chat_id: str) -> None:
    start_w = now_kst.replace(hour=12, minute=0, second=0, microsecond=0)
    end_w = now_kst.replace(hour=23, minute=59, second=59, microsecond=0)
    date_ko = f"{now_kst.month}월 {now_kst.day}일"

    sections = []
    for label, path in [("K리그1", "soccer/kor.1"), ("J리그", "soccer/jpn.1"), ("AFC챔피언스리그", "soccer/afc.champions")]:
        g = fetch_espn_window(path, start_w, end_w)
        if g:
            sections.append(f"[{label}]\n" + "\n".join(g))

    dom_info = fetch_domestic_schedule_and_odds(now_kst)
    if dom_info:
        sections.append(dom_info)

    block = "\n\n".join(sections) if sections else "오늘 예정된 국내 경기 데이터 없음"
    text = call_gemini(
        f"🏐 [{date_str} 오늘의 국내경기(KBL·WKBL·V리그) & 아시아축구 예약분석]",
        f"오늘({date_ko}) 열리는 국내 프로농구(KBL·WKBL), 프로배구(V-리그 남·여), 한국/일본축구 및 아챔 경기를 파악하여 한 경기도 빠짐없이 상세 분석과 최종 추천픽 1개씩을 작성해 줘.",
        block,
    )
    send_telegram_message(text, chat_id)


def run_overseas_soccer(now_kst: datetime.datetime, date_str: str, chat_id: str, is_weekend_slot: bool = False) -> None:
    if is_weekend_slot:
        start_w = now_kst.replace(hour=20, minute=0, second=0, microsecond=0)
        end_w = (now_kst + datetime.timedelta(days=1)).replace(hour=5, minute=30, second=0, microsecond=0)
        window_desc = "밤 20:00 ~ 내일 새벽 05:30"
    else:
        base_day = now_kst + datetime.timedelta(days=1) if now_kst.hour >= 20 else now_kst
        start_w = base_day.replace(hour=1, minute=0, second=0, microsecond=0)
        end_w = base_day.replace(hour=5, minute=50, second=0, microsecond=0)
        window_desc = "새벽 01:00 ~ 새벽 05:50"

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
        g = fetch_espn_window(path, start_w, end_w)
        if g:
            total_games += len(g)
            sections.append(f"[{label}]\n" + "\n".join(g))

    if total_games == 0:
        send_telegram_message(f"⚽ [{date_str} 해외축구 안내]\n오늘 {window_desc} 사이에 예정된 주요 해외축구 경기가 없습니다.", chat_id)
        return

    block = "\n\n".join(sections)
    text = call_gemini(
        f"⚽ [{date_str} 해외축구 ({window_desc}) 전 경기(총 {total_games}경기) 예약분석]",
        f"오직 {window_desc} 사이에 열리는 총 {total_games}경기만 빠짐없이 분석해 줘.",
        block,
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
    elif slot == "01_overseas_soccer":
        run_overseas_soccer(now_kst, date_str, chat_id, is_weekend_slot=False)
    elif slot == "20_weekend_soccer":
        run_overseas_soccer(now_kst, date_str, chat_id, is_weekend_slot=True)

    print("ALL DONE!", flush=True)


if __name__ == "__main__":
    main()
