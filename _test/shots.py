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
        c1.locator(".f-left").select_option("3–5 days")
        c1.locator(".f-solo input[value=solo]").check()
        check(c1.locator(".f-team").is_hidden(), "solo: no teammate picker")
        c1.locator(".f-b").first.fill("Cleaned stale opps")
        c1.locator(".f-n").first.fill("Close out Q4 list")
        check(c1.locator(".f-b").nth(1).is_hidden() and c1.locator(".more").first.is_visible(), "only one progress line until + Add a line")
        c1.locator(".more").first.click(); c1.locator(".f-b").nth(1).fill("Merged duplicates")
        c1.locator(".more").first.click()
        check(c1.locator(".f-b").nth(2).is_visible() and c1.locator(".more").first.is_hidden(), "third line shown, + Add a line gone at three")
        c1.locator(".f-b").nth(2).fill("Third line")
        c1.locator(".bullet").nth(1).locator(".x-line").click()     # remove line 2: line 3 moves up
        check(c1.locator(".f-b").nth(1).input_value() == "Third line" and c1.locator(".f-b").nth(2).is_hidden()
              and c1.locator(".more").first.is_visible(), "x removes an extra line, the rest move up")
        check(c1.locator(".bullet").first.locator(".x-line").count() == 0, "the first line can't be removed")
        c1.locator(".f-b").nth(1).fill("Merged duplicates")
        c1.locator(".done-btn").click()
        check(c1.locator(".proj-body").is_hidden() and "Pipeline Hygiene" in c1.locator(".proj-sum").inner_text()
              and "2 progress lines" in c1.locator(".proj-sum").inner_text(), "Done folds a project to a summary")
        c1.locator(".f-toggle").click()
        check(c1.locator(".f-b").first.is_visible(), "Edit opens it again")
        pg.click("#add")
        check(c1.locator(".proj-body").is_hidden(), "adding a project folds the finished one")
        c2 = pg.locator(".proj").nth(1)
        c2.locator(".f-pick").select_option("Board Deck Automation")
        check(c2.locator(".f-type").input_value() == "Claude", "picking a known project fills its type")
        c2.locator(".f-type").select_option("Non-Claude")
        c2.locator(".f-left").select_option("Ongoing / no end date")
        c2.locator(".f-solo input[value=team]").check()
        check(c2.locator(".f-team").is_visible() and c2.locator(".chips input[value='Drew Test']").count() == 0, "with others: roster to pick from, minus yourself")
        c2.locator(".chips input[value='Casey Test']").check(); c2.locator(".f-others").fill("Finance team")
        c2.locator(".f-b").first.fill("Waiting on data")
        c2.locator(".f-n").first.fill("Chase finance")
        pg.click("#add-task")
        tk = pg.locator(".task").last
        tk.locator(".t-cat").select_option("Deal Desk")
        tk.locator(".t-claude").select_option("yes")
        check(tk.locator(".t-saved").is_visible(), "Used Claude shows time saved")
        tk.locator(".t-saved").select_option("Under 1 hr"); tk.locator(".t-note").fill("Two deal reviews")
        pg.click("#add-idea")
        pg.locator(".idea .i-idea").last.fill("Auto-build QBR decks"); pg.locator(".idea .i-why").last.fill("Saves 2 hrs per QBR")
        pg.locator(".idea .i-status input[value=start]").last.check()
        pg.locator(".idea .i-status input[value=discuss]").last.check()
        check(pg.locator(".idea").last.locator(".i-status input:checked").count() == 1, "an idea can only have one of the three choices")
        pg.click("#add-idea")   # a second, left blank: skipped on send
        pg.click("#add")   # a third, left blank: skipped on send
        check(pg.locator("#retro").count() == 0, "no Looking back section")
        pg.fill("#spend", "75"); pg.fill("#budget", "250"); pg.check("input[name=inc][value=no]")
        # draft: autosaves, survives a reload, nothing sent
        pg.wait_for_timeout(3600)
        check("Draft saved" in (pg.text_content("#msg") or ""), "autosaves a draft: " + (pg.text_content("#msg") or ""))
        pg.reload(); pg.wait_for_load_state("networkidle")
        pg.fill("#week", nxt); pg.dispatch_event("#week", "change"); pg.wait_for_timeout(600)
        check(pg.input_value("#name") == "Drew Test", "name remembered")
        check("Picked up your draft" in (pg.text_content("#state") or ""), "draft noted after reload")
        check(pg.locator(".proj").count() == 3 and pg.locator(".proj").nth(0).locator(".f-new").input_value() == "Pipeline Hygiene"
              and pg.locator(".proj").nth(0).locator(".f-b").nth(1).input_value() == "Merged duplicates", "draft restores the projects")
        check(pg.locator("#tasks .task").count() == 1 and pg.locator(".task .t-saved").input_value() == "Under 1 hr", "draft restores other work")
        check(pg.locator(".proj").nth(0).locator(".f-left").input_value() == "3–5 days", "draft restores time left")
        check(pg.locator(".proj").nth(0).locator(".proj-body").is_hidden() and pg.locator(".proj").nth(2).locator(".proj-body").is_visible(),
              "draft: finished projects come back folded, the blank one open")
        check(pg.locator(".proj").nth(1).locator(".chips input[value='Casey Test']").is_checked()
              and pg.locator(".proj").nth(1).locator(".f-others").input_value() == "Finance team", "draft restores who's working on it")
        check(pg.locator(".idea").count() == 2 and pg.locator(".idea .i-idea").first.input_value() == "Auto-build QBR decks"
              and pg.locator(".idea .i-status input[value=discuss]").first.is_checked(), "draft restores ideas")
        check(pg.input_value("#spend") == "75" and pg.is_checked("input[name=inc][value=no]"), "draft restores usage")
        check("screenshot" in (pg.text_content("#shot-state") or ""), "says to attach the screenshot")
        pg.screenshot(path=str(OUT / "flow-draft.png"), full_page=True)
        pg.set_input_files("#shot", str(OUT / "form-phone-light.png"))
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(OUT / "flow-filled.png"), full_page=True)
        pg.click("#send"); pg.wait_for_timeout(1200)
        check(pg.is_visible("#done"), "send shows the done screen: " + (pg.text_content("#msg") or ""))
        pg.screenshot(path=str(OUT / "flow-done.png"), full_page=True)
        pg.click("#edit"); pg.wait_for_timeout(500)
        check(pg.locator(".proj").count() == 2, "Make a change reloads both projects")
        check(pg.locator("#tasks .task").count() >= 1, "Make a change reloads other work")
        check(pg.locator(".idea").count() == 1 and pg.locator(".proj").nth(1).locator(".f-left").input_value() == "Ongoing / no end date",
              "Make a change reloads ideas (blank one dropped) and time left")
        after = (_d.fromisoformat(nxt) + _t(days=7)).isoformat()      # the week after: starts from Drew's projects
        pg.fill("#week", after); pg.dispatch_event("#week", "change"); pg.wait_for_timeout(600)
        check(pg.locator(".proj").count() == 2 and pg.locator(".proj").nth(0).locator(".f-left").input_value() == "3–5 days"
              and pg.locator(".proj").nth(0).locator(".f-b").first.input_value() == "", "a new week carries over time left, not progress")
        check(pg.locator(".proj").nth(1).locator(".f-team").is_visible() and pg.locator(".proj").nth(1).locator(".chips input[value='Casey Test']").is_checked(),
              "a new week carries over who's working on it")
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
