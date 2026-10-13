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
    by_status = {s: 0 for s in STATUS_ORDER}
    for u in ups:
        by_status[u["status"]] = by_status.get(u["status"], 0) + 1
    names = {u["name"] for u in ups} | {u["name"] for u in use}
    return {
        "updates": ups, "usage": use,
        "submitted": len(names & set(roster)),
        "projects": len(ups),
        "claude": sum(u["type"] == "Claude" for u in ups),
        "nonClaude": sum(u["type"] != "Claude" for u in ups),
        "byStatus": by_status,
        "atRisk": by_status["Blocked"] + by_status["At risk"],
        "hours": sum(hours(u) for u in ups if u["type"] == "Claude"),
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
    top = sorted((i for i in impact.values() if i["hours"] > 0), key=lambda i: (-i["hours"], i["project"].lower()))[:5]
    suggestions = sorted(({"project": u["project"], "name": u["name"], "text": u["couldHelp"]}
                          for u in cur["updates"] if u["type"] != "Claude" and u.get("couldHelp")),
                         key=lambda s: (s["project"].lower(), s["name"]))

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
        "groups": project_groups, "top": top, "suggestions": suggestions,
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

STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2230;--muted:#5d6676;--line:#e3e6eb;--accent:#3b5bdb;--accent-soft:#e8edff;
--red:#c92a2a;--red-soft:#ffe9e9;--amber:#b35c00;--amber-soft:#fff2df;--green:#2b8a3e;--green-soft:#e6f6ea;--grey-soft:#eef0f3;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#12151b;--card:#1b1f27;--ink:#e7eaf0;--muted:#9aa3b2;--line:#2c323d;
--accent:#91a7ff;--accent-soft:#232c48;--red:#ff8787;--red-soft:#3a1f22;--amber:#ffc078;--amber-soft:#3a2c1a;--green:#8ce99a;--green-soft:#1d3324;--grey-soft:#262b35;--on-accent:#12151b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:24px 16px 64px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;justify-content:space-between;margin-bottom:20px}
h1{font-size:24px;margin:0}h1 small{display:block;font-size:14px;font-weight:500;color:var(--muted)}
h2{font-size:17px;margin:0 0 12px}h3{font-size:15px;margin:0}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
select,button,a.btn{font:inherit;font-size:14px;padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);text-decoration:none;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.kpi span{color:var(--muted);font-size:13px}
.kpi i{display:block;font-style:normal;font-size:12px;color:var(--muted);margin-top:2px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
.muted{color:var(--muted)}.small{font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{padding:2px 10px;border-radius:99px;background:var(--grey-soft);font-size:13px}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;font-weight:600;white-space:nowrap}
.s-Blocked{background:var(--red-soft);color:var(--red)}.s-At-risk{background:var(--amber-soft);color:var(--amber)}
.s-On-track,.s-Completed{background:var(--green-soft);color:var(--green)}.s-In-progress{background:var(--accent-soft);color:var(--accent)}
.s-Not-started{background:var(--grey-soft);color:var(--muted)}
.t-Claude{background:var(--accent-soft);color:var(--accent)}.t-Non-Claude,.t-Mixed{background:var(--grey-soft);color:var(--muted)}
.group{border-top:1px solid var(--line);padding:14px 0}.group:first-of-type{border-top:0;padding-top:4px}
.group.urgent{border-left:3px solid var(--red);padding-left:12px;margin-left:-15px}
.group-head{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
.entry{margin:8px 0 0}.entry-head{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:14px}
.entry ul{margin:4px 0 0;padding-left:20px}.entry .note{font-size:13px;color:var(--muted);margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:6px;background:var(--grey-soft);border-radius:3px;min-width:60px;margin-top:5px}.bar>div{height:100%;border-radius:3px;background:var(--accent)}
tr.flag .bar>div{background:var(--red)}tr.flag td.pct{color:var(--red);font-weight:600}
.table-scroll{overflow-x:auto}
.statuses{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
.gallery a{display:block;color:inherit;text-decoration:none;font-size:13px}
.gallery img{width:100%;height:130px;object-fit:cover;object-position:top;border-radius:8px;border:1px solid var(--line);display:block;margin-bottom:4px;background:var(--grey-soft)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media (max-width:640px){.cols{grid-template-columns:1fr}}
.empty{color:var(--muted);font-style:italic}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 16px;border-radius:8px;font-size:14px;display:none}
@media print{body{background:#fff;font-size:11.5px}.wrap{padding:0;max-width:none}.tools,#toast{display:none}
section,.kpi{border:1px solid #ccc;break-inside:avoid-page;padding:10px;margin-bottom:8px}.kpis{gap:6px;margin-bottom:8px}.kpi b{font-size:18px}
.gallery{grid-template-columns:repeat(6,1fr)}.gallery img{height:70px}.group{break-inside:avoid}}
"""


def _pill(cls: str, text: str) -> str:
    return f'<span class="pill {cls}">{escape(text)}</span>'


def _status(s: str) -> str:
    return _pill("s-" + s.replace(" ", "-"), s)


def _type(t: str) -> str:
    return _pill("t-" + t, {"Claude": "Claude project", "Non-Claude": "Non-Claude project", "Mixed": "Claude + non-Claude"}.get(t, t))


def _kpi(value: str, label: str, note: str = "") -> str:
    return f'<div class="kpi"><b>{value}</b><span>{label}</span>{f"<i>{note}</i>" if note else ""}</div>'


def _wow(now, before, has_prev, fmt=lambda v: f"{v:g}", good_up=True) -> str:
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
    o = []
    o.append(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>Weekly Progress Report</title><style>{STYLE}</style></head><body><div class="wrap">
<header><h1>Weekly Progress Report<small>Week ending {escape(nice_date(wk))}</small></h1>
<div class="tools"><form method="get" action="/report"><select name="week" onchange="this.form.submit()" aria-label="Week">{opts}</select></form>
<button type="button" onclick="copySlack()">Copy Slack summary</button>
<button type="button" class="primary" onclick="print()">Print / PDF</button>
<a class="btn" href="/export/updates.csv?week={wk}">Projects CSV</a><a class="btn" href="/export/usage.csv?week={wk}">Usage CSV</a></div></header>""")

    spend_note = (f"{100 * c['spend'] / c['budget']:.0f}% of {money(c['budget'])} budget" if c["budget"] else "no usage submitted")
    o.append('<div class="kpis">'
             + _kpi(f"{len(p['complete'])}/{p['roster']}", "sent their update",
                    _wow(c["submitted"], l["submitted"], hp, lambda v: f"{v:g} {'person' if v == 1 else 'people'}") if hp else f"{c['submitted']} submitted something")
             + _kpi(str(c["projects"]), "projects reported", f"{c['claude']} Claude · {c['nonClaude']} non-Claude")
             + _kpi(str(c["atRisk"]), "at risk or blocked", _wow(c["atRisk"], l["atRisk"], hp))
             + _kpi(f"{c['hours']:g} hrs", "saved with Claude", _wow(c["hours"], l["hours"], hp, lambda v: f"{v:g} hrs"))
             + _kpi(money(c["spend"]), "Claude spend this month", spend_note)
             + "</div>")

    # participation
    def names(ns):
        return '<div class="chips">' + "".join(f'<span class="chip">{escape(n)}</span>' for n in ns) + "</div>" if ns else '<p class="empty">Everyone</p>'
    o.append(f"""<section><h2>Participation</h2><p class="small muted">{len(p['complete'])} of {p['roster']} people sent their weekly update.</p>
<h3>Hasn't sent yet</h3>{names(p['missing']) if p['missing'] else '<p class="empty">Nobody. Everyone is in.</p>'}</section>""")

    # projects
    o.append('<section><h2>Projects</h2><div class="statuses">'
             + "".join(f'{_status(s)}<span class="small muted" style="margin-right:8px">{n}</span>' for s, n in c["byStatus"].items() if n)
             + "</div>")
    if not r["groups"]:
        o.append('<p class="empty">No project updates for this week yet.</p>')
    for g in r["groups"]:
        urgent = g["worst"] in ("Blocked", "At risk")
        o.append(f'<div class="group{" urgent" if urgent else ""}"><div class="group-head"><h3>{escape(g["project"])}</h3>{_type(g["type"])}'
                 + (' <span class="small muted">first reported this week</span>' if g["new"] else "") + "</div>")
        for u in g["entries"]:
            note = ""
            if u["type"] == "Claude":
                note = f'Time saved with Claude: {escape(u.get("timeSaved") or "None")}'
            elif u.get("couldHelp"):
                note = f'Claude could have helped: {escape(u["couldHelp"])}'
            o.append(f'<div class="entry"><div class="entry-head"><strong>{escape(u["name"])}</strong>{_status(u["status"])}'
                     + (_type(u["type"]) if g["type"] == "Mixed" else "") + "</div><ul>"
                     + "".join(f"<li>{escape(b)}</li>" for b in u["progress"]) + "</ul>"
                     + (f'<div class="note">{note}</div>' if note else "") + "</div>")
        o.append("</div>")
    o.append("</section>")

    # Claude impact
    o.append(f'<section><h2>Claude impact</h2><p class="small muted">About {c["hours"]:g} hours saved this week. '
             'Each answer counts at its range midpoint: none 0, under 1 hr 0.5, 1–3 hrs 2, 3–5 hrs 4, 5+ hrs 6.</p><div class="cols"><div><h3>Top Claude projects by time saved</h3>')
    if r["top"]:
        o.append('<table><thead><tr><th>Project</th><th>People</th><th class="num">Hours</th></tr></thead><tbody>'
                 + "".join(f'<tr><td>{escape(t["project"])}</td><td>{escape(", ".join(t["people"]))}</td><td class="num">{t["hours"]:g}</td></tr>' for t in r["top"])
                 + "</tbody></table>")
    else:
        o.append('<p class="empty">No time saved reported.</p>')
    o.append("</div><div><h3>Where Claude could have helped</h3>")
    if r["suggestions"]:
        o.append("<ul>" + "".join(f'<li><strong>{escape(s["project"])}</strong> <span class="muted">({escape(s["name"])})</span>: {escape(s["text"])}</li>' for s in r["suggestions"]) + "</ul>")
    else:
        o.append('<p class="empty">No suggestions this week.</p>')
    o.append("</div></div></section>")

    # usage and spend
    wk_note = "" if r["sameMonth"] else " A new month started this week, so there's no weekly spend change."
    o.append(f'<section><h2>Usage and spend</h2><p class="small muted">Spend so far this month against each person\'s monthly budget. '
             f'Over {FLAG_AT}% is flagged.{wk_note}</p>')
    if r["spend"]:
        o.append('<div class="table-scroll"><table><thead><tr><th>Name</th><th class="num">Spend</th><th class="num">Budget</th><th>% used</th><th class="num">This week</th></tr></thead><tbody>')
        for u in r["spend"]:
            ws = "—" if u["weekSpend"] is None else ("+" if u["weekSpend"] >= 0 else "−") + money(abs(u["weekSpend"]))
            o.append(f'<tr class="{"flag" if u["flag"] else ""}"><td>{escape(u["name"])}{" ⚑" if u["flag"] else ""}</td><td class="num">{money(u["spend"])}</td>'
                     f'<td class="num">{money(u["budget"])}</td><td class="pct">{u["pct"]:.0f}%<div class="bar"><div style="width:{min(u["pct"], 100):.0f}%"></div></div></td>'
                     f'<td class="num">{ws}</td></tr>')
        o.append(f'<tr><th>Team</th><th class="num">{money(c["spend"])}</th><th class="num">{money(c["budget"])}</th>'
                 f'<th>{(100 * c["spend"] / c["budget"]) if c["budget"] else 0:.0f}%</th><th></th></tr></tbody></table></div>')
    else:
        o.append('<p class="empty">No usage forms for this week yet.</p>')
    o.append('<h3 style="margin-top:16px">Budget increase requests</h3>')
    if r["requests"]:
        o.append('<div class="table-scroll"><table><thead><tr><th>Name</th><th class="num">Current</th><th class="num">Requested</th><th>Reason</th><th>Status</th></tr></thead><tbody>'
                 + "".join(f'<tr><td>{escape(u["name"])}</td><td class="num">{money(u["budget"])}</td><td class="num">{money(u["increase"]["amount"])}</td>'
                           f'<td>{escape(u["increase"]["reason"])}</td><td>{escape(u["increase"]["status"])}</td></tr>' for u in r["requests"])
                 + "</tbody></table></div>")
    else:
        o.append('<p class="empty">None this week.</p>')
    o.append('<h3 style="margin-top:16px">Usage screenshots</h3>')
    if r["shots"]:
        o.append('<div class="gallery">' + "".join(
            f'<a href="/screenshots/{escape(u["screenshot"])}" target="_blank" rel="noopener"><img src="/screenshots/{escape(u["screenshot"])}" alt="Usage screenshot from {escape(u["name"])}" loading="lazy">{escape(u["name"])}</a>'
            for u in r["shots"]) + "</div>")
    else:
        o.append('<p class="empty">None yet.</p>')
    o.append("</section>")

    o.append(f"""<div id="toast"></div></div><script>
async function copySlack(){{const t=document.getElementById('toast');try{{const r=await fetch('/report.txt?week={wk}');
await navigator.clipboard.writeText(await r.text());t.textContent='Slack summary copied';}}catch(e){{t.textContent='Couldn\\'t copy: '+e.message;}}
t.style.display='block';setTimeout(()=>t.style.display='none',2500);}}</script></body></html>""")
    return "".join(o)
