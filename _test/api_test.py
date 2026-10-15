"""
End-to-end check of the API against a throwaway server.

    python _test/api_test.py

Starts server.py on port 8091 with passwords set and a temp STATE_DIR, fills
two weeks of made-up submissions, and checks auth, validation, upserts, the
report and the CSVs. Leaves the server's state in _test/out/state so the
report can be looked at afterwards (python _test/api_test.py --keep serves it).
"""

import base64
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_test" / "out"
PORT = 8091
BASE = f"http://127.0.0.1:{PORT}"
TEAM = ("team", "t-pass")
ADMIN = ("admin", "a-pass")
fails = 0
SENT = []          # emails the fake Resend received


class FakeResend(BaseHTTPRequestHandler):
    def do_POST(self):
        SENT.append({"auth": self.headers.get("Authorization"), **json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"id":"fake-1"}')

    def log_message(self, *a):
        pass


def check(cond, label):
    global fails
    print(("  ok   " if cond else "  FAIL ") + label)
    fails += not cond


def call(method, path, body=None, auth=TEAM, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    try:
        with urllib.request.urlopen(req) as r:
            b = r.read()
            return r.status, (b if raw else json.loads(b or b"{}"))
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b)
        except ValueError:
            return e.code, b


def png(w=40, h=30, rgb=(60, 90, 220)):
    row = b"\x00" + bytes(rgb) * w
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * h)) + chunk(b"IEND", b""))


def data_url(rgb):
    return "data:image/png;base64," + base64.b64encode(png(rgb=rgb)).decode()


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    fake = HTTPServer(("127.0.0.1", 8099), FakeResend)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    env = {**os.environ, "PORT": str(PORT), "STATE_DIR": str(OUT / "state"),
           "APP_PASSWORD": TEAM[1], "ADMIN_PASSWORD": ADMIN[1],
           "RESEND_API_KEY": "re_test", "RESEND_API_URL": "http://127.0.0.1:8099/emails"}
    srv = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT,
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(BASE + "/healthz")
                break
            except Exception:
                time.sleep(0.1)
        run()
    finally:
        if "--keep" in sys.argv:
            print(f"\nserving at {BASE} (team {TEAM}, admin {ADMIN}); Ctrl+C to stop")
            try:
                srv.wait()
            except KeyboardInterrupt:
                pass
        srv.terminate()
    print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)


def report_week_label(w):
    d = date.fromisoformat(w)
    return f"{d:%a}, {d:%b} {d.day}, {d.year}"


def run():
    t = date.today()
    fri = t + timedelta(days=(4 - t.weekday()) % 7) if t.weekday() <= 4 else t - timedelta(days=t.weekday() - 4)
    w1, w0 = fri.isoformat(), (fri - timedelta(days=7)).isoformat()

    print("auth")
    check(call("GET", "/api/config", auth=None)[0] == 401, "forms need the team password")
    check(call("GET", "/api/config", auth=("team", "nope"))[0] == 401, "wrong password refused")
    check(call("GET", "/api/config")[0] == 200, "team can load the form")
    check(call("GET", "/api/admin")[0] == 401, "team can't open admin")
    check(call("GET", "/report", raw=True)[0] == 401, "team can't open the report")
    check(call("GET", "/api/config", auth=ADMIN)[0] == 200, "admin passes the team gate")

    print("admin config")
    people = ["Avery Test", "Blake Test", "Casey Test", "Drew Test"]
    cfg = {"roster": [{"name": n, "active": True} for n in people] + [{"name": "Gone Test", "active": False}],
           "projects": [{"name": "Commission Audit Tool", "type": "Claude", "active": True},
                        {"name": "Spif/Adjustment Process Approval Rehaul", "type": "Non-Claude", "active": True},
                        {"name": "Territory Map", "type": "", "active": True},
                        {"name": "Other", "type": "", "active": True}],
           "screenshotInstructions": "Settings → Usage, 1st of month to today.", "reportEmail": "mwike@quiltsoftware.com"}
    s, d = call("PUT", "/api/admin/config", cfg, auth=ADMIN)
    check(s == 200 and len(d["config"]["projects"]) == 3, "config saved; a project literally named Other is dropped")
    check(call("PUT", "/api/admin/config", {"roster": []}, auth=ADMIN)[0] == 400, "empty roster refused")
    check(call("PUT", "/api/admin/config", {**cfg, "reportEmail": "mwike@"}, auth=ADMIN)[0] == 400, "bad email address refused")
    s, d = call("GET", "/api/config")
    check(d["roster"] == people, "inactive people left off the form")

    def send(name, week, projects, usage, tasks=None, ideas=None):
        return call("POST", "/api/submit", {"name": name, "week": week, "projects": projects, "usage": usage, "tasks": tasks or [],
                                            "ideas": ideas or []})

    def proj(project, type_, _old_status, progress, saved=None, help_="", steps=None):
        return {"project": project, "type": type_, "timeLeft": "2–4 weeks", "solo": True, "progress": progress, "nextSteps": steps or ["Keep going"],
                "timeSaved": saved, "claudeFit": "Maybe" if help_ else "No", "claudeHow": help_}

    hub = {"category": "support hub", "timeSpent": "3-5 hrs", "claude": True, "timeSaved": "1-3 hrs", "note": "Answered rep questions"}

    cat = proj("Commission Audit Tool", "Claude", "On track",
               ["Enhancements to bring Commission Plan Builder base pieces into CIQ.", "Keep testing and tweaking.", "", "extra"],
               "3-5 hrs", "ignored for Claude")
    spif = proj("Spif/Adjustment Process Approval Rehaul", "Non-Claude", "In progress",
                ["Plan what's working and what isn't, propose ideas, build."], "1-3 hrs", "Drafting the approval matrix")
    use = {"spend": "$412.50", "budget": "500", "increaseRequested": True,
           "increase": {"amount": 800, "reason": "Board deck month", "status": "Pending"}, "screenshot": data_url((60, 90, 220))}

    print("one send: validation (nothing saved unless all of it is good)")
    s, d = send("Avery Test", w1, [cat, {**spif, "type": ""}], use)
    check(s == 400 and d["errors"][0]["project"] == 1, "a bad second project is pointed at by index")
    check(call("GET", f"/api/mine?name=Avery%20Test&week={w1}")[1]["usage"] is None, "...and nothing was saved, not even usage")
    s, d = send("Avery Test", w1, [cat, {**cat, "project": "commission audit TOOL"}], use)
    check(s == 400 and "twice" in d["error"], "same project twice refused")
    check(send("Avery Test", w1, [], use)[0] == 400, "needs at least one project")
    check(send("Avery Test", w1, [{**cat, "progress": ["", " "]}], use)[0] == 400, "needs a progress line")
    check(send("Avery Test", w1, [{**cat, "nextSteps": [" "]}], use)[0] == 400, "needs a next step")
    s, d = send("Avery Test", w1, [{**cat, "timeLeft": ""}], use)
    check(s == 400 and "time is left" in d["error"], "needs time left on each project")
    check(send("Avery Test", w1, [{**cat, "timeLeft": "forever"}], use)[0] == 400, "time left must be one of the choices")
    check("3–5 days" in call("GET", "/api/config")[1]["timeLeft"], "time left offers days as well as weeks")
    s, d = send("Avery Test", w1, [{**cat, "solo": None}], use)
    check(s == 400 and "solo" in d["error"], "needs solo or with others")
    s, d = send("Avery Test", w1, [{**cat, "solo": False, "with": [" ", "Avery Test"]}], use)
    check(s == 400 and "working with" in d["error"], "with others needs someone (not yourself)")
    s, d = send("Avery Test", w1, [cat], use, ideas=[{"idea": "", "why": "Saves time"}])
    check(s == 400 and d["errors"][0]["idea"] == 0, "an idea's why needs the idea itself")
    s, d = send("Avery Test", w1, [cat], use, ideas=[{"idea": "Renewal alerts"}])
    check(s == 400 and "where the idea stands" in d["errors"][0]["error"], "an idea says where it stands")
    s, d = send("Avery Test", w1, [cat], use, ideas=[{"idea": "Renewal alerts", "status": "push"}])
    check(s == 400, "the old 'push' choice is no longer offered")
    s, d = send("Avery Test", w1, [cat], use, [hub, {**hub, "claude": None}])
    check(s == 400 and d["errors"][0].get("task") == 1, "a bad other-work line is pointed at by index")
    check(send("Avery Test", w1, [cat], use, [{**hub, "timeSaved": None}])[0] == 400, "Claude used on other work needs time saved")
    check(send("Avery Test", w1, [cat], use, [{**hub, "note": " "}])[0] == 400, "other work needs a short summary")

    s, d = send("Nobody", w1, [cat], use)
    check(s == 400 and d["error"] == "Pick your name." and d["errors"] == [], "name must be on the roster (said once)")
    check(send("Avery Test", (fri - timedelta(days=1)).isoformat(), [cat], use)[0] == 400, "week must be a Friday")
    s, d = send("Avery Test", w1, [cat], {**use, "screenshot": None})
    check(s == 400 and d["errors"][0].get("usage"), "screenshot required the first time")
    check(send("Avery Test", w1, [cat], {**use, "screenshot": "data:image/png;base64,aGVsbG8="})[0] == 400, "non-image refused")
    s, d = send("Avery Test", w1, [cat], {**use, "increase": {**use["increase"], "reason": ""}})
    check(s == 400 and d["errors"][0].get("usage"), "increase needs a reason")

    print("one send: saving")
    s, d = send("Avery Test", w1, [cat, spif], use, [hub, {"category": "Pricing desk", "claude": False, "note": "Quote review"}])
    check(s == 200 and not d["resent"] and len(d["updates"]) == 2 and len(d["tasks"]) == 2, "two projects + other work + usage saved together")
    check(d["tasks"][0]["category"] == "Support Hub", "other-work category matched to the list's spelling")
    check(d["updates"][0]["nextSteps"] == ["Keep going"], "next steps saved")
    check("Pricing desk" in call("GET", "/api/config")[1]["taskCategories"], "a typed kind of work stays pickable")
    a_cat = d["updates"][0]
    check(a_cat["progress"] == cat["progress"][:2] + ["extra"], "blank bullets dropped, max 3 kept")
    s2, d2 = send("Avery Test", w1, [cat, {**spif, "claudeFit": None}], use)
    check(s2 == 400 and "Claude could help" in d2["error"], "non-Claude projects need Could Claude help")
    s2, d2 = send("Avery Test", w1, [cat, {**spif, "claudeFit": "Yes", "claudeHow": " "}], use)
    check(s2 == 400 and "how Claude could help" in d2["error"], "Yes / Maybe needs how")
    s, d = send("Avery Test", w1, [cat, spif], use, [hub, {"category": "Pricing desk", "claude": False, "note": "Quote review"}])
    check("couldHelp" not in a_cat and d["updates"][1]["timeSaved"] is None, "conditional fields kept only for their type")
    check(a_cat["claudeFit"] is None and a_cat["claudeHow"] == "", "Could Claude help is only kept for non-Claude projects")
    check(d["updates"][1]["claudeFit"] == "Maybe" and d["updates"][1]["claudeHow"] == "Drafting the approval matrix", "Could Claude help saved")
    check(d["usage"]["spend"] == 412.5, "$ and commas parsed")
    shot = d["usage"]["screenshot"]
    s, d = send("Avery Test", w1, [{**cat, "project": "commission audit tool"}],
                {**use, "spend": 430, "screenshot": None, "increase": {**use["increase"], "status": "Approved"}})
    check(s == 200 and d["resent"], "resend accepted")
    check(d["updates"][0]["id"] == a_cat["id"] and d["updates"][0]["project"] == "Commission Audit Tool", "resend keeps the record, project name canonical")
    check(len(call("GET", f"/api/mine?name=Avery%20Test&week={w1}")[1]["updates"]) == 1, "a project left off the resend is removed")
    check(d["usage"]["screenshot"] == shot and d["usage"]["spend"] == 430, "resend without a screenshot keeps the one on file")
    check(call("GET", f"/api/mine?name=Avery%20Test&week={w1}")[1]["tasks"] == [], "other work left off the resend is removed")
    send("Avery Test", w1, [cat, {**spif, "solo": False, "with": ["Blake Test", "Finance team", "finance TEAM"]}], {**use, "spend": 430, "screenshot": None, "increase": {**use["increase"], "status": "Approved"}}, [hub],
         ideas=[{"idea": "Auto-build QBR decks", "why": "Saves about 2 hrs per QBR", "status": "start"}, {"idea": " ", "why": ""}, {"idea": "Renewal alerts", "status": "idea"}])

    send("Blake Test", w1, [proj("Commission Audit Tool", "Claude", "Blocked", ["Waiting on CIQ API access."], "1-3 hrs"),
                            proj("Board Deck Automation", "Claude", "On track", ["First pass at the monthly deck."], "5+ hrs")],
         {"spend": 120, "budget": 300, "increaseRequested": False, "screenshot": data_url((220, 120, 60))})
    names = [p["name"] for p in call("GET", "/api/config")[1]["projects"]]
    check("Board Deck Automation" in names, "a project typed in once joins the dropdown")
    s, d = send("Casey Test", w1, [proj("board deck automation", "Non-Claude", "Completed", ["Shipped to managers."])],
                {"spend": 50, "budget": 200, "increaseRequested": False, "screenshot": data_url((40, 160, 90))})
    check(d["updates"][0]["project"] == "Board Deck Automation", "later typing matches the existing name's spelling")
    send("Casey Test", w1, [proj("Territory Map", "Non-Claude", "Completed", ["Shipped to managers."])],
         {"spend": 50, "budget": 200, "increaseRequested": False})

    # last week, for week-over-week
    send("Avery Test", w0, [proj("Commission Audit Tool", "Claude", "In progress", ["Last week"], "1-3 hrs"),
                            proj("Territory Map", "Non-Claude", "Completed", ["Done"])],
         {"spend": 300, "budget": 500, "increaseRequested": False, "screenshot": data_url((90, 90, 90))})
    send("Blake Test", w0, [proj("Commission Audit Tool", "Claude", "On track", ["Last week"], "Under 1 hr")],
         {"spend": 100, "budget": 300, "increaseRequested": False, "screenshot": data_url((90, 90, 90))})
    w2 = (fri + timedelta(days=7)).isoformat()
    s, d = call("GET", f"/api/mine?name=Avery%20Test&week={w2}")
    check(d["lastBudget"] == 500 and d["lastWeek"] == w1 and len(d["lastProjects"]) == 2 and len(d["lastTasks"]) == 1,
          "next week starts from this week's projects, other work and budget")

    print("email")
    time.sleep(0.8)
    check(not [m for m in SENT if w1 in m["subject"] or report_week_label(w1) in m["subject"]], "no email while Drew is missing")
    send("Drew Test", w1, [proj("Territory Map", "Non-Claude", "In progress", ["Handing over"])],
         {"spend": 10, "budget": 100, "increaseRequested": False, "screenshot": data_url((10, 10, 10))})
    time.sleep(0.8)
    got = [m for m in SENT if report_week_label(w1) in m["subject"]]
    check(len(got) == 1 and "all 4 in" in got[0]["subject"], "the 4th submission sends the email: " + (got[0]["subject"] if got else "none"))
    if got:
        m = got[0]
        check(m["to"] == ["mwike@quiltsoftware.com"] and m["auth"] == "Bearer re_test", "to the configured address, with the key")
        check("Board Deck Automation" in m["html"] and "/report?week=" + w1 in m["html"] and m["text"], "html has the projects and the report link; text version too")
        (OUT / "email.html").write_text(m["html"], encoding="utf-8")
    send("Drew Test", w1, [proj("Territory Map", "Non-Claude", "Completed", ["Done"])], {"spend": 10, "budget": 100, "increaseRequested": False})
    time.sleep(0.8)
    check(len([m for m in SENT if report_week_label(w1) in m["subject"]]) == 1, "a later edit doesn't send it again")
    adm = call("GET", "/api/admin", auth=ADMIN)[1]["email"]
    check(adm["configured"] and w1 in adm["sent"] and adm["sent"][w1]["reason"] == "everyone", "admin sees it was sent")
    s, d = call("POST", "/api/admin/test-email", {}, auth=ADMIN)
    check(s == 200 and SENT[-1]["subject"].startswith("[Test]"), "admin test send")
    check(call("POST", "/api/admin/test-email", {})[0] == 401, "team can't send test emails")

    print("screenshots")
    check(call("GET", f"/screenshots/{shot}", raw=True)[0] == 401, "team can't open screenshots")
    s, b = call("GET", f"/screenshots/{shot}", auth=ADMIN, raw=True)
    check(s == 200 and b[1:4] == b"PNG", "admin can open screenshots")
    check(call("GET", "/screenshots/..%2Fconfig.json", auth=ADMIN)[0] == 404, "no path tricks")

    print("deadline")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import report as rp
    check(rp.deadline("2026-10-16").isoformat() == "2026-10-17T03:00:00+00:00", "Fri 11 PM EDT = 03:00 UTC Sat")
    check(rp.deadline("2026-11-06").isoformat() == "2026-11-07T04:00:00+00:00", "Fri 11 PM EST = 04:00 UTC Sat (after DST ends)")
    check(rp.eastern("2026-10-17T13:14:00+00:00") == "Sat, Oct 17, 9:14 AM ET", "times shown in Eastern")
    s, txt0 = call("GET", f"/report.txt?week={w0}", auth=ADMIN, raw=True)
    txt0 = txt0.decode()
    check("missed the deadline: Avery Test" in txt0 and "Blake Test (sent" in txt0, "last week's sends (made today) are called out as late")
    s, t1 = call("GET", f"/report.txt?week={w1}", auth=ADMIN, raw=True)
    check("missed the deadline" not in t1.decode(), "this week's sends are on time")

    print("report")
    s, html = call("GET", f"/report?week={w1}", auth=ADMIN, raw=True)
    html = html.decode()
    check(s == 200, "report renders")
    check(html.index(">Avery Test<") < html.index(">Blake Test<") < html.index(">Casey Test<"), "digest is by person")
    check("Blocked" not in html and "At risk" not in html and "Status" not in html, "no status anywhere")
    check(html.count(">New this week<") == 2, "new this week marked (Board Deck, Spif); not Commission Audit Tool or Territory Map from last week")
    check("Where Claude could have helped" not in html, "no retro section in the report")
    check("Could Claude help? Maybe — Drafting the approval matrix" in html and "Non-Claude projects Claude could help with" in html
          and "ignored for Claude" not in html, "Could Claude help shown on the project and in Claude impact")
    check("could have helped" not in html.lower(), "no 'Claude could have helped' anywhere in the report")
    check("⏳ 2–4 weeks left" in html, "time left shown on each project")
    check("👤 Solo" in html and "👥 With Blake Test, Finance team" in html, "solo / with whom shown (duplicates dropped)")
    check('id="ideas"' in html and "Auto-build QBR decks" in html and "Saves about 2 hrs per QBR" in html and "Renewal alerts" in html, "ideas section, blank idea dropped")
    check("Need a greenlight" in html and "Just an idea" in html, "ideas show where they stand")
    s, t1 = call("GET", f"/report.txt?week={w1}", auth=ADMIN, raw=True)
    check("(2–4 weeks left)" in t1.decode() and "Avery Test: Auto-build QBR decks [Need a greenlight] — Saves about 2 hrs per QBR" in t1.decode(), "Slack text has time left and ideas")
    check('id="glance"' in html and html.index('id="glance"') < html.index('id="digest"'), "At a glance comes before the full breakdown")
    check("Gone Test" not in html and "Everyone is in" in html, "everyone in; inactive people not counted")
    check("+$20" in html, "Blake weekly spend change (120 - 100)")
    check("⚑" in html, "over-80% flag shown (Avery 86%)")
    check("+$130" in html, "weekly spend change (430 − 300)")
    s, txt = call("GET", f"/report.txt?week={w1}", auth=ADMIN, raw=True)
    txt = txt.decode()
    check("~2 hrs (+2 hrs vs last week)" in txt, "Claude hours come from other work only now: Support Hub 1-3 hrs = 2; projects no longer ask")
    check("Other work:* Support Hub (1)" in txt and "hrs ·" not in txt.split("Other work:*")[1].split("\n")[0], "other work listed without hours")
    check("*Avery Test:* Commission Audit Tool (2–4 weeks left), Spif/Adjustment Process Approval Rehaul (2–4 weeks left) · other work: Support Hub" in txt, "one line per person in the summary")
    check('id="digest"' in html and "Answered rep questions" in html and "Support Hub (other work)" in html, "other work in the report and in Claude impact")
    check(html.count('class="lh next">Next steps') >= 5, "next steps shown per update")
    print("    ", txt.replace("\n", "\n     "))
    (OUT / "report.html").write_text(html, encoding="utf-8")

    print("csv")
    s, b = call("GET", "/export/updates.csv", auth=ADMIN, raw=True)
    check(s == 200 and b.startswith("﻿".encode()) and b"Board Deck Automation" in b and "Time left".encode() in b, "updates CSV, with time left")
    s, b = call("GET", "/export/tasks.csv", auth=ADMIN, raw=True)
    check(s == 200 and b"Answered rep questions" in b, "other work CSV")
    s, b = call("GET", f"/export/usage.csv?week={w1}", auth=ADMIN, raw=True)
    import csv as _csv
    rows = list(_csv.reader(io.StringIO(b.decode("utf-8-sig"))))
    check(s == 200 and len(rows) == 5, "usage CSV filtered to a week (header + 4)")
    check(any("• Auto-build QBR decks [Need a greenlight] — Saves about 2 hrs per QBR\n• Renewal alerts [Just an idea]" in c for r in rows for c in r), "usage CSV has the ideas")

    print("drafts")
    w8 = (fri + timedelta(days=21)).isoformat()
    form = {"projects": [{"project": "Draft project", "type": "Claude", "progress": ["Half done"], "nextSteps": []}], "tasks": [], "retro": "", "spend": "12", "budget": "", "inc": None}
    check(call("PUT", "/api/draft", {"name": "Nobody", "week": w8, "form": form})[0] == 400, "draft needs a roster name")
    check(call("PUT", "/api/draft", {"name": "Drew Test", "week": w8, "form": form}, auth=None)[0] == 401, "draft needs sign-in")
    s, d = call("PUT", "/api/draft", {"name": "Drew Test", "week": w8, "form": form})
    check(s == 200 and d["draft"]["savedAt"], "team saves a half-done draft (no checks)")
    mine = call("GET", f"/api/mine?name=Drew%20Test&week={w8}")[1]
    check(mine["draft"]["form"]["projects"][0]["project"] == "Draft project" and not mine["updates"] and mine["usage"] is None, "draft comes back, nothing sent")
    s, txt = call("GET", f"/report.txt?week={w8}", auth=ADMIN, raw=True)
    check(b"Draft project" not in txt, "drafts never reach the report")
    check(call("GET", f"/api/mine?name=Casey%20Test&week={w8}")[1]["draft"] is None, "drafts are per person")
    check(call("DELETE", f"/api/draft?name=Drew%20Test&week={w8}")[0] == 200 and call("GET", f"/api/mine?name=Drew%20Test&week={w8}")[1]["draft"] is None, "discard a draft")
    call("PUT", "/api/draft", {"name": "Drew Test", "week": w8, "form": form})
    s, d = send("Drew Test", w8, [proj("Draft project", "Claude", "", ["Done"])], use)
    check(s == 200 and call("GET", f"/api/mine?name=Drew%20Test&week={w8}")[1]["draft"] is None, "sending clears the draft")
    call("PUT", "/api/draft", {"name": "Drew Test", "week": w8, "form": form})
    call("DELETE", f"/api/admin/send?name=Drew%20Test&week={w8}", auth=ADMIN)
    check(call("GET", f"/api/mine?name=Drew%20Test&week={w8}")[1]["draft"] is None, "deleting a send clears its draft too")

    print("delete a send")
    w9 = (fri + timedelta(days=14)).isoformat()
    s, d = send("Casey Test", w9, [proj("Test project", "Claude", "", ["Test"])], use)
    check(s == 200, "a test send for a later week")
    tshot = d["usage"]["screenshot"]
    sends = call("GET", "/api/admin", auth=ADMIN)[1]["sends"]
    check(sends[0]["week"] == w9 and sends[0]["name"] == "Casey Test" and sends[0]["projects"] == ["Test project"], "admin lists sends, newest week first")
    check(len({(x["week"], x["name"]) for x in sends}) == len(sends) and any(x["week"] == w1 and x["tasks"] for x in sends), "one row per person per week, with other work counted")
    check(call("DELETE", f"/api/admin/send?name=Casey%20Test&week={w9}")[0] == 401, "team can't delete")
    s, d = call("DELETE", f"/api/admin/send?name=Casey%20Test&week={w9}", auth=ADMIN)
    check(s == 200 and d["deleted"] == {"updates": 1, "tasks": 0, "usage": 1}, "admin deletes the whole send")
    mine = call("GET", f"/api/mine?name=Casey%20Test&week={w9}")[1]
    check(not mine["updates"] and mine["usage"] is None, "...and it's gone from the form")
    check(not (OUT / "state" / "screenshots" / tshot).exists(), "...and its screenshot file")
    check(call("DELETE", f"/api/admin/send?name=Casey%20Test&week={w9}", auth=ADMIN)[0] == 404, "deleting again: nothing there")
    check(call("GET", f"/api/mine?name=Casey%20Test&week={w1}")[1]["updates"], "other weeks untouched")

    print("persistence")
    files = sorted(p.name for p in (OUT / "state").iterdir())
    check(files == ["changes.jsonl", "config.json", "drafts.json", "screenshots", "sent.json", "tasks.json", "updates.json", "usage.json"], f"state files: {files}")


if __name__ == "__main__":
    main()
