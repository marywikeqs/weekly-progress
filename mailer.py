"""
mailer.py — emails the weekly report through Resend (https://resend.com).

The server sends one email per week: as soon as everyone on the roster has
sent their update, or at the backup deadline (Monday 9 AM after the week
ends) with whoever is in. Sending is over HTTPS, so Railway's SMTP block
doesn't matter.

Environment:
  RESEND_API_KEY   the Resend API key                 unset = no email at all
  REPORT_FROM      the From line                      default Resend's shared
                   test sender, which only delivers to the address the Resend
                   account was opened with, until a domain is verified there
  PUBLIC_URL       the app's address, for links       default the Railway URL
  REPORT_TIMEZONE  the backup deadline's timezone     default America/Chicago
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
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


def deadline(week: str) -> datetime:
    """The backup send: 9 AM on the Monday after the week's Friday."""
    return datetime.combine(date.fromisoformat(week) + timedelta(days=3), time(9), TZ)


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
STATUS_COLOUR = {"Blocked": RED, "At risk": AMBER, "Completed": GREEN, "On track": GREEN, "In progress": ACCENT, "Not started": MUTED}


def html(r: dict, note: str = "") -> str:
    c, l, p, hp = r["cur"], r["last"], r["participation"], r["hasPrev"]
    link = f"{PUBLIC_URL}/report?week={r['week']}"
    h2 = f'style="font-size:16px;margin:24px 0 8px;color:{INK}"'
    small = f'style="font-size:13px;color:{MUTED};margin:2px 0"'
    o = [f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:14px;line-height:1.5;color:{INK};max-width:640px">']
    if note:
        o.append(f'<p style="background:#fff2df;color:{AMBER};padding:8px 12px;border-radius:6px;margin:0 0 16px">{escape(note)}</p>')
    o.append(f'<h1 style="font-size:20px;margin:0">Weekly Progress</h1><p {small}>Week ending {escape(report.nice_date(r["week"]))}</p>')

    def wow(now, before, unit=""):
        if not hp:
            return ""
        d = now - before
        return "" if abs(d) < 1e-9 else f' <span style="color:{MUTED}">({"+" if d > 0 else "−"}{abs(d):g}{unit} vs last week)</span>'

    spend = f"{report.money(c['spend'])} of {report.money(c['budget'])} ({100 * c['spend'] / c['budget']:.0f}%)" if c["budget"] else "none reported"
    o.append(f'<table cellpadding="0" cellspacing="0" style="margin-top:14px;border-collapse:collapse;font-size:14px">'
             + "".join(f'<tr><td style="padding:3px 16px 3px 0;color:{MUTED}">{k}</td><td style="padding:3px 0"><b>{v}</b></td></tr>' for k, v in [
                 ("Sent their update", f"{len(p['complete'])} of {p['roster']}" + (f' <span style="color:{RED};font-weight:400">missing {escape(", ".join(p["missing"]))}</span>' if p["missing"] else "")),
                 ("Projects", f"{c['projects']}{wow(c['projects'], l['projects'])} <span style=\"font-weight:400;color:{MUTED}\">{c['claude']} Claude · {c['nonClaude']} non-Claude</span>"),
                 ("At risk or blocked", f"{c['atRisk']}{wow(c['atRisk'], l['atRisk'])}"),
                 ("Time saved with Claude", f"~{c['hours']:g} hrs{wow(c['hours'], l['hours'], ' hrs')}"),
                 ("Claude spend this month", spend),
             ]) + "</table>")

    o.append(f"<h2 {h2}>Projects</h2>")
    if not r["groups"]:
        o.append(f'<p {small}>No project updates.</p>')
    for g in r["groups"]:
        o.append(f'<div style="margin:0 0 14px;padding-left:10px;border-left:3px solid {RED if g["worst"] in ("Blocked", "At risk") else LINE}">'
                 f'<b>{escape(g["project"])}</b> <span style="color:{MUTED};font-size:12px">{escape(g["type"] if g["type"] != "Mixed" else "Claude + non-Claude")}'
                 f'{" · first reported this week" if g["new"] else ""}</span>')
        for u in g["entries"]:
            extra = (f'Time saved: {escape(u.get("timeSaved") or "None")}' if u["type"] == "Claude"
                     else (f'Claude could have helped: {escape(u["couldHelp"])}' if u.get("couldHelp") else ""))
            o.append(f'<div style="margin-top:4px">{escape(u["name"])} · <b style="color:{STATUS_COLOUR.get(u["status"], MUTED)}">{escape(u["status"])}</b>'
                     f'<div style="font-size:12px;color:{MUTED};margin-top:2px">Progress</div><ul style="margin:0;padding-left:18px">' + "".join(f"<li>{escape(b)}</li>" for b in u["progress"]) + "</ul>"
                     + (f'<div style="font-size:12px;color:{MUTED};margin-top:2px">Next steps</div><ul style="margin:0;padding-left:18px">' + "".join(f"<li>{escape(b)}</li>" for b in u["nextSteps"]) + "</ul>" if u.get("nextSteps") else "")
                     + (f'<div {small}>{extra}</div>' if extra else "") + "</div>")
        o.append("</div>")

    if r["other"]:
        o.append(f"<h2 {h2}>Other work</h2>")
        for g in r["other"]:
            o.append(f'<div style="margin:0 0 10px"><b>{escape(g["category"])}</b>'
                     + (f' <span style="color:{MUTED}">~{g["hours"]:g} hrs</span>' if g["hours"] else "") + '<ul style="margin:2px 0 0;padding-left:18px">'
                     + "".join(f'<li>{escape(t["name"])}{" · " + escape(t["timeSpent"]) if t.get("timeSpent") else ""}'
                               f'{" · Claude saved " + escape(t["timeSaved"]) if t["claude"] else ""}{": " + escape(t["note"]) if t.get("note") else ""}</li>'
                               for t in g["entries"]) + "</ul></div>")
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
    if r["suggestions"]:
        o.append(f'<h2 {h2}>Where Claude could have helped</h2><ul style="margin:0;padding-left:18px">'
                 + "".join(f'<li><b>{escape(s["project"])}</b> ({escape(s["name"])}): {escape(s["text"])}</li>' for s in r["suggestions"]) + "</ul>")

    o.append(f'<p style="margin:24px 0 0"><a href="{escape(link)}" style="background:{ACCENT};color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none;font-weight:bold">'
             f'Open the full report</a></p><p {small}>The full report has the usage screenshots and the latest edits. It needs the admin login.</p></div>')
    return "".join(o)
