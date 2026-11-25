import os
import math
import csv
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
        if s.startswith(("🏀", "🏒", "⚽", "🏟️", "📊", "🎯", "💎", "🔥", "📌")):
            out_lines.append(f"<b>{s}</b>")
        elif s.startswith(("①", "②", "③")):
            out_lines.append(f"<b><u>{s}</u></b>")
        elif s.startswith("[") and "vs" in s:
            out_lines.append(f"<b>{s}</b>")
        elif "최종 추천픽" in s:
            out_lines.append(f"<blockquote><b>{s}</b></blockquote>")
        elif s.startswith(("- 1폴더:", "- 2폴더:", "- 3폴더:", "1폴더:", "2폴더:", "3폴더:")):
            out_lines.append(f"<blockquote><b>{s}</b></blockquote>")
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


def fetch_espn_nba_injuries(playing_teams: set) -> str:
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
    try:
        r = requests.get(url, timeout=15)
        if not r.ok:
            return ""
        out = []
        for team_block in r.json().get("injuries", []):
            t_name = team_block.get("displayName", "")
            if playing_teams and not any(
                pt.lower() in t_name.lower() or t_name.lower() in pt.lower()
                for pt in playing_teams
            ):
                continue
            players = []
            for inj in team_block.get("injuries", []):
                status = inj.get("status", "")
                if status not in ("Out", "Doubtful", "Questionable", "Day-To-Day"):
                    continue
                ath = inj.get("athlete") or {}
                p_name = ath.get("displayName", "")
                pos = (ath.get("position") or {}).get("abbreviation", "")
                comment = (inj.get("shortComment") or "")[:60]
                if p_name:
                    players.append(f"{p_name}({pos}, {status})")
            if players:
                out.append(f"- {t_name}: " + ", ".join(players[:6]))
        return "\n".join(out)
    except Exception as e:
        print(f"ESPN NBA injuries error: {e}", flush=True)
        return ""


def fetch_fatigue_b2b(sport: str, league: str, now_kst: datetime.datetime, playing_teams: set) -> str:
    if not playing_teams:
        return ""
    played_days = {t: [] for t in playing_teams}
    for days_ago in (1, 2, 3):
        d_str = (now_kst - datetime.timedelta(days=days_ago)).strftime("%Y%m%d")
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={d_str}"
        try:
            r = requests.get(url, timeout=12)
            if not r.ok:
                continue
            for ev in r.json().get("events", []):
                dt_str = ev.get("date", "")
                if not dt_str:
                    continue
                dt_kst = datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(KST)
                diff_days = (now_kst.date() - dt_kst.date()).days
                if diff_days <= 0 or diff_days > 3:
                    continue
                comp = (ev.get("competitions") or [{}])[0]
                for c in comp.get("competitors", []):
                    t_name = (c.get("team") or {}).get("displayName", "")
                    if t_name in played_days and diff_days not in played_days[t_name]:
                        played_days[t_name].append(diff_days)
        except Exception:
            continue

    lines = []
    for t in sorted(playing_teams):
        days = sorted(played_days.get(t, []))
        flags = []
        if 1 in days:
            flags.append("백투백(B2B·피로 주의)")
        if len(days) >= 3:
            flags.append("최근 4일 3경기(강행군)")
        if not days:
            flags.append("3일 이상 충분한 휴식")
        elif 1 not in days:
            flags.append(f"휴식 {days[0]-1}일")
        lines.append(f"- {t}: " + ", ".join(flags))
    return "\n".join(lines)


def fetch_nhl_official_advanced(playing_teams: set) -> str:
    out = []
    try:
        r = requests.get("https://api-web.nhle.com/v1/standings/now", timeout=15)
        if r.ok:
            for st in r.json().get("standings", []):
                t_name = (st.get("teamName") or {}).get("default", "")
                if playing_teams and not any(
                    pt.lower() in t_name.lower() or t_name.lower() in pt.lower()
                    for pt in playing_teams
                ):
                    continue
                gp = st.get("gamesPlayed", 1) or 1
                gf_pg = round((st.get("goalFor", 0) or 0) / gp, 2)
                ga_pg = round((st.get("goalAgainst", 0) or 0) / gp, 2)
                hw, hl, ho = st.get("homeWins", 0), st.get("homeLosses", 0), st.get("homeOtLosses", 0)
                rw, rl, ro = st.get("roadWins", 0), st.get("roadLosses", 0), st.get("roadOtLosses", 0)
                l10w, l10l, l10o = st.get("l10Wins", 0), st.get("l10Losses", 0), st.get("l10OtLosses", 0)
                stk = f"{st.get('streakCode', '')}{st.get('streakCount', '')}"
                out.append(
                    f"- {t_name}: 홈({hw}-{hl}-{ho}) 원정({rw}-{rl}-{ro}) | 최근10({l10w}-{l10l}-{l10o}) 연속:{stk} | 평균득점 {gf_pg} / 평균실점 {ga_pg}"
                )
    except Exception as e:
        print(f"NHL official standings error: {e}", flush=True)

    try:
        r2 = requests.get(
            "https://api.nhle.com/stats/rest/en/team/summary?sort=points&limit=40",
            timeout=15,
        )
        if r2.ok:
            pp_lines = []
            for row in r2.json().get("data", []):
                t_name = row.get("teamFullName", "")
                if playing_teams and not any(
                    pt.lower() in t_name.lower() or t_name.lower() in pt.lower()
                    for pt in playing_teams
                ):
                    continue
                pp = round((row.get("powerPlayPct") or 0) * 100, 1)
                pk = round((row.get("penaltyKillPct") or 0) * 100, 1)
                sf = round(row.get("shotsForPerGame") or 0, 1)
                sa = round(row.get("shotsAgainstPerGame") or 0, 1)
                pp_lines.append(f"- {t_name}: PP {pp}% / PK {pk}% | 슈팅 {sf} / 피슈팅 {sa}")
            if pp_lines:
                out.append("\n[NHL 특수팀(파워플레이/페널티킬) 및 슈팅 지표]")
                out.extend(pp_lines)
    except Exception as e:
        print(f"NHL special teams error: {e}", flush=True)

    return "\n".join(out)


def _dixon_coles_probs(elo_home: float, elo_away: float, home_adv: float = 65.0) -> dict:
    diff = (elo_home + home_adv) - elo_away
    exp_h = 1.0 / (1.0 + 10.0 ** (-diff / 400.0))
    lam_h = max(0.45, min(3.2, 1.35 * (0.55 + 0.9 * exp_h)))
    lam_a = max(0.35, min(2.9, 1.35 * (1.45 - 0.9 * exp_h)))
    rho = -0.10

    def pois(k: int, lam: float) -> float:
        return (lam ** k) * math.exp(-lam) / math.factorial(k)

    def tau(x: int, y: int) -> float:
        if x == 0 and y == 0:
            return max(0.01, 1.0 - lam_h * lam_a * rho)
        if x == 0 and y == 1:
            return max(0.01, 1.0 + lam_h * rho)
        if x == 1 and y == 0:
            return max(0.01, 1.0 + lam_a * rho)
        if x == 1 and y == 1:
            return max(0.01, 1.0 - rho)
        return 1.0

    p_home = p_draw = p_away = p_over25 = 0.0
    total = 0.0
    for hg in range(7):
        for ag in range(7):
            prob = pois(hg, lam_h) * pois(ag, lam_a) * tau(hg, ag)
            total += prob
            if hg > ag:
                p_home += prob
            elif hg == ag:
                p_draw += prob
            else:
                p_away += prob
            if hg + ag >= 3:
                p_over25 += prob
    if total > 0:
        p_home, p_draw, p_away, p_over25 = (
            p_home / total * 100,
            p_draw / total * 100,
            p_away / total * 100,
            p_over25 / total * 100,
        )
    return {
        "home": round(p_home, 1),
        "draw": round(p_draw, 1),
        "away": round(p_away, 1),
        "over25": round(p_over25, 1),
        "lam_h": round(lam_h, 2),
        "lam_a": round(lam_a, 2),
    }


def fetch_clubelo_and_dixon_coles(now_kst: datetime.datetime, matchups: list) -> str:
    if not matchups:
        return ""
    d_str = now_kst.strftime("%Y-%m-%d")
    try:
        r = requests.get(f"http://api.clubelo.com/{d_str}", timeout=15)
        if not r.ok or "Club,Country" not in r.text:
            return ""
        elo_rows = []
        for row in csv.DictReader(io.StringIO(r.text)):
            club = (row.get("Club") or "").strip()
            elo = (row.get("Elo") or "").strip()
            if club and elo:
                elo_rows.append((club, float(elo)))

        def find_elo(name: str):
            nl = name.lower()
            for club, val in elo_rows:
                cl = club.lower()
                if len(cl) >= 4 and (cl in nl or nl in cl):
                    return club, val
            return None, None

        out = []
        for h_name, a_name in matchups:
            hc, he = find_elo(h_name)
            ac, ae = find_elo(a_name)
            if he and ae:
                dc = _dixon_coles_probs(he, ae)
                out.append(
                    f"- {h_name}(Elo {he:.0f}) vs {a_name}(Elo {ae:.0f}) | "
                    f"Dixon-Coles 확률: 홈승 {dc['home']}% / 무 {dc['draw']}% / 원정승 {dc['away']}% | "
                    f"기대득점 {dc['lam_h']}:{dc['lam_a']} (오버2.5 {dc['over25']}%)"
                )
        return "\n".join(out)
    except Exception as e:
        print(f"ClubElo/Dixon-Coles error: {e}", flush=True)
        return ""


def _american_to_prob_and_dec(am_str) -> tuple[float, float]:
    try:
        v = float(str(am_str).replace("+", "").strip())
        if v == 0:
            return 0.0, 0.0
        if v < 0:
            prob = (-v) / (-v + 100.0)
            dec = 1.0 + (100.0 / (-v))
        else:
            prob = 100.0 / (v + 100.0)
            dec = 1.0 + (v / 100.0)
        return round(prob * 100.0, 1), round(dec, 2)
    except Exception:
        return 0.0, 0.0


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
                cur_am = src.get("odds") or o.get("bookOdds") or ""
                open_am = src.get("openOdds") or o.get("openBookOdds") or ""
                fair_am = o.get("fairOdds") or ""
                cur_line = src.get("spread") or src.get("overUnder") or o.get("bookSpread") or o.get("bookOverUnder") or ""
                open_line = src.get("openSpread") or src.get("openOverUnder") or o.get("openBookSpread") or o.get("openBookOverUnder") or ""

                fair_prob, fair_dec = _american_to_prob_and_dec(fair_am)
                cur_prob, cur_dec = _american_to_prob_and_dec(cur_am)
                open_prob, open_dec = _american_to_prob_and_dec(open_am)

                move_tag = ""
                if cur_dec and open_dec and (open_dec - cur_dec) >= 0.05:
                    move_tag = f" 🔥샤프배당하락({open_dec}->{cur_dec})"
                elif cur_line and open_line and str(cur_line) != str(open_line):
                    move_tag = f" 📊라인이동({open_line}->{cur_line})"

                tag = "피나클" if pinn else "시장"
                if cur_dec or fair_prob or cur_line:
                    info = f"{label}({tag} {cur_line} {cur_dec if cur_dec else cur_am}".strip()
                    if fair_prob:
                        info += f" | 공정승률 {fair_prob}%/공정배당 {fair_dec}"
                    info += f"){move_tag}"
                    parts.append(info)
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


def fetch_espn_window(sport: str, league: str, start_kst: datetime.datetime, end_kst: datetime.datetime) -> tuple[str, set]:
    dates = set()
    cur = start_kst - datetime.timedelta(days=1)
    while cur.date() <= (end_kst + datetime.timedelta(days=1)).date():
        dates.add(cur.strftime("%Y%m%d"))
        cur += datetime.timedelta(days=1)

    events_out = []
    playing_teams = set()
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
                playing_teams.add(h_name)
                playing_teams.add(a_name)
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

    return "\n".join(events_out), playing_teams


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
        ("soccer", "uefa.europa", "유로파리그"),
        ("soccer", "kor.1", "K리그1"),
        ("soccer", "kor.2", "K리그2"),
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


def fetch_via_jina(target_url: str, max_chars: int = 350000) -> str:
    try:
        r = requests.get(
            f"https://r.jina.ai/{target_url}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        if r.ok and r.text:
            return r.text[:max_chars]
    except Exception:
        pass
    return ""


def _extract_mmdd_lines(raw: str, target_mmdd: str) -> str:
    if not raw or target_mmdd not in raw:
        return ""
    lines = raw.splitlines()
    matched = []
    i = 0
    while i < len(lines):
        if target_mmdd in lines[i]:
            block = " | ".join(ln.strip() for ln in lines[i : i + 7] if ln.strip())
            matched.append(block)
            i += 4
        else:
            i += 1
    return "\n".join(matched[:140]) if matched else ""


def fetch_wisetoto_by_mmdd(target_mmdd: str) -> str:
    year = datetime.datetime.now(KST).year
    for target in ["https://mw.wisetoto.com/", "https://www.wisetoto.com/"]:
        raw = fetch_via_jina(target, max_chars=350000)
        found = _extract_mmdd_lines(raw, target_mmdd)
        if found:
            return found
        if raw:
            m = re.search(r"프로토\s*승부식\s*\d{4}년도\s*(\d+)회차", raw)
            if m:
                cur_round = int(m.group(1))
                for r_num in (cur_round + 1, cur_round - 1):
                    alt_url = f"https://mw.wisetoto.com/?tab_type=proto&game_type=pt&game_category=pt1&game_year={year}&game_round={r_num}"
                    alt_raw = fetch_via_jina(alt_url, max_chars=350000)
                    alt_found = _extract_mmdd_lines(alt_raw, target_mmdd)
                    if alt_found:
                        return alt_found
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
                        temperature=0.15,
                    ),
                )
                if response and response.text:
                    return clean_telegram_text(response.text)
            except Exception as e:
                last_err = e
                print(f"Round {round_idx} | Model {model_name} failed: {e}", flush=True)
                time.sleep(3 * round_idx)
    print(f"All models failed. Last error: {last_err}", flush=True)
    return ""


def call_gemini_matches(title: str, guide: str, data_block: str) -> str:
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
4) [핵심 베팅 전략 - 적중률 극대화 & ⚪ 관망(패스) 필터링]
- 전 경기를 분석하되, 경기 수에 따라 유연하게 선별하세요: 5경기 이상 많을 때는 50:50 애매한 경기(약 30~40%)를 과감히 [⚪ 관망(패스)] 처리하고 진짜 근거가 확실한 상위 ⭐ S급 주력 / A급 추천 경기만 픽을 내세요. 반면 3~4경기 이하로 경기 수가 적을 때는 억지로 관망을 채우지 말고, 각 경기에서 가장 적중 확률이 높은 라인(1.31배 이상 일반승·언더·적정 핸디)을 찾아 ⭐ S급 / A급 추천픽을 제시하세요.
- [최소 배당 1.31배 & 유형 균형] 1.30배 이하 극저배당 일반승은 금지하며, 선별된 추천 경기 안에서 [1.31배 이상 일반승], [안전 핸디캡], [근거 확실한 언오버]를 자연스럽게 균형 배분하세요.
- [🔥 샤프 자금(오즈 무브) & 피나클 공정 승률(+EV) 최우선 원칙]: 피나클 배당이 오프닝 대비 하락(🔥샤프배당하락)하거나, 피나클 무수수료 공정승률(fairOdds)이 높으면서 프로토 배당이 1.31배 이상으로 유리하게 책정된 경기를 최우선 ⭐ S급 주력으로 올리세요. 피나클 샤프 자금 흐름과 정반대 방향의 억지 역배/마핸은 절대 금지합니다.
- [실전 낙첨 패턴 원천 차단 수칙]:
  * NBA 금지 함정: ① 상위 강팀을 상대로 약팀에 [+10.5 이상 대량 플핸] 억지 추천 금지(대패 위험). ② [원정팀 -7.5 이상 대량 마핸] 금지. 대신 부상·수비 페이스 근거가 뚜렷한 언오버(특히 언더), 1.31배 이상 확실한 일반승, 홈 적정 마핸(-3.5~-7.5)을 최우선으로 삼으세요.
  * NHL 금지 함정: ① [NHL -1.5 마핸]은 이기고도 1골 차로 낙첨되는 경우가 매우 잦으므로 원칙적 금지! 승리가 유력하면 1.31배 이상 [일반승]을 고르세요. ② 기준점 6.5에서는 무리한 오버보다 수비/골리 근거 기반 [6.5 언더]를 우선 검토하세요. ③ 원정 약팀 +1.5 플핸은 막판 엠프티넷(빈 골대) 2골 차 패배 위험이 크므로 확실한 접전 근거가 없으면 관망 처리하세요.
  * 해외축구: ClubElo 격차와 Dixon-Coles 모델 확률이 피나클 배당과 일치하는 경기만 S급/A급으로 선별하고, 불확실한 무승부 접전은 과감히 관망 처리하세요.
- 픽 유형 배지: 일반승/마핸은 🟢, 언오버는 🟡, 역배/플핸은 🟣 을 붙이고, 패스 경기는 '⚪ 관망(패스)'로 표기하세요.
5) 반드시 맨 위에 [📌 오늘의 분석 방식]을 2~3줄로 먼저 밝힌 뒤, 각 경기마다 픽만 던지지 말고 아래 양식대로 깊이 있는 분석글을 충실하게 작성하세요:

📌 [오늘의 분석 방식]
• 데이터 기준: 베트맨 프로토 승부식 공식 배당 최우선 + 피나클 배당 교차 검증 + 종목별 핵심 지표(NBA 공식 부상자·백투백 / NHL PP·PK·슈팅 / 해축 ClubElo·Dixon-Coles 확률)
• 픽 선별 기준: 1.30배 이하 저배당 제외, 1.31배 이상 일반승·핸디캡·언오버 균형 배분 및 적중률 최우선

[종목 | 한국시간] 홈팀(홈) vs 원정팀(원정)
• 라인/배당: 베트맨 프로토 승부식 배당(일반승·핸디캡·언오버 기준점)을 최우선으로 표기 (프로토 미발매 경기만 피나클/해외 배당의 프로토 환산치로 표기, 1.31배 이상 기준도 프로토 배당 기준 적용)
• 핵심 지표: 시즌·홈/원정 성적, 최근 10경기, 평균 득실·득실차 (NHL은 PP%/PK%·슈팅, 해축은 ClubElo·Dixon-Coles 확률 포함)
• 매치업 & 변수: 공식 부상/결장자(Out/Questionable), 백투백(B2B)·휴식일 차이, 공수 템포 등 핵심 변수
• 상세 분석글: 양 팀의 전술 상성, 경기 흐름 예상, 그리고 프로토 배당 대비 왜 이 픽(일반승/마핸/플핸/언오버/역배)이 가장 적중 확률과 밸류가 높은지 3~4문장으로 읽을거리 있게 충실히 서술
🌿 최종 추천픽: [⭐ S급 주력] 또는 [A급 추천] + 픽 내용 + 배지(🟢/🟡/🟣)  ※ 애매한 경기는 반드시 '🌿 최종 추천픽: ⚪ 관망(패스) — 변수 과다'로 표기

6) 주의: 이 메시지 안에는 조합을 넣지 마세요 (조합은 별도의 마지막 메시지로 따로 발송합니다).
"""
    prompt = f"{title}\n{guide}\n\n{rules}\n\n[경기 데이터]\n{data_block}"
    res = raw_gemini_call(sys_inst, prompt)
    if res:
        return res
    return f"{title}\n(일시적 AI 서버 혼잡으로 실시간 수집된 경기 및 기준점 원본을 먼저 전달합니다)\n\n{data_block}"


def call_gemini_combos(title: str, combo_rules: str, picks_context: str) -> str:
    sys_inst = (
        "당신은 적중률 최우선 스포츠 베팅 조합 전문 퀀트입니다. "
        "반드시 제공된 [오늘의 추천픽 목록] 안에서 부상·백투백·Elo·PP/PK 등 핵심 데이터 근거가 가장 확실하게 겹치는 최상위 확신 경기만 엄선해 조합하세요. "
        "50:50 애매한 접전 경기, 억지 역배, 그리고 1.30배 이하 극저배당 픽은 조합에서 과감히 제외하고 반드시 1.31배 이상 픽만 고르세요. 없는 경기를 지어내지 마세요. "
        "100% 한국어로 작성하고 마크다운 기호(*, **, #)와 빨간색 이모지(🔴, 🟥)는 절대 쓰지 마세요."
    )
    prompt = (
        f"{title}\n\n"
        f"[조합 구성 필수 규칙]\n{combo_rules}\n\n"
        "각 조합마다 반드시 아래 형식으로 깔끔하게 작성해 줘:\n"
        "① 조합 이름\n"
        "- 1폴더: 경기명 | 추천픽 (유형 배지 🟢/🟡/🟣)\n"
        "- 2폴더: 경기명 | 추천픽 (유형 배지 🟢/🟡/🟣)\n"
        "- 3폴더: 경기명 | 추천픽 (유형 배지 🟢/🟡/🟣)\n"
        "• 조합 포인트: 왜 이 3경기를 묶었는지 핵심 이유 1~2줄\n"
        "(만약 해당 종목 경기 수가 3경기 미만이면 가능한 경기 수만큼만 묶고 솔직히 표시해 줘.)\n\n"
        f"[오늘의 추천픽 목록]\n{picks_context}"
    )
    return raw_gemini_call(sys_inst, prompt)


def extract_picks_summary(analysis_text: str) -> str:
    blocks = []
    cur_match = ""
    cur_line = ""
    for raw_ln in analysis_text.splitlines():
        ln = raw_ln.strip()
        if "vs" in ln and ln.startswith("["):
            cur_match = ln
            cur_line = ""
        elif "라인/배당" in ln:
            cur_line = ln
        elif "최종 추천픽" in ln:
            if "관망" in ln or "패스" in ln:
                cur_match = ""
                continue
            part = [p for p in (cur_match, cur_line, ln) if p]
            if part:
                blocks.append("\n".join(part))
            cur_match = ""
    return "\n\n".join(blocks)


def is_slot_ready_to_grade(item: dict, now_kst: datetime.datetime) -> bool:
    item_date = item.get("date", "")
    slot = item.get("slot", "")
    today_str = now_kst.strftime("%Y-%m-%d")
    if item_date < today_str:
        return True
    if slot in ("NBA", "NHL"):
        return now_kst.hour >= 15
    if slot == "국내/아시아":
        return now_kst.hour >= 21
    if "해외축구" in slot:
        return now_kst.hour >= 8
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
            send_telegram_message(graded, chat_id, use_html=True)
            item["status"] = "graded"
            changed = True
            time.sleep(4)

    if changed:
        save_history(history)


def record_picks_to_history(slot: str, date_str: str, analysis_text: str) -> None:
    filtered = extract_picks_summary(analysis_text)
    history = load_history()
    history.append(
        {
            "date": date_str,
            "slot": slot,
            "status": "pending" if filtered else "empty",
            "picks_text": filtered,
        }
    )
    save_history(history)


def run_nba_nhl(now_kst: datetime.datetime, date_str: str, chat_id: str) -> None:
    start_kst = now_kst.replace(hour=7, minute=0, second=0, microsecond=0)
    end_kst = now_kst.replace(hour=16, minute=0, second=0, microsecond=0)
    fetch_statshawk_ping()
    wisetoto_today = fetch_wisetoto_today(now_kst)

    nba_res = ""
    nhl_res = ""

    if not already_sent(date_str, "NBA"):
        nba_espn, nba_teams = fetch_espn_window("basketball", "nba", start_kst, end_kst)
        if nba_espn:
            nba_sgo = fetch_sportsgameodds("NBA", start_kst, end_kst)
            nba_standings = fetch_espn_standings("basketball", "nba")
            nba_inj_official = fetch_espn_nba_injuries(nba_teams)
            nba_b2b = fetch_fatigue_b2b("basketball", "nba", now_kst, nba_teams)
            nba_block = f"[오늘의 NBA 경기 일정 및 기본 라인]\n{nba_espn}"
            if wisetoto_today:
                nba_block += f"\n\n[베트맨 프로토 승부식 오늘({now_kst.month:02d}.{now_kst.day:02d}) 공식 배당표 - 최우선 기준]\n{wisetoto_today}"
            if nba_sgo:
                nba_block += f"\n\n[SportsGameOdds 피나클/해외 배당 (밸류 대조 참고용)]\n{nba_sgo}"
            if nba_inj_official:
                nba_block += f"\n\n[ESPN NBA 공식 부상자 명단 (Out/Doubtful/Questionable)]\n{nba_inj_official}"
            if nba_b2b:
                nba_block += f"\n\n[NBA 팀별 백투백(B2B) 및 휴식일 피로도]\n{nba_b2b}"
            if nba_standings:
                nba_block += f"\n\n[NBA 팀별 시즌·홈/원정·최근10·득실차 지표]\n{nba_standings}"
            title = f"🏀 [{date_str}] 오늘의 NBA 전 경기 심층 분석"
            nba_res = call_gemini_matches(
                title,
                "피나클/해외 실시간 배당, ESPN 공식 부상자 명단, 백투백(B2B)·휴식일 피로도, 홈/원정·득실차 지표를 종합해 프로 퀀트 수준으로 심층 분석하세요.",
                nba_block,
            )
            send_telegram_message(nba_res, chat_id, use_html=True)
            record_picks_to_history("NBA", date_str, nba_res)
            time.sleep(8)

    if not already_sent(date_str, "NHL"):
        nhl_espn, nhl_teams = fetch_espn_window("hockey", "nhl", start_kst, end_kst)
        if nhl_espn:
            nhl_sgo = fetch_sportsgameodds("NHL", start_kst, end_kst)
            nhl_official = fetch_nhl_official_advanced(nhl_teams)
            nhl_b2b = fetch_fatigue_b2b("hockey", "nhl", now_kst, nhl_teams)
            nhl_news = fetch_en_news_rss("NHL starting goalies injury report today", max_items=8)
            nhl_block = f"[오늘의 NHL 경기 일정 및 기본 라인]\n{nhl_espn}"
            if wisetoto_today:
                nhl_block += f"\n\n[베트맨 프로토 승부식 오늘({now_kst.month:02d}.{now_kst.day:02d}) 공식 배당표 - 최우선 기준]\n{wisetoto_today}"
            if nhl_sgo:
                nhl_block += f"\n\n[SportsGameOdds 피나클/해외 배당 (밸류 대조 참고용)]\n{nhl_sgo}"
            if nhl_official:
                nhl_block += f"\n\n[NHL 공식 고급 지표 (홈/원정·득실·PP%/PK%·슈팅)]\n{nhl_official}"
            if nhl_b2b:
                nhl_block += f"\n\n[NHL 팀별 백투백(B2B) 및 휴식일 피로도]\n{nhl_b2b}"
            if nhl_news:
                nhl_block += f"\n\n[최신 NHL 선발 골리/부상 뉴스]\n{nhl_news}"
            title = f"🏒 [{date_str}] 오늘의 NHL 전 경기 심층 분석"
            nhl_res = call_gemini_matches(
                title,
                "피나클/해외 배당·퍽라인·언오버와 NHL 공식 PP%/PK%·슈팅·평균득실점, 백투백 피로도, 선발 골리 소식을 종합해 심층 분석하세요.",
                nhl_block,
            )
            send_telegram_message(nhl_res, chat_id, use_html=True)
            record_picks_to_history("NHL", date_str, nhl_res)
            time.sleep(6)

    if (nba_res or nhl_res) and not already_sent(date_str, "NBA_NHL_조합"):
        ctx_parts = []
        if nba_res:
            ctx_parts.append(f"[NBA 추천픽]\n{extract_picks_summary(nba_res)}")
        if nhl_res:
            ctx_parts.append(f"[NHL 추천픽]\n{extract_picks_summary(nhl_res)}")
        combo_rules = (
            "반드시 아래 3가지 조합을 적중률 최우선으로 구성해 줘:\n"
            "① NBA 조합 3폴더 (적중률 최우선 — 확실한 일반승을 적극 포함하고, 근거 뚜렷한 핸디캡·언오버와 자유롭게 조합)\n"
            "② NHL 조합 3폴더 (적중률 최우선 — 확실한 일반승을 적극 포함하고, +1.5 플핸·언오버와 자유롭게 조합)\n"
            "③ NBA + NHL 핸디·언오버 3폴더 (오직 NBA·NHL의 가장 확실한 핸디캡과 언오버만 섞어서 3폴더 구성)"
        )
        combo_msg = call_gemini_combos(
            f"🎯 [{date_str}] 아침 7시 NBA · NHL 최종 3폴더 추천 조합",
            combo_rules,
            "\n\n".join(ctx_parts),
        )
        if combo_msg:
            send_telegram_message(combo_msg, chat_id, use_html=True)
            record_picks_to_history("NBA_NHL_조합", date_str, "")


def run_overseas_soccer(now_kst: datetime.datetime, date_str: str, chat_id: str, is_weekend: bool) -> None:
    slot_name = "주말 밤 해외축구" if is_weekend else "새벽 해외축구"
    if already_sent(date_str, slot_name):
        return
    if is_weekend:
        start_kst = now_kst.replace(hour=20, minute=0, second=0, microsecond=0)
        end_kst = (now_kst + datetime.timedelta(days=1)).replace(hour=6, minute=30, second=0, microsecond=0)
        title = f"⚽ [{date_str}] 주말 밤 해외축구 주요 경기 분석"
    else:
        start_kst = now_kst.replace(hour=1, minute=0, second=0, microsecond=0)
        end_kst = now_kst.replace(hour=6, minute=30, second=0, microsecond=0)
        title = f"⚽ [{date_str}] 새벽 해외축구 주요 경기 분석"

    leagues = [
        ("soccer", "eng.1", "EPL"),
        ("soccer", "esp.1", "라리가"),
        ("soccer", "ita.1", "세리에A"),
        ("soccer", "ger.1", "분데스리가"),
        ("soccer", "fra.1", "리그1"),
        ("soccer", "uefa.champions", "챔피언스리그"),
        ("soccer", "uefa.europa", "유로파리그"),
        ("soccer", "uefa.europa.conf", "컨퍼런스리그"),
        ("soccer", "uefa.nations", "네이션스리그"),
    ]
    blocks = []
    matchups = []
    for sp, lg, label in leagues:
        d, _ = fetch_espn_window(sp, lg, start_kst, end_kst)
        if d:
            blocks.append(f"[{label}]\n{d}")
            for ln in d.splitlines():
                m = re.search(r"\]\s+(.+?)\(홈.*?\)\s+vs\s+(.+?)\(원정", ln)
                if m:
                    matchups.append((m.group(1).strip(), m.group(2).strip()))
    if not blocks:
        return

    sgo_soccer = fetch_sportsgameodds("EPL,LA_LIGA,IT_SERIE_A,BUNDESLIGA,LIGUE_1,UEFA_CHAMPIONS_LEAGUE,UEFA_EUROPA_LEAGUE", start_kst, end_kst)
    dc_text = fetch_clubelo_and_dixon_coles(now_kst, matchups)
    data_block = "\n\n".join(blocks)
    wisetoto_today = fetch_wisetoto_today(now_kst)
    if wisetoto_today:
        data_block += f"\n\n[베트맨 프로토 승부식 오늘({now_kst.month:02d}.{now_kst.day:02d}) 공식 배당표 - 최우선 기준]\n{wisetoto_today}"
    if sgo_soccer:
        data_block += f"\n\n[SportsGameOdds 해외/피나클 배당 (밸류 대조 참고용)]\n{sgo_soccer}"
    if dc_text:
        data_block += f"\n\n[ClubElo 전력 점수 & Dixon-Coles 통계 모델 확률/기대득점]\n{dc_text}"

    res = call_gemini_matches(
        title,
        "피나클/해외 주요 리그 배당(환급률 제거 관점)과 ClubElo·Dixon-Coles 통계 모델 확률/기대득점을 비교해 기대값(+) 밸류벳 중심으로 4항목 심층 분석하세요.",
        data_block,
    )
    send_telegram_message(res, chat_id, use_html=True)
    record_picks_to_history(slot_name, date_str, res)

    time.sleep(5)
    combo_rules = (
        "반드시 아래 2가지 해외축구 3폴더 조합을 적중률 최우선으로 구성해 줘:\n"
        "① 안정형 3폴더 조합 (적중률 최우선 — ClubElo·Dixon-Coles 근거가 가장 확실한 일반승 중심, 필요 시 안전한 핸디/언오버 혼합)\n"
        "② 일반승 + 핸디캡 + 언오버 3폴더 조합 (1폴더 일반승 + 2폴더 핸디캡 + 3폴더 언오버, 각 유형 정확히 1경기씩 구성)"
    )
    combo_msg = call_gemini_combos(
        f"🎯 [{date_str}] {slot_name} 최종 3폴더 추천 조합",
        combo_rules,
        extract_picks_summary(res),
    )
    if combo_msg:
        send_telegram_message(combo_msg, chat_id, use_html=True)


def run_domestic(now_kst: datetime.datetime, date_str: str, chat_id: str) -> None:
    if already_sent(date_str, "국내/아시아"):
        return
    start_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    end_kst = now_kst.replace(hour=23, minute=59, second=59, microsecond=0)

    asia_blocks = []
    for sp, lg, label in [
        ("soccer", "kor.1", "K리그1"),
        ("soccer", "kor.2", "K리그2"),
        ("soccer", "jpn.1", "J리그"),
        ("soccer", "afc.champions", "AFC 챔피언스리그"),
    ]:
        d, _ = fetch_espn_window(sp, lg, start_kst, end_kst)
        if d:
            asia_blocks.append(f"[{label}]\n{d}")
    asia_text = "\n".join(asia_blocks) if asia_blocks else "오늘 열리는 K리그1/K리그2/J리그/AFC 챔피언스리그 경기 없음."

    wisetoto_today = fetch_wisetoto_today(now_kst)
    kbl_news = fetch_google_news_rss(f"{now_kst.month}월 {now_kst.day}일 프로농구 프로배구 일정", max_items=8)

    data_block = (
        f"[오늘 날짜(KST): {date_str}]\n\n"
        f"[오늘({now_kst.month:02d}.{now_kst.day:02d}) 프로토 승부식 공식 대상경기 및 배당]\n{wisetoto_today or '프로토 목록 미확인'}\n\n"
        f"[ESPN 아시아축구(K리그1·2/J리그/ACL) 실시간 일정]\n{asia_text}\n\n"
        f"[참고 뉴스]\n{kbl_news}"
    )
    title = f"🏟️ [{date_str}] 국내 스포츠 및 아시아축구 분석 (프로토 기준)"
    res = call_gemini_matches(
        title,
        "위 [프로토 승부식 공식 대상경기 및 배당]과 [ESPN 아시아축구 실시간 일정]에서 오늘(KST) 실제로 열리는 국내 농구(KBL/WKBL), 배구(V리그), 아시아축구만 4항목 심층 분석하세요. 오늘 일정에 없는 종목/경기는 절대 지어내지 마세요.",
        data_block,
    )
    send_telegram_message(res, chat_id, use_html=True)
    record_picks_to_history("국내/아시아", date_str, res)

    picks_sum = extract_picks_summary(res)
    if picks_sum:
        time.sleep(5)
        combo_rules = (
            "반드시 아래 조합을 적중률 최우선으로 구성해 줘:\n"
            "① 국내 조합 3폴더 (적중률 최우선 — 확실한 일반승을 적극 포함하고, 근거가 뚜렷한 핸디캡·언오버와 자유롭게 조합)"
        )
        combo_msg = call_gemini_combos(
            f"🎯 [{date_str}] 국내 · 아시아 최종 3폴더 추천 조합",
            combo_rules,
            picks_sum,
        )
        if combo_msg:
            send_telegram_message(combo_msg, chat_id, use_html=True)


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
