"""
Screenshots of every page in real Chrome, desktop and phone width, light and
dark. Run after api_test.py (it serves that test's state):

    python _test/shots.py

Also drives the project form once (pick Other, conditional fields) and fails
on any console error or horizontal page scroll.
"""

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_test" / "out"
PORT = 8092
BASE = f"http://127.0.0.1:{PORT}"
problems = []

env = {**os.environ, "PORT": str(PORT), "STATE_DIR": str(OUT / "state"), "APP_PASSWORD": "t-pass", "ADMIN_PASSWORD": "a-pass"}
srv = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT, stderr=subprocess.DEVNULL)
for _ in range(50):
    try:
        urllib.request.urlopen(BASE + "/healthz"); break
    except Exception:
        time.sleep(0.1)

try:
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        for scheme in ("light", "dark"):
            for label, vp in (("desk", {"width": 1280, "height": 900}), ("phone", {"width": 390, "height": 844})):
                ctx = b.new_context(viewport=vp, color_scheme=scheme, http_credentials={"username": "admin", "password": "a-pass"})
                pg = ctx.new_page()
                pg.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
                pg.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
                for name, path in (("form", "/"), ("report", "/report"), ("admin", "/admin")):
                    pg.goto(BASE + path)
                    pg.wait_for_load_state("networkidle")
                    if name == "form":
                        pg.select_option("#name", "Avery Test"); pg.wait_for_timeout(400)
                        assert pg.locator(".proj").count() == 2, "Avery's sent week loads as 2 cards"
                    sw = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    if sw > 0:
                        problems.append(f"{name} {label} {scheme}: scrolls sideways by {sw}px")
                    pg.screenshot(path=str(OUT / f"{name}-{label}-{scheme}.png"), full_page=True)
                ctx.close()
        # the whole flow once, as Drew (nothing sent yet)
        ctx = b.new_context(viewport={"width": 390, "height": 844}, http_credentials={"username": "team", "password": "t-pass"})
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        pg.goto(BASE + "/"); pg.wait_for_load_state("networkidle")
        check = lambda cond, label: None if cond else problems.append("flow: " + label)
        check(pg.get_attribute("#week", "step") == "7", "date picker steps by week")
        wk = pg.input_value("#week")
        from datetime import date as _d, timedelta as _t
        wed = (_d.fromisoformat(wk) - _t(days=9)).isoformat()      # Wednesday of last week
        pg.fill("#week", wed); pg.dispatch_event("#week", "change"); pg.wait_for_timeout(200)
        check(pg.input_value("#week") == (_d.fromisoformat(wk) - _t(days=7)).isoformat(), "a Wednesday snaps to that week's Friday")
        check(pg.is_visible("#week-note"), "snap explained")
        nxt = (_d.fromisoformat(wk) + _t(days=7)).isoformat()     # next week: Drew hasn't sent it
        pg.select_option("#name", "Drew Test"); pg.wait_for_timeout(300)
        pg.fill("#week", nxt); pg.dispatch_event("#week", "change"); pg.wait_for_timeout(400)
        check(pg.locator(".proj").count() == 1, "one blank card to start")
        pg.click("#send"); pg.wait_for_timeout(300)
        check(pg.locator(".invalid").count() >= 5, "empty send marks fields in red")
        c1 = pg.locator(".proj").nth(0)
        c1.locator(".f-pick").select_option("__new"); c1.locator(".f-new").fill("Pipeline Hygiene")
        c1.locator(".f-type").select_option("Claude")
        c1.locator(".f-b").first.fill("Cleaned stale opps")
        c1.locator(".f-n").first.fill("Close out Q4 list")
        check(c1.locator(".f-b").nth(1).is_hidden() and c1.locator(".more").first.is_visible(), "only one progress line until + Add a line")
        c1.locator(".more").first.click(); c1.locator(".f-b").nth(1).fill("Merged duplicates")
        c1.locator(".more").first.click()
        check(c1.locator(".f-b").nth(2).is_visible() and c1.locator(".more").first.is_hidden(), "third line shown, + Add a line gone at three")
        pg.click("#add")
        c2 = pg.locator(".proj").nth(1)
        c2.locator(".f-pick").select_option("Board Deck Automation")
        check(c2.locator(".f-type").input_value() == "Claude", "picking a known project fills its type")
        c2.locator(".f-type").select_option("Non-Claude")
        check(c2.locator(".f-help").is_visible() and not c1.locator(".f-help").is_visible(), "non-Claude shows the could-help box")
        c2.locator(".f-b").first.fill("Waiting on data")
        c2.locator(".f-n").first.fill("Chase finance")
        pg.click("#add-task")
        tk = pg.locator(".task").last
        tk.locator(".t-cat").select_option("Deal Desk"); tk.locator(".t-spent").select_option("1-3 hrs")
        tk.locator(".t-claude").select_option("yes")
        check(tk.locator(".t-saved").is_visible(), "Used Claude shows time saved")
        tk.locator(".t-saved").select_option("Under 1 hr"); tk.locator(".t-note").fill("Two deal reviews")
        pg.click("#add")   # a third, left blank: skipped on send
        pg.fill("#spend", "75"); pg.fill("#budget", "250"); pg.check("input[name=inc][value=no]")
        pg.set_input_files("#shot", str(OUT / "form-phone-light.png"))
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(OUT / "flow-filled.png"), full_page=True)
        pg.click("#send"); pg.wait_for_timeout(1200)
        check(pg.is_visible("#done"), "send shows the done screen: " + (pg.text_content("#msg") or ""))
        pg.screenshot(path=str(OUT / "flow-done.png"), full_page=True)
        pg.click("#edit"); pg.wait_for_timeout(500)
        check(pg.locator(".proj").count() == 2, "Make a change reloads both projects")
        check(pg.locator(".task").count() >= 1, "Make a change reloads other work")
        ctx.close()

        # print layout of the report
        ctx = b.new_context(http_credentials={"username": "admin", "password": "a-pass"})
        pg = ctx.new_page(); pg.goto(BASE + "/report"); pg.wait_for_load_state("networkidle")
        pg.pdf(path=str(OUT / "report.pdf"), format="Letter", margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"})
        b.close()
finally:
    srv.terminate()

print("\n".join(problems) or "no console errors, no sideways scroll")
print("screenshots in", OUT)
