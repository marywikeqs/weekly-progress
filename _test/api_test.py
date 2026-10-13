"""
End-to-end check of the API against a throwaway server.

    python _test/api_test.py

Starts server.py on port 8091 with passwords set and a temp STATE_DIR, fills
two weeks of made-up submissions, and checks auth, validation, upserts, the
report and the CSVs. Leaves the server's state in _test/out/state so the
report can be looked at afterwards (python _test/api_test.py --keep serves it).
"""

import base64
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

    def send(name, week, projects, usage, tasks=None):
        return call("POST", "/api/submit", {"name": name, "week": week, "projects": projects, "usage": usage, "tasks": tasks or []})

    def proj(project, type_, _old_status, progress, saved=None, help_="", steps=None):
        return {"project": project, "type": type_, "progress": progress, "nextSteps": steps or ["Keep going"],
                "timeSaved": saved, "couldHelp": help_}

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
    check(send("Avery Test", w1, [{**cat, "timeSaved": ""}], use)[0] == 400, "Claude project needs time saved")
    check(send("Avery Test", w1, [{**cat, "progress": ["", " "]}], use)[0] == 400, "needs a progress line")
    check(send("Avery Test", w1, [{**cat, "nextSteps": [" "]}], use)[0] == 400, "needs a next step")
    s, d = send("Avery Test", w1, [cat], use, [hub, {**hub, "claude": None}])
    check(s == 400 and d["errors"][0].get("task") == 1, "a bad other-work line is pointed at by index")
    check(send("Avery Test", w1, [cat], use, [{**hub, "timeSaved": None}])[0] == 400, "Claude used on other work needs time saved")
    s, d = send("Nobody", w1, [cat], use)
    check(s == 400 and d["error"] == "Pick your name." and d["errors"] == [], "name must be on the roster (said once)")
    check(send("Avery Test", (fri - timedelta(days=1)).isoformat(), [cat], use)[0] == 400, "week must be a Friday")
    s, d = send("Avery Test", w1, [cat], {**use, "screenshot": None})
    check(s == 400 and d["errors"][0].get("usage"), "screenshot required the first time")
    check(send("Avery Test", w1, [cat], {**use, "screenshot": "data:image/png;base64,aGVsbG8="})[0] == 400, "non-image refused")
    s, d = send("Avery Test", w1, [cat], {**use, "increase": {**use["increase"], "reason": ""}})
    check(s == 400 and d["errors"][0].get("usage"), "increase needs a reason")

    print("one send: saving")
    s, d = send("Avery Test", w1, [cat, spif], use, [hub, {"category": "Pricing desk", "timeSpent": None, "claude": False, "note": ""}])
    check(s == 200 and not d["resent"] and len(d["updates"]) == 2 and len(d["tasks"]) == 2, "two projects + other work + usage saved together")
    check(d["tasks"][0]["category"] == "Support Hub", "other-work category matched to the list's spelling")
    check(d["updates"][0]["nextSteps"] == ["Keep going"], "next steps saved")
    check("Pricing desk" in call("GET", "/api/config")[1]["taskCategories"], "a typed kind of work stays pickable")
    a_cat = d["updates"][0]
    check(a_cat["progress"] == cat["progress"][:2] + ["extra"], "blank bullets dropped, max 3 kept")
    check(a_cat["couldHelp"] == "" and d["updates"][1]["timeSaved"] is None, "conditional fields kept only for their type")
    check(d["usage"]["spend"] == 412.5, "$ and commas parsed")
    shot = d["usage"]["screenshot"]
    s, d = send("Avery Test", w1, [{**cat, "project": "commission audit tool"}],
                {**use, "spend": 430, "screenshot": None, "increase": {**use["increase"], "status": "Approved"}})
    check(s == 200 and d["resent"], "resend accepted")
    check(d["updates"][0]["id"] == a_cat["id"] and d["updates"][0]["project"] == "Commission Audit Tool", "resend keeps the record, project name canonical")
    check(len(call("GET", f"/api/mine?name=Avery%20Test&week={w1}")[1]["updates"]) == 1, "a project left off the resend is removed")
    check(d["usage"]["screenshot"] == shot and d["usage"]["spend"] == 430, "resend without a screenshot keeps the one on file")
    check(call("GET", f"/api/mine?name=Avery%20Test&week={w1}")[1]["tasks"] == [], "other work left off the resend is removed")
    send("Avery Test", w1, [cat, spif], {**use, "spend": 430, "screenshot": None, "increase": {**use["increase"], "status": "Approved"}}, [hub])

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

    print("report")
    s, html = call("GET", f"/report?week={w1}", auth=ADMIN, raw=True)
    html = html.decode()
    check(s == 200, "report renders")
    check(html.index(">Avery Test<") < html.index(">Blake Test<") < html.index(">Casey Test<"), "digest is by person")
    check("Blocked" not in html and "At risk" not in html and "Status" not in html, "no status anywhere")
    check(html.count(">new this week<") == 2, "new this week marked (Board Deck, Spif); not Commission Audit Tool or Territory Map from last week")
    check("Drafting the approval matrix" in html, "could-help suggestion shown")
    check("Gone Test" not in html and "Everyone is in" in html, "everyone in; inactive people not counted")
    check("+$20" in html, "Blake weekly spend change (120 - 100)")
    check("⚑" in html, "over-80% flag shown (Avery 86%)")
    check("+$130" in html, "weekly spend change (430 − 300)")
    s, txt = call("GET", f"/report.txt?week={w1}", auth=ADMIN, raw=True)
    txt = txt.decode()
    check("~14 hrs (+11.5 hrs vs last week)" in txt, "hours at midpoints: projects 4 + 2 + 6, Support Hub 2 = 14; last week 2 + 0.5")
    check("Other work:* ~4 hrs" in txt, "other work time in the summary")
    check("*Avery Test:* Commission Audit Tool, Spif/Adjustment Process Approval Rehaul · other work: Support Hub" in txt, "one line per person in the summary")
    check('id="digest"' in html and "Answered rep questions" in html and "Support Hub (other work)" in html, "other work in the report and in Claude impact")
    check(html.count('class="lh">Next steps') >= 5, "next steps shown per update")
    print("    ", txt.replace("\n", "\n     "))
    (OUT / "report.html").write_text(html, encoding="utf-8")

    print("csv")
    s, b = call("GET", "/export/updates.csv", auth=ADMIN, raw=True)
    check(s == 200 and b.startswith("﻿".encode()) and b"Board Deck Automation" in b, "updates CSV")
    s, b = call("GET", "/export/tasks.csv", auth=ADMIN, raw=True)
    check(s == 200 and b"Answered rep questions" in b, "other work CSV")
    s, b = call("GET", f"/export/usage.csv?week={w1}", auth=ADMIN, raw=True)
    check(s == 200 and b.count(b"\n") == 5, "usage CSV filtered to a week (header + 4)")

    print("persistence")
    files = sorted(p.name for p in (OUT / "state").iterdir())
    check(files == ["changes.jsonl", "config.json", "screenshots", "sent.json", "tasks.json", "updates.json", "usage.json"], f"state files: {files}")


if __name__ == "__main__":
    main()
