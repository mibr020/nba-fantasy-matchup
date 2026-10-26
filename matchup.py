#!/usr/bin/env python3
"""
ESPN Fantasy Basketball H2H-categories matchup projector + lineup planner.

For every league in ESPN_LEAGUES it pulls the current matchup period, every team's
roster, player averages and the NBA schedule, and writes ONE self-contained HTML page:
  * a tab per league; categories are read from each league's own scoring settings
    (8-cat, 9-cat with TO, 3P%, DD... whatever the league uses)
  * pick any team in the league to see their matchup from their side (shareable link)
  * each player's weekly contribution = per-game average x NBA games this matchup
  * tap players to start/bench; "Optimise" finds the legal lineup that wins the most cats

Setup (private leagues need two cookies from a logged-in browser on espn.com):
  ESPN_S2   -> cookie "espn_s2"
  ESPN_SWID -> cookie "SWID"   (looks like {XXXXXXXX-XXXX-...})
Optional:
  ESPN_LEAGUES  comma-separated league ids (default below)
  ESPN_SEASON   2027 = the 2026-27 season

Usage:
  python matchup.py                 # current matchup period in every league
  python matchup.py --week 2        # a specific matchup period
  python matchup.py --demo          # offline demo data, no ESPN calls
Only dependency: requests (pip install requests).
"""
import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone, timedelta

CONFIG = {
    "LEAGUES": [int(x) for x in os.environ.get("ESPN_LEAGUES", "59026,1657628848").split(",") if x.strip()],
    "SEASON": int(os.environ.get("ESPN_SEASON", "2027")),  # ESPN labels 2026-27 as 2027
    "ESPN_S2": os.environ.get("ESPN_S2", ""),
    "SWID": os.environ.get("ESPN_SWID", ""),
}
# Fallback for picking "your" team when the SWID owner match fails: league id -> team name.
TEAM_HINTS = {1657628848: "Mazin's Magnificent Team"}

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{season}"

# ESPN stat ids for per-game averages we keep
STAT_IDS = {0: "PTS", 1: "BLK", 2: "STL", 3: "AST", 4: "OREB", 5: "DREB", 6: "REB", 11: "TO",
            13: "FGM", 14: "FGA", 15: "FTM", 16: "FTA", 17: "3PM", 18: "3PA", 37: "DD", 38: "TD"}
# ESPN scoring statId -> category definition
RATIO_CATS = {19: ("FG%", "FGM", "FGA"), 20: ("FT%", "FTM", "FTA"), 21: ("3P%", "3PM", "3PA")}
CAT_ORDER = ["FG%", "FT%", "3P%", "3PM", "FGM", "FTM", "REB", "OREB", "DREB", "AST", "STL", "BLK",
             "TO", "DD", "TD", "PTS", "FGA", "FTA", "3PA"]
DEFAULT_CATS = [19, 20, 17, 6, 3, 2, 1, 0]  # 8-cat, used if a league's settings can't be read

SLOT_NAMES = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F",
              7: "SG/SF", 8: "G/F", 9: "PF/C", 10: "F/C", 11: "UTIL", 12: "BE", 13: "IR"}
POS_NAMES = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C"}  # eligibleSlots ids
SPLIT_NAMES = {0: "season", 1: "last7", 2: "last15", 3: "last30"}
DEBUG_PLAYER = os.environ.get("DEBUG_PLAYER", "Jalen Johnson")  # logs which stat entries were used


# ----------------------------------------------------------------------------- fetch
class AccessError(Exception):
    pass


def espn_get(url, params=None):
    import requests
    cookies = {}
    if CONFIG["ESPN_S2"] and CONFIG["SWID"]:
        cookies = {"espn_s2": CONFIG["ESPN_S2"], "SWID": CONFIG["SWID"]}
    r = requests.get(url, params=params, cookies=cookies, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code in (401, 403):
        raise AccessError(f"ESPN refused access ({r.status_code}) for {url}. "
                          "Private league: check ESPN_S2 / ESPN_SWID and that you're a member.")
    r.raise_for_status()
    return r.json()


def fetch_league(league_id):
    base = BASE.format(season=CONFIG["SEASON"])
    return espn_get(f"{base}/segments/0/leagues/{league_id}",
                    params=[("view", v) for v in
                            ("mTeam", "mRoster", "mSettings", "mMatchup", "mStatus")])


def fetch_pro():
    return espn_get(BASE.format(season=CONFIG["SEASON"]), params={"view": "proTeamSchedules_wl"})


# ----------------------------------------------------------------------------- parse
def player_averages(player):
    """Return {basis_name: {stat: per-game avg}} from the stat entries ESPN sent."""
    out = {}
    for s in player.get("stats") or []:
        avg = s.get("averageStats") or {}
        if not avg:
            continue
        src, split, season = s.get("statSourceId"), s.get("statSplitTypeId"), s.get("seasonId")
        # Only full-season projections for this season, this season's actuals by split,
        # and full-season actuals for past seasons. (ESPN sends many more entries.)
        if src == 1 and split == 0 and season == CONFIG["SEASON"]:
            name = "projected"
        elif src == 0 and season == CONFIG["SEASON"] and split in SPLIT_NAMES:
            name = SPLIT_NAMES[split]
        elif src == 0 and split == 0 and season and season < CONFIG["SEASON"]:
            name = f"season{season - 1}-{str(season)[2:]}"
        else:
            continue
        if DEBUG_PLAYER and DEBUG_PLAYER in player.get("fullName", ""):
            print(f"  {player['fullName']}: {name} (season {season}, src {src}, split {split}) "
                  f"AST {avg.get('3', avg.get(3))} PTS {avg.get('0', avg.get(0))}")
        vals = {k: float(avg.get(str(i), avg.get(i, 0)) or 0) for i, k in STAT_IDS.items()}
        if any(vals.values()):
            out[name] = {k: round(v, 3) for k, v in vals.items() if v}
    return out


def league_cats(settings):
    items = (settings.get("scoringSettings") or {}).get("scoringItems") or []
    cats = []
    for it in items:
        sid, rev = it.get("statId"), bool(it.get("isReverseItem"))
        if sid in RATIO_CATS:
            label, num, den = RATIO_CATS[sid]
            cats.append({"k": label, "type": "ratio", "num": num, "den": den, "rev": rev})
        elif sid in STAT_IDS:
            cats.append({"k": STAT_IDS[sid], "type": "count", "rev": rev or sid == 11})
        else:
            print(f"  ! category statId {sid} isn't supported yet; it will be left out")
    if not cats:
        print("  ! couldn't read scoring categories; assuming standard 8-cat")
        return league_cats({"scoringSettings": {"scoringItems": [{"statId": s} for s in DEFAULT_CATS]}})
    cats.sort(key=lambda c: CAT_ORDER.index(c["k"]) if c["k"] in CAT_ORDER else 99)
    return cats


def _et():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone(timedelta(hours=-4))


def matchup_games(pro, mapping, period):
    """Games per pro team in this matchup (total and from today on), counted by game date.

    ESPN's matchupPeriods mapping isn't reliable for this: in some leagues it lists daily
    scoring periods, in others (weekly-lineup leagues) it looks like NBA week numbers,
    e.g. matchup 2 -> [2]. So the window is worked out from real game dates instead.
    """
    et = _et()
    team_dates, sp_date = {}, {}
    for t in pro["settings"]["proTeams"]:
        ds = []
        for sp, gl in (t.get("proGamesByScoringPeriod") or {}).items():
            if int(sp) < 1:
                continue
            for g in gl:
                if g.get("date"):
                    d = datetime.fromtimestamp(g["date"] / 1000, et).date()
                    ds.append(d)
                    sp_date[int(sp)] = min(sp_date.get(int(sp), d), d)
        team_dates[t["id"]] = ds
    print(f"  ESPN mapping for matchup {period}: {mapping}")
    if not sp_date:
        raise RuntimeError("NBA schedule came back empty")

    opener = min(sp_date.values())
    week1_end = opener + timedelta(days=(6 - opener.weekday()) % 7)   # first Sunday

    def nba_week(w):
        if w <= 1:
            return opener, week1_end
        s = week1_end + timedelta(days=1 + 7 * (w - 2))
        return s, s + timedelta(days=6)

    m = sorted(int(x) for x in mapping)
    daily = len(m) >= 4 and m == list(range(m[0], m[-1] + 1))
    if daily:  # daily scoring periods are consecutive days from opening night (sp 1)
        dof = lambda sp: sp_date.get(sp, opener + timedelta(days=sp - 1))
        first = dof(m[0])
        start, end = first, max(dof(m[-1]), first + timedelta(days=(6 - first.weekday()) % 7))
    else:  # treat as NBA week numbers (2-week playoff rounds come through as two weeks)
        start, end = nba_week(m[0])[0], nba_week(m[-1])[1]
    today = datetime.now(et).date()
    print(f"  Matchup {period} window {start} → {end} ({'daily ids' if daily else 'week ids'}); today {today}")

    games = {}
    for t in pro["settings"]["proTeams"]:
        ds = [d for d in team_dates.get(t["id"], []) if start <= d <= end]
        games[t["id"]] = {"total": len(ds), "remaining": sum(d >= today for d in ds),
                          "abbrev": t.get("abbrev", "")}
    print("  Games: " + ", ".join(f"{g['abbrev']} {g['total']}" for g in
                                    sorted(games.values(), key=lambda g: g["abbrev"]) if g["abbrev"]))
    return games


def team_name(t):
    return t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip() or f"Team {t['id']}"


def build_league(league, pro, week_override=None):
    settings = league["settings"]
    status = league.get("status", {})
    lid = league.get("id")
    period = int(week_override or status.get("currentMatchupPeriod") or 1)
    sp_map = settings["scheduleSettings"]["matchupPeriods"]
    games = matchup_games(pro, sp_map[str(period)], period)

    def roster(t):
        rows = []
        for e in t.get("roster", {}).get("entries", []):
            p = e["playerPoolEntry"]["player"]
            g = games.get(p.get("proTeamId"), {"total": 0, "remaining": 0, "abbrev": "FA"})
            rows.append({
                "id": p["id"],
                "name": p.get("fullName", "?"),
                "team": g["abbrev"],
                "pos": "/".join(POS_NAMES[x] for x in p.get("eligibleSlots", []) if x in POS_NAMES),
                "eligible": [x for x in p.get("eligibleSlots", []) if x not in (12, 13)],
                "slot": e.get("lineupSlotId"),
                "injury": p.get("injuryStatus") or ("INJURED" if p.get("injured") else "ACTIVE"),
                "games": g["total"], "remaining": g["remaining"],
                "avg": player_averages(p),
            })
        return rows

    teams = {}
    me = None
    swid = CONFIG["SWID"].upper()
    hint = TEAM_HINTS.get(lid, "").lower()
    for t in league["teams"]:
        name = team_name(t)
        teams[t["id"]] = {"id": t["id"], "name": name, "players": roster(t)}
        if swid and swid in [o.upper() for o in (t.get("owners") or [])]:
            me = t["id"]
    if me is None and hint:
        me = next((tid for tid, t in teams.items() if t["name"].lower() == hint), None)

    matchups = []
    for m in league.get("schedule", []):
        if m.get("matchupPeriodId") != period:
            continue
        h, a = m.get("home", {}).get("teamId"), m.get("away", {}).get("teamId")
        if h in teams and a in teams:
            matchups.append([h, a])

    slot_counts = {int(k): v for k, v in settings["rosterSettings"]["lineupSlotCounts"].items()
                   if v and int(k) not in (12, 13)}
    cats = league_cats(settings)
    print(f"  {settings.get('name')}: {len(teams)} teams, {len(matchups)} matchups, "
          f"cats {[c['k'] for c in cats]}, you = {teams[me]['name'] if me else 'not found'}")
    return {
        "id": lid,
        "name": settings.get("name", f"League {lid}"),
        "period": period,
        "cats": cats,
        "slots": {SLOT_NAMES.get(k, str(k)): {"id": k, "n": v} for k, v in slot_counts.items()},
        "teams": teams,
        "matchups": matchups,
        "me": me,
    }


# ----------------------------------------------------------------------------- demo
def demo_model():
    import random
    random.seed(4)
    G, F, C = [0, 5, 11], [3, 6, 11], [4, 11]
    W = [1, 2, 5, 6, 7, 11]
    shapes = [("PG", G), ("SG", [1, 5, 11]), ("SF", [2, 6, 11]), ("PF", F), ("C", C), ("SG/SF", W)]
    slots_seq = [0, 1, 2, 3, 4, 5, 6, 11, 11, 11, 12, 12, 12]
    pid = [0]

    def mk(tname, i):
        pos, elig = shapes[i % len(shapes)]
        if slots_seq[i] == 5:
            pos, elig = "PG", G
        if slots_seq[i] == 6:
            pos, elig = "PF", F
        pts, reb, ast = random.uniform(10, 27), random.uniform(3, 12), random.uniform(1, 9)
        fga, fta, tpa = pts / 2.2, pts / 5.5, random.uniform(1, 8)
        avg = {"PTS": pts, "REB": reb, "AST": ast, "STL": random.uniform(.5, 1.7),
               "BLK": random.uniform(.8, 2.6) if "C" in pos else random.uniform(.1, .9),
               "3PA": tpa, "3PM": tpa * random.uniform(.3, .42), "FGA": fga,
               "FGM": fga * random.uniform(.43, .6), "FTA": fta, "FTM": fta * random.uniform(.6, .92),
               "TO": random.uniform(.8, 3.5)}
        avg = {k: round(v, 2) for k, v in avg.items()}
        pid[0] += 1
        g = random.choice([2, 3, 3, 4, 4])
        return {"id": pid[0], "name": f"{tname} P{i + 1}", "team": random.choice(["BOS", "DEN", "MEM", "NYK"]),
                "pos": pos, "eligible": elig, "slot": slots_seq[i], "injury": "ACTIVE", "games": g,
                "remaining": g, "avg": {"projected": avg,
                                        "season2025-26": {k: round(v * random.uniform(.85, 1.1), 2) for k, v in avg.items()}}}

    def league(lid, name, cat_ids, tnames):
        teams = {i + 1: {"id": i + 1, "name": n, "players": [mk(n.split()[0], j) for j in range(13)]}
                 for i, n in enumerate(tnames)}
        cats = league_cats({"scoringSettings": {"scoringItems": [{"statId": s, "isReverseItem": s == 11} for s in cat_ids]}})
        return {"id": lid, "name": name, "period": 1, "cats": cats,
                "slots": {"PG": {"id": 0, "n": 1}, "SG": {"id": 1, "n": 1}, "SF": {"id": 2, "n": 1},
                          "PF": {"id": 3, "n": 1}, "C": {"id": 4, "n": 1}, "G": {"id": 5, "n": 1},
                          "F": {"id": 6, "n": 1}, "UTIL": {"id": 11, "n": 3}},
                "teams": teams, "matchups": [[1, 2], [3, 4]], "me": 1}
    return {"generated": datetime.now(timezone.utc).isoformat(), "leagues": [
        league(1, "Demo League (8-cat)", DEFAULT_CATS, ["Alpha Squad", "Bravo Ballers", "Charlie Crew", "Delta Dunkers"]),
        league(2, "Demo League (9-cat)", DEFAULT_CATS + [11], ["Echo Elite", "Foxtrot Five", "Golf Gang", "Hotel Hoops"]),
    ]}


# ----------------------------------------------------------------------------- html
def render_html(model):
    data = json.dumps(model, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.replace("/*__DATA__*/null", data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, help="matchup period id (default: current)")
    ap.add_argument("--out", default="matchup.html")
    ap.add_argument("--demo", action="store_true", help="use built-in demo data")
    ap.add_argument("--json", action="store_true", help="also dump the model as matchup.json")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if a.demo:
        model = demo_model()
    else:
        pro = fetch_pro()
        leagues = []
        for lid in CONFIG["LEAGUES"]:
            print(f"League {lid}")
            try:
                leagues.append(build_league(fetch_league(lid), pro, a.week))
            except AccessError as e:
                print(f"  ! {e}")
            except Exception as e:  # keep the other leagues going
                print(f"  ! failed: {type(e).__name__}: {e}")
        if not leagues:
            sys.exit("No leagues could be loaded.")
        model = {"generated": datetime.now(timezone.utc).isoformat(), "leagues": leagues}

    out = os.path.abspath(a.out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(model))
    if a.json:
        with open(os.path.splitext(out)[0] + ".json", "w") as f:
            json.dump(model, f, indent=1)
    print(f"Wrote {out} ({len(model['leagues'])} league(s))")
    if not a.no_open:
        try:
            webbrowser.open("file://" + out)
        except Exception:
            pass


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Matchup Planner</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1c1b19;--mute:#6f6b64;--line:#e4e1db;--me:#2563eb;--opp:#d9480f;--win:#15803d;--lose:#b91c1c;--tie:#8a6d00;--chip:#efece6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121211;--card:#1c1c1a;--ink:#eeece8;--mute:#9a968e;--line:#2e2d2a;--me:#6ea0ff;--opp:#ff8a4c;--win:#4ade80;--lose:#f87171;--tie:#facc15;--chip:#2a2926}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
main{max-width:760px;margin:0 auto;padding:12px 16px 48px}
.tabs{display:flex;gap:6px;overflow-x:auto;margin:0 -16px 12px;padding:0 16px;scrollbar-width:none}
.tabs button{flex:none;border-radius:999px;padding:7px 14px;font-size:14px;font-weight:600;color:var(--mute)}
.tabs button.on{background:var(--ink);color:var(--bg);border-color:transparent}
h1{font-size:20px;margin:0 0 2px}
.sub{color:var(--mute);font-size:13px}
.pick{display:flex;gap:8px;margin:12px 0 0}
.pick select{flex:1;min-width:0}
.vs{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin:14px 0 6px}
.vs b{font-size:16px}.vs .me{color:var(--me)}.vs .opp{color:var(--opp);text-align:right}
.score{font-size:34px;font-weight:700;text-align:center;font-variant-numeric:tabular-nums;margin:4px 0}
.score small{display:block;font-size:12px;font-weight:500;color:var(--mute)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin:12px 0}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
select,button{font:inherit;color:var(--ink);background:var(--chip);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
button{cursor:pointer}
button.primary{background:var(--me);color:#fff;border-color:transparent;font-weight:600}
label.tog{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--mute)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.cats td,.cats th{padding:7px 4px;border-bottom:1px solid var(--line);text-align:right}
.cats th{font-size:12px;color:var(--mute);font-weight:500}
.cats td:first-child,.cats th:first-child{text-align:left;font-weight:600}
.cats td:first-child small{font-weight:400;color:var(--mute);font-size:11px}
.cats td.m{color:var(--me)}.cats td.o{color:var(--opp)}
.res{font-size:12px;font-weight:700;padding:2px 7px;border-radius:999px}
.W{color:var(--win)}.L{color:var(--lose)}.T{color:var(--tie)}
.bar{height:5px;border-radius:3px;background:var(--line);position:relative;overflow:hidden;margin-top:3px}
.bar i{position:absolute;top:0;bottom:0;left:50%}
.warn{color:var(--lose);font-size:13px;margin:6px 0 0}
h2{font-size:15px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:baseline;gap:8px}
h2 b{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
h2 span{flex:none;font-size:12px;color:var(--mute);font-weight:400}
.pl{display:flex;flex-wrap:wrap;row-gap:4px;gap:10px;align-items:center;padding:9px 4px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none}
.pl:last-child{border-bottom:0}
.pl.bench{opacity:.5}
.slot{width:46px;flex:none;font-size:11px;font-weight:700;text-align:center;border-radius:8px;padding:4px 0;background:var(--chip);color:var(--mute)}
.pl.start .slot{background:var(--me);color:#fff}
#opp .pl.start .slot{background:var(--opp)}
.pl .nm{flex:1;min-width:0}
.pl .nm b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
.pl .nm em{font-style:normal;font-size:12px;color:var(--mute)}
.inj{font-size:10px;font-weight:700;color:var(--lose);margin-left:4px}
.gp{flex:none;text-align:right;font-size:11px;color:var(--mute);width:52px}
.gp b{display:block;font-size:17px;color:var(--ink)}
.line{flex:0 0 100%;display:grid;gap:2px;font-size:12.5px;color:var(--ink);font-variant-numeric:tabular-nums}
.line span{text-align:center;white-space:nowrap}
.line.none{display:block;color:var(--mute);font-size:11px}
.pl.hdr{cursor:default;padding:0 4px 4px;position:sticky;top:0;background:var(--card);z-index:1}
.pl.hdr .line{color:var(--mute);font-size:10px;font-weight:700;letter-spacing:.02em}
.note{font-size:12px;color:var(--mute);margin-top:8px}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.on{opacity:1}
</style>
</head>
<body>
<main>
  <nav class="tabs" id="tabs"></nav>
  <h1 id="title">Matchup</h1>
  <div class="sub" id="meta"></div>
  <div class="pick">
    <select id="teamSel" aria-label="Team"></select>
    <button id="swap" title="See this matchup from the other side" aria-label="Swap sides">⇄</button>
    <button id="share">Share</button>
  </div>

  <div class="vs"><b class="me" id="meName"></b><b class="opp" id="oppName"></b></div>
  <div class="score" id="score"></div>

  <div class="card controls">
    <select id="basis" aria-label="Stat basis"></select>
    <select id="gmode" aria-label="Games counted">
      <option value="games">Games: full matchup</option>
      <option value="remaining">Games: remaining only</option>
    </select>
    <label class="tog"><input type="checkbox" id="skipOut" checked> Zero out OUT players</label>
    <button class="primary" id="opt">Optimise lineup</button>
    <button id="reset">Reset</button>
  </div>

  <div class="card">
    <table class="cats"><thead><tr><th>Cat</th><th id="thMe">You</th><th id="thOpp">Opp</th><th>Win %</th><th></th></tr></thead><tbody id="cats"></tbody></table>
    <div class="warn" id="warn"></div>
  </div>

  <div class="card" id="me"><h2><b id="meHead"></b><span>tap to start / bench</span></h2><div id="meList"></div></div>
  <div class="card" id="opp"><h2><b id="oppHead"></b><span>current lineup · tap to edit</span></h2><div id="oppList"></div></div>
  <div class="note">Weekly total = per-game average × NBA games in this matchup. Win % is a rough estimate from the margin and normal week-to-week spread. Lineup edits here are only a what-if: nothing is changed on ESPN.</div>
</main>
<div class="toast" id="toast"></div>
<script>
const DATA = /*__DATA__*/null;
const SLOT_ORDER = ["PG","SG","SF","PF","C","G","F","SG/SF","G/F","PF/C","F/C","UTIL"];
const BENCH = 12, IR = 13;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

let Lg, CATS, NEED, slotList, slotName, MAX_START;   // per-league
let left, right;                                       // team ids being compared
const starts = {};                                     // "leagueId:teamId" -> Set of player ids

function opponentOf(tid) {
  const m = Lg.matchups.find(m => m.includes(tid));
  return m ? (m[0] === tid ? m[1] : m[0]) : null;
}
function startSet(tid) {
  const k = Lg.id + ":" + tid;
  if (!starts[k]) starts[k] = new Set(Lg.teams[tid].players.filter(p => p.slot !== BENCH && p.slot !== IR).map(p => p.id));
  return starts[k];
}
function resetTeam(tid) { delete starts[Lg.id + ":" + tid]; }

// ---------------------------------------------------------------- league setup
function selectLeague(i, tid) {
  Lg = DATA.leagues[i];
  CATS = Lg.cats;
  NEED = [...new Set(CATS.flatMap(c => c.type === "ratio" ? [c.num, c.den] : [c.k]))];
  slotList = []; slotName = {};
  SLOT_ORDER.forEach(n => { const s = Lg.slots[n]; if (s) { slotName[s.id] = n; for (let j = 0; j < s.n; j++) slotList.push(s.id); } });
  MAX_START = slotList.length;

  [...$("tabs").children].forEach((b, j) => b.classList.toggle("on", j === i));
  $("title").textContent = Lg.name;
  $("meta").textContent = `Matchup ${Lg.period} · ${CATS.length} categories · updated ${new Date(DATA.generated).toLocaleString()}`;

  const ids = Object.keys(Lg.teams).map(Number);
  $("teamSel").innerHTML = ids
    .sort((a, b) => (b === Lg.me) - (a === Lg.me) || Lg.teams[a].name.localeCompare(Lg.teams[b].name))
    .map(t => { const o = opponentOf(t); return `<option value="${t}">${esc(Lg.teams[t].name)}${t === Lg.me ? " (me)" : ""}${o ? " — vs " + esc(Lg.teams[o].name) : " — no matchup"}</option>`; })
    .join("");

  const bases = new Set();
  ids.forEach(t => Lg.teams[t].players.forEach(p => Object.keys(p.avg).forEach(k => bases.add(k))));
  const pref = ["projected","season","last30","last15","last7"];
  const rank = b => pref.includes(b) ? pref.indexOf(b) : 99;
  const label = {projected:"ESPN projections", season:"This season avg", last7:"Last 7 days", last15:"Last 15 days", last30:"Last 30 days"};
  const keep = $("basis").value;
  $("basis").innerHTML = [...bases].sort((a, b) => rank(a) - rank(b) || b.localeCompare(a))
    .map(b => `<option value="${b}">${label[b] || b.replace("season", "Season ")}</option>`).join("");
  if (keep && bases.has(keep)) $("basis").value = keep;

  selectTeam(tid && Lg.teams[tid] ? tid : (Lg.me || Lg.matchups[0]?.[0] || ids[0]));
}
function selectTeam(tid, opp) {
  left = tid; right = opp ?? opponentOf(tid);
  $("teamSel").value = String(tid);
  try { history.replaceState(null, "", `#l=${Lg.id}&t=${tid}`); } catch (e) {}
  render();
}

// ---------------------------------------------------------------- maths
function avgOf(p) {
  const b = $("basis").value;
  return p.avg[b] || p.avg.projected || p.avg.season || Object.values(p.avg)[0] || null;
}
function gamesOf(p) {
  if ($("skipOut").checked && /^(OUT|SUSPENSION|INJURY_RESERVE)$/.test(p.injury)) return 0;
  return p[$("gmode").value] || 0;
}
function weekly(p) {
  const a = avgOf(p), g = gamesOf(p), o = {};
  NEED.forEach(k => o[k] = a ? (a[k] || 0) * g : 0);
  return o;
}
function catVal(c, t) { return c.type === "ratio" ? (t[c.den] ? t[c.num] / t[c.den] : 0) : t[c.k]; }
function totals(tid) {
  const t = {}; NEED.forEach(k => t[k] = 0);
  if (tid == null) return t;
  const s = startSet(tid);
  Lg.teams[tid].players.forEach(p => { if (s.has(p.id)) { const w = weekly(p); NEED.forEach(k => t[k] += w[k]); } });
  return t;
}
const Phi = z => 1 / (1 + Math.exp(-1.702 * z));
function winProb(c, a, b) {
  let m, sd;
  if (c.type === "ratio") {
    const A = c.den, M = c.num;
    if (!a[A] || !b[A]) return a[A] ? 1 : b[A] ? 0 : .5;
    const p = (a[M] + b[M]) / (a[A] + b[A]);
    sd = Math.sqrt(p * (1 - p) * (1 / a[A] + 1 / b[A])) * 1.3 + 1e-6;
    m = catVal(c, a) - catVal(c, b);
  } else {
    m = a[c.k] - b[c.k];
    sd = Math.sqrt(a[c.k] + b[c.k]) * 1.1 + 0.5;
  }
  const pr = Phi(m / sd);
  return c.rev ? 1 - pr : pr;
}
const expectedCats = (a, b) => CATS.reduce((s, c) => s + winProb(c, a, b), 0);

function assign(tid) {
  const s = startSet(tid);
  const ps = Lg.teams[tid].players.filter(p => s.has(p.id));
  const owner = new Array(slotList.length).fill(-1);
  const tryP = (i, seen) => {
    for (let k = 0; k < slotList.length; k++) {
      if (seen[k] || !ps[i].eligible.includes(slotList[k])) continue;
      seen[k] = true;
      if (owner[k] < 0 || tryP(owner[k], seen)) { owner[k] = i; return true; }
    }
    return false;
  };
  const map = {}; let ok = ps.length <= MAX_START;
  ps.forEach((p, i) => { if (!tryP(i, [])) ok = false; });
  owner.forEach((i, k) => { if (i >= 0) map[ps[i].id] = slotName[slotList[k]]; });
  return { ok, map };
}

function optimise() {
  const opp = totals(right), s = startSet(left);
  const pool = Lg.teams[left].players.filter(p => p.slot !== IR);
  const score = () => assign(left).ok ? expectedCats(totals(left), opp) : -1;
  let best = score(), improved = true, guard = 0;
  while (improved && guard++ < 200) {
    improved = false;
    const on = pool.filter(p => s.has(p.id)), off = pool.filter(p => !s.has(p.id));
    if (on.length < MAX_START) for (const q of off) {
      s.add(q.id); const v = score();
      if (v > best + 1e-6) { best = v; improved = true; break; } s.delete(q.id);
    }
    if (improved) continue;
    outer: for (const p of on) for (const q of off) {
      s.delete(p.id); s.add(q.id);
      const v = score();
      if (v > best + 1e-6) { best = v; improved = true; break outer; }
      s.delete(q.id); s.add(p.id);
    }
  }
  render();
}

// ---------------------------------------------------------------- render
const fmtCat = (c, v) => c.type === "ratio" ? (v ? v.toFixed(3).replace(/^0/, "") : "—") : v.toFixed(1);
const gridCols = () => CATS.map(c => c.type === "ratio" ? "1.3fr" : "minmax(0,1fr)").join(" ");

function renderList(tid, el, side) {
  if (tid == null) { el.innerHTML = `<div class="note">No opponent this matchup.</div>`; return; }
  const a = assign(tid), s = startSet(tid), cols = gridCols();
  const ps = [...Lg.teams[tid].players].sort((x, y) => (s.has(y.id) - s.has(x.id)) || (y.games - x.games));
  const pct = (m, d) => d ? (m / d).toFixed(3).replace(/^0/, "") : "—";
  const num = v => v >= 10 ? v.toFixed(0) : v.toFixed(1);
  el.innerHTML = `<div class="pl hdr"><div class="line" style="grid-template-columns:${cols}">${CATS.map(c => `<span>${c.k}</span>`).join("")}</div></div>` +
    ps.map(p => {
      const on = s.has(p.id), w = weekly(p), g = gamesOf(p);
      const slot = p.slot === IR && !on ? "IR" : on ? (a.map[p.id] || "??") : "BE";
      const inj = p.injury && p.injury !== "ACTIVE" ? `<span class="inj">${esc(p.injury.replace(/_/g, " ").replace("DAY TO DAY", "DTD"))}</span>` : "";
      const vals = CATS.map(c => c.type === "ratio" ? pct(w[c.num], w[c.den]) : num(w[c.k]));
      const line = g ? `<div class="line" style="grid-template-columns:${cols}">${vals.map(v => `<span>${v}</span>`).join("")}</div>`
                     : `<div class="line none">no games counted</div>`;
      const left = $("gmode").value === "remaining" || p.remaining === p.games ? "" : ` · ${p.remaining} left`;
      return `<div class="pl ${on ? "start" : "bench"}" data-side="${side}" data-id="${p.id}">
        <div class="slot">${slot}</div>
        <div class="nm"><b>${esc(p.name)}${inj}</b><em>${esc(p.team)} · ${esc(p.pos)}</em></div>
        <div class="gp"><b>${g}</b>games${left}</div>${line}</div>`;
    }).join("");
}

function render() {
  const L = Lg.teams[left], R = right != null ? Lg.teams[right] : null;
  $("meName").textContent = L.name; $("oppName").textContent = R ? R.name : "—";
  $("meHead").textContent = L.name + (left === Lg.me ? " (me)" : "");
  $("oppHead").textContent = R ? R.name : "Opponent";
  $("opt").textContent = `Optimise ${left === Lg.me ? "my" : "this"} lineup`;
  const a = totals(left), b = totals(right);
  let W = 0, Ls = 0, T = 0;
  $("cats").innerHTML = CATS.map(c => {
    const pr = winProb(c, a, b), va = catVal(c, a), vb = catVal(c, b);
    const tie = Math.abs(va - vb) < (c.type === "ratio" ? .0005 : .05);
    const r = tie ? "T" : ((va > vb) !== !!c.rev) ? "W" : "L";
    r === "W" ? W++ : r === "L" ? Ls++ : T++;
    const w = Math.abs(pr - .5) * 100, lft = pr >= .5 ? 50 : 50 - w;
    return `<tr><td>${c.k}${c.rev ? " <small>(low wins)</small>" : ""}</td><td class="m">${fmtCat(c, va)}</td><td class="o">${fmtCat(c, vb)}</td>
      <td>${Math.round(pr * 100)}%<div class="bar"><i style="left:${lft}%;width:${w}%;background:${pr >= .5 ? "var(--me)" : "var(--opp)"}"></i></div></td>
      <td><span class="res ${r}">${r}</span></td></tr>`;
  }).join("");
  $("score").innerHTML = `${W}–${Ls}${T ? "–" + T : ""}<small>projected · ${expectedCats(a, b).toFixed(1)} of ${CATS.length} cats expected</small>`;
  const am = assign(left), n = startSet(left).size, warns = [];
  if (n > MAX_START) warns.push(`${n} starters; only ${MAX_START} slots.`);
  else if (!am.ok) warns.push("This lineup can't fill the slots legally — check position eligibility.");
  if (n < MAX_START) warns.push(`${MAX_START - n} empty starting slot${MAX_START - n > 1 ? "s" : ""}.`);
  $("warn").textContent = warns.join(" ");
  renderList(left, $("meList"), "L"); renderList(right, $("oppList"), "R");
}

// ---------------------------------------------------------------- events
document.addEventListener("click", e => {
  const row = e.target.closest(".pl[data-id]"); if (!row) return;
  const s = startSet(row.dataset.side === "L" ? left : right), id = +row.dataset.id;
  s.has(id) ? s.delete(id) : s.add(id);
  render();
});
["basis", "gmode", "skipOut"].forEach(id => $(id).addEventListener("change", render));
$("teamSel").onchange = () => selectTeam(+$("teamSel").value);
$("swap").onclick = () => { if (right != null) selectTeam(right, left); };
$("opt").onclick = optimise;
$("reset").onclick = () => { resetTeam(left); if (right != null) resetTeam(right); render(); };
$("share").onclick = async () => {
  const url = location.href.split("#")[0] + `#l=${Lg.id}&t=${left}`;
  const title = `${Lg.teams[left].name} — matchup ${Lg.period}`;
  try {
    if (navigator.share) { await navigator.share({ title, url }); return; }
    await navigator.clipboard.writeText(url); toast("Link copied");
  } catch (e) { if (e && e.name !== "AbortError") prompt("Copy this link:", url); }
};
function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("on"); setTimeout(() => t.classList.remove("on"), 1600); }

// ---------------------------------------------------------------- boot
$("tabs").innerHTML = DATA.leagues.map((l, i) => `<button data-i="${i}">${esc(l.name)}</button>`).join("");
$("tabs").style.display = DATA.leagues.length > 1 ? "" : "none";
$("tabs").onclick = e => { const b = e.target.closest("button"); if (b) selectLeague(+b.dataset.i); };
const h = new URLSearchParams(location.hash.slice(1));
const li = Math.max(0, DATA.leagues.findIndex(l => String(l.id) === h.get("l")));
selectLeague(li, h.get("t") ? +h.get("t") : null);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
