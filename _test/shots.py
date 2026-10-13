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
                for name, path in (("form", "/"), ("usage", "/?u=1#usage"), ("report", "/report"), ("admin", "/admin")):
                    pg.goto(BASE + path)
                    pg.wait_for_load_state("networkidle")
                    if name in ("form", "usage"):
                        pg.select_option("#name", "Avery Test")
                        pg.wait_for_timeout(300)
                    if name == "usage":
                        pg.check("input[name=inc][value=yes]")
                        assert pg.is_visible("#inc-reason"), "increase fields show on Yes"
                    if name == "form":
                        pg.select_option("#project", "__other")
                        assert pg.is_visible("#other"), "Other box shows"
                        pg.select_option("#ptype", "Non-Claude")
                        assert pg.is_visible("#help") and not pg.is_visible("#saved"), "non-Claude conditional"
                        pg.select_option("#ptype", "Claude")
                        assert pg.is_visible("#saved") and not pg.is_visible("#help"), "Claude conditional"
                    sw = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    if sw > 0:
                        problems.append(f"{name} {label} {scheme}: scrolls sideways by {sw}px")
                    pg.screenshot(path=str(OUT / f"{name}-{label}-{scheme}.png"), full_page=True)
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
