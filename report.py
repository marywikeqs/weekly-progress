"""
report.py — the CRO's weekly report, built from one week's submissions.

build() turns the data into plain numbers and lists; render_html() is the
page (prints to a one-page-ish PDF); render_text() is the Slack-ready summary.
Kept apart from server.py so a scheduled Slack/email send can reuse build().
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from html import escape

# Time-saved ranges counted at their midpoints.
TIME_SAVED_HOURS = {"None": 0, "Under 1 hr": 0.5, "1-3 hrs": 2, "3-5 hrs": 4, "5+ hrs": 6}
# upcoming ideas: where each one stands ("start" = ready to go, just needs sign-off)
IDEA_CHOICES = {"start": "Need a greenlight", "discuss": "Let's discuss", "idea": "Just an idea"}
IDEA_STATUS = {**IDEA_CHOICES, "push": "Let's discuss"}      # "push" was sent for a day before the wording changed
TIME_LEFT = ["1–2 days", "3–5 days", "1–2 weeks", "2–4 weeks", "1–3 months", "3+ months", "Ongoing / no end date"]
FLAG_AT = 80  # percent of budget


def hours(u: dict) -> float:
    return TIME_SAVED_HOURS.get(u.get("timeSaved") or "None", 0)


def friday_on_or_after(d: date) -> date:
    return d + timedelta(days=(4 - d.weekday()) % 7)


def current_week() -> str:
    """The week people are reporting on: this Friday Mon–Fri, last Friday on a weekend."""
    t = date.today()
    return (t - timedelta(days=t.weekday() - 4) if t.weekday() > 4 else friday_on_or_after(t)).isoformat()


def weeks_with_data(data: dict, include: str | None = None) -> list:
    weeks = {r["week"] for r in data["updates"]} | {r["week"] for r in data["usage"]}
    weeks.add(current_week())
    if include:
        weeks.add(include)
    return sorted(weeks, reverse=True)


def default_week(data: dict) -> str:
    """The latest week anyone has submitted for, not past the current one."""
    cur = current_week()
    weeks = [w for w in weeks_with_data(data) if w <= cur and any(
        r["week"] == w for r in data["updates"] + data["usage"])]
    return weeks[0] if weeks else cur


# Updates are due by 11 PM Eastern on the Friday the week ends. US Eastern is
# worked out here (DST: 2nd Sunday of March to 1st Sunday of November) so the
# deadline doesn't depend on the server image having a timezone database.
DEADLINE_HOUR = 23


def _eastern_offset(d: date) -> timedelta:
    mar = date(d.year, 3, 8) + timedelta(days=(6 - date(d.year, 3, 8).weekday()) % 7)
    nov = date(d.year, 11, 1) + timedelta(days=(6 - date(d.year, 11, 1).weekday()) % 7)
    return timedelta(hours=-4 if mar <= d < nov else -5)


def deadline(week: str) -> datetime:
    """Friday 11 PM Eastern for the week ending `week`, as a UTC datetime."""
    d = date.fromisoformat(week)
    return datetime(d.year, d.month, d.day, DEADLINE_HOUR, tzinfo=timezone(_eastern_offset(d))).astimezone(timezone.utc)


def eastern(iso_utc: str) -> str:
    """'2026-10-17T13:14:00+00:00' -> 'Sat, Oct 17, 9:14 AM ET'."""
    t = datetime.fromisoformat(iso_utc)
    t = t.astimezone(timezone(_eastern_offset((t - timedelta(hours=5)).date())))
    return f"{t:%a}, {t:%b} {t.day}, {t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'} ET"


def nice_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%a}, {d:%b} {d.day}, {d.year}"


def money(n: float) -> str:
    return f"${n:,.0f}" if float(n).is_integer() else f"${n:,.2f}"


def _week_numbers(data: dict, week: str, roster: list) -> dict:
    ups = [u for u in data["updates"] if u["week"] == week]
    use = [u for u in data["usage"] if u["week"] == week]
    tasks = [t for t in data.get("tasks", []) if t["week"] == week]
    names = {u["name"] for u in ups} | {u["name"] for u in use}
    return {
        "updates": ups, "usage": use, "tasks": tasks,
        "submitted": len(names & set(roster)),
        "projects": len(ups),
        "claude": sum(u["type"] == "Claude" for u in ups),
        "nonClaude": sum(u["type"] != "Claude" for u in ups),
        "hours": sum(hours(u) for u in ups if u["type"] == "Claude") + sum(hours(t) for t in tasks if t["claude"]),
        "spend": sum(u["spend"] for u in use),
        "budget": sum(u["budget"] for u in use),
    }


def build(data: dict, week: str) -> dict:
    roster = [p["name"] for p in data["config"]["roster"] if p["active"]]
    prev = (date.fromisoformat(week) - timedelta(days=7)).isoformat()
    cur, last = _week_numbers(data, week, roster), _week_numbers(data, prev, roster)
    has_prev = bool(last["updates"] or last["usage"])
    same_month = prev[:7] == week[:7]

    # participation
    did_proj = {u["name"] for u in cur["updates"]}
    did_use = {u["name"] for u in cur["usage"]}
    participation = {
        "roster": len(roster),
        "complete": sorted(n for n in roster if n in did_proj and n in did_use),
        "missing": sorted(n for n in roster if n not in did_proj or n not in did_use),
    }
    # late = first sent after the deadline (later edits don't count)
    due = deadline(week)
    participation["deadline"] = f"Fri, {due.astimezone(timezone(_eastern_offset(date.fromisoformat(week)))):%b} {date.fromisoformat(week).day}, 11 PM ET"
    participation["late"] = [{"name": u["name"], "at": eastern(u["submittedAt"])}
                             for u in sorted(cur["usage"], key=lambda u: u["name"])
                             if u["name"] in participation["complete"] and u.get("submittedAt")
                             and datetime.fromisoformat(u["submittedAt"]) > due]
    participation["overdue"] = datetime.now(timezone.utc) > due

    # the digest: each person's week, projects then other work
    first_week: dict = {}
    for u in data["updates"]:
        k = u["project"].lower()
        first_week[k] = min(first_week.get(k, u["week"]), u["week"])
    people = []
    for n in sorted({u["name"] for u in cur["updates"]} | {t["name"] for t in cur["tasks"]}):
        projects = [{**u, "new": first_week[u["project"].lower()] == week}
                    for u in sorted((u for u in cur["updates"] if u["name"] == n), key=lambda u: u["project"].lower())]
        tasks = sorted((t for t in cur["tasks"] if t["name"] == n), key=lambda t: t["category"].lower())
        people.append({"name": n, "projects": projects, "tasks": tasks})

    # Claude impact
    impact: dict = {}
    for u in cur["updates"]:
        if u["type"] == "Claude":
            i = impact.setdefault(u["project"].lower(), {"project": u["project"], "hours": 0, "people": []})
            i["hours"] += hours(u)
            i["people"].append(u["name"])
    for t in cur["tasks"]:
        if t["claude"]:
            i = impact.setdefault("task:" + t["category"].lower(), {"project": t["category"] + " (other work)", "hours": 0, "people": []})
            i["hours"] += hours(t)
            if t["name"] not in i["people"]:
                i["people"].append(t["name"])
    top = sorted((i for i in impact.values() if i["hours"] > 0), key=lambda i: (-i["hours"], i["project"].lower()))[:5]
    # "where could Claude have helped": the weekly retro answer, plus any per-project
    # notes from before that question moved to the end of the form
    suggestions = sorted([{"project": "", "name": u["name"], "text": u["retro"]} for u in cur["usage"]
                          if u.get("retro") and not re.fullmatch(r"(?i)\W*(nothing|none|n/?a|no)\b.*", u["retro"])]
                         + [{"project": u["project"], "name": u["name"], "text": u["couldHelp"]}
                            for u in cur["updates"] if u["type"] != "Claude" and u.get("couldHelp")],
                         key=lambda s: (s["name"], s["project"].lower()))

    # upcoming project ideas, by person
    ideas = [{"name": u["name"], **i} for u in sorted(cur["usage"], key=lambda u: u["name"]) for i in u.get("ideas", [])]

    # other work, grouped by kind
    other: dict = {}
    for t in sorted(cur["tasks"], key=lambda t: t["name"]):
        g = other.setdefault(t["category"].lower(), {"category": t["category"], "entries": [], "saved": 0})
        g["entries"].append(t)
        g["saved"] += hours(t) if t["claude"] else 0
    other_groups = sorted(other.values(), key=lambda g: (-len(g["entries"]), g["category"].lower()))

    # usage and spend
    prev_spend = {u["name"]: u["spend"] for u in last["usage"]} if same_month else {}
    spend_rows = []
    for u in sorted(cur["usage"], key=lambda u: u["name"]):
        pct = 100 * u["spend"] / u["budget"] if u["budget"] else 0
        spend_rows.append({**u, "pct": pct, "flag": pct > FLAG_AT,
                           "weekSpend": u["spend"] - prev_spend[u["name"]] if u["name"] in prev_spend else None})
    requests = [u for u in spend_rows if u["increaseRequested"] and u.get("increase")]

    return {
        "week": week, "prevWeek": prev, "hasPrev": has_prev, "sameMonth": same_month,
        "cur": cur, "last": last, "participation": participation,
        "people": people, "other": other_groups, "top": top, "suggestions": suggestions, "ideas": ideas,
        "spend": spend_rows, "requests": requests,
        "flagged": [u for u in spend_rows if u["flag"]],
        "shots": [u for u in spend_rows if u.get("screenshot")],
    }


# ---------------- text (Slack) ----------------

def _delta(now, before, has_prev, unit=""):
    if not has_prev:
        return ""
    d = now - before
    if abs(d) < 1e-9:
        return " (same as last week)"
    n = f"{abs(d):,.1f}".rstrip("0").rstrip(".") if isinstance(d, float) else f"{abs(d):,}"
    return f" ({'+' if d > 0 else '−'}{n}{unit} vs last week)"


def render_text(r: dict, base_url: str = "") -> str:
    c, l, p, hp = r["cur"], r["last"], r["participation"], r["hasPrev"]
    lines = [f"*Weekly Progress — week ending {nice_date(r['week'])}*", ""]
    lines.append(f"*Participation:* {len(p['complete'])} of {p['roster']} sent their update"
                 + (" · missed the deadline: " + ", ".join(f"{x['name']} (sent {x['at']})" for x in p["late"]) if p["late"] else "")
                 + (f" · hasn't sent: {', '.join(p['missing'])}" if p["missing"] else ""))
    lines.append(f"*Projects:* {c['projects']}{_delta(c['projects'], l['projects'], hp)} — "
                 f"{c['claude']} Claude · {c['nonClaude']} non-Claude")
    for pp in r["people"]:
        line = ", ".join(u["project"] + (f" ({u['timeLeft']} left)" if u.get("timeLeft") else "") for u in pp["projects"])
        if pp["tasks"]:
            line += (" · " if line else "") + "other work: " + ", ".join(t["category"] for t in pp["tasks"])
        lines.append(f"   • *{pp['name']}:* {line}")
    if r["other"]:
        lines.append("*Other work:* " + ", ".join(
            f"{g['category']} ({len(g['entries'])})" for g in r["other"]))
    top = ", ".join(f"{t['project']} ({t['hours']:g} hrs)" for t in r["top"][:3])
    lines.append(f"*Claude time saved:* ~{c['hours']:g} hrs{_delta(c['hours'], l['hours'], hp, ' hrs')}"
                 + (f" · top: {top}" if top else ""))
    if r["suggestions"]:
        lines.append("*Where Claude could have helped:*")
        lines += [f"   • {s['name']}: {s['text']}" for s in r["suggestions"]]
    if r["ideas"]:
        lines.append("*Upcoming project ideas:*")
        lines += [f"   • {i['name']}: {i['idea']}" + (f" [{IDEA_STATUS[i['status']]}]" if i.get("status") in IDEA_STATUS else "")
                  + (f" — {i['why']}" if i["why"] else "") for i in r["ideas"]]
    if c["budget"]:
        lines.append(f"*Spend:* {money(c['spend'])} of {money(c['budget'])} monthly budget ({100 * c['spend'] / c['budget']:.0f}%)"
                     + (" · over 80%: " + ", ".join(f"{u['name']} ({u['pct']:.0f}%)" for u in r["flagged"]) if r["flagged"] else ""))
    for u in r["requests"]:
        i = u["increase"]
        lines.append(f"   • Budget request: {u['name']} → {money(i['amount'])} ({i['status']}) — {i['reason']}")
    if base_url:
        lines += ["", f"Full report: {base_url}/report?week={r['week']}"]
    return "\n".join(lines) + "\n"


# ---------------- HTML ----------------

# Todoist-style: warm sidebar, white page, red accent, and Todoist's project
# colours: each person, section and kind of work gets one (as --c).
PALETTE = ["#4073ff", "#af38eb", "#299438", "#ff9933", "#158fad", "#e05194", "#884dff", "#db4035", "#7ecc49", "#14aaf5"]
GREEN, BLUE, TEAL, ORANGE, GRAPE, ACCENT = "#299438", "#4073ff", "#158fad", "#ff9933", "#884dff", "#dc4c3e"


def colour_for(key: str) -> str:
    """Stable colour for a kind of work, so Support Hub looks the same every week."""
    return PALETTE[sum(map(ord, key.lower())) % len(PALETTE)]


def person_colours(names) -> dict:
    return {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(sorted(names))}


STYLE = """
:root{--bg:#fff;--side:#fcfaf8;--ink:#202020;--muted:#666;--faint:#999;--line:#f0f0f0;--edge:#e6e6e6;--hover:#f5f5f5;
--accent:#dc4c3e;--accent-hover:#c3392c;--accent-soft:#fff6f0;--p1:#d1453b;--p2:#eb8909;--p3:#246fe0;--p4:#999;--done:#058527}
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.layout{display:grid;grid-template-columns:260px 1fr;min-height:100vh}
aside{background:var(--side);border-right:1px solid var(--line);padding:20px 12px;position:sticky;top:0;height:100vh;overflow:auto}
.app{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px;margin:0 0 18px 6px}
.app i{width:22px;height:22px;border-radius:6px;background:var(--accent);display:inline-grid;place-items:center;color:#fff;font-style:normal;font-size:13px}
aside select{width:100%;font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--edge);border-radius:5px;background:var(--bg);color:var(--ink);margin-bottom:14px}
nav a{display:flex;gap:10px;align-items:center;padding:6px 8px;border-radius:5px;color:var(--ink);text-decoration:none;font-size:14px}
nav a i{width:10px;height:10px;border-radius:50%;background:var(--c);flex:none}
nav a em{flex:1;font-style:normal}
nav a:hover{background:var(--hover)}
nav a span{color:var(--faint);font-size:12px}
nav a.alert span{color:var(--p1);font-weight:600}
.actions{border-top:1px solid var(--line);margin-top:14px;padding-top:12px;display:flex;flex-direction:column;gap:2px}
.actions a,.actions button{display:block;text-align:left;width:100%;font:inherit;font-size:13px;color:var(--muted);background:none;border:0;padding:6px 8px;border-radius:5px;cursor:pointer;text-decoration:none}
.actions a:hover,.actions button:hover{background:var(--hover);color:var(--ink)}
main{padding:32px 32px 80px;max-width:860px;width:100%}
h1{font-size:20px;margin:0}
.sub{color:var(--muted);font-size:13px;margin:2px 0 0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:20px 0 6px}
.stat{border-radius:10px;padding:12px 14px;background:color-mix(in srgb,var(--c) 10%,var(--bg));border:1px solid color-mix(in srgb,var(--c) 25%,var(--bg))}
.stat b{display:block;font-size:22px;font-variant-numeric:tabular-nums;color:color-mix(in srgb,var(--c) 85%,var(--ink))}
.stat span{font-size:12px;color:var(--ink);font-weight:600}
.stat i{display:block;font-style:normal;font-size:11.5px;color:var(--muted)}
h2{display:flex;align-items:baseline;gap:8px;font-size:15px;margin:34px 0 0;padding-bottom:6px;border-bottom:2px solid color-mix(in srgb,var(--c,var(--line)) 30%,var(--bg));scroll-margin-top:16px}
h2::before{content:"";width:10px;height:10px;border-radius:3px;background:var(--c,var(--faint));align-self:center;flex:none}
h2 span{color:var(--faint);font-weight:400;font-size:12px}
.note{color:var(--muted);font-size:12.5px;margin:8px 0 0}
.ring{flex:none;width:18px;height:18px;border-radius:50%;border:2px solid var(--p4);margin-top:1px}
.ring.claude{border-color:var(--accent)}
.ring.r-Pending{border-color:var(--p2)}.ring.r-Denied{border-color:var(--p1)}
.ring.sent,.ring.r-Approved{border-color:var(--done);background:var(--done);position:relative}
.ring.sent::after,.ring.r-Approved::after{content:"";position:absolute;left:4px;top:1px;width:4px;height:8px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
.glance{display:grid;gap:2px;margin-top:8px;background:color-mix(in srgb,#ff9933 6%,var(--bg));border:1px solid color-mix(in srgb,#ff9933 25%,var(--bg));border-radius:10px;padding:8px 14px}
.g-row{display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-bottom:1px solid var(--line)}.g-row:last-child{border-bottom:0}
.g-row .av{margin-top:1px}.g-proj{font-size:13.5px}.g-other{font-size:12.5px;color:var(--muted)}
.late{font-size:12px;font-weight:600;color:var(--p1)}
.g-help{margin:4px 0 0;padding-left:18px;font-size:13.5px}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px;align-items:start}
.pcard{border:1px solid var(--edge);border-top:4px solid var(--c);border-radius:10px;padding:12px 16px 4px;min-width:0;background:color-mix(in srgb,var(--c) 4%,var(--bg))}
.pcard .row:last-child{border-bottom:0}
.pcard .row ul li::marker{color:var(--c)}
.phead{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px}
.phead .label{font-weight:400}
.av{flex:none;width:30px;height:30px;border-radius:50%;background:var(--c);color:#fff;font-size:12px;font-weight:700;display:inline-grid;place-items:center;letter-spacing:.02em}
.av.sm{width:24px;height:24px;font-size:10.5px}
.av.off{background:transparent;border:2px dashed var(--faint);color:var(--faint)}
.sublabel{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--c);margin:12px 0 0}
.label{font-size:12px;color:var(--muted)}
.label.claude{color:var(--accent)}
.pill{display:inline-block;font-size:11.5px;font-weight:600;line-height:1.6;padding:0 9px;border-radius:999px;white-space:nowrap;
background:color-mix(in srgb,var(--c) 15%,var(--bg));color:color-mix(in srgb,var(--c) 75%,var(--ink))}
.group-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;margin:22px 0 2px;font-weight:700;font-size:14px}
.group-head .label{font-weight:400}
.group-head .newp{font-weight:400;font-size:11.5px;color:var(--faint)}
.row{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.row .body{flex:1;min-width:0}
.row .top{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.row ul{margin:1px 0 0;padding-left:16px;color:var(--muted);font-size:13.5px}
.row .lh{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--faint);margin-top:6px}
.row .lh.prog{color:#299438}.row .lh.next{color:#4073ff}
.row .meta{font-size:12px;color:var(--muted);margin-top:4px}
.tleft{font-size:12px;font-weight:600;color:var(--muted);white-space:nowrap}
.ideas{list-style:none;padding:0;margin:8px 0 0}.ideas li{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}
.ideas li:last-child{border-bottom:0}.ideas .why{font-size:13px;color:var(--muted)}
.people{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));column-gap:24px}
.people .row{align-items:center;padding:8px 0}
.people .missing{color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:8px 8px 8px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--faint);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:5px;min-width:60px}.bar>div{height:100%;border-radius:3px;background:var(--c,var(--p3))}
td .av{margin-right:8px;vertical-align:middle}
tr.flag td.pct{color:var(--p1);font-weight:600}tr.flag .bar>div{background:repeating-linear-gradient(45deg,var(--p1) 0 6px,color-mix(in srgb,var(--p1) 70%,#fff) 6px 12px)}
.table-scroll{overflow-x:auto}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:28px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-top:12px}
.gallery a{color:var(--muted);text-decoration:none;font-size:12.5px}
.gallery img{width:100%;height:120px;object-fit:cover;object-position:top;border-radius:6px;border:1px solid var(--edge);display:block;margin-bottom:4px;background:var(--hover)}
.empty{color:var(--faint);font-size:13px;padding:10px 0}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 16px;border-radius:6px;font-size:13px;display:none}
@media (max-width:760px){.layout{grid-template-columns:1fr}aside{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}
nav{display:none}.actions{flex-direction:row;flex-wrap:wrap;border:0;margin:0;padding:0}.actions a,.actions button{width:auto;border:1px solid var(--edge)}
main{padding:20px 16px 60px}.cols,.pgrid{grid-template-columns:1fr}}
@media print{*{-webkit-print-color-adjust:exact;print-color-adjust:exact}.pcard{break-inside:avoid}aside,#toast{display:none}.layout{display:block}main{padding:0;max-width:none}body{font-size:11.5px}
h2{break-after:avoid}.row{break-inside:avoid}.gallery{grid-template-columns:repeat(6,1fr)}.gallery img{height:70px}}
"""


def team(u: dict) -> str:
    """'Solo', 'With Cora Gadd, Finance', or '' for updates sent before this was asked."""
    if u.get("solo"):
        return "Solo"
    return "With " + ", ".join(u["with"]) if u.get("with") else ""


def time_left(t: str) -> str:
    """'2–4 weeks' -> '2–4 weeks left'; the end-state options read as they are."""
    return t if t.startswith(("Wrapping", "Ongoing")) else t + " left"


def _pill(text: str, colour: str) -> str:
    return f'<span class="pill" style="--c:{colour}">{escape(text)}</span>'


def _label(t: str) -> str:
    return _pill("Claude project", ACCENT) if t == "Claude" else _pill("Non-Claude project", TEAL)


def _initials(name: str) -> str:
    return "".join(w[0] for w in name.split()[:2]).upper()


def _av(name: str, colour: str, cls: str = "") -> str:
    return f'<span class="av {cls}" style="--c:{colour}" aria-hidden="true">{escape(_initials(name))}</span>'


def _stat(value: str, label: str, note: str = "", colour: str = BLUE) -> str:
    return f'<div class="stat" style="--c:{colour}"><b>{value}</b><span>{label}</span>{f"<i>{note}</i>" if note else ""}</div>'


def _wow(now, before, has_prev, fmt=lambda v: f"{v:g}") -> str:
    if not has_prev:
        return "no prior week"
    d = now - before
    if abs(d) < 1e-9:
        return "same as last week"
    return f"{'▲' if d > 0 else '▼'} {fmt(abs(d))} vs last week"


def render_html(r: dict, weeks: list) -> str:
    c, l, p, hp = r["cur"], r["last"], r["participation"], r["hasPrev"]
    wk = r["week"]
    pc = person_colours(set(p["complete"]) | set(p["missing"]) | {pp["name"] for pp in r["people"]} | {u["name"] for u in r["spend"]})
    opts = "".join(f'<option value="{w}"{" selected" if w == wk else ""}>Week ending {escape(nice_date(w))}</option>' for w in weeks)
    o = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>Weekly Progress Report</title><style>{STYLE}</style></head><body><div class="layout">
<aside><div class="app"><i>✓</i>Weekly Progress</div>
<form method="get" action="/report"><select name="week" onchange="this.form.submit()" aria-label="Week">{opts}</select></form>
<nav><a href="#people" style="--c:{GREEN}"><i></i><em>Participation</em><span>{len(p['complete'])}/{p['roster']}</span></a>
<a href="#glance" style="--c:{ORANGE}"><i></i><em>At a glance</em><span></span></a>
<a href="#digest" style="--c:{BLUE}"><i></i><em>The week, by person</em><span>{c['projects']} proj · {len(c['tasks'])} other</span></a>
{"".join(f'<a href="#p-{i}" style="--c:{pc[pp["name"]]};padding-left:26px"><i></i><em>{escape(pp["name"])}</em><span>{len(pp["projects"]) + len(pp["tasks"])}</span></a>' for i, pp in enumerate(r["people"]))}
<a href="#ideas" style="--c:{TEAL}"><i></i><em>Upcoming ideas</em><span>{len(r['ideas'])}</span></a>
<a href="#impact" style="--c:{ACCENT}"><i></i><em>Claude impact</em><span>{c['hours']:g} hrs</span></a>
<a href="#spend" style="--c:{GRAPE}"{' class="alert"' if r['flagged'] else ''}><i></i><em>Usage and spend</em><span>{money(c['spend'])}</span></a></nav>
<div class="actions"><button type="button" onclick="copySlack()">Copy Slack summary</button><button type="button" onclick="print()">Print / Save as PDF</button>
<a href="/export/updates.csv?week={wk}">Projects CSV</a><a href="/export/usage.csv?week={wk}">Usage CSV</a><a href="/admin">Admin</a></div></aside>
<main><h1>Week ending {escape(nice_date(wk))}</h1><p class="sub">Weekly digest</p>"""]

    spend_note = f"{100 * c['spend'] / c['budget']:.0f}% of {money(c['budget'])}" if c["budget"] else "none reported"
    o.append('<div class="stats">'
             + _stat(f"{len(p['complete'])}/{p['roster']}", "sent their update", (f"{len(p['late'])} late" if p["late"] else "everyone's in") if not p["missing"] else f"{len(p['missing'])} to go", ORANGE if p["late"] else GREEN)
             + _stat(str(c["projects"]), "projects", f"{c['claude']} Claude · {c['nonClaude']} non-Claude", BLUE)
             + _stat(f"{c['hours']:g} hrs", "saved with Claude", _wow(c["hours"], l["hours"], hp, lambda v: f"{v:g} hrs"), ACCENT)
             + _stat(money(c["spend"]), "Claude spend this month", spend_note, GRAPE)
             + "</div>")

    # participation
    o.append(f'<h2 id="people" style="--c:{GREEN}">Participation <span>{len(p["complete"])} of {p["roster"]}</span></h2><div class="people">')
    late = {x["name"]: x["at"] for x in p["late"]}
    for n in p["complete"]:
        o.append(f'<div class="row">{_av(n, pc[n], "sm")}<div class="body">{escape(n)}'
                 + (f'<div class="late">Late: sent {escape(late[n])}</div>' if n in late else "")
                 + '</div><span class="ring sent" title="Sent"></span></div>')
    for n in p["missing"]:
        o.append(f'<div class="row missing">{_av(n, pc[n], "sm off")}<div class="body">{escape(n)} <span class="label">hasn\'t sent yet</span>'
                 + ('<div class="late">Past the deadline</div>' if p["overdue"] else "") + "</div></div>")
    o.append("</div>")
    o.append(f'<p class="note">Due {escape(p["deadline"])}. '
             + ("Everyone is in" + (", on time." if not p["late"] else f'; {len(p["late"])} missed the deadline.') if not p["missing"] else "") + "</p>")

    # at a glance
    o.append(f'<h2 id="glance" style="--c:{ORANGE}">At a glance <span>what the team worked on</span></h2>')
    if not r["people"]:
        o.append('<p class="empty">Nothing sent for this week yet.</p>')
    o.append('<div class="glance">')
    for pp in r["people"]:
        o.append(f'<div class="g-row">{_av(pp["name"], pc[pp["name"]], "sm")}<div><strong>{escape(pp["name"])}</strong>'
                 f'<div class="g-proj">{escape(" · ".join(u["project"] for u in pp["projects"]))}</div>'
                 + (f'<div class="g-other">Other: {escape(", ".join(dict.fromkeys(t["category"] for t in pp["tasks"])))}</div>' if pp["tasks"] else "")
                 + "</div></div>")
    o.append("</div>")
    if r["suggestions"]:
        o.append('<div class="sublabel" style="--c:#dc4c3e">Where Claude could have helped</div><ul class="g-help">'
                 + "".join(f'<li><b>{escape(s["name"])}:</b> {escape(s["text"])}</li>' for s in r["suggestions"]) + "</ul>")

    # the digest, by person
    o.append(f'<h2 id="digest" style="--c:{BLUE}">The week, by person <span>{c["projects"]} project{"" if c["projects"] == 1 else "s"}'
             f' · {len(c["tasks"])} other work item{"" if len(c["tasks"]) == 1 else "s"}</span></h2>')
    if not r["people"]:
        o.append('<p class="empty">Nothing sent for this week yet.</p>')
    o.append('<div class="pgrid">')
    for i, pp in enumerate(r["people"]):
        o.append(f'<div class="pcard" id="p-{i}" style="--c:{pc[pp["name"]]}"><div class="phead">{_av(pp["name"], pc[pp["name"]])}{escape(pp["name"])}'
                 f'<span class="label">{len(pp["projects"])} project{"" if len(pp["projects"]) == 1 else "s"}'
                 + (f' · {len(pp["tasks"])} other' if pp["tasks"] else "") + "</span></div>")
        for u in pp["projects"]:
            meta = ""
            if u["type"] == "Claude" and u.get("timeSaved"):
                meta = f'⏱ Time saved with Claude: {escape(u["timeSaved"])}'
            elif u.get("couldHelp"):
                meta = f'💡 Claude could have helped: {escape(u["couldHelp"])}'
            o.append(f'<div class="row"><span class="ring{" claude" if u["type"] == "Claude" else ""}"></span><div class="body">'
                     f'<div class="top"><strong>{escape(u["project"])}</strong>{_label(u["type"])}'
                     + (_pill("New this week", GREEN) if u["new"] else "")
                     + (f'<span class="tleft">⏳ {escape(time_left(u["timeLeft"]))}</span>' if u.get("timeLeft") else "")
                     + (f'<span class="tleft">{"👤" if u.get("solo") else "👥"} {escape(team(u))}</span>' if team(u) else "") + "</div>"
                     + '<div class="lh prog">Progress</div><ul>' + "".join(f"<li>{escape(b)}</li>" for b in u["progress"]) + "</ul>"
                     + ('<div class="lh next">Next steps</div><ul>' + "".join(f"<li>{escape(b)}</li>" for b in u["nextSteps"]) + "</ul>" if u.get("nextSteps") else "")
                     + (f'<div class="meta">{meta}</div>' if meta else "") + "</div></div>")
        if pp["tasks"]:
            o.append('<div class="sublabel">Other work</div>')
        for t in pp["tasks"]:
            o.append(f'<div class="row"><span class="ring{" claude" if t["claude"] else ""}"></span><div class="body"><div class="top">{_pill(t["category"], colour_for(t["category"]))}'
                     + (_pill("Claude saved " + t["timeSaved"], ACCENT) if t["claude"] else '<span class="label">no Claude</span>') + "</div>"
                     + (f'<div class="meta">{escape(t["note"])}</div>' if t.get("note") else "") + "</div></div>")
        o.append("</div>")
    o.append("</div>")

    # upcoming project ideas
    o.append(f'<h2 id="ideas" style="--c:{TEAL}">Upcoming project ideas <span>{len(r["ideas"])}</span></h2>')
    if r["ideas"]:
        o.append('<ul class="ideas">' + "".join(
            f'<li>{_av(i["name"], pc.get(i["name"], BLUE), "sm")}<div><strong>{escape(i["idea"])}</strong> <span class="label">{escape(i["name"])}</span>'
            + (" " + _pill(IDEA_STATUS[i["status"]], {"start": GREEN, "idea": "#868e96"}.get(i["status"], BLUE)) if i.get("status") in IDEA_STATUS else "")
            + (f'<div class="why">{escape(i["why"])}</div>' if i["why"] else "") + "</div></li>" for i in r["ideas"]) + "</ul>")
    else:
        o.append('<p class="empty">No new ideas this week.</p>')

    # Claude impact
    o.append(f'<h2 id="impact" style="--c:{ACCENT}">Claude impact <span>about {c["hours"]:g} hours saved</span></h2>'
             '<p class="note">Each answer counts at its range midpoint: none 0, under 1 hr 0.5, 1–3 hrs 2, 3–5 hrs 4, 5+ hrs 6.</p>'
             + ('<div class="cols"><div>' if r["suggestions"] else "<div>") + '<div class="group-head">Top Claude time savings</div>')
    if r["top"]:
        o.append('<table><thead><tr><th>Project</th><th>People</th><th class="num">Hours</th></tr></thead><tbody>'
                 + "".join(f'<tr><td>{escape(t["project"])}</td><td>{escape(", ".join(t["people"]))}</td><td class="num">{t["hours"]:g}</td></tr>' for t in r["top"])
                 + "</tbody></table>")
    else:
        o.append('<p class="empty">No time saved reported.</p>')
    if r["suggestions"]:
        o.append('</div><div><div class="group-head">Where Claude could have helped</div>')
        o.append("".join(f'<div class="row">{_av(s["name"], pc.get(s["name"], BLUE), "sm")}<div class="body"><strong>{escape(s["name"])}</strong> '
                         + (f'<span class="label">{escape(s["project"])}</span>' if s["project"] else "")
                         + f'<div class="meta">{escape(s["text"])}</div></div></div>' for s in r["suggestions"]))
        o.append("</div>")
    o.append("</div>")

    # usage and spend
    wk_note = "" if r["sameMonth"] else " A new month started this week, so there's no weekly change."
    o.append(f'<h2 id="spend" style="--c:{GRAPE}">Usage and spend <span>{money(c["spend"])} of {money(c["budget"])}</span></h2>'
             f'<p class="note">Spend so far this month against each person\'s monthly budget. Over {FLAG_AT}% is flagged.{wk_note}</p>')
    if r["spend"]:
        o.append('<div class="table-scroll"><table><thead><tr><th>Name</th><th class="num">Spend</th><th class="num">Budget</th><th>Used</th><th class="num">This week</th></tr></thead><tbody>')
        for u in r["spend"]:
            ws = "—" if u["weekSpend"] is None else ("+" if u["weekSpend"] >= 0 else "−") + money(abs(u["weekSpend"]))
            o.append(f'<tr class="{"flag" if u["flag"] else ""}" style="--c:{pc[u["name"]]}"><td>{_av(u["name"], pc[u["name"]], "sm")}{escape(u["name"])}{" ⚑" if u["flag"] else ""}</td><td class="num">{money(u["spend"])}</td>'
                     f'<td class="num">{money(u["budget"])}</td><td class="pct">{u["pct"]:.0f}%<div class="bar"><div style="width:{min(u["pct"], 100):.0f}%"></div></div></td>'
                     f'<td class="num">{ws}</td></tr>')
        o.append(f'<tr><th>Team</th><th class="num">{money(c["spend"])}</th><th class="num">{money(c["budget"])}</th>'
                 f'<th>{(100 * c["spend"] / c["budget"]) if c["budget"] else 0:.0f}%</th><th></th></tr></tbody></table></div>')
    else:
        o.append('<p class="empty">No usage for this week yet.</p>')
    o.append('<div class="group-head">Budget increase requests</div>')
    if r["requests"]:
        o.append("".join(f'<div class="row"><span class="ring r-{escape(u["increase"]["status"])}"></span>'
                         f'<div class="body"><div class="top"><strong>{escape(u["name"])}</strong><span>{money(u["budget"])} → {money(u["increase"]["amount"])}</span>'
                         f'<span class="label">{escape(u["increase"]["status"])}</span></div><div class="meta">{escape(u["increase"]["reason"])}</div></div></div>'
                         for u in r["requests"]))
    else:
        o.append('<p class="empty">None this week.</p>')
    o.append('<div class="group-head">Usage screenshots</div>')
    if r["shots"]:
        o.append('<div class="gallery">' + "".join(
            f'<a href="/screenshots/{escape(u["screenshot"])}" target="_blank" rel="noopener"><img src="/screenshots/{escape(u["screenshot"])}" alt="Usage screenshot from {escape(u["name"])}" loading="lazy">{escape(u["name"])}</a>'
            for u in r["shots"]) + "</div>")
    else:
        o.append('<p class="empty">None yet.</p>')

    o.append(f"""</main></div><div id="toast"></div><script>
async function copySlack(){{const t=document.getElementById('toast');try{{const r=await fetch('/report.txt?week={wk}');
await navigator.clipboard.writeText(await r.text());t.textContent='Slack summary copied';}}catch(e){{t.textContent='Couldn\\'t copy: '+e.message;}}
t.style.display='block';setTimeout(()=>t.style.display='none',2500);}}</script></body></html>""")
    return "".join(o)
