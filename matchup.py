#!/usr/bin/env python3
"""
ESPN Fantasy Basketball (H2H 9-cat) weekly matchup projector + lineup planner.

Pulls your league, this week's matchup, both rosters, player averages and the NBA
schedule, then writes a single self-contained HTML page (matchup.html) where:
  * every player's projected weekly contribution = per-game average x games this matchup
  * category totals for you vs. your opponent (FG%/FT% computed from makes/attempts)
  * tap players to start/bench them and watch the category tally update live
  * "Optimise" searches for the starting 10 that wins the most categories
    while respecting your league's slot rules (PG/SG/SF/PF/C/G/F/UTIL...).

Setup (private league needs two cookies from a logged-in browser on espn.com):
  ESPN_S2   -> cookie "espn_s2"
  ESPN_SWID -> cookie "SWID"   (looks like {XXXXXXXX-XXXX-...})
Set them as env vars or edit CONFIG below.

Usage:
  python matchup.py                 # current matchup week
  python matchup.py --week 2        # a specific matchup period
  python matchup.py --team 7        # force your team id if auto-detect fails
  python matchup.py --demo          # offline demo data, no ESPN calls
Only dependency: requests (pip install requests).
"""
import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone

CONFIG = {
    "LEAGUE_ID": int(os.environ.get("ESPN_LEAGUE_ID", "59026")),
    "SEASON": int(os.environ.get("ESPN_SEASON", "2027")),  # ESPN labels 2026-27 as 2027
    "ESPN_S2": os.environ.get("ESPN_S2", ""),
    "SWID": os.environ.get("ESPN_SWID", ""),
    "TEAM_ID": os.environ.get("ESPN_TEAM_ID", ""),  # optional override
}

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{season}"

# ESPN stat ids -> our keys
STAT_IDS = {0: "PTS", 1: "BLK", 2: "STL", 3: "AST", 6: "REB",
            13: "FGM", 14: "FGA", 15: "FTM", 16: "FTA", 17: "3PM"}
SLOT_NAMES = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F",
              7: "SG/SF", 8: "G/F", 9: "PF/C", 10: "F/C", 11: "UTIL", 12: "BE", 13: "IR"}
POS_NAMES = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C"}  # eligibleSlots ids
SPLIT_NAMES = {0: "season", 1: "last7", 2: "last15", 3: "last30"}


# ----------------------------------------------------------------------------- fetch
def espn_get(url, params=None):
    import requests
    cookies = {}
    if CONFIG["ESPN_S2"] and CONFIG["SWID"]:
        cookies = {"espn_s2": CONFIG["ESPN_S2"], "SWID": CONFIG["SWID"]}
    r = requests.get(url, params=params, cookies=cookies, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code in (401, 403):
        sys.exit("ESPN refused access (401/403). Private league: set ESPN_S2 and ESPN_SWID.")
    r.raise_for_status()
    return r.json()


def fetch_live():
    base = BASE.format(season=CONFIG["SEASON"])
    league = espn_get(f"{base}/segments/0/leagues/{CONFIG['LEAGUE_ID']}",
                      params=[("view", v) for v in
                              ("mTeam", "mRoster", "mSettings", "mMatchup", "mStatus")])
    pro = espn_get(base, params={"view": "proTeamSchedules_wl"})
    return league, pro


# ----------------------------------------------------------------------------- parse
def player_averages(player):
    """Return {basis_name: {cat: per-game avg}} from whatever stat splits ESPN sent."""
    out = {}
    for s in player.get("stats") or []:
        avg = s.get("averageStats") or {}
        if not avg:
            continue
        src, split, season = s.get("statSourceId"), s.get("statSplitTypeId"), s.get("seasonId")
        if src == 1:
            name = "projected"
        elif src == 0 and split in SPLIT_NAMES:
            name = SPLIT_NAMES[split] if season == CONFIG["SEASON"] else f"season{season - 1}-{str(season)[2:]}"
        else:
            continue
        vals = {k: float(avg.get(str(i), avg.get(i, 0)) or 0) for i, k in STAT_IDS.items()}
        if any(vals.values()):
            out[name] = {k: round(v, 3) for k, v in vals.items()}
    return out


def build_model(league, pro, week_override=None, team_override=None):
    settings = league["settings"]
    status = league.get("status", {})
    sched = settings["scheduleSettings"]
    period = int(week_override or status.get("currentMatchupPeriod") or 1)
    scoring_periods = [int(x) for x in sched["matchupPeriods"][str(period)]]
    today_sp = int(league.get("scoringPeriodId") or status.get("latestScoringPeriod") or scoring_periods[0])

    # ESPN matchups run through Sunday (US Eastern). If the settings mapping comes back
    # short (seen pre-season: matchup 1 -> opening day only), extend it by game date.
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone(timedelta(hours=-4))
    sp_date = {}
    for t in pro["settings"]["proTeams"]:
        for sp, gl in (t.get("proGamesByScoringPeriod") or {}).items():
            for g in gl:
                if g.get("date"):
                    d = datetime.fromtimestamp(g["date"] / 1000, et).date()
                    sp_date[int(sp)] = min(sp_date.get(int(sp), d), d)
    print(f"ESPN mapping for matchup {period}: scoring periods {scoring_periods}")
    if sp_date and scoring_periods[0] in sp_date:
        first = sp_date[scoring_periods[0]]
        sunday = first + timedelta(days=(6 - first.weekday()) % 7)
        end = max([sunday] + [sp_date[s] for s in scoring_periods if s in sp_date])
        extra = [s for s, d in sp_date.items() if first <= d <= end]
        scoring_periods = sorted(set(scoring_periods) | set(extra))
        print(f"Using {first} → {end}: scoring periods {scoring_periods}")

    # NBA games per pro team, split into total and remaining (scoring period >= today)
    games = {}
    for t in pro["settings"]["proTeams"]:
        by_sp = t.get("proGamesByScoringPeriod") or {}
        total = rem = 0
        days = []
        for sp in scoring_periods:
            n = len(by_sp.get(str(sp), []))
            total += n
            if sp >= today_sp:
                rem += n
            if n:
                days.append(sp)
        games[t["id"]] = {"total": total, "remaining": rem, "abbrev": t.get("abbrev", ""), "days": days}

    teams = {t["id"]: t for t in league["teams"]}

    # my team
    my_id = team_override or CONFIG["TEAM_ID"]
    if my_id:
        my_id = int(my_id)
    else:
        swid = CONFIG["SWID"].upper()
        for t in league["teams"]:
            if swid and swid in [o.upper() for o in (t.get("owners") or [])]:
                my_id = t["id"]
        if not my_id:
            sys.exit("Couldn't detect your team. Re-run with --team <id>. Teams: " +
                     ", ".join(f"{t['id']}={team_name(t)}" for t in league["teams"]))

    opp_id = None
    for m in league.get("schedule", []):
        if m.get("matchupPeriodId") != period:
            continue
        h, a = m.get("home", {}).get("teamId"), m.get("away", {}).get("teamId")
        if my_id in (h, a):
            opp_id = a if h == my_id else h
    if opp_id is None:
        sys.exit(f"No matchup found for team {my_id} in matchup period {period}.")

    def roster(tid):
        rows = []
        for e in teams[tid].get("roster", {}).get("entries", []):
            p = e["playerPoolEntry"]["player"]
            g = games.get(p.get("proTeamId"), {"total": 0, "remaining": 0, "abbrev": "FA", "days": []})
            rows.append({
                "id": p["id"],
                "name": p.get("fullName", "?"),
                "team": g["abbrev"],
                "pos": "/".join(POS_NAMES[x] for x in p.get("eligibleSlots", []) if x in POS_NAMES),
                "eligible": [x for x in p.get("eligibleSlots", []) if x not in (12, 13)],
                "slot": e.get("lineupSlotId"),
                "injury": p.get("injuryStatus") or ("ACTIVE" if not p.get("injured") else "INJURED"),
                "games": g["total"], "remaining": g["remaining"],
                "avg": player_averages(p),
            })
        return rows

    slot_counts = {int(k): v for k, v in settings["rosterSettings"]["lineupSlotCounts"].items()
                   if v and int(k) not in (12, 13)}
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "league": settings.get("name", ""),
        "period": period,
        "scoringPeriods": scoring_periods,
        "todaySP": today_sp,
        "slots": {SLOT_NAMES.get(k, str(k)): {"id": k, "n": v} for k, v in slot_counts.items()},
        "me": {"id": my_id, "name": team_name(teams[my_id]), "players": roster(my_id)},
        "opp": {"id": opp_id, "name": team_name(teams[opp_id]), "players": roster(opp_id)},
    }


def team_name(t):
    return t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip() or f"Team {t['id']}"


# ----------------------------------------------------------------------------- demo
def demo_model():
    import random
    random.seed(4)

    def mk(name, team, pos, elig, slot, base, g):
        pts, reb, ast = base
        fga = pts / 2.3
        fta = pts / 5.5
        avg = {"PTS": pts, "REB": reb, "AST": ast, "STL": round(random.uniform(.6, 1.6), 2),
               "BLK": round(random.uniform(.2, 2.6) if "C" in pos else random.uniform(.1, .9), 2),
               "3PM": round(random.uniform(0, 3), 2), "FGA": round(fga, 2),
               "FGM": round(fga * random.uniform(.43, .6), 2), "FTA": round(fta, 2),
               "FTM": round(fta * random.uniform(.6, .92), 2)}
        return {"id": hash(name) % 10**6, "name": name, "team": team, "pos": pos, "eligible": elig,
                "slot": slot, "injury": "ACTIVE", "games": g, "remaining": g,
                "avg": {"projected": avg, "season2026-27": {k: v * .95 for k, v in avg.items()}}}
    G, F, C, U = [0, 5, 11], [3, 6, 11], [4, 11], [11]
    me = [mk("Guard One", "DEN", "PG", G, 0, (23, 4, 7), 3), mk("Guard Two", "MEM", "SG/PG", [0, 1, 5, 7, 11], 1, (20, 3, 6), 4),
          mk("Wing One", "TOR", "SF/SG", [1, 2, 5, 6, 7, 11], 2, (19, 8, 6), 3), mk("Forward One", "ATL", "PF", F, 3, (23, 11, 8), 4),
          mk("Big One", "LAL", "C", C, 4, (12, 12, 2), 3), mk("Guard Three", "DAL", "PG", G, 5, (24, 5, 5), 2),
          mk("Forward Two", "MEM", "PF", F, 6, (18, 8, 3), 4), mk("Wing Two", "NY", "SF/PF", [2, 3, 6, 7, 11], 11, (17, 5, 2), 3),
          mk("Wing Three", "SAC", "PF/SF", [2, 3, 6, 7, 11], 11, (14, 6, 2), 4), mk("Guard Four", "MIL", "SG/PG", [0, 1, 5, 7, 11], 11, (15, 4, 5), 3),
          mk("Big Two", "MIL", "C", C, 12, (12, 6, 2), 3), mk("Wing Four", "WSH", "SG/SF", [1, 2, 5, 6, 7, 11], 12, (14, 5, 4), 4),
          mk("Big Three", "CHI", "C", C, 12, (12, 7, 3), 2)]
    opp = [mk(f"Opp Player {i}", "BOS", p, e, s, b, g) for i, (p, e, s, b, g) in enumerate([
        ("PG", G, 0, (26, 5, 9), 3), ("SG", [1, 5, 11], 1, (21, 4, 4), 4), ("SF", [2, 6, 11], 2, (18, 6, 3), 3),
        ("PF", F, 3, (20, 9, 3), 4), ("C", C, 4, (16, 11, 3), 3), ("PG", G, 5, (17, 3, 6), 3),
        ("SF", [2, 6, 11], 6, (15, 6, 2), 4), ("C", C, 11, (11, 9, 1), 4), ("PF", F, 11, (13, 7, 2), 3),
        ("SG", [1, 5, 11], 11, (14, 3, 3), 2), ("C", C, 12, (8, 6, 1), 3)])]
    return {"generated": datetime.now(timezone.utc).isoformat(), "league": "Demo League", "period": 1,
            "scoringPeriods": list(range(1, 8)), "todaySP": 1,
            "slots": {"PG": {"id": 0, "n": 1}, "SG": {"id": 1, "n": 1}, "SF": {"id": 2, "n": 1},
                      "PF": {"id": 3, "n": 1}, "C": {"id": 4, "n": 1}, "G": {"id": 5, "n": 1},
                      "F": {"id": 6, "n": 1}, "UTIL": {"id": 11, "n": 3}},
            "me": {"id": 1, "name": "My Team (demo)", "players": me},
            "opp": {"id": 2, "name": "Opponent (demo)", "players": opp}}


# ----------------------------------------------------------------------------- html
def render_html(model):
    tpl = TEMPLATE
    data = json.dumps(model).replace("</", "<\\/")
    return tpl.replace("/*__DATA__*/null", data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, help="matchup period id (default: current)")
    ap.add_argument("--team", type=int, help="your ESPN team id (default: auto from SWID)")
    ap.add_argument("--out", default="matchup.html")
    ap.add_argument("--demo", action="store_true", help="use built-in demo data")
    ap.add_argument("--json", action="store_true", help="also dump the model as matchup.json")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if a.demo:
        model = demo_model()
    else:
        league, pro = fetch_live()
        model = build_model(league, pro, a.week, a.team)

    out = os.path.abspath(a.out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(model))
    if a.json:
        with open(os.path.splitext(out)[0] + ".json", "w") as f:
            json.dump(model, f, indent=1)
    print(f"{model['me']['name']} vs {model['opp']['name']} — matchup {model['period']}")
    print(f"Wrote {out}")
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
main{max-width:760px;margin:0 auto;padding:16px 16px 48px}
h1{font-size:20px;margin:0 0 2px}
.sub{color:var(--mute);font-size:13px}
.vs{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin:14px 0 6px}
.vs b{font-size:16px}.vs .me{color:var(--me)}.vs .opp{color:var(--opp);text-align:right}
.score{font-size:34px;font-weight:700;text-align:center;font-variant-numeric:tabular-nums;margin:4px 0}
.score small{display:block;font-size:12px;font-weight:500;color:var(--mute)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin:12px 0}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
select,button{font:inherit;color:var(--ink);background:var(--chip);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
button.primary{background:var(--me);color:#fff;border-color:transparent;font-weight:600}
label.tog{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--mute)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.cats td,.cats th{padding:7px 4px;border-bottom:1px solid var(--line);text-align:right}
.cats th{font-size:12px;color:var(--mute);font-weight:500}
.cats td:first-child,.cats th:first-child{text-align:left;font-weight:600}
.cats td.m{color:var(--me)}.cats td.o{color:var(--opp)}
.res{font-size:12px;font-weight:700;padding:2px 7px;border-radius:999px}
.W{color:var(--win)}.L{color:var(--lose)}.T{color:var(--tie)}
.bar{height:5px;border-radius:3px;background:var(--line);position:relative;overflow:hidden;margin-top:3px}
.bar i{position:absolute;top:0;bottom:0;left:50%}
.warn{color:var(--lose);font-size:13px;margin:6px 0 0}
h2{font-size:15px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:baseline}
h2 span{font-size:12px;color:var(--mute);font-weight:400}
.pl{display:flex;gap:10px;align-items:center;padding:9px 4px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none}
.pl:last-child{border-bottom:0}
.pl.bench{opacity:.5}
.slot{width:46px;flex:none;font-size:11px;font-weight:700;text-align:center;border-radius:8px;padding:4px 0;background:var(--chip);color:var(--mute)}
.pl.start .slot{background:var(--me);color:#fff}
#opp .pl.start .slot{background:var(--opp)}
.pl .nm{flex:1;min-width:0}
.pl .nm b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
.pl .nm em{font-style:normal;font-size:12px;color:var(--mute)}
.inj{font-size:10px;font-weight:700;color:var(--lose);margin-left:4px}
.gp{flex:none;text-align:right;font-size:12px;color:var(--mute);width:96px}
.gp b{display:block;font-size:17px;color:var(--ink)}
.line{font-size:11px;color:var(--mute);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.note{font-size:12px;color:var(--mute);margin-top:8px}
</style>
</head>
<body>
<main>
  <h1 id="title">Matchup</h1>
  <div class="sub" id="meta"></div>

  <div class="vs"><b class="me" id="meName"></b><b class="opp" id="oppName"></b></div>
  <div class="score" id="score"></div>

  <div class="card controls">
    <select id="basis" aria-label="Stat basis"></select>
    <select id="gmode" aria-label="Games counted">
      <option value="games">Games: full matchup</option>
      <option value="remaining">Games: remaining only</option>
    </select>
    <label class="tog"><input type="checkbox" id="skipOut" checked> Zero out OUT players</label>
    <button class="primary" id="opt">Optimise my lineup</button>
    <button id="reset">Reset</button>
  </div>

  <div class="card">
    <table class="cats"><thead><tr><th>Cat</th><th>You</th><th>Opp</th><th>Win %</th><th></th></tr></thead><tbody id="cats"></tbody></table>
    <div class="warn" id="warn"></div>
  </div>

  <div class="card" id="me"><h2>Your roster <span>tap to start / bench</span></h2><div id="meList"></div></div>
  <div class="card" id="opp"><h2>Opponent <span>their current lineup · tap to edit</span></h2><div id="oppList"></div></div>
  <div class="note">Weekly total = per-game average × NBA games in this matchup. Win % is a rough estimate from the margin and normal week-to-week spread.</div>
</main>
<script>
const D = /*__DATA__*/null;
const CATS = ["FG%","FT%","3PM","REB","AST","STL","BLK","PTS"];
const COUNT = ["3PM","REB","AST","STL","BLK","PTS"];
const SLOT_ORDER = ["PG","SG","SF","PF","C","G","F","SG/SF","G/F","PF/C","F/C","UTIL"];
const BENCH = 12, IR = 13;
const $ = id => document.getElementById(id);

// expand slot list, e.g. [0,1,2,3,4,5,6,11,11,11]
const slotList = [];
const slotName = {};
SLOT_ORDER.forEach(n => { const s = D.slots[n]; if (s) { slotName[s.id] = n; for (let i = 0; i < s.n; i++) slotList.push(s.id); } });
const MAX_START = slotList.length;

// ---- state
const isStarter = p => p.slot !== BENCH && p.slot !== IR;
let start = { me: new Set(), opp: new Set() };
function resetState() {
  start.me = new Set(D.me.players.filter(isStarter).map(p => p.id));
  start.opp = new Set(D.opp.players.filter(isStarter).map(p => p.id));
}

// ---- basis
const basisNames = new Set();
[...D.me.players, ...D.opp.players].forEach(p => Object.keys(p.avg).forEach(k => basisNames.add(k)));
const pref = ["projected","season","last30","last15","last7"];
const bases = [...basisNames].sort((a,b) => (pref.indexOf(a)+99*(pref.indexOf(a)<0)) - (pref.indexOf(b)+99*(pref.indexOf(b)<0)) || b.localeCompare(a));
const label = {projected:"ESPN projections", season:"This season avg", last7:"Last 7 days", last15:"Last 15 days", last30:"Last 30 days"};
$("basis").innerHTML = bases.map(b => `<option value="${b}">${label[b] || b.replace("season","Season ")}</option>`).join("");

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
  ["PTS","REB","AST","STL","BLK","3PM","FGM","FGA","FTM","FTA"].forEach(k => o[k] = a ? (a[k] || 0) * g : 0);
  return o;
}
function totals(side) {
  const t = {PTS:0,REB:0,AST:0,STL:0,BLK:0,"3PM":0,FGM:0,FGA:0,FTM:0,FTA:0};
  D[side].players.forEach(p => { if (start[side].has(p.id)) { const w = weekly(p); for (const k in t) t[k] += w[k]; } });
  t["FG%"] = t.FGA ? t.FGM / t.FGA : 0;
  t["FT%"] = t.FTA ? t.FTM / t.FTA : 0;
  return t;
}

// ---- win probability per category
const Phi = z => 1 / (1 + Math.exp(-1.702 * z));
function winProb(cat, a, b) {
  let m, sd;
  if (cat === "FG%" || cat === "FT%") {
    const [M, A] = cat === "FG%" ? ["FGM","FGA"] : ["FTM","FTA"];
    if (!a[A] || !b[A]) return a[A] ? 1 : b[A] ? 0 : .5;
    const p = (a[M] + b[M]) / (a[A] + b[A]);
    sd = Math.sqrt(p * (1 - p) * (1 / a[A] + 1 / b[A])) * 1.3 + 1e-6;
    m = a[cat] - b[cat];
  } else {
    m = a[cat] - b[cat];
    sd = Math.sqrt(a[cat] + b[cat]) * 1.1 + 0.5; // ~Poisson-ish weekly spread
  }
  return Phi(m / sd);
}
function expectedCats(a, b) { return CATS.reduce((s, c) => s + winProb(c, a, b), 0); }

// ---- lineup legality (bipartite matching of starters to slots)
function assign(side) {
  const ps = D[side].players.filter(p => start[side].has(p.id));
  const slotOwner = new Array(slotList.length).fill(-1);
  const tryP = (i, seen) => {
    for (let s = 0; s < slotList.length; s++) {
      if (seen[s] || !ps[i].eligible.includes(slotList[s])) continue;
      seen[s] = true;
      if (slotOwner[s] < 0 || tryP(slotOwner[s], seen)) { slotOwner[s] = i; return true; }
    }
    return false;
  };
  const out = {}; let ok = ps.length <= MAX_START;
  ps.forEach((p, i) => { if (!tryP(i, [])) ok = false; });
  slotOwner.forEach((i, s) => { if (i >= 0) out[ps[i].id] = slotName[slotList[s]]; });
  return { ok, map: out };
}

// ---- optimiser: local search over start/bench swaps, keeps lineup legal
function optimise() {
  const opp = totals("opp");
  const pool = D.me.players.filter(p => p.slot !== IR);
  const score = () => { const a = assign("me"); return a.ok ? expectedCats(totals("me"), opp) : -1; };
  // seed: fill empty slots with best-games players
  let best = score(), improved = true, guard = 0;
  while (improved && guard++ < 200) {
    improved = false;
    const on = pool.filter(p => start.me.has(p.id)), off = pool.filter(p => !start.me.has(p.id));
    // add if room
    if (on.length < MAX_START) for (const q of off) {
      start.me.add(q.id); const s = score();
      if (s > best + 1e-6) { best = s; improved = true; break; } start.me.delete(q.id);
    }
    if (improved) continue;
    outer: for (const p of on) for (const q of off) {
      start.me.delete(p.id); start.me.add(q.id);
      const s = score();
      if (s > best + 1e-6) { best = s; improved = true; break outer; }
      start.me.delete(q.id); start.me.add(p.id);
    }
  }
  render();
}

// ---- render
const fmt = (c, v) => c.endsWith("%") ? (v ? v.toFixed(3).replace(/^0/, "") : "—") : v.toFixed(1);
function renderList(side, el) {
  const a = assign(side);
  const ps = [...D[side].players].sort((x, y) => (start[side].has(y.id) - start[side].has(x.id)) || (y.games - x.games));
  el.innerHTML = ps.map(p => {
    const on = start[side].has(p.id), w = weekly(p), g = gamesOf(p);
    const slot = p.slot === IR ? "IR" : on ? (a.map[p.id] || "??") : "BE";
    const inj = p.injury && p.injury !== "ACTIVE" ? `<span class="inj">${p.injury.replace(/_/g," ").replace("DAY TO DAY","DTD")}</span>` : "";
    const line = g ? `${w.PTS.toFixed(0)} pts · ${w.REB.toFixed(0)} reb · ${w.AST.toFixed(0)} ast · ${w.STL.toFixed(1)} stl · ${w.BLK.toFixed(1)} blk · ${w["3PM"].toFixed(1)} 3pm` : "no games counted";
    return `<div class="pl ${on ? "start" : "bench"}" data-side="${side}" data-id="${p.id}">
      <div class="slot">${slot}</div>
      <div class="nm"><b>${esc(p.name)}${inj}</b><em>${esc(p.team)} · ${esc(p.pos)}</em><div class="line">${line}</div></div>
      <div class="gp"><b>${g}</b>games${$("gmode").value === "remaining" ? "" : (p.remaining !== p.games ? ` · ${p.remaining} left` : "")}</div></div>`;
  }).join("");
}
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function render() {
  const a = totals("me"), b = totals("opp");
  let W = 0, L = 0, T = 0;
  $("cats").innerHTML = CATS.map(c => {
    const pr = winProb(c, a, b);
    const r = Math.abs(a[c] - b[c]) < (c.endsWith("%") ? .0005 : .05) ? "T" : a[c] > b[c] ? "W" : "L";
    r === "W" ? W++ : r === "L" ? L++ : T++;
    const w = Math.abs(pr - .5) * 100, left = pr >= .5 ? 50 : 50 - w;
    return `<tr><td>${c}</td><td class="m">${fmt(c, a[c])}</td><td class="o">${fmt(c, b[c])}</td>
      <td>${Math.round(pr * 100)}%<div class="bar"><i style="left:${left}%;width:${w}%;background:${pr >= .5 ? "var(--me)" : "var(--opp)"}"></i></div></td>
      <td><span class="res ${r}">${r}</span></td></tr>`;
  }).join("");
  $("score").innerHTML = `${W}–${L}${T ? "–" + T : ""}<small>projected · ${expectedCats(a, b).toFixed(1)} cats expected</small>`;
  const am = assign("me"), n = start.me.size, warns = [];
  if (n > MAX_START) warns.push(`You have ${n} starters; only ${MAX_START} slots.`);
  else if (!am.ok) warns.push("This lineup can't fill your slots legally — check position eligibility.");
  if (n < MAX_START) warns.push(`${MAX_START - n} empty starting slot${MAX_START - n > 1 ? "s" : ""}.`);
  $("warn").textContent = warns.join(" ");
  renderList("me", $("meList")); renderList("opp", $("oppList"));
}

document.addEventListener("click", e => {
  const row = e.target.closest(".pl"); if (!row) return;
  const side = row.dataset.side, id = +row.dataset.id;
  start[side].has(id) ? start[side].delete(id) : start[side].add(id);
  render();
});
["basis","gmode","skipOut"].forEach(id => $(id).addEventListener("change", render));
$("opt").onclick = optimise;
$("reset").onclick = () => { resetState(); render(); };

$("title").textContent = D.league || "Matchup";
$("meta").textContent = `Matchup ${D.period} · updated ${new Date(D.generated).toLocaleString()}`;
$("meName").textContent = D.me.name; $("oppName").textContent = D.opp.name;
resetState(); render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
