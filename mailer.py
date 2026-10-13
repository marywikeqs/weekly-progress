"""
mailer.py — emails the weekly report through Resend (https://resend.com).

The server sends one email per week, as soon as everyone on the roster has
sent their update (no partial sends). Sending is over HTTPS, so Railway's SMTP block
doesn't matter.

Environment:
  RESEND_API_KEY   the Resend API key                 unset = no email at all
  REPORT_FROM      the From line                      default Resend's shared
                   test sender, which only delivers to the address the Resend
                   account was opened with, until a domain is verified there
  PUBLIC_URL       the app's address, for links       default the Railway URL
  REPORT_TIMEZONE  timezone for "this week"            default America/Chicago
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape

import report

API_KEY = os.environ.get("RESEND_API_KEY", "")
API_URL = os.environ.get("RESEND_API_URL", "https://api.resend.com/emails")   # tests point this at a fake
FROM = os.environ.get("REPORT_FROM", "Weekly Progress <onboarding@resend.dev>")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://salesopsprogress.up.railway.app").rstrip("/")
TZ_NAME = os.environ.get("REPORT_TIMEZONE", "America/Chicago")
EMAIL_RX = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[a-z]{2,}$", re.I)

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception:          # no tz database in the image: fall back to US Central standard time
    TZ = timezone(timedelta(hours=-6))


def configured() -> bool:
    return bool(API_KEY)


def recipients(cfg: dict) -> list:
    return [a for a in re.split(r"[\s,;]+", cfg.get("reportEmail", "")) if EMAIL_RX.match(a)]


def now_local() -> datetime:
    return datetime.now(TZ)


def send(to: list, subject: str, html: str, text: str) -> tuple:
    """(True, id) or (False, why). Never raises."""
    body = json.dumps({"from": FROM, "to": to, "subject": subject, "html": html, "text": text}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json", "User-Agent": "weekly-progress/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, json.loads(r.read() or b"{}").get("id", "")
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read()).get("message", "")
        except Exception:
            why = ""
        return False, f"Resend said {e.code}{': ' + why if why else ''}"
    except Exception as e:
        return False, f"Couldn't reach Resend: {e}"


# ---------------- the email ----------------

def subject(r: dict) -> str:
    p = r["participation"]
    who = f"all {p['roster']} in" if not p["missing"] else f"{len(p['complete'])} of {p['roster']} in, missing {', '.join(p['missing'])}"
    return f"Weekly Progress, week ending {report.nice_date(r['week'])} ({who})"


def plain(r: dict) -> str:
    return report.render_text(r, PUBLIC_URL).replace("*", "")


# Email clients ignore most CSS, so everything is inline and table-free where possible.
INK, MUTED, LINE, RED, AMBER, GREEN, ACCENT = "#1c2230", "#5d6676", "#e3e6eb", "#c92a2a", "#b35c00", "#2b8a3e", "#3b5bdb"


def tint(hex_: str, amount: float = 0.12) -> str:
    """hex_ mixed onto white; email clients don't do color-mix()."""
    rgb = [int(hex_[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(255 - (255 - v) * amount):02x}" for v in rgb)


def shade(hex_: str, amount: float = 0.72) -> str:
    """hex_ darkened, so light colours stay readable as text."""
    return "#" + "".join(f"{round(int(hex_[i:i + 2], 16) * amount):02x}" for i in (1, 3, 5))


def pill(text: str, colour: str) -> str:
    return (f'<span style="display:inline-block;background:{tint(colour, .16)};color:{shade(colour)};font-size:12px;font-weight:bold;'
            f'padding:1px 8px;border-radius:10px;white-space:nowrap">{escape(text)}</span>')


def html(r: dict, note: str = "") -> str:
    c, l, p, hp = r["cur"], r["last"], r["participation"], r["hasPrev"]
    pc = report.person_colours({pp["name"] for pp in r["people"]} | set(p["complete"]) | set(p["missing"]) | {u["name"] for u in r["spend"]})
    link = f"{PUBLIC_URL}/report?week={r['week']}"
    h2 = f'style="font-size:16px;margin:24px 0 8px;color:{INK}"'
    small = f'style="font-size:13px;color:{MUTED};margin:2px 0"'
    o = [f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:14px;line-height:1.5;color:{INK};max-width:640px">']
    if note:
        o.append(f'<p style="background:#fff2df;color:{AMBER};padding:8px 12px;border-radius:6px;margin:0 0 16px">{escape(note)}</p>')
    o.append(f'<div style="background:{report.ACCENT};color:#fff;border-radius:10px;padding:16px 20px"><div style="font-size:20px;font-weight:bold">Weekly digest</div>'
             f'<div style="font-size:13px;opacity:.9">Week ending {escape(report.nice_date(r["week"]))}</div></div>')

    def wow(now, before, unit=""):
        if not hp:
            return ""
        d = now - before
        return "" if abs(d) < 1e-9 else f' <span style="color:{MUTED}">({"+" if d > 0 else "−"}{abs(d):g}{unit} vs last week)</span>'

    spend = f"{report.money(c['spend'])} of {report.money(c['budget'])} ({100 * c['spend'] / c['budget']:.0f}%)" if c["budget"] else "none reported"
    tiles = [
        (report.GREEN, f"{len(p['complete'])}/{p['roster']}", "sent their update",
         f'missing {escape(", ".join(p["missing"]))}' if p["missing"] else "everyone's in"),
        (report.BLUE, str(c["projects"]), "projects", f"{c['claude']} Claude · {c['nonClaude']} non-Claude{wow(c['projects'], l['projects'])}"),
        (report.ACCENT, f"~{c['hours']:g} hrs", "saved with Claude", wow(c["hours"], l["hours"], " hrs").strip()),
        (report.GRAPE, report.money(c["spend"]), "Claude spend", spend),
    ]
    cells = [f'<td valign="top" width="50%" style="padding:4px"><div style="background:{tint(col)};border:1px solid {tint(col, .3)};border-radius:8px;padding:10px 12px">'
             f'<div style="font-size:20px;font-weight:bold;color:{shade(col, .85)}">{v}</div><div style="font-size:12px;font-weight:bold">{k}</div>'
             f'<div style="font-size:11px;color:{MUTED}">{n}</div></div></td>' for col, v, k, n in tiles]
    o.append(f'<table cellpadding="0" cellspacing="0" width="100%" style="margin-top:12px;border-collapse:collapse"><tr>{"".join(cells[:2])}</tr>'
             f'<tr>{"".join(cells[2:])}</tr></table>')

    # At a glance: one line per person, then the retro answers; the full breakdown follows
    if r["people"]:
        rows = []
        for pp in r["people"]:
            col = pc[pp["name"]]
            initials = "".join(w[0] for w in pp["name"].split()[:2]).upper()
            other = ", ".join(dict.fromkeys(t["category"] for t in pp["tasks"]))
            rows.append(f'<tr><td valign="top" style="padding:7px 10px 7px 0;border-bottom:1px solid {LINE};width:26px">'
                        f'<span style="display:inline-block;width:24px;height:24px;line-height:24px;text-align:center;border-radius:12px;background:{col};color:#fff;font-size:10px;font-weight:bold">{escape(initials)}</span></td>'
                        f'<td valign="top" style="padding:7px 0;border-bottom:1px solid {LINE}"><b>{escape(pp["name"])}</b><br>'
                        f'{escape(" · ".join(u["project"] for u in pp["projects"]))}'
                        + (f'<br><span style="color:{MUTED};font-size:13px">Other: {escape(other)}</span>' if other else "") + "</td></tr>")
        o.append(f'<div style="margin-top:16px;background:{tint(report.ORANGE, .07)};border:1px solid {tint(report.ORANGE, .3)};border-radius:8px;padding:12px 14px">'
                 f'<div style="font-size:12px;font-weight:bold;letter-spacing:.04em;color:{shade(report.ORANGE)}">AT A GLANCE</div>'
                 f'<table cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;font-size:14px">{"".join(rows)}</table>')
        if r["suggestions"]:
            o.append(f'<div style="font-size:12px;font-weight:bold;letter-spacing:.04em;color:{report.ACCENT};margin-top:12px">WHERE CLAUDE COULD HAVE HELPED</div>'
                     f'<ul style="margin:4px 0 0;padding-left:18px">'
                     + "".join(f'<li><b>{escape(s["name"])}:</b> {escape(s["text"])}</li>' for s in r["suggestions"]) + "</ul>")
        o.append(f'</div><h2 {h2}>Full breakdown</h2>')

    sub = f'style="font-size:12px;color:{MUTED};margin-top:2px"'
    ul = 'style="margin:0;padding-left:18px"'
    if not r["people"]:
        o.append(f'<p {small}>Nothing sent this week.</p>')
    for pp in r["people"]:
        col = pc[pp["name"]]
        initials = "".join(w[0] for w in pp["name"].split()[:2]).upper()
        o.append(f'<div style="margin:22px 0 0;border:1px solid {LINE};border-left:4px solid {col};border-radius:8px;padding:12px 14px;background:{tint(col, .04)}">'
                 f'<div style="font-size:16px;font-weight:bold;margin-bottom:8px"><span style="display:inline-block;width:26px;height:26px;line-height:26px;'
                 f'text-align:center;border-radius:13px;background:{col};color:#fff;font-size:11px;margin-right:8px">{escape(initials)}</span>{escape(pp["name"])}</div>')
        for u in pp["projects"]:
            extra = (f'Time saved with Claude: {escape(u["timeSaved"])}' if u["type"] == "Claude" and u.get("timeSaved")
                     else (f'Claude could have helped: {escape(u["couldHelp"])}' if u.get("couldHelp") else ""))
            o.append(f'<div style="margin:0 0 14px;padding-left:10px;border-left:3px solid {report.ACCENT if u["type"] == "Claude" else LINE}">'
                     f'<b>{escape(u["project"])}</b> {pill("Claude project", report.ACCENT) if u["type"] == "Claude" else pill("Non-Claude project", report.TEAL)}'
                     f'{" " + pill("New this week", report.GREEN) if u["new"] else ""}'
                     f'<div style="font-size:11px;font-weight:bold;color:{report.GREEN};margin-top:4px">PROGRESS</div><ul {ul}>' + "".join(f"<li>{escape(b)}</li>" for b in u["progress"]) + "</ul>"
                     + (f'<div style="font-size:11px;font-weight:bold;color:{report.BLUE};margin-top:4px">NEXT STEPS</div><ul {ul}>' + "".join(f"<li>{escape(b)}</li>" for b in u["nextSteps"]) + "</ul>" if u.get("nextSteps") else "")
                     + (f'<div {small}>{extra}</div>' if extra else "") + "</div>")
        if pp["tasks"]:
            o.append(f'<div style="font-size:11px;font-weight:bold;color:{col};margin-top:4px">OTHER WORK</div>'
                     + "".join(f'<div style="margin:4px 0">{pill(t["category"], report.colour_for(t["category"]))}'
                               f'{" " + pill("Claude saved " + t["timeSaved"], report.ACCENT) if t["claude"] else ""}{" — " + escape(t["note"]) if t.get("note") else ""}</div>'
                               for t in pp["tasks"]))
        o.append("</div>")
    if r["spend"]:
        o.append(f'<h2 {h2}>Usage and spend</h2><table cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px;width:100%">'
                 f'<tr style="color:{MUTED};font-size:12px"><td style="padding:4px 8px 4px 0;border-bottom:1px solid {LINE}">Name</td>'
                 f'<td align="right" style="padding:4px 8px;border-bottom:1px solid {LINE}">Spend</td><td align="right" style="padding:4px 8px;border-bottom:1px solid {LINE}">Budget</td>'
                 f'<td align="right" style="padding:4px 0 4px 8px;border-bottom:1px solid {LINE}">Used</td></tr>'
                 + "".join(f'<tr><td style="padding:4px 8px 4px 0;border-bottom:1px solid {LINE}">{escape(u["name"])}</td>'
                           f'<td align="right" style="padding:4px 8px;border-bottom:1px solid {LINE}">{report.money(u["spend"])}</td>'
                           f'<td align="right" style="padding:4px 8px;border-bottom:1px solid {LINE}">{report.money(u["budget"])}</td>'
                           f'<td align="right" style="padding:4px 0 4px 8px;border-bottom:1px solid {LINE};{"color:" + RED + ";font-weight:bold" if u["flag"] else ""}">{u["pct"]:.0f}%{" ⚑" if u["flag"] else ""}</td></tr>'
                           for u in r["spend"]) + "</table>")
    if r["requests"]:
        o.append(f'<h2 {h2}>Budget increase requests</h2><ul style="margin:0;padding-left:18px">'
                 + "".join(f'<li>{escape(u["name"])}: {report.money(u["budget"])} → {report.money(u["increase"]["amount"])} '
                           f'({escape(u["increase"]["status"])}). {escape(u["increase"]["reason"])}</li>' for u in r["requests"]) + "</ul>")

    o.append(f'<p style="margin:24px 0 8px"><a href="{escape(link)}" style="display:inline-block;background:{report.ACCENT};color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none;font-weight:bold">'
             f'Open the full report</a></p><p {small}>The full report has the usage screenshots and the latest edits. It needs the admin login.</p></div>')
    return "".join(o)
