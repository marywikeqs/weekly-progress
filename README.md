# Weekly Progress

The team's weekly check-in, and the CRO's report built from it.

- **The weekly form** (`/`): everything in one send. Name, week ending (a date picker that only lands on Fridays), then a card per project: project (picked from anything reported before, or a new name), Claude / Non-Claude, status, 1–3 bullets of progress and next steps, time saved with Claude for Claude projects, and what Claude could have helped with for non-Claude ones. Below that is Claude usage: spend so far this month, current monthly budget, any budget increase request (amount, reason, status) and a required usage screenshot. Nothing saves unless all of it is valid. A new week starts from last week's unfinished projects and budget, and sending again in the same week replaces that week's update.
- **Weekly report** (`/report`): participation, projects grouped by name (Blocked and At risk first), Claude time saved (range midpoints: None 0, Under 1 hr 0.5, 1–3 hrs 2, 3–5 hrs 4, 5+ hrs 6), Claude-could-help ideas, spend vs budget (over 80% flagged), budget requests and the screenshots, with week-over-week comparison. It has buttons for Print / PDF and Copy Slack summary (`/report.txt`).
- **Admin** (`/admin`): roster, project list (with default type), screenshot instructions, projects people typed under Other, and CSV exports of everything.

## Running it

Standard library Python only.

```
python server.py                 # http://localhost:8080, no passwords locally
python _test/api_test.py         # end-to-end API checks (made-up data)
python _test/shots.py            # Chrome screenshots of every page (needs playwright)
```

## Railway

Same setup as the RevOps Answer Book:

1. New Project → Deploy from GitHub repo → this repo (it picks up `railway.json` and the Dockerfile).
2. Attach a **volume** to the service. Submissions, screenshots and the roster live there. Without one, every deploy wipes them; `/healthz` shows `"persistent": false`.
3. Variables: `APP_PASSWORD` (the team's password for the forms) and `ADMIN_PASSWORD` (report, admin, screenshots, exports). The usernames are `team` and `admin`, and you can change them with `APP_USER` and `ADMIN_USER`.
4. Settings → Networking → Generate domain.

On the volume: `config.json`, `updates.json`, `usage.json`, `screenshots/` and `changes.jsonl` (every save and delete, before and after).

## Not built yet

- Sending the report to Slack or email, and reminders for people who haven't submitted. `report.build()` and `report.render_text()` are separate from the web page so a scheduled send can reuse them.
