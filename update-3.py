"""Builds data.json for the Free Play Markets page. Run daily by GitHub Actions.

Sources are layered so each covers the others' gaps:
  games      ESPN betting lines -> ESPN point spread -> team records (log5)
  soccer     ESPN three-way lines (win / draw / loss)
  NFL props  Sleeper weekly projections -> ESPN team stat leaders
  other props ESPN team stat leaders (college football, NBA, college basketball)
  weather    National Weather Service forecast
  politics   Polymarket balance-of-power prices
Any source that fails is skipped and the last good values are kept.
"""
import json, math, os, re, urllib.error, urllib.request
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


STATS = {  # ESPN leader name -> (label, min per game, max per game, rounding step)
    "passingYards": ("passing yards", 120, 380, 5),
    "rushingYards": ("rushing yards", 30, 160, 5),
    "receivingYards": ("receiving yards", 30, 150, 5),
    "pointsPerGame": ("points", 8, 40, 1),
    "points": ("points", 8, 40, 1),
}
FOOTBALL = ("passingYards", "rushingYards", "receivingYards")
BASKETBALL = ("pointsPerGame", "points")


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


def league(sport, lg, prefix, label, slope, allowed, return_events=False):
    events, seen_ids = [], set()
    for i in range(4):
        day = (NOW + timedelta(days=i)).astimezone(CT).strftime("%Y%m%d")
        data = get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard?dates={day}")
        for e in data.get("events", []):
            if e["id"] not in seen_ids and e["status"]["type"]["state"] == "pre":
                seen_ids.add(e["id"])
                events.append(e)
    events.sort(key=lambda e: e["date"])
    out, props = [], []
    for e in events[:12]:
        try:
            c = e["competitions"][0]
            home = next(t for t in c["competitors"] if t["homeAway"] == "home")
            away = next(t for t in c["competitors"] if t["homeAway"] == "away")
            hn, an = home["team"]["shortDisplayName"], away["team"]["shortDisplayName"]
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
                            "q": f"Will the {hn} beat the {an}?" if sport != "soccer" else f"Will {hn} beat {an}?",
                            "src": src})
                if pdraw:
                    out.append({"id": f"{prefix}_draw_{e['id']}", "c": "Sports", "tag": label, "px": clamp(pdraw), "t": t,
                                "q": f"Will {hn} and {an} end in a draw?", "src": src})
            if allowed and len(props) < 16:  # player props from each team's stat leaders
                for comp, opp in ((home, away), (away, home)):
                    played = games_played(comp)
                    for cat in comp.get("leaders") or []:
                        name = cat.get("name")
                        if name not in allowed or not cat.get("leaders"):
                            continue
                        l = cat["leaders"][0]
                        ath = l.get("athlete") or {}
                        stat, lo, hi, step = STATS[name]
                        v = per_game(num(l), played, lo, hi)
                        if v is None or not ath.get("displayName"):
                            continue
                        line = max(step, round(v / step) * step)
                        props.append({"id": f"p_{prefix}_{e['id']}_{ath.get('id', '0')}_{name}", "c": "Sports",
                                      "tag": f"{label} player stats", "px": 48, "t": t,
                                      "q": f"Will {ath['displayName']} have {line}+ {stat} against the {opp['team']['shortDisplayName']}?",
                                      "src": "ESPN season stat leaders"})
        except Exception:
            continue
    res = out + props[:16]
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


def norm_team(t):
    return {"WSH": "WAS", "JAC": "JAX"}.get((t or "").upper(), (t or "").upper())


SLEEPER_SPEC = {  # position -> (stat key, label, rounding step, minimum projection)
    "QB": ("pass_yd", "passing yards", 5, 150),
    "RB": ("rush_yd", "rushing yards", 5, 35),
    "WR": ("rec_yd", "receiving yards", 5, 35),
    "TE": ("rec_yd", "receiving yards", 5, 25),
}
SLEEPER_CAP = {"QB": 5, "RB": 6, "WR": 8, "TE": 3}


def sleeper_props(events, season, week):
    url = (f"https://api.sleeper.com/projections/nfl/{season}/{week}?season_type=regular"
           "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&order_by=pts_ppr")
    data = get(url)
    if not isinstance(data, list) or not data:
        raise ValueError("no projections returned")
    nxt = {}  # team abbreviation -> (event id, kickoff, opponent name)
    for e in events:
        c = e["competitions"][0]["competitors"]
        ab = [norm_team(t["team"].get("abbreviation")) for t in c]
        nm = [t["team"]["shortDisplayName"] for t in c]
        nxt[ab[0]] = (e["id"], iso(e["date"]), nm[1])
        nxt[ab[1]] = (e["id"], iso(e["date"]), nm[0])
    picks = []
    for row in data:
        p = row.get("player") or {}
        team = norm_team(row.get("team") or p.get("team"))
        spec = SLEEPER_SPEC.get(p.get("position"))
        if team not in nxt or not spec:
            continue
        if (p.get("injury_status") or "") in ("Out", "Doubtful", "IR", "PUP", "Sus"):
            continue
        st = row.get("stats") or {}
        v = st.get(spec[0])
        if not v or v < spec[3]:
            continue
        picks.append((st.get("pts_ppr") or 0, p.get("position"), spec, v, row, p, nxt[team]))
    picks.sort(key=lambda x: -x[0])
    used, out = {}, []
    for pts, pos, spec, v, row, p, (eid, t, opp) in picks:
        if used.get(pos, 0) >= SLEEPER_CAP[pos]:
            continue
        used[pos] = used.get(pos, 0) + 1
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        pid = row.get("player_id") or p.get("player_id") or name.replace(" ", "")
        line = max(spec[2], round(v / spec[2]) * spec[2])
        out.append({"id": f"p_nfl_{week}_{pid}_{spec[0]}", "c": "Sports", "tag": "NFL player stats", "px": 50, "t": t,
                    "q": f"Will {name} have {line}+ {spec[1]} against the {opp}?", "src": "Sleeper weekly projections"})
    if not out:
        raise ValueError("no usable projections")
    return out


def nfl_section():
    games, events = league("football", "nfl", "nfl", "NFL", 0.145, None, True)
    try:
        meta = get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard")
        props = sleeper_props(events, meta["season"]["year"], meta["week"]["number"])
        print("  nfl props from Sleeper:", len(props))
    except Exception as ex:
        print("  Sleeper failed, using ESPN leaders:", ex)
        props = [m for m in league("football", "nfl", "nfl", "NFL", 0.145, FOOTBALL) if m["id"].startswith("p_")]
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
        ("college football", lambda: league("football", "college-football", "cfb", "College football", 0.145, FOOTBALL), r"^(cfb_|p_cfb_)"),
        ("mlb", lambda: league("baseball", "mlb", "mlb", "MLB", 0.3, None), r"^mlb_"),
        ("nhl", lambda: league("hockey", "nhl", "nhl", "NHL", None, None), r"^nhl_"),
        ("nba", lambda: league("basketball", "nba", "nba", "NBA", 0.15, BASKETBALL), r"^(nba_|p_nba_)"),
        ("college basketball", lambda: league("basketball", "mens-college-basketball", "cbb", "College basketball", 0.15, BASKETBALL), r"^(cbb_|p_cbb_)"),
        ("premier league", soccer("eng.1", "epl", "Premier League"), r"^epl_"),
        ("la liga", soccer("esp.1", "liga", "La Liga"), r"^liga_"),
        ("bundesliga", soccer("ger.1", "bund", "Bundesliga"), r"^bund_"),
        ("serie a", soccer("ita.1", "sera", "Serie A"), r"^sera_"),
        ("ligue 1", soccer("fra.1", "lig1", "Ligue 1"), r"^lig1_"),
        ("mls", soccer("usa.1", "mls", "MLS"), r"^mls_"),
        ("champions league", soccer("uefa.champions", "ucl", "Champions League"), r"^ucl_"),
        ("wnba", lambda: league("basketball", "wnba", "wnba", "WNBA", 0.15, BASKETBALL), r"^(wnba_|p_wnba_)"),
        ("nwsl", soccer("usa.nwsl", "nwsl", "NWSL"), r"^nwsl_"),
        ("weather", weather, r"^w_"),
        ("politics", politics, r"^r_(house|senate|sweep)"),
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
    markets = [m for m in markets if datetime.fromisoformat(m["t"].replace("Z", "+00:00")) > NOW]
    asof = datetime.now(CT)
    with open(OUT, "w") as f:
        json.dump({"asof": f"{asof.strftime('%b')} {asof.day}, {asof.year}", "markets": markets, "stats": stats}, f, indent=1)
    print("wrote", len(markets), "markets")


if __name__ == "__main__":
    main()
