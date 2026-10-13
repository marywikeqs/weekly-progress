# Weekly Progress

The team's weekly check-in, and the CRO's report built from it.

- **The weekly form** (`/`): everything in one send. Name and week ending (a date picker that only lands on Fridays), then:
  - **Projects:** one card each. Project (anything reported before, or a new name), Claude / Non-Claude, *Progress this week* and *Next steps* (1–3 bullets each), time saved with Claude for Claude projects, and what Claude could have helped with for non-Claude ones.
  - **Other work** (optional): one line each for recurring work like Support Hub, Deal Desk or ad hoc analysis. Kind of work, a short summary, and whether Claude was used (and the time it saved). No hours are asked for or shown. The kinds are edited on the admin page, and anything typed in stays pickable.
  - **Claude usage:** spend so far this month, current monthly budget, any budget increase request (amount, reason, status) and a required usage screenshot.

  Nothing saves unless all of it is valid. A new week starts from last week's unfinished projects, other work and budget, and sending again in the same week replaces that week's update.
- **Weekly report** (`/report`): a weekly digest: participation, then each person's week (their projects with progress and next steps, then their other work), Claude time saved (range midpoints: None 0, Under 1 hr 0.5, 1–3 hrs 2, 3–5 hrs 4, 5+ hrs 6), Claude-could-help ideas, spend vs budget (over 80% flagged), budget requests and the screenshots, with week-over-week comparison. It has buttons for Print / PDF and Copy Slack summary (`/report.txt`).
- **Admin** (`/admin`): roster, report email address, project settings (default type, hide), kinds of other work, screenshot instructions, a test-email button, and CSV exports of everything.
- **Report email:** sent once a week through [Resend](https://resend.com), only once everyone on the roster has sent; there is no partial or deadline send. Later edits don't resend. The admin page's test button sends a preview any time.

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
3. Variables: `RESEND_API_KEY` (turns the report email on), `APP_PASSWORD` (the team's password for the forms) and `ADMIN_PASSWORD` (report, admin, screenshots, exports). The usernames are `team` and `admin`, and you can change them with `APP_USER` and `ADMIN_USER`.
4. Settings → Networking → Generate domain.

On the volume: `config.json`, `updates.json`, `tasks.json`, `usage.json`, `sent.json`, `screenshots/` and `changes.jsonl` (every save and delete, before and after).

## Not built yet

- Posting the report to Slack, and reminders for people who haven't sent. `report.build()` and `report.render_text()` are separate from the web page so either can reuse them.
