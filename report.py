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
# non-Claude projects: could Claude help with it?
CLAUDE_FIT = ["Yes", "Maybe", "No"]


def fit_text(u: dict) -> str:
    """'Could Claude help? Maybe — drafting the summaries', or '' when not asked."""
    if not u.get("claudeFit"):
        return ""
    return f"Could Claude help? {u['claudeFit']}" + (f" — {u['claudeHow']}" if u.get("claudeHow") else "")


# "Complete" closes the project out: no next steps needed, and it doesn't carry into the next week
COMPLETE = "Complete"
# the admin roadmap: each item is planned for a month (or unscheduled); one may come from someone's idea
ROADMAP_STATUS = ["Planned", "In progress", "Done"]


def idea_key(name: str, idea: str) -> tuple:
    """Who had the idea and what it is, matched loosely (case and spacing)."""
    return (str(name).strip().lower(), " ".join(str(idea).split()).lower())


def roadmapped(data: dict) -> set:
    """idea_key of every idea that's been added to the roadmap."""
    return {idea_key(s.get("name", ""), s.get("idea", "")) for it in data.get("roadmap", []) if (s := it.get("source"))}


TIME_LEFT = ["1–2 days", "3–5 days", "1–2 weeks", "2–4 weeks", "1–3 months", "3+ months", "Ongoing / no end date", COMPLETE]
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
        # people the email doesn't wait for (e.g. an intern); they still count once they send
        "optional": sorted(p["name"] for p in data["config"]["roster"] if p["active"] and not p.get("required", True)),
    }
    participation["waitingOn"] = [n for n in participation["missing"] if n not in participation["optional"]]
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

    # tweaks: small changes to existing projects or tools, by person
    tweaks = [{"name": u["name"], **t} for u in sorted(cur["usage"], key=lambda u: u["name"]) for t in u.get("tweaks", [])]
    # upcoming project ideas, by person
    rm = roadmapped(data)
    ideas = [{"name": u["name"], **i, "roadmap": idea_key(u["name"], i["idea"]) in rm}
             for u in sorted(cur["usage"], key=lambda u: u["name"]) for i in u.get("ideas", [])]

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
    # budget changes: this week's budget against the person's last one sent
    budget_changes = []
    for u in spend_rows:
        before = max((x for x in data["usage"] if x["name"] == u["name"] and x["week"] < week), key=lambda x: x["week"], default=None)
        if before and before["budget"] != u["budget"]:
            budget_changes.append({"name": u["name"], "from": before["budget"], "to": u["budget"]})

    return {
        "week": week, "prevWeek": prev, "hasPrev": has_prev, "sameMonth": same_month,
        "cur": cur, "last": last, "participation": participation,
        "people": people, "other": other_groups, "top": top, "ideas": ideas, "tweaks": tweaks,
        "fits": sorted((u for u in cur["updates"] if u.get("claudeFit") in ("Yes", "Maybe")),
                       key=lambda u: (u["claudeFit"] != "Yes", u["project"].lower())),
        "spend": spend_rows, "budgetChanges": budget_changes,
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
        line = ", ".join(u["project"] + (f" ({time_left(u['timeLeft'])})" if u.get("timeLeft") else "") for u in pp["projects"])
        if pp["tasks"]:
            line += (" · " if line else "") + "other work: " + ", ".join(t["category"] for t in pp["tasks"])
        lines.append(f"   • *{pp['name']}:* {line}")
    if r["other"]:
        lines.append("*Other work:* " + ", ".join(
            f"{g['category']} ({len(g['entries'])})" for g in r["other"]))
    top = ", ".join(f"{t['project']} ({t['hours']:g} hrs)" for t in r["top"][:3])
    lines.append(f"*Claude time saved:* ~{c['hours']:g} hrs{_delta(c['hours'], l['hours'], hp, ' hrs')}"
                 + (f" · top: {top}" if top else ""))
    if r["tweaks"]:
        lines.append("*Tweaks:*")
        lines += [f"   • {t['name']}: {t['project']} — {t['change']}" + (" (Claude)" if t["claude"] else "") for t in r["tweaks"]]
    if r["fits"]:
        lines.append("*Non-Claude projects Claude could help with:*")
        lines += [f"   • {u['project']} ({u['name']}, {u['claudeFit']}): {u['claudeHow']}" for u in r["fits"]]
    if r["ideas"]:
        lines.append("*Upcoming project ideas:*")
        lines += [f"   • {i['name']}: {i['idea']}" + (f" [{IDEA_STATUS[i['status']]}]" if i.get("status") in IDEA_STATUS else "")
                  + (" [On the roadmap]" if i.get("roadmap") else "")
                  + (f" — {i['why']}" if i["why"] else "") for i in r["ideas"]]
    if c["budget"]:
        lines.append(f"*Spend:* {money(c['spend'])} of {money(c['budget'])} monthly budget ({100 * c['spend'] / c['budget']:.0f}%)"
                     + (" · over 80%: " + ", ".join(f"{u['name']} ({u['pct']:.0f}%)" for u in r["flagged"]) if r["flagged"] else ""))
    for b in r["budgetChanges"]:
        lines.append(f"   • Budget change: {b['name']} {money(b['from'])} → {money(b['to'])}")
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
    return t if t.startswith(("Wrapping", "Ongoing", COMPLETE)) else t + " left"


def tl_badge(t: str) -> str:
    """'⏳ 2–4 weeks left', or '✅ Complete' for a finished project."""
    return ("✅ " if t == COMPLETE else "⏳ ") + time_left(t)


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

# inside the admin page's Digest: no sidebar (the admin page has the week picker and buttons)
EMBED_STYLE = "aside{display:none}.layout{display:block;min-height:0}main{padding:4px 0 24px;max-width:none}body{background:transparent}"
EMBED_BASE = '<base target="_blank">'      # screenshots and CSVs open in a new tab, not inside the frame


def render_html(r: dict, weeks: list, embed: bool = False) -> str:
    c, l, p, hp = r["cur"], r["last"], r["participation"], r["hasPrev"]
    wk = r["week"]
    pc = person_colours(set(p["complete"]) | set(p["missing"]) | {pp["name"] for pp in r["people"]} | {u["name"] for u in r["spend"]})
    opts = "".join(f'<option value="{w}"{" selected" if w == wk else ""}>Week ending {escape(nice_date(w))}</option>' for w in weeks)
    o = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>Weekly Progress Report</title><style>{STYLE}{EMBED_STYLE if embed else ""}</style>{EMBED_BASE if embed else ""}</head><body><div class="layout">
<aside><div class="app"><i>✓</i>Weekly Progress</div>
<form method="get" action="/report"><select name="week" onchange="this.form.submit()" aria-label="Week">{opts}</select></form>
<nav><a href="#people" style="--c:{GREEN}"><i></i><em>Participation</em><span>{len(p['complete'])}/{p['roster']}</span></a>
<a href="#glance" style="--c:{ORANGE}"><i></i><em>At a glance</em><span></span></a>
<a href="#digest" style="--c:{BLUE}"><i></i><em>The week, by person</em><span>{c['projects']} proj · {len(c['tasks'])} other</span></a>
{"".join(f'<a href="#p-{i}" style="--c:{pc[pp["name"]]};padding-left:26px"><i></i><em>{escape(pp["name"])}</em><span>{len(pp["projects"]) + len(pp["tasks"])}</span></a>' for i, pp in enumerate(r["people"]))}
<a href="#tweaks" style="--c:{GRAPE}"><i></i><em>Tweaks</em><span>{len(r['tweaks'])}</span></a>
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
        o.append(f'<div class="row missing">{_av(n, pc[n], "sm off")}<div class="body">{escape(n)} <span class="label">hasn\'t sent yet{" · optional" if n in p["optional"] else ""}</span>'
                 + ('<div class="late">Past the deadline</div>' if p["overdue"] and n not in p["optional"] else "") + "</div></div>")
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
            elif fit_text(u):
                meta = f'🤖 {escape(fit_text(u))}'
            o.append(f'<div class="row"><span class="ring{" claude" if u["type"] == "Claude" else ""}"></span><div class="body">'
                     f'<div class="top"><strong>{escape(u["project"])}</strong>{_label(u["type"])}'
                     + (_pill("New this week", GREEN) if u["new"] else "")
                     + (f'<span class="tleft">{escape(tl_badge(u["timeLeft"]))}</span>' if u.get("timeLeft") else "")
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

    # tweaks
    o.append(f'<h2 id="tweaks" style="--c:{GRAPE}">Tweaks <span>{len(r["tweaks"])}</span></h2>'
             '<p class="note">Small changes to existing projects and tools, short of a full build.</p>')
    if r["tweaks"]:
        o.append('<ul class="ideas">' + "".join(
            f'<li>{_av(t["name"], pc.get(t["name"], BLUE), "sm")}<div><strong>{escape(t["project"])}</strong> <span class="label">{escape(t["name"])}</span>'
            + (" " + _pill("Claude", ACCENT) if t["claude"] else "")
            + f'<div class="why">{escape(t["change"])}</div></div></li>' for t in r["tweaks"]) + "</ul>")
    else:
        o.append('<p class="empty">No tweaks this week.</p>')

    # upcoming project ideas
    o.append(f'<h2 id="ideas" style="--c:{TEAL}">Upcoming project ideas <span>{len(r["ideas"])}</span></h2>')
    if r["ideas"]:
        o.append('<ul class="ideas">' + "".join(
            f'<li>{_av(i["name"], pc.get(i["name"], BLUE), "sm")}<div><strong>{escape(i["idea"])}</strong> <span class="label">{escape(i["name"])}</span>'
            + (" " + _pill(IDEA_STATUS[i["status"]], {"start": GREEN, "idea": "#868e96"}.get(i["status"], BLUE)) if i.get("status") in IDEA_STATUS else "")
            + (" " + _pill("🗺 On the roadmap", GRAPE) if i.get("roadmap") else "")
            + (f'<div class="why">{escape(i["why"])}</div>' if i["why"] else "") + "</div></li>" for i in r["ideas"]) + "</ul>")
    else:
        o.append('<p class="empty">No new ideas this week.</p>')

    # Claude impact
    o.append(f'<h2 id="impact" style="--c:{ACCENT}">Claude impact <span>about {c["hours"]:g} hours saved</span></h2>'
             '<p class="note">Each answer counts at its range midpoint: none 0, under 1 hr 0.5, 1–3 hrs 2, 3–5 hrs 4, 5+ hrs 6.</p>'
             '<div class="group-head">Top Claude time savings</div>')
    if r["top"]:
        o.append('<table><thead><tr><th>Project</th><th>People</th><th class="num">Hours</th></tr></thead><tbody>'
                 + "".join(f'<tr><td>{escape(t["project"])}</td><td>{escape(", ".join(t["people"]))}</td><td class="num">{t["hours"]:g}</td></tr>' for t in r["top"])
                 + "</tbody></table>")
    else:
        o.append('<p class="empty">No time saved reported.</p>')
    if r["fits"]:
        o.append('<div class="group-head">Non-Claude projects Claude could help with</div>'
                 + "".join(f'<div class="row">{_av(u["name"], pc.get(u["name"], BLUE), "sm")}<div class="body"><strong>{escape(u["project"])}</strong> '
                           f'<span class="label">{escape(u["name"])}</span> {_pill(u["claudeFit"], GREEN if u["claudeFit"] == "Yes" else ORANGE)}'
                           f'<div class="meta">{escape(u["claudeHow"])}</div></div></div>' for u in r["fits"]))


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
    if r["budgetChanges"]:
        o.append('<div class="group-head">Budget changes</div>'
                 + "".join(f'<div class="row">{_av(b["name"], pc.get(b["name"], BLUE), "sm")}<div class="body"><div class="top"><strong>{escape(b["name"])}</strong>'
                           f'<span>{money(b["from"])} → {money(b["to"])}</span></div></div></div>' for b in r["budgetChanges"]))
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


# ---------------- projects over time ----------------

# how far off each "Time left" answer is, to tell moving from slipping; Ongoing has no end and Complete is done, so no trend
_TL_RANK = {t: i for i, t in enumerate(TIME_LEFT[:-2])}


def projects(data: dict) -> dict:
    """Every project across all weeks: who, each week's updates, how Time left has moved, and whether it's gone quiet."""
    cur = current_week()
    ups = [u for u in data["updates"] if u["week"] <= cur]
    latest = max((u["week"] for u in ups), default=cur)      # the newest week anyone sent
    groups: dict = {}
    for u in sorted(ups, key=lambda u: (u["week"], u.get("submittedAt") or "")):
        groups.setdefault(u["project"].lower(), []).append(u)
    out = []
    for rows in groups.values():
        last = rows[-1]
        weeks = sorted({u["week"] for u in rows}, reverse=True)
        # per week, the soonest finish anyone gave (Ongoing doesn't count)
        soonest = {}
        for w in weeks:
            ranks = [_TL_RANK[u["timeLeft"]] for u in rows if u["week"] == w and u.get("timeLeft") in _TL_RANK]
            if ranks:
                soonest[w] = min(ranks)
        trail = [TIME_LEFT[soonest[w]] for w in sorted(soonest)]
        trend = ""
        if len(soonest) >= 2:
            now_r, before_r = soonest[max(soonest)], soonest[sorted(soonest)[-2]]
            trend = "slipping" if now_r > before_r else "moving" if now_r < before_r else "steady"
        gap = (date.fromisoformat(latest) - date.fromisoformat(weeks[0])).days // 7
        done = any(u.get("timeLeft") == COMPLETE for u in rows if u["week"] == weeks[0])   # marked complete in its last update
        out.append({
            "project": last["project"], "type": last["type"],
            "people": sorted({u["name"] for u in rows}),
            "first": weeks[-1], "last": weeks[0], "weeks": len(weeks),
            "quietFor": 0 if done else gap,          # weeks since its last update (0 = updated in the latest week)
            "complete": done, "completedOn": weeks[0] if done else None,
            "trend": trend, "trail": [t for i, t in enumerate(trail) if i == 0 or t != trail[i - 1]],
            "timeline": [{"week": w, "entries": sorted((u for u in rows if u["week"] == w), key=lambda u: u["name"])}
                         for w in weeks],
        })
    order = {"slipping": 0, "moving": 1, "steady": 2, "": 3}
    out.sort(key=lambda p: (p["complete"], p["quietFor"] > 0, order[p["trend"]], p["project"].lower()))
    active = [p for p in out if not p["quietFor"] and not p["complete"]]
    return {"latest": latest, "projects": out,
            "counts": {"active": len(active), "new": sum(p["first"] == latest for p in out),
                       "slipping": sum(p["trend"] == "slipping" for p in active),
                       "quiet": sum(p["quietFor"] > 0 for p in out),
                       "complete": sum(p["complete"] for p in out)}}


PROJECTS_STYLE = """
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:18px 0 4px}
.filters button{font:inherit;font-size:12.5px;padding:4px 12px;border-radius:999px;border:1px solid var(--edge);background:var(--bg);color:var(--muted);cursor:pointer}
.filters button[aria-pressed=true]{background:var(--ink);border-color:var(--ink);color:#fff}
.filters select{font:inherit;font-size:12.5px;padding:4px 8px;border:1px solid var(--edge);border-radius:5px;background:var(--bg);color:var(--ink)}
.proj{border:1px solid var(--edge);border-left:4px solid var(--c);border-radius:10px;padding:14px 16px 6px;margin-top:14px;background:var(--bg)}
.proj.quiet,.proj.complete{opacity:.75}
.proj-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px}
.proj-head h3{margin:0;font-size:16px}
.proj-meta{font-size:12.5px;color:var(--muted);margin-top:4px;display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center}
.proj-meta .avs{display:inline-flex;gap:3px}
.trail{font-size:12.5px;margin-top:6px;color:var(--muted)}
.trail b{color:var(--ink);font-weight:600}
.flag{font-size:12px;font-weight:600}
.flag.complete{color:var(--done)}.flag.slipping{color:var(--p1)}.flag.moving{color:var(--done)}.flag.steady{color:var(--muted)}.flag.quiet{color:var(--p2)}
details.tl{margin-top:8px;border-top:1px solid var(--line)}
details.tl summary{cursor:pointer;font-size:12.5px;color:var(--muted);padding:8px 0;list-style:none}
details.tl summary::-webkit-details-marker{display:none}
details.tl summary::before{content:"▸ ";color:var(--faint)}details.tl[open] summary::before{content:"▾ "}
.wk{display:grid;grid-template-columns:96px 1fr;gap:10px;padding:8px 0;border-top:1px dashed var(--line)}
.wk-date{font-size:12px;font-weight:600;color:var(--muted)}
.wk-who{display:flex;gap:8px;align-items:center;font-size:13px;font-weight:600;margin-bottom:2px}
.wk-entry+.wk-entry{margin-top:10px}
@media (max-width:760px){.wk{grid-template-columns:1fr;gap:2px}}
@media print{details.tl{display:block}details.tl>*{display:block}.filters{display:none}.proj{break-inside:avoid}}
"""


def _weeks(n: int) -> str:
    return f"{n} week{'s' if n != 1 else ''}"


def render_projects(p: dict, embed: bool = False) -> str:
    c = p["counts"]
    people = sorted({n for x in p["projects"] for n in x["people"]})
    pc = person_colours(people)
    o = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>Weekly Progress Projects</title><style>{STYLE}{PROJECTS_STYLE}{EMBED_STYLE if embed else ""}</style>{EMBED_BASE if embed else ""}</head><body><div class="layout">
<aside><div class="app"><i>✓</i>Weekly Progress</div>
<nav>{"".join(f'<a href="#pr-{i}" style="--c:{ACCENT if x["type"] == "Claude" else TEAL}"><i></i><em>{escape(x["project"])}</em><span>{x["weeks"]}w</span></a>' for i, x in enumerate(p["projects"]))}</nav>
<div class="actions"><button type="button" onclick="print()">Print / Save as PDF</button><a href="/report">Weekly report</a><a href="/export/updates.csv">Projects CSV</a><a href="/admin">Admin</a></div></aside>
<main><h1>Projects</h1><p class="sub">Every project across all weeks, and how it's moving. Latest week: ending {escape(nice_date(p["latest"]))}.</p>
<div class="stats">{_stat(str(c["active"]), "active projects", "updated in the latest week", BLUE)}{_stat(str(c["new"]), "new", "started in the latest week", GREEN)}{_stat(str(c["slipping"]), "slipping", "time left got longer", ACCENT)}{_stat(str(c["quiet"]), "gone quiet", "no update in the latest week", ORANGE)}{_stat(str(c["complete"]), "complete", "marked complete", GREEN)}</div>
<div class="filters" role="group" aria-label="Show">
<button type="button" data-f="all" aria-pressed="true">All</button><button type="button" data-f="active" aria-pressed="false">Active</button>
<button type="button" data-f="slipping" aria-pressed="false">Slipping</button><button type="button" data-f="quiet" aria-pressed="false">Gone quiet</button><button type="button" data-f="complete" aria-pressed="false">Complete</button>
<select id="who" aria-label="Person"><option value="">Everyone</option>{"".join(f'<option>{escape(n)}</option>' for n in people)}</select></div>"""]
    if not p["projects"]:
        o.append('<p class="empty">No projects sent yet.</p>')
    for i, x in enumerate(p["projects"]):
        status = "complete" if x["complete"] else "quiet" if x["quietFor"] else "active"
        if x["complete"]:
            flag = f'<span class="flag complete">✅ Complete (week ending {escape(nice_date(x["completedOn"]))})</span>'
        elif x["quietFor"]:
            flag = f'<span class="flag quiet">No update for {_weeks(x["quietFor"])}</span>'
        else:
            flag = {"slipping": '<span class="flag slipping">▲ Slipping: time left got longer</span>',
                    "moving": '<span class="flag moving">▼ Moving: time left got shorter</span>',
                    "steady": '<span class="flag steady">Same time left as last update</span>', "": ""}[x["trend"]]
        new = x["first"] == p["latest"]
        o.append(f'<section class="proj {status}" id="pr-{i}" style="--c:{ACCENT if x["type"] == "Claude" else TEAL}" '
                 f'data-status="{status}" data-trend="{x["trend"]}" data-people="{escape("|".join(x["people"]))}">'
                 f'<div class="proj-head"><h3>{escape(x["project"])}</h3>{_label(x["type"])}'
                 + (_pill("New this week", GREEN) if new else "") + flag + "</div>"
                 f'<div class="proj-meta"><span class="avs">{"".join(_av(n, pc[n], "sm") for n in x["people"])}</span>'
                 f'<span>{escape(", ".join(x["people"]))}</span><span>Since {escape(nice_date(x["first"]))}</span>'
                 f'<span>{_weeks(x["weeks"])} of updates</span></div>')
        if x["trail"]:
            o.append('<div class="trail">Time left: ' + " → ".join(f"<b>{escape(t)}</b>" for t in x["trail"]) + "</div>")
        o.append(f'<details class="tl"{" open" if status == "active" else ""}><summary>Week by week</summary>')
        for wk in x["timeline"]:
            o.append(f'<div class="wk"><div class="wk-date">{escape(nice_date(wk["week"])[:-6])}</div><div>')
            for u in wk["entries"]:
                o.append(f'<div class="wk-entry"><div class="wk-who">{_av(u["name"], pc[u["name"]], "sm")}{escape(u["name"])}'
                         + (f' <span class="tleft">{escape(tl_badge(u["timeLeft"]))}</span>' if u.get("timeLeft") else "")
                         + (f' <span class="label">{escape(team(u))}</span>' if team(u) and not u.get("solo") else "") + "</div>"
                         + ('<div class="row" style="padding:0;border:0"><div class="body">'
                            + (f'<div class="lh prog">Progress</div><ul>{"".join(f"<li>{escape(s)}</li>" for s in u["progress"])}</ul>' if u.get("progress") else "")
                            + (f'<div class="lh next">Next steps</div><ul>{"".join(f"<li>{escape(s)}</li>" for s in u["nextSteps"])}</ul>' if u.get("nextSteps") else "")
                            + "</div></div></div>"))
            o.append("</div></div>")
        o.append("</details></section>")
    o.append("""</main></div><script>
let f = 'all';
function apply() {
  const who = document.getElementById('who').value;
  document.querySelectorAll('.proj').forEach(s => {
    const ok = (f === 'all' || (f === 'active' && s.dataset.status === 'active') || (f === 'quiet' && s.dataset.status === 'quiet') || (f === 'complete' && s.dataset.status === 'complete')
      || (f === 'slipping' && s.dataset.trend === 'slipping' && s.dataset.status === 'active'))
      && (!who || s.dataset.people.split('|').includes(who));
    s.hidden = !ok;
  });
  document.querySelectorAll('[data-f]').forEach(b => b.setAttribute('aria-pressed', b.dataset.f === f));
  if (parent !== window) parent.postMessage({wpHeight: document.documentElement.scrollHeight}, location.origin);
}
document.querySelectorAll('[data-f]').forEach(b => b.onclick = () => { f = b.dataset.f; apply(); });
document.getElementById('who').onchange = apply;
document.querySelectorAll('details').forEach(d => d.addEventListener('toggle', apply));
</script></body></html>""")
    return "".join(o)
