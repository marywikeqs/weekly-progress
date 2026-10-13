"""
report.py — the CRO's weekly report, built from one week's submissions.

build() turns the data into plain numbers and lists; render_html() is the
page (prints to a one-page-ish PDF); render_text() is the Slack-ready summary.
Kept apart from server.py so a scheduled Slack/email send can reuse build().
"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape

# Time-saved ranges counted at their midpoints.
TIME_SAVED_HOURS = {"None": 0, "Under 1 hr": 0.5, "1-3 hrs": 2, "3-5 hrs": 4, "5+ hrs": 6}
# Lower = more urgent; groups sort by their most urgent status.
STATUS_RANK = {"Blocked": 0, "At risk": 1, "Not started": 2, "In progress": 3, "On track": 4, "Completed": 5}
STATUS_ORDER = ["Blocked", "At risk", "Not started", "In progress", "On track", "Completed"]
# Other work's time spent, also at midpoints.
TIME_SPENT_HOURS = {"Under 1 hr": 0.5, "1-3 hrs": 2, "3-5 hrs": 4, "5-10 hrs": 7.5, "10+ hrs": 12}
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


def nice_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%a}, {d:%b} {d.day}, {d.year}"


def money(n: float) -> str:
    return f"${n:,.0f}" if float(n).is_integer() else f"${n:,.2f}"


def _week_numbers(data: dict, week: str, roster: list) -> dict:
    ups = [u for u in data["updates"] if u["week"] == week]
    use = [u for u in data["usage"] if u["week"] == week]
    tasks = [t for t in data.get("tasks", []) if t["week"] == week]
    by_status = {s: 0 for s in STATUS_ORDER}
    for u in ups:
        by_status[u["status"]] = by_status.get(u["status"], 0) + 1
    names = {u["name"] for u in ups} | {u["name"] for u in use}
    return {
        "updates": ups, "usage": use, "tasks": tasks,
        "submitted": len(names & set(roster)),
        "projects": len(ups),
        "claude": sum(u["type"] == "Claude" for u in ups),
        "nonClaude": sum(u["type"] != "Claude" for u in ups),
        "byStatus": by_status,
        "atRisk": by_status["Blocked"] + by_status["At risk"],
        "hours": sum(hours(u) for u in ups if u["type"] == "Claude") + sum(hours(t) for t in tasks if t["claude"]),
        "taskHours": sum(TIME_SPENT_HOURS.get(t.get("timeSpent") or "", 0) for t in tasks),
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

    # projects, grouped by name; most urgent first
    groups: dict = {}
    for u in cur["updates"]:
        groups.setdefault(u["project"].lower(), []).append(u)
    first_week: dict = {}
    for u in data["updates"]:
        k = u["project"].lower()
        first_week[k] = min(first_week.get(k, u["week"]), u["week"])
    project_groups = []
    for entries in groups.values():
        entries.sort(key=lambda u: (STATUS_RANK.get(u["status"], 9), u["name"]))
        types = {u["type"] for u in entries}
        project_groups.append({
            "project": entries[0]["project"], "entries": entries,
            "type": types.pop() if len(types) == 1 else "Mixed",
            "worst": entries[0]["status"], "new": first_week[entries[0]["project"].lower()] == week,
        })
    project_groups.sort(key=lambda g: (STATUS_RANK.get(g["worst"], 9) if g["worst"] in ("Blocked", "At risk") else 5,
                                       g["project"].lower()))

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
    suggestions = sorted(({"project": u["project"], "name": u["name"], "text": u["couldHelp"]}
                          for u in cur["updates"] if u["type"] != "Claude" and u.get("couldHelp")),
                         key=lambda s: (s["project"].lower(), s["name"]))

    # other work, grouped by kind
    other: dict = {}
    for t in sorted(cur["tasks"], key=lambda t: t["name"]):
        g = other.setdefault(t["category"].lower(), {"category": t["category"], "entries": [], "hours": 0, "saved": 0})
        g["entries"].append(t)
        g["hours"] += TIME_SPENT_HOURS.get(t.get("timeSpent") or "", 0)
        g["saved"] += hours(t) if t["claude"] else 0
    other_groups = sorted(other.values(), key=lambda g: (-g["hours"], g["category"].lower()))

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
        "groups": project_groups, "other": other_groups, "top": top, "suggestions": suggestions,
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
                 + (f" · hasn't sent: {', '.join(p['missing'])}" if p["missing"] else ""))
    lines.append(f"*Projects:* {c['projects']}{_delta(c['projects'], l['projects'], hp)} — "
                 f"{c['claude']} Claude · {c['nonClaude']} non-Claude · "
                 + " · ".join(f"{n} {s}" for s, n in c["byStatus"].items() if n))
    for g in r["groups"]:
        for u in g["entries"]:
            if u["status"] in ("Blocked", "At risk"):
                lines.append(f"   • {u['status']}: *{u['project']}* ({u['name']}) — {u['progress'][0]}")
    if r["other"]:
        lines.append(f"*Other work:* ~{c['taskHours']:g} hrs · " + ", ".join(
            f"{g['category']} ({len(g['entries'])})" for g in r["other"]))
    top = ", ".join(f"{t['project']} ({t['hours']:g} hrs)" for t in r["top"][:3])
    lines.append(f"*Claude time saved:* ~{c['hours']:g} hrs{_delta(c['hours'], l['hours'], hp, ' hrs')}"
                 + (f" · top: {top}" if top else ""))
    if r["suggestions"]:
        lines.append(f"*Where Claude could help:* {len(r['suggestions'])} idea{'s' if len(r['suggestions']) != 1 else ''} from non-Claude projects")
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

# Todoist-style: warm sidebar, white page, red accent, thin dividers, and
# status shown as Todoist's priority colours on a round marker.
STYLE = """
:root{--bg:#fff;--side:#fcfaf8;--ink:#202020;--muted:#666;--faint:#999;--line:#f0f0f0;--edge:#e6e6e6;--hover:#f5f5f5;
--accent:#dc4c3e;--accent-hover:#c3392c;--accent-soft:#fff6f0;--p1:#d1453b;--p2:#eb8909;--p3:#246fe0;--p4:#999;--done:#058527}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#1e1e1e;--side:#282828;--ink:#ededed;--muted:#a8a8a8;--faint:#7a7a7a;
--line:#2f2f2f;--edge:#3d3d3d;--hover:#2a2a2a;--accent:#e06a5f;--accent-hover:#ea7f75;--accent-soft:#3a2523;--p1:#ff7066;--p2:#ff9a14;--p3:#5297ff;--p4:#8a8a8a;--done:#4ccf74}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.layout{display:grid;grid-template-columns:260px 1fr;min-height:100vh}
aside{background:var(--side);border-right:1px solid var(--line);padding:20px 12px;position:sticky;top:0;height:100vh;overflow:auto}
.app{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px;margin:0 0 18px 6px}
.app i{width:22px;height:22px;border-radius:6px;background:var(--accent);display:inline-grid;place-items:center;color:#fff;font-style:normal;font-size:13px}
aside select{width:100%;font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--edge);border-radius:5px;background:var(--bg);color:var(--ink);margin-bottom:14px}
nav a{display:flex;justify-content:space-between;align-items:center;padding:6px 8px;border-radius:5px;color:var(--ink);text-decoration:none;font-size:14px}
nav a:hover{background:var(--hover)}
nav a span{color:var(--faint);font-size:12px}
nav a.alert span{color:var(--p1);font-weight:600}
.actions{border-top:1px solid var(--line);margin-top:14px;padding-top:12px;display:flex;flex-direction:column;gap:2px}
.actions a,.actions button{display:block;text-align:left;width:100%;font:inherit;font-size:13px;color:var(--muted);background:none;border:0;padding:6px 8px;border-radius:5px;cursor:pointer;text-decoration:none}
.actions a:hover,.actions button:hover{background:var(--hover);color:var(--ink)}
main{padding:32px 32px 80px;max-width:860px;width:100%}
h1{font-size:20px;margin:0}
.sub{color:var(--muted);font-size:13px;margin:2px 0 0}
.stats{display:flex;flex-wrap:wrap;gap:8px 28px;margin:20px 0 6px;padding:14px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted)}
.stat i{display:block;font-style:normal;font-size:11.5px;color:var(--faint)}
h2{display:flex;align-items:baseline;gap:8px;font-size:15px;margin:34px 0 0;padding-bottom:6px;border-bottom:1px solid var(--line);scroll-margin-top:16px}
h2 span{color:var(--faint);font-weight:400;font-size:12px}
.note{color:var(--muted);font-size:12.5px;margin:8px 0 0}
.ring{flex:none;width:18px;height:18px;border-radius:50%;border:2px solid var(--p4);margin-top:1px}
.ring.s-Blocked{border-color:var(--p1);background:color-mix(in srgb,var(--p1) 12%,transparent)}
.ring.s-At-risk{border-color:var(--p2);background:color-mix(in srgb,var(--p2) 12%,transparent)}
.ring.s-In-progress,.ring.s-On-track{border-color:var(--p3);background:color-mix(in srgb,var(--p3) 10%,transparent)}
.ring.s-Completed,.ring.sent{border-color:var(--done);background:var(--done);position:relative}
.ring.s-Completed::after,.ring.sent::after{content:"";position:absolute;left:4px;top:1px;width:4px;height:8px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
.flag{font-size:12px;font-weight:600}
.f-Blocked{color:var(--p1)}.f-At-risk{color:var(--p2)}.f-In-progress,.f-On-track{color:var(--p3)}.f-Completed{color:var(--done)}.f-Not-started{color:var(--p4)}
.label{font-size:12px;color:var(--muted)}
.label.claude{color:var(--accent)}
.group-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;margin:22px 0 2px;font-weight:700;font-size:14px}
.group-head .label{font-weight:400}
.group-head .newp{font-weight:400;font-size:11.5px;color:var(--faint)}
.row{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.row .body{flex:1;min-width:0}
.row .top{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.row ul{margin:1px 0 0;padding-left:16px;color:var(--muted);font-size:13.5px}
.row .lh{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--faint);margin-top:6px}
.row .meta{font-size:12px;color:var(--muted);margin-top:4px}
.people{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));column-gap:24px}
.people .row{align-items:center;padding:8px 0}
.people .missing{color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:8px 8px 8px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--faint);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:4px;background:var(--line);border-radius:2px;margin-top:5px;min-width:60px}.bar>div{height:100%;border-radius:2px;background:var(--p3)}
tr.flag td.pct{color:var(--p1);font-weight:600}tr.flag .bar>div{background:var(--p1)}
.table-scroll{overflow-x:auto}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:28px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-top:12px}
.gallery a{color:var(--muted);text-decoration:none;font-size:12.5px}
.gallery img{width:100%;height:120px;object-fit:cover;object-position:top;border-radius:6px;border:1px solid var(--edge);display:block;margin-bottom:4px;background:var(--hover)}
.empty{color:var(--faint);font-size:13px;padding:10px 0}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 16px;border-radius:6px;font-size:13px;display:none}
@media (max-width:760px){.layout{grid-template-columns:1fr}aside{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}
nav{display:none}.actions{flex-direction:row;flex-wrap:wrap;border:0;margin:0;padding:0}.actions a,.actions button{width:auto;border:1px solid var(--edge)}
main{padding:20px 16px 60px}.cols{grid-template-columns:1fr}}
@media print{aside,#toast{display:none}.layout{display:block}main{padding:0;max-width:none}body{font-size:11.5px}
h2{break-after:avoid}.row{break-inside:avoid}.gallery{grid-template-columns:repeat(6,1fr)}.gallery img{height:70px}}
"""


def _cls(s: str) -> str:
    return s.replace(" ", "-")


def _ring(status: str) -> str:
    return f'<span class="ring s-{_cls(status)}" title="{escape(status)}"></span>'


def _flag(status: str) -> str:
    return f'<span class="flag f-{_cls(status)}">{escape(status)}</span>'


def _label(t: str) -> str:
    text = {"Claude": "# Claude project", "Non-Claude": "# Non-Claude project", "Mixed": "# Claude + non-Claude"}.get(t, t)
    return f'<span class="label{" claude" if t == "Claude" else ""}">{escape(text)}</span>'


def _stat(value: str, label: str, note: str = "") -> str:
    return f'<div class="stat"><b>{value}</b><span>{label}</span>{f"<i>{note}</i>" if note else ""}</div>'


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
    opts = "".join(f'<option value="{w}"{" selected" if w == wk else ""}>Week ending {escape(nice_date(w))}</option>' for w in weeks)
    o = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>Weekly Progress Report</title><style>{STYLE}</style></head><body><div class="layout">
<aside><div class="app"><i>✓</i>Weekly Progress</div>
<form method="get" action="/report"><select name="week" onchange="this.form.submit()" aria-label="Week">{opts}</select></form>
<nav><a href="#people">Participation <span>{len(p['complete'])}/{p['roster']}</span></a>
<a href="#projects"{' class="alert"' if c['atRisk'] else ''}>Projects <span>{c['projects']}{f" · {c['atRisk']} at risk" if c['atRisk'] else ""}</span></a>
<a href="#other">Other work <span>{len(c['tasks'])}</span></a>
<a href="#impact">Claude impact <span>{c['hours']:g} hrs</span></a>
<a href="#spend"{' class="alert"' if r['flagged'] else ''}>Usage and spend <span>{money(c['spend'])}</span></a></nav>
<div class="actions"><button type="button" onclick="copySlack()">Copy Slack summary</button><button type="button" onclick="print()">Print / Save as PDF</button>
<a href="/export/updates.csv?week={wk}">Projects CSV</a><a href="/export/usage.csv?week={wk}">Usage CSV</a><a href="/admin">Admin</a></div></aside>
<main><h1>Week ending {escape(nice_date(wk))}</h1><p class="sub">Weekly Progress report</p>"""]

    spend_note = f"{100 * c['spend'] / c['budget']:.0f}% of {money(c['budget'])}" if c["budget"] else "none reported"
    o.append('<div class="stats">'
             + _stat(f"{len(p['complete'])}/{p['roster']}", "sent their update")
             + _stat(str(c["projects"]), "projects", f"{c['claude']} Claude · {c['nonClaude']} non-Claude")
             + _stat(str(c["atRisk"]), "at risk or blocked", _wow(c["atRisk"], l["atRisk"], hp))
             + _stat(f"{c['hours']:g} hrs", "saved with Claude", _wow(c["hours"], l["hours"], hp, lambda v: f"{v:g} hrs"))
             + _stat(money(c["spend"]), "Claude spend this month", spend_note)
             + "</div>")

    # participation
    o.append(f'<h2 id="people">Participation <span>{len(p["complete"])} of {p["roster"]}</span></h2><div class="people">')
    for n in p["complete"]:
        o.append(f'<div class="row"><span class="ring sent"></span><div class="body">{escape(n)}</div></div>')
    for n in p["missing"]:
        o.append(f'<div class="row missing"><span class="ring"></span><div class="body">{escape(n)} <span class="label">hasn\'t sent yet</span></div></div>')
    o.append("</div>")
    if not p["missing"]:
        o.append('<p class="note">Everyone is in.</p>')

    # projects
    counts = " · ".join(f"{n} {s.lower()}" for s, n in c["byStatus"].items() if n)
    o.append(f'<h2 id="projects">Projects <span>{counts}</span></h2>')
    if not r["groups"]:
        o.append('<p class="empty">No project updates for this week yet.</p>')
    for g in r["groups"]:
        o.append(f'<div class="group-head">{escape(g["project"])} {_label(g["type"])}'
                 + ('<span class="newp">first reported this week</span>' if g["new"] else "") + "</div>")
        for u in g["entries"]:
            meta = ""
            if u["type"] == "Claude":
                meta = f'⏱ Time saved with Claude: {escape(u.get("timeSaved") or "None")}'
            elif u.get("couldHelp"):
                meta = f'💡 Claude could have helped: {escape(u["couldHelp"])}'
            o.append(f'<div class="row">{_ring(u["status"])}<div class="body"><div class="top"><strong>{escape(u["name"])}</strong>{_flag(u["status"])}'
                     + (_label(u["type"]) if g["type"] == "Mixed" else "") + "</div>"
                     + '<div class="lh">Progress</div><ul>' + "".join(f"<li>{escape(b)}</li>" for b in u["progress"]) + "</ul>"
                     + ('<div class="lh">Next steps</div><ul>' + "".join(f"<li>{escape(b)}</li>" for b in u["nextSteps"]) + "</ul>" if u.get("nextSteps") else "")
                     + (f'<div class="meta">{meta}</div>' if meta else "") + "</div></div>")

    # other work
    o.append(f'<h2 id="other">Other work <span>{len(c["tasks"])} item{"" if len(c["tasks"]) == 1 else "s"}'
             + (f' · about {c["taskHours"]:g} hrs' if c["taskHours"] else "") + "</span></h2>")
    if not r["other"]:
        o.append('<p class="empty">No other work reported.</p>')
    for g in r["other"]:
        o.append(f'<div class="group-head">{escape(g["category"])}'
                 + (f' <span class="label">~{g["hours"]:g} hrs</span>' if g["hours"] else "")
                 + (f' <span class="label claude">{g["saved"]:g} hrs saved with Claude</span>' if g["saved"] else "") + "</div>")
        for t in g["entries"]:
            bits = [escape(t["timeSpent"])] if t.get("timeSpent") else []
            bits.append(f'<span class="label claude">Claude saved {escape(t["timeSaved"])}</span>' if t["claude"] else '<span class="label">no Claude</span>')
            o.append(f'<div class="row"><span class="ring"></span><div class="body"><div class="top"><strong>{escape(t["name"])}</strong>'
                     f'<span class="label">{" · ".join(bits)}</span></div>'
                     + (f'<div class="meta">{escape(t["note"])}</div>' if t.get("note") else "") + "</div></div>")

    # Claude impact
    o.append(f'<h2 id="impact">Claude impact <span>about {c["hours"]:g} hours saved</span></h2>'
             '<p class="note">Each answer counts at its range midpoint: none 0, under 1 hr 0.5, 1–3 hrs 2, 3–5 hrs 4, 5+ hrs 6.</p><div class="cols"><div>'
             '<div class="group-head">Top Claude projects by time saved</div>')
    if r["top"]:
        o.append('<table><thead><tr><th>Project</th><th>People</th><th class="num">Hours</th></tr></thead><tbody>'
                 + "".join(f'<tr><td>{escape(t["project"])}</td><td>{escape(", ".join(t["people"]))}</td><td class="num">{t["hours"]:g}</td></tr>' for t in r["top"])
                 + "</tbody></table>")
    else:
        o.append('<p class="empty">No time saved reported.</p>')
    o.append('</div><div><div class="group-head">Where Claude could have helped</div>')
    if r["suggestions"]:
        o.append("".join(f'<div class="row"><span class="ring"></span><div class="body"><strong>{escape(s["project"])}</strong> '
                         f'<span class="label">{escape(s["name"])}</span><div class="meta">{escape(s["text"])}</div></div></div>' for s in r["suggestions"]))
    else:
        o.append('<p class="empty">No suggestions this week.</p>')
    o.append("</div></div>")

    # usage and spend
    wk_note = "" if r["sameMonth"] else " A new month started this week, so there's no weekly change."
    o.append(f'<h2 id="spend">Usage and spend <span>{money(c["spend"])} of {money(c["budget"])}</span></h2>'
             f'<p class="note">Spend so far this month against each person\'s monthly budget. Over {FLAG_AT}% is flagged.{wk_note}</p>')
    if r["spend"]:
        o.append('<div class="table-scroll"><table><thead><tr><th>Name</th><th class="num">Spend</th><th class="num">Budget</th><th>Used</th><th class="num">This week</th></tr></thead><tbody>')
        for u in r["spend"]:
            ws = "—" if u["weekSpend"] is None else ("+" if u["weekSpend"] >= 0 else "−") + money(abs(u["weekSpend"]))
            o.append(f'<tr class="{"flag" if u["flag"] else ""}"><td>{escape(u["name"])}{" ⚑" if u["flag"] else ""}</td><td class="num">{money(u["spend"])}</td>'
                     f'<td class="num">{money(u["budget"])}</td><td class="pct">{u["pct"]:.0f}%<div class="bar"><div style="width:{min(u["pct"], 100):.0f}%"></div></div></td>'
                     f'<td class="num">{ws}</td></tr>')
        o.append(f'<tr><th>Team</th><th class="num">{money(c["spend"])}</th><th class="num">{money(c["budget"])}</th>'
                 f'<th>{(100 * c["spend"] / c["budget"]) if c["budget"] else 0:.0f}%</th><th></th></tr></tbody></table></div>')
    else:
        o.append('<p class="empty">No usage for this week yet.</p>')
    o.append('<div class="group-head">Budget increase requests</div>')
    if r["requests"]:
        o.append("".join(f'<div class="row"><span class="ring s-{_cls("At risk") if u["increase"]["status"] == "Pending" else ("Completed" if u["increase"]["status"] == "Approved" else "Blocked")}"></span>'
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
