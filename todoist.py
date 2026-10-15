"""Sends a person's projects to their own Todoist when they submit.

Each project is one task in their Todoist Inbox, with its next steps as
subtasks. The same project is reused week to week (its task id is kept),
so nothing is duplicated: a step already open under it isn't added again,
and a project whose task was completed or deleted gets a fresh one.

Uses Todoist's API v1 with the person's own API token (Todoist Settings ->
Integrations -> Developer). TODOIST_API overrides the address, for tests.
"""

import json
import os
import urllib.error
import urllib.request

API = os.environ.get("TODOIST_API", "https://api.todoist.com/api/v1").rstrip("/")
TIMEOUT = 10


class TodoistError(Exception):
    pass


def _call(token: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (401, 403):
            raise TodoistError("Todoist didn't accept the API token. Reconnect Todoist with a fresh token.")
        raise TodoistError(f"Todoist returned an error ({e.code}).")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise TodoistError("Couldn't reach Todoist.")


def _results(page):
    """API v1 lists come back as {"results": [...], "next_cursor": ...}."""
    if isinstance(page, dict):
        return page.get("results") or []
    return page or []


def check_token(token: str) -> str | None:
    """None if the token works, otherwise what's wrong."""
    try:
        _call(token, "GET", "/projects?limit=1")
    except TodoistError as e:
        return str(e)
    return None


def _open_subtasks(token: str, parent_id: str) -> list:
    out, cursor = [], None
    for _ in range(20):
        page = _call(token, "GET", f"/tasks?parent_id={parent_id}&limit=200" + (f"&cursor={cursor}" if cursor else ""))
        out += _results(page)
        cursor = page.get("next_cursor") if isinstance(page, dict) else None
        if not cursor:
            break
    return out


def describe(u: dict) -> str:
    bits = [f"{u['type']} project", u.get("timeLeft", "")]
    if u.get("solo") is False and u.get("with"):
        bits.append("With " + ", ".join(u["with"]))
    return "From Weekly Progress · " + " · ".join(b for b in bits if b)


def send(token: str, projects: list, known: dict) -> dict:
    """Creates or reuses one task per project and adds new next steps under it.

    known maps a lower-cased project name to its Todoist task id; it's
    updated in place. Returns {"projects": n, "steps": n} or raises TodoistError.
    """
    done = {"projects": 0, "steps": 0}
    for u in projects:
        key = u["project"].lower()
        task = _call(token, "GET", f"/tasks/{known[key]}") if known.get(key) else None
        if not task or task.get("checked") or task.get("is_deleted"):
            task = _call(token, "POST", "/tasks", {"content": u["project"], "description": describe(u)})
            if not task or not task.get("id"):
                raise TodoistError("Todoist didn't create the task.")
            known[key] = task["id"]
        else:
            _call(token, "POST", f"/tasks/{task['id']}", {"description": describe(u)})
        have = {(t.get("content") or "").strip().lower() for t in _open_subtasks(token, task["id"]) if not t.get("checked")}
        for step in u.get("nextSteps", []):
            if step.strip().lower() not in have:
                _call(token, "POST", "/tasks", {"content": step, "parent_id": task["id"]})
                have.add(step.strip().lower())
                done["steps"] += 1
        done["projects"] += 1
    return done
