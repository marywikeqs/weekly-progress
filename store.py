"""
store.py — the app's data in Supabase (Postgres), over its REST API, standard
library only. Used on Vercel, where there's no disk that survives; locally and
on Railway the apps keep using files and this module stays idle.

Three tables (create them once with the SQL in README.md):
  app_kv       one row per stored thing: key, value (jsonb), version.
               Every save says which version it read; if someone saved in
               between, the save is refused instead of overwriting theirs.
  app_changes  an append-only log of every save and delete.
  app_locks    short-lived locks, so two copies of the app don't do the same
               one-off job (sending the weekly email, syncing Todoist) at once.
Row Level Security is on with no policies: only the secret key reaches them.

Environment (the Vercel ↔ Supabase integration sets these):
  SUPABASE_URL                                   https://<project>.supabase.co
  SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY   server-only key; never sent to a browser
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

URL = (os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "").rstrip("/")
KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
ON = bool(URL and KEY)


class StoreError(OSError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _call(method: str, table: str, query: dict | None = None, body=None, prefer: str = ""):
    headers = {"apikey": KEY, "Content-Type": "application/json", "Accept": "application/json"}
    if KEY.startswith("eyJ"):            # the older service_role key is a JWT and goes here too
        headers["Authorization"] = f"Bearer {KEY}"
    if prefer:
        headers["Prefer"] = prefer
    qs = urllib.parse.urlencode(query or {}, safe='(),."*:')
    req = urllib.request.Request(f"{URL}/rest/v1/{table}" + (f"?{qs}" if qs else ""), method=method, headers=headers,
                                 data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise StoreError(f"Supabase {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise StoreError(f"Couldn't reach Supabase: {e}") from None
    return json.loads(raw) if raw else None


def _in(keys) -> str:
    return "in.(" + ",".join('"' + k.replace('"', '') + '"' for k in keys) + ")"


def get_many(keys) -> dict:
    """{key: (value, version)}; a key that isn't stored yet comes back as (None, 0)."""
    keys = list(keys)
    rows = _call("GET", "app_kv", {"select": "key,value,version", "key": _in(keys)}) or []
    found = {r["key"]: (r["value"], r["version"]) for r in rows}
    return {k: found.get(k, (None, 0)) for k in keys}


def get(key: str):
    return get_many([key])[key][0]


def save(key: str, value, version: int) -> int:
    """Store value if the row is still at `version` (0 = not stored yet). The new version, or -1 if someone saved first."""
    if not version:
        rows = _call("POST", "app_kv", {"on_conflict": "key"}, {"key": key, "value": value, "version": 1, "updated_at": _now()},
                     "resolution=ignore-duplicates,return=representation")
    else:
        rows = _call("PATCH", "app_kv", {"key": f"eq.{key}", "version": f"eq.{version}"},
                     {"value": value, "version": version + 1, "updated_at": _now()}, "return=representation")
    return rows[0]["version"] if rows else -1


def put(key: str, value) -> None:
    """Store value whatever is there (moving data in); bumps the version so nobody's older read can save over it."""
    cur = get_many([key])[key][1]
    if save(key, value, cur) == -1:
        raise StoreError(f"{key} changed while it was being replaced; run again")


def delete(key: str) -> None:
    _call("DELETE", "app_kv", {"key": f"eq.{key}"})


def log(app: str, entry: dict) -> None:
    _call("POST", "app_changes", None, {"app": app, "entry": entry}, "return=minimal")


def count_changes(app: str) -> int:
    return len(_call("GET", "app_changes", {"select": "id", "app": f"eq.{app}"}) or [])


def lock(name: str, seconds: int) -> str | None:
    """Take a lock for up to `seconds`; a token to unlock with, or None if someone else holds it."""
    _call("DELETE", "app_locks", {"name": f"eq.{name}", "expires_at": f"lt.{_now()}"})
    token = os.urandom(8).hex()
    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    rows = _call("POST", "app_locks", {"on_conflict": "name"}, {"name": name, "token": token, "expires_at": until},
                 "resolution=ignore-duplicates,return=representation")
    return token if rows else None


def unlock(name: str, token: str) -> None:
    _call("DELETE", "app_locks", {"name": f"eq.{name}", "token": f"eq.{token}"})
