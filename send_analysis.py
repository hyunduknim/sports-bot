python
import os
import json
import re
import time
import datetime
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from google import genai
from google.genai import types

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
HISTORY_FILE = "picks_history.json"

KST = datetime.timezone(datetime.timedelta(hours=9))

MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-4-hydrogen",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
]


def load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(history: list) -> None:
    try:
        meta_items = [x for x in history if isinstance(x, dict) and x.get("meta") == "chat_id"][:1]
        pick_items = [x for x in history if isinstance(x, dict) and x.get("meta") != "chat_id"][-45:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(meta_items + pick_items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"History save error: {e}", flush=True)


def already_sent(date_str: str, slot: str) -> bool:
    history = load_history()
    return any(
        isinstance(x, dict) and x.get("date") == date_str and x.get("slot") == slot
        for x in history
    )


def resolve_telegram_chat_id() -> str:
    bot_id = TELEGRAM_BOT_TOKEN.split(":")[0].strip() if ":" in TELEGRAM_BOT_TOKEN else ""
    history = load_history()
    for item in history:
        if isinstance(item, dict) and item.get("meta") == "chat_id":
            cid = str(item.get("chat_id", "")).strip()
            if cid and cid != bot_id:
                return cid

    if TELEGRAM_BOT_TOKEN:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                timeout=10,
            )
            if r.ok:
                for item in reversed(r.json().get("result", [])):
                    msg = item.get("message") or item.get("edited_message") or {}
                    chat = msg.get("chat", {})
                    frm = msg.get("from", {})
                    cid = str(chat.get("id", "")).strip()
                    if cid and cid != bot_id and not frm.get("is_bot", False) and chat.get("type") == "private":
                        history.insert(0, {"meta": "chat_id", "chat_id": cid})
                        save_history(history)
                        print(f"Resolved human chat_id: {cid}", flush=True)
                        return cid
        except Exception as e:
            print(f"getUpdates error: {e}", flush=True)

    print("No human chat_id found. Send /start to @hyunduk_sportsbot first.", flush=True)
    return ""


def clean_telegram_text(text: str) -> str:
    text = text.replace("**", "").replace("*", "").replace("###", "").replace("##", "").replace("#", "")
    text = text.replace("---", "")
    text = text.replace("🔴", "🌿").replace("🟥", "🌿")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def send_telegram_message(text: str, chat_id: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not text or not chat_id:
        return
    cleaned = clean_telegram_text(text)
    paragraphs = cleaned.split("\n\n")
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
                    chunks.append(p[i : i + 3800])
                current = ""
    if current:
        chunks.append(current)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in chunks:
        try:
            r = requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=15)
            if not r.ok:
                print(f"Telegram error: {r.status_code} {r.text}", flush=True)
            time.sleep(0.5)
        except Exception as e:
            print(f"Telegram send exception: {e}", flush=True)


def fetch_espn_window(sport: str, league: str, start_kst: datetime.datetime, end_kst: datetime.datetime) -> str:
    dates = set()
    cur = start_kst - datetime.timedelta(days=1)
    while cur.date() <= (end_kst + datetime.timedelta(days=1)).date():
        dates.add(cur.strftime("%Y%m%d"))
        cur += datetime.timedelta(days=1)

    events_out = []
    seen_ids = set()
    for d in sorted(dates):
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={d}"
        try:
            r = requests.get(url, timeout=15)
            if not r.ok:
                continue
            for ev in r.json().get("events", []):
                ev_id = ev.get("id")
                if ev_id in seen_ids:
                    continue
                date_str = ev.get("date", "")
                if not date_str:
                    continue
                dt_utc = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                dt_kst = dt_utc.astimezone(KST)
                if not (start_kst <= dt_kst <= end_kst):
                    continue
                seen_ids.add(ev_id)

                comp = (ev.get("competitions") or [{}])[0]
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                h_name = home.get("team", {}).get("displayName", "Home")
                a_name = away.get("team", {}).get("displayName", "Away")
                h_rec = (home.get("records") or [{}])[0].get("summary", "")
                a_rec = (away.get("records") or [{}])[0].get("summary", "")

                odds_info = ""
                odds_list = comp.get("odds") or []
                if odds_list:
                    o = odds_list[0]
                    details = o.get("details", "")
                    ou = o.get("overUnder", "")
                    odds_info = f" | 배당라인: {details}, 기준점(O/U): {ou}"

                events_out.append(
                    f"- [{dt_kst.strftime('%m/%d %H:%M')} KST] {h_name}(홈, {h_rec}) vs {a_name}(원정, {a_rec}){odds_info}"
                )
        except Exception as e:
            print(f"ESPN fetch error ({league} {d}): {e}", flush=True)

    return "\n".join(events_out)


def fetch_finished_scores(now_kst: datetime.datetime) -> str:
    leagues = [
        ("basketball", "nba", "NBA"),
        ("hockey", "nhl", "NHL"),
        ("soccer", "eng.1", "EPL"),
        ("soccer", "esp.1", "라리가"),
        ("soccer", "ita.1", "세리에A"),
        ("soccer", "ger.1", "분데스리가"),
        ("soccer", "fra.1", "리그1"),
        ("soccer", "uefa.champions", "챔피언스리그"),
        ("soccer", "kor.1", "K리그1"),
        ("soccer", "jpn.1", "J리그"),
        ("soccer", "afc.champions", "아챔"),
    ]
    dates = [
        (now_kst - datetime.timedelta(days=1)).strftime("%Y%m%d"),
        now_kst.strftime("%Y%m%d"),
    ]
    out = []
    seen = set()
    for sport, league, label in leagues:
        for d in dates:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={d}"
            try:
                r = requests.get(url, timeout=12)
                if not r.ok:
                    continue
                for ev in r.json().get("events", []):
                    ev_id = ev.get("id")
                    if ev_id in seen:
                        continue
                    status = ev.get("status", {}).get("type", {}).get("completed", False)
                    if not status:
                        continue
                    seen.add(ev_id)
                    comp = (ev.get("competitions") or [{}])[0]
                    competitors = comp.get("competitors", [])
                    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                    h_name = home.get("team", {}).get("displayName", "")
                    a_name = away.get("team", {}).get("displayName", "")
                    h_score = home.get("score", "")
                    a_score = away.get("score", "")
                    out.append(f"[{label}] {h_name}(홈) {h_score} : {a_score} {a_name}(원정)")
            except Exception:
                continue
    return "\n".join(out)


def fetch_via_jina(target_url: str, max_chars: int = 40000) -> str:
    try:
        r = requests.get(
            f"https://r.jina.ai/{target_url}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=25,
        )
        if r.ok and r.text:
            return r.text[:max_chars]
    except Exception:
        pass
    return ""


def fetch_wisetoto_today(now_kst: datetime.datetime) -> str:
    today_mmdd = f"{now_kst.month:02d}.{now_kst.day:02d}"
    for target in ["https://mw.wisetoto.com/", "https://www.wisetoto.com/"]:
        raw = fetch_via_jina(target, max_chars=40000)
        if not raw or today_mmdd not in raw:
            continue
        lines = raw.splitlines()
        matched = []
        i = 0
        while i < len(lines):
            if today_mmdd in lines[i]:
                block = " | ".join(ln.strip() for ln in lines[i : i + 6] if ln.strip())
                matched.append(block)
                i += 4
            else:
                i += 1
        if matched:
            return "\n".join(matched[:80])
    return ""


def fetch_google_news_rss(query: str, max_items: int = 8) -> str:
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        r = requests.get(url, timeout=15)
        if not r.ok:
            return ""
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item")[:max_items]:
            title = item.findtext("title", "").strip()
            pub = item.findtext("pubDate", "").strip()
            if title:
                items.append(f"- {title} ({pub})")
        return "\n".join(items)
    except Exception:
        return ""


def raw_gemini_call(sys_inst: str, prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing")
    client = genai.Client(api_key=GEMINI_API_KEY)
    last_err = None
    for round_idx in range(1, 4):
        for model_name in MODELS:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=sys_inst,
                        temperature=0.3,
                    ),
                )
                if response and response.text:
                    return clean_telegram_text(response.text)
            except Exception as e:
                last_err = e
                print(f"Round {round_idx} | Model {model_name} failed: {e}", flush=True)
                time.sleep(8 * round_idx)
    print(f"All models failed. Last error: {last_err}", flush=True)
    return ""


def call_gemini(title: str, guide: str, data_block: str) -> str:
    sys_inst = (
        "당신은 한국인 스포츠 베팅 전문 애널리스트입니다. "
        "반드시 100% 한국어로만 작성하고 영어 문장이나 영어 약어 팀명(HOU, MIA 등)을 절대 쓰지 마세요. "
        "마크다운 기호(*, **, #)와 빨간색 이모지(🔴, 🟥)를 절대 사용하지 마세요. "
        "절대로 [경기 데이터]에 없는 가상의 경기나 다른 날짜의 경기를 지어내지 마세요. 오직 오늘 실제로 열리는 것이 확인된 경기만 분석하세요."
    )
    rules = """
1) [경기 데이터]에서 오늘 날짜(KST)로 실제 열리는 것이 확인된 경기만 단 한 경기도 빠뜨리지 말고 순서대로 분석해 줘. (데이터에 없는 종목이나 없는 경기는 절대 지어내지 마!)
2) 텔레그램 메시지용이므로 *, **, #, --- 같은 마크다운 기호와 빨간색 이모지(🔴, 🟥)는 절대 쓰지 마.
3) 모든 팀명은 반드시 한국어 풀네임으로 적고 (홈) / (원정)과 한국시간(KST)을 꼭 표시해 줘.
4) [핵심 베팅 전략 - 단일 유형 쏠림 절대 금지]
- 하루 전체 추천픽이 언오버만, 또는 일반승만으로 몰리면 안 됩니다.
- 분석 경기가 3경기 이상이면 [일반승(정배 또는 역배)], [핸디캡(마핸 또는 플핸)], [언오버(오버 또는 언더)] 3가지 유형이 각각 최소 1경기 이상 반드시 포함되어야 합니다.
- 분석 경기가 2경기 이하이면 서로 다른 유형(예: 1경기 핸디캡 + 1경기 언오버)으로 섞으세요.
- 1.4배 이하 저배당 일반승만 기계적으로 추천하지 말고, 전력 차가 크면 마핸(-핸디), 접전이나 이변 가능성이 보이면 플핸(+핸디)이나 역배 일반승을 적극 활용하세요.
5) 각 경기마다 아래 양식대로 2~3문장의 핵심 분석 근거와 최종 추천픽 딱 1개만 작성해 줘.
[종목 | 한국시간] 홈팀(홈) vs 원정팀(원정) | 배당/기준점 정보
• 분석 근거: 양 팀 시즌 성적, 홈/원정 강약점, 백투백·부상 변수, 그리고 왜 이 픽(일반승/마핸/플핸/언오버/역배)이 가장 밸류가 높은지 2~3문장으로 명확하게 설명
🌿 최종 추천픽: (예: '수원 케이티 -3.5 핸디승 (마핸)', '삼성생명 +5.5 핸디승 (플핸)', '흥국생명 승 (역배 일반승)', '기준점 160.5 오버' 중 딱 1개만)
6) 2경기 이상일 때만 맨 마지막에 가장 확실한 2폴더 추천 조합 1개를 덧붙여 줘 (가능하면 핸디캡/일반승 + 언오버처럼 서로 다른 유형으로 구성).
"""
    prompt = f"{title}\n{guide}\n\n{rules}\n\n[경기 데이터]\n{data_block}"
    res = raw_gemini_call(sys_inst, prompt)
    if res:
        return res
    return f"{title}\n(일시적 AI 서버 혼잡으로 실시간 수집된 경기 및 기준점 원본을 먼저 전달합니다)\n\n{data_block}"


def grade_previous_picks(now_kst: datetime.datetime, chat_id: str) -> None:
    if not chat_id:
        return
    history = load_history()
    pending = [item for item in history if item.get("status") == "pending"]
    if not pending:
        return

    scores_text = fetch_finished_scores(now_kst)
    dom_news = fetch_google_news_rss(f"{now_kst.month}월 {now_kst.day}일 프로농구 프로배구 경기 결과 스코어", max_items=10)
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
    history = load_history()
    history.append(
        {
            "date": date_str,
            "slot": slot,
            "status": "pending" if lines else "empty",
            "picks_text": "\n".join(lines),
        }
    )
    save_history(history)


def run_nba_nhl(now_kst: datetime.datetime, date_str: str, chat_id: str) -> None:
    start_kst = now_kst.replace(hour=7, minute=0, second=0, microsecond=0)
    end_kst = now_kst.replace(hour=16, minute=0, second=0, microsecond=0)

    if not already_sent(date_str, "NBA"):
        nba_data = fetch_espn_window("basketball", "nba", start_kst, end_kst)
        if nba_data:
            nba_res = call_gemini(
                f"🏀 [{date_str}] 오늘의 NBA 전 경기 분석",
                "피나클/해외 정배·핸디캡·언오버 기준점 및 국내 프로토 환산 관점으로 오늘(한국시간 07:00~16:00) 열리는 NBA 경기만 분석하세요.",
                nba_data,
            )
            send_telegram_message(nba_res, chat_id)
            record_picks_to_history("NBA", date_str, nba_res)
            time.sleep(12)

    if not already_sent(date_str, "NHL"):
        nhl_data = fetch_espn_window("hockey", "nhl", start_kst, end_kst)
        if nhl_data:
            nhl_res = call_gemini(
                f"🏒 [{date_str}] 오늘의 NHL 전 경기 분석",
                "피나클/해외 배당·핸디캡(퍽라인)·언오버 기준으로 오늘(한국시간 07:00~16:00) 열리는 NHL 경기만 분석하세요.",
                nhl_data,
            )
            send_telegram_message(nhl_res, chat_id)
            record_picks_to_history("NHL", date_str, nhl_res)


def run_overseas_soccer(now_kst: datetime.datetime, date_str: str, chat_id: str, is_weekend: bool) -> None:
    if already_sent(date_str, "해외축구"):
        return
    if is_weekend:
        start_kst = now_kst.replace(hour=20, minute=0, second=0, microsecond=0)
        end_kst = (now_kst + datetime.timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        title = f"⚽ [{date_str}] 주말 밤 해외축구 주요 경기 분석"
    else:
        start_kst = now_kst.replace(hour=1, minute=0, second=0, microsecond=0)
        end_kst = now_kst.replace(hour=6, minute=30, second=0, microsecond=0)
        title = f"⚽ [{date_str}] 평일 새벽 해외축구 주요 경기 분석"

    leagues = [
        ("soccer", "eng.1", "EPL"),
        ("soccer", "esp.1", "라리가"),
        ("soccer", "ita.1", "세리에A"),
        ("soccer", "ger.1", "분데스리가"),
        ("soccer", "fra.1", "리그1"),
        ("soccer", "uefa.champions", "챔피언스리그"),
    ]
    blocks = []
    for sp, lg, label in leagues:
        d = fetch_espn_window(sp, lg, start_kst, end_kst)
        if d:
            blocks.append(f"[{label}]\n{d}")
    if not blocks:
        return

    res = call_gemini(
        title,
        "피나클/해외 주요 리그 배당·핸디캡·언오버 기준으로 해당 시간대 해외축구 경기들을 분석하세요.",
        "\n\n".join(blocks),
    )
    send_telegram_message(res, chat_id)
    record_picks_to_history("해외축구", date_str, res)


def run_domestic(now_kst: datetime.datetime, date_str: str, chat_id: str) -> None:
    if already_sent(date_str, "국내/아시아"):
        return
    start_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    end_kst = now_kst.replace(hour=23, minute=59, second=59, microsecond=0)

    asia_blocks = []
    for sp, lg, label in [
        ("soccer", "kor.1", "K리그1"),
        ("soccer", "jpn.1", "J리그"),
        ("soccer", "afc.champions", "AFC 챔피언스리그"),
    ]:
        d = fetch_espn_window(sp, lg, start_kst, end_kst)
        if d:
            asia_blocks.append(f"[{label}]\n{d}")
    asia_text = "\n".join(asia_blocks) if asia_blocks else "오늘 열리는 K리그1/J리그/AFC 챔피언스리그 경기 없음."

    wisetoto_today = fetch_wisetoto_today(now_kst)
    kbl_news = fetch_google_news_rss(f"{now_kst.month}월 {now_kst.day}일 프로농구 프로배구 일정", max_items=8)

    data_block = (
        f"[오늘 날짜(KST): {date_str}]\n\n"
        f"[오늘({now_kst.month:02d}.{now_kst.day:02d}) 프로토 승부식 공식 대상경기 및 배당]\n{wisetoto_today or '프로토 목록 미확인'}\n\n"
        f"[ESPN 아시아축구(K리그/J리그/ACL) 실시간 일정]\n{asia_text}\n\n"
        f"[참고 뉴스]\n{kbl_news}"
    )
    res = call_gemini(
        f"🏟️ [{date_str}] 국내 스포츠 및 아시아축구 분석 (프로토 기준)",
        "위 [프로토 승부식 공식 대상경기 및 배당]과 [ESPN 아시아축구 실시간 일정]에서 오늘(KST) 실제로 열리는 국내 농구(KBL/WKBL), 배구(V리그), 아시아축구만 분석하세요. 오늘 일정에 없는 종목/경기는 절대 지어내지 마세요.",
        data_block,
    )
    send_telegram_message(res, chat_id)
    record_picks_to_history("국내/아시아", date_str, res)


def main() -> None:
    now_kst = datetime.datetime.now(KST)
    date_str = now_kst.strftime("%Y-%m-%d")
    chat_id = resolve_telegram_chat_id()
    if not chat_id:
        return

    grade_previous_picks(now_kst, chat_id)

    hour = now_kst.hour
    weekday = now_kst.weekday()  # 0=Mon ... 6=Sun
    is_weekend = weekday >= 5

    if 0 <= hour < 5:
        if not is_weekend:
            run_overseas_soccer(now_kst, date_str, chat_id, is_weekend=False)
    elif 6 <= hour < 11:
        run_nba_nhl(now_kst, date_str, chat_id)
    elif 12 <= hour < 15:
        if is_weekend:
            run_domestic(now_kst, date_str, chat_id)
    elif 16 <= hour < 19:
        run_domestic(now_kst, date_str, chat_id)
    elif 19 <= hour <= 23:
        if is_weekend:
            run_overseas_soccer(now_kst, date_str, chat_id, is_weekend=True)
        else:
            run_domestic(now_kst, date_str, chat_id)


if __name__ == "__main__":
    main()
```
