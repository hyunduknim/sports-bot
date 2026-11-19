import os
import io
import html
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
SPORTSGAMEODDS_KEY = os.environ.get("SPORTSGAMEODDS_KEY", "").strip().strip('"').strip("'")
STATSHAWK_KEY = os.environ.get("STATSHAWK_KEY", "").strip().strip('"').strip("'")
HISTORY_FILE = "picks_history.json"

KST = datetime.timezone(datetime.timedelta(hours=9))

MODELS = [
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
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


def format_telegram_html(text: str) -> str:
    cleaned = clean_telegram_text(text)
    escaped = html.escape(cleaned)
    out_lines = []
    for ln in escaped.splitlines():
        s = ln.strip()
        if not s:
            out_lines.append("")
            continue
        if s.startswith(("🏀", "🏒", "⚽", "🏟️", "📊")):
            out_lines.append(f"<b>{s}</b>")
        elif s.startswith("[") and "vs" in s:
            out_lines.append(f"<b>{s}</b>")
        elif "최종 추천픽" in s:
            out_lines.append(f"<blockquote><b>{s}</b></blockquote>")
        elif s.startswith(("🎯", "💎")):
            out_lines.append(f"<b>{s}</b>")
        elif s.startswith(("- 1폴더:", "- 2폴더:", "1폴더:", "2폴더:")):
            out_lines.append(f"<code>{s}</code>")
        else:
            out_lines.append(ln)
    return "\n".join(out_lines)


def send_telegram_message(text: str, chat_id: str, use_html: bool = True) -> None:
    if not TELEGRAM_BOT_TOKEN or not text or not chat_id:
        return
    formatted = format_telegram_html(text) if use_html else clean_telegram_text(text)
    paragraphs = formatted.split("\n\n")
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 2 <= 3700:
            current = f"{current}\n\n{p}".strip() if current else p
        else:
            if current:
                chunks.append(current)
            if len(p) <= 3700:
                current = p
            else:
                for i in range(0, len(p), 3700):
                    chunks.append(p[i : i + 3700])
                current = ""
    if current:
        chunks.append(current)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in chunks:
        try:
            payload = {"chat_id": chat_id, "text": chunk}
            if use_html:
                payload["parse_mode"] = "HTML"
            r = requests.post(url, json=payload, timeout=15)
            if not r.ok and use_html:
                plain_chunk = re.sub(r"<[^>]+>", "", chunk)
                plain_chunk = html.unescape(plain_chunk)
                r = requests.post(url, json={"chat_id": chat_id, "text": plain_chunk}, timeout=15)
            if not r.ok:
                print(f"Telegram error: {r.status_code} {r.text}", flush=True)
            time.sleep(0.5)
        except Exception as e:
            print(f"Telegram send exception: {e}", flush=True)


def send_telegram_photo(img_bytes: bytes, caption: str, chat_id: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not img_bytes or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        files = {"photo": ("analysis_chart.png", img_bytes, "image/png")}
        data = {"chat_id": chat_id, "caption": clean_telegram_text(caption)[:900]}
        r = requests.post(url, data=data, files=files, timeout=25)
        if not r.ok:
            print(f"Telegram photo error: {r.status_code} {r.text}", flush=True)
        time.sleep(0.5)
    except Exception as e:
        print(f"Telegram photo exception: {e}", flush=True)


def build_summary_card_image(title: str, chart_data: dict) -> bytes:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        from matplotlib.patches import Patch
    except Exception as e:
        print(f"matplotlib unavailable: {e}", flush=True)
        return b""

    font_paths = [
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    chosen_font = None
    for fp in font_paths:
        if os.path.exists(fp):
            font_manager.fontManager.addfont(fp)
            chosen_font = font_manager.FontProperties(fname=fp).get_name()
            break
    if chosen_font:
        plt.rcParams["font.family"] = chosen_font
    plt.rcParams["axes.unicode_minus"] = False

    picks = chart_data.get("picks") or []
    combos = chart_data.get("combos") or []
    if not picks and not combos:
        return b""

    bg_color = "#0F172A"
    card_color = "#1E293B"
    text_color = "#F8FAFC"
    sub_color = "#CBD5E1"
    type_colors = {
        "일반승": "#10B981",
        "마핸": "#0EA5E9",
        "플핸": "#A855F7",
        "역배": "#EC4899",
        "언오버": "#F59E0B",
    }

    n_picks = max(len(picks), 1)
    n_combos = len(combos)
    fig_h = max(6.0, 2.2 + n_picks * 0.58 + (2.8 if n_combos else 0.4))
    fig = plt.figure(figsize=(10.5, fig_h), dpi=180, facecolor=bg_color)

    if n_combos:
        gs = fig.add_gridspec(2, 1, height_ratios=[max(2.6, n_picks * 0.65), 2.5], hspace=0.35)
        ax_bar = fig.add_subplot(gs[0])
        ax_tbl = fig.add_subplot(gs[1])
    else:
        ax_bar = fig.add_subplot(111)
        ax_tbl = None

    clean_title = re.sub(r"[^\w\s\[\]\-\(\)\.\:\|가-힣]", "", title).strip()
    fig.suptitle(clean_title, color=text_color, fontsize=15, fontweight="bold", y=0.97)

    ax_bar.set_facecolor(card_color)
    if picks:
        labels = []
        scores = []
        colors = []
        pick_texts = []
        for item in picks[:12]:
            match_lbl = str(item.get("match", ""))[:22]
            pick_lbl = str(item.get("pick", ""))[:24]
            ptype = str(item.get("type", "일반승"))
            conf = int(item.get("confidence", 70) or 70)
            conf = max(45, min(95, conf))
            labels.append(match_lbl)
            scores.append(conf)
            pick_texts.append(f"{pick_lbl} ({conf}%)")
            c = "#10B981"
            for k, v in type_colors.items():
                if k in ptype or k in pick_lbl:
                    c = v
                    break
            colors.append(c)

        y_pos = list(range(len(labels)))[::-1]
        bars = ax_bar.barh(y_pos, scores, color=colors, height=0.62, edgecolor="#334155", linewidth=0.8)
        ax_bar.set_yticks(y_pos)
        ax_bar.set_yticklabels(labels, color=text_color, fontsize=9.5, fontweight="bold")
        ax_bar.set_xlim(0, 118)
        ax_bar.set_xlabel("AI 밸류 신뢰도 (%)", color=sub_color, fontsize=9.5)
        ax_bar.tick_params(axis="x", colors=sub_color, labelsize=8.5)
        ax_bar.grid(axis="x", linestyle="--", alpha=0.2, color="#94A3B8")
        for spine in ("top", "right"):
            ax_bar.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax_bar.spines[spine].set_color("#475569")

        for bar, ptxt in zip(bars, pick_texts):
            w = bar.get_width()
            ax_bar.text(
                w + 1.5,
                bar.get_y() + bar.get_height() / 2,
                ptxt,
                va="center",
                ha="left",
                color=text_color,
                fontsize=9,
                fontweight="bold",
            )

        legend_handles = [Patch(facecolor=v, label=k) for k, v in type_colors.items()]
        ax_bar.legend(
            handles=legend_handles,
            loc="upper right",
            ncol=5,
            frameon=False,
            fontsize=8.5,
            labelcolor=sub_color,
            bbox_to_anchor=(1.0, 1.15),
        )

    if ax_tbl is not None and combos:
        ax_tbl.set_facecolor(bg_color)
        ax_tbl.axis("off")
        ax_tbl.set_title("추천 2폴더 조합표", color="#38BDF8", fontsize=12.5, fontweight="bold", pad=10, loc="left")

        col_labels = ["조합 구분", "1폴더 픽", "2폴더 픽", "핵심 포인트"]
        cell_text = []
        row_colors = []
        for idx, cb in enumerate(combos[:3]):
            cname = str(cb.get("name", f"조합 {idx+1}"))[:14]
            leg1 = str(cb.get("leg1", ""))[:26]
            leg2 = str(cb.get("leg2", ""))[:26]
            note = str(cb.get("note", ""))[:30]
            cell_text.append([cname, leg1, leg2, note])
            row_colors.append("#1E293B" if idx % 2 == 0 else "#162032")

        table = ax_tbl.table(
            cellText=cell_text,
            colLabels=col_labels,
            cellLoc="center",
            loc="center",
            colWidths=[0.17, 0.28, 0.28, 0.27],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(9.2)
        table.scale(1.0, 2.2)

        for (row, col), cell in table.get_celld().items():
            cell.set_edgecolor("#334155")
            if row == 0:
                cell.set_facecolor("#0284C7")
                cell.set_text_props(color="#FFFFFF", fontweight="bold")
            else:
                cell.set_facecolor(row_colors[row - 1])
                if col == 0:
                    badge_col = "#10B981" if row == 1 else "#A855F7"
                    cell.set_text_props(color=badge_col, fontweight="bold")
                elif col in (1, 2):
                    cell.set_text_props(color="#FDE047", fontweight="bold")
                else:
                    cell.set_text_props(color=sub_color)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", facecolor=bg_color, bbox_inches="tight", pad_inches=0.35)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def extract_chart_json(raw_text: str) -> tuple[str, dict]:
    m = re.search(r"===CHART_JSON_START===\s*(\{.*?\})\s*===CHART_JSON_END===", raw_text, re.DOTALL)
    if not m:
        return raw_text, {}
    clean_body = (raw_text[: m.start()] + raw_text[m.end() :]).strip()
    try:
        parsed = json.loads(m.group(1))
        return clean_body, parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        print(f"Chart JSON parse error: {e}", flush=True)
        return clean_body, {}


def fetch_sportsgameodds(league_ids: str, start_kst: datetime.datetime, end_kst: datetime.datetime) -> str:
    if not SPORTSGAMEODDS_KEY:
        return ""
    url = "https://api.sportsgameodds.com/v2/events"
    params = {
        "leagueID": league_ids,
        "oddsAvailable": "true",
        "startsAfter": start_kst.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "startsBefore": end_kst.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 30,
    }
    try:
        r = requests.get(url, headers={"x-api-key": SPORTSGAMEODDS_KEY}, params=params, timeout=15)
        if not r.ok:
            print(f"SportsGameOdds status {r.status_code}: {r.text[:200]}", flush=True)
            return ""
        events = r.json().get("data", [])
        lines = []
        for ev in events:
            teams = ev.get("teams", {})
            h_name = (teams.get("home", {}).get("names", {}) or {}).get("long", "Home")
            a_name = (teams.get("away", {}).get("names", {}) or {}).get("long", "Away")
            starts_at = (ev.get("status", {}) or {}).get("startsAt", "")
            odds = ev.get("odds", {}) or {}
            parts = []
            for odd_key, label in [
                ("points-home-game-ml-home", "홈승"),
                ("points-away-game-ml-away", "원정승"),
                ("points-home-game-sp-home", "홈핸디"),
                ("points-away-game-sp-away", "원정핸디"),
                ("points-all-game-ou-over", "오버"),
                ("points-all-game-ou-under", "언더"),
            ]:
                o = odds.get(odd_key)
                if not isinstance(o, dict):
                    continue
                pinn = (o.get("byBookmaker") or {}).get("pinnacle")
                src = pinn if isinstance(pinn, dict) else o
                val = src.get("odds") or src.get("bookOdds") or src.get("fairOdds") or ""
                line = src.get("spread") or src.get("overUnder") or o.get("bookSpread") or o.get("bookOverUnder") or ""
                tag = "피나클" if pinn else "시장컨센서스"
                if val or line:
                    parts.append(f"{label}({tag}:{line} {val})".strip())
            if parts:
                lines.append(f"- [{starts_at}] {h_name}(홈) vs {a_name}(원정) | " + ", ".join(parts))
        return "\n".join(lines)
    except Exception as e:
        print(f"SportsGameOdds error: {e}", flush=True)
        return ""


def fetch_statshawk_ping() -> str:
    if not STATSHAWK_KEY:
        return ""
    try:
        r = requests.get(
            "https://api.statshawk.ai/v1/competitions",
            headers={"X-API-Key": STATSHAWK_KEY},
            timeout=10,
        )
        if r.ok:
            return "StatsHawk API 연결 정상"
    except Exception:
        pass
    return ""


def fetch_espn_standings(sport: str, league: str) -> str:
    url = f"https://site.api.espn.com/apis/v2/sports/{sport}/{league}/standings"
    try:
        r = requests.get(url, timeout=15)
        if not r.ok:
            return ""
        data = r.json()
        rows = []
        for conf in data.get("children", []):
            entries = (conf.get("standings") or {}).get("entries", [])
            for entry in entries:
                team = (entry.get("team") or {}).get("displayName", "")
                stats_map = {s.get("name"): s.get("displayValue", "") for s in entry.get("stats", [])}
                rec = stats_map.get("overall", "")
                home_rec = stats_map.get("Home", "")
                road_rec = stats_map.get("Road", "")
                streak = stats_map.get("streak", "")
                l10 = stats_map.get("Last Ten Games", "")
                ppg = stats_map.get("avgPointsFor", "") or stats_map.get("pointsFor", "")
                oppg = stats_map.get("avgPointsAgainst", "") or stats_map.get("pointsAgainst", "")
                diff = stats_map.get("differential", "") or stats_map.get("pointDifferential", "")
                if team:
                    rows.append(
                        f"- {team}: 시즌 {rec} | 홈 {home_rec} / 원정 {road_rec} | 최근10 {l10} | 연속 {streak} | 득점 {ppg} / 실점 {oppg} (득실차 {diff})"
                    )
        return "\n".join(rows)
    except Exception as e:
        print(f"ESPN standings error ({league}): {e}", flush=True)
        return ""


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
                h_rec = ", ".join(
                    f"{rec.get('name', '')}:{rec.get('summary', '')}"
                    for rec in (home.get("records") or [])[:3]
                )
                a_rec = ", ".join(
                    f"{rec.get('name', '')}:{rec.get('summary', '')}"
                    for rec in (away.get("records") or [])[:3]
                )

                odds_info = ""
                odds_list = comp.get("odds") or []
                if odds_list:
                    o = odds_list[0]
                    details = o.get("details", "")
                    ou = o.get("overUnder", "")
                    odds_info = f" | ESPN라인: {details}, O/U: {ou}"

                events_out.append(
                    f"- [{dt_kst.strftime('%m/%d %H:%M')} KST] {h_name}(홈, {h_rec}) vs {a_name}(원정, {a_rec}){odds_info}"
                )
        except Exception as e:
            print(f"ESPN fetch error ({league} {d}): {e}", flush=True)

    return "\n".join(events_out)


def fetch_en_news_rss(query: str, max_items: int = 8) -> str:
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
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
        (now_kst - datetime.timedelta(days=2)).strftime("%Y%m%d"),
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


def fetch_wisetoto_by_mmdd(target_mmdd: str) -> str:
    for target in ["https://mw.wisetoto.com/", "https://www.wisetoto.com/"]:
        raw = fetch_via_jina(target, max_chars=45000)
        if not raw or target_mmdd not in raw:
            continue
        lines = raw.splitlines()
        matched = []
        i = 0
        while i < len(lines):
            if target_mmdd in lines[i]:
                block = " | ".join(ln.strip() for ln in lines[i : i + 6] if ln.strip())
                matched.append(block)
                i += 4
            else:
                i += 1
        if matched:
            return "\n".join(matched[:100])
    return ""


def fetch_wisetoto_today(now_kst: datetime.datetime) -> str:
    return fetch_wisetoto_by_mmdd(f"{now_kst.month:02d}.{now_kst.day:02d}")


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
                        temperature=0.35,
                    ),
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as e:
                last_err = e
                print(f"Round {round_idx} | Model {model_name} failed: {e}", flush=True)
                time.sleep(8 * round_idx)
    print(f"All models failed. Last error: {last_err}", flush=True)
    return ""


def call_gemini(title: str, guide: str, data_block: str, detailed_pro: bool = False) -> tuple[str, dict]:
    sys_inst = (
        "당신은 최상위 1% 프로 스포츠 베팅 퀀트·애널리스트입니다. "
        "반드시 100% 한국어로만 작성하고 영어 문장이나 영어 약어 팀명(HOU, MIA 등)을 절대 쓰지 마세요. "
        "마크다운 기호(*, **, #)와 빨간색 이모지(🔴, 🟥)를 절대 사용하지 마세요. "
        "절대로 [경기 데이터]에 없는 가상의 경기나 다른 날짜의 경기를 지어내지 마세요. 오직 오늘 실제로 열리는 것이 확인된 경기만 분석하세요."
    )
    rules = """
1) [경기 데이터]에서 오늘 날짜(KST)로 실제 열리는 것이 확인된 모든 경기를 빠짐없이 심층 분석하되, 항목별로 줄바꿈을 깔끔하게 정리하세요.
2) 텔레그램 메시지용이므로 *, **, #, --- 같은 마크다운 기호와 빨간색 이모지(🔴, 🟥)는 절대 쓰지 마세요.
3) 모든 팀명은 100% 한국어 풀네임으로 적고 (홈) / (원정)과 한국시간(KST)을 표시하세요.
4) [핵심 베팅 전략 - 정배 편중 절대 금지 & 역배·플핸 적극 활용]
- 5경기 이상이면 최소 2경기 이상, 3~4경기면 최소 1경기 이상은 반드시 [역배 일반승] 또는 [플핸(+핸디승)]을 선정하세요.
- 전체 추천픽에 [일반승(정배/역배)], [핸디캡(마핸/플핸)], [언오버(오버/언더)] 3가지 유형이 고르게 섞여야 합니다.
- 픽 유형 배지: 일반승/마핸은 🟢, 언오버는 🟡, 역배/플핸은 🟣 을 최종 추천픽 끝에 붙이세요.
5) 각 경기는 기존의 디테일한 심층 분석 항목을 모두 유지하되, 항목마다 1줄씩 깔끔하게 끊어 가독성을 높이세요:
[종목 | 한국시간] 홈팀(홈) vs 원정팀(원정)
• 라인/배당: 피나클/해외 또는 프로토 핸디캡 및 언오버 기준점
• 핵심 지표: 양 팀 시즌·홈/원정 성적, 최근 10경기 흐름, 평균 득실점·득실차 비교
• 매치업 & 변수: 공수 페이스(템포), 백투백·원정 피로도, 부상/결장 뉴스 및 선발 골리(NHL) 등 승부처
• 밸류 근거: 왜 일반승 대신 이 라인(마핸/플핸/언오버/역배)이 가장 기대값이 높은지 핵심 정리
🌿 최종 추천픽: (예: 유타 재즈 +11.5 핸디승 (플핸) 🟣)

6) 맨 마지막에 아래 2개 조합을 깔끔하게 정리하세요 (2경기 이상일 때):
🎯 [조합 1 · 안정형 주력 2폴더]
- 1폴더: 팀명 + 추천픽
- 2폴더: 팀명 + 추천픽
• 포인트: 선정 이유 1줄

💎 [조합 2 · 고배당 밸류 2폴더]
- 1폴더: 팀명 + 추천픽 (역배 또는 플핸)
- 2폴더: 팀명 + 추천픽
• 포인트: 선정 이유 1줄

7) 답변 맨 마지막에는 반드시 아래 형식의 순수 JSON 블록을 덧붙여 차트와 컬러 조합표를 만들 수 있게 하세요 (JSON 안에는 주석이나 마크다운 금지):
===CHART_JSON_START===
{
  "picks": [
    {"match": "홈팀 vs 원정팀", "pick": "추천픽 요약", "type": "일반승|마핸|플핸|역배|언오버 중 하나", "confidence": 78}
  ],
  "combos": [
    {"name": "안정형 주력", "leg1": "1폴더 요약", "leg2": "2폴더 요약", "note": "핵심 포인트"},
    {"name": "고배당 밸류", "leg1": "1폴더 요약", "leg2": "2폴더 요약", "note": "핵심 포인트"}
  ]
}
===CHART_JSON_END===
"""
    prompt = f"{title}\n{guide}\n\n{rules}\n\n[경기 데이터]\n{data_block}"
    raw_res = raw_gemini_call(sys_inst, prompt)
    if raw_res:
        body, chart_json = extract_chart_json(raw_res)
        return clean_telegram_text(body), chart_json
    return f"{title}\n(일시적 AI 서버 혼잡으로 실시간 수집된 경기 및 기준점 원본을 먼저 전달합니다)\n\n{data_block}", {}


def deliver_analysis_with_visual(title: str, body: str, chart_json: dict, chat_id: str) -> None:
    send_telegram_message(body, chat_id, use_html=True)
    if chart_json:
        img_bytes = build_summary_card_image(title, chart_json)
        if img_bytes:
            send_telegram_photo(img_bytes, f"📊 {title} — 밸류 신뢰도 차트 & 컬러 조합표", chat_id)


def is_slot_ready_to_grade(item: dict, now_kst: datetime.datetime) -> bool:
    item_date = item.get("date", "")
    slot = item.get("slot", "")
    today_str = now_kst.strftime("%Y-%m-%d")
    if item_date < today_str:
        return True
    if slot in ("NBA", "NHL"):
        return now_kst.hour >= 16
    if slot == "국내/아시아":
        return now_kst.hour >= 22
    if slot == "해외축구":
        return now_kst.hour >= 9
    return True


def grade_previous_picks(now_kst: datetime.datetime, chat_id: str) -> None:
    if not chat_id:
        return
    history = load_history()
    ready_items = [
        item for item in history
        if isinstance(item, dict)
        and item.get("status") == "pending"
        and item.get("picks_text")
        and is_slot_ready_to_grade(item, now_kst)
    ]
    if not ready_items:
        return

    scores_text = fetch_finished_scores(now_kst)
    yest_kst = now_kst - datetime.timedelta(days=1)
    wt_yest = fetch_wisetoto_by_mmdd(f"{yest_kst.month:02d}.{yest_kst.day:02d}")
    wt_today = fetch_wisetoto_by_mmdd(f"{now_kst.month:02d}.{now_kst.day:02d}")
    dom_news_yest = fetch_google_news_rss(f"{yest_kst.month}월 {yest_kst.day}일 프로농구 프로배구 경기 결과", max_items=10)
    dom_news_today = fetch_google_news_rss(f"{now_kst.month}월 {now_kst.day}일 프로농구 프로배구 경기 결과", max_items=8)
    combined_scores = (
        f"{scores_text}\n\n"
        f"[와이즈토토 프로토 공식 경기 결과 ({yest_kst.month:02d}.{yest_kst.day:02d})]\n{wt_yest}\n\n"
        f"[와이즈토토 프로토 공식 경기 결과 ({now_kst.month:02d}.{now_kst.day:02d})]\n{wt_today}\n\n"
        f"[국내 경기 결과 뉴스 ({yest_kst.month}월 {yest_kst.day}일)]\n{dom_news_yest}\n\n"
        f"[국내 경기 결과 뉴스 ({now_kst.month}월 {now_kst.day}일)]\n{dom_news_today}"
    ).strip()
    if not combined_scores:
        return

    sys_inst = (
        "당신은 스포츠 경기 결과 채점관입니다. "
        "제공된 [종료된 경기 최종 스코어]와 [이전 추천픽 목록]을 대조하여, 해당 종목의 모든 종료 경기를 단 한 경기도 빠뜨리지 말고 정확히 채점하세요. "
        "마크다운 기호(*, **, #)는 절대 쓰지 마세요."
    )

    changed = False
    for item in ready_items:
        slot = item.get("slot", "")
        dt = item.get("date", "")
        picks_text = item.get("picks_text", "")
        prompt = (
            f"아래 [{dt} | {slot}] 추천픽 목록을 [종료된 경기 최종 스코어]와 대조해서, 결과가 확인된 경기를 빠짐없이 채점해 줘.\n"
            "양식:\n"
            f"📊 [{dt} {slot} 경기 종료 자동 채점 결과]\n"
            "• 홈팀 vs 원정팀 (최종 스코어) | 추천픽 -> 적중⭕ 또는 낙첨❌\n"
            "마지막 줄에 총 전적(예: 5적중 2낙첨)을 요약해 줘. 결과가 하나도 확인되지 않았으면 정확히 'NONE'이라고만 답해 줘.\n\n"
            f"[이전 추천픽 목록]\n{picks_text}\n\n"
            f"[종료된 경기 최종 스코어]\n{combined_scores}"
        )
        graded = raw_gemini_call(sys_inst, prompt)
        if graded and graded.strip() != "NONE" and ("적중⭕" in graded or "낙첨❌" in graded):
            send_telegram_message(clean_telegram_text(graded), chat_id, use_html=True)
            item["status"] = "graded"
            changed = True
            time.sleep(5)

    if changed:
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
    fetch_statshawk_ping()

    if not already_sent(date_str, "NBA"):
        nba_espn = fetch_espn_window("basketball", "nba", start_kst, end_kst)
        if nba_espn:
            nba_sgo = fetch_sportsgameodds("NBA", start_kst, end_kst)
            nba_standings = fetch_espn_standings("basketball", "nba")
            nba_inj = fetch_en_news_rss("NBA injury report lineup today", max_items=8)
            nba_block = f"[오늘의 NBA 경기 일정 및 기본 라인]\n{nba_espn}"
            if nba_sgo:
                nba_block += f"\n\n[SportsGameOdds 피나클/해외 실시간 배당]\n{nba_sgo}"
            if nba_standings:
                nba_block += f"\n\n[NBA 팀별 시즌·홈/원정·최근10·득실차 지표]\n{nba_standings}"
            if nba_inj:
                nba_block += f"\n\n[최신 NBA 부상/결장 뉴스]\n{nba_inj}"
            title = f"🏀 [{date_str}] 오늘의 NBA 전 경기 분석"
            nba_res, nba_chart = call_gemini(
                title,
                "피나클/해외 실시간 배당과 팀별 홈·원정 성적, 최근 10경기, 득실차, 부상 리포트를 종합해 핵심만 간결하게 분석하고 2폴더 조합 2세트 및 차트 JSON을 작성하세요.",
                nba_block,
                detailed_pro=True,
            )
            deliver_analysis_with_visual(title, nba_res, nba_chart, chat_id)
            record_picks_to_history("NBA", date_str, nba_res)
            time.sleep(12)

    if not already_sent(date_str, "NHL"):
        nhl_espn = fetch_espn_window("hockey", "nhl", start_kst, end_kst)
        if nhl_espn:
            nhl_sgo = fetch_sportsgameodds("NHL", start_kst, end_kst)
            nhl_standings = fetch_espn_standings("hockey", "nhl")
            nhl_news = fetch_en_news_rss("NHL starting goalies injury report today", max_items=8)
            nhl_block = f"[오늘의 NHL 경기 일정 및 기본 라인]\n{nhl_espn}"
            if nhl_sgo:
                nhl_block += f"\n\n[SportsGameOdds 피나클/해외 실시간 배당]\n{nhl_sgo}"
            if nhl_standings:
                nhl_block += f"\n\n[NHL 팀별 시즌·홈/원정·최근10·득실차 지표]\n{nhl_standings}"
            if nhl_news:
                nhl_block += f"\n\n[최신 NHL 선발 골리/부상 뉴스]\n{nhl_news}"
            title = f"🏒 [{date_str}] 오늘의 NHL 전 경기 분석"
            nhl_res, nhl_chart = call_gemini(
                title,
                "피나클/해외 배당·퍽라인·언오버와 팀별 홈/원정 성적, 최근 10경기, 득실차, 선발 골리 소식을 종합해 핵심만 간결하게 분석하고 2폴더 조합 2세트 및 차트 JSON을 작성하세요.",
                nhl_block,
                detailed_pro=True,
            )
            deliver_analysis_with_visual(title, nhl_res, nhl_chart, chat_id)
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

    sgo_soccer = fetch_sportsgameodds("EPL,LA_LIGA,SERIE_A,BUNDESLIGA,LIGUE_1,UEFA_CHAMPIONS_LEAGUE", start_kst, end_kst)
    data_block = "\n\n".join(blocks)
    if sgo_soccer:
        data_block += f"\n\n[SportsGameOdds 해외/피나클 배당]\n{sgo_soccer}"

    res, chart_json = call_gemini(
        title,
        "피나클/해외 주요 리그 배당·핸디캡·언오버 기준으로 해당 시간대 해외축구 경기들을 핵심만 간결하게 분석하고 2폴더 조합 2세트 및 차트 JSON을 작성하세요.",
        data_block,
        detailed_pro=True,
    )
    deliver_analysis_with_visual(title, res, chart_json, chat_id)
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
    title = f"🏟️ [{date_str}] 국내 스포츠 및 아시아축구 분석 (프로토 기준)"
    res, chart_json = call_gemini(
        title,
        "위 [프로토 승부식 공식 대상경기 및 배당]과 [ESPN 아시아축구 실시간 일정]에서 오늘(KST) 실제로 열리는 국내 농구(KBL/WKBL), 배구(V리그), 아시아축구만 간결하게 분석하고 2폴더 조합 2세트 및 차트 JSON을 작성하세요.",
        data_block,
        detailed_pro=False,
    )
    deliver_analysis_with_visual(title, res, chart_json, chat_id)
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
