import os
import re
import json
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
HISTORY_FILE = "picks_history.json"


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


def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history(items: list) -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(items[-30:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"History save note: {e}", flush=True)


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


def fetch_finished_scores(now_kst: datetime.datetime) -> str:
    paths = [
        ("NBA", "basketball/nba"),
        ("NHL", "hockey/nhl"),
        ("K리그1", "soccer/kor.1"),
        ("J리그", "soccer/jpn.1"),
        ("EPL", "soccer/eng.1"),
        ("라리가", "soccer/esp.1"),
        ("분데스리가", "soccer/ger.1"),
        ("세리에A", "soccer/ita.1"),
        ("리그1", "soccer/fra.1"),
        ("챔피언스리그", "soccer/uefa.champions"),
        ("네이션스리그", "soccer/uefa.nations"),
    ]
    dates_to_check = [
        (now_kst - datetime.timedelta(days=1)).strftime("%Y%m%d"),
        now_kst.strftime("%Y%m%d"),
    ]
    seen_ids = set()
    results = []
    for label, path in paths:
        for d_str in dates_to_check:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={d_str}"
            try:
                r = requests.get(url, timeout=(4, 6))
                if r.status_code != 200:
                    continue
                for ev in r.json().get("events", []):
                    ev_id = ev.get("id", "")
                    if ev_id in seen_ids:
                        continue
                    state = ev.get("status", {}).get("type", {}).get("state", "")
                    if state != "post":
                        continue
                    seen_ids.add(ev_id)
                    comp = (ev.get("competitions") or [{}])[0]
                    home_s, away_s = "", ""
                    for c in comp.get("competitors") or []:
                        t_name = c.get("team", {}).get("displayName", "")
                        score = c.get("score", "0")
                        if c.get("homeAway") == "home":
                            home_s = f"{t_name}(홈) {score}"
                        else:
                            away_s = f"{t_name}(원정) {score}"
                    results.append(f"- [{label} 종료] {home_s} : {away_s}")
            except Exception:
                continue
    return "\n".join(results)


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

    yna_rss = fetch_google_news_rss(f"\"오늘의 경기({day_num}일)\" 연합뉴스 프로농구 프로배구", max_items=5)
    if yna_rss:
        parts.append(f"[연합뉴스 오늘의 경기 공식 일정]\n{yna_rss}")

    kbl_rss = fetch_google_news_rss(f"{month_num}월 {day_num}일 프로농구 OR 여자농구 경기 일정 맞대결", max_items=8)
    v_rss = fetch_google_news_rss(f"{month_num}월 {day_num}일 프로배구 V리그 남자부 여자부 경기 일정", max_items=8)
    if kbl_rss:
        parts.append(f"[오늘 국내 농구 프리뷰/일정]\n{kbl_rss}")
    if v_rss:
        parts.append(f"[오늘 국내 배구 프리뷰/일정]\n{v_rss}")

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


def raw_gemini_call(sys_inst: str, prompt: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=90000))
    models_to_try = [
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-4-hydrogen",
        "gemini-3.6-flash",
        "gemini-3.1-flash-lite",
    ]
    for round_idx in range(1, 4):
        for model_name in models_to_try:
            try:
                print(f"[Round {round_idx}] Trying model: {model_name}...", flush=True)
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=sys_inst,
                        temperature=0.2,
                        max_output_tokens=8192,
                    ),
                )
                if response and response.text:
                    return clean_telegram_text(response.text)
            except Exception as e:
                print(f"Model {model_name} failed: {e}", flush=True)
                time.sleep(8 * round_idx)
    return ""


def call_gemini(title: str, guide: str, data_block: str) -> str:
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
        rules = (
        "1) 확인된 모든 오늘 경기를 단 한 경기도 빠뜨리지 말고 순서대로 전부 분석해 줘 (중간 생략 및 분석 거부 절대 금지).\n"
        "2) 텔레그램 메시지용이므로 **, *, ###, --- 같은 마크다운 기호는 절대 쓰지 마.\n"
        "3) 모든 팀명은 반드시 한국어 풀네임으로 적고 (홈) / (원정)을 꼭 표시해 줘. 빨간색(🔴, 🟥) 이모지는 절대 쓰지 마.\n"
        "4) [핵심 베팅 전략 - 정배/일반승 편식 금지]:\n"
        "   • 1.4배 이하 저배당 일반승만 기계적으로 추천하지 마. 마이너스 핸디캡(마핸), 플러스 핸디캡(플핸), 언더/오버, 그리고 과감한 역배(업셋)까지 모두 검토해서 가장 밸류가 높은 픽을 골라.\n"
        "   • 강팀의 백투백·원정 피로·결장 변수나 과도한 핸디캡 라인이 보이면 주저 없이 약팀 플핸(+핸디)이나 역배승을 추천해.\n"
        "   • 전체 경기 추천픽이 일반승이나 정배 한쪽으로만 쏠리지 말고 핸디캡(마핸/플핸)·언오버·역배가 경기 성격에 맞게 골고루 섞이도록 선정해.\n"
        "5) 각 경기마다 아래 양식대로 2~3문장의 핵심 분석 근거와 최종 추천픽 딱 1개만 작성해 줘:\n"
        "   [종목 | 한국시간] 홈팀(홈) vs 원정팀(원정) | 기준점\n"
        "   • 분석 근거: 양 팀 시즌 성적, 홈/원정 유불리, 백투백·부상 변수, 그리고 왜 이 픽(플핸/마핸/언오버/역배)이 가장 밸류가 높은지 2~3문장으로 명확하게 설명\n"
        "   🌿 최종 추천픽: (종합 분석 후 가장 밸류 높은 딱 1개만 선정)\n"
        "6) 맨 마지막에 가장 확실한 2폴더 추천 조합 1개를 덧붙여 줘."
            rules = """
[분석 및 추천픽 필수 규칙]
1. 한 가지 베팅 유형(언오버만, 또는 일반승만)으로 쏠리는 것을 절대 금지합니다.
2. 추천픽 배분 비율(필수):
   - 분석 경기가 3경기 이상이면: [일반승(정배 또는 역배)], [핸디캡(마핸 또는 플핸)], [언오버(오버 또는 언더)] 3가지 유형이 각각 최소 1경기 이상 반드시 포함되어야 합니다.
   - 분석 경기가 2경기 이하이면: 모든 경기를 같은 유형으로 추천하지 말고 서로 다른 유형(예: 1경기 핸디캡 + 1경기 언오버)으로 섞으세요.
3. 정배 일반승 남발 금지: 배당 메리트가 낮은 단순 정배 일반승 대신, 전력 차가 크면 마핸(-핸디), 접전이나 이변 가능성이 보이면 플핸(+핸디)이나 역배 일반승을 적극 활용하세요.
4. 경기당 추천픽은 반드시 '🌿 최종 추천픽:' 딱 1개만 제시하고, 유형이 바로 보이게 적으세요.
   - 표기 예시: '수원 케이티 -3.5 핸디승 (마핸)' / '삼성생명 +5.5 핸디승 (플핸)' / '흥국생명 승 (역배 일반승)' / '기준점 160.5 오버'
5. 최종 2폴더 추천 조합도 가능하면 서로 다른 유형(예: 1폴더 핸디캡 또는 일반승 + 2폴더 언오버)으로 섞어서 구성하세요.
6. 모든 팀명은 100% 한국어 정식 명칭을 사용하고 (홈)/(원정)과 한국시간(KST)을 표시하세요. 마크다운 기호(*, **, #, ---)와 빨간색 이모지(🔴, 🟥)는 절대 쓰지 마세요.
"""

    )
    )
    prompt = f"{title}\n{guide}\n\n{rules}\n\n[경기 데이터]\n{data_block}"
    res = raw_gemini_call(sys_inst, prompt)
    if res:
        return res
    return f"{title}\n(일시적 AI 서버 혼잡으로 실시간 수집된 경기 및 기준점 원본을 먼저 전달합니다)\n\n{data_block}"


def grade_previous_picks(now_kst: datetime.datetime, chat_id: str) -> None:
    history = load_history()
    pending = [item for item in history if item.get("status") == "pending"]
    if not pending:
        return

    scores_text = fetch_finished_scores(now_kst)
    dom_news = fetch_google_news_rss(f"{now_kst.month}월 {now_kst.day}일 프로농구 프로배구 경기 결과 스코어", max_items=8)
    combined_scores = f"{scores_text}\n\n[국내 경기 결과 뉴스]\n{dom_news}".strip()
    if not combined_scores:
        return

    picks_summary = "\n\n".join(
        f"[{p['date']} | {p['slot']}]\n{p['picks_text']}" for p in pending
    )
    sys_inst = (
        "당신은 스포츠 경기 결과 채점관입니다. "
        "제공된 [종료된 경기 최종 스코어]와 [이전 추천픽 목록]을 대조하여, 실제로 경기가 종료되어 최종 스코어가 확인된 경기만 정확히 채점하세요. "
        "아직 경기 결과가 확인되지 않은 경기는 채점하지 마세요. 마크다운 기호(*, **, #)는 절대 쓰지 마세요."
    )
    prompt = (
        "아래 [이전 추천픽 목록] 중 [종료된 경기 최종 스코어]에서 결과가 확인된 경기만 골라서 채점해 줘.\n"
        "양식:\n"
        "📊 [경기 종료 자동 채점 결과]\n"
        "• 홈팀 vs 원정팀 (최종 스코어) | 추천픽 -> 적중⭕ 또는 낙첨❌\n"
        "마지막 줄에 총 전적(예: 5적중 2낙첨)을 요약해 줘. 결과가 하나도 확인되지 않았으면 정확히 'NONE'이라고만 답해 줘.\n\n"
        f"[이전 추천픽 목록]\n{picks_summary}\n\n"
        f"[종료된 경기 최종 스코어]\n{combined_scores}"
    )
    graded = raw_gemini_call(sys_inst, prompt)
    if graded and graded.strip() != "NONE" and ("적중⭕" in graded or "낙첨❌" in graded):
        send_telegram_message(graded, chat_id)
        for item in history:
            if item.get("status") == "pending":
                item["status"] = "graded"
        save_history(history)


def record_picks_to_history(slot: str, date_str: str, analysis_text: str) -> None:
    lines = [ln.strip() for ln in analysis_text.splitlines() if ("vs" in ln or "최종 추천픽" in ln)]
    if not lines:
        return
    history = load_history()
    history.append({
        "date": date_str,
        "slot": slot,
        "status": "pending",
        "picks_text": "\n".join(lines)[:3500],
    })
    save_history(history)


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
        record_picks_to_history("NBA", date_str, text)

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
        record_picks_to_history("NHL", date_str, text)


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
    record_picks_to_history("국내경기", date_str, text)


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
    record_picks_to_history("해외축구", date_str, text)


def main():
    kst = pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(kst)
    date_str = now_kst.strftime("%Y-%m-%d (%a)")
    slot = detect_current_slot(now_kst)
    chat_id = resolve_telegram_chat_id()
    print(f"Running slot: {slot} -> chat_id: {chat_id}", flush=True)

    # 1. 이전 경기들 중 종료된 경기부터 적중⭕/낙첨❌ 자동 채점 후 전송
    grade_previous_picks(now_kst, chat_id)

    # 2. 이번 시간대 새 경기 분석 및 추천픽 저장
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
