"""Builds data.json for the Free Play Markets page. Run daily by GitHub Actions."""
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


def games(sport, league, prefix, slope):
    # one request per day (simple date format), merged and de-duplicated by event id
    events, seen_ids = [], set()
    for i in range(4):
        day = (NOW + timedelta(days=i)).astimezone(CT).strftime("%Y%m%d")
        data = get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={day}")
        for e in data.get("events", []):
            if e["id"] not in seen_ids:
                seen_ids.add(e["id"])
                events.append(e)
    out = []
    for e in events:
        try:
            if e["status"]["type"]["state"] != "pre":
                continue
            c = e["competitions"][0]
            home = next(t for t in c["competitors"] if t["homeAway"] == "home")
            away = next(t for t in c["competitors"] if t["homeAway"] == "away")
            p = None
            o = (c.get("odds") or [{}])[0]
            ml_h = (o.get("homeTeamOdds") or {}).get("moneyLine")
            ml_a = (o.get("awayTeamOdds") or {}).get("moneyLine")
            if ml_h and ml_a:
                ph, pa = am_to_p(ml_h), am_to_p(ml_a)
                p = ph / (ph + pa)
            else:
                m = re.match(r"^(\S+)\s+(-?\d+(\.\d+)?)$", o.get("details") or "")
                if m:
                    fav, pts = m.group(1), abs(float(m.group(2)))
                    pf = 1 / (1 + math.exp(-slope * pts))
                    p = pf if fav == home["team"].get("abbreviation") else 1 - pf
            if p is None:
                continue
            t = e["date"]
            if len(t) == 17:  # 2026-10-04T17:00Z
                t = t[:-1] + ":00Z"
            out.append({
                "id": f"{prefix}_{e['id']}", "c": "Games", "tag": "", "px": clamp(p), "t": t,
                "q": f"Will the {home['team']['shortDisplayName']} beat the {away['team']['shortDisplayName']}?",
            })
        except Exception:
            continue
    return out


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
        label = f"{p['name'] if p['name'] in ('Today',) else d.strftime('%A')}, {d.strftime('%b')} {d.day}"
        label = f"{d.strftime('%A')}, {d.strftime('%b')} {d.day}"
        t = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
        key = d.strftime("%Y%m%d")
        out.append({"id": f"w_rain_{key}", "c": "Random", "tag": "Weather", "px": clamp(pop / 100),
                    "t": t, "q": f"Will it rain in Houston on {label}?"})
        hi = p["temperature"]
        out.append({"id": f"w_hot_{key}", "c": "Random", "tag": "Weather",
                    "px": clamp(1 / (1 + math.exp(-(hi - 84.5) / 2))), "t": t,
                    "q": f"Will Houston hit a high of 85\u00b0F or more on {label}?"})
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
    mk = lambda i, q, p: {"id": i, "c": "Politics", "tag": "Politics", "px": clamp(p), "t": t, "q": q}
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
    sections = [
        ("nfl", lambda: games("football", "nfl", "nfl", 0.145), r"^nfl_"),
        ("mlb", lambda: games("baseball", "mlb", "mlb", 0.3), r"^mlb_"),
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
    # drop anything already closed
    markets = [m for m in markets if datetime.fromisoformat(m["t"].replace("Z", "+00:00")) > NOW]
    asof = datetime.now(CT)
    with open(OUT, "w") as f:
        json.dump({"asof": f"{asof.strftime('%b')} {asof.day}, {asof.year}", "markets": markets}, f, indent=1)
    print("wrote", len(markets), "markets")


if __name__ == "__main__":
    main()
