"""Builds data.json for the Free Play Markets page. Run daily by GitHub Actions.

Sources are layered so each covers the others' gaps:
  games      ESPN betting lines -> ESPN point spread -> team records (log5)
  soccer     ESPN three-way lines (win / draw / loss)
  NFL props  Sleeper weekly projections -> ESPN team stat leaders
  other props ESPN team stat leaders (college football, NBA, WNBA, college basketball, MLB)
  weather    National Weather Service forecast
  politics   Polymarket balance-of-power prices, plus top active Polymarket markets
  more       Polymarket top markets sorted into Politics, Economics and Random
Any source that fails is skipped and the last good values are kept.
"""
import json, math, os, re, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
NOW = datetime.now(timezone.utc)
OUT = "data.json"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "free-play-markets (github actions)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace")
        raise RuntimeError(f"{e.code} for {url} :: {body}")


def clamp(p):
    return max(3, min(97, round(p * 100)))


def am_to_p(o):
    o = float(o)
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def iso(t):
    return t[:-1] + ":00Z" if len(t) == 17 else t  # 2026-10-04T17:00Z -> with seconds


# ESPN leader name -> ("line", label, min/game, max/game, step)   threshold props ("250+ passing yards")
#                  or ("rate", phrase, min/game, max/game, k)       "happens at least once" props
SPECS = {
    "passingYards": ("line", "passing yards", 120, 380, 5),
    "rushingYards": ("line", "rushing yards", 30, 160, 5),
    "receivingYards": ("line", "receiving yards", 30, 150, 5),
    "pointsPerGame": ("line", "points", 8, 40, 1), "points": ("line", "points", 8, 40, 1),
    "reboundsPerGame": ("line", "rebounds", 3, 16, 1), "rebounds": ("line", "rebounds", 3, 16, 1),
    "assistsPerGame": ("line", "assists", 2, 12, 1), "assists": ("line", "assists", 2, 12, 1),
    "homeRuns": ("rate", "hit a home run", 0.03, 0.5, 1.0), "HR": ("rate", "hit a home run", 0.03, 0.5, 1.0),
    "RBIs": ("rate", "drive in a run", 0.1, 1.2, 0.9), "RBI": ("rate", "drive in a run", 0.1, 1.2, 0.9),
    "hits": ("rate", "record a hit", 0.5, 1.6, 0.85), "H": ("rate", "record a hit", 0.5, 1.6, 0.85),
}
FOOTBALL = ("passingYards", "rushingYards", "receivingYards")
BASKETBALL = ("pointsPerGame", "points", "reboundsPerGame", "rebounds", "assistsPerGame", "assists")
BASEBALL = ("homeRuns", "HR", "RBIs", "RBI", "hits", "H")
P4_CONFERENCES = {"1", "4", "5", "8"}  # ACC, Big 12, Big Ten, SEC


def num(l):
    try:
        return float(l["value"])
    except Exception:
        m = re.search(r"[\d,]+\.?\d*", str(l.get("displayValue", "")))
        return float(m.group(0).replace(",", "")) if m else None


def record_ints(comp):
    try:
        return [int(x) for x in re.findall(r"\d+", (comp.get("records") or [{}])[0].get("summary", ""))]
    except Exception:
        return []


def games_played(comp):
    return sum(record_ints(comp))


def win_pct(comp):
    r = record_ints(comp)
    if len(r) < 2 or sum(r) < 2:
        return None
    return (r[0] + 0.5 * (r[2] if len(r) > 2 else 0)) / sum(r)


def per_game(value, played, lo, hi):
    """ESPN may give a season total or a per-game average, so accept whichever lands in a sane range."""
    if value is None:
        return None
    for v in (value, value / played if played else None):
        if v is not None and lo <= v <= hi:
            return v
    return None


def team_name(comp, college):
    name = comp["team"]["shortDisplayName"]
    if college:
        rank = (comp.get("curatedRank") or {}).get("current")
        if isinstance(rank, int) and 1 <= rank <= 25:
            return f"No. {rank} {name}"
    return name


def cfb_filter(e):
    """Keep ranked teams and Power 4 conference teams (plus Notre Dame)."""
    for comp in e["competitions"][0]["competitors"]:
        rank = (comp.get("curatedRank") or {}).get("current")
        if isinstance(rank, int) and 1 <= rank <= 25:
            return True
        t = comp.get("team") or {}
        if str(t.get("conferenceId", "")) in P4_CONFERENCES or t.get("abbreviation") == "ND":
            return True
    return False


def league(sport, lg, prefix, label, slope, allowed, return_events=False,
           days=4, cap=12, params="", pfilter=None, pcap=16):
    events, seen_ids = [], set()
    for i in range(days):
        day = (NOW + timedelta(days=i)).astimezone(CT).strftime("%Y%m%d")
        base = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard?dates={day}"
        try:
            data = get(base + params)
        except RuntimeError as ex:
            if not params:
                raise
            print(f"  {lg}: extra options rejected, retrying plain ({str(ex)[:60]})")
            data = get(base)
        for e in data.get("events", []):
            if e["id"] in seen_ids or e["status"]["type"]["state"] != "pre":
                continue
            seen_ids.add(e["id"])
            try:
                if pfilter and not pfilter(e):
                    continue
            except Exception:
                continue
            events.append(e)
    events.sort(key=lambda e: e["date"])
    college = label.startswith("College")
    out, props, seen_cats = [], [], set()
    for e in events[:cap]:
        try:
            c = e["competitions"][0]
            home = next(t for t in c["competitors"] if t["homeAway"] == "home")
            away = next(t for t in c["competitors"] if t["homeAway"] == "away")
            hn, an = team_name(home, college), team_name(away, college)
            t = iso(e["date"])
            o = (c.get("odds") or [{}])[0]
            ml_h = (o.get("homeTeamOdds") or {}).get("moneyLine")
            ml_a = (o.get("awayTeamOdds") or {}).get("moneyLine")
            ml_d = (o.get("drawOdds") or {}).get("moneyLine")
            p = pdraw = src = None
            if ml_h and ml_a:  # 1) betting lines
                ph, pa = am_to_p(ml_h), am_to_p(ml_a)
                pd_ = am_to_p(ml_d) if ml_d else 0
                tot = ph + pa + pd_
                p, pdraw, src = ph / tot, (pd_ / tot if ml_d else None), "ESPN betting lines"
            elif slope:  # 2) point spread
                m = re.match(r"^(\S+)\s+(-?\d+(\.\d+)?)$", o.get("details") or "")
                if m:
                    pf = 1 / (1 + math.exp(-slope * abs(float(m.group(2)))))
                    p = pf if m.group(1) == home["team"].get("abbreviation") else 1 - pf
                    src = "ESPN point spread"
            if p is None and sport != "soccer":  # 3) team records (log5 with a small home edge)
                rh, ra = win_pct(home), win_pct(away)
                if rh is not None and ra is not None and 0 < rh < 1 and 0 < ra < 1:
                    p = rh * (1 - ra) / (rh * (1 - ra) + ra * (1 - rh))
                    p, src = min(0.9, max(0.1, p + 0.03)), "team records (no betting line yet)"
            if p is not None:
                out.append({"id": f"{prefix}_{e['id']}", "c": "Sports", "tag": label, "px": clamp(p), "t": t,
                            "q": f"Will the {hn} beat the {an}?" if sport not in ("soccer",) and not college else f"Will {hn} beat {an}?",
                            "src": src})
                if pdraw:
                    out.append({"id": f"{prefix}_draw_{e['id']}", "c": "Sports", "tag": label, "px": clamp(pdraw), "t": t,
                                "q": f"Will {hn} and {an} end in a draw?", "src": src})
            if allowed and len(props) < pcap:  # player props from each team's stat leaders
                for comp, opp in ((home, away), (away, home)):
                    played = games_played(comp)
                    for cat in comp.get("leaders") or []:
                        name = cat.get("name")
                        seen_cats.add(name)
                        if name not in allowed or name not in SPECS or not cat.get("leaders"):
                            continue
                        l = cat["leaders"][0]
                        ath = l.get("athlete") or {}
                        kind, text, lo, hi, k = SPECS[name]
                        v = per_game(num(l), played, lo, hi)
                        if v is None or not ath.get("displayName"):
                            continue
                        oppn = opp["team"]["shortDisplayName"]
                        if kind == "line":
                            line = max(k, round(v / k) * k)
                            q, px = f"Will {ath['displayName']} have {line}+ {text} against the {oppn}?", 48
                        else:
                            q, px = f"Will {ath['displayName']} {text} against the {oppn}?", clamp(1 - math.exp(-v * k))
                        props.append({"id": f"p_{prefix}_{e['id']}_{ath.get('id', '0')}_{name}", "c": "Sports",
                                      "tag": f"{label} player stats", "px": px, "t": t, "q": q,
                                      "src": "ESPN season stat leaders"})
        except Exception:
            continue
    if allowed:
        print(f"  {label}: {len(events)} games in window, leader categories seen: {sorted(c for c in seen_cats if c)}")
    res = out + props[:pcap]
    return (res, events) if return_events else res


def results(sport, lg, label):
    """Finished games from the last two days with each team's top performers."""
    out, seen = [], set()
    for back in (0, 1):
        day = (NOW - timedelta(days=back)).astimezone(CT).strftime("%Y%m%d")
        data = get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard?dates={day}")
        for e in data.get("events", []):
            try:
                if e["id"] in seen or e["status"]["type"]["state"] != "post":
                    continue
                seen.add(e["id"])
                c = e["competitions"][0]
                home = next(t for t in c["competitors"] if t["homeAway"] == "home")
                away = next(t for t in c["competitors"] if t["homeAway"] == "away")
                leaders = []
                for comp in (home, away):
                    for cat in (comp.get("leaders") or [])[:3]:
                        top = (cat.get("leaders") or [None])[0]
                        if not top or not (top.get("athlete") or {}).get("displayName"):
                            continue
                        leaders.append({"team": comp["team"]["shortDisplayName"],
                                        "cat": cat.get("displayName") or cat.get("name") or "",
                                        "name": top["athlete"]["displayName"],
                                        "val": str(top.get("displayValue", ""))})
                out.append({"league": label, "date": iso(e["date"]),
                            "home": home["team"]["shortDisplayName"], "hs": str(home.get("score", "")),
                            "away": away["team"]["shortDisplayName"], "as": str(away.get("score", "")),
                            "leaders": leaders})
            except Exception:
                continue
    out.sort(key=lambda g: g["date"], reverse=True)
    return out[:8]


STAT_LEAGUES = [
    ("football", "nfl", "NFL"), ("football", "college-football", "College football"),
    ("baseball", "mlb", "MLB"), ("hockey", "nhl", "NHL"), ("basketball", "nba", "NBA"),
    ("basketball", "wnba", "WNBA"), ("basketball", "mens-college-basketball", "College basketball"),
    ("soccer", "eng.1", "Premier League"), ("soccer", "esp.1", "La Liga"), ("soccer", "ger.1", "Bundesliga"),
    ("soccer", "ita.1", "Serie A"), ("soccer", "fra.1", "Ligue 1"), ("soccer", "usa.1", "MLS"),
    ("soccer", "usa.nwsl", "NWSL"), ("soccer", "uefa.champions", "Champions League"),
]


RESULT_LEAGUES = [
    ("football", "nfl", "nfl"), ("football", "college-football", "cfb"), ("baseball", "mlb", "mlb"),
    ("hockey", "nhl", "nhl"), ("basketball", "nba", "nba"), ("basketball", "wnba", "wnba"),
    ("basketball", "mens-college-basketball", "cbb"), ("soccer", "eng.1", "epl"), ("soccer", "esp.1", "liga"),
    ("soccer", "ger.1", "bund"), ("soccer", "ita.1", "sera"), ("soccer", "fra.1", "lig1"),
    ("soccer", "usa.1", "mls"), ("soccer", "usa.nwsl", "nwsl"), ("soccer", "uefa.champions", "ucl"),
]


def final_results(sport, lg, prefix):
    """Real outcomes of finished games, keyed by market id (1 = Yes, 0 = No). The page settles bets with these."""
    out = {}
    for back in (0, 1, 2):
        day = (NOW - timedelta(days=back)).astimezone(CT).strftime("%Y%m%d")
        data = get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard?dates={day}")
        for e in data.get("events", []):
            try:
                if e["status"]["type"]["state"] != "post":
                    continue
                comp = e["competitions"][0]["competitors"]
                home = next(t for t in comp if t["homeAway"] == "home")
                away = next(t for t in comp if t["homeAway"] == "away")
                hw, aw = bool(home.get("winner")), bool(away.get("winner"))
                if not hw and not aw and home.get("score") != away.get("score"):
                    continue  # no clear result, leave it unsettled
                out[f"{prefix}_{e['id']}"] = 1 if hw else 0
                if sport == "soccer":
                    out[f"{prefix}_draw_{e['id']}"] = 0 if (hw or aw) else 1
            except Exception:
                continue
    return out


def norm_team(t):
    return {"WSH": "WAS", "JAC": "JAX"}.get((t or "").upper(), (t or "").upper())


def sleeper_props(events, season, week):
    url = (f"https://api.sleeper.com/projections/nfl/{season}/{week}?season_type=regular"
           "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&order_by=pts_ppr")
    data = get(url)
    if not isinstance(data, list) or not data:
        raise ValueError("no projections returned")
    nxt = {}  # team abbreviation -> (kickoff, opponent name)
    for e in events:
        c = e["competitions"][0]["competitors"]
        ab = [norm_team(t["team"].get("abbreviation")) for t in c]
        nm = [t["team"]["shortDisplayName"] for t in c]
        nxt[ab[0]] = (iso(e["date"]), nm[1])
        nxt[ab[1]] = (iso(e["date"]), nm[0])
    cands = []
    for row in data:
        p = row.get("player") or {}
        team = norm_team(row.get("team") or p.get("team"))
        pos = p.get("position")
        if team not in nxt or pos not in ("QB", "RB", "WR", "TE"):
            continue
        if (p.get("injury_status") or "") in ("Out", "Doubtful", "IR", "PUP", "Sus"):
            continue
        st = row.get("stats") or {}
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        pid = row.get("player_id") or p.get("player_id") or name.replace(" ", "")
        cands.append({"st": st, "pts": st.get("pts_ppr") or 0, "pos": pos, "name": name, "pid": pid,
                      "t": nxt[team][0], "opp": nxt[team][1]})
    cands.sort(key=lambda c: -c["pts"])
    caps = {"QB": 14, "RB": 18, "WR": 26, "TE": 10}
    used, out = {}, []

    def add(c, key, q, px):
        out.append({"id": f"p_nfl_{week}_{c['pid']}_{key}", "c": "Sports", "tag": "NFL player stats", "px": px,
                    "t": c["t"], "q": q, "src": "Sleeper weekly projections"})

    for c in cands:
        pos, st = c["pos"], c["st"]
        if used.get(pos, 0) >= caps[pos]:
            continue
        key, label, minv = {"QB": ("pass_yd", "passing yards", 150), "RB": ("rush_yd", "rushing yards", 35),
                            "WR": ("rec_yd", "receiving yards", 35), "TE": ("rec_yd", "receiving yards", 25)}[pos]
        v = st.get(key) or 0
        if v < minv:
            continue
        used[pos] = used.get(pos, 0) + 1
        add(c, key, f"Will {c['name']} have {max(5, round(v / 5) * 5)}+ {label} against the {c['opp']}?", 50)
        if pos == "QB":
            td = st.get("pass_td") or 0
            if td >= 0.8:
                n = 1 if td < 1.5 else 2
                add(c, "pass_td", f"Will {c['name']} throw {n}+ touchdown pass{'es' if n > 1 else ''} against the {c['opp']}?", 50)
        else:
            rec = st.get("rec") or 0
            if rec >= 2.5:
                add(c, "rec", f"Will {c['name']} have {round(rec)}+ receptions against the {c['opp']}?", 50)
    tds = sorted(((c["st"].get("rush_td") or 0) + (c["st"].get("rec_td") or 0), c) for c in cands)
    for td, c in list(reversed(tds))[:26]:
        if td >= 0.25:
            add(c, "anytd", f"Will {c['name']} score a touchdown against the {c['opp']}?", clamp(1 - math.exp(-td)))
    if not out:
        raise ValueError("no usable projections")
    return out


def nfl_section():
    games, events = league("football", "nfl", "nfl", "NFL", 0.145, None, True, days=7, cap=20)
    try:
        meta = get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard")
        props = sleeper_props(events, meta["season"]["year"], meta["week"]["number"])
        print("  nfl props from Sleeper:", len(props))
    except Exception as ex:
        print("  Sleeper failed, using ESPN leaders:", ex)
        props = [m for m in league("football", "nfl", "nfl", "NFL", 0.145, FOOTBALL, days=7, cap=20, pcap=40) if m["id"].startswith("p_")]
    return games + props


def weather():
    pts = get("https://api.weather.gov/points/29.7604,-95.3698")
    periods = get(pts["properties"]["forecast"])["properties"]["periods"]
    out, seen = [], 0
    for p in periods:
        if not p.get("isDaytime"):
            continue
        d = datetime.fromisoformat(p["startTime"])
        end = datetime(d.year, d.month, d.day, 23, 59, tzinfo=CT).astimezone(timezone.utc)
        if end <= NOW:
            continue
        label = f"{d.strftime('%A')}, {d.strftime('%b')} {d.day}"
        t = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
        key = d.strftime("%Y%m%d")
        out.append({"id": f"w_rain_{key}", "c": "Random", "tag": "Weather", "px": clamp(pop / 100), "t": t,
                    "q": f"Will it rain in Houston on {label}?", "src": "National Weather Service"})
        hi = p["temperature"]
        out.append({"id": f"w_hot_{key}", "c": "Random", "tag": "Weather",
                    "px": clamp(1 / (1 + math.exp(-(hi - 84.5) / 2))), "t": t,
                    "q": f"Will Houston hit a high of 85\u00b0F or more on {label}?", "src": "National Weather Service"})
        seen += 1
        if seen == 4:
            break
    return out


def politics():
    ev = get("https://gamma-api.polymarket.com/events?slug=balance-of-power-2026-midterms")
    vals = {}
    for m in ev[0]["markets"]:
        name = (m.get("groupItemTitle") or m.get("question") or "").lower()
        yes = float(json.loads(m["outcomePrices"])[0])
        if "democrats sweep" in name:
            vals["ds"] = yes
        elif "republicans sweep" in name:
            vals["rs"] = yes
        elif "r senate, d house" in name:
            vals["rd"] = yes
        elif "d senate, r house" in name:
            vals["dr"] = yes
    if not all(k in vals for k in ("ds", "rs", "rd", "dr")):
        raise ValueError("missing outcomes")
    t = "2026-11-04T06:00:00Z"
    mk = lambda i, q, p: {"id": i, "c": "Politics", "tag": "Politics", "px": clamp(p), "t": t, "q": q, "src": "Polymarket"}
    return [
        mk("r_house", "Will Democrats win control of the U.S. House in the Nov 3 midterms?", vals["ds"] + vals["rd"]),
        mk("r_senate", "Will Democrats win control of the U.S. Senate in the Nov 3 midterms?", vals["ds"] + vals["dr"]),
        mk("r_sweepd", "Will Democrats win both the House and the Senate in the Nov 3 midterms?", vals["ds"]),
        mk("r_sweepr", "Will Republicans win both the House and the Senate in the Nov 3 midterms?", vals["rs"]),
    ]


BLOCK = re.compile(r"assassinat|\bkill|\bdeath|\bdie\b|\bdies\b|\bdead\b|shooting|terror|massacre|nuclear|genocide|\brape|suicide|overdose|\bwar crimes", re.I)
SPORTS_TAGS = {"sports", "nfl", "nba", "mlb", "nhl", "soccer", "college-football", "college-basketball", "ncaa", "epl",
               "tennis", "ufc", "mma", "golf", "f1", "formula-1", "cricket", "esports", "boxing", "olympics", "wnba",
               "la-liga", "champions-league", "mls", "world-cup", "fifa"}
POLITICS_TAGS = {"politics", "elections", "us-politics", "global-elections", "world-elections", "trump", "congress",
                 "midterms", "us-election", "senate", "house", "primaries", "government", "supreme-court", "president"}
WORLD_TAGS = {"geopolitics", "world", "foreign-policy", "middle-east", "ukraine", "china", "europe"}
ECON_TAGS = {"economy", "economics", "fed", "fed-rates", "inflation", "finance", "business", "recession", "gdp", "jobs",
             "unemployment", "stocks", "earnings", "commodities", "oil", "interest-rates", "tariffs", "ipos", "big-tech"}
CRYPTO_TAGS = {"crypto", "bitcoin", "ethereum", "crypto-prices"}
GENERIC = {"all", "trending", "featured", "hide from new", "recurring", "rewards", "new", "breaking news", "parent for derivative"}
CAPS = {"Politics": 140, "Economics": 80, "Random": 140}


def pm_markets():
    """Top active Polymarket markets, sorted into Politics, Economics and Random with their live prices."""
    events = []
    for off in (0, 100, 200, 300):
        got = get(f"https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100&order=volume24hr&ascending=false&offset={off}")
        if not isinstance(got, list) or not got:
            break
        events += got
    out, counts, tagcounts = [], {}, {}
    for ev in events:
        try:
            tags = [t for t in (ev.get("tags") or []) if isinstance(t, dict)]
            slugs = {str(t.get("slug", "")).lower() for t in tags}
            labels = [str(t.get("label", "")) for t in tags if str(t.get("label", "")).lower() not in GENERIC]
            if slugs & SPORTS_TAGS:
                continue
            if slugs & POLITICS_TAGS:
                cat, tag = "Politics", "Politics"
            elif slugs & WORLD_TAGS:
                cat, tag = "Politics", "World"
            elif slugs & ECON_TAGS:
                cat, tag = "Economics", "Economy"
            elif slugs & CRYPTO_TAGS:
                cat, tag = "Economics", "Crypto"
            else:
                cat, tag = "Random", (labels[0] if labels else "Other")[:16]
            cands = []
            for m in ev.get("markets") or []:
                if m.get("closed") or m.get("active") is False:
                    continue
                outs = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
                if [str(x).lower() for x in (outs or [])] != ["yes", "no"]:
                    continue
                yes = float(json.loads(m["outcomePrices"])[0]) if isinstance(m.get("outcomePrices"), str) else float(m["outcomePrices"][0])
                if not 0.03 <= yes <= 0.97:
                    continue
                end = m.get("endDate") or ev.get("endDate")
                if not end:
                    continue
                endt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                if endt.tzinfo is None:
                    endt = endt.replace(tzinfo=timezone.utc)
                if not NOW < endt < NOW + timedelta(days=800):
                    continue
                q = (m.get("question") or "").strip()
                if not q or BLOCK.search(q) or BLOCK.search(ev.get("title") or ""):
                    continue
                cands.append((yes, f"pm_{m['id']}", q, endt))
            cands.sort(reverse=True)
            for yes, mid, q, endt in cands[:6]:
                if counts.get(cat, 0) >= CAPS[cat] or tagcounts.get((cat, tag), 0) >= 30:
                    break
                counts[cat] = counts.get(cat, 0) + 1
                tagcounts[(cat, tag)] = tagcounts.get((cat, tag), 0) + 1
                out.append({"id": mid, "c": cat, "tag": tag, "px": clamp(yes), "q": q[:150],
                            "t": endt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "src": "Polymarket"})
        except Exception:
            continue
    if not out:
        raise ValueError("no usable markets")
    print("  by category:", counts)
    return out


def main():
    old = []
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT)).get("markets", [])
        except Exception:
            pass
    soccer = lambda code, pre, lab: (lambda: league("soccer", code, pre, lab, None, None))
    sections = [
        ("nfl", nfl_section, r"^(nfl_|p_nfl_)"),
        ("college football", lambda: league("football", "college-football", "cfb", "College football", 0.145, FOOTBALL,
                                            days=7, cap=70, params="&groups=80&limit=300", pfilter=cfb_filter, pcap=30), r"^(cfb_|p_cfb_)"),
        ("mlb", lambda: league("baseball", "mlb", "mlb", "MLB", 0.3, BASEBALL, days=3, cap=48, pcap=36), r"^(mlb_|p_mlb_)"),
        ("nhl", lambda: league("hockey", "nhl", "nhl", "NHL", None, None, days=3, cap=30), r"^nhl_"),
        ("nba", lambda: league("basketball", "nba", "nba", "NBA", 0.15, BASKETBALL, days=3, cap=30, pcap=36), r"^(nba_|p_nba_)"),
        ("college basketball", lambda: league("basketball", "mens-college-basketball", "cbb", "College basketball", 0.15, BASKETBALL,
                                              days=3, cap=140, params="&groups=50&limit=400", pcap=36), r"^(cbb_|p_cbb_)"),
        ("premier league", soccer("eng.1", "epl", "Premier League"), r"^epl_"),
        ("la liga", soccer("esp.1", "liga", "La Liga"), r"^liga_"),
        ("bundesliga", soccer("ger.1", "bund", "Bundesliga"), r"^bund_"),
        ("serie a", soccer("ita.1", "sera", "Serie A"), r"^sera_"),
        ("ligue 1", soccer("fra.1", "lig1", "Ligue 1"), r"^lig1_"),
        ("mls", soccer("usa.1", "mls", "MLS"), r"^mls_"),
        ("champions league", soccer("uefa.champions", "ucl", "Champions League"), r"^ucl_"),
        ("wnba", lambda: league("basketball", "wnba", "wnba", "WNBA", 0.15, BASKETBALL, days=3, cap=14), r"^(wnba_|p_wnba_)"),
        ("nwsl", soccer("usa.nwsl", "nwsl", "NWSL"), r"^nwsl_"),
        ("weather", weather, r"^w_"),
        ("politics", politics, r"^r_(house|senate|sweep)"),
        ("polymarket", pm_markets, r"^pm_"),
    ]
    markets = []
    for name, fn, pat in sections:
        try:
            got = fn()
            print(name, "ok", len(got))
            markets += got
        except Exception as ex:
            print(name, "failed, keeping old values:", ex)
            markets += [m for m in old if re.match(pat, m["id"])]
    try:
        old_stats = json.load(open(OUT)).get("stats", []) if os.path.exists(OUT) else []
    except Exception:
        old_stats = []
    stats = []
    for sport, lg, lab in STAT_LEAGUES:
        try:
            got = results(sport, lg, lab)
            print("stats", lab, len(got))
            stats += got
        except Exception as ex:
            print("stats", lab, "failed, keeping old values:", ex)
            stats += [g for g in old_stats if g.get("league") == lab]
    try:
        old_results = json.load(open(OUT)).get("results", {}) if os.path.exists(OUT) else {}
    except Exception:
        old_results = {}
    settled = dict(old_results)
    for sport, lg, pre in RESULT_LEAGUES:
        try:
            got = final_results(sport, lg, pre)
            print("results", pre, len(got))
            settled.update(got)
        except Exception as ex:
            print("results", pre, "failed:", ex)
    settled = dict(list(settled.items())[-800:])
    markets = [m for m in markets if datetime.fromisoformat(m["t"].replace("Z", "+00:00")) > NOW]
    asof = datetime.now(CT)
    with open(OUT, "w") as f:
        json.dump({"asof": f"{asof.strftime('%b')} {asof.day}, {asof.year}", "markets": markets, "stats": stats, "results": settled}, f, indent=1)
    print("wrote", len(markets), "markets")


if __name__ == "__main__":
    main()
