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
import zlib
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_test" / "out"
PORT = 8091
BASE = f"http://127.0.0.1:{PORT}"
TEAM = ("team", "t-pass")
ADMIN = ("admin", "a-pass")
fails = 0


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
    env = {**os.environ, "PORT": str(PORT), "STATE_DIR": str(OUT / "state"),
           "APP_PASSWORD": TEAM[1], "ADMIN_PASSWORD": ADMIN[1]}
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
           "screenshotInstructions": "Settings → Usage, 1st of month to today."}
    s, d = call("PUT", "/api/admin/config", cfg, auth=ADMIN)
    check(s == 200 and len(d["config"]["projects"]) == 3, "config saved; a project literally named Other is dropped")
    check(call("PUT", "/api/admin/config", {"roster": []}, auth=ADMIN)[0] == 400, "empty roster refused")
    s, d = call("GET", "/api/config")
    check(d["roster"] == people, "inactive people left off the form")

    print("project updates")
    base = {"name": "Avery Test", "week": w1, "project": "Commission Audit Tool", "type": "Claude", "status": "On track",
            "progress": ["Enhancements to bring Commission Plan Builder base pieces into CIQ.", "Keep testing and tweaking.", "", "extra"],
            "timeSaved": "3-5 hrs", "couldHelp": "ignored for Claude"}
    s, d = call("POST", "/api/updates", base)
    check(s == 201 and d["update"]["progress"] == base["progress"][:2] + ["extra"], "saved; blank bullets dropped, max 3 kept")
    check(d["update"]["couldHelp"] == "", "could-help ignored on a Claude project")
    first_id = d["update"]["id"]
    s, d = call("POST", "/api/updates", {**base, "status": "At risk"})
    check(s == 200 and d["replaced"] and d["update"]["id"] == first_id, "same project same week replaces, not duplicates")
    check(call("POST", "/api/updates", {**base, "timeSaved": ""})[0] == 400, "Claude project needs time saved")
    check(call("POST", "/api/updates", {**base, "progress": ["", " "]})[0] == 400, "needs a progress line")
    check(call("POST", "/api/updates", {**base, "name": "Nobody"})[0] == 400, "name must be on the roster")
    bad_week = (fri - timedelta(days=1)).isoformat()
    check(call("POST", "/api/updates", {**base, "week": bad_week})[0] == 400, "week must be a Friday")
    s, d = call("POST", "/api/updates", {**base, "project": "commission audit tool"})
    check(d.get("update", {}).get("project") == "Commission Audit Tool", "known project matched case-insensitively")

    rows = [
        ("Avery Test", "Spif/Adjustment Process Approval Rehaul", "Non-Claude", "In progress",
         ["Plan what's working and what isn't, propose ideas, build."], None, "Drafting the approval matrix"),
        ("Blake Test", "Commission Audit Tool", "Claude", "Blocked", ["Waiting on CIQ API access."], "1-3 hrs", ""),
        ("Blake Test", "Board Deck Automation", "Claude", "On track", ["First pass at the monthly deck."], "5+ hrs", ""),
        ("Casey Test", "Territory Map", "Non-Claude", "Completed", ["Shipped to managers."], None, ""),
    ]
    for n, p, ty, st, pr, ts, ch in rows:
        s, d = call("POST", "/api/updates", {"name": n, "week": w1, "project": p, "type": ty, "status": st,
                                              "progress": pr, "timeSaved": ts, "couldHelp": ch})
        check(s == 201, f"{n}: {p}")
    check(d["update"]["type"] == "Non-Claude" and d["update"]["timeSaved"] is None, "time saved dropped on a non-Claude project")
    s, d = call("GET", f"/api/mine?name=Blake%20Test&week={w1}")
    check(len(d["updates"]) == 2, "a person sees their own week")
    other = next(u for u in d["updates"] if u["project"] == "Board Deck Automation")
    names = [p["name"] for p in call("GET", "/api/config")[1]["projects"]]
    check("Board Deck Automation" in names, "a project typed in once joins the dropdown")
    s, d = call("POST", "/api/updates", {**other, "id": None, "name": "Casey Test", "project": "board deck automation"})
    check(d["update"]["project"] == "Board Deck Automation", "later typing matches the existing name's spelling")
    call("DELETE", f"/api/updates/{d['update']['id']}")

    # edit by id onto a project they already have -> conflict
    s, _ = call("POST", "/api/updates", {**other, "project": "Commission Audit Tool", "id": other["id"]})
    check(s == 409, "editing onto an existing project of theirs is refused")
    # delete and re-add
    tmp = call("POST", "/api/updates", {"name": "Drew Test", "week": w1, "project": "Territory Map", "type": "Non-Claude",
                                         "status": "Not started", "progress": ["x"]})[1]["update"]
    check(call("DELETE", f"/api/updates/{tmp['id']}")[1]["deleted"], "delete works")

    # last week, for week-over-week
    for n, p, ty, st, ts in [("Avery Test", "Commission Audit Tool", "Claude", "In progress", "1-3 hrs"),
                             ("Blake Test", "Commission Audit Tool", "Claude", "On track", "Under 1 hr")]:
        call("POST", "/api/updates", {"name": n, "week": w0, "project": p, "type": ty, "status": st, "progress": ["Last week"], "timeSaved": ts})

    print("usage")
    u = {"name": "Avery Test", "week": w1, "spend": "$412.50", "budget": "500", "increaseRequested": False,
         "screenshot": data_url((60, 90, 220))}
    check(call("POST", "/api/usage", {**u, "screenshot": None})[0] == 400, "screenshot required")
    check(call("POST", "/api/usage", {**u, "screenshot": "data:image/png;base64,aGVsbG8="})[0] == 400, "non-image refused")
    check(call("POST", "/api/usage", {**u, "increaseRequested": True, "increase": {"amount": 800, "reason": "", "status": "Pending"}})[0] == 400,
          "increase needs a reason")
    s, d = call("POST", "/api/usage", {**u, "increaseRequested": True, "increase": {"amount": 800, "reason": "Board deck month", "status": "Pending"}})
    check(s == 201 and d["usage"]["spend"] == 412.5, "usage saved, $ and commas parsed")
    shot = d["usage"]["screenshot"]
    s, d = call("POST", "/api/usage", {**u, "spend": 430, "screenshot": None, "increaseRequested": True,
                                        "increase": {"amount": 800, "reason": "Board deck month", "status": "Approved"}})
    check(s == 200 and d["usage"]["screenshot"] == shot and d["usage"]["spend"] == 430, "resubmit keeps the screenshot on file")
    call("POST", "/api/usage", {"name": "Blake Test", "week": w1, "spend": 120, "budget": 300, "increaseRequested": False, "screenshot": data_url((220, 120, 60))})
    call("POST", "/api/usage", {"name": "Avery Test", "week": w0, "spend": 300, "budget": 500, "increaseRequested": False, "screenshot": data_url((90, 90, 90))})
    s, d = call("GET", f"/api/mine?name=Avery%20Test&week={w1}")
    check(d["lastBudget"] == 500, "last budget offered as the pre-fill")
    check(call("GET", f"/screenshots/{shot}", raw=True)[0] == 401, "team can't open screenshots")
    s, b = call("GET", f"/screenshots/{shot}", auth=ADMIN, raw=True)
    check(s == 200 and b[:4] == b"\x89PNG", "admin can open screenshots")
    check(call("GET", "/screenshots/..%2Fconfig.json", auth=ADMIN)[0] == 404, "no path tricks")

    print("report")
    s, html = call("GET", f"/report?week={w1}", auth=ADMIN, raw=True)
    html = html.decode()
    check(s == 200, "report renders")
    check(html.index("Commission Audit Tool") < html.index("Board Deck Automation"), "blocked project listed before on-track ones")
    check(html.count("first reported this week") == 3, "projects new this week marked (Board Deck, Spif, Territory Map); not Commission Audit Tool from last week")
    check("Drafting the approval matrix" in html, "could-help suggestion shown")
    check("Drew Test" in html and "Gone Test" not in html, "missing list = active roster only")
    check("⚑" in html, "over-80% flag shown (Avery 86%)")
    check("+$130" in html, "weekly spend change (430 − 300)")
    s, txt = call("GET", f"/report.txt?week={w1}", auth=ADMIN, raw=True)
    txt = txt.decode()
    check("~12 hrs (+9.5 hrs vs last week)" in txt, "hours at midpoints: 4 + 2 + 6 = 12, last week 2 + 0.5")
    print("    ", txt.replace("\n", "\n     "))
    (OUT / "report.html").write_text(html, encoding="utf-8")

    print("csv")
    s, b = call("GET", "/export/updates.csv", auth=ADMIN, raw=True)
    check(s == 200 and b.startswith("﻿".encode()) and b"Board Deck Automation" in b, "updates CSV")
    s, b = call("GET", f"/export/usage.csv?week={w1}", auth=ADMIN, raw=True)
    check(s == 200 and b.count(b"\n") == 3, "usage CSV filtered to a week (header + 2)")

    print("persistence")
    files = sorted(p.name for p in (OUT / "state").iterdir())
    check(files == ["changes.jsonl", "config.json", "screenshots", "updates.json", "usage.json"], f"state files: {files}")


if __name__ == "__main__":
    main()
