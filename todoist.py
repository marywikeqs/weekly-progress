"""Sends a person's projects to their own Todoist when they submit.

Each project is one task in the shared "Weekly Progress" Todoist project
(their Inbox if they're not in it yet), assigned to the person who sent it,
with its next steps as subtasks. A project is never created twice:
  * its task id is kept, and reused every week, wherever the task was moved;
  * a completed project task is reopened rather than replaced;
  * with no id on file (a deleted task, a reconnected account), an open task
    of theirs with the same name is adopted before a new one is made;
  * tasks made before the project existed are moved into it.
Next steps follow the form: new ones are added, ones already open aren't
added again, and steps this app added that drop off the list are completed.
Subtasks the person added in Todoist themselves are left alone.

Uses Todoist's API v1 with the person's own API token (Todoist Settings ->
Integrations -> Developer). TODOIST_API overrides the address, for tests.
"""

import json
import os
import urllib.error
import urllib.parse
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
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (401, 403):
            raise TodoistError("Todoist didn't accept the API token. Reconnect Todoist with a fresh token.")
        raise TodoistError(f"Todoist returned an error ({e.code}).")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise TodoistError("Couldn't reach Todoist.")


def _all(token: str, path: str, **params) -> list:
    """Every page of an API v1 list ({"results": [...], "next_cursor": ...})."""
    out, cursor = [], None
    for _ in range(50):
        q = {**params, "limit": 200, **({"cursor": cursor} if cursor else {})}
        page = _call(token, "GET", f"{path}?{urllib.parse.urlencode(q)}") or {}
        out += (page.get("results") or []) if isinstance(page, dict) else page
        cursor = page.get("next_cursor") if isinstance(page, dict) else None
        if not cursor:
            break
    return out


def _done(t: dict) -> bool:
    return bool(t.get("checked") or t.get("completed_at"))


def _norm(s: str) -> str:
    return " ".join(str(s or "").split()).lower()


def check_token(token: str) -> str | None:
    """None if the token works, otherwise what's wrong."""
    try:
        _call(token, "GET", "/projects?limit=1")
    except TodoistError as e:
        return str(e)
    return None


def describe(u: dict) -> str:
    bits = [f"{u['type']} project", u.get("timeLeft", "")]
    if u.get("solo") is False and u.get("with"):
        bits.append("With " + ", ".join(u["with"]))
    return "From Weekly Progress · " + " · ".join(b for b in bits if b)


PROJECT_NAME = "Weekly Progress"


def _where(token: str):
    """(project, my user id): the shared Weekly Progress project if this person can see it, else (None, id)."""
    me = (_call(token, "GET", "/user") or {}).get("id")
    matches = [p for p in _all(token, "/projects") if _norm(p.get("name")) == _norm(PROJECT_NAME) and not p.get("is_archived")]
    matches.sort(key=lambda p: not p.get("is_shared"))           # the shared one first
    return (matches[0] if matches else None), me


def _mine(t: dict, me) -> bool:
    who = t.get("responsible_uid") or t.get("assignee_id")
    return (who == me) if who else (t.get("added_by_uid") or t.get("creator_id")) == me


def send(token: str, projects: list, known: dict) -> dict:
    """One task per project in the Weekly Progress project, next steps as its
    subtasks, the sender as owner (assignee), nothing duplicated.

    known maps a lower-cased project name to {"id": task id, "steps": {step: subtask id}}
    (an older plain id string is accepted); it's updated in place.
    Returns {"projects": n, "steps": added, "closed": n[, "note": ...]} or raises TodoistError.
    """
    out = {"projects": 0, "steps": 0, "closed": 0}
    proj, me = _where(token)
    pid = proj["id"] if proj else None
    shared = bool(proj and proj.get("is_shared"))              # Todoist only takes an assignee in a shared project
    assign = shared and bool(me)
    if not proj:
        out["note"] = (f"You're not in the {PROJECT_NAME} project in Todoist yet, so these went to your Inbox. "
                       "Once you're added, your next send moves them there.")
    owner = {"assignee_id": me} if assign else {}
    where = {"project_id": pid} if pid else {}
    open_tops = None          # my open top-level tasks there, by name; fetched only if needed
    for u in projects:
        key = u["project"].lower()
        rec = known.get(key)
        rec = {"id": rec, "steps": {}} if isinstance(rec, str) else dict(rec or {"id": None, "steps": {}})
        task = _call(token, "GET", f"/tasks/{rec['id']}") if rec.get("id") else None
        if task and task.get("is_deleted"):
            task = None
        if task and _done(task):
            _call(token, "POST", f"/tasks/{task['id']}/reopen")          # still going: bring it back
        if not task:
            if open_tops is None:
                open_tops = {}
                for t in _all(token, "/tasks", **where):
                    if not t.get("parent_id") and _mine(t, me):          # never someone else's task
                        open_tops.setdefault(_norm(t.get("content")), t)
            task = open_tops.get(_norm(u["project"]))
            if not task:
                task = _call(token, "POST", "/tasks", {"content": u["project"], "description": describe(u), **where, **owner})
                if not task or not task.get("id"):
                    raise TodoistError("Todoist didn't create the task.")
                open_tops[_norm(u["project"])] = task
            rec = {"id": task["id"], "steps": {}}
        if shared and not me:      # no user id from /user: whoever made this person's task is them
            me = task.get("added_by_uid") or task.get("creator_id")
            assign, owner = bool(me), ({"assignee_id": me} if me else {})
        if pid and task.get("project_id") != pid:
            _call(token, "POST", f"/tasks/{task['id']}/move", {"project_id": pid})   # its subtasks come along
        _call(token, "POST", f"/tasks/{task['id']}", {"content": u["project"], "description": describe(u),
                                                     **(owner if not (task.get("responsible_uid") or task.get("assignee_id")) else {})})

        subs = [t for t in _all(token, "/tasks", parent_id=task["id"]) if not _done(t)]
        open_by_text = {_norm(t.get("content")): t["id"] for t in subs}
        open_ids = {t["id"] for t in subs}
        if assign:      # subtasks with no owner yet become the sender's
            for t in subs:
                if not (t.get("responsible_uid") or t.get("assignee_id")):
                    _call(token, "POST", f"/tasks/{t['id']}", owner)
        want, steps = set(), {}
        for step in u.get("nextSteps", []):
            n = _norm(step)
            if n in want:
                continue
            want.add(n)
            if n in open_by_text:
                steps[n] = rec["steps"].get(n) or open_by_text[n]
            else:
                t = _call(token, "POST", "/tasks", {"content": step, "parent_id": task["id"], **owner})
                steps[n] = (t or {}).get("id")
                out["steps"] += 1
        # steps this app added that are no longer next steps: done or dropped, so complete them
        for n, sid in rec["steps"].items():
            if n not in want and sid in open_ids:
                _call(token, "POST", f"/tasks/{sid}/close")
                out["closed"] += 1
        known[key] = {"id": task["id"], "steps": {n: s for n, s in steps.items() if s}}
        out["projects"] += 1
    return out
