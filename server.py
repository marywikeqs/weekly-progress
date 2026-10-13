"""
server.py — Weekly Progress.

The team's weekly check-in: a Project Update form (one per project per person
per week), a Weekly Usage form (one per person per week, with a screenshot of
their Claude usage), and the CRO's weekly report built from both. Standard
library only, same shape as the RevOps Answer Book and the Commission Planner:
Basic Auth, data on a Railway volume.

Where things live:
  * the app         — index.html (the forms), admin.html, report.py; from the
                      repo, deploys with every push.
  * the settings    — STATE_DIR/config.json: roster, project list, screenshot
                      instructions. Seeded from data/seed_config.json the first
                      time the app starts with an empty volume, then edited on
                      the admin page only.
  * the submissions — STATE_DIR/updates.json and usage.json, plus
                      STATE_DIR/screenshots/. STATE_DIR/changes.jsonl keeps one
                      line per save or delete so nothing is lost.

Who sees what:
  * team  (APP_USER / APP_PASSWORD)   — the two forms.
  * admin (ADMIN_USER / ADMIN_PASSWORD) — everything: the forms, the admin
    page, the report, screenshots and the CSV exports.
  With neither password set (running locally) everything is open.

Environment:
  PORT            listen port                       (Railway sets this)
  APP_USER        team username                     default "team"
  APP_PASSWORD    team password                     unset = forms are open
  ADMIN_USER      admin username                    default "admin"
  ADMIN_PASSWORD  admin password                    unset = admin pages are
                                                    open only when APP_PASSWORD
                                                    is unset too
  STATE_DIR       where submissions are written     default the Railway volume,
                                                    else ./state
  APP_UID/APP_GID who to run as after claiming a root-owned volume

Run locally:  python server.py   (then http://localhost:8080)
"""

from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import os
import re
import secrets
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import mailer
import report

APP_ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
APP_USER = os.environ.get("APP_USER", "team")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
STATE_DIR = Path(
    os.environ.get("STATE_DIR")
    or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or (APP_ROOT / "state")
).resolve()
SHOTS = STATE_DIR / "screenshots"
LOG = STATE_DIR / "changes.jsonl"
SEED = APP_ROOT / "data" / "seed_config.json"
APP_UID = int(os.environ.get("APP_UID", "10001"))
APP_GID = int(os.environ.get("APP_GID", os.environ.get("APP_UID", "10001")))

MAX_BODY = 256 * 1024
MAX_SHOT = 8 * 1024 * 1024          # the page shrinks big screenshots before sending
SHOT_RX = re.compile(r"^[a-f0-9]{20}\.(png|jpg|webp)$")
LOCK = threading.Lock()

STATUSES = ["Not started", "In progress", "On track", "At risk", "Blocked", "Completed"]
TYPES = ["Claude", "Non-Claude"]
TIME_SAVED = list(report.TIME_SAVED_HOURS)
REQUEST_STATUSES = ["Approved", "Pending", "Denied"]
TIME_SPENT = ["Under 1 hr", "1-3 hrs", "3-5 hrs", "5-10 hrs", "10+ hrs"]
DEFAULT_CATEGORIES = ["Support Hub", "Deal Desk", "Ad hoc analysis"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------- data on disk ----------------

# Everything lives in memory once loaded; the disk copy is what survives a
# restart. If STATE_DIR can't be written the app still serves, and /healthz
# and the pages say saves fail, instead of crash-looping on deploy.
DATA: dict = {}
WRITABLE = True


def _read(name: str, default):
    p = STATE_DIR / name
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"could not read {p}: {exc}\n")
    return default


def load() -> None:
    cfg = _read("config.json", None)
    if cfg is None:
        cfg = json.loads(SEED.read_text(encoding="utf-8"))
        DATA["config"] = cfg
        write("config")
    cfg.setdefault("reportEmail", "")
    cfg.setdefault("taskCategories", list(DEFAULT_CATEGORIES))
    DATA["config"] = cfg
    DATA["updates"] = _read("updates.json", [])
    DATA["usage"] = _read("usage.json", [])
    DATA["tasks"] = _read("tasks.json", [])      # other work: Support Hub, Deal Desk, ...
    DATA["sent"] = _read("sent.json", {})      # week -> when the report email went out


def write(key: str) -> bool:
    global WRITABLE
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        target = STATE_DIR / f"{key}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(DATA[key], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
        WRITABLE = True
    except OSError as exc:
        WRITABLE = False
        sys.stderr.write(f"could not save {key}: {exc}\n")
    return WRITABLE


def log_change(action: str, kind: str, before, after) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now(), "action": action, "kind": kind,
                                "before": before, "after": after}, ensure_ascii=False) + "\n")
    except OSError as exc:
        sys.stderr.write(f"could not append to {LOG}: {exc}\n")


# ---------------- validation ----------------

def is_friday(s) -> bool:
    try:
        d = date.fromisoformat(str(s))
    except ValueError:
        return False
    return d.weekday() == 4 and abs((d - date.today()).days) <= 400


def money(v) -> float | None:
    try:
        n = round(float(str(v).replace(",", "").replace("$", "").strip()), 2)
    except ValueError:
        return None
    return n if 0 <= n < 1e7 else None


def roster_names() -> set:
    return {p["name"] for p in DATA["config"]["roster"]}


def project_choices() -> list:
    """The project dropdown: the admin list plus every project anyone has
    submitted, so a project typed in once is pickable from then on. An admin
    entry sets the default type, and unticking Active hides a project."""
    out = {p["name"].lower(): dict(p) for p in DATA["config"]["projects"]}
    for u in sorted(DATA["updates"], key=lambda u: u.get("updatedAt", "")):
        c = out.setdefault(u["project"].lower(), {"name": u["project"], "type": "", "active": True, "learned": True})
        if c.get("learned"):
            c["type"] = u["type"]          # most recent submission's type
    return sorted(out.values(), key=lambda p: p["name"].lower())


def clean_update(d: dict) -> dict | str:
    name = str(d.get("name", "")).strip()
    if name not in roster_names():
        return "Pick your name."
    if not is_friday(d.get("week")):
        return "Pick the week ending date."
    project = re.sub(r"\s+", " ", str(d.get("project", ""))).strip()[:120]
    if not project:
        return "Pick a project, or choose New project and type its name."
    known = {p["name"].lower(): p["name"] for p in project_choices()}
    project = known.get(project.lower(), project)
    ptype = d.get("type")
    if ptype not in TYPES:
        return "Pick a project type."
    status = d.get("status")
    if status not in STATUSES:
        return "Pick a status."
    def lines(key):
        out = [re.sub(r"\s+", " ", str(b)).strip()[:500] for b in (d.get(key) or [])]
        return [b for b in out if b][:3]
    bullets, steps = lines("progress"), lines("nextSteps")
    if not bullets:
        return "Add at least one line of progress this week."
    if not steps:
        return "Add at least one next step."
    out = {"name": name, "week": d["week"], "project": project,
           "type": ptype, "status": status, "progress": bullets, "nextSteps": steps,
           "timeSaved": None, "couldHelp": ""}
    if ptype == "Claude":
        if d.get("timeSaved") not in TIME_SAVED:
            return "Pick how much time Claude saved this week."
        out["timeSaved"] = d["timeSaved"]
    else:
        out["couldHelp"] = re.sub(r"\s+", " ", str(d.get("couldHelp", ""))).strip()[:300]
    return out


def task_categories() -> list:
    """The admin's categories plus any typed in before, so they stay pickable."""
    out = {c.lower(): c for c in DATA["config"].get("taskCategories", [])}
    for t in DATA["tasks"]:
        out.setdefault(t["category"].lower(), t["category"])
    return list(out.values())


def clean_task(d: dict) -> dict | str:
    cat = re.sub(r"\s+", " ", str(d.get("category", ""))).strip()[:80]
    if not cat:
        return "Choose or name the kind of work."
    cat = {c.lower(): c for c in task_categories()}.get(cat.lower(), cat)
    spent = d.get("timeSpent") or None
    if spent is not None and spent not in TIME_SPENT:
        return "Choose how much time it took."
    if d.get("claude") not in (True, False):
        return "Say whether you used Claude for it."
    out = {"category": cat, "timeSpent": spent, "note": re.sub(r"\s+", " ", str(d.get("note", ""))).strip()[:300],
           "claude": d["claude"], "timeSaved": None}
    if d["claude"]:
        if d.get("timeSaved") not in TIME_SAVED:
            return "Choose how much time Claude saved."
        out["timeSaved"] = d["timeSaved"]
    return out


def clean_usage(d: dict) -> dict | str:
    name = str(d.get("name", "")).strip()
    if name not in roster_names():
        return "Pick your name."
    if not is_friday(d.get("week")):
        return "Pick the week ending date."
    spend, budget = money(d.get("spend")), money(d.get("budget"))
    if spend is None:
        return "Enter your spend so far this month."
    if budget is None or budget <= 0:
        return "Enter your current monthly budget."
    if d.get("increaseRequested") not in (True, False):
        return "Say whether you requested a budget increase this week."
    out = {"name": name, "week": d["week"], "spend": spend, "budget": budget,
           "increaseRequested": d["increaseRequested"], "increase": None}
    if d["increaseRequested"]:
        inc = d.get("increase") or {}
        amount = money(inc.get("amount"))
        reason = re.sub(r"\s+", " ", str(inc.get("reason", ""))).strip()[:300]
        if amount is None or amount <= 0:
            return "Enter the new budget amount you requested."
        if not reason:
            return "Enter the reason for the increase."
        if inc.get("status") not in REQUEST_STATUSES:
            return "Pick the request status."
        out["increase"] = {"amount": amount, "reason": reason, "status": inc["status"]}
    return out


def save_shot(shot) -> str | None:
    """A data: URL from the page -> a file name under screenshots/, or None."""
    m = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)", str(shot or ""))
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except ValueError:
        return None
    ext = {"png": "png", "jpeg": "jpg", "webp": "webp"}[m.group(1)]
    magic = {"png": raw[:8] == b"\x89PNG\r\n\x1a\n", "jpg": raw[:3] == b"\xff\xd8\xff",
             "webp": raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"}[ext]
    if not magic or not raw or len(raw) > MAX_SHOT:
        return None
    fname = f"{secrets.token_hex(10)}.{ext}"
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        (SHOTS / fname).write_bytes(raw)
    except OSError as exc:
        sys.stderr.write(f"could not save screenshot: {exc}\n")
        return None
    return fname


def clean_config(d: dict) -> dict | str:
    roster, seen = [], set()
    for p in d.get("roster") or []:
        n = re.sub(r"\s+", " ", str(p.get("name", ""))).strip()[:80]
        if n and n.lower() not in seen:
            seen.add(n.lower())
            roster.append({"name": n, "active": bool(p.get("active", True))})
    projects, seen = [], set()
    for p in d.get("projects") or []:
        n = re.sub(r"\s+", " ", str(p.get("name", ""))).strip()[:120]
        if n and n.lower() not in seen and n.lower() != "other":
            seen.add(n.lower())
            projects.append({"name": n, "type": p.get("type") if p.get("type") in TYPES else "",
                             "active": bool(p.get("active", True))})
    instructions = str(d.get("screenshotInstructions", "")).strip()[:2000]
    if not roster:
        return "The roster needs at least one name."
    emails = [a for a in re.split(r"[\s,;]+", str(d.get("reportEmail", ""))) if a]
    bad = [a for a in emails if not mailer.EMAIL_RX.match(a)]
    if bad:
        return f"That email address doesn't look right: {bad[0]}"
    cats, seen = [], set()
    sent = d["taskCategories"] if isinstance(d.get("taskCategories"), list) else DATA["config"].get("taskCategories", [])
    for c in sent:
        c = re.sub(r"\s+", " ", str(c)).strip()[:80]
        if c and c.lower() not in seen:
            seen.add(c.lower())
            cats.append(c)
    return {"roster": roster, "projects": projects, "screenshotInstructions": instructions,
            "reportEmail": ", ".join(emails), "taskCategories": cats}


# ---------------- the report email ----------------

# One thread sends, so a week can't go out twice. It wakes every 10 minutes
# (for the Monday backup and retries) and straight after every submission.
MAIL_KICK = threading.Event()
MAIL_ERROR = {"at": "", "error": ""}


def weeks_to_check() -> list:
    t = mailer.now_local().date()
    recent = {r["week"] for r in DATA["updates"] + DATA["usage"] if r["week"] >= (t - timedelta(days=21)).isoformat()}
    recent.add((t - timedelta(days=(t.weekday() - 4) % 7)).isoformat())      # the Friday just gone (or today)
    return sorted(w for w in recent if w not in DATA["sent"])


def due_emails() -> list:
    """[(week, report, reason)] for weeks whose email should go now."""
    out = []
    if not any(p["active"] for p in DATA["config"]["roster"]):
        return out
    now = mailer.now_local()
    for w in weeks_to_check():
        r = report.build(DATA, w)
        if not r["participation"]["missing"]:
            out.append((w, r, "everyone"))
        elif r["participation"]["complete"] and mailer.deadline(w) <= now < mailer.deadline(w) + timedelta(days=3):
            # (a week nobody sent anything for gets no email)
            out.append((w, r, "deadline"))
    return out


def mail_once() -> None:
    if not mailer.configured():
        return
    with LOCK:
        to = mailer.recipients(DATA["config"])
        due = due_emails() if to else []
    for week, r, reason in due:
        note = "" if reason == "everyone" else f"Sent at the Monday deadline. Still missing: {', '.join(r['participation']['missing'])}."
        ok, detail = mailer.send(to, mailer.subject(r), mailer.html(r, note), mailer.plain(r))
        with LOCK:
            if ok:
                DATA["sent"][week] = {"at": now(), "to": to, "reason": reason, "id": detail}
                write("sent")
                MAIL_ERROR.update(at="", error="")
            else:
                MAIL_ERROR.update(at=now(), error=detail)
        sys.stderr.write(f"report email for {week} ({reason}): {'sent' if ok else detail}\n")


def mail_loop() -> None:
    while True:
        try:
            mail_once()
        except Exception as exc:          # never let the thread die
            sys.stderr.write(f"mail loop error: {exc}\n")
        MAIL_KICK.wait(600)
        MAIL_KICK.clear()


# ---------------- exports ----------------

def to_csv(rows: list, cols: list) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in cols])
    for r in rows:
        w.writerow([c[1](r) for c in cols])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8


UPDATE_COLS = [
    ("Week ending", lambda r: r["week"]), ("Name", lambda r: r["name"]),
    ("Project", lambda r: r["project"]),
    ("Type", lambda r: r["type"]), ("Status", lambda r: r["status"]),
    ("Progress this week", lambda r: "\n".join("• " + b for b in r["progress"])),
    ("Next steps", lambda r: "\n".join("• " + b for b in r.get("nextSteps", []))),
    ("Time saved with Claude", lambda r: r.get("timeSaved") or ""),
    ("Time saved (hrs, midpoint)", lambda r: report.hours(r) if r["type"] == "Claude" else ""),
    ("Claude could have helped", lambda r: r.get("couldHelp", "")),
    ("Submitted", lambda r: r.get("submittedAt", "")), ("Last updated", lambda r: r.get("updatedAt", "")),
]
TASK_COLS = [
    ("Week ending", lambda r: r["week"]), ("Name", lambda r: r["name"]), ("Work", lambda r: r["category"]),
    ("Time spent", lambda r: r.get("timeSpent") or ""), ("Note", lambda r: r.get("note", "")),
    ("Used Claude", lambda r: "Yes" if r["claude"] else "No"), ("Time saved with Claude", lambda r: r.get("timeSaved") or ""),
    ("Time saved (hrs, midpoint)", lambda r: report.hours(r) if r["claude"] else ""),
    ("Submitted", lambda r: r.get("submittedAt", "")), ("Last updated", lambda r: r.get("updatedAt", "")),
]
USAGE_COLS = [
    ("Week ending", lambda r: r["week"]), ("Name", lambda r: r["name"]),
    ("Spend this month", lambda r: r["spend"]), ("Monthly budget", lambda r: r["budget"]),
    ("% of budget", lambda r: round(100 * r["spend"] / r["budget"], 1) if r["budget"] else ""),
    ("Requested increase", lambda r: "Yes" if r["increaseRequested"] else "No"),
    ("Requested budget", lambda r: (r.get("increase") or {}).get("amount", "")),
    ("Reason", lambda r: (r.get("increase") or {}).get("reason", "")),
    ("Request status", lambda r: (r.get("increase") or {}).get("status", "")),
    ("Screenshot", lambda r: f"/screenshots/{r['screenshot']}" if r.get("screenshot") else ""),
    ("Submitted", lambda r: r.get("submittedAt", "")), ("Last updated", lambda r: r.get("updatedAt", "")),
]


# ---------------- volume plumbing (as in the Answer Book) ----------------

def claim_state_dir() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        for root, dirs, files in os.walk(STATE_DIR):
            os.chown(root, APP_UID, APP_GID)
            for f in files:
                os.chown(os.path.join(root, f), APP_UID, APP_GID)
    except OSError as exc:
        sys.stderr.write(f"could not take ownership of {STATE_DIR}: {exc}\n")
    try:
        os.setgroups([])
        os.setgid(APP_GID)
        os.setuid(APP_UID)
    except OSError as exc:
        sys.stderr.write(f"WARNING: still running as root: {exc}\n")


def state_persistent() -> bool:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        return os.stat(STATE_DIR).st_dev != os.stat(APP_ROOT).st_dev
    except OSError:
        return False


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "WeeklyProgress"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_body(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_body(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def role(self) -> str | None:
        """'admin', 'team', or None. Admin passes every team gate too."""
        if not APP_PASSWORD and not ADMIN_PASSWORD:
            return "admin"
        h = self.headers.get("Authorization", "")
        user = pw = ""
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
            except Exception:
                pass
        if ADMIN_PASSWORD and hmac.compare_digest(user, ADMIN_USER) & hmac.compare_digest(pw, ADMIN_PASSWORD):
            return "admin"
        if not APP_PASSWORD or (hmac.compare_digest(user, APP_USER) & hmac.compare_digest(pw, APP_PASSWORD)):
            return "team"
        return None

    def gate(self, need: str) -> bool:
        r = self.role()
        if r == "admin" or (need == "team" and r == "team"):
            return True
        realm = "Weekly Progress admin" if need == "admin" else "Weekly Progress"
        self.send_body(HTTPStatus.UNAUTHORIZED, b"Sign in required.", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": f'Basic realm="{realm}"'})
        return False

    def read_json(self, limit=MAX_BODY):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            return None
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return None

    def page(self, name: str):
        self.send_body(200, (APP_ROOT / name).read_bytes(), "text/html; charset=utf-8")

    def base_url(self) -> str:
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"localhost:{PORT}"
        proto = self.headers.get("X-Forwarded-Proto") or "http"   # Railway sets it to https
        return f"{proto}://{host}"

    # GET ------------------------------------------------------------
    def do_GET(self):
        url = urlsplit(self.path)
        path, q = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        if path == "/healthz":
            return self.send_json(200, {"ok": True, "persistent": state_persistent(), "writable": WRITABLE,
                                        "teamAuth": bool(APP_PASSWORD), "adminAuth": bool(ADMIN_PASSWORD),
                                        "email": mailer.configured()})
        if path in ("/", "/index.html"):
            return self.gate("team") and self.page("index.html")
        if path == "/api/config":
            if not self.gate("team"):
                return
            cfg = DATA["config"]
            return self.send_json(200, {
                "roster": [p["name"] for p in cfg["roster"] if p["active"]],
                "projects": [{"name": p["name"], "type": p["type"]} for p in project_choices() if p["active"]],
                "screenshotInstructions": cfg.get("screenshotInstructions", ""),
                "statuses": STATUSES, "timeSaved": TIME_SAVED, "requestStatuses": REQUEST_STATUSES,
                "taskCategories": task_categories(), "timeSpent": TIME_SPENT,
                "writable": WRITABLE, "persistent": state_persistent(),
            })
        if path == "/api/mine":
            if not self.gate("team"):
                return
            name, week = q.get("name", ""), q.get("week", "")
            with LOCK:
                ups = [u for u in DATA["updates"] if u["name"] == name and u["week"] == week]
                use = next((u for u in DATA["usage"] if u["name"] == name and u["week"] == week), None)
                earlier = sorted((u for u in DATA["usage"] if u["name"] == name and u["week"] < week), key=lambda u: u["week"])
                prev_week = max((u["week"] for u in DATA["updates"] if u["name"] == name and u["week"] < week), default=None)
                prev = [u for u in DATA["updates"] if u["name"] == name and u["week"] == prev_week]
                tasks = [t for t in DATA["tasks"] if t["name"] == name and t["week"] == week]
                prev_tasks = [t for t in DATA["tasks"] if t["name"] == name and t["week"] == prev_week]
            # A new week starts from last week's projects, other work and budget.
            return self.send_json(200, {"updates": ups, "usage": use, "tasks": tasks,
                                        "lastBudget": earlier[-1]["budget"] if earlier else None,
                                        "lastWeek": prev_week, "lastProjects": prev, "lastTasks": prev_tasks})

        # admin only from here
        if path in ("/admin", "/admin.html"):
            return self.gate("admin") and self.page("admin.html")
        if path == "/api/admin":
            if not self.gate("admin"):
                return
            with LOCK:
                listed = {p["name"].lower() for p in DATA["config"]["projects"]}
                learned = {}
                for u in DATA["updates"]:
                    if u["project"].lower() not in listed:
                        o = learned.setdefault(u["project"].lower(), {"name": u["project"], "type": u["type"], "count": 0, "last": ""})
                        o["count"] += 1
                        o["last"] = max(o["last"], u["week"])
                email = {"configured": mailer.configured(), "from": mailer.FROM, "timezone": mailer.TZ_NAME,
                         "sent": DATA["sent"], "error": MAIL_ERROR["error"], "errorAt": MAIL_ERROR["at"]}
                return self.send_json(200, {"config": DATA["config"], "learned": sorted(learned.values(), key=lambda o: o["name"].lower()), "email": email,
                                            "weeks": report.weeks_with_data(DATA), "writable": WRITABLE, "persistent": state_persistent()})
        if path in ("/report", "/report.txt"):
            if not self.gate("admin"):
                return
            with LOCK:
                week = q.get("week") if is_friday(q.get("week")) else report.default_week(DATA)
                r = report.build(DATA, week)
            if path == "/report.txt":
                return self.send_body(200, report.render_text(r, self.base_url()).encode("utf-8"), "text/plain; charset=utf-8")
            return self.send_body(200, report.render_html(r, report.weeks_with_data(DATA, week)).encode("utf-8"), "text/html; charset=utf-8")
        m = re.fullmatch(r"/screenshots/([^/]+)", path)
        if m:
            if not self.gate("admin"):
                return
            if not SHOT_RX.match(m.group(1)) or not (SHOTS / m.group(1)).is_file():
                return self.send_json(404, {"error": "Not found"})
            ext = m.group(1).rsplit(".", 1)[1]
            ctype = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[ext]
            return self.send_body(200, (SHOTS / m.group(1)).read_bytes(), ctype, {"Cache-Control": "private, max-age=86400"})
        m = re.fullmatch(r"/export/(updates|usage|tasks)\.csv", path)
        if m:
            if not self.gate("admin"):
                return
            with LOCK:
                rows = [r for r in DATA[m.group(1)] if not q.get("week") or r["week"] == q["week"]]
            rows.sort(key=lambda r: (r["week"], r["name"], r.get("project", r.get("category", ""))))
            cols, label = {"updates": (UPDATE_COLS, "projects"), "usage": (USAGE_COLS, "usage"), "tasks": (TASK_COLS, "other-work")}[m.group(1)]
            body = to_csv(rows, cols)
            fname = f"weekly-{label}{'-' + q['week'] if q.get('week') else ''}.csv"
            return self.send_body(200, body, "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{fname}"'})
        self.send_json(404, {"error": "Not found"})

    # writes -----------------------------------------------------------
    def unsaved(self):
        return self.send_json(503, {"error": "The server couldn't save that. Its storage isn't writable."})

    def do_POST(self):
        """The whole week in one send: every project update, other work, and usage.

        Validates everything first and saves nothing unless all of it is
        good. The projects sent become this person's full list for the week
        (one left off is deleted); a resend just updates."""
        if urlsplit(self.path).path == "/api/admin/test-email":
            return self.test_email()
        if urlsplit(self.path).path != "/api/submit":
            return self.send_json(404, {"error": "Not found"})
        if not self.gate("team"):
            return
        body = self.read_json(MAX_SHOT * 4 // 3 + MAX_BODY) or {}
        name, week = str(body.get("name", "")).strip(), body.get("week")
        with LOCK:
            projects, errors, seen = [], [], set()
            for i, d in enumerate(body.get("projects") or []):
                u = clean_update({**d, "name": name, "week": week})
                if isinstance(u, str):
                    errors.append({"project": i, "error": u})
                elif u["project"].lower() in seen:
                    errors.append({"project": i, "error": f"{u['project']} is in here twice. Combine them into one."})
                else:
                    seen.add(u["project"].lower())
                    projects.append(u)
            if not projects and not errors:
                errors.append({"project": None, "error": "Add at least one project."})
            tasks = []
            for i, d in enumerate(body.get("tasks") or []):
                t = clean_task(d)
                if isinstance(t, str):
                    errors.append({"task": i, "error": t})
                else:
                    tasks.append({"name": name, "week": week, **t})
            usage = clean_usage({**(body.get("usage") or {}), "name": name, "week": week})
            if isinstance(usage, str):
                errors.append({"usage": True, "error": usage})
            cur_use = next((r for r in DATA["usage"] if r["name"] == name and r["week"] == week), None)
            shot = cur_use.get("screenshot") if cur_use else None
            if not errors and not (body.get("usage") or {}).get("screenshot") and not shot:
                errors.append({"usage": True, "error": "Attach your usage screenshot."})
            if errors:
                # name / week problems repeat on every part; say them once
                top = next((e["error"] for e in errors if e["error"] in ("Pick your name.", "Pick the week ending date.")), None)
                return self.send_json(400, {"error": top or errors[0]["error"], "errors": [] if top else errors})
            if (body.get("usage") or {}).get("screenshot"):
                shot = save_shot(body["usage"]["screenshot"])
                if not shot:
                    return self.send_json(400, {"error": "That screenshot couldn't be read. Upload a PNG or JPG image.",
                                                "errors": [{"usage": True, "error": "That screenshot couldn't be read."}]})
            usage["screenshot"] = shot

            # projects: replace this person's week with what was sent
            stamp = now()
            old = {r["project"].lower(): r for r in DATA["updates"] if r["name"] == name and r["week"] == week}
            keep = [r for r in DATA["updates"] if not (r["name"] == name and r["week"] == week)]
            saved = []
            for u in projects:
                prev = old.pop(u["project"].lower(), None)
                if prev:
                    rec = {**prev, **u, "updatedAt": stamp}
                    if {k: prev.get(k) for k in u} != u:
                        log_change("update", "project", prev, rec)
                else:
                    rec = {"id": "u-" + secrets.token_hex(5), **u, "submittedAt": stamp, "updatedAt": stamp}
                    log_change("create", "project", None, rec)
                saved.append(rec)
            for gone in old.values():
                log_change("delete", "project", gone, None)
            before_updates, before_usage, before_tasks = DATA["updates"], list(DATA["usage"]), DATA["tasks"]
            DATA["updates"] = keep + saved
            mine = lambda r: r["name"] == name and r["week"] == week
            old_tasks = [r for r in DATA["tasks"] if mine(r)]
            saved_tasks = [{"id": "t-" + secrets.token_hex(5), **t, "submittedAt": stamp, "updatedAt": stamp} for t in tasks]
            DATA["tasks"] = [r for r in DATA["tasks"] if not mine(r)] + saved_tasks
            if old_tasks or saved_tasks:
                log_change("replace", "tasks", old_tasks, saved_tasks)
            if cur_use:
                rec_use = {**cur_use, **usage, "updatedAt": stamp}
                DATA["usage"] = [rec_use if r is cur_use else r for r in DATA["usage"]]
            else:
                rec_use = {"id": "s-" + secrets.token_hex(5), **usage, "submittedAt": stamp, "updatedAt": stamp}
                DATA["usage"].append(rec_use)
            if not (write("updates") and write("usage") and write("tasks")):
                DATA["updates"], DATA["usage"], DATA["tasks"] = before_updates, before_usage, before_tasks
                return self.unsaved()
            log_change("update" if cur_use else "create", "usage", cur_use, rec_use)
        MAIL_KICK.set()       # everyone in? the email goes now
        return self.send_json(200, {"updates": saved, "tasks": saved_tasks, "usage": rec_use, "resent": bool(cur_use)})

    def test_email(self):
        """Sends the latest week's report now, marked as a test. Doesn't count as that week's email."""
        if not self.gate("admin"):
            return
        if not mailer.configured():
            return self.send_json(400, {"error": "Email isn't set up yet: add RESEND_API_KEY in Railway's Variables."})
        with LOCK:
            to = mailer.recipients(DATA["config"])
            r = report.build(DATA, report.default_week(DATA))
        if not to:
            return self.send_json(400, {"error": "Add an address to send the report to, and save first."})
        ok, detail = mailer.send(to, "[Test] " + mailer.subject(r), mailer.html(r, "This is a test send from the admin page."), mailer.plain(r))
        if not ok:
            return self.send_json(502, {"error": detail})
        self.send_json(200, {"sent": True, "to": to, "week": r["week"]})

    def do_PUT(self):
        if urlsplit(self.path).path != "/api/admin/config":
            return self.send_json(404, {"error": "Not found"})
        if not self.gate("admin"):
            return
        body = self.read_json() or {}
        with LOCK:
            cfg = clean_config(body)
            if isinstance(cfg, str):
                return self.send_json(400, {"error": cfg})
            before = DATA["config"]
            DATA["config"] = cfg
            if not write("config"):
                DATA["config"] = before
                return self.unsaved()
            log_change("update", "config", before, cfg)
        self.send_json(200, {"config": cfg})


def main():
    claim_state_dir()
    with LOCK:
        load()
    sys.stderr.write(f"data at {STATE_DIR} ({'persistent volume' if state_persistent() else 'container disk — resets on deploy'}); "
                     f"team auth {'on' if APP_PASSWORD else 'OFF'}, admin auth {'on' if ADMIN_PASSWORD else 'OFF'}\n")
    to = ", ".join(mailer.recipients(DATA["config"])) or "nobody yet"
    sys.stderr.write(f"report email {'on, to ' + to if mailer.configured() else 'OFF (no RESEND_API_KEY)'}\n")
    threading.Thread(target=mail_loop, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
